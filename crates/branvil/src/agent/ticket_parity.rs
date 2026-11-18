//! Ticket-specific frozen host assertions at the provider and selection boundaries.
use super::*;
use crate::api::MemberApi;
use crate::test_support::Fixture as Embedded;

struct Fixture {
    running: Embedded,
    owner: MemberApi,
    service: TicketAgent,
    sequence: std::sync::atomic::AtomicUsize,
}
impl Fixture {
    async fn new() -> Self {
        let running = Embedded::new("owner1").await;
        let owner = running.owner.clone();
        for workspace in ["test", "other"] {
            owner
                .repos_create_workspace(decode(json!({"name":workspace})).unwrap())
                .await
                .unwrap();
            owner
                .repos_create(
                    decode(json!({"path":format!("{workspace}/repo"),"labels":["bug"]})).unwrap(),
                )
                .await
                .unwrap();
        }
        let service = TicketAgent::new(
            running.data(),
            "test".into(),
            "agent1".into(),
            "agent1".into(),
        );
        Self {
            running,
            owner,
            service,
            sequence: std::sync::atomic::AtomicUsize::new(0),
        }
    }
    fn request(&self) -> CreateTicket {
        CreateTicket {
            tracker: "test/repo".into(),
            title: "Work".into(),
            body: String::new(),
            labels: vec![],
            add_to_board: true,
            interactive: false,
            assignee: None,
            lane: None,
            parent: None,
            blocked_by: vec![],
        }
    }
    async fn create(&self) -> String {
        self.service
            .create_ticket(self.request())
            .await
            .unwrap()
            .target
            .url
    }
    async fn read(&self, url: &str) -> Value {
        TicketService::read(&self.service, Resource::Tickets, url, None, "")
            .await
            .unwrap()
            .items()[0]
            .clone()
    }
    async fn assigned(&self, lane: &str) -> String {
        let url = self.create().await;
        let key = url.rsplit('/').next().unwrap();
        self.owner
            .tickets_set_assignee("test/repo", key, Some("agent1".into()))
            .await
            .unwrap();
        self.owner
            .tickets_put_on_board("test/repo", key, decode(json!({"lane":lane})).unwrap())
            .await
            .unwrap();
        url
    }
}
fn card() -> Value {
    json!({"url":"http://unused/test/repo/-/tickets/1","key":"1","title":"Work","body":"","repository":"test/repo","state":"open","lane":"in_progress","assignee":{"name":"agent1","assigned_at":"2026-01-02T00:00:00Z"},"interactive_ancestor":null,"comments":[],"mentions":[]})
}
fn comment(author: &str, body: &str) -> Value {
    json!({"author":author,"body":body,"created_at":"2026-01-03T00:00:00Z","mentions":[]})
}
async fn reason(service: &TicketAgent, card: &Value) -> Option<String> {
    service
        .select_card(&typed_card(card), false)
        .await
        .unwrap()
        .and_then(|t| t.trigger_reason)
}

#[tokio::test]
async fn waiting_eligibility_and_comment_changes_are_reread() {
    let f = Fixture::new().await;
    let url = f.assigned("in_progress").await;
    for interactive in [false, true] {
        f.service
            .update_ticket(
                &url,
                UpdateTicket {
                    interactive: Some(interactive),
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(
            TicketService::can_receive_work(&f.service, &url)
                .await
                .unwrap(),
            !interactive
        );
        assert_eq!(
            !f.service.candidates().await.unwrap().is_empty(),
            !interactive
        );
        assert_eq!(f.read(&url).await["lane"], "in_progress");
    }
    f.service
        .update_ticket(
            &url,
            UpdateTicket {
                interactive: Some(false),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    TicketService::comment(&f.service, &url, "Answered", None)
        .await
        .unwrap();
    assert!(
        !TicketService::can_receive_work(&f.service, &url)
            .await
            .unwrap()
    );
    f.running.stop().await;
}

#[tokio::test]
async fn ready_and_working_triggers_observe_member_words_and_assignment() {
    let f = Fixture::new().await;
    let mut c = card();
    assert_eq!(
        reason(&f.service, &c).await.as_deref(),
        Some("working_lane")
    );
    c["mentions"] = json!(["AGENT1"]);
    assert_eq!(
        reason(&f.service, &c).await.as_deref(),
        Some("issue_mention")
    );
    c["comments"] = json!([comment("agent1", "On it")]);
    assert_eq!(reason(&f.service, &c).await, None);
    c["comments"]
        .as_array_mut()
        .unwrap()
        .push(comment("human", "Any news?"));
    assert_eq!(
        reason(&f.service, &c).await.as_deref(),
        Some("issue_comment")
    );
    c["lane"] = json!("ready");
    let task = f
        .service
        .select_card(&typed_card(&c), false)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(task.trigger_reason.as_deref(), Some("ready_lane"));
    assert_eq!(
        (
            task.label.as_str(),
            task.provider.as_str(),
            task.assignee.as_deref()
        ),
        ("#1", "branvil", Some("agent1"))
    );
    c["comments"] = json!([comment("agent1", "Already answered")]);
    assert_eq!(reason(&f.service, &c).await, None);
    c["comments"][0]["created_at"] = json!("2026-01-01T00:00:00Z");
    assert_eq!(reason(&f.service, &c).await.as_deref(), Some("ready_lane"));
    f.running.stop().await;
}

#[tokio::test]
async fn interactive_and_scope_guards_cover_every_trigger() {
    let f = Fixture::new().await;
    for (lane, mentions, comments, assignee, expected) in [
        ("ready", json!([]), json!([]), "agent1", "ready_lane"),
        (
            "in_progress",
            json!([]),
            json!([]),
            "agent1",
            "working_lane",
        ),
        (
            "in_progress",
            json!([]),
            json!([comment("agent1", "Question"), comment("human", "Yes")]),
            "agent1",
            "issue_comment",
        ),
        (
            "in_progress",
            json!(["agent1"]),
            json!([]),
            "agent1",
            "issue_mention",
        ),
        (
            "new",
            json!([]),
            json!([{"author":"human","body":"summons","mentions":["agent1"]}]),
            "human",
            "issue_mention",
        ),
    ] {
        let mut c = card();
        c["lane"] = json!(lane);
        c["mentions"] = mentions;
        c["comments"] = comments;
        c["assignee"]["name"] = json!(assignee);
        assert_eq!(reason(&f.service, &c).await.as_deref(), Some(expected));
        for field in ["interactive", "interactive_ancestor"] {
            c[field] = if field == "interactive" {
                json!(true)
            } else {
                json!({"url":"grandparent"})
            };
            assert_eq!(reason(&f.service, &c).await, None);
            c[field] = if field == "interactive" {
                json!(false)
            } else {
                Value::Null
            };
            assert_eq!(reason(&f.service, &c).await.as_deref(), Some(expected));
        }
    }
    for patch in [
        json!({"state":"closed"}),
        json!({"repository":"other/repo"}),
        json!({"lane":"new"}),
        json!({"assignee":{"name":"human"}}),
    ] {
        let mut c = card();
        c.as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        assert_eq!(reason(&f.service, &c).await, None);
    }
    f.running.stop().await;
}

#[tokio::test]
async fn resolved_mentions_and_retained_status_determine_selection() {
    let f = Fixture::new().await;
    for (body, mentions, expected) in [
        ("@agent1", json!([]), "working_lane"),
        ("Stored summons", json!(["AGENT1"]), "issue_mention"),
    ] {
        let mut c = card();
        c["body"] = json!(body);
        c["mentions"] = mentions;
        assert_eq!(reason(&f.service, &c).await.as_deref(), Some(expected));
    }
    for held in ["failed", "rate_limited"] {
        for timestamp in [Value::Null, json!("not-a-time")] {
            let mut c = card();
            let mut last = comment("agent1", "Held");
            last["created_at"] = timestamp;
            last["meta"] = json!({"kind":"workflow_error","reason":held,"person_id":"aiko","run_id":"r","retry_after_at":"2100-01-01T00:00:00Z"});
            let mut earlier = comment("human", "Before assignment");
            earlier["created_at"] = json!("2026-01-01T00:00:00Z");
            c["comments"] = json!([last, earlier]);
            c["pull_requests"] = json!([{"merged":true}]);
            assert_eq!(reason(&f.service, &c).await, None);
            c["comments"]
                .as_array_mut()
                .unwrap()
                .push(comment("human", "Try again"));
            assert_eq!(
                reason(&f.service, &c).await.as_deref(),
                Some("issue_comment")
            );
        }
    }
    f.running.stop().await;
}

#[tokio::test]
async fn assignment_filters_mentions_without_dropping_visible_comments() {
    let f = Fixture::new().await;
    for pull in [Value::Null, json!({"state":"open"}), json!({"merged":true})] {
        for (timestamp, before) in [
            (json!("2026-01-01T00:00:00Z"), true),
            (json!("2026-01-02T00:00:00Z"), false),
            (json!("2026-01-03T00:00:00Z"), false),
            (Value::Null, false),
            (json!("not-a-time"), false),
        ] {
            let mut c = card();
            let mut msg = comment("human", "Earlier request");
            msg["mentions"] = json!(["agent1"]);
            msg["created_at"] = timestamp;
            c["comments"] = json!([msg]);
            if !pull.is_null() {
                c["pull_requests"] = json!([pull]);
            }
            let task = f.service.select_card(&typed_card(&c), false).await.unwrap();
            let expected = if !before {
                Some("issue_comment")
            } else if pull.is_null() {
                Some("working_lane")
            } else {
                None
            };
            assert_eq!(
                task.as_ref().and_then(|t| t.trigger_reason.as_deref()),
                expected
            );
            if let Some(t) = task {
                assert_eq!(t.comments[0].content, "Earlier request");
            }
        }
    }
    f.running.stop().await;
}

#[tokio::test]
async fn creation_assignees_labels_status_and_parent_preserve_contract() {
    let f = Fixture::new().await;
    let mut request = f.request();
    request.add_to_board = false;
    assert!(f.service.create_ticket(request).await.is_err());
    let url = f.create().await;
    let read = f.read(&url).await;
    assert_eq!(
        (
            read["lane"].clone(),
            read["key"].clone(),
            read["tracker"].clone(),
            read["repository"].clone()
        ),
        (
            json!("new"),
            json!("1"),
            json!("test/repo"),
            json!("test/repo")
        )
    );
    assert_eq!(read["assignees"], json!([]));
    f.owner
        .tickets_set_assignee("test/repo", "1", Some("agent1".into()))
        .await
        .unwrap();
    assert_eq!(f.read(&url).await["assignees"], json!(["agent1"]));
    f.owner
        .tickets_set_assignee("test/repo", "1", None)
        .await
        .unwrap();
    assert_eq!(f.read(&url).await["assignees"], json!([]));
    let mut valid = f.request();
    valid.labels = vec!["bug".into()];
    assert!(f.service.create_ticket(valid).await.is_ok());
    let mut invalid = f.request();
    invalid.labels = vec!["docs".into()];
    assert!(f.service.create_ticket(invalid).await.is_err());
    assert!(
        f.service
            .update_ticket(
                &url,
                UpdateTicket {
                    add_labels: vec!["docs".into()],
                    ..Default::default()
                }
            )
            .await
            .is_err()
    );
    let authored = f
        .owner
        .tickets_create_ticket(
            "test/repo",
            decode(json!({"title":"Human report","body":""})).unwrap(),
        )
        .await
        .unwrap()
        .url;
    let status: WorkflowStatus =
        decode(json!({"reason":"failed","person_id":"aiko","run_id":"r","subject":url})).unwrap();
    TicketService::comment(&f.service, &authored, "It failed", Some(&status))
        .await
        .unwrap();
    TicketService::comment(&f.service, &authored, "Plain", None)
        .await
        .unwrap();
    let comments = TicketService::read(&f.service, Resource::TicketComments, &authored, None, "")
        .await
        .unwrap();
    assert_eq!(comments.items()[0]["body"], "@owner1\n\nIt failed");
    let page = f
        .owner
        .page("parity-status".into(), Some(100), None)
        .unwrap();
    let raw = f
        .owner
        .tickets_list_comments("test/repo", authored.rsplit('/').next().unwrap(), page)
        .await
        .unwrap();
    assert_eq!(raw.items[0].meta.as_ref(), Some(&status.payload()));
    assert_eq!(comments.items()[1]["body"], "Plain");
    assert!(raw.items[1].meta.is_none());
    let child = f.create().await;
    f.service
        .update_ticket(
            &child,
            UpdateTicket {
                parent: Some(url.clone()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    f.service
        .update_ticket(
            &child,
            UpdateTicket {
                title: Some("Renamed".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let read = f.read(&child).await;
    assert_eq!(read["title"], "Renamed");
    assert_eq!(read["parent"]["url"], url);
    assert_eq!(read["parent"]["label"], "#1");
    f.running.stop().await;
}

#[tokio::test]
async fn blocked_ready_work_and_cross_workspace_mutations_are_refused() {
    let f = Fixture::new().await;
    let ready = f.assigned("ready").await;
    let working = f.assigned("in_progress").await;
    let blocker = f.create().await;
    for url in [&ready, &working] {
        f.service
            .update_ticket(
                url,
                UpdateTicket {
                    add_blocked_by: vec![blocker.clone()],
                    ..Default::default()
                },
            )
            .await
            .unwrap();
    }
    let tasks = f.service.candidates().await.unwrap();
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].url.as_deref(), Some(working.as_str()));
    assert_eq!(tasks[0].trigger_reason.as_deref(), Some("working_lane"));
    let task = Task {
        url: Some(ready.clone()),
        ..Default::default()
    };
    assert!(f.service.refresh(&task).await.unwrap().is_none());
    assert!(
        f.service
            .move_ticket(&task, MoveLane::InProgress)
            .await
            .is_err()
    );
    let foreign = f
        .owner
        .tickets_create_ticket(
            "other/repo",
            decode(json!({"title":"Foreign","body":"","assignee":"agent1","lane":"ready"}))
                .unwrap(),
        )
        .await
        .unwrap()
        .url;
    assert!(
        !f.service
            .candidates()
            .await
            .unwrap()
            .iter()
            .any(|t| t.url.as_deref() == Some(&foreign))
    );
    assert!(
        f.service
            .refresh(&Task {
                url: Some(foreign.clone()),
                ..Default::default()
            })
            .await
            .unwrap()
            .is_none()
    );
    assert!(f.read(&foreign).await["lane"].is_null());
    assert!(
        f.service
            .move_ticket(
                &Task {
                    url: Some(foreign.clone()),
                    ..Default::default()
                },
                MoveLane::Done
            )
            .await
            .is_err()
    );
    let before = f.read(&ready).await;
    for request in [
        UpdateTicket {
            parent: Some(foreign.clone()),
            ..Default::default()
        },
        UpdateTicket {
            add_blocked_by: vec![foreign.clone()],
            ..Default::default()
        },
        UpdateTicket {
            remove_blocked_by: vec![foreign.clone()],
            ..Default::default()
        },
    ] {
        assert!(f.service.update_ticket(&ready, request).await.is_err());
        assert_eq!(f.read(&ready).await, before);
    }
    assert_eq!(
        f.owner
            .tickets_get_ticket("other/repo", "1")
            .await
            .unwrap()
            .lane,
        "ready"
    );
    f.running.stop().await;
}

#[tokio::test]
async fn edited_and_deleted_comments_are_reread_before_dispatch() {
    let f = Fixture::new().await;
    let url = f.assigned("in_progress").await;
    let key = url.rsplit('/').next().unwrap();
    let original = f
        .owner
        .tickets_add_comment(
            "test/repo",
            key,
            decode(json!({"body":"Original"})).unwrap(),
        )
        .await
        .unwrap();
    let task = f.service.candidates().await.unwrap().remove(0);
    assert_eq!(task.comments[0].content, "Original");
    f.owner
        .tickets_change_comment("test/repo", key, &original.id, Some("Edited".into()))
        .await
        .unwrap();
    let page = TicketService::read(&f.service, Resource::TicketComments, &url, None, "")
        .await
        .unwrap();
    assert_eq!(page.items()[0]["id"], original.id);
    assert_eq!(page.items()[0]["body"], "Edited");
    assert_eq!(page.items()[0]["created_at"], original.created_at);
    assert_eq!(
        f.service.refresh(&task).await.unwrap().unwrap().comments[0].content,
        "Edited"
    );
    let held: WorkflowStatus =
        decode(json!({"reason":"failed","person_id":"aiko","run_id":"r"})).unwrap();
    let failed = TicketService::comment(&f.service, &url, "Failed", Some(&held))
        .await
        .unwrap();
    assert!(f.service.refresh(&task).await.unwrap().is_none());
    f.owner
        .tickets_change_comment("test/repo", key, &failed.id, None)
        .await
        .unwrap();
    assert_eq!(
        f.service.refresh(&task).await.unwrap().unwrap().comments[0].content,
        "Edited"
    );
    f.owner
        .tickets_change_comment("test/repo", key, &original.id, None)
        .await
        .unwrap();
    assert!(
        TicketService::read(&f.service, Resource::TicketComments, &url, None, "")
            .await
            .unwrap()
            .items()
            .is_empty()
    );
    assert!(
        f.service
            .refresh(&task)
            .await
            .unwrap()
            .unwrap()
            .comments
            .is_empty()
    );
    assert!(f.service.candidates().await.unwrap()[0].comments.is_empty());
    f.running.stop().await;
}

#[tokio::test]
async fn other_assignee_mentions_preserve_human_assignment_and_lane() {
    let f = Fixture::new().await;
    let url = f.create().await;
    let key = url.rsplit('/').next().unwrap();
    f.owner
        .tickets_set_assignee("test/repo", key, Some("owner1".into()))
        .await
        .unwrap();
    f.owner
        .tickets_add_comment(
            "test/repo",
            key,
            decode(json!({"body":"@agent1 Please help"})).unwrap(),
        )
        .await
        .unwrap();
    for count in [1, 59] {
        for _ in 0..count {
            f.create().await;
        }
        let tasks = f.service.candidates().await.unwrap();
        assert_eq!(tasks.len(), 1);
        assert_eq!(tasks[0].url.as_deref(), Some(url.as_str()));
        assert_eq!(tasks[0].assignee, None);
        assert_eq!(tasks[0].trigger_reason.as_deref(), Some("issue_mention"));
    }
    let task = f.service.candidates().await.unwrap().remove(0);
    TicketService::comment(&f.service, &url, "Context provided", None)
        .await
        .unwrap();
    f.owner
        .tickets_add_comment("test/repo", key, decode(json!({"body":"Thanks"})).unwrap())
        .await
        .unwrap();
    assert!(f.service.refresh(&task).await.unwrap().is_none());
    let read = f.owner.tickets_get_ticket("test/repo", key).await.unwrap();
    assert_eq!(read.assignee.unwrap().name, "owner1");
    assert_eq!(read.lane, "new");
    f.running.stop().await;
}

#[tokio::test]
async fn interactive_ancestors_block_candidates_and_refresh_for_every_trigger() {
    let f = Fixture::new().await;
    for (lane, body, comments, assignee, expected) in [
        ("ready", "", vec![], "agent1", "ready_lane"),
        ("in_progress", "", vec![], "agent1", "working_lane"),
        (
            "in_progress",
            "",
            vec![("agent1", "Question"), ("owner1", "Yes")],
            "agent1",
            "issue_comment",
        ),
        (
            "in_progress",
            "@agent1 Please",
            vec![],
            "agent1",
            "issue_mention",
        ),
        (
            "new",
            "",
            vec![("owner1", "@agent1 Please")],
            "owner1",
            "issue_mention",
        ),
    ] {
        for depth in [0, 2] {
            let url = f.assigned(lane).await;
            let key = url.rsplit('/').next().unwrap();
            f.owner
                .tickets_set_assignee("test/repo", key, Some(assignee.into()))
                .await
                .unwrap();
            f.service
                .update_ticket(
                    &url,
                    UpdateTicket {
                        body: Some(body.into()),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            for (author, text) in &comments {
                if *author == "agent1" {
                    TicketService::comment(&f.service, &url, text, None)
                        .await
                        .unwrap();
                } else {
                    f.owner
                        .tickets_add_comment(
                            "test/repo",
                            key,
                            decode(json!({"body":text})).unwrap(),
                        )
                        .await
                        .unwrap();
                }
            }
            let mut holder = url.clone();
            for _ in 0..depth {
                let parent = f.create().await;
                f.service
                    .update_ticket(
                        &holder,
                        UpdateTicket {
                            parent: Some(parent.clone()),
                            ..Default::default()
                        },
                    )
                    .await
                    .unwrap();
                holder = parent;
            }
            let task = f
                .service
                .candidates()
                .await
                .unwrap()
                .into_iter()
                .find(|t| t.url.as_deref() == Some(&url))
                .unwrap();
            assert_eq!(task.trigger_reason.as_deref(), Some(expected));
            f.service
                .update_ticket(
                    &holder,
                    UpdateTicket {
                        interactive: Some(true),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            assert!(
                !f.service
                    .candidates()
                    .await
                    .unwrap()
                    .iter()
                    .any(|t| t.url.as_deref() == Some(&url))
            );
            assert!(f.service.refresh(&task).await.unwrap().is_none());
            assert_eq!(f.read(&url).await["lane"], lane);
            f.service
                .update_ticket(
                    &holder,
                    UpdateTicket {
                        interactive: Some(false),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            assert_eq!(
                f.service
                    .refresh(&task)
                    .await
                    .unwrap()
                    .unwrap()
                    .trigger_reason
                    .as_deref(),
                Some(expected)
            );
            assert!(
                f.service
                    .candidates()
                    .await
                    .unwrap()
                    .iter()
                    .any(|t| t.url.as_deref() == Some(&url))
            );
            f.service
                .update_ticket(
                    &url,
                    UpdateTicket {
                        state: Some("closed".into()),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
        }
    }
    f.running.stop().await;
}

#[tokio::test]
async fn canonical_ticket_addresses_fail_without_mutation() {
    let f = Fixture::new().await;
    let url = f.create().await;
    let base = f.running.running.data.base_url();
    for address in [
        format!("{base}/test/repo/-/tickets/01"),
        format!("{base}/test/repo/-/tickets/0"),
        format!("{base}/test/repo/-/tickets/DEMO-1"),
        format!("{url}?x=1"),
        format!("{base}/test/repo/-/pulls/1"),
        format!("{base}/test/-/tickets/1"),
    ] {
        assert_eq!(
            TicketService::comment(&f.service, &address, "Hi", None)
                .await
                .unwrap_err(),
            diagnostic(
                "integrations.branvil.api.not_ticket",
                [("url", address.clone())]
            )
        );
    }
    assert!(
        TicketService::read(&f.service, Resource::TicketComments, &url, None, "")
            .await
            .unwrap()
            .items()
            .is_empty()
    );
    f.running.stop().await;
}

impl Fixture {
    fn git(&self, args: &[&str]) -> String {
        self.running.git("test/repo", args)
    }
    async fn linked(&self, url: Option<&str>, closes: bool, merged: bool) -> String {
        let sequence = self
            .sequence
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let branch = format!("feature-{sequence}");
        let mut base = self.git(&["for-each-ref", "--format=%(objectname)", "refs/heads/main"]);
        if base.is_empty() {
            let tree = self.git(&["hash-object", "-t", "tree", "-w", "--stdin"]);
            base = self.git(&["commit-tree", &tree, "-m", "base"]);
            self.git(&["update-ref", "refs/heads/main", &base]);
        }
        let head = self.running.commit(
            "test/repo",
            &branch,
            Some(&base),
            &format!("feature-{sequence}.txt"),
            &format!("feature {sequence}"),
        );
        let pull = self.service.member().await.unwrap().pulls_create("test/repo",decode(json!({"head":branch,"base":"main","title":"Work","body":"","tickets":url.into_iter().map(|u|json!({"url":u,"closes":closes})).collect::<Vec<_>>()})).unwrap()).await.unwrap();
        if merged {
            let n = pull.pull.url.rsplit('/').next().unwrap().parse().unwrap();
            self.owner
                .pulls_review(
                    "test/repo",
                    n,
                    decode(json!({"verdict":"approve","commit":head})).unwrap(),
                )
                .await
                .unwrap();
            self.owner
                .pulls_merge(
                    "test/repo",
                    n,
                    decode(json!({"expected_head":head,"expected_base":base})).unwrap(),
                )
                .await
                .unwrap();
        }
        pull.pull.url
    }
}

#[tokio::test]
async fn linked_pulls_yield_to_conversations_and_finish_only_after_answer() {
    let f = Fixture::new().await;
    for merged in [false, true] {
        for mention in [false, true] {
            let url = f.assigned("in_progress").await;
            let key = url.rsplit('/').next().unwrap();
            if mention {
                f.service
                    .update_ticket(
                        &url,
                        UpdateTicket {
                            body: Some("@agent1 Please".into()),
                            ..Default::default()
                        },
                    )
                    .await
                    .unwrap();
            } else {
                TicketService::comment(&f.service, &url, "Question", None)
                    .await
                    .unwrap();
                f.owner
                    .tickets_add_comment("test/repo", key, decode(json!({"body":"Yes"})).unwrap())
                    .await
                    .unwrap();
            }
            f.linked(Some(&url), false, merged).await;
            let task = f.service.candidates().await.unwrap().remove(0);
            assert_eq!(
                f.service
                    .refresh(&task)
                    .await
                    .unwrap()
                    .unwrap()
                    .trigger_reason
                    .as_deref(),
                Some(if mention {
                    "issue_mention"
                } else {
                    "issue_comment"
                })
            );
            assert_eq!(f.read(&url).await["lane"], "in_progress");
            let completed: WorkflowStatus = decode(json!({
                "reason":"completed", "person_id":"agent1", "run_id":"done",
                "work_kind":"ticket", "work_identity":url
            }))
            .unwrap();
            TicketService::comment(&f.service, &url, "Done", Some(&completed))
                .await
                .unwrap();
            assert!(f.service.refresh(&task).await.unwrap().is_none());
            assert_eq!(
                f.read(&url).await["lane"],
                if merged { "done" } else { "in_progress" }
            );
            f.service
                .update_ticket(
                    &url,
                    UpdateTicket {
                        state: Some("closed".into()),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
        }
    }
    for closes in [false, true] {
        for merged in [false, true] {
            let url = f.assigned("in_progress").await;
            let completed: WorkflowStatus = decode(json!({
                "reason":"completed", "person_id":"agent1", "run_id":"done",
                "work_kind":"ticket", "work_identity":url
            }))
            .unwrap();
            TicketService::comment(&f.service, &url, "Done", Some(&completed))
                .await
                .unwrap();
            f.linked(Some(&url), closes, merged).await;
            assert!(f.service.candidates().await.unwrap().is_empty());
            let read = f.read(&url).await;
            assert_eq!(read["lane"], if merged { "done" } else { "in_progress" });
            assert_eq!(
                read["state"],
                if merged && closes { "closed" } else { "open" }
            );
            f.service
                .update_ticket(
                    &url,
                    UpdateTicket {
                        state: Some("closed".into()),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
        }
    }
    f.running.stop().await;
}

#[tokio::test]
async fn assignment_timing_is_shared_by_candidate_and_refresh_reads() {
    let f = Fixture::new().await;
    for linked in [None, Some(false), Some(true)] {
        for before in [false, true] {
            for refresh in [false, true] {
                let url = f.create().await;
                let key = url.rsplit('/').next().unwrap();
                if before {
                    f.owner
                        .tickets_add_comment(
                            "test/repo",
                            key,
                            decode(json!({"body":"@agent1 earlier request"})).unwrap(),
                        )
                        .await
                        .unwrap();
                }
                if let Some(merged) = linked {
                    f.linked(Some(&url), false, merged).await;
                }
                f.owner
                    .tickets_set_assignee("test/repo", key, Some("agent1".into()))
                    .await
                    .unwrap();
                f.owner
                    .tickets_put_on_board(
                        "test/repo",
                        key,
                        decode(json!({"lane":"in_progress"})).unwrap(),
                    )
                    .await
                    .unwrap();
                if !before {
                    f.owner
                        .tickets_add_comment(
                            "test/repo",
                            key,
                            decode(json!({"body":"@agent1 later request"})).unwrap(),
                        )
                        .await
                        .unwrap();
                }
                let tasks = if refresh {
                    f.service
                        .refresh(&Task {
                            url: Some(url.clone()),
                            ..Default::default()
                        })
                        .await
                        .unwrap()
                        .into_iter()
                        .collect::<Vec<_>>()
                } else {
                    f.service.candidates().await.unwrap()
                };
                let expected = if !before {
                    Some("issue_comment")
                } else if linked.is_none() {
                    Some("working_lane")
                } else {
                    None
                };
                assert_eq!(
                    tasks.first().and_then(|t| t.trigger_reason.as_deref()),
                    expected
                );
                if let Some(task) = tasks.first() {
                    assert_eq!(task.comments.len(), 1);
                }
                assert_eq!(f.read(&url).await["lane"], "in_progress");
                f.service
                    .update_ticket(
                        &url,
                        UpdateTicket {
                            state: Some("closed".into()),
                            ..Default::default()
                        },
                    )
                    .await
                    .unwrap();
            }
        }
    }
    f.running.stop().await;
}

#[tokio::test]
async fn closed_items_include_unboarded_pulls_and_only_workspace_tickets() {
    let f = Fixture::new().await;
    let pull = f.linked(None, false, true).await;
    let start = chrono::Utc::now() - chrono::Duration::days(3650);
    let end = chrono::Utc::now() + chrono::Duration::days(3650);
    let items = f.service.closed_since(start, end).await.unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].url, pull);
    let closed = f.create().await;
    f.service
        .update_ticket(
            &closed,
            UpdateTicket {
                state: Some("closed".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let foreign = f
        .owner
        .tickets_create_ticket(
            "other/repo",
            decode(json!({"title":"Elsewhere","body":""})).unwrap(),
        )
        .await
        .unwrap();
    f.owner
        .tickets_update_ticket(
            "other/repo",
            &foreign.key,
            decode(json!({"state":"closed"})).unwrap(),
        )
        .await
        .unwrap();
    let actual: std::collections::BTreeSet<_> = f
        .service
        .closed_since(start, end)
        .await
        .unwrap()
        .into_iter()
        .map(|i| i.url)
        .collect();
    assert_eq!(actual, [pull, closed].into());
    f.running.stop().await;
}

#[tokio::test]
async fn ready_candidates_precede_working_and_sort_by_creation() {
    let f = Fixture::new().await;
    let working = f.assigned("in_progress").await;
    let old = f.assigned("ready").await;
    let new = f.assigned("ready").await;
    let mut expected = vec![old, new];
    for _ in 0..10 {
        tokio::time::sleep(std::time::Duration::from_millis(2)).await;
        expected.push(f.assigned("ready").await);
    }
    expected.push(working);
    let urls: Vec<_> = f
        .service
        .candidates()
        .await
        .unwrap()
        .into_iter()
        .map(|t| t.url.unwrap())
        .collect();
    assert_eq!(urls, expected);
    f.running.stop().await;
}

#[test]
fn ticket_mentions_are_words_with_frozen_name_boundaries() {
    for body in ["cc @AIKO.", "@aiko, see this"] {
        assert!(mentions(body, "aiko"));
    }
    for body in [
        "cc @aiko-bot",
        "cc @aiko.bot",
        "mail aiko@example.com @aikox",
    ] {
        assert!(!mentions(body, "aiko"));
    }
}

#[tokio::test]
async fn only_member_open_board_work_is_selected_on_both_reads() {
    let f = Fixture::new().await;
    let theirs = f.assigned("ready").await;
    f.owner
        .tickets_set_assignee("test/repo", "1", Some("owner1".into()))
        .await
        .unwrap();
    let closed = f.assigned("ready").await;
    f.service
        .update_ticket(
            &closed,
            UpdateTicket {
                state: Some("closed".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let new = f.assigned("new").await;
    assert!(f.service.candidates().await.unwrap().is_empty());
    for url in [theirs, new, closed] {
        assert!(
            f.service
                .refresh(&Task {
                    url: Some(url),
                    ..Default::default()
                })
                .await
                .unwrap()
                .is_none()
        );
    }
    f.running.stop().await;
}

#[tokio::test]
async fn repository_ticket_numbers_and_assignee_projection_are_independent() {
    let f = Fixture::new().await;
    let foreign = f
        .owner
        .tickets_create_ticket(
            "other/repo",
            decode(json!({"title":"Elsewhere","body":""})).unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(foreign.key, "1");
    let url = f.create().await;
    assert_eq!(
        url,
        format!(
            "{}/test/repo/-/tickets/1",
            f.running.running.data.base_url()
        )
    );
    assert_eq!(f.read(&url).await["label"], "#1");
    assert_eq!(f.read(&url).await["author"], "agent1");
    for (assigned, expected) in [
        (None, json!([])),
        (Some("agent1"), json!(["agent1"])),
        (Some("owner1"), json!(["owner1"])),
    ] {
        f.owner
            .tickets_set_assignee("test/repo", "1", assigned.map(Into::into))
            .await
            .unwrap();
        assert_eq!(f.read(&url).await["assignees"], expected);
        assert_eq!(f.read(&url).await["author"], "agent1");
    }
    f.running.stop().await;
}

fn typed_card(v: &Value) -> tickets::CardView {
    let strv = |k: &str| text(v, k).to_owned();
    let refs = |k: &str| {
        v[k].as_array()
            .into_iter()
            .flatten()
            .map(|r| tickets::TicketRef {
                url: text(r, "url").into(),
                repository: text(r, "repository").into(),
                key: text(r, "key").into(),
                title: text(r, "title").into(),
                state: text(r, "state").into(),
                state_reason: None,
            })
            .collect()
    };
    tickets::CardView {
        ticket: tickets::TicketView {
            repair: v["repair"] == true,
            url: strv("url"),
            repository: strv("repository"),
            key: strv("key"),
            title: strv("title"),
            body: strv("body"),
            mentions: serde_json::from_value(v["mentions"].clone()).unwrap_or_default(),
            state: strv("state"),
            state_reason: None,
            author: strv("author"),
            assignee: v["assignee"].is_object().then(|| crate::model::Assignee {
                name: text(&v["assignee"], "name").into(),
                assigned_at: text(&v["assignee"], "assigned_at").into(),
            }),
            labels: vec![],
            interactive: v["interactive"] == true,
            interactive_ancestor: v["interactive_ancestor"].is_object().then(|| {
                tickets::TicketRef {
                    url: String::new(),
                    repository: String::new(),
                    key: String::new(),
                    title: String::new(),
                    state: String::new(),
                    state_reason: None,
                }
            }),
            upstream_issue: None,
            lane: strv("lane"),
            rank: strv("rank"),
            parent: None,
            children: vec![],
            blocked_by: refs("blocked_by"),
            blocks: vec![],
            created_at: strv("created_at"),
            updated_at: strv("updated_at"),
            closed_at: None,
            revision: 1,
        },
        comments: v["comments"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|c| crate::api::CommentView {
                id: text(c, "id").into(),
                author: text(c, "author").into(),
                body: text(c, "body").into(),
                mentions: serde_json::from_value(c["mentions"].clone()).ok(),
                meta: Some(c["meta"].clone()),
                created_at: text(c, "created_at").into(),
                reactions: vec![],
                url: text(c, "url").into(),
            })
            .collect(),
        pull_requests: v["pull_requests"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|p| tickets::LinkedPull {
                url: text(p, "url").into(),
                label: text(p, "label").into(),
                title: text(p, "title").into(),
                state: text(p, "state").into(),
                merged: p["merged"] == true,
                closes: None,
                updated_at: text(p, "updated_at").into(),
            })
            .collect(),
    }
}

use crate::test_support::{Running, initialize};
#[tokio::test]
async fn ticket_task_comments_use_roster_names_and_role_fallbacks() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(crate::vault::Memory::default()),
    )
    .await
    .unwrap();
    let mut service = TicketAgent::new(
        running.data.clone(),
        "test".into(),
        "aiko-service".into(),
        "Aiko".into(),
    );
    for key in ["1", "9999999999"] {
        assert!(
            service
                .item(
                    &format!("{}/test/repo/-/pulls/{key}", running.data.base_url()),
                    "pulls"
                )
                .is_ok()
        );
    }
    for key in ["0", "01", "+1", "10000000000", "-1", "１"] {
        assert!(
            service
                .item(
                    &format!("{}/test/repo/-/pulls/{key}", running.data.base_url()),
                    "pulls"
                )
                .is_err(),
            "{key}"
        );
    }
    // Supply the roster at the same boundary as the integration factory.
    service.members = [
        ("aiko-service".into(), "Aiko".into()),
        ("otota".into(), "Otota".into()),
        ("strasse".into(), "Street".into()),
        ("nameless".into(), String::new()),
    ]
    .into();
    let card = json!({"repository":"test/repo","state":"open","lane":"ready","assignee":{"name":"aiko-service"},"interactive_ancestor":null,"comments":[
        {"author":"AIKO-SERVICE","body":"started"},
        {"author":"OTOTA","body":"continue"},
        {"author":"external","body":"hello"},
        {"author":"Straße","body":"hello"},
        {"author":"nameless","body":"hello"},
        {"author":"","body":"anonymous"}
    ]});
    let task = service
        .select_card(&typed_card(&card), false)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        task.comments
            .iter()
            .map(|c| (c.author.as_str(), c.author_type.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("Aiko", "Assistant"),
            ("Otota", "User"),
            ("User", "User"),
            ("Street", "User"),
            ("User", "User"),
            ("", "")
        ]
    );
    service.person_id = "Straße".into();
    assert_eq!(service.author_type("STRASSE"), "Assistant");
    service.person_id.clear();
    assert_eq!(service.author_type(""), "");
    running.stop().await.unwrap();
}

#[tokio::test]
async fn a_roster_member_that_never_acted_can_be_assigned() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(crate::vault::Memory::default()),
    )
    .await
    .unwrap();
    let data = &running.data;
    let owner = data.owner();
    owner
        .repos_create_workspace(decode(json!({"name":"test"})).unwrap())
        .await
        .unwrap();
    owner
        .repos_create(decode(json!({"path":"test/repo"})).unwrap())
        .await
        .unwrap();
    // The roster names the owner (the human), the acting agent, an agent
    // that has never acted, and a name no member may have.
    let roster = [
        ("owner1", "Owner"),
        ("aiko", "Aiko"),
        ("kenji", "Kenji"),
        ("branvil", "Reserved"),
    ]
    .map(|(login, name)| (login.to_owned(), name.to_owned()))
    .into();
    let service = TicketAgent::new(data.clone(), "test".into(), "aiko".into(), "Aiko".into())
        .with_members(&roster);
    let created = service
        .create_ticket(CreateTicket {
            tracker: "test/repo".into(),
            title: "for kenji".into(),
            body: String::new(),
            labels: vec![],
            add_to_board: true,
            interactive: false,
            assignee: Some(ActorName("kenji".into())),
            lane: None,
            parent: None,
            blocked_by: vec![],
        })
        .await
        .unwrap();
    let ticket = service.ticket_view(&created.target.url).await.unwrap();
    assert_eq!(ticket.assignee.unwrap().name, "kenji");
    let kenji = owner.members_get("kenji".into()).await.unwrap();
    assert_eq!(
        (kenji.kind, kenji.display_name.as_str()),
        (crate::api::MemberKind::Agent, "Kenji")
    );
    assert_eq!(
        owner.members_get("owner1".into()).await.unwrap().kind,
        crate::api::MemberKind::Owner
    );
    assert_eq!(
        owner
            .members_get("branvil".into())
            .await
            .err()
            .unwrap()
            .code,
        "not_found"
    );
    running.stop().await.unwrap();
}

#[path = "contracts.rs"]
mod contracts;
struct Driver<'a>(&'a Fixture);
impl contracts::TicketContractDriver for Driver<'_> {
    fn human_comment<'a>(&'a self, url: &'a str, body: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            self.0
                .owner
                .tickets_add_comment(
                    "test/repo",
                    url.rsplit('/').next().unwrap(),
                    decode(json!({"body":body})).unwrap(),
                )
                .await
                .map_err(|e| error(e, "comment"))?;
            Ok(())
        })
    }
    fn assign<'a>(&'a self, url: &'a str, actor: &'a ActorName) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let k = url.rsplit('/').next().unwrap();
            self.0
                .owner
                .tickets_set_assignee("test/repo", k, Some(actor.0.clone()))
                .await
                .map_err(|e| error(e, "assign"))?;
            self.0
                .owner
                .tickets_put_on_board("test/repo", k, decode(json!({"lane":"ready"})).unwrap())
                .await
                .map_err(|e| error(e, "move"))?;
            Ok(())
        })
    }
    fn merge_reference<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            self.0.linked(Some(url), false, true).await;
            Ok(())
        })
    }
}
#[tokio::test]
async fn ticket_behavior_contract() {
    let f = Fixture::new().await;
    contracts::ticket_contract(&f.service, "test/repo", "bug", "agent1").await;
    f.running.stop().await;
}
#[tokio::test]
async fn ticket_patrol_contract() {
    let f = Fixture::new().await;
    contracts::ticket_patrol_contract(
        &f.service,
        &Driver(&f),
        "test/repo",
        "bug",
        ActorName("agent1".into()),
        ActorName("owner1".into()),
    )
    .await;
    f.running.stop().await;
}
#[tokio::test]
async fn ticket_placement_contract() {
    let f = Fixture::new().await;
    f.owner
        .repos_create(decode(json!({"path":"test/other"})).unwrap())
        .await
        .unwrap();
    contracts::ticket_placement_tracker_contract(&f.service, "test/repo", "test/other").await;
    f.running.stop().await;
}
#[tokio::test]
async fn ticket_scope_contract() {
    let f = Fixture::new().await;
    let t = f
        .owner
        .tickets_create_ticket(
            "other/repo",
            decode(json!({"title":"Their ticket"})).unwrap(),
        )
        .await
        .unwrap();
    let c = f
        .owner
        .tickets_add_comment(
            "other/repo",
            &t.key,
            decode(json!({"body":"Their comment"})).unwrap(),
        )
        .await
        .unwrap();
    contracts::ticket_scope_contract(&f.service, "test/repo", "other/repo", &t.url, &c.id).await;
    f.running.stop().await;
}
#[tokio::test]
async fn ticket_wrong_subject_contract() {
    let f = Fixture::new().await;
    let url = f.linked(None, false, false).await;
    let c = f
        .owner
        .pulls_add_comment(
            "test/repo",
            url.rsplit('/').next().unwrap().parse().unwrap(),
            decode(json!({"body":"Wrong subject"})).unwrap(),
        )
        .await
        .unwrap();
    contracts::ticket_wrong_subject_contract(&f.service, &url, &c.id).await;
    f.running.stop().await;
}
#[tokio::test]
async fn ticket_attachment_contract() {
    let f = Fixture::new().await;
    let a = f
        .owner
        .attachments_upload(
            "test/repo",
            Some("screen.png".into()),
            b"\x89PNG\r\n\x1a\nscreen".to_vec(),
        )
        .await
        .unwrap();
    let b = f
        .owner
        .attachments_upload("test/repo", Some("log.gif".into()), b"GIF89alog".to_vec())
        .await
        .unwrap();
    let c = f
        .owner
        .attachments_upload(
            "test/repo",
            Some("unused.png".into()),
            b"\x89PNG\r\n\x1a\nunused".to_vec(),
        )
        .await
        .unwrap();
    contracts::ticket_attachment_contract(
        &f.service,
        "test/repo",
        contracts::TicketAttachmentFixture {
            screen_url: a.url,
            screen_id: a.id,
            log_url: b.url,
            log_id: b.id,
            unused_id: c.id,
        },
    )
    .await;
    f.running.stop().await;
}

#[tokio::test]
async fn recorded_remainder_survives_merge_and_plain_comments_until_completed() {
    let f = Fixture::new().await;
    let url = f.assigned("in_progress").await;
    f.linked(Some(&url), false, true).await;
    for outcome in ["work_remaining", "asking", "blocked", "completed"] {
        let status: WorkflowStatus = decode(json!({
            "reason": outcome, "person_id": "agent1", "run_id": outcome,
            "work_kind": "ticket", "work_identity": url
        }))
        .unwrap();
        TicketService::comment(&f.service, &url, "Outcome", Some(&status))
            .await
            .unwrap();
        TicketService::comment(&f.service, &url, "Additional detail", None)
            .await
            .unwrap();
        let tasks = f.service.candidates().await.unwrap();
        assert_eq!(
            tasks.iter().any(|t| t.url.as_deref() == Some(&url)),
            outcome == "work_remaining"
        );
        assert_eq!(
            f.read(&url).await["lane"],
            if outcome == "completed" {
                "done"
            } else {
                "in_progress"
            }
        );
        if outcome == "work_remaining" {
            let resumed = f
                .service
                .refresh(
                    tasks
                        .iter()
                        .find(|t| t.url.as_deref() == Some(&url))
                        .unwrap(),
                )
                .await
                .unwrap()
                .unwrap();
            assert_eq!(resumed.trigger_reason.as_deref(), Some("working_lane"));
        }
    }
    f.running.stop().await;
}

#[tokio::test]
async fn ticket_outcomes_require_current_assignee_and_assignment_history() {
    let f = Fixture::new().await;
    for (author, person, identity, timestamp, expected) in [
        (
            "agent1",
            "agent1",
            "http://unused/test/repo/-/tickets/1",
            "2026-01-03T00:00:00Z",
            Some("working_lane"),
        ),
        (
            "other",
            "agent1",
            "http://unused/test/repo/-/tickets/1",
            "2026-01-03T00:00:00Z",
            None,
        ),
        (
            "agent1",
            "other",
            "http://unused/test/repo/-/tickets/1",
            "2026-01-03T00:00:00Z",
            None,
        ),
        (
            "agent1",
            "agent1",
            "other-ticket",
            "2026-01-03T00:00:00Z",
            None,
        ),
        (
            "agent1",
            "agent1",
            "http://unused/test/repo/-/tickets/1",
            "2026-01-01T00:00:00Z",
            None,
        ),
    ] {
        let mut c = card();
        let mut outcome = comment(author, "Remainder");
        outcome["created_at"] = json!(timestamp);
        outcome["meta"] = json!({
            "kind":"workflow_error", "reason":"work_remaining", "person_id":person,
            "work_kind":"ticket", "work_identity":identity
        });
        c["comments"] = json!([outcome, comment("agent1", "Plain detail")]);
        c["pull_requests"] = json!([{"merged":true}]);
        assert_eq!(
            reason(&f.service, &c).await.as_deref(),
            expected,
            "{author}/{person}/{identity}/{timestamp}"
        );
    }
    f.running.stop().await;
}
