use super::store::{lock, text};
use crate::factory::{IntegrationEvent, IntegrationObserver};
use gb_ports::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};
pub(in crate::factory) struct LocalChat {
    root: PathBuf,
    actor: String,
    display: String,
}
impl LocalChat {
    pub(in crate::factory) fn new(root: PathBuf, actor: &str, display: &str) -> Self {
        Self {
            root: root.join("chat"),
            actor: actor.into(),
            display: display.into(),
        }
    }
    fn path(&self, c: &str) -> Result<PathBuf, ServiceError> {
        channel_path(&self.root, c)
    }
    fn messages(&self, c: &str) -> Result<Vec<Value>, ServiceError> {
        let p = self.path(c)?;
        let bytes = match fs::read(p) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            Err(_) => return Err(ServiceError::Failed),
        };
        let mut found: BTreeMap<String, Value> = BTreeMap::new();
        for raw in bytes.split(|b| *b == b'\n') {
            if let Some(mut v) = record(raw) {
                if v.get("reaction").is_some() {
                    if let Some(m) = found.get_mut(text(&v, "message_id")) {
                        m["reactions"]
                            .as_array_mut()
                            .ok_or(ServiceError::Failed)?
                            .push(json!({"reaction":v["reaction"],"author":v["author"]}));
                    }
                } else {
                    v["reactions"] = json!([]);
                    found.insert(text(&v, "message_id").into(), v);
                }
            }
        }
        let mut out: Vec<_> = found.into_values().collect();
        out.sort_by_key(|v| {
            event(c, v)
                .map(|e| e.message.position())
                .unwrap_or_default()
        });
        Ok(out)
    }
    fn append(&self, c: &str, v: &Value) -> Result<(), ServiceError> {
        let p = self.path(c)?;
        fs::create_dir_all(&self.root).map_err(|_| ServiceError::Failed)?;
        let mut b = serde_json::to_vec(v).map_err(|_| ServiceError::Failed)?;
        b.push(b'\n');
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(p)
            .and_then(|mut f| f.write_all(&b))
            .map_err(|_| ServiceError::Failed)
    }
}
fn channel_path(root: &Path, c: &str) -> Result<PathBuf, ServiceError> {
    if c.is_empty()
        || c == "."
        || c == ".."
        || !c
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
    {
        return Err(ServiceError::NotFound);
    }
    Ok(root.join(format!("{c}.jsonl")))
}
pub(super) fn mention_ids(s: &str) -> Vec<String> {
    let mut out = vec![];
    for part in s.split('@').skip(1) {
        let n: String = part
            .chars()
            .take_while(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '_' || *c == '-')
            .collect();
        if !n.is_empty() && !out.contains(&n) {
            out.push(n)
        }
    }
    out
}
fn record(b: &[u8]) -> Option<Value> {
    let v: Value = serde_json::from_slice(b).ok()?;
    v["message_id"].as_str()?;
    if v.get("reaction").is_none() {
        chrono::DateTime::parse_from_rfc3339(v["occurred_at"].as_str()?).ok()?;
    }
    Some(v)
}
fn event(c: &str, v: &Value) -> Option<ChatEvent> {
    let id = text(v, "message_id");
    let thread = if text(v, "thread_id").is_empty() {
        id
    } else {
        text(v, "thread_id")
    };
    Some(ChatEvent {
        event_id: format!("{c}:{id}"),
        message: ChatMessage {
            channel_id: c.into(),
            message_id: id.into(),
            thread_id: thread.into(),
            occurred_at: chrono::DateTime::parse_from_rfc3339(v["occurred_at"].as_str()?).ok()?,
            author_id: v["author"]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(str::to_owned),
            text: text(v, "text").into(),
            mentions: mention_ids(text(v, "text")),
            is_bot_message: v["bot"].as_bool().unwrap_or(false),
        },
        is_thread_reply: id != thread,
        status: WorkflowStatus::from_payload(&v["status"]),
    })
}
fn page(
    events: Vec<ChatEvent>,
    cursor: Option<String>,
    limit: usize,
) -> Result<ChatEventPage, ServiceError> {
    let cursor = cursor.unwrap_or_else(|| "0".into());
    if !cursor.bytes().all(|b| b.is_ascii_digit()) || cursor.is_empty() {
        return Err(ServiceError::InvalidContinuation);
    }
    let start = cursor
        .parse::<usize>()
        .map_err(|_| ServiceError::InvalidContinuation)?;
    let end = start.saturating_add(limit);
    Ok(ChatEventPage {
        cursor: (end < events.len()).then(|| end.to_string()),
        events: events.into_iter().skip(start).take(limit).collect(),
    })
}
impl ChatService for LocalChat {
    fn get_bot_identity(&self) -> ServiceFuture<'_, ChatIdentity> {
        Box::pin(async {
            Ok(ChatIdentity {
                user_id: self.actor.clone(),
                display_name: self.display.clone(),
                workspace: "local".into(),
            })
        })
    }
    fn check_credentials(&self) -> ServiceFuture<'_, Vec<CredentialCheck>> {
        Box::pin(async { Ok(vec![]) })
    }
    fn self_user_id(&self, a: &ActorName) -> String {
        a.0.clone()
    }
    fn parse_message_url(&self, s: &str) -> Result<ChatMessageRef, ServiceError> {
        let u = url::Url::parse(s).map_err(|_| ServiceError::NotFound)?;
        let (c, m) = u
            .path()
            .trim_matches('/')
            .split_once('/')
            .ok_or(ServiceError::NotFound)?;
        if u.scheme() != "local" || u.host_str() != Some("chat") || m.is_empty() {
            return Err(ServiceError::NotFound);
        }
        self.path(c)?;
        Ok(ChatMessageRef {
            channel_id: c.into(),
            message_id: m.into(),
            thread_id: u
                .query_pairs()
                .find(|(k, v)| k == "thread" && !v.is_empty())
                .map(|(_, v)| v.into_owned())
                .unwrap_or_else(|| m.into()),
        })
    }
    fn mentioned_user_ids(&self, s: &str) -> Vec<String> {
        mention_ids(s)
    }
    fn list_channel_events<'a>(
        &'a self,
        c: &'a str,
        q: ChannelEventsQuery,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            let events = self
                .messages(c)?
                .iter()
                .filter_map(|v| event(c, v))
                .filter(|e| {
                    !e.is_thread_reply
                        && q.since.is_none_or(|s| e.message.occurred_at >= s)
                        && q.until.is_none_or(|u| e.message.occurred_at <= u)
                })
                .collect();
            page(events, q.cursor, q.limit)
        })
    }
    fn list_thread_events<'a>(
        &'a self,
        c: &'a str,
        t: &'a str,
        cursor: Option<String>,
        limit: usize,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            let events: Vec<_> = self
                .messages(c)?
                .iter()
                .filter_map(|v| event(c, v))
                .filter(|e| e.message.thread_id == t)
                .collect();
            if !events.iter().any(|e| e.message.message_id == t) {
                return Err(ServiceError::NotFound);
            }
            page(events, cursor, limit)
        })
    }
    fn resolve_channel_id<'a>(&'a self, n: &'a str) -> ServiceFuture<'a, Option<String>> {
        Box::pin(async move {
            let n = n.trim().trim_start_matches('#');
            Ok(self.path(n).ok().filter(|p| p.exists()).map(|_| n.into()))
        })
    }
    fn post_message<'a>(
        &'a self,
        c: &'a str,
        t: &'a str,
        thread: Option<&'a str>,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, ChatPostResult> {
        Box::pin(async move {
            let _guard = lock()?;
            let id = uuid::Uuid::new_v4().simple().to_string();
            let mut time = chrono::Utc::now().fixed_offset();
            if let Some(latest) = self
                .messages(c)?
                .iter()
                .filter_map(|v| event(c, v))
                .map(|e| e.message.occurred_at)
                .max()
                && time <= latest
            {
                time = latest + chrono::Duration::microseconds(1)
            }
            let thread = thread.filter(|t| !t.is_empty()).unwrap_or(&id);
            let mut v = json!({"message_id":id,"thread_id":thread,"occurred_at":time.to_rfc3339(),"author":self.actor,"text":t,"bot":true});
            if let Some(s) = status {
                v["status"] = s.payload()
            }
            self.append(c, &v)?;
            Ok(ChatPostResult {
                channel_id: c.into(),
                message_id: id.clone(),
                thread_id: thread.into(),
                occurred_at: time,
            })
        })
    }
    fn add_reaction<'a>(
        &'a self,
        c: &'a str,
        id: &'a str,
        r: SemanticReaction,
    ) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let _guard = lock()?;
            let v = self
                .messages(c)?
                .into_iter()
                .find(|v| text(v, "message_id") == id)
                .ok_or(ServiceError::NotFound)?;
            let reaction = json!({"reaction":r,"author":self.actor});
            if !v["reactions"]
                .as_array()
                .is_some_and(|a| a.contains(&reaction))
            {
                self.append(
                    c,
                    &json!({"message_id":id,"reaction":r,"author":self.actor}),
                )?
            }
            Ok(())
        })
    }
    fn normalize_participant_text(&self, s: &str, labels: &BTreeMap<String, String>) -> String {
        regex::Regex::new(r"@([a-z0-9_-]+)")
            .expect("static regex")
            .replace_all(s, |c: &regex::Captures<'_>| {
                format!(
                    "@{}",
                    labels
                        .get(&c[1])
                        .map(String::as_str)
                        .unwrap_or("participant")
                )
            })
            .into_owned()
    }
    fn render_participant_text(&self, s: &str, labels: &BTreeMap<String, String>) -> String {
        regex::Regex::new(r"@([A-Za-z0-9_-]+)")
            .expect("static regex")
            .replace_all(s, |c: &regex::Captures<'_>| {
                format!(
                    "@{}",
                    labels
                        .iter()
                        .find(|(_, v)| **v == c[1])
                        .map(|(k, _)| k.as_str())
                        .unwrap_or(&c[1])
                )
            })
            .into_owned()
    }
}
pub(in crate::factory) struct LocalListener {
    root: PathBuf,
    activity: Arc<dyn Fn() + Send + Sync>,
    observer: IntegrationObserver,
    worker: Mutex<Option<Worker>>,
    queue: Arc<Mutex<Vec<ChatEvent>>>,
}
impl LocalListener {
    pub(in crate::factory) fn new(
        root: PathBuf,
        activity: Arc<dyn Fn() + Send + Sync>,
        observer: IntegrationObserver,
    ) -> Self {
        Self {
            root: root.join("chat"),
            activity,
            observer,
            worker: Mutex::new(None),
            queue: Arc::new(Mutex::new(vec![])),
        }
    }
}
fn files(root: &Path) -> Vec<PathBuf> {
    let mut out: Vec<_> = fs::read_dir(root)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
        .collect();
    out.sort();
    out
}
impl EventListener for LocalListener {
    fn connected(&self) -> bool {
        self.worker.lock().is_ok_and(|worker| {
            worker.as_ref().is_some_and(|worker| {
                worker.running.load(Ordering::SeqCst) && !worker.thread.is_finished()
            })
        })
    }
    fn auth_failed(&self) -> bool {
        false
    }
    fn start(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async {
            let mut handle = self.worker.lock().map_err(|_| ServiceError::Failed)?;
            if handle
                .as_ref()
                .is_some_and(|w| w.running.load(Ordering::SeqCst) && !w.thread.is_finished())
            {
                return Ok(());
            }
            let mut offsets: BTreeMap<PathBuf, usize> = files(&self.root)
                .into_iter()
                .filter_map(|p| fs::metadata(&p).ok().map(|m| (p, m.len() as usize)))
                .collect();
            let worker_running = Arc::new(AtomicBool::new(true));
            let (root, running, queue, activity, observer) = (
                self.root.clone(),
                worker_running.clone(),
                self.queue.clone(),
                self.activity.clone(),
                self.observer.clone(),
            );
            let worker_thread = thread::spawn(move || {
                struct RunningGuard(Arc<AtomicBool>);
                impl Drop for RunningGuard {
                    fn drop(&mut self) {
                        self.0.store(false, Ordering::SeqCst);
                    }
                }
                let _running_guard = RunningGuard(running.clone());
                while running.load(Ordering::SeqCst) {
                    thread::sleep(Duration::from_millis(100));
                    if !running.load(Ordering::SeqCst) {
                        break;
                    }
                    let mut arrived = vec![];
                    for p in files(&root) {
                        let offset = offsets.entry(p.clone()).or_default();
                        if let Ok(b) = fs::read(&p) {
                            if *offset > b.len() {
                                *offset = 0
                            }
                            let data = &b[*offset..];
                            if let Some(last) = data.iter().rposition(|b| *b == b'\n') {
                                for line in data[..=last].split(|b| *b == b'\n') {
                                    if let Some(v) = record(line) {
                                        if v.get("reaction").is_none()
                                            && let Some(e) = event(
                                                p.file_stem()
                                                    .and_then(|s| s.to_str())
                                                    .unwrap_or(""),
                                                &v,
                                            )
                                        {
                                            arrived.push(e)
                                        }
                                    } else if !line.iter().all(|b| b.is_ascii_whitespace()) {
                                        observer(IntegrationEvent::ReceiveRecordSkipped {
                                            provider: "local",
                                            code: "malformed_line",
                                        });
                                    }
                                }
                                *offset += last + 1;
                            }
                        } else {
                            observer(IntegrationEvent::ReceiveRecordSkipped {
                                provider: "local",
                                code: "unreadable_channel",
                            });
                        }
                    }
                    if !arrived.is_empty() {
                        if let Ok(mut q) = queue.lock() {
                            q.extend(arrived)
                        }
                        activity();
                    }
                }
            });
            *handle = Some(Worker {
                thread: worker_thread,
                running: worker_running,
            });
            drop(handle);
            (self.activity)();
            Ok(())
        })
    }
    fn stop(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async {
            self.stop_sync();
            Ok(())
        })
    }
    fn drain_events(&self) -> Vec<ChatEvent> {
        let mut out = self
            .queue
            .lock()
            .map(|mut q| std::mem::take(&mut *q))
            .unwrap_or_default();
        out.sort_by_key(|e| e.message.position());
        out
    }
}
struct Worker {
    thread: JoinHandle<()>,
    running: Arc<AtomicBool>,
}
fn stop_worker(worker: Worker, timeout: Duration) {
    worker.running.store(false, Ordering::SeqCst);
    if worker.thread.thread().id() == thread::current().id() {
        return;
    }
    let deadline = std::time::Instant::now() + timeout;
    while !worker.thread.is_finished() {
        let remaining = deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            return;
        }
        thread::sleep(remaining.min(Duration::from_millis(1)));
    }
    let _ = worker.thread.join();
}
impl LocalListener {
    fn stop_sync(&self) {
        self.stop_with_timeout(Duration::from_secs(2));
    }
    fn stop_with_timeout(&self, timeout: Duration) {
        if let Ok(mut handle) = self.worker.lock()
            && let Some(worker) = handle.take()
        {
            stop_worker(worker, timeout);
        }
    }
}
impl Drop for LocalListener {
    fn drop(&mut self) {
        self.stop_sync()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_frozen_damaged_record_is_skipped_alone() {
        for bad in [
            "not json",
            "null",
            "[1,2]",
            r#"{"text":"no id","occurred_at":"2026-10-01T00:00:00+00:00"}"#,
            r#"{"message_id":"x","occurred_at":"yesterday"}"#,
            r#"{"message_id":null,"occurred_at":"2026-10-01T00:00:00+00:00"}"#,
            r#"{"reaction":"ack"}"#,
            r#"{"message_id":"x","occurred_at":"2026-10-01T00:00:00"}"#,
        ] {
            assert!(record(bad.as_bytes()).is_none(), "{bad}");
        }
    }
    #[tokio::test]
    async fn blocked_worker_shutdown_finishes_within_frozen_two_second_bound() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir_all(root.path().join("chat")).unwrap();
        let (entered_tx, entered_rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let release = Mutex::new(release_rx);
        let listener = Arc::new(LocalListener::new(
            root.path().into(),
            Arc::new(|| {}),
            Arc::new(move |_| {
                entered_tx.send(()).unwrap();
                release.lock().unwrap().recv().unwrap();
            }),
        ));
        listener.start().await.unwrap();
        fs::write(root.path().join("chat/events.jsonl"), b"broken\n").unwrap();
        entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let (stopped_tx, stopped_rx) = std::sync::mpsc::channel();
        let stopping = listener.clone();
        let stop = thread::spawn(move || {
            stopping.stop_sync();
            stopped_tx.send(()).unwrap();
        });
        let bounded = stopped_rx.recv_timeout(Duration::from_millis(2100)).is_ok();
        release_tx.send(()).unwrap();
        stop.join().unwrap();
        assert!(
            bounded,
            "a blocked worker exceeded the frozen two-second shutdown bound"
        );
    }
    fn listener(root: &Path) -> Arc<LocalListener> {
        Arc::new(LocalListener::new(
            root.into(),
            Arc::new(|| {}),
            Arc::new(|_| {}),
        ))
    }
    fn blocked_worker(
        listener: &LocalListener,
    ) -> (
        std::sync::mpsc::Sender<()>,
        std::sync::mpsc::Receiver<bool>,
        Arc<AtomicBool>,
    ) {
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let (exited_tx, exited_rx) = std::sync::mpsc::channel();
        let running = Arc::new(AtomicBool::new(true));
        let flag = running.clone();
        let worker = thread::spawn(move || {
            release_rx.recv().unwrap();
            exited_tx.send(flag.load(Ordering::SeqCst)).unwrap();
        });
        *listener.worker.lock().unwrap() = Some(Worker {
            thread: worker,
            running: running.clone(),
        });
        (release_tx, exited_rx, running)
    }
    #[tokio::test]
    async fn detached_worker_cannot_be_revived_by_next_start() {
        let root = tempfile::tempdir().unwrap();
        let listener = listener(root.path());
        let (release, exited, old_running) = blocked_worker(&listener);
        listener.stop_with_timeout(Duration::from_millis(10));
        assert!(!listener.connected());
        assert!(!old_running.load(Ordering::SeqCst));
        listener.start().await.unwrap();
        assert!(!Arc::ptr_eq(
            &old_running,
            &listener.worker.lock().unwrap().as_ref().unwrap().running
        ));
        release.send(()).unwrap();
        assert!(!exited.recv_timeout(Duration::from_secs(1)).unwrap());
        assert!(listener.connected());
        listener.stop_sync();
    }
    #[test]
    fn concurrent_start_waits_for_stop_of_previous_generation() {
        let root = tempfile::tempdir().unwrap();
        let listener = listener(root.path());
        let (release, exited, _) = blocked_worker(&listener);
        let stopping = listener.clone();
        let stop = thread::spawn(move || stopping.stop_with_timeout(Duration::from_millis(500)));
        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        while listener.worker.try_lock().is_ok() {
            assert!(std::time::Instant::now() < deadline);
            thread::yield_now();
        }
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let starting = listener.clone();
        let start = thread::spawn(move || {
            tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap()
                .block_on(starting.start())
                .unwrap();
            started_tx.send(()).unwrap();
        });
        assert!(started_rx.recv_timeout(Duration::from_millis(10)).is_err());
        release.send(()).unwrap();
        assert!(!exited.recv_timeout(Duration::from_secs(1)).unwrap());
        stop.join().unwrap();
        started_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        start.join().unwrap();
        assert!(listener.connected());
        listener.stop_sync();
    }
    #[test]
    fn a_worker_can_stop_itself_without_joining_its_own_thread() {
        let (flag_tx, flag_rx) = std::sync::mpsc::channel();
        let (go_tx, go_rx) = std::sync::mpsc::channel();
        let (handle_tx, handle_rx) = std::sync::mpsc::channel();
        let running = Arc::new(AtomicBool::new(true));
        let flag = running.clone();
        let worker = thread::spawn(move || {
            go_rx.recv().unwrap();
            stop_worker(
                Worker {
                    thread: handle_rx.recv().unwrap(),
                    running: flag.clone(),
                },
                Duration::from_millis(10),
            );
            flag_tx.send(flag.load(Ordering::SeqCst)).unwrap();
        });
        handle_tx.send(worker).unwrap();
        go_tx.send(()).unwrap();
        assert!(!flag_rx.recv_timeout(Duration::from_secs(1)).unwrap());
    }
}
