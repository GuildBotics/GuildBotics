use branvil::agent::pr_readiness::*;
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
            conflicts: serde_json::from_value(f["conflicts"].clone()).unwrap_or_default(),
        }),
        Feedback::default(),
        serde_json::from_value(input["failed_logs"].clone()).unwrap(),
    )
}

#[test]
fn frozen_python_decisions() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/readiness.json")).unwrap();
    assert_eq!(cases.len(), 2435);
    for (i, case) in cases.iter().enumerate() {
        // The frozen decisions predate feedback, which never changes them.
        let mut expected = case["expected"].clone();
        expected["feedback"] = json!(Feedback::default());
        assert_eq!(
            serde_json::to_value(run(&case["input"]).unwrap()).unwrap(),
            expected,
            "case {i}"
        );
    }
}

fn said(author: &str, id: &str, at: &str) -> Said {
    Said {
        author: author.into(),
        id: id.into(),
        at: at.into(),
        notice: false,
    }
}

#[test]
fn feedback_counts_others_and_names_their_latest_remark_of_each_kind() {
    let notice = Said {
        notice: true,
        ..said("aiko", "notice", "9")
    };
    let empty = feedback_of("me", vec![], vec![said("me", "own", "9"), notice], vec![]);
    assert_eq!(empty, Feedback::default());
    let remarks = || {
        vec![
            said("aiko", "r1", "2026-11-17T10:00:00Z"),
            said("me", "own", "2026-11-17T12:00:00Z"),
            said("kenji", "r2", "2026-11-17T11:00:00Z"),
            said("aiko", "r0", "2026-11-17T09:00:00Z"),
        ]
    };
    let latest = Remarks {
        count: 3,
        latest: Some(Remark {
            id: "r2".into(),
            at: "2026-11-17T11:00:00Z".into(),
        }),
    };
    assert_eq!(
        feedback_of("me", remarks(), remarks(), remarks()),
        Feedback {
            reviews: latest.clone(),
            comments: latest.clone(),
            thread_comments: latest,
        }
    );
    let kinds = feedback_of(
        "me",
        vec![said("aiko", "review", "1")],
        vec![],
        vec![said("aiko", "first", "2"), said("kenji", "tie", "2")],
    );
    assert_eq!(kinds.reviews.count, 1);
    assert_eq!(kinds.comments, Remarks::default());
    assert_eq!(kinds.thread_comments.latest.unwrap().id, "tie");
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
    let error = run(&input).unwrap_err();
    assert_eq!(error.translation_key(), "integrations.resources.too_large");
    assert!(error.translation_args().is_empty());
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

#[test]
fn service_post_merge_skip_is_ready_with_other_successful_checks() {
    let mut input = log_input("");
    input["freshness"] = json!({"base_sha":"base", "current_base_sha":"base", "behind_by":0, "requires_up_to_date":true});
    input["failed_logs"] = json!([]);
    input["checks_expected"] = json!(true);
    input["checks"] = json!([
        {"name":"slow","status":"completed","conclusion":"skipped","details_url":"","details":"post-merge only (repository setting)"},
        {"name":"fast","status":"completed","conclusion":"success","details_url":""}
    ]);
    let result = run(&input).unwrap();
    assert_eq!(result.readiness, ReadinessReadiness::Ready);
    assert!(result.completion_blockers.is_empty());
}
