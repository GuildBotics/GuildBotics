//! Shared chat input and acknowledgements; all mutations hold one workspace lock.
use gb_base::{files, shared_writes::SharedWrites};
use gb_ports::{ChatEvent, ChatMessage};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
    sync::Arc,
};

#[derive(Clone, Serialize, Deserialize)]
pub struct Pending {
    #[serde(flatten)]
    pub event: ChatEvent,
    #[serde(default = "strict")]
    pub chat_participation: String,
    #[serde(default)]
    pub attempt_count: u32,
    #[serde(default = "five")]
    pub max_attempts: u32,
    #[serde(default)]
    pub next_attempt_at: Option<String>,
    #[serde(default)]
    pub run_id: String,
    #[serde(default)]
    pub last_error_category: String,
    #[serde(default)]
    pub wake_cursor: String,
}
fn strict() -> String {
    "strict".into()
}
fn five() -> u32 {
    5
}
impl Pending {
    pub fn new(event: ChatEvent, participation: &str) -> Self {
        Self {
            event,
            chat_participation: participation.into(),
            attempt_count: 0,
            max_attempts: 5,
            next_attempt_at: None,
            run_id: String::new(),
            last_error_category: String::new(),
            wake_cursor: String::new(),
        }
    }
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            !self.event.event_id.is_empty()
                && !self.event.message.message_id.is_empty()
                && !self.event.message.thread_id.is_empty(),
            "invalid pending event identity"
        );
        Ok(())
    }
}

pub struct Store {
    pub workspace: PathBuf,
    pub writes: Arc<SharedWrites>,
    pub limit: usize,
}
fn segment(text: &str) -> anyhow::Result<String> {
    let mut result = String::new();
    let mut invalid = false;
    for c in text.chars() {
        if c.is_ascii_alphanumeric() || "._-".contains(c) {
            result.push(c);
            invalid = false;
        } else if !invalid {
            result.push('_');
            invalid = true;
        }
    }
    // Dot-only inputs must never turn a record path into a parent directory.
    anyhow::ensure!(
        !result.is_empty() && result != "." && result != "..",
        "invalid chat state segment"
    );
    Ok(result)
}
fn read(path: &Path) -> anyhow::Result<Value> {
    Ok(files::read_if_present(path)?
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({})))
}
fn events(value: &Value, channel: &str) -> anyhow::Result<Vec<Pending>> {
    value["events"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|item| {
            let mut item = item.clone();
            if item.get("channel_id").is_none() {
                item["channel_id"] = channel.into();
            }
            for key in ["attempt_count", "max_attempts"] {
                let number = match &item[key] {
                    Value::Number(n) => n.as_i64().unwrap_or_default(),
                    Value::String(s) => s.parse::<i64>().unwrap_or_default(),
                    Value::Bool(b) => i64::from(*b),
                    _ => 0,
                }
                .max(0);
                item[key] = json!(if key == "max_attempts" && number == 0 {
                    5
                } else {
                    number
                });
            }
            if item["text"].is_null() {
                item["text"] = json!("");
            }
            if !item["mentions"].is_array() {
                item["mentions"] = json!([]);
            }
            if item["chat_participation"].is_null() || item["chat_participation"] == "" {
                item["chat_participation"] = json!("strict");
            }
            for key in ["run_id", "last_error_category", "wake_cursor"] {
                if item[key].is_null() {
                    item[key] = json!("");
                }
            }
            let pending: Pending = serde_json::from_value(item)?;
            pending.validate()?;
            Ok(pending)
        })
        .collect()
}
impl Store {
    /// Discard receive backlog and tracked threads; processed cursors survive.
    pub fn reset_receive(
        &self,
        service: &str,
        persons: &[String],
        cutoff: &str,
    ) -> anyhow::Result<usize> {
        if persons.is_empty() {
            return Ok(0);
        }
        self.writes.transaction(|writer| {
            let mut count = 0;
            for person in persons {
                let root = self.root(service, person)?;
                writer.write_json(&root.join("receive_cutoff.json"), &json!({"cutoff":cutoff}))?;
                let mut channels = BTreeSet::new();
                for kind in ["channels", "pending_events"] {
                    for path in json_files(&root.join(kind))? {
                        channels.insert(path.file_stem().unwrap().to_string_lossy().to_string());
                    }
                }
                if root.join("threads").is_dir() {
                    for entry in std::fs::read_dir(root.join("threads"))? {
                        let entry = entry?;
                        if entry.path().is_dir() {
                            channels.insert(entry.file_name().to_string_lossy().to_string());
                        }
                    }
                }
                for channel in channels {
                    let path = self.file(service, person, "pending_events", &channel)?;
                    if writer.delete(&path).is_err() {
                        writer.write_json(&path, &json!({"events":[]}))?;
                    }
                    for cache in [false, true] {
                        let path = self.thread_file(service, person, &channel, "_", cache)?;
                        if let Some(dir) = path.parent() {
                            for path in json_files(dir).unwrap_or_default() {
                                let _ = writer.delete(&path);
                            }
                            let _ = std::fs::remove_dir(dir);
                        }
                    }
                    count += 1;
                }
            }
            Ok(count)
        })
    }
    pub fn adopt_effort(
        &self,
        subject: &gb_wire::ChatSubject,
        person: &str,
        payload: &Value,
    ) -> anyhow::Result<()> {
        if payload["adopt_response_effort"] != true || payload["selection"]["route"] != "agent" {
            return Ok(());
        }
        let Some(effort @ ("default" | "high")) = payload["selection"]["response_effort"].as_str()
        else {
            return Ok(());
        };
        anyhow::ensure!(
            payload["person_id"] == person,
            "judgment person differs from command"
        );
        let path = self.thread_file(
            &subject.service,
            person,
            &subject.channel_id,
            &subject.thread_id,
            false,
        )?;
        self.writes.transaction(|writer| {
            let mut value = read(&path)?;
            value["effort"] = json!(if value["effort"] == "high" || effort == "high" {
                "high"
            } else {
                "default"
            });
            value["channel_id"] = json!(subject.channel_id);
            value["thread_id"] = json!(subject.thread_id);
            writer.write_json(&path, &value)?;
            Ok(())
        })
    }
    pub fn thread_file(
        &self,
        service: &str,
        person: &str,
        channel: &str,
        thread: &str,
        cache: bool,
    ) -> anyhow::Result<PathBuf> {
        let root = if cache {
            self.workspace
                .join(".guildbotics/local/chat-cache")
                .join(segment(service)?)
                .join(segment(person)?)
        } else {
            self.root(service, person)?
        };
        Ok(root
            .join("threads")
            .join(segment(channel)?)
            .join(format!("{}.json", segment(thread)?)))
    }
    pub fn thread(
        &self,
        service: &str,
        person: &str,
        channel: &str,
        thread: &str,
    ) -> anyhow::Result<Value> {
        read(&self.thread_file(service, person, channel, thread, false)?)
    }
    pub fn update_thread(
        &self,
        service: &str,
        person: &str,
        channel: &str,
        thread: &str,
        patch: &Value,
    ) -> anyhow::Result<()> {
        let path = self.thread_file(service, person, channel, thread, false)?;
        self.writes.transaction(|writer| {
            let mut value = read(&path)?;
            value.as_object_mut().unwrap().extend(
                patch
                    .as_object()
                    .ok_or_else(|| anyhow::anyhow!("thread patch must be an object"))?
                    .clone(),
            );
            value["channel_id"] = channel.into();
            value["thread_id"] = thread.into();
            stamp(&mut value);
            writer.write_json(&path, &value)
        })
    }
    pub fn messages(
        &self,
        service: &str,
        person: &str,
        channel: &str,
        thread: &str,
    ) -> anyhow::Result<Vec<ChatMessage>> {
        let value = read(&self.thread_file(service, person, channel, thread, true)?)?;
        let mut messages = value["messages"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| serde_json::from_value::<ChatMessage>(v.clone()).ok())
            .collect::<Vec<_>>();
        messages.sort_by_key(ChatMessage::position);
        Ok(messages)
    }
    pub fn cache(&self, service: &str, person: &str, message: &ChatMessage) -> anyhow::Result<()> {
        let channel = &message.channel_id;
        let thread = &message.thread_id;
        let path = self.thread_file(service, person, channel, thread, true)?;
        let shared = self.thread_file(service, person, channel, thread, false)?;
        self.writes.transaction(|writer| {
            let mut messages=self.messages(service,person,channel,thread)?;
            messages.retain(|m|m.message_id!=message.message_id);messages.push(message.clone());messages.sort_by_key(ChatMessage::position);
            let excess=messages.len().saturating_sub(self.limit.max(1));messages.drain(..excess);
            writer.write_json(&path,&json!({"schema_version":gb_base::files::SHARED_RECORD_SCHEMA_VERSION,"messages":messages}))?;
            if read(&shared)?.as_object().is_some_and(|v|v.is_empty()) {writer.write_json(&shared,&json!({"schema_version":gb_base::files::SHARED_RECORD_SCHEMA_VERSION,"channel_id":channel,"thread_id":thread}))?;}
            Ok(())
        })
    }
    pub fn threads(
        &self,
        service: &str,
        person: &str,
        channel: &str,
    ) -> anyhow::Result<Vec<(String, Value)>> {
        let root = self
            .root(service, person)?
            .join("threads")
            .join(segment(channel)?);
        json_files(&root)?
            .into_iter()
            .map(|path| {
                Ok((
                    path.file_stem().unwrap().to_string_lossy().to_string(),
                    read(&path)?,
                ))
            })
            .collect()
    }
    pub fn channels(&self, service: &str, person: &str) -> anyhow::Result<Vec<String>> {
        Ok(
            json_files(&self.root(service, person)?.join("pending_events"))?
                .iter()
                .map(|p| p.file_stem().unwrap().to_string_lossy().to_string())
                .collect(),
        )
    }
    pub fn cutoff(
        &self,
        service: &str,
        person: &str,
    ) -> anyhow::Result<Option<chrono::DateTime<chrono::Utc>>> {
        Ok(
            read(&self.root(service, person)?.join("receive_cutoff.json"))?["cutoff"]
                .as_str()
                .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                .map(|v| v.to_utc()),
        )
    }
    fn root(&self, service: &str, person: &str) -> anyhow::Result<PathBuf> {
        Ok(self
            .workspace
            .join(".guildbotics/state/chat_state")
            .join(segment(service)?)
            .join(segment(person)?))
    }
    fn file(
        &self,
        service: &str,
        person: &str,
        kind: &str,
        channel: &str,
    ) -> anyhow::Result<PathBuf> {
        Ok(self
            .root(service, person)?
            .join(kind)
            .join(format!("{}.json", segment(channel)?)))
    }
    pub fn pending(
        &self,
        service: &str,
        person: &str,
        channel: &str,
    ) -> anyhow::Result<Vec<Pending>> {
        let mut pending = events(
            &read(&self.file(service, person, "pending_events", channel)?)?,
            channel,
        )?;
        pending.sort_by_key(|p| p.event.message.position());
        Ok(pending)
    }
    pub fn cursor(&self, service: &str, person: &str, channel: &str) -> anyhow::Result<Value> {
        read(&self.file(service, person, "channels", channel)?)
    }

    /// Redelivery updates content and policy, retaining the existing retry budget.
    pub fn receive(
        &self,
        service: &str,
        person: &str,
        event: ChatEvent,
        participation: &str,
    ) -> anyhow::Result<()> {
        let channel = event.message.channel_id.clone();
        let path = self.file(service, person, "pending_events", &channel)?;
        self.writes.transaction(|writer| {
            let mut value = read(&path)?;
            let mut queued = events(&value, &channel)?;
            if let Some(pending) = queued
                .iter_mut()
                .find(|p| p.event.event_id == event.event_id)
            {
                pending.event = event;
                pending.chat_participation = participation.into();
            } else {
                queued.push(Pending::new(event, participation));
            }
            for pending in &queued {
                pending.validate()?;
            }
            queued.sort_by_key(|p| p.event.message.position());
            let excess = queued.len().saturating_sub(self.limit.max(1));
            queued.drain(..excess);
            value["events"] = serde_json::to_value(queued)?;
            stamp(&mut value);
            writer.write_json(&path, &value)
        })
    }
    pub fn save(&self, service: &str, person: &str, pending: &Pending) -> anyhow::Result<()> {
        pending.validate()?;
        let channel = &pending.event.message.channel_id;
        let path = self.file(service, person, "pending_events", channel)?;
        self.writes.transaction(|writer| {
            let mut value = read(&path)?;
            let mut queued = events(&value, channel)?;
            queued.retain(|p| p.event.event_id != pending.event.event_id);
            queued.push(pending.clone());
            queued.sort_by_key(|p| p.event.message.position());
            let excess = queued.len().saturating_sub(self.limit.max(1));
            queued.drain(..excess);
            value["events"] = serde_json::to_value(queued)?;
            stamp(&mut value);
            writer.write_json(&path, &value)
        })
    }
    /// The cursor and removal share the lock. Removal covers the whole batch,
    /// including IDs dropped from the bounded processed history.
    pub fn acknowledge(
        &self,
        service: &str,
        person: &str,
        channel: &str,
        ids: &[String],
    ) -> anyhow::Result<()> {
        let cursor = self.file(service, person, "channels", channel)?;
        let queue = self.file(service, person, "pending_events", channel)?;
        self.writes.transaction(|writer| {
            let mut value = read(&cursor)?;
            let mut seen = BTreeSet::new();
            let mut history = value["processed_event_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .chain(ids.iter().map(String::as_str))
                .filter(|id| !id.is_empty() && seen.insert((*id).to_owned()))
                .map(str::to_owned)
                .collect::<Vec<_>>();
            let excess = history.len().saturating_sub(self.limit.max(1));
            history.drain(..excess);
            value["processed_event_ids"] = json!(history);
            stamp(&mut value);
            writer.write_json(&cursor, &value)?;
            let mut queued = read(&queue)?;
            let mut remaining = events(&queued, channel)?;
            remaining.retain(|p| !ids.contains(&p.event.event_id));
            if remaining.is_empty() {
                writer.delete(&queue)?;
            } else {
                queued["events"] = serde_json::to_value(remaining)?;
                stamp(&mut queued);
                writer.write_json(&queue, &queued)?;
            }
            Ok(())
        })
    }
    pub fn watermark(
        &self,
        service: &str,
        person: &str,
        channel: &str,
        at: &str,
    ) -> anyhow::Result<()> {
        let path = self.file(service, person, "channels", channel)?;
        self.writes.transaction(|writer| {
            let mut value = read(&path)?;
            value["watermark"] = at.into();
            stamp(&mut value);
            writer.write_json(&path, &value)
        })
    }
}
fn json_files(root: &Path) -> anyhow::Result<Vec<PathBuf>> {
    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e.into()),
    };
    let mut paths = Vec::new();
    for entry in entries {
        let entry = entry?;
        if entry.file_type()?.is_file() && entry.path().extension().is_some_and(|e| e == "json") {
            paths.push(entry.path());
        }
    }
    paths.sort();
    Ok(paths)
}
fn stamp(value: &mut Value) {
    value["schema_version"] = gb_base::files::SHARED_RECORD_SCHEMA_VERSION.into();
}

#[cfg(test)]
mod tests {
    use super::*;
    fn store(root: &Path, limit: usize) -> Store {
        Store {
            workspace: root.into(),
            writes: Arc::new(SharedWrites::new(root.join("identity"), Some(root.into()))),
            limit,
        }
    }
    fn event(id: &str, second: u32) -> ChatEvent {
        serde_json::from_value(json!({"channel_id":"C1","thread_id":"T1","message_id":id,"event_id":id,"occurred_at":format!("2026-11-05T00:00:{second:02}Z"),"author_id":"owner","text":id})).unwrap()
    }
    #[test]
    fn redelivery_retains_attempts_and_unrelated_queue_entries_and_cursor_updates_do_not_lose_acknowledgements()
     {
        let root = tempfile::tempdir().unwrap();
        let s = store(root.path(), 500);
        s.receive("local", "aiko", event("E1", 1), "strict")
            .unwrap();
        let mut pending = s.pending("local", "aiko", "C1").unwrap().remove(0);
        pending.attempt_count = 3;
        pending.run_id = "run".into();
        pending.last_error_category = "rate_limited".into();
        pending.next_attempt_at = Some("later".into());
        s.save("local", "aiko", &pending).unwrap();
        s.receive("local", "aiko", event("E2", 2), "social")
            .unwrap();
        let mut updated = event("E1", 1);
        updated.message.text = "edited".into();
        s.receive("local", "aiko", updated, "social").unwrap();
        let queued = s.pending("local", "aiko", "C1").unwrap();
        assert_eq!(queued.len(), 2);
        assert_eq!(queued[0].attempt_count, 3);
        assert_eq!(queued[0].next_attempt_at.as_deref(), Some("later"));
        assert_eq!(queued[0].run_id, "run");
        assert_eq!(queued[0].chat_participation, "social");
        assert_eq!(queued[0].event.message.text, "edited");
        s.acknowledge("local", "aiko", "C1", &["E1".into()])
            .unwrap();
        s.watermark("local", "aiko", "C1", "2026-11-05T00:01:00Z")
            .unwrap();
        assert_eq!(
            s.cursor("local", "aiko", "C1").unwrap()["processed_event_ids"],
            json!(["E1"])
        );
        assert_eq!(
            s.pending("local", "aiko", "C1").unwrap()[0].event.event_id,
            "E2"
        );
    }
    #[test]
    fn acknowledgement_larger_than_bounded_history_removes_whole_batch_and_keeps_future_input() {
        let root = tempfile::tempdir().unwrap();
        let s = store(root.path(), 2);
        for (id, second) in [("E1", 1), ("E2", 2), ("E3", 3)] {
            s.receive("local", "aiko", event(id, second), "strict")
                .unwrap();
        }
        s.acknowledge(
            "local",
            "aiko",
            "C1",
            &["E1".into(), "E2".into(), "E3".into()],
        )
        .unwrap();
        assert!(s.pending("local", "aiko", "C1").unwrap().is_empty());
        assert!(
            !s.file("local", "aiko", "pending_events", "C1")
                .unwrap()
                .exists()
        );
        assert_eq!(
            s.cursor("local", "aiko", "C1").unwrap()["processed_event_ids"],
            json!(["E2", "E3"])
        );
        s.receive("local", "aiko", event("E4", 4), "strict")
            .unwrap();
        assert_eq!(s.pending("local", "aiko", "C1").unwrap().len(), 1);
    }
    #[test]
    fn malformed_pending_identity_fails_without_rewriting_or_removing_the_queue() {
        let root = tempfile::tempdir().unwrap();
        let s = store(root.path(), 500);
        let path = s.file("local", "aiko", "pending_events", "C1").unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let bytes = br#"{"events":[{"event_id":"E1"}]}"#;
        std::fs::write(&path, bytes).unwrap();
        assert!(s.pending("local", "aiko", "C1").is_err());
        assert!(
            s.receive("local", "aiko", event("E2", 2), "strict")
                .is_err()
        );
        assert_eq!(std::fs::read(path).unwrap(), bytes);
        for invalid in ["", ".", ".."] {
            assert!(s.root(invalid, "aiko").is_err());
        }
    }

    #[test]
    fn pending_record_defaults_keep_the_frozen_string_budget_and_null_text_rules() {
        let mut raw = serde_json::to_value(event("E1", 1)).unwrap();
        raw.as_object_mut().unwrap().remove("channel_id");
        raw["attempt_count"] = json!("3");
        raw["max_attempts"] = json!(-2);
        raw["text"] = Value::Null;
        raw["mentions"] = json!("not an array");
        raw["chat_participation"] = json!("");
        let decoded = events(&json!({"events":[raw]}), "C1").unwrap().remove(0);
        assert_eq!(decoded.event.message.channel_id, "C1");
        assert_eq!(decoded.attempt_count, 3);
        assert_eq!(decoded.max_attempts, 5);
        assert!(decoded.event.message.text.is_empty());
        assert!(decoded.event.message.mentions.is_empty());
        assert_eq!(decoded.chat_participation, "strict");
    }

    #[test]
    fn thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes() {
        let root = tempfile::tempdir().unwrap();
        let s = store(root.path(), 2);
        assert_eq!(s.thread("local", "aiko", "C1", "T1").unwrap(), json!({}));
        assert_eq!(s.cursor("local", "aiko", "C1").unwrap(), json!({}));
        assert!(s.messages("local", "aiko", "C1", "T1").unwrap().is_empty());
        let state = json!({"participants":["aiko","yuki"],"thread_topic":"weekly news","latest_focus":"business angle","effort":"high",
            "backfill_disabled_reason":"thread_not_found","backfill_error_count":2,"last_backfill_error":"thread_not_found",
            "handoffs":[{"person_id":"yuki","roles":["design"],"message_id":"M2","text":"@yuki wording?","thread_topic":"weekly news","latest_focus":"business angle"}],
            "system_notices":[{"kind":"workflow_error","reason":"rate_limited","person_id":"aiko","source_event_id":"E1","message_id":"M3","run_id":"run","retry_after_at":"2026-07-04T11:44:00+09:00","retry_after_text":"11:44 AM","recorded_at":"2026-07-04T02:44:00Z"}]});
        s.update_thread("local", "aiko", "C1", "T1", &state)
            .unwrap();
        let loaded = s.thread("local", "aiko", "C1", "T1").unwrap();
        for (key, value) in state.as_object().unwrap() {
            assert_eq!(&loaded[key], value);
        }
        assert_eq!(loaded["channel_id"], "C1");
        assert_eq!(loaded["thread_id"], "T1");
        for (service, person, channel) in [
            ("other", "aiko", "C1"),
            ("local", "yuki", "C1"),
            ("local", "aiko", "C2"),
        ] {
            assert_eq!(s.thread(service, person, channel, "T1").unwrap(), json!({}));
            assert_eq!(s.cursor(service, person, channel).unwrap(), json!({}));
        }
        s.acknowledge(
            "local",
            "aiko",
            "C1",
            &["E1".into(), "E2".into(), "E1".into()],
        )
        .unwrap();
        s.watermark("local", "aiko", "C1", "2026-11-05T00:00:01Z")
            .unwrap();
        assert_eq!(
            s.cursor("local", "aiko", "C1").unwrap()["processed_event_ids"],
            json!(["E1", "E2"])
        );
        assert_eq!(
            s.cursor("local", "aiko", "C1").unwrap()["watermark"],
            "2026-11-05T00:00:01Z"
        );
        let mut message = event("M1", 1).message;
        message.text = "BODY_CANARY".into();
        s.cache("local", "aiko", &message).unwrap();
        let shared = s.thread_file("local", "aiko", "C1", "T1", false).unwrap();
        assert!(
            !std::fs::read_to_string(&shared)
                .unwrap()
                .contains("BODY_CANARY")
        );
        assert_eq!(
            s.messages("local", "aiko", "C1", "T1").unwrap()[0].text,
            "BODY_CANARY"
        );
        let cache = s.thread_file("local", "aiko", "C1", "T1", true).unwrap();
        assert!(cache.starts_with(root.path().join(".guildbotics/local/chat-cache")));
        let mut second = event("M2", 2).message;
        s.cache("local", "aiko", &second).unwrap();
        second.text = "edited".into();
        s.cache("local", "aiko", &second).unwrap();
        let mut third = event("M3", 3).message;
        third.is_bot_message = true;
        s.cache("local", "aiko", &third).unwrap();
        let messages = s.messages("local", "aiko", "C1", "T1").unwrap();
        assert_eq!(
            messages
                .iter()
                .map(|m| m.message_id.as_str())
                .collect::<Vec<_>>(),
            ["M2", "M3"]
        );
        assert_eq!(messages[0].text, "edited");
        assert!(messages[1].is_bot_message);
        assert_eq!(s.thread("local", "aiko", "C1", "T1").unwrap(), loaded);
        s.update_thread("local", "aiko", "C1", "T2", &json!({}))
            .unwrap();
        s.update_thread("local", "aiko", "C2", "T3", &json!({}))
            .unwrap();
        assert_eq!(
            s.threads("local", "aiko", "C1")
                .unwrap()
                .iter()
                .map(|(id, _)| id.as_str())
                .collect::<Vec<_>>(),
            ["T1", "T2"]
        );
        s.writes
            .transaction(|writer| {
                writer.write_json(
                    &s.root("local", "aiko")?.join("receive_cutoff.json"),
                    &json!({"cutoff":"2026-11-05T00:00:00Z"}),
                )
            })
            .unwrap();
        assert_eq!(
            s.cutoff("local", "aiko").unwrap(),
            Some("2026-11-05T00:00:00Z".parse().unwrap())
        );
        assert!(s.cutoff("local", "yuki").unwrap().is_none());
    }

    #[test]
    fn malformed_pending_status_and_each_missing_identity_are_errors_and_preserve_the_file() {
        let root = tempfile::tempdir().unwrap();
        let s = store(root.path(), 500);
        let path = s.file("local", "aiko", "pending_events", "C1").unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        for key in [
            "event_id",
            "message_id",
            "thread_id",
            "occurred_at",
            "invalid_status",
        ] {
            let mut raw = serde_json::to_value(event("E1", 1)).unwrap();
            if key == "invalid_status" {
                raw["status"] = json!({"reason":"unknown"});
            } else {
                raw.as_object_mut().unwrap().remove(key);
            }
            let original = serde_json::to_vec(&json!({"events":[raw]})).unwrap();
            std::fs::write(&path, &original).unwrap();
            assert!(s.pending("local", "aiko", "C1").is_err(), "{key}");
            assert!(
                s.receive("local", "aiko", event("E2", 2), "strict")
                    .is_err(),
                "{key}"
            );
            assert_eq!(std::fs::read(&path).unwrap(), original);
        }
    }
    #[test]
    fn pending_status_retry_metadata_and_channel_enumeration_preserve_member_scope() {
        let root = tempfile::tempdir().unwrap();
        let s = store(root.path(), 500);
        assert!(s.channels("local", "aiko").unwrap().is_empty());
        for status in [None,Some(serde_json::from_value(json!({"reason":"rate_limited","person_id":"aiko","run_id":"run","subject":"ticket","retry_after_at":"2026-11-05T00:05:00Z","retry_after_text":"later"})).unwrap())] {
            let mut received=event("E1",1);
            received.status=status.clone();
            s.receive("local","aiko",received,"social").unwrap();
            let mut pending=s.pending("local","aiko","C1").unwrap().remove(0);
            assert_eq!(pending.event.status,status);
            pending.attempt_count=2;pending.max_attempts=7;pending.run_id="run".into();pending.next_attempt_at=Some("2026-11-05T00:05:00Z".into());pending.last_error_category="rate_limited".into();pending.wake_cursor="cursor".into();
            s.save("local","aiko",&pending).unwrap();
            assert_eq!(serde_json::to_value(s.pending("local","aiko","C1").unwrap().remove(0)).unwrap(),serde_json::to_value(pending).unwrap());
        }
        for (person, channel) in [("aiko", "C2"), ("yuki", "C9")] {
            let mut received = event("E2", 2);
            received.message.channel_id = channel.into();
            s.receive("local", person, received, "strict").unwrap();
        }
        assert_eq!(s.channels("local", "aiko").unwrap(), ["C1", "C2"]);
        assert_eq!(s.channels("local", "yuki").unwrap(), ["C9"]);
        assert!(s.channels("other", "aiko").unwrap().is_empty());
    }
}
