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
        branvil_members: Default::default(),
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
    let code = factory.create_code_hosting_service().unwrap();
    assert!(matches!(
        code.read(Resource::DependencyAlerts, "acme/demo", None, "")
            .await,
        Err(ServiceError::Unsupported)
    ));
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
    let member_dir = c
        .identity_root
        .join(".guildbotics/config/team/members/aiko_bob");
    // A leftover directory is not a configured identity.
    let stale =
        ServiceIntegrationFactory::new(c.clone(), &secrets, None, Arc::new(|_| {})).unwrap();
    assert!(stale.listener_key().is_ok());
    fs::write(member_dir.join("member.yml"), "{}").unwrap();
    assert!(stale.listener_key().is_ok());
    fs::create_dir(member_dir.join("identity.yml")).unwrap();
    assert!(stale.listener_key().is_ok());
    fs::remove_dir(member_dir.join("identity.yml")).unwrap();
    fs::write(member_dir.join("identity.yml"), "{}").unwrap();
    // Collision checks must use the store's root even if the caller supplies another.
    c.identity_root = d.path().join("unrelated-root");
    let collision = ServiceIntegrationFactory::new(c, &secrets, None, Arc::new(|_| {})).unwrap();
    let error = collision.listener_key().unwrap_err();
    assert_eq!(
        error,
        ServiceError::CredentialPrefixConflict {
            member: "aiko-bob".into(),
            conflicting_member: "aiko_bob".into(),
        }
    );
    assert_eq!(
        error.translation_key(),
        "integrations.resources.credential_prefix_conflict"
    );
    assert_eq!(
        error.translation_args(),
        BTreeMap::from([
            ("member".into(), "aiko-bob".into()),
            ("conflicting_member".into(), "aiko_bob".into()),
        ])
    );
    for locale in [
        include_str!("../../../guildbotics/templates/locales/integrations/resources.en.yml"),
        include_str!("../../../guildbotics/templates/locales/integrations/resources.ja.yml"),
    ] {
        assert!(locale.contains("credential_prefix_conflict:"));
        assert!(locale.contains("%{member}"));
        assert!(locale.contains("%{conflicting_member}"));
    }
    assert!(!format!("{error:?}").contains("test-value"));
}

#[tokio::test]
async fn branvil_factory_supplies_roster_names_to_task_comments() {
    let d = tempfile::tempdir().unwrap();
    let data_root = d.path().join("branvil");
    branvil::api::initialize(&data_root, "owner1").unwrap();
    let token = fs::read_to_string(data_root.join("secrets/owner.token")).unwrap();
    let running = branvil::api::Running::start(&data_root).await.unwrap();
    let owner = running.data.authenticate(token.clone()).await.unwrap();
    owner
        .repos_create_workspace(serde_json::from_value(serde_json::json!({"name":"acme"})).unwrap())
        .await
        .unwrap();
    owner
        .repos_create(serde_json::from_value(serde_json::json!({"path":"acme/repo"})).unwrap())
        .await
        .unwrap();
    owner
        .members_create(
            serde_json::from_value(serde_json::json!({"name":"aiko-service","kind":"agent"}))
                .unwrap(),
        )
        .await
        .unwrap();
    let member = owner
        .members_issue_token(
            "aiko-service".into(),
            serde_json::from_value(serde_json::json!({"label":"fixture"})).unwrap(),
        )
        .await
        .unwrap();
    let ticket = owner.tickets_create_ticket("acme/repo", serde_json::from_value(serde_json::json!({"title":"task","body":"please","lane":"ready","assignee":"aiko-service"})).unwrap()).await.unwrap();
    owner
        .tickets_add_comment(
            "acme/repo",
            &ticket.key,
            serde_json::from_value(serde_json::json!({"body":"please"})).unwrap(),
        )
        .await
        .unwrap();
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
    secrets.set("AIKO_BRANVIL_TOKEN", &member.token).unwrap();
    let mut c = config(d.path());
    c.ticket_provider = "branvil".into();
    c.accounts
        .insert("branvil".into(), ActorName("aiko-service".into()));
    // Deliberately vary case and include account whitespace as in saved identities.
    c.branvil_members.insert(" OWNER1 ".into(), "Otota".into());
    let factory =
        ServiceIntegrationFactory::new(c, &secrets, Some(running.data.clone()), Arc::new(|_| {}))
            .unwrap();
    let tasks = factory
        .create_ticket_service()
        .unwrap()
        .candidates()
        .await
        .unwrap();
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].comments[0].author, "Otota");
    assert_eq!(tasks[0].comments[0].author_type, "User");
    running.stop().await.unwrap();
}

#[test]
fn slack_shared_connections_base_and_required_token_population() {
    let dir = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(dir.path().join("identity"), None);
    let key = FakeKeychain::default();
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: dir.path().join("machine"),
            keychain: &key,
        },
    )
    .unwrap();
    let mut c = config(dir.path());
    c.chat_provider = "slack".into();
    c.person_id = "alice".into();
    let factory =
        |config| ServiceIntegrationFactory::new(config, &secrets, None, Arc::new(|_| {})).unwrap();
    assert_eq!(
        factory(c.clone()).listener_key().unwrap_err(),
        ServiceError::MissingCredential {
            name: "ALICE_SLACK_APP_TOKEN".into()
        }
    );
    for member in ["ALICE", "BOB"] {
        secrets
            .set(&format!("{member}_SLACK_APP_TOKEN"), "synthetic-shared")
            .unwrap();
    }
    let shared = factory(c.clone()).listener_key().unwrap();
    let mut other = c.clone();
    other.person_id = "bob".into();
    assert_eq!(shared, factory(other).listener_key().unwrap());
    c.slack_base_url = "https://slack.com/api///".into();
    assert_eq!(shared, factory(c.clone()).listener_key().unwrap());
    c.slack_base_url = "https://other.invalid/api".into();
    assert_ne!(shared, factory(c.clone()).listener_key().unwrap());
    for (bot, app) in [(false, false), (true, false), (false, true), (true, true)] {
        for (suffix, present) in [("BOT_TOKEN", bot), ("APP_TOKEN", app)] {
            let name = format!("ALICE_SLACK_{suffix}");
            if present {
                secrets.set(&name, "synthetic").unwrap();
            } else {
                secrets.delete(&name).unwrap();
            }
        }
        let result = factory(c.clone()).verify_chat_credentials();
        assert_eq!(result.is_ok(), bot && app);
        if !(bot && app) {
            assert_eq!(
                result.unwrap_err(),
                ServiceError::MissingCredential {
                    name: format!("ALICE_SLACK_{}_TOKEN", if bot { "APP" } else { "BOT" }),
                }
            );
        }
    }
}

#[tokio::test]
async fn slack_listener_uses_bound_members_app_token_and_base() {
    use std::io::{Read, Write};
    let server = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}/slack", server.local_addr().unwrap());
    let served = std::thread::spawn(move || {
        let (mut stream, _) = server.accept().unwrap();
        stream
            .set_read_timeout(Some(std::time::Duration::from_secs(3)))
            .unwrap();
        let mut request = Vec::new();
        let mut buffer = [0; 1024];
        while !request.windows(4).any(|part| part == b"\r\n\r\n") {
            let n = stream.read(&mut buffer).unwrap();
            assert!(n > 0);
            request.extend_from_slice(&buffer[..n]);
        }
        let body = r#"{"ok":false,"error":"invalid_auth"}"#;
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
        String::from_utf8(request).unwrap()
    });
    let dir = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(dir.path().join("identity"), None);
    let key = FakeKeychain::default();
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: dir.path().join("machine"),
            keychain: &key,
        },
    )
    .unwrap();
    secrets
        .set("AIKO_SLACK_APP_TOKEN", "synthetic-aiko-app")
        .unwrap();
    secrets
        .set("BOB_SLACK_APP_TOKEN", "synthetic-bob-app")
        .unwrap();
    let mut c = config(dir.path());
    c.chat_provider = "slack".into();
    c.slack_base_url = base;
    let factory = ServiceIntegrationFactory::new(c, &secrets, None, Arc::new(|_| {})).unwrap();
    let listener = factory.create_event_listener(Arc::new(|| {})).unwrap();
    listener.start().await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while !listener.auth_failed() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    listener.stop().await.unwrap();
    let request = served.join().unwrap().to_ascii_lowercase();
    assert!(request.starts_with("post /slack/apps.connections.open "));
    assert!(request.contains("authorization: bearer synthetic-aiko-app"));
    assert!(!request.contains("synthetic-bob-app"));
}
