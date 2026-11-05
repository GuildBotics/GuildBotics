use chrono::{DateTime, Utc};
use gb_ports::*;
use serde_json::json;

fn message(time: &str, id: &str, author: Option<&str>) -> ChatMessage {
    ChatMessage {
        channel_id: "channel".into(),
        message_id: id.into(),
        thread_id: "root".into(),
        occurred_at: DateTime::parse_from_rfc3339(time).unwrap(),
        author_id: author.map(str::to_string),
        text: "hello 🦀".into(),
        mentions: vec!["someone".into()],
        is_bot_message: true,
    }
}

#[test]
fn positions_use_utc_microseconds_and_opaque_id_tiebreaks() {
    let a = message("2026-11-04T09:00:00.000001+09:00", "10", None);
    let b = message("2026-11-04T00:00:00.000001Z", "2", None);
    assert_eq!(a.position(), "2026-11-04T00:00:00.000001Z 10");
    assert!(a.position() < b.position());
    assert_eq!(
        message("2026-11-04T00:00:00Z", "id", None).position(),
        "2026-11-04T00:00:00.000000Z id"
    );
}

#[test]
fn event_payload_has_exact_guest_shape_and_keeps_status_internal() {
    for (time, expected) in [
        ("2026-11-04T00:00:00Z", "2026-11-04T00:00:00+00:00"),
        ("2026-11-04T00:00:00.1Z", "2026-11-04T00:00:00.100000+00:00"),
    ] {
        let event = ChatEvent {
            message: message(time, "reply", None),
            event_id: "event".into(),
            is_thread_reply: true,
            status: WorkflowStatus::from_payload(
                &json!({"kind":"workflow_error", "reason":"asking", "person_id":"aiko"}),
            ),
        };
        assert!(event.status.is_some());
        assert_eq!(
            event.payload(),
            json!({"event_id":"event", "channel_id":"channel", "message_id":"reply", "thread_id":"root", "occurred_at":expected, "author_id":"", "text":"hello 🦀", "mentions":["someone"], "is_bot_message":true, "is_thread_reply":true})
        );
    }
}

#[test]
fn payload_preserves_recorded_offset_while_position_orders_utc() {
    for (recorded, expected) in [
        ("2026-11-04T09:00:00+09:00", "2026-11-04T09:00:00+09:00"),
        (
            "2026-11-04T09:00:00.1+09:00",
            "2026-11-04T09:00:00.100000+09:00",
        ),
        ("2026-11-03T20:30:00-03:30", "2026-11-03T20:30:00-03:30"),
    ] {
        let event: ChatEvent = serde_json::from_value(json!({
            "channel_id":"channel", "message_id":"id", "thread_id":"id",
            "occurred_at":recorded, "author_id":null, "text":"hello", "event_id":"event"
        }))
        .unwrap();
        assert_eq!(event.payload()["occurred_at"], expected);
        let mut utc = message(recorded, "id", None);
        utc.occurred_at = utc.occurred_at.with_timezone(&Utc).fixed_offset();
        assert_eq!(event.message.position(), utc.position());
        let roundtrip: ChatEvent =
            serde_json::from_value(serde_json::to_value(&event).unwrap()).unwrap();
        assert_eq!(roundtrip.payload(), event.payload());
    }
}

#[test]
fn empty_or_absent_actor_is_never_a_sender_match() {
    for author in [None, Some(""), Some("account")] {
        let m = message("2026-11-04T00:00:00Z", "id", author);
        assert!(!m.is_from_user(None));
        assert!(!m.is_from_user(Some("")));
        assert_eq!(m.is_from_user(Some("account")), author == Some("account"));
    }
}

#[test]
fn scope_refusal_has_an_actionable_fixed_message() {
    assert_eq!(
        ServiceError::OutOfScope.to_string(),
        "integrations.resources.out_of_scope"
    );
    assert_ne!(
        ServiceError::OutOfScope.to_string(),
        ServiceError::Failed.to_string()
    );
    // An object-safe port can be selected once by the provider factory.
    fn accept(_: &dyn ChatService) {}
    let _ = accept;
}
