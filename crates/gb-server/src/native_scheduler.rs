//! Production adapter for the scheduler's three input sources.
use crate::{
    service::{Outcome, Pending},
    ticket_selector::Selector,
    workflow_runtime::{Refused, Runtime},
};
use branvil::agent::port::Task;
use gb_base::service::ServiceError;
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::Arc,
};
use tokio::sync::Mutex;

pub struct Engine {
    pub runtime: Arc<Runtime>,
    pub chat: Option<crate::chat_dispatch::Dispatcher>,
    cycle: Mutex<Cycle>,
    environment: std::sync::Mutex<Option<String>>,
}

impl Engine {
    pub fn new(runtime: Arc<Runtime>, chat: Option<crate::chat_dispatch::Dispatcher>) -> Self {
        Self {
            runtime,
            chat,
            cycle: Mutex::new(Cycle::default()),
            environment: std::sync::Mutex::new(None),
        }
    }
    fn outcome(result: Result<gb_wire::CommandReply, Refused>, source: &str) -> Outcome {
        match result {
            Ok(_) | Err(Refused::Admission(crate::execution::Rejected::Duplicate)) => {
                Outcome::Succeeded
            }
            Err(Refused::Admission(
                crate::execution::Rejected::Store | crate::execution::Rejected::InvalidIdentity,
            )) => Outcome::Failed,
            Err(Refused::Admission(_)) => Outcome::Deferred,
            Err(Refused::Dispatch(crate::dispatcher::DispatchFailure::Cancelled)) => {
                Outcome::Deferred
            }
            Err(Refused::Dispatch(error)) if error.deferred(source) => Outcome::Deferred,
            Err(_) => Outcome::Failed,
        }
    }
    fn environment_ready(&self, reason: Option<String>) -> bool {
        let mut previous = self.environment.lock().unwrap();
        if *previous != reason {
            *previous = reason.clone();
            let message = environment_message(&self.runtime.language, reason.as_deref());
            if reason.is_some() {
                tracing::warn!("{message}");
            } else {
                tracing::info!("{message}");
            }
        }
        reason.is_none()
    }
}

/// Keep only work already attempted, never a saved ordering of future work.
#[derive(Default)]
struct Cycle {
    attempted: BTreeSet<Option<String>>,
}
impl Cycle {
    async fn patrol<F, Fut>(
        &mut self,
        selector: &Selector<'_>,
        run: F,
    ) -> (Result<Outcome, ServiceError>, bool)
    where
        F: FnOnce(Task, gb_wire::WorkflowInvocation) -> Fut,
        Fut: std::future::Future<Output = Outcome>,
    {
        let mut tasks: VecDeque<_> = match selector.candidates().await {
            Ok(tasks) => tasks
                .into_iter()
                .filter(|task| !self.attempted.contains(&task.url))
                .collect(),
            Err(error) => {
                self.attempted.clear();
                return (Err(error), true);
            }
        };
        while let Some(candidate) = tasks.pop_front() {
            let mut repair = candidate.repair;
            let result = match selector.refresh(&candidate).await {
                Ok(None) => continue,
                Ok(Some(invocation)) => {
                    let task: Task = serde_json::from_value(invocation.payload["task"].clone())
                        .expect("selector serialized a validated refreshed task");
                    repair = task.repair;
                    Ok(run(task, invocation).await)
                }
                Err(error) => Err(error),
            };
            let deferred = matches!(&result, Ok(Outcome::Deferred))
                || result.as_ref().is_err_and(|error| error.is_unavailable());
            if !deferred {
                self.attempted.insert(candidate.url);
            }
            let exhausted = !deferred && tasks.is_empty();
            if exhausted {
                self.attempted.clear();
            }
            return (
                result.map(|outcome| repair_outcome(outcome, repair)),
                exhausted,
            );
        }
        self.attempted.clear();
        (Ok(Outcome::Succeeded), true)
    }
}
fn repair_outcome(outcome: Outcome, repair: bool) -> Outcome {
    if repair && outcome == Outcome::Failed {
        Outcome::Deferred
    } else {
        outcome
    }
}

fn environment_message(language: &str, reason: Option<&str>) -> String {
    crate::i18n::t(
        language,
        if reason.is_some() {
            "drivers.task_scheduler.environment_deferred"
        } else {
            "drivers.task_scheduler.environment_resumed"
        },
        &[("reason", reason.unwrap_or_default())],
    )
}

impl crate::scheduler::Engine for Engine {
    fn available(&self) -> Pending<'_, bool> {
        Box::pin(async {
            if self.runtime.draining.is_cancelled() {
                return false;
            }
            let refusal = gb_environment::device::read(
                &self.runtime.dispatcher.host,
                &self.runtime.dispatcher.workspace,
            )
            .await
            .refusal
            .map(|s| crate::commands::say(s, &self.runtime.language));
            self.environment_ready(refusal)
        })
    }
    fn command<'a>(
        &'a self,
        line: &'a str,
        source: &'a str,
        slot: BTreeMap<String, String>,
    ) -> Pending<'a, Outcome> {
        Box::pin(
            async move { Self::outcome(self.runtime.command(line, source, slot).await, source) },
        )
    }
    fn patrol(&self, slot: BTreeMap<String, String>) -> Pending<'_, (Outcome, bool)> {
        Box::pin(async move {
            let Some(tickets) = self.runtime.tickets.as_deref() else {
                return (Outcome::Deferred, true);
            };
            let diagnostics = self.runtime.diagnostics();
            let selector = Selector {
                roster: &self.runtime.roster,
                diagnostics: &diagnostics,
                person: &self.runtime.person.person_id,
                source: "routine",
                language: &self.runtime.language,
                ticket: tickets,
                code: self.runtime.code.as_deref(),
                runner: self.runtime.as_ref(),
                quota: self.runtime.quota.as_deref(),
            };
            let mut cycle = self.cycle.lock().await;
            let (result, exhausted) = cycle
                .patrol(&selector, |task, invocation| async move {
                    Self::outcome(
                        self.runtime
                            .ticket(
                                &task,
                                invocation,
                                slot,
                                crate::ticket_selector::max_attempts(
                                    &std::env::var("GUILDBOTICS_TICKET_MAX_ATTEMPTS")
                                        .unwrap_or_default(),
                                ),
                            )
                            .await,
                        "routine",
                    )
                })
                .await;
            let outcome = match result {
                Ok(outcome) => outcome,
                Err(error) => {
                    self.runtime
                        .selection_failed(error.clone(), "routine")
                        .await;
                    if error.is_unavailable() {
                        Outcome::Deferred
                    } else {
                        Outcome::Failed
                    }
                }
            };
            (outcome, exhausted)
        })
    }
    fn events(&self) -> Pending<'_, Outcome> {
        Box::pin(async {
            match &self.chat {
                Some(chat) => match chat.process(chrono::Utc::now()).await {
                    Ok(0) => Outcome::Deferred,
                    Ok(_) => Outcome::Succeeded,
                    Err(error) => {
                        tracing::warn!("chat processing failed: {error:#}");
                        Outcome::Failed
                    }
                },
                None => Outcome::Deferred,
            }
        })
    }
    fn failed(&self, errors: u8, limit: u8, source: &str) {
        self.runtime.diagnostics().record(serde_json::from_value(serde_json::json!({"kind":"event","type":"scheduler.worker.failed","trace_id":null,"source":"scheduler","person_id":self.runtime.person.person_id,
            "timestamp":chrono::Utc::now().to_rfc3339(),"attributes":{"service_run_id":self.runtime.service_run_id.as_deref().unwrap_or("")},
            "payload":{"source":source,"consecutive_errors":errors,"consecutive_error_limit":limit}})).expect("worker failure"));
        tracing::error!(
            person = self.runtime.person.person_id,
            errors,
            "member worker stopped after repeated failures"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn duplicate_is_consumed_while_an_unadmitted_slot_is_deferred() {
        use crate::execution::Rejected;
        for source in ["scheduled", "routine"] {
            for rejected in [
                Rejected::Duplicate,
                Rejected::LeaseUnavailable,
                Rejected::Draining,
            ] {
                assert_eq!(
                    Engine::outcome(Err(Refused::Admission(rejected)), source),
                    if rejected == Rejected::Duplicate {
                        Outcome::Succeeded
                    } else {
                        Outcome::Deferred
                    }
                );
            }
        }
    }
    #[test]
    fn environment_deferral_warns_once_per_changed_reason_then_announces_recovery() {
        let root = tempfile::tempdir().unwrap();
        let runtime = Arc::new(crate::test_chat::runtime(root.path()));
        let engine = Engine::new(runtime, None);
        let (_, messages) = crate::test_logs::capture(|| {
            assert!(engine.environment_ready(None));
            for reason in ["building", "filesystem"] {
                assert!(!engine.environment_ready(Some(reason.into())));
                assert!(!engine.environment_ready(Some(reason.into())));
            }
            assert!(engine.environment_ready(None));
            assert!(engine.environment_ready(None));
        });
        assert_eq!(
            messages,
            vec![
                "Work is deferred on this device: building",
                "Work is deferred on this device: filesystem",
                "The agent environment is ready; resuming."
            ]
        );
        for language in ["en", "ja"] {
            for reason in [None, Some("building")] {
                let message = environment_message(language, reason);
                assert!(!message.starts_with("drivers."));
                if reason.is_some() {
                    assert!(message.contains("building"));
                }
            }
        }
    }
}

#[cfg(test)]
mod repair_tests {
    use super::*;
    #[test]
    fn repeated_repair_failures_preserve_the_previous_worker_error_count() {
        let mut errors = 2;
        for _ in 0..5 {
            assert_eq!(repair_outcome(Outcome::Failed, true), Outcome::Deferred);
        }
        // The worker increments only Failed and resets only Succeeded.
        for outcome in [
            repair_outcome(Outcome::Failed, true),
            repair_outcome(Outcome::Failed, false),
        ] {
            match outcome {
                Outcome::Failed => errors += 1,
                Outcome::Succeeded => errors = 0,
                Outcome::Deferred => (),
            }
        }
        assert_eq!(errors, 3);
    }
}

#[cfg(test)]
mod cycle_tests {
    use super::*;
    use serde_json::json;

    /// A claimed check can precede the asynchronous creation of main's record.
    async fn current_verification(read: impl AsyncFn() -> serde_json::Value) -> serde_json::Value {
        tokio::time::timeout(std::time::Duration::from_secs(15), async {
            loop {
                let current = read().await;
                if !current.is_null() {
                    break current;
                }
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("the main verification record was never created")
    }

    #[tokio::test]
    async fn verification_reads_wait_for_record_creation() {
        // Control both reads used by repair: before finish and while awaiting retry.
        for number in [0, 1] {
            let (requests, mut reads) = tokio::sync::mpsc::unbounded_channel();
            let read = async || {
                let (send, receive) = tokio::sync::oneshot::channel();
                requests.send(send).unwrap();
                receive.await.unwrap()
            };
            let waiting = async {
                let current = current_verification(read).await;
                assert_eq!(current["retry"]["number"].as_u64().unwrap(), number);
            };
            let create = async {
                reads
                    .recv()
                    .await
                    .unwrap()
                    .send(serde_json::Value::Null)
                    .unwrap();
                // Creation stays withheld until the reader observes the absent record
                // and asks again, so an immediate read cannot accidentally pass.
                reads
                    .recv()
                    .await
                    .unwrap()
                    .send(json!({"retry":{"number":number}}))
                    .unwrap();
            };
            tokio::time::timeout(std::time::Duration::from_secs(5), async {
                tokio::join!(waiting, create);
            })
            .await
            .unwrap();
        }
    }

    struct Fixture {
        root: tempfile::TempDir,
        native: crate::member::native::Native,
        runtime: Runtime,
        /// Each ticket by the name a test gives it: its key and URL.
        tickets: std::sync::Mutex<BTreeMap<String, (String, String)>>,
    }
    impl Fixture {
        fn new() -> Self {
            let root = tempfile::tempdir().unwrap();
            let native = crate::member::native::tests::fixture(root.path());
            let runtime = crate::test_chat::runtime(root.path());
            Self {
                root,
                native,
                runtime,
                tickets: Default::default(),
            }
        }
        fn owner(&self) -> &branvil::api::MemberApi {
            &self.native.fixture.as_ref().unwrap().owner
        }
        fn agent(&self) -> &branvil::agent::TicketAgent {
            self.runtime.tickets.as_deref().unwrap()
        }
        /// Puts the named tickets assigned to aiko in this order on the
        /// ready lane, creating each one first named; the others leave it.
        async fn order(&self, names: &[&str]) {
            let owner = self.owner();
            for name in names {
                if self.tickets.lock().unwrap().contains_key(*name) {
                    continue;
                }
                let ticket = owner
                    .tickets_create_ticket(
                        "acme/demo",
                        branvil::test_support::input(json!({"title":name})),
                    )
                    .await
                    .unwrap();
                owner
                    .tickets_set_assignee("acme/demo", &ticket.key, Some("aiko".into()))
                    .await
                    .unwrap();
                self.tickets
                    .lock()
                    .unwrap()
                    .insert((*name).into(), (ticket.key, ticket.url));
            }
            let tickets = self.tickets.lock().unwrap().clone();
            for (name, (key, _)) in &tickets {
                if !names.contains(&name.as_str()) {
                    owner
                        .tickets_put_on_board(
                            "acme/demo",
                            key,
                            branvil::test_support::input(json!({"lane":"new"})),
                        )
                        .await
                        .unwrap();
                }
            }
            // Each card placed without a neighbour goes to the lane's end.
            for name in names {
                owner
                    .tickets_put_on_board(
                        "acme/demo",
                        &tickets[*name].0,
                        branvil::test_support::input(json!({"lane":"ready"})),
                    )
                    .await
                    .unwrap();
            }
        }
        /// A repair ticket of the demo repository, as Branvil opens one once
        /// the default branch's verification has failed: a runner fails each
        /// attempt of its check until the automatic retries are used up.
        async fn repair(&self) -> String {
            use crate::member::native::tests::git;
            let fixture = self.native.fixture.as_ref().unwrap();
            let owner = &fixture.owner;
            let seed = self.root.path().join("repair-seed");
            std::fs::create_dir_all(seed.join(".branvil")).unwrap();
            git(&seed, &["init", "-qb", "main"]);
            std::fs::write(
                seed.join(".branvil/ci.toml"),
                "[[jobs]]\nname = \"test\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\n",
            )
            .unwrap();
            git(&seed, &["add", "."]);
            git(&seed, &["commit", "-qm", "pipeline"]);
            let head = git(&seed, &["rev-parse", "HEAD"]);
            let bare = fixture.root.join("repos/acme/demo.git");
            git(
                &bare,
                &[
                    "fetch",
                    "-q",
                    "--update-head-ok",
                    seed.to_str().unwrap(),
                    "main:main",
                ],
            );
            owner
                .members_create(branvil::test_support::input(
                    json!({"name":"runner1","kind":"runner"}),
                ))
                .await
                .unwrap();
            let token = owner
                .members_issue_token(
                    "runner1".into(),
                    branvil::test_support::input(json!({"label":"fixture"})),
                )
                .await
                .unwrap()
                .token;
            // Queued without an event; its claim announces the new main.
            branvil::test_support::check(
                &fixture.data,
                &fixture.root,
                "acme/demo",
                "main",
                &head,
                None,
            )
            .await;
            let base = format!("{}/api/v1/runner", fixture.data.base_url());
            let client = reqwest::Client::builder()
                .no_proxy()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .unwrap();
            let claim = || async {
                tokio::time::timeout(std::time::Duration::from_secs(15), async {
                    loop {
                        let response = client
                            .post(format!("{base}/claim"))
                            .bearer_auth(&token)
                            .json(&json!({"executor":"microsandbox","executor_state":"ready","architecture":"arm64", "images":[{"reference":"docker.io/library/alpine:3.22","architecture":"arm64","digest":format!("sha256:{}","a".repeat(64))}]}))
                            .send()
                            .await
                            .unwrap();
                        if response.status() == reqwest::StatusCode::OK {
                            let claim: serde_json::Value = response.json().await.unwrap();
                            assert_eq!(claim["sha"], head);
                            break claim;
                        }
                        assert_eq!(response.status(), reqwest::StatusCode::NO_CONTENT);
                        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                    }
                })
                .await
                .expect("the main check was never queued")
            };
            let verification = || async {
                current_verification(async || {
                    serde_json::to_value(owner.repos_verification("acme/demo").await.unwrap())
                        .unwrap()["current"]
                        .clone()
                })
                .await
            };
            loop {
                let attempt = claim().await;
                let before = verification().await["retry"]["number"].as_u64().unwrap();
                client
                    .post(format!(
                        "{base}/attempts/{}/finish",
                        attempt["attempt_id"].as_str().unwrap()
                    ))
                    .bearer_auth(&token)
                    .json(&json!({"lease":attempt["lease"],"conclusion":"failure","image_digest":format!("sha256:{}","2".repeat(64))}))
                    .send()
                    .await
                    .unwrap()
                    .error_for_status()
                    .unwrap();
                let failed = tokio::time::timeout(std::time::Duration::from_secs(15), async {
                    loop {
                        let current = verification().await;
                        if current["state"] == "failed" {
                            break true;
                        }
                        if current["retry"]["number"].as_u64().unwrap() > before {
                            break false;
                        }
                        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                    }
                })
                .await
                .expect("verification neither failed nor retried");
                if failed {
                    break;
                }
            }
            let ticket = tokio::time::timeout(std::time::Duration::from_secs(15), async {
                loop {
                    let page = owner
                        .tickets_cards(
                            "acme",
                            branvil::test_support::input(
                                json!({"repair":true,"state":"open","comments":"none"}),
                            ),
                            owner.page("repairs".into(), Some(100), None).unwrap(),
                        )
                        .await
                        .unwrap();
                    if let Some(card) = page.items.into_iter().next() {
                        break card.ticket;
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                }
            })
            .await
            .expect("the failed verification opened no repair ticket");
            assert!(ticket.repair);
            assert_eq!(ticket.lane, "ready");
            // The first agent by name responds while no merged PR names another.
            assert_eq!(ticket.assignee.as_ref().unwrap().name, "aiko");
            self.tickets
                .lock()
                .unwrap()
                .insert("repair".into(), (ticket.key, ticket.url.clone()));
            ticket.url
        }
        fn name(&self, url: &str) -> String {
            self.tickets
                .lock()
                .unwrap()
                .iter()
                .find(|(_, (_, u))| u == url)
                .map_or_else(|| url.into(), |(name, _)| name.clone())
        }
        async fn patrol(
            &self,
            cycle: &mut Cycle,
            outcome: Outcome,
        ) -> (Result<Outcome, ServiceError>, bool, Option<String>) {
            let diagnostics = self.runtime.diagnostics();
            let selector = Selector {
                roster: &self.runtime.roster,
                diagnostics: &diagnostics,
                person: "aiko",
                source: "routine",
                language: "en",
                ticket: self.agent(),
                code: self.runtime.code.as_deref(),
                runner: &self.runtime,
                quota: None,
            };
            let mut selected = None;
            let (result, exhausted) = cycle
                .patrol(&selector, |task, _| {
                    selected = task.url;
                    std::future::ready(outcome)
                })
                .await;
            (result, exhausted, selected.map(|url| self.name(&url)))
        }
    }
    #[tokio::test]
    async fn reordering_and_newly_ready_work_apply_to_the_next_start() {
        let f = Fixture::new();
        let mut cycle = Cycle::default();
        for (order, expected, exhausted) in [
            (vec!["A", "B", "C", "D"], "A", false),
            (vec!["D", "A", "B", "C"], "D", false),
            (vec!["new", "D", "A", "B", "C"], "new", false),
            (vec!["new", "D", "A", "B", "C"], "B", false),
            (vec!["new", "D", "A", "B", "C"], "C", true),
        ] {
            f.order(&order).await;
            assert_eq!(
                f.patrol(&mut cycle, Outcome::Succeeded).await,
                (Ok(Outcome::Succeeded), exhausted, Some(expected.into()))
            );
        }
        assert!(cycle.attempted.is_empty());
    }
    #[tokio::test]
    async fn failed_work_is_skipped_until_the_cycle_ends_but_deferred_work_retries() {
        let f = Fixture::new();
        f.order(&["A", "B"]).await;
        let mut cycle = Cycle::default();
        for (outcome, selected, exhausted) in [
            (Outcome::Failed, "A", false),
            (Outcome::Deferred, "B", false),
            (Outcome::Succeeded, "B", true),
            (Outcome::Deferred, "A", false),
            (Outcome::Succeeded, "A", false),
        ] {
            assert_eq!(
                f.patrol(&mut cycle, outcome).await,
                (Ok(outcome), exhausted, Some(selected.into()))
            );
        }
    }
    #[tokio::test]
    async fn fresh_repairs_take_precedence_and_failed_repairs_do_not_starve_other_work() {
        let f = Fixture::new();
        f.order(&["A", "B"]).await;
        let mut cycle = Cycle::default();
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await.2,
            Some("A".into())
        );
        f.repair().await;
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Failed).await,
            (Ok(Outcome::Deferred), false, Some("repair".into()))
        );
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Ok(Outcome::Succeeded), true, Some("B".into()))
        );
    }
    #[tokio::test]
    async fn failed_pr_refresh_does_not_block_the_next_ticket() {
        let f = Fixture::new();
        f.order(&["ticket"]).await;
        crate::member::native::tests::seed_code(&f.native, f.root.path());
        let code = f.runtime.code.as_deref().unwrap();
        let pull = code
            .create_pull_request(branvil::agent::port::CreatePullRequest {
                repository: "acme/demo".into(),
                head: "feature".into(),
                base: "main".into(),
                title: "Fix login".into(),
                body: String::new(),
                draft: false,
                tickets: vec![],
            })
            .await
            .unwrap();
        f.owner()
            .pulls_add_comment(
                "acme/demo",
                1,
                branvil::test_support::input(json!({"body":"Please fix"})),
            )
            .await
            .unwrap();
        f.tickets.lock().unwrap().insert(
            "PR".into(),
            (String::new(), pull.written.target.url.clone()),
        );
        code.faults
            .fail("refresh_pull_request", ServiceError::Unsupported);
        let mut cycle = Cycle::default();
        // The PR is listed first; refreshing it fails at the selector boundary.
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Err(ServiceError::Unsupported), false, None)
        );
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Ok(Outcome::Succeeded), true, Some("ticket".into()))
        );
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Err(ServiceError::Unsupported), false, None)
        );
        assert_eq!(
            code.faults
                .calls()
                .into_iter()
                .filter(|(operation, _)| *operation == "refresh_pull_request")
                .map(|(_, url)| url)
                .collect::<Vec<_>>(),
            vec![pull.written.target.url.clone(); 2]
        );
    }
    #[tokio::test]
    async fn refresh_failure_is_consumed_but_unavailability_is_deferred() {
        let f = Fixture::new();
        f.order(&["A", "B"]).await;
        let mut cycle = Cycle::default();
        let faults = &f.agent().faults;
        faults.fail("refresh", ServiceError::Failed);
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Err(ServiceError::Failed), false, None)
        );
        faults.clear();
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await.2,
            Some("B".into())
        );
        faults.fail("refresh", ServiceError::Unavailable);
        for _ in 0..2 {
            assert_eq!(
                f.patrol(&mut cycle, Outcome::Succeeded).await,
                (Err(ServiceError::Unavailable), false, None)
            );
            assert!(cycle.attempted.is_empty());
        }
        faults.clear();
        f.order(&[]).await;
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Ok(Outcome::Succeeded), true, None)
        );
    }
    #[tokio::test]
    async fn candidate_fetch_failures_end_the_cycle_and_keep_the_error_class() {
        let f = Fixture::new();
        for error in [ServiceError::Failed, ServiceError::Unavailable] {
            f.agent().faults.fail("candidates", error.clone());
            let mut cycle = Cycle::default();
            cycle.attempted.insert(Some("previous".into()));
            assert_eq!(
                f.patrol(&mut cycle, Outcome::Succeeded).await,
                (Err(error), true, None)
            );
            assert!(cycle.attempted.is_empty());
        }
    }
}
