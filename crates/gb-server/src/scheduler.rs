//! Each member drains one serial queue, preserving deferred schedule slots.
use crate::{
    schedule::ScheduledCommand,
    service::{Outcome, Pending, Worker},
    service_settings::Settings,
};
use chrono::{Duration, Local, NaiveDateTime, Timelike};
use std::{
    collections::{BTreeMap, VecDeque},
    sync::Arc,
};
use tokio::sync::Mutex;

pub trait Engine: Send + Sync {
    fn available(&self) -> Pending<'_, bool>;
    fn command<'a>(
        &'a self,
        line: &'a str,
        source: &'a str,
        slot: BTreeMap<String, String>,
    ) -> Pending<'a, Outcome>;
    /// Returns whether this patrol batch is exhausted, including stale input.
    fn patrol(&self, slot: BTreeMap<String, String>) -> Pending<'_, (Outcome, bool)>;
    fn events(&self) -> Pending<'_, Outcome>;
    fn failed(&self, errors: u8, limit: u8, source: &str);
}

struct Member {
    engine: Arc<dyn Engine>,
    scheduled: Vec<ScheduledCommand>,
    routines: Vec<String>,
    routine_index: usize,
    next_routine: Option<NaiveDateTime>,
    next_routine_check: Option<NaiveDateTime>,
    next_check: Option<NaiveDateTime>,
    pending: VecDeque<(usize, NaiveDateTime)>,
    patrol: bool,
    wake_patrol: bool,
    patrol_retry: Option<NaiveDateTime>,
    last_source: &'static str,
    error_limit: u8,
}

/// Wake bits are independent of the member lock held during a run.
#[derive(Default)]
pub struct Wakes(std::sync::Mutex<BTreeMap<String, bool>>);
impl Wakes {
    pub fn all(&self) {
        self.except(None);
    }
    pub fn except(&self, person: Option<&str>) {
        for (member, pending) in self.0.lock().unwrap().iter_mut() {
            if Some(member.as_str()) != person {
                *pending = true;
            }
        }
    }
    fn take(&self, person: &str) -> bool {
        self.0
            .lock()
            .unwrap()
            .get_mut(person)
            .is_some_and(std::mem::take)
    }
}
#[derive(Default)]
pub struct Scheduler {
    members: BTreeMap<String, Mutex<Member>>,
    pub wakes: Arc<Wakes>,
    heartbeats: std::sync::Mutex<BTreeMap<String, serde_json::Value>>,
}

impl Scheduler {
    pub fn with_wakes(wakes: Arc<Wakes>) -> Self {
        Self {
            wakes,
            ..Self::default()
        }
    }
    #[cfg(test)]
    pub(crate) fn seed_heartbeat(&self, person: &str) {
        self.heartbeats.lock().unwrap().insert(
            person.into(),
            serde_json::json!({"person_id":person,"last_routine_at":"2026-11-05T00:00:00Z"}),
        );
    }

    pub fn heartbeats(&self) -> Vec<serde_json::Value> {
        self.heartbeats.lock().unwrap().values().cloned().collect()
    }
    pub fn add(
        &mut self,
        person: String,
        engine: Arc<dyn Engine>,
        scheduled: Vec<ScheduledCommand>,
        routines: Vec<String>,
    ) {
        self.wakes.0.lock().unwrap().insert(person.clone(), false);
        self.members.insert(
            person,
            Mutex::new(Member {
                engine,
                scheduled,
                routines,
                routine_index: 0,
                next_routine: None,
                next_routine_check: None,
                next_check: None,
                pending: VecDeque::new(),
                patrol: false,
                wake_patrol: false,
                patrol_retry: None,
                last_source: "",
                error_limit: 3,
            }),
        );
    }

    pub async fn tick(&self, person: &str, settings: &Settings, now: NaiveDateTime) -> Outcome {
        let started = tokio::time::Instant::now();
        let Some(member) = self.members.get(person) else {
            return Outcome::Failed;
        };
        // The lock belongs to the member, so another member never waits for it.
        let mut state = member.lock().await;
        state.error_limit = settings.max_consecutive_errors;
        if !state.engine.available().await {
            return Outcome::Deferred;
        }
        if self.wakes.take(person) {
            state.wake_patrol = true;
        }
        if state.next_check.is_none_or(|next| now >= next) && state.pending.is_empty() {
            state.next_check = Some(now + Duration::minutes(1));
            if settings.scheduled {
                for index in 0..state.scheduled.len() {
                    match state.scheduled[index].should_run(now) {
                        Ok(true) => state.pending.push_back((index, now)),
                        Ok(false) => (),
                        Err(_) => return Outcome::Failed,
                    }
                }
            }
        }
        if settings.scheduled
            && let Some((index, due)) = state.pending.pop_front()
        {
            state.last_source = "scheduled";
            let line = state.scheduled[index].command.clone();
            let outcome = state
                .engine
                .command(&line, "scheduled", slot("scheduled", person, &line, due))
                .await;
            if outcome == Outcome::Deferred {
                // Keep the sampled occurrence, even if a command outlives a
                // cron boundary. A deferral never samples a different time.
                state.scheduled[index].defer();
            }
            return outcome;
        }
        let routine_due = state.next_routine_check.is_none_or(|next| now >= next)
            && (state.patrol || state.next_routine.is_none_or(|next| now >= next));
        let woken = !routine_due
            && state.wake_patrol
            && state
                .routines
                .iter()
                .any(|line| line == crate::ticket_selector::COMMAND);
        if settings.routine && (routine_due || woken) {
            let line = if woken || state.patrol {
                crate::ticket_selector::COMMAND.to_owned()
            } else if state.routines.is_empty() {
                String::new()
            } else {
                state.routines[state.routine_index % state.routines.len()].clone()
            };
            let is_patrol = line == crate::ticket_selector::COMMAND;
            let retry_ready = !is_patrol || state.patrol_retry.is_none_or(|next| now >= next);
            if !line.is_empty() && retry_ready {
                if !woken && !state.patrol && !state.routines.is_empty() {
                    state.routine_index += 1;
                }
                if is_patrol {
                    state.wake_patrol = false;
                }

                state.last_source = "routine";
                if !woken {
                    state.next_routine_check = Some(now + Duration::minutes(1));
                }
                let (outcome, exhausted) = if line == crate::ticket_selector::COMMAND {
                    state
                        .engine
                        .patrol(slot("routine", person, &line, now))
                        .await
                } else {
                    (
                        state
                            .engine
                            .command(&line, "routine", slot("routine", person, &line, now))
                            .await,
                        true,
                    )
                };
                if is_patrol {
                    state.patrol_retry = (outcome != Outcome::Succeeded || !exhausted)
                        .then_some(now + Duration::minutes(1));
                }
                if woken {
                    state.wake_patrol = !exhausted;
                } else {
                    state.patrol = !exhausted;
                }
                if exhausted && !woken {
                    let completed = now + Duration::from_std(started.elapsed()).unwrap_or_default();
                    let next =
                        completed + Duration::minutes(i64::from(settings.routine_interval_minutes));
                    state.next_routine = Some(next);
                    let stamp = |time: NaiveDateTime| {
                        time.and_local_timezone(Local)
                            .earliest()
                            .map(|v| v.to_rfc3339())
                            .unwrap_or_else(|| time.and_utc().to_rfc3339())
                    };
                    self.heartbeats.lock().unwrap().insert(person.into(), serde_json::json!({"person_id":person,"last_routine_at":stamp(completed),"next_routine_at":stamp(next)}));
                }
                return outcome;
            }
        }
        if settings.event_queue {
            state.last_source = "event_queue";
            {
                state.engine.events().await;
                Outcome::Deferred
            }
        } else {
            Outcome::Deferred
        }
    }
}

impl Worker for Scheduler {
    fn cycle<'a>(&'a self, person: &'a str, settings: &'a Settings) -> Pending<'a, Outcome> {
        Box::pin(async move {
            self.tick(person, settings, Local::now().naive_local())
                .await
        })
    }
    fn worker_failed(&self, person: &str, errors: u8) {
        if let Some(member) = self.members.get(person)
            && let Ok(member) = member.try_lock()
        {
            member
                .engine
                .failed(errors, member.error_limit, member.last_source);
        }
    }
}

pub fn slot(
    kind: &str,
    person: &str,
    command: &str,
    time: NaiveDateTime,
) -> BTreeMap<String, String> {
    [
        ("kind", kind.to_owned()),
        ("person_id", person.to_owned()),
        ("command", command.to_owned()),
        (
            "slot",
            time.with_second(0)
                .unwrap()
                .with_nanosecond(0)
                .unwrap()
                .format("%Y-%m-%dT%H:%M:%S")
                .to_string(),
        ),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_owned(), v))
    .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Mutex as StdMutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    };
    type Call = (String, String, BTreeMap<String, String>);
    struct Fake {
        ready: AtomicBool,
        delay: AtomicU64,
        calls: StdMutex<Vec<Call>>,
        outcomes: StdMutex<VecDeque<Outcome>>,
        event_outcome: StdMutex<Outcome>,
        patrol: StdMutex<VecDeque<(Outcome, bool)>>,
    }
    impl Fake {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                ready: AtomicBool::new(true),
                delay: AtomicU64::new(0),
                calls: StdMutex::new(vec![]),
                outcomes: StdMutex::new(VecDeque::new()),
                event_outcome: StdMutex::new(Outcome::Deferred),
                patrol: StdMutex::new(VecDeque::new()),
            })
        }
    }
    impl Engine for Fake {
        fn available(&self) -> Pending<'_, bool> {
            Box::pin(async { self.ready.load(Ordering::SeqCst) })
        }
        fn command<'a>(
            &'a self,
            line: &'a str,
            source: &'a str,
            slot: BTreeMap<String, String>,
        ) -> Pending<'a, Outcome> {
            Box::pin(async move {
                self.calls
                    .lock()
                    .unwrap()
                    .push((line.into(), source.into(), slot));
                let delay = self.delay.load(Ordering::SeqCst);
                if delay > 0 {
                    tokio::time::sleep(std::time::Duration::from_secs(delay)).await;
                }
                self.outcomes
                    .lock()
                    .unwrap()
                    .pop_front()
                    .unwrap_or(Outcome::Succeeded)
            })
        }
        fn patrol(&self, slot: BTreeMap<String, String>) -> Pending<'_, (Outcome, bool)> {
            Box::pin(async move {
                self.calls
                    .lock()
                    .unwrap()
                    .push(("patrol".into(), "routine".into(), slot));
                self.patrol
                    .lock()
                    .unwrap()
                    .pop_front()
                    .unwrap_or((Outcome::Succeeded, true))
            })
        }
        fn events(&self) -> Pending<'_, Outcome> {
            Box::pin(async {
                self.calls.lock().unwrap().push((
                    "events".into(),
                    "event_queue".into(),
                    BTreeMap::new(),
                ));
                *self.event_outcome.lock().unwrap()
            })
        }
        fn failed(&self, _: u8, _: u8, _: &str) {}
    }
    #[tokio::test]
    async fn branvil_subscription_advances_patrol_and_stops_on_shutdown() {
        use branvil::events::{Change, MemberKind, Notice};
        let engine = Fake::new();
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![],
            vec![crate::ticket_selector::COMMAND.into()],
        );
        let settings = Settings {
            event_queue: false,
            ..Default::default()
        };
        scheduler.tick("aiko", &settings, at(0)).await;
        let mut expected = 1;
        for kind in [
            "ticket",
            "pull",
            "repo",
            "check",
            "verification",
            "upstream",
        ] {
            for actor in [Some(MemberKind::Agent), Some(MemberKind::Owner), None] {
                let notices = futures_util::stream::iter([Notice::Change(Change {
                    kind,
                    path: "org/repo".into(),
                    id: "1".into(),
                    actor,
                })]);
                crate::service_runtime::listen_branvil(
                    notices,
                    scheduler.wakes.clone(),
                    Default::default(),
                )
                .await;
                scheduler.tick("aiko", &settings, at(0)).await;
                expected += usize::from(actor != Some(MemberKind::Agent));
                assert_eq!(
                    engine.calls.lock().unwrap().len(),
                    expected,
                    "{kind} {actor:?}"
                );
            }
        }
        crate::service_runtime::listen_branvil(
            futures_util::stream::iter([Notice::Reset]),
            scheduler.wakes.clone(),
            Default::default(),
        )
        .await;
        scheduler.tick("aiko", &settings, at(0)).await;
        assert_eq!(engine.calls.lock().unwrap().len(), expected + 1);
        let stop = tokio_util::sync::CancellationToken::new();
        stop.cancel();
        crate::service_runtime::listen_branvil(
            futures_util::stream::pending(),
            scheduler.wakes.clone(),
            stop,
        )
        .await;
        assert!(!scheduler.wakes.take("aiko"));
    }

    #[tokio::test]
    async fn wake_coalesces_and_preserves_periodic_fallback_and_source_switch() {
        let engine = Fake::new();
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![],
            vec![crate::ticket_selector::COMMAND.into()],
        );
        let mut settings = Settings {
            event_queue: false,
            ..Default::default()
        };
        scheduler.tick("aiko", &settings, at(0)).await;
        for _ in 0..3 {
            scheduler.wakes.all();
        }
        assert_eq!(engine.calls.lock().unwrap().len(), 1);
        scheduler
            .tick("aiko", &settings, at(0) + Duration::seconds(5))
            .await;
        scheduler
            .tick("aiko", &settings, at(0) + Duration::seconds(10))
            .await;
        assert_eq!(engine.calls.lock().unwrap().len(), 2);
        scheduler.tick("aiko", &settings, at(11)).await;
        assert_eq!(engine.calls.lock().unwrap().len(), 3);
        settings.routine = false;
        scheduler.wakes.all();
        scheduler.tick("aiko", &settings, at(12)).await;
        assert_eq!(engine.calls.lock().unwrap().len(), 3);
        settings.routine = true;
        scheduler.tick("aiko", &settings, at(12)).await;
        assert_eq!(engine.calls.lock().unwrap().len(), 4);
    }

    #[tokio::test(start_paused = true)]
    async fn wake_during_run_survives_completion_and_unavailability() {
        let engine = Fake::new();
        engine.delay.store(120, Ordering::SeqCst);
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![],
            vec!["work".into(), crate::ticket_selector::COMMAND.into()],
        );
        let scheduler = Arc::new(scheduler);
        let running = tokio::spawn({
            let scheduler = scheduler.clone();
            async move { scheduler.tick("aiko", &Settings::default(), at(0)).await }
        });
        while engine.calls.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
        scheduler.wakes.all();
        scheduler.wakes.all();
        running.await.unwrap();
        engine.delay.store(0, Ordering::SeqCst);
        engine.ready.store(false, Ordering::SeqCst);
        scheduler.tick("aiko", &Settings::default(), at(2)).await;
        engine.ready.store(true, Ordering::SeqCst);
        scheduler.tick("aiko", &Settings::default(), at(2)).await;
        scheduler
            .tick(
                "aiko",
                &Settings {
                    event_queue: false,
                    ..Default::default()
                },
                at(2),
            )
            .await;
        assert_eq!(engine.calls.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn finishing_execution_wakes_other_members_without_self_notification() {
        use gb_observability::runs::RunState;
        let root = tempfile::tempdir().unwrap();
        let cleanup = tokio_util::task::TaskTracker::new();
        let coordinator = crate::execution::Coordinator::default();
        let mut scheduler = Scheduler::with_wakes(coordinator.generation().wakes.clone());
        let engine = Fake::new();
        for person in ["aiko", "yuki"] {
            scheduler.add(
                person.into(),
                engine.clone(),
                vec![],
                vec![crate::ticket_selector::COMMAND.into()],
            );
            scheduler.tick(person, &Settings::default(), at(0)).await;
        }
        for (i, state) in [
            RunState::Succeeded,
            RunState::Failed,
            RunState::Cancelled,
            RunState::Deferred,
            RunState::Interrupted,
            RunState::ResultUnknown,
        ]
        .into_iter()
        .enumerate()
        {
            let admission = crate::execution::Admission {
                generation: Some(coordinator.generation()), command: "work".into(), workspace: root.path().into(),
                writes: Arc::new(gb_base::shared_writes::SharedWrites::new(root.path().into(), None)),
                record: serde_json::from_value(serde_json::json!({"run_id":format!("wake-{i}"),"work_kind":"manual","member_id":"aiko","device_id":"device","execution_mode":"user_initiated","source":"manual","started_at":"2026-11-05T00:00:00Z"})).unwrap(),
                redactor: Default::default(), exclusive: false, record_start: true, cleanup: cleanup.clone(),
            };
            let run = crate::execution::Execution::enter(
                admission,
                tokio_util::sync::CancellationToken::new(),
            )
            .await
            .unwrap();
            assert!(!scheduler.wakes.take("aiko"));
            run.finish(state).await.unwrap();
            for person in ["aiko", "yuki"] {
                assert_eq!(
                    scheduler.wakes.take(person),
                    person != "aiko" && state != RunState::Deferred
                );
            }
        }
        cleanup.close();
        cleanup.wait().await;
    }

    #[test]
    fn excluding_sender_preserves_external_pending_and_coalesces_other_members() {
        let wakes = Wakes::default();
        wakes
            .0
            .lock()
            .unwrap()
            .extend([("aiko".into(), false), ("yuki".into(), false)]);
        wakes.all();
        wakes.except(Some("aiko"));
        assert!(wakes.take("aiko"));
        assert!(wakes.take("yuki"));
        for _ in 0..3 {
            wakes.except(Some("aiko"));
        }
        assert!(!wakes.take("aiko"));
        assert!(wakes.take("yuki"));
        assert!(!wakes.take("yuki"));
    }

    #[tokio::test]
    async fn wakes_preserve_custom_rotation_and_do_not_starve_due_commands() {
        for mixed in [false, true] {
            let engine = Fake::new();
            let mut scheduler = Scheduler::default();
            let mut routines = vec!["custom".into()];
            if mixed {
                routines.push(crate::ticket_selector::COMMAND.into());
            }
            scheduler.add("aiko".into(), engine.clone(), vec![], routines);
            let settings = Settings {
                event_queue: false,
                routine_interval_minutes: 10,
                ..Default::default()
            };
            scheduler.tick("aiko", &settings, at(0)).await;
            for seconds in [5, 10, 15, 20, 25, 601, 1202] {
                scheduler.wakes.all();
                scheduler
                    .tick("aiko", &settings, at(0) + Duration::seconds(seconds))
                    .await;
            }
            let calls = engine.calls.lock().unwrap();
            assert_eq!(
                calls.iter().filter(|c| c.0 == "custom").count(),
                if mixed { 2 } else { 3 }
            );
            if mixed {
                assert_eq!(calls.last().unwrap().0, "custom");
            }
        }
    }

    #[tokio::test]
    async fn all_wake_sources_preserve_retry_for_every_outcome_and_batch_state() {
        for source in ["aiko", "kenji", "external"] {
            for outcome in [Outcome::Succeeded, Outcome::Failed, Outcome::Deferred] {
                for exhausted in [true, false] {
                    let engine = Fake::new();
                    engine
                        .patrol
                        .lock()
                        .unwrap()
                        .extend([(outcome, exhausted); 20]);
                    let mut scheduler = Scheduler::default();
                    scheduler.add(
                        "aiko".into(),
                        engine.clone(),
                        vec![],
                        vec![crate::ticket_selector::COMMAND.into()],
                    );
                    let settings = Settings {
                        event_queue: false,
                        ..Default::default()
                    };
                    for seconds in [0, 5, 10, 15, 20, 25, 60] {
                        if source == "external" {
                            scheduler.wakes.all();
                        } else {
                            scheduler.wakes.except(Some(source));
                        }
                        scheduler
                            .tick("aiko", &settings, at(0) + Duration::seconds(seconds))
                            .await;
                    }
                    let expected = if source == "aiko" && exhausted {
                        1
                    } else if outcome == Outcome::Succeeded && exhausted {
                        7
                    } else {
                        2
                    };
                    assert_eq!(
                        engine.calls.lock().unwrap().len(),
                        expected,
                        "{source} {outcome:?} {exhausted}"
                    );
                }
            }
        }
    }

    #[tokio::test]
    async fn successful_exhaustion_clears_the_previous_failure_retry_deadline() {
        for outcome in [Outcome::Failed, Outcome::Deferred] {
            let engine = Fake::new();
            engine.patrol.lock().unwrap().push_back((outcome, true));
            let mut scheduler = Scheduler::default();
            scheduler.add(
                "aiko".into(),
                engine.clone(),
                vec![],
                vec![crate::ticket_selector::COMMAND.into()],
            );
            let settings = Settings {
                event_queue: false,
                ..Default::default()
            };
            scheduler.tick("aiko", &settings, at(0)).await;
            for seconds in [60, 65] {
                scheduler.wakes.all();
                scheduler
                    .tick("aiko", &settings, at(0) + Duration::seconds(seconds))
                    .await;
            }
            assert_eq!(engine.calls.lock().unwrap().len(), 3);
        }
    }

    #[tokio::test]
    async fn failed_members_cannot_keep_each_other_running_without_retry_delay() {
        let mut scheduler = Scheduler::default();
        let engine = Fake::new();
        engine
            .patrol
            .lock()
            .unwrap()
            .extend([(Outcome::Failed, true); 20]);
        for person in ["aiko", "kenji"] {
            scheduler.add(
                person.into(),
                engine.clone(),
                vec![],
                vec![crate::ticket_selector::COMMAND.into()],
            );
        }
        let settings = Settings {
            event_queue: false,
            ..Default::default()
        };
        for seconds in [0, 5, 10, 15, 20, 25, 60] {
            for person in ["aiko", "kenji"] {
                if scheduler
                    .tick(person, &settings, at(0) + Duration::seconds(seconds))
                    .await
                    == Outcome::Failed
                {
                    scheduler.wakes.except(Some(person));
                }
            }
            assert_eq!(
                engine.calls.lock().unwrap().len(),
                if seconds < 60 { 2 } else { 4 }
            );
        }
    }

    #[tokio::test]
    async fn extra_patrol_batch_preserves_custom_deadline_and_scheduled_priority() {
        let engine = Fake::new();
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![ScheduledCommand::new("scheduled".into(), "1 12 * * *".into(), at(-1)).unwrap()],
            vec!["custom".into(), crate::ticket_selector::COMMAND.into()],
        );
        let settings = Settings {
            event_queue: false,
            routine_interval_minutes: 10,
            ..Default::default()
        };
        scheduler.tick("aiko", &settings, at(0)).await;
        engine
            .patrol
            .lock()
            .unwrap()
            .extend([(Outcome::Succeeded, false), (Outcome::Succeeded, true)]);
        scheduler.wakes.all();
        scheduler.tick("aiko", &settings, at(1)).await;
        scheduler.tick("aiko", &settings, at(1)).await;
        scheduler
            .tick("aiko", &settings, at(1) + Duration::seconds(5))
            .await;
        scheduler.tick("aiko", &settings, at(2)).await;
        scheduler.tick("aiko", &settings, at(11)).await;
        scheduler.tick("aiko", &settings, at(22)).await;
        assert_eq!(
            engine
                .calls
                .lock()
                .unwrap()
                .iter()
                .map(|c| c.0.as_str())
                .collect::<Vec<_>>(),
            vec![
                "custom",
                "scheduled",
                "patrol",
                "patrol",
                "patrol",
                "custom"
            ]
        );
    }

    #[tokio::test]
    async fn chat_results_never_reset_or_increase_command_errors() {
        let fake = Fake::new();
        let mut scheduler = Scheduler::default();
        scheduler.add("aiko".into(), fake.clone(), vec![], vec![]);
        let settings = Settings {
            routine: false,
            scheduled: false,
            ..Default::default()
        };
        for outcome in [Outcome::Succeeded, Outcome::Failed] {
            *fake.event_outcome.lock().unwrap() = outcome;
            assert_eq!(
                scheduler.tick("aiko", &settings, at(0)).await,
                Outcome::Deferred
            );
        }
        assert_eq!(fake.calls.lock().unwrap().len(), 2);
    }
    fn at(minute: i64) -> NaiveDateTime {
        "2026-11-05T12:00:00".parse::<NaiveDateTime>().unwrap() + Duration::minutes(minute)
    }
    #[tokio::test(start_paused = true)]
    async fn routine_interval_is_measured_after_the_command_finishes_and_chat_runs_between_patrol_checks()
     {
        let engine = Fake::new();
        engine.delay.store(120, Ordering::SeqCst);
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![],
            vec!["r1".into(), "r2".into()],
        );
        let settings = Settings::default();
        assert!(scheduler.heartbeats().is_empty());
        assert_eq!(
            scheduler.tick("aiko", &settings, at(0)).await,
            Outcome::Succeeded
        );
        let heartbeat = scheduler.heartbeats();
        assert_eq!(heartbeat.len(), 1);
        assert_eq!(heartbeat[0]["person_id"], "aiko");
        let last =
            chrono::DateTime::parse_from_rfc3339(heartbeat[0]["last_routine_at"].as_str().unwrap())
                .unwrap();
        let next =
            chrono::DateTime::parse_from_rfc3339(heartbeat[0]["next_routine_at"].as_str().unwrap())
                .unwrap();
        assert_eq!(last.naive_local(), at(2));
        assert_eq!(next - last, Duration::minutes(3));
        assert_eq!(
            scheduler.tick("aiko", &settings, at(4)).await,
            Outcome::Deferred
        );
        assert_eq!(
            scheduler.tick("aiko", &settings, at(5)).await,
            Outcome::Succeeded
        );
        assert_eq!(
            engine
                .calls
                .lock()
                .unwrap()
                .iter()
                .map(|c| c.0.as_str())
                .collect::<Vec<_>>(),
            vec!["r1", "events", "r2"]
        );
        let engine = Fake::new();
        engine
            .patrol
            .lock()
            .unwrap()
            .push_back((Outcome::Succeeded, false));
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![],
            vec![crate::ticket_selector::COMMAND.into()],
        );
        scheduler.tick("aiko", &settings, at(0)).await;
        scheduler
            .tick("aiko", &settings, at(0) + Duration::seconds(5))
            .await;
        assert_eq!(
            engine
                .calls
                .lock()
                .unwrap()
                .iter()
                .map(|c| c.0.as_str())
                .collect::<Vec<_>>(),
            vec!["patrol", "events"]
        );
    }
    #[tokio::test(start_paused = true)]
    async fn scheduled_then_routines_round_robin_then_chat_and_no_schedule_runs_twice() {
        let engine = Fake::new();
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![ScheduledCommand::new("scheduled".into(), "0 12 * * *".into(), at(-1)).unwrap()],
            vec!["r1".into(), "r2".into()],
        );
        let settings = Settings::default();
        for minute in [0, 0, 0, 1, 10, 10] {
            scheduler.tick("aiko", &settings, at(minute)).await;
        }
        let calls = engine.calls.lock().unwrap();
        assert_eq!(
            calls.iter().map(|c| c.0.as_str()).collect::<Vec<_>>(),
            vec!["scheduled", "r1", "events", "events", "r2", "events"]
        );
        assert_eq!(calls[0].2["slot"], "2026-11-05T12:00:00");
    }
    #[tokio::test(start_paused = true)]
    async fn unavailable_environment_consumes_neither_routine_nor_schedule_and_deferral_keeps_the_slot()
     {
        let engine = Fake::new();
        engine.ready.store(false, Ordering::SeqCst);
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![ScheduledCommand::new("scheduled".into(), "* * * * *".into(), at(-1)).unwrap()],
            vec!["r1".into()],
        );
        let settings = Settings::default();
        assert_eq!(
            scheduler.tick("aiko", &settings, at(0)).await,
            Outcome::Deferred
        );
        assert!(engine.calls.lock().unwrap().is_empty());
        engine.ready.store(true, Ordering::SeqCst);
        engine.outcomes.lock().unwrap().push_back(Outcome::Deferred);
        assert_eq!(
            scheduler.tick("aiko", &settings, at(1)).await,
            Outcome::Deferred
        );
        assert_eq!(
            scheduler.tick("aiko", &settings, at(2)).await,
            Outcome::Succeeded
        );
        let calls = engine.calls.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].0, calls[1].0);
        assert_eq!(calls[0].2["slot"], "2026-11-05T12:01:00");
        assert_eq!(calls[1].2["slot"], "2026-11-05T12:02:00");
    }
    #[tokio::test(start_paused = true)]
    async fn patrol_batch_ignores_interval_until_exhausted_and_source_switches_gate_work() {
        let engine = Fake::new();
        engine.patrol.lock().unwrap().extend([
            (Outcome::Succeeded, false),
            (Outcome::Failed, false),
            (Outcome::Succeeded, true),
        ]);
        let mut scheduler = Scheduler::default();
        scheduler.add(
            "aiko".into(),
            engine.clone(),
            vec![],
            vec![crate::ticket_selector::COMMAND.into()],
        );
        let mut settings = Settings::default();
        for (minute, expected) in [Outcome::Succeeded, Outcome::Failed, Outcome::Succeeded]
            .into_iter()
            .enumerate()
        {
            assert_eq!(
                scheduler.tick("aiko", &settings, at(minute as i64)).await,
                expected
            );
        }
        assert_eq!(
            scheduler.tick("aiko", &settings, at(3)).await,
            Outcome::Deferred
        );
        settings.routine = false;
        settings.event_queue = false;
        assert_eq!(
            scheduler.tick("aiko", &settings, at(20)).await,
            Outcome::Deferred
        );
        assert_eq!(
            engine
                .calls
                .lock()
                .unwrap()
                .iter()
                .map(|c| c.0.as_str())
                .collect::<Vec<_>>(),
            vec!["patrol", "patrol", "patrol", "events"]
        );
    }
}
