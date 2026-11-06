//! The provider-independent setup, against a recording provider registry.
use gb_base::shared_writes::{IdentityLock, SharedWriteBusyError, SharedWrites};
use gb_config::{
    repository::{ConfigRepository, ConfigWriteReceipt, Revisions},
    secrets::{DeviceKey, Keychain},
};
use gb_setup::*;
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    pin::Pin,
    sync::{Arc, Mutex},
    time::Duration,
};

/// What the providers were asked, in order.
type Log = Arc<Mutex<Vec<String>>>;

/// Two providers with a member account, `alpha` (tickets and code) and
/// `beta` (chat, reading channel IDs), and `local`, which keeps its items.
#[derive(Default)]
struct Models {
    log: Log,
    /// Hooks that refuse: `prepare:alpha`, `stored:alpha`, `removed:beta`, ...
    refuse: BTreeSet<String>,
}

fn refusing(hooks: &[&str]) -> Models {
    Models {
        refuse: hooks.iter().map(|h| h.to_string()).collect(),
        ..Models::default()
    }
}

struct Account {
    provider: String,
    form: Value,
    log: Log,
    refuse: BTreeSet<String>,
}

fn refusal(code: &str) -> SetupError {
    SetupError::text(code, format!("{code} refused"))
}

impl MemberAccount for Account {
    fn prepare<'a>(&'a mut self, member: &'a SavedMember) -> Hook<'a> {
        Box::pin(async move {
            self.log.lock().unwrap().push(format!(
                "prepare:{}:{}",
                self.provider,
                member.secret(&format!("{}_TOKEN", self.provider.to_uppercase()))
            ));
            if self.refuse.contains(&format!("prepare:{}", self.provider)) {
                return Err(refusal("provider_refused"));
            }
            Ok(())
        })
    }
    fn stored(&self, _: &str, human: bool) -> Result<StoredAccount, SetupError> {
        self.log
            .lock()
            .unwrap()
            .push(format!("stored:{}:{human}", self.provider));
        if self.refuse.contains(&format!("stored:{}", self.provider)) {
            return Err(refusal("account_refused"));
        }
        let mut stored = StoredAccount::default();
        if let Some(user) = self.form.get("user") {
            let user = match user {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            stored
                .account_info
                .insert(format!("{}_user", self.provider), user);
        }
        if let Some(Value::String(token)) = self.form.get("token")
            && !token.is_empty()
        {
            stored.secrets.insert(
                format!("{}_TOKEN", self.provider.to_uppercase()),
                token.clone(),
            );
        }
        Ok(stored)
    }
    fn settled(self: Box<Self>, saved: bool) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        let (log, provider) = (self.log.clone(), self.provider.clone());
        Box::pin(async move {
            log.lock()
                .unwrap()
                .push(format!("settled:{provider}:{saved}"))
        })
    }
}

impl ProviderModels for Models {
    fn member_secret_names(&self) -> Vec<String> {
        vec!["ALPHA_TOKEN".into(), "BETA_TOKEN".into()]
    }
    fn service(&self, kind: ServiceKind, form: &Value) -> anyhow::Result<Value> {
        let name = form["name"].as_str().unwrap_or("").to_lowercase();
        let serves = match name.as_str() {
            "alpha" => kind != ServiceKind::ChatService,
            "beta" => kind == ServiceKind::ChatService,
            "local" => true,
            _ => false,
        };
        anyhow::ensure!(
            serves,
            InvalidInput(format!("'{name}' provides no {kind:?}"))
        );
        let mut item = form.as_object().cloned().unwrap_or_default();
        item.insert("name".into(), name.into());
        Ok(Value::Object(item))
    }
    fn member_providers(&self) -> Vec<String> {
        vec!["alpha".into(), "beta".into()]
    }
    fn account_info_keys(&self, provider: &str) -> Vec<String> {
        vec![format!("{provider}_user")]
    }
    fn account(&self, provider: &str, form: &Value) -> anyhow::Result<Box<dyn MemberAccount>> {
        anyhow::ensure!(
            ["alpha", "beta"].contains(&provider),
            InvalidInput(format!("'{provider}' has no member account"))
        );
        anyhow::ensure!(
            form.as_object()
                .is_some_and(|f| f.keys().all(|k| ["user", "token"].contains(&k.as_str()))),
            InvalidInput(format!("{provider}: unknown field"))
        );
        Ok(Box::new(Account {
            provider: provider.into(),
            form: form.clone(),
            log: self.log.clone(),
            refuse: self.refuse.clone(),
        }))
    }
    fn view(
        &self,
        provider: &str,
        account_info: &BTreeMap<String, String>,
        has_secret: &dyn Fn(&str) -> bool,
    ) -> Value {
        json!({
            "user": account_info.get(&format!("{provider}_user")).cloned().unwrap_or_default(),
            "has_token": has_secret(&format!("{}_TOKEN", provider.to_uppercase())),
        })
    }
    fn removed<'a>(&'a self, provider: &'a str, member: &'a SavedMember) -> Hook<'a> {
        Box::pin(async move {
            self.log
                .lock()
                .unwrap()
                .push(format!("removed:{provider}:{}", member.person_id));
            if self.refuse.contains(&format!("removed:{provider}")) {
                return Err(refusal("removal_refused"));
            }
            Ok(())
        })
    }
    fn avatar_changed<'a>(&'a self, provider: &'a str, member: &'a SavedMember) -> Hook<'a> {
        Box::pin(async move {
            let avatar = member
                .avatar
                .as_ref()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            self.log
                .lock()
                .unwrap()
                .push(format!("avatar_changed:{provider}:{avatar}"));
            if self.refuse.contains(&format!("avatar_changed:{provider}")) {
                return Err(refusal("avatar_refused"));
            }
            Ok(())
        })
    }
    fn channel(&self, services: &Value, reference: &str) -> anyhow::Result<Map<String, Value>> {
        let mut chat = Map::new();
        if services["chat_service"]["name"] == "beta" && reference.starts_with('C') {
            chat.insert("channel_id".into(), reference.into());
        } else {
            chat.insert("channel_name".into(), reference.into());
        }
        Ok(chat)
    }
}

struct FixedKey;
impl Keychain for FixedKey {
    fn read(&self) -> anyhow::Result<Option<String>> {
        Ok(Some("ExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExM=".into()))
    }
    fn create(&self, _: &str) -> anyhow::Result<()> {
        panic!("use synthetic key")
    }
}
/// A keychain that cannot be used.
struct Locked;
impl Keychain for Locked {
    fn read(&self) -> anyhow::Result<Option<String>> {
        anyhow::bail!("locked")
    }
    fn create(&self, _: &str) -> anyhow::Result<()> {
        anyhow::bail!("locked")
    }
}
/// A keychain the test fails on if anything touches it.
struct Untouchable;
impl Keychain for Untouchable {
    fn read(&self) -> anyhow::Result<Option<String>> {
        panic!("the keychain must not be read")
    }
    fn create(&self, _: &str) -> anyhow::Result<()> {
        panic!("the keychain must not be created")
    }
}

fn templates() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../guildbotics/templates")
}

/// A machine with one identity root and workspaces under one directory.
struct World {
    dir: tempfile::TempDir,
    repo: ConfigRepository,
    models: Models,
    templates: PathBuf,
}
impl World {
    fn new() -> World {
        World::with(Models::default())
    }
    fn with(models: Models) -> World {
        let dir = tempfile::tempdir().unwrap();
        let repo = repository(dir.path(), "workspace");
        World {
            dir,
            repo,
            models,
            templates: templates(),
        }
    }
    fn setup(&self) -> Setup<'_> {
        self.setup_with(&FixedKey)
    }
    fn setup_with<'a>(&'a self, keychain: &'a dyn Keychain) -> Setup<'a> {
        Setup {
            repository: &self.repo,
            providers: &self.models,
            templates: &self.templates,
            device: DeviceKey {
                machine_root: self.dir.path().join("machine"),
                keychain,
            },
        }
    }
    fn workspace(&self) -> PathBuf {
        self.dir.path().join("workspace/.guildbotics/config")
    }
    fn identities(&self) -> PathBuf {
        self.dir.path().join("identity/.guildbotics/config")
    }
    fn identity(&self, id: &str) -> Value {
        read(
            &self
                .identities()
                .join(format!("team/members/{id}/identity.yml")),
        )
    }
    fn member(&self, id: &str) -> Value {
        read(
            &self
                .workspace()
                .join(format!("team/members/{id}/member.yml")),
        )
    }
    fn project(&self) -> Value {
        read(&self.workspace().join("team/project.yml"))
    }
    fn log(&self) -> Vec<String> {
        std::mem::take(&mut self.models.log.lock().unwrap())
    }
    fn identity_secrets(&self) -> BTreeMap<String, String> {
        self.repo
            .identity_secrets(device(self.dir.path()))
            .unwrap()
            .values()
            .unwrap()
    }
    fn workspace_secrets(&self) -> BTreeMap<String, String> {
        self.repo
            .workspace_secrets(device(self.dir.path()))
            .unwrap()
            .values()
            .unwrap()
    }
    fn save(&self, form: Value) -> anyhow::Result<ConfigWriteReceipt<()>> {
        save(&self.setup(), form, &Revisions::new())
    }
    fn init(&self, form: Value) {
        self.setup()
            .init_project(&serde_json::from_value(form).unwrap())
            .unwrap();
    }
    fn write(&self, relative: &str, text: &str) {
        let path = self.workspace().join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    fn identity_file(&self, id: &str, text: &str) {
        let directory = self.identities().join("team/members").join(id);
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("identity.yml"), text).unwrap();
    }
}

fn save(
    setup: &Setup<'_>,
    form: Value,
    expected: &Revisions,
) -> anyhow::Result<ConfigWriteReceipt<()>> {
    let id = form["person_id"].as_str().unwrap().to_owned();
    block_on(setup.save_member(&id, person(form), expected))
}
/// A member form, with the fields every form has where the test names none.
fn person(mut form: Value) -> PersonSetupInput {
    for (key, value) in [("person_type", json!("agent")), ("is_active", json!(false))] {
        if form.get(key).is_none() {
            form[key] = value;
        }
    }
    serde_json::from_value(form).unwrap()
}
fn repository(root: &Path, workspace: &str) -> ConfigRepository {
    let mut writes = SharedWrites::new(root.join("identity"), Some(root.join(workspace)));
    writes.timeout = Duration::from_millis(300);
    ConfigRepository::new(writes).unwrap()
}
fn device(root: &Path) -> DeviceKey<'_> {
    DeviceKey {
        machine_root: root.join("machine"),
        keychain: &FixedKey,
    }
}
fn read(path: &Path) -> Value {
    gb_config::yaml_value(&fs::read_to_string(path).unwrap()).unwrap()
}
fn block_on<F: Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap()
        .block_on(future)
}
fn code(error: &anyhow::Error) -> String {
    error
        .downcast_ref::<SetupError>()
        .map(|e| e.code.clone())
        .unwrap_or_else(|| panic!("not a SetupError: {error:#}"))
}
fn agent(id: &str) -> Value {
    json!({"person_id": id, "person_name": "Test", "person_type": "agent", "is_active": true,
        "roles": ["architect"], "accounts": {"alpha": {"user": "test-user", "token": "alpha-1"}}})
}
fn keys(value: &Value) -> BTreeSet<String> {
    value.as_object().unwrap().keys().cloned().collect()
}
fn names(values: &[&str]) -> BTreeSet<String> {
    values.iter().map(|v| v.to_string()).collect()
}
fn bytes_under(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn walk(dir: &Path, found: &mut BTreeMap<PathBuf, Vec<u8>>) {
        for entry in fs::read_dir(dir).into_iter().flatten().flatten() {
            if entry.path().is_dir() {
                walk(&entry.path(), found);
            } else if !entry.path().ends_with("shared-write.lock") {
                found.insert(entry.path(), fs::read(entry.path()).unwrap());
            }
        }
    }
    let mut found = BTreeMap::new();
    walk(root, &mut found);
    found
}

#[test]
fn provider_hooks_run_in_frozen_order() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    // A save: every account's stored items, their effective secrets and
    // each account's preparation outside the locks, then the write, and the
    // settling.
    world
        .save(
            json!({"person_id": "a", "person_name": "A", "person_type": "agent",
            "accounts": {"alpha": {"user": "u", "token": "alpha-1"}, "beta": {"user": "v"}}}),
        )
        .unwrap();
    assert_eq!(
        world.log(),
        [
            "stored:alpha:false",
            "stored:beta:false",
            "prepare:alpha:alpha-1",
            "prepare:beta:",
            "stored:alpha:false",
            "stored:beta:false",
            "settled:alpha:true",
            "settled:beta:true",
        ]
    );
    // A refusal before the write settles as not saved, and writes nothing.
    let refused = World::with(refusing(&["prepare:beta"]));
    let error = refused
        .save(json!({"person_id": "a", "person_name": "A",
            "accounts": {"alpha": {"token": "alpha-1"}, "beta": {}}}))
        .unwrap_err();
    assert_eq!(code(&error), "provider_refused");
    assert_eq!(
        refused.log()[2..],
        [
            "prepare:alpha:alpha-1",
            "prepare:beta:",
            "settled:alpha:false",
            "settled:beta:false",
        ]
    );
    assert!(!refused.identities().join("team/members/a").exists());
    assert!(
        !refused
            .dir
            .path()
            .join("identity/.guildbotics/config/secrets.yml")
            .exists()
    );
    // A stale save is refused before it begins writing: not saved.
    let stale = Revisions::from([("team/members/a/identity.yml".to_owned(), String::new())]);
    let error = save(
        &world.setup(),
        json!({"person_id": "a", "person_name": "B", "accounts": {"alpha": {}}}),
        &stale,
    )
    .unwrap_err();
    assert!(error.is::<gb_config::repository::StaleConfigWriteError>());
    assert_eq!(world.log().last().unwrap(), "settled:alpha:false");
    // A failure after the write began settles nothing: what the provider
    // obtained stays for the member it may have been stored for.
    world.identity_file("c_x", "person_id: c_x\n");
    world.log();
    let error = world
        .save(json!({"person_id": "c-x", "person_name": "C", "accounts": {"alpha": {}}}))
        .unwrap_err();
    assert_eq!(code(&error), "person_env_prefix_conflict");
    let log = world.log();
    assert!(log.contains(&"prepare:alpha:".to_owned()), "{log:?}");
    assert!(
        log.iter().all(|entry| !entry.starts_with("settled")),
        "{log:?}"
    );
    // A deletion asks every provider first, and a refusal keeps the member.
    let removing = World::with(refusing(&["removed:beta"]));
    removing.init(json!({"language": "en"}));
    removing.save(agent("a")).unwrap();
    removing.log();
    let error = block_on(removing.setup().delete_identity("a")).unwrap_err();
    assert_eq!(code(&error), "removal_refused");
    assert_eq!(removing.log(), ["removed:alpha:a", "removed:beta:a"]);
    assert!(
        removing
            .workspace()
            .join("team/members/a/member.yml")
            .is_file()
    );
    assert!(
        removing
            .identities()
            .join("team/members/a/identity.yml")
            .is_file()
    );
    block_on(world.setup().delete_identity("a")).unwrap();
    assert_eq!(world.log(), ["removed:alpha:a", "removed:beta:a"]);
    // Taking a member out of a workspace asks no provider.
    world.save(agent("b")).unwrap();
    world.log();
    world.setup().remove_member("b").unwrap();
    assert!(world.log().is_empty());
    // A new picture is handed over once stored; a refusal keeps it.
    let avatar = World::with(refusing(&["avatar_changed:alpha"]));
    let error = block_on(avatar.setup().change_avatar("b", b"png", "me.PNG")).unwrap_err();
    assert_eq!(code(&error), "avatar_refused");
    assert_eq!(avatar.log(), ["avatar_changed:alpha:avatar.png"]);
    assert_eq!(
        fs::read(avatar.identities().join("team/members/b/avatar.png")).unwrap(),
        b"png"
    );
    block_on(world.setup().change_avatar("b", b"gif", "x.gif")).unwrap();
    assert_eq!(
        world.log(),
        [
            "avatar_changed:alpha:avatar.gif",
            "avatar_changed:beta:avatar.gif"
        ]
    );
    assert!(
        !world
            .identities()
            .join("team/members/b/avatar.png")
            .exists()
    );
    let oversized = vec![0; AVATAR_LIMIT + 1];
    let error = block_on(world.setup().change_avatar("b", &oversized, "x.png")).unwrap_err();
    assert_eq!(code(&error), "avatar_invalid");
    assert!(world.log().is_empty());
}
#[test]
fn saves_separate_documents_and_identity_secrets_and_reports_revisions() {
    let world = World::new();
    world.init(json!({"language": "en", "services": {"chat_service": {"name": "beta"}}}));
    let receipt = world
        .save(json!({"person_id": "a", "person_name": "Ann", "person_type": "agent",
            "git_email": "ann@example.com", "roles": ["architect:"], "character": {"archetype": "x"},
            "chat_channels": ["#C01", " general ", "  "], "chat_channel_participation": {"#C01": "social"},
            "work_stop_notification_channels": [" #C01 ", "deleted"],
            "accounts": {"alpha": {"user": "ann", "token": "secret-value"}}}))
        .unwrap();
    let identity_text =
        fs::read_to_string(world.identities().join("team/members/a/identity.yml")).unwrap();
    assert!(!identity_text.contains("secret-value"));
    let identity = world.identity("a");
    assert_eq!(
        identity["account_info"],
        json!({"git_user": "Ann", "git_email": "ann@example.com", "alpha_user": "ann"})
    );
    assert_eq!(
        identity["profile"],
        json!({"character": {"archetype": "x"}})
    );
    let member = world.member("a");
    assert_eq!(member["profile"], json!({"roles": {"architect": {}}}));
    assert_eq!(
        member["message_channels"],
        json!([
            {"name": "C01", "used_as": ["internal_communication"], "used_by": ["a"],
             "channel_info": {"channel_id": "C01"},
             "chat": {"enabled": true, "participation": "social", "notify_work_stops": true, "channel_id": "C01"}},
            {"name": "general", "used_as": ["internal_communication"], "used_by": ["a"],
             "channel_info": {},
             "chat": {"enabled": true, "participation": "strict", "notify_work_stops": false, "channel_name": "general"}},
        ])
    );
    assert_eq!(world.identity_secrets()["A_ALPHA_TOKEN"], "secret-value");
    assert!(world.workspace_secrets().is_empty());
    let read = world.setup().read_member("a").unwrap();
    assert_eq!(receipt.revisions, read.revisions);
    assert_eq!(
        receipt.revisions.keys().collect::<Vec<_>>(),
        ["team/members/a/identity.yml", "team/members/a/member.yml"]
    );
    assert_eq!(read.work_stop_notification_channels, ["C01"]);
    // Cleared, no channel is a destination any more.
    let mut form = json!({"person_id": "a", "person_name": "Ann", "chat_channels": ["C01"]});
    form["work_stop_notification_channels"] = json!([]);
    world.save(form).unwrap();
    assert!(
        world
            .setup()
            .read_member("a")
            .unwrap()
            .work_stop_notification_channels
            .is_empty()
    );
}

#[test]
fn member_files_hold_exactly_their_fields_and_omit_empty_settings() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world
        .save(json!({"person_id": "a", "person_name": "{Ann}", "person_type": "agent", "is_active": true,
            "git_email": "a@example.com", "roles": ["dev"], "speaking_style": "{calm}", "relationships": "{peers}",
            "character": {"archetype": "x", "nested": {"traits": ["kind"]}}, "chat_channels": ["room"],
            "routine_commands": ["workflows/x", " check --all ", " "],
            "task_schedules": [{"command": " report --daily ", "schedules": ["0 9 * * 1", " 30 17 * * 5 ", ""]},
                               {"command": "weekly", "schedules": ["0 0 * * 0"]}]}))
        .unwrap();
    assert_eq!(
        keys(&world.identity("a")),
        names(&[
            "account_info",
            "name",
            "person_id",
            "person_type",
            "profile",
            "relationships",
            "speaking_style"
        ])
    );
    assert_eq!(
        keys(&world.member("a")),
        names(&[
            "is_active",
            "message_channels",
            "profile",
            "routine_commands",
            "task_schedules"
        ])
    );
    assert_eq!(
        world.identity("a")["profile"],
        json!({"character": {"archetype": "x", "nested": {"traits": ["kind"]}}})
    );
    assert_eq!(
        (
            &world.identity("a")["speaking_style"],
            &world.identity("a")["relationships"]
        ),
        (&json!("{calm}"), &json!("{peers}"))
    );
    assert_eq!(
        world.member("a")["routine_commands"],
        json!(["workflows/x", "check --all"])
    );
    assert_eq!(
        world.member("a")["task_schedules"],
        json!([{"command": "report --daily", "schedules": ["0 9 * * 1", "30 17 * * 5"]},
               {"command": "weekly", "schedules": ["0 0 * * 0"]}])
    );
    let snapshot = world.setup().read_member("a").unwrap();
    assert_eq!(snapshot.person_name, "{Ann}");
    assert_eq!(
        (
            snapshot.speaking_style.as_str(),
            snapshot.relationships.as_str()
        ),
        ("{calm}", "{peers}")
    );
    assert_eq!(snapshot.character["nested"], json!({"traits": ["kind"]}));
    assert_eq!(snapshot.routine_commands, ["workflows/x", "check --all"]);
    assert_eq!(
        json!(snapshot.task_schedules),
        world.member("a")["task_schedules"]
    );
    let team = gb_config::team::Team::load(&world.workspace(), &world.dir.path().join("identity"))
        .unwrap();
    assert_eq!(
        team.members
            .iter()
            .map(|m| (&m.person_id[..], &m.name[..]))
            .collect::<Vec<_>>(),
        [("a", "{Ann}")]
    );
    // Saved empty, the patrol settings leave no key behind.
    world
        .save(json!({"person_id": "a", "person_name": "Ann", "person_type": "agent"}))
        .unwrap();
    assert_eq!(keys(&world.member("a")), names(&["is_active", "profile"]));
    assert!(world.identity("a").get("speaking_style").is_none());
    // A schedule the form gets wrong is an invalid request.
    for schedule in [
        json!({"command": "x", "schedules": ["0 9 * * 1 7"]}),
        json!({"command": "x", "schedules": ["* * * *"]}),
        json!({"command": "   ", "schedules": ["0 9 * * 1"]}),
    ] {
        let error = world
            .save(json!({"person_id": "a", "person_name": "Ann", "task_schedules": [schedule]}))
            .unwrap_err();
        assert!(error.is::<InvalidInput>(), "{error:#}");
    }
}

#[test]
fn accounts_of_every_provider_merge_and_secrets_never_reach_files() {
    let world = World::new();
    world
        .save(json!({"person_id": "a", "person_name": "Ann", "git_email": "a@x",
            "accounts": {"alpha": {"user": 42, "token": "t-alpha"}, "beta": {"user": "b", "token": "t-beta"}}}))
        .unwrap();
    assert_eq!(
        world.identity("a")["account_info"],
        json!({"git_user": "Ann", "git_email": "a@x", "alpha_user": "42", "beta_user": "b"})
    );
    assert_eq!(
        world.identity_secrets(),
        BTreeMap::from([
            ("A_ALPHA_TOKEN".to_owned(), "t-alpha".to_owned()),
            ("A_BETA_TOKEN".to_owned(), "t-beta".to_owned()),
        ])
    );
    for (path, bytes) in bytes_under(world.dir.path()) {
        let text = String::from_utf8_lossy(&bytes);
        assert!(
            !text.contains("t-alpha") && !text.contains("t-beta"),
            "{}",
            path.display()
        );
    }
    assert!(world.workspace_secrets().is_empty());
}

#[test]
fn a_save_keeps_the_account_entries_its_form_cannot_edit() {
    let world = World::new();
    world.identity_file(
        "a",
        "person_id: a\nname: Ann\nperson_type: agent\naccount_info:\n  git_user: Old\n  git_email: old@x\n  alpha_user: ann\n  beta_user: b\n  slack_user_id: U1\n  branvil_user_id: 7\n",
    );
    world.write("team/members/a/member.yml", "is_active: false\n");
    // Without the beta account, and an alpha account that clears its user.
    world
        .save(json!({"person_id": "a", "person_name": "Ann", "git_email": "a@x", "roles": ["reviewer"],
            "accounts": {"alpha": {}}}))
        .unwrap();
    assert_eq!(
        world.identity("a")["account_info"],
        json!({"git_user": "Ann", "git_email": "a@x", "beta_user": "b", "slack_user_id": "U1",
            "branvil_user_id": 7})
    );
    // Without any account, every stored entry stays.
    world
        .save(json!({"person_id": "a", "person_name": "Ann", "git_email": "a@x", "roles": []}))
        .unwrap();
    assert_eq!(
        world.identity("a")["account_info"],
        json!({"git_user": "Ann", "git_email": "a@x", "beta_user": "b", "slack_user_id": "U1",
            "branvil_user_id": 7})
    );
}

#[test]
fn human_members_cannot_patrol_and_removal_retains_identity_and_clears_default() {
    let world = World::new();
    world.init(json!({"language": "en", "services": {"ticket_service": {"name": "alpha"}}}));
    world
        .save(
            json!({"person_id": "h", "person_name": "Hu", "person_type": "human", "is_active": true,
            "roles": ["owner"], "speaking_style": "s", "relationships": "r", "character": {"a": 1},
            "chat_channels": ["room"], "routine_commands": ["x"],
            "task_schedules": [{"command": "x", "schedules": ["0 9 * * 1"]}],
            "accounts": {"alpha": {"user": "hu"}}}),
        )
        .unwrap();
    assert!(world.log().contains(&"stored:alpha:true".to_owned()));
    let identity = world.identity("h");
    assert!(identity.get("profile").is_none());
    assert!(identity.get("speaking_style").is_none() && identity.get("relationships").is_none());
    assert_eq!(
        world.member("h"),
        json!({"is_active": false, "profile": {"roles": {"owner": {}}}})
    );
    let project = fs::read(world.workspace().join("team/project.yml")).unwrap();
    let error = world.setup().set_default_person("h").unwrap_err();
    assert_eq!(code(&error), "person_not_executable");
    assert_eq!(
        fs::read(world.workspace().join("team/project.yml")).unwrap(),
        project
    );
    // An inactive agent can be the default, as in the Python version.
    world
        .save(
            json!({"person_id": "a", "person_name": "A", "is_active": false,
            "accounts": {"alpha": {"token": "t"}, "beta": {"token": "u"}}}),
        )
        .unwrap();
    world.setup().set_default_person("a").unwrap();
    world.setup().remove_member("a").unwrap();
    assert!(world.project().get("default_person_id").is_none());
    assert!(
        world
            .identities()
            .join("team/members/a/identity.yml")
            .is_file()
    );
    block_on(world.setup().delete_identity("a")).unwrap();
    assert!(!world.identities().join("team/members/a").exists());
    assert!(world.identity_secrets().is_empty());
}

#[test]
fn project_model_validation_defaults_and_kind_based_brains_round_trip() {
    let world = World::new();
    world.init(json!({"language": "en", "services": {"ticket_service": {"name": "Alpha"}}}));
    world
        .save(json!({"person_id": "a", "person_name": "A"}))
        .unwrap();
    assert_eq!(
        world.member("a")["routine_commands"],
        json!([TICKET_WORKFLOW_COMMAND])
    );
    world
        .save(json!({"person_id": "b", "person_name": "B"}))
        .unwrap();
    world.setup().set_default_person("a").unwrap();
    world.setup().remove_member("b").unwrap();
    assert_eq!(world.project()["default_person_id"], "a");
    world.setup().remove_member("a").unwrap();
    assert!(world.project().get("default_person_id").is_none());
    for refused in [
        json!({"ticket_service": {"name": "beta"}}),
        json!({"ticket_service": {"name": "jira"}}),
        json!({"ticket_service": {}}),
        json!({"wiki_service": {"name": "alpha"}}),
    ] {
        let input = serde_json::from_value(json!({"language": "en", "services": refused})).unwrap();
        let before = bytes_under(&world.workspace());
        let error = world
            .setup()
            .update_project(&input, &Revisions::new())
            .unwrap_err();
        assert!(error.is::<InvalidInput>(), "{error:#}");
        assert_eq!(bytes_under(&world.workspace()), before);
    }
    let mapping = BrainMapping::from([(
        "default".to_owned(),
        Brain::AiCli {
            cli_agent: "codex".into(),
        },
    )]);
    world
        .setup()
        .save_brains(&mapping, &Revisions::new())
        .unwrap();
    assert_eq!(world.setup().read_brains().unwrap(), mapping);
}

#[test]
fn default_patrol_is_seeded_only_when_joining_a_ticket_project() {
    // No project, and a project without tickets.
    for services in [
        None,
        Some(json!({"code_hosting_service": {"name": "alpha"}})),
    ] {
        let world = World::new();
        if let Some(services) = services {
            world.init(json!({"language": "en", "services": services}));
        }
        world
            .save(json!({"person_id": "a", "person_name": "A"}))
            .unwrap();
        assert!(world.member("a").get("routine_commands").is_none());
    }
    // Saved again, a participating member gets no default.
    let world = World::new();
    world.init(json!({"language": "en"}));
    world
        .save(json!({"person_id": "a", "person_name": "A"}))
        .unwrap();
    world.write(
        "team/project.yml",
        "language: en\nservices:\n  ticket_service:\n    name: alpha\n",
    );
    world
        .save(json!({"person_id": "a", "person_name": "A"}))
        .unwrap();
    assert!(world.member("a").get("routine_commands").is_none());
}

#[test]
fn second_workspace_joins_the_same_identity() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world
        .save(
            json!({"person_id": "a", "person_name": "A", "is_active": true,
            "accounts": {"alpha": {"user": "u", "token": "t"}}}),
        )
        .unwrap();
    let second = repository(world.dir.path(), "second");
    let setup = Setup {
        repository: &second,
        ..world.setup()
    };
    setup
        .init_project(
            &serde_json::from_value(json!({"language": "en",
            "services": {"ticket_service": {"name": "alpha"}}}))
            .unwrap(),
        )
        .unwrap();
    let snapshot = setup.read_member("a").unwrap();
    assert!(!snapshot.participating);
    assert_eq!(snapshot.person_name, "A");
    assert_eq!(
        snapshot.accounts["alpha"],
        json!({"user": "u", "has_token": true})
    );
    assert_eq!(
        setup.list_identities().unwrap(),
        [IdentitySummary {
            person_id: "a".into(),
            name: "A".into(),
            person_type: "agent".into(),
            participating: false
        }]
    );
    save(
        &setup,
        json!({"person_id": "a", "person_name": "Renamed", "is_active": false}),
        &snapshot.revisions,
    )
    .unwrap();
    assert_eq!(world.identity("a")["name"], "Renamed");
    assert_eq!(world.member("a")["is_active"], true);
    let joined = read(
        &world
            .dir
            .path()
            .join("second/.guildbotics/config/team/members/a/member.yml"),
    );
    assert_eq!(joined["is_active"], false);
    assert_eq!(joined["routine_commands"], json!([TICKET_WORKFLOW_COMMAND]));
    assert!(world.member("a").get("routine_commands").is_none());
    assert_eq!(world.identity_secrets()["A_ALPHA_TOKEN"], "t");
    assert!(setup.read_member("a").unwrap().participating);
}

#[test]
fn listing_orphans_and_foreign_fields_in_stored_files() {
    let world = World::new();
    world.write(
        "team/members/a/member.yml",
        "is_active: true\nprofile:\n  roles:\n    dev: {}\n",
    );
    world.write("team/members/empty/.keep", "");
    world.write("team/members/b/member.yml", "is_active: false\n");
    world.identity_file("b", "person_id: b\nname: Bea\nperson_type: human\n");
    assert_eq!(
        world.setup().list_members().unwrap(),
        [
            PersonSummary {
                person_id: "a".into(),
                name: String::new(),
                person_type: String::new(),
                is_active: true,
                roles: vec!["dev".into()],
                identity: false
            },
            PersonSummary {
                person_id: "b".into(),
                name: "Bea".into(),
                person_type: "human".into(),
                is_active: false,
                roles: vec![],
                identity: true
            },
        ]
    );
    let read = world.setup().read_member("a").unwrap();
    assert!(read.participating);
    assert_eq!(read.person_name, "");
}

#[test]
fn stored_task_schedules_skip_invalid_entries_on_read() {
    let world = World::new();
    world.write(
        "team/members/a/member.yml",
        "task_schedules:\n  - command: ok\n    schedules: ['0 9 * * 1']\n  - command: bad\n    schedules: ['* *']\n  - just text\n",
    );
    let read = world.setup().read_member("a").unwrap();
    assert_eq!(
        json!(read.task_schedules),
        json!([{"command": "ok", "schedules": ["0 9 * * 1"]}])
    );
}

#[test]
fn member_snapshot_shows_provider_account_views_and_avatar_timestamp() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world
        .save(json!({"person_id": "a", "person_name": "A", "git_email": "a@x", "character": {"k": "v"},
            "chat_channels": ["room"], "chat_channel_participation": {"room": "muted"},
            "accounts": {"alpha": {"user": "u", "token": "t"}}}))
        .unwrap();
    let read = world.setup().read_member("a").unwrap();
    assert_eq!(
        Value::Object(read.accounts.clone()),
        json!({"alpha": {"user": "u", "has_token": true}, "beta": {"user": "", "has_token": false}})
    );
    assert_eq!(read.git_email, "a@x");
    assert_eq!(read.character["k"], "v");
    assert_eq!(read.chat_channels, ["room"]);
    assert_eq!(read.chat_channel_participation["room"], "muted");
    assert!(read.participating);
    assert_eq!(read.avatar_timestamp, 0);
    let avatar = world.identities().join("team/members/a/avatar.png");
    fs::write(&avatar, b"png").unwrap();
    let mtime = fs::metadata(&avatar)
        .unwrap()
        .modified()
        .unwrap()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    assert_eq!(
        world.setup().read_member("a").unwrap().avatar_timestamp,
        mtime
    );
}

#[test]
fn blank_secrets_keep_existing_values_and_new_values_replace() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world
        .save(json!({"person_id": "a", "person_name": "A", "roles": ["x"],
            "accounts": {"alpha": {"token": "v1"}, "beta": {"token": "w1"}}}))
        .unwrap();
    world
        .save(
            json!({"person_id": "a", "person_name": "B", "speaking_style": "new", "roles": ["y"],
            "accounts": {"alpha": {"token": ""}, "beta": {}}}),
        )
        .unwrap();
    assert_eq!(world.identity_secrets()["A_ALPHA_TOKEN"], "v1");
    assert_eq!(world.identity_secrets()["A_BETA_TOKEN"], "w1");
    assert_eq!(world.identity("a")["name"], "B");
    assert_eq!(world.identity("a")["speaking_style"], "new");
    assert_eq!(world.member("a")["profile"]["roles"], json!({"y": {}}));
    world
        .save(json!({"person_id": "a", "person_name": "B", "accounts": {"alpha": {"token": "v2"}}}))
        .unwrap();
    assert_eq!(world.identity_secrets()["A_ALPHA_TOKEN"], "v2");
    world.setup().remove_member("a").unwrap();
    assert_eq!(world.identity_secrets()["A_ALPHA_TOKEN"], "v2");
    // Another ID is another member, holding none of these.
    world
        .save(json!({"person_id": "b", "person_name": "B"}))
        .unwrap();
    assert_eq!(
        world.identity_secrets().keys().collect::<Vec<_>>(),
        ["A_ALPHA_TOKEN", "A_BETA_TOKEN"]
    );
    assert_eq!(
        world.setup().read_member("b").unwrap().accounts["alpha"]["has_token"],
        false
    );
    block_on(world.setup().delete_identity("a")).unwrap();
    assert!(world.identity_secrets().is_empty());
    assert!(!world.identities().join("team/members/a").exists());
    assert!(world.identities().join("team/members/b").exists());
}

#[test]
fn every_member_secret_survives_removal_and_goes_with_the_identity() {
    let world = World::new();
    world.init(json!({"language": "en", "provider_api_keys": {"openai": "k"}}));
    world
        .save(json!({"person_id": "a", "person_name": "A",
            "accounts": {"alpha": {"token": "t1"}, "beta": {"token": "t2"}}}))
        .unwrap();
    world.setup().remove_member("a").unwrap();
    assert_eq!(world.identity_secrets().len(), 2);
    block_on(world.setup().delete_identity("a")).unwrap();
    assert!(world.identity_secrets().is_empty());
    assert_eq!(world.workspace_secrets()["OPENAI_API_KEY"], "k");
}

#[test]
fn deletes_entire_member_and_identity_directories_and_rejects_absent_members() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world.save(agent("a")).unwrap();
    world.write("team/members/a/intelligences/x.yml", "a: 1\n");
    block_on(world.setup().change_avatar("a", b"png", "a.png")).unwrap();
    let receipt = world.setup().remove_member("a").unwrap();
    assert_eq!(receipt.revisions["team/members/a/member.yml"], "");
    assert!(!world.workspace().join("team/members/a").exists());
    assert!(
        world
            .identities()
            .join("team/members/a/avatar.png")
            .is_file()
    );
    assert!(!world.setup().list_identities().unwrap()[0].participating);
    assert_eq!(
        code(&world.setup().remove_member("a").unwrap_err()),
        "person_not_found"
    );
    let receipt = block_on(world.setup().delete_identity("a")).unwrap();
    assert_eq!(receipt.revisions["team/members/a/identity.yml"], "");
    assert!(!world.identities().join("team/members/a").exists());
    assert_eq!(
        code(&world.setup().read_member("a").unwrap_err()),
        "person_not_found"
    );
    assert_eq!(
        code(&block_on(world.setup().delete_identity("a")).unwrap_err()),
        "person_not_found"
    );
}

#[test]
fn namespace_collisions_and_bad_provider_forms_do_not_write() {
    // The other owner in the workspace too, or only an identity; a repair
    // name counts as well.
    for (other, target) in [
        ("alice-bak", "alice_bak"),
        ("Alice", "alice"),
        ("alıce", "alice"),
    ] {
        for joined in [true, false] {
            let world = World::new();
            world.identity_file(other, &format!("person_id: {other}\n"));
            if joined {
                world.write(
                    &format!("team/members/{other}/member.yml"),
                    "is_active: true\n",
                );
            }
            let before = bytes_under(world.dir.path());
            let directories = |root: PathBuf| -> BTreeSet<_> {
                fs::read_dir(root.join("team/members"))
                    .into_iter()
                    .flatten()
                    .flatten()
                    .map(|e| e.file_name())
                    .collect()
            };
            let members = (
                directories(world.identities()),
                directories(world.workspace()),
            );
            let error = save(
                &world.setup_with(&Untouchable),
                json!({"person_id": target, "person_name": "T", "accounts": {"alpha": {"token": "x"}}}),
                &Revisions::new(),
            )
            .unwrap_err();
            let error = error.downcast_ref::<SetupError>().unwrap();
            assert_eq!(error.code, "person_env_prefix_conflict");
            assert_eq!(
                error.sentence,
                Sentence::Key(
                    "member_config.target_prefix_conflict".into(),
                    vec![
                        ("person_id".into(), target.into()),
                        ("members".into(), other.into())
                    ]
                )
            );
            assert_eq!(bytes_under(world.dir.path()), before);
            assert_eq!(
                (
                    directories(world.identities()),
                    directories(world.workspace())
                ),
                members
            );
            if other == "alice-bak" {
                // The owner of the prefix can still save.
                world
                    .save(json!({"person_id": other, "person_name": "O"}))
                    .unwrap();
            }
        }
    }
    // A provider's refusal of an account reaches the caller as it was.
    let world = World::with(refusing(&["stored:alpha"]));
    let error = world
        .save(json!({"person_id": "a", "person_name": "A", "accounts": {"alpha": {}}}))
        .unwrap_err();
    assert_eq!(code(&error), "account_refused");
    assert!(!world.identities().join("team/members/a").exists());
    assert!(!world.workspace().join("team/members/a").exists());
    assert!(!world.identities().join("secrets.yml").exists());
    // Forms the models refuse are invalid requests, and write nothing.
    for accounts in [json!({"gamma": {}}), json!({"alpha": {"unknown": 1}})] {
        let error = world
            .save(json!({"person_id": "a", "person_name": "A", "accounts": accounts}))
            .unwrap_err();
        assert!(error.is::<InvalidInput>(), "{error:#}");
    }
    assert!(!world.identities().join("team/members/a").exists());
    assert!(!world.workspace().join("team/members/a").exists());
    assert!(!world.identities().join("secrets.yml").exists());
}

#[test]
fn refused_or_failed_account_leaves_files_and_secrets_unchanged() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world.save(agent("a")).unwrap();
    let before = bytes_under(world.dir.path());
    let models = refusing(&["prepare:alpha"]);
    let refused = Setup {
        providers: &models,
        ..world.setup()
    };
    let error = save(
        &refused,
        json!({"person_id": "a", "person_name": "New", "accounts": {"alpha": {"token": "new"}}}),
        &Revisions::new(),
    )
    .unwrap_err();
    assert_eq!(code(&error), "provider_refused");
    assert_eq!(bytes_under(world.dir.path()), before);
    assert_eq!(world.identity_secrets()["A_ALPHA_TOKEN"], "alpha-1");
}

#[test]
fn keychain_failure_leaves_both_member_files_absent() {
    let world = World::new();
    let error = save(&world.setup_with(&Locked), agent("a"), &Revisions::new()).unwrap_err();
    assert!(error.is::<SecretsUnavailable>(), "{error:#}");
    assert!(
        !world
            .identities()
            .join("team/members/a/identity.yml")
            .exists()
    );
    assert!(!world.workspace().join("team/members/a/member.yml").exists());
}

#[test]
fn keychain_failure_on_update_or_delete_changes_nothing() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world.save(agent("a")).unwrap();
    let before = bytes_under(world.dir.path());
    let update =
        json!({"person_id": "a", "person_name": "New", "accounts": {"alpha": {"token": "new"}}});
    let error = save(&world.setup_with(&Locked), update, &Revisions::new()).unwrap_err();
    assert!(error.is::<SecretsUnavailable>(), "{error:#}");
    assert_eq!(bytes_under(world.dir.path()), before);
    let error = block_on(world.setup_with(&Locked).delete_identity("a")).unwrap_err();
    assert!(error.is::<SecretsUnavailable>(), "{error:#}");
    assert!(
        world
            .identities()
            .join("team/members/a/identity.yml")
            .is_file()
    );
    block_on(world.setup().delete_identity("a")).unwrap();
    assert!(!world.identities().join("team/members/a").exists());
}

#[test]
fn ambiguous_identity_can_be_removed_without_reading_or_deleting_shared_secrets() {
    for (kept, removed) in [("alice-bak", "alice_bak"), ("Alice", "alıce")] {
        let world = World::new();
        world.init(json!({"language": "en"}));
        let shared = format!("{}ALPHA_TOKEN", kept.replace('-', "_").to_uppercase() + "_");
        let store = world
            .repo
            .identity_secrets(device(world.dir.path()))
            .unwrap();
        let prefix = kept.replace('-', "_").to_uppercase() + "_";
        for name in world.models.member_secret_names() {
            store.set(&format!("{prefix}{name}"), "shared").unwrap();
        }
        for id in [kept, removed] {
            world.identity_file(id, &format!("person_id: {id}\nname: {id}\n"));
            world.write(
                &format!("team/members/{id}/member.yml"),
                "is_active: true\n",
            );
        }
        let sealed_path = world
            .dir
            .path()
            .join("identity/.guildbotics/local/secrets.enc.json");
        let sealed = fs::read(&sealed_path).unwrap();
        let untouched = world.setup_with(&Untouchable);
        let read = untouched.read_member(removed).unwrap();
        assert_eq!(read.accounts.keys().collect::<Vec<_>>(), ["alpha", "beta"]);
        assert!(
            read.accounts
                .values()
                .all(|view| view["has_token"] == false)
        );
        untouched.remove_member(removed).unwrap();
        assert!(
            !world
                .workspace()
                .join("team/members")
                .join(removed)
                .exists()
        );
        assert!(
            world
                .identities()
                .join("team/members")
                .join(removed)
                .exists()
        );
        if removed == "alice_bak" {
            block_on(untouched.delete_identity(removed)).unwrap();
            assert!(
                !world
                    .identities()
                    .join("team/members")
                    .join(removed)
                    .exists()
            );
            assert_eq!(fs::read(&sealed_path).unwrap(), sealed);
            // The other one owns the keys from now on and can replace them.
            let team =
                gb_config::team::Team::load(&world.workspace(), &world.dir.path().join("identity"))
                    .unwrap();
            assert_eq!(team.members[0].person_id, kept);
            world
                .save(json!({"person_id": kept, "person_name": "K", "accounts": {"alpha": {"token": "mine"}}}))
                .unwrap();
            assert_eq!(world.identity_secrets()[&shared], "mine");
        } else {
            // A repair name addresses no identity to delete.
            let error = block_on(untouched.delete_identity(removed)).unwrap_err();
            assert!(error.is::<InvalidInput>(), "{error:#}");
            assert!(
                world
                    .identities()
                    .join("team/members")
                    .join(removed)
                    .exists()
            );
            assert_eq!(fs::read(&sealed_path).unwrap(), sealed);
        }
        assert!(world.identities().join("team/members").join(kept).exists());
    }
}

#[test]
fn secret_namespace_collisions_refuse_only_the_colliding_target() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world
        .save(json!({"person_id": "foo-bar", "person_name": "F", "accounts": {"alpha": {"token": "f"}}}))
        .unwrap();
    world.identity_file("foo_bar", "person_id: foo_bar\n");
    world
        .save(json!({"person_id": "carol", "person_name": "C", "accounts": {"alpha": {"token": "c"}}}))
        .unwrap();
    assert_eq!(world.identity_secrets()["FOO_BAR_ALPHA_TOKEN"], "f");
    assert_eq!(world.identity_secrets()["CAROL_ALPHA_TOKEN"], "c");
}

#[test]
#[cfg(unix)]
fn recursive_removal_does_not_follow_child_links() {
    use std::os::unix::fs::symlink;
    let world = World::new();
    world
        .save(json!({"person_id": "foo-bar", "person_name": "F", "accounts": {"alpha": {"token": "f"}}}))
        .unwrap();
    let outside = world.dir.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("keep"), "unrelated").unwrap();
    let member = world.workspace().join("team/members/foo-bar");
    symlink(&outside, member.join("linked")).unwrap();
    world.setup().remove_member("foo-bar").unwrap();
    assert!(outside.join("keep").is_file());
    assert!(!member.exists());
    assert_eq!(world.identity_secrets()["FOO_BAR_ALPHA_TOKEN"], "f");
}

#[test]
fn identity_is_locked_before_waiting_for_workspace() {
    let world = World::new();
    let workspace = world.dir.path().join("workspace");
    let identity = world.dir.path().join("identity");
    let _workspace_lock = IdentityLock::acquire(&workspace, Duration::from_millis(30)).unwrap();
    std::thread::scope(|scope| {
        let handle = scope.spawn(|| world.save(json!({"person_id": "a", "person_name": "A"})));
        let lockfile = identity.join(".guildbotics/local/run/shared-write.lock");
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            assert!(std::time::Instant::now() < deadline);
            if lockfile.exists()
                && gb_base::files::FileLock::try_hold(&lockfile)
                    .unwrap()
                    .is_none()
            {
                break;
            }
            std::thread::yield_now();
        }
        // The workspace cannot be obtained, yet the outer identity is held.
        assert!(
            handle
                .join()
                .unwrap()
                .unwrap_err()
                .is::<SharedWriteBusyError>()
        );
    });
    assert!(
        !identity
            .join(".guildbotics/config/team/members/a/identity.yml")
            .exists()
    );
}

#[test]
fn set_default_person_reads_the_project_inside_the_span() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world.save(agent("a")).unwrap();
    // While another writer holds the identities, nothing is read: the
    // missing member would otherwise be refused instead.
    let _held = IdentityLock::acquire(
        &world.dir.path().join("identity"),
        Duration::from_millis(30),
    )
    .unwrap();
    fs::remove_file(world.workspace().join("team/members/a/member.yml")).unwrap();
    fs::write(world.workspace().join("team/project.yml"), "[").unwrap();
    fs::write(
        world.identities().join("team/members/a/identity.yml"),
        "person_type: human\n",
    )
    .unwrap();
    let error = world.setup().set_default_person("a").unwrap_err();
    assert!(error.is::<SharedWriteBusyError>(), "{error:#}");
}

#[test]
fn set_default_person_writes_clears_and_refuses_with_codes() {
    let world = World::new();
    world.init(json!({"language": "ja", "description": "d"}));
    world.save(agent("a")).unwrap();
    world.setup().set_default_person("a").unwrap();
    assert_eq!(world.project()["default_person_id"], "a");
    assert_eq!(world.project()["language"], "ja");
    let revisions = world.repo.revisions(&["team/project.yml"]).unwrap();
    let receipt = world.setup().set_default_person("a").unwrap();
    assert_eq!(
        receipt.revisions["team/project.yml"],
        revisions["team/project.yml"]
    );
    let before = fs::read(world.workspace().join("team/project.yml")).unwrap();
    assert_eq!(
        code(&world.setup().set_default_person("ghost").unwrap_err()),
        "person_not_found"
    );
    for malformed in [
        "../members/alice",
        "alice/../alice",
        "Alice",
        "alice bob",
        "alice.md",
    ] {
        assert_eq!(
            code(&world.setup().set_default_person(malformed).unwrap_err()),
            "invalid_person_id"
        );
    }
    assert_eq!(
        fs::read(world.workspace().join("team/project.yml")).unwrap(),
        before
    );
    world.setup().set_default_person("").unwrap();
    assert!(world.project().get("default_person_id").is_none());
    assert_eq!(world.project()["description"], "d");
}

#[test]
fn member_ids_are_validated_by_every_entry() {
    let world = World::new();
    world.write("team/members/con/member.yml", "is_active: true\n");
    for bad in ["../../.ssh", "con"] {
        let error = world
            .save(json!({"person_id": bad, "person_name": "x"}))
            .unwrap_err();
        assert!(error.is::<InvalidInput>(), "{bad}");
        let error = block_on(world.setup().delete_identity(bad)).unwrap_err();
        assert!(error.is::<InvalidInput>(), "{bad}");
        let error = block_on(world.setup().change_avatar(bad, b"x", "a.png")).unwrap_err();
        assert!(error.is::<InvalidInput>(), "{bad}");
        assert_eq!(
            code(&world.setup().set_default_person(bad).unwrap_err()),
            "invalid_person_id"
        );
    }
    // A repair name can still be read and removed, never an escaping one.
    assert!(world.setup().read_member("con").unwrap().participating);
    assert!(
        world
            .setup()
            .read_member("../../.ssh")
            .unwrap_err()
            .is::<InvalidInput>()
    );
    assert!(
        world
            .setup()
            .remove_member("../../.ssh")
            .unwrap_err()
            .is::<InvalidInput>()
    );
    world.setup().remove_member("con").unwrap();
    assert!(!world.workspace().join("team/members/con").exists());
}

#[test]
fn new_project_starts_mappings_from_templates() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    assert_eq!(
        read(&world.workspace().join("intelligences/model_mapping.yml"))["default"],
        "models/openai/default.yml"
    );
    assert_eq!(
        read(
            &world
                .workspace()
                .join("intelligences/cli_agent_mapping.yml")
        )["default"],
        "cli_agents/codex/default.yml"
    );
}

#[test]
fn project_save_keeps_default_person_and_existing_mapping_defaults() {
    let world = World::new();
    world.init(json!({"language": "en", "llm_api_type": "gemini", "cli_agent": "claude"}));
    world.save(agent("a")).unwrap();
    world.setup().set_default_person("a").unwrap();
    let update = serde_json::from_value(json!({"language": "ja", "description": "Ops"})).unwrap();
    world
        .setup()
        .update_project(&update, &Revisions::new())
        .unwrap();
    assert_eq!(world.project()["default_person_id"], "a");
    assert_eq!(
        read(&world.workspace().join("intelligences/model_mapping.yml"))["default"],
        "models/gemini/default.yml"
    );
    assert_eq!(
        read(
            &world
                .workspace()
                .join("intelligences/cli_agent_mapping.yml")
        )["default"],
        "cli_agents/claude/default.yml"
    );
}

#[test]
fn project_services_are_replaced_whole_and_empty_removes_them() {
    let world = World::new();
    world.init(
        json!({"language": "en", "services": {"ticket_service": {"name": "alpha"},
        "code_hosting_service": {"name": "alpha"}, "chat_service": {"name": "beta"}}}),
    );
    let update = |services: Value| {
        let input =
            serde_json::from_value(json!({"language": "en", "services": services})).unwrap();
        world
            .setup()
            .update_project(&input, &Revisions::new())
            .unwrap();
    };
    update(json!({"chat_service": {"name": "beta"}}));
    assert_eq!(
        world.project()["services"],
        json!({"chat_service": {"name": "beta"}})
    );
    update(json!({}));
    assert!(world.project().get("services").is_none());
    assert!(world.setup().read_project().unwrap().services.is_empty());
}

#[test]
fn project_snapshot_reports_selections_key_presence_and_lowercased_provider_names() {
    let world = World::new();
    world.init(
        json!({"language": "ja", "llm_api_type": "anthropic", "cli_agent": "claude",
        "provider_api_keys": {"openai": "k"}}),
    );
    world.write(
        "team/project.yml",
        "language: ja\nservices:\n  chat_service:\n    name: Beta\n    extra: 1\n",
    );
    let snapshot = world.setup().read_project().unwrap();
    assert_eq!(
        snapshot.services["chat_service"],
        json!({"name": "beta", "extra": 1})
    );
    assert_eq!(snapshot.llm_api_type, "anthropic");
    assert_eq!(snapshot.cli_agent, "claude");
    assert_eq!(snapshot.language, "ja");
    assert!(snapshot.provider_api_keys["openai"]);
    assert!(!snapshot.provider_api_keys["gemini"]);
    let mut paths = PROJECT_CONFIG_PATHS.to_vec();
    paths.sort();
    assert_eq!(snapshot.revisions.keys().collect::<Vec<_>>(), paths);
    fs::remove_file(world.workspace().join("team/project.yml")).unwrap();
    assert_eq!(
        code(&world.setup().read_project().unwrap_err()),
        "project_not_found"
    );
}

#[test]
fn model_keys_use_workspace_store() {
    let world = World::new();
    world.init(json!({"language": "en", "provider_api_keys": {"openai": "sk-1", "unknown": "x"}}));
    assert_eq!(
        world.workspace_secrets(),
        BTreeMap::from([("OPENAI_API_KEY".to_owned(), "sk-1".to_owned())])
    );
    assert!(
        !world
            .dir
            .path()
            .join("identity/.guildbotics/config/secrets.yml")
            .exists()
    );
}

#[test]
fn model_keys_are_trimmed_and_added_without_removing_others() {
    let world = World::new();
    world.init(json!({"language": "en", "provider_api_keys": {"openai": " sk-test\n"}}));
    assert_eq!(world.workspace_secrets()["OPENAI_API_KEY"], "sk-test");
    let update = serde_json::from_value(json!({"language": "en",
        "provider_api_keys": {"anthropic": "a-key", "gemini": ""}}))
    .unwrap();
    world
        .setup()
        .update_project(&update, &Revisions::new())
        .unwrap();
    assert_eq!(
        world.workspace_secrets(),
        BTreeMap::from([
            ("ANTHROPIC_API_KEY".to_owned(), "a-key".to_owned()),
            ("OPENAI_API_KEY".to_owned(), "sk-test".to_owned()),
        ])
    );
}

#[test]
fn templates_are_bytes_samples_are_not_overwritten_and_paths_are_checked_first() {
    let world = World::new();
    world.write("commands/custom.md", "mine\n");
    world.init(json!({"language": "en"}));
    assert_eq!(
        fs::read_to_string(world.workspace().join("commands/custom.md")).unwrap(),
        "mine\n"
    );
    assert!(!world.workspace().join("commands/translate.md").exists());
    // The definitions land byte for byte, whatever their newlines.
    let mut source = World::new();
    let copied = source.dir.path().join("templates");
    copy_tree(&templates(), &copied);
    fs::write(
        copied.join("intelligences/cli_agents/codex/default.yml"),
        b"a: 1\r\nb: 2\r\n",
    )
    .unwrap();
    source.templates = copied.clone();
    source.init(json!({"language": "ja"}));
    assert_eq!(
        fs::read(
            source
                .workspace()
                .join("intelligences/cli_agents/codex/default.yml")
        )
        .unwrap(),
        b"a: 1\r\nb: 2\r\n"
    );
    for tool in ["antigravity", "claude", "copilot", "grok"] {
        let relative = format!("intelligences/cli_agents/{tool}/default.yml");
        assert_eq!(
            fs::read(source.workspace().join(&relative)).unwrap(),
            fs::read(copied.join(&relative)).unwrap()
        );
    }
    assert_eq!(
        fs::read(source.workspace().join("commands/translate.md")).unwrap(),
        fs::read(templates().join("sample_commands/ja/translate.md")).unwrap()
    );
    // An update leaves the definitions as they are and seeds no samples
    // over existing commands.
    fs::write(
        source
            .workspace()
            .join("intelligences/cli_agents/codex/default.yml"),
        "edited\n",
    )
    .unwrap();
    let update = serde_json::from_value(json!({"language": "en"})).unwrap();
    source
        .setup()
        .update_project(&update, &Revisions::new())
        .unwrap();
    assert_eq!(
        fs::read_to_string(
            source
                .workspace()
                .join("intelligences/cli_agents/codex/default.yml")
        )
        .unwrap(),
        "edited\n"
    );
}
fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap().flatten() {
        let target = to.join(entry.file_name());
        if entry.path().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

/// A sample command's declaration: a Markdown file's front matter, or a
/// YAML command.
fn declaration(language: &str, name: &str) -> Value {
    let text = fs::read_to_string(
        templates()
            .join("sample_commands")
            .join(language)
            .join(name),
    )
    .unwrap();
    if name.ends_with(".md") {
        let front = text
            .trim_start_matches("---\n")
            .split("\n---")
            .next()
            .unwrap();
        gb_config::yaml_value(front).unwrap()
    } else {
        gb_config::yaml_value(&text).unwrap()
    }
}

#[test]
fn sample_commands_declare_their_contracts() {
    let brains = read(&templates().join("intelligences/brain_mapping.yml"));
    for (language, default_language) in [("en", "English"), ("ja", "日本語")] {
        for (name, input) in [
            ("context-info.md", "hidden"),
            ("summarize.md", "hidden"),
            ("translate.md", "required"),
        ] {
            assert_eq!(
                declaration(language, name)["inputs"],
                json!({"message": input}),
                "{language}/{name}"
            );
        }
        for entry in fs::read_dir(templates().join("sample_commands").join(language)).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().into_owned();
            if !name.ends_with(".md") {
                continue;
            }
            let brain = declaration(language, &name)["brain"]
                .as_str()
                .unwrap_or("default")
                .to_owned();
            assert!(
                ["none", "-", "null"].contains(&brain.as_str()) || brains.get(&brain).is_some(),
                "{language}/{name}: {brain}"
            );
        }
        let translate = declaration(language, "translate.md");
        assert_eq!(translate["brain"], "default");
        assert_eq!(
            translate["commands"],
            json!([{"name": "os_ui_language", "command": "functions/get_os_ui_language"}])
        );
        let source = fs::read_to_string(
            templates()
                .join("sample_commands")
                .join(language)
                .join("translate.md"),
        )
        .unwrap();
        for absent in [
            "${1}",
            "${2}",
            "context.language_code",
            "context.language_name",
        ] {
            assert!(!source.contains(absent), "{language}: {absent}");
        }
        let time = declaration(language, "get-time-of-day.yml");
        assert_eq!(time["inputs"], json!({"message": "hidden"}));
        let last = time["commands"].as_array().unwrap().last().unwrap();
        assert_eq!(last["template_engine"], "jinja2");
        let prompt = last["prompt"].as_str().unwrap();
        for part in [
            "{{ context.person.name }}",
            "{{ context.person.speaking_style }}",
            "{{ current_time }}",
            "{{ time_of_day.label }}",
        ] {
            assert!(prompt.contains(part), "{language}: {part}");
        }
        assert_eq!(
            declaration(language, "summarize.md")["args"],
            json!({"file": {"required": true}, "language": {"default": default_language}})
        );
    }
}

#[test]
fn an_empty_workspace_model_definition_leaves_the_templates_key() {
    let world = World::new();
    world.init(json!({"language": "en"}));
    world.write("intelligences/models/openai/default.yml", "");
    world.write(
        "intelligences/models/custom/default.yml",
        "api_key_env: CUSTOM_KEY\n",
    );
    let update = serde_json::from_value(json!({"language": "en",
        "provider_api_keys": {"openai": "o", "custom": "c"}}))
    .unwrap();
    world
        .setup()
        .update_project(&update, &Revisions::new())
        .unwrap();
    assert_eq!(world.workspace_secrets()["OPENAI_API_KEY"], "o");
    assert_eq!(world.workspace_secrets()["CUSTOM_KEY"], "c");
    let keys = world.setup().read_project().unwrap().provider_api_keys;
    assert!(keys["openai"] && keys["custom"]);
}
