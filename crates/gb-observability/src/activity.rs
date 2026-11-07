//! Provider-neutral facts in `state/events/<year>/<month>/<id>.json`.
use crate::redaction::{MAX_SHARED_TEXT_CHARS, Redactor};
use chrono::{DateTime, Datelike, FixedOffset, Utc};
use gb_base::{files::SHARED_RECORD_SCHEMA_VERSION, shared_writes::Writer};
use serde_json::{Value, json};
use sha2::Digest;
use std::path::{Path, PathBuf};

pub const DOMAIN_EVENT_TYPES: &[&str] = &[
    "repository.push",
    "pull_request.item",
    "ticket.item",
    "ticket.comment",
    "workflow.completed",
    "workflow.completion_missing",
    "workflow.rate_limited",
    "pull_request.review_limit",
    "chat_dispatch.retry_scheduled",
    "chat_dispatch.abandoned",
];

pub fn is_domain_activity_event(kind: &str) -> bool {
    DOMAIN_EVENT_TYPES.contains(&kind)
}

pub(crate) fn text<'a>(v: &'a Value, names: &[&str]) -> &'a str {
    names
        .iter()
        .find_map(|k| v.get(k).and_then(Value::as_str).filter(|s| !s.is_empty()))
        .unwrap_or("")
}

pub(crate) fn timestamp(v: &Value, key: &str) -> Option<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(v.get(key)?.as_str()?).ok()
}

pub struct ActivityEvents {
    root: PathBuf,
}
impl ActivityEvents {
    pub fn open(workspace: &Path) -> Self {
        Self {
            root: workspace.join(".guildbotics/state/events"),
        }
    }

    /// Reconcile a domain event independently of its local diagnostics record.
    /// The caller holds the shared write transaction across the check and append.
    pub fn record_once(
        &self,
        writer: &mut Writer<'_>,
        record: &Value,
        redactor: &Redactor,
    ) -> anyhow::Result<bool> {
        let (path, event) = self.once_record(record, redactor)?;
        if read_event(&path, &event)?.is_some() {
            return Ok(false);
        }
        writer.write_json(&path, &event)?;
        Ok(true)
    }

    /// Read only this event's durable location, independent of other activity history.
    pub fn existing_once(
        &self,
        record: &Value,
        redactor: &Redactor,
    ) -> anyhow::Result<Option<Value>> {
        let (path, event) = self.once_record(record, redactor)?;
        read_event(&path, &event)
    }

    fn once_record(&self, record: &Value, redactor: &Redactor) -> anyhow::Result<(PathBuf, Value)> {
        let trace = text(record, &["trace_id", "local_trace_id"]);
        anyhow::ensure!(!trace.is_empty(), "domain event requires a trace identity");
        // The terminal journal supplies a stable date, including after month boundaries.
        let date = timestamp(record, "timestamp")
            .ok_or_else(|| anyhow::anyhow!("domain event requires a timestamp"))?;
        let mut record = record.clone();
        record["event_id"] = sha2::Sha256::digest(serde_json::to_vec(&(
            trace,
            text(&record, &["type", "kind"]),
        ))?)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>()
        .into();
        let (_, event) = self.render(&record, redactor)?;
        Ok((self.path(&event, date)?, event))
    }

    pub fn record(
        &self,
        writer: &mut Writer<'_>,
        record: &Value,
        redactor: &Redactor,
    ) -> anyhow::Result<PathBuf> {
        let (path, event) = self.render(record, redactor)?;
        writer.write_json(&path, &event)?;
        Ok(path)
    }

    fn render(&self, record: &Value, redactor: &Redactor) -> anyhow::Result<(PathBuf, Value)> {
        let generated = uuid::Uuid::new_v4().simple().to_string();
        let now = Utc::now().to_rfc3339();
        let event_id = text(record, &["event_id"]);
        let event_id = if event_id.is_empty() {
            &generated
        } else {
            event_id
        };
        super::runs::checked_id(event_id)?;
        let occurred = text(record, &["timestamp", "occurred_at"]);
        let occurred = if occurred.is_empty() { &now } else { occurred };
        let mut event = json!({
            "schema_version": SHARED_RECORD_SCHEMA_VERSION,
            "event_id": event_id, "occurred_at": occurred,
            "member_id": text(record, &["person_id", "member_id"]),
            "kind": text(record, &["type", "kind"]),
            "local_trace_id": text(record, &["trace_id", "local_trace_id"]),
            "safe_summary": text(record, &["safe_summary", "type"]).chars().take(MAX_SHARED_TEXT_CHARS).collect::<String>(),
            "links": record.get("links").filter(|v| v.is_array()).cloned().unwrap_or(json!([])),
            "payload": record.get("payload").filter(|v| v.is_object()).cloned().unwrap_or(json!({})),
            "attributes": record.get("attributes").filter(|v| v.is_object()).cloned().unwrap_or(json!({})),
        });
        if event["kind"] == "" {
            event["kind"] = "event".into();
        }
        for key in [
            "workspace_id",
            "device_id",
            "subject",
            "run_id",
            "source",
            "command",
            "workflow",
        ] {
            event[key] = text(record, &[key]).into();
        }
        let event = redactor.redact(event);
        let date = timestamp(&event, "occurred_at").unwrap_or_else(|| Utc::now().fixed_offset());
        Ok((self.path(&event, date)?, event))
    }

    fn path(&self, event: &Value, date: DateTime<FixedOffset>) -> anyhow::Result<PathBuf> {
        // The bounded/masked identifier must name the file as well as the record.
        let id = event["event_id"].as_str().unwrap();
        super::runs::checked_id(id)?;
        Ok(self
            .root
            .join(format!("{:04}/{:02}/{id}.json", date.year(), date.month())))
    }

    pub fn list_between(
        &self,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> anyhow::Result<Vec<Value>> {
        let mut events = Vec::new();
        let first = start
            .checked_sub_signed(chrono::Duration::days(1))
            .unwrap_or(start);
        let last = end
            .checked_add_signed(chrono::Duration::days(1))
            .unwrap_or(end);
        // Scan existing directories, never enumerate calendar months. Offset
        // timestamps are compared as instants, including neighbouring months.
        for year in readable_children(&self.root) {
            let Some(year_number) = year
                .file_name()
                .and_then(|n| n.to_str())
                .and_then(|n| n.parse::<i32>().ok())
            else {
                continue;
            };
            if !year.is_dir() {
                continue;
            }
            for month in readable_children(&year) {
                let Some(month_number) = month
                    .file_name()
                    .and_then(|n| n.to_str())
                    .and_then(|n| n.parse::<u32>().ok())
                else {
                    continue;
                };
                if !month.is_dir() {
                    continue;
                }
                let calendar = (year_number, month_number);
                if calendar < (first.year(), first.month())
                    || calendar > (last.year(), last.month())
                {
                    continue;
                }
                for path in readable_children(&month) {
                    if path.extension().is_none_or(|e| e != "json") {
                        continue;
                    }
                    let Ok(bytes) = std::fs::read(path) else {
                        continue;
                    };
                    let Ok(v) = serde_json::from_slice::<Value>(&bytes) else {
                        continue;
                    };
                    if matches!(
                        text(&v, &["kind"]),
                        "command.started"
                            | "command.finished"
                            | "command.failed"
                            | "command.deferred"
                            | "member.command.started"
                            | "member.command.finished"
                            | "member.command.failed"
                    ) {
                        continue;
                    }
                    if timestamp(&v, "occurred_at").is_some_and(|t| t >= start && t <= end) {
                        events.push(v);
                    }
                }
            }
        }
        events.sort_by(|a, b| {
            (text(a, &["occurred_at"]), text(a, &["event_id"]))
                .cmp(&(text(b, &["occurred_at"]), text(b, &["event_id"])))
        });
        Ok(events)
    }
}

fn read_event(path: &Path, expected: &Value) -> anyhow::Result<Option<Value>> {
    match std::fs::read(path) {
        Ok(bytes) => {
            let event: Value = serde_json::from_slice(&bytes)?;
            for key in ["event_id", "kind", "local_trace_id", "occurred_at"] {
                anyhow::ensure!(
                    event[key] == expected[key],
                    "conflicting activity identity at {}",
                    path.display()
                );
            }
            anyhow::ensure!(
                event["payload"].is_object() && event["attributes"].is_object(),
                "invalid activity event at {}",
                path.display()
            );
            Ok(Some(event))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

/// Display scans preserve readable siblings even when a directory or entry fails.
pub(crate) fn readable_children(path: &Path) -> Vec<PathBuf> {
    let mut paths: Vec<_> = std::fs::read_dir(path)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .collect();
    paths.sort();
    paths
}

pub(crate) fn children(path: &Path) -> std::io::Result<Vec<PathBuf>> {
    let entries = match std::fs::read_dir(path) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut paths = entries
        .map(|e| e.map(|e| e.path()))
        .collect::<Result<Vec<_>, _>>()?;
    paths.sort();
    Ok(paths)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn domain_allowlist_keeps_device_health_and_lifecycle_local() {
        for kind in DOMAIN_EVENT_TYPES {
            assert!(is_domain_activity_event(kind));
        }
        for kind in [
            "review_limit.started",
            "review_limit.finished",
            "review_limit.failed",
            "review_limit.deferred",
            "command.started",
            "command.finished",
            "member.command.started",
            "member.command.failed",
            "span.finished",
            "session.pointer",
            "credential.failed",
            "diagnostics.completed",
            "verify.completed",
            "scheduler.worker.failed",
            "workflow.started",
            "github.debug",
        ] {
            assert!(!is_domain_activity_event(kind), "{kind}");
        }
    }

    #[test]
    fn display_scans_existing_months_for_bounded_and_calendar_wide_windows() {
        let root = tempfile::tempdir().unwrap();
        let store = ActivityEvents::open(root.path());
        let writes = gb_base::shared_writes::SharedWrites::new(
            root.path().join("identity"),
            Some(root.path().into()),
        );
        writes
            .transaction(|writer| {
                for occurred in [
                    "2026-06-30T23:00:00Z",
                    "2026-07-15T09:00:00Z",
                    "2026-09-01T00:00:00Z",
                    "2027-01-10T08:00:00Z",
                ] {
                    store.record(
                        writer,
                        &json!({"type":"repository.push","timestamp":occurred}),
                        &Redactor::default(),
                    )?;
                }
                Ok(())
            })
            .unwrap();
        let start = DateTime::parse_from_rfc3339("2026-07-01T00:00:00Z")
            .unwrap()
            .to_utc();
        let end = DateTime::parse_from_rfc3339("2026-08-31T00:00:00Z")
            .unwrap()
            .to_utc();
        let bounded = store.list_between(start, end).unwrap();
        assert_eq!(bounded.len(), 1);
        assert_eq!(bounded[0]["occurred_at"], "2026-07-15T09:00:00Z");
        let all = store
            .list_between(DateTime::<Utc>::MIN_UTC, DateTime::<Utc>::MAX_UTC)
            .unwrap();
        assert_eq!(all.len(), 4);
        // Unreadable files do not discard other entries in their month.
        std::fs::create_dir_all(store.root.join("2026/07/unreadable.json")).unwrap();
        std::fs::write(store.root.join("2026/07/corrupt.json"), "broken").unwrap();
        assert_eq!(store.list_between(start, end).unwrap(), bounded);
        std::fs::remove_dir_all(&store.root).unwrap();
        std::fs::write(&store.root, "not a directory").unwrap();
        assert!(store.list_between(start, end).unwrap().is_empty());
    }

    #[test]
    fn recovery_partition_uses_the_journal_date_even_when_redacted() {
        let root = tempfile::tempdir().unwrap();
        let store = ActivityEvents::open(root.path());
        let redactor = Redactor::new(["2026-10-".into()]);
        let event = json!({"type":"workflow.completed","trace_id":"run","timestamp":"2026-10-31T23:59:00Z"});
        let (path, rendered) = store.once_record(&event, &redactor).unwrap();
        assert_eq!(path.parent().unwrap(), store.root.join("2026/10"));
        assert!(timestamp(&rendered, "occurred_at").is_none());
    }

    #[test]
    fn recovery_dedup_reads_only_the_stable_target_and_fails_closed_on_its_errors() {
        let root = tempfile::tempdir().unwrap();
        let store = ActivityEvents::open(root.path());
        let writes = gb_base::shared_writes::SharedWrites::new(
            root.path().join("identity"),
            Some(root.path().into()),
        );
        let redactor = Redactor::new(["private-trace".into()]);
        let event = json!({"type":"workflow.completed","trace_id":"private-trace","timestamp":"2026-10-31T23:59:00Z"});
        let (path, _) = store.once_record(&event, &redactor).unwrap();
        assert_eq!(path.parent().unwrap(), store.root.join("2026/10"));
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        for contents in ["invalid json", "{}", r#"{"kind":"other"}"#] {
            std::fs::write(&path, contents).unwrap();
            assert!(
                writes
                    .transaction(|writer| store.record_once(writer, &event, &redactor))
                    .is_err()
            );
        }
        std::fs::remove_file(&path).unwrap();
        std::fs::create_dir(&path).unwrap();
        assert!(
            writes
                .transaction(|writer| store.record_once(writer, &event, &redactor))
                .is_err()
        );
        std::fs::remove_dir(&path).unwrap();
        let unrelated = store.root.join("2025/01/broken.json");
        std::fs::create_dir_all(unrelated.parent().unwrap()).unwrap();
        std::fs::write(unrelated, "broken").unwrap();
        std::fs::create_dir_all(store.root.join("2026/10/unreadable.json")).unwrap();
        assert!(
            writes
                .transaction(|writer| store.record_once(writer, &event, &redactor))
                .unwrap()
        );
        let restarted = ActivityEvents::open(root.path());
        assert!(
            !writes
                .transaction(|writer| restarted.record_once(writer, &event, &redactor))
                .unwrap()
        );
        assert!(
            restarted
                .existing_once(&event, &redactor)
                .unwrap()
                .is_some()
        );
        let mut other = event.clone();
        other["trace_id"] = "another-private-trace".into();
        assert!(
            writes
                .transaction(|writer| store.record_once(writer, &other, &redactor))
                .unwrap()
        );
        assert_ne!(
            store.once_record(&event, &redactor).unwrap().0,
            store.once_record(&other, &redactor).unwrap().0
        );
    }
}
