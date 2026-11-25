//! Workspace diagnostics and change notifications for the browser.

use std::sync::{Arc, Mutex, OnceLock};

use gb_observability::{Diagnostics, Trace};
use serde_json::{Map, Value, json};

#[derive(Default)]
pub struct EventBus {
    store: Mutex<Option<Diagnostics>>,
    changes: Arc<OnceLock<branvil::api::DataApi>>,
    #[cfg(test)]
    recorded: Mutex<Vec<Value>>,
}

/// Where an event comes from: the trace it belongs to, and a source and
/// attributes that win over the trace's.
#[derive(Default)]
pub struct Origin<'a> {
    pub trace: Option<&'a Trace>,
    pub source: Option<&'a str>,
    pub attributes: Map<String, Value>,
}

impl EventBus {
    pub(crate) fn with_changes(changes: Arc<OnceLock<branvil::api::DataApi>>) -> Self {
        Self {
            changes,
            ..Self::default()
        }
    }

    fn store(&self) -> Option<Diagnostics> {
        self.store.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// Records what is published from now on in `store`: the selected
    /// workspace's diagnostics, or none while no workspace is selected.
    pub fn record_in(&self, store: Option<Diagnostics>) {
        *self.store.lock().unwrap_or_else(|p| p.into_inner()) = store;
    }

    /// Publishes `event_type` with `payload`, from any thread.
    pub fn publish(&self, event_type: &str, payload: Map<String, Value>, origin: Origin<'_>) {
        let mut item = Map::new();
        item.insert("kind".into(), "event".into());
        item.insert("type".into(), event_type.into());
        item.extend(correlation(origin));
        item.insert("payload".into(), payload.into());
        item.insert("timestamp".into(), timestamp());
        if let Some(store) = self.store() {
            store.record(item.clone());
        }
        self.announce(&item);
    }

    /// Announces a recorded change without sending its diagnostic payload.
    pub fn announce(&self, item: &Map<String, Value>) {
        #[cfg(test)]
        self.recorded
            .lock()
            .unwrap()
            .push(Value::Object(item.clone()));
        let Some(host) = self.changes.get() else {
            return;
        };
        match item.get("type").and_then(Value::as_str) {
            Some("workspace.changed") => host.workspace_changed(),
            Some(kind)
                if kind.starts_with("command.")
                    && item.get("source").and_then(Value::as_str) == Some("manual")
                    && item
                        .get("attributes")
                        .and_then(|a| a.get("run.entry"))
                        .and_then(Value::as_str)
                        == Some("command") =>
            {
                host.command_runs_changed();
            }
            _ => {}
        }
    }

    /// Records a log line of `level` (`INFO`, `WARNING`, ...).
    pub fn publish_log(&self, level: &str, message: &str, trace: Option<&Trace>) {
        self.record_log(level, message, trace.map(Trace::event_fields));
    }

    pub(crate) fn record_log(
        &self,
        level: &str,
        message: &str,
        fields: Option<Map<String, Value>>,
    ) {
        let mut item = Map::new();
        item.insert("kind".into(), "log".into());
        item.insert("level".into(), level.into());
        item.insert("message".into(), message.into());
        item.extend(fields.unwrap_or_else(|| correlation(Origin::default())));
        item.insert("timestamp".into(), timestamp());
        if let Some(store) = self.store() {
            store.record(item);
        }
    }

    #[cfg(test)]
    pub fn snapshot(&self) -> Vec<Value> {
        self.recorded.lock().unwrap().clone()
    }
}

fn correlation(origin: Origin<'_>) -> Map<String, Value> {
    let mut fields = match origin.trace {
        Some(trace) => trace.event_fields(),
        None => {
            let Value::Object(fields) = json!({
                "trace_id": null, "span_id": null, "parent_id": null, "span": "",
                "source": null, "person_id": "", "command": "", "workflow": "",
                "attributes": {},
            }) else {
                unreachable!("an object")
            };
            fields
        }
    };
    if let Some(source) = origin.source {
        fields.insert("source".into(), source.into());
    }
    if let Some(Value::Object(attributes)) = fields.get_mut("attributes") {
        attributes.extend(origin.attributes);
    }
    fields
}

fn timestamp() -> Value {
    chrono::Local::now()
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
        .into()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload(value: Value) -> Map<String, Value> {
        match value {
            Value::Object(map) => map,
            _ => panic!("an object"),
        }
    }

    fn records(workspace: &std::path::Path) -> Vec<Value> {
        let path = workspace.join(".guildbotics/local/run");
        let mut found = vec![];
        for file in walk(&path) {
            if file.extension().is_some_and(|e| e == "jsonl") {
                for line in std::fs::read_to_string(&file).unwrap().lines() {
                    found.push((file.clone(), serde_json::from_str::<Value>(line).unwrap()));
                }
            }
        }
        // One record may be both indexed and in its transcript.
        let mut unique: Vec<Value> = vec![];
        for (_, record) in found {
            if !unique.contains(&record) {
                unique.push(record);
            }
        }
        unique
    }

    fn walk(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return vec![];
        };
        entries
            .flatten()
            .flat_map(|e| {
                if e.path().is_dir() {
                    walk(&e.path())
                } else {
                    vec![e.path()]
                }
            })
            .collect()
    }

    #[test]
    fn a_log_is_stored_with_the_diagnostics() {
        let workspace = tempfile::tempdir().unwrap();
        let bus = EventBus::default();
        let diagnostics = Diagnostics::open(workspace.path());
        // A record outside any trace lands in the system transcript.
        diagnostics.start_system_session("service-run").unwrap();
        bus.record_in(Some(diagnostics.clone()));
        bus.publish(
            "command.started",
            payload(json!({"command": "hello"})),
            Origin::default(),
        );
        bus.publish_log("INFO", "log line", None);
        let recorded = records(workspace.path());
        assert_eq!(bus.snapshot()[0]["type"], "command.started");
        assert!(recorded.contains(&bus.snapshot()[0]), "{recorded:?}");
        let logs: Vec<_> = recorded.iter().filter(|r| r["kind"] == "log").collect();
        assert_eq!(logs.len(), 1);
        assert_eq!(
            (&logs[0]["level"], &logs[0]["message"]),
            (&json!("INFO"), &json!("log line"))
        );
        assert!(logs[0].get("type").is_none());
        assert!(logs[0]["trace_id"].is_null());
        diagnostics.finish_system_session().unwrap();
    }

    #[test]
    fn an_event_carries_the_correlation_of_its_trace() {
        let workspace = tempfile::tempdir().unwrap();
        let trace = Trace::open(&Diagnostics::open(workspace.path()), "manual")
            .person("alice")
            .command("hello");
        let bus = EventBus::default();
        bus.publish(
            "command.done",
            payload(json!({"ok": true})),
            Origin {
                trace: Some(&trace),
                ..Origin::default()
            },
        );
        let event = &bus.snapshot()[0];
        assert_eq!(event["kind"], "event");
        assert_eq!(event["trace_id"], trace.id());
        assert_eq!(event["source"], "manual");
        assert_eq!(event["command"], "hello");
        assert_eq!(event["person_id"], "alice");
        assert_eq!(event["payload"], json!({"ok": true}));
        assert!(!event["timestamp"].as_str().unwrap().is_empty());
        assert!(event.get("call_id").is_none());
    }

    #[test]
    fn an_event_without_payload_or_trace_has_empty_payload_and_null_correlation() {
        let bus = EventBus::default();
        bus.publish("empty", Map::new(), Origin::default());
        let event = &bus.snapshot()[0];
        let keys: Vec<_> = event.as_object().unwrap().keys().cloned().collect();
        let expected = [
            "kind",
            "type",
            "trace_id",
            "span_id",
            "parent_id",
            "span",
            "source",
            "person_id",
            "command",
            "workflow",
            "attributes",
            "payload",
            "timestamp",
        ];
        assert_eq!(keys, expected);
        assert_eq!(event["payload"], json!({}));
        for key in ["trace_id", "span_id", "parent_id", "source"] {
            assert!(event[key].is_null(), "{key}");
        }
        assert_eq!(event["attributes"], json!({}));
        let stamp = event["timestamp"].as_str().unwrap();
        assert!(
            chrono::DateTime::parse_from_rfc3339(stamp).is_ok(),
            "{stamp}"
        );
        assert_eq!(stamp.split('.').nth(1).unwrap().len(), "000000+09:00".len());
    }

    #[test]
    fn an_explicit_source_and_attributes_win_over_the_traces() {
        let workspace = tempfile::tempdir().unwrap();
        let trace = Trace::open(&Diagnostics::open(workspace.path()), "manual")
            .attribute("service_run_id", "old")
            .attribute("kept", "yes");
        let bus = EventBus::default();
        bus.publish(
            "scheduler.running",
            Map::new(),
            Origin {
                trace: Some(&trace),
                source: Some("scheduler"),
                attributes: payload(json!({"service_run_id": "svc-1"})),
            },
        );
        bus.publish(
            "scheduler.running",
            Map::new(),
            Origin {
                source: Some("scheduler"),
                attributes: payload(json!({"service_run_id": "svc-1"})),
                ..Origin::default()
            },
        );
        let events = bus.snapshot();
        assert_eq!(events[0]["source"], "scheduler");
        assert_eq!(
            events[0]["attributes"],
            json!({"service_run_id": "svc-1", "kept": "yes"})
        );
        assert_eq!(events[1]["source"], "scheduler");
        assert_eq!(events[1]["attributes"], json!({"service_run_id": "svc-1"}));
    }
}
