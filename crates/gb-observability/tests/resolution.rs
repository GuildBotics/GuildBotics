use gb_observability::{
    TRACE_COMPLETED_EVENT_TYPES, TraceStatus, first_seen_attributes, resolve_trace_title,
};
use serde_json::{Value, json};

fn event(name: &str) -> Value {
    json!({"kind":"event", "type":name})
}
fn status(records: &[Value]) -> &'static str {
    let mut fold = TraceStatus::default();
    for record in records {
        fold.add(record);
    }
    fold.resolve()
}

#[test]
fn only_whole_trace_completion_can_succeed() {
    assert_eq!(status(&[]), "info");
    for completed in TRACE_COMPLETED_EVENT_TYPES {
        assert_eq!(status(&[event(completed)]), "success");
        assert_eq!(
            status(&[event("provider.failed"), event(completed)]),
            "failed"
        );
        assert_eq!(
            status(&[event(completed), event("provider.failed")]),
            "failed"
        );
    }
    for child in ["span.finished", "agent.finished", "provider.started"] {
        assert_eq!(status(&[event(child)]), "running");
    }
    assert_eq!(
        status(&[json!({"kind":"io","type":"command.finished"})]),
        "info"
    );
    assert_eq!(
        status(&[
            json!({"kind":"log","level":"critical"}),
            event("command.finished")
        ]),
        "failed"
    );
}

#[test]
fn workflow_evidence_and_dispatch_have_explicit_precedence() {
    for (events, expected) in [
        (
            &[
                "command.started",
                "span.finished",
                "workflow.completion_missing",
                "chat_dispatch.retry_scheduled",
            ][..],
            "retry_scheduled",
        ),
        (
            &[
                "command.started",
                "span.finished",
                "workflow.completion_missing",
            ][..],
            "incomplete",
        ),
        (
            &["span.finished", "chat_dispatch.abandoned"][..],
            "abandoned",
        ),
        (
            &[
                "span.finished",
                "workflow.completion_missing",
                "workflow.completed",
            ][..],
            "success",
        ),
        (&["cli_agent.failed", "workflow.completed"][..], "success"),
    ] {
        assert_eq!(
            status(&events.iter().map(|name| event(name)).collect::<Vec<_>>()),
            expected
        );
    }
    let ranked = [
        (event("chat_dispatch.abandoned"), "abandoned"),
        (event("workflow.rate_limited"), "rate_limited"),
        (event("chat_dispatch.retry_scheduled"), "retry_scheduled"),
        (event("workflow.completion_missing"), "incomplete"),
        (event("command.deferred"), "info"),
        (event("provider.failed"), "failed"),
    ];
    for (i, (strong, expected)) in ranked.iter().enumerate() {
        for (weak, _) in &ranked[i + 1..] {
            if strong["type"]
                .as_str()
                .unwrap()
                .starts_with("chat_dispatch.")
                && weak["type"].as_str().unwrap().starts_with("chat_dispatch.")
            {
                continue;
            }
            assert_eq!(status(&[strong.clone(), weak.clone()]), *expected);
            assert_eq!(status(&[weak.clone(), strong.clone()]), *expected);
        }
    }
    assert_eq!(
        status(&[event("provider.failed"), event("workflow.completed")]),
        "success"
    );
    assert_eq!(
        status(&[
            json!({"kind":"event","type":"workflow.completed","payload":{"status":"awaiting_checks"}})
        ]),
        "awaiting_checks"
    );
    for (lifecycle, expected) in [
        ("running", "running"),
        ("succeeded", "success"),
        ("success", "success"),
        ("cancelled", "failed"),
        ("interrupted", "failed"),
        ("failed", "failed"),
        ("result_unknown", "info"),
        ("deferred", "info"),
        ("unknown", "info"),
    ] {
        let mut fold = TraceStatus::default();
        fold.observe(lifecycle);
        assert_eq!(fold.resolve(), expected);
    }
}

#[test]
fn titles_follow_candidate_order_and_skip_read_only_payloads() {
    let records = [
        json!({"attributes":{"target.title":"first","target.label":"first"}}),
        json!({"attributes":{"target.title":"later","target.label":"later"}}),
    ];
    let attrs = first_seen_attributes(&records);
    assert_eq!(
        resolve_trace_title(
            &records,
            &attrs,
            "c",
            "w",
            "id",
            || panic!("must stay lazy"),
            |_| panic!()
        ),
        "first"
    );
    let choices = [
        json!({"attributes":{"memory.action":"recall"}, "payload":{"title":"read"}}),
        json!({"attributes":{"target.action":"inspected"}, "payload":{"title":"inspected"}}),
        json!({"payload":{"title":"written", "fields":{"prompt":"prompt", "brain":"brain", "cli_agent":"agent"}}}),
        json!({"type":"fallback event"}),
    ];
    for (attrs, summary, expected) in [
        (json!({}), "\n summary\nrest", "summary"),
        (json!({"memory.title":"memory"}), "", "memory"),
        (json!({"target.label":"#1"}), "", "written"),
    ] {
        assert_eq!(
            resolve_trace_title(
                &choices,
                attrs.as_object().unwrap(),
                "c",
                "w",
                "id",
                || summary.into(),
                |_| panic!()
            ),
            expected
        );
    }
    let attrs = json!({"target.kind":"pull_request", "target.label":"!2"});
    assert_eq!(
        resolve_trace_title(
            &[],
            attrs.as_object().unwrap(),
            "",
            "",
            "id",
            String::new,
            |_| panic!()
        ),
        "PR !2"
    );
    let attrs = json!({"chat.provider":"slack"});
    assert_eq!(
        resolve_trace_title(
            &choices[2..],
            attrs.as_object().unwrap(),
            "",
            "",
            "id",
            String::new,
            |_| "localized trigger".into()
        ),
        "written"
    );
    assert_eq!(
        resolve_trace_title(
            &[],
            attrs.as_object().unwrap(),
            "",
            "",
            "id",
            String::new,
            |_| "localized trigger".into()
        ),
        "localized trigger"
    );
    let empty = serde_json::Map::new();
    for (records, c, w, expected) in [
        (&choices[2..], "c", "w", "written"),
        (&choices[3..], "c", "w", "w"),
        (&choices[3..], "c", "", "c"),
        (&choices[3..], "", "", "fallback event"),
        (&choices[0..0], "", "", "id"),
    ] {
        assert_eq!(
            resolve_trace_title(records, &empty, c, w, "id", String::new, |_| panic!()),
            expected
        );
    }
}

#[test]
fn title_fields_fall_through_in_order() {
    let empty = serde_json::Map::new();
    let mut record = json!({"payload":{"fields":{"prompt":"prompt","brain":"brain","cli_agent":"agent"}},"message":"message"});
    for (key, expected) in [
        ("prompt", "prompt"),
        ("brain", "brain"),
        ("cli_agent", "agent"),
    ] {
        assert_eq!(
            resolve_trace_title(
                &[record.clone()],
                &empty,
                "",
                "",
                "id",
                String::new,
                |_| panic!()
            ),
            expected
        );
        record["payload"]["fields"]
            .as_object_mut()
            .unwrap()
            .remove(key);
    }
    assert_eq!(
        resolve_trace_title(&[record], &empty, "", "", "id", String::new, |_| panic!()),
        "message"
    );
    let attrs = json!({"memory.doc_id":"doc"});
    assert_eq!(
        resolve_trace_title(
            &[],
            attrs.as_object().unwrap(),
            "",
            "",
            "id",
            String::new,
            |_| panic!()
        ),
        "doc"
    );
}

#[test]
fn reader_uses_workflow_evidence_and_earliest_target_despite_append_order() {
    let root = tempfile::tempdir().unwrap();
    let store = gb_observability::Diagnostics::open(root.path());
    for record in [
        json!({"kind":"event","type":"target.recorded","trace_id":"t","timestamp":"2026-11-03T10:00:02Z","attributes":{"target.title":"later","target.label":"later"}}),
        json!({"kind":"event","type":"target.recorded","trace_id":"t","timestamp":"2026-11-03T10:00:01Z","attributes":{"target.title":"first","target.label":"first"}}),
        json!({"kind":"event","type":"workflow.completed","trace_id":"t","timestamp":"2026-11-03T10:00:03Z","payload":{"status":"awaiting_checks"}}),
    ] {
        store.record(record.as_object().unwrap().clone());
    }
    let traces = store.traces(None, None, None, None, None, 100).unwrap();
    assert_eq!(traces["traces"][0]["status"], "awaiting_checks");
    assert_eq!(traces["traces"][0]["title"], "first");
    let detail = store.trace_detail("t").unwrap();
    assert_eq!(detail["summary"]["title"], "first");
    assert_eq!(detail["summary"]["attributes"]["target.label"], "first");
    let by_label = |label| {
        store
            .traces(None, None, None, Some("target.label"), Some(label), 100)
            .unwrap()
    };
    assert_eq!(by_label("first")["traces"].as_array().unwrap().len(), 1);
    assert_eq!(by_label("later")["traces"], json!([]));
}
