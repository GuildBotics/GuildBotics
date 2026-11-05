//! Domain payload assembly, above the existing shared activity store.
use gb_base::shared_writes::Writer;
use gb_observability::{activity::ActivityEvents, redaction::Redactor};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}
pub fn target_record(target: &Value) -> (Value, Value) {
    let entry = json!({"label":text(target,"label"),"title":text(target,"title").trim(),"url":text(target,"url")});
    let attributes = json!({"target.provider":text(target,"provider"),"target.kind":text(target,"kind"),
        "target.label":entry["label"],"target.url":entry["url"],"target.title":entry["title"]});
    (entry, attributes)
}
pub fn target(target: &Value, action: &str, merged: Option<bool>) -> Value {
    let (mut entry, mut attributes) = target_record(target);
    let kind = text(target, "kind");
    if let Some(merged) = merged {
        entry["merged"] = merged.into();
    }
    attributes["target.action"] = action.into();
    let mut payload = json!({"action":action});
    payload[kind] = entry;
    json!({"type":format!("{kind}.item"),"payload":payload,"attributes":attributes})
}
pub fn inspected(target: &Value, read_only: bool) -> Value {
    let (entry, mut attributes) = target_record(target);
    attributes["target.action"] = if read_only { "inspected" } else { "" }.into();
    let mut payload = json!({});
    payload[text(target, "kind")] = entry;
    json!({"type":"target.recorded","payload":payload,"attributes":attributes})
}
pub fn pr_created(result: &Value) -> Option<Value> {
    (result["created"] == true).then(|| target(&result["target"], "opened", Some(false)))
}
pub fn ticket_closed(result: &Value) -> Option<Value> {
    (result["state"] == "closed" && result["state_changed"] == true)
        .then(|| target(&result["target"], "closed", None))
}
pub fn ticket_commented(result: &Value) -> Value {
    let (entry, mut attributes) = target_record(&result["target"]);
    attributes["target.action"] = "commented".into();
    json!({"type":"ticket.comment","payload":{"action":"commented","ticket":entry,"comment":{"id":result["id"],"url":text(result,"url")}},"attributes":attributes})
}
pub fn pushed(result: &Value, provider: &str) -> Option<Value> {
    if result["pushed"] != true {
        return None;
    }
    let branch = text(result, "branch");
    Some(
        json!({"type":"repository.push","payload":{"action":"push","ref":if branch.is_empty(){String::new()}else{format!("refs/heads/{branch}")},
        "commits":result.get("commits").cloned().unwrap_or(json!([]))},"attributes":{"target.action":"push","target.provider":provider}}),
    )
}
pub fn completed(run_id: &str, attempt: u64, recovered: bool, status: &str) -> Value {
    json!({"type":"workflow.completed","attributes":{"workflow.completion":"recorded"},"payload":{"run_id":run_id,"attempt":attempt,"recovered":recovered,"status":status}})
}
pub fn completion_missing(run_id: &str, attempt: u64, max_attempts: u64, error: &str) -> Value {
    json!({"type":"workflow.completion_missing","attributes":{"workflow.completion":"missing"},"payload":{"run_id":run_id,"attempt":attempt,"max_attempts":max_attempts,"error":error}})
}
pub fn rate_limited(
    run_id: &str,
    subject_id: &str,
    source_event_id: &str,
    retry_after_at: &str,
    retry_after_text: &str,
) -> Value {
    json!({"type":"workflow.rate_limited","attributes":{"error.category":"rate_limited","rate_limit.retry_after_at":retry_after_at,"rate_limit.retry_after_text":retry_after_text},
        "payload":{"category":"rate_limited","retry_after_at":retry_after_at,"retry_after_text":retry_after_text,"run_id":run_id,"subject_id":subject_id,"source_event_id":source_event_id}})
}
pub fn review_limit(
    event_type: &str,
    task: &gb_ports::Task,
    run_id: &str,
    comment_url: &str,
    error_type: &str,
    comment_error_type: &str,
) -> Value {
    json!({"type":event_type,"attributes":{"target.kind":if task.pull_request_url.is_some(){"pull_request"}else{"ticket"},"target.provider":task.provider,"target.label":task.label,"target.title":task.title,"target.url":task.pull_request_url.as_ref().or(task.url.as_ref())},"payload":{"run_id":run_id,"reason":"review_limit","action":if event_type=="pull_request.review_limit"{"draft"}else{""},"count":3,
        "pull_request":{"url":task.pull_request_url,"label":task.label,"title":task.title},"comment_url":comment_url,"error":error_type,"comment_error":comment_error_type}})
}
pub fn dispatch(
    event_id: &str,
    run_id: &str,
    attempt: u64,
    max_attempts: u64,
    next_attempt_at: Option<&str>,
    error: &str,
    category: &str,
) -> Value {
    let state = if next_attempt_at.is_some() {
        "retry_scheduled"
    } else {
        "abandoned"
    };
    let mut payload = json!({"event_id":event_id,"run_id":run_id,"attempt_count":if next_attempt_at.is_some(){attempt}else{attempt.min(max_attempts)},"max_attempts":max_attempts,"error_category":category});
    if let Some(next) = next_attempt_at {
        payload["next_attempt_at"] = next.into();
    } else {
        payload["error"] = error.into();
    }
    json!({"type":format!("chat_dispatch.{state}"),"attributes":{"chat_dispatch.state":state},"payload":payload})
}
/// Correlation is explicit and cannot be exchanged by concurrent invocations.
pub fn record(
    workspace: &Path,
    writer: &mut Writer<'_>,
    event: Value,
    correlation: &Value,
    redactor: &Redactor,
) -> anyhow::Result<Option<PathBuf>> {
    let mut result = correlation.as_object().cloned().unwrap_or_default();
    let mut attributes = result
        .remove("attributes")
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    if let Some(extra) = event["attributes"].as_object() {
        attributes.extend(extra.clone());
    }
    if let Some(event) = event.as_object() {
        result.extend(event.clone());
    }
    if result.get("source").is_none_or(|v| v == "") {
        result.insert(
            "source".into(),
            attributes
                .get("target.provider")
                .cloned()
                .unwrap_or("".into()),
        );
    }
    result.insert("attributes".into(), attributes.into());
    result.insert("kind".into(), "event".into());
    result
        .entry("timestamp")
        .or_insert_with(|| chrono::Utc::now().to_rfc3339().into());
    let shared = if gb_observability::activity::is_domain_activity_event(
        result["type"].as_str().unwrap_or(""),
    ) {
        Some(ActivityEvents::open(workspace).record(
            writer,
            &Value::Object(result.clone()),
            redactor,
        )?)
    } else {
        None
    };
    let local = redactor.local(result.into());
    gb_observability::Diagnostics::open(workspace).record(local.as_object().unwrap().clone());
    Ok(shared)
}

/// Fetch outside the shared-write span; deduplication and recording happen
/// together so simultaneous refreshes cannot import an activity twice.
pub async fn refresh(
    workspace: &Path,
    writes: &gb_base::shared_writes::SharedWrites,
    service: Option<&dyn gb_ports::TicketService>,
    provider: &str,
    start: chrono::DateTime<chrono::Utc>,
    end: chrono::DateTime<chrono::Utc>,
    redactor: &Redactor,
) -> anyhow::Result<usize> {
    let Some(service) = service else {
        return Ok(0);
    };
    let closed = service.closed_since(start, end).await;
    let cleanup = service.aclose().await;
    let closed = closed?;
    cleanup?;
    writes.transaction(|writer| {
        let store=ActivityEvents::open(workspace);
        let mut existing:std::collections::BTreeSet<_>=store.list_between(start,end)?.iter()
            .filter_map(|v|v["attributes"]["target.activity_id"].as_str().map(str::to_owned)).collect();
        let mut count=0;
        for item in closed {
            let kind=serde_json::to_value(item.kind)?.as_str().unwrap().to_owned();
            let id=format!("{kind}:{}:{}",item.url,if item.merged {"merged"}else{"closed"});
            if !existing.insert(id.clone()) {continue;}
            let mut event=target(&json!({"kind":kind,"url":item.url,"title":item.title,"label":item.label,"provider":provider}),"closed",Some(item.merged));
            event["attributes"]["target.activity_id"]=id.into();event["timestamp"]=item.closed_at.into();
            record(workspace,writer,event,&json!({}),redactor)?;count+=1;
        }
        Ok(count)
    })
}
