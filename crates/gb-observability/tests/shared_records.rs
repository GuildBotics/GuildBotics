use chrono::{DateTime, Duration, Utc};
use gb_base::{files::SHARED_RECORD_SCHEMA_VERSION, shared_writes::SharedWrites};
use gb_observability::{
    activity::ActivityEvents,
    interactive::{InteractiveSession, InteractiveSessions, SessionCommand},
    redaction::Redactor,
    runs::{RunFinish, RunRecord, RunState, RunStore},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../../records/golden/shared-records.json")).unwrap()
}
fn redactor() -> Redactor {
    Redactor::new(["synthetic-secret".into()])
}
fn attributes(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}
fn seed(id: &str) -> RunRecord {
    serde_json::from_value(
        json!({"run_id":id,"work_kind":"ticket","execution_mode":"autonomous",
        "member_id":"p1","device_id":"device-1","source":"routine","started_at":"<now>",
        "attributes":{"target.title":"synthetic-secret"}}),
    )
    .unwrap()
}
fn session() -> InteractiveSession {
    serde_json::from_value(
        json!({"trace_id":"trace-1","person_id":"p1","workspace":"/workspace",
        "host":"codex","thread_key":"thread","started_at":"2026-11-03T10:00:00+00:00",
        "last_seen_at":"2026-11-03T10:00:00+00:00","expires_at":"2026-11-03T10:30:00+00:00"}),
    )
    .unwrap()
}
fn write_population(root: &Path) -> anyhow::Result<Value> {
    SharedWrites::new(root.to_owned(), None).transaction(|w| {
        let mut events = Vec::new();
        for kind in ["ticket.comment", "command.failed", "diagnostics.completed"] {
            let path = ActivityEvents::open(root).record(w, &json!({"event_id":kind,"type":kind,
                "timestamp":"2026-11-01T00:30:00+14:00", "person_id":"p1", "schema_version":999,
                "payload":{"nested":["synthetic-secret in error","界".repeat(501)],"stdout":"local only"},
                "attributes":{"new_field":"synthetic-secret"}}), &redactor())?;
            events.push(serde_json::from_slice::<Value>(&std::fs::read(path)?)?);
        }
        let runs = RunStore::open(root);
        let mut incoming=seed("run-1");
        incoming.attributes.insert("target.empty".into(),String::new());
        incoming.work_identity=Some(attributes(&[("key", &format!("synthetic-secret{}","x".repeat(501)))]));
        runs.start(w, incoming, &redactor())?;
        runs.append(w, seed("run-1"), &json!({"kind":"evidence","evidence_type":"issue_comment",
            "payload":{"text":"synthetic-secret","messages":["local"]}}), "<now>", &redactor())?;
        let run = runs.finish(w, "run-1", RunFinish { state: RunState::Failed, summary: "synthetic-secret",
            attributes: attributes(&[("target.title","later"),("target.url","ticket/1"),("target.empty","filled")]), now:"<now>" }, &redactor())?.unwrap();
        let sessions = InteractiveSessions::open(root);
        for (command, status, attrs) in [
            ("member ticket read", "success", attributes(&[("target.title","synthetic-secret"),("local.path","local")])),
            ("member ticket comment", "failed", attributes(&[("target.title","later"),("target.url","ticket/1")]))
        ] {
            sessions.record(w, &session(), SessionCommand { command, status, attributes:attrs, now:"2026-11-03T10:00:00+00:00" }, &redactor())?;
        }
        let shared: Value = serde_json::from_slice(&std::fs::read(root.join(".guildbotics/state/sessions/trace-1.json"))?)?;
        Ok(json!({"events":events,"run":run,"session":shared}))
    })
}

#[test]
fn both_hosts_write_the_same_records() {
    let root = tempfile::tempdir().unwrap();
    let actual = write_population(root.path()).unwrap();
    for key in ["events", "run", "session"] {
        assert_eq!(actual[key], fixture()[key], "{key}");
    }
}

fn inspect_population(path: &Path, count: &mut usize) {
    for entry in std::fs::read_dir(path).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            inspect_population(&path, count);
            continue;
        }
        assert_eq!(
            path.extension().unwrap(),
            "json",
            "unclassified record kind"
        );
        let record: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            record["schema_version"],
            SHARED_RECORD_SCHEMA_VERSION,
            "{}",
            path.display()
        );
        *count += 1;
    }
}
#[test]
fn every_record_actually_written_under_state_has_the_current_generation() {
    let root = tempfile::tempdir().unwrap();
    write_population(root.path()).unwrap();
    let mut count = 0;
    inspect_population(&root.path().join(".guildbotics/state"), &mut count);
    assert!(count >= 5);
}

#[test]
fn redaction_walks_new_fields_arrays_unicode_and_masks_before_truncating() {
    let redactor = Redactor::new(["12345678".into(), "1234567890".into(), "short".into()]);
    let masked = redactor.redact(
        json!({"future":[{"field":"1234567890 short"},"界".repeat(501)],
        "stdout":"gone","nested":{"messages":["gone"],"value":true}}),
    );
    assert_eq!(masked["future"][0]["field"], "*** short");
    assert_eq!(masked["future"][1].as_str().unwrap().chars().count(), 500);
    assert_eq!(masked["nested"], json!({"value":true}));
    assert!(masked.get("stdout").is_none());
    assert_eq!(
        redactor.text(&format!("{}1234567890", "x".repeat(498))),
        format!("{}**", "x".repeat(498))
    );
}

#[test]
fn event_windows_compare_instants_across_months_and_only_skip_command_lifecycle() {
    let root = tempfile::tempdir().unwrap();
    write_population(root.path()).unwrap();
    let instant = DateTime::parse_from_rfc3339("2026-10-31T10:30:00Z")
        .unwrap()
        .with_timezone(&Utc);
    let events = ActivityEvents::open(root.path())
        .list_between(instant, instant)
        .unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["kind"], "diagnostics.completed");
    assert_eq!(events[1]["kind"], "ticket.comment");
    assert!(
        ActivityEvents::open(root.path())
            .list_between(instant + Duration::seconds(1), instant + Duration::days(1))
            .unwrap()
            .is_empty()
    );
}

#[test]
fn retries_keep_evidence_but_never_reopen_completed_or_unknown_results() {
    let root = tempfile::tempdir().unwrap();
    write_population(root.path()).unwrap();
    SharedWrites::new(root.path().to_owned(), None)
        .transaction(|w| {
            let runs = RunStore::open(root.path());
            let retry = runs.start(w, seed("run-1"), &redactor())?;
            assert_eq!(retry.status, RunState::Running);
            assert!(retry.finished_at.is_none());
            assert_eq!(retry.provider_evidence.len(), 1);
            assert_eq!(retry.attributes["target.title"], "***");
            for state in [RunState::Succeeded, RunState::ResultUnknown] {
                let id = format!("{state:?}");
                runs.start(w, seed(&id), &redactor())?;
                let finished = runs
                    .finish(
                        w,
                        &id,
                        RunFinish {
                            state,
                            summary: "ok",
                            attributes: BTreeMap::new(),
                            now: "<now>",
                        },
                        &redactor(),
                    )?
                    .unwrap();
                assert_eq!(runs.start(w, seed(&id), &redactor())?, finished);
            }
            assert!(
                runs.finish(
                    w,
                    "absent",
                    RunFinish {
                        state: RunState::Failed,
                        summary: "",
                        attributes: BTreeMap::new(),
                        now: "<now>"
                    },
                    &redactor()
                )?
                .is_none()
            );
            assert!(runs.read("..").is_err());
            assert!(runs.read("a/b").is_err());
            Ok(())
        })
        .unwrap();
}

#[test]
fn interactive_identity_is_reused_at_expiry_then_rotates_and_is_scoped() {
    let root = tempfile::tempdir().unwrap();
    let now = DateTime::parse_from_rfc3339("2026-11-03T10:00:00Z")
        .unwrap()
        .with_timezone(&Utc);
    SharedWrites::new(root.path().to_owned(), None)
        .transaction(|w| {
            let sessions = InteractiveSessions::open(root.path());
            let a =
                sessions.start_or_touch(w, "p1", "codex", "thread", now, Duration::minutes(30))?;
            let b = sessions.start_or_touch(
                w,
                "p1",
                "codex",
                "thread",
                now + Duration::minutes(30),
                Duration::minutes(30),
            )?;
            assert_eq!(a.trace_id, b.trace_id);
            let c = sessions.start_or_touch(
                w,
                "p1",
                "codex",
                "thread",
                now + Duration::minutes(61),
                Duration::minutes(30),
            )?;
            assert_ne!(a.trace_id, c.trace_id);
            let d = sessions.start_or_touch(
                w,
                "p1",
                "codex",
                "other",
                now + Duration::minutes(61),
                Duration::minutes(30),
            )?;
            assert_ne!(c.trace_id, d.trace_id);
            Ok(())
        })
        .unwrap();
}

#[test]
fn strict_journal_reads_reject_corruption_while_timeline_skips_it() {
    let root = tempfile::tempdir().unwrap();
    let path = root
        .path()
        .join(".guildbotics/state/task-runs/bad/result.json");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, "{broken").unwrap();
    let runs = RunStore::open(root.path());
    assert!(runs.records().is_err());
    assert!(
        runs.records_between(Utc::now(), Utc::now())
            .unwrap()
            .is_empty()
    );
}

#[test]
fn every_update_overwrites_a_foreign_generation() {
    let root = tempfile::tempdir().unwrap();
    write_population(root.path()).unwrap();
    for relative in ["task-runs/run-1/result.json", "sessions/trace-1.json"] {
        let path = root.path().join(".guildbotics/state").join(relative);
        let mut v: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        v["schema_version"] = 999.into();
        std::fs::write(path, v.to_string()).unwrap();
    }
    SharedWrites::new(root.path().to_owned(), None)
        .transaction(|w| {
            RunStore::open(root.path()).append(
                w,
                seed("run-1"),
                &json!({"kind":"note","message":"updated"}),
                "<now>",
                &redactor(),
            )?;
            InteractiveSessions::open(root.path()).record(
                w,
                &session(),
                SessionCommand {
                    command: "read",
                    status: "success",
                    attributes: BTreeMap::new(),
                    now: "<now>",
                },
                &redactor(),
            )?;
            Ok(())
        })
        .unwrap();
    inspect_population(&root.path().join(".guildbotics/state"), &mut 0);
}

#[test]
fn system_sessions_share_a_transcript_and_finish_with_counts_from_disk() {
    let root = tempfile::tempdir().unwrap();
    let diagnostics = gb_observability::Diagnostics::open(root.path());
    assert!(diagnostics.system_trace_id().is_none());
    diagnostics.start_system_session("service-1").unwrap();
    let active_trace = diagnostics.system_trace_id().unwrap();
    assert!(active_trace.starts_with("system:system-"));
    diagnostics.start_system_session("service-2").unwrap();
    let other = gb_observability::Diagnostics::open(root.path());
    other.record(
        json!({"kind":"log","level":"ERROR","message":"failure","trace_id":null})
            .as_object()
            .unwrap()
            .clone(),
    );
    assert!(
        diagnostics
            .prune_transcripts(
                std::time::SystemTime::now() + std::time::Duration::from_secs(86400),
                std::time::Duration::ZERO
            )
            .unwrap()
            .is_empty()
    );
    other.finish_system_session().unwrap();
    diagnostics.finish_system_session().unwrap();
    let paths: Vec<_> = std::fs::read_dir(root.path().join(".guildbotics/local/run/sessions"))
        .unwrap()
        .collect();
    assert_eq!(paths.len(), 1);
    let path = paths[0].as_ref().unwrap().path();
    let records: Vec<Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(records.len(), 3);
    assert_eq!(records[2]["payload"]["event_count"], 2);
    assert_eq!(records[2]["payload"]["log_count"], 1);
    assert_eq!(records[2]["payload"]["error_count"], 1);
    let id = records[0]["attributes"]["system_session_id"]
        .as_str()
        .unwrap();
    assert_eq!(active_trace, format!("system:{id}"));
    assert!(diagnostics.system_trace_id().is_none());
    let detail = diagnostics.trace_detail(&active_trace).unwrap();
    assert_eq!(detail["records"].as_array().unwrap().len(), 3);
    assert_eq!(detail["transcript_available"], true);
    assert_eq!(
        diagnostics
            .prune_transcripts(
                std::time::SystemTime::now() + std::time::Duration::from_secs(86400),
                std::time::Duration::ZERO
            )
            .unwrap(),
        vec![path]
    );
}

#[test]
fn system_summaries_hide_the_latest_and_mark_abandoned_processes_interrupted() {
    let root = tempfile::tempdir().unwrap();
    let diagnostics = gb_observability::Diagnostics::open(root.path());
    for (id, day) in [("old", 1), ("latest", 2)] {
        diagnostics.record(json!({"kind":"event","type":"system.started","trace_id":null,
            "timestamp":format!("2026-11-{day:02}T00:00:00Z"),"attributes":{"system_session_id":id}}).as_object().unwrap().clone());
    }
    let list = diagnostics
        .traces(None, None, None, None, None, 1000)
        .unwrap();
    assert_eq!(list["traces"].as_array().unwrap().len(), 1);
    assert_eq!(list["traces"][0]["trace_id"], "system:old");
    assert_eq!(list["traces"][0]["status"], "interrupted");
    assert_eq!(list["traces"][0]["command"], "Global / system");
    for (query, count) in [
        ("interrupted", 0),
        ("status", 0),
        ("system.started", 1),
        ("Global", 1),
        ("old", 1),
    ] {
        assert_eq!(
            diagnostics
                .traces(None, None, Some(query), None, None, 1000)
                .unwrap()["traces"]
                .as_array()
                .unwrap()
                .len(),
            count,
            "{query}"
        );
    }
    assert_eq!(
        diagnostics.trace_detail("system:latest").unwrap()["summary"]["status"],
        "running"
    );
    assert!(
        diagnostics
            .traces(Some("system"), None, None, None, None, 1000)
            .unwrap()["traces"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn recorded_completions_remain_terminal_and_supply_the_trace_title() {
    use gb_observability::runs::{Completion, RunCompletion, RunResult};
    let root = tempfile::tempdir().unwrap();
    SharedWrites::new(root.path().to_owned(), None)
        .transaction(|w| {
            let runs = RunStore::open(root.path());
            let mut incoming = seed("completed");
            incoming.started_at = "2026-11-03T10:00:00Z".into();
            for completion in [
                Completion::Done,
                Completion::Asking,
                Completion::Blocked,
                Completion::AwaitingChecks,
            ] {
                incoming.run_id = format!("{completion:?}");
                runs.start(w, incoming.clone(), &redactor())?;
                let finished = runs.record_completion(
                    w,
                    &incoming.run_id,
                    RunCompletion {
                        result: RunResult {
                            subject_type: "ticket".into(),
                            subject_id: "ticket/1".into(),
                            subject_url: "ticket/1".into(),
                            status: completion,
                            waiting_for: vec![],
                        },
                        member_id: "p1",
                        summary: "Summary synthetic-secret\nnext line",
                        now: "2026-11-03T10:10:00Z",
                    },
                    &redactor(),
                )?;
                assert_eq!(runs.start(w, incoming.clone(), &redactor())?, finished);
                assert!(!finished.left_work_undone());
            }
            assert_eq!(
                runs.summaries_by_subject()?[&("ticket/1".into(), "p1".into())],
                "Summary ***\nnext line"
            );
            Ok(())
        })
        .unwrap();
    let diagnostics = gb_observability::Diagnostics::open(root.path());
    diagnostics.record(json!({"kind":"event","type":"command.started","trace_id":"trace","person_id":"p1","timestamp":"2026-11-03T10:00:00Z","attributes":{"target.url":"ticket/1"}}).as_object().unwrap().clone());
    assert_eq!(
        diagnostics
            .traces(None, None, None, None, None, 10)
            .unwrap()["traces"][0]["title"],
        "Summary ***"
    );
    assert_eq!(
        diagnostics.trace_detail("trace").unwrap()["summary"]["title"],
        "Summary ***"
    );
}

#[test]
fn recorded_durations_keep_the_same_float_when_served_as_json() {
    // Python's JSON reader preserves the nearest representable float. Compare
    // with Rust's standard parser so applying the same lossy JSON parser to
    // both sides cannot hide a changed duration in a file-to-file golden.
    for ticks in 1..1000 {
        let decimal = format!("{:.17}", f64::from(ticks) / 37.0);
        let expected = decimal.parse::<f64>().unwrap();
        let record: Value =
            serde_json::from_str(&format!("{{\"duration_ms\":{decimal}}}")).unwrap();
        assert_eq!(record["duration_ms"].as_f64().unwrap(), expected);
        let served: Value = serde_json::from_str(&record.to_string()).unwrap();
        assert_eq!(served["duration_ms"].as_f64().unwrap(), expected);
    }
}

#[test]
fn completed_records_match_python_for_every_completion_and_work_kind() {
    use gb_observability::runs::{RunCompletion, RunResult};
    let root = tempfile::tempdir().unwrap();
    SharedWrites::new(root.path().to_owned(), None)
        .transaction(|w| {
            let runs = RunStore::open(root.path());
            for expected in fixture()["completions"].as_array().unwrap() {
                let id = expected["run_id"].as_str().unwrap();
                let mut incoming = seed(id);
                incoming.work_kind = id.rsplit_once('-').unwrap().0.into();
                incoming.source = String::new();
                incoming.attributes.clear();
                runs.start(w, incoming.clone(), &redactor())?;
                for kind in ["issue_comment", "checks_awaited"] {
                    runs.append(
                        w,
                        incoming.clone(),
                        &json!({"kind":"evidence","evidence_type":kind,"payload":{}}),
                        "<now>",
                        &redactor(),
                    )?;
                }
                let result: RunResult = serde_json::from_value(expected["result"].clone())?;
                let actual = runs.record_completion(
                    w,
                    id,
                    RunCompletion {
                        result,
                        member_id: "p1",
                        summary: "completed",
                        now: "<now>",
                    },
                    &redactor(),
                )?;
                assert_eq!(serde_json::to_value(actual)?, *expected);
                assert_eq!(
                    runs.summaries_by_subject()?[&("ticket/1".into(), "p1".into())],
                    "completed"
                );
            }
            Ok(())
        })
        .unwrap();
}

#[test]
fn corrupted_interactive_entry_does_not_reset_other_conversations() {
    let root = tempfile::tempdir().unwrap();
    let now = Utc::now();
    SharedWrites::new(root.path().to_owned(), None)
        .transaction(|w| {
            let sessions = InteractiveSessions::open(root.path());
            let a = sessions.start_or_touch(w, "p1", "codex", "", now, Duration::minutes(30))?;
            let path = root
                .path()
                .join(".guildbotics/local/run/interactive_trace_state.json");
            let mut state: Value = serde_json::from_slice(&std::fs::read(&path)?)?;
            state["sessions"]
                .as_object_mut()
                .unwrap()
                .values_mut()
                .next()
                .unwrap()
                .as_object_mut()
                .unwrap()
                .remove("thread_key");
            state["sessions"]["broken"] = json!({"trace_id":"broken"});
            w.write_json(&path, &state)?;
            let b = sessions.start_or_touch(w, "p1", "codex", "", now, Duration::minutes(30))?;
            assert_eq!(a.trace_id, b.trace_id);
            Ok(())
        })
        .unwrap();
}

#[test]
fn evidence_creating_a_run_applies_the_same_attribute_boundary() {
    let root = tempfile::tempdir().unwrap();
    SharedWrites::new(root.path().to_owned(), None)
        .transaction(|w| {
            let mut incoming = seed("new");
            incoming.attributes.insert("empty".into(), String::new());
            let record = RunStore::open(root.path()).append(
                w,
                incoming,
                &json!({"kind":"evidence","evidence_type":"issue_comment"}),
                "<now>",
                &redactor(),
            )?;
            assert_eq!(record.attributes, attributes(&[("target.title", "***")]));
            Ok(())
        })
        .unwrap();
}
