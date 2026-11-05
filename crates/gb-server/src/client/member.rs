//! The interactive CLI opens caller-owned files; the server owns the command.
use std::{
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

use gb_observability::redaction::Redactor;
use serde_json::{Value, json};

use super::{OwnerClient, checked};
use crate::{
    i18n,
    member::{MemberRequest, Result as Reply},
};

mod git;

const MAX_METADATA: usize = 32 << 20;
const MAX_CONTENT: usize = 2 << 20;

pub async fn run(mut arguments: Vec<String>) -> anyhow::Result<u8> {
    let language = super::language().await;
    let data = gb_base::home::data()?;
    let running =
        crate::server::running(&data, language)?.ok_or_else(|| backend_required(language))?;
    let owner = OwnerClient::connect(&data, running.port, language)
        .await?
        .ok_or_else(|| backend_required(language))?;
    let explicit = workspace_argument(&mut arguments, language)?;
    let workspace = selected_workspace(&data, explicit, language)?;
    let workspace_id = gb_workspace::identity::read_workspace_identity(&workspace)?
        .ok_or_else(|| {
            anyhow::anyhow!(i18n::t(
                language,
                "cli.member.client.workspace_required",
                &[]
            ))
        })?
        .workspace_id;
    let redact = redactor(&workspace)?;
    let original = arguments;
    let (interactive_host, interactive_thread_key) = interactive_context();
    let mut request = MemberRequest {
        version: crate::member::native::binary_version()?,
        workspace_id,
        arguments: redacted(&redact, &original),
        stdin: String::new(),
        interactive_host,
        interactive_thread_key,
        phase: None,
        data: Default::default(),
    };
    let client = Client { owner, language };
    let mut description = client.describe(&request).await?;
    if description.exit_code != 0 {
        return print(description);
    }
    if description
        .metadata
        .get("plan")
        .and_then(|plan| plan.get("result"))
        .is_none()
        && description
            .metadata
            .get("plan")
            .is_some_and(|plan| plan["memory"] != true)
        && request.arguments != original
    {
        request.arguments.clone_from(&original);
        description = client.describe(&request).await?;
        if description.exit_code != 0 {
            return print(description);
        }
    }
    let plan = description
        .metadata
        .get("plan")
        .ok_or_else(|| client.invalid_response())?;
    if let Some(result) = plan.get("result") {
        return print(serde_json::from_value(result.clone())?);
    }

    request.stdin = content(plan, &original, language)?;
    request.arguments = serde_json::from_value(plan["arguments"].clone())?;
    if plan["memory"] == true {
        request.arguments = redacted(&redact, &request.arguments);
        request.stdin = redact
            .local(request.stdin.into())
            .as_str()
            .unwrap_or_default()
            .to_owned();
    }
    if !plan["git"].is_null() {
        let receipt = git::run(&client, request, &plan["git"]).await?;
        println!(
            "{}",
            render(&receipt, &plan["git"]["output_format"], language)?
        );
        return Ok(0);
    }
    let download = plan["download"] == true;
    if download {
        request.arguments.extend(["--format".into(), "json".into()]);
    }
    let response = client.post("/member/run", &request).await?;
    let (mut result, mut response) = client.header(response).await?;
    let bytes = client
        .payload(
            &mut response,
            &result,
            if download {
                gb_member::archive::MAX_ARTIFACT_BYTES as usize
            } else {
                0
            },
        )
        .await?;
    if result.exit_code == 0 && download {
        let metadata = result
            .metadata
            .get("download")
            .ok_or_else(|| client.invalid_response())?;
        let destination = metadata["destination"]
            .as_str()
            .ok_or_else(|| client.invalid_response())?;
        let destination = gb_base::host_path::normalize(Path::new(destination))?;
        let attachment = metadata["attachment"]
            .as_array()
            .filter(|values| !values.is_empty());
        let files = match attachment {
            Some(values) => vec![
                gb_member::archive::attachment(
                    &bytes,
                    &destination,
                    values
                        .first()
                        .and_then(Value::as_str)
                        .ok_or_else(|| client.invalid_response())?,
                    values
                        .get(1)
                        .and_then(Value::as_str)
                        .ok_or_else(|| client.invalid_response())?,
                )
                .map_err(|e| client.render_error(e))?,
            ],
            None => gb_member::archive::extract(&bytes, &destination)
                .map_err(|e| client.render_error(e))?,
        };
        let mut payload: Value = serde_json::from_str(&result.stdout)?;
        payload["destination"] = json!(destination.canonicalize()?);
        payload["files"] = json!(files);
        result.stdout = format!("{}\n", render(&payload, &plan["output_format"], language)?);
    }
    print(result)
}

fn selected_workspace(
    data: &Path,
    chosen: Option<PathBuf>,
    language: &str,
) -> anyhow::Result<PathBuf> {
    use gb_workspace::selection::{CONFIG_DIR_ENV, WORKSPACE_ROOT_ENV, specified};
    let selected = match chosen {
        Some(path) => Some(path),
        None => {
            let configured = specified(None, std::env::var_os(CONFIG_DIR_ENV))?;
            if configured.is_some() {
                configured
            } else {
                let root = specified(std::env::var_os(WORKSPACE_ROOT_ENV), None)?;
                let cwd = std::env::current_dir()?;
                root.or_else(|| cwd.join(".guildbotics/config").is_dir().then_some(cwd))
            }
        }
    };
    crate::workspace::read_selection(data, selected, language)
}

fn workspace_argument(
    arguments: &mut Vec<String>,
    language: &str,
) -> anyhow::Result<Option<PathBuf>> {
    let mut selected = None;
    while let Some(argument) = arguments.first() {
        if let Some(path) = argument.strip_prefix("--workspace=") {
            selected = Some(PathBuf::from(path));
            arguments.remove(0);
        } else if argument == "--workspace" {
            anyhow::ensure!(
                arguments.len() >= 2,
                i18n::t(
                    language,
                    "cli.member.option_value",
                    &[("option", "--workspace")]
                )
            );
            selected = Some(PathBuf::from(arguments.remove(1)));
            arguments.remove(0);
        } else {
            break;
        }
    }
    Ok(selected)
}

fn content(plan: &Value, original: &[String], language: &str) -> anyhow::Result<String> {
    let mut bytes = Vec::new();
    if !plan["content_file"].is_null() {
        let path = plan["file_source"]["index"]
            .as_u64()
            .and_then(|index| original.get(index as usize))
            .and_then(|arg| {
                plan["file_source"]["offset"]
                    .as_u64()
                    .and_then(|offset| arg.get(offset as usize..))
            })
            .ok_or_else(|| {
                anyhow::anyhow!(i18n::t(language, "cli.member.client.invalid_response", &[]))
            })?;
        std::fs::File::open(path)
            .and_then(|file| file.take(MAX_CONTENT as u64 + 1).read_to_end(&mut bytes))
            .map_err(|e| {
                anyhow::anyhow!(i18n::t(
                    language,
                    "cli.member.content.file_read_failed",
                    &[("path", path), ("error", &e.to_string())]
                ))
            })?;
        anyhow::ensure!(
            bytes.len() <= MAX_CONTENT,
            i18n::t(language, "app_api.errors.member_request_too_large", &[])
        );
        return String::from_utf8(bytes).map_err(|_| {
            anyhow::anyhow!(i18n::t(
                language,
                "cli.member.content.file_not_utf8",
                &[("path", path)]
            ))
        });
    }
    if plan["stdin"] == true {
        io::stdin()
            .take(MAX_CONTENT as u64 + 1)
            .read_to_end(&mut bytes)?;
        anyhow::ensure!(
            bytes.len() <= MAX_CONTENT,
            i18n::t(language, "app_api.errors.member_request_too_large", &[])
        );
    }
    Ok(String::from_utf8(bytes)?)
}

fn interactive_context() -> (String, String) {
    let get = |key| {
        std::env::var(key)
            .ok()
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty())
    };
    let host = get("GUILDBOTICS_INTERACTIVE_HOST").unwrap_or_else(|| {
        for (kind, keys) in [
            ("codex", &["CODEX_THREAD_ID", "CODEX_SHELL"][..]),
            (
                "claude_code",
                &["CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDECODE"][..],
            ),
        ] {
            if keys.iter().any(|key| get(key).is_some()) {
                return kind.into();
            }
        }
        "unknown".into()
    });
    let thread = [
        "GUILDBOTICS_INTERACTIVE_THREAD_KEY",
        "CODEX_THREAD_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
    ]
    .into_iter()
    .find_map(get)
    .unwrap_or_default();
    (host, thread)
}

fn redactor(workspace: &Path) -> anyhow::Result<Redactor> {
    let mut names = gb_config::secrets::read_secret_index(&workspace.join(".guildbotics/config"))?;
    names.extend(gb_config::secrets::read_secret_index(
        &gb_base::home::identity_root()?.join(".guildbotics/config"),
    )?);
    let mut values = names
        .into_iter()
        .filter_map(|name| std::env::var(name).ok())
        .collect::<Vec<_>>();
    for (name, value) in std::env::vars_os() {
        let upper = name.to_string_lossy().to_ascii_uppercase();
        if ["TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "API_KEY"]
            .iter()
            .any(|key| upper.contains(key))
            && let Ok(value) = value.into_string()
        {
            values.push(value);
        }
    }
    Ok(Redactor::with_min_length(values, 4))
}

fn redacted(redactor: &Redactor, arguments: &[String]) -> Vec<String> {
    arguments
        .iter()
        .map(|arg| {
            redactor
                .local(arg.clone().into())
                .as_str()
                .unwrap_or_default()
                .to_owned()
        })
        .collect()
}

fn render(payload: &Value, format: &Value, language: &str) -> anyhow::Result<String> {
    Ok(if format == "json" {
        serde_json::to_string(payload)?
    } else {
        gb_member::output::markdown(payload, |key| i18n::t(language, key, &[]))
    })
}

fn print(result: Reply) -> anyhow::Result<u8> {
    io::stdout().write_all(result.stdout.as_bytes())?;
    io::stderr().write_all(result.stderr.as_bytes())?;
    Ok(u8::try_from(result.exit_code).unwrap_or(1))
}

fn backend_required(language: &str) -> anyhow::Error {
    anyhow::anyhow!(i18n::t(language, "cli.member.client.backend_required", &[]))
}

struct Client {
    owner: OwnerClient,
    language: &'static str,
}

impl Client {
    fn invalid_response(&self) -> anyhow::Error {
        anyhow::anyhow!(i18n::t(
            self.language,
            "cli.member.client.invalid_response",
            &[]
        ))
    }

    fn render_error(&self, error: anyhow::Error) -> anyhow::Error {
        if let Some(error) = error.downcast_ref::<gb_member::MemberError>() {
            anyhow::anyhow!(error.render(|key, args| {
                let params: Vec<_> = args.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
                i18n::t(self.language, key, &params)
            }))
        } else {
            error
        }
    }

    async fn describe(&self, request: &MemberRequest) -> anyhow::Result<Reply> {
        let response = self.post("/member/plan", request).await?;
        let (result, mut response) = self.header(response).await?;
        self.payload(&mut response, &result, 0).await?;
        Ok(result)
    }

    async fn post(&self, path: &str, request: &MemberRequest) -> anyhow::Result<reqwest::Response> {
        checked(
            self.owner
                .request(reqwest::Method::POST, path)
                .json(request)
                .send()
                .await?,
        )
        .await
    }

    async fn header(&self, mut response: reqwest::Response) -> anyhow::Result<(Reply, Stream)> {
        let mut metadata = Vec::new();
        while let Some(bytes) = response.chunk().await? {
            let end = bytes.iter().position(|b| *b == b'\n');
            let prefix = end.unwrap_or(bytes.len());
            anyhow::ensure!(
                metadata.len() + prefix <= MAX_METADATA,
                self.invalid_response()
            );
            metadata.extend_from_slice(&bytes[..prefix]);
            if let Some(end) = end {
                let reply =
                    serde_json::from_slice(&metadata).map_err(|_| self.invalid_response())?;
                return Ok((
                    reply,
                    Stream {
                        prefix: bytes[end + 1..].into(),
                        response,
                    },
                ));
            }
        }
        Err(self.invalid_response())
    }

    async fn payload(
        &self,
        stream: &mut Stream,
        reply: &Reply,
        limit: usize,
    ) -> anyhow::Result<Vec<u8>> {
        let mut payload = std::mem::take(&mut stream.prefix);
        let size = size(reply)?;
        anyhow::ensure!(
            size <= limit && payload.len() <= size,
            self.invalid_response()
        );
        while let Some(chunk) = stream.response.chunk().await? {
            anyhow::ensure!(payload.len() + chunk.len() <= size, self.invalid_response());
            payload.extend_from_slice(&chunk);
        }
        anyhow::ensure!(payload.len() == size, self.invalid_response());
        Ok(payload)
    }
}

fn size(reply: &Reply) -> anyhow::Result<usize> {
    Ok(reply
        .metadata
        .get("size")
        .cloned()
        .map(serde_json::from_value::<usize>)
        .transpose()?
        .unwrap_or(0))
}

struct Stream {
    prefix: Vec<u8>,
    response: reqwest::Response,
}
