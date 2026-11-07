//! The setup screens' configuration API over HTTP, against a server whose
//! machine, identities and workspaces are temporary directories.
use super::*;
use axum::body::Body as HttpBody;
use axum::http::Request;
use gb_base::shared_writes::IdentityLock;
use gb_config::secrets::Keychain;
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Duration;
use tower::ServiceExt as _;

/// Every route that writes the shared config: each compares what its screen
/// read before writing, and answers a lock held elsewhere with 503.
pub(crate) const CONFIG_WRITERS: &[(&str, &str)] = &[
    ("post", "/config/init"),
    ("put", "/config/project"),
    ("put", "/config/project/default-person"),
    ("post", "/config/members"),
    ("put", "/config/members/{name}"),
    ("delete", "/config/members/{name}"),
    ("post", "/config/members/{name}/avatar"),
    ("delete", "/identities/{name}"),
    ("put", "/transcripts/settings"),
    ("post", "/commands/files"),
    ("put", "/commands/files/{id}"),
    ("delete", "/commands/files/{id}"),
    ("post", "/commands/author/apply"),
];

/// The file ID of the shared command `hello.md`.
const HELLO: &str = "aGVsbG8ubWQ";

#[derive(Clone, Copy, PartialEq)]
enum Keys {
    Fixed,
    Locked,
    Untouchable,
}
/// The machine's keychain, as the test needs it at the moment.
struct TestKeychain(Mutex<Keys>);
impl Keychain for TestKeychain {
    fn read(&self) -> anyhow::Result<Option<String>> {
        match *self.0.lock().unwrap() {
            Keys::Fixed => Ok(Some("ExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExM=".into())),
            Keys::Locked => anyhow::bail!("the keychain is locked"),
            Keys::Untouchable => panic!("the keychain must not be read"),
        }
    }
    fn create(&self, _: &str) -> anyhow::Result<()> {
        panic!("the fixture's key exists")
    }
}

struct Fixture {
    _dir: tempfile::TempDir,
    root: PathBuf,
    api: Shared,
    token: String,
    keys: Arc<TestKeychain>,
}

fn fixture() -> Fixture {
    fixture_with(|root| Selected {
        workspace: Some(root.join("workspace")),
        ..Default::default()
    })
}

/// A server whose selection `selected` makes from the fixture's root.
fn fixture_with(selected: impl FnOnce(&Path) -> Selected) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let root = gb_base::host_path::normalize(dir.path()).unwrap();
    for directory in ["workspace", "home", "identity", "machine/data"] {
        std::fs::create_dir_all(root.join(directory)).unwrap();
    }
    let data = root.join("machine/data");
    let keys = Arc::new(TestKeychain(Mutex::new(Keys::Fixed)));
    let mut api = crate::api::Api::new(
        selected(&root),
        crate::input_files::InputFileStore::deferred(&root.join("inputs")),
        crate::auth::ServerAuth::open(&data, 8765).unwrap(),
        "instance".into(),
        data,
    );
    api.home = root.join("home");
    api.identity_root = root.join("identity");
    api.keychain = keys.clone();
    api.write_timeout = Duration::from_millis(50);
    let api = Arc::new(api);
    let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
    Fixture {
        _dir: dir,
        root,
        api,
        token: token.trim().into(),
        keys,
    }
}

impl Fixture {
    fn workspace(&self) -> PathBuf {
        self.root.join("workspace")
    }
    fn config(&self) -> PathBuf {
        self.workspace().join(".guildbotics/config")
    }
    fn identities(&self) -> PathBuf {
        self.root.join("identity/.guildbotics/config/team/members")
    }
    fn data(&self) -> PathBuf {
        self.root.join("machine/data")
    }
    /// Claude's login saved on this device as `environment login` leaves it,
    /// and a later turn's failed authentication, which only guides.
    fn claude_logged_in(&self) {
        let tool = self.data().join("agent_environment/claude");
        let login = json!({"claudeAiOauth": {"accessToken": "synthetic",
            "refreshToken": "synthetic", "expiresAt": 4_102_444_800_000_u64}});
        gb_config::credential_vault::seal(
            &self.device(),
            &tool.join("login.sealed"),
            "claude:default:claude-oauth",
            &[(".credentials.json".into(), login.to_string().into_bytes())].into(),
        )
        .unwrap();
        self.write(&tool.join("authentication-failed"), "");
    }
    fn keys(&self, keys: Keys) {
        *self.keys.0.lock().unwrap() = keys;
    }
    fn repository(&self) -> ConfigRepository {
        ConfigRepository::new(SharedWrites::new(
            self.root.join("identity"),
            Some(self.workspace()),
        ))
        .unwrap()
    }
    fn device(&self) -> gb_config::secrets::DeviceKey<'_> {
        gb_config::secrets::DeviceKey {
            machine_root: self.root.join("machine"),
            keychain: &*self.keys,
        }
    }
    fn identity_secrets(&self) -> BTreeMap<String, String> {
        self.repository()
            .identity_secrets(self.device())
            .unwrap()
            .values()
            .unwrap()
    }
    fn workspace_secrets(&self) -> BTreeMap<String, String> {
        self.repository()
            .workspace_secrets(self.device())
            .unwrap()
            .values()
            .unwrap()
    }
    fn seed_identity_secret(&self, key: &str, value: &str) {
        self.repository()
            .identity_secrets(self.device())
            .unwrap()
            .set(key, value)
            .unwrap();
    }
    fn write(&self, path: &Path, text: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    fn read(&self, path: &Path) -> Value {
        gb_config::yaml_value(&std::fs::read_to_string(path).unwrap()).unwrap()
    }
    fn request(&self, method: &str, uri: &str) -> axum::http::request::Builder {
        Request::builder()
            .method(method)
            .uri(format!("/api{uri}"))
            .header("host", "127.0.0.1:8765")
            .header("authorization", format!("Bearer {}", self.token))
    }
    async fn respond(&self, request: Request<HttpBody>) -> Response {
        crate::api::router(self.api.clone())
            .oneshot(request)
            .await
            .unwrap()
    }
    async fn call(&self, method: &str, uri: &str, body: Value) -> (StatusCode, Value) {
        self.call_in("en", method, uri, body).await
    }
    async fn call_in(
        &self,
        language: &str,
        method: &str,
        uri: &str,
        body: Value,
    ) -> (StatusCode, Value) {
        let request = self
            .request(method, uri)
            .header("content-type", "application/json")
            .header("x-guildbotics-language", language)
            .body(if body.is_null() {
                HttpBody::empty()
            } else {
                HttpBody::from(body.to_string())
            })
            .unwrap();
        answer(self.respond(request).await).await
    }
    /// Sends a form composed against a read, which names the config it was
    /// read in: the selected workspace's unless the form names another.
    async fn save(&self, method: &str, uri: &str, form: Value) -> (StatusCode, Value) {
        self.save_in("en", method, uri, form).await
    }
    async fn save_in(
        &self,
        language: &str,
        method: &str,
        uri: &str,
        mut form: Value,
    ) -> (StatusCode, Value) {
        if form.get("config_dir").is_none() {
            form["config_dir"] = json!(self.config());
        }
        self.call_in(language, method, uri, form).await
    }
    async fn get(&self, uri: &str) -> Value {
        let (status, body) = self.call("GET", uri, Value::Null).await;
        assert_eq!(status, StatusCode::OK, "{uri}: {body}");
        body
    }
    async fn init(&self) -> Value {
        self.init_with(json!({"language": "en"})).await
    }
    async fn init_with(&self, mut form: Value) -> Value {
        form["config_dir"] = json!(self.config());
        let (status, body) = self.call("POST", "/config/init", form).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        body
    }
    async fn add(&self, form: Value) -> Value {
        let (status, body) = self.save("POST", "/config/members", form).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        body
    }
    async fn upload(&self, id: &str, name: &str, content: &[u8]) -> (StatusCode, Value) {
        let mut body = format!(
            "--fixture\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: image/png\r\n\r\n"
        )
        .into_bytes();
        body.extend_from_slice(content);
        body.extend_from_slice(b"\r\n--fixture--\r\n");
        let request = self
            .request("POST", &format!("/config/members/{id}/avatar"))
            .header("content-type", "multipart/form-data; boundary=fixture")
            .body(HttpBody::from(body))
            .unwrap();
        answer(self.respond(request).await).await
    }
    fn events(&self, kind: &str) -> Vec<Value> {
        self.api
            .events
            .snapshot()
            .into_iter()
            .filter(|e| e["type"] == kind)
            .collect()
    }
}

async fn answer(response: Response) -> (StatusCode, Value) {
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 64 << 20)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes)
            .unwrap_or(Value::String(String::from_utf8_lossy(&bytes).into_owned())),
    )
}

fn agent(id: &str) -> Value {
    json!({"person_id": id, "person_name": "Test", "person_type": "agent", "is_active": true,
        "roles": ["architect"]})
}

fn bytes_under(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn walk(dir: &Path, found: &mut BTreeMap<PathBuf, Vec<u8>>) {
        for entry in std::fs::read_dir(dir).into_iter().flatten().flatten() {
            if entry.path().is_dir() {
                walk(&entry.path(), found);
            } else if !entry.path().ends_with("shared-write.lock") {
                found.insert(
                    entry.path(),
                    std::fs::read(entry.path()).unwrap_or_default(),
                );
            }
        }
    }
    let mut found = BTreeMap::new();
    walk(root, &mut found);
    found
}

fn urlencode(name: &str) -> String {
    url::form_urlencoded::byte_serialize(name.as_bytes())
        .collect::<String>()
        .replace('+', "%20")
}

#[tokio::test]
async fn config_init_writes_the_project_and_stores_api_keys_only_in_the_secret_store() {
    let f = fixture();
    let body = f
        .init_with(
            json!({"language": "en", "description": "Ours", "llm_api_type": "openai",
            "cli_agent": "codex", "provider_api_keys": {"openai": "sk-synthetic"}}),
        )
        .await;
    assert!(!body.to_string().contains("sk-synthetic"));
    assert_eq!(
        body["revisions"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        [
            "intelligences/cli_agent_mapping.yml",
            "intelligences/model_mapping.yml",
            "team/project.yml"
        ]
    );
    assert_eq!(
        f.read(&f.config().join("team/project.yml"))["description"],
        "Ours"
    );
    assert!(!f.workspace().join(".env").exists());
    assert_eq!(
        f.get("/config/project").await["provider_api_keys"]["openai"],
        true
    );
    assert_eq!(f.workspace_secrets()["OPENAI_API_KEY"], "sk-synthetic");
    assert!(f.identity_secrets().is_empty());
    for (path, bytes) in bytes_under(&f.root) {
        assert!(
            !String::from_utf8_lossy(&bytes).contains("sk-synthetic"),
            "{}",
            path.display()
        );
    }
}

#[tokio::test]
async fn init_creates_a_cli_compatible_workspace() {
    let f = fixture();
    f.init_with(json!({"language": "ja", "description": "D",
        "provider_api_keys": {"gemini": "g-key", "openai": "o-key"}}))
        .await;
    let registered = gb_workspace::selection::registered(&f.data()).unwrap();
    assert_eq!(registered, [f.workspace()]);
    for file in [
        "team/project.yml",
        "intelligences/model_mapping.yml",
        "intelligences/cli_agent_mapping.yml",
        "commands/translate.md",
        "commands/summarize.md",
        "commands/context-info.md",
        "commands/get-time-of-day.yml",
    ] {
        assert!(f.config().join(file).is_file(), "{file}");
    }
    let templates = gb_base::home::python_package().join("templates");
    for tool in ["antigravity", "claude", "codex", "copilot", "grok"] {
        let relative = format!("intelligences/cli_agents/{tool}/default.yml");
        assert_eq!(
            std::fs::read(f.config().join(&relative)).unwrap(),
            std::fs::read(templates.join(&relative)).unwrap()
        );
    }
    assert_eq!(
        std::fs::read(f.config().join("commands/translate.md")).unwrap(),
        std::fs::read(templates.join("sample_commands/ja/translate.md")).unwrap()
    );
    let project = f.read(&f.config().join("team/project.yml"));
    assert_eq!(project, json!({"language": "ja", "description": "D"}));
    gb_config::team::Team::load(&f.config(), &f.root.join("identity")).unwrap();
    let repository = ConfigRepository::new(SharedWrites::new(
        f.root.join("identity"),
        Some(f.workspace()),
    ))
    .unwrap();
    let device = || gb_config::secrets::DeviceKey {
        machine_root: f.root.join("machine"),
        keychain: &*f.keys,
    };
    let values = repository
        .workspace_secrets(device())
        .unwrap()
        .values()
        .unwrap();
    assert_eq!(values["GOOGLE_API_KEY"], "g-key");
    assert_eq!(values["OPENAI_API_KEY"], "o-key");
    assert!(
        repository
            .identity_secrets(device())
            .unwrap()
            .values()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn init_refuses_a_workspace_inside_the_exchange_or_a_grant() {
    let f = fixture();
    let inside = f.root.join("home/Documents/GuildBotics/new-workspace");
    let (status, body) = f
        .call(
            "POST",
            "/config/init",
            json!({"config_dir": inside.join(".guildbotics/config"), "language": "en"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_workspace_location");
    assert!(
        body["message"]
            .as_str()
            .unwrap()
            .contains("Documents/GuildBotics")
    );
    assert!(!inside.exists());
    assert!(
        gb_workspace::selection::registered(&f.data())
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn a_setup_refusal_answers_400_with_its_code_and_reason() {
    let f = fixture();
    let inside = f.root.join("home/Documents/GuildBotics/w");
    let (status, body) = f
        .call(
            "POST",
            "/config/init",
            json!({"config_dir": inside.join(".guildbotics/config"), "language": "en"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_workspace_location");
    assert_eq!(body["context"], json!({}));
    assert_eq!(
        body["message"],
        crate::i18n::t(
            "en",
            "safe_paths.workspace_granted",
            &[
                ("path", &inside.display().to_string()),
                (
                    "granted",
                    &f.root
                        .join("home/Documents/GuildBotics")
                        .display()
                        .to_string()
                ),
            ]
        )
    );
    assert!(!inside.exists());
    assert!(
        gb_workspace::selection::registered(&f.data())
            .unwrap()
            .is_empty()
    );
    // An invalid form registers nothing either.
    let (status, _) = f
        .call(
            "POST",
            "/config/init",
            json!({"config_dir": f.config(), "language": "fr"}),
        )
        .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(
        gb_workspace::selection::registered(&f.data())
            .unwrap()
            .is_empty()
    );
    assert!(!f.config().exists());
    // A refusal of the setup itself goes through the same answer.
    f.init().await;
    f.add(
        json!({"person_id": "h", "person_name": "H", "is_active": false, "person_type": "human"}),
    )
    .await;
    let before = std::fs::read(f.config().join("team/project.yml")).unwrap();
    let (status, body) = f
        .call(
            "PUT",
            "/config/project/default-person",
            json!({"person_id": "h"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body,
        json!({"code": "person_not_executable",
            "message": "A human member cannot be the default command executor.", "context": {}})
    );
    assert_eq!(
        std::fs::read(f.config().join("team/project.yml")).unwrap(),
        before
    );
}

#[tokio::test]
async fn the_project_is_read_and_saved_without_touching_what_the_form_does_not_name() {
    let f = fixture();
    f.init_with(
        json!({"language": "en", "llm_api_type": "openai", "cli_agent": "claude",
        "provider_api_keys": {"openai": "k"}}),
    )
    .await;
    f.write(
        &f.config().join("team/project.yml"),
        "language: en\nname: Kept\nservices:\n  ticket_service:\n    name: Local\n    tracker: acme\n",
    );
    let read = f.get("/config/project").await;
    assert_eq!(read["language"], "en");
    assert_eq!(read["llm_api_type"], "openai");
    assert_eq!(read["cli_agent"], "claude");
    assert_eq!(read["provider_api_keys"]["openai"], true);
    assert_eq!(read["provider_api_keys"]["gemini"], false);
    assert_eq!(
        read["services"]["ticket_service"],
        json!({"name": "local", "tracker": "acme"})
    );
    assert_eq!(read["config_dir"], json!(f.config()));
    let (status, _) = f
        .save(
            "PUT",
            "/config/project",
            json!({"language": "ja", "description": "New", "llm_api_type": "gemini", "cli_agent": "codex",
                "services": {"chat_service": {"name": "Local"}, "code_hosting_service": {"name": "branvil", "workspace": "acme"}}, "expected_revisions": read["revisions"]}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    let project = f.read(&f.config().join("team/project.yml"));
    assert_eq!(project["name"], "Kept");
    assert_eq!(project["language"], "ja");
    assert_eq!(project["description"], "New");
    assert_eq!(f.workspace_secrets()["OPENAI_API_KEY"], "k");
    assert_eq!(
        project["services"],
        json!({"chat_service": {"name": "local"},
            "code_hosting_service": {"name": "branvil", "workspace": "acme"}})
    );
    let again = f.get("/config/project").await;
    assert_eq!(again["services"], project["services"]);
    assert_eq!(again["llm_api_type"], "gemini");
    assert_eq!(again["provider_api_keys"]["openai"], true);
}

#[tokio::test]
async fn a_project_saved_without_services_has_none() {
    let f = fixture();
    f.init_with(json!({"language": "en", "services": {"chat_service": {"name": "local"}}}))
        .await;
    let (status, _) = f
        .save(
            "PUT",
            "/config/project",
            json!({"language": "en", "services": {}}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        f.read(&f.config().join("team/project.yml"))
            .get("services")
            .is_none()
    );
    assert_eq!(f.get("/config/project").await["services"], json!({}));
}

#[tokio::test]
async fn invalid_project_services_are_refused_before_anything_is_written() {
    let f = fixture();
    f.init().await;
    let before = bytes_under(&f.config());
    for services in [
        json!({"chat_service": {"name": "branvil"}}),
        json!({"ticket_service": {"name": "jira"}}),
        json!({"wiki_service": {"name": "local"}}),
        json!({"code_hosting_service": {}}),
    ] {
        let (status, body) = f
            .save(
                "PUT",
                "/config/project",
                json!({"language": "en", "services": services}),
            )
            .await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{services}");
        assert_eq!(body["code"], "validation_error");
        assert!(body["context"]["errors"].is_array());
    }
    assert_eq!(bytes_under(&f.config()), before);
}

#[tokio::test]
async fn a_project_save_stores_only_the_api_keys_it_is_given() {
    let f = fixture();
    f.init().await;
    let store = || f.root.join("workspace/.guildbotics/local/secrets.enc.json");
    let before = std::fs::read(store()).ok();
    let (status, _) = f
        .save("PUT", "/config/project", json!({"language": "en"}))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(std::fs::read(store()).ok(), before);
    assert!(
        f.read(&f.config().join("team/project.yml"))
            .get("services")
            .is_none()
    );
    let (status, _) = f
        .save(
            "PUT",
            "/config/project",
            json!({"language": "en", "provider_api_keys": {"openai": "token", "gemini": ""}}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    let keys = f.get("/config/project").await["provider_api_keys"].clone();
    assert_eq!(
        (&keys["openai"], &keys["gemini"]),
        (&json!(true), &json!(false))
    );
}

#[tokio::test]
async fn update_keeps_cli_agents_and_keys_and_seeds_empty_commands() {
    let f = fixture();
    f.init_with(json!({"language": "en", "provider_api_keys": {"openai": "k"}}))
        .await;
    f.write(
        &f.config().join("intelligences/cli_agents/custom.yml"),
        "mine: 1\n",
    );
    f.write(
        &f.config()
            .join("intelligences/cli_agents/codex/default.yml"),
        "edited: 1\n",
    );
    std::fs::remove_dir_all(f.config().join("commands")).unwrap();
    let (status, _) = f
        .save("PUT", "/config/project", json!({"language": "ja"}))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        std::fs::read_to_string(
            f.config()
                .join("intelligences/cli_agents/codex/default.yml")
        )
        .unwrap(),
        "edited: 1\n"
    );
    assert_eq!(
        std::fs::read_to_string(f.config().join("intelligences/cli_agents/custom.yml")).unwrap(),
        "mine: 1\n"
    );
    assert_eq!(f.workspace_secrets()["OPENAI_API_KEY"], "k");
    let templates = gb_base::home::python_package().join("templates");
    assert_eq!(
        std::fs::read(f.config().join("commands/translate.md")).unwrap(),
        std::fs::read(templates.join("sample_commands/ja/translate.md")).unwrap()
    );
    assert!(!f.workspace().join(".env").exists());
}

#[tokio::test]
async fn project_reads_before_init_answer_project_not_found() {
    let f = fixture();
    let (status, body) = f.call("GET", "/config/project", Value::Null).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "project_not_found");
    assert_eq!(body["message"], "Project config was not found.");
    assert_eq!(
        body["context"]["project"],
        json!(f.config().join("team/project.yml"))
    );
    let (status, body) = f
        .call(
            "PUT",
            "/config/project/default-person",
            json!({"person_id": ""}),
        )
        .await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::BAD_REQUEST, &json!("project_not_found"))
    );
}

#[tokio::test]
async fn a_project_read_reports_the_revisions_of_its_three_files() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/project").await;
    let keys: Vec<_> = read["revisions"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    assert_eq!(
        keys,
        [
            "intelligences/cli_agent_mapping.yml",
            "intelligences/model_mapping.yml",
            "team/project.yml"
        ]
    );
}

async fn save_description(
    f: &Fixture,
    description: &str,
    revisions: &Value,
) -> (StatusCode, Value) {
    f.save(
        "PUT",
        "/config/project",
        json!({"language": "en", "description": description, "expected_revisions": revisions}),
    )
    .await
}

#[tokio::test]
async fn a_project_save_at_the_read_revisions_applies() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/project").await;
    let (status, _) = save_description(&f, "Renamed", &read["revisions"]).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        f.read(&f.config().join("team/project.yml"))["description"],
        "Renamed"
    );
}

#[tokio::test]
async fn a_stale_project_save_is_refused_and_keeps_the_newer_content() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/project").await;
    let (status, _) = save_description(&f, "From the other machine", &read["revisions"]).await;
    assert_eq!(status, StatusCode::OK);
    let (status, body) = save_description(&f, "Stale", &read["revisions"]).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "config_changed");
    assert_eq!(body["context"]["path"], "config/team/project.yml");
    assert_eq!(
        f.read(&f.config().join("team/project.yml"))["description"],
        "From the other machine"
    );
    assert_eq!(
        body["context"]["revisions"],
        f.get("/config/project").await["revisions"]
    );
}

#[tokio::test]
async fn a_stale_file_the_screen_never_edited_refuses_the_project_save() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/project").await;
    f.write(
        &f.config().join("intelligences/cli_agent_mapping.yml"),
        "default: cli_agents/claude/default.yml\n",
    );
    let (status, body) = save_description(&f, "Changed", &read["revisions"]).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        body["context"]["path"],
        "config/intelligences/cli_agent_mapping.yml"
    );
    assert!(
        f.read(&f.config().join("team/project.yml"))
            .get("description")
            .is_none()
    );
}

#[tokio::test]
async fn a_project_save_without_revisions_applies() {
    let f = fixture();
    f.init().await;
    let (status, _) = save_description(&f, "Renamed", &json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        f.read(&f.config().join("team/project.yml"))["description"],
        "Renamed"
    );
}

#[tokio::test]
async fn a_project_save_answers_with_the_revisions_it_wrote() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/project").await;
    let (_, saved) = save_description(&f, "New", &read["revisions"]).await;
    assert_eq!(
        saved["revisions"],
        f.get("/config/project").await["revisions"]
    );
    assert_ne!(
        saved["revisions"]["team/project.yml"],
        read["revisions"]["team/project.yml"]
    );
}

#[tokio::test]
async fn saving_twice_from_one_screen_is_not_a_conflict() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/project").await;
    let (_, first) = save_description(&f, "First", &read["revisions"]).await;
    let (status, _) = save_description(&f, "Second", &first["revisions"]).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        f.read(&f.config().join("team/project.yml"))["description"],
        "Second"
    );
}

#[tokio::test]
async fn a_file_created_after_the_read_refuses_the_save() {
    let f = fixture();
    f.init().await;
    std::fs::remove_file(f.config().join("intelligences/cli_agent_mapping.yml")).unwrap();
    let read = f.get("/config/project").await;
    assert_eq!(read["revisions"]["intelligences/cli_agent_mapping.yml"], "");
    f.write(
        &f.config().join("intelligences/cli_agent_mapping.yml"),
        "default: claude\n",
    );
    let (status, _) = save_description(&f, "x", &read["revisions"]).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        std::fs::read_to_string(f.config().join("intelligences/cli_agent_mapping.yml")).unwrap(),
        "default: claude\n"
    );
}

#[tokio::test]
async fn a_revision_outside_config_is_a_bad_request() {
    let f = fixture();
    f.init().await;
    let before = bytes_under(&f.workspace());
    let (status, body) =
        save_description(&f, "x", &json!({"../state/workspace.json": "abc"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "config_revision_invalid");
    assert_eq!(bytes_under(&f.workspace()), before);
}

#[tokio::test]
async fn the_default_person_is_stored_in_the_selected_project_and_must_exist() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let (status, _) = f
        .call(
            "PUT",
            "/config/project/default-person",
            json!({"person_id": "alice"}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        f.read(&f.config().join("team/project.yml")),
        json!({"language": "en", "default_person_id": "alice"})
    );
    let before = std::fs::read(f.config().join("team/project.yml")).unwrap();
    let (status, body) = f
        .call(
            "PUT",
            "/config/project/default-person",
            json!({"person_id": "ghost"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "person_not_found");
    assert_eq!(body["message"], "Member config was not found.");
    assert_eq!(
        std::fs::read(f.config().join("team/project.yml")).unwrap(),
        before
    );
    let (status, _) = f
        .call(
            "PUT",
            "/config/project/default-person",
            json!({"person_id": "Not An Id"}),
        )
        .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn roles_are_the_template_roles_of_the_asked_language() {
    let f = fixture();
    let japanese = f.get("/config/roles?language=ja").await;
    assert_eq!(f.get("/config/roles").await, japanese);
    let ids: Vec<_> = japanese["roles"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["role_id"].as_str().unwrap().to_owned())
        .collect();
    for id in [
        "architect",
        "product",
        "customer_success",
        "growth",
        "operations",
    ] {
        assert!(ids.contains(&id.to_owned()), "{id}");
    }
    for id in ["programmer", "professional", "personal"] {
        assert!(!ids.contains(&id.to_owned()), "{id}");
    }
    let first = &japanese["roles"][0];
    assert_eq!(first["role_id"], "product");
    assert!(first["summary"].as_str().unwrap().contains("仕様"));
    assert_ne!(f.get("/config/roles?language=en").await, japanese);
}

#[tokio::test]
async fn roles_of_an_unsupported_language_are_a_validation_error() {
    let f = fixture();
    let (status, body) = f
        .call("GET", "/config/roles?language=fr", Value::Null)
        .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["code"], "validation_error");
    assert!(body["context"]["errors"].is_array());
}

#[tokio::test]
async fn a_member_is_read_saved_taken_out_of_the_workspace_and_deleted() {
    let f = fixture();
    f.init().await;
    f.add(json!({"person_id": "alice", "person_name": "Alice", "person_type": "agent", "is_active": true,
        "roles": ["architect"], "character": {"archetype": "calm"}, "chat_channels": ["C01234567"],
        "chat_channel_participation": {"C01234567": "social"}}))
        .await;
    // A secret the member holds with a provider stays with its identity.
    f.seed_identity_secret("ALICE_BRANVIL_TOKEN", "t-synthetic");
    let read = f.get("/config/members/alice").await;
    assert!(!read.to_string().contains("t-synthetic"));
    assert_eq!(read["person_id"], "alice");
    assert_eq!(read["person_name"], "Alice");
    assert_eq!(read["is_active"], true);
    assert_eq!(read["roles"], json!(["architect"]));
    assert_eq!(read["character"]["archetype"], "calm");
    assert_eq!(read["chat_channels"], json!(["C01234567"]));
    assert_eq!(
        read["chat_channel_participation"],
        json!({"C01234567": "social"})
    );
    assert_eq!(
        read["accounts"],
        json!({"branvil":{"username":"","has_token":true},"slack":{"user_id":"","has_bot_token":false,"has_app_token":false}})
    );
    let (status, _) = f
        .save(
            "PUT",
            "/config/members/alice",
            json!({"person_id": "alice", "person_name": "Alice B",
            "is_active": false, "person_type": "agent", "git_email": "a@example.com", "roles": ["reviewer"],
            "character": {"archetype": "bold"}, "chat_channels": ["C09999999"],
            "chat_channel_participation": {"C09999999": "muted"},
            "work_stop_notification_channels": ["C09999999"],
            "expected_revisions": read["revisions"]}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    let identity = f.read(&f.identities().join("alice/identity.yml"));
    assert_eq!(identity["name"], "Alice B");
    assert_eq!(
        identity["profile"],
        json!({"character": {"archetype": "bold"}})
    );
    assert_eq!(
        identity["account_info"],
        json!({"git_user": "Alice B", "git_email": "a@example.com"})
    );
    let member = f.read(&f.config().join("team/members/alice/member.yml"));
    assert_eq!(member["profile"], json!({"roles": {"reviewer": {}}}));
    assert_eq!(member["message_channels"][0]["name"], "C09999999");
    assert_eq!(
        member["message_channels"][0]["chat"],
        json!({"enabled": true, "participation": "muted", "notify_work_stops": true,
            "channel_name": "C09999999"})
    );
    assert_eq!(
        f.get("/config/members/alice").await["work_stop_notification_channels"],
        json!(["C09999999"])
    );
    let (status, _) = f.call("DELETE", "/config/members/alice", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert!(!f.config().join("team/members/alice").exists());
    assert!(f.identities().join("alice/identity.yml").is_file());
    assert_eq!(f.identity_secrets()["ALICE_BRANVIL_TOKEN"], "t-synthetic");
    assert!(
        f.workspace_secrets()
            .keys()
            .all(|k| !k.starts_with("ALICE_"))
    );
    let (status, _) = f.call("DELETE", "/identities/alice", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert!(!f.identities().join("alice").exists());
    assert!(!f.identity_secrets().contains_key("ALICE_BRANVIL_TOKEN"));
}

#[tokio::test]
async fn a_new_member_composed_in_another_workspace_is_refused_and_written_nowhere() {
    let f = fixture();
    f.init().await;
    let elsewhere = f.root.join("elsewhere/.guildbotics/config");
    let mut form = agent("new_member");
    form["config_dir"] = json!(elsewhere);
    let before = bytes_under(&f.config());
    let (status, body) = f.save("POST", "/config/members", form).await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::CONFLICT, &json!("config_changed"))
    );
    assert_eq!(body["context"]["path"], "config");
    assert_eq!(bytes_under(&f.config()), before);
    assert!(!f.root.join("elsewhere").exists());
    // The same form, composed in the selected workspace, is added there.
    f.add(agent("new_member")).await;
    assert!(
        f.config()
            .join("team/members/new_member/member.yml")
            .is_file()
    );
}

#[tokio::test]
async fn a_member_without_accounts_is_saved_with_only_its_git_user() {
    let f = fixture();
    f.init().await;
    f.add(json!({"person_id": "local-agent", "person_name": "Local Agent", "is_active": false, "person_type": ""}))
        .await;
    let identity = f.read(&f.identities().join("local-agent/identity.yml"));
    assert_eq!(identity["person_id"], "local-agent");
    assert!(identity.get("person_type").is_none());
    assert_eq!(identity["account_info"], json!({"git_user": "Local Agent"}));
    assert!(!f.workspace().join(".env").exists());
}

#[tokio::test]
async fn a_roles_only_save_keeps_the_accounts_no_model_shows() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let path = f.identities().join("alice/identity.yml");
    f.write(
        &path,
        "person_id: alice\nname: Test\nperson_type: agent\naccount_info:\n  git_user: Test\n  branvil_username: alice-bot\n  slack_user_id: U0ALICE\n",
    );
    let read = f.get("/config/members/alice").await;
    assert_eq!(
        read["accounts"],
        json!({"branvil":{"username":"alice-bot","has_token":false},"slack":{"user_id":"U0ALICE","has_bot_token":false,"has_app_token":false}})
    );
    let mut form = agent("alice");
    form["roles"] = json!(["reviewer"]);
    form["expected_revisions"] = read["revisions"].clone();
    let (status, body) = f.save("PUT", "/config/members/alice", form).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let account_info = &f.read(&path)["account_info"];
    assert_eq!(account_info["branvil_username"], "alice-bot");
    assert_eq!(account_info["slack_user_id"], "U0ALICE");
}

#[tokio::test]
async fn reading_an_unknown_member_answers_person_not_found() {
    let f = fixture();
    let (status, body) = f.call("GET", "/config/members/alice", Value::Null).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "person_not_found");
    assert_eq!(body["message"], "Member config was not found.");
}

#[tokio::test]
async fn saving_a_member_under_another_id_is_refused() {
    let f = fixture();
    let (status, body) = f
        .save(
            "PUT",
            "/config/members/alice",
            json!({"person_id": "bob", "person_type": "agent", "is_active": false, "person_name": "B"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body,
        json!({"code": "person_id_mismatch", "message": "person_id must match the path parameter.",
            "context": {}})
    );
    assert!(!f.identities().exists());
}

#[tokio::test]
async fn a_member_save_under_another_id_is_refused() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let (status, body) = f
        .save(
            "PUT",
            "/config/members/alice",
            json!({"person_id": "alice2", "person_type": "agent", "is_active": false, "person_name": "A"}),
        )
        .await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::BAD_REQUEST, &json!("person_id_mismatch"))
    );
    assert!(!f.identities().join("alice2").exists());
}

#[tokio::test]
async fn a_member_read_reports_both_revisions_and_a_stale_save_is_refused() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let read = f.get("/config/members/alice").await;
    assert_eq!(read["config_dir"], json!(f.config()));
    assert_eq!(
        read["revisions"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        [
            "team/members/alice/identity.yml",
            "team/members/alice/member.yml"
        ]
    );
    let save = |name: &str| json!({"person_id": "alice", "person_type": "agent", "is_active": false, "person_name": name, "expected_revisions": read["revisions"]});
    let (status, _) = f
        .save(
            "PUT",
            "/config/members/alice",
            save("From the other machine"),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    let (status, body) = f.save("PUT", "/config/members/alice", save("Stale")).await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::CONFLICT, &json!("config_changed"))
    );
    assert_eq!(
        body["context"]["path"],
        "config/team/members/alice/identity.yml"
    );
    assert_eq!(
        f.read(&f.identities().join("alice/identity.yml"))["name"],
        "From the other machine"
    );
}

#[tokio::test]
async fn a_member_save_answers_with_both_files_revisions() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let read = f.get("/config/members/alice").await;
    let (_, saved) = f
        .save(
            "PUT",
            "/config/members/alice",
            json!({"person_id": "alice", "person_type": "agent", "is_active": false, "person_name": "A", "expected_revisions": read["revisions"]}),
        )
        .await;
    assert_eq!(
        saved["revisions"],
        f.get("/config/members/alice").await["revisions"]
    );
    let (status, _) = f
        .save(
            "PUT",
            "/config/members/alice",
            json!({"person_id": "alice", "person_type": "agent", "is_active": false, "person_name": "B", "expected_revisions": saved["revisions"]}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn a_member_change_after_the_read_refuses_the_forms_save() {
    for changed in ["identity", "member"] {
        let f = fixture();
        f.init().await;
        f.add(agent("alice")).await;
        let read = f.get("/config/members/alice").await;
        let path = if changed == "identity" {
            f.identities().join("alice/identity.yml")
        } else {
            f.config().join("team/members/alice/member.yml")
        };
        f.write(&path, "is_active: true\n");
        let (status, _) = f
            .save(
                "PUT",
                "/config/members/alice",
                json!({"person_id": "alice", "person_type": "agent", "is_active": false, "person_name": "Form",
                    "expected_revisions": read["revisions"]}),
            )
            .await;
        assert_eq!(status, StatusCode::CONFLICT, "{changed}");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "is_active: true\n");
    }
}

#[tokio::test]
async fn a_change_right_after_a_member_save_refuses_the_next_save() {
    for changed in ["identity", "member"] {
        let f = fixture();
        f.init().await;
        let saved = f.add(agent("alice")).await;
        let path = if changed == "identity" {
            f.identities().join("alice/identity.yml")
        } else {
            f.config().join("team/members/alice/member.yml")
        };
        f.write(&path, "is_active: true\n");
        let (status, _) = f
            .save(
                "PUT",
                "/config/members/alice",
                json!({"person_id": "alice", "person_type": "agent", "is_active": false, "person_name": "Form",
                    "expected_revisions": saved["revisions"]}),
            )
            .await;
        assert_eq!(status, StatusCode::CONFLICT, "{changed}");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "is_active: true\n");
    }
}

#[tokio::test]
async fn deleting_an_identity_answers_with_what_it_left() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let (status, body) = f.call("DELETE", "/identities/alice", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["revisions"],
        json!({"team/members/alice/identity.yml": "", "team/members/alice/member.yml": ""})
    );
}

#[tokio::test]
async fn a_member_save_keeps_trimmed_task_schedules_and_drops_blank_ones() {
    let f = fixture();
    f.init().await;
    let mut form = agent("alice");
    form["task_schedules"] = json!([
        {"command": "run-it", "schedules": ["0 9 * * 1"]},
        {"command": "  run-it  ", "schedules": ["   ", "", "  0 9 * * 1  ", "\t"]},
    ]);
    f.add(form).await;
    assert_eq!(
        f.read(&f.config().join("team/members/alice/member.yml"))["task_schedules"],
        json!([{"command": "run-it", "schedules": ["0 9 * * 1"]},
            {"command": "run-it", "schedules": ["0 9 * * 1"]}])
    );
}

#[tokio::test]
async fn a_member_save_refuses_a_schedule_without_five_fields_or_a_command() {
    let f = fixture();
    f.init().await;
    for schedule in [
        json!({"command": "x", "schedules": ["0 9 * * 1 7"]}),
        json!({"command": "x", "schedules": ["0 9 * *"]}),
        json!({"command": "x", "schedules": ["* * * *"]}),
        json!({"command": "x", "schedules": ["*"]}),
        json!({"command": "   ", "schedules": ["0 9 * * 1"]}),
    ] {
        let mut form = agent("alice");
        form["task_schedules"] = json!([schedule]);
        let (status, body) = f.save("POST", "/config/members", form).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{schedule}");
        assert_eq!(body["code"], "validation_error");
        let reason = body["context"]["errors"][0]["msg"].as_str().unwrap();
        assert!(
            reason.contains("five-field") || reason.contains("command is required"),
            "{reason}"
        );
    }
    assert!(!f.identities().join("alice").exists());
}

#[tokio::test]
async fn member_form_normalizes_channels_policies_and_work_stop_destinations() {
    let f = fixture();
    f.init().await;
    let mut form = agent("alice");
    form["chat_channels"] = json!([" #general ", "  ", "random"]);
    form["chat_channel_participation"] = json!({"#general": "social", "random": "muted"});
    form["work_stop_notification_channels"] = json!([" #general ", "deleted", " "]);
    f.add(form).await;
    let member = f.read(&f.config().join("team/members/alice/member.yml"));
    let channels = member["message_channels"].as_array().unwrap();
    assert_eq!(channels.len(), 2);
    assert_eq!(channels[0]["name"], "general");
    assert_eq!(channels[0]["chat"]["participation"], "social");
    assert_eq!(channels[0]["chat"]["notify_work_stops"], true);
    assert_eq!(channels[1]["chat"]["notify_work_stops"], false);
    assert_eq!(channels[1]["chat"]["participation"], "muted");
    let read = f.get("/config/members/alice").await;
    assert_eq!(read["work_stop_notification_channels"], json!(["general"]));
    let mut cleared = agent("alice");
    cleared["chat_channels"] = json!(["general", "random"]);
    cleared["work_stop_notification_channels"] = json!([]);
    cleared["expected_revisions"] = read["revisions"].clone();
    let (status, _) = f.save("PUT", "/config/members/alice", cleared).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        f.get("/config/members/alice").await["work_stop_notification_channels"],
        json!([])
    );
}

#[tokio::test]
async fn chat_channels_fall_back_to_names_without_an_id_reading_chat() {
    for services in [json!({}), json!({"chat_service": {"name": "local"}})] {
        let f = fixture();
        f.init_with(json!({"language": "en", "services": services}))
            .await;
        let mut form = agent("alice");
        form["chat_channels"] = json!(["#C0123456789"]);
        f.add(form).await;
        let member = f.read(&f.config().join("team/members/alice/member.yml"));
        let channel = &member["message_channels"][0];
        assert_eq!(channel["channel_info"], json!({}));
        assert_eq!(
            channel["chat"],
            json!({"enabled": true, "participation": "strict", "notify_work_stops": false,
                "channel_name": "C0123456789"})
        );
    }
}

#[tokio::test]
async fn a_new_member_whose_secret_prefix_collides_is_refused_with_the_colliding_ids() {
    let f = fixture();
    f.init().await;
    // Identities of another workspace count as well.
    for id in ["alice-bak", "bob"] {
        f.write(
            &f.identities().join(format!("{id}/identity.yml")),
            &format!("person_id: {id}\nname: X\n"),
        );
    }
    let before = (bytes_under(&f.identities()), bytes_under(&f.config()));
    for language in ["en", "ja"] {
        let (status, body) = f
            .save_in(language, "POST", "/config/members", agent("alice_bak"))
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["code"], "person_env_prefix_conflict");
        assert_eq!(
            body["message"],
            crate::i18n::t(
                language,
                "member_config.target_prefix_conflict",
                &[("person_id", "alice_bak"), ("members", "alice-bak")]
            )
        );
    }
    assert_eq!(
        (bytes_under(&f.identities()), bytes_under(&f.config())),
        before
    );
}

#[tokio::test]
async fn a_member_stored_under_no_valid_id_is_listed_read_and_removed() {
    let names: &[&str] = if cfg!(windows) {
        &["Alice", "alice.bak", "alice backup", "あいこ"]
    } else {
        &[
            "con",
            "aux",
            "nul",
            "Alice",
            "alice.bak",
            "alice backup",
            "あいこ",
        ]
    };
    for (name, identity) in names.iter().flat_map(|n| [(n, true), (n, false)]) {
        let f = fixture();
        f.init().await;
        f.add(agent("aiko")).await;
        f.write(
            &f.config().join(format!("team/members/{name}/member.yml")),
            "is_active: true\n",
        );
        let identity_file = f.identities().join(format!("{name}/identity.yml"));
        if identity {
            f.write(&identity_file, &format!("person_id: {name}\nname: X\n"));
        }
        let team = f.get("/team").await;
        let listed: Vec<_> = team["members"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["person_id"].clone())
            .collect();
        assert!(listed.contains(&json!(name)), "{name}: {team}");
        assert!(
            team["problem"]
                .as_str()
                .unwrap()
                .contains(&identity_file.display().to_string()),
            "{name}: {team}"
        );
        let uri = format!("/config/members/{}", urlencode(name));
        assert_eq!(f.get(&uri).await["person_id"], json!(name));
        let (status, _) = f.call("DELETE", &uri, Value::Null).await;
        assert_eq!(status, StatusCode::OK, "{name}");
        assert!(!f.config().join(format!("team/members/{name}")).exists());
        assert_eq!(f.get("/team").await["problem"], "", "{name}");
    }
}

#[tokio::test]
async fn an_identity_naming_another_member_is_repaired_by_saving_it() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    f.write(
        &f.identities().join("alice/identity.yml"),
        "person_id: bob\nname: Bob\n",
    );
    assert_ne!(f.get("/team").await["problem"], "");
    let read = f.get("/config/members/alice").await;
    let (status, _) = f
        .save(
            "PUT",
            "/config/members/alice",
            json!({"person_id": "alice", "person_type": "agent", "is_active": false, "person_name": "Alice", "expected_revisions": read["revisions"]}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        f.read(&f.identities().join("alice/identity.yml"))["person_id"],
        "alice"
    );
    assert_eq!(f.get("/team").await["problem"], "");
}

#[tokio::test]
async fn invalid_member_files_stay_listed_and_can_be_saved_or_removed() {
    for (broken, content) in [
        ("member", ""),
        ("member", "[broken]"),
        ("member", "is_active: ["),
        ("member", "routine_commands: 123\nprofile: []\n"),
        ("member", "name: Misplaced\n"),
        ("identity", ""),
        ("identity", "person_id: ["),
        (
            "identity",
            "person_id: broken\naccount_info: []\nprofile: []\n",
        ),
        ("identity", "person_id: broken\nis_active: true\n"),
    ] {
        for repair in ["save", "remove"] {
            let f = fixture();
            f.init().await;
            f.add(agent("broken")).await;
            let path = if broken == "member" {
                f.config().join("team/members/broken/member.yml")
            } else {
                f.identities().join("broken/identity.yml")
            };
            f.write(&path, content);
            let team = f.get("/team").await;
            assert!(
                team["members"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|m| m["person_id"] == "broken"),
                "{content}: {team}"
            );
            assert!(
                team["problem"]
                    .as_str()
                    .unwrap()
                    .contains(&path.display().to_string()),
                "{content}: {team}"
            );
            let read = f.get("/config/members/broken").await;
            let (status, body) = if repair == "save" {
                f.save(
                    "PUT",
                    "/config/members/broken",
                    json!({"person_id": "broken", "person_type": "agent", "is_active": false, "person_name": "B",
                        "expected_revisions": read["revisions"]}),
                )
                .await
            } else {
                f.call("DELETE", "/config/members/broken", Value::Null)
                    .await
            };
            assert_eq!(status, StatusCode::OK, "{content} {repair}: {body}");
            assert_eq!(f.get("/team").await["problem"], "", "{content} {repair}");
        }
    }
}

#[tokio::test]
async fn a_member_directory_without_member_yml_is_not_a_member() {
    let f = fixture();
    f.init().await;
    std::fs::create_dir_all(f.config().join("team/members/alice.bak")).unwrap();
    let team = f.get("/team").await;
    assert_eq!(team["members"], json!([]));
    assert_eq!(team["problem"], "");
}

#[tokio::test]
async fn reading_a_member_needs_its_stored_member_file() {
    let f = fixture();
    f.init().await;
    for name in ["con", "Alice", "alice.bak", "alice backup", "あいこ"] {
        let directory = f.config().join(format!("team/members/{name}"));
        std::fs::create_dir_all(&directory).unwrap();
        let uri = format!("/config/members/{}", urlencode(name));
        let (status, _) = f.call("GET", &uri, Value::Null).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{name}");
        assert_eq!(std::fs::read_dir(&directory).unwrap().count(), 0);
    }
}

#[tokio::test]
async fn colliding_identities_are_removed_without_touching_shared_secrets() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice-bak")).await;
    f.write(
        &f.identities().join("alice_bak/identity.yml"),
        "person_id: alice_bak\nname: Other\n",
    );
    f.seed_identity_secret("ALICE_BAK_BRANVIL_TOKEN", "shared");
    let sealed = f.root.join("identity/.guildbotics/local/secrets.enc.json");
    let before = std::fs::read(&sealed).unwrap();
    f.keys(Keys::Untouchable);
    let read = f.get("/config/members/alice-bak").await;
    assert!(
        read["accounts"]["branvil"]["has_token"] == false
            && read["accounts"]["slack"]["has_bot_token"] == false,
        "{read}"
    );
    let (status, body) = f.call("DELETE", "/identities/alice_bak", Value::Null).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(std::fs::read(&sealed).unwrap(), before);
    assert!(f.identities().join("alice-bak").exists());
    f.keys(Keys::Fixed);
    assert_eq!(f.identity_secrets()["ALICE_BAK_BRANVIL_TOKEN"], "shared");
    assert_eq!(f.get("/team").await["problem"], "");
}

#[tokio::test]
async fn a_secret_store_that_cannot_be_used_answers_503_with_its_reason() {
    for language in ["en", "ja"] {
        let f = fixture();
        f.init_with(json!({"language": "en", "provider_api_keys": {"openai": "sk-synthetic"}}))
            .await;
        f.keys(Keys::Locked);
        let (status, body) = f
            .call_in(language, "GET", "/config/project", Value::Null)
            .await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body["code"], "secret_store_unavailable");
        assert_eq!(body["context"], json!({}));
        let message = body["message"].as_str().unwrap();
        assert!(!message.contains("sk-synthetic") && !message.contains("%{"));
        let empty = crate::i18n::t(
            language,
            "app_api.errors.secret_store_unavailable",
            &[("reason", "")],
        );
        assert!(message.starts_with(empty.trim_end()), "{message}");
        let (status, body) = f
            .save_in(
                language,
                "PUT",
                "/config/project",
                json!({"language": "en", "provider_api_keys": {"openai": "new"}}),
            )
            .await;
        assert_eq!(
            (status, &body["code"]),
            (
                StatusCode::SERVICE_UNAVAILABLE,
                &json!("secret_store_unavailable")
            )
        );
        // Creating a project stores its keys too.
        let other = fixture();
        other.keys(Keys::Locked);
        let (status, body) = other
            .call_in(language, "POST", "/config/init",
                json!({"config_dir": other.config(), "language": "en", "provider_api_keys": {"openai": "k"}}))
            .await;
        assert_eq!(
            (status, &body["code"]),
            (
                StatusCode::SERVICE_UNAVAILABLE,
                &json!("secret_store_unavailable")
            )
        );
        assert!(!other.config().join("team/project.yml").exists());
    }
}

#[tokio::test]
async fn a_member_without_an_avatar_answers_avatar_not_found() {
    let f = fixture();
    let (status, body) = f
        .call("GET", "/config/members/alice/avatar", Value::Null)
        .await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::NOT_FOUND, &json!("avatar_not_found"))
    );
}

#[tokio::test]
async fn the_avatar_is_served_from_the_identity_without_caching() {
    let f = fixture();
    let picture = b"\x89PNG synthetic";
    std::fs::create_dir_all(f.identities().join("alice")).unwrap();
    std::fs::write(f.identities().join("alice/avatar.png"), picture).unwrap();
    let response = f
        .respond(
            f.request("GET", "/config/members/alice/avatar")
                .body(HttpBody::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["cache-control"], "no-cache");
    assert_eq!(response.headers()["content-type"], "image/png");
    assert!(response.headers().contains_key("x-content-type-options"));
    let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .unwrap();
    assert_eq!(&bytes[..], picture);
}

#[tokio::test]
async fn the_avatar_is_served_to_a_browser_session_cookie() {
    let f = fixture();
    std::fs::create_dir_all(f.identities().join("alice")).unwrap();
    std::fs::write(f.identities().join("alice/avatar.png"), b"png").unwrap();
    let link = f.api.auth.entry_link("/");
    let code = link.url.rsplit('#').next().unwrap();
    let (cookie, _csrf) = f.api.auth.start_session(code).unwrap().unwrap();
    let request = Request::get("/api/config/members/alice/avatar")
        .header("host", "127.0.0.1:8765")
        .header("cookie", format!("{}={cookie}", f.api.auth.cookie))
        .body(HttpBody::empty())
        .unwrap();
    let response = f.respond(request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .unwrap();
    assert_eq!(&bytes[..], b"png");
}

#[tokio::test]
async fn the_avatar_refuses_a_request_without_a_credential_even_with_a_token_in_the_query() {
    let f = fixture();
    for uri in [
        "/api/config/members/alice/avatar".to_owned(),
        format!("/api/config/members/alice/avatar?token={}", f.token),
    ] {
        let request = Request::get(uri)
            .header("host", "127.0.0.1:8765")
            .body(HttpBody::empty())
            .unwrap();
        let (status, body) = answer(f.respond(request).await).await;
        assert_eq!(
            (status, &body["code"]),
            (StatusCode::UNAUTHORIZED, &json!("unauthorized"))
        );
    }
}

#[tokio::test]
async fn an_uploaded_avatar_replaces_the_identitys_and_answers_its_mtime() {
    let f = fixture();
    f.write(&f.identities().join("alice/avatar.gif"), "old");
    let (status, body) = f.upload("alice", "avatar.png", b"new picture").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let path = f.identities().join("alice/avatar.png");
    assert_eq!(std::fs::read(&path).unwrap(), b"new picture");
    assert!(!f.identities().join("alice/avatar.gif").exists());
    let mtime = std::fs::metadata(&path)
        .unwrap()
        .modified()
        .unwrap()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    assert_eq!(body["avatar_timestamp"], mtime);
}

#[tokio::test]
async fn an_avatar_over_four_mib_is_refused_and_not_stored() {
    let f = fixture();
    let (status, body) = f
        .upload("alice", "avatar.png", &vec![0; gb_setup::AVATAR_LIMIT + 1])
        .await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::BAD_REQUEST, &json!("avatar_invalid"))
    );
    assert_eq!(body["message"], "Avatar file is too large (max 4 MB).");
    assert!(gb_setup::avatar_in(&f.root.join("identity"), "alice").is_none());
}

#[tokio::test]
async fn transcript_settings_report_defaults_and_usage() {
    let f = fixture();
    assert_eq!(
        f.get("/transcripts/settings").await,
        json!({"detail": "standard", "retention_days": 30,
            "sessions_dir": f.workspace().join(".guildbotics/local/run/sessions"),
            "total_size_bytes": 0, "index_size_bytes": 0, "index_rewrite_threshold_bytes": 8388608,
            "memory_size_bytes": 0, "memory_max_size_bytes": 8388608})
    );
    f.write(
        &f.workspace()
            .join(".guildbotics/local/run/sessions/a.jsonl"),
        "12345",
    );
    f.write(
        &f.workspace()
            .join(".guildbotics/local/run/diagnostics.jsonl"),
        "123",
    );
    for name in ["aiko.jsonl", "yuki.jsonl"] {
        f.write(
            &f.workspace()
                .join(".guildbotics/state/documents/memory_events")
                .join(name),
            "12",
        );
    }
    let usage = f.get("/transcripts/settings").await;
    assert_eq!(
        (
            &usage["total_size_bytes"],
            &usage["index_size_bytes"],
            &usage["memory_size_bytes"],
            &usage["memory_max_size_bytes"]
        ),
        (&json!(5), &json!(3), &json!(4), &json!(16777216))
    );
}

#[tokio::test]
async fn transcript_settings_save_writes_config_and_answers_the_new_settings() {
    let f = fixture();
    let (status, body) = f
        .call(
            "PUT",
            "/transcripts/settings",
            json!({"detail": "full", "retention_days": 14}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        (&body["detail"], &body["retention_days"]),
        (&json!("full"), &json!(14))
    );
    assert_eq!(
        f.read(&f.config().join("transcripts.yml")),
        json!({"detail": "full", "retention_days": 14})
    );
    let (status, body) = f
        .call(
            "PUT",
            "/transcripts/settings",
            json!({"detail": "verbose", "retention_days": 14}),
        )
        .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["code"], "validation_error");
    assert_eq!(body["message"], "Request validation failed.");
    assert!(body["context"]["errors"][0]["msg"].is_string(), "{body}");
    f.api.select(Selected::default());
    let (status, body) = f
        .call(
            "PUT",
            "/transcripts/settings",
            json!({"detail": "full", "retention_days": 14}),
        )
        .await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::CONFLICT, &json!("workspace_not_configured"))
    );
    assert_eq!(
        body["message"],
        "No workspace is selected, so there are no transcript settings."
    );
    let (status, body) = f.call("GET", "/transcripts/settings", Value::Null).await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::CONFLICT, &json!("workspace_not_configured"))
    );
}

#[tokio::test]
async fn transcript_settings_are_read_and_saved_in_the_workspace_config() {
    let f = fixture();
    let (status, body) = f
        .call(
            "PUT",
            "/transcripts/settings",
            json!({"detail": "full", "retention_days": 14}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, f.get("/transcripts/settings").await);
    f.write(
        &f.config().join("transcripts.yml"),
        "detail: FULL\nretention_days: 0\n",
    );
    let read = f.get("/transcripts/settings").await;
    assert_eq!(
        (&read["detail"], &read["retention_days"]),
        (&json!("full"), &json!(30))
    );
}

#[tokio::test]
async fn transcript_retention_outside_one_to_3650_days_is_refused() {
    let f = fixture();
    for days in [0, 3651] {
        let (status, body) = f
            .call(
                "PUT",
                "/transcripts/settings",
                json!({"detail": "full", "retention_days": days}),
            )
            .await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{days}");
        assert_eq!(body["code"], "validation_error");
        assert!(!f.config().join("transcripts.yml").exists());
    }
    for days in [1, 3650] {
        let (status, _) = f
            .call(
                "PUT",
                "/transcripts/settings",
                json!({"detail": "standard", "retention_days": days}),
            )
            .await;
        assert_eq!(status, StatusCode::OK, "{days}");
    }
}

#[tokio::test]
async fn team_lists_members_with_sorted_roles_and_the_default_it_runs_as() {
    let f = fixture();
    f.init().await;
    let mut form = agent("aiko");
    form["roles"] = json!(["reviewer", "architect", "developer"]);
    f.add(form).await;
    let mut idle = agent("bob");
    idle["is_active"] = json!(false);
    idle["roles"] = json!([]);
    f.add(idle).await;
    let team = f.get("/team").await;
    assert_eq!(
        team["members"][0]["roles"],
        json!(["architect", "developer", "reviewer"])
    );
    assert_eq!(team["members"][1]["is_active"], false);
    assert_eq!(team["members"][1]["roles"], json!([]));
    assert_eq!(team["default_person_id"], "aiko");
    // Without an active member, nobody runs a command by default.
    let lone = fixture();
    lone.init().await;
    let mut idle = agent("bob");
    idle["is_active"] = json!(false);
    lone.add(idle).await;
    assert_eq!(lone.get("/team").await["default_person_id"], "");
}

#[tokio::test]
async fn health_names_the_service_the_selected_workspace_and_the_machine() {
    let f = fixture();
    let health = f.get("/health").await;
    assert_eq!(health["status"], "ok");
    assert_eq!(health["service_instance_id"], "instance");
    assert_eq!(health["workspace"], json!(f.workspace()));
    let device = gb_workspace::identity::ensure_device_identity(&f.data()).unwrap();
    assert_eq!(health["machine_name"], device.display_name);
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let (status, _) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::OK);
    let health = f.get("/health").await;
    assert_eq!(health["workspace"], json!(other));
    assert_eq!(
        health["workspace_id"],
        gb_workspace::identity::ensure_workspace_identity(&other)
            .unwrap()
            .workspace_id
    );
}

#[tokio::test]
async fn config_status_without_a_project_or_a_workspace_reports_absence() {
    let f = fixture();
    let status = f.get("/config/status").await;
    assert_eq!(status["project_file_exists"], false);
    assert_eq!(status["workspace"], json!(f.workspace()));
    assert_eq!(
        status["workspace_state_dir"],
        json!(f.workspace().join(".guildbotics/state"))
    );
    f.api.select(Selected::default());
    let status = f.get("/config/status").await;
    for key in [
        "workspace",
        "config_dir",
        "project_file",
        "storage_dir",
        "workspace_state_dir",
        "workspace_local_dir",
    ] {
        assert!(status[key].is_null(), "{key}");
    }
    assert_eq!(status["project_file_exists"], false);
}

#[tokio::test]
async fn a_workspace_switch_selects_it_and_announces_workspace_changed() {
    let f = fixture();
    let other = f.root.join("other");
    f.write(
        &other.join(".guildbotics/config/team/project.yml"),
        "language: en\n",
    );
    let (status, body) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["workspace"], json!(other));
    assert_eq!(body["config_dir"], json!(other.join(".guildbotics/config")));
    assert_eq!(
        body["project_file"],
        json!(other.join(".guildbotics/config/team/project.yml"))
    );
    assert_eq!(body["project_file_exists"], true);
    assert_eq!(body["storage_dir"], json!(other));
    assert_eq!(
        body["workspace_state_dir"],
        json!(other.join(".guildbotics/state"))
    );
    assert_eq!(
        body["workspace_local_dir"],
        json!(other.join(".guildbotics/local"))
    );
    assert_eq!(f.get("/config/status").await, body);
    let changed = f.events("workspace.changed");
    assert_eq!(changed.len(), 1);
    assert_eq!(changed[0]["payload"], json!({"workspace": other}));
    assert_eq!(
        f.read(&f.data().join("active-workspace.json")),
        json!({"workspace": other, "config_dir": other.join(".guildbotics/config")})
    );
}

#[tokio::test]
async fn a_switch_whose_request_goes_away_still_ends_opened_and_announced() {
    let f = fixture();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    // The registry's lock keeps the switch persisting while its client
    // goes away.
    let registry =
        gb_base::files::FileLock::hold(&f.data().join("workspaces.lock"), Duration::from_secs(1))
            .unwrap();
    let request = f.call("POST", "/workspace", json!({"workspace_dir": other}));
    assert!(
        tokio::time::timeout(Duration::from_millis(300), request)
            .await
            .is_err()
    );
    assert!(f.api.admit().is_none(), "the switch outlives its request");
    assert_eq!(f.api.selected().workspace, Some(f.workspace()));
    drop(registry);
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while f.api.admit().is_none() {
        assert!(
            std::time::Instant::now() < deadline,
            "the switch never ended"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert_eq!(f.api.selected().workspace, Some(other.clone()));
    assert_eq!(f.get("/config/status").await["workspace"], json!(other));
    assert_eq!(
        f.read(&f.data().join("active-workspace.json"))["workspace"],
        json!(other)
    );
    assert_eq!(
        f.events("workspace.changed")[0]["payload"],
        json!({"workspace": other})
    );
}

/// Serves the fixture's API on a loopback port until the token is
/// cancelled, as the server does.
async fn serving(
    f: &Fixture,
) -> (
    u16,
    tokio_util::sync::CancellationToken,
    tokio::task::JoinHandle<()>,
) {
    let (server, port) = crate::server::Server::for_test(f.api.clone()).await;
    let stop = tokio_util::sync::CancellationToken::new();
    let served = tokio::spawn({
        let stop = stop.clone();
        async move {
            let (_held, result) = server
                .serve(
                    async move { stop.cancelled().await },
                    std::future::pending(),
                )
                .await;
            result.unwrap();
        }
    });
    (port, stop, served)
}

/// Sends `body` to `uri` of the server on `port` and goes away before it
/// is answered.
async fn leave(f: &Fixture, port: u16, uri: &str, body: Value) {
    let sent = reqwest::Client::new()
        .post(format!("http://127.0.0.1:{port}/api{uri}"))
        .header("host", "127.0.0.1:8765")
        .bearer_auth(&f.token)
        .json(&body)
        .timeout(Duration::from_millis(300))
        .send()
        .await;
    assert!(sent.unwrap_err().is_timeout(), "{uri} answered");
}

#[tokio::test]
async fn a_stopping_server_waits_for_the_switch_its_client_left() {
    let f = fixture();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let registry =
        gb_base::files::FileLock::hold(&f.data().join("workspaces.lock"), Duration::from_secs(1))
            .unwrap();
    let (port, stop, served) = serving(&f).await;
    leave(&f, port, "/workspace", json!({"workspace_dir": other})).await;
    assert!(f.api.admit().is_none(), "the switch is under way");
    stop.cancel();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(!served.is_finished(), "the server waits for the switch");
    drop(registry);
    served.await.unwrap();
    assert_eq!(f.api.selected().workspace, Some(other.clone()));
    // The last selection's system transcript is the one finished.
    let finished = bytes_under(&other.join(".guildbotics/local/run/sessions"))
        .values()
        .any(|bytes| String::from_utf8_lossy(bytes).contains("system.finished"));
    assert!(finished);
}

#[tokio::test]
async fn a_service_the_servers_work_starts_after_its_stop_is_stopped_too() {
    let (f, selected) = running();
    let begin = tokio_util::sync::CancellationToken::new();
    let late = f.api.work.spawn({
        let (begin, selected) = (begin.clone(), selected.clone());
        async move {
            begin.cancelled().await;
            assert!(selected.service.start().await.unwrap().running);
            // A turn of the late service, which ends only when cancelled.
            let turn = selected.service.execution.generation();
            selected
                .service
                .execution
                .spawn(|generation| async move { generation.cancelled.cancelled().await })
                .unwrap();
            turn
        }
    });
    let (_, stop, served) = serving(&f).await;
    let stopped = selected.service.execution.generation();
    stop.cancel();
    // The start begins once the server's stop has begun.
    while !stopped.draining.is_cancelled() {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    begin.cancel();
    tokio::time::timeout(Duration::from_secs(30), served)
        .await
        .expect("the server cancels what the late start runs")
        .unwrap();
    assert!(late.await.unwrap().cancelled.is_cancelled());
    assert_ne!(selected.service.service.status().await.state, "running");
}

/// An `Api` to build a selection with before the fixture's own exists.
fn placeholder(root: &Path) -> crate::api::Api {
    crate::api::Api::new(
        Selected::default(),
        crate::input_files::InputFileStore::deferred(&root.join("inputs")),
        crate::auth::ServerAuth::open(&root.join("placeholder"), 8765).unwrap(),
        "instance".into(),
        root.join("machine/data"),
    )
}

#[tokio::test]
async fn a_workspace_switch_stops_the_service_selects_the_workspace_and_announces_it() {
    let f =
        fixture_with(|root| Selected::open(Some(root.join("workspace")), &placeholder(root), None));
    let (one, two) = (f.root.join("one"), f.root.join("two"));
    f.write(&one.join(".env"), "");
    f.write(&two.join(".env"), "HOME=/elsewhere\nUSERPROFILE=x\n");
    for workspace in [&one, &two] {
        let (status, body) = f
            .call("POST", "/workspace", json!({"workspace_dir": workspace}))
            .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["workspace"], json!(workspace));
        assert_eq!(
            body["cwd"],
            json!(f.root.join("home/Documents/GuildBotics"))
        );
        assert_eq!(body["machine_state_dir"], json!(f.data()));
        assert_eq!(f.get("/health").await["workspace"], json!(workspace));
        let active = f.read(&f.data().join("active-workspace.json"));
        assert_eq!(
            active,
            json!({"workspace": workspace, "config_dir": workspace.join(".guildbotics/config")})
        );
        let status = f.get("/config/status").await;
        assert_eq!(status["workspace"], json!(workspace));
        assert_eq!(status["project_file_exists"], false);
        assert_eq!(f.get("/team").await["members"], json!([]));
    }
    assert_eq!(
        gb_workspace::selection::registered(&f.data()).unwrap(),
        [one.clone(), two.clone()]
    );
    assert_eq!(f.events("workspace.changed").len(), 2);
    assert!(!f.api.selected().service.service.status().await.running);
}

#[tokio::test]
async fn a_workspace_switch_restarts_the_system_session_under_the_same_service_run_id() {
    let f = fixture();
    gb_observability::Diagnostics::open(&f.workspace())
        .start_system_session("instance")
        .unwrap();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let (status, _) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::OK);
    let records = |workspace: &Path| -> Vec<Value> {
        let sessions = workspace.join(".guildbotics/local/run");
        let mut found = Vec::new();
        for (_, bytes) in bytes_under(&sessions) {
            for line in String::from_utf8_lossy(&bytes).lines() {
                if let Ok(record) = serde_json::from_str::<Value>(line) {
                    found.push(record);
                }
            }
        }
        found
    };
    let old = records(&f.workspace());
    assert!(
        old.iter().any(|r| r["type"] == "system.finished"),
        "{old:?}"
    );
    let started = |records: &[Value]| {
        records
            .iter()
            .find(|r| r["type"] == "system.started")
            .cloned()
            .unwrap()
    };
    let (before, after) = (started(&old), started(&records(&other)));
    assert_ne!(
        before["attributes"]["system_session_id"],
        after["attributes"]["system_session_id"]
    );
    assert_eq!(after["attributes"]["service_run_id"], "instance");
    assert_eq!(
        before["attributes"]["service_run_id"],
        after["attributes"]["service_run_id"]
    );
    gb_observability::Diagnostics::open(&other)
        .finish_system_session()
        .unwrap();
}

#[tokio::test]
async fn diagnostics_follow_the_switched_workspace() {
    let f = fixture();
    let first = gb_observability::Diagnostics::open(&f.workspace());
    let earlier = gb_observability::Trace::open(&first, "manual");
    earlier.event("command.started", serde_json::Map::new());
    assert!(
        f.get("/diagnostics/traces")
            .await
            .to_string()
            .contains(earlier.id())
    );
    let index = f
        .workspace()
        .join(".guildbotics/local/run/diagnostics.jsonl");
    let before = std::fs::read(&index).unwrap();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    f.call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    let traces = f.get("/diagnostics/traces").await;
    assert!(!traces.to_string().contains(earlier.id()), "{traces}");
    // What the server records from now on lands in the selected workspace.
    let recorded = |workspace: &Path| {
        bytes_under(&workspace.join(".guildbotics/local/run"))
            .values()
            .any(|bytes| String::from_utf8_lossy(bytes).contains("workspace.changed"))
    };
    assert!(recorded(&other));
    assert!(!recorded(&f.workspace()));
    assert_eq!(std::fs::read(&index).unwrap(), before);
    gb_observability::Diagnostics::open(&other)
        .finish_system_session()
        .unwrap();
}

#[tokio::test]
async fn a_switched_workspace_never_sees_the_previous_workspaces_secrets() {
    let f = fixture();
    f.init_with(json!({"language": "en", "provider_api_keys": {"openai": "k"}}))
        .await;
    assert_eq!(
        f.get("/config/project").await["provider_api_keys"]["openai"],
        true
    );
    let other = f.root.join("other");
    f.write(
        &other.join(".guildbotics/config/team/project.yml"),
        "language: en\n",
    );
    f.call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(
        f.get("/config/project").await["provider_api_keys"]["openai"],
        false
    );
    assert!(!other.join(".guildbotics/local/secrets.enc.json").exists());
}

#[tokio::test]
async fn a_switch_to_a_missing_path_or_a_file_is_refused() {
    let f = fixture();
    f.write(&f.root.join("file"), "x");
    for (name, code) in [
        ("missing", "workspace_not_found"),
        ("file", "workspace_not_directory"),
    ] {
        let path = f.root.join(name);
        let spelled = f.root.join("workspace/..").join(name);
        let (status, body) = f
            .call("POST", "/workspace", json!({"workspace_dir": spelled}))
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["code"], code);
        assert_eq!(body["context"], json!({"workspace_dir": path}));
    }
    assert_eq!(f.api.selected().workspace, Some(f.workspace()));
    assert!(!f.data().join("active-workspace.json").exists());
    assert!(f.events("workspace.changed").is_empty());
}

#[tokio::test]
async fn a_workspace_that_is_missing_or_not_a_directory_is_refused() {
    let f = fixture();
    f.write(&f.root.join("file"), "x");
    for language in ["en", "ja"] {
        let (status, body) = f
            .call_in(
                language,
                "POST",
                "/workspace",
                json!({"workspace_dir": f.root.join("missing")}),
            )
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(
            body["message"],
            crate::i18n::t(language, "app_api.errors.workspace_not_found", &[])
        );
        let (_, body) = f
            .call_in(
                language,
                "POST",
                "/workspace",
                json!({"workspace_dir": f.root.join("file")}),
            )
            .await;
        assert_eq!(body["context"]["workspace_dir"], json!(f.root.join("file")));
    }
    assert!(
        gb_workspace::selection::registered(&f.data())
            .unwrap()
            .is_empty()
    );
    assert!(f.events("workspace.changed").is_empty());
}

#[tokio::test]
async fn a_workspace_inside_the_exchange_directory_is_refused_without_registering_it() {
    let f = fixture();
    let inside = f.root.join("home/Documents/GuildBotics/workspace");
    std::fs::create_dir_all(&inside).unwrap();
    let before = bytes_under(&f.data());
    let (status, body) = f
        .call("POST", "/workspace", json!({"workspace_dir": inside}))
        .await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::BAD_REQUEST, &json!("unsafe_host_path"))
    );
    assert!(
        body["message"]
            .as_str()
            .unwrap()
            .contains("Documents/GuildBotics")
    );
    assert_eq!(bytes_under(&f.data()), before);
    assert!(f.events("workspace.changed").is_empty());
    assert_eq!(
        f.get("/config/status").await["workspace"],
        json!(f.workspace())
    );
}

/// A server whose selected workspace has a service that can run.
fn running() -> (Fixture, Arc<Selected>) {
    let f = fixture_with(|root| {
        let native = crate::member::native::tests::fixture(root);
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        Selected {
            workspace: Some(native.workspace.clone()),
            member: None,
            service: crate::service_runtime::Controller::new(native, host),
        }
    });
    let selected = f.api.selected();
    (f, selected)
}

#[tokio::test]
async fn a_workspace_switch_under_active_work_is_refused_and_changes_nothing() {
    let (f, selected) = running();
    assert!(selected.service.start().await.unwrap().running);
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let (status, body) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "workspace_switch_blocked_by_active_work");
    assert_eq!(
        body["context"],
        json!({"active_work_count": 0, "scheduler_state": "running", "events_state": "stopped"})
    );
    assert!(
        selected.service.service.status().await.running,
        "refused before any stop"
    );
    assert_eq!(f.api.selected().workspace, selected.workspace);
    assert!(!f.data().join("active-workspace.json").exists());
    assert!(f.events("workspace.changed").is_empty());
    selected.service.stop(true).await;
    // A run being admitted holds the selection too.
    let admitted = f.api.switching.read().await;
    let (status, _) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    drop(admitted);
    let (status, _) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn a_workspace_switch_whose_forced_stop_leaves_work_is_refused() {
    let (f, selected) = running();
    // Work that outlives the forced stop: an admitted run still running.
    let release = tokio_util::sync::CancellationToken::new();
    let held = release.clone();
    let _work = selected
        .service
        .execution
        .spawn(|_| async move { held.cancelled().await })
        .unwrap();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let (status, body) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(
        (status, &body["code"]),
        (
            StatusCode::CONFLICT,
            &json!("workspace_switch_blocked_by_active_work")
        )
    );
    assert_eq!(f.api.selected().workspace, selected.workspace);
    assert!(!f.data().join("active-workspace.json").exists());
    assert!(f.events("workspace.changed").is_empty());
    release.cancel();
}

#[tokio::test]
async fn a_service_start_whose_request_goes_away_still_starts_the_service() {
    let (f, selected) = running();
    let held = selected.service.hold_start_for_test().await;
    let request = f.call("POST", "/scheduler/start", Value::Null);
    assert!(
        tokio::time::timeout(Duration::from_millis(300), request)
            .await
            .is_err()
    );
    assert!(
        f.api.switching.try_write().is_err(),
        "the start stays admitted"
    );
    assert_eq!(f.api.work.len(), 1, "the start is the server's work");
    drop(held);
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while f.api.switching.try_write().is_err() {
        assert!(
            std::time::Instant::now() < deadline,
            "the start never ended"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let status = selected.service.service.status().await;
    assert_eq!((status.state, status.running), ("running", true));
    selected.service.stop(true).await;
}

#[tokio::test]
async fn the_workspace_switch_is_refused_exactly_while_the_status_has_active_work() {
    let (f, selected) = running();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let switch = || f.call("POST", "/workspace", json!({"workspace_dir": other}));
    assert_eq!(f.get("/scheduler/status").await["has_active_work"], false);
    assert!(selected.service.start().await.unwrap().running);
    assert_eq!(f.get("/scheduler/status").await["has_active_work"], true);
    assert_eq!(switch().await.0, StatusCode::CONFLICT);
    selected.service.stop(true).await;
    assert_eq!(f.get("/scheduler/status").await["has_active_work"], false);
    let release = tokio_util::sync::CancellationToken::new();
    let held = release.clone();
    let work = selected
        .service
        .execution
        .spawn(|_| async move { held.cancelled().await })
        .unwrap();
    assert_eq!(f.get("/scheduler/status").await["has_active_work"], true);
    assert_eq!(switch().await.0, StatusCode::CONFLICT);
    release.cancel();
    work.await.unwrap();
    assert_eq!(f.get("/scheduler/status").await["has_active_work"], false);
    assert_eq!(switch().await.0, StatusCode::OK);
}

#[tokio::test]
async fn forgetting_the_workspace_a_switch_selects_waits_for_the_switch_and_is_refused() {
    let f = fixture();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let registry =
        gb_base::files::FileLock::hold(&f.data().join("workspaces.lock"), Duration::from_secs(1))
            .unwrap();
    let switch = f.call("POST", "/workspace", json!({"workspace_dir": other}));
    let forget = f.call(
        "DELETE",
        "/device/workspaces",
        json!({"workspace_dir": other}),
    );
    tokio::pin!(switch, forget);
    let pending = Duration::from_millis(300);
    assert!(tokio::time::timeout(pending, &mut switch).await.is_err());
    assert!(tokio::time::timeout(pending, &mut forget).await.is_err());
    drop(registry);
    let ((switched, _), (forgot, answer)) = tokio::join!(switch, forget);
    assert_eq!(switched, StatusCode::OK);
    assert_eq!(forgot, StatusCode::BAD_REQUEST, "{answer}");
    assert_eq!(answer["code"], "unsafe_host_path");
    assert_eq!(f.get("/device/workspaces").await, json!([other]));
}

#[tokio::test]
async fn registered_workspaces_are_listed_and_unregistering_keeps_the_directory() {
    let f = fixture();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    gb_workspace::selection::register(&f.data(), &other).unwrap();
    gb_workspace::selection::register(&f.data(), &f.workspace()).unwrap();
    assert_eq!(
        f.get("/device/workspaces").await,
        json!([other, f.workspace()])
    );
    let (status, left) = f
        .call(
            "DELETE",
            "/device/workspaces",
            json!({"workspace_dir": other}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(left, json!([f.workspace()]));
    assert_eq!(
        gb_workspace::selection::registered(&f.data()).unwrap(),
        [f.workspace()]
    );
    assert!(other.is_dir());
}

#[tokio::test]
async fn the_selected_workspace_cannot_be_unregistered() {
    let f = fixture();
    gb_workspace::selection::register(&f.data(), &f.workspace()).unwrap();
    let (status, body) = f
        .call(
            "DELETE",
            "/device/workspaces",
            json!({"workspace_dir": f.workspace()}),
        )
        .await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::BAD_REQUEST, &json!("unsafe_host_path"))
    );
    assert_eq!(
        body["message"],
        crate::i18n::t("en", "safe_paths.active_workspace", &[])
    );
    assert_eq!(
        gb_workspace::selection::registered(&f.data()).unwrap(),
        [f.workspace()]
    );
}

#[cfg(unix)]
#[tokio::test]
async fn an_obsolete_registration_is_removed_without_reaching_its_path() {
    use std::os::unix::fs::PermissionsExt;
    let f = fixture();
    let locked = f.root.join("locked");
    let behind = locked.join("workspace");
    std::fs::create_dir_all(&behind).unwrap();
    gb_workspace::selection::register(&f.data(), &behind).unwrap();
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
    let (status, left) = f
        .call(
            "DELETE",
            "/device/workspaces",
            json!({"workspace_dir": behind}),
        )
        .await;
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!((status, left), (StatusCode::OK, json!([])));
    // The selected one is refused by its name alone.
    gb_workspace::selection::register(&f.data(), &behind).unwrap();
    f.api.select(Selected {
        workspace: Some(behind.clone()),
        ..Default::default()
    });
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
    let (status, body) = f
        .call(
            "DELETE",
            "/device/workspaces",
            json!({"workspace_dir": behind}),
        )
        .await;
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::BAD_REQUEST, &json!("unsafe_host_path"))
    );
    assert!(
        gb_workspace::selection::registered(&f.data())
            .unwrap()
            .contains(&behind)
    );
    // Named through a link, the selected one is refused too, and a
    // registration reached only through a link is still forgotten.
    std::os::unix::fs::symlink(&locked, f.root.join("alias")).unwrap();
    let (status, _) = f
        .call(
            "DELETE",
            "/device/workspaces",
            json!({"workspace_dir": f.root.join("alias/workspace")}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let other = f.root.join("alias/other");
    gb_workspace::selection::register(&f.data(), &f.root.join("plain")).unwrap();
    let (status, left) = f
        .call(
            "DELETE",
            "/device/workspaces",
            json!({"workspace_dir": other}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(left.as_array().unwrap().iter().all(|p| p != &json!(other)));
}

#[tokio::test]
async fn every_config_route_refuses_a_request_without_a_credential() {
    let f = fixture();
    for (method, path) in [
        ("POST", "/config/init"),
        ("POST", "/workspace"),
        ("GET", "/device/workspaces"),
        ("DELETE", "/device/workspaces"),
        ("GET", "/config/project"),
        ("PUT", "/config/project"),
        ("PUT", "/config/project/default-person"),
        ("GET", "/config/roles"),
        ("POST", "/config/members"),
        ("GET", "/config/members/a"),
        ("PUT", "/config/members/a"),
        ("DELETE", "/config/members/a"),
        ("GET", "/config/members/a/avatar"),
        ("POST", "/config/members/a/avatar"),
        ("GET", "/identities"),
        ("DELETE", "/identities/a"),
        ("GET", "/transcripts/settings"),
        ("PUT", "/transcripts/settings"),
    ] {
        for credential in [None, Some("Bearer gbt_0123456789abcdef_wrong")] {
            let mut request = Request::builder()
                .method(method)
                .uri(format!("/api{path}"))
                .header("host", "127.0.0.1:8765")
                .header("content-type", "application/json");
            if let Some(credential) = credential {
                request = request.header("authorization", credential);
            }
            let (status, body) =
                answer(f.respond(request.body(HttpBody::from("{}")).unwrap()).await).await;
            assert_eq!(status, StatusCode::UNAUTHORIZED, "{method} {path}");
            assert_eq!(
                body,
                json!({"code": "unauthorized",
                    "message": crate::i18n::t("en", "app_api.errors.unauthorized", &[]), "context": {}})
            );
        }
    }
}

/// Each config writer with the body its screen sends, once the workspace
/// has a project and a member read.
async fn writers(f: &Fixture) -> Vec<(&'static str, String, Value)> {
    f.init().await;
    f.add(agent("aiko")).await;
    let project = f.get("/config/project").await["revisions"].clone();
    let member = f.get("/config/members/aiko").await["revisions"].clone();
    let hello = f.config().join("commands/hello.md");
    f.write(&hello, "hello\n");
    let hello = gb_config::command_files::revision(&std::fs::read(hello).unwrap());
    vec![
        (
            "POST",
            "/config/init".into(),
            json!({"config_dir": f.config(), "language": "en"}),
        ),
        (
            "PUT",
            "/config/project".into(),
            json!({"config_dir": f.config(), "language": "ja", "expected_revisions": project}),
        ),
        (
            "PUT",
            "/config/project/default-person".into(),
            json!({"person_id": "aiko"}),
        ),
        (
            "POST",
            "/config/members".into(),
            json!({"config_dir": f.config(), "person_id": "aiko", "person_type": "agent", "is_active": false, "person_name": "N", "expected_revisions": member}),
        ),
        (
            "PUT",
            "/config/members/aiko".into(),
            json!({"config_dir": f.config(), "person_id": "aiko", "person_type": "agent", "is_active": false, "person_name": "N", "expected_revisions": member}),
        ),
        ("DELETE", "/config/members/aiko".into(), Value::Null),
        ("POST", "/config/members/aiko/avatar".into(), Value::Null),
        ("DELETE", "/identities/aiko".into(), Value::Null),
        (
            "PUT",
            "/transcripts/settings".into(),
            json!({"detail": "full", "retention_days": 2}),
        ),
        (
            "POST",
            "/commands/files".into(),
            json!({"command": "made", "format": "shell"}),
        ),
        (
            "PUT",
            format!("/commands/files/{HELLO}"),
            json!({"content": "changed\n", "expected_revision": hello}),
        ),
        (
            "DELETE",
            format!("/commands/files/{HELLO}?expected_revision={hello}"),
            Value::Null,
        ),
        (
            "POST",
            "/commands/author/apply".into(),
            json!({"changes": [{"operation": "create", "command": "proposed", "format": "shell",
                "relative_path": "proposed.sh", "content": "echo hi\n"}]}),
        ),
    ]
}

#[tokio::test]
async fn every_config_writer_answers_config_busy_while_the_locks_are_held() {
    for held in ["identity", "workspace"] {
        let f = fixture();
        let routes = writers(&f).await;
        let covered: Vec<_> = routes
            .iter()
            .map(|(m, p, _)| {
                let route = p.split('?').next().unwrap_or(p);
                (
                    m.to_lowercase(),
                    route.replace("aiko", "{name}").replace(HELLO, "{id}"),
                )
            })
            .collect();
        assert_eq!(
            covered,
            CONFIG_WRITERS
                .iter()
                .map(|(m, p)| (m.to_string(), p.to_string()))
                .collect::<Vec<_>>()
        );
        let root = if held == "identity" {
            f.root.join("identity")
        } else {
            f.workspace()
        };
        let _lock = IdentityLock::acquire(&root, Duration::from_secs(1)).unwrap();
        let before = (bytes_under(&f.config()), bytes_under(&f.identities()));
        for (method, path, body) in routes {
            let (status, answer) = if path.ends_with("/avatar") {
                f.upload("aiko", "a.png", b"png").await
            } else {
                f.call(method, &path, body).await
            };
            assert_eq!(
                status,
                StatusCode::SERVICE_UNAVAILABLE,
                "{held}: {method} {path}: {answer}"
            );
            assert_eq!(answer["code"], "config_busy");
        }
        assert_eq!(
            (bytes_under(&f.config()), bytes_under(&f.identities())),
            before,
            "{held}"
        );
    }
}

#[tokio::test]
async fn every_config_writer_refuses_a_stale_revision_before_writing() {
    let f = fixture();
    let routes = writers(&f).await;
    // The files the screens read have changed since.
    f.write(
        &f.config().join("team/project.yml"),
        "language: en\ndescription: newer\n",
    );
    f.write(
        &f.identities().join("aiko/identity.yml"),
        "person_id: aiko\nname: Newer\n",
    );
    let before = (bytes_under(&f.config()), bytes_under(&f.identities()));
    let mut compared = 0;
    for (method, path, body) in routes {
        if body.get("expected_revisions").is_none() {
            continue;
        }
        compared += 1;
        let (status, answer) = f.call(method, &path, body).await;
        assert_eq!(status, StatusCode::CONFLICT, "{method} {path}: {answer}");
        assert_eq!(answer["code"], "config_changed");
    }
    assert_eq!(compared, 3);
    assert_eq!(
        (bytes_under(&f.config()), bytes_under(&f.identities())),
        before
    );
}

/// Every save composed against a read names the config it read: one read
/// in a workspace whose files are the selected one's, byte for byte, is
/// still refused, and one that names no config is not a save.
#[tokio::test]
async fn every_composed_save_is_refused_unless_it_names_the_selected_config() {
    let f = fixture();
    let routes = writers(&f).await;
    let other = f.root.join("other/.guildbotics/config");
    for (path, bytes) in bytes_under(&f.config()) {
        let copy = other.join(path.strip_prefix(f.config()).unwrap());
        std::fs::create_dir_all(copy.parent().unwrap()).unwrap();
        std::fs::write(copy, bytes).unwrap();
    }
    let before = (
        bytes_under(&f.config()),
        bytes_under(&other),
        bytes_under(&f.identities()),
    );
    let mut composed = 0;
    for (method, path, body) in routes {
        if body.get("expected_revisions").is_none() {
            continue;
        }
        composed += 1;
        let mut elsewhere = body.clone();
        elsewhere["config_dir"] = json!(other);
        let (status, answer) = f.call(method, &path, elsewhere).await;
        assert_eq!(status, StatusCode::CONFLICT, "{method} {path}: {answer}");
        assert_eq!(answer["code"], "config_changed");
        let mut unnamed = body;
        unnamed.as_object_mut().unwrap().remove("config_dir");
        let (status, answer) = f.call(method, &path, unnamed).await;
        assert_eq!(
            status,
            StatusCode::UNPROCESSABLE_ENTITY,
            "{method} {path}: {answer}"
        );
    }
    assert_eq!(composed, 3);
    assert_eq!(
        (
            bytes_under(&f.config()),
            bytes_under(&other),
            bytes_under(&f.identities())
        ),
        before
    );
}

#[tokio::test]
async fn every_config_writer_answers_where_its_files_now_stand() {
    let f = fixture();
    let created = f.init().await;
    assert_eq!(
        created["revisions"],
        f.get("/config/project").await["revisions"]
    );
    f.add(agent("aiko")).await;
    let (_, chosen) = f
        .call(
            "PUT",
            "/config/project/default-person",
            json!({"person_id": "aiko"}),
        )
        .await;
    assert_eq!(
        chosen["revisions"],
        f.get("/config/project").await["revisions"]
    );
    let (_, removed) = f.call("DELETE", "/config/members/aiko", Value::Null).await;
    assert_eq!(
        removed["revisions"],
        json!({"team/members/aiko/member.yml": ""})
    );
    let (_, deleted) = f.call("DELETE", "/identities/aiko", Value::Null).await;
    assert_eq!(
        deleted["revisions"],
        json!({"team/members/aiko/identity.yml": ""})
    );
    let (_, saved) = f
        .call(
            "PUT",
            "/transcripts/settings",
            json!({"detail": "full", "retention_days": 3}),
        )
        .await;
    assert_eq!(saved, f.get("/transcripts/settings").await);
}

#[tokio::test]
async fn reads_answer_while_another_writer_holds_the_locks() {
    let f = fixture();
    f.init().await;
    f.add(agent("aiko")).await;
    let _identity =
        IdentityLock::acquire(&f.root.join("identity"), Duration::from_secs(1)).unwrap();
    // The project's API keys are read from the secret store, whose index
    // and sealed file are read together under the locks: that read answers
    // a held lock with 503 as a save does.
    let (status, body) = f.call("GET", "/config/project", Value::Null).await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::SERVICE_UNAVAILABLE, &json!("config_busy"))
    );
    let (status, body) = f.call("GET", "/config/members/aiko", Value::Null).await;
    assert_eq!(
        (status, &body["code"]),
        (StatusCode::SERVICE_UNAVAILABLE, &json!("config_busy"))
    );
    for uri in ["/team", "/identities"] {
        let (status, body) = f.call("GET", uri, Value::Null).await;
        assert_eq!(status, StatusCode::OK, "{uri}: {body}");
    }
}

#[tokio::test]
async fn no_work_is_admitted_for_a_workspace_on_its_way_out() {
    let (f, _) = running();
    let _switching = f.api.switching.clone().write_owned().await;
    for (uri, body) in [
        ("/scheduler/start", json!({})),
        (
            "/member/run",
            json!({"version": "x", "workspace_id": "x", "arguments": ["help"]}),
        ),
        ("/commands/run", json!({"command": "hello"})),
    ] {
        let (status, answer) = f.call("POST", uri, body).await;
        assert_eq!(
            (status, &answer["code"]),
            (StatusCode::CONFLICT, &json!("command_workspace_changing")),
            "{uri}"
        );
    }
}

#[tokio::test]
async fn an_admitted_run_keeps_the_workspace_selected_until_it_ends() {
    let f = fixture();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let admitted = f.api.admit().unwrap();
    let (status, body) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["context"]["active_work_count"], 1);
    drop(admitted);
    let (status, _) = f
        .call("POST", "/workspace", json!({"workspace_dir": other}))
        .await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn a_project_file_that_does_not_parse_is_refused_rather_than_written_over() {
    let f = fixture();
    f.init().await;
    f.write(
        &f.config().join("team/project.yml"),
        "default_person_id: aiko\nname: [\n",
    );
    let before = bytes_under(&f.config());
    for (method, uri, body) in [
        ("GET", "/config/project", Value::Null),
        (
            "PUT",
            "/config/project",
            json!({"config_dir": f.config(), "language": "en"}),
        ),
        (
            "PUT",
            "/config/project/default-person",
            json!({"person_id": ""}),
        ),
    ] {
        let (status, _) = f.call(method, uri, body).await;
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{method} {uri}");
    }
    assert_eq!(bytes_under(&f.config()), before);
}

#[tokio::test]
async fn a_member_form_needs_its_type_and_activity_and_reads_policies_in_any_case() {
    let f = fixture();
    f.init().await;
    for missing in ["person_type", "is_active"] {
        let mut form = agent("aiko");
        form.as_object_mut().unwrap().remove(missing);
        let (status, body) = f.save("POST", "/config/members", form).await;
        assert_eq!(
            (status, &body["code"]),
            (StatusCode::UNPROCESSABLE_ENTITY, &json!("validation_error")),
            "{missing}"
        );
    }
    let mut form = agent("aiko");
    form["chat_channels"] = json!(["room"]);
    form["chat_channel_participation"] = json!({"room": " Social "});
    f.add(form).await;
    let member = f.config().join("team/members/aiko/member.yml");
    assert_eq!(
        f.read(&member)["message_channels"][0]["chat"]["participation"],
        "social"
    );
    let text = std::fs::read_to_string(&member)
        .unwrap()
        .replace("\"social\"", "\"Muted\"");
    std::fs::write(&member, text).unwrap();
    assert_eq!(
        f.get("/config/members/aiko").await["chat_channel_participation"]["room"],
        "muted"
    );
}

#[tokio::test]
async fn a_stale_identity_refuses_with_the_identitys_revisions_only() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let read = f.get("/config/members/alice").await;
    f.write(
        &f.identities().join("alice/identity.yml"),
        "person_id: alice\nname: Newer\n",
    );
    let (status, body) = f
        .save(
            "PUT",
            "/config/members/alice",
            json!({"person_id": "alice", "person_name": "A",
            "person_type": "agent", "is_active": true, "expected_revisions": read["revisions"]}),
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        body["context"]["revisions"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        ["team/members/alice/identity.yml"]
    );
}

#[tokio::test]
async fn an_old_picture_is_replaced_whatever_the_case_of_its_suffix() {
    let f = fixture();
    f.write(&f.identities().join("alice/avatar.PNG"), "old");
    let (status, _) = f.upload("alice", "new.gif", b"gif").await;
    assert_eq!(status, StatusCode::OK);
    let names: Vec<_> = std::fs::read_dir(f.identities().join("alice"))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(names, ["avatar.gif"]);
}

#[tokio::test]
async fn the_exchange_directory_is_recognized_under_another_spelling() {
    let f = fixture();
    let exchange = f.root.join("home/Documents/GuildBotics");
    std::fs::create_dir_all(&exchange).unwrap();
    // Another letter case names the same directory only where the file
    // system ignores case; a link is refused on its own.
    let other_case = f.root.join("home/documents/guildbotics");
    if other_case.exists() {
        let (status, body) = f
            .call(
                "POST",
                "/config/init",
                json!({"config_dir": other_case.join("ws/.guildbotics/config"), "language": "en"}),
            )
            .await;
        assert_eq!(
            (status, &body["code"]),
            (
                StatusCode::BAD_REQUEST,
                &json!("invalid_workspace_location")
            )
        );
    }
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(&exchange, f.root.join("home/link")).unwrap();
        let (status, _) = f
            .call("POST", "/config/init",
                json!({"config_dir": f.root.join("home/link/ws/.guildbotics/config"), "language": "en"}))
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }
    assert!(
        gb_workspace::selection::registered(&f.data())
            .unwrap()
            .is_empty()
    );
}

mod providers;

mod slack_review;

mod branvil_diagnostics;

mod command_files;

mod commands;

mod verify;
