//! Localized Branvil workspace diagnostics.
use super::*;
use crate::setup_providers::Registry;

fn services(code: &str, tickets: &str) -> Value {
    json!({"code_hosting_service":{"name":"branvil","workspace":code},"ticket_service":{"name":"branvil","workspace":tickets}})
}

#[tokio::test]
async fn diagnostics_check_each_distinct_workspace_in_requested_language() {
    let f = fixture();
    let path = f.root.join("branvil");
    branvil::api::initialize(&path, "owner1").unwrap();
    let running = branvil::api::Running::start(
        &path,
        std::sync::Arc::new(branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    f.api.branvil.set(running.data.clone()).ok().unwrap();
    running
        .data
        .owner()
        .repos_create_workspace(branvil::test_support::input(json!({"name":"acme"})))
        .await
        .unwrap();
    let registry = Registry(Some(f.api.clone()));
    for lang in ["en", "ja"] {
        let checks = registry
            .diagnose_branvil(&services("acme", "absent"), lang)
            .await;
        let found: Vec<_> = checks
            .iter()
            .map(|c| (c.code.as_str(), c.target.as_str(), c.status.as_str()))
            .collect();
        assert_eq!(
            found,
            [
                ("branvil_workspace", "acme", "ok"),
                ("branvil_workspace", "absent", "error")
            ]
        );
        assert_eq!(
            checks[0].message,
            crate::i18n::t(
                lang,
                "integrations.branvil.provider.workspace",
                &[("workspace", "acme")]
            )
        );
        assert!(!checks[1].message.contains("integrations.branvil"));
        assert!(!checks[1].message.contains("%{"));
        // One workspace named by both services is checked once.
        assert_eq!(
            registry
                .diagnose_branvil(&services("acme", "acme"), lang)
                .await
                .len(),
            1
        );
    }
    // Services on other providers ask nothing of Branvil.
    assert!(
        registry
            .diagnose_branvil(&json!({"chat_service":{"name":"slack"}}), "en")
            .await
            .is_empty()
    );
    running.stop().await.unwrap();
}

#[tokio::test]
async fn a_stopped_branvil_fails_each_workspace_check() {
    let f = fixture();
    let registry = Registry(Some(f.api.clone()));
    for lang in ["en", "ja"] {
        let checks = registry
            .diagnose_branvil(&services("acme", "other"), lang)
            .await;
        assert_eq!(checks.len(), 2);
        for (check, target) in checks.iter().zip(["acme", "other"]) {
            assert_eq!(
                (
                    check.code.as_str(),
                    check.status.as_str(),
                    check.target.as_str()
                ),
                ("branvil_workspace", "error", target)
            );
            assert_eq!(
                check.message,
                crate::i18n::t(lang, "integrations.branvil.api.stopped", &[])
            );
        }
    }
}
