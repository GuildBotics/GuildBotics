//! Reading the index and command transcripts written by either host.

use crate::diagnostics::{Diagnostics, read_records, safe_id};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}

fn stamp(value: &Value) -> (i64, String) {
    let s = text(value, "timestamp");
    (
        chrono::DateTime::parse_from_rfc3339(s)
            .map(|d| d.timestamp_micros())
            .unwrap_or(0),
        s.into(),
    )
}

impl Diagnostics {
    fn completion_summaries(&self) -> BTreeMap<(String, String), String> {
        let workspace = self
            .index
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .and_then(|p| p.parent());
        workspace
            .and_then(|w| crate::runs::RunStore::open(w).summaries_by_subject().ok())
            .unwrap_or_default()
    }
    fn indexed(&self) -> std::io::Result<Vec<Value>> {
        let mut records = read_records(&self.index)?;
        let start = records
            .len()
            .saturating_sub(crate::diagnostics::INDEX_RETAINED);
        Ok(records.drain(start..).map(Value::Object).collect())
    }

    /// Execution summaries use the index, never transcript I/O as searchable text.
    pub fn traces(
        &self,
        source: Option<&str>,
        person: Option<&str>,
        query: Option<&str>,
        attr_key: Option<&str>,
        attr_value: Option<&str>,
        limit: usize,
    ) -> std::io::Result<Value> {
        let mut groups: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        let indexed = self.indexed()?;
        for record in &indexed {
            let id = text(record, "trace_id").to_owned();
            if !id.is_empty() {
                groups.entry(id).or_default().push(record.clone());
            }
        }
        let mut summaries = Vec::new();
        let completions = std::cell::OnceCell::new();
        for (id, records) in groups {
            let summary = summary(&id, &records, |attrs, person| {
                completed_title(
                    completions.get_or_init(|| self.completion_summaries()),
                    attrs,
                    person,
                )
            });
            if source.is_some_and(|s| !s.is_empty() && text(&summary, "source") != s)
                || person.is_some_and(|s| !s.is_empty() && text(&summary, "person_id") != s)
            {
                continue;
            }
            if let (Some(k), Some(v)) = (
                attr_key.filter(|s| !s.is_empty()),
                attr_value.filter(|s| !s.is_empty()),
            ) && summary["attributes"].get(k).map(display).as_deref() != Some(v)
            {
                continue;
            }
            if let Some(q) = query.filter(|s| !s.is_empty()) {
                let mut searchable = ["trace_id", "source", "person_id", "command", "workflow"]
                    .iter()
                    .map(|k| text(&summary, k).to_owned())
                    .collect::<Vec<_>>();
                searchable.push(summary["attributes"].to_string());
                for r in &records {
                    match text(r, "kind") {
                        "event" => searchable.push(text(r, "type").into()),
                        "log" => searchable.push(text(r, "message").into()),
                        "memory" => {
                            searchable.push(format!("{} {}", text(r, "type"), r["payload"]))
                        }
                        _ => {}
                    }
                }
                if !searchable
                    .join(" ")
                    .to_lowercase()
                    .contains(&q.to_lowercase())
                {
                    continue;
                }
            }
            summaries.push(summary);
        }
        if source.is_none() && person.is_none() && attr_key.is_none() {
            summaries.extend(system_summaries(&indexed, false).into_iter().filter(|s| {
                query.is_none_or(|q| s.to_string().to_lowercase().contains(&q.to_lowercase()))
            }));
        }
        summaries.sort_by_key(|s| std::cmp::Reverse(stamp(&json!({"timestamp":s["started_at"]}))));
        summaries.truncate(limit);
        Ok(json!({"traces":summaries}))
    }

    pub fn trace_detail(&self, id: &str) -> std::io::Result<Value> {
        let path = self.sessions.join(format!(
            "{}.jsonl",
            safe_id(id.strip_prefix("system:").unwrap_or(id))
        ));
        let summary = if id.starts_with("system:") {
            system_summaries(&self.indexed()?, true)
                .into_iter()
                .find(|s| s["trace_id"] == id)
                .unwrap_or(Value::Null)
        } else {
            let indexed: Vec<_> = self
                .indexed()?
                .into_iter()
                .filter(|r| text(r, "trace_id") == id)
                .collect();
            if indexed.is_empty() {
                Value::Null
            } else {
                summary(id, &indexed, |attrs, person| {
                    completed_title(&self.completion_summaries(), attrs, person)
                })
            }
        };
        let mut records: Vec<Value> = read_records(&path)?
            .into_iter()
            .map(Value::Object)
            .collect();
        records.sort_by_key(stamp);
        let mut records = crate::streams::collapse(records);
        if let Some(workspace) = self.index.ancestors().nth(4) {
            let mut audit = Vec::new();
            for path in crate::activity::children(
                &workspace.join(".guildbotics/state/documents/memory_events"),
            )? {
                if path.extension().is_some_and(|e| e == "jsonl") {
                    audit.extend(
                        read_records(&path)?
                            .into_iter()
                            .map(Value::Object)
                            .filter(|r| text(r, "trace_id") == id),
                    );
                }
            }
            audit.reverse();
            audit.sort_by_key(|r| std::cmp::Reverse(stamp(r)));
            audit.truncate(1000);
            records.extend(audit);
        }
        records.sort_by_key(stamp);
        Ok(
            json!({"trace_id":id,"summary":summary,"records":records.iter().map(record).collect::<Vec<_>>(),"transcript_available":path.is_file()}),
        )
    }
}

fn display(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        _ => v.to_string(),
    }
}

fn summary(
    id: &str,
    records: &[Value],
    completion: impl FnOnce(&Map<String, Value>, &str) -> String,
) -> Value {
    let mut result = json!({"trace_id":id,"source":"","person_id":"","command":"","workflow":"","title":"",
        "started_at":"","updated_at":"","status":"info","event_count":0,"log_count":0,"error_count":0,"span_count":0,"attributes":{}});
    if id.starts_with("system:") {
        result["source"] = "system".into();
        result["command"] = "Global / system".into();
    }
    let mut status = crate::TraceStatus::default();
    let mut spans = BTreeSet::new();
    let mut sorted: Vec<_> = records.iter().collect();
    sorted.sort_by_key(|r| stamp(r));
    for r in &sorted {
        status.add(r);
        let timestamp = text(r, "timestamp");
        if !timestamp.is_empty() {
            if result["started_at"] == "" {
                result["started_at"] = timestamp.into();
            }
            result["updated_at"] = timestamp.into();
        }
        for key in ["source", "person_id", "command", "workflow"] {
            if result[key] == "" && r[key].is_string() {
                result[key] = r[key].clone();
            }
        }
        if !text(r, "span_id").is_empty() {
            spans.insert(text(r, "span_id"));
        }
        let event = text(r, "type");
        let error = event.ends_with(".failed");
        match text(r, "kind") {
            "event" => {
                increment(&mut result, "event_count");
                if error {
                    increment(&mut result, "error_count");
                }
                if event.ends_with(".finished") || error {
                    for key in ["event_count", "log_count", "error_count", "span_count"] {
                        if r["payload"][key].is_u64() {
                            result[key] = r["payload"][key].clone();
                        }
                    }
                }
            }
            "log" => {
                increment(&mut result, "log_count");
                if ["ERROR", "CRITICAL"].contains(&text(r, "level").to_uppercase().as_str()) {
                    increment(&mut result, "error_count");
                }
            }
            _ => {}
        }
    }
    result["status"] = status.resolve().into();
    result["span_count"] = result["span_count"]
        .as_u64()
        .unwrap_or(0)
        .max(spans.len() as u64)
        .into();
    let ordered: Vec<_> = sorted.into_iter().cloned().collect();
    result["attributes"] = crate::first_seen_attributes(&ordered).into();
    let title = crate::resolve_trace_title(
        &ordered,
        result["attributes"].as_object().unwrap(),
        text(&result, "command"),
        text(&result, "workflow"),
        id,
        || {
            completion(
                result["attributes"].as_object().unwrap(),
                text(&result, "person_id"),
            )
        },
        |provider| {
            format!(
                "{} thread",
                provider
                    .chars()
                    .enumerate()
                    .map(|(i, c)| if i == 0 {
                        c.to_uppercase().collect::<String>()
                    } else {
                        c.to_lowercase().collect()
                    })
                    .collect::<String>()
            )
        },
    );
    result["title"] = title.into();
    result
}

fn system_summaries(indexed: &[Value], include_latest: bool) -> Vec<Value> {
    let mut groups: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    let mut order = Vec::new();
    for r in indexed {
        if !matches!(text(r, "type"), "system.started" | "system.finished") {
            continue;
        }
        let Some(id) = r["attributes"]["system_session_id"]
            .as_str()
            .filter(|s| !s.is_empty())
        else {
            continue;
        };
        if !groups.contains_key(id) {
            order.push(id.to_string());
        }
        groups.entry(id.into()).or_default().push(r.clone());
    }
    let latest = order.last();
    groups
        .into_iter()
        .filter_map(|(id, records)| {
            if Some(&id) == latest && !include_latest {
                return None;
            }
            let mut s = summary(&format!("system:{id}"), &records, |_, _| String::new());
            if Some(&id) != latest && !records.iter().any(|r| r["type"] == "system.finished") {
                s["status"] = "interrupted".into();
            }
            Some(s)
        })
        .collect()
}

fn completed_title(
    summaries: &BTreeMap<(String, String), String>,
    attrs: &Map<String, Value>,
    person: &str,
) -> String {
    let attr = |key| attrs.get(key).and_then(Value::as_str).unwrap_or("");
    let provider = attr("chat.provider").trim();
    let subject = if provider.is_empty() {
        attr("target.url").to_string()
    } else {
        format!(
            "{provider}:{}:{}:{}",
            attr("chat.channel"),
            attr("chat.thread"),
            attr("event_id")
        )
    };
    summaries
        .get(&(subject, person.into()))
        .cloned()
        .unwrap_or_default()
}

fn increment(result: &mut Value, key: &str) {
    result[key] = (result[key].as_u64().unwrap_or(0) + 1).into();
}

fn record(raw: &Value) -> Value {
    let mut result = Map::new();
    for key in [
        "kind",
        "timestamp",
        "span",
        "source",
        "person_id",
        "command",
        "workflow",
        "type",
        "level",
        "message",
    ] {
        result.insert(key.into(), text(raw, key).into());
    }
    for key in ["trace_id", "span_id", "parent_id", "call_id"] {
        result.insert(key.into(), raw.get(key).cloned().unwrap_or(Value::Null));
    }
    for key in ["attributes", "payload"] {
        result.insert(
            key.into(),
            raw.get(key)
                .filter(|v| v.is_object())
                .cloned()
                .unwrap_or(json!({})),
        );
    }
    result.insert(
        "presentation".into(),
        crate::presentations::presentation(raw),
    );
    result.into()
}
