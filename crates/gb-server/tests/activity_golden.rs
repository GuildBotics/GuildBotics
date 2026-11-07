use serde_json::Value;

#[test]
fn timeline_and_diagnostics_agree_at_every_frozen_execution_stage() {
    use serde_json::json;
    for (events, state, expected) in [
        (vec!["span.finished"], "running", "running"),
        (
            vec!["span.finished", "command.finished"],
            "succeeded",
            "success",
        ),
        (vec!["span.finished", "command.failed"], "failed", "failed"),
        (vec!["span.failed", "command.deferred"], "deferred", "info"),
        (
            vec!["span.finished", "command.finished", "workflow.completed"],
            "succeeded",
            "success",
        ),
        (
            vec![
                "command.failed",
                "workflow.completion_missing",
                "chat_dispatch.retry_scheduled",
            ],
            "failed",
            "retry_scheduled",
        ),
        (
            vec!["workflow.rate_limited", "command.failed"],
            "failed",
            "rate_limited",
        ),
    ] {
        let workspace = tempfile::tempdir().unwrap();
        let diagnostics = gb_observability::Diagnostics::open(workspace.path());
        let attrs = json!({"target.url":"local://tickets/T-42","target.label":"T-42","target.kind":"ticket"});
        let mut facts = vec![];
        for (i, kind) in std::iter::once("command.started").chain(events).enumerate() {
            let record = json!({"kind":"event","type":kind,"trace_id":"t-chat","person_id":"alice","timestamp":format!("2026-07-01T10:00:{i:02}+00:00"),"source":"event_listener","command":"chat","attributes":attrs,"payload":{}});
            diagnostics.record(record.as_object().unwrap().clone());
            if gb_observability::activity::is_domain_activity_event(kind) {
                facts.push(record);
            }
        }
        let lifecycle = json!({"trace_id":"t-chat","person_id":"alice","source":"event_listener","command":"chat","status":state,"started_at":"2026-07-01T10:00:00+00:00","ended_at":"","attributes":attrs,"summary":"","has_evidence":false});
        let timeline = gb_observability::timeline::build(
            vec![json!({"person_id":"alice","name":"Alice","person_type":"agent","roles":[]})],
            vec![lifecycle],
            facts,
            |_| false,
            |key, params| gb_server::i18n::t("en", key, params),
        );
        assert_eq!(timeline["sessions"][0]["status"], expected);
        assert_eq!(
            diagnostics.trace_detail("t-chat").unwrap()["summary"]["status"],
            expected
        );
    }
}

#[test]
fn frozen_lifecycle_records_keep_summary_evidence_source_and_last_seen_time() {
    let cases: Vec<Value> = serde_json::from_str(include_str!(
        "../../../records/golden/activity-lifecycles.json"
    ))
    .unwrap();
    assert_eq!(cases.len(), 5);
    for case in cases {
        let r = &case["arguments"]["record"];
        let answer = if case["function"] == "lifecycle_from_run" {
            gb_observability::timeline::lifecycle_from_run(
                &serde_json::from_value(r.clone()).unwrap(),
            )
        } else {
            gb_observability::timeline::lifecycle_from_session(r)
        };
        assert_eq!(answer, case["expected"], "{}", case["test"]);
    }
}

#[test]
fn frozen_link_conditions_preserve_source_labels_read_only_actions_and_deduplication() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("../../../records/golden/activity-links.json")).unwrap();
    for case in cases {
        let args = &case["arguments"];
        let name = case["function"].as_str().unwrap();
        let answer = if name == "dedupe_links" {
            Value::Array(gb_observability::timeline::dedupe(
                args["links"].as_array().unwrap().clone(),
            ))
        } else {
            let mut record = args["record"].clone();
            if !record.is_object() {
                record = serde_json::json!({});
            }
            record["attributes"] = args["attributes"].clone();
            record["payload"] = args["payload"].clone();
            if let Some(timestamp) = args.get("timestamp") {
                record["timestamp"] = timestamp.clone();
            }
            let links = gb_observability::timeline::links(&record);
            match name {
                "links_from_attributes" => Value::Array(
                    links
                        .into_iter()
                        .filter(|l| l["kind"] != "doc" && l["kind"] != "commit")
                        .collect(),
                ),
                "links_from_record" => Value::Array(links),
                "doc_link_from_memory" => links
                    .into_iter()
                    .find(|l| l["kind"] == "doc")
                    .unwrap_or(Value::Null),
                "source_links_from_payload" => Value::Array(
                    links
                        .into_iter()
                        .filter(|l| l["kind"] == "external")
                        .collect(),
                ),
                "memory_diagnostics_url" => {
                    links.into_iter().find(|l| l["kind"] == "doc").unwrap()["url"].clone()
                }
                other => panic!("unknown frozen function {other}"),
            }
        };
        assert_eq!(answer, case["expected"], "{} {name}", case["test"]);
    }
}

#[test]
fn frozen_history_conditions_match_in_both_languages() {
    let cases: Vec<Value> = serde_json::from_str(include_str!(
        "../../../records/golden/activity-history.json"
    ))
    .unwrap();
    assert!(cases.len() >= 58);
    for case in cases {
        let language = case["language"].as_str().unwrap();
        let answer = gb_observability::timeline::build(
            case["members"].as_array().unwrap().clone(),
            case["lifecycles"].as_array().unwrap().clone(),
            case["records"].as_array().unwrap().clone(),
            |id| case["details"].as_array().unwrap().iter().any(|v| v == id),
            |key, params| gb_server::i18n::t(language, key, params),
        );
        assert_eq!(answer, case["expected"], "{}", case["test"]);
    }
}
