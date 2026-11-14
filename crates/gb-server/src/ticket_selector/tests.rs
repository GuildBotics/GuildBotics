use super::*;

#[test]
fn every_rate_limit_reset_hint_and_retry_guidance_renders_in_both_languages() {
    let tokyo = chrono::FixedOffset::east_opt(9 * 3600).unwrap();
    for language in ["en", "ja"] {
        for chat in [false, true] {
            for (at, hint, suffix, display) in [
                (
                    "2026-10-03T15:30:12+09:00",
                    "Resets in 25h57m34s",
                    "_with_reset",
                    "2026-10-03 15:30:12+09:00",
                ),
                (
                    "2026-10-03T06:30:12Z",
                    "",
                    "_with_reset",
                    "2026-10-03 15:30:12+09:00",
                ),
                (
                    "2026-07-04T02:44:00Z",
                    "11:44 AM",
                    "_with_reset",
                    "2026-07-04 11:44:00+09:00",
                ),
                (
                    "",
                    "Resets in 25h57m34s",
                    "_with_hint",
                    "Resets in 25h57m34s",
                ),
                ("", "", "", ""),
                ("invalid", "Resets in 1h", "_with_hint", "Resets in 1h"),
                ("invalid", "", "", ""),
                (
                    "2026-10-03T06:30:12",
                    "Resets in 1h",
                    "_with_hint",
                    "Resets in 1h",
                ),
                ("2026-10-03T06:30:12", "", "", ""),
            ] {
                let guidance = if chat {
                    "rate_limited_retry_chat"
                } else if suffix == "_with_reset" {
                    "rate_limited_retry_at"
                } else {
                    "rate_limited_retry_ticket"
                };
                let guidance = crate::i18n::t(
                    language,
                    &format!("commands.workflows.common.{guidance}"),
                    &[],
                );
                let expected = crate::i18n::t(
                    language,
                    &format!("commands.workflows.common.rate_limited_escalation{suffix}"),
                    &[("retry_after", display), ("retry_guidance", &guidance)],
                );
                let notice = rate_limit_notice_display(language, at, hint, chat, |time| {
                    time.with_timezone(&tokyo)
                        .format("%Y-%m-%d %H:%M:%S%:z")
                        .to_string()
                });
                assert_eq!(notice, expected);
                assert!(!notice.starts_with("commands."));
                assert!(notice.contains(&guidance));
                if suffix == "_with_reset" && !hint.is_empty() {
                    assert!(!notice.contains(hint));
                }
                if suffix != "_with_reset" {
                    assert!(notice.contains(if language == "ja" {
                        "復帰時刻は不明です"
                    } else {
                        "reset time is unknown"
                    }));
                }
            }
        }
    }
}
#[tokio::test]
async fn rate_limit_envelopes_accept_direct_and_exhausted_cli_errors_but_not_other_categories() {
    for wrapped in [false, true] {
        for category in ["rate_limited", "timeout", "unknown"] {
            let f = Fixture::new();
            let task = f.task().await;
            let mut failure = guest(category);
            if wrapped && let DispatchFailure::Guest(error) = &mut failure {
                error.type_ = "CompletionRetryExhausted".into();
            }
            *f.runner.failure.lock().unwrap() = Some(failure);
            let selector = f.selector("routine");
            let trace = f.trace("routine");
            let result = selector
                .run(&task, selector.invocation(&task).unwrap(), &trace, 5)
                .await;
            let limited = category == "rate_limited";
            assert_eq!(result.is_ok(), limited);
            assert_eq!(
                f.runner.activity.lock().unwrap().len(),
                usize::from(limited)
            );
            assert_eq!(f.runner.stops.lock().unwrap().len(), usize::from(!limited));
        }
    }
}

#[tokio::test]
async fn default_and_clamped_budgets_reach_the_turn_without_moving_in_progress_work() {
    for (raw, expected) in [("", 5), ("0", 1), ("x", 5), ("-10", 1), (" 7 ", 7)] {
        let f = Fixture::new();
        let mut task = f.task().await;
        task.status = "in_progress".into();
        let faults = crate::test_ticket::Faults::new(f.ticket.clone());
        faults
            .errors
            .lock()
            .unwrap()
            .insert("move", ServiceError::Failed);
        let mut selector = f.selector("routine");
        selector.ticket = &faults;
        let trace = f.trace("routine");
        selector
            .run(
                &task,
                selector.invocation(&task).unwrap(),
                &trace,
                max_attempts(raw),
            )
            .await
            .unwrap();
        let calls = f.runner.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].run_id, trace.id());
        assert_eq!(calls[0].payload["max_completion_attempts"], expected);
        let stored: Value = serde_json::from_slice(
            &std::fs::read(f.root.path().join("services/tickets/acme%2Fteam/T-1.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(stored["lane"], "ready");
    }
}

#[tokio::test]
async fn listing_limited_pulls_uses_distinct_traces_and_refresh_limit_still_runs_the_next_ticket() {
    for source in ["manual", "scheduled", "routine"] {
        for at_refresh in [false, true] {
            let f = Fixture::new();
            let mut first = f.pull().await;
            let mut second = f.pull_branch("other").await;
            let ticket = f.task().await;
            let mut faults = CodeFaults::new(f.code.clone());
            faults.refresh_limit = at_refresh;
            if !at_refresh {
                first.trigger_reason = Some(REVIEW_LIMIT.into());
                second.trigger_reason = Some(REVIEW_LIMIT.into());
            }
            faults.candidates = Some(vec![first.clone(), second.clone()]);
            let mut selector = f.selector(source);
            selector.code = Some(&faults);
            let (selected, invocation) = selector.next().await.unwrap().unwrap();
            assert_eq!(selected.url, ticket.url);
            let outer = f.trace(source);
            selector
                .run(&selected, invocation, &outer, 5)
                .await
                .unwrap();
            let activity = f.runner.activity.lock().unwrap().clone();
            assert_eq!(activity.len(), 2);
            assert_ne!(activity[0]["trace_id"], activity[1]["trace_id"]);
            assert!(
                activity.iter().all(
                    |e| e["trace_id"] != outer.id() && e["type"] == "pull_request.review_limit"
                )
            );
            assert_eq!(f.runner.stops.lock().unwrap().len(), 2);
            let calls = f.runner.calls.lock().unwrap();
            assert_eq!(calls.len(), 1);
            assert_eq!(
                calls[0].payload["ticket_url"],
                ticket.url.as_deref().unwrap()
            );
            for number in [1, 2] {
                let path = f
                    .root
                    .path()
                    .join(format!("services/code/acme%2Frepo/pulls/{number}.json"));
                let stored: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
                assert_eq!(stored["draft"], true);
                assert_eq!(stored["comments"].as_array().unwrap().len(), 2);
            }
            assert!(
                gb_observability::runs::RunStore::open(f.root.path())
                    .records()
                    .unwrap()
                    .is_empty()
            );
        }
    }
}
#[tokio::test]
async fn concurrent_reviewers_and_a_prior_human_draft_never_announce_a_second_conversion() {
    for drafted_by_human in [false, true] {
        let f = Fixture::new();
        let mut pull = f.pull().await;
        pull.trigger_reason = Some(REVIEW_LIMIT.into());
        if drafted_by_human {
            assert!(
                f.code
                    .convert_pull_request_to_draft(pull.url.as_deref().unwrap())
                    .await
                    .unwrap()
            );
        }
        let first = f.selector("routine");
        let mut second = f.selector("routine");
        second.person = "yuki";
        tokio::join!(first.hand_over(&pull), second.hand_over(&pull));
        assert_eq!(
            f.runner.activity.lock().unwrap().len(),
            usize::from(!drafted_by_human)
        );
        assert_eq!(
            f.runner.stops.lock().unwrap().len(),
            usize::from(!drafted_by_human)
        );
        let stored: Value = serde_json::from_slice(
            &std::fs::read(f.root.path().join("services/code/acme%2Frepo/pulls/1.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(stored["draft"], true);
        assert_eq!(
            stored["comments"].as_array().unwrap().len(),
            if drafted_by_human { 1 } else { 2 }
        );
    }
}

#[tokio::test]
async fn move_errors_preserve_the_ready_lane_and_only_unavailable_autonomous_work_defers() {
    for source in ["manual", "scheduled", "routine"] {
        for error in [
            ServiceError::Failed,
            ServiceError::NotFound,
            ServiceError::Unsupported,
            ServiceError::Unavailable,
        ] {
            let f = Fixture::new();
            let task = f.task().await;
            let faults = crate::test_ticket::Faults::new(f.ticket.clone());
            faults.errors.lock().unwrap().insert("move", error.clone());
            let mut selector = f.selector(source);
            selector.ticket = &faults;
            let trace = f.trace(source);
            assert!(
                matches!(selector.run(&task,selector.invocation(&task).unwrap(),&trace,5).await,Err(DispatchFailure::Service(e)) if e==error)
            );
            assert!(f.runner.calls.lock().unwrap().is_empty());
            let stored: Value = serde_json::from_slice(
                &std::fs::read(f.root.path().join("services/tickets/acme%2Fteam/T-1.json"))
                    .unwrap(),
            )
            .unwrap();
            assert_eq!(stored["lane"], "ready");
            let deferred = source != "manual" && error.is_unavailable();
            let comments = f.comments(&task).await;
            assert_eq!(comments.len(), usize::from(!deferred));
            if !deferred {
                assert_eq!(stored["comments"][0]["status"]["reason"], "failed");
            }
            assert_eq!(
                f.runner.stops.lock().unwrap().len(),
                usize::from(!deferred && source != "manual")
            );
        }
    }
}
#[tokio::test]
async fn failed_comment_delivery_keeps_rate_limit_evidence_and_an_unaddressed_failure_still_notifies()
 {
    for fail_post in [false, true] {
        for limited in [false, true] {
            let f = Fixture::new();
            let task = f.task().await;
            let mut faults = crate::test_ticket::Faults::new(f.ticket.clone());
            faults.omit_comment_url = true;
            if fail_post {
                faults
                    .errors
                    .lock()
                    .unwrap()
                    .insert("comment", ServiceError::Failed);
            }
            let mut selector = f.selector("routine");
            selector.ticket = &faults;
            *f.runner.failure.lock().unwrap() =
                Some(guest(if limited { "rate_limited" } else { "unknown" }));
            let trace = f.trace("routine");
            let result = selector
                .run(&task, selector.invocation(&task).unwrap(), &trace, 5)
                .await;
            assert_eq!(result.is_ok(), limited);
            assert_eq!(f.comments(&task).await.len(), usize::from(!fail_post));
            assert_eq!(
                f.runner.activity.lock().unwrap().len(),
                usize::from(limited)
            );
            let stops = f.runner.stops.lock().unwrap();
            assert_eq!(stops.len(), usize::from(!fail_post && !limited));
            if let Some(stop) = stops.first() {
                assert_eq!(stop.url, task.url.as_deref().unwrap());
                assert!(stop.comment_url.is_empty());
            }
        }
    }
}
#[test]
fn task_trace_attributes_distinguish_ticket_and_pull_request_without_changing_the_work_kind() {
    for pull in [false, true] {
        let task = Task {
            title: "T".into(),
            url: Some("url".into()),
            pull_request_url: pull.then(|| "url".into()),
            ..Default::default()
        };
        assert_eq!(task_attributes(&task),serde_json::from_value(json!({"target.provider":"","target.label":"","target.kind":if pull {"pull_request"} else {"ticket"},"target.title":"T","target.url":"url"})).unwrap());
    }
}

use gb_config::secrets::{DeviceKey, Keychain, Scope, SecretStore};
use gb_ports::*;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

struct NoKeys;
impl Keychain for NoKeys {
    fn read(&self) -> anyhow::Result<Option<String>> {
        panic!("local provider needs no key")
    }
    fn create(&self, _: &str) -> anyhow::Result<()> {
        panic!("local provider needs no key")
    }
}
#[derive(Default)]
struct FakeRunner {
    calls: Arc<Mutex<Vec<WorkflowInvocation>>>,
    fail_evidence: bool,
    failure: Mutex<Option<DispatchFailure>>,
    records: Mutex<Vec<RunRecord>>,
    evidence: Mutex<Vec<Value>>,
    stops: Mutex<Vec<WorkStop>>,
    said: Mutex<Vec<String>>,
    completed: Mutex<usize>,
    voice: Mutex<Option<String>>,
    activity: Mutex<Vec<Value>>,
}
impl Runner for FakeRunner {
    fn run<'a>(
        &'a self,
        invocation: WorkflowInvocation,
        _: &'a Trace,
    ) -> Pending<'a, Result<CommandReply, DispatchFailure>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push(invocation);
            match self.failure.lock().unwrap().take() {
                Some(error) => Err(error),
                None => Ok(CommandReply::default()),
            }
        })
    }
    fn say<'a>(
        &'a self,
        text: &'a str,
        _: &'a WorkflowInvocation,
        _: &'a Trace,
    ) -> Pending<'a, Option<String>> {
        Box::pin(async move {
            self.said.lock().unwrap().push(text.into());
            self.voice.lock().unwrap().clone()
        })
    }
    fn evidence(&self, _: &str, kind: &str, payload: Value) -> anyhow::Result<()> {
        self.evidence
            .lock()
            .unwrap()
            .push(json!({"type":kind,"payload":payload}));
        if self.fail_evidence && kind.starts_with("checks_yield") {
            anyhow::bail!("journal unavailable");
        }
        Ok(())
    }
    fn record(&self, id: &str) -> anyhow::Result<Option<RunRecord>> {
        Ok(self
            .records
            .lock()
            .unwrap()
            .iter()
            .find(|r| r.run_id == id)
            .cloned())
    }
    fn activity(&self, trace: &Trace, kind: &str, payload: Value) {
        self.activity
            .lock()
            .unwrap()
            .push(json!({"trace_id":trace.id(),"type":kind,"payload":payload}));
    }
    fn notify<'a>(&'a self, stop: WorkStop, _: &'a Trace) -> Pending<'a, ()> {
        Box::pin(async move {
            self.stops.lock().unwrap().push(stop);
        })
    }
    fn completed<'a>(&'a self, _: &'a Task, _: &'a str, _: &'a Trace) -> Pending<'a, ()> {
        Box::pin(async move {
            *self.completed.lock().unwrap() += 1;
        })
    }
}
struct Fixture {
    root: tempfile::TempDir,
    ticket: Arc<dyn TicketService>,
    code: Arc<dyn CodeHostingService>,
    runner: FakeRunner,
    diagnostics: Diagnostics,
}
impl Fixture {
    fn new() -> Self {
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
                keychain: &NoKeys,
            },
        )
        .unwrap();
        let factory = gb_integrations::ServiceIntegrationFactory::new(
            gb_integrations::IntegrationConfig {
                local_root: root.path().join("services"),
                identity_root: root.path().join("identity"),
                person_id: "aiko".into(),
                display_name: "Aiko".into(),
                accounts: Default::default(),
                branvil_members: Default::default(),
                ticket_provider: "local".into(),
                code_provider: "local".into(),
                chat_provider: "local".into(),
                ticket_scope: "acme".into(),
                code_scope: "acme".into(),
                checks: "success".into(),
                slack_base_url: String::new(),
            },
            &secrets,
            None,
            Arc::new(|_| {}),
        )
        .unwrap();
        let ticket = factory.create_ticket_service().unwrap();
        let code = factory.create_code_hosting_service().unwrap();
        let path = root.path().join("services/tickets/acme%2Fteam");
        std::fs::create_dir_all(&path).unwrap();
        std::fs::write(path.join("tracker.json"), br#"{"repository":"acme/repo"}"#).unwrap();
        let diagnostics = Diagnostics::open(root.path());
        Self {
            root,
            ticket,
            code,
            runner: FakeRunner::default(),
            diagnostics,
        }
    }
    fn selector<'a>(&'a self, source: &'a str) -> Selector<'a> {
        Selector {
            person: "aiko",
            source,
            language: "en",
            ticket: self.ticket.as_ref(),
            code: Some(self.code.as_ref()),
            runner: &self.runner,
            quota: None,
            diagnostics: &self.diagnostics,
        }
    }
    async fn task(&self) -> Task {
        self.ticket
            .create_ticket(CreateTicket {
                tracker: "acme/team".into(),
                title: "Fix login".into(),
                body: String::new(),
                labels: vec![],
                add_to_board: true,
                interactive: false,
                assignee: Some(ActorName("local-aiko".into())),
                lane: Some(CreateLane::Ready),
                parent: None,
                blocked_by: vec![],
            })
            .await
            .unwrap();
        self.ticket.candidates().await.unwrap().remove(0)
    }
    fn trace(&self, source: &str) -> Trace {
        Trace::open(&self.diagnostics, source).person("aiko")
    }
    async fn comments(&self, task: &Task) -> Vec<Value> {
        serde_json::to_value(
            self.ticket
                .read(
                    Resource::TicketComments,
                    task.url.as_deref().unwrap(),
                    None,
                    "",
                )
                .await
                .unwrap(),
        )
        .unwrap()["items"]
            .as_array()
            .unwrap()
            .clone()
    }

    async fn pull(&self) -> Task {
        self.pull_branch("feature").await
    }
    async fn pull_branch(&self, branch: &str) -> Task {
        let bare = self
            .root
            .path()
            .join("services/code/acme%2Frepo/repository.git");
        std::fs::create_dir_all(&bare).unwrap();
        let git = |args: &[&str]| {
            let mut cmd = std::process::Command::new("git");
            for (key, _) in std::env::vars_os() {
                if key.to_string_lossy().starts_with("GIT_") {
                    cmd.env_remove(key);
                }
            }
            let output = cmd
                .args([
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                ])
                .args(args)
                .current_dir(&bare)
                .env("HOME", &bare)
                .env("USERPROFILE", &bare)
                .env("XDG_CONFIG_HOME", &bare)
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .env(
                    "GIT_CONFIG_GLOBAL",
                    if cfg!(windows) { "NUL" } else { "/dev/null" },
                )
                .env("GIT_ALLOW_PROTOCOL", "file")
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            String::from_utf8(output.stdout).unwrap().trim().to_owned()
        };
        git(&["init", "--bare", "--initial-branch=main"]);
        let tree = git(&["mktree"]);
        let head = git(&["commit-tree", &tree, "-m", "initial"]);
        for branch in ["main", branch] {
            git(&["update-ref", &format!("refs/heads/{branch}"), &head]);
        }
        let pull = self
            .code
            .create_pull_request(CreatePullRequest {
                repository: "acme/repo".into(),
                head: branch.into(),
                base: "main".into(),
                title: "Fix login".into(),
                body: String::new(),
                draft: false,
                tickets: vec![],
            })
            .await
            .unwrap();
        self.code
            .comment(&pull.target.url, "Please fix", None)
            .await
            .unwrap();
        // A different author leaves feedback for the member's PR.
        let path = self.root.path().join(format!(
            "services/code/acme%2Frepo/pulls/{}.json",
            pull.target.url.rsplit('/').next().unwrap()
        ));
        let mut stored: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        stored["comments"][0]["author"] = json!("reviewer");
        std::fs::write(path, serde_json::to_vec(&stored).unwrap()).unwrap();
        self.code
            .pull_request_candidates()
            .await
            .unwrap()
            .into_iter()
            .find(|task| task.url.as_deref() == Some(&pull.target.url))
            .unwrap()
    }

    fn record(&self, id: &str, evidence: Vec<Value>, summary: &str) {
        self.runner.records.lock().unwrap().push(serde_json::from_value(json!({"run_id":id,"work_kind":"chat","execution_mode":"autonomous",
            "member_id":"aiko","device_id":"device","started_at":"2026-11-05T00:00:00Z","safe_summary":summary,"provider_evidence":evidence})).unwrap());
    }
}
fn guest(category: &str) -> DispatchFailure {
    DispatchFailure::Guest(Box::new(serde_json::from_value(json!({"command":false,"type":"CliAgentExecutionError","message":"private path must never appear",
        "cli_agent":"codex","cli_agent_result":{"error_category":category,"error_details":{"retry_after_at":"2027-01-01T00:00:00Z","retry_after_text":"later"}}})).unwrap()))
}

#[tokio::test]
async fn ready_ticket_moves_once_and_turn_receives_trace_identity_and_completion_budget() {
    let f = Fixture::new();
    let task = f.task().await;
    let selector = f.selector("routine");
    let trace = f.trace("routine");
    selector
        .run(&task, selector.invocation(&task).unwrap(), &trace, 5)
        .await
        .unwrap();
    let calls = f.runner.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].run_id, trace.id());
    assert_eq!(calls[0].payload["max_completion_attempts"], 5);
    assert_eq!(
        calls[0].work.as_ref().unwrap().identity,
        task.url.as_deref().unwrap()
    );
    assert_eq!(
        f.ticket.refresh(&task).await.unwrap().unwrap().status,
        "in_progress"
    );
    assert_eq!(*f.runner.completed.lock().unwrap(), 1);
    assert!(f.comments(&task).await.is_empty());
}

#[tokio::test]
async fn mention_preserves_lane_and_assignee_and_uses_a_distinct_conversation_kind() {
    for lane in ["new", "ready", "in_progress", "done"] {
        let f = Fixture::new();
        let _ = f.task().await;
        let path = f.root.path().join("services/tickets/acme%2Fteam/T-1.json");
        let mut stored: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        stored["lane"] = json!(lane);
        stored["assignees"] = json!(["local-yuki"]);
        stored["comments"] = json!([{"id":"mention","body":"@local-aiko Please provide context.","author":"owner","created_at":"2026-11-05T00:00:00Z"}]);
        std::fs::write(&path, serde_json::to_vec(&stored).unwrap()).unwrap();
        let selector = f.selector("routine");
        let trace = f.trace("routine");
        let candidates = selector.candidates().await.unwrap();
        assert_eq!(candidates.len(), 1);
        let task = &candidates[0];
        let invocation = selector.refresh(task).await.unwrap().unwrap();
        assert_eq!(invocation.work.as_ref().unwrap().kind, "ticket_mention");
        selector.run(task, invocation, &trace, 1).await.unwrap();
        let after: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(after["lane"], lane);
        assert_eq!(after["assignees"], json!(["local-yuki"]));
        f.ticket
            .comment(
                task.url.as_deref().unwrap(),
                "@local-yuki Here is the context.",
                None,
            )
            .await
            .unwrap();
        assert!(selector.refresh(task).await.unwrap().is_none());
    }
}

#[tokio::test]
async fn safe_failure_and_timeout_are_voiced_and_only_autonomous_failures_announce() {
    for language in ["en", "ja"] {
        for source in ["routine", "manual"] {
            for category in ["unknown", "timeout"] {
                for voice in [
                    None,
                    Some("Member's safe explanation".to_owned()),
                    Some(String::new()),
                ] {
                    let f = Fixture::new();
                    let task = f.task().await;
                    *f.runner.failure.lock().unwrap() = Some(guest(category));
                    *f.runner.voice.lock().unwrap() = voice.clone();
                    let mut selector = f.selector(source);
                    selector.language = language;
                    let trace = f.trace(source);
                    assert!(
                        selector
                            .run(&task, selector.invocation(&task).unwrap(), &trace, 5)
                            .await
                            .is_err()
                    );
                    let comments = f.comments(&task).await;
                    assert_eq!(comments.len(), 1);
                    let body = comments[0]["body"].as_str().unwrap();
                    assert!(!body.contains("private path"));
                    if let Some(voice) = voice.filter(|s| !s.is_empty()) {
                        assert_eq!(body, voice);
                    } else {
                        assert_eq!(
                            body,
                            crate::i18n::t(
                                language,
                                if category == "timeout" {
                                    "drivers.task_scheduler.turn_timeout"
                                } else {
                                    "drivers.task_scheduler.task_error"
                                },
                                &[("hours", "3")]
                            )
                        );
                    }
                    assert_eq!(f.runner.said.lock().unwrap().len(), 1);
                    let stops = f.runner.stops.lock().unwrap().clone();
                    assert_eq!(stops.len(), usize::from(source != "manual"));
                    if let Some(stop) = stops.first() {
                        assert_eq!(stop.reason, "failed");
                        assert_eq!(stop.url, task.url.as_deref().unwrap());
                    }
                    assert_eq!(*f.runner.completed.lock().unwrap(), 0);
                    if category == "timeout" && source == "routine" {
                        assert!(selector.candidates().await.unwrap().is_empty());
                        f.ticket
                            .comment(task.url.as_deref().unwrap(), "Please continue.", None)
                            .await
                            .unwrap();
                        let path = f.root.path().join("services/tickets/acme%2Fteam/T-1.json");
                        let mut stored: Value =
                            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
                        stored["comments"][1]["author"] = json!("owner");
                        std::fs::write(path, serde_json::to_vec(&stored).unwrap()).unwrap();
                        let resumed = selector.refresh(&task).await.unwrap().unwrap();
                        assert_eq!(resumed.work, selector.invocation(&task).unwrap().work);
                        assert_eq!(f.comments(&task).await.len(), 2);
                        assert_eq!(f.runner.stops.lock().unwrap().len(), 1);
                    }
                }
            }
        }
    }
}

#[tokio::test]
async fn cancellation_and_unavailable_autonomous_work_leave_no_failure_comment() {
    for source in ["manual", "routine"] {
        for cancelled in [true, false] {
            let f = Fixture::new();
            let task = f.task().await;
            *f.runner.failure.lock().unwrap() = Some(if cancelled {
                DispatchFailure::Cancelled
            } else {
                DispatchFailure::Service(ServiceError::Unavailable)
            });
            let selector = f.selector(source);
            let trace = f.trace(source);
            assert!(
                selector
                    .run(&task, selector.invocation(&task).unwrap(), &trace, 5)
                    .await
                    .is_err()
            );
            let reported = !cancelled && source == "manual";
            assert_eq!(f.comments(&task).await.len(), usize::from(reported));
            assert!(f.runner.stops.lock().unwrap().is_empty());
        }
    }
}

#[tokio::test]
async fn rate_limit_settles_without_worker_failure_or_work_stop_and_preserves_reset() {
    let f = Fixture::new();
    let task = f.task().await;
    *f.runner.failure.lock().unwrap() = Some(guest("rate_limited"));
    let selector = f.selector("routine");
    let trace = f.trace("routine");
    selector
        .run(&task, selector.invocation(&task).unwrap(), &trace, 5)
        .await
        .unwrap();
    let comments = f.comments(&task).await;
    assert_eq!(comments.len(), 1);
    assert!(comments[0]["body"].as_str().unwrap().contains("2027-01-01"));
    assert!(f.runner.said.lock().unwrap().is_empty());
    assert!(f.runner.stops.lock().unwrap().is_empty());
    let activity = f.runner.activity.lock().unwrap().clone();
    assert_eq!(activity.len(), 1);
    assert_eq!(activity[0]["type"], "workflow.rate_limited");
    assert_eq!(activity[0]["trace_id"], trace.id());
    assert_eq!(
        activity[0]["payload"]["retry_after_at"],
        "2027-01-01T00:00:00Z"
    );
    assert_eq!(*f.runner.completed.lock().unwrap(), 0);
    let stored: Value = serde_json::from_slice(
        &std::fs::read(f.root.path().join("services/tickets/acme%2Fteam/T-1.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(stored["comments"][0]["status"]["reason"], "rate_limited");
    assert_eq!(
        stored["comments"][0]["status"]["retry_after_at"],
        "2027-01-01T00:00:00Z"
    );
}

#[test]
fn attempts_and_rate_limit_displays_preserve_default_clamp_reset_and_hint_rules() {
    for (raw, expected) in [("", 5), ("bad", 5), ("0", 1), ("-10", 1), (" 7 ", 7)] {
        assert_eq!(max_attempts(raw), expected);
    }
    for language in ["en", "ja"] {
        for (at, hint) in [
            ("2027-01-01T00:00:00Z", "hint"),
            ("2027-01-01T00:00:00", "hint"),
            ("bad", ""),
            ("", "hint"),
        ] {
            let notice = rate_limit_notice(language, at, hint, false);
            assert!(!notice.starts_with("commands."));
            assert_eq!(
                notice.contains("hint"),
                !at.ends_with('Z') && !hint.is_empty()
            );
        }
    }
}

#[test]
fn checks_wait_detection_uses_evidence_type_and_exact_target_url() {
    let url = "local://code/acme/repo/pulls/1";
    assert!(awaits(
        &[json!({"type":"checks_awaited","payload":{"target":{"url":url}}})],
        url
    ));
    for item in [
        json!({"type":"pr_comment","payload":{"target":{"url":url}}}),
        json!({"type":"checks_awaited","payload":{"text":url}}),
        json!({"type":"checks_awaited","payload":{"target":{"url":format!("{url}0")}}}),
    ] {
        assert!(!awaits(&[item], url));
    }
}

#[tokio::test]
async fn next_takes_only_the_first_refreshed_item_and_idle_selection_creates_no_run_or_trace() {
    let f = Fixture::new();
    let selector = f.selector("manual");
    assert!(selector.next().await.unwrap().is_none());
    assert!(!f.root.path().join(".guildbotics/local/run").exists());
    assert!(f.runner.calls.lock().unwrap().is_empty());
    let first = f.task().await;
    let _ = f.task().await;
    let (task, invocation) = selector.next().await.unwrap().unwrap();
    assert_eq!(task.url, first.url);
    assert_eq!(
        invocation.payload["ticket_url"],
        first.url.as_deref().unwrap()
    );
    assert!(invocation.run_id.is_empty());
    let trace = f.trace("manual");
    selector.run(&task, invocation, &trace, 5).await.unwrap();
    let candidates = f.ticket.candidates().await.unwrap();
    assert_eq!(candidates.len(), 2);
    assert_eq!(
        candidates
            .iter()
            .filter(|t| t.status == "in_progress")
            .count(),
        1
    );
    assert_eq!(candidates.iter().filter(|t| t.status == "ready").count(), 1);
    assert_eq!(f.runner.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn candidates_put_pull_requests_first_and_refresh_each_from_its_own_service() {
    let f = Fixture::new();
    let ticket = f.task().await;
    let pull = f.pull().await;
    let selector = f.selector("routine");
    let candidates = selector.candidates().await.unwrap();
    assert_eq!(
        candidates.iter().map(|t| t.url.clone()).collect::<Vec<_>>(),
        vec![pull.url.clone(), ticket.url.clone()]
    );
    for (task, pr) in [(&pull, true), (&ticket, false)] {
        let invocation = selector.refresh(task).await.unwrap().unwrap();
        assert_eq!(
            invocation.payload["ticket_url"],
            task.url.as_deref().unwrap()
        );
        assert_eq!(
            invocation.payload["pull_request_url"],
            if pr { task.url.as_deref().unwrap() } else { "" }
        );
    }
}

#[tokio::test]
async fn failed_pull_request_runs_report_to_code_host_and_leave_the_ticket_untouched() {
    let f = Fixture::new();
    let ticket = f.task().await;
    let pull = f.pull().await;
    *f.runner.failure.lock().unwrap() = Some(guest("timeout"));
    let selector = f.selector("routine");
    let trace = f.trace("routine");
    assert!(
        selector
            .run(&pull, selector.invocation(&pull).unwrap(), &trace, 5)
            .await
            .is_err()
    );
    assert!(f.comments(&ticket).await.is_empty());
    let path = f.root.path().join("services/code/acme%2Frepo/pulls/1.json");
    let stored: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert_eq!(stored["comments"].as_array().unwrap().len(), 2);
    assert_eq!(stored["comments"][1]["status"]["reason"], "failed");
    assert_eq!(
        stored["comments"][1]["status"]["subject"],
        pull.url.as_deref().unwrap()
    );
    let stops = f.runner.stops.lock().unwrap();
    assert_eq!(stops.len(), 1);
    assert_eq!(stops[0].url, pull.url.as_deref().unwrap());
}

#[tokio::test]
async fn resumed_checks_restore_work_summary_and_settle_only_when_not_awaited_again() {
    for (trigger, resumes, completion, awaits, settled) in [
        ("pull_request_checks", true, "done", 0, true),
        ("pull_request_checks", true, "asking", 0, true),
        ("pull_request_checks", true, "awaiting_checks", 1, false),
        ("pull_request_checks", true, "awaiting_checks", 2, false),
        ("pull_request_checks", true, "awaiting_checks", 3, true),
        ("pull_request_feedback", true, "done", 0, true),
        ("pull_request_feedback", false, "done", 0, false),
    ] {
        let f = Fixture::new();
        let mut pull = f.pull().await;
        pull.trigger_reason = Some(trigger.into());
        pull.resumes = resumes.then(|| AwaitedRun {
            run_id: "old".into(),
            work_kind: "chat".into(),
            work_identity: "local:aiko:C1:T1".into(),
        });
        let selector = f.selector("routine");
        let missing = selector.invocation(&pull).unwrap();
        assert_eq!(missing.payload["awaited_summary"], "");
        f.record("old", vec![], "Report in the original chat thread.");
        let invocation = selector.invocation(&pull).unwrap();
        if resumes {
            assert_eq!(invocation.work.as_ref().unwrap().kind, "chat");
            assert_eq!(
                invocation.work.as_ref().unwrap().identity,
                "local:aiko:C1:T1"
            );
            assert_eq!(
                invocation.payload["awaited_summary"],
                "Report in the original chat thread."
            );
        } else {
            assert_eq!(invocation.payload["awaited_summary"], "");
        }
        let trace = f.trace("routine");
        let mut evidence = vec![];
        if awaits == 1 || awaits == 2 {
            evidence.push(json!({"type":"checks_awaited","payload":{"target":{"url":pull.url}}}));
        }
        if awaits == 2 || awaits == 3 {
            evidence.push(json!({"type":"checks_awaited","payload":{"target":{"url":"local://code/acme/other/pulls/9"}}}));
        }
        f.record(trace.id(), evidence, "");
        f.runner.records.lock().unwrap().last_mut().unwrap().result = Some(
            serde_json::from_value(
                json!({"subject_type":"ticket","subject_id":pull.url,"status":completion}),
            )
            .unwrap(),
        );
        selector.run(&pull, invocation, &trace, 5).await.unwrap();
        let evidence = f.runner.evidence.lock().unwrap();
        assert_eq!(evidence.len(), usize::from(resumes));
        if resumes {
            assert_eq!(evidence[0]["type"], "checks_resumed");
            assert_eq!(
                evidence[0]["payload"]["target"]["url"],
                pull.url.as_deref().unwrap()
            );
        }
        let stored: Value = serde_json::from_slice(
            &std::fs::read(f.root.path().join("services/code/acme%2Frepo/pulls/1.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            stored["comments"].as_array().unwrap().len(),
            if settled { 2 } else { 1 }
        );
        if settled {
            assert_eq!(stored["comments"][1]["status"]["reason"], "checks_settled");
        }
    }
}

#[tokio::test]
async fn review_limit_conversion_elects_one_notice_in_its_own_trace_without_an_agent_run() {
    let f = Fixture::new();
    let mut pull = f.pull().await;
    pull.trigger_reason = Some(REVIEW_LIMIT.into());
    let selector = f.selector("routine");
    selector.hand_over(&pull).await;
    selector.hand_over(&pull).await;
    let stored: Value = serde_json::from_slice(
        &std::fs::read(f.root.path().join("services/code/acme%2Frepo/pulls/1.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(stored["draft"], true);
    assert_eq!(stored["comments"].as_array().unwrap().len(), 2);
    assert_eq!(stored["comments"][1]["status"]["reason"], "review_limit");
    assert!(f.runner.calls.lock().unwrap().is_empty());
    let stops = f.runner.stops.lock().unwrap();
    assert_eq!(stops.len(), 1);
    assert_eq!(stops[0].reason, "review_limit");
    assert!(
        gb_observability::runs::RunStore::open(f.root.path())
            .records()
            .unwrap()
            .is_empty()
    );
}

struct CodeFaults {
    yield_result: Option<Result<usize, ServiceError>>,
    yield_calls: Mutex<Vec<String>>,
    runner_calls: Option<Arc<Mutex<Vec<WorkflowInvocation>>>>,

    candidates: Option<Vec<Task>>,
    refresh_limit: bool,
    block: Option<(&'static str, tokio_util::sync::CancellationToken)>,
    inner: Arc<dyn CodeHostingService>,
    draft: Option<ServiceError>,
    fail_comment: bool,
    fail_links: bool,
    link_override: Option<Vec<TicketLink>>,
    quota_error: Option<ServiceError>,
}
impl CodeFaults {
    fn new(inner: Arc<dyn CodeHostingService>) -> Self {
        Self {
            inner,
            yield_result: None,
            yield_calls: Mutex::default(),
            runner_calls: None,
            candidates: None,
            refresh_limit: false,
            block: None,
            draft: None,
            fail_comment: false,
            fail_links: false,
            link_override: None,
            quota_error: None,
        }
    }
}
impl CodeHostingService for CodeFaults {
    fn yield_pull_request_checks<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, usize> {
        Box::pin(async move {
            if let Some(calls) = &self.runner_calls {
                assert!(calls.lock().unwrap().is_empty());
            }
            self.yield_calls.lock().unwrap().push(url.into());
            self.yield_result
                .clone()
                .unwrap_or(Err(ServiceError::Unsupported))
        })
    }

    fn linked_tickets<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, Vec<TicketLink>> {
        if let Some(links) = &self.link_override {
            return Box::pin(async move { Ok(links.clone()) });
        }
        if self.fail_links {
            Box::pin(async { Err(ServiceError::Unavailable) })
        } else {
            self.inner.linked_tickets(url)
        }
    }
    fn read<'a>(
        &'a self,
        resource: Resource,
        subject: &'a str,
        parameters: Option<&'a Value>,
        continuation: &'a str,
    ) -> ServiceFuture<'a, ReadPage> {
        self.inner.read(resource, subject, parameters, continuation)
    }
    fn identity(&self, check: bool) -> ServiceFuture<'_, BTreeMap<String, String>> {
        self.inner.identity(check)
    }
    fn comment<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        if let Some(("comment", entered)) = &self.block {
            Box::pin(async move {
                entered.cancel();
                std::future::pending().await
            })
        } else if self.fail_comment {
            Box::pin(async { Err(ServiceError::Failed) })
        } else {
            self.inner.comment(url, body, status)
        }
    }
    fn create_pull_request(
        &self,
        request: CreatePullRequest,
    ) -> ServiceFuture<'_, PullRequestCreated> {
        self.inner.create_pull_request(request)
    }
    fn update_pull_request<'a>(
        &'a self,
        url: &'a str,
        request: UpdatePullRequest,
    ) -> ServiceFuture<'a, PullRequestUpdated> {
        self.inner.update_pull_request(url, request)
    }
    fn review<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        verdict: ReviewVerdict,
        head: &'a str,
    ) -> ServiceFuture<'a, ReviewSubmitted> {
        self.inner.review(url, body, verdict, head)
    }
    fn review_comment<'a>(
        &'a self,
        url: &'a str,
        request: ReviewComment,
    ) -> ServiceFuture<'a, ReviewCommentWritten> {
        self.inner.review_comment(url, request)
    }
    fn reply<'a>(
        &'a self,
        url: &'a str,
        reply_to: &'a str,
        body: &'a str,
    ) -> ServiceFuture<'a, ReplyWritten> {
        self.inner.reply(url, reply_to, body)
    }
    fn add_reaction<'a>(
        &'a self,
        url: &'a str,
        target_id: &'a str,
        reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded> {
        self.inner.add_reaction(url, target_id, reaction)
    }
    fn readiness<'a>(
        &'a self,
        url: &'a str,
        failed_logs: bool,
        log_tail_bytes: usize,
    ) -> ServiceFuture<'a, Readiness> {
        self.inner.readiness(url, failed_logs, log_tail_bytes)
    }
    fn open_pull_requests<'a>(
        &'a self,
        repository: &'a str,
        branch: &'a str,
    ) -> ServiceFuture<'a, Vec<String>> {
        self.inner.open_pull_requests(repository, branch)
    }
    fn artifact<'a>(
        &'a self,
        url: &'a str,
        name: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<ArtifactRef>> {
        self.inner.artifact(url, name, limit)
    }
    fn clone_url<'a>(&'a self, repository: &'a str) -> ServiceFuture<'a, String> {
        self.inner.clone_url(repository)
    }
    fn default_branch<'a>(&'a self, repository: &'a str) -> ServiceFuture<'a, String> {
        self.inner.default_branch(repository)
    }
    fn pull_request_head<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, PullRequestHead> {
        self.inner.pull_request_head(url)
    }
    fn git_credential(&self) -> ServiceFuture<'_, Option<GitCredential>> {
        self.inner.git_credential()
    }
    fn check_push(&self, repository: &str) -> Result<(), ServiceError> {
        self.inner.check_push(repository)
    }
    fn commit_url(&self, repository: &str, sha: &str) -> String {
        self.inner.commit_url(repository, sha)
    }
    fn repository_from_remote(&self, remote_url: &str) -> Option<String> {
        self.inner.repository_from_remote(remote_url)
    }
    fn authored_open_pull_requests(&self) -> ServiceFuture<'_, Vec<String>> {
        if let Some(error) = &self.quota_error {
            Box::pin(async move { Err(error.clone()) })
        } else {
            self.inner.authored_open_pull_requests()
        }
    }
    fn pull_request_candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        if let Some(tasks) = &self.candidates {
            Box::pin(async { Ok(tasks.clone()) })
        } else {
            self.inner.pull_request_candidates()
        }
    }
    fn refresh_pull_request<'a>(&'a self, task: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        if self.refresh_limit {
            Box::pin(async move {
                let mut task = task.clone();
                task.trigger_reason = Some(REVIEW_LIMIT.into());
                Ok(Some(task))
            })
        } else {
            self.inner.refresh_pull_request(task)
        }
    }
    fn convert_pull_request_to_draft<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool> {
        if let Some(("convert", entered)) = &self.block {
            Box::pin(async move {
                entered.cancel();
                std::future::pending().await
            })
        } else if let Some(error) = &self.draft {
            Box::pin(async move { Err(error.clone()) })
        } else {
            self.inner.convert_pull_request_to_draft(url)
        }
    }
    fn can_receive_work<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool> {
        self.inner.can_receive_work(url)
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        self.inner.aclose()
    }
}

#[tokio::test]
async fn review_limit_conversion_failures_notify_but_unavailability_defers_and_retries_without_an_agent()
 {
    for error in [
        ServiceError::Failed,
        ServiceError::NotFound,
        ServiceError::Unsupported,
        ServiceError::Unavailable,
    ] {
        let f = Fixture::new();
        let mut pull = f.pull().await;
        pull.trigger_reason = Some(REVIEW_LIMIT.into());
        let mut faults = CodeFaults::new(f.code.clone());
        faults.draft = Some(error.clone());
        let mut selector = f.selector("manual");
        selector.code = Some(&faults);
        selector.hand_over(&pull).await;
        let path = f.root.path().join("services/code/acme%2Frepo/pulls/1.json");
        let stored: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(stored["draft"], false);
        assert!(f.runner.calls.lock().unwrap().is_empty());
        assert!(f.runner.activity.lock().unwrap().is_empty());
        let boundaries = std::fs::read_dir(f.root.path().join(".guildbotics/local/run/sessions"))
            .unwrap()
            .flat_map(|file| {
                std::fs::read_to_string(file.unwrap().path())
                    .unwrap()
                    .lines()
                    .map(|line| serde_json::from_str::<Value>(line).unwrap())
                    .collect::<Vec<_>>()
            })
            .filter(|e| {
                e["type"]
                    .as_str()
                    .is_some_and(|t| t.starts_with("review_limit."))
            })
            .collect::<Vec<_>>();
        assert_eq!(boundaries.first().unwrap()["type"], "review_limit.started");
        assert_eq!(
            boundaries.last().unwrap()["type"],
            if error.is_unavailable() {
                "review_limit.deferred"
            } else {
                "review_limit.failed"
            }
        );
        if error == ServiceError::Unavailable {
            assert_eq!(stored["comments"].as_array().unwrap().len(), 1);
            assert!(f.runner.stops.lock().unwrap().is_empty());
            f.selector("manual").hand_over(&pull).await;
            assert_eq!(f.runner.stops.lock().unwrap()[0].reason, "review_limit");
        } else {
            assert_eq!(stored["comments"].as_array().unwrap().len(), 2);
            assert_eq!(stored["comments"][1]["status"]["reason"], "failed");
            assert_eq!(f.runner.stops.lock().unwrap()[0].reason, "failed");
        }
    }
}
#[tokio::test]
async fn review_limit_failed_pr_notice_still_records_and_notifies_with_the_pr_address() {
    let f = Fixture::new();
    let mut pull = f.pull().await;
    pull.trigger_reason = Some(REVIEW_LIMIT.into());
    let mut faults = CodeFaults::new(f.code.clone());
    faults.fail_comment = true;
    let mut selector = f.selector("routine");
    selector.code = Some(&faults);
    selector.hand_over(&pull).await;
    let activity = f.runner.activity.lock().unwrap();
    assert_eq!(activity.len(), 1);
    assert_eq!(activity[0]["type"], "pull_request.review_limit");
    assert_eq!(activity[0]["payload"]["comment_error"], "ServiceError");
    let stops = f.runner.stops.lock().unwrap();
    assert_eq!(stops.len(), 1);
    assert_eq!(stops[0].reason, "review_limit");
    assert_eq!(stops[0].url, pull.pull_request_url.unwrap());
    assert!(stops[0].comment_url.is_empty());
}

#[tokio::test]
async fn cancelled_review_limit_conversion_and_comment_close_the_trace_and_record_any_completed_draft()
 {
    for phase in ["convert", "comment"] {
        let f = Fixture::new();
        let mut pull = f.pull().await;
        pull.trigger_reason = Some(REVIEW_LIMIT.into());
        let entered = tokio_util::sync::CancellationToken::new();
        let mut faults = CodeFaults::new(f.code.clone());
        faults.block = Some((phase, entered.clone()));
        let mut selector = f.selector("manual");
        selector.code = Some(&faults);
        let mut run = Box::pin(selector.hand_over(&pull));
        tokio::select! {()=entered.cancelled()=>(),()=&mut run=>panic!("operation did not wait")}
        drop(run);
        let activity = f.runner.activity.lock().unwrap();
        assert_eq!(activity.len(), usize::from(phase == "comment"));
        if phase == "comment" {
            assert_eq!(activity[0]["payload"]["comment_error"], "CancelledError");
        }
        let index = std::fs::read_to_string(
            f.root
                .path()
                .join(".guildbotics/local/run/diagnostics.jsonl"),
        )
        .unwrap();
        let pointer = index
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .find(|e| e["type"] == "session.pointer")
            .unwrap();
        let events = std::fs::read_to_string(
            f.root
                .path()
                .join(".guildbotics/local/run/sessions")
                .join(format!("{}.jsonl", pointer["trace_id"].as_str().unwrap())),
        )
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|e| {
            e["type"].as_str().is_some_and(|kind| {
                kind.starts_with("review_limit.") || kind == "pull_request.review_limit"
            })
        })
        .collect::<Vec<_>>();
        assert_eq!(events.first().unwrap()["type"], "review_limit.started");
        assert_eq!(events.last().unwrap()["type"], "review_limit.failed");
        assert_eq!(events.last().unwrap()["payload"]["error"], "CancelledError");
        for event in &events {
            assert_eq!(event["command"], "pull_request_review_limit");
            if event["type"] != "pull_request.review_limit" {
                assert_eq!(event["payload"]["run_id"], event["trace_id"]);
                assert_eq!(event["payload"]["count"], 3);
                assert_eq!(
                    event["payload"]["pull_request"]["url"],
                    pull.pull_request_url.clone().unwrap()
                );
                assert_eq!(event["payload"]["pull_request"]["title"], pull.title);
            }
        }
        assert!(
            events
                .iter()
                .all(|e| e["trace_id"] == events[0]["trace_id"])
        );
        assert!(f.runner.calls.lock().unwrap().is_empty());
        assert!(
            gb_observability::runs::RunStore::open(f.root.path())
                .records()
                .unwrap()
                .is_empty()
        );
    }
}

#[tokio::test]
async fn author_feedback_uses_the_only_open_owned_ticket_and_preserves_resumes() {
    for count in [0, 1, 2] {
        let f = Fixture::new();
        let task = f.task().await;
        let second = f
            .ticket
            .create_ticket(CreateTicket {
                tracker: "acme/team".into(),
                title: "Second".into(),
                body: String::new(),
                labels: vec![],
                add_to_board: true,
                interactive: false,
                assignee: Some(ActorName("local-aiko".into())),
                lane: Some(CreateLane::Ready),
                parent: None,
                blocked_by: vec![],
            })
            .await
            .unwrap();
        let pull = f.pull().await;
        let url = pull.url.as_deref().unwrap();
        let urls = [task.url.as_deref().unwrap(), second.target.url.as_str()];
        f.code
            .update_pull_request(
                url,
                UpdatePullRequest {
                    tickets: Some(
                        urls[..count]
                            .iter()
                            .map(|u| TicketLink {
                                url: (*u).into(),
                                closes: false,
                            })
                            .collect(),
                    ),
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        let selector = f.selector("routine");
        let selected = selector.refresh(&pull).await.unwrap().unwrap();
        let work = selected.work.unwrap();
        assert_eq!(
            work.kind,
            if count == 1 { "ticket" } else { "pull_request" }
        );
        assert_eq!(work.identity, if count == 1 { urls[0] } else { url });
        if count == 1 {
            assert_eq!(Some(work), selector.invocation(&task).unwrap().work);
        }
    }
}

#[tokio::test]
async fn unreadable_author_links_keep_pr_work_selectable() {
    for missing_ticket in [false, true] {
        let f = Fixture::new();
        let pull = f.pull().await;
        let mut faulty = CodeFaults::new(f.code.clone());
        faulty.fail_links = !missing_ticket;
        if missing_ticket {
            faulty.link_override = Some(vec![TicketLink {
                url: "local://ticket/acme/team/999999".into(),
                closes: false,
            }]);
        }
        let mut selector = f.selector("routine");
        selector.code = Some(&faulty);
        let selected = selector.next().await.unwrap().unwrap().1;
        assert_eq!(selected.work.unwrap().identity, pull.url.unwrap());
    }
}

#[tokio::test]
async fn open_pr_limit_is_rechecked_before_moving_ready_work_and_does_not_stop_other_work() {
    for source in ["routine", "scheduled", "manual"] {
        let f = Fixture::new();
        let quota = PullRequestQuota::new(2);
        let mut selector = f.selector(source);
        selector.quota = Some(&quota);
        let first = f.task().await;
        let second = f.task().await;
        let third = f.task().await;
        let candidates = selector.candidates().await.unwrap();
        assert_eq!(quota.snapshot().unwrap().count, 0);
        assert!(candidates.iter().any(|t| t.url == third.url));
        f.pull_branch("one").await;
        f.pull_branch("two").await;
        assert_eq!(
            selector.refresh(&third).await.unwrap().is_none(),
            source != "manual"
        );
        // PR feedback still wins selection while ready tickets are deferred.
        assert!(
            selector
                .next()
                .await
                .unwrap()
                .unwrap()
                .0
                .pull_request_url
                .is_some()
        );
        selector
            .run(
                &third,
                selector.invocation(&third).unwrap(),
                &f.trace(source),
                5,
            )
            .await
            .unwrap();
        assert_eq!(
            f.runner.calls.lock().unwrap().len(),
            usize::from(source == "manual")
        );
        let detail = f
            .ticket
            .read(Resource::Tickets, third.url.as_deref().unwrap(), None, "")
            .await
            .unwrap();
        assert_eq!(
            detail.items()[0]["lane"],
            if source == "manual" {
                "in_progress"
            } else {
                "ready"
            }
        );
        if source != "manual" {
            assert!(quota.snapshot().unwrap().deferred);
            for reason in [
                "working_lane",
                "issue_comment",
                "issue_mention",
                "pull_request_feedback",
                "pull_request_review",
                "pull_request_checks",
            ] {
                let mut work = first.clone();
                work.status = "in_progress".into();
                work.trigger_reason = Some(reason.into());
                selector
                    .run(
                        &work,
                        selector.invocation(&work).unwrap(),
                        &f.trace(source),
                        5,
                    )
                    .await
                    .unwrap();
            }
            assert_eq!(f.runner.calls.lock().unwrap().len(), 6);
            let paths = f.root.path().join("services/code/acme%2Frepo/pulls");
            let path = std::fs::read_dir(paths)
                .unwrap()
                .map(|entry| entry.unwrap().path())
                .find(|path| path.extension().is_some_and(|ext| ext == "json"))
                .unwrap();
            let mut pull: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
            pull["state"] = json!("closed");
            std::fs::write(path, serde_json::to_vec(&pull).unwrap()).unwrap();
            selector
                .run(
                    &second,
                    selector.invocation(&second).unwrap(),
                    &f.trace(source),
                    5,
                )
                .await
                .unwrap();
            assert_eq!(f.runner.calls.lock().unwrap().len(), 7);
            assert_eq!(quota.snapshot().unwrap().count, 1);
        }
    }
}

#[tokio::test]
async fn quota_read_errors_defer_only_new_ready_work_and_clear_the_display() {
    for source in ["routine", "scheduled", "manual"] {
        for error in [
            ServiceError::NotFound,
            ServiceError::Failed,
            ServiceError::Unavailable,
        ] {
            let f = Fixture::new();
            let ready = f.task().await;
            let pull = f.pull().await;
            let quota = PullRequestQuota::new(2);
            let mut selector = f.selector(source);
            selector.quota = Some(&quota);
            selector.candidates().await.unwrap();
            assert!(quota.snapshot().is_some());
            let mut faults = CodeFaults::new(f.code.clone());
            faults.quota_error = Some(error);
            selector.code = Some(&faults);
            assert!(!selector.candidates().await.unwrap().is_empty());
            assert!(quota.snapshot().is_none());
            assert_eq!(selector.next().await.unwrap().unwrap().0.url, pull.url);
            assert_eq!(
                selector.refresh(&ready).await.unwrap().is_none(),
                source != "manual"
            );
            selector
                .run(
                    &ready,
                    selector.invocation(&ready).unwrap(),
                    &f.trace(source),
                    5,
                )
                .await
                .unwrap();
            assert_eq!(
                f.runner.calls.lock().unwrap().len(),
                usize::from(source == "manual")
            );
            for reason in [
                "working_lane",
                "issue_comment",
                "issue_mention",
                "pull_request_feedback",
                "pull_request_review",
                "pull_request_checks",
            ] {
                let mut task = ready.clone();
                task.status = "in_progress".into();
                task.trigger_reason = Some(reason.into());
                selector
                    .run(
                        &task,
                        selector.invocation(&task).unwrap(),
                        &f.trace(source),
                        5,
                    )
                    .await
                    .unwrap();
            }
            assert_eq!(
                f.runner.calls.lock().unwrap().len(),
                6 + usize::from(source == "manual")
            );
            let working = f.task().await;
            f.ticket
                .move_ticket(&working, MoveLane::InProgress)
                .await
                .unwrap();
            let mut working = working;
            working.status = "in_progress".into();
            let mut working_faults = CodeFaults::new(f.code.clone());
            working_faults.quota_error = faults.quota_error.clone();
            working_faults.candidates = Some(vec![ready.clone(), working.clone()]);
            selector.code = Some(&working_faults);
            if source != "manual" {
                assert_eq!(selector.next().await.unwrap().unwrap().0.url, working.url);
            }
            selector.code = Some(f.code.as_ref());
            selector.candidates().await.unwrap();
            assert!(quota.snapshot().is_some());
        }
    }
}

#[tokio::test]
async fn feedback_yields_once_before_author_runs_and_failures_are_best_effort() {
    for trigger in [
        "pull_request_feedback",
        "pull_request_checks",
        "ready_lane",
        "pull_request_review",
        "issue_mention",
    ] {
        for resumes in [false, true] {
            for result in [
                Ok(2),
                Ok(0),
                Err(ServiceError::Unsupported),
                Err(ServiceError::Unavailable),
            ] {
                for fail_evidence in [false, true] {
                    let mut f = Fixture::new();
                    f.runner.fail_evidence = fail_evidence;
                    let mut task = f.pull().await;
                    task.trigger_reason = Some(trigger.into());
                    task.resumes = resumes.then(|| AwaitedRun {
                        run_id: "old".into(),
                        work_kind: "pull_request".into(),
                        work_identity: "pull".into(),
                    });
                    let mut code = CodeFaults::new(f.code.clone());
                    code.yield_result = Some(result.clone());
                    code.runner_calls = Some(f.runner.calls.clone());
                    let mut selector = f.selector("routine");
                    selector.code = Some(&code);
                    let trace = f.trace("routine");
                    selector
                        .run(&task, selector.invocation(&task).unwrap(), &trace, 5)
                        .await
                        .unwrap();
                    assert_eq!(f.runner.calls.lock().unwrap().len(), 1);
                    let feedback = trigger == "pull_request_feedback";
                    assert_eq!(
                        code.yield_calls.lock().unwrap().as_slice(),
                        if feedback {
                            vec![task.pull_request_url.clone().unwrap()]
                        } else {
                            vec![]
                        }
                    );
                    let evidence = f.runner.evidence.lock().unwrap();
                    let yielded = evidence
                        .iter()
                        .filter(|e| e["type"] == "checks_yielded")
                        .collect::<Vec<_>>();
                    let failed = evidence
                        .iter()
                        .filter(|e| e["type"] == "checks_yield_failed")
                        .count();
                    assert_eq!(yielded.len(), usize::from(feedback && result == Ok(2)));
                    assert_eq!(
                        failed,
                        usize::from(feedback && result == Err(ServiceError::Unavailable))
                    );
                    if let Some(fact) = yielded.first() {
                        assert_eq!(fact["payload"]["count"], 2);
                        assert_eq!(
                            fact["payload"]["target"]["url"],
                            task.pull_request_url.as_deref().unwrap()
                        );
                    }
                }
            }
        }
    }
}
