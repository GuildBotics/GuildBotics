#[test]
fn port_owned_locale_keys_are_provider_neutral() {
    let source = include_str!("../src/lib.rs");
    for literal in source.split('"').skip(1).step_by(2) {
        if literal.starts_with("integrations.") {
            assert!(literal.starts_with("integrations.resources."), "{literal}");
        }
    }
}

#[test]
fn diagnostic_carries_the_providers_key_and_safe_arguments_without_interpreting_them() {
    let error = gb_ports::ServiceError::Diagnostic {
        class: gb_ports::ServiceErrorClass::Failure,
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
    use gb_ports::ServiceError;
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
    use gb_ports::{ServiceError, ServiceErrorClass};
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
