//! Localized Branvil diagnostics and unavailable connection storage.
use super::*;
use crate::setup_providers::Registry;
use gb_setup::SavedMember;

async fn connected(f: &Fixture) -> (branvil::api::Running, String) {
    let path = f.root.join("branvil");
    branvil::api::initialize(&path, "owner1").unwrap();
    let token = std::fs::read_to_string(path.join("secrets/owner.token")).unwrap();
    let running = branvil::api::Running::start(&path).await.unwrap();
    f.api.branvil.set(running.data.clone()).ok().unwrap();
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", &token);
    f.write(
        &connection(f),
        &json!({"base_url":running.data.base_url()}).to_string(),
    );
    (running, token)
}
fn connection(f: &Fixture) -> PathBuf {
    f.root
        .join("identity/.guildbotics/config/connections/branvil.yml")
}
fn member(name: &str, username: &str, token: &str) -> SavedMember {
    SavedMember {
        person_id: name.into(),
        name: name.into(),
        human: false,
        account_info: [("branvil_username".into(), username.into())].into(),
        secrets: [("BRANVIL_TOKEN".into(), token.into())].into(),
        avatar: None,
    }
}
#[tokio::test]
async fn diagnostics_report_each_credential_failure_in_requested_language() {
    let f = fixture();
    let (running, token) = connected(&f).await;
    let members = [
        member("absent_name", "", ""),
        member("absent_token", "agent1", ""),
        member("refused", "agent1", "synthetic-invalid"),
        member("mismatch", "agent1", &token),
        member("valid", "OWNER1", &token),
    ];
    let registry = Registry(Some(f.api.clone()));
    for lang in ["en", "ja"] {
        let checks = registry.diagnose_branvil(&members, &json!({"code_hosting_service":{"name":"branvil","workspace":"absent"},"ticket_service":{"name":"branvil","workspace":"absent"}}), lang).await;
        let access: Vec<_> = checks
            .iter()
            .filter(|c| c.code == "branvil_access")
            .collect();
        assert_eq!(access.len(), 5);
        for (check, key, args) in [
            (access[0], "username_missing", vec![]),
            (
                access[1],
                "token_missing",
                vec![("name", "ABSENT_TOKEN_BRANVIL_TOKEN")],
            ),
            (access[2], "authentication", vec![]),
            (
                access[3],
                "identity_mismatch",
                vec![("name", "owner1"), ("username", "agent1")],
            ),
        ] {
            assert_eq!(check.status, "error");
            assert!(check.credential);
            assert_eq!(
                check.message,
                crate::i18n::t(lang, &format!("integrations.branvil.api.{key}"), &args)
            );
        }
        assert_eq!(access[4].status, "ok");
        assert_eq!(access[4].person_id, "valid");
        let workspaces: Vec<_> = checks
            .iter()
            .filter(|c| c.code == "branvil_workspace")
            .collect();
        assert_eq!(workspaces.len(), 1);
        assert_eq!(workspaces[0].status, "error");
        assert_eq!(workspaces[0].target, "absent");
        assert!(!workspaces[0].message.contains("integrations.branvil"));
        assert!(!workspaces[0].message.contains("%{"));
        let rendered = serde_json::to_string(&checks).unwrap();
        assert!(!rendered.contains(&token));
        assert!(!rendered.contains("synthetic-invalid"));
    }
    running.stop().await.unwrap();
}
#[tokio::test]
async fn connection_storage_failure_is_distinct_from_missing_connection() {
    // Branvil holds its process-wide Git lock for the lifetime of the process.
    let f = fixture();
    let (running, _) = connected(&f).await;
    let encrypted = f.root.join("identity/.guildbotics/local/secrets.enc.json");
    let original_secrets = std::fs::read(&encrypted).unwrap();
    let original_connection = std::fs::read(connection(&f)).unwrap();
    let registry = Registry(Some(f.api.clone()));
    for failure in ["locked", "corrupt", "connection_io"] {
        match failure {
            "locked" => f.keys(Keys::Locked),
            "corrupt" => std::fs::write(&encrypted, "malformed").unwrap(),
            _ => {
                std::fs::remove_file(connection(&f)).unwrap();
                std::fs::create_dir(connection(&f)).unwrap();
            }
        }
        let error = match registry.admin().await {
            Err(e) => e,
            Ok(_) => panic!("unavailable store must fail"),
        };
        assert_eq!(error.code, "secret_store_unavailable", "{failure}");
        assert_eq!(error.status, 503, "{failure}");
        for lang in ["en", "ja"] {
            let rendered = crate::setup_providers::accounts::render(error.clone(), lang);
            assert!(!rendered.contains("%{"));
            assert!(!rendered.contains("app_api.errors"));
            for route in [
                "/config/providers/branvil/connection",
                "/config/providers/branvil/workspaces",
            ] {
                let (status, body) = f.call_in(lang, "POST", route, json!({})).await;
                assert_eq!(
                    status,
                    StatusCode::SERVICE_UNAVAILABLE,
                    "{failure}: {route}"
                );
                assert_eq!(body["code"], "secret_store_unavailable");
                assert_eq!(body["message"], rendered);
            }
            let mut human = member("human", "owner1", "");
            human.human = true;
            let checks = registry
                .diagnose_branvil(
                    &[human],
                    &json!({"ticket_service":{"name":"branvil","workspace":"acme"}}),
                    lang,
                )
                .await;
            let owner = checks.iter().find(|c| c.code == "branvil_owner").unwrap();
            assert_eq!(owner.status, "error");
            assert_eq!(owner.message, rendered);
        }
        match failure {
            "locked" => f.keys(Keys::Fixed),
            "corrupt" => std::fs::write(&encrypted, &original_secrets).unwrap(),
            _ => {
                std::fs::remove_dir(connection(&f)).unwrap();
                std::fs::write(connection(&f), &original_connection).unwrap();
            }
        }
        assert!(
            registry.admin().await.unwrap().is_some(),
            "{failure}: storage recovered"
        );
    }
    running.stop().await.unwrap();
    let f = fixture();
    let registry = Registry(Some(f.api.clone()));
    f.keys(Keys::Untouchable);
    assert!(registry.admin().await.unwrap().is_none());
}
