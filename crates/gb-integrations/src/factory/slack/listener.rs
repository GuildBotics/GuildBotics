use super::{
    Form, IntegrationEvent, IntegrationObserver, SlackFailure, SlackFuture, SlackTransport, answer,
    events::{event, text},
};
use futures_util::{SinkExt, StreamExt};
use gb_ports::{ChatEvent, EventListener, ServiceFuture};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{sync::watch, task::JoinHandle};

pub(super) enum SocketFrame {
    Text(String),
    Pong(Vec<u8>),
}

pub(super) trait Socket: Send {
    fn receive(&mut self) -> SlackFuture<'_, Option<SocketFrame>>;
    fn send(&mut self, value: String) -> SlackFuture<'_, ()>;
    fn ping(&mut self, payload: Vec<u8>) -> SlackFuture<'_, ()>;
    fn close(&mut self) -> SlackFuture<'_, ()>;
}
pub(super) trait SocketTransport: Send + Sync {
    fn connect<'a>(&'a self, url: &'a str) -> SlackFuture<'a, Box<dyn Socket>>;
}
pub(super) struct WebSocketTransport;
struct WebSocket(
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
);
impl SocketTransport for WebSocketTransport {
    fn connect<'a>(&'a self, url: &'a str) -> SlackFuture<'a, Box<dyn Socket>> {
        Box::pin(async move {
            let (ws, _) = tokio_tungstenite::connect_async_with_config(url, None, true)
                .await
                .map_err(|_| SlackFailure::Unavailable)?;
            Ok(Box::new(WebSocket(ws)) as Box<dyn Socket>)
        })
    }
}
impl Socket for WebSocket {
    fn receive(&mut self) -> SlackFuture<'_, Option<SocketFrame>> {
        Box::pin(async move {
            loop {
                match self.0.next().await {
                    Some(Ok(tokio_tungstenite::tungstenite::Message::Text(t))) => {
                        return Ok(Some(SocketFrame::Text(t.to_string())));
                    }
                    Some(Ok(tokio_tungstenite::tungstenite::Message::Binary(t))) => {
                        return Ok(Some(SocketFrame::Text(
                            String::from_utf8_lossy(&t).into_owned(),
                        )));
                    }
                    Some(Ok(tokio_tungstenite::tungstenite::Message::Pong(payload))) => {
                        return Ok(Some(SocketFrame::Pong(payload.to_vec())));
                    }
                    Some(Ok(tokio_tungstenite::tungstenite::Message::Close(_))) | None => {
                        return Ok(None);
                    }
                    Some(Err(_)) => return Err(SlackFailure::Unavailable),
                    _ => {}
                }
            }
        })
    }
    fn send(&mut self, value: String) -> SlackFuture<'_, ()> {
        Box::pin(async move {
            self.0
                .send(tokio_tungstenite::tungstenite::Message::Text(value.into()))
                .await
                .map_err(|_| SlackFailure::Unavailable)
        })
    }
    fn ping(&mut self, payload: Vec<u8>) -> SlackFuture<'_, ()> {
        Box::pin(async move {
            self.0
                .send(tokio_tungstenite::tungstenite::Message::Ping(
                    payload.into(),
                ))
                .await
                .map_err(|_| SlackFailure::Unavailable)
        })
    }
    fn close(&mut self) -> SlackFuture<'_, ()> {
        Box::pin(async move {
            self.0
                .close(None)
                .await
                .map_err(|_| SlackFailure::Unavailable)
        })
    }
}
struct State {
    connected: AtomicBool,
    auth_failed: AtomicBool,
    queue: Mutex<Vec<ChatEvent>>,
    activity: Arc<dyn Fn() + Send + Sync>,
    observer: IntegrationObserver,
    attempts: AtomicU64,
    received: AtomicU64,
    acks: AtomicU64,
}
impl State {
    fn connected(&self, value: bool) {
        if self.connected.swap(value, Ordering::AcqRel) != value {
            (self.observer)(if value {
                IntegrationEvent::ReceiveConnected { provider: "slack" }
            } else {
                IntegrationEvent::ReceiveDisconnected { provider: "slack" }
            });
        }
        (self.activity)();
    }
}
struct Running {
    cancel: watch::Sender<bool>,
    task: JoinHandle<()>,
}
impl Drop for Running {
    fn drop(&mut self) {
        self.task.abort();
    }
}
pub(in crate::factory) struct SlackListener {
    state: Arc<State>,
    token: String,
    member: String,
    base: String,
    http: Arc<dyn SlackTransport>,
    sockets: Arc<dyn SocketTransport>,
    running: tokio::sync::Mutex<Option<Running>>,
}
impl SlackListener {
    pub(in crate::factory) fn new(
        token: String,
        base: String,
        http: Arc<dyn SlackTransport>,
        activity: Arc<dyn Fn() + Send + Sync>,
        observer: IntegrationObserver,
    ) -> Self {
        Self::with_socket(
            token,
            base,
            http,
            Arc::new(WebSocketTransport),
            activity,
            observer,
        )
    }
    pub(in crate::factory) fn with_member(mut self, member: String) -> Self {
        self.member = member;
        self
    }
    pub(super) fn with_socket(
        token: String,
        base: String,
        http: Arc<dyn SlackTransport>,
        sockets: Arc<dyn SocketTransport>,
        activity: Arc<dyn Fn() + Send + Sync>,
        observer: IntegrationObserver,
    ) -> Self {
        Self {
            state: Arc::new(State {
                connected: AtomicBool::new(false),
                auth_failed: AtomicBool::new(false),
                queue: Mutex::new(Vec::new()),
                activity,
                observer,
                attempts: AtomicU64::new(0),
                received: AtomicU64::new(0),
                acks: AtomicU64::new(0),
            }),
            token,
            member: String::new(),
            base: base.trim_end_matches('/').into(),
            http,
            sockets,
            running: tokio::sync::Mutex::new(None),
        }
    }
}
struct Connected(Arc<State>);
impl Drop for Connected {
    fn drop(&mut self) {
        self.0.connected(false);
    }
}
impl EventListener for SlackListener {
    fn connected(&self) -> bool {
        self.state.connected.load(Ordering::Acquire)
    }
    fn auth_failed(&self) -> bool {
        self.state.auth_failed.load(Ordering::Acquire)
    }
    fn start(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async move {
            let mut running = self.running.lock().await;
            if self.auth_failed() || running.as_ref().is_some_and(|r| !r.task.is_finished()) {
                return Ok(());
            }
            let (cancel, mut stop) = watch::channel(false);
            let (state, token, base, http, sockets) = (
                self.state.clone(),
                self.token.clone(),
                self.base.clone(),
                self.http.clone(),
                self.sockets.clone(),
            );
            let member = self.member.clone();
            let task = tokio::spawn(async move {
                let _guard = Connected(state.clone());
                loop {
                    let attempt = state.attempts.fetch_add(1, Ordering::Relaxed) + 1;
                    if attempt > 1 {
                        (state.observer)(IntegrationEvent::ReceiveReconnecting {
                            provider: "slack",
                            attempt,
                        });
                    }
                    let connection = async {
                        if token.is_empty() {
                            return Err(SlackFailure::Auth("not_configured"));
                        }
                        let payload = answer(
                            http.post(&base, "apps.connections.open", &token, Form::new())
                                .await?,
                        )?;
                        let url = text(&payload, "url");
                        if url.is_empty() {
                            return Err(SlackFailure::InvalidResult);
                        }
                        sockets.connect(url).await
                    };
                    let opened = tokio::select! { _=stop.changed()=>break, result=tokio::time::timeout(Duration::from_secs(10),connection)=>result.unwrap_or(Err(SlackFailure::Unavailable)) };
                    match opened {
                        Err(SlackFailure::Auth(code)) if code != "unauthorized" => {
                            state.auth_failed.store(true, Ordering::Release);
                            (state.observer)(IntegrationEvent::ReceiveAuthFailed {
                                provider: "slack",
                                code,
                                member: member.clone(),
                            });
                            (state.activity)();
                            break;
                        }
                        Ok(mut socket) => {
                            state.connected(true);
                            let mut ping_at = tokio::time::Instant::now() + Duration::from_secs(20);
                            let mut pending_pong: Option<(Vec<u8>, tokio::time::Instant)> = None;
                            let mut ping_id = 0u64;
                            loop {
                                let deadline = pending_pong
                                    .as_ref()
                                    .map_or(ping_at, |(_, deadline)| *deadline);
                                let frame = tokio::select! {
                                    biased;
                                    _=stop.changed()=>break,
                                    _=tokio::time::sleep_until(deadline)=>{
                                        if pending_pong.is_some() {
                                            break;
                                        }
                                        ping_id += 1;
                                        let payload = ping_id.to_be_bytes().to_vec();
                                        let pong_deadline = tokio::time::Instant::now() + Duration::from_secs(20);
                                        let result = tokio::select! {
                                            _=stop.changed()=>break,
                                            result=tokio::time::timeout_at(pong_deadline, socket.ping(payload.clone()))=>result,
                                        };
                                        if !matches!(result, Ok(Ok(()))) {
                                            break;
                                        }
                                        pending_pong = Some((payload, pong_deadline));
                                        continue;
                                    },
                                    frame=socket.receive()=>frame,
                                };
                                let frame = match frame {
                                    Ok(Some(SocketFrame::Text(frame))) => frame,
                                    Ok(Some(SocketFrame::Pong(payload))) => {
                                        if pending_pong
                                            .as_ref()
                                            .is_some_and(|(expected, _)| *expected == payload)
                                        {
                                            pending_pong = None;
                                            ping_at = tokio::time::Instant::now()
                                                + Duration::from_secs(20);
                                        }
                                        continue;
                                    }
                                    _ => break,
                                };
                                let Ok(payload) = serde_json::from_str::<Value>(&frame) else {
                                    continue;
                                };
                                if let Some(item) = incoming(&payload) {
                                    state.received.fetch_add(1, Ordering::Relaxed);
                                    state.queue.lock().unwrap().push(item);
                                    (state.activity)();
                                }
                                if let Some(id) = payload
                                    .get("envelope_id")
                                    .and_then(Value::as_str)
                                    .filter(|s| !s.is_empty())
                                {
                                    let ack = socket.send(json!({"envelope_id":id}).to_string());
                                    let ack_deadline = deadline
                                        .min(tokio::time::Instant::now() + Duration::from_secs(10));
                                    tokio::select! {
                                        _=stop.changed()=>break,
                                        result=tokio::time::timeout_at(ack_deadline, ack)=>{
                                            if matches!(result, Ok(Ok(()))) {
                                                state.acks.fetch_add(1,Ordering::Relaxed);
                                            }
                                        }
                                    }
                                }
                            }
                            state.connected(false);
                            let _ =
                                tokio::time::timeout(Duration::from_secs(1), socket.close()).await;
                        }
                        Err(_) => {}
                    }
                    if *stop.borrow() {
                        break;
                    }
                    tokio::select! {_=stop.changed()=>break,_=tokio::time::sleep(Duration::from_secs(1))=>{}}
                }
            });
            *running = Some(Running { cancel, task });
            Ok(())
        })
    }
    fn stop(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async move {
            let mut running = self.running.lock().await;
            if let Some(mut run) = running.take() {
                let _ = run.cancel.send(true);
                if tokio::time::timeout(Duration::from_secs(2), &mut run.task)
                    .await
                    .is_err()
                {
                    run.task.abort();
                    let _ = (&mut run.task).await;
                }
            }
            self.state.connected(false);
            let attempts = self.state.attempts.load(Ordering::Relaxed);
            (self.state.observer)(IntegrationEvent::ReceiveStopped {
                provider: "slack",
                connect_attempts: attempts,
                reconnects: attempts.saturating_sub(1),
                received_events: self.state.received.load(Ordering::Relaxed),
                acks_sent: self.state.acks.load(Ordering::Relaxed),
            });
            Ok(())
        })
    }
    fn drain_events(&self) -> Vec<ChatEvent> {
        let mut events = std::mem::take(&mut *self.state.queue.lock().unwrap());
        events.sort_by_key(|e| e.message.position());
        events
    }
}
fn incoming(envelope: &Value) -> Option<ChatEvent> {
    if text(envelope, "type") != "events_api" {
        return None;
    }
    let raw = &envelope["payload"]["event"];
    if text(raw, "type") != "message" {
        return None;
    }
    let channel = text(raw, "channel");
    if channel.is_empty() {
        return None;
    }
    event(channel, raw).ok().flatten()
}
