//! Live reception stays responsive while paginated history catches up.

use crate::{chat_state::Store, work_stops::Subscription};
use chrono::{DateTime, Duration, Utc};
use gb_member::chat_updates::{Heartbeat, Subject};
use gb_ports::{ChannelEventsQuery, ChatEvent, ChatService, EventListener, ServiceError};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::Arc,
};
use tokio::{sync::Mutex, time::Instant};
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
pub struct Connection {
    pub service: String,
    pub key: String,
    pub listener: Arc<dyn EventListener>,
    pub wake: Arc<tokio::sync::Notify>,
}

pub struct Receiver {
    pub service: String,
    pub person: String,
    pub chat: Arc<dyn ChatService>,
    pub listener: Arc<dyn EventListener>,
    wake: Arc<tokio::sync::Notify>,
    pub channels: Vec<Subscription>,
    pub store: Arc<Store>,
    heartbeat: Heartbeat,
    catching_up: Mutex<BTreeSet<String>>,
    undelivered: Mutex<VecDeque<ChatEvent>>,
    started: Instant,
}
impl Receiver {
    pub async fn new(
        person: String,
        chat: Arc<dyn ChatService>,
        connection: Connection,
        subscriptions: Vec<Subscription>,
        store: Arc<Store>,
    ) -> Result<Self, ServiceError> {
        let mut channels = BTreeMap::new();
        for mut channel in subscriptions {
            if channel.channel_id.is_empty() {
                channel.channel_id = chat
                    .resolve_channel_id(&channel.channel_name)
                    .await?
                    .unwrap_or_default();
            }
            if !channel.channel_id.is_empty() {
                channels
                    .entry(channel.channel_id.clone())
                    .or_insert(channel);
            }
        }
        let heartbeat = Heartbeat::new(&store.workspace);
        let writes = gb_base::shared_writes::SharedWrites {
            identity: store.writes.identity.clone(),
            workspace: store.writes.workspace.clone(),
            timeout: std::time::Duration::ZERO,
        };
        let store = Arc::new(Store {
            workspace: store.workspace.clone(),
            writes: Arc::new(writes),
            limit: store.limit,
        });
        Ok(Self {
            service: connection.service,
            person,
            chat,
            listener: connection.listener,
            wake: connection.wake,
            catching_up: Mutex::new(channels.keys().cloned().collect()),
            channels: channels.into_values().collect(),
            store,
            heartbeat,
            undelivered: Mutex::new(VecDeque::new()),
            started: Instant::now(),
        })
    }
    fn status(&self, channel: &str, state: &str) {
        let subject = Subject {
            service: self.service.clone(),
            channel_id: channel.into(),
            thread_id: String::new(),
            event_id: String::new(),
            self_user_id: String::new(),
        };
        if self
            .heartbeat
            .save(
                &subject,
                &self.person,
                state,
                Utc::now().timestamp_millis() as f64 / 1000.0,
                self.started.elapsed().as_secs_f64(),
            )
            .is_err()
        {
            tracing::warn!("cannot save chat receiver heartbeat");
        }
    }
    pub async fn run(&self, stop: CancellationToken) -> anyhow::Result<()> {
        let result = async {
            self.listener.start().await?;
            tokio::join!(self.receive_loop(&stop), self.backfill_loop(&stop));
            Ok(())
        }
        .await;
        for channel in &self.channels {
            self.status(&channel.channel_id, "unavailable");
        }
        let stopped = self.listener.stop().await;
        result.and(stopped.map_err(Into::into))
    }
    async fn receive_loop(&self, stop: &CancellationToken) {
        loop {
            tokio::select! {biased; ()=stop.cancelled()=>break, ()=self.drain()=>()}
            tokio::select! {()=stop.cancelled()=>break, ()=self.wake.notified()=>(), ()=tokio::time::sleep(std::time::Duration::from_secs(5))=>()}
        }
    }
    pub async fn drain(&self) {
        self.accept(self.listener.drain_events()).await;
    }
    async fn accept(&self, events: Vec<ChatEvent>) {
        let mut undelivered = self.undelivered.lock().await;
        undelivered.extend(events);
        let mut failure = None;
        while let Some(event) = undelivered.front() {
            if let Some(channel) = self
                .channels
                .iter()
                .find(|c| c.channel_id == event.message.channel_id)
                && let Err(error) = self.queue(event.clone(), channel)
            {
                failure = Some(error);
                break;
            }
            undelivered.pop_front();
        }
        let waiting = self.catching_up.lock().await;
        for channel in &self.channels {
            self.status(
                &channel.channel_id,
                if failure.as_ref().is_some_and(|e| {
                    e.downcast_ref::<gb_base::shared_writes::SharedWriteBusyError>()
                        .is_none()
                }) || !self.listener.connected()
                {
                    "unavailable"
                } else if failure.is_some() || waiting.contains(&channel.channel_id) {
                    "catching_up"
                } else {
                    "ready"
                },
            );
        }
    }
    async fn persist<T>(&self, mut apply: impl FnMut() -> anyhow::Result<T>) -> anyhow::Result<T> {
        loop {
            match apply() {
                Err(error)
                    if error
                        .downcast_ref::<gb_base::shared_writes::SharedWriteBusyError>()
                        .is_some() =>
                {
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await
                }
                result => return result,
            }
        }
    }
    fn queue(&self, event: ChatEvent, policy: &Subscription) -> anyhow::Result<()> {
        let cursor = self
            .store
            .cursor(&self.service, &self.person, &event.message.channel_id)?;
        if cursor["processed_event_ids"]
            .as_array()
            .is_some_and(|ids| ids.contains(&serde_json::json!(event.event_id)))
        {
            return Ok(());
        }
        if event.status.is_some() {
            return self.store.acknowledge(
                &self.service,
                &self.person,
                &event.message.channel_id,
                &[event.event_id],
            );
        }
        self.store
            .receive(&self.service, &self.person, event, &policy.participation)
    }
    async fn backfill_loop(&self, stop: &CancellationToken) {
        let mut last = BTreeMap::<String, Instant>::new();
        loop {
            for channel in &self.channels {
                let due = last.get(&channel.channel_id).is_none_or(|at| {
                    channel.interval_seconds > 0.0
                        && at.elapsed().as_secs_f64() >= channel.interval_seconds
                });
                if !due {
                    continue;
                }
                self.catching_up
                    .lock()
                    .await
                    .insert(channel.channel_id.clone());
                self.status(
                    &channel.channel_id,
                    if self.listener.connected() {
                        "catching_up"
                    } else {
                        "unavailable"
                    },
                );
                let completed = tokio::select! {biased; ()=stop.cancelled()=>None, result=self.backfill(channel,Utc::now())=>{if result.is_err() {tracing::warn!("chat history backfill failed");} Some(result.is_ok())}};
                let Some(completed) = completed else {
                    return;
                };
                if completed {
                    self.catching_up.lock().await.remove(&channel.channel_id);
                }
                self.accept(vec![]).await;
                last.insert(channel.channel_id.clone(), Instant::now());
            }
            tokio::select! {()=stop.cancelled()=>break, ()=tokio::time::sleep(std::time::Duration::from_secs(5))=>()}
        }
    }
    pub async fn backfill(&self, policy: &Subscription, now: DateTime<Utc>) -> anyhow::Result<()> {
        let channel = &policy.channel_id;
        let cutoff = self.store.cutoff(&self.service, &self.person)?;
        let stored = self.store.cursor(&self.service, &self.person, channel)?;
        let previous = stored["watermark"]
            .as_str()
            .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
            .map(|t| t.to_utc());
        let since = previous
            .map(|t| t - Duration::milliseconds((policy.overlap_seconds * 1000.0) as i64))
            .or_else(|| {
                (policy.startup_minutes > 0)
                    .then(|| now - Duration::minutes(i64::from(policy.startup_minutes)))
            });
        if let Some(since) = since {
            let since = cutoff.map_or(since, |cutoff| cutoff.max(since));
            let mut cursor = None;
            let mut watermark = previous;
            let mut seen = BTreeSet::new();
            loop {
                let page = self
                    .chat
                    .list_channel_events(
                        channel,
                        ChannelEventsQuery {
                            since: Some(since),
                            until: None,
                            cursor,
                            limit: policy.limit,
                        },
                    )
                    .await?;
                for event in page.events {
                    let time = event.message.occurred_at.to_utc();
                    watermark = Some(watermark.map_or(time, |t| t.max(time)));
                    if cutoff.is_none_or(|cutoff| time > cutoff) {
                        self.persist(|| self.queue(event.clone(), policy)).await?;
                    }
                }
                cursor = page.cursor;
                if let Some(cursor) = &cursor {
                    anyhow::ensure!(seen.insert(cursor.clone()), "repeated history cursor");
                } else {
                    break;
                }
            }
            if let Some(watermark) = watermark
                && Some(watermark) != previous
            {
                self.persist(|| {
                    self.store.watermark(
                        &self.service,
                        &self.person,
                        channel,
                        &watermark.to_rfc3339(),
                    )
                })
                .await?;
            }
        }
        for (thread, state) in self.store.threads(&self.service, &self.person, channel)? {
            if state["backfill_disabled_reason"]
                .as_str()
                .is_some_and(|v| !v.is_empty())
            {
                continue;
            }
            let cached = self
                .store
                .messages(&self.service, &self.person, channel, &thread)?;
            let since = cached.last().map(|m| {
                m.occurred_at.to_utc()
                    - Duration::milliseconds((policy.overlap_seconds * 1000.0) as i64)
            });
            let mut cursor = None;
            let mut seen = BTreeSet::new();
            loop {
                let page = match self
                    .chat
                    .list_thread_events(channel, &thread, cursor, policy.limit)
                    .await
                {
                    Ok(page) => page,
                    Err(error) if error.is_not_found() => {
                        self.store.update_thread(&self.service,&self.person,channel,&thread,&serde_json::json!({"backfill_disabled_reason":"thread_not_found","last_backfill_error":"thread_not_found","backfill_error_count":state["backfill_error_count"].as_u64().unwrap_or(0)+1}))?;
                        break;
                    }
                    Err(error) => return Err(error.into()),
                };
                for event in page.events {
                    let time = event.message.occurred_at.to_utc();
                    if since.is_none_or(|since| time >= since)
                        && cutoff.is_none_or(|cutoff| time > cutoff)
                    {
                        self.persist(|| self.queue(event.clone(), policy)).await?;
                    }
                }
                cursor = page.cursor;
                if let Some(cursor) = &cursor {
                    anyhow::ensure!(seen.insert(cursor.clone()), "repeated thread cursor");
                } else {
                    break;
                }
            }
        }
        Ok(())
    }
}

/// One connection is drained once and fanned out to every subscribed member.
pub struct Group {
    listener: Arc<dyn EventListener>,
    receivers: Vec<Arc<Receiver>>,
}
impl Group {
    pub fn auth_failed(&self) -> bool {
        self.listener.auth_failed()
    }
    pub fn people(&self) -> Vec<String> {
        self.receivers.iter().map(|r| r.person.clone()).collect()
    }
    pub fn new(receivers: Vec<Arc<Receiver>>) -> anyhow::Result<Self> {
        let listener = receivers
            .first()
            .ok_or_else(|| anyhow::anyhow!("empty receiver group"))?
            .listener
            .clone();
        anyhow::ensure!(
            receivers
                .iter()
                .all(|r| Arc::ptr_eq(&r.listener, &listener)
                    && Arc::ptr_eq(&r.wake, &receivers[0].wake)),
            "a receiver group must share one connection"
        );
        Ok(Self {
            listener,
            receivers,
        })
    }
    pub async fn drain(&self) {
        let events = self.listener.drain_events();
        for receiver in &self.receivers {
            receiver.accept(events.clone()).await;
        }
    }
    pub async fn run(&self, stop: CancellationToken) -> anyhow::Result<()> {
        let result=async {
            self.listener.start().await?;
            let live=async {
                loop {
                    tokio::select! {biased; ()=stop.cancelled()=>break, ()=self.drain()=>()}
                    tokio::select! {()=stop.cancelled()=>break, ()=self.receivers[0].wake.notified()=>(), ()=tokio::time::sleep(std::time::Duration::from_secs(5))=>()}
                }
            };
            let histories=futures_util::future::join_all(self.receivers.iter().map(|r|r.backfill_loop(&stop)));
            tokio::join!(live,histories);Ok(())
        }.await;
        for receiver in &self.receivers {
            for channel in &receiver.channels {
                receiver.status(&channel.channel_id, "unavailable");
            }
        }
        let stopped = self.listener.stop().await;
        result.and(stopped.map_err(Into::into))
    }
}

#[cfg(test)]
mod tests {

    fn subject() -> Subject {
        Subject {
            service: "local".into(),
            channel_id: "C1".into(),
            thread_id: "live".into(),
            event_id: "C1:live".into(),
            self_user_id: "local-aiko".into(),
        }
    }
    fn state(f: &Fixture, person: &str) -> String {
        Heartbeat::new(&f.store.workspace).state(
            &subject(),
            person,
            Utc::now().timestamp_millis() as f64 / 1000.0,
        )
    }
    async fn ready(f: &Fixture, receiver: &Receiver) {
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            loop {
                receiver.drain().await;
                if state(f, "aiko") == "ready" {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    }
    #[tokio::test(start_paused = true)]
    async fn failed_backfill_obeys_each_members_cadence_and_never_claims_ready_until_recovery() {
        let f = Fixture::new();
        f.history(&[]);
        f.chat
            .errors
            .lock()
            .unwrap()
            .insert("channel:C1".into(), ServiceError::Unavailable);
        let aiko = f
            .receiver("aiko", json!({"backfill_interval_seconds":300}))
            .await;
        let yuki = f
            .receiver("yuki", json!({"backfill_interval_seconds":0}))
            .await;
        let stop = CancellationToken::new();
        let mut tasks = vec![];
        for receiver in [aiko, yuki] {
            let stop = stop.clone();
            tasks.push(tokio::spawn(
                async move { receiver.backfill_loop(&stop).await },
            ));
        }
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        let count = || {
            f.chat
                .calls
                .lock()
                .unwrap()
                .iter()
                .filter(|c| c["operation"] == "channel")
                .count()
        };
        assert_eq!(count(), 2);
        tokio::time::advance(std::time::Duration::from_secs(5)).await;
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        assert_eq!(count(), 2);
        assert_eq!(state(&f, "aiko"), "catching_up");
        tokio::time::advance(std::time::Duration::from_secs(295)).await;
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        assert_eq!(count(), 3);
        f.chat.errors.lock().unwrap().clear();
        tokio::time::advance(std::time::Duration::from_secs(300)).await;
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        assert_eq!(count(), 4);
        assert_eq!(state(&f, "aiko"), "ready");
        assert_eq!(state(&f, "yuki"), "catching_up");
        stop.cancel();
        for task in tasks {
            task.await.unwrap();
        }
        tokio::time::advance(std::time::Duration::from_secs(300)).await;
        assert_eq!(count(), 4);
        assert!(
            gb_observability::runs::RunStore::open(&f.store.workspace)
                .records()
                .unwrap()
                .is_empty()
        );
    }

    #[tokio::test]
    async fn failed_receive_save_retains_input_and_heartbeat_recovers_only_after_durable_save() {
        let f = Fixture::new();
        f.history(&[row("live", "live", 1)]);
        let receiver = f
            .receiver("aiko", json!({"startup_backfill_minutes":0}))
            .await;
        let stop = CancellationToken::new();
        let task = tokio::spawn({
            let receiver = receiver.clone();
            let stop = stop.clone();
            async move { receiver.run(stop).await }
        });
        ready(&f, &receiver).await;
        let path = f
            .store
            .workspace
            .join(".guildbotics/state/chat_state/local/aiko/pending_events/C1.json");
        std::fs::create_dir_all(&path).unwrap();
        let event = f
            .chat
            .inner
            .list_thread_events("C1", "live", None, 100)
            .await
            .unwrap()
            .events
            .remove(0);
        f.listener.events.lock().unwrap().push(event);
        receiver.drain().await;
        assert_eq!(state(&f, "aiko"), "unavailable");
        assert_eq!(receiver.undelivered.lock().await.len(), 1);
        assert!(path.is_dir());
        std::fs::remove_dir(path).unwrap();
        receiver.drain().await;
        assert!(receiver.undelivered.lock().await.is_empty());
        assert_eq!(state(&f, "aiko"), "ready");
        assert_eq!(f.store.pending("local", "aiko", "C1").unwrap().len(), 1);
        stop.cancel();
        task.await.unwrap().unwrap();
        assert_eq!(state(&f, "aiko"), "unavailable");
    }

    #[tokio::test]
    async fn both_live_and_backfill_shared_write_waits_keep_a_fresh_catching_up_heartbeat() {
        for source in ["socket", "backfill"] {
            let f = Fixture::new();
            f.history(&[]);
            let receiver = f.receiver("aiko", json!({})).await;
            let stop = CancellationToken::new();
            let task = tokio::spawn({
                let receiver = receiver.clone();
                let stop = stop.clone();
                async move { receiver.run(stop).await }
            });
            ready(&f, &receiver).await;
            stop.cancel();
            task.await.unwrap().unwrap();
            let mut incoming = row("live", "live", 1);
            incoming["occurred_at"] = json!(Utc::now().to_rfc3339());
            f.history(&[incoming]);
            let event = f
                .chat
                .inner
                .list_thread_events("C1", "live", None, 100)
                .await
                .unwrap()
                .events
                .remove(0);
            let writes = f.store.writes.clone();
            let (held, wait) = tokio::sync::oneshot::channel();
            let (release, released) = std::sync::mpsc::channel();
            let holder = tokio::task::spawn_blocking(move || {
                writes.transaction(|_| {
                    held.send(()).unwrap();
                    released.recv().unwrap();
                    Ok(())
                })
            });
            wait.await.unwrap();
            if source == "socket" {
                f.listener.events.lock().unwrap().push(event);
            }
            let stop = CancellationToken::new();
            let task = tokio::spawn({
                let receiver = receiver.clone();
                let stop = stop.clone();
                async move { receiver.run(stop).await }
            });
            tokio::time::timeout(std::time::Duration::from_secs(1), async {
                loop {
                    if state(&f, "aiko") == "catching_up" {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            assert!(f.store.pending("local", "aiko", "C1").unwrap().is_empty());
            // The heartbeat uses the local atomic writer, not the held shared writer.
            assert_eq!(state(&f, "aiko"), "catching_up");
            release.send(()).unwrap();
            holder.await.unwrap().unwrap();
            ready(&f, &receiver).await;
            assert_eq!(f.store.pending("local", "aiko", "C1").unwrap().len(), 1);
            stop.cancel();
            task.await.unwrap().unwrap();
            assert_eq!(state(&f, "aiko"), "unavailable");
        }
    }

    use super::*;
    use serde_json::{Value, json};
    use std::sync::atomic::Ordering;
    struct Fixture {
        root: tempfile::TempDir,
        chat: Arc<crate::test_chat::Faults>,
        store: Arc<Store>,
        listener: Arc<crate::member::native::tests::Received>,
        connection: Connection,
    }
    impl Fixture {
        fn new() -> Self {
            let root = tempfile::tempdir().unwrap();
            let runtime = crate::test_chat::runtime(root.path());
            let store = Arc::new(Store {
                workspace: runtime.dispatcher.workspace,
                writes: runtime.writes,
                limit: 500,
            });
            let chat = Arc::new(crate::test_chat::Faults::new(runtime.chat.unwrap()));
            let listener = Arc::new(crate::member::native::tests::Received::new());
            let connection = Connection {
                service: "local".into(),
                key: "shared".into(),
                listener: listener.clone(),
                wake: Default::default(),
            };
            Self {
                root,
                chat,
                store,
                listener,
                connection,
            }
        }
        fn history(&self, rows: &[Value]) {
            let path = self
                .store
                .workspace
                .join(".guildbotics/local/services/chat/C1.jsonl");
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(
                path,
                rows.iter().map(|r| format!("{r}\n")).collect::<String>(),
            )
            .unwrap();
        }
        async fn receiver(&self, person: &str, options: Value) -> Arc<Receiver> {
            let mut policy =
                json!({"channel_name":"C1","participation":"social","startup_backfill_minutes":60});
            policy
                .as_object_mut()
                .unwrap()
                .extend(options.as_object().unwrap().clone());
            Arc::new(
                Receiver::new(
                    person.into(),
                    self.chat.clone(),
                    self.connection.clone(),
                    crate::work_stops::subscriptions(
                        &json!({"message_channels":[{"chat":policy}]}),
                    ),
                    self.store.clone(),
                )
                .await
                .unwrap(),
            )
        }
    }
    fn at(seconds: i64) -> DateTime<Utc> {
        "2026-11-05T00:00:00Z".parse::<DateTime<Utc>>().unwrap() + Duration::seconds(seconds)
    }
    fn row(id: &str, thread: &str, seconds: i64) -> Value {
        json!({"message_id":id,"thread_id":thread,"author":"owner","text":id,"occurred_at":at(seconds).to_rfc3339()})
    }

    #[tokio::test]
    async fn backfill_preserves_cutoff_equal_times_thread_overlap_and_missing_thread_disable() {
        for case in ["cutoff", "same_time", "thread"] {
            let f = Fixture::new();
            let options = if case == "thread" {
                json!({"startup_backfill_minutes":0})
            } else if case == "same_time" {
                json!({"backfill_overlap_seconds":0})
            } else {
                json!({})
            };
            match case {
                "cutoff" => {
                    f.history(&[row("a", "a", 100), row("b", "b", 200), row("c", "c", 250)]);
                    f.store
                        .watermark("local", "aiko", "C1", &at(205).to_rfc3339())
                        .unwrap();
                    f.store
                        .writes
                        .transaction(|writer| {
                            writer.write_json(
                                &f.store.workspace.join(
                                    ".guildbotics/state/chat_state/local/aiko/receive_cutoff.json",
                                ),
                                &json!({"cutoff":at(200).to_rfc3339()}),
                            )
                        })
                        .unwrap();
                }
                "same_time" => {
                    f.history(&[row("m-b", "m-b", 100), row("m-a", "m-a", 100)]);
                    f.store
                        .watermark("local", "aiko", "C1", &at(100).to_rfc3339())
                        .unwrap();
                }
                _ => {
                    f.history(&[
                        row("t1", "t1", 0),
                        row("r1", "t1", 10),
                        row("r2", "t1", 100),
                    ]);
                    f.store
                        .update_thread("local", "aiko", "C1", "gone", &json!({}))
                        .unwrap();
                    let mut known = f
                        .chat
                        .inner
                        .list_thread_events("C1", "t1", None, 100)
                        .await
                        .unwrap()
                        .events
                        .remove(2)
                        .message;
                    known.occurred_at = at(80).fixed_offset();
                    f.store.cache("local", "aiko", &known).unwrap();
                }
            }
            let receiver = f.receiver("aiko", options).await;
            for _ in 0..2 {
                receiver
                    .backfill(&receiver.channels[0], at(300))
                    .await
                    .unwrap();
            }
            let pending = f.store.pending("local", "aiko", "C1").unwrap();
            let ids = pending
                .iter()
                .map(|p| p.event.message.message_id.as_str())
                .collect::<Vec<_>>();
            let calls = f.chat.calls.lock().unwrap().clone();
            assert_eq!(
                calls.iter().filter(|c| c["operation"] == "resolve").count(),
                1
            );
            match case {
                "cutoff" => {
                    assert_eq!(ids, ["c"]);
                    assert_eq!(
                        f.store.cursor("local", "aiko", "C1").unwrap()["watermark"],
                        at(250).to_rfc3339()
                    );
                    assert!(
                        calls
                            .iter()
                            .filter(|c| c["operation"] == "channel")
                            .all(
                                |c| DateTime::parse_from_rfc3339(c["since"].as_str().unwrap())
                                    .unwrap()
                                    .to_utc()
                                    == at(200)
                            )
                    );
                }
                "same_time" => {
                    assert_eq!(ids, ["m-a", "m-b"]);
                    assert_eq!(
                        f.store.cursor("local", "aiko", "C1").unwrap()["watermark"],
                        at(100).to_rfc3339()
                    );
                }
                _ => {
                    assert_eq!(ids, ["r2"]);
                    let state = f.store.thread("local", "aiko", "C1", "gone").unwrap();
                    assert_eq!(state["backfill_disabled_reason"], "thread_not_found");
                    assert_eq!(state["backfill_error_count"], 1);
                    assert_eq!(state["last_backfill_error"], "thread_not_found");
                    assert_eq!(
                        calls
                            .iter()
                            .filter(|c| c["operation"] == "thread" && c["thread"] == "gone")
                            .count(),
                        1
                    );
                }
            }
            assert!(pending.iter().all(|p| p.chat_participation == "social"));
            assert!(f.root.path().exists());
        }
    }

    #[tokio::test]
    async fn one_connection_wakes_from_another_thread_during_backfill_and_fans_out_only_unprocessed_input()
     {
        let f = Fixture::new();
        f.history(&[row("live", "live", 1)]);
        let entered = CancellationToken::new();
        let release = CancellationToken::new();
        *f.chat.blocking.lock().unwrap() = Some(("channel".into(), entered.clone(), release));
        let mut receivers = vec![];
        for person in ["aiko", "yuki", "carol"] {
            receivers.push(f.receiver(person, json!({})).await);
        }
        f.store
            .acknowledge("local", "carol", "C1", &["C1:live".into()])
            .unwrap();
        let group = Arc::new(Group::new(receivers).unwrap());
        let stop = CancellationToken::new();
        let task = tokio::spawn({
            let group = group.clone();
            let stop = stop.clone();
            async move { group.run(stop).await }
        });
        entered.cancelled().await;
        let event = f
            .chat
            .inner
            .list_thread_events("C1", "live", None, 100)
            .await
            .unwrap()
            .events
            .remove(0);
        let listener = f.listener.clone();
        let wake = f.connection.wake.clone();
        std::thread::spawn(move || {
            listener.events.lock().unwrap().push(event);
            wake.notify_one();
        })
        .join()
        .unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            loop {
                if f.store.pending("local", "aiko", "C1").unwrap().len() == 1
                    && f.store.pending("local", "yuki", "C1").unwrap().len() == 1
                {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(f.store.pending("local", "carol", "C1").unwrap().is_empty());
        assert_eq!(f.listener.starts.load(Ordering::SeqCst), 1);
        assert_eq!(
            f.store.pending("local", "aiko", "C1").unwrap()[0]
                .event
                .message
                .text,
            "live"
        );
        stop.cancel();
        tokio::time::timeout(std::time::Duration::from_secs(1), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(f.listener.stops.load(Ordering::SeqCst), 1);
        assert!(!f.listener.connected());
        let subject = Subject {
            service: "local".into(),
            channel_id: "C1".into(),
            thread_id: "live".into(),
            event_id: "C1:live".into(),
            self_user_id: "local-aiko".into(),
        };
        assert_eq!(
            Heartbeat::new(&f.store.workspace).state(
                &subject,
                "aiko",
                Utc::now().timestamp_millis() as f64 / 1000.0
            ),
            "unavailable"
        );
    }
}
