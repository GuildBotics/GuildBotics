//! Branch assertions carried from app_api/test_trace_presentations.py.
use super::presentation;
use serde_json::{Value, json};
fn event(name: &str, payload: Value, attributes: Value) -> Value {
    json!({"kind":"event","type":name,"payload":payload,"attributes":attributes})
}

#[test]
fn frozen_presentations_preserve_messages_labels_tones_and_effort() {
    let mut cases = vec![
        (
            event(
                "command.failed",
                json!({"message":"Command was cancelled."}),
                json!({}),
            ),
            json!({"message":"Command was cancelled.","tone":"danger"}),
        ),
        (
            event(
                "command.deferred",
                json!({"message":"Service unavailable."}),
                json!({}),
            ),
            json!({"label_key":"diagnostics.executions.eventTypes.command_deferred","message":"Service unavailable.","tone":"info"}),
        ),
        (
            event(
                "ticket.comment",
                json!({"ticket":{"label":"#313"}}),
                json!({"target.label":"#313"}),
            ),
            json!({"message":"#313","label_key":"diagnostics.executions.eventTypes.ticket_comment"}),
        ),
        (
            event(
                "pull_request.item",
                json!({"action":"opened","pull_request":{"label":"!3"}}),
                json!({}),
            ),
            json!({"message":"!3"}),
        ),
        (
            event(
                "repository.push",
                json!({"ref":"refs/heads/ticket/T-1","commits":[]}),
                json!({"target.label":"#9"}),
            ),
            json!({"message":"ticket/T-1"}),
        ),
        (
            event(
                "agent_runtime.assistant",
                json!({"name":"partial","message":"Hello","partial":true}),
                json!({}),
            ),
            json!({"label_key":"diagnostics.executions.agentRuntime.assistant_partial","message":"Hello"}),
        ),
        (
            event(
                "span.finished",
                json!({"model":"claude-code","duration_ms":24100}),
                json!({}),
            ),
            json!({"message":"claude-code · 24.1s"}),
        ),
        (
            event(
                "span.finished",
                json!({"model":"claude-sonnet-5","duration_ms":24100,"effort":"high"}),
                json!({}),
            ),
            json!({"message":"claude-sonnet-5 · high · 24.1s"}),
        ),
        (
            event(
                "workflow.completed",
                json!({"run_id":"run-1","attempt":2}),
                json!({}),
            ),
            json!({"message_key":"diagnostics.executions.messages.workflow_completed","message_params":{"run":"run-1","attempt":2,"max_attempts":0,"retry_at":""}}),
        ),
        (
            event(
                "chat_dispatch.retry_scheduled",
                json!({"run_id":"run-1","attempt_count":2,"max_attempts":3,"next_attempt_at":"2026-07-18T15:00:00+09:00"}),
                json!({}),
            ),
            json!({"message_key":"diagnostics.executions.messages.chat_dispatch_retry_scheduled","message_params":{"run":"run-1","attempt":2,"max_attempts":3,"retry_at":"2026-07-18T15:00:00+09:00"}}),
        ),
        (
            json!({"kind":"log","level":"INFO","message":"working"}),
            json!({"tone":"neutral","message":"working"}),
        ),
        (
            event(
                "decision.evaluated",
                json!({"brain":"chat_decision","model":"jev-1.13.0","route":"agent","reason":"invalid","error":"authentication_error"}),
                json!({}),
            ),
            json!({"message":"chat_decision · jev-1.13.0 · agent · invalid · authentication_error","tone":"warning"}),
        ),
        (
            event("plugin.custom", json!("invalid"), json!({})),
            json!({"label_key":"diagnostics.executions.eventTypes.unknown","message":"plugin.custom"}),
        ),
        (
            event(
                "target.scope_refused",
                json!({"target":"POST /repos/other/demo/issues","scope_owner":"acme"}),
                json!({}),
            ),
            json!({"message":"POST /repos/other/demo/issues","tone":"danger"}),
        ),
        (
            event(
                "target.recorded",
                json!({"pull_request":{"label":"#544"}}),
                json!({"target.title":"診断ログの実行タイトル","target.action":"inspected"}),
            ),
            json!({"message":"#544 · 診断ログの実行タイトル"}),
        ),
        (
            event(
                "agent_environment.network_egress_candidate",
                json!({"candidates":[{"destination":"blocked.example","port":443},{"destination":"203.0.113.8"}]}),
                json!({}),
            ),
            json!({"label_key":"diagnostics.executions.eventTypes.network_egress_candidate","message":"blocked.example:443, 203.0.113.8","tone":"info"}),
        ),
        (
            json!({"kind":"io","type":"llm.request","payload":{"effort":{"requested":"HIGH","resolved":"high"}}}),
            json!({"effort":"high"}),
        ),
        (
            json!({"kind":"io","type":"llm.request","payload":{}}),
            json!({"effort":""}),
        ),
        (
            json!({"kind":"io","type":"cli_agent.request","payload":{"effort":{"requested":"high","resolved":""}}}),
            json!({"effort":""}),
        ),
        (
            event(
                "command.finished",
                json!({"effort":{"resolved":"high"}}),
                json!({}),
            ),
            json!({"effort":""}),
        ),
        (
            json!({"kind":"io","type":"command.output","payload":{"output":"result 日本語"}}),
            json!({"label_key":"diagnostics.executions.ioTypes.command_output","message":"result 日本語"}),
        ),
    ];
    for phase in ["started", "finished"] {
        let mut raw = event(
            &format!("member.command.{phase}"),
            json!({"command":"guildbotics member memory record"}),
            json!({}),
        );
        raw["command"] = "guildbotics member memory record".into();
        cases.push((raw,json!({"message":"guildbotics member memory record","label_key":format!("diagnostics.executions.eventTypes.command_{phase}")})));
    }
    for name in ["workflow.completion_missing", "chat_dispatch.abandoned"] {
        cases.push((
            event(
                name,
                json!({"error":"Completion evidence was not recorded."}),
                json!({}),
            ),
            json!({"message_key":"","message":"Completion evidence was not recorded."}),
        ));
    }
    for (name, label) in [
        ("thinking", "assistant_thinking"),
        ("completed", "assistant"),
    ] {
        cases.push((event("agent_runtime.assistant",json!({"name":name,"message":"Reply"}),json!({})),json!({"label_key":format!("diagnostics.executions.agentRuntime.{label}"),"message":"Reply"})));
    }
    for tool in ["codex", "claude"] {
        cases.push((
            event(
                "credential.failed",
                json!({"provider":"cli_agent","cli_agent":tool,"code":"authentication"}),
                json!({}),
            ),
            json!({"message_params":{"provider":tool,"code":"authentication"}}),
        ));
    }
    for (raw, expected) in cases {
        let actual = presentation(&raw);
        for (key, value) in expected.as_object().unwrap() {
            assert_eq!(&actual[key], value, "{key}: {raw}");
        }
    }
}

#[test]
fn frozen_rate_limit_presentations_resolve_reset_in_local_time() {
    // Nextest runs each test in its own process, before chrono reads TZ.
    unsafe {
        std::env::set_var("TZ", "Asia/Tokyo");
    }
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
            "",
            "Resets in 25h57m34s",
            "_with_hint",
            "Resets in 25h57m34s",
        ),
        ("", "", "", ""),
    ] {
        let result = presentation(&event(
            "workflow.rate_limited",
            json!({"retry_after_at":at,"retry_after_text":hint}),
            json!({}),
        ));
        assert_eq!(
            result["message_key"],
            format!("diagnostics.executions.messages.workflow_rate_limited{suffix}")
        );
        assert_eq!(result["message_params"]["retry_at"], display);
        assert_eq!(result["tone"], "warning");
    }
}

#[test]
fn indexed_events_and_production_emitters_have_deliberate_presentations() {
    use syn::visit::Visit;
    #[derive(Default)]
    struct Events(std::collections::BTreeSet<String>);
    impl<'a> Visit<'a> for Events {
        fn visit_item_mod(&mut self, item: &'a syn::ItemMod) {
            if !item.attrs.iter().any(|a| {
                a.path().is_ident("cfg")
                    && a.meta
                        .require_list()
                        .is_ok_and(|l| l.tokens.to_string() == "test")
            }) {
                syn::visit::visit_item_mod(self, item);
            }
        }
        fn visit_expr_method_call(&mut self, call: &'a syn::ExprMethodCall) {
            if ["event", "event_once", "publish"]
                .iter()
                .any(|name| call.method == *name)
                && let Some(syn::Expr::Lit(syn::ExprLit {
                    lit: syn::Lit::Str(s),
                    ..
                })) = call.args.first()
            {
                let value = s.value();
                if value.contains('.')
                    && value.split('.').all(|part| {
                        !part.is_empty() && part.chars().all(|c| c.is_ascii_lowercase() || c == '_')
                    })
                {
                    self.0.insert(value);
                }
            }
            syn::visit::visit_expr_method_call(self, call);
        }
    }
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("git")
        .args(["ls-files", "crates"])
        .current_dir(&repo)
        .output()
        .unwrap();
    assert!(output.status.success());
    let mut events = Events::default();
    for name in String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .filter(|p| {
            p.ends_with(".rs")
                && !p.contains("/tests/")
                && !p.contains("/src/test_")
                && !p.ends_with("/tests.rs")
                && !p.ends_with("_tests.rs")
        })
    {
        let text = std::fs::read_to_string(repo.join(name)).unwrap();
        if let Ok(tree) = syn::parse_file(&text) {
            events.visit_file(&tree);
        }
    }
    events.0.extend(
        crate::diagnostics::INDEX_EVENT_TYPES
            .iter()
            .map(|s| s.to_string()),
    );
    events
        .0
        .extend(["system.started".into(), "system.finished".into()]);
    assert!(!events.0.is_empty());
    for name in events.0 {
        // These frozen events were passed through constants, outside the old
        // literal scan. Keep their fallback display rather than inventing UI.
        if [
            "review_limit.started",
            "review_limit.finished",
            "review_limit.failed",
            "review_limit.deferred",
            "pull_request.review_limit",
            "work_stop.notified",
            "work_stop.suppressed",
        ]
        .contains(&name.as_str())
        {
            continue;
        }
        let result = presentation(&event(&name, json!({}), json!({})));
        assert_ne!(
            result["label_key"], "diagnostics.executions.eventTypes.unknown",
            "{name}"
        );
    }
    let mut canary = Events::default();
    canary.visit_file(
        &syn::parse_file("fn canary(){trace.event(\"plugin.new_event\", payload);}").unwrap(),
    );
    assert!(canary.0.contains("plugin.new_event"));
    assert_eq!(
        presentation(&event("plugin.new_event", json!({}), json!({})))["label_key"],
        "diagnostics.executions.eventTypes.unknown"
    );
}
