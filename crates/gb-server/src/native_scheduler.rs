//! Production adapter for the scheduler's three input sources.
use crate::{
    service::{Outcome, Pending},
    ticket_selector::Selector,
    workflow_runtime::{Refused, Runtime},
};
use gb_ports::Task;
use std::{
    collections::{BTreeMap, VecDeque},
    sync::Arc,
};
use tokio::sync::Mutex;

pub struct Engine {
    pub runtime: Arc<Runtime>,
    pub chat: Option<crate::chat_dispatch::Dispatcher>,
    candidates: Mutex<Option<VecDeque<Task>>>,
    environment: std::sync::Mutex<Option<String>>,
}

impl Engine {
    pub fn new(runtime: Arc<Runtime>, chat: Option<crate::chat_dispatch::Dispatcher>) -> Self {
        Self {
            runtime,
            chat,
            candidates: Mutex::new(None),
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

fn prepend_repairs(batch: &mut VecDeque<Task>, repairs: Vec<Task>) {
    for task in repairs.into_iter().rev() {
        batch.retain(|old| old.url != task.url);
        batch.push_front(task);
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
            let mut batch = self.candidates.lock().await;
            if batch.is_none() {
                match selector.candidates().await {
                    Ok(tasks) => *batch = Some(tasks.into()),
                    Err(error) => {
                        self.runtime
                            .selection_failed(error.clone(), "routine")
                            .await;
                        return (
                            if error.is_unavailable() {
                                Outcome::Deferred
                            } else {
                                Outcome::Failed
                            },
                            true,
                        );
                    }
                }
            } else if let Some(tasks) = batch.as_mut() {
                match selector.repair_candidates().await {
                    Ok(fresh) => {
                        prepend_repairs(tasks, fresh);
                    }
                    Err(error) => {
                        self.runtime
                            .selection_failed(error.clone(), "routine")
                            .await;
                        return (
                            if error.is_unavailable() {
                                Outcome::Deferred
                            } else {
                                Outcome::Failed
                            },
                            false,
                        );
                    }
                }
            }
            while let Some(task) = batch.as_mut().and_then(VecDeque::pop_front) {
                match selector.refresh(&task).await {
                    Ok(None) => continue,
                    Ok(Some(invocation)) => {
                        let task: Task = serde_json::from_value(invocation.payload["task"].clone())
                            .expect("selector serialized a validated refreshed task");
                        let outcome = Self::outcome(
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
                        );
                        if outcome == Outcome::Deferred {
                            batch.as_mut().unwrap().push_front(task.clone());
                        }
                        let exhausted = batch.as_ref().is_none_or(VecDeque::is_empty);
                        if exhausted {
                            *batch = None;
                        }
                        return (repair_outcome(outcome, task.repair), exhausted);
                    }
                    Err(error) => {
                        self.runtime
                            .selection_failed(error.clone(), "routine")
                            .await;
                        let outcome = if error.is_unavailable() {
                            batch.as_mut().unwrap().push_front(task.clone());
                            Outcome::Deferred
                        } else {
                            Outcome::Failed
                        };
                        let exhausted = batch.as_ref().is_none_or(VecDeque::is_empty);
                        if exhausted {
                            *batch = None;
                        }
                        return (outcome, exhausted);
                    }
                }
            }
            *batch = None;
            (Outcome::Succeeded, true)
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
    fn fresh_repair_tickets_and_prs_precede_the_saved_batch_without_losing_it() {
        let task = |url: &str, repair| Task {
            url: Some(url.into()),
            repair,
            ..Default::default()
        };
        let mut batch =
            VecDeque::from([task("ordinary-pr", false), task("ordinary-ticket", false)]);
        for url in ["repair-ticket", "repair-pr"] {
            prepend_repairs(&mut batch, vec![task(url, true)]);
            assert_eq!(batch.pop_front().unwrap().url.as_deref(), Some(url));
        }
        assert_eq!(
            batch.pop_front().unwrap().url.as_deref(),
            Some("ordinary-pr")
        );
        assert_eq!(
            batch.pop_front().unwrap().url.as_deref(),
            Some("ordinary-ticket")
        );
        let mut batch = VecDeque::from([task("repair", true), task("ordinary", false)]);
        prepend_repairs(&mut batch, vec![task("repair", true)]);
        assert_eq!(batch.len(), 2);
    }
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
