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
            Ok(_) => Outcome::Succeeded,
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
                diagnostics: &diagnostics,
                person: &self.runtime.person.person_id,
                source: "routine",
                language: &self.runtime.language,
                ticket: tickets,
                code: self.runtime.code.as_deref(),
                runner: self.runtime.as_ref(),
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
                            batch.as_mut().unwrap().push_front(task);
                        }
                        let exhausted = batch.as_ref().is_none_or(VecDeque::is_empty);
                        if exhausted {
                            *batch = None;
                        }
                        return (outcome, exhausted);
                    }
                    Err(error) => {
                        self.runtime
                            .selection_failed(error.clone(), "routine")
                            .await;
                        let outcome = if error.is_unavailable() {
                            batch.as_mut().unwrap().push_front(task);
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
                    Err(_) => Outcome::Failed,
                },
                None => Outcome::Deferred,
            }
        })
    }
    fn failed(&self, errors: u8, limit: u8, source: &str) {
        self.runtime.diagnostics().record(serde_json::from_value(serde_json::json!({"kind":"event","type":"scheduler.worker.failed","trace_id":null,"source":"scheduler","person_id":self.runtime.person.person_id,
            "timestamp":chrono::Utc::now().to_rfc3339(),"attributes":{"service_run_id":self.runtime.generation.as_ref().map(|g|g.id.as_str()).unwrap_or("")},
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
