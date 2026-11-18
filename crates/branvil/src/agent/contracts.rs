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

pub async fn ticket_contract(t: &dyn TicketService, tracker: &str, label: &str, author: &str) {
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
    assert_eq!(detail["author"], author);
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
pub async fn ticket_board_order_contract(
    t: &dyn TicketService,
    _driver: &dyn TicketContractDriver,
    tracker: &str,
    actor: &ActorName,
    other: &ActorName,
) {
    let mut urls = vec![];
    for i in 0..4 {
        let mut q = ticket_request(tracker, &format!("Board order {i}"));
        q.lane = Some(CreateLane::Ready);
        q.assignee = Some(if i == 2 { other } else { actor }.clone());
        if i == 2 {
            q.body = format!("@{} Please check", actor.0);
        }
        urls.push(t.create_ticket(q).await.unwrap().target.url);
    }
    async fn order(t: &dyn TicketService, urls: &[String]) -> Vec<String> {
        t.candidates()
            .await
            .unwrap()
            .into_iter()
            .filter_map(|task| task.url.filter(|u| urls.contains(u)))
            .collect()
    }
    assert_eq!(order(t, &urls).await, urls);
    async fn read_order(t: &dyn TicketService, tracker: &str, urls: &[String]) -> Vec<String> {
        tracker_items(t, tracker, &json!({"lane":"ready","page_size":1}))
            .await
            .into_iter()
            .filter_map(|v| {
                assert!(v["assignees"].is_array());
                assert_eq!(v["lane"], "ready");
                v["url"]
                    .as_str()
                    .filter(|u| urls.iter().any(|x| x == u))
                    .map(str::to_owned)
            })
            .collect()
    }
    assert_eq!(read_order(t, tracker, &urls).await, urls);
    for placement in [
        TicketPlacement::Before(urls[1].clone()),
        TicketPlacement::After(urls[1].clone()),
    ] {
        let before = ticket_detail(t, &urls[0]).await;
        let error = t
            .update_ticket(
                &urls[0],
                UpdateTicket {
                    placement: Some(placement),
                    lane: Some(MoveLane::InProgress),
                    interactive: Some(true),
                    assignee: Some(other.clone()),
                    title: Some("Must not change".into()),
                    ..Default::default()
                },
            )
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "integrations.resources.ticket_placement");
        assert_eq!(ticket_detail(t, &urls[0]).await, before);
        assert_eq!(read_order(t, tracker, &urls).await, urls);
    }
    let other_lane = ticket_create(t, tracker, "Other lane").await;
    let before = read_order(t, tracker, &urls).await;
    for placement in [
        TicketPlacement::Before(other_lane.clone()),
        TicketPlacement::After(other_lane),
    ] {
        let e = t
            .update_ticket(
                &urls[0],
                UpdateTicket {
                    placement: Some(placement),
                    title: Some("Must not change".into()),
                    ..Default::default()
                },
            )
            .await
            .unwrap_err();
        assert_eq!(e.to_string(), "integrations.resources.ticket_placement");
        assert_ne!(ticket_detail(t, &urls[0]).await["title"], "Must not change");
        assert_eq!(read_order(t, tracker, &urls).await, before);
    }
    t.update_ticket(
        &urls[0],
        UpdateTicket {
            placement: Some(TicketPlacement::After(urls[1].clone())),
            lane: Some(MoveLane::Ready),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(
        read_order(t, tracker, &urls).await,
        vec![
            urls[1].clone(),
            urls[0].clone(),
            urls[2].clone(),
            urls[3].clone()
        ]
    );
    assert_eq!(ticket_detail(t, &urls[0]).await["lane"], "ready");
    t.update_ticket(
        &urls[0],
        UpdateTicket {
            placement: Some(TicketPlacement::Before(urls[1].clone())),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    t.update_ticket(
        &urls[2],
        UpdateTicket {
            placement: Some(TicketPlacement::Before(urls[0].clone())),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(
        order(t, &urls).await,
        vec![
            urls[2].clone(),
            urls[0].clone(),
            urls[1].clone(),
            urls[3].clone()
        ]
    );
    t.update_ticket(
        &urls[1],
        UpdateTicket {
            placement: Some(TicketPlacement::Before(urls[2].clone())),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(read_order(t, tracker, &urls).await, order(t, &urls).await);
    t.update_ticket(
        &urls[1],
        UpdateTicket {
            add_blocked_by: vec![urls[0].clone()],
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(
        order(t, &urls).await,
        vec![urls[2].clone(), urls[0].clone(), urls[3].clone()]
    );
    t.update_ticket(
        &urls[1],
        UpdateTicket {
            remove_blocked_by: vec![urls[0].clone()],
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(
        order(t, &urls).await,
        vec![
            urls[1].clone(),
            urls[2].clone(),
            urls[0].clone(),
            urls[3].clone()
        ]
    );
    // Moving to the working lane keeps its original creation-time ordering.
    for url in [&urls[1], &urls[0]] {
        t.update_ticket(
            url,
            UpdateTicket {
                lane: Some(MoveLane::InProgress),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    }
    assert_eq!(
        order(t, &urls).await,
        vec![
            urls[2].clone(),
            urls[3].clone(),
            urls[0].clone(),
            urls[1].clone()
        ]
    );
    // A moved card joins the lane's end; a newly created card does too.
    t.move_ticket(
        &Task {
            url: Some(urls[0].clone()),
            ..Default::default()
        },
        MoveLane::Ready,
    )
    .await
    .unwrap();
    let mut q = ticket_request(tracker, "Board order appended");
    q.lane = Some(CreateLane::Ready);
    q.assignee = Some(actor.clone());
    urls.push(t.create_ticket(q).await.unwrap().target.url);
    assert_eq!(
        order(t, &urls).await,
        vec![
            urls[2].clone(),
            urls[3].clone(),
            urls[0].clone(),
            urls[4].clone(),
            urls[1].clone()
        ]
    );
    for url in &urls {
        finish_task(t, url).await;
    }
    t.comment(&urls[2], "Confirmed", None).await.unwrap();
}
pub async fn ticket_patrol_contract(
    t: &dyn TicketService,
    driver: &dyn TicketContractDriver,
    tracker: &str,
    label: &str,
    actor: ActorName,
    other_actor: ActorName,
) {
    ticket_board_order_contract(t, driver, tracker, &actor, &other_actor).await;
    // Interactive takeover replaces assignment and moves through the same placement path.
    let mut q = ticket_request(tracker, "Interactive takeover");
    q.assignee = Some(other_actor.clone());
    q.lane = Some(CreateLane::Ready);
    let url = t.create_ticket(q).await.unwrap().target.url;
    let updated = t
        .update_ticket(
            &url,
            UpdateTicket {
                interactive: Some(true),
                assignee: Some(actor.clone()),
                lane: Some(MoveLane::InProgress),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(updated.assignees, vec![actor.clone()]);
    assert_eq!(updated.lane, Some(Lane::InProgress));
    assert!(updated.interactive);
    let read = t.read(Resource::Tickets, &url, None, "").await.unwrap();
    let item = &read.items()[0];
    assert_eq!(item["assignees"], json!([actor]));
    assert_eq!(item["lane"], "in_progress");
    assert_eq!(item["interactive"], true);
    let reassigned = t
        .update_ticket(
            &url,
            UpdateTicket {
                assignee: Some(other_actor.clone()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(reassigned.assignees, vec![other_actor.clone()]);
    assert!(reassigned.interactive);
    assert_eq!(reassigned.lane, Some(Lane::InProgress));
    finish_task(t, &url).await;
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
    for placement in [
        TicketPlacement::Before(theirs.into()),
        TicketPlacement::After(theirs.into()),
    ] {
        assert_eq!(
            t.update_ticket(
                &mine,
                UpdateTicket {
                    placement: Some(placement),
                    ..Default::default()
                }
            )
            .await
            .unwrap_err(),
            ServiceError::OutOfScope
        );
    }
    for placement in [
        TicketPlacement::Before(mine.clone()),
        TicketPlacement::After(mine.clone()),
    ] {
        assert_eq!(
            t.update_ticket(
                theirs,
                UpdateTicket {
                    placement: Some(placement),
                    ..Default::default()
                }
            )
            .await
            .unwrap_err(),
            ServiceError::OutOfScope
        );
    }
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

pub async fn ticket_placement_tracker_contract(t: &dyn TicketService, tracker: &str, other: &str) {
    let mut q = ticket_request(tracker, "Order source");
    q.lane = Some(CreateLane::Ready);
    let source = t.create_ticket(q).await.unwrap().target.url;
    let mut q = ticket_request(other, "Order anchor");
    q.lane = Some(CreateLane::Ready);
    let anchor = t.create_ticket(q).await.unwrap().target.url;
    for placement in [
        TicketPlacement::Before(anchor.clone()),
        TicketPlacement::After(anchor.clone()),
    ] {
        let e = t
            .update_ticket(
                &source,
                UpdateTicket {
                    placement: Some(placement),
                    title: Some("Must not change".into()),
                    ..Default::default()
                },
            )
            .await
            .unwrap_err();
        assert_eq!(e.to_string(), "integrations.resources.ticket_placement");
        assert_eq!(ticket_detail(t, &source).await["title"], "Order source");
        assert_eq!(ticket_detail(t, &anchor).await["lane"], "ready");
    }
}
