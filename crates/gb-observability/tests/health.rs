use gb_observability::{Diagnostics, health::SystemAlerts};
use serde_json::{Value, json};
fn turn(
    person: &str,
    tool: &str,
    model: &str,
    specified: Value,
    effort: &str,
    stamp: &str,
    status: &str,
) -> Value {
    json!({"kind":"event","type":format!("span.{status}"),"timestamp":stamp,"person_id":person,"attributes":{"agent.kind":"cli_agent","agent.adapter":tool},"payload":{"model":model,"model_specified":specified,"effort":effort}})
}
#[test]
fn test_latest_finished_or_failed_turn_per_member_and_tool() {
    let f = Fixture::new();
    for r in [
        turn(
            "alice",
            "codex",
            "old",
            json!(false),
            "low",
            "2026-09-29T10:00:00Z",
            "finished",
        ),
        turn(
            "alice",
            "claude",
            "sonnet",
            json!(true),
            "high",
            "2026-09-29T10:00:00Z",
            "finished",
        ),
        turn(
            "bob",
            "codex",
            "bob-model",
            json!(false),
            "xhigh",
            "2026-09-29T10:00:00Z",
            "finished",
        ),
        turn(
            "alice",
            "codex",
            "",
            json!(false),
            "",
            "2026-09-29T12:00:00Z",
            "failed",
        ),
        turn(
            "alice",
            "codex",
            "late-arriving-old",
            json!(false),
            "",
            "2026-09-29T19:30:00+09:00",
            "finished",
        ),
        turn(
            "alice",
            "codex",
            "still-running",
            json!(false),
            "",
            "2026-09-30T00:00:00Z",
            "started",
        ),
    ] {
        f.record(r);
    }
    assert_eq!(
        f.store.last_cli_agent_turns(),
        json!([
            {"person_id":"alice","agent":"claude","model":"sonnet","model_specified":true,"effort":"high","timestamp":"2026-09-29T10:00:00Z"},
            {"person_id":"alice","agent":"codex","model":"","model_specified":false,"effort":"","timestamp":"2026-09-29T12:00:00Z"},
            {"person_id":"bob","agent":"codex","model":"bob-model","model_specified":false,"effort":"xhigh","timestamp":"2026-09-29T10:00:00Z"}
        ])
    );
}
#[test]
fn test_incomplete_turn_facts_are_not_inferred_from_current_settings() {
    for (person, tool, specified, stamp) in [
        ("", "codex", json!(false), "2026-09-29T10:00:00Z"),
        ("alice", "", json!(false), "2026-09-29T10:00:00Z"),
        ("alice", "codex", Value::Null, "2026-09-29T10:00:00Z"),
        ("alice", "codex", json!(false), "invalid"),
    ] {
        let f = Fixture::new();
        f.record(turn(
            person, tool, "actual", specified, "", stamp, "finished",
        ));
        assert_eq!(f.store.last_cli_agent_turns(), json!([]));
    }
}
#[test]
fn test_each_keys_latest_call_decides_its_state() {
    let f = Fixture::new();
    for (status, stamp, service, provider, category) in [
        ("failed", "2026-10-08T10:00:00Z", "llm", "openai", "credit"),
        ("finished", "2026-10-08T10:05:00Z", "llm", "openai", ""),
        (
            "failed",
            "2026-10-08T10:00:00Z",
            "llm",
            "anthropic",
            "credit",
        ),
        (
            "failed",
            "2026-10-08T19:10:00+09:00",
            "llm",
            "anthropic",
            "rate_limit",
        ),
        (
            "finished",
            "2026-10-08T19:00:00+09:00",
            "llm",
            "anthropic",
            "",
        ),
        (
            "failed",
            "2026-10-08T10:00:00Z",
            "jev",
            "",
            "authentication",
        ),
        ("failed", "2026-10-08T11:00:00Z", "jev", "", ""),
    ] {
        let mut r = call(status, service, provider, category);
        r["timestamp"] = stamp.into();
        f.record(r);
    }
    let mut other = event("span.failed");
    other["attributes"] = json!({"agent.kind":"cli_agent","error.category":"credit"});
    f.record(other);
    assert_eq!(
        f.store.inference_failures(),
        json!({"llm":{"anthropic":{"category":"rate_limit","timestamp":"2026-10-08T19:10:00+09:00","status_code":429,"response":"rate_limit answer"}},"jev":{"category":"authentication","timestamp":"2026-10-08T10:00:00Z","status_code":429,"response":"authentication answer"}})
    );
}
#[test]
fn test_no_records_read_as_no_turns_and_no_failures() {
    let f = Fixture::new();
    assert_eq!(f.store.last_cli_agent_turns(), json!([]));
    assert_eq!(f.store.inference_failures(), json!({"llm":{},"jev":null}));
}
struct Fixture {
    dir: tempfile::TempDir,
    store: Diagnostics,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let store = Diagnostics::open(dir.path());
        Self { dir, store }
    }
    fn record(&self, r: Value) {
        self.store.record(r.as_object().unwrap().clone());
    }
    fn list(&self) -> Vec<Value> {
        self.view(&json!({}), &[], None)
    }
    fn view(&self, runtime: &Value, problems: &[Value], dismiss: Option<&str>) -> Vec<Value> {
        SystemAlerts::list(&self.store, runtime, problems, dismiss)["alerts"]
            .as_array()
            .unwrap()
            .clone()
    }
    fn dismiss(&self, id: &str) {
        self.view(&json!({}), &[], Some(id));
    }
    fn path(&self) -> std::path::PathBuf {
        self.dir
            .path()
            .join(".guildbotics/local/run/diagnostics.jsonl")
    }
}
fn event(kind: &str) -> Value {
    json!({"kind":"event","type":kind,"timestamp":"2026-07-11T10:00:00+09:00","trace_id":"trace-1","person_id":"alice","command":"workflows/demo","payload":{}})
}
fn call(status: &str, service: &str, provider: &str, category: &str) -> Value {
    let mut r = event(&format!("span.{status}"));
    r["attributes"] = json!({"credential.provider":service,"llm.provider":provider,"error.category":category,"error.status_code":429,"error.response":format!("{category} answer")});
    r
}
fn check(event_type: &str, service: &str, status: &str) -> Value {
    let mut r = event(event_type);
    r["payload"] = json!({"checks":[{"section":service,"code":"llm_api_key","status":status,"person_id":"alice","credential":true,"context":{"provider":"openai"}}]});
    r
}
fn refused(provider: &str) -> Value {
    let mut r = event("credential.failed");
    r["payload"] = json!({"provider":provider});
    r
}
#[test]
fn test_credential_alert_opens_on_first_error_and_closes_on_success() {
    for p in ["github", "slack"] {
        let f = Fixture::new();
        f.record(check("diagnostics.completed", p, "error"));
        let a = f.list();
        assert_eq!(a.len(), 1);
        assert_eq!(
            (
                a[0]["id"].clone(),
                a[0]["code"].clone(),
                a[0]["provider"].clone(),
                a[0]["person_id"].clone(),
                a[0]["severity"].clone()
            ),
            (
                json!(format!("credential:{p}:alice")),
                json!("credential"),
                json!(p),
                json!("alice"),
                json!("critical")
            )
        );
        f.record(check("diagnostics.completed", p, "ok"));
        assert!(f.list().is_empty());
    }
}
#[test]
fn test_command_failure_warns_once_and_later_success_closes_it() {
    let f = Fixture::new();
    f.record(event("command.failed"));
    let mut r = event("command.failed");
    r["trace_id"] = "trace-2".into();
    f.record(r);
    let a = f.list();
    assert_eq!(a[0]["code"], "command_failed");
    assert_eq!(a[0]["severity"], "warning");
    assert_eq!(a[0]["occurrence_count"], 2);
    let mut r = event("command.finished");
    r["trace_id"] = "trace-3".into();
    r["command"] = "other".into();
    f.record(r);
    assert!(f.list().is_empty());
}
#[test]
fn test_unresolved_alert_survives_diagnostics_rotation_and_service_restart() {
    let f = Fixture::new();
    f.record(event("command.failed"));
    assert_eq!(f.list().len(), 1);
    std::fs::write(f.path(), "").unwrap();
    let restarted = Diagnostics::open(f.dir.path());
    assert_eq!(
        SystemAlerts::list(&restarted, &json!({}), &[], None)["alerts"][0]["code"],
        "command_failed"
    );
}
#[test]
fn test_rate_limit_is_not_closed_by_finish_from_same_trace() {
    let f = Fixture::new();
    f.record(event("workflow.rate_limited"));
    f.record(event("command.finished"));
    assert_eq!(f.list()[0]["code"], "rate_limited");
}
#[test]
fn test_rate_limit_replaces_command_failure_from_an_earlier_trace() {
    let f = Fixture::new();
    f.record(event("command.failed"));
    let mut r = event("workflow.rate_limited");
    r["trace_id"] = "trace-2".into();
    f.record(r);
    assert_eq!(f.list().len(), 1);
    assert_eq!(f.list()[0]["code"], "rate_limited");
}
#[test]
fn test_non_credential_diagnostic_error_does_not_open_credential_alert() {
    let f = Fixture::new();
    for p in ["github", "slack"] {
        let mut r = check("diagnostics.completed", p, "error");
        r["payload"]["checks"][0]["credential"] = false.into();
        f.record(r);
    }
    assert!(f.list().is_empty());
}
#[test]
fn test_llm_diagnostics_open_one_alert_per_provider() {
    let f = Fixture::new();
    let mut r = check("diagnostics.completed", "llm", "error");
    let mut bob = r["payload"]["checks"][0].clone();
    bob["person_id"] = "bob".into();
    r["payload"]["checks"].as_array_mut().unwrap().push(bob);
    f.record(r);
    let a = f.list();
    assert_eq!(a.len(), 1);
    assert_eq!(a[0]["id"], "credential:llm:openai");
    assert_eq!(a[0]["code"], "credential_llm");
    assert_eq!(a[0]["person_id"], "");
    assert_eq!(a[0]["command"], "openai");
    f.record(call("finished", "llm", "openai", ""));
    assert!(f.list().is_empty());
}
#[test]
fn test_finding_the_key_configured_closes_its_missing_key_alert() {
    let f = Fixture::new();
    f.record(check("diagnostics.completed", "llm", "error"));
    assert_eq!(f.list()[0]["id"], "credential:llm:openai");
    f.record(check("verify.completed", "llm", "ok"));
    assert!(f.list().is_empty());
}
#[test]
fn test_a_refused_inference_call_opens_the_providers_alert_until_one_succeeds() {
    for (service, provider, code) in [
        ("llm", "openai", "credential_llm"),
        ("jev", "", "credential_jev"),
    ] {
        for category in ["authentication", "credit"] {
            for first in [false, true] {
                let f = Fixture::new();
                if first {
                    f.record(event("command.failed"));
                }
                f.record(call("failed", service, provider, category));
                if !first {
                    f.record(event("command.failed"));
                }
                let a = f.list();
                assert_eq!(a.len(), 1);
                assert_eq!(a[0]["id"], format!("inference:{service}:{provider}"));
                assert_eq!(a[0]["code"], code);
                assert_eq!(a[0]["reason"], category);
                assert_eq!(a[0]["command"], provider);
                assert_eq!(a[0]["trace_id"], "trace-1");
                f.record(call("finished", "llm", "anthropic", ""));
                assert_eq!(f.list().len(), 1);
                f.record(call("finished", service, provider, ""));
                assert!(f.list().is_empty());
            }
        }
    }
}
#[test]
fn test_a_refusal_that_passes_by_itself_opens_no_alert() {
    for c in ["rate_limit", "other", ""] {
        let f = Fixture::new();
        f.record(call("failed", "llm", "openai", c));
        assert!(f.list().is_empty());
    }
}
#[test]
fn test_checking_the_key_is_configured_never_settles_a_refusal() {
    for checks in [
        vec![("verify.completed", "ok")],
        vec![("verify.completed", "error"), ("verify.completed", "ok")],
        vec![("verify.completed", "")],
        vec![
            ("diagnostics.completed", "error"),
            ("verify.completed", "ok"),
        ],
        vec![("diagnostics.completed", "")],
    ] {
        let f = Fixture::new();
        f.record(call("failed", "llm", "openai", "credit"));
        for (t, s) in checks {
            let mut r = check(t, "llm", s);
            if s.is_empty() {
                r["payload"]["checks"] = json!([]);
            }
            f.record(r);
        }
        let reasons: Vec<_> = f
            .list()
            .into_iter()
            .filter(|a| a["reason"] != "")
            .map(|a| a["reason"].clone())
            .collect();
        assert_eq!(reasons, vec![json!("credit")]);
        f.record(call("finished", "llm", "openai", ""));
        assert!(f.list().is_empty());
    }
}
#[test]
fn test_full_diagnostics_keep_provider_alerts_of_no_member() {
    let f = Fixture::new();
    f.record(call("failed", "llm", "openai", "authentication"));
    let mut r = event("diagnostics.completed");
    r["payload"] = json!({"active_members":["alice"],"checks":[]});
    f.record(r);
    assert_eq!(f.list()[0]["id"], "inference:llm:openai");
}
#[test]
fn test_a_providers_refusal_opens_the_members_credential_alert() {
    for p in ["branvil", "github", "local", "slack"] {
        let f = Fixture::new();
        f.record(refused(p));
        let a = f.list();
        assert_eq!(a.len(), 1);
        assert_eq!(a[0]["id"], format!("credential:{p}:alice"));
        assert_eq!(a[0]["code"], "credential");
        assert_eq!(a[0]["provider"], p);
        assert_eq!(a[0]["person_id"], "alice");
    }
}
#[test]
fn test_full_diagnostics_closes_alert_for_removed_member() {
    let f = Fixture::new();
    f.record(refused("github"));
    assert_eq!(f.list().len(), 1);
    let mut r = event("diagnostics.completed");
    r["payload"] = json!({"active_members":[],"scope_person_id":""});
    f.record(r);
    assert!(f.list().is_empty());
}
#[test]
fn test_full_diagnostics_closes_the_alerts_of_providers_no_longer_used() {
    for providers in [Some(json!(["github"])), None] {
        let f = Fixture::new();
        f.record(refused("github"));
        f.record(refused("slack"));
        f.record(check("diagnostics.completed", "llm", "error"));
        assert_eq!(f.list().len(), 3);
        let mut r = event("diagnostics.completed");
        r["payload"] = json!({"active_members":["alice"],"scope_person_id":""});
        if let Some(p) = &providers {
            r["payload"]["providers"] = p.clone();
        }
        f.record(r);
        let ids: Vec<_> = f.list().into_iter().map(|a| a["id"].clone()).collect();
        assert_eq!(
            ids,
            if providers.is_some() {
                vec![
                    json!("credential:github:alice"),
                    json!("credential:llm:openai"),
                ]
            } else {
                vec![
                    json!("credential:github:alice"),
                    json!("credential:llm:openai"),
                    json!("credential:slack:alice"),
                ]
            }
        );
    }
}
#[test]
fn test_credential_failure_replaces_command_warning_for_same_trace() {
    for reverse in [false, true] {
        let f = Fixture::new();
        let mut records = vec![event("command.failed"), refused("github")];
        if reverse {
            records.reverse();
        }
        for r in records {
            f.record(r);
        }
        assert_eq!(f.list().len(), 1);
        assert_eq!(f.list()[0]["id"], "credential:github:alice");
    }
}
#[test]
fn test_cli_agent_diagnostics_do_not_create_member_credential_alerts() {
    let f = Fixture::new();
    f.record(refused("cli_agent"));
    assert!(f.list().is_empty());
}
#[test]
fn test_dismiss_hides_current_occurrence_and_later_failure_reopens() {
    let f = Fixture::new();
    f.record(event("command.failed"));
    let id = f.list()[0]["id"].as_str().unwrap().to_owned();
    f.dismiss(&id);
    assert!(f.list().is_empty());
    assert_eq!(
        SystemAlerts::list(&Diagnostics::open(f.dir.path()), &json!({}), &[], None)["alerts"],
        json!([])
    );
    let mut r = event("command.failed");
    r["trace_id"] = "trace-2".into();
    f.record(r);
    assert_eq!(f.list()[0]["code"], "command_failed");
}
#[test]
fn test_dismissed_failure_is_not_replayed_after_diagnostics_rotation() {
    let f = Fixture::new();
    f.record(event("command.failed"));
    let id = f.list()[0]["id"].as_str().unwrap().to_owned();
    f.dismiss(&id);
    let old = std::fs::read_to_string(f.path()).unwrap();
    let last = old.lines().last().unwrap();
    std::fs::write(
        f.path(),
        format!("{last}\n{{\"kind\":\"log\",\"message\":\"noise\"}}\n"),
    )
    .unwrap();
    assert!(f.list().is_empty());
}
#[test]
fn test_state_tracks_a_compact_incremental_cursor() {
    let f = Fixture::new();
    f.record(event("command.failed"));
    f.list();
    let s: Value = serde_json::from_slice(
        &std::fs::read(f.path().with_file_name("system-alerts.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(s["version"], 3);
    assert_eq!(s["cursor"]["offset"], f.path().metadata().unwrap().len());
    assert!(s.get("processed").is_none());
}
#[test]
fn test_hash_state_migrates_without_replaying_old_events() {
    let f = Fixture::new();
    f.record(event("command.failed"));
    let original = f.list();
    let path = f.path().with_file_name("system-alerts.json");
    let mut s: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    s["version"] = 2.into();
    s["processed"] = json!(["legacy-record-hash"]);
    s.as_object_mut().unwrap().remove("cursor");
    std::fs::write(&path, s.to_string()).unwrap();
    assert_eq!(f.list(), original);
    let s: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert_eq!(s["version"], 3);
    assert!(s.get("processed").is_none());
}
fn listening(chat: &str, failed: bool) -> Value {
    json!({"events":{"events_chat_provider":chat,"events_auth_failed_persons":if failed {vec!["alice"]}else{vec![]},"started_at":"2026-07-11T10:00:00+09:00"}})
}
#[test]
fn test_a_listener_refused_for_a_member_opens_its_credential_alert() {
    for chat in ["slack", "local"] {
        let f = Fixture::new();
        let a = f.view(&listening(chat, true), &[], None);
        assert_eq!(a.len(), 1);
        assert_eq!(a[0]["id"], format!("credential:{chat}:alice"));
        assert_eq!(a[0]["code"], "credential");
        assert_eq!(a[0]["provider"], chat);
        assert_eq!(a[0]["person_id"], "alice");
        assert_eq!(a[0]["severity"], "critical");
    }
}
#[test]
fn test_runtime_alert_dismissal_lasts_until_condition_resolves() {
    let f = Fixture::new();
    for chat in ["slack", "local"] {
        let r = listening(chat, true);
        let a = f.view(&r, &[], None);
        assert_eq!(a.len(), 1);
        assert!(
            f.view(&r, &[], Some(a[0]["id"].as_str().unwrap()))
                .is_empty()
        );
        assert!(f.view(&r, &[], None).is_empty());
    }
    assert!(f.view(&listening("local", false), &[], None).is_empty());
    assert_eq!(
        f.view(&listening("local", true), &[], None)[0]["id"],
        "credential:local:alice"
    );
    assert!(f.view(&listening("slack", true), &[], None).is_empty());
}
#[test]
fn test_worker_failure_persists_until_scheduler_restarts() {
    let f = Fixture::new();
    f.record(event("scheduler.worker.failed"));
    assert_eq!(f.list()[0]["code"], "worker_stopped");
    f.record(event("scheduler.running"));
    assert!(f.list().is_empty());
}
#[test]
fn test_environment_problems_open_one_alert_per_thing_to_fix() {
    let f = Fixture::new();
    let p = vec![
        json!({"person_id":"","slot":"","setting":"snapshot","reason":"missing"}),
        json!({"person_id":"","slot":"codex","setting":"tool","reason":"login"}),
        json!({"person_id":"aiko","slot":"default","setting":"grants","reason":"path"}),
        json!({"person_id":"kenji","slot":"default","setting":"grants","reason":"path"}),
    ];
    let a = f.view(&json!({}), &p, None);
    assert_eq!(a.len(), 4);
    for (id, code, person, slot, setting, reason) in [
        (
            "agent-environment:device",
            "agent_environment_unavailable",
            "",
            "",
            "snapshot",
            "missing",
        ),
        (
            "agent-environment:tool:codex",
            "agent_environment_tool_unavailable",
            "",
            "codex",
            "tool",
            "login",
        ),
        (
            "agent-environment:aiko:default",
            "agent_environment_slot_blocked",
            "aiko",
            "default",
            "grants",
            "path",
        ),
        (
            "agent-environment:kenji:default",
            "agent_environment_slot_blocked",
            "kenji",
            "default",
            "grants",
            "path",
        ),
    ] {
        let a = a.iter().find(|a| a["id"] == id).unwrap();
        assert_eq!(a["code"], code);
        assert_eq!(a["severity"], "warning");
        assert_eq!(a["person_id"], person);
        assert_eq!(a["command"], slot);
        assert_eq!(a["setting"], setting);
        assert_eq!(a["reason"], reason);
        assert_eq!(a["actions"], json!(["setup"]));
    }
    assert_eq!(
        f.view(&json!({}), &p, Some("agent-environment:device"))
            .len(),
        4
    );
    assert!(f.list().is_empty());
}
#[test]
fn test_credential_alert_closes_when_the_same_credential_is_verified_again() {
    let f = Fixture::new();
    f.record(refused("github"));
    let a = f.list();
    assert_eq!(a[0]["code"], "credential");
    assert_eq!(a[0]["provider"], "github");
    assert_eq!(a[0]["person_id"], "alice");
    f.dismiss("credential:github:alice");
    let mut r = event("credential.verified");
    r["payload"] = json!({"provider":"github"});
    f.record(r.clone());
    assert!(f.list().is_empty());
    f.record(refused("github"));
    r["person_id"] = "bob".into();
    f.record(r);
    assert_eq!(f.list()[0]["id"], "credential:github:alice");
}
#[test]
fn test_cli_authentication_command_failure_does_not_leave_an_execution_alert() {
    let f = Fixture::new();
    let mut r = event("command.failed");
    r["payload"] = json!({"code":"cli_agent_authentication"});
    f.record(r);
    let a = f.view(
        &json!({}),
        &[json!({"slot":"codex","setting":"tool","reason":"Log in again"})],
        None,
    );
    assert_eq!(a.len(), 1);
    assert_eq!(a[0]["id"], "agent-environment:tool:codex");
    assert!(f.list().is_empty());
}
