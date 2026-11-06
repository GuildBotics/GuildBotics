//! This machine's command line as a client of the server on its port, as the
//! Python version's (`guildbotics/utils/local_api.py`) is with the owner
//! token file: whatever listens on the port first sees only the token's id
//! and a fresh nonce, and the token is sent only once the answer proves that
//! the listener knows the token's digest.

use std::time::Duration;

use serde_json::{Value, json};

use crate::{auth, i18n};

pub mod connect;
pub mod member;

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
        let base = format!("http://127.0.0.1:{port}/api");
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
    message: String,
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
        person: person.or_else(|| {
            let inline = inline.trim();
            (!inline.is_empty()).then(|| inline.to_owned())
        }),
        message,
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
        async |origin: String| gb_integrations::factory::human_git_credential(&origin).await,
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
            let reason = error
                .downcast_ref::<gb_ports::ServiceError>()
                .map(|error| {
                    let args = error.translation_args();
                    let args = args
                        .iter()
                        .map(|(key, value)| (key.as_str(), value.as_str()))
                        .collect::<Vec<_>>();
                    i18n::t(language, error.translation_key(), &args)
                })
                .unwrap_or_else(|| error.to_string());
            writeln!(
                stderr,
                "{}",
                i18n::t(
                    language,
                    "integrations.branvil.owner_git.unavailable",
                    &[("reason", &reason)]
                )
            )?;
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
    let local = OwnerClient::connect(&data, port, language)
        .await
        .map_err(|e| e.to_string())?;
    let owner = match local {
        Some(owner) => Some(owner),
        None => match connect::credential(port) {
            Some(token) => OwnerClient::with_token(port, language, token)
                .await
                .map_err(|e| e.to_string())?,
            None => None,
        },
    }
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
                        anyhow::bail!("Branvil is not reachable.");
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
                assert!(
                    String::from_utf8(error)
                        .unwrap()
                        .contains("Branvil is not reachable.")
                );
            } else {
                assert_eq!(output, b"username=otota\npassword=bvt_fixture\n");
                assert!(error.is_empty());
            }
        }
    }
}
