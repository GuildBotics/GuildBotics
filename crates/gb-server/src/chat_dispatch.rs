//! Select unread thread batches and settle their bounded, durable retries.
use crate::{
    chat_state::{Pending, Store},
    service::Pending as Future,
};
use chrono::{DateTime, Duration, Utc};
use gb_member::context::PublicMember;
use gb_observability::runs::RunRecord;
use gb_ports::{ChatEvent, ChatMessage, ChatService, WorkflowStatus};
use gb_wire::WorkflowInvocation;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

pub const COMMAND: &str = "workflows/chat_conversation_workflow";
pub fn attributes(service: &str, pending: &Pending) -> serde_json::Map<String, Value> {
    serde_json::from_value(json!({"chat.provider":service,"chat.channel":pending.event.message.channel_id,"chat.thread":pending.event.message.thread_id,"chat.message":pending.event.message.message_id,"event_id":pending.event.event_id})).expect("chat attributes")
}
#[derive(Debug)]
pub struct ContextUnavailable;
impl std::fmt::Display for ContextUnavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("thread context unavailable")
    }
}
impl std::error::Error for ContextUnavailable {}
#[derive(Debug)]
pub enum Failure {
    Rejected,
    Unavailable,
    Failed,
    RateLimited { at: String, hint: String },
}
pub trait Runner: Send + Sync {
    fn selection_failed<'a>(
        &'a self,
        _pending: &'a Pending,
        _error: &'a anyhow::Error,
    ) -> Future<'a, ()> {
        Box::pin(async {})
    }
    fn roster<'a>(&'a self, fallback: &'a [PublicMember]) -> Future<'a, Vec<PublicMember>> {
        Box::pin(async move { fallback.to_vec() })
    }
    /// Admission precedes incrementing and saving the attempt. Rejected work
    /// consumes no budget. Successful dispatch returns the terminal journal.
    fn dispatch<'a>(
        &'a self,
        pending: &'a mut Pending,
        invocation: WorkflowInvocation,
        store: &'a Store,
    ) -> Future<'a, Result<RunRecord, Failure>>;
    fn record(&self, id: &str) -> anyhow::Result<Option<RunRecord>>;
    fn event(&self, kind: &str, pending: &Pending, payload: Value);
}
pub struct Dispatcher {
    pub person: String,
    pub service: String,
    pub language: String,
    pub roster: Vec<PublicMember>,
    pub chat: Arc<dyn ChatService>,
    pub store: Arc<Store>,
    pub runner: Arc<dyn Runner>,
}
impl Dispatcher {
    pub async fn process(&self, now: DateTime<Utc>) -> anyhow::Result<usize> {
        let mut count = 0;
        for channel in self.store.channels(&self.service, &self.person)? {
            let queued = self.store.pending(&self.service, &self.person, &channel)?;
            let mut threads = Vec::<Vec<Pending>>::new();
            for pending in queued {
                if let Some(group) = threads
                    .iter_mut()
                    .find(|g| g[0].event.message.thread_id == pending.event.message.thread_id)
                {
                    group.push(pending);
                } else {
                    threads.push(vec![pending]);
                }
            }
            for group in threads {
                let processed = self.processed(&channel)?;
                let Some(mut pending) = group
                    .iter()
                    .find(|p| !processed.contains(&p.event.event_id))
                    .cloned()
                else {
                    self.store.acknowledge(
                        &self.service,
                        &self.person,
                        &channel,
                        &group
                            .iter()
                            .map(|p| p.event.event_id.clone())
                            .collect::<Vec<_>>(),
                    )?;
                    continue;
                };
                if !due(&pending, now) {
                    let follower = group
                        .iter()
                        .filter(|p| p.event.event_id != pending.event.event_id)
                        .map(|p| p.event.message.position())
                        .max()
                        .unwrap_or_default();
                    if pending.last_error_category != "failed" || follower <= pending.wake_cursor {
                        continue;
                    }
                    pending.wake_cursor = follower;
                    pending.next_attempt_at = None;
                    self.store.save(&self.service, &self.person, &pending)?;
                }
                if pending.run_id.is_empty() {
                    pending.run_id = uuid::Uuid::new_v4().simple().to_string();
                }
                if pending.attempt_count == 0
                    && let Ok(raw) = std::env::var("GUILDBOTICS_CHAT_MAX_ATTEMPTS")
                {
                    pending.max_attempts = crate::ticket_selector::max_attempts(&raw) as u32;
                }
                if let Some(record) = self.runner.record(&pending.run_id)?
                    && record.finished_at.is_some()
                    && record.result.is_some()
                {
                    self.settle(&pending, &record, true).await?;
                    count += 1;
                    continue;
                }
                let invocation = match self.prepare(&pending, &group, &processed).await {
                    Ok(Some(invocation)) => invocation,
                    Ok(None) => {
                        count += 1;
                        continue;
                    }
                    Err(error) => {
                        self.runner.selection_failed(&pending, &error).await;
                        let unavailable = error.downcast_ref::<ContextUnavailable>().is_some()
                            || error
                                .downcast_ref::<gb_ports::ServiceError>()
                                .is_some_and(gb_ports::ServiceError::is_unavailable);
                        if !unavailable {
                            pending.attempt_count = pending.attempt_count.saturating_add(1);
                        }
                        self.retry(
                            &mut pending,
                            if unavailable {
                                Failure::Unavailable
                            } else {
                                Failure::Failed
                            },
                            now,
                        )
                        .await?;
                        continue;
                    }
                };
                let requested_run_id = pending.run_id.clone();
                match self
                    .runner
                    .dispatch(&mut pending, invocation, &self.store)
                    .await
                {
                    Ok(record) if record.finished_at.is_some() && record.result.is_some() => {
                        self.settle(&pending, &record, record.run_id != requested_run_id)
                            .await?;
                        count += 1;
                    }
                    Ok(_) => self.retry(&mut pending, Failure::Failed, now).await?,
                    Err(Failure::Rejected) => (),
                    Err(error) => self.retry(&mut pending, error, now).await?,
                }
            }
        }
        Ok(count)
    }
    fn processed(&self, channel: &str) -> anyhow::Result<BTreeSet<String>> {
        Ok(
            self.store.cursor(&self.service, &self.person, channel)?["processed_event_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect(),
        )
    }
    async fn prepare(
        &self,
        pending: &Pending,
        group: &[Pending],
        processed: &BTreeSet<String>,
    ) -> anyhow::Result<Option<WorkflowInvocation>> {
        let event = &pending.event;
        let channel = &event.message.channel_id;
        let thread = &event.message.thread_id;
        let identity = self.chat.get_bot_identity().await?;
        let roster = self.runner.roster(&self.roster).await;
        if event.message.is_from_user(Some(&identity.user_id)) || event.status.is_some() {
            self.store.acknowledge(
                &self.service,
                &self.person,
                channel,
                std::slice::from_ref(&event.event_id),
            )?;
            return Ok(None);
        }
        let mut snapshot = BTreeMap::new();
        let mut cursor = None;
        let mut seen = BTreeSet::new();
        loop {
            let page = self
                .chat
                .list_thread_events(channel, thread, cursor, 100)
                .await
                .map_err(|_| ContextUnavailable)?;
            for item in page.events {
                snapshot.insert(item.message.position(), item);
            }
            let previous = snapshot
                .keys()
                .filter(|position| *position < &event.message.position())
                .cloned()
                .collect::<Vec<_>>();
            for position in previous.iter().take(previous.len().saturating_sub(100)) {
                snapshot.remove(position);
            }
            cursor = page.cursor;
            if let Some(cursor) = &cursor {
                if !seen.insert(cursor.clone()) {
                    return Err(ContextUnavailable.into());
                }
            } else {
                break;
            }
        }
        let snapshot = snapshot.into_values().collect::<Vec<_>>();
        let mut batch = BTreeMap::new();
        for item in std::iter::once(event)
            .chain(group.iter().map(|p| &p.event))
            .chain(snapshot.iter())
        {
            if item.message.channel_id == *channel
                && item.message.thread_id == *thread
                && item.message.position() >= event.message.position()
                && !processed.contains(&item.event_id)
                && item.status.is_none()
            {
                batch.insert(item.event_id.clone(), item.clone());
            }
        }
        let mut batch = batch.into_values().collect::<Vec<_>>();
        batch.sort_by_key(|e| e.message.position());
        let cached = self
            .store
            .messages(&self.service, &self.person, channel, thread)?;
        let mentioned = snapshot
            .iter()
            .map(|e| &e.message)
            .chain(cached.iter())
            .any(|m| m.mentions.contains(&identity.user_id));
        if batch
            .iter()
            .filter(|e| !e.message.is_from_user(Some(&identity.user_id)))
            .all(|e| {
                skip(
                    &pending.chat_participation,
                    &e.message.mentions,
                    &identity.user_id,
                    mentioned,
                )
            })
        {
            self.store.acknowledge(
                &self.service,
                &self.person,
                channel,
                &batch.iter().map(|e| e.event_id.clone()).collect::<Vec<_>>(),
            )?;
            return Ok(None);
        }
        for item in snapshot.iter().chain(batch.iter()) {
            self.store
                .cache(&self.service, &self.person, &item.message)?;
        }
        let target = batch
            .iter()
            .rev()
            .find(|e| !e.message.is_from_user(Some(&identity.user_id)))
            .ok_or_else(|| anyhow::anyhow!("empty unread batch"))?;
        let state = self
            .store
            .thread(&self.service, &self.person, channel, thread)?;
        let prompt = self.prompt(
            &roster,
            &identity.user_id,
            &state,
            &snapshot,
            &batch,
            &pending.chat_participation,
        )?;
        let work_identity = format!(
            "{}:{}:{}:{}",
            self.service, identity.user_id, channel, thread
        );
        Ok(Some(serde_json::from_value(
            json!({"command":COMMAND,"person_id":self.person,"source":"event_queue","trigger_type":"chat","run_id":pending.run_id,
            "work":{"kind":"chat","identity":work_identity,"chat":{"service":self.service,"channel_id":channel,"thread_id":thread,"event_id":event.event_id,"self_user_id":identity.user_id}},
            "payload":{"attempt":pending.attempt_count+1,"subject":{"service":self.service,"channel_id":channel,"thread_id":thread,"event_id":event.event_id,"self_user_id":identity.user_id},
            "message_id":target.message.message_id,"context_cursor":batch.last().unwrap().message.position(),"effort":state["effort"].as_str().unwrap_or(""),"prompt":prompt}}),
        )?))
    }
    fn prompt(
        &self,
        roster: &[PublicMember],
        self_id: &str,
        state: &Value,
        snapshot: &[ChatEvent],
        batch: &[ChatEvent],
        policy: &str,
    ) -> anyhow::Result<Value> {
        let event = &batch[0];
        let channel = &event.message.channel_id;
        let thread = &event.message.thread_id;
        let cached = self
            .store
            .messages(&self.service, &self.person, channel, thread)?;
        let people = roster
            .iter()
            .filter_map(|p| {
                p.accounts
                    .get(&self.service)
                    .filter(|id| !id.trim().is_empty())
                    .map(|id| {
                        (
                            self.chat.self_user_id(&gb_ports::ActorName(id.clone())),
                            p.person_id.clone(),
                        )
                    })
            })
            .collect::<BTreeMap<_, _>>();
        let mut labels = BTreeMap::from([(self_id.to_owned(), self.person.clone())]);
        let mut ordered = vec![];
        let mut bots = BTreeSet::new();
        for message in cached
            .iter()
            .rev()
            .take(20)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .chain(std::iter::once(&event.message))
        {
            if let Some(id) = &message.author_id {
                if !ordered.contains(id) {
                    ordered.push(id.clone());
                }
                if message.is_bot_message {
                    bots.insert(id.clone());
                }
            }
            for mention in &message.mentions {
                if !ordered.contains(mention) {
                    ordered.push(mention.clone());
                }
            }
        }
        let (mut agent, mut user) = (1, 1);
        for id in ordered {
            if labels.contains_key(&id) {
                continue;
            }
            let label = people.get(&id).cloned().unwrap_or_else(|| {
                if bots.contains(&id) {
                    let label = format!("agent_{agent}");
                    agent += 1;
                    label
                } else {
                    let label = format!("user_{user}");
                    user += 1;
                    label
                }
            });
            labels.insert(id, label);
        }
        for (id, person) in &people {
            labels.entry(id.clone()).or_insert(person.clone());
        }
        let message = |m: &ChatMessage| {
            json!({"content":self.chat.normalize_participant_text(&m.text,&labels),"author":m.author_id.as_ref().and_then(|id|labels.get(id)).map(String::as_str).unwrap_or(if m.is_bot_message {"agent"} else {"user"}),
            "author_type":if m.is_bot_message && m.is_from_user(Some(self_id)) {"Assistant"} else {"User"},"timestamp":m.position()})
        };
        let mut context = BTreeMap::new();
        for m in cached
            .iter()
            .rev()
            .take(100)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .chain(snapshot.iter().map(|e| &e.message))
        {
            context.insert(m.position(), message(m));
        }
        let positions = batch
            .iter()
            .map(|e| e.message.position())
            .collect::<BTreeSet<_>>();
        let cutoff = batch.last().unwrap().message.position();
        context.retain(|key, _| !positions.contains(key) && key < &cutoff);
        let context = context.into_values().collect::<Vec<_>>();
        let from = context.len().saturating_sub(100);
        let handoffs=roster.iter().filter(|p|p.person_id!=self.person && people.values().any(|id|id==&p.person_id) && p.roles.as_object().is_some_and(|r|!r.is_empty())).map(|p|json!({"person_id":p.person_id,"name":p.name.trim(),"mention":format!("@{}",p.person_id),"roles":p.roles})).collect::<Vec<_>>();
        Ok(
            json!({"unprocessed_messages":batch.iter().map(|e|message(&e.message)).collect::<Vec<_>>(),"participant_labels":labels,"handoff_candidates":handoffs,"chat_participation":policy,
            "previous_thread_context":{"thread_topic":state["thread_topic"].as_str().unwrap_or(""),"latest_focus":state["latest_focus"].as_str().unwrap_or(""),"handoffs":state["handoffs"].as_array().cloned().unwrap_or_default()},
            "thread_context":&context[from..],"thread_context_complete":true,"source_cursor":event.message.position(),"source_snapshot":snapshot.iter().map(ChatEvent::payload).collect::<Vec<_>>(),"event_ids":batch.iter().map(|e|&e.event_id).collect::<Vec<_>>()}),
        )
    }
    async fn settle(
        &self,
        pending: &Pending,
        record: &RunRecord,
        recovered: bool,
    ) -> anyhow::Result<()> {
        let status = record
            .result
            .as_ref()
            .map(|r| match r.status {
                gb_observability::runs::Completion::Done => "done",
                gb_observability::runs::Completion::Asking => "asking",
                gb_observability::runs::Completion::AwaitingChecks => "awaiting_checks",
                _ => "blocked",
            })
            .unwrap_or("");
        if recovered {
            self.runner.event("workflow.completed",pending,json!({"run_id":record.run_id,"attempt":pending.attempt_count.max(1),"recovered":true,"status":status}));
        }
        let mut ids =
            gb_member::chat_updates::completed_event_ids(&record.provider_evidence, status);
        ids.push(pending.event.event_id.clone());
        let channel = &pending.event.message.channel_id;
        let thread = &pending.event.message.thread_id;
        self.store
            .acknowledge(&self.service, &self.person, channel, &ids)?;
        if let Some(posted) = record
            .provider_evidence
            .iter()
            .rev()
            .find(|e| matches!(e["type"].as_str(), Some("chat_reply" | "chat_post")))
        {
            let payload = &posted["payload"];
            let text = payload["text"].as_str().unwrap_or("").trim();
            let message_id = payload["message_id"].as_str().unwrap_or("").trim();
            if !text.is_empty() && !message_id.is_empty() {
                let target_thread = payload["thread_id"].as_str().unwrap_or(thread);
                let mentions = self.chat.mentioned_user_ids(text);
                let identity = self.chat.get_bot_identity().await?;
                let occurred_at =
                    DateTime::parse_from_rfc3339(payload["occurred_at"].as_str().unwrap_or(""))?;
                self.store.cache(
                    &self.service,
                    &self.person,
                    &ChatMessage {
                        channel_id: channel.clone(),
                        thread_id: target_thread.into(),
                        message_id: message_id.into(),
                        occurred_at,
                        author_id: Some(identity.user_id),
                        text: text.into(),
                        mentions: mentions.clone(),
                        is_bot_message: true,
                    },
                )?;
                let state = self
                    .store
                    .thread(&self.service, &self.person, channel, thread)?;
                let mut handoffs = state["handoffs"].as_array().cloned().unwrap_or_default();
                for member in &self.runner.roster(&self.roster).await {
                    if member.person_id == self.person {
                        continue;
                    }
                    let Some(account) = member.accounts.get(&self.service) else {
                        continue;
                    };
                    if account.trim().is_empty() {
                        continue;
                    }
                    let user = self
                        .chat
                        .self_user_id(&gb_ports::ActorName(account.clone()));
                    if mentions.contains(&user)
                        && !handoffs.iter().any(|h| {
                            h["person_id"] == member.person_id && h["message_id"] == message_id
                        })
                    {
                        let normalized = text.split_whitespace().collect::<Vec<_>>().join(" ");
                        let shortened = if normalized.chars().count() > 240 {
                            format!(
                                "{}...",
                                normalized.chars().take(239).collect::<String>().trim_end()
                            )
                        } else {
                            normalized
                        };
                        handoffs.push(json!({"person_id":member.person_id,"roles":member.roles.as_object().map(|m|m.keys().cloned().collect::<Vec<_>>()).unwrap_or_default(),"message_id":message_id,"text":shortened,"thread_topic":state["thread_topic"].as_str().unwrap_or(""),"latest_focus":state["latest_focus"].as_str().unwrap_or("")}));
                    }
                }
                self.store.update_thread(
                    &self.service,
                    &self.person,
                    channel,
                    thread,
                    &json!({"handoffs":handoffs}),
                )?;
            }
        }

        if record.provider_evidence.iter().any(|e| {
            matches!(
                e["type"].as_str(),
                Some("chat_reply" | "chat_post" | "chat_reaction")
            )
        }) {
            let state = self
                .store
                .thread(&self.service, &self.person, channel, thread)?;
            let mut participants = state["participants"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            if !participants.contains(&json!(self.person)) {
                participants.push(json!(self.person));
            }
            self.store.update_thread(
                &self.service,
                &self.person,
                channel,
                thread,
                &json!({"participants":participants}),
            )?;
        }
        Ok(())
    }
    async fn retry(
        &self,
        pending: &mut Pending,
        error: Failure,
        now: DateTime<Utc>,
    ) -> anyhow::Result<()> {
        if let Failure::RateLimited { at, hint } = &error {
            self.runner.event("workflow.rate_limited",pending,json!({"category":"rate_limited","retry_after_at":at,"retry_after_text":hint,"subject_id":pending.event.event_id,"source_event_id":pending.event.event_id}));
        }
        let unavailable = matches!(error, Failure::Unavailable);
        let rate = matches!(error, Failure::RateLimited { .. });
        pending.last_error_category = if unavailable {
            "provider_unavailable"
        } else if rate {
            "rate_limited"
        } else {
            "failed"
        }
        .into();
        if !unavailable && pending.attempt_count >= pending.max_attempts {
            if let Failure::RateLimited { at, hint } = &error {
                self.notice(pending, "rate_limited", at, hint).await;
            } else {
                self.notice(pending, "failed", "", "").await;
            }
            self.runner.event(
                "chat_dispatch.abandoned",
                pending,
                json!({"error_category":pending.last_error_category}),
            );
            return self.store.acknowledge(
                &self.service,
                &self.person,
                &pending.event.message.channel_id,
                std::slice::from_ref(&pending.event.event_id),
            );
        }
        let next = match error {
            Failure::RateLimited { at, hint } => {
                self.notice(pending, "rate_limited", &at, &hint).await;
                if at.is_empty() {
                    (now + Duration::seconds(backoff(pending.attempt_count))).to_rfc3339()
                } else {
                    at
                }
            }
            _ => (now + Duration::seconds(backoff(pending.attempt_count.max(1)))).to_rfc3339(),
        };
        pending.next_attempt_at = Some(next);
        self.store.save(&self.service, &self.person, pending)?;
        self.runner.event("chat_dispatch.retry_scheduled",pending,json!({"error_category":pending.last_error_category,"next_attempt_at":pending.next_attempt_at}));
        Ok(())
    }
    async fn notice(&self, pending: &Pending, reason: &str, at: &str, hint: &str) {
        let channel = &pending.event.message.channel_id;
        let thread = &pending.event.message.thread_id;
        let Ok(state) = self
            .store
            .thread(&self.service, &self.person, channel, thread)
        else {
            return;
        };
        let mut notices = state["system_notices"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        if notices.iter().any(|n| {
            n["kind"] == "workflow_error" && n["source_event_id"] == pending.event.event_id
        }) {
            return;
        }
        let body = if reason == "rate_limited" {
            crate::ticket_selector::rate_limit_notice(&self.language, at, hint, true)
        } else {
            crate::i18n::t(
                &self.language,
                "commands.workflows.chat_conversation_workflow.incomplete_escalation",
                &[],
            )
        };
        let Ok(status) = serde_json::from_value::<WorkflowStatus>(
            json!({"reason":reason,"person_id":self.person,"run_id":pending.run_id,"subject":pending.event.event_id,"retry_after_at":at,"retry_after_text":hint}),
        ) else {
            return;
        };
        if let Ok(written) = self
            .chat
            .post_message(channel, &body, Some(thread), Some(&status))
            .await
        {
            notices.push(json!({"kind":"workflow_error","reason":reason,"person_id":self.person,"source_event_id":pending.event.event_id,"message_id":written.message_id,"run_id":pending.run_id,"retry_after_at":at,"retry_after_text":hint,"recorded_at":Utc::now().to_rfc3339()}));
            let _ = self.store.update_thread(
                &self.service,
                &self.person,
                channel,
                thread,
                &json!({"system_notices":notices}),
            );
        }
    }
}
pub fn backoff(attempt: u32) -> i64 {
    match attempt {
        0 | 1 => 60,
        2 => 120,
        3 => 300,
        _ => 900,
    }
}
fn due(pending: &Pending, now: DateTime<Utc>) -> bool {
    pending
        .next_attempt_at
        .as_deref()
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .is_none_or(|at| at <= now)
}
fn skip(policy: &str, mentions: &[String], self_id: &str, thread_mentions: bool) -> bool {
    let addressed = mentions.iter().any(|id| id == self_id);
    if policy == "muted" {
        return !addressed;
    }
    if !mentions.is_empty() && !addressed {
        return true;
    }
    policy != "social" && !addressed && !thread_mentions
}
