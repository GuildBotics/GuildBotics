//! Queue-only refresh and source-thread publication checks. Reading updates
//! neither consumes the queue nor advances the processed-event cursor.
use crate::{MemberError, Result, WriteGuard};
use gb_base::{files, shared_writes::SharedWrites};
use gb_observability::{
    redaction::Redactor,
    runs::{RunRecord, RunStore},
};
use gb_ports::ChatEvent;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::Mutex,
};
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Subject {
    pub service: String,
    pub channel_id: String,
    pub thread_id: String,
    pub event_id: String,
    pub self_user_id: String,
}
impl Subject {
    pub fn id(&self) -> String {
        format!(
            "{}:{}:{}:{}",
            self.service, self.channel_id, self.thread_id, self.event_id
        )
    }
}
fn failed(_: anyhow::Error) -> MemberError {
    MemberError::Capability("capabilities.task_runs.store_failed")
}
fn segment(value: &str) -> String {
    let mut result = String::new();
    let mut invalid = false;
    for c in value.chars() {
        if c.is_ascii_alphanumeric() || "._-".contains(c) {
            result.push(c);
            invalid = false;
        } else if !invalid {
            result.push('_');
            invalid = true;
        }
    }
    result
}
fn read(path: &Path) -> Value {
    std::fs::read(path)
        .ok()
        .and_then(|v| serde_json::from_slice::<Value>(&v).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}
fn ids(value: &Value) -> impl Iterator<Item = &str> {
    value["event_ids"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
}
pub fn batch_event_ids(evidence: &[Value]) -> Vec<String> {
    let mut result = vec![];
    for record in evidence {
        if record["type"] == "chat_batch" {
            result.clear();
        }
        if matches!(record["type"].as_str(), Some("chat_batch" | "chat_updates")) {
            for id in ids(&record["payload"]) {
                if !result.iter().any(|v| v == id) {
                    result.push(id.into());
                }
            }
        }
    }
    result
}
pub fn completed_event_ids(evidence: &[Value], status: &str) -> Vec<String> {
    let (mut batch, mut updates, mut handled) = (vec![], vec![], vec![]);
    let add = |to: &mut Vec<String>, ids: Vec<String>| {
        for id in ids {
            if !to.contains(&id) {
                to.push(id);
            }
        }
    };
    for record in evidence {
        match record["type"].as_str().unwrap_or("") {
            "chat_batch" => {
                batch = ids(&record["payload"]).map(str::to_owned).collect();
                updates.clear();
                handled.clear();
            }
            "chat_updates" => add(
                &mut updates,
                ids(&record["payload"]).map(str::to_owned).collect(),
            ),
            "chat_post" | "chat_reply" | "chat_reaction" | "issue_comment" | "pr_comment"
            | "pr_review" | "pr_review_comment" | "pr_reply" | "reaction_add" | "pr_create"
            | "pr_update" | "git_publish" | "git_push" | "issue_create" | "issue_update" => {
                add(&mut handled, updates.clone())
            }
            _ => {}
        }
    }
    if matches!(status, "done" | "asking" | "awaiting_checks") {
        add(&mut batch, handled);
    }
    batch
}
pub struct Heartbeat {
    root: PathBuf,
    saved: Mutex<BTreeMap<PathBuf, (String, f64)>>,
}
impl Heartbeat {
    pub fn new(workspace: &Path) -> Self {
        Self {
            root: workspace.join(".guildbotics/local/chat-receivers"),
            saved: Mutex::new(BTreeMap::new()),
        }
    }
    pub fn path(&self, service: &str, person: &str, channel: &str) -> PathBuf {
        let hash = Sha256::digest(crate::identity::bytes((service, person, channel)))
            .iter()
            .map(|v| format!("{v:02x}"))
            .collect::<String>();
        self.root.join(format!("{hash}.json"))
    }
    pub fn state(&self, subject: &Subject, person: &str, now: f64) -> String {
        let data = read(&self.path(&subject.service, person, &subject.channel_id));
        let age = now - data["checked_at"].as_f64().unwrap_or(f64::NEG_INFINITY);
        let state = data["state"].as_str().unwrap_or("");
        if (0.0..=15.0).contains(&age) && matches!(state, "ready" | "catching_up" | "unavailable") {
            state.into()
        } else {
            "unavailable".into()
        }
    }
    pub fn save(
        &self,
        subject: &Subject,
        person: &str,
        state: &str,
        wall: f64,
        monotonic: f64,
    ) -> Result<()> {
        if !matches!(state, "ready" | "catching_up" | "unavailable") {
            return Err(MemberError::Capability(
                "cli.member.chat_updates.unavailable",
            ));
        }
        let path = self.path(&subject.service, person, &subject.channel_id);
        let mut saved = self
            .saved
            .lock()
            .map_err(|_| MemberError::Capability("cli.member.chat_updates.unavailable"))?;
        if saved
            .get(&path)
            .is_some_and(|(previous, time)| previous == state && monotonic - *time < 5.0)
        {
            return Ok(());
        }
        files::atomic_write(
            &path,
            serde_json::to_string(&json!({"state":state,"checked_at":wall}))
                .expect("heartbeat JSON")
                .as_bytes(),
        )
        .map_err(|_| MemberError::Capability("cli.member.chat_updates.unavailable"))?;
        saved.insert(path, (state.into(), monotonic));
        Ok(())
    }
}
pub struct Gate<'a> {
    pub workspace: &'a Path,
    pub writes: &'a SharedWrites,
    pub person: &'a str,
    pub run_id: &'a str,
    pub subject: Option<&'a Subject>,
    pub redactor: &'a Redactor,
    pub now: &'a (dyn Fn() -> f64 + Send + Sync),
    pub render: &'a (dyn Fn(&str) -> String + Send + Sync),
}
impl Gate<'_> {
    fn snapshot(&self) -> Result<(RunRecord, Vec<Value>)> {
        if self.subject.is_none() || self.run_id.is_empty() {
            return Err(MemberError::Capability(
                "cli.member.chat_updates.invalid_run",
            ));
        }
        let record = RunStore::open(self.workspace)
            .read(self.run_id)
            .map_err(failed)?
            .ok_or(MemberError::Capability(
                "cli.member.chat_updates.invalid_run",
            ))?;
        if record.member_id != self.person {
            return Err(MemberError::Capability(
                "cli.member.chat_updates.invalid_run",
            ));
        }
        let index = record
            .provider_evidence
            .iter()
            .rposition(|v| v["type"] == "chat_batch")
            .ok_or(MemberError::Capability(
                "cli.member.chat_updates.invalid_run",
            ))?;
        let evidence = record.provider_evidence[index..].to_vec();
        Ok((record, evidence))
    }
    fn pending(&self, subject: &Subject, evidence: &[Value]) -> Result<Vec<ChatEvent>> {
        let root = self
            .workspace
            .join(".guildbotics/state/chat_state")
            .join(segment(&subject.service))
            .join(segment(self.person));
        let cursor = read(
            &root
                .join("channels")
                .join(format!("{}.json", segment(&subject.channel_id))),
        );
        let processed = cursor["processed_event_ids"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<BTreeSet<_>>();
        let delivered = batch_event_ids(evidence)
            .into_iter()
            .collect::<BTreeSet<_>>();
        let queue = read(
            &root
                .join("pending_events")
                .join(format!("{}.json", segment(&subject.channel_id))),
        );
        let mut result = vec![];
        for item in queue["events"].as_array().into_iter().flatten() {
            let mut item = item.clone();
            if item.get("channel_id").is_none() {
                item["channel_id"] = subject.channel_id.clone().into();
            }
            if item.get("text").is_none_or(Value::is_null) {
                item["text"] = "".into();
            }
            let event: ChatEvent = serde_json::from_value(item)
                .map_err(|_| MemberError::Capability("cli.member.chat_updates.invalid_queue"))?;
            if event.message.thread_id == subject.thread_id
                && !delivered.contains(&event.event_id)
                && event.message.author_id.as_deref() != Some(&subject.self_user_id)
                && event.status.is_none()
                && !processed.contains(event.event_id.as_str())
            {
                result.push(event);
            }
        }
        result.sort_by_key(|v| v.message.position());
        Ok(result)
    }
    pub fn check(&self) -> Result<Value> {
        let (record, evidence) = self.snapshot()?;
        let subject = self.subject.expect("checked source");
        let state = Heartbeat::new(self.workspace).state(subject, self.person, (self.now)());
        let mut result = json!({"run_id":self.run_id,"service":subject.service,"channel_id":subject.channel_id,"thread_id":subject.thread_id});
        if state != "ready" {
            result["status"] = state.clone().into();
            result["messages"] = json!([]);
            result["reason"] = (self.render)(if state == "catching_up" {
                "cli.member.chat_updates.catching_up"
            } else {
                "cli.member.chat_updates.unavailable"
            })
            .into();
            return Ok(result);
        }
        let events = self.pending(subject, &evidence)?;
        let delivered = events
            .iter()
            .map(|v| v.event_id.clone())
            .collect::<Vec<_>>();
        self.writes.transaction(|writer|{RunStore::open(self.workspace).append(writer,record,&json!({"kind":"evidence","evidence_type":"chat_updates","payload":{"event_ids":delivered}}),&chrono::Utc::now().to_rfc3339(),self.redactor)?;Ok(())}).map_err(failed)?;
        result["status"] = if events.is_empty() {
            "up_to_date"
        } else {
            "new_messages"
        }
        .into();
        result["messages"] = events
            .iter()
            .map(ChatEvent::payload)
            .collect::<Vec<_>>()
            .into();
        Ok(result)
    }
    pub fn noop(&self, reason: &str) -> Result<Value> {
        let subject = self.subject.ok_or(MemberError::Capability(
            "cli.member.chat_updates.invalid_run",
        ))?;
        Ok(
            json!({"service":subject.service,"channel_id":subject.channel_id,"thread_id":subject.thread_id,"event_id":subject.event_id,"reason":reason,"noop":true}),
        )
    }
}
impl WriteGuard for Gate<'_> {
    fn ensure_current(&self) -> Result<()> {
        let Some(subject) = self.subject else {
            return Ok(());
        };
        let (_, evidence) = self.snapshot()?;
        let state = Heartbeat::new(self.workspace).state(subject, self.person, (self.now)());
        let reason = if state == "catching_up" {
            Some("cli.member.chat_updates.catching_up")
        } else if state != "ready" {
            Some("cli.member.chat_updates.unavailable")
        } else if !evidence.iter().any(|v| v["type"] == "chat_updates") {
            Some("cli.member.chat_updates.not_checked")
        } else if !self.pending(subject, &evidence)?.is_empty() {
            Some("cli.member.chat_updates.new_messages")
        } else {
            None
        };
        match reason {
            None => Ok(()),
            Some(reason) => Err(MemberError::CapabilityArgs {
                key: "cli.member.chat_updates.required",
                args: [
                    ("person".into(), self.person.into()),
                    ("reason".into(), (self.render)(reason)),
                ]
                .into(),
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn now() -> f64 {
        1000.0
    }
    fn render(key: &str) -> String {
        key.into()
    }
    struct Fixture {
        _temp: tempfile::TempDir,
        workspace: PathBuf,
        writes: SharedWrites,
        masks: Redactor,
        subject: Subject,
    }
    impl Fixture {
        fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            let workspace = temp.path().join("workspace");
            let writes = SharedWrites::new(temp.path().join("identity"), Some(workspace.clone()));
            let masks = Redactor::new([]);
            let record:RunRecord=serde_json::from_value(json!({"run_id":"run","member_id":"aiko","work_kind":"chat","work_identity":{"channel_id":"wrong-journal-channel","thread_id":"wrong-journal-thread"},"device_id":"device","execution_mode":"autonomous","started_at":"2026-11-05T00:00:00Z"})).unwrap();
            writes.transaction(|writer|{let store=RunStore::open(&workspace);let record=store.start(writer,record,&masks)?;store.append(writer,record,&json!({"kind":"evidence","evidence_type":"chat_batch","payload":{"event_ids":["E1"]}}),"now",&masks)?;Ok(())}).unwrap();
            let subject = Subject {
                service: "slack".into(),
                channel_id: "C1".into(),
                thread_id: "100.1".into(),
                event_id: "E1".into(),
                self_user_id: "BOT".into(),
            };
            Heartbeat::new(&workspace)
                .save(&subject, "aiko", "ready", 1000.0, 0.0)
                .unwrap();
            Self {
                _temp: temp,
                workspace,
                writes,
                masks,
                subject,
            }
        }
        fn gate(&self) -> Gate<'_> {
            Gate {
                workspace: &self.workspace,
                writes: &self.writes,
                person: "aiko",
                run_id: "run",
                subject: Some(&self.subject),
                redactor: &self.masks,
                now: &now,
                render: &render,
            }
        }
        fn queue_path(&self) -> PathBuf {
            self.workspace
                .join(".guildbotics/state/chat_state/slack/aiko/pending_events/C1.json")
        }
        fn queue(&self, events: Vec<Value>) {
            self.writes
                .transaction(|writer| {
                    writer.write_json(&self.queue_path(), &json!({"events":events}))
                })
                .unwrap();
        }
        fn evidence(&self) -> Vec<Value> {
            RunStore::open(&self.workspace)
                .read("run")
                .unwrap()
                .unwrap()
                .provider_evidence
        }
    }
    fn event(n: usize) -> Value {
        json!({"event_id":format!("E{n}"),"channel_id":"C1","thread_id":"100.1","message_id":format!("{n}.1"),"occurred_at":format!("2026-11-05T00:00:{n:02}Z"),"author_id":"OTHER","text":format!("request {n}")})
    }
    #[test]
    fn checking_delivers_sorted_input_without_consuming_the_queue_or_cursor() {
        let f = Fixture::new();
        f.queue(vec![event(5), event(3)]);
        let before = std::fs::read(f.queue_path()).unwrap();
        assert!(f.gate().ensure_current().is_err());
        let result = f.gate().check().unwrap();
        assert_eq!(result["status"], "new_messages");
        assert_eq!(
            result["messages"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v["event_id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["E3", "E5"]
        );
        assert_eq!(std::fs::read(f.queue_path()).unwrap(), before);
        assert!(f.gate().ensure_current().is_ok());
        assert_eq!(f.gate().check().unwrap()["status"], "up_to_date");
        assert_eq!(batch_event_ids(&f.evidence()), ["E1", "E3", "E5"]);
        f.queue(vec![event(3), event(5), event(6)]);
        assert!(f.gate().ensure_current().is_err());
    }
    #[test]
    fn only_source_thread_external_unprocessed_nonstatus_events_are_delivered() {
        let f = Fixture::new();
        let mut own = event(2);
        own["author_id"] = "BOT".into();
        let mut other = event(3);
        other["thread_id"] = "elsewhere".into();
        let mut status = event(4);
        status["status"] =
            json!({"reason":"failed","person_id":"another","run_id":"other","subject":"C1:100.1"});
        f.queue(vec![event(1), own, other, status, event(5), event(6)]);
        f.writes
            .transaction(|writer| {
                writer.write_json(
                    &f.workspace
                        .join(".guildbotics/state/chat_state/slack/aiko/channels/C1.json"),
                    &json!({"processed_event_ids":["E5"]}),
                )
            })
            .unwrap();
        let result = f.gate().check().unwrap();
        assert_eq!(result["channel_id"], "C1");
        assert_eq!(result["thread_id"], "100.1");
        assert_eq!(result["messages"].as_array().unwrap().len(), 1);
        assert_eq!(result["messages"][0]["event_id"], "E6");
        f.queue(vec![json!({"event_id":"malformed"})]);
        assert!(f.gate().check().is_err());
    }
    #[test]
    fn reception_delays_and_stale_or_future_heartbeats_are_never_no_new_messages() {
        let f = Fixture::new();
        let heartbeat = Heartbeat::new(&f.workspace);
        for (state, checked_at) in [
            ("catching_up", 1000.0),
            ("unavailable", 1000.0),
            ("ready", 984.0),
            ("ready", 1001.0),
        ] {
            heartbeat
                .save(&f.subject, "aiko", state, checked_at, 100.0 + checked_at)
                .unwrap();
            let result = f.gate().check().unwrap();
            assert!(result["status"] == "catching_up" || result["status"] == "unavailable");
            assert_eq!(result["messages"], json!([]));
            assert!(f.gate().ensure_current().is_err());
        }
        assert_eq!(f.evidence().len(), 1);
        let mut gate = f.gate();
        gate.subject = None;
        assert!(gate.ensure_current().is_ok());
        assert!(gate.check().is_err());
    }
    #[test]
    fn new_batches_reset_membership_and_updates_need_a_later_external_action_to_be_acknowledged() {
        let record = |kind: &str, ids: Vec<&str>| json!({"type":kind,"payload":{"event_ids":ids}});
        let mut evidence = vec![
            record("chat_batch", vec!["E1"]),
            record("chat_updates", vec!["E2"]),
            record("git_commit", vec![]),
            record("chat_noop", vec![]),
        ];
        assert_eq!(completed_event_ids(&evidence, "done"), ["E1"]);
        evidence.push(record("chat_reply", vec![]));
        evidence.push(record("chat_updates", vec!["E3"]));
        assert_eq!(completed_event_ids(&evidence, "done"), ["E1", "E2"]);
        assert_eq!(completed_event_ids(&evidence, "blocked"), ["E1"]);
        evidence.push(record("chat_batch", vec!["E4"]));
        assert_eq!(batch_event_ids(&evidence), ["E4"]);
        assert_eq!(completed_event_ids(&evidence, "asking"), ["E4"]);
    }
    #[test]
    fn heartbeat_writes_are_throttled_without_hiding_transitions() {
        let f = Fixture::new();
        let heartbeat = Heartbeat::new(&f.workspace);
        let path = heartbeat.path("slack", "aiko", "C1");
        heartbeat
            .save(&f.subject, "aiko", "ready", 1000.0, 0.0)
            .unwrap();
        heartbeat
            .save(&f.subject, "aiko", "ready", 1001.0, 1.0)
            .unwrap();
        assert_eq!(read(&path)["checked_at"], 1000.0);
        heartbeat
            .save(&f.subject, "aiko", "catching_up", 1002.0, 2.0)
            .unwrap();
        assert_eq!(read(&path)["state"], "catching_up");
        heartbeat
            .save(&f.subject, "aiko", "catching_up", 1008.0, 8.0)
            .unwrap();
        assert_eq!(read(&path)["checked_at"], 1008.0);
    }
}
