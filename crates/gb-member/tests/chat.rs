use gb_member::{
    WriteGuard,
    chat::{Chat, Reference},
};
use gb_ports::*;
use serde_json::json;
use std::{collections::BTreeMap, sync::Mutex};
type Posted = (String, String, Option<String>);
#[derive(Default)]
struct Double {
    posts: Mutex<Vec<Posted>>,
    checks: Vec<CredentialCheck>,
    error: Option<ServiceError>,
}
impl ChatService for Double {
    fn get_bot_identity(&self) -> ServiceFuture<'_, ChatIdentity> {
        Box::pin(async {
            if let Some(error) = &self.error {
                return Err(error.clone());
            }
            Ok(ChatIdentity {
                user_id: "U1".into(),
                display_name: "Aiko".into(),
                workspace: "W1".into(),
            })
        })
    }
    fn check_credentials(&self) -> ServiceFuture<'_, Vec<CredentialCheck>> {
        Box::pin(async { Ok(self.checks.clone()) })
    }
    fn self_user_id(&self, account: &ActorName) -> String {
        account.0.clone()
    }
    fn parse_message_url(&self, url: &str) -> Result<ChatMessageRef, ServiceError> {
        if url != "message-url" {
            return Err(ServiceError::OutOfScope);
        }
        Ok(ChatMessageRef {
            channel_id: "C1".into(),
            message_id: "reply".into(),
            thread_id: "root".into(),
        })
    }
    fn mentioned_user_ids(&self, _text: &str) -> Vec<String> {
        vec![]
    }
    fn list_channel_events<'a>(
        &'a self,
        channel: &'a str,
        query: ChannelEventsQuery,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            assert_eq!(channel, "C1");
            assert_eq!(query.limit, 3);
            Ok(page())
        })
    }
    fn list_thread_events<'a>(
        &'a self,
        channel: &'a str,
        thread: &'a str,
        _cursor: Option<String>,
        limit: usize,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            assert_eq!(channel, "C1");
            assert_eq!(thread, "root");
            assert_eq!(limit, 3);
            Ok(page())
        })
    }
    fn resolve_channel_id<'a>(&'a self, name: &'a str) -> ServiceFuture<'a, Option<String>> {
        Box::pin(async move { Ok((name == "general").then(|| "C1".into())) })
    }
    fn post_message<'a>(
        &'a self,
        channel: &'a str,
        text: &'a str,
        thread: Option<&'a str>,
        _status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, ChatPostResult> {
        Box::pin(async move {
            self.posts.lock().unwrap().push((
                channel.into(),
                text.into(),
                thread.map(str::to_owned),
            ));
            Ok(ChatPostResult {
                channel_id: channel.into(),
                message_id: "posted".into(),
                thread_id: thread.unwrap_or("posted").into(),
                occurred_at: chrono::DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z").unwrap(),
            })
        })
    }
    fn add_reaction<'a>(
        &'a self,
        channel: &'a str,
        message: &'a str,
        _reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            assert_eq!(channel, "C1");
            assert_eq!(message, "reply");
            Ok(())
        })
    }
    fn render_participant_text(&self, text: &str, labels: &BTreeMap<String, String>) -> String {
        let mut result = text.to_owned();
        for (name, account) in labels {
            result = result.replace(name, account);
        }
        result
    }
}
#[tokio::test]
async fn chat_service_errors_are_preserved() {
    for error in [
        ServiceError::Failed,
        ServiceError::NotFound,
        ServiceError::Unavailable,
        ServiceError::Unsupported,
    ] {
        let provider = Double {
            error: Some(error.clone()),
            ..Default::default()
        };
        let c = Chat {
            service: &provider,
            service_name: "local",
            person_id: "aiko",
            guard: &Current,
            participant_labels: BTreeMap::new(),
        };
        assert!(matches!(c.identity().await,Err(gb_member::MemberError::Service(e)) if e==error));
    }
}
fn page() -> ChatEventPage {
    ChatEventPage{events:[2,1].into_iter().map(|n|serde_json::from_value(json!({"event_id":format!("e{n}"),"channel_id":"C1","message_id":n.to_string(),"thread_id":"root",
        "occurred_at":format!("2026-01-01T00:00:0{n}Z"),"author_id":"U2","text":"hello","status":null})).unwrap()).collect(),cursor:Some("next".into())}
}
struct Current;
impl WriteGuard for Current {
    fn ensure_current(&self) -> gb_member::Result<()> {
        Ok(())
    }
}
#[tokio::test]
async fn member_chat_identity_posts_replies_reactions_and_sorted_inspections() {
    let provider = Double::default();
    let c = Chat {
        service: &provider,
        service_name: "local",
        person_id: "aiko",
        guard: &Current,
        participant_labels: BTreeMap::from([("Reviewer".into(), "@reviewer".into())]),
    };
    assert_eq!(c.identity().await.unwrap().person_id, "aiko");
    assert_eq!(c.resolve_channel("missing").await.unwrap().channel_id, "");
    let reference = Reference {
        channel_name: Some("general"),
        message_id: Some("root"),
        ..Default::default()
    };
    let post = c.post(&reference, "Hello Reviewer", false).await.unwrap();
    assert!(post.posted);
    assert_eq!(post.text, "Hello @reviewer");
    assert_eq!(post.thread_id, "posted");
    let reply = Reference {
        message_url: Some("message-url"),
        ..Default::default()
    };
    assert_eq!(
        c.post(&reply, "Reply Reviewer", true)
            .await
            .unwrap()
            .thread_id,
        "root"
    );
    assert_eq!(
        c.post(&reference, "By name Reviewer", true)
            .await
            .unwrap()
            .thread_id,
        "root"
    );
    assert_eq!(
        c.react(&reply, SemanticReaction::Ack)
            .await
            .unwrap()
            .message_id,
        "reply"
    );
    for result in [
        c.inspect_thread(&reference, 3).await.unwrap(),
        c.inspect_thread(&reply, 3).await.unwrap(),
        c.inspect_channel(
            &reference,
            ChannelEventsQuery {
                limit: 3,
                ..Default::default()
            },
        )
        .await
        .unwrap(),
    ] {
        assert_eq!(result["next_cursor"], "next");
        assert_eq!(result["messages"][0]["message_id"], "1");
        assert!(result["messages"][0].get("status").is_none());
    }
    assert!(c.inspect_thread(&Reference::default(), 3).await.is_err());
    assert!(
        c.post(
            &Reference {
                channel_name: Some("missing"),
                ..Default::default()
            },
            "text",
            false
        )
        .await
        .is_err()
    );
    assert_eq!(provider.posts.lock().unwrap().len(), 3);
}
#[tokio::test]
async fn credential_status_handles_empty_unconfigured_mixed_and_failed_checks() {
    for (statuses, expected) in [
        (vec![], "ok"),
        (vec![CredentialStatus::Unconfigured], "unconfigured"),
        (
            vec![CredentialStatus::Ok, CredentialStatus::Unconfigured],
            "ok",
        ),
        (
            vec![CredentialStatus::Ok, CredentialStatus::Failed],
            "failed",
        ),
    ] {
        let provider = Double {
            checks: statuses
                .into_iter()
                .map(|status| CredentialCheck {
                    name: "TOKEN_NAME".into(),
                    status,
                    error: if status == CredentialStatus::Failed {
                        "safe guidance".into()
                    } else {
                        String::new()
                    },
                    error_args: Default::default(),
                })
                .collect(),
            ..Default::default()
        };
        let c = Chat {
            service: &provider,
            service_name: "local",
            person_id: "aiko",
            guard: &Current,
            participant_labels: BTreeMap::new(),
        };
        let result = c.check_credentials().await.unwrap();
        assert_eq!(result["status"], expected);
        if expected == "failed" {
            assert_eq!(result["credentials"][1]["error"], "safe guidance");
        }
    }
}
