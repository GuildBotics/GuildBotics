//! Shared activity, with only transcript availability tied to this device.
use crate::api::{ApiError, Shared};
use axum::{
    Json,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use chrono::{DateTime, FixedOffset, Utc};
use gb_observability::timeline::{iso_timestamp, parse_timestamp};
use gb_observability::{
    Diagnostics, activity::ActivityEvents, interactive::InteractiveSessions, runs::RunStore,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{path::Path, sync::Arc};
#[cfg(test)]
pub(crate) type RefreshRunner =
    Arc<dyn Fn(DateTime<Utc>, DateTime<Utc>) -> anyhow::Result<()> + Send + Sync>;

fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub(crate) async fn reset(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let Some(admitted) = api.admit() else {
        return ApiError::new(StatusCode::CONFLICT, "command_workspace_changing", &headers)
            .into_response();
    };
    let selected = api.selected();
    let Some(workspace) = selected.workspace.clone() else {
        return ApiError::new(StatusCode::BAD_REQUEST, "config_not_found", &headers)
            .into_response();
    };
    let identities = api.identity_root.clone();
    let timeout = api.write_timeout;
    let events = api.events.clone();
    let result = api
        .work
        .spawn(async move {
            let _admitted = admitted;
            let result = selected
                .service
                .reset_chat_state(move || {
                    let roots = gb_config::commands::CommandRoots {
                        config: workspace.join(".guildbotics/config"),
                        templates: gb_base::home::python_package().join("templates"),
                    };
                    let team = gb_config::team::Team::load(&roots.config, &identities)?;
                    let project = gb_config::yaml_value(&std::fs::read_to_string(
                        roots.config.join("team/project.yml"),
                    )?)?;
                    let provider =
                        text(&project["services"]["chat_service"], "name").to_lowercase();
                    let supported =
                        gb_integrations::factory::ChatProvider::parse(&provider).is_some();
                    let mut persons = vec![];
                    for member in &team.members {
                        if !member.is_active || !supported {
                            continue;
                        }
                        let metadata = gb_config::resolved_guest::member(
                            &roots,
                            &identities,
                            &member.person_id,
                        )?;
                        if crate::work_stops::subscriptions(&metadata)
                            .iter()
                            .any(|s| !s.channel_id.is_empty() || !s.channel_name.is_empty())
                        {
                            persons.push(member.person_id.clone());
                        }
                    }
                    let mut writes = gb_base::shared_writes::SharedWrites::new(
                        identities,
                        Some(workspace.clone()),
                    );
                    writes.timeout = timeout;
                    let store = crate::chat_state::Store {
                        workspace,
                        writes: Arc::new(writes),
                        limit: 500,
                    };
                    let channels =
                        store.reset_receive(&provider, &persons, &Utc::now().to_rfc3339())?;
                    Ok(json!({"members_reset":persons.len(),"channels_reset":channels}))
                })
                .await?;
            events.publish(
                "chat.receive_state_reset",
                json!({"members":result["members_reset"],"channels":result["channels_reset"]})
                    .as_object()
                    .unwrap()
                    .clone(),
                crate::events::Origin::default(),
            );
            Ok::<_, anyhow::Error>(result)
        })
        .await
        .map_err(anyhow::Error::from)
        .and_then(|r| r);
    match result {
        Ok(result) => Json(result).into_response(),
        Err(e) => {
            let (status, code) = if e.to_string() == "runtime_running" {
                (StatusCode::CONFLICT, "runtime_running")
            } else if e
                .downcast_ref::<gb_base::shared_writes::SharedWriteBusyError>()
                .is_some()
            {
                (StatusCode::SERVICE_UNAVAILABLE, "config_busy")
            } else {
                (StatusCode::INTERNAL_SERVER_ERROR, "internal_error")
            };
            ApiError::new(status, code, &headers).into_response()
        }
    }
}
fn parsed(s: &str) -> Option<DateTime<Utc>> {
    parse_timestamp(s).map(|v| v.to_utc())
}
#[derive(Default, Deserialize)]
pub(crate) struct Window {
    start: Option<String>,
    end: Option<String>,
    #[serde(default)]
    refresh: bool,
    sync_start: Option<String>,
    sync_end: Option<String>,
}
struct ParsedWindow {
    start: DateTime<FixedOffset>,
    end: DateTime<FixedOffset>,
    sync_start: DateTime<FixedOffset>,
    sync_end: DateTime<FixedOffset>,
}
fn window(q: &Window, headers: &HeaderMap) -> Result<ParsedWindow, ApiError> {
    let end = q
        .end
        .as_deref()
        .and_then(parse_timestamp)
        .unwrap_or_else(|| parse_timestamp(&Utc::now().to_rfc3339()).unwrap());
    let start = q
        .start
        .as_deref()
        .and_then(parse_timestamp)
        .unwrap_or(end - chrono::Duration::days(7));
    if start > end {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "invalid_activity_history_range",
            headers,
        )
        .context(
            json!({"start":q.start.as_deref().unwrap_or(""),"end":q.end.as_deref().unwrap_or("")}),
        ));
    }
    let error = |key| {
        ApiError::new(StatusCode::BAD_REQUEST,"invalid_activity_sync_range",headers).key(key,vec![]).context(json!({"sync_start":q.sync_start.as_deref().unwrap_or(""),"sync_end":q.sync_end.as_deref().unwrap_or("")}))
    };
    let sync = match (&q.sync_start, &q.sync_end) {
        (None, None) => (start, end),
        (Some(s), Some(e)) => {
            let (Some(s), Some(e)) = (parse_timestamp(s), parse_timestamp(e)) else {
                return Err(error("invalid_activity_sync_range.timestamps"));
            };
            if s > e {
                return Err(error("invalid_activity_sync_range.order"));
            }
            (s, e)
        }
        _ => return Err(error("invalid_activity_sync_range.together")),
    };
    Ok(ParsedWindow {
        start,
        end,
        sync_start: sync.0,
        sync_end: sync.1,
    })
}
pub(crate) async fn history(
    State(api): State<Shared>,
    headers: HeaderMap,
    query: Result<Query<Window>, axum::extract::rejection::QueryRejection>,
) -> Response {
    let q = match query {
        Ok(Query(q)) => q,
        Err(_) => {
            return ApiError::new(
                StatusCode::UNPROCESSABLE_ENTITY,
                "validation_error",
                &headers,
            )
            .into_response();
        }
    };
    let ParsedWindow {
        start,
        end,
        sync_start,
        sync_end,
    } = match window(&q, &headers) {
        Ok(w) => w,
        Err(e) => return e.into_response(),
    };
    let selected = api.selected();
    let Some(workspace) = selected.workspace.clone() else {
        return ApiError::new(StatusCode::BAD_REQUEST, "config_not_found", &headers)
            .into_response();
    };
    let project = workspace.join(".guildbotics/config/team/project.yml");
    if !project.exists() {
        return ApiError::new(StatusCode::BAD_REQUEST, "config_not_found", &headers)
            .context(json!({"path":project}))
            .into_response();
    }
    let language = crate::i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    // A stopping server starts no sync for its drain to wait for; the
    // screen shows what is already recorded.
    if !api.shutdown.stopping() {
        refresh(&api, &workspace, sync_start, sync_end, q.refresh);
    }
    let identities = api.identity_root.clone();
    match tokio::task::spawn_blocking(move || {
        read(
            &workspace,
            &identities,
            start.to_utc(),
            end.to_utc(),
            language,
        )
    })
    .await
    {
        Ok(result) => crate::screens::answer(
            result.map(|mut result| {
                result["start"] = iso_timestamp(start).into();
                result["end"] = iso_timestamp(end).into();
                result
            }),
            &headers,
        ),
        Err(_) => ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            &headers,
        )
        .into_response(),
    }
}
fn read(
    workspace: &Path,
    identities: &Path,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
    language: &str,
) -> anyhow::Result<Value> {
    let config = workspace.join(".guildbotics/config");
    let team = gb_config::team::Team::load(&config, identities)?;
    let summary = team.screen_summary(&config, identities)?;
    let members=summary["members"].as_array().unwrap_or(&vec![]).iter().map(|m|json!({"person_id":m["person_id"],"name":m["name"],"person_type":m["person_type"],"roles":m["roles"]})).collect();
    let store = Diagnostics::open(workspace);
    let mut lifecycles = Vec::new();
    for r in RunStore::open(workspace).records_between(start, end)? {
        lifecycles.push(gb_observability::timeline::lifecycle_from_run(&r));
    }
    for r in InteractiveSessions::open(workspace).list_between(start, end)? {
        lifecycles.push(gb_observability::timeline::lifecycle_from_session(&r));
    }

    let mut records:Vec<_>=ActivityEvents::open(workspace).list_between(start,end)?.into_iter().map(|r|json!({"kind":"event","type":r["kind"],"timestamp":r["occurred_at"],"trace_id":r["local_trace_id"],"person_id":r["member_id"],"source":r["source"],"command":r["command"],"workflow":r["workflow"],"payload":r["payload"],"attributes":r["attributes"]})).collect();
    records.extend(
        store
            .activity_targets()?
            .into_iter()
            .filter(|r| parsed(text(r, "timestamp")).is_some_and(|t| t >= start && t < end)),
    );
    records.extend(crate::diagnostics::memory_records(workspace));
    records.retain(|r| parsed(text(r, "timestamp")).is_some_and(|t| t >= start && t <= end));
    let result = gb_observability::timeline::build(
        members,
        lifecycles,
        records,
        |id| store.transcript_exists(id),
        |key, params| crate::i18n::t(language, key, params),
    );
    Ok(result)
}
fn completed(workspace: &Path) -> Vec<Value> {
    std::fs::read(workspace.join(".guildbotics/local/run/activity_sync_weeks.json"))
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| v["completed"].as_array().cloned())
        .unwrap_or_default()
}
fn refresh(
    api: &Shared,
    workspace: &Path,
    start: DateTime<FixedOffset>,
    end: DateTime<FixedOffset>,
    force: bool,
) {
    let period = json!([iso_timestamp(start), iso_timestamp(end)]);
    let mut attempts = api.activity_sync.lock().unwrap_or_else(|p| p.into_inner());
    let key = (
        workspace.to_path_buf(),
        iso_timestamp(start),
        iso_timestamp(end),
    );
    if !force
        && ((end <= Utc::now() && completed(workspace).contains(&period))
            || attempts
                .get(&key)
                .is_some_and(|last| last.elapsed() < std::time::Duration::from_secs(300)))
    {
        return;
    }
    attempts.insert(key, std::time::Instant::now());
    let (api, workspace) = (api.clone(), workspace.to_path_buf());
    api.work.clone().spawn(async move {
        if let Err(e) = sync(&api, &workspace, start.to_utc(), end.to_utc()).await {
            tracing::warn!("activity refresh failed: {e:#}");
            return;
        }
        if end <= Utc::now() {
            let _guard = api.activity_sync.lock().unwrap_or_else(|p| p.into_inner());
            let mut periods = completed(&workspace);
            if !periods.contains(&period) {
                periods.push(period);
            }
            let _ = gb_base::files::atomic_write(
                &workspace.join(".guildbotics/local/run/activity_sync_weeks.json"),
                serde_json::to_vec(&json!({"completed":periods}))
                    .unwrap()
                    .as_slice(),
            );
        }
    });
}
async fn sync(
    api: &Shared,
    workspace: &Path,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> anyhow::Result<()> {
    #[cfg(test)]
    if let Some(run) = api.activity_runner.lock().unwrap().clone() {
        return run(start, end);
    }
    use gb_config::{
        commands::CommandRoots,
        resolved_guest,
        secrets::{DeviceKey, Scope, SecretStore},
    };
    let roots = CommandRoots {
        config: workspace.join(".guildbotics/config"),
        templates: gb_base::home::python_package().join("templates"),
    };
    let team = gb_config::team::Team::load(&roots.config, &api.identity_root)?;
    let project = gb_config::yaml_value(&std::fs::read_to_string(
        roots.config.join("team/project.yml"),
    )?)?;
    let writes = gb_base::shared_writes::SharedWrites::new(
        api.identity_root.clone(),
        Some(workspace.into()),
    );
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: api.data.parent().unwrap_or(&api.data).into(),
            keychain: api.keychain.as_ref(),
        },
    )?;
    let roster = team
        .members
        .iter()
        .map(|m| {
            resolved_guest::member(&roots, &api.identity_root, &m.person_id)
                .map(|v| crate::member::native::public(&v))
        })
        .collect::<anyhow::Result<Vec<_>>>()?;
    let mut tickets = None;
    for person in &roster {
        let config = crate::member::native::integration(
            workspace,
            &api.identity_root,
            &project,
            person,
            &roster,
        );
        if config.ticket_provider == "branvil" {
            let prefix =
                gb_config::team::person_secret_prefix(&api.identity_root, &person.person_id)?;
            if secrets
                .get(&(prefix + "BRANVIL_TOKEN"))?
                .is_none_or(|v| v.is_empty())
            {
                continue;
            }
        } else if config.ticket_provider != "local" {
            continue;
        }
        tickets = Some(
            gb_integrations::ServiceIntegrationFactory::new(
                config,
                &secrets,
                api.branvil.get().cloned(),
                Arc::new(|_| {}),
            )?
            .create_ticket_service()?,
        );
        break;
    }
    let Some(tickets) = tickets else {
        return Ok(());
    };
    let result = tickets.closed_since(start, end).await;
    let closed = tickets.aclose().await;
    let items = result?;
    closed?;
    let events = ActivityEvents::open(workspace);
    let workspace_secrets = SecretStore::new(
        &writes,
        Scope::Workspace,
        DeviceKey {
            machine_root: api.data.parent().unwrap_or(&api.data).into(),
            keychain: api.keychain.as_ref(),
        },
    )?;
    let redactor = gb_observability::redaction::Redactor::new(
        secrets
            .values()?
            .into_values()
            .chain(workspace_secrets.values()?.into_values()),
    );
    let imported = writes.transaction(|writer| {
        let mut imported = Vec::new();
        let existing = events.list_between(start, end)?;
        let mut ids = existing
            .iter()
            .filter_map(|r| {
                r["attributes"]["target.activity_id"]
                    .as_str()
                    .map(String::from)
            })
            .collect::<std::collections::BTreeSet<_>>();
        for item in items {
            let mut target = serde_json::to_value(&item)?;
            let kind = text(&target, "kind").to_owned();
            let id = format!(
                "{kind}:{}:{}",
                item.url,
                if item.merged { "merged" } else { "closed" }
            );
            if !ids.insert(id.clone()) {
                continue;
            }
            target["provider"] = project["services"]["ticket_service"]["name"].clone();
            let mut record = gb_member::events::target(&target, "closed", Some(item.merged));
            record["attributes"]["target.activity_id"] = id.into();
            record["timestamp"] = item.closed_at.into();
            record["kind"] = "event".into();
            record["source"] = target["provider"].clone();
            let record = redactor.redact(record);
            events.record(writer, &record, &redactor)?;
            imported.push(record.as_object().unwrap().clone());
        }
        Ok(imported)
    })?;
    let diagnostics = Diagnostics::open(workspace);
    for record in imported {
        diagnostics.record(record);
    }
    Ok(())
}
