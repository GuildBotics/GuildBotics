//! The diagnostics records of a workspace, written as the Python version
//! writes them (`guildbotics/observability/diagnostics_store.py`,
//! `session_transcripts.py`), so that either version reads what the other
//! wrote: every record of a trace in its transcript
//! (`.guildbotics/local/run/sessions/<trace>.jsonl`), and the index
//! (`.guildbotics/local/run/diagnostics.jsonl`) holding a pointer to each
//! transcript and the records the execution list is built from.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{Map, Value, json};
use sha2::Digest;

/// The size at which the index is rewritten with its newest records only.
const INDEX_MAX_BYTES: u64 = 8 * 1024 * 1024;
/// How many records a rewritten index keeps.
const INDEX_RETAINED: usize = 5000;
const MAX_SESSION_ID_LENGTH: usize = 180;

/// The events the index holds besides the pointers: the boundaries of
/// executions and what the execution list reads of them.
const INDEX_EVENT_TYPES: &[&str] = &[
    "command.started",
    "command.finished",
    "command.failed",
    "command.deferred",
    "member.command.started",
    "member.command.finished",
    "member.command.failed",
    "diagnostics.completed",
    "verify.completed",
    "span.finished",
    "span.failed",
    "scheduler.failed",
    "scheduler.running",
    "scheduler.worker.failed",
    "workflow.completed",
    "workflow.completion_missing",
    "chat_dispatch.retry_scheduled",
    "chat_dispatch.abandoned",
    "workflow.rate_limited",
    "credential.failed",
    "credential.verified",
    "target.recorded",
];

/// The events that end an execution, which carry its final counts.
const FINISHED_BOUNDARIES: &[&str] = &[
    "command.finished",
    "command.failed",
    "command.deferred",
    "member.command.finished",
    "member.command.failed",
    "system.finished",
];

/// The diagnostics records of one workspace.
#[derive(Clone)]
pub struct Diagnostics {
    pub(crate) index: PathBuf,
    pub(crate) sessions: PathBuf,
}

/// Every writer of this process appends one record at a time, whichever
/// workspace or `Diagnostics` it writes through: deciding whether a
/// transcript exists and counting it at an end share the span of the write.
static WRITING: Mutex<()> = Mutex::new(());

impl Diagnostics {
    pub fn open(workspace: &Path) -> Diagnostics {
        let run = workspace.join(".guildbotics/local/run");
        Diagnostics {
            index: run.join("diagnostics.jsonl"),
            sessions: run.join("sessions"),
        }
    }

    /// Records `record`, a record of the trace it names. A record that cannot
    /// be written is logged rather than failing what it records, as in the
    /// Python version.
    pub(crate) fn record(&self, record: Map<String, Value>) {
        let _writing = WRITING
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Err(e) = self.route(record) {
            tracing::warn!("cannot write a diagnostics record: {e}");
        }
    }

    fn route(&self, mut record: Map<String, Value>) -> std::io::Result<()> {
        let trace_id = text(&record, "trace_id");
        let name = format!("{}.jsonl", safe_id(&trace_id));
        let transcript = self.sessions.join(&name);
        let mut indexed = Vec::new();
        if !transcript.exists() {
            indexed.push(pointer(&record, &format!("sessions/{name}")));
        }
        if FINISHED_BOUNDARIES.contains(&text(&record, "type").as_str()) {
            let mut counts = Counts::default();
            for existing in read_records(&transcript)? {
                counts.add(&existing);
            }
            counts.add(&record);
            let mut payload = match record.remove("payload") {
                Some(Value::Object(payload)) => payload,
                _ => Map::new(),
            };
            payload.extend(counts.payload());
            record.insert("payload".into(), payload.into());
        }
        append(&transcript, &[&record])?;
        if INDEX_EVENT_TYPES.contains(&text(&record, "type").as_str()) {
            indexed.push(record);
        }
        self.index(&indexed)
    }

    /// Appends to the index, rewriting it with its newest records once it has
    /// grown to its limit. The rewrite keeps the file, so a reader holding a
    /// position in it relocates rather than replays it.
    fn index(&self, records: &[Map<String, Value>]) -> std::io::Result<()> {
        if records.is_empty() {
            return Ok(());
        }
        let full = std::fs::metadata(&self.index).is_ok_and(|m| m.len() >= INDEX_MAX_BYTES);
        if !full {
            return append(&self.index, &records.iter().collect::<Vec<_>>());
        }
        let mut kept = read_records(&self.index)?;
        kept.extend(records.iter().cloned());
        let start = kept.len().saturating_sub(INDEX_RETAINED);
        let mut text = String::new();
        for record in &kept[start..] {
            text.push_str(&Value::Object(record.clone()).to_string());
            text.push('\n');
        }
        std::fs::write(&self.index, text)
    }
}

/// The index's record that a trace's transcript exists and where.
fn pointer(record: &Map<String, Value>, path: &str) -> Map<String, Value> {
    let mut attributes = match record.get("attributes") {
        Some(Value::Object(attributes)) => attributes.clone(),
        _ => Map::new(),
    };
    attributes.insert("session.path".into(), path.into());
    let Value::Object(pointer) = json!({
        "kind": "event",
        "type": "session.pointer",
        "timestamp": text(record, "timestamp"),
        "trace_id": text(record, "trace_id"),
        "span_id": null,
        "parent_id": null,
        "call_id": null,
        "span": "",
        "source": text(record, "source"),
        "person_id": text(record, "person_id"),
        "command": text(record, "command"),
        "workflow": text(record, "workflow"),
        "attributes": attributes,
        "payload": {"path": path},
    }) else {
        unreachable!("an object")
    };
    pointer
}

/// What an execution's records add up to, as its end records them.
#[derive(Default)]
struct Counts {
    events: u64,
    logs: u64,
    errors: u64,
    spans: std::collections::BTreeSet<String>,
}

impl Counts {
    fn add(&mut self, record: &Map<String, Value>) {
        match text(record, "kind").as_str() {
            "event" => {
                self.events += 1;
                if text(record, "type").ends_with(".failed") {
                    self.errors += 1;
                }
            }
            "log" => {
                self.logs += 1;
                if matches!(
                    text(record, "level").to_uppercase().as_str(),
                    "ERROR" | "CRITICAL"
                ) {
                    self.errors += 1;
                }
            }
            _ => {}
        }
        let span = text(record, "span_id");
        if !span.is_empty() {
            self.spans.insert(span);
        }
    }

    fn payload(&self) -> Map<String, Value> {
        let Value::Object(counts) = json!({
            "event_count": self.events,
            "log_count": self.logs,
            "error_count": self.errors,
            "span_count": self.spans.len(),
        }) else {
            unreachable!("an object")
        };
        counts
    }
}

/// A field as a string, empty when absent or null, as `str(x or "")` reads it.
fn text(record: &Map<String, Value>, key: &str) -> String {
    match record.get(key) {
        Some(Value::String(s)) => s.clone(),
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(other) => other.to_string(),
    }
}

/// The transcript's file name of a trace: its id where that is a plain name,
/// else the id's SHA-256.
pub(crate) fn safe_id(id: &str) -> String {
    let mut cleaned = String::new();
    let mut replaced = false;
    for c in id.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-') {
            cleaned.push(c);
            replaced = false;
        } else if !replaced {
            cleaned.push('-');
            replaced = true;
        }
    }
    let cleaned = cleaned.trim_matches(['.', '-']);
    if !cleaned.is_empty() && cleaned.chars().count() <= MAX_SESSION_ID_LENGTH {
        return cleaned.into();
    }
    sha2::Sha256::digest(id.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn append(path: &Path, records: &[&Map<String, Value>]) -> std::io::Result<()> {
    std::fs::create_dir_all(path.parent().expect("a file has a parent"))?;
    let mut text = String::new();
    for record in records {
        text.push_str(&Value::Object((*record).clone()).to_string());
        text.push('\n');
    }
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?
        .write_all(text.as_bytes())
}

/// The records of a JSONL file, skipping lines that are not JSON objects.
pub(crate) fn read_records(path: &Path) -> std::io::Result<Vec<Map<String, Value>>> {
    let text = match std::fs::read(path) {
        Ok(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    Ok(text
        .lines()
        .filter_map(|line| match serde_json::from_str(line) {
            Ok(Value::Object(record)) => Some(record),
            _ => None,
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(trace: &str, kind: &str, event_type: &str) -> Map<String, Value> {
        let Value::Object(record) = json!({
            "kind": kind, "type": event_type, "trace_id": trace, "payload": {},
            "timestamp": "2026-11-02T00:00:00+00:00", "attributes": {"a": 1},
        }) else {
            unreachable!()
        };
        record
    }

    #[test]
    fn a_transcript_is_named_by_the_trace_id_when_it_is_a_plain_name() {
        assert_eq!(safe_id("0123abcd"), "0123abcd");
        assert_eq!(safe_id("system:a/b..c"), "system-a-b..c");
        assert_eq!(safe_id("..-x-.."), "x");
        let long = "a".repeat(181);
        for hashed in ["", "//", long.as_str()] {
            let name = safe_id(hashed);
            assert_eq!(name.len(), 64, "{hashed}");
            assert!(name.bytes().all(|b| b.is_ascii_hexdigit()));
        }
    }

    #[test]
    fn the_index_holds_a_pointer_and_the_boundaries_and_the_end_counts_all() {
        let workspace = tempfile::tempdir().unwrap();
        let diagnostics = Diagnostics::open(workspace.path());
        diagnostics.record(event("t", "event", "command.started"));
        let mut log = event("t", "log", "");
        log.insert("level".into(), "error".into());
        log.insert("span_id".into(), "s".into());
        diagnostics.record(log);
        diagnostics.record(event("t", "event", "command.finished"));

        let run = workspace.path().join(".guildbotics/local/run");
        let index = read_records(&run.join("diagnostics.jsonl")).unwrap();
        let types: Vec<_> = index.iter().map(|r| text(r, "type")).collect();
        assert_eq!(
            types,
            ["session.pointer", "command.started", "command.finished"]
        );
        assert_eq!(
            index[0]["attributes"],
            json!({"a": 1, "session.path": "sessions/t.jsonl"})
        );
        assert_eq!(index[0]["payload"], json!({"path": "sessions/t.jsonl"}));
        let transcript = read_records(&run.join("sessions/t.jsonl")).unwrap();
        assert_eq!(transcript.len(), 3);
        let counts = json!({"event_count": 2, "log_count": 1, "error_count": 1, "span_count": 1});
        assert_eq!(transcript[2]["payload"], counts);
        assert_eq!(index[2]["payload"], counts);
    }

    #[test]
    fn a_full_index_is_rewritten_in_place_with_its_newest_records() {
        let workspace = tempfile::tempdir().unwrap();
        let diagnostics = Diagnostics::open(workspace.path());
        let index = workspace
            .path()
            .join(".guildbotics/local/run/diagnostics.jsonl");
        std::fs::create_dir_all(index.parent().unwrap()).unwrap();
        let line = format!("{}\n", json!({"type": "old", "pad": "x".repeat(1000)}));
        let lines = (INDEX_MAX_BYTES as usize).div_ceil(line.len());
        std::fs::write(&index, format!("not json\n{}", line.repeat(lines))).unwrap();
        #[cfg(unix)]
        let inode = std::os::unix::fs::MetadataExt::ino(&std::fs::metadata(&index).unwrap());

        diagnostics.record(event("t", "event", "command.started"));
        let kept = read_records(&index).unwrap();
        assert_eq!(kept.len(), INDEX_RETAINED);
        let types: Vec<_> = kept[INDEX_RETAINED - 2..]
            .iter()
            .map(|r| text(r, "type"))
            .collect();
        assert_eq!(types, ["session.pointer", "command.started"]);
        #[cfg(unix)]
        assert_eq!(
            std::os::unix::fs::MetadataExt::ino(&std::fs::metadata(&index).unwrap()),
            inode
        );
    }
}
