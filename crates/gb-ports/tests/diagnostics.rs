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
