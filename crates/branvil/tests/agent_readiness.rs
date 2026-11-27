use branvil::agent::port::*;
use branvil::agent::pr_readiness::*;
use serde_json::{Value, json};

fn run(input: &Value) -> Result<Readiness, ServiceError> {
    let mut result = Readiness {
        target: serde_json::from_value(input["target"].clone()).unwrap(),
        report: branvil::api::pulls::ChecksView {
            head_sha: input["head_sha"].as_str().unwrap().into(),
            base_sha: String::new(),
            behind_by: None,
            conflicts: None,
            checks: vec![],
            required_checks: Some(RequiredChecks::Passed),
            required_names: vec![],
            checks_expected: false,
            base_checks: 0,
            merge_checks: None,
            post_merge_checks: None,
            base_failure: None,
        },
        feedback: Feedback::default(),
        failed_logs: serde_json::from_value(input["failed_logs"].clone()).unwrap(),
    };
    fit_log_tails(&mut result)?;
    Ok(result)
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
    json!({"target":{"kind":"pull_request", "url":"local:pr/1", "title":"PR 🦀", "label":"!1"}, "head_sha":"head", "checks":[], "failed_logs":[log.clone(), log]})
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
fn oversized_evidence_preserves_the_failure_logs_and_marks_what_was_omitted() {
    let mut input = log_input("tail");
    let evidence = json!({
        "sha": "head", "attempt": 2, "runner_deferral_count": 0, "runner_deferrals": {},
        "other_attempt_count": 0, "other_attempts": [], "test_failures": [],
    });
    input["failed_logs"][0]["evidence"] = evidence.clone();
    input["failed_logs"][1]["evidence"] = evidence.clone();
    input["failed_logs"][0]["evidence"]["runner"] = json!("🦀".repeat(MAX_PAGE_BYTES / 8));

    let result = run(&input).unwrap();
    assert!(page_bytes(&result).unwrap() <= MAX_PAGE_BYTES);
    let logs = serde_json::to_value(result.failed_logs.unwrap()).unwrap();
    assert_eq!(logs[0]["evidence_truncated"], true);
    assert!(logs[0].get("evidence").is_none());
    assert_eq!(logs[1]["evidence"]["sha"], "head");
    assert_eq!(logs[0]["log"], "tail");
    assert_eq!(logs[1]["log"], "tail");
}

#[test]
fn download_budgets() {
    assert_eq!(
        effective_tail_bytes(MAX_PAGE_BYTES, 2),
        Ok(MAX_PAGE_BYTES / 2)
    );
    assert_eq!(effective_tail_bytes(10, 2), Ok(10));
    assert_eq!(effective_tail_bytes(10, MAX_PAGE_BYTES + 1), Ok(1));
    assert!(effective_tail_bytes(10, 0).is_err());
}
