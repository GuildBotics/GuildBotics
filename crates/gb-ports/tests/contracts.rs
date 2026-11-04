//! Golden results were produced by the models and read_conditions executed
//! from python-host:fea9e05aaf8b4bca96e19d615b6811ea0eacb7d0, not Rust.
//! Inputs exercise each required field and type, explicit null, omitted
//! defaults, nested models, and rejected unknown fields.
use gb_ports::*;
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

#[test]
fn rate_limit_reset_requires_an_explicit_zone_and_preserves_unknown_holds() {
    let now = gb_ports::parse_timestamp("2026-01-02T00:00:00Z").unwrap();
    for (time, held) in [
        ("2026-01-02T00:00", true),
        ("20260101T2359", true),
        (" 2026-01-01T23:59+00:00 ", true),
        ("2026-01-01T23:59+00:00", false),
        ("20260102T0000Z", false),
        ("2026-W01-5T00:00+00:00", false),
        ("2026-01-02🦀00:00+00:00", false),
        ("20260102T0001", true),
        ("2026-W01-5T00:01+00:00", true),
        ("invalid", true),
    ] {
        let status=gb_ports::WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"rate_limited","person_id":"aiko","retry_after_at":time})).unwrap();
        assert_eq!(status.holds_selection(now), held, "{time}");
    }
}

fn normalize<T: DeserializeOwned + Serialize>(value: Value) -> Result<Value, serde_json::Error> {
    serde_json::to_value(serde_json::from_value::<T>(value)?)
}

fn model(name: &str, value: Value) -> Result<Value, serde_json::Error> {
    macro_rules! models {
        ($($ty:ident),+ $(,)?) => {
            match name { $(stringify!($ty) => normalize::<$ty>(value)),+, _ => panic!("unknown fixture model: {name}") }
        }
    }
    models!(
        TicketSummary,
        Ticket,
        Comment,
        Attachment,
        TicketPullRequest,
        PullRequest,
        Review,
        FileChange,
        ReviewThread,
        ThreadComment,
        Readiness,
        DependencyAlert,
        TicketCreated,
        TicketUpdated,
        ClosedItem,
        PullRequestHead,
        PullRequestCreated,
        PullRequestUpdated,
        ReviewSubmitted,
        ReviewCommentWritten,
        ReplyWritten,
        ArtifactRef,
        WorkTarget,
        TicketRelative,
        CommentableLine,
        Check,
        CompletionBlocker,
        FailedLog,
        AdvisoryIdentifier,
        CommentWritten,
        ReactionAdded
    )
}

#[test]
fn frozen_python_model_results_and_rejections() {
    let fixtures: Vec<Value> = serde_json::from_str(include_str!("fixtures/models.json")).unwrap();
    for case in fixtures {
        let name = case["model"].as_str().unwrap();
        let actual = model(name, case["input"].clone());
        if case["valid"] == true {
            assert_eq!(actual.unwrap(), case["expected"], "{case}");
        } else {
            assert!(actual.is_err(), "accepted invalid {case}");
        }
    }
}

#[test]
fn frozen_python_read_conditions() {
    let fixtures: Vec<Value> = serde_json::from_str(include_str!("fixtures/queries.json")).unwrap();
    for case in fixtures {
        let resource = case["resource"].as_str().unwrap().parse().unwrap();
        let actual = read_conditions(
            resource,
            (!case["parameters"].is_null()).then_some(&case["parameters"]),
            case["continuation"].as_str().unwrap(),
        );
        if let Some(error) = case.get("error") {
            let expected = match error.as_str().unwrap() {
                "continuation" => ServiceError::InvalidContinuation,
                "parameters" => ServiceError::InvalidParameters,
                other => panic!("unknown Python error: {other}"),
            };
            assert_eq!(actual, Err(expected), "{case}");
        } else {
            assert_eq!(actual.unwrap(), case["expected"], "{case}");
        }
    }
}

#[test]
fn every_resource_validates_pages_before_exposing_them() {
    let metadata: Value = serde_json::from_str(include_str!("fixtures/resources.json")).unwrap();
    let fixtures: Vec<Value> = serde_json::from_str(include_str!("fixtures/models.json")).unwrap();
    let names = [
        (Resource::TrackerTickets, "TicketSummary"),
        (Resource::Tickets, "Ticket"),
        (Resource::TicketComments, "Comment"),
        (Resource::TicketAttachments, "Attachment"),
        (Resource::TicketPullRequests, "TicketPullRequest"),
        (Resource::PullRequests, "PullRequest"),
        (Resource::PullRequestComments, "Comment"),
        (Resource::PullRequestReviews, "Review"),
        (Resource::PullRequestFiles, "FileChange"),
        (Resource::PullRequestThreads, "ReviewThread"),
        (Resource::ReviewThreadComments, "ThreadComment"),
        (Resource::PullRequestReadiness, "Readiness"),
        (Resource::DependencyAlerts, "DependencyAlert"),
    ];
    assert_eq!(
        names.iter().map(|(r, _)| *r).collect::<Vec<_>>(),
        Resource::ALL
    );
    for (resource, name) in names {
        let subject = match resource.subject() {
            ReadSubject::Ticket => "ticket",
            ReadSubject::PullRequest => "pull_request",
            ReadSubject::Repository => "repository",
        };
        assert_eq!(
            metadata[resource.name()],
            json!({"subject":subject,"ticket_service":resource.is_ticket_service(),"detail":resource.is_detail()})
        );
        assert_eq!(resource.name().parse::<Resource>().unwrap(), resource);
        for fixture in fixtures.iter().filter(|f| f["model"] == name) {
            let actual = ReadPage::of(
                resource,
                [fixture["input"].clone()],
                Some("next".into()),
                None,
            );
            if fixture["valid"] == true {
                let page = actual.unwrap();
                assert_eq!(page.items(), &[fixture["expected"].clone()]);
                assert_eq!(page.continuation(), Some("next"));
                assert!(page.target().is_none());
                assert_eq!(
                    serde_json::to_value(page).unwrap(),
                    json!({"items":[fixture["expected"]],"continuation":"next","target":null})
                );
            } else {
                assert_eq!(
                    actual,
                    Err(ServiceError::InvalidResult),
                    "{resource:?}: {fixture}"
                );
            }
        }
    }
    assert_eq!(
        "unknown".parse::<Resource>(),
        Err(ServiceError::InvalidResource)
    );
}

#[test]
fn actor_names_are_distinct_from_member_ids_and_preserve_all_actor_fields() {
    // Typed construction fails to compile if an actor field becomes a String.
    let actor = ActorName("public-account".into());
    let fixtures: Vec<Value> = serde_json::from_str(include_str!("fixtures/models.json")).unwrap();
    let example = |name: &str| {
        fixtures
            .iter()
            .find(|v| v["model"] == name && v["valid"] == true)
            .unwrap()["input"]
            .clone()
    };
    let ticket: Ticket = serde_json::from_value(example("Ticket")).unwrap();
    let pull: PullRequest = serde_json::from_value(example("PullRequest")).unwrap();
    let comment: Comment = serde_json::from_value(example("Comment")).unwrap();
    let review: Review = serde_json::from_value(example("Review")).unwrap();
    let thread: ThreadComment = serde_json::from_value(example("ThreadComment")).unwrap();
    let authors: [&ActorName; 4] = [
        &pull.author,
        &comment.author,
        &review.author,
        &thread.author,
    ];
    let assigned: [&Vec<ActorName>; 3] = [
        &ticket.assignees,
        &pull.assignees,
        &pull.requested_reviewers,
    ];
    assert!(authors.into_iter().all(|a| a == &actor));
    assert!(
        assigned
            .into_iter()
            .all(|a| a == std::slice::from_ref(&actor))
    );
    let population: Value = serde_json::from_str(include_str!("fixtures/actors.json")).unwrap();
    let actual = population
        .as_object()
        .unwrap()
        .iter()
        .flat_map(|(r, fields)| {
            fields
                .as_array()
                .unwrap()
                .iter()
                .map(move |f| (r.as_str(), f.as_str().unwrap()))
        })
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(
        actual,
        std::collections::BTreeSet::from([
            ("tickets", "assignees"),
            ("ticket_comments", "author"),
            ("pull_requests", "assignees"),
            ("pull_requests", "author"),
            ("pull_requests", "requested_reviewers"),
            ("pull_request_comments", "author"),
            ("pull_request_reviews", "author"),
            ("review_thread_comments", "author")
        ])
    );
}

#[test]
fn statuses_suppress_work_and_only_failures_and_rate_limits_hold_selection() {
    let now = "2026-11-04T12:00:00Z".parse().unwrap();
    for reason in [
        "failed",
        "rate_limited",
        "review_limit",
        "awaiting_checks",
        "checks_settled",
        "asking",
        "blocked",
    ] {
        let status: WorkflowStatus = serde_json::from_value(json!({"reason":reason,"person_id":"member","run_id":"run","work_kind":"ticket","work_identity":"url"})).unwrap();
        assert_eq!(
            status.holds_selection(now),
            ["failed", "rate_limited"].contains(&reason)
        );
        assert_eq!(status.payload()["kind"], "workflow_error");
        assert_eq!(status.payload()["routing"], "suppress");
        assert!(status.payload().get("subject").is_none());
        assert_eq!(
            WorkflowStatus::from_payload(&status.payload()),
            Some(status)
        );
    }
    for (retry, expected) in [
        ("", true),
        ("invalid", true),
        ("2026-11-04T12:00:00", true),
        ("2026-11-04T12:00:00Z", false),
        ("2026-11-04T21:00:01+09:00", true),
        ("2026-11-03T12:00:00Z", false),
    ] {
        let status = WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"rate_limited","person_id":"member","retry_after_at":retry,"unknown":"ignored"})).unwrap();
        assert_eq!(status.holds_selection(now), expected, "{retry}");
    }
    for payload in [
        json!(null),
        json!({}),
        json!({"kind":"work"}),
        json!({"kind":"workflow_error","reason":"invalid","person_id":"m"}),
        json!({"kind":"workflow_error","reason":"awaiting_checks","person_id":"m"}),
        json!({"kind":"workflow_error","reason":"awaiting_checks","person_id":"m","work_kind":"ticket"}),
    ] {
        assert!(WorkflowStatus::from_payload(&payload).is_none());
    }
}
