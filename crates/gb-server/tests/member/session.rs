use crate::support;
use crate::support::{Current, Unused};
use branvil::agent::port::*;
use branvil::agent::{CodeAgent, TicketAgent};
use gb_base::shared_writes::SharedWrites;
use gb_member::{
    MemberError,
    command::Parsed,
    memory::{Memory, Scope},
};
use gb_observability::redaction::Redactor;
use gb_server::member::{
    repository::{Repository, RepositoryServices},
    session::{Control, Session},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, sync::Mutex};
#[derive(Default)]
struct Records(Mutex<Vec<Value>>);
impl Control for Records {
    fn command<'a>(
        &'a self,
        parsed: &'a Parsed,
        content: &'a str,
    ) -> gb_server::member::git::Pending<'a, Value> {
        Box::pin(async move { Ok(json!({"command":parsed.command,"summary":content})) })
    }
    fn record(&self, command: &str, read_only: bool, payload: &Value) -> gb_member::Result<()> {
        self.0
            .lock()
            .unwrap()
            .push(json!({"command":command,"read_only":read_only,"payload":payload}));
        Ok(())
    }
}
fn session<'a>(
    services: &'a dyn RepositoryServices,
    roster: &'a BTreeMap<String, ActorName>,
    records: &'a Records,
) -> Session<'a> {
    Session {
        person_id: "aiko",
        person_type: "agent",
        delegated_to: Some("aiko"),
        workflow: true,
        repository: Repository {
            services,
            guard: &Current,
            roster,
        },
        chat: None,
        memory: None,
        git: None,
        context: json!({"person_id":"aiko"}),
        control: records,
        git_root: Path::new("/unused-host-root"),
    }
}
fn args(values: &[&str]) -> Vec<String> {
    values.iter().map(|v| (*v).into()).collect()
}
#[tokio::test]
async fn human_subject_and_missing_or_foreign_delegation_fail_before_a_write() {
    let services = Unused;
    let roster = BTreeMap::new();
    let records = Records::default();
    let input = args(&[
        "ticket",
        "comment",
        "--person",
        "aiko",
        "--url",
        "url",
        "--content-stdin",
    ]);
    for delegated in [None, Some("other")] {
        let mut session = session(&services, &roster, &records);
        session.delegated_to = delegated;
        let error = session.execute(&input, "body").await.err().unwrap();
        assert!(error.to_string().contains("invalid_delegation"));
    }
    let mut session = session(&services, &roster, &records);
    session.person_type = "human";
    assert_eq!(
        session
            .execute(&input, "body")
            .await
            .err()
            .unwrap()
            .to_string(),
        "cli.member.human_subject"
    );
    assert!(records.0.lock().unwrap().is_empty());
}
#[tokio::test]
async fn read_only_commands_need_no_workflow_delegation_and_failed_providers_keep_their_category() {
    let services = support::Embedded::new().await;
    services.fail_all(ServiceError::Unavailable);
    let roster = BTreeMap::new();
    let records = Records::default();
    let mut session = session(&services, &roster, &records);
    session.delegated_to = None;
    assert_eq!(
        session
            .execute(&args(&["context", "--person", "aiko"]), "")
            .await
            .unwrap()
            .payload["person_id"],
        "aiko"
    );
    let error = session
        .execute(
            &args(&[
                "repo-info",
                "read",
                "--person",
                "aiko",
                "--resource",
                "dependency_alerts",
                "--subject",
                "o/r",
            ]),
            "",
        )
        .await
        .err()
        .unwrap();
    assert!(matches!(
        error.downcast_ref::<MemberError>(),
        Some(MemberError::Service(ServiceError::Unavailable))
    ));
    assert_eq!(records.0.lock().unwrap().len(), 1);
    services.stop().await;
}
#[tokio::test]
async fn memory_dispatch_masks_four_character_values_and_preserves_source_scope_and_optional_body()
{
    let temp = tempfile::tempdir().unwrap();
    let workspace = temp.path().join("workspace");
    let identity = temp.path().join("identity");
    let writes = SharedWrites::new(identity, Some(workspace.clone()));
    let masks = Redactor::with_min_length(["FOUR".into()], 4);
    let memory = Memory::open(
        &writes,
        &workspace,
        "aiko",
        &temp.path().join("data"),
        false,
        &masks,
        json!({}),
    )
    .unwrap();
    let services = Unused;
    let roster = BTreeMap::new();
    let records = Records::default();
    let mut session = session(&services, &roster, &records);
    session.memory = Some(memory);
    let answer = session
        .execute(
            &args(&[
                "memory",
                "record",
                "--person",
                "aiko",
                "--scope",
                "team",
                "--title",
                "note",
                "--ticket",
                " ticket-url ",
                "--thread",
                "thread-url",
                "--content-stdin",
            ]),
            "private FOUR value\n",
        )
        .await
        .unwrap();
    let id = answer.payload["doc_id"].as_str().unwrap();
    session
        .execute(
            &args(&[
                "memory", "update", "--person", "aiko", "--id", id, "--team", "--title", "updated",
            ]),
            "unconsumed",
        )
        .await
        .unwrap();
    let doc = session
        .memory
        .as_ref()
        .unwrap()
        .get(id, Some(Scope::Team))
        .unwrap();
    assert!(!doc.to_string().contains("FOUR"));
    assert!(doc["body"].as_str().unwrap().starts_with("private "));
    assert!(!doc["body"].as_str().unwrap().contains("unconsumed"));
    assert_eq!(
        doc["meta"]["source"],
        json!([{"type":"ticket","url":"ticket-url"},{"type":"thread","url":"thread-url"}])
    );
}
#[tokio::test]
async fn usage_and_content_errors_never_access_services_or_record_evidence() {
    let services = Unused;
    let roster = BTreeMap::new();
    let records = Records::default();
    let session = session(&services, &roster, &records);
    for input in [
        args(&[
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "url",
            "--content-file",
            "/must-not-open",
        ]),
        args(&[
            "pr", "create", "--person", "aiko", "--repo", "o/r", "--head", "topic", "--title",
            "title",
        ]),
        args(&[
            "ticket",
            "comment",
            "--person",
            "another",
            "--url",
            "url",
            "--content-stdin",
        ]),
    ] {
        assert!(session.execute(&input, "body").await.is_err());
    }
    assert!(records.0.lock().unwrap().is_empty());
}
#[tokio::test]
async fn run_commands_dispatch_only_through_the_bound_control_and_reject_caller_run_ids() {
    let services = Unused;
    let roster = BTreeMap::new();
    let records = Records::default();
    let session = session(&services, &roster, &records);
    let answer = session
        .execute(
            &args(&[
                "task",
                "complete",
                "--person",
                "aiko",
                "--status",
                "done",
                "--content-stdin",
            ]),
            "summary\n",
        )
        .await
        .unwrap();
    assert_eq!(
        answer.payload,
        json!({"command":"task complete","summary":"summary\n"})
    );
    assert!(
        session
            .execute(
                &args(&[
                    "task",
                    "complete",
                    "--person",
                    "aiko",
                    "--status",
                    "done",
                    "--run-id",
                    "other",
                    "--content-stdin"
                ]),
                "summary"
            )
            .await
            .is_err()
    );
    assert_eq!(records.0.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn context_credential_check_never_consults_the_code_host() {
    struct Absent;
    impl RepositoryServices for Absent {
        fn code(&self) -> gb_member::Result<&CodeAgent> {
            panic!("absent code host must not be consulted")
        }
        fn tickets(&self) -> gb_member::Result<&TicketAgent> {
            panic!("no ticket access")
        }
    }
    let roster = BTreeMap::new();
    let records = Records::default();
    let session = session(&Absent, &roster, &records);
    let result = session
        .execute(
            &args(&["context", "--person", "aiko", "--check-credentials"]),
            "",
        )
        .await
        .unwrap();
    assert_eq!(result.payload["person_id"], "aiko");
    assert!(result.payload.get("credential_status").is_none());
}

#[tokio::test]
async fn checks_retry_returns_url_and_preserves_service_errors() {
    let services = support::Embedded::new().await;
    let pr = services.pull("feature", true, &[]).await;
    let head = services.code.pull_view(&pr).await.unwrap().head_sha;
    services
        .fixture
        .check(support::REPO, "feature", &head, Some("failure"))
        .await;
    let checks = services
        .code
        .readiness(&pr, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap()
        .report
        .checks;
    let (workspace, repository) = support::REPO.split_once('/').unwrap();
    let jobs = format!(
        "{}/workspaces/{workspace}/repos/{repository}/jobs/",
        services.base()
    );
    let job = format!("{jobs}{}", checks[0].attempt_id.as_deref().unwrap());
    let roster = BTreeMap::new();
    for error in [
        Some(ServiceError::NotFound),
        Some(ServiceError::Unsupported),
        None,
    ] {
        services.code.faults.clear();
        if let Some(error) = &error {
            services.code.faults.fail("retry_job", error.clone());
        }
        let records = Records::default();
        let session = session(&services, &roster, &records);
        let result = session
            .execute(
                &args(&["checks", "retry", "--person", "aiko", "--job-url", &job]),
                "",
            )
            .await;
        if let Some(error) = error {
            assert!(
                matches!(result, Err(e) if e.downcast_ref::<MemberError>().is_some_and(|e| matches!(e, MemberError::Service(actual) if actual == &error)))
            );
            assert!(records.0.lock().unwrap().is_empty());
        } else {
            let next = result.unwrap().payload["url"].as_str().unwrap().to_owned();
            assert!(next.starts_with(&jobs));
            assert_ne!(next, job);
            assert_eq!(records.0.lock().unwrap()[0]["command"], "checks retry");
        }
    }
    services.stop().await;
}

#[tokio::test]
async fn ticket_assignment_and_lane_require_interactive_approval_and_roster() {
    let services = support::Embedded::new().await;
    services
        .code
        .faults
        .fail("update_ticket", ServiceError::Unsupported);
    let roster = [
        ("aiko".into(), ActorName("agent1".into())),
        ("owner".into(), ActorName("human1".into())),
    ]
    .into();
    let records = Records::default();
    for option in [["--assignee", "aiko"], ["--lane", "in_progress"]] {
        for (workflow, delegated) in [(true, Some("aiko")), (false, Some("aiko")), (false, None)] {
            let mut session = session(&services, &roster, &records);
            session.workflow = workflow;
            session.delegated_to = delegated;
            let mut input = args(&["ticket", "update", "--person", "aiko", "--url", "url"]);
            input.extend(args(&option));
            if !workflow && delegated.is_none() {
                assert_eq!(
                    session.execute(&input, "").await.err().unwrap().to_string(),
                    "cli.member.human_approval_required"
                );
            }
            input.push("--human-approved".into());
            let error = session.execute(&input, "").await.err().unwrap();
            if workflow || delegated.is_some() {
                assert_eq!(
                    error.to_string(),
                    "cli.member.ticket_update_interactive_only"
                );
            } else {
                assert!(error.downcast_ref::<MemberError>().is_some());
                assert!(error.to_string().contains("unsupported"));
            }
        }
    }
    let mut session = session(&services, &roster, &records);
    session.workflow = false;
    session.delegated_to = None;
    for person in ["aiko", "owner", "missing"] {
        let input = args(&[
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "url",
            "--assignee",
            person,
            "--interactive",
            "--lane",
            "in_progress",
            "--human-approved",
            "--title",
            "Unchanged on refusal",
        ]);
        let error = session.execute(&input, "").await.err().unwrap();
        assert_eq!(
            error.to_string(),
            if person == "missing" {
                "cli.member.ticket_assignee"
            } else {
                "integrations.resources.operation_unsupported"
            }
        );
    }
    assert!(records.0.lock().unwrap().is_empty());
    services.stop().await;
}

#[tokio::test]
async fn interactive_ticket_update_resolves_roster_and_returns_complete_state() {
    let services = support::Embedded::new().await;
    services.fixture.member("agent1").await;
    for (person, actor) in [("aiko", "agent1"), ("owner", "owner")] {
        let url = services.ticket("Work").await;
        let roster = [(person.into(), ActorName(actor.into()))].into();
        let records = Records::default();
        let mut session = session(&services, &roster, &records);
        session.workflow = false;
        session.delegated_to = None;
        let result = session
            .execute(
                &args(&[
                    "ticket",
                    "update",
                    "--person",
                    "aiko",
                    "--url",
                    &url,
                    "--assignee",
                    person,
                    "--interactive",
                    "--lane",
                    "in_progress",
                    "--human-approved",
                ]),
                "",
            )
            .await
            .unwrap();
        assert_eq!(result.payload["assignee"]["name"], actor);
        assert_eq!(result.payload["lane"], "in_progress");
        assert_eq!(result.payload["interactive"], true);
        let ticket = services.code.ticket_view(&url).await.unwrap();
        assert_eq!(ticket.assignee.map(|a| a.name).as_deref(), Some(actor));
        assert_eq!(ticket.lane, "in_progress");
        assert!(ticket.interactive);
    }
    services.stop().await;
}

#[tokio::test]
async fn ticket_order_requires_interactive_approval_and_exclusive_anchor() {
    let services = support::Embedded::new().await;
    services
        .code
        .faults
        .fail("update_ticket", ServiceError::Unavailable);
    let roster = BTreeMap::new();
    let records = Records::default();
    for flag in ["--before", "--after"] {
        let input = args(&[
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "url",
            flag,
            "anchor",
            "--human-approved",
        ]);
        for (workflow, delegated) in [(true, Some("aiko")), (false, Some("aiko"))] {
            let mut s = session(&services, &roster, &records);
            s.workflow = workflow;
            s.delegated_to = delegated;
            assert_eq!(
                s.execute(&input, "").await.err().unwrap().to_string(),
                "cli.member.ticket_order_workflow"
            );
        }
        let mut s = session(&services, &roster, &records);
        s.workflow = false;
        s.delegated_to = None;
        let no_approval = input[..input.len() - 1].to_vec();
        assert_eq!(
            s.execute(&no_approval, "").await.err().unwrap().to_string(),
            "cli.member.human_approval_required"
        );
        // A placement alone must reach the provider rather than empty-update validation.
        let result = s.execute(&input, "").await;
        assert_eq!(
            result.err().unwrap().to_string(),
            "integrations.resources.unavailable"
        );
    }
    let input = args(&[
        "ticket",
        "update",
        "--person",
        "aiko",
        "--url",
        "url",
        "--before",
        "a",
        "--after",
        "b",
        "--human-approved",
    ]);
    let mut s = session(&services, &roster, &records);
    s.workflow = false;
    s.delegated_to = None;
    assert_eq!(
        s.execute(&input, "").await.err().unwrap().to_string(),
        "cli.member.ticket_order_conflict"
    );
    services.stop().await;
}
