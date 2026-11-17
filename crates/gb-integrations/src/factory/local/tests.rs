use super::*;
use gb_ports::*;
use serde_json::json;
use std::{
    fs,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};
#[path = "../../../tests/support/chat.rs"]
mod contracts;

#[tokio::test]
async fn chat_shared_contract() {
    let d = tempfile::tempdir().unwrap();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    contracts::chat_contract(&c, "general").await;
    let lines = fs::read_to_string(d.path().join("chat/general.jsonl")).unwrap();
    assert_eq!(lines.lines().count(), 3);
}
#[tokio::test]
async fn chat_malformed_records_cursor_status_and_listener_partial_lines() {
    let d = tempfile::tempdir().unwrap();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    assert_eq!(c.resolve_channel_id("missing").await.unwrap(), None);
    assert_eq!(
        c.list_thread_events("missing", "root", None, 30)
            .await
            .unwrap_err(),
        ServiceError::NotFound
    );
    assert!(
        c.list_channel_events("missing", ChannelEventsQuery::default())
            .await
            .unwrap()
            .events
            .is_empty()
    );
    fs::create_dir_all(d.path().join("chat/unreadable.jsonl")).unwrap();
    assert_eq!(
        c.resolve_channel_id("unreadable").await.unwrap(),
        Some("unreadable".into())
    );
    assert_eq!(
        c.list_thread_events("unreadable", "root", None, 30)
            .await
            .unwrap_err(),
        ServiceError::Failed
    );
    assert_eq!(
        c.list_channel_events("unreadable", ChannelEventsQuery::default())
            .await
            .unwrap_err(),
        ServiceError::Failed
    );
    let calls = Arc::new(AtomicUsize::new(0));
    let copy = calls.clone();
    let diagnostics = Arc::new(std::sync::Mutex::new(Vec::new()));
    let received_diagnostics = diagnostics.clone();
    let listener = LocalListener::new(
        d.path().into(),
        Arc::new(move || {
            copy.fetch_add(1, Ordering::SeqCst);
        }),
        Arc::new(move |event| {
            if let crate::factory::IntegrationEvent::ReceiveRecordSkipped { code, .. } = event {
                received_diagnostics.lock().unwrap().push(code);
            }
        }),
    );
    listener.start().await.unwrap();
    listener.start().await.unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    fs::write(d.path().join("chat/events.jsonl"),b"invalid\n[]\n{\"message_id\":\"bad\",\"occurred_at\":\"2026-01-01\"}\n{\"message_id\":\"good\",\"occurred_at\":\"2026-01-01T00:00:00Z\",\"status\":[]}").unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(180)).await;
    assert!(listener.drain_events().is_empty());
    use std::io::Write;
    fs::OpenOptions::new()
        .append(true)
        .open(d.path().join("chat/events.jsonl"))
        .unwrap()
        .write_all(b"\n")
        .unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(180)).await;
    let events = listener.drain_events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].message.message_id, "good");
    assert!(events[0].status.is_none());
    assert_eq!(
        c.resolve_channel_id("events").await.unwrap(),
        Some("events".into())
    );
    let channel = c
        .list_channel_events(
            "events",
            ChannelEventsQuery {
                cursor: Some(String::new()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(channel.events.len(), 1);
    assert_eq!(channel.events[0].message.message_id, "good");
    assert_eq!(
        c.list_thread_events("events", "good", Some(String::new()), 30)
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    c.add_reaction("events", "good", SemanticReaction::Ack)
        .await
        .unwrap();
    assert_eq!(
        c.list_thread_events("events", "good", None, 30)
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    let records = fs::read_to_string(d.path().join("chat/events.jsonl")).unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(records.lines().last().unwrap()).unwrap(),
        json!({"message_id":"good","reaction":"ack","author":"local-aiko"})
    );
    for cursor in ["bad", "²"] {
        assert_eq!(
            c.list_channel_events(
                "events",
                ChannelEventsQuery {
                    cursor: Some(cursor.into()),
                    ..Default::default()
                }
            )
            .await
            .unwrap_err(),
            ServiceError::InvalidContinuation
        );
        assert_eq!(
            c.list_thread_events("events", "good", Some(cursor.into()), 30)
                .await
                .unwrap_err(),
            ServiceError::InvalidContinuation
        );
    }
    listener.stop().await.unwrap();
    assert!(!listener.connected());
    assert!(!listener.auth_failed());
    {
        let diagnostics = diagnostics.lock().unwrap();
        assert!(diagnostics.contains(&"malformed_line"));
        assert!(diagnostics.contains(&"unreadable_channel"));
    }
    let q = ChannelEventsQuery {
        cursor: Some("bad".into()),
        ..Default::default()
    };
    assert!(c.list_channel_events("events", q).await.is_err());
}
#[tokio::test]
async fn chat_status_inclusive_window_and_participant_text() {
    let d = tempfile::tempdir().unwrap();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    let status = WorkflowStatus::from_payload(
        &json!({"kind":"workflow_error","reason":"failed","person_id":"aiko"}),
    )
    .unwrap();
    let post = c
        .post_message(
            "window",
            "@local-aiko @local-bob @local-aiko",
            None,
            Some(&status),
        )
        .await
        .unwrap();
    let q = ChannelEventsQuery {
        since: Some(post.occurred_at.to_utc()),
        until: Some(post.occurred_at.to_utc()),
        ..Default::default()
    };
    let page = c.list_channel_events("window", q).await.unwrap();
    assert_eq!(page.events.len(), 1);
    assert_eq!(page.events[0].status, Some(status));
    assert_eq!(
        page.events[0].message.mentions,
        vec!["local-aiko", "local-bob"]
    );
    let labels = std::collections::BTreeMap::from([("local-aiko".into(), "Member1".into())]);
    assert_eq!(
        c.normalize_participant_text("@local-aiko @stranger", &labels),
        "@Member1 @participant"
    );
    assert_eq!(
        c.render_participant_text("@Member1 @other", &labels),
        "@local-aiko @other"
    );
}
#[tokio::test]
async fn posts_after_a_future_clock_tick_still_follow_in_order() {
    let d = tempfile::tempdir().unwrap();
    fs::create_dir_all(d.path().join("chat")).unwrap();
    fs::write(
        d.path().join("chat/clock.jsonl"),
        b"{\"message_id\":\"first\",\"occurred_at\":\"2099-01-01T00:00:00Z\",\"text\":\"first\"}\n",
    )
    .unwrap();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    let first = c.post_message("clock", "second", None, None).await.unwrap();
    let second = c.post_message("clock", "third", None, None).await.unwrap();
    assert_eq!(
        second.occurred_at - first.occurred_at,
        chrono::Duration::microseconds(1)
    );
    let all = c
        .list_channel_events("clock", ChannelEventsQuery::default())
        .await
        .unwrap();
    assert_eq!(
        all.events
            .iter()
            .map(|e| e.message.text.as_str())
            .collect::<Vec<_>>(),
        vec!["first", "second", "third"]
    );
}
