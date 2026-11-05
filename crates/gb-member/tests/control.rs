mod support;
use gb_base::shared_writes::SharedWrites;
use gb_member::{
    MemberError, WriteGuard, command,
    control::{Identity, Recipients, RunControl},
    repository::{Repository, RepositoryServices},
    session::Control,
};
use gb_observability::{
    redaction::Redactor,
    runs::{RunRecord, RunStore},
};
use gb_ports::*;
use serde_json::json;
use std::collections::BTreeMap;
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
struct Receives;
impl Recipients for Receives {
    fn receives<'a>(&'a self, _: &'a str, _: &'a str) -> gb_member::git::Pending<'a, bool> {
        Box::pin(async { Ok(true) })
    }
}
fn parsed(command: &[&str]) -> command::Parsed {
    command::parse(&command.iter().map(|v| (*v).into()).collect::<Vec<_>>()).unwrap()
}
fn seed(writes: &SharedWrites, workspace: &std::path::Path, redactor: &Redactor) {
    let record:RunRecord=serde_json::from_value(json!({"run_id":"run","work_kind":"command","execution_mode":"autonomous","member_id":"aiko","device_id":"device","started_at":"2026-11-05T00:00:00Z"})).unwrap();
    writes
        .transaction(|writer| {
            RunStore::open(workspace).start(writer, record, redactor)?;
            Ok(())
        })
        .unwrap();
}
fn control<'a>(
    workspace: &'a std::path::Path,
    writes: &'a SharedWrites,
    repository: &'a Repository<'a>,
    redactor: &'a Redactor,
    recipients: &'a dyn Recipients,
    roster: &'a [gb_member::context::PublicMember],
) -> RunControl<'a> {
    RunControl {
        chat: None,
        identity: Identity {
            run_id: "run",
            person_id: "aiko",
            work_kind: "command",
            work_identity: "command-identity",
            subject_type: "command",
            subject_id: "command-identity",
            subject_url: "",
        },
        workspace,
        writes,
        repository,
        roster,
        recipients,
        redactor,
        observe: &|_, _, _| Ok(()),
        render_notice: &|key| key.into(),
        render_wait: &|_| "wait".into(),
    }
}
#[tokio::test]
async fn completion_requires_its_own_evidence_and_stores_a_redacted_summary() {
    let temp = tempfile::tempdir().unwrap();
    let workspace = temp.path().join("workspace");
    let writes = SharedWrites::new(temp.path().join("identity"), Some(workspace.clone()));
    let redactor = Redactor::new(["secret-sentinel".into()]);
    seed(&writes, &workspace, &redactor);
    let services = Services(support::Probe::new(None));
    let roster = BTreeMap::new();
    let repository = Repository {
        services: &services,
        guard: &Current,
        roster: &roster,
    };
    let mut control = control(&workspace, &writes, &repository, &redactor, &Receives, &[]);
    control.identity.work_identity = "command-identity";
    control.identity.subject_type = "command";
    control.identity.subject_id = "command-identity";
    control.identity.person_id = "aiko";
    let done = parsed(&[
        "task",
        "complete",
        "--person",
        "aiko",
        "--status",
        "done",
        "--content-stdin",
    ]);
    assert!(control.command(&done, "summary").await.is_err());
    control
        .record("ticket comment", false, &json!({"target":{"url":"item"}}))
        .unwrap();
    let result = control
        .command(&done, "summary secret-sentinel")
        .await
        .unwrap();
    assert_eq!(result["run_id"], "run");
    assert_eq!(result["evidence_count"], 1);
    assert!(!result.to_string().contains("secret-sentinel"));
    let status = control
        .command(&parsed(&["task", "status"]), "")
        .await
        .unwrap();
    assert_eq!(status["status"], "done");
}
#[tokio::test]
async fn asking_and_blocked_need_a_comment_and_foreign_members_cannot_complete_the_run() {
    let temp = tempfile::tempdir().unwrap();
    let workspace = temp.path().join("workspace");
    let writes = SharedWrites::new(temp.path().join("identity"), Some(workspace.clone()));
    let redactor = Redactor::new([]);
    seed(&writes, &workspace, &redactor);
    let services = Services(support::Probe::new(None));
    let roster = BTreeMap::new();
    let repository = Repository {
        services: &services,
        guard: &Current,
        roster: &roster,
    };
    let mut control = control(&workspace, &writes, &repository, &redactor, &Receives, &[]);
    control.identity.work_identity = "command-identity";
    control.identity.subject_type = "command";
    control.identity.subject_id = "command-identity";
    control.identity.person_id = "aiko";
    for status in ["asking", "blocked"] {
        let p = parsed(&[
            "task",
            "complete",
            "--person",
            "aiko",
            "--status",
            status,
            "--content-stdin",
        ]);
        assert!(matches!(
            control.command(&p, "reason").await,
            Err(MemberError::CapabilityArgs {
                key: "capabilities.task_runs.comment_required",
                ..
            })
        ));
    }
    control
        .record("pr reply", false, &json!({"target":{"url":"item"}}))
        .unwrap();
    control.identity.person_id = "other";
    let p = parsed(&[
        "task",
        "complete",
        "--person",
        "other",
        "--status",
        "blocked",
        "--content-stdin",
    ]);
    assert!(control.command(&p, "reason").await.is_err());
    assert!(
        RunStore::open(&workspace)
            .read("run")
            .unwrap()
            .unwrap()
            .result
            .is_none()
    );
}
#[tokio::test]
async fn publishing_code_needs_a_pr_receipt_and_failed_operations_provide_no_evidence() {
    let temp = tempfile::tempdir().unwrap();
    let workspace = temp.path().join("workspace");
    let writes = SharedWrites::new(temp.path().join("identity"), Some(workspace.clone()));
    let redactor = Redactor::new([]);
    seed(&writes, &workspace, &redactor);
    let services = Services(support::Probe::new(None));
    let roster = BTreeMap::new();
    let repository = Repository {
        services: &services,
        guard: &Current,
        roster: &roster,
    };
    let mut control = control(&workspace, &writes, &repository, &redactor, &Receives, &[]);
    control.identity.work_identity = "command-identity";
    control.identity.subject_type = "command";
    control.identity.subject_id = "command-identity";
    control.identity.person_id = "aiko";
    control
        .record("git publish", false, &json!({"pushed":true}))
        .unwrap();
    let p = parsed(&[
        "task",
        "complete",
        "--person",
        "aiko",
        "--status",
        "done",
        "--content-stdin",
    ]);
    assert!(control.command(&p, "summary").await.is_err());
    control
        .record("pr create", false, &json!({"created":true}))
        .unwrap();
    assert!(control.command(&p, "summary").await.is_ok());
    assert_eq!(
        RunStore::open(&workspace)
            .read("run")
            .unwrap()
            .unwrap()
            .provider_evidence
            .len(),
        2
    );
}
struct Routing<'a> {
    routes: std::collections::BTreeSet<(&'a str, &'a str)>,
    mutate: Option<(&'a SharedWrites, &'a std::path::Path)>,
}
impl Recipients for Routing<'_> {
    fn receives<'a>(
        &'a self,
        person: &'a str,
        subject: &'a str,
    ) -> gb_member::git::Pending<'a, bool> {
        Box::pin(async move {
            if let Some((writes, workspace)) = self.mutate {
                writes
                    .transaction(|writer| {
                        let mut record = RunStore::open(workspace).read("run")?.unwrap();
                        record
                            .attributes
                            .insert("changed".into(), "during-await".into());
                        RunStore::open(workspace).start(writer, record, &Redactor::default())?;
                        Ok(())
                    })
                    .unwrap();
            }
            Ok(self.routes.contains(&(person, subject)))
        })
    }
}
fn member(id: &str, kind: &str, active: bool) -> gb_member::context::PublicMember {
    gb_member::context::PublicMember {
        person_id: id.into(),
        person_type: kind.into(),
        is_active: active,
        name: id.into(),
        roles: json!({}),
        profile: json!({}),
        speaking_style: String::new(),
        accounts: BTreeMap::new(),
    }
}
#[tokio::test]
async fn waiting_uses_only_commented_targets_and_validates_every_recipient() {
    for (recipients, kind, active, comment, accepted) in [
        (vec!["bot"], "agent", true, true, true),
        (vec!["bot", "bot"], "agent", true, true, true),
        (vec!["bot", "other"], "agent", true, true, true),
        (vec!["bot"], "agent", false, true, false),
        (vec!["missing"], "agent", true, true, false),
        (vec!["aiko"], "agent", true, true, false),
        (vec!["bot"], "agent", true, false, false),
        (vec!["bot"], "human", false, true, true),
        (vec![], "agent", true, true, true),
    ] {
        let temp = tempfile::tempdir().unwrap();
        let workspace = temp.path().join("workspace");
        let writes = SharedWrites::new(temp.path().join("identity"), Some(workspace.clone()));
        let redactor = Redactor::default();
        seed(&writes, &workspace, &redactor);
        let services = Services(support::Probe::new(None));
        let accounts = BTreeMap::new();
        let repository = Repository {
            services: &services,
            guard: &Current,
            roster: &accounts,
        };
        let roster = [member("bot", kind, active), member("other", kind, active)];
        let routing = Routing {
            routes: [("bot", "commented-pr"), ("other", "commented-ticket")].into(),
            mutate: None,
        };
        let mut control = control(
            &workspace,
            &writes,
            &repository,
            &redactor,
            &routing,
            &roster,
        );
        control.identity.work_identity = "source-ticket";
        control.identity.subject_type = "command";
        control.identity.subject_id = "source-ticket";
        control.identity.person_id = "aiko";
        control
            .record(
                if comment { "pr comment" } else { "pr update" },
                false,
                &json!({"target":{"url":"commented-pr"}}),
            )
            .unwrap();
        if comment {
            control
                .record(
                    "ticket comment",
                    false,
                    &json!({"target":{"url":"commented-ticket"}}),
                )
                .unwrap();
        }
        let mut input = vec![
            "task",
            "complete",
            "--person",
            "aiko",
            "--status",
            "asking",
            "--content-stdin",
        ];
        for recipient in &recipients {
            input.extend(["--waiting-for", recipient]);
        }
        let result = control.command(&parsed(&input), "question").await;
        assert_eq!(
            result.is_ok(),
            accepted,
            "{recipients:?} {kind} {active} {comment}"
        );
        if let Ok(result) = result {
            assert_eq!(result["subject_id"], "source-ticket");
            assert_eq!(
                result["waiting_for"].as_array().unwrap().len(),
                recipients
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
            );
            if recipients.is_empty() {
                assert_eq!(
                    result["notification"],
                    "capabilities.task_runs.owner_notified"
                );
            }
        }
    }
}
#[tokio::test]
async fn a_journal_changed_during_async_recipient_validation_cannot_complete() {
    let temp = tempfile::tempdir().unwrap();
    let workspace = temp.path().join("workspace");
    let writes = SharedWrites::new(temp.path().join("identity"), Some(workspace.clone()));
    let redactor = Redactor::default();
    seed(&writes, &workspace, &redactor);
    let services = Services(support::Probe::new(None));
    let accounts = BTreeMap::new();
    let repository = Repository {
        services: &services,
        guard: &Current,
        roster: &accounts,
    };
    let roster = [member("bot", "agent", true)];
    let routing = Routing {
        routes: [("bot", "commented-pr")].into(),
        mutate: Some((&writes, &workspace)),
    };
    let mut control = control(
        &workspace,
        &writes,
        &repository,
        &redactor,
        &routing,
        &roster,
    );
    control.identity.work_identity = "source-ticket";
    control.identity.subject_type = "command";
    control.identity.subject_id = "source-ticket";
    control.identity.person_id = "aiko";
    control
        .record(
            "pr comment",
            false,
            &json!({"target":{"url":"commented-pr"}}),
        )
        .unwrap();
    let result = control
        .command(
            &parsed(&[
                "task",
                "complete",
                "--person",
                "aiko",
                "--status",
                "asking",
                "--waiting-for",
                "bot",
                "--content-stdin",
            ]),
            "question",
        )
        .await;
    assert!(result.is_err());
    assert!(
        RunStore::open(&workspace)
            .read("run")
            .unwrap()
            .unwrap()
            .result
            .is_none()
    );
}
#[tokio::test]
async fn completion_evidence_rules_cover_chat_events_threads_and_all_completion_states() {
    for (subject, command, status, evidence, accepted) in [
        ("chat", "chat complete", "done", "chat noop", true),
        ("chat", "chat complete", "done", "ticket comment", false),
        ("chat", "chat complete", "asking", "chat noop", false),
        ("chat", "chat complete", "asking", "chat reply", true),
        ("chat", "chat complete", "blocked", "", true),
        ("chat_thread", "task complete", "done", "chat noop", false),
        ("chat_thread", "task complete", "done", "pr update", true),
        ("chat_thread", "task complete", "asking", "pr comment", true),
        ("command", "task complete", "done", "pr review", true),
        (
            "command",
            "task complete",
            "done",
            "pr review-comment",
            true,
        ),
        ("command", "task complete", "done", "pr reply", true),
        ("command", "task complete", "blocked", "pr update", false),
    ] {
        let temp = tempfile::tempdir().unwrap();
        let workspace = temp.path().join("workspace");
        let writes = SharedWrites::new(temp.path().join("identity"), Some(workspace.clone()));
        let redactor = Redactor::default();
        seed(&writes, &workspace, &redactor);
        let services = Services(support::Probe::new(None));
        let accounts = BTreeMap::new();
        let repository = Repository {
            services: &services,
            guard: &Current,
            roster: &accounts,
        };
        let mut control = control(&workspace, &writes, &repository, &redactor, &Receives, &[]);
        control.identity.work_identity = "source";
        control.identity.subject_type = subject;
        control.identity.subject_id = "source";
        control.identity.person_id = "aiko";
        if !evidence.is_empty() {
            control.record(evidence, false, &json!({})).unwrap();
        }
        let mut args = command.split_whitespace().collect::<Vec<_>>();
        args.extend(["--person", "aiko", "--status", status, "--content-stdin"]);
        let result = control.command(&parsed(&args), "safe summary").await;
        assert_eq!(result.is_ok(), accepted, "{subject}/{status}/{evidence}");
    }
}
