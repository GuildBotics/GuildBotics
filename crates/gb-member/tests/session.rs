use crate::support;
use gb_base::shared_writes::SharedWrites;
use gb_member::{
    MemberError, WriteGuard,
    command::Parsed,
    memory::{Memory, Scope},
    repository::{Repository, RepositoryServices},
    session::{Control, Session},
};
use gb_observability::redaction::Redactor;
use gb_ports::*;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, sync::Mutex};
struct Services(support::Probe);
impl RepositoryServices for Services {
    fn code(&self) -> gb_member::Result<&dyn CodeHostingService> {
        Ok(&self.0)
    }
    fn tickets(&self) -> gb_member::Result<&dyn TicketService> {
        Ok(&self.0)
    }
}
struct Current;
impl WriteGuard for Current {
    fn ensure_current(&self) -> gb_member::Result<()> {
        Ok(())
    }
}
#[derive(Default)]
struct Records(Mutex<Vec<Value>>);
impl Control for Records {
    fn command<'a>(
        &'a self,
        parsed: &'a Parsed,
        content: &'a str,
    ) -> gb_member::git::Pending<'a, Value> {
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
    services: &'a Services,
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
    let services = Services(support::Probe::new(None));
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
    let services = Services(support::Probe::new(Some(ServiceError::Unavailable)));
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
    let services = Services(support::Probe::new(None));
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
    let services = Services(support::Probe::new(None));
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
    let services = Services(support::Probe::new(None));
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
async fn context_without_a_code_host_keeps_credentials_unchecked() {
    struct Absent;
    impl RepositoryServices for Absent {
        fn code(&self) -> gb_member::Result<&dyn CodeHostingService> {
            panic!("absent code host must not be consulted")
        }
        fn tickets(&self) -> gb_member::Result<&dyn TicketService> {
            panic!("no ticket access")
        }
        fn code_configured(&self) -> bool {
            false
        }
    }
    let services = Services(support::Probe::new(None));
    let roster = BTreeMap::new();
    let records = Records::default();
    let mut session = session(&services, &roster, &records);
    session.repository.services = &Absent;
    let result = session
        .execute(
            &args(&["context", "--person", "aiko", "--check-credentials"]),
            "",
        )
        .await
        .unwrap();
    assert_eq!(result.payload["credential_status"], "unchecked");
}

#[tokio::test]
async fn checks_retry_returns_url_and_preserves_service_errors() {
    for error in [
        None,
        Some(ServiceError::NotFound),
        Some(ServiceError::Unsupported),
    ] {
        let services = Services(support::Probe::new(error.clone()));
        let roster = BTreeMap::new();
        let records = Records::default();
        let session = session(&services, &roster, &records);
        let result = session
            .execute(
                &args(&["checks", "retry", "--person", "aiko", "--job-url", "job"]),
                "",
            )
            .await;
        if let Some(error) = error {
            assert!(
                matches!(result, Err(e) if e.downcast_ref::<MemberError>().is_some_and(|e| matches!(e, MemberError::Service(actual) if actual == &error)))
            );
            assert!(records.0.lock().unwrap().is_empty());
        } else {
            assert_eq!(result.unwrap().payload["url"], "job/next");
            assert_eq!(records.0.lock().unwrap()[0]["command"], "checks retry");
        }
    }
}

#[tokio::test]
async fn ticket_assignment_and_lane_require_interactive_approval_and_roster() {
    let services = Services(support::Probe::new(Some(ServiceError::Unsupported)));
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
}

#[tokio::test]
async fn interactive_ticket_update_resolves_roster_and_returns_complete_state() {
    for (person, actor) in [("aiko", "agent1"), ("owner", "human1")] {
        let mut probe = support::Probe::new(None);
        probe.update_result = Some(
            serde_json::from_value(json!({
                "body":"", "state":"open", "state_changed":false, "labels":[],
                "target":{"kind":"ticket","url":"url","title":"Work","label":"#1"},
                "assignees":[actor], "lane":"in_progress", "interactive":true,
                "interactive_ancestor":null
            }))
            .unwrap(),
        );
        let services = Services(probe);
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
                    "url",
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
        assert_eq!(result.payload["assignees"], json!([actor]));
        assert_eq!(result.payload["lane"], "in_progress");
        assert_eq!(result.payload["interactive"], true);
        let requests = services.0.ticket_updates.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].assignee, Some(ActorName(actor.into())));
        assert_eq!(requests[0].lane, Some(MoveLane::InProgress));
        assert_eq!(requests[0].interactive, Some(true));
    }
}
