use gb_base::shared_writes::SharedWrites;
use gb_config::secrets::{DeviceKey, Keychain, Scope, SecretStore};
use gb_integrations::{IntegrationConfig, ServiceIntegrationFactory};
use gb_ports::*;
use std::{
    collections::BTreeMap,
    fs,
    sync::{Arc, Mutex},
};
#[derive(Default)]
struct FakeKeychain(Mutex<Option<String>>);
impl Keychain for FakeKeychain {
    fn read(&self) -> anyhow::Result<Option<String>> {
        Ok(self.0.lock().unwrap().clone())
    }
    fn create(&self, key: &str) -> anyhow::Result<()> {
        *self.0.lock().unwrap() = Some(key.into());
        Ok(())
    }
}
fn config(root: &std::path::Path) -> IntegrationConfig {
    IntegrationConfig {
        local_root: root.join("services"),
        identity_root: root.join("identity"),
        person_id: "aiko".into(),
        display_name: "Aiko".into(),
        accounts: BTreeMap::new(),
        ticket_provider: "local".into(),
        code_provider: "local".into(),
        chat_provider: "local".into(),
        ticket_scope: "acme".into(),
        code_scope: "acme".into(),
        checks: "success".into(),
        slack_base_url: String::new(),
    }
}
#[tokio::test]
async fn public_factory_constructs_local_services_and_shares_listener_identity() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().join("identity"), Some(d.path().join("workspace")));
    let key = FakeKeychain::default();
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: d.path().join("machine"),
            keychain: &key,
        },
    )
    .unwrap();
    let factory =
        ServiceIntegrationFactory::new(config(d.path()), &secrets, None, Arc::new(|_| {})).unwrap();
    let chat = factory.create_chat_service().unwrap();
    assert_eq!(chat.get_bot_identity().await.unwrap().user_id, "local-aiko");
    factory.create_ticket_service().unwrap();
    factory.create_code_hosting_service().unwrap();
    let mut another = config(d.path());
    another.person_id = "bob".into();
    let another =
        ServiceIntegrationFactory::new(another, &secrets, None, Arc::new(|_| {})).unwrap();
    assert_eq!(
        factory.listener_key().unwrap(),
        another.listener_key().unwrap()
    );
    let listener = factory.create_event_listener(Arc::new(|| {})).unwrap();
    listener.start().await.unwrap();
    assert!(listener.connected());
    listener.stop().await.unwrap();
    assert!(!listener.connected());
}
#[test]
fn factory_refuses_workspace_secrets_and_unknown_providers() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().join("identity"), Some(d.path().join("workspace")));
    let key = FakeKeychain::default();
    let workspace = SecretStore::new(
        &writes,
        Scope::Workspace,
        DeviceKey {
            machine_root: d.path().join("machine"),
            keychain: &key,
        },
    )
    .unwrap();
    assert!(
        ServiceIntegrationFactory::new(config(d.path()), &workspace, None, Arc::new(|_| {}))
            .is_err()
    );
    let identity = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: d.path().join("machine"),
            keychain: &key,
        },
    )
    .unwrap();
    let mut c = config(d.path());
    c.ticket_provider = "unknown".into();
    c.code_provider = "unknown".into();
    c.chat_provider = "unknown".into();
    let f = ServiceIntegrationFactory::new(c, &identity, None, Arc::new(|_| {})).unwrap();
    assert!(matches!(
        f.create_ticket_service(),
        Err(ServiceError::Unsupported)
    ));
    assert!(matches!(
        f.create_code_hosting_service(),
        Err(ServiceError::Unsupported)
    ));
    assert!(matches!(
        f.create_chat_service(),
        Err(ServiceError::Unsupported)
    ));
    assert!(matches!(
        f.create_event_listener(Arc::new(|| {})),
        Err(ServiceError::Unsupported)
    ));
}
#[test]
fn member_scoped_secrets_and_collision_withholding() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().join("identity"), Some(d.path().join("workspace")));
    let key = FakeKeychain::default();
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: d.path().join("machine"),
            keychain: &key,
        },
    )
    .unwrap();
    secrets
        .set("SLACK_APP_TOKEN", "unscoped-test-value")
        .unwrap();
    secrets
        .set("AIKO_SLACK_APP_TOKEN", "aiko-test-value")
        .unwrap();
    secrets
        .set("BOB_SLACK_APP_TOKEN", "bob-test-value")
        .unwrap();
    let mut c = config(d.path());
    c.chat_provider = "slack".into();
    let a = ServiceIntegrationFactory::new(c.clone(), &secrets, None, Arc::new(|_| {})).unwrap();
    let a_key = a.listener_key().unwrap();
    assert!(!a_key.contains("test-value"));
    c.person_id = "bob".into();
    let b = ServiceIntegrationFactory::new(c.clone(), &secrets, None, Arc::new(|_| {})).unwrap();
    assert_ne!(a_key, b.listener_key().unwrap());
    c.person_id = "nobody".into();
    let empty =
        ServiceIntegrationFactory::new(c.clone(), &secrets, None, Arc::new(|_| {})).unwrap();
    assert!(matches!(
        empty.listener_key(),
        Err(ServiceError::MissingCredential { .. })
    ));
    c.person_id = "aiko-bob".into();
    secrets
        .set("AIKO_BOB_SLACK_APP_TOKEN", "collision-test-value")
        .unwrap();
    fs::create_dir_all(
        c.identity_root
            .join(".guildbotics/config/team/members/aiko_bob"),
    )
    .unwrap();
    // Collision checks must use the store's root even if the caller supplies another.
    c.identity_root = d.path().join("unrelated-root");
    let collision = ServiceIntegrationFactory::new(c, &secrets, None, Arc::new(|_| {})).unwrap();
    assert!(matches!(
        collision.listener_key(),
        Err(ServiceError::Failed)
    ));
}
