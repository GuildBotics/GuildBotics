//! Provider-neutral facts in `state/events/<year>/<month>/<id>.json`.
use crate::redaction::{MAX_SHARED_TEXT_CHARS, Redactor};
use chrono::{DateTime, Datelike, FixedOffset, Utc};
use gb_base::{files::SHARED_RECORD_SCHEMA_VERSION, shared_writes::Writer};
use serde_json::{Value, json};
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
        let trace = text(record, &["trace_id", "local_trace_id"]);
        anyhow::ensure!(!trace.is_empty(), "domain event requires a trace identity");
        // Recovery must fail closed on unreadable records, unlike a best-effort activity feed.
        for year in children(&self.root)? {
            if !year.is_dir() {
                continue;
            }
            for month in children(&year)? {
                if !month.is_dir() {
                    continue;
                }
                for path in children(&month)? {
                    if path.extension().is_none_or(|extension| extension != "json") {
                        continue;
                    }
                    let existing: Value = serde_json::from_slice(&std::fs::read(path)?)?;
                    if text(&existing, &["local_trace_id"]) == trace
                        && text(&existing, &["kind"]) == text(record, &["type", "kind"])
                    {
                        return Ok(false);
                    }
                }
            }
        }
        self.record(writer, record, redactor)?;
        Ok(true)
    }

    pub fn record(
        &self,
        writer: &mut Writer<'_>,
        record: &Value,
        redactor: &Redactor,
    ) -> anyhow::Result<PathBuf> {
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
        // The bounded/masked identifier must name the file as well as the record.
        let id = event["event_id"].as_str().unwrap();
        super::runs::checked_id(id)?;
        let path = self
            .root
            .join(format!("{:04}/{:02}/{id}.json", date.year(), date.month()));
        writer.write_json(&path, &event)?;
        Ok(path)
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
        for year in children(&self.root)? {
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
            for month in children(&year)? {
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
                for path in children(&month)? {
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
    fn recovery_dedup_fails_closed_on_unreadable_or_malformed_event_records() {
        let root = tempfile::tempdir().unwrap();
        let store = ActivityEvents::open(root.path());
        let writes = gb_base::shared_writes::SharedWrites::new(
            root.path().join("identity"),
            Some(root.path().into()),
        );
        let path = store.root.join("2026/11/prior.json");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let redactor = Redactor::new(Vec::<String>::new());
        let event = json!({"type":"workflow.completed","trace_id":"run"});
        std::fs::write(&path, "invalid json").unwrap();
        assert!(
            writes
                .transaction(|writer| store.record_once(writer, &event, &redactor))
                .is_err()
        );
        std::fs::remove_file(&path).unwrap();
        std::fs::create_dir(&path).unwrap();
        assert!(
            writes
                .transaction(|writer| store.record_once(writer, &event, &redactor))
                .is_err()
        );
        std::fs::remove_dir(&path).unwrap();
        assert!(
            writes
                .transaction(|writer| store.record_once(writer, &event, &redactor))
                .unwrap()
        );
        assert!(
            !writes
                .transaction(|writer| store.record_once(writer, &event, &redactor))
                .unwrap()
        );
    }
}
