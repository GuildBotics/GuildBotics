//! `POST /-/guildbotics/api/verify` (凍結版 `tests/guildbotics/app_api/test_verify.py`).
//! The fixture's device has no snapshot and no tool login, so the isolated
//! environment and the AI CLI tool are refused as such.

use super::*;

async fn verified(f: &Fixture) -> Value {
    let (status, answer) = f.call("POST", "/verify", Value::Null).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    answer
}

fn check<'a>(answer: &'a Value, code: &str) -> Option<&'a Value> {
    answer["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["code"] == code)
}

fn codes(answer: &Value) -> Vec<String> {
    answer["checks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["code"].as_str().unwrap().to_owned())
        .collect()
}

fn verify_sentence(key: &str, params: &[(&str, &str)]) -> String {
    crate::i18n::t("en", &format!("app_api.verify.{key}"), params)
}

fn set_project(f: &Fixture, key: &str, value: Value) {
    let path = f.config().join("team/project.yml");
    let mut project = f.read(&path);
    project[key] = value;
    f.write(&path, &project.to_string());
}

#[tokio::test]
async fn post_verify_answers_the_checks_and_publishes_verify_completed() {
    let f = fixture();
    f.init().await;
    let answer = verified(&f).await;
    let fields: Vec<_> = answer.as_object().unwrap().keys().cloned().collect();
    assert_eq!(
        fields,
        [
            "ok",
            "config",
            "active_members",
            "checks",
            "warnings",
            "errors"
        ]
    );
    assert_eq!(answer["ok"], false);
    assert_eq!(answer["config"], f.get("/config/status").await);
    assert_eq!(answer["active_members"], json!([]));
    assert_eq!(answer["errors"][0]["code"], "active_members");
    assert_eq!(
        codes(&answer)[..4],
        [
            "config_project_file",
            "secret_store",
            "team_load",
            "active_members"
        ]
    );
    for status in ["warning", "error"] {
        let picked: Vec<_> = answer["checks"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["status"] == status)
            .cloned()
            .collect();
        assert_eq!(answer[format!("{status}s")], json!(picked));
    }
    let completed = f.events("verify.completed");
    assert_eq!(completed.len(), 1);
    assert_eq!(
        completed[0]["payload"],
        json!({"ok": false, "checks": answer["checks"]})
    );
    assert_eq!(completed[0]["source"], "diagnostics");
    assert_eq!(completed[0]["command"], "verify");
    let trace = completed[0]["trace_id"].as_str().unwrap();
    let detail = f.get(&format!("/diagnostics/traces/{trace}")).await;
    assert_eq!(detail["summary"]["status"], "success", "{detail}");
}

#[tokio::test]
async fn a_missing_project_file_fails_config_project_file_and_team_load() {
    let f = fixture();
    let answer = verified(&f).await;
    let project = f.config().join("team/project.yml");
    assert_eq!(
        check(&answer, "config_project_file").unwrap(),
        &json!({"code": "config_project_file", "status": "error",
            "message": verify_sentence("config_project_file.error", &[]),
            "target": project.display().to_string(), "context": {}})
    );
    let team = check(&answer, "team_load").unwrap();
    assert_eq!(team["status"], "error");
    assert_eq!(team["context"], json!({"error_type": "AppApiError"}));
    assert_eq!(
        team["message"],
        crate::i18n::t("en", "app_api.errors.config_not_found", &[])
    );
    assert_eq!(answer["ok"], false);
    f.init().await;
    let answer = verified(&f).await;
    assert_eq!(
        check(&answer, "config_project_file").unwrap()["status"],
        "ok"
    );
}

#[tokio::test]
async fn the_secret_store_check_follows_the_keychain() {
    let f = fixture();
    f.init().await;
    let answer = verified(&f).await;
    assert_eq!(check(&answer, "secret_store").unwrap()["status"], "ok");
    f.keys(Keys::Locked);
    let answer = verified(&f).await;
    assert_eq!(
        check(&answer, "secret_store").unwrap()["message"],
        verify_sentence("secret_store.error", &[])
    );
    assert_eq!(answer["ok"], false);
}

#[tokio::test]
async fn a_team_that_cannot_load_is_one_team_load_error_and_skips_the_team_checks() {
    let f = fixture();
    f.init().await;
    f.add(agent("bot")).await;
    f.write(&f.config().join("team/members/bot/member.yml"), "[broken\n");
    let answer = verified(&f).await;
    let team = check(&answer, "team_load").unwrap();
    assert_eq!(team["status"], "error");
    assert_eq!(team["context"], json!({"error_type": "ValueError"}));
    assert_ne!(team["message"], "");
    assert_eq!(
        codes(&answer),
        ["config_project_file", "secret_store", "team_load"]
    );
}

#[tokio::test]
async fn active_members_lists_the_active_member_ids_in_team_order() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    f.add(agent("bob")).await;
    let mut carol = agent("carol");
    carol["is_active"] = json!(false);
    f.add(carol).await;
    let answer = verified(&f).await;
    assert_eq!(answer["active_members"], json!(["alice", "bob"]));
    assert_eq!(
        check(&answer, "active_members").unwrap(),
        &json!({"code": "active_members", "status": "ok",
            "message": verify_sentence("active_members.ok", &[]), "target": "",
            "context": {"active_members": ["alice", "bob"]}})
    );
}

#[tokio::test]
async fn the_default_models_provider_key_is_checked_in_the_workspace_store() {
    let f = fixture();
    f.init().await;
    f.add(agent("bot")).await;
    let mapping = f.config().join("intelligences/model_mapping.yml");
    let keys = gb_setup::provider_env_keys(
        &gb_base::home::python_package().join("templates"),
        &f.config().join("intelligences/models"),
    );
    let (provider, key) = keys.iter().next().unwrap();
    f.write(&mapping, &format!("default: models/{provider}/model.yml\n"));
    let answer = verified(&f).await;
    assert_eq!(
        check(&answer, "llm_api_key").unwrap(),
        &json!({"code": "llm_api_key", "status": "error",
            "message": verify_sentence("key.error", &[("key", key)]), "target": key,
            "context": {"provider": provider}})
    );
    f.repository()
        .workspace_secrets(f.device())
        .unwrap()
        .set(key, "value")
        .unwrap();
    let answer = verified(&f).await;
    assert_eq!(check(&answer, "llm_api_key").unwrap()["status"], "ok");
    // A key in the server's own environment is enough. Every process has
    // PATH, so the test sets nothing in its environment.
    f.write(
        &f.config().join("intelligences/models/fixture/default.yml"),
        "api_key_env: PATH\n",
    );
    f.write(&mapping, "default: models/fixture/model.yml\n");
    let answer = verified(&f).await;
    assert_eq!(
        check(&answer, "llm_api_key").unwrap(),
        &json!({"code": "llm_api_key", "status": "ok",
            "message": verify_sentence("key.ok", &[("key", "PATH")]), "target": "PATH",
            "context": {"provider": "fixture"}})
    );
    f.write(&mapping, "default: models/unknown/whatever.yml\n");
    let answer = verified(&f).await;
    assert!(check(&answer, "llm_api_key").is_none());
    let provider = check(&answer, "llm_provider").unwrap();
    assert_eq!(provider["status"], "warning");
    assert_eq!(provider["context"], json!({"provider": "unknown"}));
    assert!(answer["warnings"].as_array().unwrap().contains(provider));
}

#[tokio::test]
async fn the_device_and_the_default_tool_are_checked_by_their_own_refusals() {
    let f = fixture();
    f.init().await;
    f.add(agent("bot")).await;
    let host = crate::command_catalog::host(&f.api).unwrap();
    let device = gb_environment::device::read(&host, &f.workspace()).await;
    let environment = check(&verified(&f).await, "agent_environment")
        .unwrap()
        .clone();
    assert_eq!(environment["status"], "error");
    assert_eq!(
        environment["message"],
        crate::commands::say(device.refusal.unwrap(), "en")
    );
    let mapping = f.config().join("intelligences/cli_agent_mapping.yml");
    f.write(&mapping, "default: cli_agents/codex/default.yml\n");
    let answer = verified(&f).await;
    let tool = check(&answer, "cli_agent_environment").unwrap();
    assert_eq!(tool["status"], "error");
    assert_eq!(tool["target"], "codex");
    assert_eq!(
        tool["message"],
        crate::i18n::t(
            "en",
            "intelligences.agent_environment.tool.credentials_missing_browser",
            &[("tool", "Codex")],
        )
    );
    // The tool is checked apart from the device, which still refuses, and a
    // past failed authentication does not make it an error.
    f.write(&mapping, "default: cli_agents/claude/default.yml\n");
    f.claude_logged_in();
    let answer = verified(&f).await;
    let tool = check(&answer, "cli_agent_environment").unwrap();
    assert_eq!(
        (&tool["status"], &tool["target"]),
        (&json!("ok"), &json!("claude"))
    );
    assert_eq!(
        check(&answer, "agent_environment").unwrap()["status"],
        "error"
    );
    f.write(&mapping, "default: ghost-cli.yml\n");
    let answer = verified(&f).await;
    assert!(check(&answer, "cli_agent_environment").is_none());
    assert_eq!(
        check(&answer, "cli_agent_mapping").unwrap()["status"],
        "warning"
    );
    assert!(check(&answer, "agent_environment").is_some());
}

/// A member's Branvil account, as its identity holds it.
fn branvil_account(f: &Fixture, person: &str, username: &str) {
    let path = f.identities().join(format!("{person}/identity.yml"));
    let mut identity = f.read(&path);
    identity["account_info"] = json!({ "branvil_username": username });
    f.write(&path, &identity.to_string());
}

#[tokio::test]
async fn only_the_providers_the_services_name_are_verified_with_each_members_checks() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let human = json!({"person_id": "owner", "person_name": "Owner", "person_type": "human", "is_active": true});
    f.add(human).await;
    // The screens keep a human inactive; a hand-written member may be active.
    let participation = f.config().join("team/members/owner/member.yml");
    let mut owner = f.read(&participation);
    owner["is_active"] = json!(true);
    f.write(&participation, &owner.to_string());
    let answer = verified(&f).await;
    assert!(check(&answer, "provider_secret").is_none());
    assert!(check(&answer, "branvil_credential").is_none());
    set_project(
        &f,
        "services",
        json!({"ticket_service": {"name": "branvil", "workspace": "acme"}}),
    );
    let answer = verified(&f).await;
    assert_eq!(
        check(&answer, "provider_secret").unwrap(),
        &json!({"code": "provider_secret", "status": "error",
            "message": verify_sentence("key.error", &[("key", "BRANVIL_ADMIN_TOKEN")]),
            "target": "BRANVIL_ADMIN_TOKEN", "context": {"provider": "branvil"}})
    );
    let targets: Vec<_> = answer["errors"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["code"] == "branvil_credential")
        .map(|c| c["target"].as_str().unwrap().to_owned())
        .collect();
    let token = gb_config::team::person_secret_name("alice", "BRANVIL_TOKEN");
    assert_eq!(
        targets,
        [
            "account_info.branvil_username".to_owned(),
            token.clone(),
            "account_info.branvil_username".to_owned()
        ],
        "the human is not asked for a token"
    );
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", "admin");
    branvil_account(&f, "alice", "alice");
    f.seed_identity_secret(&token, "token");
    let answer = verified(&f).await;
    assert_eq!(check(&answer, "provider_secret").unwrap()["status"], "ok");
    let alice: Vec<_> = answer["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["code"] == "branvil_credential" && c["context"]["person_id"] == "alice")
        .map(|c| c["status"].clone())
        .collect();
    assert_eq!(alice, [json!("ok"), json!("ok")]);
    let fields: Vec<_> = check(&answer, "branvil_credential")
        .unwrap()
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    assert_eq!(fields, ["code", "status", "message", "target", "context"]);
    // Slack asks a subscribed agent for its tokens, never a subscribed human.
    owner["message_channels"] = json!([{"name": "general", "chat": {"channel_id": "C1"}}]);
    f.write(&participation, &owner.to_string());
    set_project(&f, "services", json!({"chat_service": {"name": "slack"}}));
    let slack: Vec<_> = verified(&f).await["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["code"] == "slack_credential")
        .map(|c| c["context"]["person_id"].clone())
        .collect();
    assert!(!slack.contains(&json!("owner")), "{slack:?}");
}

#[tokio::test]
async fn a_member_whose_secrets_cannot_be_read_is_an_error_of_its_own() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    set_project(
        &f,
        "services",
        json!({"ticket_service": {"name": "branvil", "workspace": "acme"}}),
    );
    // The keychain answers, but the identity store's encrypted file is broken.
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", "admin");
    f.write(
        &f.root.join("identity/.guildbotics/local/secrets.enc.json"),
        "{\"version\": 1}",
    );
    let answer = verified(&f).await;
    assert_eq!(check(&answer, "secret_store").unwrap()["status"], "ok");
    let unread = json!({"code": "member_credentials", "status": "error",
        "message": "The settings of alice could not be read, so its credentials were not checked.",
        "target": "", "context": {"person_id": "alice"}});
    assert_eq!(check(&answer, "member_credentials").unwrap(), &unread);
    assert!(answer["errors"].as_array().unwrap().contains(&unread));
    // Once per member, however many providers the services name.
    set_project(
        &f,
        "services",
        json!({"ticket_service": {"name": "branvil", "workspace": "acme"},
            "chat_service": {"name": "slack"}}),
    );
    let answer = verified(&f).await;
    let codes = codes(&answer);
    assert_eq!(
        codes.iter().filter(|c| *c == "member_credentials").count(),
        1
    );
    assert!(
        !codes.iter().any(|c| c.ends_with("_credential")),
        "{codes:?}"
    );
}
