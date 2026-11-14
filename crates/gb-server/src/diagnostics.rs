//! The diagnostics screen's checks and read-only assistant.
use crate::api::{ApiError, Body, Shared};
use crate::commands::{Launch, RunRequest};
use axum::{
    Json,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use gb_observability::{Diagnostics, Trace};
use serde::Deserialize;
use serde_json::{Value, json};

#[cfg(test)]
pub(crate) type CheckRunner = std::sync::Arc<
    dyn Fn(
            &str,
            &str,
            &std::path::Path,
            &str,
        ) -> Result<String, (String, Option<Box<gb_wire::CommandFailure>>)>
        + Send
        + Sync,
>;

fn language(headers: &HeaderMap) -> &'static str {
    crate::i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|s| s.to_str().ok())
            .unwrap_or(""),
    )
}
fn check(
    lang: &str,
    section: &str,
    code: &str,
    status: &str,
    person: &str,
    context: Value,
) -> Value {
    json!({"section":section,"code":code,"status":status,"person_id":person,"context":context,"credential":false,"target":"",
        "message":crate::i18n::t(lang,&format!("app_api.diagnostics.{code}.{status}"),&[("env_key",context["env_key"].as_str().unwrap_or(""))])})
}
fn response(checks: Vec<Value>, active: Vec<String>) -> Value {
    let matching = |status| {
        checks
            .iter()
            .filter(|c| c["status"] == status)
            .cloned()
            .collect::<Vec<_>>()
    };
    let errors = matching("error");
    json!({"ok":errors.is_empty(),"active_members":active,"warnings":matching("warning"),"errors":errors,"checks":checks})
}
#[derive(Default, Deserialize)]
pub(crate) struct ScenarioQuery {
    person_id: Option<String>,
}

struct DiagnosticRun {
    trace: Option<Trace>,
    finished: bool,
    language: &'static str,
}
impl Drop for DiagnosticRun {
    fn drop(&mut self) {
        if !self.finished
            && let Some(trace) = &self.trace
        {
            trace.event("command.failed",json!({"command":"diagnostics","error_type":"CancelledError","code":"command_cancelled","message":crate::i18n::t(self.language,"app_api.errors.command_cancelled",&[])}).as_object().unwrap().clone());
        }
    }
}

pub(crate) async fn scenario(
    State(api): State<Shared>,
    headers: HeaderMap,
    Query(query): Query<ScenarioQuery>,
) -> Response {
    let Some(admitted) = api.admit() else {
        return ApiError::new(StatusCode::CONFLICT, "command_workspace_changing", &headers)
            .into_response();
    };
    let selected = api.selected();
    let lang = language(&headers);
    let trace = selected.workspace.as_ref().map(|workspace| {
        let events = api.events.clone();
        Trace::open(&Diagnostics::open(workspace), "diagnostics")
            .command("diagnostics")
            .person(query.person_id.as_deref().unwrap_or(""))
            .announced(std::sync::Arc::new(move |e| events.announce(e)))
    });
    use tracing::Instrument as _;
    let span = trace
        .as_ref()
        .map(|t| {
            tracing::info_span!(
                "diagnostics",
                diagnostics = serde_json::to_string(&t.event_fields()).unwrap().as_str()
            )
        })
        .unwrap_or_else(tracing::Span::none);
    async move {
    let mut completion=DiagnosticRun{trace:trace.clone(),finished:false,language:lang};
    let loaded = match &selected.workspace {
        Some(w) => load_context(&api,w,query.person_id.as_deref()).await,
        None => Err(anyhow::anyhow!(crate::i18n::t(
            lang,
            "app_api.errors.config_not_found",
            &[]
        ))),
    };
    let load_error = loaded.as_ref().err().map(|e| format!("{e}"));
    let (mut checks, mut active) = (Vec::new(), Vec::new());
    let mut providers = None;
    if let (Ok((team,saved)), Some(workspace)) = (loaded, &selected.workspace) {
        let project =
            std::fs::read_to_string(workspace.join(".guildbotics/config/team/project.yml"))
                .ok()
                .and_then(|s| gb_config::yaml_value(&s).ok())
                .unwrap_or(Value::Null);
        providers = Some(crate::setup_providers::Registry::named(&project));
        checks.push(check(lang, "config", "config_load", "ok", "", json!({})));
        let members: Vec<_> = team
            .members
            .iter()
            .filter(
                |m| match query.person_id.as_deref().filter(|s| !s.is_empty()) {
                    Some(person) => m.person_id == person,
                    None => m.is_active && m.person_type != "human",
                },
            )
            .collect();
        if members.is_empty() {
            checks.push(check(
                lang,
                "members",
                if query.person_id.is_some() {
                    "member_not_found"
                } else {
                    "active_members"
                },
                "error",
                query.person_id.as_deref().unwrap_or(""),
                json!({}),
            ));
        } else {
            active = members.iter().map(|m| m.person_id.clone()).collect();
            if members[0].person_type == "human" && query.person_id.is_some() {
                let person = &members[0].person_id;
                checks.push(check(
                    lang,
                    "members",
                    "human_member_reference",
                    "ok",
                    person,
                    json!({}),
                ));
                let roles=team.screen_summary(&workspace.join(".guildbotics/config"),&api.identity_root).ok().and_then(|summary|summary["members"].as_array().and_then(|members|members.iter().find(|m|m["person_id"].as_str()==Some(person.as_str())).map(|m|m["roles"].as_array().cloned().unwrap_or_default()))).unwrap_or_default();
                checks.push(check(
                    lang,
                    "members",
                    "human_member_roles",
                    if roles.is_empty() { "error" } else { "ok" },
                    person,
                    if roles.is_empty(){json!({})}else{json!({"roles":roles})},
                ));
            } else {
                let inactive: Vec<_> = members
                    .iter()
                    .filter(|m| !m.is_active)
                    .map(|m| m.person_id.clone())
                    .collect();
                if !inactive.is_empty() {
                    checks.push(check(
                        lang,
                        "members",
                        "member_inactive",
                        "warning",
                        &inactive[0],
                        json!({"inactive_members":inactive}),
                    ));
                }
                checks.push(check(
                    lang,
                    "members",
                    "active_members",
                    "ok",
                    "",
                    json!({"members":active}),
                ));
                checks.push(llm(&api, &headers, workspace, &active[0], trace.as_ref()).await);
                for member in &members {
                    checks.push(
                        cli(&api, &headers, workspace, &member.person_id, trace.as_ref()).await,
                    );
                }
            }
            let project =
                std::fs::read_to_string(workspace.join(".guildbotics/config/team/project.yml"))
                    .ok()
                    .and_then(|s| gb_config::yaml_value(&s).ok())
                    .unwrap_or(Value::Null);
            let names = crate::setup_providers::Registry::named(&project);
            providers = Some(names.clone());
            let saved=saved.into_iter().filter(|m|members.iter().any(|member|member.person_id==m.person_id)).collect::<Vec<_>>();
            let registry = crate::setup_providers::Registry(Some(api.clone()));
            if names.contains(&"branvil") {
                checks.extend(
                    registry
                        .diagnose_branvil(&saved, &project["services"], lang)
                        .await
                        .into_iter()
                        .map(|c| serde_json::to_value(c).unwrap()),
                );
            }
            if names.contains(&"slack") {
                let members: Vec<_> = saved
                    .iter()
                    .map(|m| {
                        let file = workspace.join(format!(
                            ".guildbotics/config/team/members/{}/member.yml",
                            m.person_id
                        ));
                        let config = std::fs::read_to_string(file)
                            .ok()
                            .and_then(|s| gb_config::yaml_value(&s).ok())
                            .unwrap_or(Value::Null);
                        (
                            m,
                            config["message_channels"]
                                .as_array()
                                .cloned()
                                .unwrap_or_default(),
                        )
                    })
                    .collect();
                checks.extend(
                    registry
                        .diagnose_slack(&members, lang)
                        .await
                        .into_iter()
                        .map(|c| serde_json::to_value(c).unwrap()),
                );
            }
        }
    } else {
        checks.push(failed_check(
            lang,
            "config",
            "config_load",
            "",
            load_error.as_deref().unwrap_or(""),
            json!({}),
        ));
    }
    let result = response(checks, active);
    let mut payload = json!({"ok":result["ok"],"active_members":result["active_members"],"checks":result["checks"],"scope_person_id":query.person_id.unwrap_or_default()});
    if let Some(providers) = providers {
        payload["providers"] = json!(providers);
    }
    if let Some(trace) = trace {
        trace.event(
            "diagnostics.completed",
            payload.as_object().unwrap().clone(),
        );
    } else {
        api.events.publish(
            "diagnostics.completed",
            payload.as_object().unwrap().clone(),
            Default::default(),
        );
    }
    drop(admitted);
    completion.finished=true;
    Json(result).into_response()
    }.instrument(span).await
}

async fn load_context(
    api: &Shared,
    workspace: &std::path::Path,
    target: Option<&str>,
) -> anyhow::Result<(gb_config::team::Team, Vec<gb_setup::SavedMember>)> {
    let team =
        gb_config::team::Team::load(&workspace.join(".guildbotics/config"), &api.identity_root)?;
    let mut saved = Vec::new();
    for member in team
        .members
        .iter()
        .filter(|m| match target.filter(|s| !s.is_empty()) {
            Some(p) => m.person_id == p,
            None => m.is_active && m.person_type != "human",
        })
    {
        let person = member.person_id.clone();
        let member = crate::config_api::with_setup(api, workspace.to_owned(), move |setup| {
            setup.diagnostic_member(&person)
        })
        .await?;
        saved.push(member);
    }
    Ok((team, saved))
}

async fn run_check(
    api: &Shared,
    headers: &HeaderMap,
    command: &str,
    person: &str,
    cwd: &std::path::Path,
    trace: Option<&Trace>,
) -> Result<String, (String, Option<Box<gb_wire::CommandFailure>>)> {
    const MESSAGE: &str = "This is a read-only diagnostics check. Reply with exactly OK. Do not create, modify, delete, or inspect unrelated files.";
    #[cfg(test)]
    if let Some(run) = api.diagnostics_runner.lock().unwrap().clone() {
        return run(command, person, cwd, MESSAGE);
    }
    let capture = std::sync::Arc::new(std::sync::Mutex::new(None));
    let mut launch = Launch::manual(command);
    launch.trace = trace.cloned();
    launch.diagnostic_check = true;
    launch.attributes.clear();
    launch.cwd = Some(cwd.to_owned());
    launch.failure_capture = Some(capture.clone());
    let request = RunRequest {
        remote_copy: false,
        preflight: false,
        remotes: Default::default(),
        client_cwd: None,
        upload: None,
        command: command.into(),
        input_ids: vec![],
        args: vec![],
        person: Some(person.into()),
        message: MESSAGE.into(),
        cwd: None,
        expected_workspace: None,
        request_id: None,
        expected_command_file_id: None,
        expected_command_file_revision: None,
    };
    let Some(admitted) = api.admit() else {
        return Err((
            crate::i18n::t(
                language(headers),
                "app_api.errors.command_workspace_changing",
                &[],
            ),
            None,
        ));
    };
    match crate::commands::served(api, headers, request, launch, admitted).await {
        Ok(ran) => Ok(ran.output),
        Err(response) => {
            let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
                .await
                .unwrap_or_default();
            let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
            let failure = capture.lock().unwrap_or_else(|p| p.into_inner()).take();
            Err((
                failure
                    .as_ref()
                    .map(|f| f.message.clone())
                    .unwrap_or_else(|| value["message"].as_str().unwrap_or("").into()),
                failure.map(Box::new),
            ))
        }
    }
}
async fn llm(
    api: &Shared,
    headers: &HeaderMap,
    workspace: &std::path::Path,
    person: &str,
    trace: Option<&Trace>,
) -> Value {
    let lang = language(headers);
    let host = crate::command_catalog::host(api).expect("host paths");
    let config = workspace.join(".guildbotics/config");
    let templates = host.package.join("templates");
    let mapping = [
        config.join("intelligences/model_mapping.yml"),
        templates.join("intelligences/model_mapping.yml"),
    ]
    .into_iter()
    .find_map(|p| std::fs::read_to_string(p).ok())
    .and_then(|s| gb_config::yaml_value(&s).ok())
    .unwrap_or(Value::Null);
    let provider = mapping["default"]
        .as_str()
        .unwrap_or("")
        .strip_prefix("models/")
        .and_then(|s| s.split('/').next())
        .unwrap_or("");
    let keys = gb_setup::provider_env_keys(&templates, &config.join("intelligences/models"));
    if let Some(key) = keys.get(provider)
        && !matches!(
            crate::host_window::host_secret_in(&host, workspace, key, &*api.keychain),
            Ok(Some(_))
        )
    {
        let mut result = check(
            lang,
            "llm",
            "llm_api_key",
            "error",
            person,
            json!({"provider":provider,"env_key":key}),
        );
        result["credential"] = true.into();
        return result;
    }
    let temporary = match tempfile::tempdir() {
        Ok(t) => t,
        Err(e) => {
            return failed_check(
                lang,
                "llm",
                "llm_live_call",
                person,
                &e.to_string(),
                json!({"provider":provider,"error_type":"OSError"}),
            );
        }
    };
    match run_check(
        api,
        headers,
        "diagnostics/llm",
        person,
        temporary.path(),
        trace,
    )
    .await
    {
        Ok(_) => check(
            lang,
            "llm",
            "llm_live_call",
            "ok",
            person,
            json!({"provider":provider}),
        ),
        Err((message, failure)) => {
            let mut facts = json!({"provider":provider,"error_type":"CommandError"});
            if let Some(f) = failure {
                facts["error_type"] = if f.inference_error_type.is_empty() {
                    f.type_
                } else {
                    f.inference_error_type
                }
                .into();
                if !f.inference_category.is_empty() {
                    facts["category"] = f.inference_category.into();
                }
            }
            failed_check(lang, "llm", "llm_live_call", person, &message, facts)
        }
    }
}
fn failed_check(
    lang: &str,
    section: &str,
    code: &str,
    person: &str,
    message: &str,
    facts: Value,
) -> Value {
    let mut result = check(lang, section, code, "error", person, facts);
    result["message"] = format!(
        "{}: {message}",
        result["message"]
            .as_str()
            .unwrap()
            .trim_end_matches(['.', '。'])
    )
    .into();
    result
}
async fn cli(
    api: &Shared,
    headers: &HeaderMap,
    workspace: &std::path::Path,
    person: &str,
    trace: Option<&Trace>,
) -> Value {
    let lang = language(headers);
    let roots = gb_config::commands::CommandRoots {
        config: workspace.join(".guildbotics/config"),
        templates: gb_base::home::python_package().join("templates"),
    };
    let language = gb_config::team::Team::load(&roots.config, &api.identity_root)
        .map(|t| t.language)
        .unwrap_or_else(|_| "en".into());
    let resolved = roots
        .resolve("diagnostics/cli_agent", &language, Some(person))
        .and_then(|p| gb_config::commands::command_metadata(&p));
    let metadata = match resolved {
        Ok(metadata) => metadata,
        Err(e) => {
            return failed_check(
                lang,
                "cli_agent",
                "cli_agent_brain",
                person,
                &e.to_string(),
                json!({"error_type":"ValueError"}),
            );
        }
    };
    let brain = metadata.metadata["brain"]
        .as_str()
        .unwrap_or("default")
        .to_owned();
    let tool = gb_config::resolved_guest::brain_tool(&roots, person, &brain);
    let tool = match tool {
        Ok(Some(tool)) => tool,
        Err(e) => {
            return failed_check(
                lang,
                "cli_agent",
                "cli_agent_brain",
                person,
                &e.to_string(),
                json!({"error_type":"ValueError"}),
            );
        }
        Ok(None) => {
            let mut result = check(
                lang,
                "cli_agent",
                "cli_agent_brain",
                "error",
                person,
                json!({"brain":brain}),
            );
            result["message"] =
                crate::i18n::t(lang, "app_api.diagnostics.cli_agent_no_tool", &[]).into();
            return result;
        }
    };
    let temporary = match tempfile::tempdir() {
        Ok(t) => t,
        Err(e) => {
            return failed_check(
                lang,
                "cli_agent",
                "cli_agent_brain",
                person,
                &e.to_string(),
                json!({"error_type":"OSError"}),
            );
        }
    };
    let outcome = run_check(
        api,
        headers,
        "diagnostics/cli_agent",
        person,
        temporary.path(),
        trace,
    )
    .await;
    let mut result = match outcome {
        Ok(stdout) => cli_outcome(lang, person, &stdout, "", 0),
        Err((_message, Some(failure))) if failure.cli_agent_result.is_some() => {
            let result = Value::Object(failure.cli_agent_result.unwrap());
            cli_outcome(
                lang,
                person,
                result["stdout"].as_str().unwrap_or(""),
                result["stderr"].as_str().unwrap_or(""),
                result["returncode"].as_i64().unwrap_or(1),
            )
        }
        Err((message, failure)) => failed_check(
            lang,
            "cli_agent",
            "cli_agent_brain",
            person,
            &message,
            json!({"error_type":failure.map(|f|f.type_).unwrap_or_else(||"CommandError".into())}),
        ),
    };
    result["target"] = tool.into();
    result
}
fn shortened(value: &str) -> String {
    let value = value.trim();
    let mut result = value.chars().take(1000).collect::<String>();
    if value.chars().count() > 1000 {
        result.push_str("...");
    }
    result
}
fn cli_outcome(lang: &str, person: &str, stdout: &str, stderr: &str, returncode: i64) -> Value {
    if returncode == 0 && !stdout.trim().is_empty() {
        return check(
            lang,
            "cli_agent",
            "cli_agent_brain",
            "ok",
            person,
            json!({}),
        );
    }
    let empty = returncode == 0;
    let (stdout, stderr) = (shortened(stdout), shortened(stderr));
    if empty {
        tracing::warn!(
            person_id = person,
            "AI CLI tool diagnostics returned empty stdout"
        );
    }
    let key = if empty {
        "cli_agent_empty"
    } else {
        "cli_agent_exit"
    };
    let prefix = crate::i18n::t(
        lang,
        &format!("app_api.diagnostics.{key}"),
        &[("returncode", &returncode.to_string())],
    );
    let details = if !stderr.is_empty() {
        format!("stderr: {stderr}")
    } else if !stdout.is_empty() {
        format!("stdout: {stdout}")
    } else {
        crate::i18n::t(lang, "app_api.diagnostics.cli_agent_no_stderr", &[])
    };
    let context = if empty {
        json!({"empty_stdout":true,"stderr":stderr})
    } else {
        json!({"returncode":returncode,"stderr":stderr,"stdout":stdout})
    };
    let mut result = check(
        lang,
        "cli_agent",
        "cli_agent_brain",
        "error",
        person,
        context,
    );
    result["message"] = format!("{prefix} {details}").into();
    result
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Troubleshoot {
    conversation_id: String,
    message: String,
    person: Option<String>,
    focus: Option<Focus>,
}
#[derive(Deserialize, serde::Serialize, Default)]
#[serde(default, deny_unknown_fields)]
struct Focus {
    view: View,
    trace_id: String,
    source: String,
    person_id: String,
    query: String,
    span_id: String,
    call_id: String,
    record_timestamp: String,
    record_type: String,
    record_message: String,
}
#[derive(Deserialize, serde::Serialize, Default)]
#[serde(rename_all = "lowercase")]
enum View {
    #[default]
    Trace,
    Global,
    Memory,
}

#[derive(Default, Deserialize)]
pub(crate) struct ReadQuery {
    limit: Option<usize>,
    person_id: Option<String>,
    doc_id: Option<String>,
    action: Option<String>,
    trace_id: Option<String>,
    source: Option<String>,
    q: Option<String>,
    since: Option<String>,
    until: Option<String>,
}
fn limit(
    query: Result<Query<ReadQuery>, axum::extract::rejection::QueryRejection>,
    headers: &HeaderMap,
) -> Result<ReadQuery, Box<Response>> {
    match query {
        Ok(Query(q)) if q.limit.is_none_or(|l| (1..=1000).contains(&l)) => Ok(q),
        _ => Err(Box::new(
            ApiError::new(
                StatusCode::UNPROCESSABLE_ENTITY,
                "validation_error",
                headers,
            )
            .into_response(),
        )),
    }
}
pub(crate) async fn global(
    State(api): State<Shared>,
    headers: HeaderMap,
    query: Result<Query<ReadQuery>, axum::extract::rejection::QueryRejection>,
) -> Response {
    let q = match limit(query, &headers) {
        Ok(q) => q,
        Err(r) => return *r,
    };
    let Some(workspace) = &api.selected().workspace else {
        return Json(
            json!({"trace_id":"","summary":null,"records":[],"transcript_available":false}),
        )
        .into_response();
    };
    let store = Diagnostics::open(workspace);
    match store.latest_system_records(q.limit.unwrap_or(200)) {
        Ok(mut result) => {
            let id = result["trace_id"].as_str().unwrap_or("");
            let summary = store
                .trace_detail(id)
                .ok()
                .map(|v| v["summary"].clone())
                .unwrap_or(Value::Null);
            result["summary"] = summary;
            result["transcript_available"] =
                (!result["records"].as_array().unwrap().is_empty()).into();
            Json(result).into_response()
        }
        Err(e) => crate::commands::internal(&e.to_string(), "OSError"),
    }
}
fn parsed_stamp(value: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    chrono::DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|t| t.to_utc())
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
                .ok()
                .map(|t| t.and_utc())
        })
}
pub(crate) fn memory_records(workspace: &std::path::Path) -> Vec<Value> {
    let root = workspace.join(".guildbotics/state/documents/memory_events");
    let mut paths = std::fs::read_dir(root)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "jsonl"))
        .collect::<Vec<_>>();
    paths.sort();
    paths
        .into_iter()
        .filter_map(|path| std::fs::read_to_string(path).ok())
        .flat_map(|s| {
            s.lines()
                .filter_map(|line| serde_json::from_str::<Value>(line).ok())
                .filter(Value::is_object)
                .collect::<Vec<_>>()
        })
        .collect()
}
pub(crate) async fn memory_events(
    State(api): State<Shared>,
    headers: HeaderMap,
    query: Result<Query<ReadQuery>, axum::extract::rejection::QueryRejection>,
) -> Response {
    let q = match limit(query, &headers) {
        Ok(q) => q,
        Err(r) => return *r,
    };
    let selected = api.selected();
    let Some(workspace) = &selected.workspace else {
        return Json(json!({"event_count":0,"events":[]})).into_response();
    };
    let root = workspace.join(".guildbotics/state/documents");
    let mut events = memory_records(workspace);
    events.reverse();
    let text = |v: &Value| v.as_str().unwrap_or("").to_owned();
    events.retain(|e| {
        let timestamp = parsed_stamp(&text(&e["timestamp"]));
        [
            (&q.person_id, &e["person_id"]),
            (&q.doc_id, &e["attributes"]["memory.doc_id"]),
            (&q.action, &e["attributes"]["memory.action"]),
            (&q.trace_id, &e["trace_id"]),
        ]
        .iter()
        .all(|(filter, v)| {
            filter
                .as_deref()
                .is_none_or(|f| f.is_empty() || text(v) == f)
        }) && q.source.as_ref().is_none_or(|s| {
            e["payload"]["source"]
                .to_string()
                .to_lowercase()
                .contains(&s.to_lowercase())
        }) && q
            .q
            .as_ref()
            .is_none_or(|s| json!({"type":e["type"],"message":e["message"],"person_id":e["person_id"],"attributes":e["attributes"],"payload":e["payload"]}).to_string().to_lowercase().contains(&s.to_lowercase()))
            && q.since
                .as_deref()
                .and_then(parsed_stamp)
                .is_none_or(|since| timestamp.is_some_and(|t| t >= since))
            && q.until
                .as_deref()
                .and_then(parsed_stamp)
                .is_none_or(|until| timestamp.is_some_and(|t| t <= until))
    });
    events.sort_by_key(|e| std::cmp::Reverse(parsed_stamp(&text(&e["timestamp"]))));
    let events=events.into_iter().take(q.limit.unwrap_or(200)).map(|e|{
        let a=&e["attributes"];let p=&e["payload"];
        let path=text(&a["memory.path"]);
        let parts=path.split('/').collect::<Vec<_>>();
        let preview=if parts.len()>=2 && parts[0]=="documents" && parts.iter().all(|p|!matches!(*p,""|"."|"..") && !p.contains('\\')) {
            let body=parts[1..].iter().fold(root.clone(),|p,part|p.join(part)).join("body.md");
            std::fs::read_to_string(body).unwrap_or_default().chars().take(800).collect::<String>()
        }else{String::new()};
        let strings=|key:&str|p[key].as_array().map(|items|items.iter().filter(|v|v.is_string()).cloned().collect::<Vec<_>>()).unwrap_or_default();
        json!({"timestamp":text(&e["timestamp"]),"action":a["memory.action"].as_str().unwrap_or_else(||e["type"].as_str().unwrap_or("").trim_start_matches("memory.")),"person_id":text(&e["person_id"]),"scope":text(&a["memory.scope"]),"doc_id":text(&a["memory.doc_id"]),"path":path,"title":text(&p["title"]),"summary":text(&p["summary"]),"kind":text(&a["memory.kind"]),"trace_id":e["trace_id"],"run_id":text(&a["run_id"]),"task_run_id":text(&a["task_run_id"]),"source":p["source"].as_array().map(|items|items.iter().filter(|v|v.is_object()).cloned().collect::<Vec<_>>()).unwrap_or_default(),"changed_fields":strings("changed_fields"),"query_keywords":strings("query_keywords"),"result_count":p["result_count"].as_i64(),"duration_ms":p["duration_ms"].as_f64(),"body_preview":preview})
    }).collect::<Vec<_>>();
    Json(json!({"event_count":events.len(),"events":events})).into_response()
}

fn debug_values(api: &Shared) -> Value {
    serde_json::to_value(gb_config::debug::read(api.selected().workspace.as_deref())).unwrap()
}
pub(crate) async fn debug(State(api): State<Shared>) -> Response {
    Json(debug_values(&api)).into_response()
}

pub(crate) fn prune(workspace: &std::path::Path) {
    let settings = std::fs::read_to_string(workspace.join(".guildbotics/config/transcripts.yml"))
        .ok()
        .and_then(|s| gb_config::yaml_value(&s).ok())
        .unwrap_or(Value::Null);
    let retention = crate::config_api::transcript_retention_days(&settings) as u64;
    if let Err(e) = Diagnostics::open(workspace).prune_transcripts(
        std::time::SystemTime::now(),
        std::time::Duration::from_secs(retention.saturating_mul(86400)),
    ) {
        tracing::warn!("cannot prune transcripts: {e}");
    }
}
pub(crate) async fn maintenance(api: Shared) {
    loop {
        let workspace = api.selected().workspace.clone();
        let data = api.data.clone();
        let inputs = api.inputs.clone();
        let _ = tokio::task::spawn_blocking(move || {
            if let Some(workspace) = workspace {
                prune(&workspace);
            }
            if let Err(error) = gb_environment::run_files::prune(&data) {
                tracing::warn!("Cannot prune run files: {error}");
            }
            if let Err(error) = inputs.prune() {
                tracing::warn!("Cannot prune uploads: {error}");
            }
        })
        .await;
        tokio::select! {()=api.cancelled.cancelled()=>return,()=tokio::time::sleep(std::time::Duration::from_secs(86400))=>{}}
    }
}
#[derive(Deserialize)]
pub(crate) struct DebugUpdate {
    enabled: bool,
}
pub(crate) async fn update_debug(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<DebugUpdate>,
) -> Response {
    let Some(_admitted) = api.admit() else {
        return ApiError::new(StatusCode::CONFLICT, "command_workspace_changing", &headers)
            .into_response();
    };
    let selected = api.selected();
    let Some(workspace) = &selected.workspace else {
        return ApiError::new(StatusCode::CONFLICT, "workspace_not_configured", &headers)
            .key("workspace_not_configured.plain", vec![])
            .into_response();
    };
    let path = workspace.join(".guildbotics/local/debug.env");
    let level = if request.enabled { "DEBUG" } else { "INFO" };
    if let Err(e) = gb_base::files::atomic_write(
        &path,
        format!("LOG_LEVEL={level}\nAGNO_DEBUG={}\n", request.enabled).as_bytes(),
    ) {
        return crate::commands::internal(&e.to_string(), "OSError");
    }
    crate::diagnostic_logging::level(Some(level));
    Json(debug_values(&api)).into_response()
}
pub(crate) async fn troubleshoot(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<Troubleshoot>,
) -> Response {
    if !(1..=128).contains(&request.conversation_id.chars().count()) || request.message.is_empty() {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response();
    }
    let Some(admitted) = api.admit() else {
        return ApiError::new(StatusCode::CONFLICT, "command_workspace_changing", &headers)
            .into_response();
    };
    let selected = api.selected();
    let Some(workspace) = &selected.workspace else {
        return ApiError::new(StatusCode::BAD_REQUEST, "config_not_found", &headers)
            .into_response();
    };
    let focus = request.focus.unwrap_or_default();
    let label = if focus.trace_id.is_empty() {
        serde_json::to_value(&focus.view)
            .unwrap()
            .as_str()
            .unwrap()
            .to_owned()
    } else {
        focus.trace_id.clone()
    };
    let mut launch = Launch::manual("assistants/troubleshoot");
    launch.label = format!("troubleshoot:{label}");
    launch.attributes.insert(
        "troubleshooting.conversation_id".into(),
        request.conversation_id.clone().into(),
    );
    launch.work = Some(("troubleshooting".into(), request.conversation_id));
    launch.cwd = Some(workspace.join(".guildbotics/local/work/troubleshooting"));
    launch.failure = (StatusCode::BAD_GATEWAY, "troubleshooting_failed");
    launch.result = Some(("TroubleshootingResult", troubleshooting_result));
    let roots = gb_config::commands::CommandRoots {
        config: workspace.join(".guildbotics/config"),
        templates: gb_base::home::python_package().join("templates"),
    };
    let team = gb_config::team::Team::load(&roots.config, &api.identity_root).ok();
    let language = team.as_ref().map(|t| t.language.as_str()).unwrap_or("en");
    let person = team
        .as_ref()
        .and_then(|t| {
            request
                .person
                .as_deref()
                .and_then(|p| t.find(p))
                .or_else(|| {
                    if request.person.is_none() {
                        t.default_person_id().and_then(|id| t.find(id))
                    } else {
                        None
                    }
                })
        })
        .map(|p| p.person_id.as_str());
    let access = match roots
        .resolve("assistants/troubleshoot", language, person)
        .and_then(|p| gb_config::commands::command_access(&p))
    {
        Ok(access) => access,
        Err(e) => {
            return ApiError::new(StatusCode::BAD_GATEWAY, "troubleshooting_failed", &headers)
                .reason(e.to_string())
                .into_response();
        }
    };
    let directories = troubleshooting_directories(
        access.inspects,
        &roots.config,
        &api.identity_root,
        workspace,
    );
    let run = RunRequest {
        remote_copy: false,
        preflight: false,
        remotes: Default::default(),
        client_cwd: None,
        upload: None,
        command: "assistants/troubleshoot".into(),
        input_ids: vec![],
        args: vec![],
        person: request.person,
        message: json!({"question":request.message,"focus":focus,"directories":directories})
            .to_string(),
        cwd: None,
        expected_workspace: None,
        request_id: None,
        expected_command_file_id: None,
        expected_command_file_revision: None,
    };
    match crate::commands::served(&api, &headers, run, launch, admitted).await {
        Err(response) => response,
        Ok(ran) => {
            Json(troubleshooting_answer(&Diagnostics::open(workspace), &ran)).into_response()
        }
    }
}

pub(crate) fn troubleshooting_directories(
    scopes: Vec<gb_wire::InspectsItem>,
    config: &std::path::Path,
    identity_root: &std::path::Path,
    workspace: &std::path::Path,
) -> serde_json::Map<String, Value> {
    let mut directories = serde_json::Map::new();
    for scope in scopes {
        let paths = match scope {
            gb_wire::InspectsItem::Config => {
                directories.insert(
                    "templates".into(),
                    format!("{}/templates", gb_environment::CODE_ROOT).into(),
                );
                vec![
                    ("config", config.to_owned()),
                    (
                        "identities",
                        identity_root.join(".guildbotics/config/team/members"),
                    ),
                ]
            }
            gb_wire::InspectsItem::Diagnostics => {
                vec![("diagnostics", workspace.join(".guildbotics/local/run"))]
            }
        };
        for (name, path) in paths {
            if path.is_dir()
                && let Ok(path) = gb_environment::guest_path(&path)
            {
                directories.insert(name.into(), path.into());
            }
        }
    }
    directories
}

fn troubleshooting_result(value: &Value) -> bool {
    serde_json::from_value::<gb_wire::TroubleshootingResult>(value.clone()).is_ok()
        && value["trace_ids"]
            .as_array()
            .is_none_or(|ids| ids.len() <= 10)
}

fn troubleshooting_answer(store: &Diagnostics, ran: &crate::commands::Ran) -> Value {
    let ids = ran.result["trace_ids"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|id| {
            !id.is_empty()
                && store
                    .trace_detail(id)
                    .is_ok_and(|v| !v["summary"].is_null())
        })
        .collect::<Vec<_>>();
    json!({"trace_id":ran.trace_id,"message":ran.result["message"],"trace_ids":ids})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cancelling_diagnostics_closes_the_trace_without_claiming_completion() {
        let root = tempfile::tempdir().unwrap();
        let store = Diagnostics::open(root.path());
        let trace = Trace::open(&store, "diagnostics").command("diagnostics");
        drop(DiagnosticRun {
            trace: Some(trace.clone()),
            finished: false,
            language: "en",
        });
        let detail = store.trace_detail(trace.id()).unwrap();
        assert_eq!(detail["summary"]["status"], "failed");
        let records = detail["records"].as_array().unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0]["type"], "command.failed");
        assert_eq!(records[0]["payload"]["error_type"], "CancelledError");
    }
    #[test]
    fn cli_failure_details_preserve_exit_stream_fallbacks_and_truncation() {
        for (stdout, stderr, expected) in [
            ("", "login required", "stderr: login required"),
            ("output", "", "stdout: output"),
            ("", "", "No stderr was returned."),
        ] {
            let check = cli_outcome("en", "alice", stdout, stderr, 2);
            assert_eq!(check["status"], "error");
            assert_eq!(
                check["context"],
                json!({"returncode":2,"stdout":stdout,"stderr":stderr})
            );
            assert!(
                check["message"]
                    .as_str()
                    .unwrap()
                    .starts_with("AI CLI tool command failed (exit code: 2).")
            );
            assert!(check["message"].as_str().unwrap().contains(expected));
        }
        let check = cli_outcome("en", "alice", "", &"あ".repeat(1001), 2);
        assert_eq!(
            check["context"]["stderr"],
            format!("{}...", "あ".repeat(1000))
        );
        let check = cli_outcome("en", "alice", "   ", "", 0);
        assert_eq!(check["context"], json!({"empty_stdout":true,"stderr":""}));
        assert!(
            check["message"]
                .as_str()
                .unwrap()
                .contains("completed but returned no response")
        );
    }
    #[test]
    fn troubleshooting_result_keeps_only_recorded_references_and_validates_shape() {
        let root = tempfile::tempdir().unwrap();
        let store = Diagnostics::open(root.path());
        let trace = Trace::open(&store, "manual");
        trace.event("command.started", serde_json::Map::new());
        let result = json!({"message":"The token expired.","trace_ids":[trace.id(),"","unknown"]});
        assert!(troubleshooting_result(&result));
        let ran = crate::commands::Ran {
            trace_id: "assistant".into(),
            output: String::new(),
            result,
        };
        assert_eq!(
            troubleshooting_answer(&store, &ran),
            json!({"trace_id":"assistant","message":"The token expired.","trace_ids":[trace.id()]})
        );
        assert!(troubleshooting_result(&json!({"message":"Answer"})));
        for invalid in [
            json!({"message":1}),
            json!({"message":"Answer","unknown":1}),
            json!({"message":"Answer","trace_ids":[1]}),
            json!({"message":"Answer","trace_ids":vec!["id";11]}),
        ] {
            assert!(!troubleshooting_result(&invalid));
        }
        let focus = serde_json::to_value(Focus::default()).unwrap();
        assert_eq!(focus["view"], "trace");
        assert_eq!(focus.as_object().unwrap().len(), 10);
        assert!(
            focus
                .as_object()
                .unwrap()
                .iter()
                .filter(|(key, _)| key.as_str() != "view")
                .all(|(_, value)| *value == "")
        );
    }
}
