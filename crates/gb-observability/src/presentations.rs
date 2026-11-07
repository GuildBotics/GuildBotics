//! Provider-neutral record display, matching app_api/trace_presentations.py.
use crate::activity::text;

use serde_json::{Value, json};

fn first<'a>(values: &[&'a str]) -> &'a str {
    values.iter().copied().find(|s| !s.is_empty()).unwrap_or("")
}
fn event_key(name: &str) -> String {
    format!("diagnostics.executions.eventTypes.{name}")
}
fn message_key(name: &str) -> String {
    format!("diagnostics.executions.messages.{name}")
}
fn event_tone(event: &str) -> &'static str {
    if event.ends_with(".failed") {
        "danger"
    } else if ["running", "started", "finished"]
        .iter()
        .any(|p| event.contains(p))
    {
        "success"
    } else if event.contains("stopping") {
        "warning"
    } else {
        "neutral"
    }
}

pub(crate) fn presentation(raw: &Value) -> Value {
    let event = text(raw, &["type"]);
    let payload = &raw["payload"];
    let attrs = &raw["attributes"];
    let field = |keys: &[&str]| text(payload, keys).to_owned();
    let mut label = event.to_owned();
    let mut label_key = String::new();
    let mut message = String::new();
    let mut message_name = String::new();
    let mut params = json!({});
    let mut tone = "neutral";
    let mut effort = String::new();
    match text(raw, &["kind"]) {
        "log" => {
            label = first(&[text(raw, &["level"]), "LOG"]).to_uppercase();
            message = text(raw, &["message"]).into();
            tone = match label.as_str() {
                "ERROR" | "CRITICAL" => "danger",
                "WARNING" => "warning",
                _ => "neutral",
            };
        }
        "io" => {
            if [
                "llm.request",
                "llm.response",
                "cli_agent.request",
                "cli_agent.response",
                "command.output",
            ]
            .contains(&event)
            {
                label_key = format!("diagnostics.executions.ioTypes.{}", event.replace('.', "_"));
            }
            label = first(&[event, "I/O"]).into();
            message = first(&[
                text(raw, &["message"]),
                text(payload, &["message", "prompt", "stdout", "output"]),
                event,
            ])
            .into();
            effort = text(&payload["effort"], &["resolved"]).into();
            tone = "info";
        }
        "memory" => {
            let action = first(&[
                text(attrs, &["memory.action"]),
                event.strip_prefix("memory.").unwrap_or(event),
            ]);
            if !action.is_empty() {
                label_key = format!("diagnostics.memory.actions.{action}");
            }
            label = first(&[action, "memory"]).into();
            message = first(&[text(raw, &["message"]), event]).into();
            tone = match action {
                "record" => "success",
                "recall" | "get" | "update" | "promote" => "info",
                "archive" => "warning",
                _ => "neutral",
            };
        }
        _ => {
            if event.starts_with("command.") || event.starts_with("member.command.") {
                let phase = event.rsplit('.').next().unwrap_or("");
                label_key = event_key(&format!("command_{phase}"));
                label = format!("command.{phase}");
                let failure = if matches!(phase, "failed" | "deferred") {
                    field(&["message", "error", "error_type", "code"])
                } else {
                    String::new()
                };
                message = first(&[
                    &failure,
                    text(raw, &["command"]),
                    text(payload, &["command"]),
                    &label,
                ])
                .into();
                tone = match phase {
                    "failed" => "danger",
                    "deferred" => "info",
                    _ => "success",
                };
            } else if event == "scheduler.worker.failed" {
                label_key = event_key("scheduler_worker_failed");
                message_name = message_key("scheduler_worker_failed");
                params = json!({"source":payload.get("source").unwrap_or(&json!("")),"count":payload.get("consecutive_errors").unwrap_or(&json!(0)),"limit":payload.get("consecutive_error_limit").unwrap_or(&json!(0))});
                tone = "danger";
            } else if event.starts_with("scheduler.") || event.starts_with("events.") {
                let (target, phase) = event.split_once('.').unwrap();
                label_key = event_key(if target == "scheduler" {
                    "scheduler"
                } else {
                    "event_listener"
                });
                message = field(&["error", "message"]);
                if message.is_empty() {
                    message_name = message_key(&format!("{target}_{phase}"));
                }
                tone = match phase {
                    "failed" => "danger",
                    "stopping" => "warning",
                    "starting" | "running" => "success",
                    _ => "neutral",
                };
            } else if let Some(subtype) = event.strip_prefix("agent_runtime.") {
                let mut name = if [
                    "process",
                    "turn",
                    "assistant",
                    "command",
                    "file_change",
                    "tool",
                    "approval",
                    "usage",
                    "failed",
                ]
                .contains(&subtype)
                {
                    subtype
                } else {
                    ""
                };
                if subtype == "assistant" && payload["partial"] == true {
                    name = "assistant_partial";
                }
                if subtype == "assistant" && payload["name"] == "thinking" {
                    name = "assistant_thinking";
                }
                if subtype == "approval" && payload["name"] == "policy" {
                    name = "approval_policy";
                }
                if !name.is_empty() {
                    label_key = format!("diagnostics.executions.agentRuntime.{name}");
                }
                message = field(&["message"]);
                let name = field(&["name"]);
                if message.is_empty() && !name.is_empty() {
                    message_name = format!("diagnostics.executions.eventNames.{name}");
                }
                message = first(&[&message, &name, event]).into();
                if subtype == "failed" {
                    tone = "danger";
                }
            } else if event == "agent_environment.network_egress_candidate" {
                label_key = event_key("network_egress_candidate");
                let destinations: Vec<_> = payload["candidates"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|c| {
                        let destination = text(c, &["destination"]);
                        if destination.is_empty() {
                            return None;
                        }
                        Some(if c["port"].as_u64().is_some_and(|v| v > 0) {
                            format!("{destination}:{}", c["port"])
                        } else {
                            destination.into()
                        })
                    })
                    .collect();
                message = destinations.join(", ");
                if message.is_empty() {
                    message = field(&["message"]);
                }
                tone = "info";
            } else if let Some(status) = event.strip_prefix("span.") {
                label_key = format!("diagnostics.executions.spanEvents.{status}");
                let mut parts = vec![field(&["model"]), field(&["effort"])];
                if let Some(ms) = payload["duration_ms"].as_f64() {
                    parts.push(if ms < 1000.0 {
                        format!("{:.0}ms", ms.round_ties_even().max(0.0))
                    } else {
                        format!("{:.1}s", ms / 1000.0)
                    });
                }
                message = parts
                    .into_iter()
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>()
                    .join(" · ");
                tone = if status == "failed" {
                    "danger"
                } else {
                    "success"
                };
            } else {
                exact(
                    raw,
                    &mut label_key,
                    &mut message,
                    &mut message_name,
                    &mut params,
                    &mut tone,
                );
            }
            if message.is_empty() {
                message = event.into();
            }
        }
    }
    json!({"label_key":label_key,"label_fallback":label,"message_key":message_name,"message":message,"message_params":params,"tone":tone,"effort":effort})
}

fn exact(
    raw: &Value,
    key: &mut String,
    message: &mut String,
    message_name: &mut String,
    params: &mut Value,
    tone: &mut &str,
) {
    let event = text(raw, &["type"]);
    let p = &raw["payload"];
    let a = &raw["attributes"];
    let label = event.replace('.', "_");
    match event {
        "decision.evaluated" => {
            *key = event_key(&label);
            *message = ["brain", "model", "route", "reason", "error"]
                .iter()
                .filter_map(|k| p.get(k))
                .filter(|v| !v.is_null() && **v != "" && **v != false)
                .map(|v| {
                    v.as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| v.to_string())
                })
                .collect::<Vec<_>>()
                .join(" · ");
            *tone = if text(p, &["error"]).is_empty() {
                "info"
            } else {
                "warning"
            };
        }
        "repository.push" | "pull_request.item" | "ticket.item" | "ticket.comment"
        | "target.recorded" => {
            *key = event_key(&label);
            let target = first(&[
                text(a, &["target.label"]),
                text(&p["pull_request"], &["label"]),
                text(&p["ticket"], &["label"]),
            ]);
            let title = first(&[
                text(&p["pull_request"], &["title"]),
                text(&p["ticket"], &["title"]),
                text(p, &["title"]),
                text(a, &["target.title"]),
            ]);
            *message = if title.is_empty() {
                target.into()
            } else if target.is_empty() {
                title.into()
            } else {
                format!("{target} · {title}")
            };
            if event == "repository.push" && !text(p, &["ref"]).is_empty() {
                *message = text(p, &["ref"]).trim_start_matches("refs/heads/").into();
            }
            if message.is_empty() {
                *message = first(&[text(p, &["action"]), event]).into();
            }
            *tone = "success";
        }
        "credential.failed" | "credential.verified" => {
            *key = event_key(&label);
            *message_name = message_key(&label);
            let provider = first(&[text(p, &["provider"]), "provider"]);
            let provider = if provider == "cli_agent" {
                first(&[text(p, &["cli_agent"]), provider])
            } else {
                provider
            };
            if event == "credential.verified" {
                *message = provider.into();
                *params = json!({"provider":provider});
                *tone = "success";
            } else {
                let code = first(&[text(p, &["code"]), "authentication"]);
                *message = code.into();
                *params = json!({"provider":provider,"code":code});
                *tone = "danger";
            }
        }
        "diagnostics.completed" | "verify.completed" => {
            *key = event_key(&label);
            *message_name = message_key(&label);
            let ok = p["ok"].as_bool().unwrap_or(false);
            *params = json!({"count":p["checks"].as_array().map_or(0,Vec::len),"ok":ok});
            *tone = if ok { "success" } else { "danger" };
        }
        "system.started" | "system.finished" => {
            *key = event_key(&label);
            *message = first(&[
                text(a, &["service_run_id", "system_session_id"]),
                text(p, &["path"]),
                event,
            ])
            .into();
            *tone = if event.ends_with(".started") {
                "success"
            } else {
                "neutral"
            };
        }
        "session.pointer" => {
            *key = event_key(&label);
            *message = first(&[text(p, &["path"]), text(a, &["session.path"]), event]).into();
        }
        "workspace.changed" => {
            *key = event_key(&label);
            *message = first(&[text(p, &["workspace"]), event]).into();
        }
        "target.scope_refused" => {
            *key = event_key(&label);
            *message = first(&[text(p, &["target"]), event]).into();
            *tone = "danger";
        }
        "chat.receive_state_reset" => {
            *key = event_key(&label);
            *message_name = message_key(&label);
            *params = json!({"members":p.get("members").unwrap_or(&json!(0)),"channels":p.get("channels").unwrap_or(&json!(0))});
        }
        _ if event.starts_with("workflow.") || event.starts_with("chat_dispatch.") => {
            let known = [
                "workflow.completed",
                "workflow.completion_missing",
                "workflow.rate_limited",
                "chat_dispatch.retry_scheduled",
                "chat_dispatch.abandoned",
            ]
            .contains(&event);
            if known {
                *key = event_key(&label);
            }
            let error = text(p, &["error", "message"]);
            *message = first(&[
                error,
                if event.starts_with("workflow.") {
                    text(raw, &["command"])
                } else {
                    ""
                },
                event,
            ])
            .into();
            let mut suffix = "";
            let mut reset = String::new();
            if event.starts_with("workflow.") {
                if let Ok(t) =
                    chrono::DateTime::parse_from_rfc3339(text(p, &["retry_after_at"]).trim())
                {
                    suffix = "_with_reset";
                    reset = t
                        .with_timezone(&chrono::Local)
                        .format("%Y-%m-%d %H:%M:%S%:z")
                        .to_string();
                } else if !text(p, &["retry_after_text"]).is_empty() {
                    suffix = "_with_hint";
                    reset = text(p, &["retry_after_text"]).into();
                }
                *params = json!({"run":p.get("run_id").unwrap_or(&json!("")),"attempt":p.get("attempt").unwrap_or(&json!(0)),"max_attempts":p.get("max_attempts").unwrap_or(&json!(0)),"retry_at":reset});
            } else {
                *params = json!({"run":p.get("run_id").unwrap_or(&json!("")),"attempt":p.get("attempt_count").unwrap_or(&json!(0)),"max_attempts":p.get("max_attempts").unwrap_or(&json!(0)),"retry_at":p.get("next_attempt_at").unwrap_or(&json!(""))});
            }
            if known
                && error.is_empty()
                && !matches!(
                    event,
                    "workflow.completion_missing" | "chat_dispatch.abandoned"
                )
            {
                *message_name = message_key(&format!(
                    "{label}{}",
                    if event == "workflow.rate_limited" {
                        suffix
                    } else {
                        ""
                    }
                ));
            }
            *tone = match event {
                "workflow.completion_missing" | "chat_dispatch.abandoned" => "danger",
                "workflow.rate_limited" | "chat_dispatch.retry_scheduled" => "warning",
                _ => "success",
            };
        }
        _ => {
            *key = event_key("unknown");
            *message = first(&[
                text(raw, &["message"]),
                text(p, &["message", "error", "code", "error_type"]),
                event,
            ])
            .into();
            *tone = event_tone(event);
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn every_presentation_catalog_entry_matches_python() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../../../records/golden/shared-records.json"))
                .unwrap();
        for case in fixture["presentations"].as_array().unwrap() {
            assert_eq!(
                super::presentation(&case["raw"]),
                case["expected"],
                "{}",
                case["raw"]
            );
        }
    }
}

#[cfg(test)]
mod frozen_tests;
