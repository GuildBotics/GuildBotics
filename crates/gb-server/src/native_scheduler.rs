//! Production adapter for the scheduler's three input sources.
use crate::{
    service::{Outcome, Pending},
    ticket_selector::Selector,
    workflow_runtime::{Refused, Runtime},
};
use gb_base::service::ServiceError;
use branvil::agent::port::Task;
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

    struct Fixture {
        _root: tempfile::TempDir,
        runtime: Runtime,
        tickets: crate::test_ticket::Faults,
    }
    impl Fixture {
        fn new() -> Self {
            let root = tempfile::tempdir().unwrap();
            let runtime = crate::test_chat::runtime(root.path());
            let tickets = crate::test_ticket::Faults::new(runtime.tickets.clone().unwrap());
            Self {
                _root: root,
                runtime,
                tickets,
            }
        }
        fn order(&self, urls: &[&str]) {
            *self.tickets.candidates.lock().unwrap() = Some(
                urls.iter()
                    .map(|url| Task {
                        url: Some((*url).into()),
                        assignee: Some("aiko".into()),
                        ..Default::default()
                    })
                    .collect(),
            );
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
                ticket: &self.tickets,
                code: None,
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
            (result, exhausted, selected)
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
            f.order(&order);
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
        f.order(&["A", "B"]);
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
        f.order(&["A", "B"]);
        let mut cycle = Cycle::default();
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await.2,
            Some("A".into())
        );
        f.order(&["A", "B", "repair"]);
        f.tickets.candidates.lock().unwrap().as_mut().unwrap()[2].repair = true;
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
        f.order(&["PR", "ticket"]);
        f.tickets.candidates.lock().unwrap().as_mut().unwrap()[0].pull_request_url =
            Some("PR".into());
        let mut cycle = Cycle::default();
        // No code service: refreshing this PR fails at the selector boundary.
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
    }
    #[tokio::test]
    async fn refresh_failure_is_consumed_but_unavailability_is_deferred() {
        let f = Fixture::new();
        f.order(&["A", "B"]);
        let mut cycle = Cycle::default();
        f.tickets
            .errors
            .lock()
            .unwrap()
            .insert("refresh", ServiceError::Failed);
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Err(ServiceError::Failed), false, None)
        );
        f.tickets.errors.lock().unwrap().clear();
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await.2,
            Some("B".into())
        );
        f.tickets
            .errors
            .lock()
            .unwrap()
            .insert("refresh", ServiceError::Unavailable);
        for _ in 0..2 {
            assert_eq!(
                f.patrol(&mut cycle, Outcome::Succeeded).await,
                (Err(ServiceError::Unavailable), false, None)
            );
            assert!(cycle.attempted.is_empty());
        }
        f.tickets.errors.lock().unwrap().clear();
        f.order(&[]);
        assert_eq!(
            f.patrol(&mut cycle, Outcome::Succeeded).await,
            (Ok(Outcome::Succeeded), true, None)
        );
    }
    #[tokio::test]
    async fn candidate_fetch_failures_end_the_cycle_and_keep_the_error_class() {
        let f = Fixture::new();
        for error in [ServiceError::Failed, ServiceError::Unavailable] {
            f.tickets
                .errors
                .lock()
                .unwrap()
                .insert("candidates", error.clone());
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
