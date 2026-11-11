//! This machine's command line as a client of the server on its port, as the
//! Python version's (`guildbotics/utils/local_api.py`) is with the owner
//! token file: whatever listens on the port first sees only the token's id
//! and a fresh nonce, and the token is sent only once the answer proves that
//! the listener knows the token's digest.

use std::{
    path::{Path, PathBuf},
    time::Duration,
};

use serde_json::{Value, json};

use crate::{auth, i18n};

pub mod connect;
pub mod diagnostics;
mod git_credentials;
pub mod member;
pub mod secrets;

const PROOF_TIMEOUT: Duration = Duration::from_secs(2);
const TIMEOUT: Duration = Duration::from_secs(10);
/// Read a language explicitly selected in the shell, or use the OS UI language.
pub async fn language() -> &'static str {
    let selected = gb_environment::language::posix_tag(|name| std::env::var(name).ok());
    if let Some(selected) = selected {
        return i18n::language(&selected);
    }
    i18n::language(
        &gb_environment::language::ui_language()
            .await
            .unwrap_or_default(),
    )
}

/// A client authorized by this machine's owner token. Only the proof
/// endpoint receives requests before the listener authenticates itself.
pub struct OwnerClient {
    http: reqwest::Client,
    base: String,
    token: String,
    language: &'static str,
}

impl OwnerClient {
    pub async fn connect(
        data: &std::path::Path,
        port: u16,
        language: &'static str,
    ) -> anyhow::Result<Option<Self>> {
        let token =
            match std::fs::read_to_string(auth::directory(data, port).join(auth::OWNER_TOKEN)) {
                Ok(token) => token,
                Err(_) => return Ok(None),
            };
        Self::with_token(port, language, token).await
    }

    async fn with_token(
        port: u16,
        language: &'static str,
        token: String,
    ) -> anyhow::Result<Option<Self>> {
        let base = format!("http://127.0.0.1:{port}/-/guildbotics/api");
        let http = http()?;
        let Some((id, secret)) = auth::parse_token(&token) else {
            return Ok(None);
        };
        if !proves(&http, &base, id, &loopback_auth::digest(secret)).await {
            return Ok(None);
        }
        Ok(Some(Self {
            http,
            base,
            token,
            language,
        }))
    }

    pub fn request(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        self.http
            .request(method, format!("{}{path}", self.base))
            .bearer_auth(self.token.trim())
            .header("x-guildbotics-language", self.language)
    }

    async fn health(&self) -> Option<Value> {
        self.request(reqwest::Method::GET, "/health")
            .timeout(PROOF_TIMEOUT)
            .send()
            .await
            .ok()?
            .error_for_status()
            .ok()?
            .json()
            .await
            .ok()
    }

    pub async fn post(
        &self,
        path: &str,
        body: Option<&Value>,
        timeout: Option<Duration>,
    ) -> anyhow::Result<Value> {
        let mut request = self.request(reqwest::Method::POST, path);
        if let Some(body) = body {
            request = request.json(body);
        }
        if let Some(timeout) = timeout {
            request = request.timeout(timeout);
        }
        Ok(checked(request.send().await?).await?.json().await?)
    }
}

fn http() -> anyhow::Result<reqwest::Client> {
    Ok(reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(PROOF_TIMEOUT)
        .build()?)
}

/// Keep the status on errors whose response has no usable explanation.
pub async fn checked(response: reqwest::Response) -> anyhow::Result<reqwest::Response> {
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }
    let answer: Value = response.json().await.unwrap_or_default();
    anyhow::bail!(response_error(status, &answer))
}

pub(crate) fn response_error(status: reqwest::StatusCode, answer: &Value) -> String {
    answer["message"]
        .as_str()
        .filter(|text| !text.trim().is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| format!("HTTP {status}"))
}

pub(crate) fn command_output(answer: Value, language: &str) -> anyhow::Result<String> {
    answer["output"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| anyhow::anyhow!(i18n::t(language, "cli.run.invalid_output", &[])))
}

/// Run on the server that has this workspace open, or use the same execution
/// boundary locally when there is no matching server. Never retry after POST.
pub async fn run_command(
    spec: &str,
    args: Vec<String>,
    person: Option<String>,
    message: impl FnOnce() -> std::io::Result<String>,
    cwd: Option<std::path::PathBuf>,
) -> anyhow::Result<String> {
    use crate::commands::RunRequest;
    use gb_base::host_path;
    let language = language().await;
    let (name, inline) = spec.split_once('@').unwrap_or((spec, ""));
    let name = name.trim();
    anyhow::ensure!(
        !name.is_empty(),
        i18n::t(language, "cli.run.empty_command", &[])
    );
    let data = gb_base::home::data()?;
    let workspace = crate::workspace::resolve(&data, None, language)?;
    let cwd = host_path::normalize(&match cwd {
        Some(cwd) => cwd,
        None => std::env::current_dir()?,
    })?;
    let request = RunRequest {
        command: name.to_owned(),
        args,
        person: person.filter(|person| !person.is_empty()).or_else(|| {
            let inline = inline.trim();
            (!inline.is_empty()).then(|| inline.to_owned())
        }),
        message: message()?,
        cwd: Some(cwd),
        expected_workspace: Some(workspace.clone()),
        expected_command_file_id: None,
        expected_command_file_revision: None,
    };
    if let Some(running) = crate::server::running(&data, language)?
        && let Some(owner) = OwnerClient::connect(&data, running.port, language).await?
        && let Some(health) = owner.health().await
        && health["workspace"] == json!(workspace)
    {
        let response = owner
            .request(reqwest::Method::POST, "/commands/run")
            .json(&request)
            .send()
            .await?;
        if !response.status().is_success() {
            let status = response.status().as_u16();
            let text = response.text().await?;
            let error = serde_json::from_str::<Value>(&text).unwrap_or_default();
            if let Some(message) = error["message"].as_str() {
                anyhow::bail!("{message}");
            }
            anyhow::bail!("HTTP {status}: {text}");
        }
        return command_output(response.json().await?, language);
    }
    crate::commands::run_locally(request, &workspace, &data, language).await
}

pub async fn git_credential(action: &str) -> anyhow::Result<()> {
    let language = language().await;
    answer_git_credential(
        action,
        std::io::stdin().lock(),
        std::io::stdout().lock(),
        std::io::stderr().lock(),
        language,
        async |origin: String| {
            git_credentials::credential(&origin, |data| {
                let device = gb_workspace::identity::ensure_device_identity(data)?;
                Ok((device.device_id, device.display_name))
            })
            .await
        },
    )
    .await
}

async fn answer_git_credential(
    action: &str,
    input: impl std::io::BufRead,
    mut stdout: impl std::io::Write,
    mut stderr: impl std::io::Write,
    language: &str,
    lookup: impl AsyncFnOnce(String) -> anyhow::Result<Option<gb_ports::GitCredential>>,
) -> anyhow::Result<()> {
    let mut attributes = std::collections::BTreeMap::new();
    for line in input.split(b'\n') {
        let line = line?;
        let text = String::from_utf8_lossy(&line);
        let text = text.trim_end_matches('\r');
        if text.is_empty() {
            break;
        }
        if let Some((name, value)) = text.split_once('=')
            && matches!(name, "protocol" | "host")
        {
            attributes.insert(name.to_owned(), value.to_owned());
        }
    }
    if action != "get" {
        return Ok(());
    }
    let value = |name| attributes.get(name).map(String::as_str).unwrap_or_default();
    let protocol = value("protocol");
    let host = value("host");
    // The protocol fields are untrusted. Do not echo a userinfo or control
    // sequence masquerading as the remote's host.
    let origin = format!("{protocol}://{host}");
    let safe = !protocol.is_empty()
        && protocol.bytes().all(|b| b.is_ascii_alphabetic())
        && host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".:_-[]".contains(&b));
    let origin = if safe { origin.as_str() } else { "" };
    match lookup(origin.to_owned()).await {
        Ok(Some(credential)) => {
            stdout.write_all(
                format!(
                    "username={}\npassword={}\n",
                    credential.username, credential.password
                )
                .as_bytes(),
            )?;
        }
        Ok(None) => writeln!(
            stderr,
            "{}",
            i18n::t(
                language,
                "cli.git_credential.not_served",
                &[("origin", origin)]
            )
        )?,
        Err(error) => {
            let message = match error.downcast_ref::<gb_ports::ServiceError>() {
                Some(error) => {
                    let args = error.translation_args();
                    let args = args
                        .iter()
                        .map(|(key, value)| (key.as_str(), value.as_str()))
                        .collect::<Vec<_>>();
                    i18n::t(language, error.translation_key(), &args)
                }
                None => i18n::t(
                    language,
                    "integrations.branvil.owner_git.unavailable",
                    &[("reason", &error.to_string())],
                ),
            };
            writeln!(stderr, "{message}")?;
            stdout.write_all(b"quit=1\n")?;
        }
    }
    Ok(())
}

/// A single-use link into the screens of the server on this machine's port,
/// or the sentence saying why there is none.
pub async fn entry_link() -> Result<String, String> {
    let port = crate::server::port().map_err(|e| e.to_string())?;
    let data = gb_base::home::data().map_err(|e| format!("{e:#}"))?;
    let language = language().await;
    let owner = entry_client(&data, port, language, connect::credential)
        .await
        .map_err(|error| error.to_string())?
        .ok_or_else(|| i18n::t(language, "cli.server.unreachable", &[]))?;
    let refused = |reason: String| i18n::t(language, "cli.server.refused", &[("reason", &reason)]);
    let answer = owner
        .post("/session/links", Some(&json!({})), Some(TIMEOUT))
        .await
        .map_err(|e| refused(e.to_string()))?;
    answer["url"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| i18n::t(language, "cli.server.invalid_link", &[]))
}

async fn entry_client(
    data: &Path,
    port: u16,
    language: &'static str,
    consent: impl FnOnce(u16) -> Option<String>,
) -> anyhow::Result<Option<OwnerClient>> {
    if let Some(owner) = OwnerClient::connect(data, port, language).await? {
        return Ok(Some(owner));
    }
    match consent(port) {
        Some(token) => OwnerClient::with_token(port, language, token).await,
        None => Ok(None),
    }
}

/// Resolve a CLI user's file or directory, including existing symlink ancestors.
fn caller_path(path: &Path) -> std::io::Result<PathBuf> {
    let expanded = gb_base::host_path::expand_home(path);
    for ancestor in expanded.ancestors() {
        if !ancestor.ends_with("..") {
            continue;
        }
        let Some(parent) = ancestor
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
        else {
            continue;
        };
        match parent.metadata() {
            Ok(metadata) if !metadata.is_dir() => {
                return Err(std::io::ErrorKind::NotADirectory.into());
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    match expanded.canonicalize() {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            gb_base::shared_writes::resolved(&expanded)
        }
        result => result,
    }
}

/// Whether the listener on the port answers a fresh nonce as only the holder
/// of the token's digest can.
async fn proves(http: &reqwest::Client, base: &str, id: &str, digest: &str) -> bool {
    let nonce = loopback_auth::hex(&loopback_auth::random_bytes::<32>());
    let answer = http
        .post(format!("{base}/local-api/proof"))
        .timeout(PROOF_TIMEOUT)
        .json(&json!({"token_id": id, "nonce": nonce}))
        .send()
        .await
        .and_then(reqwest::Response::error_for_status);
    let Ok(answer) = answer else {
        return false;
    };
    let proof = answer.json::<Value>().await.unwrap_or_default();
    proof["proof"].as_str() == Some(&auth::proof(digest, id, &nonce))
}

#[cfg(test)]
mod tests {
    use super::answer_git_credential;

    #[cfg(unix)]
    #[test]
    fn caller_paths_accept_missing_destinations_but_not_a_file_as_parent() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        std::fs::create_dir(root.join("sub")).unwrap();
        std::fs::write(root.join("file"), "fixture").unwrap();
        assert_eq!(
            super::caller_path(&root.join("sub/../destination")).unwrap(),
            root.join("destination")
        );
        assert_eq!(
            super::caller_path(&root.join("file/../destination"))
                .unwrap_err()
                .kind(),
            std::io::ErrorKind::NotADirectory,
        );
    }

    #[tokio::test]
    async fn proof_uses_fresh_nonces_and_sends_auth_and_language_only_after_success() {
        use super::*;
        use axum::{Json, Router, http::HeaderMap, routing::post};
        use std::sync::{Arc, Mutex};

        const TOKEN: &str = "gbt_0123456789abcdef_consent";
        let seen = Arc::new(Mutex::new(Vec::<String>::new()));
        let app = Router::new()
            .route(
                "/-/guildbotics/api/local-api/proof",
                post({
                    let seen = seen.clone();
                    move |headers: HeaderMap, Json(body): Json<Value>| {
                        let seen = seen.clone();
                        async move {
                            assert!(!headers.contains_key("authorization"));
                            assert!(!headers.contains_key("x-guildbotics-language"));
                            let nonce = body["nonce"].as_str().unwrap();
                            let mut seen = seen.lock().unwrap();
                            assert!(!seen.iter().any(|value| value == nonce));
                            seen.push(nonce.to_owned());
                            let (id, secret) = auth::parse_token(TOKEN).unwrap();
                            let proof = if body["token_id"] == id {
                                auth::proof(&loopback_auth::digest(secret), id, nonce)
                            } else {
                                String::new()
                            };
                            Json(json!({"proof":proof}))
                        }
                    }
                }),
            )
            .route(
                "/-/guildbotics/api/session/links",
                post(|headers: HeaderMap| async move {
                    assert_eq!(headers["authorization"], format!("Bearer {TOKEN}").as_str());
                    assert_eq!(headers["x-guildbotics-language"], "ja");
                    Json(json!({"url":"fixture"}))
                }),
            );
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let peer = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let data = tempfile::tempdir().unwrap();
        assert!(
            OwnerClient::connect(data.path(), port, "ja")
                .await
                .unwrap()
                .is_none()
        );
        assert!(seen.lock().unwrap().is_empty());
        let token_path = auth::directory(data.path(), port).join(auth::OWNER_TOKEN);
        std::fs::create_dir_all(token_path.parent().unwrap()).unwrap();
        std::fs::write(&token_path, "gbt_abcdef0123456789_stale").unwrap();
        assert!(
            entry_client(data.path(), port, "ja", |_| None)
                .await
                .unwrap()
                .is_none()
        );
        let consent = entry_client(data.path(), port, "ja", |selected| {
            assert_eq!(selected, port);
            Some(TOKEN.into())
        })
        .await
        .unwrap()
        .unwrap();
        assert_eq!(
            consent
                .post("/session/links", None, Some(TIMEOUT))
                .await
                .unwrap()["url"],
            "fixture"
        );
        std::fs::write(token_path, TOKEN).unwrap();
        let owner = entry_client(data.path(), port, "ja", |_| {
            panic!("owner proof precedes keychain lookup")
        })
        .await
        .unwrap()
        .unwrap();
        owner
            .post("/session/links", None, Some(TIMEOUT))
            .await
            .unwrap();
        assert_eq!(seen.lock().unwrap().len(), 4);
        peer.abort();
    }

    #[tokio::test]
    async fn a_bad_proof_never_authenticates_an_owner_or_consent_token() {
        use super::*;
        use axum::{Router, http::HeaderMap, routing::post};
        use std::sync::{Arc, Mutex};

        const OWNER: &str = "gbt_0123456789abcdef_owner";
        const CONSENT: &str = "gbt_abcdef0123456789_consent";
        for mode in ["replay", "non-json", "array", "number", "non-ascii", "slow"] {
            let ids = Arc::new(Mutex::new(Vec::new()));
            let app = Router::new().route(
                "/-/guildbotics/api/local-api/proof",
                post({
                    let ids = ids.clone();
                    move |headers: HeaderMap, axum::Json(body): axum::Json<Value>| {
                        let ids = ids.clone();
                        async move {
                            assert!(!headers.contains_key("authorization"));
                            assert!(!headers.contains_key("x-guildbotics-language"));
                            let id = body["token_id"].as_str().unwrap();
                            ids.lock().unwrap().push(id.to_owned());
                            match mode {
                                "replay" => {
                                    let (_, secret) = auth::parse_token(if id.starts_with("012") {
                                        OWNER
                                    } else {
                                        CONSENT
                                    })
                                    .unwrap();
                                    let proof = auth::proof(
                                        &loopback_auth::digest(secret),
                                        id,
                                        &"0".repeat(64),
                                    );
                                    json!({"proof":proof}).to_string()
                                }
                                "non-json" => "{".into(),
                                "array" => "[{}]".into(),
                                "number" => "{\"proof\":42}".into(),
                                "non-ascii" => "{\"proof\":\"偽\"}".into(),
                                "slow" => {
                                    tokio::time::sleep(PROOF_TIMEOUT + Duration::from_secs(1))
                                        .await;
                                    let (_, secret) = auth::parse_token(if id.starts_with("012") {
                                        OWNER
                                    } else {
                                        CONSENT
                                    })
                                    .unwrap();
                                    let nonce = body["nonce"].as_str().unwrap();
                                    let proof =
                                        auth::proof(&loopback_auth::digest(secret), id, nonce);
                                    json!({"proof":proof}).to_string()
                                }
                                _ => unreachable!(),
                            }
                        }
                    }
                }),
            );
            let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
                .await
                .unwrap();
            let port = listener.local_addr().unwrap().port();
            let peer = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
            let data = tempfile::tempdir().unwrap();
            let token_path = auth::directory(data.path(), port).join(auth::OWNER_TOKEN);
            std::fs::create_dir_all(token_path.parent().unwrap()).unwrap();
            std::fs::write(token_path, OWNER).unwrap();
            assert!(
                entry_client(data.path(), port, "ja", |_| Some(CONSENT.into()))
                    .await
                    .unwrap()
                    .is_none(),
                "{mode}"
            );
            assert_eq!(
                *ids.lock().unwrap(),
                ["0123456789abcdef", "abcdef0123456789"],
                "{mode}"
            );
            peer.abort();
        }
    }

    #[tokio::test]
    async fn git_helper_distinguishes_unserved_origin_from_missing_backend() {
        let input = b"protocol=https\nhost=forge.example.invalid:9443\npath=a/b\npassword=fake-do-not-echo\n\n";
        for language in ["en", "ja"] {
            for missing_backend in [false, true] {
                let mut output = Vec::new();
                let mut error = Vec::new();
                let required = crate::i18n::t(language, "cli.git_credential.backend_required", &[]);
                answer_git_credential(
                    "get",
                    &input[..],
                    &mut output,
                    &mut error,
                    language,
                    async |origin| {
                        assert_eq!(origin, "https://forge.example.invalid:9443");
                        if missing_backend {
                            anyhow::bail!(required.clone());
                        }
                        Ok(None)
                    },
                )
                .await
                .unwrap();
                let error = String::from_utf8(error).unwrap();
                if missing_backend {
                    assert_eq!(output, b"quit=1\n");
                    assert!(error.contains(&required));
                    assert!(!required.contains("cli.git_credential.backend_required"));
                } else {
                    assert!(output.is_empty());
                    assert_eq!(
                        error.trim(),
                        crate::i18n::t(
                            language,
                            "cli.git_credential.not_served",
                            &[("origin", "https://forge.example.invalid:9443")]
                        )
                    );
                }
                assert!(!error.contains("fake-do-not-echo") && !error.contains("a/b"));
            }
        }
    }

    #[tokio::test]
    async fn git_helper_emits_credentials_for_the_origin_and_ends_the_search_on_failure() {
        let origin = "http://127.0.0.1:7878";
        for (input, fail) in [
            (
                "capability[]=authtype\r\ncapability[]=state\r\nprotocol=http\r\nhost=127.0.0.1:7878\r\npath=git/acme/demo.git\r\nwwwauth[]=Basic realm=branvil\r\n\r\n",
                false,
            ),
            ("protocol=http\nhost=127.0.0.1:7878", false),
            ("protocol=http\nhost=127.0.0.1:7878\n\n", true),
        ] {
            let mut output = Vec::new();
            let mut error = Vec::new();
            answer_git_credential(
                "get",
                input.as_bytes(),
                &mut output,
                &mut error,
                "en",
                async |asked| {
                    assert_eq!(asked, origin);
                    if fail {
                        return Err(gb_ports::ServiceError::Diagnostic {
                            class: gb_ports::ServiceErrorClass::Unavailable,
                            key: "integrations.branvil.api.unreachable",
                            args: [("base_url".into(), origin.into())].into(),
                        }
                        .into());
                    }
                    Ok(Some(gb_ports::GitCredential {
                        username: "otota".into(),
                        password: "bvt_fixture".into(),
                    }))
                },
            )
            .await
            .unwrap();
            if fail {
                assert_eq!(output, b"quit=1\n");
                assert_eq!(
                    String::from_utf8(error).unwrap(),
                    format!(
                        "Branvil is not reachable at {origin}. Check that Branvil is running on this machine.\n"
                    )
                );
            } else {
                assert_eq!(output, b"username=otota\npassword=bvt_fixture\n");
                assert!(error.is_empty());
            }
        }
    }
}
