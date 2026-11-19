use gb_base::service::*;
use serde_json::json;

#[test]
fn rate_limit_reset_requires_an_explicit_zone_and_preserves_unknown_holds() {
    let now = parse_timestamp("2026-01-02T00:00:00Z").unwrap();
    for (time, held) in [
        ("2026-01-02T00:00", true),
        ("20260101T2359", true),
        (" 2026-01-01T23:59+00:00 ", true),
        ("2026-01-01T23:59+00:00", false),
        ("20260102T0000Z", false),
        ("2026-W01-5T00:00+00:00", false),
        ("2026-01-02🦀00:00+00:00", false),
        ("20260102T0001", true),
        ("2026-W01-5T00:01+00:00", true),
        ("invalid", true),
    ] {
        let status=WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"rate_limited","person_id":"aiko","retry_after_at":time})).unwrap();
        assert_eq!(status.holds_selection(now), held, "{time}");
    }
}

#[test]
fn statuses_suppress_work_and_only_failures_and_rate_limits_hold_selection() {
    let now = "2026-11-04T12:00:00Z".parse().unwrap();
    for reason in [
        "completed",
        "work_remaining",
        "failed",
        "rate_limited",
        "review_limit",
        "awaiting_checks",
        "checks_settled",
        "asking",
        "blocked",
    ] {
        let status: WorkflowStatus = serde_json::from_value(json!({"reason":reason,"person_id":"member","run_id":"run","work_kind":"ticket","work_identity":"url"})).unwrap();
        assert_eq!(
            status.holds_selection(now),
            ["failed", "rate_limited"].contains(&reason)
        );
        assert_eq!(status.payload()["kind"], "workflow_error");
        assert_eq!(status.payload()["routing"], "suppress");
        assert!(status.payload().get("subject").is_none());
        assert_eq!(
            WorkflowStatus::from_payload(&status.payload()),
            Some(status)
        );
    }
    for (retry, expected) in [
        ("", true),
        ("invalid", true),
        ("2026-11-04T12:00:00", true),
        ("2026-11-04T12:00:00Z", false),
        ("2026-11-04T21:00:01+09:00", true),
        ("2026-11-03T12:00:00Z", false),
    ] {
        let status = WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"rate_limited","person_id":"member","retry_after_at":retry,"unknown":"ignored"})).unwrap();
        assert_eq!(status.holds_selection(now), expected, "{retry}");
    }
    for payload in [
        json!(null),
        json!({}),
        json!({"kind":"work"}),
        json!({"kind":"workflow_error","reason":"invalid","person_id":"m"}),
        json!({"kind":"workflow_error","reason":"awaiting_checks","person_id":"m"}),
        json!({"kind":"workflow_error","reason":"awaiting_checks","person_id":"m","work_kind":"ticket"}),
    ] {
        assert!(WorkflowStatus::from_payload(&payload).is_none());
    }
}

#[test]
fn diagnostic_carries_the_providers_key_and_safe_arguments_without_interpreting_them() {
    let error = ServiceError::Diagnostic {
        class: ServiceErrorClass::Failure,
        key: "example.credentials.missing",
        args: [("setting".into(), "MEMBER_KEY".into())].into(),
    };
    assert_eq!(error.translation_key(), "example.credentials.missing");
    assert_eq!(error.to_string(), "example.credentials.missing");
    assert_eq!(error.translation_args()["setting"], "MEMBER_KEY");
    let mut copy = error.translation_args();
    copy.clear();
    assert_eq!(error.translation_args().len(), 1);
    assert!(std::error::Error::source(&error).is_none());
}

#[test]
fn resource_diagnostics_preserve_recovery_arguments_and_categories() {
    use ServiceError;
    for (error, key, args, missing) in [
        (ServiceError::ResponseTooLarge, "too_large", vec![], false),
        (
            ServiceError::AttachmentMissing {
                url: "local://tickets/acme/1".into(),
                id: "report".into(),
            },
            "attachment_missing",
            vec![("url", "local://tickets/acme/1"), ("id", "report")],
            true,
        ),
        (
            ServiceError::AttachmentTooLarge {
                name: "report.txt".into(),
                limit: 1024,
            },
            "attachment_too_large",
            vec![("name", "report.txt"), ("limit", "1024")],
            false,
        ),
    ] {
        let expected = format!("integrations.resources.{key}");
        assert_eq!(error.translation_key(), expected);
        assert_eq!(error.to_string(), expected);
        assert_eq!(
            error.translation_args(),
            args.into_iter()
                .map(|(k, v)| (k.into(), v.into()))
                .collect()
        );
        assert_eq!(error.is_not_found(), missing);
        assert!(!error.is_unsupported());
        assert!(std::error::Error::source(&error).is_none());
    }
}

#[test]
fn unavailable_category_is_independent_of_provider_guidance() {
    use {ServiceError, ServiceErrorClass};
    assert!(ServiceError::Unavailable.is_unavailable());
    for class in [
        ServiceErrorClass::Failure,
        ServiceErrorClass::NotFound,
        ServiceErrorClass::Unsupported,
        ServiceErrorClass::Unavailable,
    ] {
        let error = ServiceError::Diagnostic {
            class,
            key: "example.service.retry",
            args: [("operation".into(), "read".into())].into(),
        };
        assert_eq!(
            error.is_unavailable(),
            class == ServiceErrorClass::Unavailable
        );
        assert_eq!(error.is_not_found(), class == ServiceErrorClass::NotFound);
        assert_eq!(
            error.is_unsupported(),
            class == ServiceErrorClass::Unsupported
        );
        assert_eq!(error.translation_key(), "example.service.retry");
        assert_eq!(error.translation_args()["operation"], "read");
    }
    for error in [
        ServiceError::Failed,
        ServiceError::NotFound,
        ServiceError::Unsupported,
        ServiceError::InvalidResult,
        ServiceError::OutOfScope,
    ] {
        assert!(!error.is_unavailable());
    }
}
