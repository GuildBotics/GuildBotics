//! Shared behavioral contracts, run against real hermetic provider fixtures.
use gb_ports::*;
use serde_json::{Value, json};

pub fn ticket_request(tracker: &str, title: &str) -> CreateTicket {
    CreateTicket {
        tracker: tracker.into(),
        title: title.into(),
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
async fn ticket_detail(t: &dyn TicketService, url: &str) -> Value {
    let p = t.read(Resource::Tickets, url, None, "").await.unwrap();
    assert_eq!(p.items().len(), 1);
    p.items()[0].clone()
}
async fn ticket_create(t: &dyn TicketService, tracker: &str, title: &str) -> String {
    t.create_ticket(ticket_request(tracker, title))
        .await
        .unwrap()
        .target
        .url
}
async fn tracker_items(t: &dyn TicketService, tracker: &str, parameters: &Value) -> Vec<Value> {
    let mut items = vec![];
    let mut cursor = String::new();
    loop {
        let p = t
            .read(Resource::TrackerTickets, tracker, Some(parameters), &cursor)
            .await
            .unwrap();
        assert!(p.target().is_none());
        items.extend_from_slice(p.items());
        match p.continuation() {
            Some(next) => {
                assert_ne!(next, cursor);
                cursor = next.into()
            }
            None => break,
        }
    }
    items
}
async fn task_for(t: &dyn TicketService, url: &str) -> Option<Task> {
    t.candidates()
        .await
        .unwrap()
        .into_iter()
        .find(|task| task.url.as_deref() == Some(url))
}
async fn finish_task(t: &dyn TicketService, url: &str) {
    let task = Task {
        url: Some(url.into()),
        ..Default::default()
    };
    assert!(t.move_ticket(&task, MoveLane::Done).await.unwrap());
}

pub async fn ticket_contract(t: &dyn TicketService, tracker: &str, label: &str) {
    // Read/write result shapes, label set semantics, comment IDs, and close idempotence.
    let mut request = ticket_request(tracker, "Fix login");
    request.body = "It fails.".into();
    request.labels = vec![label.into(), label.into()];
    let created = t.create_ticket(request).await.unwrap();
    assert_eq!(created.target.kind, WorkKind::Ticket);
    assert_eq!(created.target.title, "Fix login");
    assert_eq!(created.labels, vec![label]);
    let url = &created.target.url;
    let updated = t
        .update_ticket(
            url,
            UpdateTicket {
                title: Some("Fix the login".into()),
                body: Some("It still fails.".into()),
                remove_labels: vec!["missing".into()],
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(updated.target.title, "Fix the login");
    assert_eq!(updated.body, "It still fails.");
    assert!(!updated.state_changed);
    let written = t.comment(url, "Looking into it. \n", None).await.unwrap();
    let page = t.read(Resource::Tickets, url, None, "").await.unwrap();
    assert_eq!(page.target(), Some(&updated.target));
    assert_eq!(written.target, updated.target);
    let detail = &page.items()[0];
    assert_eq!(
        (
            detail["title"].as_str(),
            detail["body"].as_str(),
            detail["state"].as_str()
        ),
        (Some("Fix the login"), Some("It still fails."), Some("open"))
    );
    assert_eq!(detail["labels"], json!([label]));
    assert_eq!(detail["tracker"], tracker);
    assert!(detail["repository"].is_string());
    assert_eq!(detail["lane"], "new");
    assert!(!detail["key"].as_str().unwrap().is_empty());
    assert_eq!(detail["label"], created.target.label);
    let comments = t
        .read(Resource::TicketComments, url, None, "")
        .await
        .unwrap();
    assert_eq!(comments.items().len(), 1);
    assert_eq!(comments.items()[0]["id"], written.id);
    assert_eq!(comments.items()[0]["body"], "Looking into it.");
    assert_eq!(comments.items()[0]["author_type"], "Assistant");
    let reaction = t
        .add_reaction(url, &written.id, SemanticReaction::Support)
        .await
        .unwrap();
    assert_eq!(reaction.id, written.id);
    assert_eq!(reaction.reaction, SemanticReaction::Support);
    let removed = t
        .update_ticket(
            url,
            UpdateTicket {
                remove_labels: vec![label.into()],
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert!(removed.labels.is_empty());
    let before = ticket_detail(t, url).await;
    assert!(
        t.update_ticket(
            url,
            UpdateTicket {
                title: Some("must not persist".into()),
                add_labels: vec!["invented-undefined-label".into()],
                ..Default::default()
            }
        )
        .await
        .is_err()
    );
    assert_eq!(ticket_detail(t, url).await, before);
    let count = tracker_items(t, tracker, &json!({"state":"all","page_size":100}))
        .await
        .len();
    let mut invalid = ticket_request(tracker, "undefined");
    invalid.labels = vec!["invented-undefined-label".into()];
    assert!(t.create_ticket(invalid).await.is_err());
    assert_eq!(
        tracker_items(t, tracker, &json!({"state":"all","page_size":100}))
            .await
            .len(),
        count
    );
    let other = ticket_create(t, tracker, "Reaction bound to ticket").await;
    assert!(
        (t.add_reaction(&other, &written.id, SemanticReaction::Ack)
            .await
            .unwrap_err())
        .is_not_found()
    );
    assert_eq!(
        t.add_reaction(url, &written.id, SemanticReaction::Ack)
            .await
            .unwrap()
            .id,
        written.id
    );
    assert!(serde_json::from_value::<SemanticReaction>(json!("eyes")).is_err());
    for params in [
        json!({"state":"invalid"}),
        json!({"page_size":0}),
        json!({"page_size":101}),
        json!({"text":3}),
        json!({"label":[]}),
        json!({"node":"thread"}),
        json!({"q":"raw"}),
    ] {
        assert!(
            t.read(Resource::TrackerTickets, tracker, Some(&params), "")
                .await
                .is_err(),
            "{params}"
        );
    }
    assert!(
        t.read(Resource::TrackerTickets, url, None, "")
            .await
            .is_err()
    );
    assert!(
        t.read(
            Resource::TrackerTickets,
            tracker,
            None,
            "https://invalid.test/page"
        )
        .await
        .is_err()
    );
    assert!(t.read(Resource::Tickets, url, None, "2").await.is_err());
    assert!(matches!(
        t.read(Resource::PullRequests, url, None, "").await,
        Err(ServiceError::InvalidResource)
    ));
    assert!("not_a_resource".parse::<Resource>().is_err());
    let close = || UpdateTicket {
        state: Some("closed".into()),
        state_reason: Some("completed".into()),
        ..Default::default()
    };
    let first = t.update_ticket(url, close()).await.unwrap();
    let again = t.update_ticket(url, close()).await.unwrap();
    assert_eq!((&*first.state, first.state_changed), ("closed", true));
    assert_eq!((&*again.state, again.state_changed), ("closed", false));
    let closed = t
        .closed_since(
            "2026-01-01T00:00:00Z".parse().unwrap(),
            "2100-01-01T00:00:00Z".parse().unwrap(),
        )
        .await
        .unwrap();
    let item = closed.iter().find(|c| c.url == *url).unwrap();
    assert_eq!(item.kind, WorkKind::Ticket);
    assert_eq!(item.label, created.target.label);
    assert!(!item.merged);
    assert!(!closed.iter().any(|c| c.url == other));
    assert!(
        t.closed_since(
            "2000-01-01T00:00:00Z".parse().unwrap(),
            "2001-01-01T00:00:00Z".parse().unwrap()
        )
        .await
        .unwrap()
        .is_empty()
    );
    // Search is all-term title/body matching, not comment text; pagination retains order.
    let mut search = vec![];
    for (title, body, labels) in [
        ("Contractneedle Alpha", "secondword", vec![label.into()]),
        ("contractneedle Beta secondword", "", vec![label.into()]),
        ("Contractneedle Gamma", "SECONDWORD", vec![label.into()]),
        ("Contractneedle missing term", "", vec![label.into()]),
        ("Contractneedle without label", "secondword", vec![]),
    ] {
        let mut q = ticket_request(tracker, title);
        q.body = body.into();
        q.labels = labels;
        q.lane = Some(CreateLane::Ready);
        search.push(t.create_ticket(q).await.unwrap().target.url);
    }
    t.update_ticket(&search[2], close()).await.unwrap();
    t.comment(&search[3], "secondword", None).await.unwrap();
    let conditions =
        json!({"text":"  CONTRACTNEEDLE\tsecondword ","state":"all","label":label,"page_size":1});
    let found = tracker_items(t, tracker, &conditions).await;
    assert_eq!(
        found
            .iter()
            .map(|v| v["url"].as_str().unwrap())
            .collect::<Vec<_>>(),
        search[..3].iter().map(String::as_str).collect::<Vec<_>>()
    );
    assert_eq!(
        found
            .iter()
            .map(|v| v["state"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["open", "open", "closed"]
    );
    for v in &found {
        assert_eq!(v["kind"], "ticket");
        assert!(!v["key"].as_str().unwrap().is_empty());
        assert!(!v["label"].as_str().unwrap().is_empty());
        assert_eq!(v["labels"], json!([label]));
        assert!([json!("ready"), json!("done")].contains(&v["lane"]));
        assert!(chrono::DateTime::parse_from_rfc3339(v["updated_at"].as_str().unwrap()).is_ok());
        assert!(v.get("body").is_none());
    }
    for (state, expected) in [("open", &search[..2]), ("closed", &search[2..3])] {
        let found=tracker_items(t,tracker,&json!({"text":"contractneedle secondword","state":state,"label":label,"page_size":100})).await;
        assert_eq!(
            found
                .iter()
                .map(|v| v["url"].as_str().unwrap())
                .collect::<Vec<_>>(),
            expected.iter().map(String::as_str).collect::<Vec<_>>()
        );
    }
    assert!(
        tracker_items(
            t,
            tracker,
            &json!({"text":"nothing-matches-this-contract","state":"all"})
        )
        .await
        .is_empty()
    );
    let defaults = tracker_items(t, tracker, &json!({})).await;
    for (i, url) in search.iter().enumerate() {
        assert_eq!(defaults.iter().any(|v| v["url"] == *url), i != 2);
    }
    // Relations are reciprocal, deduplicated, and removable.
    let parent = ticket_create(t, tracker, "Parent").await;
    let child = ticket_create(t, tracker, "Child").await;
    let blocker = ticket_create(t, tracker, "Blocker").await;
    t.update_ticket(
        &child,
        UpdateTicket {
            parent: Some(parent.clone()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    for _ in 0..2 {
        t.update_ticket(
            &parent,
            UpdateTicket {
                add_blocked_by: vec![blocker.clone()],
                ..Default::default()
            },
        )
        .await
        .unwrap();
    }
    let p = ticket_detail(t, &parent).await;
    assert_eq!(p["children"][0]["url"], child);
    assert_eq!(p["children"][0]["kind"], "ticket");
    assert_eq!(p["children"][0]["title"], "Child");
    assert_eq!(p["children"][0]["state"], "open");
    assert_eq!(p["blocked_by"].as_array().unwrap().len(), 1);
    assert_eq!(p["blocked_by"][0]["url"], blocker);
    assert_eq!(p["blocked_by"][0]["state"], "open");
    assert!(p["parent"].is_null());
    assert_eq!(p["blocks"], json!([]));
    assert_eq!(ticket_detail(t, &child).await["parent"]["url"], parent);
    assert_eq!(ticket_detail(t, &blocker).await["blocks"][0]["url"], parent);
    t.update_ticket(
        &child,
        UpdateTicket {
            parent: Some(String::new()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    t.update_ticket(
        &parent,
        UpdateTicket {
            remove_blocked_by: vec![blocker],
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let p = ticket_detail(t, &parent).await;
    assert_eq!(p["children"], json!([]));
    assert_eq!(p["blocked_by"], json!([]));
    assert!(ticket_detail(t, &child).await["parent"].is_null());
    let before = tracker_items(t, tracker, &json!({"state":"all","page_size":100}))
        .await
        .len();
    let mut off = ticket_request(tracker, "Off board");
    off.add_to_board = false;
    match t.create_ticket(off).await {
        Ok(created) => assert!(ticket_detail(t, &created.target.url).await["lane"].is_null()),
        Err(error) if error.is_unsupported() => assert_eq!(
            tracker_items(t, tracker, &json!({"state":"all","page_size":100}))
                .await
                .len(),
            before
        ),
        Err(e) => panic!("off-board create failed: {e}"),
    };
}

pub trait TicketContractDriver: Send + Sync {
    fn human_comment<'a>(&'a self, url: &'a str, body: &'a str) -> ServiceFuture<'a, ()>;
    fn assign<'a>(&'a self, url: &'a str, actor: &'a ActorName) -> ServiceFuture<'a, ()>;
    fn merge_reference<'a>(&'a self, ticket_url: &'a str) -> ServiceFuture<'a, ()>;
}
pub async fn ticket_patrol_contract(
    t: &dyn TicketService,
    driver: &dyn TicketContractDriver,
    tracker: &str,
    label: &str,
    actor: ActorName,
    other_actor: ActorName,
) {
    let mention = format!("@{}", actor.0);
    let elsewhere = format!("@{} Please provide context.", other_actor.0);
    // Every creation combination publishes complete assignment, relations and lane.
    let parent = ticket_create(t, tracker, "Creation parent").await;
    let blocker = ticket_create(t, tracker, "Creation blocker").await;
    t.update_ticket(
        &blocker,
        UpdateTicket {
            state: Some("closed".into()),
            state_reason: Some("completed".into()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    for relation in 0..4 {
        for lane in [
            None,
            Some(CreateLane::New),
            Some(CreateLane::Ready),
            Some(CreateLane::InProgress),
        ] {
            let mut q = ticket_request(tracker, "Created complete");
            q.assignee = Some(actor.clone());
            q.labels = vec![label.into()];
            q.lane = lane;
            if relation & 1 != 0 {
                q.parent = Some(parent.clone())
            }
            if relation & 2 != 0 {
                q.blocked_by = vec![blocker.clone()]
            };
            let expected =
                serde_json::to_value(creation_lane(lane, q.parent.as_deref(), &q.blocked_by))
                    .unwrap();
            let expected_parent = q.parent.clone();
            let expected_blockers = q.blocked_by.clone();
            let c = t.create_ticket(q).await.unwrap();
            let v = ticket_detail(t, &c.target.url).await;
            assert_eq!(v["lane"], expected);
            assert_eq!(v["assignees"], json!([actor]));
            assert_eq!(v["labels"], json!([label]));
            assert_eq!(v["parent"]["url"].as_str(), expected_parent.as_deref());
            assert_eq!(
                v["blocked_by"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|b| b["url"].as_str().unwrap())
                    .collect::<Vec<_>>(),
                expected_blockers
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()
            );
            assert_eq!(
                task_for(t, &c.target.url).await.is_some(),
                expected != "new"
            );
            finish_task(t, &c.target.url).await;
        }
    }
    for assigned in [&actor, &other_actor] {
        let mut q = ticket_request(tracker, "Roster assignment");
        q.assignee = Some(assigned.clone());
        q.lane = Some(CreateLane::Ready);
        let c = t.create_ticket(q).await.unwrap();
        assert_eq!(
            ticket_detail(t, &c.target.url).await["assignees"],
            json!([assigned])
        );
        finish_task(t, &c.target.url).await;
    }
    for related in [false, true] {
        for lane in [None, Some(CreateLane::Ready), Some(CreateLane::InProgress)] {
            let mut q = ticket_request(tracker, "Interactive creation");
            q.interactive = true;
            q.assignee = Some(actor.clone());
            q.lane = lane;
            if related {
                q.parent = Some(parent.clone())
            };
            let expected =
                serde_json::to_value(creation_lane(lane, q.parent.as_deref(), &[])).unwrap();
            let c = t.create_ticket(q).await.unwrap();
            assert_eq!(ticket_detail(t, &c.target.url).await["lane"], expected);
            assert!(task_for(t, &c.target.url).await.is_none());
            let mut child = ticket_request(tracker, "Interactive descendant");
            child.assignee = Some(actor.clone());
            child.parent = Some(c.target.url.clone());
            let child = t.create_ticket(child).await.unwrap();
            assert!(task_for(t, &child.target.url).await.is_none());
            assert!(
                t.refresh(&Task {
                    url: Some(child.target.url.clone()),
                    ..Default::default()
                })
                .await
                .unwrap()
                .is_none()
            );
            finish_task(t, &child.target.url).await;
            finish_task(t, &c.target.url).await;
        }
    }
    // Own and inherited interaction settings, including same-write reparenting.
    let root = ticket_create(t, tracker, "Interactive root").await;
    let parent = ticket_create(t, tracker, "Interactive parent").await;
    let child = ticket_create(t, tracker, "Interactive child").await;
    t.update_ticket(
        &parent,
        UpdateTicket {
            parent: Some(root.clone()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    t.update_ticket(
        &child,
        UpdateTicket {
            parent: Some(parent.clone()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    driver.assign(&child, &actor).await.unwrap();
    let task = task_for(t, &child).await.unwrap();
    let initial = ticket_detail(t, &child).await;
    for u in [&root, &parent, &child] {
        let v = ticket_detail(t, u).await;
        assert_eq!(v["interactive"], false);
        assert!(v["interactive_ancestor"].is_null());
    }
    let enabled = t
        .update_ticket(
            &child,
            UpdateTicket {
                interactive: Some(true),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert!(enabled.interactive);
    assert!(enabled.interactive_ancestor.is_none());
    assert!(task_for(t, &child).await.is_none());
    assert!(t.refresh(&task).await.unwrap().is_none());
    assert!(!t.can_receive_work(&child).await.unwrap());
    assert!(
        t.update_ticket(&child, UpdateTicket::default())
            .await
            .unwrap()
            .interactive
    );
    let disabled = t
        .update_ticket(
            &child,
            UpdateTicket {
                interactive: Some(false),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert!(!disabled.interactive);
    assert!(disabled.interactive_ancestor.is_none());
    assert!(task_for(t, &child).await.is_some());
    assert!(t.refresh(&task).await.unwrap().is_some());
    assert!(t.can_receive_work(&child).await.unwrap());
    t.update_ticket(
        &root,
        UpdateTicket {
            interactive: Some(true),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let inherited = t
        .update_ticket(
            &child,
            UpdateTicket {
                interactive: Some(false),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(
        inherited.interactive_ancestor.as_deref(),
        Some(root.as_str())
    );
    assert_eq!(ticket_detail(t, &child).await["interactive_ancestor"], root);
    assert!(task_for(t, &child).await.is_none());
    assert!(t.refresh(&task).await.unwrap().is_none());
    assert!(!t.can_receive_work(&child).await.unwrap());
    t.update_ticket(
        &parent,
        UpdateTicket {
            interactive: Some(true),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let both = t
        .update_ticket(
            &child,
            UpdateTicket {
                interactive: Some(true),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert!(both.interactive);
    assert_eq!(both.interactive_ancestor.as_deref(), Some(parent.as_str()));
    assert_eq!(
        ticket_detail(t, &child).await["interactive_ancestor"],
        parent
    );
    t.update_ticket(
        &parent,
        UpdateTicket {
            interactive: Some(false),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    t.update_ticket(
        &child,
        UpdateTicket {
            interactive: Some(false),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(ticket_detail(t, &child).await["interactive_ancestor"], root);
    t.update_ticket(
        &root,
        UpdateTicket {
            interactive: Some(false),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert!(ticket_detail(t, &child).await["interactive_ancestor"].is_null());
    assert!(task_for(t, &child).await.is_some());
    t.update_ticket(
        &parent,
        UpdateTicket {
            interactive: Some(true),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let detached = t
        .update_ticket(
            &child,
            UpdateTicket {
                interactive: Some(false),
                parent: Some(String::new()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert!(!detached.interactive);
    assert!(detached.interactive_ancestor.is_none());
    let v = ticket_detail(t, &child).await;
    assert_eq!(v["lane"], "ready");
    assert_eq!(v["assignees"], initial["assignees"]);
    finish_task(t, &child).await;
    // Ready-to-working refresh and own answer suppression.
    let url = ticket_create(t, tracker, "Move and refresh").await;
    driver.assign(&url, &actor).await.unwrap();
    let task = task_for(t, &url).await.unwrap();
    assert_eq!(task.status, "ready");
    assert_eq!(task.trigger_reason.as_deref(), Some("ready_lane"));
    assert!(t.move_ticket(&task, MoveLane::InProgress).await.unwrap());
    assert_eq!(ticket_detail(t, &url).await["lane"], "in_progress");
    let refreshed = t.refresh(&task).await.unwrap().unwrap();
    assert_eq!(refreshed.status, "in_progress");
    assert_eq!(refreshed.trigger_reason.as_deref(), Some("working_lane"));
    t.comment(&url, "On it.", None).await.unwrap();
    assert!(t.refresh(&task).await.unwrap().is_none());
    finish_task(t, &url).await;
    // Ready work waits for open blockers, while working work can continue.
    let blocker = ticket_create(t, tracker, "Open blocker").await;
    let url = ticket_create(t, tracker, "Ready blocked").await;
    t.update_ticket(
        &url,
        UpdateTicket {
            add_blocked_by: vec![blocker.clone()],
            ..Default::default()
        },
    )
    .await
    .unwrap();
    driver.assign(&url, &actor).await.unwrap();
    assert!(task_for(t, &url).await.is_none());
    t.update_ticket(
        &blocker,
        UpdateTicket {
            state: Some("closed".into()),
            state_reason: Some("not_planned".into()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert!(task_for(t, &url).await.is_some());
    finish_task(t, &url).await;
    for uppercase in [false, true] {
        let url = ticket_create(t, tracker, "Unassigned mention").await;
        let addressed = if uppercase {
            mention.to_uppercase()
        } else {
            mention.clone()
        };
        driver
            .human_comment(&url, &format!("{addressed} Please provide context."))
            .await
            .unwrap();
        let before = ticket_detail(t, &url).await;
        let task = task_for(t, &url).await.unwrap();
        assert!(task.assignee.is_none());
        assert_eq!(task.trigger_reason.as_deref(), Some("issue_mention"));
        assert_eq!(
            t.refresh(&task)
                .await
                .unwrap()
                .unwrap()
                .trigger_reason
                .as_deref(),
            Some("issue_mention")
        );
        assert_eq!(ticket_detail(t, &url).await, before);
        t.comment(&url, "Here is the context.", None).await.unwrap();
        driver.human_comment(&url, "Thanks.").await.unwrap();
        assert!(task_for(t, &url).await.is_none());
        assert!(t.refresh(&task).await.unwrap().is_none());
        let after = ticket_detail(t, &url).await;
        assert_eq!(after["lane"], before["lane"]);
        assert_eq!(after["assignees"], before["assignees"]);
    }
    for working in [false, true] {
        let url = ticket_create(t, tracker, "Other recipients").await;
        driver.assign(&url, &actor).await.unwrap();
        let task = task_for(t, &url).await.unwrap();
        if working {
            t.move_ticket(&task, MoveLane::InProgress).await.unwrap();
        }
        t.comment(&url, "Please ask for context.", None)
            .await
            .unwrap();
        driver.human_comment(&url, &elsewhere).await.unwrap();
        assert!(task_for(t, &url).await.is_none());
        assert!(t.refresh(&task).await.unwrap().is_none());
        driver
            .human_comment(&url, &format!("{mention} Here is the context."))
            .await
            .unwrap();
        let selected = task_for(t, &url).await.unwrap();
        assert!(selected.assignee.is_some());
        assert_eq!(
            selected.trigger_reason.as_deref(),
            Some(if working {
                "issue_comment"
            } else {
                "ready_lane"
            })
        );
        assert!(t.refresh(&selected).await.unwrap().is_some());
        finish_task(t, &url).await;
        let url = ticket_create(t, tracker, "Pending mention survives recipients").await;
        driver.assign(&url, &actor).await.unwrap();
        let task = task_for(t, &url).await.unwrap();
        if working {
            t.move_ticket(&task, MoveLane::InProgress).await.unwrap();
        }
        driver
            .human_comment(&url, &format!("{mention} Investigate."))
            .await
            .unwrap();
        driver.human_comment(&url, &elsewhere).await.unwrap();
        let selected = task_for(t, &url).await.unwrap();
        assert_eq!(
            selected.trigger_reason.as_deref(),
            Some(if working {
                "issue_comment"
            } else {
                "ready_lane"
            })
        );
        assert!(t.refresh(&selected).await.unwrap().is_some());
        finish_task(t, &url).await;
        let url = ticket_create(t, tracker, "Held status survives recipients").await;
        driver.assign(&url, &actor).await.unwrap();
        let task = task_for(t, &url).await.unwrap();
        if working {
            t.move_ticket(&task, MoveLane::InProgress).await.unwrap();
        }
        let status=WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"failed","person_id":"aiko","run_id":"failed-run"})).unwrap();
        t.comment(&url, "The run failed.", Some(&status))
            .await
            .unwrap();
        driver.human_comment(&url, &elsewhere).await.unwrap();
        assert!(task_for(t, &url).await.is_none());
        assert!(t.refresh(&task).await.unwrap().is_none());
        finish_task(t, &url).await;
    }
    let url = ticket_create(t, tracker, "New assignment ignores other recipient").await;
    driver.assign(&url, &actor).await.unwrap();
    driver.human_comment(&url, &elsewhere).await.unwrap();
    let task = task_for(t, &url).await.unwrap();
    assert_eq!(task.trigger_reason.as_deref(), Some("ready_lane"));
    assert_eq!(
        t.refresh(&task)
            .await
            .unwrap()
            .unwrap()
            .trigger_reason
            .as_deref(),
        Some("ready_lane")
    );
    finish_task(t, &url).await;
    let url = ticket_create(t, tracker, "Assignment refreshes mention").await;
    driver
        .human_comment(&url, &format!("{mention} Context?"))
        .await
        .unwrap();
    let mention_task = task_for(t, &url).await.unwrap();
    assert!(mention_task.assignee.is_none());
    driver.assign(&url, &actor).await.unwrap();
    let assigned = task_for(t, &url).await.unwrap();
    assert_eq!(assigned.trigger_reason.as_deref(), Some("ready_lane"));
    let refreshed = t.refresh(&mention_task).await.unwrap().unwrap();
    assert_eq!(refreshed.assignee, assigned.assignee);
    assert_eq!(refreshed.trigger_reason.as_deref(), Some("ready_lane"));
    assert_eq!(
        t.candidates()
            .await
            .unwrap()
            .iter()
            .filter(|task| task.url.as_deref() == Some(&url))
            .count(),
        1
    );
    finish_task(t, &url).await;
    let url = ticket_create(t, tracker, "Held merged reference").await;
    driver.assign(&url, &actor).await.unwrap();
    let task = task_for(t, &url).await.unwrap();
    t.move_ticket(&task, MoveLane::InProgress).await.unwrap();
    driver.merge_reference(&url).await.unwrap();
    let status=WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"failed","person_id":"aiko","run_id":"failed-run"})).unwrap();
    t.comment(&url, "The run failed.", Some(&status))
        .await
        .unwrap();
    driver.human_comment(&url, &elsewhere).await.unwrap();
    assert!(task_for(t, &url).await.is_none());
    assert!(t.refresh(&task).await.unwrap().is_none());
    assert_eq!(ticket_detail(t, &url).await["lane"], "in_progress");
    finish_task(t, &url).await;
}

pub async fn ticket_scope_contract(
    t: &dyn TicketService,
    tracker: &str,
    their_tracker: &str,
    theirs: &str,
    their_comment: &str,
) {
    let before = ticket_detail(t, theirs).await;
    let before_comments = t
        .read(Resource::TicketComments, theirs, None, "")
        .await
        .unwrap();
    let count = tracker_items(t, their_tracker, &json!({"state":"all","page_size":100}))
        .await
        .len();
    let mine = ticket_create(t, tracker, "Scoped relations").await;
    assert_eq!(
        t.create_ticket(ticket_request(their_tracker, "Denied"))
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        t.comment(theirs, "Hi", None).await.unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        t.add_reaction(theirs, their_comment, SemanticReaction::Ack)
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        t.move_ticket(
            &Task {
                url: Some(theirs.into()),
                ..Default::default()
            },
            MoveLane::InProgress
        )
        .await
        .unwrap_err(),
        ServiceError::OutOfScope
    );
    for q in [
        UpdateTicket {
            parent: Some(theirs.into()),
            ..Default::default()
        },
        UpdateTicket {
            add_blocked_by: vec![theirs.into()],
            ..Default::default()
        },
        UpdateTicket {
            remove_blocked_by: vec![theirs.into()],
            ..Default::default()
        },
    ] {
        assert_eq!(
            t.update_ticket(&mine, q).await.unwrap_err(),
            ServiceError::OutOfScope
        );
    }
    for q in [
        UpdateTicket {
            parent: Some(mine.clone()),
            ..Default::default()
        },
        UpdateTicket {
            parent: Some(String::new()),
            ..Default::default()
        },
        UpdateTicket {
            add_blocked_by: vec![mine.clone()],
            ..Default::default()
        },
        UpdateTicket {
            remove_blocked_by: vec![mine],
            ..Default::default()
        },
    ] {
        assert_eq!(
            t.update_ticket(theirs, q).await.unwrap_err(),
            ServiceError::OutOfScope
        );
    }
    for relation in [true, false] {
        let mut q = ticket_request(tracker, "Denied relation at creation");
        if relation {
            q.parent = Some(theirs.into())
        } else {
            q.blocked_by = vec![theirs.into()]
        };
        let count = tracker_items(t, tracker, &json!({"state":"all","page_size":100}))
            .await
            .len();
        assert_eq!(
            t.create_ticket(q).await.unwrap_err(),
            ServiceError::OutOfScope
        );
        assert_eq!(
            tracker_items(t, tracker, &json!({"state":"all","page_size":100}))
                .await
                .len(),
            count
        );
    }
    assert_eq!(ticket_detail(t, theirs).await, before);
    assert_eq!(
        t.read(Resource::TicketComments, theirs, None, "")
            .await
            .unwrap(),
        before_comments
    );
    assert_eq!(
        tracker_items(t, their_tracker, &json!({"state":"all","page_size":100}))
            .await
            .len(),
        count
    );
    ticket_create(t, tracker, "Allowed write after refusals").await;
}
pub async fn ticket_wrong_subject_contract(
    t: &dyn TicketService,
    pull_url: &str,
    pull_comment: &str,
) {
    assert!((t.comment(pull_url, "Hi", None).await.unwrap_err()).is_not_found());
    assert!(
        (t.add_reaction(pull_url, pull_comment, SemanticReaction::Ack)
            .await
            .unwrap_err())
        .is_not_found()
    );
    assert!(
        (t.update_ticket(
            pull_url,
            UpdateTicket {
                title: Some("Renamed".into()),
                state: Some("closed".into()),
                ..Default::default()
            }
        )
        .await
        .unwrap_err())
        .is_not_found()
    );
    for r in [Resource::Tickets, Resource::TicketComments] {
        assert!((t.read(r, pull_url, None, "").await.unwrap_err()).is_not_found());
    }
}
pub struct TicketAttachmentFixture {
    pub screen_url: String,
    pub screen_id: String,
    pub log_url: String,
    pub log_id: String,
    pub unused_id: String,
}
pub async fn ticket_attachment_contract(
    t: &dyn TicketService,
    tracker: &str,
    f: TicketAttachmentFixture,
) {
    let screen = b"\x89PNG\r\n\x1a\nscreen";
    let log = b"GIF89alog";
    let url = ticket_create(t, tracker, "Referenced files").await;
    t.update_ticket(
        &url,
        UpdateTicket {
            body: Some(format!("The login fails:\n\n{}", f.screen_url)),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let comment = t
        .comment(
            &url,
            &format!("The log: {}, and again {}", f.log_url, f.screen_url),
            None,
        )
        .await
        .unwrap();
    let page = t
        .read(Resource::TicketAttachments, &url, None, "")
        .await
        .unwrap();
    assert_eq!(page.items().len(), 2);
    for (id, name, media, bytes, refs) in [
        (
            &f.screen_id,
            "screen.png",
            "image/png",
            screen.as_slice(),
            vec!["body".to_string(), comment.id.clone()],
        ),
        (
            &f.log_id,
            "log.gif",
            "image/gif",
            log.as_slice(),
            vec![comment.id.clone()],
        ),
    ] {
        let v = page.items().iter().find(|v| v["id"] == *id).unwrap();
        assert_eq!(v["name"], name);
        assert_eq!(v["media_type"], media);
        assert_eq!(v["size"], bytes.len());
        assert_eq!(v["referenced_by"], json!(refs));
        let d = t.attachment(&url, id, bytes.len()).await.unwrap();
        assert_eq!(serde_json::to_value(d.metadata).unwrap(), *v);
        assert_eq!(d.content, bytes);
    }
    let too_large = t
        .attachment(&url, &f.screen_id, screen.len() - 1)
        .await
        .unwrap_err();
    assert_eq!(
        too_large.translation_key(),
        "integrations.resources.attachment_too_large"
    );
    assert_eq!(
        too_large.translation_args(),
        [
            ("name".into(), "screen.png".into()),
            ("limit".into(), (screen.len() - 1).to_string()),
        ]
        .into()
    );
    let other = ticket_create(t, tracker, "No attachment references").await;
    for (url, id) in [
        (&other, &f.screen_id),
        (&url, &f.unused_id),
        (&url, &"missing-attachment".to_string()),
    ] {
        let error = t.attachment(url, id, 1024).await.unwrap_err();
        assert_eq!(
            error.translation_key(),
            "integrations.resources.attachment_missing"
        );
        assert_eq!(
            error.translation_args(),
            [("url".into(), url.clone()), ("id".into(), id.clone()),].into()
        );
        assert!(error.is_not_found());
        assert!(!error.is_unsupported());
        assert!(std::error::Error::source(&error).is_none());
    }
}

pub async fn ticket_search_decoy_contract(
    t: &dyn TicketService,
    c: &dyn CodeHostingService,
    tracker: &str,
    repository: &str,
    head: &str,
    base: &str,
) {
    let title = "Contractneedle-PR-only-match";
    let pr = c
        .create_pull_request(CreatePullRequest {
            repository: repository.into(),
            head: head.into(),
            base: base.into(),
            title: title.into(),
            body: String::new(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap();
    assert_eq!(pr.target.title, title);
    assert!(
        tracker_items(t, tracker, &json!({"text":title,"state":"all"}))
            .await
            .is_empty()
    );
}

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
pub async fn member_show_contract(service: &dyn CodeHostingService, repository: &str) {
    let temp = tempfile::tempdir().unwrap();
    let branch = service.default_branch(repository).await.unwrap();
    let result = gb_member::git::show(
        temp.path(),
        service,
        repository,
        &[".".into(), "does-not-exist.fixture".into()],
        std::time::Instant::now() + std::time::Duration::from_secs(30),
    )
    .await
    .unwrap();
    assert_eq!(result["default_branch"], branch);
    assert_eq!(result["items"][0]["kind"], "directory");
    assert!(
        result["items"][0]["entries"]
            .as_array()
            .is_some_and(|v| !v.is_empty())
    );
    assert_eq!(
        result["items"][1],
        json!({"path":"does-not-exist.fixture","kind":"missing"})
    );
    assert!(!temp.path().join("clones").exists());
}
/// Frozen code-host operation population; every assertion runs for both providers.
pub async fn code_contract(
    service: &dyn CodeHostingService,
    repository: &str,
    head_branch: &str,
    base: &str,
) {
    member_show_contract(service, repository).await;
    // Invalid semantic values stop at the typed port boundary before a provider call.
    assert!(serde_json::from_value::<SemanticReaction>(json!("eyes")).is_err());
    let request = |branch: &str| CreatePullRequest {
        repository: repository.into(),
        head: branch.into(),
        base: base.into(),
        title: "contract PR".into(),
        body: "contract body".into(),
        draft: false,
        tickets: vec![],
    };
    let pr = service
        .create_pull_request(request(head_branch))
        .await
        .unwrap();
    assert!(pr.created);
    let again = service
        .create_pull_request(request(head_branch))
        .await
        .unwrap();
    assert!(!again.created);
    assert_eq!(again.target, pr.target);
    let other = service
        .create_pull_request(request(&format!("{head_branch}-other")))
        .await
        .unwrap();
    let url = &pr.target.url;
    let page = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert_eq!(page.target(), Some(&pr.target));
    let read = &page.items()[0];
    assert_eq!(read["head"], head_branch);
    assert_eq!(read["base"], base);
    assert_eq!(read["author_type"], "Assistant");
    assert_eq!(read["repository"], repository);
    assert_eq!(read["head_repository"], repository);
    let head = read["head_sha"].as_str().unwrap();
    assert!(!head.is_empty());
    let credential = service.git_credential().await.unwrap();
    if let Some(credential) = credential {
        assert!(!credential.username.is_empty());
        assert!(!credential.password.is_empty());
    }
    service.identity(false).await.unwrap();
    service.identity(true).await.unwrap();
    let remote = service.clone_url(repository).await.unwrap();
    assert_eq!(
        service.repository_from_remote(&remote).as_deref(),
        Some(repository)
    );
    assert_eq!(
        service.repository_from_remote("https://foreign.invalid/repo.git"),
        None
    );
    assert_eq!(service.default_branch(repository).await.unwrap(), base);
    service.check_push(repository).unwrap();
    let branch = service.pull_request_head(url).await.unwrap();
    assert_eq!(branch.repository, repository);
    assert_eq!(branch.branch, head_branch);
    assert_eq!(
        service
            .open_pull_requests(repository, head_branch)
            .await
            .unwrap(),
        vec![url.clone()]
    );
    assert!(
        service
            .open_pull_requests(repository, "contract-nonexistent")
            .await
            .unwrap()
            .is_empty()
    );
    let commit_url = service.commit_url(repository, head);
    assert!(commit_url.is_empty() || commit_url.contains(head));
    match service
        .read(Resource::DependencyAlerts, repository, None, "")
        .await
    {
        Ok(page) => {
            for alert in page.items() {
                assert!(alert["id"].as_str().is_some_and(|id| !id.is_empty()));
                assert!(
                    ["open", "resolved", "dismissed"].contains(&alert["state"].as_str().unwrap())
                );
            }
        }
        Err(error) if error.is_unsupported() => {}
        Err(error) => panic!("dependency alerts read failed: {error:?}"),
    }
    for limit in [0, MAX_LOG_TAIL_BYTES + 1] {
        for logs in [false, true] {
            assert!(matches!(
                service.readiness(url, logs, limit).await,
                Err(ServiceError::InvalidParameters)
            ));
        }
    }
    let ready = service
        .readiness(url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(ready.head_sha, head);
    assert_eq!(ready.current_head_sha, head);
    assert_eq!(ready.target, pr.target);
    assert!(ready.failed_logs.is_none());
    let read_ready = service
        .read(Resource::PullRequestReadiness, url, None, "")
        .await
        .unwrap();
    assert_eq!(read_ready.items()[0]["head_sha"], head);
    let files = service
        .read(Resource::PullRequestFiles, url, None, "")
        .await
        .unwrap();
    let readme = files
        .items()
        .iter()
        .find(|v| v["path"] == "README.md")
        .expect("README change");
    assert_eq!(readme["patch_available"], true);
    assert_eq!(readme["patch_complete"], true);
    assert!(
        readme["commentable_lines"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["side"] == "RIGHT" && v["line"] == 1)
    );
    let review = service
        .review(url, "review \n", ReviewVerdict::Comment, head)
        .await
        .unwrap();
    assert_eq!(review.commit, head);
    assert_eq!(review.verdict, ReviewVerdict::Comment);
    assert_eq!(review.target, pr.target);
    let reviews = service
        .read(Resource::PullRequestReviews, url, None, "")
        .await
        .unwrap();
    let review_id = reviews.items().last().unwrap()["id"]
        .as_str()
        .unwrap()
        .to_owned();
    assert_eq!(reviews.items().last().unwrap()["body"], "review");
    assert!(
        service
            .review(url, "stale", ReviewVerdict::Comment, "missing-head")
            .await
            .is_err()
    );
    assert_eq!(
        service
            .read(Resource::PullRequestReviews, url, None, "")
            .await
            .unwrap()
            .items()
            .len(),
        reviews.items().len()
    );
    let inline = service
        .review_comment(
            url,
            ReviewComment {
                body: "inline \n".into(),
                head: head.into(),
                path: "README.md".into(),
                line: 1,
                side: "RIGHT".into(),
                start_line: None,
                start_side: None,
            },
        )
        .await
        .unwrap();
    assert_eq!(inline.target, pr.target);
    assert_eq!(inline.path, "README.md");
    assert_eq!(inline.line, 1);
    let reply = service.reply(url, &inline.id, "reply \n").await.unwrap();
    assert_ne!(reply.id, inline.id);
    assert_eq!(reply.target, pr.target);
    let threads = service
        .read(Resource::PullRequestThreads, url, None, "")
        .await
        .unwrap();
    let thread = threads
        .items()
        .iter()
        .find(|t| {
            t["comments"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["id"] == inline.id)
        })
        .unwrap();
    assert_eq!(thread["comments"].as_array().unwrap().len(), 2);
    assert_eq!(thread["comments"][0]["body"], "inline");
    assert_eq!(thread["comments"][1]["body"], "reply");
    assert_eq!(thread["comments_complete"], true);
    let thread_comments = service
        .read(
            Resource::ReviewThreadComments,
            url,
            Some(&json!({"node":thread["id"]})),
            "",
        )
        .await
        .unwrap();
    assert_eq!(thread_comments.items().len(), 2);
    assert_eq!(thread_comments.items()[1]["reply_to_id"], inline.id);
    let written = service.comment(url, "comment \n", None).await.unwrap();
    assert_eq!(written.target, pr.target);
    let comments = service
        .read(Resource::PullRequestComments, url, None, "")
        .await
        .unwrap();
    assert_eq!(comments.items().last().unwrap()["body"], "comment");
    for id in [&written.id, &inline.id, &review_id] {
        assert!(matches!(
            service
                .add_reaction(&other.target.url, id, SemanticReaction::Agree)
                .await,
            Err(ref error) if error.is_not_found()
        ));
        assert!(matches!(
            service.reply(&other.target.url, id, "wrong PR").await,
            Err(ref error) if error.is_not_found()
        ));
        let r = service
            .add_reaction(url, id, SemanticReaction::Agree)
            .await
            .unwrap();
        assert_eq!(r.id, *id);
        assert_eq!(r.reaction, SemanticReaction::Agree);
    }
    let elsewhere = format!("outside/{repository}");
    assert!(matches!(
        service.check_push(&elsewhere),
        Err(ServiceError::OutOfScope)
    ));
    let mut out = request(head_branch);
    out.repository = elsewhere.clone();
    assert!(matches!(
        service.create_pull_request(out).await,
        Err(ServiceError::OutOfScope)
    ));
    let outside_url = url.replace(repository, &elsewhere);
    assert!(matches!(
        service.convert_pull_request_to_draft(&outside_url).await,
        Err(ServiceError::OutOfScope)
    ));
    let updated = service
        .update_pull_request(
            url,
            UpdatePullRequest {
                title: Some("updated".into()),
                body: Some("updated body".into()),
                tickets: None,
            },
        )
        .await
        .unwrap();
    assert_eq!(updated.body, "updated body");
    assert_eq!(updated.target.title, "updated");
    let unsupported = service
        .update_pull_request(
            url,
            UpdatePullRequest {
                title: Some("must not land".into()),
                tickets: Some(vec![TicketLink {
                    url: "https://foreign.invalid/tickets/1".into(),
                    closes: true,
                }]),
                ..Default::default()
            },
        )
        .await;
    assert!(matches!(unsupported, Err(ref error) if error.is_unsupported()));
    assert_eq!(
        service
            .read(Resource::PullRequests, url, None, "")
            .await
            .unwrap()
            .items()[0]["title"],
        "updated"
    );
    assert!(matches!(
        service
            .read(
                Resource::TicketPullRequests,
                "https://foreign.invalid/tickets/1",
                None,
                ""
            )
            .await,
        Err(ref error) if error.is_unsupported()
    ));
    let (a, b) = tokio::join!(
        service.convert_pull_request_to_draft(url),
        service.convert_pull_request_to_draft(url)
    );
    let mut converted = [a.unwrap(), b.unwrap()];
    converted.sort();
    assert_eq!(converted, [false, true]);
    assert!(!service.convert_pull_request_to_draft(url).await.unwrap());
    assert_eq!(
        service
            .read(Resource::PullRequests, url, None, "")
            .await
            .unwrap()
            .items()[0]["draft"],
        true
    );
    service.pull_request_candidates().await.unwrap();
    assert!(!service.can_receive_work(url).await.unwrap());
    let task = Task {
        pull_request_url: Some(url.clone()),
        url: Some(url.clone()),
        ..Default::default()
    };
    assert!(service.refresh_pull_request(&task).await.unwrap().is_none());
    service.aclose().await.unwrap();
}

/// Run after a fixture advances the same PR's branch, preserving its old SHA.
pub async fn code_stale_head_contract(service: &dyn CodeHostingService, url: &str, old_head: &str) {
    let before = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    let head = before.items()[0]["head_sha"].as_str().unwrap();
    assert_ne!(head, old_head);
    let reviews = service
        .read(Resource::PullRequestReviews, url, None, "")
        .await
        .unwrap();
    let threads = service
        .read(Resource::PullRequestThreads, url, None, "")
        .await
        .unwrap();
    assert!(
        service
            .review(url, "stale", ReviewVerdict::Comment, old_head)
            .await
            .is_err()
    );
    assert!(
        service
            .review_comment(
                url,
                ReviewComment {
                    body: "stale".into(),
                    head: old_head.into(),
                    path: "README.md".into(),
                    line: 1,
                    side: "RIGHT".into(),
                    start_line: None,
                    start_side: None
                }
            )
            .await
            .is_err()
    );
    assert_eq!(
        service
            .read(Resource::PullRequestReviews, url, None, "")
            .await
            .unwrap()
            .items(),
        reviews.items()
    );
    assert_eq!(
        service
            .read(Resource::PullRequestThreads, url, None, "")
            .await
            .unwrap()
            .items(),
        threads.items()
    );
    assert_eq!(
        service
            .review(url, "current", ReviewVerdict::Comment, head)
            .await
            .unwrap()
            .commit,
        head
    );
}
/// Run after a fixture removes the PR's base and adds `<base>/next`.
pub async fn code_missing_base_contract(service: &dyn CodeHostingService, url: &str) {
    assert!(
        service
            .readiness(url, false, MAX_LOG_TAIL_BYTES)
            .await
            .is_err()
    );
    assert!(
        service
            .read(Resource::PullRequestReadiness, url, None, "")
            .await
            .is_err()
    );
}
/// Run after the fixture merges or closes a PR.
pub async fn code_closed_contract(service: &dyn CodeHostingService, url: &str) {
    let p = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert_eq!(p.items()[0]["state"], "closed");
    assert!(!service.convert_pull_request_to_draft(url).await.unwrap());
}
/// Run as a different member from the PR author. Both restored providers support these verdicts.
pub async fn code_other_actor_contract(service: &dyn CodeHostingService, url: &str) {
    let p = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    let head = p.items()[0]["head_sha"].as_str().unwrap();
    for verdict in [ReviewVerdict::Approve, ReviewVerdict::RequestChanges] {
        let r = service.review(url, "verdict", verdict, head).await.unwrap();
        assert_eq!(r.verdict, verdict);
        assert_eq!(r.commit, head);
    }
}
/// The fixture installs the same ZIP bytes on the PR's current head.
pub async fn code_artifact_contract(
    service: &dyn CodeHostingService,
    url: &str,
    head: &str,
    bytes: &[u8],
) {
    let artifact = service
        .artifact(url, "report", bytes.len() + 1)
        .await
        .unwrap();
    assert_eq!(artifact.metadata.name, "report");
    assert_eq!(artifact.metadata.id, format!("{head}/report"));
    assert_eq!(artifact.content, bytes);
    assert!(service.artifact(url, "report", 1).await.is_err());
}
/// Run after advancing the head, leaving the old head's `report` artifact in place.
pub async fn code_old_artifact_contract(service: &dyn CodeHostingService, url: &str) {
    assert!(matches!(
        service.artifact(url, "report", 1024 * 1024).await,
        Err(ref error) if error.is_not_found()
    ));
}
/// The fixture includes a changed UTF-8 filename and UTF-8 patch text.
pub async fn code_utf8_files_contract(
    service: &dyn CodeHostingService,
    url: &str,
    path: &str,
    text: &str,
) {
    let p = service
        .read(Resource::PullRequestFiles, url, None, "")
        .await
        .unwrap();
    let file = p
        .items()
        .iter()
        .find(|v| v["path"] == path)
        .expect("UTF-8 path preserved");
    assert!(file["patch"].as_str().unwrap().contains(text));
    assert_eq!(file["patch_available"], true);
    assert_eq!(file["patch_complete"], true);
}

/// Wrong item kinds never cross the code-host boundary or cause writes.
pub async fn code_kind_boundary_contract(
    code: &dyn CodeHostingService,
    tickets: &dyn TicketService,
    tracker: &str,
    pr_url: &str,
) {
    let ticket = tickets
        .create_ticket(CreateTicket {
            tracker: tracker.into(),
            title: "kind boundary".into(),
            body: String::new(),
            labels: vec![],
            add_to_board: true,
            interactive: false,
            assignee: None,
            lane: None,
            parent: None,
            blocked_by: vec![],
        })
        .await
        .unwrap()
        .target
        .url;
    assert!(matches!(
        code.comment(&ticket, "wrong kind", None).await,
        Err(ref error) if error.is_not_found()
    ));
    assert!(matches!(
        code.review(&ticket, "wrong kind", ReviewVerdict::Comment, "head")
            .await,
        Err(ref error) if error.is_not_found()
    ));
    assert!(matches!(
        code.readiness(&ticket, false, MAX_LOG_TAIL_BYTES).await,
        Err(ref error) if error.is_not_found()
    ));
    assert!(matches!(
        code.pull_request_head(&ticket).await,
        Err(ref error) if error.is_not_found()
    ));
    for resource in Resource::ALL {
        if resource.is_ticket_service() {
            assert!(code.read(*resource, &ticket, None, "").await.is_err());
        } else if matches!(
            resource,
            Resource::PullRequests
                | Resource::PullRequestComments
                | Resource::PullRequestReviews
                | Resource::PullRequestFiles
                | Resource::PullRequestReadiness
        ) {
            assert!(matches!(
                code.read(*resource, &ticket, None, "").await,
                Err(ref error) if error.is_not_found()
            ));
        }
    }
    assert!(matches!(
        code.read(Resource::TicketPullRequests, pr_url, None, "")
            .await,
        Err(ref error) if error.is_not_found()
    ));
    let before = code
        .read(Resource::PullRequests, pr_url, None, "")
        .await
        .unwrap();
    let (prefix, key) = ticket.rsplit_once('/').unwrap();
    let missing = format!(
        "{prefix}/{}999999999",
        if key.starts_with("T-") { "T-" } else { "" }
    );
    assert!(matches!(
        code.update_pull_request(
            pr_url,
            UpdatePullRequest {
                title: Some("must not land".into()),
                tickets: Some(vec![TicketLink {
                    url: missing,
                    closes: false
                }]),
                ..Default::default()
            }
        )
        .await,
        Err(ref error) if error.is_not_found()
    ));
    for closes in [false, true] {
        let p = &before.items()[0];
        assert!(matches!(
            code.create_pull_request(CreatePullRequest {
                repository: p["repository"].as_str().unwrap().into(),
                head: format!("{}-wrong-kind-{closes}", p["head"].as_str().unwrap()),
                base: p["base"].as_str().unwrap().into(),
                title: "must not land".into(),
                body: String::new(),
                draft: false,
                tickets: vec![TicketLink {
                    url: pr_url.into(),
                    closes
                }]
            })
            .await,
            Err(ref error) if error.is_not_found()
        ));
        assert!(matches!(
            code.update_pull_request(
                pr_url,
                UpdatePullRequest {
                    body: Some("must not land".into()),
                    tickets: Some(vec![TicketLink {
                        url: pr_url.into(),
                        closes
                    }]),
                    ..Default::default()
                }
            )
            .await,
            Err(ref error) if error.is_not_found()
        ));
    }
    assert_eq!(
        code.read(Resource::PullRequests, pr_url, None, "")
            .await
            .unwrap()
            .items(),
        before.items()
    );
}

pub struct CodeLinkCase {
    pub url: String,
    pub states: Vec<(String, String)>,
}
/// Fixtures prepare aliases `<head>-links-0` through `<head>-links-7`, then
/// merge each returned PR and pass it to `code_link_case_finished`.
pub async fn code_link_population(
    code: &dyn CodeHostingService,
    tickets: &dyn TicketService,
    repository: &str,
    tracker: &str,
    head: &str,
    base: &str,
) -> Vec<CodeLinkCase> {
    async fn ticket(t: &dyn TicketService, tracker: &str, title: &str) -> String {
        t.create_ticket(CreateTicket {
            tracker: tracker.into(),
            title: title.into(),
            body: String::new(),
            labels: vec![],
            add_to_board: true,
            interactive: false,
            assignee: None,
            lane: None,
            parent: None,
            blocked_by: vec![],
        })
        .await
        .unwrap()
        .target
        .url
    }
    let mut cases = vec![];
    for i in 0..7 {
        let first = ticket(tickets, tracker, &format!("first {i}")).await;
        let second = ticket(tickets, tracker, &format!("second {i}")).await;
        let unlinked = ticket(tickets, tracker, &format!("unlinked {i}")).await;
        let body = format!("Context parent {second}");
        let initial = if i == 0 {
            vec![
                TicketLink {
                    url: first.clone(),
                    closes: true,
                },
                TicketLink {
                    url: second.clone(),
                    closes: true,
                },
            ]
        } else {
            vec![
                TicketLink {
                    url: first.clone(),
                    closes: i != 2 && i != 3,
                },
                TicketLink {
                    url: "https://foreign.invalid/tickets/1".into(),
                    closes: false,
                },
            ]
        };
        let pr = code
            .create_pull_request(CreatePullRequest {
                repository: repository.into(),
                head: format!("{head}-links-{i}"),
                base: base.into(),
                title: format!("links {i}"),
                body: body.clone(),
                draft: false,
                tickets: initial,
            })
            .await
            .unwrap();
        let mut first_state = "closed";
        let mut second_state = if i == 0 { "closed" } else { "open" };
        match i {
            2 | 3 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![TicketLink {
                            url: first.clone(),
                            closes: true,
                        }]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            }
            4 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![TicketLink {
                            url: first.clone(),
                            closes: false,
                        }]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
                first_state = "open";
            }
            5 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
                first_state = "open";
            }
            6 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![TicketLink {
                            url: second.clone(),
                            closes: true,
                        }]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
                first_state = "open";
                second_state = "closed";
            }
            _ => {}
        }
        code.update_pull_request(
            &pr.target.url,
            UpdatePullRequest {
                body: Some(format!("{body}\nRefs {first}\nCloses {unlinked}")),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert!(
            code.read(Resource::TicketPullRequests, &unlinked, None, "")
                .await
                .unwrap()
                .items()
                .is_empty()
        );
        let refs = code
            .read(Resource::TicketPullRequests, &first, None, "")
            .await
            .unwrap();
        if i >= 5 {
            assert!(refs.items().is_empty());
        } else {
            assert!(refs.items().iter().any(|p| p["url"] == pr.target.url));
        }
        let before = code
            .read(Resource::PullRequests, &pr.target.url, None, "")
            .await
            .unwrap();
        assert!(before.items()[0]["body"].as_str().unwrap().contains(&body));
        assert!(matches!(
            code.update_pull_request(
                &pr.target.url,
                UpdatePullRequest {
                    title: Some("must not land".into()),
                    body: Some("must not land".into()),
                    tickets: Some(vec![
                        TicketLink {
                            url: first.clone(),
                            closes: true
                        },
                        TicketLink {
                            url: "https://foreign.invalid/tickets/1".into(),
                            closes: true
                        }
                    ])
                }
            )
            .await,
            Err(ref error) if error.is_unsupported()
        ));
        assert_eq!(
            code.read(Resource::PullRequests, &pr.target.url, None, "")
                .await
                .unwrap()
                .items(),
            before.items()
        );
        if i == 0 {
            let reference = code
                .create_pull_request(CreatePullRequest {
                    repository: repository.into(),
                    head: format!("{head}-links-7"),
                    base: base.into(),
                    title: "references".into(),
                    body: String::new(),
                    draft: false,
                    tickets: vec![
                        TicketLink {
                            url: first.clone(),
                            closes: false,
                        },
                        TicketLink {
                            url: unlinked.clone(),
                            closes: false,
                        },
                    ],
                })
                .await
                .unwrap();
            let refs = code
                .read(Resource::TicketPullRequests, &first, None, "")
                .await
                .unwrap();
            assert!(refs.items().iter().any(|v| v["url"] == pr.target.url));
            assert!(
                refs.items()
                    .iter()
                    .any(|v| v["url"] == reference.target.url)
            );
            cases.push(CodeLinkCase {
                url: reference.target.url,
                states: vec![
                    (first.clone(), "open".into()),
                    (second.clone(), "open".into()),
                    (unlinked.clone(), "open".into()),
                ],
            });
        }
        cases.push(CodeLinkCase {
            url: pr.target.url,
            states: vec![
                (first, first_state.into()),
                (second, second_state.into()),
                (unlinked, "open".into()),
            ],
        });
    }
    cases
}
pub async fn code_link_case_finished(tickets: &dyn TicketService, case: &CodeLinkCase) {
    for (url, state) in &case.states {
        assert_eq!(
            tickets
                .read(Resource::Tickets, url, None, "")
                .await
                .unwrap()
                .items()[0]["state"],
            *state,
            "{url}"
        );
    }
}

/// A same-service ticket of another repository can be referenced, but cannot
/// be closed by this PR. Failed replacement changes neither title nor body.
pub async fn code_cross_repo_link_contract(
    code: &dyn CodeHostingService,
    url: &str,
    other_ticket: &str,
) {
    let before = code
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert!(matches!(
        code.update_pull_request(
            url,
            UpdatePullRequest {
                title: Some("must not land".into()),
                body: Some("must not land".into()),
                tickets: Some(vec![TicketLink {
                    url: other_ticket.into(),
                    closes: true
                }])
            }
        )
        .await,
        Err(ref error) if error.is_unsupported()
    ));
    assert_eq!(
        code.read(Resource::PullRequests, url, None, "")
            .await
            .unwrap()
            .items(),
        before.items()
    );
    code.update_pull_request(
        url,
        UpdatePullRequest {
            tickets: Some(vec![TicketLink {
                url: other_ticket.into(),
                closes: false,
            }]),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert!(
        code.read(Resource::TicketPullRequests, other_ticket, None, "")
            .await
            .unwrap()
            .items()
            .iter()
            .any(|p| p["url"] == url)
    );
    code.update_pull_request(
        url,
        UpdatePullRequest {
            tickets: Some(vec![]),
            ..Default::default()
        },
    )
    .await
    .unwrap();
}

/// Branvil lists newest first across signed pages; Local preserves its frozen
/// ascending store order. Both traverse more than one provider page fully.
pub async fn code_open_order_contract(
    code: &dyn CodeHostingService,
    repository: &str,
    head: &str,
    bases: &[String],
    newest_first: bool,
) {
    assert!(
        bases.len() > 100,
        "fixture must cross Branvil's page boundary"
    );
    let mut created = vec![];
    for base in bases {
        let result = code
            .create_pull_request(CreatePullRequest {
                repository: repository.into(),
                head: head.into(),
                base: base.clone(),
                title: format!("page {base}"),
                body: String::new(),
                draft: false,
                tickets: vec![],
            })
            .await
            .unwrap();
        assert!(result.created);
        created.push(result.target.url);
    }
    if newest_first {
        created.reverse();
    }
    assert_eq!(
        code.open_pull_requests(repository, head).await.unwrap(),
        created
    );
    assert!(
        code.open_pull_requests(repository, "contract-absent-head")
            .await
            .unwrap()
            .is_empty()
    );
}
