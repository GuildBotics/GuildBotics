//! Traces: one bounded unit of work each (one manual command run, one
//! scheduled run, one received event), as the Python version's
//! `guildbotics/utils/correlation.py` has them. A run is its trace
//! (`run_id == trace_id`): whatever starts a run takes the [`Trace`] it runs
//! in, so there is no way to start one outside a trace.

use std::future::Future;

use serde_json::{Map, Value, json};

use crate::diagnostics::Diagnostics;

/// One bounded unit of work, and where its records go.
pub struct Trace {
    diagnostics: Diagnostics,
    id: String,
    source: String,
    person_id: String,
    workflow: String,
    attributes: Map<String, Value>,
}

/// How a run that did not succeed ended, as its `command.failed` names it.
pub trait Failure {
    /// What failed, as the screens name it (the Python version's exception
    /// type name).
    fn error_type(&self) -> String;

    /// What the screens do about it: empty for a plain failure, or one the
    /// alert rules know, such as `cli_agent_authentication`.
    fn code(&self) -> &str {
        ""
    }
}

impl Trace {
    /// Opens a new trace from `source` (`manual`, `scheduled`, ...).
    pub fn open(diagnostics: &Diagnostics, source: &str) -> Trace {
        Trace {
            diagnostics: diagnostics.clone(),
            id: uuid::Uuid::new_v4().simple().to_string(),
            source: source.into(),
            person_id: String::new(),
            workflow: String::new(),
            attributes: Map::new(),
        }
    }

    pub fn person(mut self, person_id: &str) -> Trace {
        self.person_id = person_id.into();
        self
    }

    pub fn workflow(mut self, workflow: &str) -> Trace {
        self.workflow = workflow.into();
        self
    }

    /// Adds an attribute every record of the trace carries; a null adds none.
    pub fn attribute(mut self, key: &str, value: impl Into<Value>) -> Trace {
        match value.into() {
            Value::Null => {}
            value => {
                self.attributes.insert(key.into(), value);
            }
        }
        self
    }

    /// The trace's id, which is also the id of the run it covers.
    pub fn id(&self) -> &str {
        &self.id
    }

    fn record(
        &self,
        event_type: &str,
        person_id: &str,
        command: &str,
        payload: Map<String, Value>,
    ) {
        let Value::Object(record) = json!({
            "kind": "event",
            "type": event_type,
            "trace_id": self.id,
            "span_id": null,
            "parent_id": null,
            "call_id": null,
            "span": "",
            "source": self.source,
            // The member the run names, else the trace's, as in the Python version.
            "person_id": if person_id.is_empty() { &self.person_id } else { person_id },
            "command": command,
            "workflow": self.workflow,
            "attributes": self.attributes,
            "payload": payload,
            "timestamp": chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
        }) else {
            unreachable!("an object")
        };
        self.diagnostics.record(record);
    }

    /// Runs `run`, the command `command` of the member `person_id`, between
    /// the records that say it started and how it ended. Only the layer that
    /// opens a trace can say that the whole execution ended, and it says so
    /// however it ends: a run dropped before it ends, such as one cancelled by
    /// a stop, is recorded as cancelled, so its trace never reads as still
    /// running.
    pub async fn run_command<T, E: Failure>(
        &self,
        command: &str,
        person_id: &str,
        run: impl Future<Output = Result<T, E>>,
    ) -> Result<T, E> {
        let mut payload = Map::new();
        payload.insert("command".into(), command.into());
        payload.insert("person".into(), person_id.into());
        self.record("command.started", person_id, command, payload.clone());
        let mut unended = Unended {
            trace: self,
            command,
            person_id,
            payload: Some(payload),
        };
        let ended = run.await;
        let mut payload = unended.payload.take().expect("ends once");
        match &ended {
            Ok(_) => self.record("command.finished", person_id, command, payload),
            Err(failure) => {
                payload.insert("error_type".into(), failure.error_type().into());
                payload.insert("code".into(), failure.code().into());
                self.record("command.failed", person_id, command, payload);
            }
        }
        ended
    }
}

/// A run that has started and not ended: dropped as it is, it was cancelled,
/// or it panicked.
struct Unended<'a> {
    trace: &'a Trace,
    command: &'a str,
    person_id: &'a str,
    payload: Option<Map<String, Value>>,
}

impl Drop for Unended<'_> {
    fn drop(&mut self) {
        let Some(mut payload) = self.payload.take() else {
            return;
        };
        let (error_type, code) = if std::thread::panicking() {
            ("panic", "")
        } else {
            ("CancelledError", "cancelled")
        };
        payload.insert("error_type".into(), error_type.into());
        payload.insert("code".into(), code.into());
        self.trace
            .record("command.failed", self.person_id, self.command, payload);
    }
}
