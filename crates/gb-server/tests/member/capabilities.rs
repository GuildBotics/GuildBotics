use crate::support;
use branvil::agent::port::*;
use branvil::agent::{CodeAgent, TicketAgent};
use gb_member::{MemberError, WriteGuard};
use gb_server::member::repository::*;
use serde_json::json;
use std::collections::BTreeMap;
struct Stale;
struct Forbidden;
impl RepositoryServices for Forbidden {
    fn tickets(&self) -> gb_member::Result<&TicketAgent> {
        panic!("provider must not be created")
    }
    fn code(&self) -> gb_member::Result<&CodeAgent> {
        panic!("provider must not be created")
    }
}
struct Current;
impl WriteGuard for Current {
    fn ensure_current(&self) -> gb_member::Result<()> {
        Ok(())
    }
}
impl WriteGuard for Stale {
    fn ensure_current(&self) -> gb_member::Result<()> {
        Err(MemberError::Capability("chat.stale"))
    }
}
fn request() -> CreateTicket {
    CreateTicket {
        tracker: support::REPO.into(),
        title: "Title".into(),
        body: "Body".into(),
        labels: vec![],
        interactive: false,
        assignee: None,
        lane: None,
        parent: None,
        blocked_by: vec![],
    }
}
#[tokio::test]
async fn human_approval_precedes_service_and_chat_access() {
    let providers = Forbidden;
    let roster = BTreeMap::new();
    let r = Repository {
        services: &providers,
        guard: &Stale,
        roster: &roster,
    };
    assert_eq!(
        r.ticket_create(request(), None, false)
            .await
            .unwrap_err()
            .to_string(),
        "cli.member.human_approval_required"
    );
    for state in ["open", "closed"] {
        assert_eq!(
            r.ticket_update(
                "url",
                UpdateTicket {
                    state: Some(state.into()),
                    ..Default::default()
                },
                false
            )
            .await
            .unwrap_err()
            .to_string(),
            "cli.member.human_approval_required"
        );
    }
    for enabled in [true, false] {
        assert_eq!(
            r.ticket_update(
                "url",
                UpdateTicket {
                    interactive: Some(enabled),
                    ..Default::default()
                },
                false
            )
            .await
            .unwrap_err()
            .to_string(),
            "cli.member.human_approval_required"
        );
    }
    assert_eq!(
        r.ticket_create(request(), Some("missing"), true)
            .await
            .unwrap_err()
            .to_string(),
        "cli.member.ticket_assignee"
    );
}
#[tokio::test]
async fn writes_consult_the_durable_chat_guard() {
    let providers = Forbidden;
    let roster = BTreeMap::new();
    let r = Repository {
        services: &providers,
        guard: &Stale,
        roster: &roster,
    };
    assert_eq!(
        r.ticket_create(request(), None, true)
            .await
            .unwrap_err()
            .to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.ticket_comment("url", "body")
            .await
            .unwrap_err()
            .to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.pr_comment("url", "body").await.unwrap_err().to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.pr_review("url", "body", ReviewVerdict::Approve, "head")
            .await
            .unwrap_err()
            .to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.pr_reply("url", "id", "body")
            .await
            .unwrap_err()
            .to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.pr_reaction("url", "id", SemanticReaction::Ack)
            .await
            .unwrap_err()
            .to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.ticket_reaction("url", "id", SemanticReaction::Celebrate)
            .await
            .unwrap_err()
            .to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.ticket_update(
            "url",
            UpdateTicket {
                parent: Some("".into()),
                ..Default::default()
            },
            false
        )
        .await
        .unwrap_err()
        .to_string(),
        "chat.stale"
    );
    assert_eq!(
        r.ticket_update(
            "url",
            UpdateTicket {
                add_labels: vec![" ".into()],
                ..Default::default()
            },
            false
        )
        .await
        .unwrap_err()
        .to_string(),
        "cli.member.ticket_update_empty"
    );
    assert_eq!(
        r.pr_update("url", UpdatePullRequest::default())
            .await
            .unwrap_err()
            .to_string(),
        "cli.member.pr_update_empty"
    );
}
#[test]
fn all_inline_coordinate_branches_preserve_their_diagnosis() {
    let base = ReviewComment {
        body: "body".into(),
        head: "head".into(),
        path: "file".into(),
        line: 4,
        side: "RIGHT".into(),
        start_line: None,
        start_side: None,
    };
    assert!(validate_location(&base).is_ok());
    let mut ranged = base.clone();
    ranged.start_line = Some(2);
    ranged.start_side = Some("RIGHT".into());
    assert!(validate_location(&ranged).is_ok());
    let mut cases = Vec::new();
    let mut c = base.clone();
    c.path = " ".into();
    cases.push((c, "review_path"));
    let mut c = base.clone();
    c.line = 0;
    cases.push((c, "review_line"));
    let mut c = base.clone();
    c.side = "UP".into();
    cases.push((c, "review_side"));
    let mut c = ranged.clone();
    c.start_side = Some("UP".into());
    cases.push((c, "review_start_side"));
    let mut c = base;
    c.start_line = Some(2);
    cases.push((c, "review_range"));
    let mut c = ranged.clone();
    c.start_line = Some(0);
    cases.push((c, "review_start_line"));
    let mut c = ranged.clone();
    c.start_side = Some("LEFT".into());
    cases.push((c, "review_range_side"));
    let mut c = ranged;
    c.start_line = Some(5);
    cases.push((c, "review_range_order"));
    for (request, key) in cases {
        assert_eq!(
            validate_location(&request).unwrap_err().to_string(),
            format!("cli.member.{key}")
        );
    }
}
#[test]
fn evidence_includes_written_pushed_and_resumed_prs_once() {
    assert_eq!(
        evidence_urls(&[
            json!({"payload":{"target":{"kind":"pull_request","url":"one"}}}),
            json!({"payload":{"pull_requests":[{"url":"two"},{"url":"one"},{"url":""}]}}),
            json!({"payload":{"target":{"kind":"ticket","url":"ticket"}}})
        ]),
        vec!["one", "two"]
    );
}
fn readiness(states: &[&str]) -> Readiness {
    readiness_of("url", states.first().copied().unwrap_or("passed"))
}
fn readiness_of(url: &str, state: &str) -> Readiness {
    Readiness {
        target: WorkTarget {
            kind: WorkKind::PullRequest,
            url: url.into(),
            title: "T".into(),
            label: "!1".into(),
            closed: false,
        },
        report: branvil::api::pulls::ChecksView {
            head_sha: "head".into(),
            base_sha: String::new(),
            behind_by: None,
            conflicts: None,
            checks: vec![],
            required_checks: serde_json::from_value(json!(state)).unwrap(),
            required_names: vec![],
            checks_expected: false,
            base_checks: 0,
        },
        feedback: Feedback::default(),
        failed_logs: None,
    }
}
#[test]
fn run_completion_uses_only_required_checks() {
    assert!(ensure_awaitable(&[]).is_err());
    assert!(ensure_ready(&[]).is_ok());
    for draft in [false, true] {
        for state in ["passed", "running", "failed"] {
            let results = vec![(
                json!({"draft":draft,"conflicts":["a"],"unanswered":["c"]}),
                readiness(&[state]),
            )];
            assert_eq!(ensure_ready(&results).is_ok(), state == "passed");
            assert_eq!(ensure_awaitable(&results).is_ok(), state == "running");
        }
    }
    assert!(
        ensure_awaitable(&[
            (json!({}), readiness(&["running"])),
            (json!({}), readiness(&["failed"]))
        ])
        .is_err()
    );
}
#[tokio::test]
async fn service_failures_keep_the_original_category_and_safe_diagnosis() {
    let providers = support::Embedded::new().await;
    let ticket = providers.ticket("T").await;
    let pr = providers.pull("feature", true, &[]).await;
    let roster = BTreeMap::new();
    for error in [
        ServiceError::Failed,
        ServiceError::NotFound,
        ServiceError::Unavailable,
        ServiceError::Unsupported,
        ServiceError::Diagnostic {
            class: ServiceErrorClass::Unavailable,
            key: "safe.diagnosis",
            args: BTreeMap::new(),
        },
    ] {
        providers.fail_all(error.clone());
        let r = Repository {
            services: &providers,
            guard: &Current,
            roster: &roster,
        };
        for result in [
            r.ticket_create(request(), None, true).await,
            r.ticket_comment(&ticket, "body").await,
            r.pr_comment(&pr, "body").await,
            r.pr_update(
                &pr,
                UpdatePullRequest {
                    title: Some("title".into()),
                    ..Default::default()
                },
            )
            .await,
            r.pr_review(&pr, "body", ReviewVerdict::Approve, "head")
                .await,
            r.pr_reply(&pr, "id", "body").await,
            r.pr_reaction(&pr, "id", SemanticReaction::Ack).await,
            r.ticket_reaction(&ticket, "id", SemanticReaction::Ack)
                .await,
            r.read(Resource::Tickets, &ticket, None, "").await,
            r.read(Resource::DependencyAlerts, support::REPO, None, "")
                .await,
        ] {
            assert!(matches!(result,Err(MemberError::Service(ref e)) if e==&error));
        }
    }
    providers.stop().await;
}
/// Renders a member diagnostic from the shipped catalog; a missing key fails.
fn localized(error: &MemberError, language: &str) -> String {
    let source = match language {
        "en" => include_str!("../../../../guildbotics/templates/locales/cli/member.en.yml"),
        _ => include_str!("../../../../guildbotics/templates/locales/cli/member.ja.yml"),
    };
    let catalog: serde_json::Value = serde_saphyr::from_str(source).unwrap();
    error.render(|key: &str, args: &BTreeMap<String, String>| {
        let mut text = catalog[language][key.strip_prefix("cli.member.").unwrap()]
            .as_str()
            .unwrap_or_else(|| panic!("{key} is not in the {language} catalog"))
            .to_owned();
        for (key, value) in args {
            text = text.replace(&format!("%{{{key}}}"), value);
        }
        text
    })
}
#[tokio::test]
async fn completion_revalidates_only_owned_open_prs_and_preserves_read_failures() {
    assert!(
        completion_readiness(&Forbidden, None, &[])
            .await
            .unwrap()
            .is_empty()
    );
    let provider = support::Embedded::new().await;
    let mine = provider.pull("mine", true, &[]).await;
    let ready = completion_readiness(&provider, Some(&mine), &[])
        .await
        .unwrap();
    assert_eq!(ready.len(), 1);
    assert_eq!(ready[0].target.url, mine);
    provider.require_checks();
    let gated = provider.pull("gated", true, &[]).await;
    let incomplete = completion_readiness(&provider, Some(&gated), &[])
        .await
        .unwrap_err();
    assert!(matches!(
        incomplete,
        MemberError::CapabilityArgs {
            key: "cli.member.required_checks_incomplete",
            ..
        }
    ));
    // The key and its blockers argument resolve in both catalogs.
    for language in ["en", "ja"] {
        assert!(
            localized(&incomplete, language).contains(&format!("{gated}: running")),
            "{language}"
        );
    }
    let theirs = provider.pull("theirs", false, &[]).await;
    let closed = provider.pull("closed", true, &[]).await;
    provider.close_pull(&closed).await;
    for url in [&theirs, &closed] {
        assert!(
            completion_readiness(&provider, Some(url), &[])
                .await
                .unwrap()
                .is_empty()
        );
    }
    // A ticket kept outside this code host has no pull requests here.
    let foreign = "https://elsewhere.invalid/acme/repo/-/tickets/1";
    assert!(
        completion_readiness(&provider, Some(foreign), &[])
            .await
            .unwrap()
            .is_empty()
    );
    provider.code.faults.fail("read", ServiceError::Unavailable);
    assert!(matches!(
        completion_readiness(&provider, Some(foreign), &[]).await,
        Err(MemberError::Service(ServiceError::Unavailable))
    ));
    provider.stop().await;
}
#[tokio::test]
async fn wait_notices_keep_the_work_identity_and_do_not_wait_for_ready_prs() {
    let provider = support::Embedded::new().await;
    provider.require_checks();
    let pr = provider.pull("feature", true, &[]).await;
    let pending = provider
        .code
        .readiness(&pr, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(pending.report.required_checks, RequiredChecks::Running);
    let roster = BTreeMap::new();
    let r = Repository {
        services: &provider,
        guard: &Current,
        roster: &roster,
    };
    let results = vec![(json!({"draft":false}), pending.clone())];
    let work = || AwaitWork {
        person_id: "aiko",
        run_id: "run-1",
        work_kind: "ticket",
        work_identity: "ticket-url",
        subject: "ticket",
    };
    let notices = r
        .await_checks(
            &results,
            work(),
            |_| "localized wait notice".into(),
            |_| Ok(()),
        )
        .await
        .unwrap();
    assert_eq!(notices.len(), 1);
    let rows = provider.notices(&pr).await;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["work_identity"], "ticket-url");
    assert_eq!(rows[0]["head"], pending.report.head_sha);
    let draft = r
        .await_checks(
            &[(json!({"draft":true}), pending.clone())],
            work(),
            |_| "draft wait notice".into(),
            |_| Ok(()),
        )
        .await;
    assert!(draft.is_ok(), "{draft:?}");
    assert_eq!(provider.notices(&pr).await.len(), 2);
    provider.stop().await;
}
#[tokio::test]
async fn closed_board_imports_are_idempotent_and_keep_merge_identity() {
    let d = tempfile::tempdir().unwrap();
    let writes = gb_base::shared_writes::SharedWrites::new(d.path().into(), None);
    let redactor = gb_observability::redaction::Redactor::default();
    let provider = support::Embedded::new().await;
    let start = chrono::Utc::now() - chrono::Duration::minutes(1);
    let ticket = provider.ticket("T").await;
    let pr = provider.pull("feature", true, &[]).await;
    provider.close_ticket(&ticket).await;
    let view = provider.code.pull_view(&pr).await.unwrap();
    provider
        .fixture
        .merge(support::REPO, view.number, &view.head_sha, &view.base_sha)
        .await;
    let end = chrono::Utc::now() + chrono::Duration::minutes(1);
    let ticket_closed = provider
        .code
        .ticket_view(&ticket)
        .await
        .unwrap()
        .closed_at
        .unwrap();
    let pr_closed = provider
        .code
        .pull_view(&pr)
        .await
        .unwrap()
        .closed_at
        .unwrap();
    assert_eq!(
        gb_server::member::events::refresh(d.path(), &writes, None, "", start, end, &redactor)
            .await
            .unwrap(),
        0
    );
    for expected in [2, 0] {
        assert_eq!(
            gb_server::member::events::refresh(
                d.path(),
                &writes,
                Some(&provider.code),
                "local",
                start,
                end,
                &redactor
            )
            .await
            .unwrap(),
            expected
        );
    }
    let events = gb_observability::activity::ActivityEvents::open(d.path())
        .list_between(start, end)
        .unwrap();
    assert_eq!(events.len(), 2);
    assert!(
        events
            .iter()
            .any(|r| r["attributes"]["target.activity_id"] == format!("pull_request:{pr}:merged"))
    );
    assert!(
        events
            .iter()
            .any(|r| r["attributes"]["target.activity_id"] == format!("ticket:{ticket}:closed"))
    );
    for event in &events {
        assert_eq!(event["member_id"], "");
        let is_pr = event["kind"] == "pull_request.item";
        assert_eq!(
            event["kind"],
            if is_pr {
                "pull_request.item"
            } else {
                "ticket.item"
            }
        );
        assert_eq!(
            event["occurred_at"],
            if is_pr { &pr_closed } else { &ticket_closed }.as_str()
        );
        assert_eq!(
            event["payload"][if is_pr { "pull_request" } else { "ticket" }]["url"],
            if is_pr { &pr } else { &ticket }.as_str()
        );
    }
    provider.stop().await;
}
#[tokio::test]
async fn approval_diagnostics_are_localized_before_service_access() {
    let provider = Forbidden;
    let roster = BTreeMap::new();
    let r = Repository {
        services: &provider,
        guard: &Stale,
        roster: &roster,
    };
    for (language, source) in [
        (
            "en",
            include_str!("../../../../guildbotics/templates/locales/cli/member.en.yml"),
        ),
        (
            "ja",
            include_str!("../../../../guildbotics/templates/locales/cli/member.ja.yml"),
        ),
    ] {
        let catalog: serde_json::Value = serde_saphyr::from_str(source).unwrap();
        let translate = |key: &str, args: &BTreeMap<String, String>| {
            let mut text = catalog[language][key.strip_prefix("cli.member.").unwrap()]
                .as_str()
                .expect("translated key")
                .to_owned();
            for (key, value) in args {
                text = text.replace(&format!("%{{{key}}}"), value);
            }
            text
        };
        let mut errors = vec![(
            r.ticket_create(request(), None, false).await.unwrap_err(),
            "Creating a ticket".to_owned(),
            "チケットの作成".to_owned(),
        )];
        for state in ["open", "closed"] {
            errors.push((
                r.ticket_update(
                    "url",
                    UpdateTicket {
                        state: Some(state.into()),
                        ..Default::default()
                    },
                    false,
                )
                .await
                .unwrap_err(),
                format!("Changing the ticket state to '{state}'"),
                format!("チケットの状態の '{state}' への変更"),
            ));
        }
        for enabled in [true, false] {
            errors.push((
                r.ticket_update(
                    "url",
                    UpdateTicket {
                        interactive: Some(enabled),
                        ..Default::default()
                    },
                    false,
                )
                .await
                .unwrap_err(),
                "Changing the ticket interactive mode".to_owned(),
                "チケットの対話指定の切り替え".to_owned(),
            ));
        }
        for request in [
            UpdateTicket {
                assignee: Some(ActorName("aiko".into())),
                ..Default::default()
            },
            UpdateTicket {
                lane: Some(MoveLane::InProgress),
                ..Default::default()
            },
            UpdateTicket {
                assignee: Some(ActorName("aiko".into())),
                lane: Some(MoveLane::InProgress),
                ..Default::default()
            },
        ] {
            errors.push((
                r.ticket_update("url", request, false).await.unwrap_err(),
                "Changing the ticket assignee or lane".to_owned(),
                "チケットの担当とレーンの変更".to_owned(),
            ));
        }
        for placement in [
            TicketPlacement::Before("anchor".into()),
            TicketPlacement::After("anchor".into()),
        ] {
            errors.push((
                r.ticket_update(
                    "url",
                    UpdateTicket {
                        placement: Some(placement),
                        ..Default::default()
                    },
                    false,
                )
                .await
                .unwrap_err(),
                "Changing the ticket order".to_owned(),
                "チケットの着手の順の変更".to_owned(),
            ));
        }
        for (error, en, ja) in errors {
            let expected = if language == "ja" {
                format!(
                    "{ja}には、人の依頼または承認が必要です。人に確認してから --human-approved を付けて再実行してください。"
                )
            } else {
                format!(
                    "{en} requires a human instruction or approval. Ask the human first, then re-run with --human-approved."
                )
            };
            assert_eq!(error.render(translate), expected);
        }
    }
}
#[tokio::test]
async fn roster_assignment_and_initial_ticket_state_reach_the_provider() {
    let provider = support::Embedded::new().await;
    provider.fixture.member("agent1").await;
    let roster = BTreeMap::from([
        ("agent".into(), ActorName("agent1".into())),
        ("human".into(), ActorName("owner".into())),
    ]);
    let r = Repository {
        services: &provider,
        guard: &Current,
        roster: &roster,
    };
    let parent = provider.ticket("parent").await;
    let blocker = provider.ticket("blocker").await;
    // A ticket started in progress is blocked only by open tickets.
    provider.close_ticket(&blocker).await;
    for person in ["agent", "human"] {
        let mut request = request();
        request.interactive = true;
        request.lane = Some(CreateLane::InProgress);
        request.parent = Some(parent.clone());
        request.blocked_by = vec![format!(" {blocker} "), " ".into()];
        request.labels = vec![" bug ".into()];
        let created = r.ticket_create(request, Some(person), true).await.unwrap();
        let ticket = provider
            .code
            .ticket_view(created["url"].as_str().unwrap())
            .await
            .unwrap();
        assert_eq!(
            ticket.assignee.map(|a| a.name),
            Some(roster[person].0.clone())
        );
        assert_eq!(
            ticket
                .blocked_by
                .iter()
                .map(|t| t.url.as_str())
                .collect::<Vec<_>>(),
            vec![blocker.as_str()]
        );
        assert_eq!(ticket.labels, vec!["bug"]);
        assert_eq!(ticket.lane, "in_progress");
        assert!(ticket.interactive);
        assert_eq!(ticket.parent.map(|t| t.url), Some(parent.clone()));
    }
    provider.stop().await;
}
#[test]
fn blockers_keep_diagnostics_and_omit_ready_targets() {
    let ready = (json!({}), readiness(&[]));
    for code in ["failed", "running"] {
        let blocked = (json!({}), readiness(&[code]));
        for error in [
            ensure_ready(&[ready.clone(), blocked.clone()]).unwrap_err(),
            ensure_ready(&[blocked, ready.clone()]).unwrap_err(),
        ] {
            match error {
                MemberError::CapabilityArgs { args, .. } => {
                    assert_eq!(args["blockers"], format!("url: {code}"))
                }
                other => panic!("{other}"),
            }
        }
    }
    if let MemberError::CapabilityArgs { args, .. } =
        ensure_awaitable(&[(json!({}), readiness(&["failed"]))]).unwrap_err()
    {
        assert_eq!(args["blockers"], "url: failed");
    } else {
        panic!("expected diagnostic arguments");
    }
    assert_eq!(
        ensure_awaitable(&[]).unwrap_err().to_string(),
        "cli.member.checks_none"
    );
}
#[tokio::test]
async fn completion_preserves_factory_failure_and_orders_owned_references() {
    struct Missing;
    impl RepositoryServices for Missing {
        fn tickets(&self) -> gb_member::Result<&TicketAgent> {
            unreachable!()
        }
        fn code(&self) -> gb_member::Result<&CodeAgent> {
            Err(ServiceError::Diagnostic {
                class: ServiceErrorClass::Failure,
                key: "integrations.resources.unsupported",
                args: [("service".into(), "code_hosting_service".into())].into(),
            }
            .into())
        }
    }
    assert!(matches!(
        completion_readiness(&Missing, Some("ticket"), &[]).await,
        Err(MemberError::Service(ServiceError::Diagnostic {
            key: "integrations.resources.unsupported",
            ..
        }))
    ));
    let p = support::Embedded::new().await;
    let ticket = p.ticket("T").await;
    let mine = p.pull("mine", true, &[&ticket]).await;
    p.pull("foreign", false, &[&ticket]).await;
    let closed = p.pull("closed", true, &[&ticket]).await;
    p.close_pull(&closed).await;
    let touched = p.pull("touched", true, &[]).await;
    let evidence = [json!({"payload":{"pull_requests":[{"url":mine},{"url":touched}]}})];
    let result = completion_readiness(&p, Some(&ticket), &evidence)
        .await
        .unwrap();
    assert_eq!(
        result
            .iter()
            .map(|r| r.target.url.as_str())
            .collect::<Vec<_>>(),
        vec![mine.as_str(), touched.as_str()]
    );
    p.stop().await;
}
#[tokio::test]
async fn waits_record_each_success_and_retain_all_work_status_fields() {
    let p = support::Embedded::new().await;
    p.require_checks();
    let a = p.pull("a", true, &[]).await;
    let pending = p
        .code
        .readiness(&a, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    let roster = BTreeMap::new();
    for kind in ["ticket", "chat", "manual", "checks_resumed"] {
        let r = Repository {
            services: &p,
            guard: &Current,
            roster: &roster,
        };
        let mut recorded = vec![];
        r.await_checks(
            &[
                (json!({"draft":false}), readiness(&[])),
                (json!({"draft":false}), pending.clone()),
            ],
            AwaitWork {
                person_id: "aiko",
                run_id: "run",
                work_kind: kind,
                work_identity: "original",
                subject: "subject",
            },
            |_| "wait".into(),
            |v| {
                recorded.push(v.clone());
                Ok(())
            },
        )
        .await
        .unwrap();
        assert_eq!(recorded.len(), 1);
        assert_eq!(recorded[0]["target"]["url"], a);
        assert_eq!(
            p.notices(&a).await.last().unwrap(),
            &json!({"reason":"awaiting_checks","person_id":"aiko","run_id":"run","subject":"subject","head":pending.report.head_sha,"work_kind":kind,"work_identity":"original","kind":"workflow_error","routing":"suppress"})
        );
    }
    // The notice on a pull request that no longer exists fails after the first.
    let gone = readiness_of(
        &format!("{}/{}/-/pulls/999", p.base(), support::REPO),
        "running",
    );
    let r = Repository {
        services: &p,
        guard: &Current,
        roster: &roster,
    };
    let mut recorded = vec![];
    let results = [pending.clone(), gone].map(|r| (json!({"draft":false}), r));
    assert!(
        r.await_checks(
            &results,
            AwaitWork {
                person_id: "aiko",
                run_id: "run",
                work_kind: "chat",
                work_identity: "original",
                subject: "subject"
            },
            |_| "wait".into(),
            |v| {
                recorded.push(v.clone());
                Ok(())
            }
        )
        .await
        .is_err()
    );
    assert_eq!(recorded.len(), 1);
    assert_eq!(recorded[0]["target"]["url"], a);
    p.stop().await;
}
