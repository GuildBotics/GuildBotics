//! The workspace registration is the source of the code host scope.
use super::*;
use crate::setup_providers::Registry;

#[tokio::test]
async fn diagnostics_check_the_execution_workspace_registration() {
    let f = fixture();
    let path = f.root.join("branvil");
    branvil::test_support::initialize(&path, "owner1").unwrap();
    let running = branvil::test_support::Running::start(
        &path,
        std::sync::Arc::new(branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    f.api.branvil.set(running.data.clone()).ok().unwrap();
    let registry = Registry(Some(f.api.clone()));
    let services =
        json!({"ticket_service":{"name":"branvil"},"code_hosting_service":{"name":"branvil"}});
    let root = f.workspace();
    let checks = registry.diagnose_branvil(&services, &root, "en").await;
    assert_eq!(checks[0].status, "error");
    running
        .data
        .owner()
        .assign_execution_root("acme".into(), root.clone())
        .await
        .unwrap();
    for lang in ["en", "ja"] {
        let checks = registry.diagnose_branvil(&services, &root, lang).await;
        assert_eq!(checks.len(), 1);
        assert_eq!(
            (checks[0].target.as_str(), checks[0].status.as_str()),
            ("acme", "ok")
        );
    }
    assert!(
        registry
            .diagnose_branvil(&json!({"chat_service":{"name":"slack"}}), &root, "en")
            .await
            .is_empty()
    );
    running.stop().await.unwrap();
}
