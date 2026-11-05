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
    queries: Mutex<Vec<ChannelEventsQuery>>,
    cursors: Mutex<Vec<Option<String>>>,
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
            assert!(query.since.is_some());
            assert!(query.until.is_some());
            self.queries.lock().unwrap().push(query);
            Ok(page())
        })
    }
    fn list_thread_events<'a>(
        &'a self,
        channel: &'a str,
        thread: &'a str,
        cursor: Option<String>,
        limit: usize,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            assert_eq!(channel, "C1");
            assert_eq!(thread, "root");
            assert_eq!(limit, 3);
            self.cursors.lock().unwrap().push(cursor);
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
        ServiceError::MissingCredential {
            name: "AIKO_SLACK_BOT_TOKEN".into(),
        },
        ServiceError::Diagnostic {
            class: ServiceErrorClass::Failure,
            key: "key",
            args: [("code".into(), "invalid_auth".into())].into(),
        },
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
    assert_eq!(post.channel_id, "C1");
    assert_eq!(post.message_id, "posted");
    assert_eq!(post.occurred_at, "2026-01-01T00:00:00+00:00");
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
        c.inspect_thread_page(&reply, 3, Some("page2".into()))
            .await
            .unwrap(),
        c.inspect_channel(
            &reference,
            ChannelEventsQuery {
                limit: 3,
                since: Some("2026-01-01T00:00:00Z".parse().unwrap()),
                until: Some("2026-01-02T00:00:00Z".parse().unwrap()),
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
    assert_eq!(
        *provider.cursors.lock().unwrap(),
        vec![None, None, Some("page2".into())]
    );
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
    assert_eq!(
        *provider.posts.lock().unwrap(),
        vec![
            ("C1".into(), "Hello @reviewer".into(), None),
            ("C1".into(), "Reply @reviewer".into(), Some("root".into())),
            ("C1".into(), "By name @reviewer".into(), Some("root".into()))
        ]
    );
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
                    error_args: [
                        ("member".into(), "aiko".into()),
                        ("code".into(), "invalid_auth".into()),
                    ]
                    .into(),
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
        let expected_credentials: Vec<_> = provider
            .checks
            .iter()
            .map(|v| {
                let mut row = json!({"name":v.name,"status":v.status,"error_args":v.error_args});
                if !v.error.is_empty() {
                    row["error"] = v.error.clone().into();
                }
                row
            })
            .collect();
        assert_eq!(result["credentials"], json!(expected_credentials));
        assert_eq!(result["status"], expected);
        if expected == "failed" {
            assert_eq!(result["credentials"][1]["error"], "safe guidance");
        }
    }
}

#[tokio::test]
async fn channel_inspection_parses_naive_times_from_cli_arguments_before_the_provider() {
    use chrono::TimeZone;
    use gb_member::{
        repository::{Repository, RepositoryServices},
        session::{Control, Session},
    };
    struct Forbidden;
    impl RepositoryServices for Forbidden {
        fn code(&self) -> gb_member::Result<&dyn CodeHostingService> {
            panic!("no code access")
        }
        fn tickets(&self) -> gb_member::Result<&dyn TicketService> {
            panic!("no ticket access")
        }
    }
    struct Records;
    impl Control for Records {
        fn command<'a>(
            &'a self,
            _: &'a gb_member::command::Parsed,
            _: &'a str,
        ) -> gb_member::git::Pending<'a, serde_json::Value> {
            Box::pin(async { panic!("no run command") })
        }
        fn record(&self, _: &str, _: bool, _: &serde_json::Value) -> gb_member::Result<()> {
            Ok(())
        }
    }
    let provider = Double::default();
    let roster = BTreeMap::new();
    let session = Session {
        person_id: "aiko",
        person_type: "agent",
        delegated_to: None,
        workflow: true,
        repository: Repository {
            services: &Forbidden,
            guard: &Current,
            roster: &roster,
        },
        chat: Some(Chat {
            service: &provider,
            service_name: "local",
            person_id: "aiko",
            guard: &Current,
            participant_labels: BTreeMap::new(),
        }),
        memory: None,
        git: None,
        context: json!({}),
        control: &Records,
        git_root: std::path::Path::new("/unused"),
    };
    for since in ["2026-05-01", "2026-05-01T09:00:00"] {
        let input = [
            "chat",
            "inspect",
            "channel",
            "--person",
            "aiko",
            "--channel-id",
            "C1",
            "--limit",
            "3",
            "--since",
            since,
            "--until",
            "2026-05-02T09:00:00",
        ]
        .map(str::to_owned);
        session.execute(&input, "").await.unwrap();
        let queries = provider.queries.lock().unwrap();
        let query = queries.last().unwrap();
        let hour = if since.len() == 10 { 0 } else { 9 };
        assert_eq!(
            query.since,
            Some(
                chrono::Local
                    .with_ymd_and_hms(2026, 5, 1, hour, 0, 0)
                    .earliest()
                    .unwrap()
                    .to_utc()
            )
        );
        assert_eq!(
            query.until,
            Some(
                chrono::Local
                    .with_ymd_and_hms(2026, 5, 2, 9, 0, 0)
                    .earliest()
                    .unwrap()
                    .to_utc()
            )
        );
    }
    let input = [
        "chat",
        "inspect",
        "channel",
        "--person",
        "aiko",
        "--channel-id",
        "C1",
        "--since",
        "invalid",
    ]
    .map(str::to_owned);
    assert_eq!(
        session.execute(&input, "").await.err().unwrap().to_string(),
        "cli.member.time_invalid"
    );
    assert_eq!(provider.queries.lock().unwrap().len(), 2);
}
