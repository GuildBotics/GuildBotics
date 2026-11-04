//! Frozen origin/python-host Slack service, message-event and socket contracts.
use super::listener::{Socket, SocketTransport};
use super::*;
use serde_json::json;
use std::{
    collections::VecDeque,
    sync::atomic::{AtomicUsize, Ordering},
};

#[derive(Default)]
struct MockHttp {
    replies: Mutex<VecDeque<Result<Value, SlackFailure>>>,
    calls: Mutex<Vec<(String, String, Form)>>,
}
impl MockHttp {
    fn with(replies: Vec<Value>) -> Arc<Self> {
        Arc::new(Self {
            replies: Mutex::new(replies.into_iter().map(Ok).collect()),
            ..Self::default()
        })
    }
}
impl SlackTransport for MockHttp {
    fn post<'a>(
        &'a self,
        _: &'a str,
        method: &'a str,
        token: &'a str,
        form: Form,
    ) -> SlackFuture<'a, Value> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push((method.into(), token.into(), form));
            self.replies
                .lock()
                .unwrap()
                .pop_front()
                .expect("unexpected Web API call")
        })
    }
}
fn service(http: Arc<MockHttp>) -> SlackChatService {
    SlackChatService::new(
        "synthetic-bot".into(),
        "synthetic-app".into(),
        BASE_URL.into(),
        http,
        Arc::new(|_| {}),
    )
}
fn status() -> WorkflowStatus {
    WorkflowStatus::from_payload(
        &json!({"kind":"workflow_error","reason":"failed","person_id":"alice","run_id":"r1"}),
    )
    .unwrap()
}

#[tokio::test]
async fn web_requests_and_metadata() {
    let http = MockHttp::with(vec![
        json!({"ok":true,"user_id":"U1","user":"bot","team":"team"}),
        json!({"ok":true,"messages":[{"ts":"2.000001","thread_ts":"1.0","user":"U1","text":"<@U2> <@U2|name>"}],"response_metadata":{"next_cursor":"next"}}),
        json!({"ok":true,"messages":[]}),
        json!({"ok":true,"ts":"3.000001"}),
        json!({"ok":true}),
        json!({"ok":false,"error":"already_reacted"}),
    ]);
    let chat = service(http.clone());
    assert_eq!(chat.get_bot_identity().await.unwrap().user_id, "U1");
    let page = chat
        .list_channel_events(
            "C1",
            ChannelEventsQuery {
                cursor: Some("first".into()),
                since: Some(timestamp("1.000001").unwrap().with_timezone(&chrono::Utc)),
                until: Some(timestamp("9.0").unwrap().with_timezone(&chrono::Utc)),
                limit: 10,
            },
        )
        .await
        .unwrap();
    assert_eq!(page.cursor.as_deref(), Some("next"));
    assert_eq!(page.events[0].message.mentions, vec!["U2"]);
    assert!(page.events[0].is_thread_reply);
    chat.list_thread_events("C1", "1.0", Some("page2".into()), 2)
        .await
        .unwrap();
    let posted = chat
        .post_message("C1", "hello", Some("1.0"), Some(&status()))
        .await
        .unwrap();
    assert_eq!(posted.thread_id, "1.0");
    assert_eq!(posted.occurred_at.timestamp_subsec_micros(), 1);
    chat.add_reaction("C1", "3.000001", SemanticReaction::Support)
        .await
        .unwrap();
    chat.add_reaction("C1", "3.000001", SemanticReaction::Support)
        .await
        .unwrap();
    let calls = http.calls.lock().unwrap();
    assert_eq!(calls[0].0, "auth.test");
    assert_eq!(calls[1].2["oldest"], "1.000001");
    assert_eq!(calls[1].2["latest"], "9.000000");
    assert_eq!(calls[1].2["inclusive"], "true");
    assert_eq!(calls[1].2["include_all_metadata"], "true");
    assert_eq!(calls[2].2["cursor"], "page2");
    assert_eq!(calls[3].2["thread_ts"], "1.0");
    assert_eq!(
        serde_json::from_str::<Value>(&calls[3].2["metadata"]).unwrap(),
        metadata(&status())
    );
    assert_eq!(calls[4].2["name"], "heart");
}
#[tokio::test]
async fn channel_pagination_cache_and_narrow_scope_probe() {
    let http = MockHttp::with(vec![
        json!({"ok":true,"channels":[],"response_metadata":{"next_cursor":"two"}}),
        json!({"ok":true,"channels":[{"id":"C1","name":"dev","name_normalized":"dev-normalized"}]}),
        json!({"ok":true}),
    ]);
    let chat = service(http.clone());
    assert_eq!(
        chat.resolve_channel_id(" #dev-normalized ")
            .await
            .unwrap()
            .as_deref(),
        Some("C1")
    );
    assert_eq!(
        chat.resolve_channel_id("dev-normalized")
            .await
            .unwrap()
            .as_deref(),
        Some("C1")
    );
    assert_eq!(chat.resolve_channel_id(" # ").await.unwrap(), None);
    chat.probe_read_scopes().await.unwrap();
    let calls = http.calls.lock().unwrap();
    assert_eq!(calls.len(), 3);
    assert_eq!(calls[1].2["cursor"], "two");
    assert_eq!(calls[2].2["limit"], "1");
}
#[tokio::test]
async fn strict_answers_and_error_taxonomy() {
    for bad in [
        json!({}),
        json!({"messages":null}),
        json!({"messages":[1]}),
        json!({"messages":[{"ts":"1.0"},{"ts":"bad"}]}),
        json!({"messages":[],"response_metadata":[]}),
    ] {
        let mut p = bad;
        p["ok"] = json!(true);
        let chat = service(MockHttp::with(vec![p]));
        assert_eq!(
            chat.list_channel_events("C", ChannelEventsQuery::default())
                .await,
            Err(ServiceError::InvalidResult)
        );
    }
    for bad in [json!(null), json!(42), json!("")] {
        let chat = service(MockHttp::with(vec![json!({"ok":true,"user_id":bad})]));
        assert_eq!(
            chat.get_bot_identity().await,
            Err(ServiceError::InvalidResult)
        );
    }
    for bad in [json!(null), json!(42), json!("bad"), json!("")] {
        let chat = service(MockHttp::with(vec![json!({"ok":true,"ts":bad})]));
        assert_eq!(
            chat.post_message("C", "x", None, None).await,
            Err(ServiceError::InvalidResult)
        );
    }
    let chat = service(MockHttp::with(vec![
        json!({"ok":false,"error":"thread_not_found"}),
        json!({"ok":false,"error":"not_in_channel"}),
        json!({"ok":true,"channels":null}),
    ]));
    assert_eq!(
        chat.list_thread_events("C", "1", None, 100).await,
        Err(ServiceError::NotFound)
    );
    assert_eq!(
        chat.add_reaction("C", "1", SemanticReaction::Ack).await,
        Err(ServiceError::Failed)
    );
    assert_eq!(
        chat.resolve_channel_id("x").await,
        Err(ServiceError::InvalidResult)
    );
    assert_eq!(answer(json!([])), Err(SlackFailure::InvalidResult));
}
#[tokio::test]
async fn credentials_checked_independently_without_values() {
    let http = MockHttp::with(vec![
        json!({"ok":false,"error":"invalid_auth","detail":"synthetic-bot"}),
        json!({"ok":true}),
    ]);
    let chat = service(http.clone());
    let checks = chat.check_credentials().await.unwrap();
    assert_eq!(checks[0].status, CredentialStatus::Failed);
    assert_eq!(checks[1].status, CredentialStatus::Ok);
    assert!(!checks[0].error.contains("synthetic"));
    {
        let calls = http.calls.lock().unwrap();
        assert_eq!(calls[0].1, "synthetic-bot");
        assert_eq!(calls[1].1, "synthetic-app");
    }
    let empty = SlackChatService::new(
        "".into(),
        "".into(),
        BASE_URL.into(),
        http,
        Arc::new(|_| {}),
    );
    assert!(
        empty
            .check_credentials()
            .await
            .unwrap()
            .iter()
            .all(|c| c.status == CredentialStatus::Unconfigured)
    );
}
#[test]
fn participant_mentions_and_permalinks() {
    let chat = service(MockHttp::with(vec![]));
    assert_eq!(
        chat.mentioned_user_ids("<@U1> <@U2|name> <@U1>"),
        vec!["U1", "U2"]
    );
    let labels = BTreeMap::from([
        ("U1".into(), "person-one".into()),
        ("U2".into(), "agent_2".into()),
    ]);
    assert_eq!(
        chat.normalize_participant_text("<@U1|name> <@U9>", &labels),
        "@person-one @participant"
    );
    assert_eq!(
        chat.render_participant_text("@PERSON-ONE @agent_2 @unknown", &labels),
        "<@U1> agent_2 @unknown"
    );
    let r = chat
        .parse_message_url("https://team.slack.com/archives/C1/p1234567000001?thread_ts=1.0")
        .unwrap();
    assert_eq!(r.message_id, "1234567.000001");
    assert_eq!(r.thread_id, "1.0");
    for u in [
        "https://team.slack.com/x/C1/p1234567000001",
        "https://team.slack.com/archives/C1/pabc",
        "bad",
    ] {
        assert_eq!(chat.parse_message_url(u), Err(ServiceError::NotFound));
    }
}
#[test]
fn timestamp_event_subtypes_and_status() {
    for ts in ["1.000001", "1783154640.123456", "-1.123456", "0.000000"] {
        assert_eq!(
            time_ts(timestamp(ts).unwrap().with_timezone(&chrono::Utc)),
            ts
        );
    }
    for subtype in [
        "",
        "bot_message",
        "file_share",
        "me_message",
        "thread_broadcast",
    ] {
        let e = event(
            "C",
            &json!({"ts":"2.0","thread_ts":"1.0","subtype":subtype,"metadata":metadata(&status())}),
        )
        .unwrap()
        .unwrap();
        assert!(e.is_thread_reply);
        assert_eq!(e.status, Some(status()));
        assert_eq!(e.message.is_bot_message, subtype == "bot_message");
    }
    for subtype in ["channel_join", "message_changed", "message_deleted"] {
        assert!(event("C", &json!({"subtype":subtype})).unwrap().is_none());
    }
    for ts in ["", "bad", "1.bad"] {
        assert!(event("C", &json!({"ts":ts})).is_err());
    }
    assert_eq!(
        event("C", &json!({"ts":"1.0","metadata":{"event_type":"other"}}))
            .unwrap()
            .unwrap()
            .status,
        None
    );
}

struct MockSocket {
    frames: VecDeque<Option<String>>,
    acks: Arc<Mutex<Vec<String>>>,
    closed: Arc<AtomicUsize>,
    fail_acks: bool,
}
impl Socket for MockSocket {
    fn receive(&mut self) -> SlackFuture<'_, Option<String>> {
        Box::pin(async move {
            match self.frames.pop_front() {
                Some(v) => Ok(v),
                None => std::future::pending().await,
            }
        })
    }
    fn send(&mut self, value: String) -> SlackFuture<'_, ()> {
        Box::pin(async move {
            self.acks.lock().unwrap().push(value);
            if self.fail_acks {
                Err(SlackFailure::Unavailable)
            } else {
                Ok(())
            }
        })
    }
    fn close(&mut self) -> SlackFuture<'_, ()> {
        Box::pin(async move {
            self.closed.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })
    }
}
#[derive(Default)]
struct MockSockets {
    queue: Mutex<VecDeque<MockSocket>>,
    connections: AtomicUsize,
}
impl SocketTransport for MockSockets {
    fn connect<'a>(&'a self, _: &'a str) -> SlackFuture<'a, Box<dyn Socket>> {
        Box::pin(async move {
            self.connections.fetch_add(1, Ordering::SeqCst);
            Ok(Box::new(self.queue.lock().unwrap().pop_front().unwrap()) as Box<dyn Socket>)
        })
    }
}
fn envelope(id: &str, ts: &str) -> String {
    json!({"type":"events_api","envelope_id":id,"payload":{"event":{"type":"message","channel":"C","ts":ts}}}).to_string()
}
async fn until(mut condition: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !condition() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}
#[tokio::test]
async fn socket_ack_reconnect_queue_and_stop() {
    let acks = Arc::new(Mutex::new(Vec::new()));
    let closed = Arc::new(AtomicUsize::new(0));
    let sockets = Arc::new(MockSockets::default());
    for frames in [
        vec![
            Some(envelope("second", "2.0")),
            Some("bad JSON".into()),
            Some(envelope("invalid", "bad")),
            Some(json!({"type":"hello","envelope_id":"hello"}).to_string()),
            None,
        ],
        vec![Some(envelope("first", "1.0"))],
    ] {
        sockets.queue.lock().unwrap().push_back(MockSocket {
            frames: frames.into(),
            acks: acks.clone(),
            closed: closed.clone(),
            fail_acks: false,
        });
    }
    let http = MockHttp::with(vec![
        json!({"ok":true,"url":"wss://synthetic"}),
        json!({"ok":true,"url":"wss://synthetic"}),
    ]);
    let activities = Arc::new(AtomicUsize::new(0));
    let count = activities.clone();
    let listener = SlackListener::with_socket(
        "synthetic".into(),
        BASE_URL.into(),
        http,
        sockets.clone(),
        Arc::new(move || {
            count.fetch_add(1, Ordering::SeqCst);
        }),
        Arc::new(|_| {}),
    );
    listener.start().await.unwrap();
    listener.start().await.unwrap();
    until(|| acks.lock().unwrap().len() == 4).await;
    assert!(listener.connected());
    assert_eq!(sockets.connections.load(Ordering::SeqCst), 2);
    listener.stop().await.unwrap();
    listener.stop().await.unwrap();
    assert!(!listener.connected());
    assert_eq!(closed.load(Ordering::SeqCst), 2);
    let events = listener.drain_events();
    assert_eq!(
        events
            .iter()
            .map(|e| e.message.message_id.as_str())
            .collect::<Vec<_>>(),
        vec!["1.0", "2.0"]
    );
    assert!(listener.drain_events().is_empty());
    assert!(activities.load(Ordering::SeqCst) >= 6);
    let ack_ids: Vec<_> = acks
        .lock()
        .unwrap()
        .iter()
        .map(|a| {
            serde_json::from_str::<Value>(a).unwrap()["envelope_id"]
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect();
    assert_eq!(ack_ids, vec!["second", "invalid", "hello", "first"]);
}
#[tokio::test]
async fn permanent_socket_auth_never_retries_or_restarts() {
    let http = MockHttp::with(vec![json!({"ok":false,"error":"invalid_auth"})]);
    let listener = SlackListener::with_socket(
        "synthetic".into(),
        BASE_URL.into(),
        http.clone(),
        Arc::new(MockSockets::default()),
        Arc::new(|| {}),
        Arc::new(|_| {}),
    );
    listener.start().await.unwrap();
    until(|| listener.auth_failed()).await;
    listener.stop().await.unwrap();
    listener.start().await.unwrap();
    assert!(listener.auth_failed());
    assert!(!listener.connected());
    assert_eq!(http.calls.lock().unwrap().len(), 1);
}
struct BlockedHttp;
impl SlackTransport for BlockedHttp {
    fn post<'a>(&'a self, _: &'a str, _: &'a str, _: &'a str, _: Form) -> SlackFuture<'a, Value> {
        Box::pin(std::future::pending())
    }
}
#[tokio::test]
async fn stop_cancels_blocked_connection() {
    let listener = SlackListener::with_socket(
        "synthetic".into(),
        BASE_URL.into(),
        Arc::new(BlockedHttp),
        Arc::new(MockSockets::default()),
        Arc::new(|| {}),
        Arc::new(|_| {}),
    );
    listener.start().await.unwrap();
    tokio::task::yield_now().await;
    tokio::time::timeout(Duration::from_millis(100), listener.stop())
        .await
        .unwrap()
        .unwrap();
    assert!(!listener.connected());
}

#[tokio::test]
async fn missing_scope_preserves_only_safe_scope_arguments() {
    let payload = json!({"ok":false,"error":"missing_scope","needed":"channels:history,groups:history","provided":"chat:write,synthetic-secret,https://example.test"});
    let chat = service(MockHttp::with(vec![
        payload.clone(),
        payload,
        json!({"ok":true}),
    ]));
    assert_eq!(
        chat.list_channel_events("C", ChannelEventsQuery::default())
            .await,
        Err(ServiceError::MissingScope {
            needed: "channels:history,groups:history".into(),
            provided: "chat:write".into()
        })
    );
    let checks = chat.check_credentials().await.unwrap();
    assert_eq!(
        checks[0].error,
        "integrations.slack.chat_service.missing_scope"
    );
    assert_eq!(
        checks[0].error_args["needed"],
        "channels:history,groups:history"
    );
    assert_eq!(checks[0].error_args["provided"], "chat:write");
}
struct CountingBlockedHttp(AtomicUsize);
impl SlackTransport for CountingBlockedHttp {
    fn post<'a>(&'a self, _: &'a str, _: &'a str, _: &'a str, _: Form) -> SlackFuture<'a, Value> {
        Box::pin(async move {
            self.0.fetch_add(1, Ordering::SeqCst);
            std::future::pending().await
        })
    }
}
#[tokio::test(start_paused = true)]
async fn socket_open_timeout_retries_and_stop_cancels_backoff() {
    let http = Arc::new(CountingBlockedHttp(AtomicUsize::new(0)));
    let listener = SlackListener::with_socket(
        "synthetic".into(),
        BASE_URL.into(),
        http.clone(),
        Arc::new(MockSockets::default()),
        Arc::new(|| {}),
        Arc::new(|_| {}),
    );
    listener.start().await.unwrap();
    tokio::task::yield_now().await;
    assert_eq!(http.0.load(Ordering::SeqCst), 1);
    tokio::time::advance(Duration::from_secs(10)).await;
    tokio::task::yield_now().await;
    assert!(!listener.connected());
    assert!(!listener.auth_failed());
    assert_eq!(http.0.load(Ordering::SeqCst), 1);
    tokio::time::advance(Duration::from_secs(1)).await;
    tokio::task::yield_now().await;
    assert_eq!(http.0.load(Ordering::SeqCst), 2);
    tokio::time::advance(Duration::from_secs(10)).await;
    tokio::task::yield_now().await;
    listener.stop().await.unwrap();
    tokio::time::advance(Duration::from_secs(20)).await;
    assert_eq!(http.0.load(Ordering::SeqCst), 2);
}
#[tokio::test]
async fn avatar_prefers_largest_profile_image_and_requires_one() {
    let chat = service(MockHttp::with(vec![
        json!({"ok":true,"user":{"profile":{"image_512":"largest","image_72":"small"}}}),
        json!({"ok":true,"user":{"profile":{}}}),
    ]));
    assert_eq!(chat.get_user_avatar_url("U1").await.unwrap(), "largest");
    assert_eq!(
        chat.get_user_avatar_url("U1").await,
        Err(ServiceError::InvalidResult)
    );
}

#[tokio::test]
async fn credential_failure_observation_is_safe_and_probes_are_quiet() {
    let observations = Arc::new(Mutex::new(Vec::new()));
    let saved = observations.clone();
    let http = MockHttp::with(vec![
        json!({"ok":false,"error":"invalid_auth","token":"synthetic-bot"}),
        json!({"ok":false,"error":"invalid_auth"}),
        json!({"ok":false,"error":"not_authed"}),
    ]);
    let chat = SlackChatService::new(
        "synthetic-bot".into(),
        "synthetic-app".into(),
        BASE_URL.into(),
        http,
        Arc::new(move |event| saved.lock().unwrap().push(event)),
    );
    assert_eq!(chat.get_bot_identity().await, Err(ServiceError::Failed));
    assert!(
        chat.check_credentials()
            .await
            .unwrap()
            .iter()
            .all(|c| c.status == CredentialStatus::Failed)
    );
    assert_eq!(
        *observations.lock().unwrap(),
        vec![IntegrationEvent::CredentialFailed {
            provider: "slack",
            code: "invalid_auth"
        }]
    );
}
#[tokio::test]
async fn listener_observes_connection_and_stopped_counters() {
    let observations = Arc::new(Mutex::new(Vec::new()));
    let saved = observations.clone();
    let acks = Arc::new(Mutex::new(Vec::new()));
    let sockets = Arc::new(MockSockets::default());
    sockets.queue.lock().unwrap().push_back(MockSocket {
        frames: vec![Some(envelope("one", "1.0"))].into(),
        acks: acks.clone(),
        closed: Arc::new(AtomicUsize::new(0)),
        fail_acks: false,
    });
    let listener = SlackListener::with_socket(
        "synthetic".into(),
        BASE_URL.into(),
        MockHttp::with(vec![json!({"ok":true,"url":"wss://synthetic"})]),
        sockets,
        Arc::new(|| {}),
        Arc::new(move |event| saved.lock().unwrap().push(event)),
    );
    listener.start().await.unwrap();
    until(|| !acks.lock().unwrap().is_empty()).await;
    listener.stop().await.unwrap();
    assert_eq!(
        *observations.lock().unwrap(),
        vec![
            IntegrationEvent::ReceiveConnected { provider: "slack" },
            IntegrationEvent::ReceiveDisconnected { provider: "slack" },
            IntegrationEvent::ReceiveStopped {
                provider: "slack",
                connect_attempts: 1,
                reconnects: 0,
                received_events: 1,
                acks_sent: 1
            }
        ]
    );
}
#[test]
fn credential_diagnostic_keys_exist_in_both_locales() {
    let en = include_str!(
        "../../../../../guildbotics/templates/locales/integrations/slack/chat_service.en.yml"
    );
    let ja = include_str!(
        "../../../../../guildbotics/templates/locales/integrations/slack/chat_service.ja.yml"
    );
    for key in ["authentication_failed", "request_failed", "missing_scope"] {
        for catalog in [en, ja] {
            assert!(
                catalog
                    .lines()
                    .any(|line| line.starts_with(&format!("  {key}:")))
            );
        }
    }
    for catalog in [en, ja] {
        assert!(catalog.contains("%{needed}"));
        assert!(catalog.contains("%{provided}"));
    }
}

#[tokio::test]
async fn credentials_transport_failure_is_classified_without_sensitive_text() {
    let http = Arc::new(MockHttp::default());
    http.replies.lock().unwrap().extend([
        Err(SlackFailure::Unavailable),
        Err(SlackFailure::InvalidResult),
    ]);
    let checks = service(http).check_credentials().await.unwrap();
    for check in checks {
        assert_eq!(check.status, CredentialStatus::Failed);
        assert_eq!(
            check.error,
            "integrations.slack.chat_service.request_failed"
        );
        assert!(check.error_args.is_empty());
    }
}
#[test]
fn slack_answer_error_classification() {
    assert!(serde_json::from_value::<SemanticReaction>(json!("unknown")).is_err());
    for code in [
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "no_permission",
        "not_allowed_token_type",
    ] {
        assert!(
            matches!(answer(json!({"ok":false,"error":code})),Err(SlackFailure::Auth(safe)) if safe==code)
        );
    }
    for payload in [
        json!({}),
        json!({"ok":false}),
        json!({"ok":false,"error":"unknown_code"}),
    ] {
        assert_eq!(answer(payload), Err(SlackFailure::Api));
    }
    for payload in [json!(null), json!([]), json!("invalid JSON shape")] {
        assert_eq!(answer(payload), Err(SlackFailure::InvalidResult));
    }
    let root = event("C", &json!({"ts":"1.0","bot_id":"B1","text":""}))
        .unwrap()
        .unwrap();
    assert_eq!(root.message.thread_id, "1.0");
    assert!(!root.is_thread_reply);
    assert!(root.message.is_bot_message);
    assert_eq!(root.message.author_id, None);
    assert_eq!(root.status, None);
    for metadata in [
        json!(null),
        json!("not a mapping"),
        json!({"event_type":"other","event_payload":{"kind":"workflow_error"}}),
        json!({"event_type":"guildbotics.workflow_status","event_payload":"bad"}),
        json!({"event_type":"guildbotics.workflow_status","event_payload":{"kind":"other","reason":"failed","person_id":"a"}}),
        json!({"event_type":"guildbotics.workflow_status","event_payload":{"kind":"workflow_error","reason":"unknown"}}),
    ] {
        assert_eq!(
            event("C", &json!({"ts":"1.0","metadata":metadata}))
                .unwrap()
                .unwrap()
                .status,
            None
        );
    }
}

#[tokio::test]
async fn invalid_web_api_url_fails_before_network_without_exposing_credentials() {
    let transport = WebTransport::new().unwrap();
    assert_eq!(
        transport
            .post("://invalid", "auth.test", "synthetic-secret", Form::new())
            .await,
        Err(SlackFailure::Unavailable)
    );
}

#[tokio::test]
async fn missing_bot_credential_names_the_member_key_without_preventing_checks() {
    let chat = SlackChatService::new(
        "".into(),
        "".into(),
        BASE_URL.into(),
        MockHttp::with(vec![]),
        Arc::new(|_| {}),
    )
    .with_token_name("ALICE_SLACK_BOT_TOKEN".into());
    assert_eq!(
        chat.get_bot_identity().await,
        Err(ServiceError::MissingCredential {
            name: "ALICE_SLACK_BOT_TOKEN".into()
        })
    );
    assert!(
        chat.check_credentials()
            .await
            .unwrap()
            .iter()
            .all(|c| c.status == CredentialStatus::Unconfigured)
    );
}
#[tokio::test]
async fn every_semantic_reaction_uses_its_slack_name() {
    let http = MockHttp::with((0..4).map(|_| json!({"ok":true})).collect());
    let chat = service(http.clone());
    for reaction in [
        SemanticReaction::Ack,
        SemanticReaction::Agree,
        SemanticReaction::Celebrate,
        SemanticReaction::Support,
    ] {
        chat.add_reaction("C", "1.0", reaction).await.unwrap();
    }
    assert_eq!(
        http.calls
            .lock()
            .unwrap()
            .iter()
            .map(|c| c.2["name"].clone())
            .collect::<Vec<_>>(),
        vec!["white_check_mark", "thumbsup", "tada", "heart"]
    );
}
#[tokio::test]
async fn channel_absence_is_success_but_repeated_cursor_is_not() {
    let chat = service(MockHttp::with(vec![json!({"ok":true,"channels":[]})]));
    assert_eq!(chat.resolve_channel_id("missing").await.unwrap(), None);
    let repeated = json!({"ok":true,"channels":[],"response_metadata":{"next_cursor":"same"}});
    let chat = service(MockHttp::with(vec![repeated.clone(), repeated]));
    assert_eq!(
        chat.resolve_channel_id("missing").await,
        Err(ServiceError::InvalidResult)
    );
}
#[tokio::test]
async fn ack_failures_do_not_drop_queued_messages_or_stop_receiving() {
    let acks = Arc::new(Mutex::new(Vec::new()));
    let sockets = Arc::new(MockSockets::default());
    sockets.queue.lock().unwrap().push_back(MockSocket {
        frames: vec![Some(envelope("one", "1.0")), Some(envelope("two", "2.0"))].into(),
        acks: acks.clone(),
        closed: Arc::new(AtomicUsize::new(0)),
        fail_acks: true,
    });
    let observations = Arc::new(Mutex::new(Vec::new()));
    let saved = observations.clone();
    let listener = SlackListener::with_socket(
        "synthetic".into(),
        BASE_URL.into(),
        MockHttp::with(vec![json!({"ok":true,"url":"wss://synthetic"})]),
        sockets,
        Arc::new(|| {}),
        Arc::new(move |event| saved.lock().unwrap().push(event)),
    );
    listener.start().await.unwrap();
    until(|| acks.lock().unwrap().len() == 2).await;
    assert!(listener.connected());
    listener.stop().await.unwrap();
    assert_eq!(listener.drain_events().len(), 2);
    assert!(
        observations
            .lock()
            .unwrap()
            .contains(&IntegrationEvent::ReceiveStopped {
                provider: "slack",
                connect_attempts: 1,
                reconnects: 0,
                received_events: 2,
                acks_sent: 0
            })
    );
}
#[tokio::test]
async fn envelopes_without_a_conversation_are_acked_and_never_queued() {
    let acks = Arc::new(Mutex::new(Vec::new()));
    let sockets = Arc::new(MockSockets::default());
    let frames=[json!({"type":"hello"}),json!({"type":"events_api","payload":null}),json!({"type":"events_api","payload":{"event":{"type":"reaction_added","channel":"C","ts":"1.0"}}}),json!({"type":"events_api","payload":{"event":{"type":"message","ts":"1.0"}}}),json!({"type":"events_api","payload":{"event":{"type":"message","channel":"C","ts":"1.0","subtype":"message_deleted"}}})].into_iter().enumerate().map(|(i,mut p)|{p["envelope_id"]=json!(i.to_string());Some(p.to_string())}).collect();
    sockets.queue.lock().unwrap().push_back(MockSocket {
        frames,
        acks: acks.clone(),
        closed: Arc::new(AtomicUsize::new(0)),
        fail_acks: false,
    });
    let listener = SlackListener::with_socket(
        "synthetic".into(),
        BASE_URL.into(),
        MockHttp::with(vec![json!({"ok":true,"url":"wss://synthetic"})]),
        sockets,
        Arc::new(|| {}),
        Arc::new(|_| {}),
    );
    listener.start().await.unwrap();
    until(|| acks.lock().unwrap().len() == 5).await;
    listener.stop().await.unwrap();
    assert!(listener.drain_events().is_empty());
}
