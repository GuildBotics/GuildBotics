//! A stopped ticket's notice, separate from the execution that stopped.
use gb_member::context::PublicMember;
use gb_observability::runs::{Completion, RunRecord, RunState};
use gb_ports::{ChatService, Task, WorkflowStatus};
use serde_json::{Value, json};
use std::collections::BTreeSet;

#[derive(Clone, Debug, PartialEq)]
pub struct Subscription {
    pub channel_id: String,
    pub channel_name: String,
    pub participation: String,
    pub notify_work_stops: bool,
    pub startup_minutes: u32,
    pub interval_seconds: f64,
    pub overlap_seconds: f64,
    pub limit: usize,
}

pub fn subscriptions(member: &Value) -> Vec<Subscription> {
    member["message_channels"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|channel| {
            let config = channel["chat"]
                .as_object()
                .filter(|v| !v.is_empty())
                .or_else(|| channel["channel_info"]["chat"].as_object())?;
            if config.get("enabled").and_then(Value::as_bool) == Some(false) {
                return None;
            }
            let text = |key: &str| config.get(key).and_then(Value::as_str).unwrap_or("").trim();
            let channel_id = if text("channel_id").is_empty() {
                channel["channel_info"]["channel_id"]
                    .as_str()
                    .unwrap_or("")
                    .trim()
            } else {
                text("channel_id")
            };
            let channel_name = if text("channel_name").is_empty() {
                channel["name"].as_str().unwrap_or("").trim()
            } else {
                text("channel_name")
            };
            let participation = text("participation").to_ascii_lowercase();
            let number = |key: &str, default: f64| {
                config
                    .get(key)
                    .and_then(Value::as_f64)
                    .unwrap_or(default)
                    .max(0.0)
            };
            Some(Subscription {
                channel_id: channel_id.into(),
                channel_name: channel_name.into(),
                participation: if ["strict", "social", "muted"].contains(&participation.as_str()) {
                    participation
                } else {
                    "strict".into()
                },
                notify_work_stops: config.get("notify_work_stops") == Some(&Value::Bool(true)),
                startup_minutes: number("startup_backfill_minutes", 60.0) as u32,
                interval_seconds: number("backfill_interval_seconds", 300.0),
                overlap_seconds: number("backfill_overlap_seconds", 60.0),
                limit: (number("backfill_limit", 100.0) as usize).max(1),
            })
        })
        .collect()
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkStop {
    pub reason: &'static str,
    pub run_id: String,
    pub url: String,
    pub label: String,
    pub title: String,
    pub comment_url: String,
}

impl WorkStop {
    pub fn of(task: &Task, reason: &'static str, run_id: &str, comment_url: &str) -> Self {
        Self {
            reason,
            run_id: run_id.into(),
            url: task
                .pull_request_url
                .as_ref()
                .or(task.url.as_ref())
                .cloned()
                .unwrap_or_default(),
            label: task.label.clone(),
            title: task.title.clone(),
            comment_url: comment_url.into(),
        }
    }
}

pub fn comment_url(evidence: &[Value], url: &str) -> String {
    let comments = evidence
        .iter()
        .filter(|item| {
            matches!(
                item["type"].as_str(),
                Some(
                    "issue_comment" | "pr_comment" | "pr_review" | "pr_review_comment" | "pr_reply"
                )
            )
        })
        .map(|item| &item["payload"])
        .filter(|payload| payload["url"].as_str().is_some_and(|url| !url.is_empty()))
        .collect::<Vec<_>>();
    comments
        .iter()
        .rev()
        .find(|payload| payload["target"]["url"] == url)
        .or_else(|| comments.last())
        .and_then(|payload| payload["url"].as_str())
        .unwrap_or("")
        .into()
}

pub fn completed_stop(
    task: &Task,
    record: &RunRecord,
    roster: &[PublicMember],
) -> Option<WorkStop> {
    if record.finished_at.is_none() || record.status == RunState::Running {
        return None;
    }
    let result = record.result.as_ref()?;
    let reason = match result.status {
        Completion::Asking | Completion::Blocked => {
            if !result.waiting_for.is_empty()
                && result.waiting_for.iter().all(|id| {
                    roster.iter().any(|member| {
                        member.person_id == *id && member.is_active && member.person_type != "human"
                    })
                })
            {
                return None;
            }
            if result.status == Completion::Asking {
                "asking"
            } else {
                "blocked"
            }
        }
        _ => return None,
    };
    let mut stop = WorkStop::of(task, reason, &record.run_id, "");
    stop.comment_url = comment_url(&record.provider_evidence, &stop.url);
    Some(stop)
}

/// Never changes the run result. Only opted-in channels receive one notice;
/// a workflow status suppresses the notification as input for every member.
pub async fn notify(
    chat: &dyn ChatService,
    person: &PublicMember,
    channels: &[Subscription],
    stop: &WorkStop,
    language: &str,
    observe: &(dyn Fn(Value) + Send + Sync),
) {
    let item = [stop.label.as_str(), stop.title.as_str()]
        .into_iter()
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    let mut body = crate::i18n::t(
        language,
        &format!("capabilities.work_stops.{}", stop.reason),
        &[("name", &person.name), ("item", &item), ("url", &stop.url)],
    );
    if !stop.comment_url.is_empty() {
        body.push('\n');
        body.push_str(&crate::i18n::t(
            language,
            "capabilities.work_stops.comment",
            &[("url", &stop.comment_url)],
        ));
    }
    let Ok(status) = serde_json::from_value::<WorkflowStatus>(
        json!({"reason":stop.reason,"person_id":person.person_id,"run_id":stop.run_id,"subject":stop.url}),
    ) else {
        return;
    };
    let mut attempted = BTreeSet::new();
    for channel in channels.iter().filter(|channel| channel.notify_work_stops) {
        let resolved = if channel.channel_id.is_empty() {
            chat.resolve_channel_id(&channel.channel_name)
                .await
                .map(|id| id.unwrap_or_default())
        } else {
            Ok(channel.channel_id.clone())
        };
        let (id, delivered, error) = match resolved {
            Ok(id) if !id.is_empty() => {
                if !attempted.insert(id.clone()) {
                    continue;
                }
                let result = chat.post_message(&id, &body, None, Some(&status)).await;
                let error = result
                    .as_ref()
                    .err()
                    .map(|e| e.translation_key())
                    .unwrap_or_default();
                (id, result.is_ok(), error)
            }
            Ok(_) => (String::new(), false, ""),
            Err(error) => (String::new(), false, error.translation_key()),
        };
        observe(
            json!({"run_id":stop.run_id,"reason":stop.reason,"url":stop.url,"comment_url":stop.comment_url,
            "channel_id":id,"channel_name":channel.channel_name,"delivered":delivered,"error":error}),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn person(id: &str, kind: &str, active: bool) -> PublicMember {
        serde_json::from_value(
            json!({"person_id":id,"name":id,"person_type":kind,"is_active":active,
            "roles":{},"profile":{},"speaking_style":"","accounts":{}}),
        )
        .unwrap()
    }

    #[test]
    fn only_completed_waits_exclusively_for_active_agents_suppress_a_stop() {
        let roster = vec![
            person("aiko", "agent", true),
            person("owner", "human", true),
            person("inactive", "agent", false),
        ];
        let task = Task {
            url: Some("ticket".into()),
            ..Task::default()
        };
        for (waiting, expected) in [
            (vec!["aiko"], false),
            (vec!["owner"], true),
            (vec!["inactive"], true),
            (vec!["missing"], true),
            (vec![], true),
            (vec!["aiko", "owner"], true),
        ] {
            let mut record:RunRecord=serde_json::from_value(json!({"run_id":"run","work_kind":"ticket","execution_mode":"autonomous",
                "member_id":"aiko","device_id":"device","started_at":"2026-11-05T00:00:00Z","finished_at":"2026-11-05T00:01:00Z","status":"failed",
                "result":{"subject_type":"ticket","subject_id":"ticket","status":"asking","waiting_for":waiting}})).unwrap();
            assert_eq!(completed_stop(&task, &record, &roster).is_some(), expected);
            if expected {
                assert_eq!(
                    completed_stop(&task, &record, &roster).unwrap().reason,
                    "asking"
                );
            }
            for completion in [
                Completion::Done,
                Completion::Asking,
                Completion::Blocked,
                Completion::AwaitingChecks,
            ] {
                record.result.as_mut().unwrap().status = completion;
                let reason = match completion {
                    Completion::Asking => Some("asking"),
                    Completion::Blocked => Some("blocked"),
                    Completion::Done | Completion::AwaitingChecks => None,
                };
                let stop = completed_stop(&task, &record, &roster);
                if expected && let Some(reason) = reason {
                    assert_eq!(stop, Some(WorkStop::of(&task, reason, "run", "")));
                } else {
                    assert!(stop.is_none());
                }
            }
            record.result = None;
            assert!(completed_stop(&task, &record, &roster).is_none());
            record.finished_at = None;
            assert!(completed_stop(&task, &record, &roster).is_none());
        }
    }

    #[tokio::test]
    async fn local_chat_receives_one_status_notice_per_opted_in_channel() {
        use gb_config::secrets::{DeviceKey, Keychain, Scope, SecretStore};
        struct UnusedKeychain;
        impl Keychain for UnusedKeychain {
            fn read(&self) -> anyhow::Result<Option<String>> {
                panic!("local provider must not read a credential");
            }
            fn create(&self, _: &str) -> anyhow::Result<()> {
                panic!("local provider must not create a credential");
            }
        }
        let root = tempfile::tempdir().unwrap();
        let writes = gb_base::shared_writes::SharedWrites::new(
            root.path().join("identity"),
            Some(root.path().join("workspace")),
        );
        let secrets = SecretStore::new(
            &writes,
            Scope::Identity,
            DeviceKey {
                machine_root: root.path().into(),
                keychain: &UnusedKeychain,
            },
        )
        .unwrap();
        let config = gb_integrations::IntegrationConfig {
            local_root: root.path().join("services"),
            identity_root: root.path().join("identity"),
            person_id: "aiko".into(),
            display_name: "Aiko".into(),
            accounts: Default::default(),
            branvil_members: Default::default(),
            ticket_provider: "local".into(),
            code_provider: "local".into(),
            chat_provider: "local".into(),
            ticket_scope: String::new(),
            code_scope: String::new(),
            checks: String::new(),
            slack_base_url: String::new(),
        };
        let factory = gb_integrations::ServiceIntegrationFactory::new(
            config,
            &secrets,
            None,
            std::sync::Arc::new(|_| {}),
        )
        .unwrap();
        let chat = factory.create_chat_service().unwrap();
        chat.post_message("notify", "seed", None, None)
            .await
            .unwrap();
        let channels = subscriptions(&json!({"message_channels":[
            {"chat":{"channel_id":"notify","notify_work_stops":true}},
            {"chat":{"channel_name":"notify","notify_work_stops":true}},
            {"chat":{"channel_id":"excluded","notify_work_stops":false}}
        ]}));
        let task = Task {
            url: Some("local://tickets/90".into()),
            label: "#90".into(),
            title: "Task".into(),
            ..Task::default()
        };
        let stop = WorkStop::of(&task, "blocked", "run", "local://comment/1");
        let observed = std::sync::Mutex::new(vec![]);
        notify(
            chat.as_ref(),
            &person("aiko", "agent", true),
            &channels,
            &stop,
            "ja",
            &|value| observed.lock().unwrap().push(value),
        )
        .await;
        let events = chat
            .list_channel_events(
                "notify",
                gb_ports::ChannelEventsQuery {
                    since: None,
                    until: None,
                    cursor: None,
                    limit: 100,
                },
            )
            .await
            .unwrap()
            .events;
        assert_eq!(events.len(), 2);
        let notice = &events[1];
        assert!(notice.message.text.contains("#90 Task"));
        assert!(notice.message.text.contains("local://comment/1"));
        assert_eq!(
            notice.status.as_ref().unwrap().reason(),
            gb_ports::WorkflowStatusReason::Blocked
        );
        assert_eq!(observed.lock().unwrap().len(), 1);
        assert_eq!(observed.lock().unwrap()[0]["delivered"], true);
    }

    #[test]
    fn subscriptions_preserve_enabled_participation_notification_and_backfill_policies() {
        let member = json!({"message_channels":[
            {"name":"primary","chat":{"enabled":true,"participation":" SOCIAL ","notify_work_stops":true,"backfill_limit":20}},
            {"name":"disabled","chat":{"enabled":false}},
            {"name":"muted","channel_info":{"channel_id":"C2","chat":{"participation":"muted"}}},
            {"name":"unknown","chat":{"participation":"invalid","notify_work_stops":"true"}},
            {"name":"not chat"}
        ]});
        let channels = subscriptions(&member);
        assert_eq!(channels.len(), 3);
        assert_eq!(channels[0].participation, "social");
        assert!(channels[0].notify_work_stops);
        assert_eq!(channels[0].channel_name, "primary");
        assert_eq!(channels[0].limit, 20);
        assert_eq!(channels[1].channel_id, "C2");
        assert_eq!(channels[1].participation, "muted");
        assert_eq!(channels[2].participation, "strict");
        assert!(!channels[2].notify_work_stops);
    }

    #[test]
    fn latest_comment_on_the_subject_precedes_comments_elsewhere() {
        let evidence = vec![
            json!({"type":"issue_comment","payload":{"target":{"url":"ticket"},"url":"comment1"}}),
            json!({"type":"pr_reply","payload":{"target":{"url":"pr"},"url":"comment2"}}),
            json!({"type":"pr_comment","payload":{"target":{"url":"ticket"},"url":"comment3"}}),
        ];
        assert_eq!(comment_url(&evidence, "ticket"), "comment3");
        assert_eq!(comment_url(&evidence, "pr"), "comment2");
        assert_eq!(comment_url(&evidence, "other"), "comment3");
        assert_eq!(comment_url(&[], "ticket"), "");
        for kind in [
            "issue_comment",
            "pr_comment",
            "pr_review",
            "pr_review_comment",
            "pr_reply",
        ] {
            assert_eq!(
                comment_url(
                    &[json!({"type":kind,"payload":{"target":{"url":"ticket"},"url":"linked"}})],
                    "ticket"
                ),
                "linked"
            );
        }
        assert_eq!(
            comment_url(
                &[
                    json!({"type":"pr_create","payload":{"url":"created"}}),
                    json!({"type":"reaction_add","payload":{"url":"reaction"}}),
                    json!({"type":"issue_comment","payload":{"target":{"url":"ticket"}}})
                ],
                "ticket"
            ),
            ""
        );
    }

    #[tokio::test]
    async fn notification_matrix_preserves_language_status_scope_and_safe_delivery_failures() {
        let root = tempfile::tempdir().unwrap();
        let chat = crate::test_chat::Faults::new(crate::test_chat::local(root.path()));
        let member = person("aiko", "agent", true);
        for language in ["en", "ja"] {
            for reason in ["asking", "blocked", "failed", "review_limit"] {
                for address in ["", "local://comment/1"] {
                    let prefix = format!("{language}-{reason}-{}", !address.is_empty());
                    let first = format!("{prefix}-one");
                    let second = format!("{prefix}-two");
                    chat.inner
                        .post_message(&first, "seed", None, None)
                        .await
                        .unwrap();
                    let channels = subscriptions(&json!({"message_channels":[
                        {"name":"first","chat":{"channel_id":first,"notify_work_stops":true}},
                        {"name":"second","chat":{"channel_id":second,"enabled":true,"notify_work_stops":true}},
                        {"name":first,"chat":{"notify_work_stops":true}},
                        {"name":"disabled","chat":{"channel_id":"disabled","enabled":false,"notify_work_stops":true}},
                        {"name":"quiet","chat":{"channel_id":"quiet","notify_work_stops":false}},
                        {"name":"legacy","chat":{"channel_id":"legacy"}}
                    ]}));
                    let stop = WorkStop {
                        reason,
                        run_id: prefix.clone(),
                        url: "local://tickets/90".into(),
                        label: "#90".into(),
                        title: "Task".into(),
                        comment_url: address.into(),
                    };
                    let observed = std::sync::Mutex::new(vec![]);
                    notify(&chat, &member, &channels, &stop, language, &|v| {
                        observed.lock().unwrap().push(v)
                    })
                    .await;
                    let mut expected = crate::i18n::t(
                        language,
                        &format!("capabilities.work_stops.{reason}"),
                        &[
                            ("name", &member.name),
                            ("item", "#90 Task"),
                            ("url", &stop.url),
                        ],
                    );
                    if !address.is_empty() {
                        expected.push('\n');
                        expected.push_str(&crate::i18n::t(
                            language,
                            "capabilities.work_stops.comment",
                            &[("url", address)],
                        ));
                    }
                    for id in [&first, &second] {
                        let page = chat
                            .inner
                            .list_channel_events(id, Default::default())
                            .await
                            .unwrap();
                        let notice = page.events.last().unwrap();
                        assert_eq!(notice.message.text, expected);
                        assert_eq!(notice.message.author_id.as_deref(), Some("local-aiko"));
                        let expected_status: WorkflowStatus = serde_json::from_value(json!({"reason":reason,"person_id":"aiko","run_id":prefix,"subject":stop.url})).unwrap();
                        assert_eq!(notice.status.as_ref().unwrap(), &expected_status);
                    }
                    let observed = observed.lock().unwrap();
                    assert_eq!(observed.len(), 2);
                    assert!(
                        observed
                            .iter()
                            .all(|v| v["delivered"] == true && v["error"] == "")
                    );
                    assert_eq!(observed[0]["channel_id"], first);
                    assert_eq!(observed[1]["channel_id"], second);
                }
            }
        }
        let channels = subscriptions(&json!({"message_channels":[
            {"chat":{"channel_id":"bad","notify_work_stops":true}},
            {"chat":{"channel_id":"good","notify_work_stops":true}},
            {"name":"missing","chat":{"notify_work_stops":true}}
        ]}));
        chat.errors
            .lock()
            .unwrap()
            .insert("post:bad".into(), gb_ports::ServiceError::Failed);
        let stop = WorkStop::of(
            &Task {
                url: Some("ticket".into()),
                ..Default::default()
            },
            "blocked",
            "run",
            "",
        );
        let observed = std::sync::Mutex::new(vec![]);
        notify(&chat, &member, &channels, &stop, "en", &|v| {
            observed.lock().unwrap().push(v)
        })
        .await;
        let events = observed.lock().unwrap().clone();
        assert_eq!(events.len(), 3);
        assert_eq!(events[0]["delivered"], false);
        assert_eq!(events[0]["error"], "integrations.resources.service_failed");
        assert_eq!(events[1]["delivered"], true);
        assert_eq!(events[2]["channel_id"], "");
        assert_eq!(events[2]["channel_name"], "missing");
        assert_eq!(events[2]["delivered"], false);
        assert_eq!(
            chat.inner
                .list_channel_events("good", Default::default())
                .await
                .unwrap()
                .events
                .len(),
            1
        );
        assert!(
            chat.inner
                .list_channel_events("quiet", Default::default())
                .await
                .unwrap()
                .events
                .is_empty()
        );
        assert!(
            chat.inner
                .list_channel_events("disabled", Default::default())
                .await
                .unwrap()
                .events
                .is_empty()
        );
        assert!(
            chat.inner
                .list_channel_events("legacy", Default::default())
                .await
                .unwrap()
                .events
                .is_empty()
        );
        notify(&chat, &member, &[], &stop, "en", &|_| {
            panic!("empty subscriptions cannot notify")
        })
        .await;
        let channels = subscriptions(
            &json!({"message_channels":[{"chat":{"channel_id":"good","notify_work_stops":true}}]}),
        );
        for (reason, id) in [("asking", "run-x"), ("blocked", "run-y")] {
            let mut next = stop.clone();
            next.reason = reason;
            next.run_id = id.into();
            next.comment_url = "local://same-comment".into();
            notify(&chat, &member, &channels, &next, "en", &|_| ()).await;
        }
        let notices = chat
            .inner
            .list_channel_events("good", Default::default())
            .await
            .unwrap()
            .events;
        assert_eq!(notices.len(), 3);
        for (index, id) in [(1, "run-x"), (2, "run-y")] {
            assert_eq!(
                serde_json::to_value(notices[index].status.as_ref().unwrap()).unwrap()["run_id"],
                id
            );
            assert!(notices[index].message.text.contains("local://same-comment"));
        }
    }
}
