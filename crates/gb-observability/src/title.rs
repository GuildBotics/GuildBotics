//! Title resolution. Callers supply the costly run lookup and localized trigger.
use serde_json::{Map, Value};

pub fn is_read_only_action(attributes: &Value) -> bool {
    matches!(
        attributes["memory.action"].as_str(),
        Some("recall" | "get" | "touch")
    ) || attributes["target.action"] == "inspected"
}

/// Records must be in timestamp order; the earliest value of each key wins.
pub fn first_seen_attributes(records: &[Value]) -> Map<String, Value> {
    let mut merged = Map::new();
    for record in records {
        if let Some(attrs) = record["attributes"].as_object() {
            for (key, value) in attrs {
                merged.entry(key.clone()).or_insert_with(|| value.clone());
            }
        }
    }
    merged
}

pub fn target_reference_label(kind: &str, label: &str) -> String {
    if label.is_empty() {
        return String::new();
    }
    format!(
        "{} {label}",
        if kind == "pull_request" {
            "PR"
        } else {
            "Ticket"
        }
    )
}

/// The summary lookup is lazy, and the trigger formatter owns localization.
pub fn resolve_trace_title(
    records: &[Value],
    attributes: &Map<String, Value>,
    command: &str,
    workflow: &str,
    fallback: &str,
    completion_summary: impl FnOnce() -> String,
    chat_trigger: impl FnOnce(&str) -> String,
) -> String {
    let attr = |key| attributes.get(key).and_then(Value::as_str).unwrap_or("");
    let target = attr("target.title");
    if !target.is_empty() {
        return target.into();
    }
    let summary = completion_summary();
    let summary = summary
        .lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("")
        .trim();
    if !summary.is_empty() {
        return summary.into();
    }
    let payload_title = records
        .iter()
        .filter(|r| !is_read_only_action(&r["attributes"]))
        .find_map(|r| r["payload"]["title"].as_str().filter(|s| !s.is_empty()))
        .unwrap_or("");
    let reference = target_reference_label(attr("target.kind"), attr("target.label"));
    for value in [attr("memory.title"), payload_title, &reference] {
        if !value.is_empty() {
            return value.into();
        }
    }
    let provider = attr("chat.provider").trim();
    if !provider.is_empty() {
        let label = chat_trigger(provider);
        if !label.is_empty() {
            return label;
        }
    }
    let field = |key: &str| {
        records
            .iter()
            .find_map(|r| {
                [&r["payload"]["fields"][key], &r["payload"][key]]
                    .into_iter()
                    .find_map(|v| v.as_str().filter(|s| !s.is_empty()))
            })
            .unwrap_or("")
    };
    for value in [
        field("prompt"),
        workflow,
        command,
        attr("memory.doc_id"),
        field("brain"),
        field("cli_agent"),
    ] {
        if !value.is_empty() {
            return value.into();
        }
    }
    records
        .iter()
        .find_map(|r| {
            ["type", "event", "message"]
                .into_iter()
                .find_map(|key| r[key].as_str().filter(|s| !s.is_empty()))
        })
        .unwrap_or(fallback)
        .into()
}
