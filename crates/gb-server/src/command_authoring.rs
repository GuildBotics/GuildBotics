//! `POST /api/commands/author`: one request of the command editor's
//! assistant, as the Python version answers it (`AppRuntime.author_command`).
//! The bundled `assistants/author_command` answers it, read-only, as the work
//! of the editor's conversation: the request holds the editor's file and the
//! shared commands, and the answer is an answer or changes to review.

use axum::Json;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use gb_config::command_writes::Format;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::api::{ApiError, Body, Shared};
use crate::commands::{Launch, RunRequest};

const COMMAND: &str = "assistants/author_command";

#[derive(Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
enum Mode {
    Create,
    Edit,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct AuthoringRequest {
    conversation_id: String,
    message: String,
    person: Option<String>,
    mode: Mode,
    #[serde(default)]
    command: String,
    format: Option<Format>,
    #[serde(default)]
    content: String,
    #[serde(default)]
    file_id: String,
    #[serde(default)]
    revision: String,
}

impl AuthoringRequest {
    /// Whether the request is one: an edit names the file it edits, a
    /// creation names nothing yet.
    fn valid(&self) -> bool {
        let sized =
            (1..=128).contains(&self.conversation_id.chars().count()) && !self.message.is_empty();
        let names = [
            !self.command.trim().is_empty(),
            self.format.is_some(),
            !self.file_id.is_empty(),
            !self.revision.is_empty(),
        ];
        sized
            && match self.mode {
                Mode::Edit => names.iter().all(|named| *named),
                Mode::Create => !names.iter().any(|named| *named) && self.content.is_empty(),
            }
    }
}

pub(crate) async fn author(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<AuthoringRequest>,
) -> Response {
    if !request.valid() {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .context(json!({"errors": [{"msg": "an edit names its command, format, file and revision; a creation none"}]}))
        .into_response();
    }
    let Some(admitted) = api.admit() else {
        return ApiError::new(StatusCode::CONFLICT, "command_workspace_changing", &headers)
            .into_response();
    };
    let workspace = match crate::command_catalog::workspace(&api, &headers).await {
        Ok(workspace) => workspace,
        Err(refused) => return refused,
    };
    let available = match available_commands(&workspace) {
        Ok(available) => available,
        Err(e) => return crate::commands::internal(&format!("{e:#}"), "OSError"),
    };
    let message = json!({"mode": if request.mode == Mode::Edit {"edit"} else {"create"},
        "command": request.command, "format": request.format.map(Format::name),
        "current_content": request.content, "instruction": request.message,
        "available_commands": available});
    let label = if request.command.is_empty() {
        "new-command"
    } else {
        &request.command
    };
    let launch = Launch {
        label: format!("author:{label}"),
        attributes: serde_json::Map::from_iter([(
            "command_authoring.conversation_id".into(),
            request.conversation_id.clone().into(),
        )]),
        work: Some(("command_authoring".into(), request.conversation_id.clone())),
        cwd: Some(
            workspace
                .directory
                .join(".guildbotics/local/work/command-authoring"),
        ),
        failure: (StatusCode::BAD_GATEWAY, "command_authoring_failed"),
        result: Some(("CommandAuthoringResult", authoring_result)),
    };
    let run = RunRequest {
        command: COMMAND.into(),
        args: vec![],
        person: request.person.clone(),
        message: message.to_string(),
        cwd: None,
        expected_workspace: None,
        expected_command_file_id: None,
        expected_command_file_revision: None,
    };
    let ran = match crate::commands::served(&api, &headers, run, launch, admitted).await {
        Ok(ran) => ran,
        Err(refused) => return refused,
    };
    Json(answer(&request, &ran.trace_id, &ran.result)).into_response()
}

/// The assistant's `result` as the editor reads it: each change names the
/// file it writes, and an update the file and revision the editor read.
fn answer(request: &AuthoringRequest, trace_id: &str, result: &Value) -> Value {
    let update = request.mode == Mode::Edit;
    let changes: Vec<Value> = result["changes"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|change| {
            let extension = change["format"]
                .as_str()
                .and_then(gb_config::command_writes::extension)
                .unwrap_or("");
            let updates = update && change["operation"] == "update";
            json!({"operation": change["operation"], "command": change["command"],
                "format": change["format"], "relative_path": format!("{}{extension}", change["command"].as_str().unwrap_or("")),
                "content": change["content"],
                "file_id": if updates { request.file_id.as_str() } else { "" },
                "expected_revision": if updates { request.revision.as_str() } else { "" }})
        })
        .collect();
    json!({"trace_id": trace_id, "message": result["message"],
        "action": result["action"], "changes": changes})
}

/// The shared commands, as the assistant reads them: name, format, file and
/// content.
fn available_commands(workspace: &crate::command_catalog::Workspace) -> anyhow::Result<Value> {
    let roots = &workspace.roots;
    let language = &workspace.team.language;
    let listed = roots.shared_files(language)?;
    let mut available = Vec::new();
    for file in listed["files"].as_array().into_iter().flatten() {
        let id = file["id"].as_str().unwrap_or("");
        let Some(path) = roots.selected_file(id, language) else {
            continue;
        };
        let detail = roots.file_detail(&path, language)?;
        available.push(
            json!({"command": detail["command"], "format": detail["format"],
            "relative_path": detail["relative_path"], "content": detail["content"]}),
        );
    }
    Ok(available.into())
}

/// Whether `value` is what the assistant answers: an answer without
/// changes, or between one and ten changes to review.
fn authoring_result(value: &Value) -> bool {
    let Some(result) = value.as_object() else {
        return false;
    };
    let keys_only = |object: &serde_json::Map<String, Value>, keys: &[&str]| {
        object.keys().all(|k| keys.contains(&k.as_str()))
    };
    let changes = match result.get("changes") {
        None => Some(&[][..]),
        Some(Value::Array(changes)) => Some(changes.as_slice()),
        Some(_) => None,
    };
    let Some(changes) = changes else {
        return false;
    };
    let valid_change = |change: &Value| {
        change.as_object().is_some_and(|c| {
            keys_only(c, &["operation", "command", "format", "content"])
                && ["create", "update"].contains(&c["operation"].as_str().unwrap_or(""))
                && c["command"].as_str().is_some_and(|s| !s.is_empty())
                && c["format"]
                    .as_str()
                    .and_then(gb_config::command_writes::extension)
                    .is_some()
                && c["content"].as_str().is_some_and(|s| !s.is_empty())
        })
    };
    keys_only(result, &["action", "message", "changes"])
        && result.get("message").is_some_and(Value::is_string)
        && changes.len() <= 10
        && changes.iter().all(valid_change)
        && match result.get("action").and_then(Value::as_str) {
            Some("answer") => changes.is_empty(),
            Some("propose_changes") => !changes.is_empty(),
            _ => false,
        }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(mode: &str) -> AuthoringRequest {
        serde_json::from_value(if mode == "edit" {
            json!({"conversation_id": "c", "message": "m", "mode": "edit",
                "command": "reports/weekly", "format": "python", "content": "old source",
                "file_id": "cmVwb3J0cy93ZWVrbHkucHk", "revision": "revision-1"})
        } else {
            json!({"conversation_id": "c", "message": "m", "mode": "create"})
        })
        .unwrap()
    }

    #[test]
    fn an_answer_names_the_files_its_changes_write() {
        let result = json!({"action": "propose_changes", "message": "Done.", "changes": [
            {"operation": "update", "command": "reports/weekly", "format": "python", "content": "new source"},
            {"operation": "create", "command": "reports/helper", "format": "markdown", "content": "---\nbrain: default\n---\n"}]});
        assert!(authoring_result(&result));
        let edited = answer(&request("edit"), "t-1", &result);
        assert_eq!(
            edited,
            json!({"trace_id": "t-1", "message": "Done.", "action": "propose_changes", "changes": [
                {"operation": "update", "command": "reports/weekly", "format": "python",
                    "relative_path": "reports/weekly.py", "content": "new source",
                    "file_id": "cmVwb3J0cy93ZWVrbHkucHk", "expected_revision": "revision-1"},
                {"operation": "create", "command": "reports/helper", "format": "markdown",
                    "relative_path": "reports/helper.md", "content": "---\nbrain: default\n---\n",
                    "file_id": "", "expected_revision": ""}]})
        );
        let created = answer(&request("create"), "t-2", &result);
        assert_eq!(created["changes"][0]["file_id"], "");
        assert_eq!(created["changes"][0]["expected_revision"], "");
    }

    #[test]
    fn only_an_answer_or_one_to_ten_changes_is_an_authoring_result() {
        let change =
            json!({"operation": "create", "command": "a", "format": "shell", "content": "x"});
        for (result, valid) in [
            (json!({"action": "answer", "message": "Hi."}), true),
            (
                json!({"action": "answer", "message": "Hi.", "changes": []}),
                true,
            ),
            (
                json!({"action": "answer", "message": "Hi.", "changes": [change]}),
                false,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": []}),
                false,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": [change]}),
                true,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": vec![change.clone(); 11]}),
                false,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": [{"operation": "delete", "command": "a", "format": "shell", "content": "x"}]}),
                false,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": [{"operation": "create", "command": "", "format": "shell", "content": "x"}]}),
                false,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": [{"operation": "create", "command": "a", "format": "toml", "content": "x"}]}),
                false,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": [{"operation": "create", "command": "a", "format": "shell", "content": ""}]}),
                false,
            ),
            (
                json!({"action": "propose_changes", "message": "x", "changes": [{"operation": "create", "command": "a", "format": "shell", "content": "x", "extra": 1}]}),
                false,
            ),
            (
                json!({"action": "answer", "message": "Hi.", "extra": 1}),
                false,
            ),
            (json!({"action": "answer"}), false),
            (json!({"action": "decide", "message": "Hi."}), false),
            (json!("plain text"), false),
            (Value::Null, false),
        ] {
            assert_eq!(authoring_result(&result), valid, "{result}");
        }
    }
}
