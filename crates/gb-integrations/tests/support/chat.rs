//! Shared local chat behavior.
use gb_base::service::*;
use gb_integrations::chat::*;

pub async fn chat_contract(service: &dyn ChatService, channel: &str) {
    let identity = service.get_bot_identity().await.unwrap();
    assert!(!identity.user_id.is_empty());
    let root = service
        .post_message(channel, "root", None, None)
        .await
        .unwrap();
    let reply = service
        .post_message(channel, "reply", Some(&root.thread_id), None)
        .await
        .unwrap();
    assert!(reply.occurred_at > root.occurred_at);
    assert_eq!(
        service
            .list_channel_events(channel, ChannelEventsQuery::default())
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    let page = service
        .list_thread_events(channel, &root.thread_id, None, 1)
        .await
        .unwrap();
    assert_eq!(page.events[0].message.message_id, root.message_id);
    assert!(page.cursor.is_some());
    let next = service
        .list_thread_events(channel, &root.thread_id, page.cursor, 1)
        .await
        .unwrap();
    assert_eq!(next.events[0].message.message_id, reply.message_id);
    for _ in 0..2 {
        service
            .add_reaction(channel, &root.message_id, SemanticReaction::Ack)
            .await
            .unwrap()
    }
    assert!(
        service
            .list_thread_events(channel, "missing", None, 10)
            .await
            .is_err()
    );
}
