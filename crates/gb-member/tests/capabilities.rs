mod support;
use gb_member::{MemberError, WriteGuard, repository::*};
use gb_ports::*;
use serde_json::json;
use std::collections::BTreeMap;
struct Stale;
struct Forbidden;
impl RepositoryServices for Forbidden {
    fn tickets(&self) -> gb_member::Result<&dyn TicketService> {
        panic!("provider must not be created")
    }
    fn code(&self) -> gb_member::Result<&dyn CodeHostingService> {
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
        tracker: "tracker".into(),
        title: "Title".into(),
        body: "Body".into(),
        labels: vec![],
        add_to_board: true,
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
fn readiness(blockers: &[&str]) -> Readiness {
    serde_json::from_value(json!({"target":{"kind":"pull_request","url":"url","title":"T","label":"!1"},"base_sha":"base","head_sha":"head",
        "behind_by":0,"out_of_date":false,"current_base_sha":"base","current_head_sha":"head","rollup":"pending","checks":[],"checks_expected":true,
        "readiness":if blockers.is_empty(){"ready"}else{"blocked"},"completion_blockers":blockers.iter().map(|b|json!({"code":b,"message":b,"next_action":"fix"})).collect::<Vec<_>>()})).unwrap()
}
#[test]
fn only_pending_checks_of_nondraft_prs_can_be_awaited() {
    assert!(ensure_awaitable(&[]).is_err());
    assert!(ensure_ready(&[]).is_ok());
    for (draft, blockers, allowed) in [
        (false, vec!["checks_pending"], true),
        (true, vec!["checks_pending"], false),
        (false, vec!["checks_pending", "base_out_of_date"], false),
        (false, vec!["checks_failed"], false),
        (false, vec![], false),
    ] {
        let results = vec![(json!({"draft":draft}), readiness(&blockers))];
        assert_eq!(ensure_awaitable(&results).is_ok(), allowed);
        assert_eq!(ensure_ready(&results).is_ok(), blockers.is_empty());
    }
}
#[tokio::test]
async fn service_failures_keep_the_original_category_and_safe_diagnosis() {
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
        let providers = support::Probe::new(Some(error.clone()));
        let roster = BTreeMap::new();
        let r = Repository {
            services: &providers,
            guard: &Current,
            roster: &roster,
        };
        for result in [
            r.ticket_create(request(), None, true).await,
            r.ticket_comment("ticket", "body").await,
            r.pr_comment("pr", "body").await,
            r.pr_update(
                "pr",
                UpdatePullRequest {
                    title: Some("title".into()),
                    ..Default::default()
                },
            )
            .await,
            r.pr_review("pr", "body", ReviewVerdict::Approve, "head")
                .await,
            r.pr_reply("pr", "id", "body").await,
            r.pr_reaction("pr", "id", SemanticReaction::Ack).await,
            r.ticket_reaction("ticket", "id", SemanticReaction::Ack)
                .await,
            r.read(Resource::Tickets, "ticket", None, "").await,
            r.read(Resource::DependencyAlerts, "repo", None, "").await,
        ] {
            assert!(matches!(result,Err(MemberError::Service(ref e)) if e==&error));
        }
    }
}
fn pr_page(author_type: &str, state: &str) -> ReadPage {
    ReadPage::of(Resource::PullRequests,[json!({"kind":"pull_request","url":"url","title":"T","label":"!1","repository":"repo","body":"","state":state,
        "assignees":[],"author":"local-aiko","requested_reviewers":[],"labels":[],"author_type":author_type,"head":"feature","head_repository":"repo","head_sha":"head","base":"main","merged":false,"draft":false,"changed_files":null})],None,None).unwrap()
}
#[tokio::test]
async fn completion_revalidates_only_owned_open_prs_and_preserves_read_failures() {
    assert!(
        completion_readiness(None, None, &[])
            .await
            .unwrap()
            .is_empty()
    );
    let mut provider = support::Probe::new(None);
    provider.pages.insert(
        "pull_requests|url|".into(),
        Ok(pr_page("Assistant", "open")),
    );
    provider.checks.insert("url".into(), readiness(&[]));
    assert_eq!(
        completion_readiness(Some(&provider), Some("url"), &[])
            .await
            .unwrap()
            .len(),
        1
    );
    provider
        .checks
        .insert("url".into(), readiness(&["checks_pending"]));
    assert!(
        completion_readiness(Some(&provider), Some("url"), &[])
            .await
            .is_err()
    );
    for (author, state) in [("User", "open"), ("Assistant", "closed")] {
        provider
            .pages
            .insert("pull_requests|url|".into(), Ok(pr_page(author, state)));
        assert!(
            completion_readiness(Some(&provider), Some("url"), &[])
                .await
                .unwrap()
                .is_empty()
        );
    }
    provider
        .pages
        .insert("pull_requests|ticket|".into(), Err(ServiceError::NotFound));
    provider.pages.insert(
        "ticket_pull_requests|ticket|".into(),
        Err(ServiceError::Unsupported),
    );
    assert!(
        completion_readiness(Some(&provider), Some("ticket"), &[])
            .await
            .unwrap()
            .is_empty()
    );
    provider.pages.insert(
        "pull_requests|ticket|".into(),
        Err(ServiceError::Unavailable),
    );
    assert!(matches!(
        completion_readiness(Some(&provider), Some("ticket"), &[]).await,
        Err(MemberError::Service(ServiceError::Unavailable))
    ));
}
#[tokio::test]
async fn wait_notices_keep_the_work_identity_and_do_not_wait_for_ready_prs() {
    let mut provider = support::Probe::new(None);
    let roster = BTreeMap::new();
    let pending = readiness(&["checks_pending"]);
    provider.checks.insert("url".into(), pending.clone());
    let r = Repository {
        services: &provider,
        guard: &Current,
        roster: &roster,
    };
    let results = vec![(json!({"draft":false}), pending)];
    let work = || AwaitWork {
        person_id: "aiko",
        run_id: "run-1",
        work_kind: "ticket",
        work_identity: "ticket-url",
        subject: "ticket",
    };
    let notices = r
        .await_checks(&results, work(), |_| "localized wait notice".into())
        .await
        .unwrap();
    assert_eq!(notices.len(), 1);
    {
        let rows = provider.notices.lock().unwrap();
        assert_eq!(rows[0]["work_identity"], "ticket-url");
        assert_eq!(rows[0]["head"], "head");
    }
    assert!(
        r.await_checks(
            &[(json!({"draft":true}), readiness(&["checks_pending"]))],
            work(),
            |_| String::new()
        )
        .await
        .is_err()
    );
    assert_eq!(provider.notices.lock().unwrap().len(), 1);
}
#[tokio::test]
async fn closed_board_imports_are_idempotent_and_keep_merge_identity() {
    let d = tempfile::tempdir().unwrap();
    let writes = gb_base::shared_writes::SharedWrites::new(d.path().into(), None);
    let redactor = gb_observability::redaction::Redactor::default();
    let mut provider = support::Probe::new(None);
    provider.closed = vec![
        ClosedItem {
            kind: WorkKind::Ticket,
            url: "ticket".into(),
            title: "T".into(),
            label: "T-1".into(),
            closed_at: "2026-01-01T01:00:00Z".into(),
            merged: false,
        },
        ClosedItem {
            kind: WorkKind::PullRequest,
            url: "pr".into(),
            title: "PR".into(),
            label: "!1".into(),
            closed_at: "2026-01-01T02:00:00Z".into(),
            merged: true,
        },
    ];
    let start = "2026-01-01T00:00:00Z".parse().unwrap();
    let end = "2026-01-02T00:00:00Z".parse().unwrap();
    assert_eq!(
        gb_member::events::refresh(d.path(), &writes, None, "", start, end, &redactor)
            .await
            .unwrap(),
        0
    );
    for expected in [2, 0] {
        assert_eq!(
            gb_member::events::refresh(
                d.path(),
                &writes,
                Some(&provider),
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
    assert!(
        events
            .iter()
            .any(|r| r["attributes"]["target.activity_id"] == "pull_request:pr:merged")
    );
}
#[tokio::test]
async fn open_readiness_skips_closed_results() {
    let mut provider = support::Probe::new(None);
    provider.open = vec!["url".into(), "closed".into()];
    provider.checks.insert("url".into(), readiness(&[]));
    let mut closed = readiness(&[]);
    closed.readiness = ReadinessReadiness::NotApplicable;
    provider.checks.insert("closed".into(), closed);
    assert_eq!(
        open_pull_request_readiness(&provider, "repo", "feature")
            .await
            .unwrap(),
        vec![json!({"url":"url","readiness":"ready","completion_blockers":[]})]
    );
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
            include_str!("../../../guildbotics/templates/locales/cli/member.en.yml"),
        ),
        (
            "ja",
            include_str!("../../../guildbotics/templates/locales/cli/member.ja.yml"),
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
        let mut errors = vec![r.ticket_create(request(), None, false).await.unwrap_err()];
        for state in ["open", "closed"] {
            errors.push(
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
            );
        }
        for enabled in [true, false] {
            errors.push(
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
            );
        }
        for error in errors {
            let rendered = error.render(translate);
            assert!(rendered.contains("--human-approved"));
            assert!(!rendered.contains("%{"));
            assert!(rendered.contains(if language == "ja" {
                "人の依頼"
            } else {
                "human instruction"
            }));
        }
    }
}
#[tokio::test]
async fn roster_assignment_and_initial_ticket_state_reach_the_provider() {
    let provider = support::Probe::new(Some(ServiceError::Unsupported));
    let roster = BTreeMap::from([
        ("agent".into(), ActorName("agent-account".into())),
        ("human".into(), ActorName("human-account".into())),
    ]);
    let r = Repository {
        services: &provider,
        guard: &Current,
        roster: &roster,
    };
    for person in ["agent", "human"] {
        let mut request = request();
        request.interactive = true;
        request.lane = Some(CreateLane::InProgress);
        request.parent = Some("parent".into());
        request.blocked_by = vec![" blocker ".into(), " ".into()];
        request.labels = vec![" bug ".into()];
        request.add_to_board = false;
        assert!(r.ticket_create(request, Some(person), true).await.is_err());
        let requests = provider.ticket_requests.lock().unwrap();
        let sent = requests.last().unwrap();
        assert_eq!(sent.assignee.as_ref(), roster.get(person));
        assert_eq!(sent.blocked_by, vec!["blocker"]);
        assert_eq!(sent.labels, vec!["bug"]);
        assert_eq!(sent.lane, Some(CreateLane::InProgress));
        assert!(sent.interactive);
        assert!(!sent.add_to_board);
        assert_eq!(sent.parent.as_deref(), Some("parent"));
    }
}
