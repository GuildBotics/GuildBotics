use gb_base::shared_writes::{IdentityLock, SharedWrites};
use gb_config::{
    repository::{ConfigRepository, Revisions},
    secrets::{DeviceKey, Keychain},
};
use gb_setup::*;
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path, time::Duration};

struct Models;
impl ProviderModels for Models {
    fn member_secret_names(&self) -> Vec<String> {
        vec!["TOKEN".into()]
    }
    fn service(&self, _: ServiceKind, form: &Value) -> anyhow::Result<Value> {
        anyhow::ensure!(
            form["name"] == "test" && form["project"].is_string(),
            "invalid service form"
        );
        Ok(form.clone())
    }
    fn account(
        &self,
        provider: &str,
        form: &Value,
        _: &str,
        _: bool,
    ) -> anyhow::Result<StoredAccount> {
        anyhow::ensure!(
            provider == "test" && form["user"].is_string(),
            "invalid account"
        );
        Ok(StoredAccount {
            account_info: BTreeMap::from([(
                "test_user".into(),
                form["user"].as_str().unwrap().into(),
            )]),
            secrets: BTreeMap::from([("TOKEN".into(), "synthetic-test-value".into())]),
        })
    }
    fn channel(&self, _: &Value, reference: &str) -> anyhow::Result<Value> {
        Ok(json!({"channel_name":reference}))
    }
}
struct FixedKey;
impl Keychain for FixedKey {
    fn read(&self) -> anyhow::Result<Option<String>> {
        Ok(Some("ExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExM=".into()))
    }
    fn create(&self, _: &str) -> anyhow::Result<()> {
        panic!("use synthetic key")
    }
}
fn repository(root: &Path) -> ConfigRepository {
    let mut writes = SharedWrites::new(root.join("identity"), Some(root.join("workspace")));
    writes.timeout = Duration::from_millis(300);
    ConfigRepository::new(writes).unwrap()
}
fn device(root: &Path) -> DeviceKey<'_> {
    DeviceKey {
        machine_root: root.into(),
        keychain: &FixedKey,
    }
}
fn input(id: &str) -> MemberInput {
    serde_json::from_value(json!({"identity":{"person_id":id,"name":"Test","person_type":"agent"},
        "participation":{"is_active":true,"profile":{"roles":{"architect":{}}},
        "message_channels":[{"name":"#room","chat":{"participation":"social","notify_work_stops":true}}]},
        "accounts":{"test":{"user":"test-user"}}})).unwrap()
}

#[test]
fn saves_separate_documents_and_identity_secrets_and_reports_revisions() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    let saved = setup
        .save_member("a", &input("a"), &Revisions::new(), device(dir.path()))
        .unwrap();
    assert_eq!(saved.revisions.len(), 2);
    let snapshot = setup.read_member("a").unwrap();
    assert_eq!(snapshot.identity.account_info["test_user"], "test-user");
    assert!(snapshot.participation.is_active);
    assert_eq!(snapshot.participation.message_channels[0]["name"], "room");
    assert_eq!(
        snapshot.participation.message_channels[0]["chat"]["participation"],
        "social"
    );
    assert_eq!(
        snapshot.participation.message_channels[0]["chat"]["notify_work_stops"],
        true
    );
    let store = repo.identity_secrets(device(dir.path())).unwrap();
    assert_eq!(
        store.get("A_TOKEN").unwrap().as_deref(),
        Some("synthetic-test-value")
    );
    assert!(
        !dir.path()
            .join("workspace/.guildbotics/config/secrets.yml")
            .exists()
    );
    let before = fs::read(
        dir.path()
            .join("identity/.guildbotics/local/secrets.enc.json"),
    )
    .unwrap();
    assert!(
        setup
            .save_member("a", &input("a"), &Revisions::new(), device(dir.path()))
            .is_err()
    );
    assert_eq!(
        before,
        fs::read(
            dir.path()
                .join("identity/.guildbotics/local/secrets.enc.json")
        )
        .unwrap()
    );
    assert!(
        setup
            .save_member("b", &input("a"), &Revisions::new(), device(dir.path()))
            .is_err()
    );
    setup
        .save_member("a", &input("a"), &saved.revisions, device(dir.path()))
        .unwrap();
}

#[test]
fn rejects_every_other_document_field_including_profiles() {
    for field in [
        "is_active",
        "message_channels",
        "routine_commands",
        "task_schedules",
    ] {
        let mut form = serde_json::to_value(input("a").identity).unwrap();
        form[field] = Value::Null;
        assert!(serde_json::from_value::<Identity>(form).is_err(), "{field}");
    }
    for field in [
        "person_id",
        "name",
        "person_type",
        "account_info",
        "speaking_style",
        "relationships",
    ] {
        let mut form = json!({});
        form[field] = Value::Null;
        assert!(
            serde_json::from_value::<Participation>(form).is_err(),
            "{field}"
        );
    }
    assert!(serde_json::from_value::<IdentityProfile>(json!({"roles":{}})).is_err());
    assert!(serde_json::from_value::<ParticipationProfile>(json!({"character":{}})).is_err());
}

#[test]
fn namespace_collisions_and_bad_provider_forms_do_not_write() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    setup
        .save_member(
            "foo-bar",
            &input("foo-bar"),
            &Revisions::new(),
            device(dir.path()),
        )
        .unwrap();
    assert!(
        setup
            .save_member(
                "foo_bar",
                &input("foo_bar"),
                &Revisions::new(),
                device(dir.path())
            )
            .is_err()
    );
    let mut bad = input("b");
    bad.accounts.insert("unknown".into(), json!({}));
    assert!(
        setup
            .save_member("b", &bad, &Revisions::new(), device(dir.path()))
            .is_err()
    );
    assert!(
        !dir.path()
            .join("identity/.guildbotics/config/team/members/b")
            .exists()
    );
    for id in ["../a", "CON", "con", "a/b", "", "a.b"] {
        assert!(
            setup
                .save_member(id, &input(id), &Revisions::new(), device(dir.path()))
                .is_err()
        );
    }
}

#[test]
fn human_members_cannot_patrol_and_removal_retains_identity_and_clears_default() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    let mut form = input("a");
    form.identity.person_type = "human".into();
    let saved = setup
        .save_member("a", &form, &Revisions::new(), device(dir.path()))
        .unwrap();
    let snapshot = setup.read_member("a").unwrap();
    assert!(!snapshot.participation.is_active);
    assert!(snapshot.participation.message_channels.is_empty());
    assert!(setup.set_default_person("a", &Revisions::new()).is_err());
    setup.remove_member("a", &saved.revisions).unwrap();
    assert!(setup.read_member("a").is_ok());
    setup
        .delete_identity("a", &saved.revisions, device(dir.path()))
        .unwrap();
    assert!(
        repo.identity_secrets(device(dir.path()))
            .unwrap()
            .get("A_TOKEN")
            .unwrap()
            .is_none()
    );
    assert!(setup.read_member("a").is_err());
}

#[test]
fn project_model_validation_defaults_and_kind_based_brains_round_trip() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    let project: ProjectInput = serde_json::from_value(
        json!({"language":"ja", "llm_api_type":"openai", "cli_agent":"codex",
        "services":{"ticket_service":{"name":"test", "project":"test/project"}}}),
    )
    .unwrap();
    setup.save_project(&project, &Revisions::new()).unwrap();
    assert_eq!(setup.read_project().unwrap()["language"], "ja");
    let saved = setup
        .save_member("a", &input("a"), &Revisions::new(), device(dir.path()))
        .unwrap();
    assert_eq!(
        setup
            .read_member("a")
            .unwrap()
            .participation
            .routine_commands,
        ["workflows/ticket_driven_workflow"]
    );
    let project_revision = repo.revisions(&["team/project.yml"]).unwrap();
    let result = setup.set_default_person("a", &project_revision).unwrap();
    let mut expected = saved.revisions;
    expected.extend(result.revisions);
    setup.remove_member("a", &expected).unwrap();
    assert!(
        setup
            .read_project()
            .unwrap()
            .get("default_person_id")
            .is_none()
    );
    let mapping: BrainMapping = serde_json::from_value(json!({"agent":{"kind":"ai_cli", "cli_agent":"default"},
        "default":{"kind":"model", "model":"default"},"probabilities":{"kind":"jev","model":"jev-latest"}})).unwrap();
    setup.save_brains(&mapping, &Revisions::new()).unwrap();
    assert_eq!(setup.read_brains().unwrap(), mapping);
    assert!(serde_json::from_value::<Brain>(json!({"class":"python.Brain","args":{}})).is_err());
    assert!(
        serde_json::from_value::<ProjectInput>(json!({"language":"ja","services":{"unknown":{}}}))
            .is_err()
    );
    let mut bad = project;
    bad.services
        .insert(ServiceKind::ChatService, json!({"name":"unknown"}));
    assert!(setup.save_project(&bad, &Revisions::new()).is_err());
}

#[test]
fn identity_is_locked_before_waiting_for_workspace() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let workspace = dir.path().join("workspace");
    let identity = dir.path().join("identity");
    let _workspace_lock = IdentityLock::acquire(&workspace, Duration::from_millis(30)).unwrap();
    std::thread::scope(|scope| {
        let handle = scope.spawn(|| {
            Setup {
                repository: &repo,
                providers: &Models,
            }
            .save_member("a", &input("a"), &Revisions::new(), device(dir.path()))
        });
        let lockfile = identity.join(".guildbotics/local/run/shared-write.lock");
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            assert!(std::time::Instant::now() < deadline);
            if lockfile.exists()
                && gb_base::files::FileLock::try_hold(&lockfile)
                    .unwrap()
                    .is_none()
            {
                break;
            }
            std::thread::yield_now();
        }
        // The workspace cannot be obtained, yet the outer identity is held.
        assert!(handle.join().unwrap().is_err());
    });
    assert!(
        !identity
            .join(".guildbotics/config/team/members/a/identity.yml")
            .exists()
    );
}

#[test]
fn templates_are_bytes_samples_are_not_overwritten_and_paths_are_checked_first() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    let mut files = BTreeMap::from([
        (
            "commands/example.sh".into(),
            b"#!/bin/sh\necho sample\n".to_vec(),
        ),
        (
            "intelligences/cli_agents/test/default.yml".into(),
            b"command: test\n".to_vec(),
        ),
    ]);
    setup.install_templates(&files).unwrap();
    let sample = dir
        .path()
        .join("workspace/.guildbotics/config/commands/example.sh");
    assert_eq!(fs::read(&sample).unwrap(), files["commands/example.sh"]);
    files.insert("commands/example.sh".into(), b"overwritten".to_vec());
    setup.install_templates(&files).unwrap();
    assert_ne!(fs::read(&sample).unwrap(), b"overwritten");
    files.insert("commands/../../escape".into(), b"invalid".to_vec());
    assert!(setup.install_templates(&files).is_err());
}

#[test]
fn listing_orphans_and_foreign_fields_in_stored_files() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    let saved = setup
        .save_member("a", &input("a"), &Revisions::new(), device(dir.path()))
        .unwrap();
    assert_eq!(setup.list_members(true).unwrap()[0].person_id, "a");
    setup.remove_member("a", &saved.revisions).unwrap();
    assert!(!setup.list_members(true).unwrap()[0].participating);
    assert!(setup.list_members(false).unwrap().is_empty());
    setup
        .save_member(
            "a",
            &input("a"),
            &repo
                .identity_view()
                .revisions(&["team/members/a/identity.yml"])
                .unwrap(),
            device(dir.path()),
        )
        .unwrap();
    let ip = repo
        .identity_view()
        .path("team/members/a/identity.yml")
        .unwrap();
    let mut identity: Value = serde_json::from_slice(&fs::read(&ip).unwrap()).unwrap();
    identity["is_active"] = json!(true);
    fs::write(&ip, serde_json::to_vec(&identity).unwrap()).unwrap();
    assert!(setup.read_member("a").is_err());
    fs::remove_file(ip).unwrap();
    assert!(setup.list_members(false).unwrap()[0].identity.is_none());
}

#[test]
fn keychain_failure_leaves_both_member_files_absent() {
    struct Locked;
    impl Keychain for Locked {
        fn read(&self) -> anyhow::Result<Option<String>> {
            anyhow::bail!("locked")
        }
        fn create(&self, _: &str) -> anyhow::Result<()> {
            anyhow::bail!("locked")
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    assert!(
        setup
            .save_member(
                "a",
                &input("a"),
                &Revisions::new(),
                DeviceKey {
                    machine_root: dir.path().into(),
                    keychain: &Locked
                }
            )
            .is_err()
    );
    assert!(
        !dir.path()
            .join("identity/.guildbotics/config/team/members/a/identity.yml")
            .exists()
    );
    assert!(
        !dir.path()
            .join("workspace/.guildbotics/config/team/members/a/member.yml")
            .exists()
    );
}

#[test]
fn model_keys_use_workspace_store() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let setup = Setup {
        repository: &repo,
        providers: &Models,
    };
    setup
        .save_model_keys(
            &BTreeMap::from([("MODEL_API_KEY".into(), "synthetic-model-key".into())]),
            device(dir.path()),
        )
        .unwrap();
    assert_eq!(
        repo.workspace_secrets(device(dir.path()))
            .unwrap()
            .keys()
            .unwrap(),
        ["MODEL_API_KEY"]
    );
    assert!(
        !dir.path()
            .join("identity/.guildbotics/config/secrets.yml")
            .exists()
    );
}
