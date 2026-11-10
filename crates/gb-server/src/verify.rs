//! `POST /-/guildbotics/api/verify`: whether the selected workspace is ready to run, as the
//! Python version checks it (`guildbotics/app_api/verify.py`): its project,
//! the secret store, the team and its active members, the LLM's key, the
//! isolated environment, the AI CLI tool and the providers' credentials. The
//! check is a trace of its own, which `verify.completed` ends.

use std::path::Path;

use axum::Json;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::response::{IntoResponse, Response};
use gb_config::team::Team;
use serde_json::{Map, Value, json};

use crate::api::Shared;
use crate::i18n;
use crate::setup_providers::Registry;

struct Checks {
    checks: Vec<Value>,
    language: &'static str,
}

impl Checks {
    fn push(&mut self, code: &str, status: &str, message: String, target: &str, context: Value) {
        self.checks
            .push(json!({"code": code, "status": status, "message": message,
            "target": target, "context": context}));
    }

    /// The check `code` with its own sentence for `status`.
    fn said(&mut self, code: &str, status: &str, target: &str, context: Value) {
        let message = i18n::t(
            self.language,
            &format!("app_api.verify.{code}.{status}"),
            &[],
        );
        self.push(code, status, message, target, context);
    }

    /// Whether the key `key` has a value, as the check `code`.
    fn key(&mut self, code: &str, key: &str, set: bool, context: Value) {
        let status = if set { "ok" } else { "error" };
        let message = i18n::t(
            self.language,
            &format!("app_api.verify.key.{status}"),
            &[("key", key)],
        );
        self.push(code, status, message, key, context);
    }
}

pub(crate) async fn verify(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let language = i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    let config = crate::screens::status(&api).await;
    let workspace = api.selected().workspace.clone();
    let mut checks = Checks {
        checks: Vec::new(),
        language,
    };
    let project = config["project_file"].as_str().map(Path::new);
    checks.said(
        "config_project_file",
        if config["project_file_exists"] == true {
            "ok"
        } else {
            "error"
        },
        &project.map_or("None".into(), |p| p.display().to_string()),
        json!({}),
    );
    checks.said(
        "secret_store",
        if api.keychain.read().is_ok() {
            "ok"
        } else {
            "error"
        },
        "",
        json!({}),
    );
    let mut active = Vec::new();
    match (&workspace, project.filter(|p| p.is_file())) {
        (Some(workspace), Some(_)) => {
            match Team::load(&workspace.join(".guildbotics/config"), &api.identity_root) {
                Ok(team) => {
                    checks.said("team_load", "ok", "", json!({}));
                    active = team
                        .members
                        .iter()
                        .filter(|m| m.is_active)
                        .map(|m| m.person_id.clone())
                        .collect();
                    checks.said(
                        "active_members",
                        if active.is_empty() { "error" } else { "ok" },
                        "",
                        json!({ "active_members": active }),
                    );
                    workspace_checks(&api, workspace, &active, &mut checks).await;
                }
                Err(e) => checks.push(
                    "team_load",
                    "error",
                    format!("{e:#}"),
                    "",
                    json!({"error_type": "ValueError"}),
                ),
            }
        }
        _ => checks.push(
            "team_load",
            "error",
            i18n::t(language, "app_api.errors.config_not_found", &[]),
            "",
            json!({"error_type": "AppApiError"}),
        ),
    }
    let with = |status: &str| -> Vec<Value> {
        checks
            .checks
            .iter()
            .filter(|c| c["status"] == status)
            .cloned()
            .collect()
    };
    let (warnings, errors) = (with("warning"), with("error"));
    let ok = errors.is_empty();
    let mut completed = Map::new();
    completed.insert("ok".into(), ok.into());
    completed.insert("checks".into(), checks.checks.clone().into());
    // The trace opens with the event that ends it: a request dropped while
    // checking leaves no trace that never ends.
    match &workspace {
        Some(workspace) => {
            let events = api.events.clone();
            gb_observability::Trace::open(
                &gb_observability::Diagnostics::open(workspace),
                "diagnostics",
            )
            .command("verify")
            .announced(std::sync::Arc::new(move |event| events.announce(event)))
            .event("verify.completed", completed);
        }
        None => api.events.publish(
            "verify.completed",
            completed,
            crate::events::Origin::default(),
        ),
    }
    Json(
        json!({"ok": ok, "config": config, "active_members": active, "checks": checks.checks,
        "warnings": warnings, "errors": errors}),
    )
    .into_response()
}

/// The checks of a workspace whose team was read: the LLM's key, the
/// isolated environment, the default AI CLI tool and the providers.
async fn workspace_checks(api: &Shared, workspace: &Path, active: &[String], checks: &mut Checks) {
    let Ok(host) = crate::command_catalog::host(api) else {
        return;
    };
    let config = workspace.join(".guildbotics/config");
    let templates = host.package.join("templates");
    let default_of = |mapping: &str, prefix: &str| -> String {
        [config.join(mapping), templates.join(mapping)]
            .into_iter()
            .find(|path| path.is_file())
            .and_then(|path| std::fs::read_to_string(path).ok())
            .and_then(|text| gb_config::yaml_value(&text).ok())
            .and_then(|mapping| mapping["default"].as_str().map(str::to_owned))
            .and_then(|relative| {
                let mut parts = relative.split('/');
                (parts.next() == Some(prefix)).then(|| parts.next().map(str::to_owned))?
            })
            .unwrap_or_default()
    };
    let provider = default_of("intelligences/model_mapping.yml", "models");
    let keys = gb_setup::provider_env_keys(&templates, &config.join("intelligences/models"));
    match keys.get(&provider) {
        None => checks.said(
            "llm_provider",
            "warning",
            "",
            json!({ "provider": provider }),
        ),
        Some(key) => checks.key(
            "llm_api_key",
            key,
            matches!(
                crate::host_window::host_secret_in(&host, workspace, key, &*api.keychain),
                Ok(Some(_))
            ),
            json!({ "provider": provider }),
        ),
    }
    let device = gb_environment::device::read(&host, workspace).await;
    match device.refusal {
        None => checks.said("agent_environment", "ok", "", json!({})),
        Some(refusal) => {
            let message = crate::commands::say(refusal, checks.language);
            checks.push("agent_environment", "error", message, "", json!({}));
        }
    }
    let tool = default_of("intelligences/cli_agent_mapping.yml", "cli_agents");
    let refusals = crate::environment::tool_refusals(&host, &*api.keychain, checks.language)
        .unwrap_or_default();
    match refusals.get(&tool) {
        None => checks.said("cli_agent_mapping", "warning", "", json!({})),
        Some(refusal) if refusal.is_empty() => {
            let message = i18n::t(
                checks.language,
                "app_api.verify.cli_agent_environment.ok",
                &[("tool", &tool)],
            );
            checks.push("cli_agent_environment", "ok", message, &tool, json!({}));
        }
        Some(refusal) => {
            checks.push(
                "cli_agent_environment",
                "error",
                refusal.clone(),
                &tool,
                json!({}),
            );
        }
    }
    let project = std::fs::read_to_string(config.join("team/project.yml"))
        .ok()
        .and_then(|text| gb_config::yaml_value(&text).ok())
        .unwrap_or(Value::Null);
    let registry = Registry(Some(api.clone()));
    let providers = Registry::named(&project);
    // Each member once: one whose settings cannot be read is an error of its
    // own, and the other checks are still worth answering.
    let mut members = Vec::new();
    for person in active.iter().filter(|_| !providers.is_empty()) {
        let member = crate::config_api::with_setup(api, workspace.to_path_buf(), {
            let person = person.clone();
            move |setup| setup.saved_member(&person)
        })
        .await;
        match member {
            Ok(member) => members.push((person, member)),
            Err(_) => {
                let message = i18n::t(
                    checks.language,
                    "app_api.verify.member_credentials.error",
                    &[("person", person.as_str())],
                );
                let context = json!({ "person_id": person });
                checks.push("member_credentials", "error", message, "", context);
            }
        }
    }
    for provider in providers {
        for key in Registry::identity_secret_keys(provider) {
            let set = matches!(
                crate::host_window::host_secret_in(&host, workspace, key, &*api.keychain),
                Ok(Some(_))
            );
            checks.key("provider_secret", key, set, json!({ "provider": provider }));
        }
        for (person, member) in &members {
            let subscribed =
                std::fs::read_to_string(config.join(format!("team/members/{person}/member.yml")))
                    .ok()
                    .and_then(|text| gb_config::yaml_value(&text).ok())
                    .is_some_and(|m| {
                        m["message_channels"]
                            .as_array()
                            .is_some_and(|c| !c.is_empty())
                    });
            for check in registry.verify(provider, member, subscribed, checks.language) {
                checks.push(
                    &check.code,
                    &check.status,
                    check.message,
                    &check.target,
                    check.context,
                );
            }
        }
    }
}
