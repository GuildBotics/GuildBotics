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
    fn indexed(&self) -> std::io::Result<Vec<Value>> {
        Ok(read_records(&self.index)?
            .into_iter()
            .map(Value::Object)
            .collect())
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
        for record in self.indexed()? {
            let id = text(&record, "trace_id").to_owned();
            if !id.is_empty() {
                groups.entry(id).or_default().push(record);
            }
        }
        let mut summaries = Vec::new();
        for (id, records) in groups {
            let summary = summary(&id, &records);
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
        summaries.sort_by_key(|s| std::cmp::Reverse(stamp(&json!({"timestamp":s["started_at"]}))));
        summaries.truncate(limit);
        Ok(json!({"traces":summaries}))
    }

    pub fn trace_detail(&self, id: &str) -> std::io::Result<Value> {
        let indexed: Vec<_> = self
            .indexed()?
            .into_iter()
            .filter(|r| text(r, "trace_id") == id)
            .collect();
        let path = self.sessions.join(format!("{}.jsonl", safe_id(id)));
        let mut records: Vec<Value> = read_records(&path)?
            .into_iter()
            .map(Value::Object)
            .collect();
        records.sort_by_key(stamp);
        Ok(
            json!({"trace_id":id, "summary":if indexed.is_empty() {Value::Null} else {summary(id,&indexed)},
            "records":records.iter().map(record).collect::<Vec<_>>(), "transcript_available":path.is_file()}),
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

fn summary(id: &str, records: &[Value]) -> Value {
    let mut result = json!({"trace_id":id,"source":"","person_id":"","command":"","workflow":"","title":"",
        "started_at":"","updated_at":"","status":"info","event_count":0,"log_count":0,"error_count":0,"span_count":0,"attributes":{}});
    let mut status = "info";
    let mut spans = BTreeSet::new();
    let mut sorted: Vec<_> = records.iter().collect();
    sorted.sort_by_key(|r| stamp(r));
    for r in &sorted {
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
        if let Some(attrs) = r["attributes"].as_object() {
            for (k, v) in attrs {
                result["attributes"]
                    .as_object_mut()
                    .unwrap()
                    .entry(k)
                    .or_insert(v.clone());
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
                    status = "failed";
                } else if status != "failed" && status != "success" {
                    if event == "command.finished" || event == "member.command.finished" {
                        status = "success";
                    } else if event.ends_with(".started") || event.ends_with(".finished") {
                        status = "running";
                    }
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
                    status = "failed";
                }
            }
            _ => {}
        }
    }
    result["status"] = status.into();
    result["span_count"] = result["span_count"]
        .as_u64()
        .unwrap_or(0)
        .max(spans.len() as u64)
        .into();
    let title = result["attributes"]["target.title"]
        .as_str()
        .or_else(|| result["attributes"]["memory.title"].as_str())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            sorted
                .iter()
                .find_map(|r| r["payload"]["title"].as_str().filter(|s| !s.is_empty()))
        })
        .or_else(|| {
            ["workflow", "command"]
                .iter()
                .find_map(|k| result[*k].as_str().filter(|s| !s.is_empty()))
        })
        .or_else(|| {
            sorted.iter().find_map(|r| {
                ["type", "event", "message"]
                    .iter()
                    .find_map(|k| r[*k].as_str().filter(|s| !s.is_empty()))
            })
        })
        .unwrap_or(id)
        .to_owned();
    result["title"] = title.into();
    result
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
    result.insert("presentation".into(), presentation(raw));
    result.into()
}

fn presentation(raw: &Value) -> Value {
    let event = text(raw, "type");
    let payload = &raw["payload"];
    let mut p = json!({"label_key":"","label_fallback":event,"message_key":"","message":"","message_params":{},"tone":"neutral","effort":""});
    if text(raw, "kind") == "io" {
        p["label_key"] =
            format!("diagnostics.executions.ioTypes.{}", event.replace('.', "_")).into();
        p["tone"] = "info".into();
        p["message"] = ["message", "prompt", "stdout", "output"]
            .iter()
            .find_map(|k| payload[*k].as_str().filter(|s| !s.is_empty()))
            .unwrap_or(event)
            .into();
        if !text(raw, "message").is_empty() {
            p["message"] = raw["message"].clone();
        }
    } else if event.starts_with("command.") {
        let phase = event.rsplit('.').next().unwrap_or("");
        p["label_key"] = format!("diagnostics.executions.eventTypes.command_{phase}").into();
        p["tone"] = if phase == "failed" {
            "danger"
        } else {
            "success"
        }
        .into();
        let failure = ["message", "error", "error_type", "code"]
            .iter()
            .find_map(|k| payload[*k].as_str().filter(|s| !s.is_empty()));
        p["message"] = if phase == "failed" { failure } else { None }
            .or_else(|| raw["command"].as_str().filter(|s| !s.is_empty()))
            .or_else(|| payload["command"].as_str().filter(|s| !s.is_empty()))
            .unwrap_or(event)
            .into();
    } else {
        p["message"] = event.into();
    }
    p
}
