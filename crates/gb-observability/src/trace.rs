//! Traces: one bounded unit of work each (one manual command run, one
//! scheduled run, one received event), as the Python version's
//! `guildbotics/utils/correlation.py` has them. A run is its trace
//! (`run_id == trace_id`): whatever starts a run takes the [`Trace`] it runs
//! in, so there is no way to start one outside a trace.

use std::future::Future;

use serde_json::{Map, Value, json};

use crate::diagnostics::Diagnostics;

/// One bounded unit of work, and where its records go.
#[derive(Clone)]
pub struct Trace {
    diagnostics: Diagnostics,
    id: String,
    source: String,
    person_id: String,
    workflow: String,
    command: String,
    attributes: Map<String, Value>,
    span: Option<SpanContext>,
}

/// How a run that did not succeed ended, as its `command.failed` names it.
pub trait Failure {
    fn deferred(&self, _source: &str) -> bool {
        false
    }
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
    /// Reopen the boundary of a persisted retry, retaining its run identity.
    pub fn resume(diagnostics: &Diagnostics, source: &str, id: &str) -> anyhow::Result<Self> {
        crate::runs::checked_id(id)?;
        let mut trace = Self::open(diagnostics, source);
        trace.id = id.into();
        Ok(trace)
    }
    /// Opens a new trace from `source` (`manual`, `scheduled`, ...).
    pub fn open(diagnostics: &Diagnostics, source: &str) -> Trace {
        Trace {
            diagnostics: diagnostics.clone(),
            id: uuid::Uuid::new_v4().simple().to_string(),
            source: source.into(),
            person_id: String::new(),
            workflow: String::new(),
            command: String::new(),
            attributes: Map::new(),
            span: None,
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

    /// Identify the command on domain events outside its execution future.
    pub fn command(mut self, command: &str) -> Self {
        self.command = command.into();
        self
    }

    /// Correlation for a domain event written to the shared activity store.
    pub fn correlation(&self) -> Value {
        json!({"trace_id":self.id,"source":self.source,"person_id":self.person_id,
            "command":self.command,"workflow":self.workflow,"attributes":self.attributes})
    }

    /// The correlation an event of the server's event bus carries: the
    /// trace's, and its span's without the call id.
    pub fn event_fields(&self) -> Map<String, Value> {
        let Value::Object(fields) = json!({
            "trace_id": self.id,
            "span_id": self.span.as_ref().map(|s| &s.id),
            "parent_id": self.span.as_ref().and_then(|s| s.parent_id.as_ref()),
            "span": self.span.as_ref().map(|s| s.name.as_str()).unwrap_or(""),
            "source": self.source,
            "person_id": self.person_id,
            "command": self.command,
            "workflow": self.workflow,
            "attributes": self.attributes,
        }) else {
            unreachable!("an object")
        };
        fields
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

    fn record_value(
        &self,
        event_type: &str,
        person_id: &str,
        command: &str,
        payload: Map<String, Value>,
    ) -> Map<String, Value> {
        let Value::Object(record) = json!({
            "kind": if event_type == "command.output" { "io" } else { "event" },
            "type": event_type,
            "trace_id": self.id,
            "span_id": self.span.as_ref().map(|s| &s.id),
            "parent_id": self.span.as_ref().and_then(|s| s.parent_id.as_ref()),
            "call_id": self.span.as_ref().map(|s| &s.call_id),
            "span": self.span.as_ref().map(|s| s.name.as_str()).unwrap_or(""),
            "source": self.source,
            // The member the run names, else the trace's, as in the Python version.
            "person_id": if person_id.is_empty() { &self.person_id } else { person_id },
            "command": if command.is_empty() { &self.command } else { command },
            "workflow": self.workflow,
            "attributes": self.attributes,
            "payload": payload,
            "timestamp": chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
        }) else {
            unreachable!("an object")
        };
        record
    }

    fn record(
        &self,
        event_type: &str,
        person_id: &str,
        command: &str,
        payload: Map<String, Value>,
    ) {
        self.diagnostics
            .record(self.record_value(event_type, person_id, command, payload));
    }

    /// Append a domain event only when this trace transcript does not already contain its type.
    /// Read/write failures prevent the caller from acknowledging recoverable work.
    pub fn event_once(
        &self,
        event_type: &str,
        payload: Map<String, Value>,
    ) -> std::io::Result<bool> {
        self.diagnostics
            .record_once(self.record_value(event_type, "", "", payload))
    }

    /// Join an existing execution without opening or ending a boundary.
    pub fn join(diagnostics: &Diagnostics, id: &str) -> JoinedTrace {
        JoinedTrace {
            trace: Trace {
                diagnostics: diagnostics.clone(),
                id: id.into(),
                source: String::new(),
                person_id: String::new(),
                workflow: String::new(),
                command: String::new(),
                attributes: Map::new(),
                span: None,
            },
        }
    }

    /// A nested operation has its own span and call ids, but the same trace.
    pub fn span(&self, name: &str) -> JoinedTrace {
        let mut trace = self.clone();
        trace.span = Some(SpanContext {
            id: uuid::Uuid::new_v4().simple().to_string(),
            call_id: uuid::Uuid::new_v4().simple().to_string(),
            parent_id: self.span.as_ref().map(|s| s.id.clone()),
            name: name.into(),
        });
        JoinedTrace { trace }
    }

    /// Write an event with this execution's correlation fields.
    pub fn event(&self, event_type: &str, payload: Map<String, Value>) {
        self.record(event_type, "", "", payload);
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
                self.record(
                    if failure.deferred(&self.source) {
                        "command.deferred"
                    } else {
                        "command.failed"
                    },
                    person_id,
                    command,
                    payload,
                );
            }
        }
        ended
    }

    /// Records the nonempty text returned by a command, before its boundary ends.
    pub fn command_output(&self, command: &str, person_id: &str, output: &str) {
        if !output.is_empty() {
            let mut payload = Map::new();
            payload.insert("output".into(), output.into());
            self.record("command.output", person_id, command, payload);
        }
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

#[derive(Clone)]
pub struct SpanContext {
    pub id: String,
    pub call_id: String,
    pub parent_id: Option<String>,
    pub name: String,
}

/// A participant can record operations, but cannot run an owner's boundary.
/// Correlation is explicit, so it cannot leak to another concurrent task.
pub struct JoinedTrace {
    trace: Trace,
}

impl JoinedTrace {
    /// Bind the host's span when recording work received from another process.
    pub fn bind_span(mut self, span: SpanContext) -> Self {
        self.trace.span = Some(span);
        self
    }

    pub fn command(mut self, command: &str) -> Self {
        self.trace.command = command.into();
        self
    }

    pub fn person(mut self, person_id: &str) -> Self {
        self.trace.person_id = person_id.into();
        self
    }

    pub fn attribute(mut self, key: &str, value: impl Into<Value>) -> Self {
        self.trace = self.trace.attribute(key, value);
        self
    }

    pub fn span(&self, name: &str) -> Self {
        self.trace.span(name)
    }

    pub fn event(&self, event_type: &str, payload: Map<String, Value>) {
        self.trace.event(event_type, payload);
    }
}
