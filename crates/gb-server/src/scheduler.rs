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
    last_source: &'static str,
    error_limit: u8,
}

#[derive(Default)]
pub struct Scheduler {
    members: BTreeMap<String, Mutex<Member>>,
    heartbeats: std::sync::Mutex<BTreeMap<String, serde_json::Value>>,
}

impl Scheduler {
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
        if settings.routine
            && state.next_routine_check.is_none_or(|next| now >= next)
            && (state.patrol || state.next_routine.is_none_or(|next| now >= next))
        {
            let line = if state.patrol {
                crate::ticket_selector::COMMAND.to_owned()
            } else if state.routines.is_empty() {
                String::new()
            } else {
                let line = state.routines[state.routine_index % state.routines.len()].clone();
                state.routine_index += 1;
                line
            };
            if !line.is_empty() {
                state.last_source = "routine";
                state.next_routine_check = Some(now + Duration::minutes(1));
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
                state.patrol = !exhausted;
                if exhausted {
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
            state.engine.events().await
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
        patrol: StdMutex<VecDeque<(Outcome, bool)>>,
    }
    impl Fake {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                ready: AtomicBool::new(true),
                delay: AtomicU64::new(0),
                calls: StdMutex::new(vec![]),
                outcomes: StdMutex::new(VecDeque::new()),
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
                Outcome::Deferred
            })
        }
        fn failed(&self, _: u8, _: u8, _: &str) {}
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
        assert_eq!(next - last, Duration::minutes(10));
        assert_eq!(
            scheduler.tick("aiko", &settings, at(10)).await,
            Outcome::Deferred
        );
        assert_eq!(
            scheduler.tick("aiko", &settings, at(12)).await,
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
