use chrono::{DateTime, FixedOffset, Utc};
use gb_ports::{ChatEvent, ChatMessage, ServiceError, WorkflowStatus};
use regex::Regex;
use serde_json::{Value, json};
use std::{collections::BTreeSet, sync::LazyLock};

pub(super) static MENTION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<@([A-Z0-9]+)(?:\|[^>]+)?>").unwrap());
pub(super) fn mentions(text: &str) -> Vec<String> {
    let mut seen = BTreeSet::new();
    MENTION
        .captures_iter(text)
        .filter_map(|c| {
            let id = c[1].to_owned();
            seen.insert(id.clone()).then_some(id)
        })
        .collect()
}
pub(super) fn timestamp(ts: &str) -> Result<DateTime<FixedOffset>, ServiceError> {
    let (seconds, fraction) = ts.split_once('.').unwrap_or((ts, ""));
    let seconds = seconds
        .parse::<i64>()
        .map_err(|_| ServiceError::InvalidResult)?;
    let fraction: String = fraction.chars().take(6).collect();
    if !fraction.bytes().all(|b| b.is_ascii_digit()) {
        return Err(ServiceError::InvalidResult);
    }
    let micros = format!("{fraction:0<6}")
        .parse::<u32>()
        .map_err(|_| ServiceError::InvalidResult)?;
    DateTime::from_timestamp(seconds, micros * 1000)
        .map(|t| t.fixed_offset())
        .ok_or(ServiceError::InvalidResult)
}
pub(super) fn time_ts(t: DateTime<Utc>) -> String {
    format!("{}.{:06}", t.timestamp(), t.timestamp_subsec_micros())
}
pub(super) fn metadata(status: &WorkflowStatus) -> Value {
    json!({"event_type":"guildbotics.workflow_status", "event_payload": status.payload()})
}
pub(super) fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}
pub(super) fn event(channel: &str, raw: &Value) -> Result<Option<ChatEvent>, ServiceError> {
    let subtype = text(raw, "subtype");
    if ![
        "",
        "bot_message",
        "file_share",
        "me_message",
        "thread_broadcast",
    ]
    .contains(&subtype)
    {
        return Ok(None);
    }
    let ts = text(raw, "ts");
    let occurred_at = timestamp(ts)?;
    let thread = match text(raw, "thread_ts") {
        "" => ts,
        v => v,
    };
    let body = text(raw, "text");
    let status = raw
        .get("metadata")
        .filter(|m| text(m, "event_type") == "guildbotics.workflow_status")
        .and_then(|m| WorkflowStatus::from_payload(&m["event_payload"]));
    Ok(Some(ChatEvent {
        message: ChatMessage {
            channel_id: channel.into(),
            message_id: ts.into(),
            thread_id: thread.into(),
            occurred_at,
            author_id: match text(raw, "user") {
                "" => None,
                v => Some(v.into()),
            },
            text: body.into(),
            mentions: mentions(body),
            is_bot_message: !text(raw, "bot_id").is_empty() || subtype == "bot_message",
        },
        event_id: format!("{channel}:{ts}"),
        is_thread_reply: thread != ts,
        status,
    }))
}
