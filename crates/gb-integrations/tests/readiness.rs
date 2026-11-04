use gb_integrations::*;
use gb_ports::*;
use serde_json::{Value, json};

fn run(input: &Value) -> Result<Readiness, ServiceError> {
    let f = &input["freshness"];
    readiness_of(
        serde_json::from_value(input["target"].clone()).unwrap(),
        input["head_sha"].as_str().unwrap().into(),
        input["current_head_sha"].as_str().unwrap().into(),
        serde_json::from_value(input["checks"].clone()).unwrap(),
        input["checks_expected"].as_bool().unwrap(),
        (!f.is_null()).then(|| Freshness {
            base_sha: f["base_sha"].as_str().unwrap().into(),
            behind_by: f["behind_by"].as_i64().unwrap(),
            current_base_sha: f["current_base_sha"].as_str().unwrap().into(),
            requires_up_to_date: f["requires_up_to_date"].as_bool().unwrap(),
        }),
        serde_json::from_value(input["failed_logs"].clone()).unwrap(),
    )
}

#[test]
fn frozen_python_decisions() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/readiness.json")).unwrap();
    assert_eq!(cases.len(), 2432);
    for (i, case) in cases.iter().enumerate() {
        assert_eq!(
            serde_json::to_value(run(&case["input"]).unwrap()).unwrap(),
            case["expected"],
            "case {i}"
        );
    }
}

fn log_input(text: &str) -> Value {
    let log = json!({"job_id":"1", "name":"ci", "conclusion":"failure", "url":"", "artifact_names":[], "log":text, "log_bytes":text.len(), "tail_limit_bytes":MAX_LOG_TAIL_BYTES, "truncated":false});
    json!({"target":{"kind":"pull_request", "url":"local:pr/1", "title":"PR 🦀", "label":"!1"}, "head_sha":"head", "current_head_sha":"head", "checks":[], "checks_expected":false, "freshness":null, "failed_logs":[log.clone(), log]})
}

#[test]
fn frozen_python_log_budgets() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/logs.json")).unwrap();
    assert_eq!(cases.len(), 8);
    for case in cases {
        let text = case["text"]
            .as_str()
            .unwrap()
            .repeat(case["repeats"].as_u64().unwrap() as usize);
        let mut input = log_input(&text);
        for log in input["failed_logs"].as_array_mut().unwrap() {
            log["artifact_names"] = json!(["trace 🦀"]);
            log["truncated"] = case["already_truncated"].clone();
        }
        let result = run(&input).unwrap();
        assert_eq!(
            page_bytes(&result).unwrap(),
            case["page_bytes"].as_u64().unwrap() as usize
        );
        let mut expected = case["expected"].clone();
        let chars: Vec<_> = text.chars().collect();
        for (i, log) in expected["failed_logs"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .enumerate()
        {
            let count = case["lengths"][i].as_u64().unwrap() as usize;
            log["log"] = json!(chars[chars.len() - count..].iter().collect::<String>());
        }
        assert_eq!(serde_json::to_value(result).unwrap(), expected);
    }
}

#[test]
fn logs_fit_the_guest_page_preserving_unicode_suffixes() {
    for text in [
        "🦀日本\"\\\n\u{0}".repeat(MAX_LOG_TAIL_BYTES),
        "a".repeat(MAX_PAGE_BYTES),
    ] {
        let result = run(&log_input(&text)).unwrap();
        assert!(page_bytes(&result).unwrap() <= MAX_PAGE_BYTES);
        assert!(
            serde_json::to_vec(
                &json!({"items":[result.clone()], "continuation":null, "target":result.target})
            )
            .unwrap()
            .len()
                <= MAX_PAGE_BYTES
        );
        for log in result.failed_logs.unwrap() {
            assert!(text.ends_with(&log.log));
            assert!(!log.log.is_empty());
            assert!(log.truncated);
            assert_eq!(log.log_bytes, log.log.len() as i64);
            assert!(log.tail_limit_bytes <= log.log_bytes);
        }
    }
}

#[test]
fn empty_and_short_logs_and_oversized_metadata() {
    for text in ["", "line 🦀\n\"\\"] {
        let result = run(&log_input(text)).unwrap();
        for log in result.failed_logs.unwrap() {
            assert_eq!(log.log, text);
            assert_eq!(log.log_bytes, text.len() as i64);
            assert!(!log.truncated);
            assert_eq!(log.tail_limit_bytes, MAX_LOG_TAIL_BYTES as i64);
        }
    }
    let mut input = log_input("tail");
    input["failed_logs"][0]["name"] = json!("🦀".repeat(MAX_PAGE_BYTES));
    assert!(matches!(run(&input), Err(ServiceError::InvalidResult)));
    input["failed_logs"] = json!([]);
    assert_eq!(run(&input).unwrap().failed_logs, Some(vec![]));
}

#[test]
fn primary_failures_and_download_budgets() {
    let checks: Vec<Check> = serde_json::from_value(json!([
        {"name":"cancelled", "status":"completed", "conclusion":"cancelled", "details_url":""},
        {"name":"failed", "status":"completed", "conclusion":"failure", "details_url":""},
        {"name":"stale", "status":"completed", "conclusion":"stale", "details_url":""}
    ]))
    .unwrap();
    assert_eq!(
        inspected(&checks)
            .iter()
            .map(|c| c.name.as_str())
            .collect::<Vec<_>>(),
        vec!["failed"]
    );
    assert_eq!(inspected(&checks[..1]).len(), 1);
    assert!(inspected(&[]).is_empty());
    assert_eq!(
        effective_tail_bytes(MAX_PAGE_BYTES, 2),
        Ok(MAX_PAGE_BYTES / 2)
    );
    assert_eq!(effective_tail_bytes(10, 2), Ok(10));
    assert_eq!(effective_tail_bytes(10, MAX_PAGE_BYTES + 1), Ok(1));
    assert!(effective_tail_bytes(10, 0).is_err());
}
