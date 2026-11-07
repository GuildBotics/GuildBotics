//! Durable device health, folded from recorded facts rather than current settings.
use crate::Diagnostics;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or_default()
}
pub fn inference_key(r: &Value) -> Option<(&str, &str)> {
    (text(r, "kind") == "event"
        && matches!(text(r, "type"), "span.finished" | "span.failed")
        && matches!(text(&r["attributes"], "credential.provider"), "llm" | "jev"))
    .then(|| {
        (
            text(&r["attributes"], "credential.provider"),
            text(&r["attributes"], "llm.provider"),
        )
    })
}
fn timestamp(r: &Value) -> Option<chrono::DateTime<chrono::Utc>> {
    let s = text(r, "timestamp");
    chrono::DateTime::parse_from_rfc3339(s)
        .map(|t| t.to_utc())
        .or_else(|_| {
            chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f")
                .or_else(|_| chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f"))
                .map(|t| t.and_utc())
        })
        .ok()
}
impl Diagnostics {
    pub fn last_cli_agent_turns(&self) -> Value {
        let (records, _) = self.records_after(None);
        let mut latest = BTreeMap::new();
        for r in records {
            let a = &r["attributes"];
            let p = &r["payload"];
            let person = text(&r, "person_id");
            let tool = text(a, "agent.adapter");
            let Some(when) = timestamp(&r) else { continue };
            if !matches!(text(&r, "type"), "span.finished" | "span.failed")
                || a["agent.kind"] != "cli_agent"
                || person.is_empty()
                || tool.is_empty()
                || !p["model_specified"].is_boolean()
            {
                continue;
            }
            let key = (person.to_owned(), tool.to_owned());
            if latest.get(&key).is_none_or(|(at, _)| when >= *at) {
                latest.insert(key, (when, json!({"person_id":person,"agent":tool,"model":text(p,"model"),"model_specified":p["model_specified"],"effort":text(p,"effort"),"timestamp":r["timestamp"]})));
            }
        }
        latest.into_values().map(|(_, v)| v).collect()
    }
    pub fn inference_failures(&self) -> Value {
        let (records, _) = self.records_after(None);
        let mut latest = BTreeMap::new();
        for r in records {
            let Some((service, provider)) = inference_key(&r) else {
                continue;
            };
            let Some(when) = timestamp(&r) else { continue };
            let failed = r["type"] == "span.failed";
            let a = &r["attributes"];
            if failed && text(a, "error.category").is_empty() {
                continue;
            }
            let key = (service.to_owned(), provider.to_owned());
            if latest.get(&key).is_none_or(|(at, _)| when >= *at) {
                latest.insert(key,(when,if failed {json!({"category":a["error.category"],"timestamp":r["timestamp"],"status_code":a["error.status_code"],"response":text(a,"error.response")})}else{Value::Null}));
            }
        }
        let mut llm = serde_json::Map::new();
        let mut jev = Value::Null;
        for ((service, provider), (_, failure)) in latest {
            if service == "llm" && !provider.is_empty() && !failure.is_null() {
                llm.insert(provider, failure);
            } else if service == "jev" && provider.is_empty() {
                jev = failure;
            }
        }
        json!({"llm":llm,"jev":jev})
    }
}

#[derive(Default)]
pub struct SystemAlerts {
    alerts: BTreeMap<String, Value>,
    dismissed: BTreeSet<String>,
}
impl SystemAlerts {
    /// Serialization by the caller covers load, fold, dismiss and save together.
    pub fn list(
        store: &Diagnostics,
        runtime: &Value,
        problems: &[Value],
        dismiss: Option<&str>,
    ) -> Value {
        let path = store.index.with_file_name("system-alerts.json");
        let state = std::fs::read(&path)
            .ok()
            .and_then(|s| serde_json::from_slice::<Value>(&s).ok());
        let mut this = Self::default();
        let mut cursor = None;
        if let Some(state) = state {
            for alert in state["alerts"].as_array().into_iter().flatten() {
                if alert.is_object() {
                    this.alerts.insert(text(alert, "id").into(), alert.clone());
                }
            }
            this.dismissed = state["dismissed"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect();
            cursor = if state["version"] == 3 {
                state.get("cursor").cloned()
            } else {
                Some(store.records_after(None).1)
            };
        }
        let (records, cursor) = store.records_after(cursor.as_ref());
        for r in records {
            if r["kind"] == "event" {
                this.apply(&r);
            }
        }
        let chat = format!(
            "credential:{}:",
            text(&runtime["events"], "events_chat_provider")
        );
        let failed: BTreeSet<_> = runtime["events"]["events_auth_failed_persons"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(|p| format!("{chat}{p}"))
            .collect();
        this.dismissed.retain(|k| {
            !(k.starts_with("agent-environment:")
                || k == "runtime:scheduler" && runtime["scheduler"]["state"] != "failed"
                || k.starts_with(&chat) && !failed.contains(k))
        });
        let mut visible = this.alerts.clone();
        for problem in problems {
            let person = text(problem, "person_id");
            let slot = text(problem, "slot");
            let setting = text(problem, "setting");
            let (key, code) = match setting {
                "image_differs" => (
                    "agent-environment:image".into(),
                    "agent_environment_image_differs",
                ),
                "tool" => (
                    format!("agent-environment:tool:{slot}"),
                    "agent_environment_tool_unavailable",
                ),
                "grants" => (
                    format!("agent-environment:{person}:{slot}"),
                    "agent_environment_slot_blocked",
                ),
                _ => (
                    "agent-environment:device".into(),
                    "agent_environment_unavailable",
                ),
            };
            visible.entry(key.clone()).or_insert_with(|| {
                alert(
                    &key,
                    code,
                    "warning",
                    "",
                    person,
                    slot,
                    "",
                    "",
                    text(problem, "reason"),
                    setting,
                    json!(["setup"]),
                )
            });
        }
        if runtime["scheduler"]["state"] == "failed"
            && !this.dismissed.contains("runtime:scheduler")
        {
            let s = &runtime["scheduler"];
            visible
                .entry("runtime:scheduler".into())
                .or_insert_with(|| {
                    alert(
                        "runtime:scheduler",
                        "scheduler_failed",
                        "critical",
                        s["stopped_at"]
                            .as_str()
                            .filter(|s| !s.is_empty())
                            .unwrap_or(text(s, "started_at")),
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        json!(["service", "diagnostics"]),
                    )
                });
        }
        for key in failed {
            if !this.dismissed.contains(&key) {
                let person = key.strip_prefix(&chat).unwrap();
                visible.entry(key.clone()).or_insert_with(|| {
                    alert(
                        &key,
                        "credential",
                        "critical",
                        text(&runtime["events"], "started_at"),
                        person,
                        "",
                        text(&runtime["events"], "events_chat_provider"),
                        "",
                        "",
                        "",
                        json!(["diagnostics", "setup"]),
                    )
                });
            }
        }
        if let Some(id) = dismiss.filter(|id| visible.contains_key(*id)) {
            this.alerts.remove(id);
            this.dismissed.insert(id.into());
            // Runtime environment problems intentionally reappear immediately.
            if !id.starts_with("agent-environment:") {
                visible.remove(id);
            }
        }
        let saved = json!({"version":3,"cursor":cursor,"alerts":this.alerts.into_values().collect::<Vec<_>>(),"dismissed":this.dismissed});
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(path, saved.to_string());
        let mut alerts: Vec<_> = visible.into_values().collect();
        alerts.sort_by_key(|a| {
            (
                a["severity"] != "critical",
                text(a, "opened_at").to_owned(),
                text(a, "id").to_owned(),
            )
        });
        json!({"alerts":alerts})
    }
    #[allow(clippy::too_many_arguments)]
    fn open(
        &mut self,
        key: String,
        code: &str,
        severity: &str,
        r: &Value,
        person: &str,
        command: &str,
        provider: &str,
        trace: &str,
        reason: &str,
        actions: Value,
    ) {
        self.dismissed.remove(&key);
        if let Some(a) = self.alerts.get_mut(&key) {
            a["updated_at"] = r["timestamp"].clone();
            a["occurrence_count"] = (a["occurrence_count"].as_u64().unwrap_or(1) + 1).into();
            if !trace.is_empty() {
                a["trace_id"] = trace.into();
            }
            a["reason"] = reason.into();
        } else {
            self.alerts.insert(
                key.clone(),
                alert(
                    &key,
                    code,
                    severity,
                    text(r, "timestamp"),
                    person,
                    command,
                    provider,
                    trace,
                    reason,
                    "",
                    actions,
                ),
            );
        }
    }
    #[allow(clippy::too_many_arguments)]
    fn credential(
        &mut self,
        service: &str,
        scope: &str,
        r: &Value,
        trace: &str,
        reason: &str,
        actions: Value,
        prefix: &str,
    ) {
        let (code, person, command, provider) = match service {
            "llm" => ("credential_llm", "", scope, ""),
            "jev" => ("credential_jev", "", scope, ""),
            _ => ("credential", scope, "", service),
        };
        self.open(
            format!("{prefix}:{service}:{scope}"),
            code,
            "critical",
            r,
            person,
            command,
            provider,
            trace,
            reason,
            actions,
        );
    }
    fn remove_cause(&mut self, person: &str, trace: &str) {
        if !trace.is_empty() {
            self.alerts.retain(|k, a| {
                !(execution(k) && a["person_id"] == person && a["trace_id"] == trace)
            });
        }
    }
    fn apply(&mut self, r: &Value) {
        let person = text(r, "person_id");
        let trace = text(r, "trace_id");
        let payload = &r["payload"];
        match text(r, "type") {
            "diagnostics.completed" | "verify.completed" => {
                let verify = r["type"] == "verify.completed";
                if !verify && text(payload, "scope_person_id").is_empty() {
                    self.alerts.retain(|key, _| {
                        let parts: Vec<_> = key.splitn(3, ':').collect();
                        if parts.len() != 3
                            || parts[0] != "credential"
                            || matches!(parts[1], "llm" | "jev")
                        {
                            return true;
                        }
                        (!payload["active_members"].is_array()
                            || parts[2].is_empty()
                            || payload["active_members"]
                                .as_array()
                                .unwrap()
                                .iter()
                                .any(|p| p == parts[2]))
                            && (!payload["providers"].is_array()
                                || payload["providers"]
                                    .as_array()
                                    .unwrap()
                                    .iter()
                                    .any(|p| p == parts[1]))
                    });
                }
                let mut groups: BTreeMap<(&str, &str), Vec<&Value>> = BTreeMap::new();
                for c in payload["checks"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|c| c.is_object())
                {
                    let service = if verify {
                        if c["code"] != "llm_api_key" {
                            continue;
                        }
                        "llm"
                    } else {
                        if c["credential"] != true {
                            continue;
                        }
                        text(c, "section")
                    };
                    let scope = if matches!(service, "llm" | "jev") {
                        text(&c["context"], "provider")
                    } else {
                        text(c, "person_id")
                    };
                    groups.entry((service, scope)).or_default().push(c);
                }
                if verify && groups.is_empty() {
                    self.alerts.retain(|k, _| !k.starts_with("credential:llm:"));
                }
                for ((service, scope), checks) in groups {
                    if checks.iter().any(|c| c["status"] == "error") {
                        self.credential(
                            service,
                            scope,
                            r,
                            "",
                            "",
                            json!(["diagnostics", "setup"]),
                            "credential",
                        );
                    } else if checks.iter().any(|c| c["status"] == "ok") {
                        self.alerts.remove(&format!("credential:{service}:{scope}"));
                    }
                }
            }
            "command.failed" | "workflow.rate_limited" => {
                let rate = r["type"] == "workflow.rate_limited";
                if !rate
                    && matches!(
                        text(payload, "code"),
                        "cancelled"
                            | "cli_agent_authentication"
                            | "person_not_found"
                            | "person_selection_required"
                    )
                {
                    return;
                }
                if !trace.is_empty()
                    && self.alerts.iter().any(|(k, a)| {
                        (k.starts_with("credential:") || k.starts_with("inference:"))
                            && (a["person_id"] == person || a["person_id"] == "")
                            && a["trace_id"] == trace
                    })
                {
                    return;
                }
                let command = r["command"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or(text(payload, "command"));
                let code = if rate {
                    "rate_limited"
                } else {
                    "command_failed"
                };
                if !rate
                    && self
                        .alerts
                        .contains_key(&format!("rate_limited:{person}:{command}"))
                {
                    return;
                }
                if rate {
                    self.alerts
                        .remove(&format!("command_failed:{person}:{command}"));
                }
                self.open(
                    format!("{code}:{person}:{command}"),
                    code,
                    "warning",
                    r,
                    person,
                    command,
                    "",
                    trace,
                    "",
                    if trace.is_empty() {
                        json!(["diagnostics"])
                    } else {
                        json!(["trace"])
                    },
                );
            }
            "command.finished" => self.alerts.retain(|k, a| {
                !(execution(k)
                    && a["person_id"] == person
                    && !(a["code"] == "rate_limited"
                        && !trace.is_empty()
                        && a["trace_id"] == trace))
            }),
            "credential.failed" => {
                let provider = text(payload, "provider");
                if !matches!(provider, "github" | "slack" | "local" | "branvil") {
                    return;
                }
                self.remove_cause(person, trace);
                let actions = if trace.is_empty() {
                    json!(["diagnostics", "setup"])
                } else {
                    json!(["diagnostics", "setup", "trace"])
                };
                self.credential(provider, person, r, trace, "", actions, "credential");
            }
            "credential.verified" => {
                let key = format!("credential:{}:{person}", text(payload, "provider"));
                self.alerts.remove(&key);
                self.dismissed.remove(&key);
            }
            "scheduler.worker.failed" | "scheduler.failed" => {
                let worker = r["type"] == "scheduler.worker.failed";
                self.open(
                    if worker {
                        format!("runtime:worker:{person}")
                    } else {
                        "runtime:scheduler".into()
                    },
                    if worker {
                        "worker_stopped"
                    } else {
                        "scheduler_failed"
                    },
                    "critical",
                    r,
                    if worker { person } else { "" },
                    "",
                    "",
                    "",
                    "",
                    json!(["service", "diagnostics"]),
                );
            }
            "scheduler.running" => {
                let keys: Vec<_> = self
                    .alerts
                    .keys()
                    .filter(|k| {
                        k.as_str() == "runtime:scheduler" || k.starts_with("runtime:worker:")
                    })
                    .cloned()
                    .collect();
                for k in keys {
                    self.alerts.remove(&k);
                    self.dismissed.remove(&k);
                }
            }
            _ => {
                if let Some((service, provider)) = inference_key(r) {
                    let key = format!("inference:{service}:{provider}");
                    if r["type"] == "span.finished" {
                        for k in [key, format!("credential:{service}:{provider}")] {
                            self.alerts.remove(&k);
                            self.dismissed.remove(&k);
                        }
                    } else {
                        let category = text(&r["attributes"], "error.category");
                        if matches!(category, "authentication" | "credit") {
                            self.remove_cause(person, trace);
                            self.credential(
                                service,
                                provider,
                                r,
                                trace,
                                category,
                                if trace.is_empty() {
                                    json!(["setup"])
                                } else {
                                    json!(["setup", "trace"])
                                },
                                "inference",
                            );
                        }
                    }
                }
            }
        }
    }
}
fn execution(k: &str) -> bool {
    k.starts_with("command_failed:") || k.starts_with("rate_limited:")
}
#[allow(clippy::too_many_arguments)]
fn alert(
    id: &str,
    code: &str,
    severity: &str,
    timestamp: &str,
    person: &str,
    command: &str,
    provider: &str,
    trace: &str,
    reason: &str,
    setting: &str,
    actions: Value,
) -> Value {
    json!({"id":id,"code":code,"severity":severity,"opened_at":timestamp,"updated_at":timestamp,"occurrence_count":1,"person_id":person,"command":command,"provider":provider,"trace_id":trace,"reason":reason,"setting":setting,"actions":actions})
}
