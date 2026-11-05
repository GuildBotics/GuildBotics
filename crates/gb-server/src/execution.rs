//! One execution lease and journal boundary, shared by every work source.
use gb_base::{files::FileLock, shared_writes::SharedWrites};
use gb_observability::{
    redaction::Redactor,
    runs::{RunFinish, RunRecord, RunState, RunStore},
};
use std::{path::PathBuf, sync::Arc};
use tokio_util::{sync::CancellationToken, task::TaskTracker};

/// One drain generation is shared by API commands and autonomous workers.
/// Registration and sealing are atomic, so a request cannot enter after a
/// stopper has observed an empty tracker.
#[derive(Clone, Default)]
pub struct Coordinator(Arc<std::sync::Mutex<Arc<Generation>>>);
pub struct Generation {
    pub id: String,
    pub draining: CancellationToken,
    pub cancelled: CancellationToken,
    pub work: TaskTracker,
    active: std::sync::Mutex<std::collections::BTreeMap<String, serde_json::Value>>,
}
impl Default for Generation {
    fn default() -> Self {
        Self {
            id: uuid::Uuid::new_v4().simple().to_string(),
            draining: CancellationToken::new(),
            cancelled: CancellationToken::new(),
            work: TaskTracker::new(),
            active: Default::default(),
        }
    }
}
impl Generation {
    pub fn snapshot(&self) -> Vec<serde_json::Value> {
        self.active.lock().unwrap().values().cloned().collect()
    }
}
struct Active(Arc<Generation>, String);
impl Drop for Active {
    fn drop(&mut self) {
        self.0.active.lock().unwrap().remove(&self.1);
    }
}
impl Coordinator {
    pub fn generation(&self) -> Arc<Generation> {
        self.0.lock().unwrap().clone()
    }
    pub fn spawn<F>(
        &self,
        make: impl FnOnce(Arc<Generation>) -> F,
    ) -> Result<tokio::task::JoinHandle<F::Output>, Rejected>
    where
        F: std::future::Future + Send + 'static,
        F::Output: Send + 'static,
    {
        let generation = self.0.lock().unwrap();
        if generation.draining.is_cancelled() {
            return Err(Rejected::Draining);
        }
        Ok(generation.work.spawn(make(generation.clone())))
    }
    pub fn begin_drain(&self, force: bool) -> Arc<Generation> {
        let generation = self.0.lock().unwrap();
        generation.draining.cancel();
        if force {
            generation.cancelled.cancel();
        }
        generation.work.close();
        generation.clone()
    }
    pub async fn wait(&self, generation: Arc<Generation>) {
        generation.work.wait().await;
        let mut current = self.0.lock().unwrap();
        if Arc::ptr_eq(&current, &generation) {
            *current = Arc::default();
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rejected {
    Draining,
    LeaseUnavailable,
    Duplicate,
    InvalidIdentity,
    Store,
}
impl std::fmt::Display for Rejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for Rejected {}

pub struct Admission {
    pub generation: Option<Arc<Generation>>,
    pub command: String,
    pub workspace: PathBuf,
    pub writes: Arc<SharedWrites>,
    pub record: RunRecord,
    pub redactor: Redactor,
    pub exclusive: bool,
    pub record_start: bool,
    pub cleanup: TaskTracker,
}

pub struct Execution {
    admission: Option<Admission>,
    lease: Option<FileLock>,
    active: Option<Active>,
}

pub fn failure_state(source: &str, error: &anyhow::Error) -> RunState {
    if source != "manual"
        && error.chain().any(|cause| {
            cause
                .downcast_ref::<gb_ports::ServiceError>()
                .is_some_and(gb_ports::ServiceError::is_unavailable)
        })
    {
        RunState::Deferred
    } else {
        RunState::Failed
    }
}

impl Execution {
    pub async fn enter(
        mut admission: Admission,
        draining: CancellationToken,
    ) -> Result<Self, Rejected> {
        if draining.is_cancelled() {
            return Err(Rejected::Draining);
        }
        if admission.record.run_id.is_empty()
            || !gb_config::team::valid_person_id(&admission.record.member_id)
        {
            return Err(Rejected::InvalidIdentity);
        }
        if admission.record.work_identity.is_none() {
            admission.record.work_identity =
                Some([("work_id".into(), admission.record.run_id.clone())].into());
        }
        let tracker = admission.cleanup.clone();
        let (sender, receiver) = tokio::sync::oneshot::channel();
        tracker.spawn(async move {
        let result=tokio::task::spawn_blocking(move || {
            let lease = if admission.exclusive {
                let path=admission.workspace.join(".guildbotics/local/run/person-leases").join(format!("{}.lock",admission.record.member_id));
                let mut lease=FileLock::try_hold(&path).map_err(|_|Rejected::Store)?;
                if lease.is_none() {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                    lease=FileLock::try_hold(&path).map_err(|_|Rejected::Store)?;
                }
                let lease=lease.ok_or(Rejected::LeaseUnavailable)?;
                {
                    use std::io::{Seek,SeekFrom,Write};
                    let metadata=serde_json::json!({"pid":std::process::id(),"person_id":admission.record.member_id,
                        "lease_id":uuid::Uuid::new_v4().simple().to_string(),"source":admission.record.source,
                        "command":admission.command,"work_id":admission.record.run_id,"run_id":admission.record.run_id,"started_at":admission.record.started_at});
                    let mut file=lease.file();
                    file.set_len(0).and_then(|()|file.seek(SeekFrom::Start(0))).map_err(|_|Rejected::Store)?;
                    file.write_all(format!(" {metadata}\n").as_bytes()).map_err(|_|Rejected::Store)?;
                    file.sync_all().map_err(|_|Rejected::Store)?;
                }
                Some(lease)
            } else {
                None
            };
            admission
                .writes
                .transaction(|writer| {
                    if draining.is_cancelled() {return Err(Rejected::Draining.into());}
                    let store = RunStore::open(&admission.workspace);
                    if admission.record.source != "manual"
                        && store.records()?.iter().any(|record| {
                            record.member_id == admission.record.member_id
                                && record.work_identity == admission.record.work_identity
                                && !record.left_work_undone()
                        })
                    {
                        return Ok(false);
                    }
                    if admission.record_start {
                        store.start(writer, admission.record.clone(), &admission.redactor)?;
                    }
                    Ok(true)
                })
                .map_err(|error| error.downcast_ref::<Rejected>().copied().unwrap_or(Rejected::Store))
                .or_else(|error| {
                    if admission.record.source=="manual" && error==Rejected::Store {
                        tracing::warn!("cannot record manual command admission");
                        Ok(true)
                    } else {Err(error)}
                })
                .and_then(|accepted| {
                    if accepted {
                        let active = admission.generation.as_ref().map(|generation| {
                            let record = &admission.record;
                            generation.active.lock().unwrap().insert(record.run_id.clone(), serde_json::json!({
                                "id":record.run_id,"source":record.source,"person_id":record.member_id,
                                "command":admission.redactor.text(&admission.command),"started_at":record.started_at,
                            }));
                            Active(generation.clone(), record.run_id.clone())
                        });
                        Ok(Self {
                            admission: Some(admission),
                            lease,
                            active,
                        })
                    } else {
                        Err(Rejected::Duplicate)
                    }
                })
        })
        .await.unwrap_or(Err(Rejected::Store));
        // If the caller was cancelled, dispose of the acquired execution
        // before this tracked task ends, so cleanup cannot escape draining.
        let _ = sender.send(result);
        });
        receiver.await.unwrap_or(Err(Rejected::Store))
    }

    pub async fn finish(mut self, state: RunState) -> anyhow::Result<()> {
        let admission = self.admission.take().expect("one execution boundary");
        let lease = self.lease.take();
        let active = self.active.take();
        let cleanup = admission.cleanup.clone();
        cleanup
            .spawn_blocking(move || {
                let _active = active;
                settle(admission, lease, state)
            })
            .await?
    }
}

impl Drop for Execution {
    fn drop(&mut self) {
        if let Some(admission) = self.admission.take() {
            let lease = self.lease.take();
            let active = self.active.take();
            admission.cleanup.clone().spawn_blocking(move || {
                let _active = active;
                if let Err(error) = settle(admission, lease, RunState::Cancelled) {
                    tracing::error!("cannot record cancelled work: {error:#}");
                }
            });
        }
    }
}

fn settle(admission: Admission, lease: Option<FileLock>, state: RunState) -> anyhow::Result<()> {
    let _lease = lease;
    let manual = admission.record.source == "manual";
    let result = admission.writes.transaction(|writer| {
        RunStore::open(&admission.workspace).finish(
            writer,
            &admission.record.run_id,
            RunFinish {
                state,
                summary: match state {
                    RunState::Succeeded => "Execution completed.",
                    RunState::Deferred => "Waiting for an unavailable service.",
                    _ => "Execution was cancelled or failed.",
                },
                attributes: admission.record.attributes,
                now: &chrono::Utc::now().to_rfc3339(),
            },
            &admission.redactor,
        )?;
        Ok(())
    });
    if manual && result.is_err() {
        tracing::warn!("cannot record manual command completion");
        Ok(())
    } else {
        result
    }
}

#[cfg(test)]
mod tests {
    #[tokio::test]
    async fn coordinator_seals_registration_until_graceful_drain_finishes_and_force_escalates_it() {
        use super::*;
        let coordinator = Arc::new(Coordinator::default());
        let entered = CancellationToken::new();
        let release = CancellationToken::new();
        let task = coordinator.spawn({
            let (entered, release) = (entered.clone(), release.clone());
            move |generation| async move {
                entered.cancel();
                tokio::select! { () = release.cancelled() => true, () = generation.cancelled.cancelled() => false }
            }
        }).unwrap();
        entered.cancelled().await;
        let generation = coordinator.begin_drain(false);
        assert!(matches!(
            coordinator.spawn(|_| async {}),
            Err(Rejected::Draining)
        ));
        assert!(!generation.cancelled.is_cancelled());
        let waiting = tokio::spawn({
            let coordinator = coordinator.clone();
            async move {
                coordinator.wait(generation).await;
            }
        });
        tokio::task::yield_now().await;
        assert!(!waiting.is_finished());
        release.cancel();
        assert!(task.await.unwrap());
        waiting.await.unwrap();
        let entered = CancellationToken::new();
        let task = coordinator
            .spawn({
                let entered = entered.clone();
                move |generation| async move {
                    entered.cancel();
                    generation.cancelled.cancelled().await;
                }
            })
            .unwrap();
        entered.cancelled().await;
        let first = coordinator.begin_drain(false);
        let forced = coordinator.begin_drain(true);
        assert!(Arc::ptr_eq(&first, &forced));
        assert!(first.cancelled.is_cancelled());
        task.await.unwrap();
        coordinator.wait(forced).await;
        assert!(!coordinator.generation().draining.is_cancelled());
    }
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn manual_journaling_is_best_effort_but_autonomous_admission_is_strict() {
        let root = tempfile::tempdir().unwrap();
        let cleanup = TaskTracker::new();
        std::fs::create_dir_all(root.path().join(".guildbotics/state")).unwrap();
        std::fs::write(
            root.path().join(".guildbotics/state/task-runs"),
            "broken store",
        )
        .unwrap();
        assert!(matches!(
            Execution::enter(
                admission(root.path(), "automatic", &cleanup),
                CancellationToken::new()
            )
            .await,
            Err(Rejected::Store)
        ));
        let mut manual = admission(root.path(), "manual", &cleanup);
        manual.record.source = "manual".into();
        Execution::enter(manual, CancellationToken::new())
            .await
            .unwrap()
            .finish(RunState::Succeeded)
            .await
            .unwrap();
    }

    #[test]
    fn only_operations_that_never_reached_the_service_are_autonomously_deferred() {
        use gb_ports::ServiceError;
        for source in ["manual", "scheduled", "routine", "event_queue"] {
            for error in [
                ServiceError::Failed,
                ServiceError::NotFound,
                ServiceError::Unsupported,
                ServiceError::Unavailable,
            ] {
                let unavailable = error.is_unavailable();
                let error = anyhow::Error::new(error).context("outer boundary");
                assert_eq!(
                    failure_state(source, &error),
                    if source != "manual" && unavailable {
                        RunState::Deferred
                    } else {
                        RunState::Failed
                    }
                );
            }
        }
    }

    #[tokio::test]
    async fn stop_during_a_shared_write_wait_rejects_admission_without_creating_a_run() {
        for abort in [false, true] {
            let root = tempfile::tempdir().unwrap();
            let cleanup = TaskTracker::new();
            let incoming = admission(root.path(), "waiting", &cleanup);
            let writes = incoming.writes.clone();
            let (held_tx, held_rx) = tokio::sync::oneshot::channel();
            let (release_tx, release_rx) = std::sync::mpsc::channel();
            let holder = tokio::task::spawn_blocking(move || {
                writes.transaction(|_| {
                    held_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                    Ok(())
                })
            });
            held_rx.await.unwrap();
            let token = CancellationToken::new();
            let mut run = Some(tokio::spawn(Execution::enter(incoming, token.clone())));
            while !root
                .path()
                .join(".guildbotics/local/run/person-leases/aiko.lock")
                .exists()
            {
                tokio::task::yield_now().await;
            }
            token.cancel();
            if abort {
                let task = run.take().unwrap();
                task.abort();
                assert!(task.await.err().unwrap().is_cancelled());
                cleanup.close();
                assert!(
                    tokio::time::timeout(std::time::Duration::from_millis(20), cleanup.wait())
                        .await
                        .is_err()
                );
            }
            release_tx.send(()).unwrap();
            holder.await.unwrap().unwrap();
            if let Some(run) = run {
                assert!(matches!(run.await.unwrap(), Err(Rejected::Draining)));
            }
            cleanup.close();
            cleanup.wait().await;
            assert!(RunStore::open(root.path()).records().unwrap().is_empty());
        }
    }

    fn admission(root: &std::path::Path, id: &str, cleanup: &TaskTracker) -> Admission {
        Admission {generation:None,command:"workflows/ticket_driven_workflow".into(),workspace:root.into(),writes:Arc::new(SharedWrites::new(root.into(),None)),
            record:serde_json::from_value(json!({"run_id":id,"work_kind":"routine","member_id":"aiko","device_id":"device",
                "execution_mode":"autonomous","source":"routine","started_at":"2026-11-05T00:00:00Z","work_identity":{"slot":"1"}})).unwrap(),
            redactor:Redactor::default(),exclusive:true,record_start:true,cleanup:cleanup.clone()}
    }

    #[tokio::test]
    async fn every_source_shares_one_member_lease_and_cancel_is_journaled_before_release() {
        let root = tempfile::tempdir().unwrap();
        let cleanup = TaskTracker::new();
        let generation = Arc::new(Generation::default());
        let mut input = admission(root.path(), "run1", &cleanup);
        input.generation = Some(generation.clone());
        let running = Execution::enter(input, CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(generation.snapshot()[0]["id"], "run1");
        assert_eq!(generation.snapshot()[0]["person_id"], "aiko");
        assert_eq!(generation.snapshot()[0]["source"], "routine");
        assert!(matches!(
            Execution::enter(
                admission(root.path(), "run2", &cleanup),
                CancellationToken::new()
            )
            .await,
            Err(Rejected::LeaseUnavailable)
        ));
        assert!(RunStore::open(root.path()).read("run2").unwrap().is_none());
        let mut reader = admission(root.path(), "reader", &cleanup);
        reader.exclusive = false;
        reader.generation = Some(generation.clone());
        reader.record.work_identity = Some([("slot".into(), "reader".into())].into());
        let reader = Execution::enter(reader, CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(generation.snapshot().len(), 2);
        reader.finish(RunState::Succeeded).await.unwrap();
        assert_eq!(generation.snapshot().len(), 1);
        let lease_path = root
            .path()
            .join(".guildbotics/local/run/person-leases/aiko.lock");
        assert!(FileLock::try_hold(&lease_path).unwrap().is_none());
        let mut other = admission(root.path(), "other-member", &cleanup);
        other.record.member_id = "yuki".into();
        Execution::enter(other, CancellationToken::new())
            .await
            .unwrap()
            .finish(RunState::Succeeded)
            .await
            .unwrap();
        let mut draining_reader = admission(root.path(), "draining-reader", &cleanup);
        draining_reader.exclusive = false;
        let stopped = CancellationToken::new();
        stopped.cancel();
        assert!(matches!(
            Execution::enter(draining_reader, stopped).await,
            Err(Rejected::Draining)
        ));
        drop(running);
        cleanup.close();
        cleanup.wait().await;
        assert!(generation.snapshot().is_empty());
        assert_eq!(
            RunStore::open(root.path())
                .read("run1")
                .unwrap()
                .unwrap()
                .status,
            RunState::Cancelled
        );
        let resumed = Execution::enter(
            admission(root.path(), "run2", &cleanup),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        resumed.finish(RunState::Succeeded).await.unwrap();
        assert!(matches!(
            Execution::enter(
                admission(root.path(), "run3", &cleanup),
                CancellationToken::new()
            )
            .await,
            Err(Rejected::Duplicate)
        ));
    }

    #[tokio::test]
    async fn every_terminal_state_preserves_evidence_and_only_unfinished_work_retries() {
        use gb_observability::runs::{Completion, RunCompletion, RunResult};
        for (status, asking) in [
            (RunState::Failed, false),
            (RunState::Cancelled, false),
            (RunState::Interrupted, false),
            (RunState::Deferred, false),
            (RunState::Succeeded, false),
            (RunState::ResultUnknown, false),
            (RunState::Failed, true),
        ] {
            let retryable = match status {
                RunState::Failed
                | RunState::Cancelled
                | RunState::Interrupted
                | RunState::Deferred => !asking,
                RunState::Succeeded | RunState::ResultUnknown => false,
                RunState::Running => panic!("not terminal"),
            };
            let root = tempfile::tempdir().unwrap();
            let cleanup = TaskTracker::new();
            let input = admission(root.path(), "same-run", &cleanup);
            let writes = input.writes.clone();
            let execution = Execution::enter(input, CancellationToken::new())
                .await
                .unwrap();
            let store = RunStore::open(root.path());
            writes.transaction(|writer| {
                store.append(writer,store.read("same-run")?.unwrap(),&serde_json::json!({"kind":"evidence","evidence_type":"chat_reply","payload":{"text":"partial"}}),"2026-11-05T00:01:00Z",&Redactor::default())?;
                if asking {store.record_completion(writer,"same-run",RunCompletion {result:RunResult {subject_type:"chat".into(),subject_id:"local:C1:T1".into(),subject_url:String::new(),status:Completion::Asking,waiting_for:vec![]},member_id:"aiko",summary:"Which one?",now:"2026-11-05T00:01:00Z"},&Redactor::default())?;}
                Ok(())
            }).unwrap();
            execution.finish(status).await.unwrap();
            let next = Execution::enter(
                admission(root.path(), "same-run", &cleanup),
                CancellationToken::new(),
            )
            .await;
            if retryable {
                let next = next.unwrap();
                let record = store.read("same-run").unwrap().unwrap();
                assert_eq!(record.run_id, "same-run");
                assert_eq!(record.status, RunState::Running);
                assert!(record.finished_at.is_none());
                assert_eq!(record.provider_evidence.len(), 1);
                next.finish(RunState::Cancelled).await.unwrap();
            } else {
                assert!(matches!(next, Err(Rejected::Duplicate)));
            }
            cleanup.close();
            cleanup.wait().await;
        }
    }

    #[tokio::test]
    async fn idle_selection_leaves_no_record_and_deferred_input_can_retry() {
        let root = tempfile::tempdir().unwrap();
        let cleanup = TaskTracker::new();
        let mut selected = admission(root.path(), "idle", &cleanup);
        selected.record_start = false;
        Execution::enter(selected, CancellationToken::new())
            .await
            .unwrap()
            .finish(RunState::Succeeded)
            .await
            .unwrap();
        assert!(RunStore::open(root.path()).records().unwrap().is_empty());
        Execution::enter(
            admission(root.path(), "deferred", &cleanup),
            CancellationToken::new(),
        )
        .await
        .unwrap()
        .finish(RunState::Deferred)
        .await
        .unwrap();
        let retry = Execution::enter(
            admission(root.path(), "retry", &cleanup),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        retry.finish(RunState::Failed).await.unwrap();
        assert_eq!(
            RunStore::open(root.path())
                .read("retry")
                .unwrap()
                .unwrap()
                .status,
            RunState::Failed
        );
        assert!(matches!(
            Execution::enter(admission(root.path(), "draining", &cleanup), {
                let token = CancellationToken::new();
                token.cancel();
                token
            })
            .await,
            Err(Rejected::Draining)
        ));
        assert!(
            RunStore::open(root.path())
                .read("draining")
                .unwrap()
                .is_none()
        );
    }
}
