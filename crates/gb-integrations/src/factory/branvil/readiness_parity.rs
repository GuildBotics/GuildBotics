//! Normalize independent check and current-pull observations at the provider boundary.
use super::*;
use branvil::api::{Running, initialize, pulls};

fn report() -> pulls::ChecksView {
    let mut report = pulls::ChecksView {
        head_sha: "observed-head".into(),
        base_sha: "observed-base".into(),
        behind_by: Some(2),
        checks: vec![Default::default()],
        checks_expected: true,
        base_checks: 0,
    };
    let check = &mut report.checks[0];
    check.name = "test".into();
    check.status = "completed".into();
    check.conclusion = Some("success".into());
    check.details = Some("provider detail".into());
    check.details_url = Some("https://example.invalid/check".into());
    report
}

#[tokio::test]
async fn readiness_keeps_report_and_current_heads_distinct() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(::branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    let service = BranvilService::new(
        running.data.clone(),
        String::new(),
        "owner1".into(),
        "test".into(),
        "aiko".into(),
        std::sync::Arc::new(|_| {}),
    );
    for (head, base, codes) in [
        ("observed-head", "observed-base", vec![]),
        ("moved-head", "observed-base", vec!["head_changed"]),
        ("observed-head", "moved-base", vec!["base_changed"]),
        (
            "moved-head",
            "moved-base",
            vec!["head_changed", "base_changed"],
        ),
    ] {
        let current = json!({"url":"http://example.invalid/test/repo/-/pulls/1","label":"!1","title":"Current title","state":"open","head_sha":head,"base_sha":base});
        let normalized = service
            .readiness_result(report(), current, Some(vec![]))
            .unwrap();
        assert_eq!(normalized.head_sha, "observed-head");
        assert_eq!(normalized.current_head_sha, head);
        assert_eq!(normalized.base_sha.as_deref(), Some("observed-base"));
        assert_eq!(normalized.current_base_sha.as_deref(), Some(base));
        assert_eq!(normalized.behind_by, Some(2));
        assert_eq!(normalized.out_of_date, Some(true));
        assert!(normalized.checks_expected);
        assert_eq!(
            normalized.readiness,
            if codes.is_empty() {
                ReadinessReadiness::Ready
            } else {
                ReadinessReadiness::Blocked
            }
        );
        assert_eq!(
            normalized
                .completion_blockers
                .iter()
                .map(|b| b.code.as_str())
                .collect::<Vec<_>>(),
            codes
        );
        assert_eq!(normalized.target.kind, WorkKind::PullRequest);
        assert_eq!(normalized.target.title, "Current title");
        assert_eq!(normalized.target.label, "!1");
        assert_eq!(normalized.checks[0].name, "test");
        assert_eq!(
            normalized.checks[0].details.as_deref(),
            Some("provider detail")
        );
        assert_eq!(
            normalized.checks[0].details_url,
            "https://example.invalid/check"
        );
        assert_eq!(normalized.failed_logs, Some(vec![]));
    }
    let url = "http://example.invalid/test/repo/-/pulls/1";
    for (missing, expected) in [
        ("head", "integrations.branvil.api.no_head"),
        ("report_base", "integrations.branvil.api.no_base"),
        ("current_base", "integrations.branvil.api.no_base"),
    ] {
        let mut checks = report();
        let mut current =
            json!({"url":url,"state":"open","head_sha":"observed-head","base_sha":"observed-base"});
        match missing {
            "head" => checks.head_sha.clear(),
            "report_base" => checks.base_sha.clear(),
            _ => current["base_sha"] = json!(""),
        }
        assert_eq!(
            service.readiness_result(checks, current, None).unwrap_err(),
            diagnostic(expected, [("url", url.to_owned())])
        );
    }
    running.stop().await.unwrap();
}
