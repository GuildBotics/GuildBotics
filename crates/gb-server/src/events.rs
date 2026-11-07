//! The server's in-process event bus, as the Python version's
//! (`guildbotics/app_api/events.py`): every event is recorded in the selected
//! workspace's diagnostics and handed to every subscriber, which first
//! receives the latest ones. A log is recorded only: it is neither kept nor
//! streamed.

use std::collections::VecDeque;
use std::sync::Mutex;

use gb_observability::{Diagnostics, Trace};
use serde_json::{Map, Value, json};
use tokio::sync::mpsc;

/// How many of the latest events a new subscriber receives first.
pub const HISTORY_LIMIT: usize = 200;

pub struct EventBus {
    limit: usize,
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    history: VecDeque<Value>,
    subscribers: Vec<mpsc::UnboundedSender<Value>>,
    store: Option<Diagnostics>,
}

/// Where an event comes from: the trace it belongs to, and a source and
/// attributes that win over the trace's.
#[derive(Default)]
pub struct Origin<'a> {
    pub trace: Option<&'a Trace>,
    pub source: Option<&'a str>,
    pub attributes: Map<String, Value>,
}

/// The events published after it was opened, the latest ones first. Dropping
/// it closes it: nothing is sent to it afterwards.
pub struct Subscription(mpsc::UnboundedReceiver<Value>);

impl Subscription {
    pub async fn next(&mut self) -> Option<Value> {
        self.0.recv().await
    }
}

impl Default for EventBus {
    fn default() -> Self {
        EventBus::new(HISTORY_LIMIT)
    }
}

impl EventBus {
    pub fn new(limit: usize) -> EventBus {
        EventBus {
            limit,
            state: Mutex::default(),
        }
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// Records what is published from now on in `store`: the selected
    /// workspace's diagnostics, or none while no workspace is selected.
    pub fn record_in(&self, store: Option<Diagnostics>) {
        self.state().store = store;
    }

    /// Publishes `event_type` with `payload`, from any thread.
    pub fn publish(&self, event_type: &str, payload: Map<String, Value>, origin: Origin<'_>) {
        let mut item = Map::new();
        item.insert("kind".into(), "event".into());
        item.insert("type".into(), event_type.into());
        item.extend(correlation(origin));
        item.insert("payload".into(), payload.into());
        item.insert("timestamp".into(), timestamp());
        self.deliver(Value::Object(item), true);
    }

    /// Hands `item`, an event already recorded, to every subscriber.
    pub fn announce(&self, item: &Map<String, Value>) {
        self.deliver(Value::Object(item.clone()), false);
    }

    fn deliver(&self, item: Value, record: bool) {
        let store = {
            let mut state = self.state();
            if state.history.len() == self.limit {
                state.history.pop_front();
            }
            if self.limit > 0 {
                state.history.push_back(item.clone());
            }
            state
                .subscribers
                .retain(|subscriber| subscriber.send(item.clone()).is_ok());
            state.store.clone()
        };
        if let (true, Some(store), Value::Object(item)) = (record, store, item) {
            store.record(item);
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
        let store = self.state().store.clone();
        if let Some(store) = store {
            store.record(item);
        }
    }

    /// Opens a subscription that receives the kept events in the order they
    /// were published, then every later one, with none missed or repeated.
    pub fn subscribe(&self) -> Subscription {
        let (sender, receiver) = mpsc::unbounded_channel();
        let mut state = self.state();
        for item in &state.history {
            let _ = sender.send(item.clone());
        }
        state.subscribers.push(sender);
        Subscription(receiver)
    }

    /// The kept events, oldest first.
    pub fn snapshot(&self) -> Vec<Value> {
        self.state().history.iter().cloned().collect()
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
    use std::time::Duration;

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

    async fn nothing_within(subscription: &mut Subscription, wait: Duration) {
        assert!(
            tokio::time::timeout(wait, subscription.next())
                .await
                .is_err(),
            "nothing more is sent"
        );
    }

    #[test]
    fn history_keeps_only_the_latest_events_up_to_its_limit() {
        let bus = EventBus::new(3);
        for index in 0..5 {
            bus.publish("tick", payload(json!({"index": index})), Origin::default());
        }
        let kept: Vec<_> = bus
            .snapshot()
            .iter()
            .map(|e| e["payload"]["index"].clone())
            .collect();
        assert_eq!(kept, [json!(2), json!(3), json!(4)]);
        assert_eq!(EventBus::default().limit, 200);
    }

    #[tokio::test]
    async fn a_new_subscriber_first_receives_the_history_in_order() {
        let bus = EventBus::default();
        bus.publish("first", payload(json!({"n": 1})), Origin::default());
        bus.publish("second", payload(json!({"n": 2})), Origin::default());
        let mut subscription = bus.subscribe();
        bus.publish("third", payload(json!({"n": 3})), Origin::default());
        for (kind, n) in [("first", 1), ("second", 2), ("third", 3)] {
            let event = subscription.next().await.unwrap();
            assert_eq!(
                (event["type"].as_str(), &event["payload"]),
                (Some(kind), &json!({"n": n}))
            );
        }
        nothing_within(&mut subscription, Duration::from_millis(200)).await;
    }

    #[tokio::test]
    async fn a_closed_subscription_receives_nothing() {
        let bus = EventBus::default();
        let subscription = bus.subscribe();
        drop(subscription);
        bus.publish("after", Map::new(), Origin::default());
        assert!(
            bus.state().subscribers.is_empty(),
            "a closed one is forgotten"
        );
        let mut open = bus.subscribe();
        assert_eq!(open.next().await.unwrap()["type"], "after");
        nothing_within(&mut open, Duration::from_millis(200)).await;
    }

    #[tokio::test]
    async fn a_log_is_recorded_but_never_streamed_nor_kept_in_history() {
        let workspace = tempfile::tempdir().unwrap();
        let bus = EventBus::default();
        let diagnostics = Diagnostics::open(workspace.path());
        // A record outside any trace lands in the system transcript.
        diagnostics.start_system_session("service-run").unwrap();
        bus.record_in(Some(diagnostics.clone()));
        let mut subscription = bus.subscribe();
        bus.publish(
            "command.started",
            payload(json!({"command": "hello"})),
            Origin::default(),
        );
        bus.publish_log("INFO", "log line", None);
        let streamed = subscription.next().await.unwrap();
        assert_eq!(streamed["type"], "command.started");
        assert!(streamed.get("level").is_none());
        nothing_within(&mut subscription, Duration::from_millis(200)).await;
        assert_eq!(bus.snapshot(), std::slice::from_ref(&streamed));
        let recorded = records(workspace.path());
        assert!(recorded.contains(&streamed), "{recorded:?}");
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

    #[tokio::test]
    async fn an_event_carries_the_correlation_of_its_trace() {
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
        // A later subscriber is replayed the event with its trace.
        let mut subscription = bus.subscribe();
        let event = subscription.next().await.unwrap();
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

    #[tokio::test]
    async fn a_publish_from_another_thread_reaches_an_async_subscriber() {
        let bus = std::sync::Arc::new(EventBus::default());
        let mut subscription = bus.subscribe();
        let publisher = bus.clone();
        std::thread::spawn(move || {
            publisher.publish("from-thread", payload(json!({"n": 42})), Origin::default())
        })
        .join()
        .unwrap();
        let event = tokio::time::timeout(Duration::from_secs(2), subscription.next())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            (event["type"].as_str(), &event["payload"]),
            (Some("from-thread"), &json!({"n": 42}))
        );
    }
}
