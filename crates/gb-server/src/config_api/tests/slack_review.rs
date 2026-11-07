//! Slack error contracts, using only loopback probes and temporary identities.
use super::providers::slack_fixture;
use super::*;

#[tokio::test]
async fn slack_avatar_missing_member_and_broken_files_are_actionable() {
    let (f, double) = slack_fixture().await;
    for lang in ["en", "ja"] {
        let (status, v) = f
            .call_in(
                lang,
                "POST",
                "/config/members/unknown/avatar/slack",
                json!({}),
            )
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(
            v["message"],
            crate::i18n::t(
                lang,
                "app_api.errors.person_not_found.plain",
                &[("person", "unknown")]
            )
        );
    }
    let path = f.identities().join("agent1/identity.yml");
    f.write(&path, "[invalid yaml");
    for lang in ["en", "ja"] {
        for endpoint in [
            "/config/providers/slack/verify-tokens",
            "/config/members/agent1/avatar/slack",
        ] {
            let (status, v) = f
                .call_in(lang, "POST", endpoint, json!({"person_id":"agent1"}))
                .await;
            assert_eq!(status, StatusCode::BAD_REQUEST, "{v}");
            assert_eq!(v["code"], "member_config_invalid");
            assert_eq!(v["context"]["path"], path.to_str().unwrap());
            assert!(v["message"].as_str().unwrap().contains("identity.yml"));
        }
    }
    assert!(double.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn slack_verification_propagates_a_locked_secret_store() {
    let (f, double) = slack_fixture().await;
    f.keys(Keys::Locked);
    let (status, value) = f
        .call(
            "POST",
            "/config/providers/slack/verify-tokens",
            json!({"person_id":"agent1"}),
        )
        .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(value["code"], "secret_store_unavailable");
    assert!(double.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn slack_diagnosis_missing_bot_avoids_calls_and_errors_name_the_method() {
    let (f, double) = slack_fixture().await;
    let registry = crate::setup_providers::Registry(Some(f.api.clone()));
    let mut member = gb_setup::SavedMember {
        person_id: "agent1".into(),
        name: "Agent".into(),
        human: false,
        account_info: BTreeMap::new(),
        secrets: BTreeMap::new(),
        avatar: None,
    };
    for lang in ["en", "ja"] {
        let checks = registry
            .diagnose_slack(&[(&member, vec![json!({"channel_name":"general"})])], lang)
            .await;
        assert_eq!(checks[0].code, "slack_app_token");
        assert_eq!(checks[0].status, "error");
        assert_eq!(checks[0].target, "AGENT1_SLACK_APP_TOKEN");
        assert_eq!(checks[1].code, "slack_access");
        assert_eq!(checks[1].context["error_type"], "ServiceError");
        assert_eq!(
            checks[1].message,
            crate::i18n::t(
                lang,
                "integrations.slack.setup.bot_missing",
                &[("key", "AGENT1_SLACK_BOT_TOKEN")]
            )
        );
    }
    assert!(double.calls.lock().unwrap().is_empty());
    member
        .secrets
        .insert("SLACK_BOT_TOKEN".into(), "xoxb-synthetic".into());
    member
        .secrets
        .insert("SLACK_APP_TOKEN".into(), "xapp-synthetic".into());
    *double.mode.lock().unwrap() = "both_errors".into();
    for lang in ["en", "ja"] {
        let checks = registry
            .diagnose_slack(&[(&member, vec![json!({"channel_name":"general"})])], lang)
            .await;
        for (check, key, method, code) in [
            (
                &checks[0],
                "app_failed",
                "apps.connections.open",
                "not_allowed_token_type",
            ),
            (&checks[1], "bot_failed", "auth.test", "invalid_auth"),
        ] {
            let detail = crate::i18n::t(
                lang,
                "integrations.slack.setup.api_failed",
                &[("method", method), ("error", code)],
            );
            assert_eq!(
                check.message,
                crate::i18n::t(
                    lang,
                    &format!("integrations.slack.setup.{key}"),
                    &[("detail", &detail)]
                )
            );
            assert!(check.message.contains(method));
            assert!(check.message.contains(code));
            assert!(!check.message.contains(".:"));
        }
    }
}

#[tokio::test]
async fn slack_registration_encodes_manifest_spaces_like_the_frozen_link() {
    let f = fixture();
    let (status, value) = f
        .call(
            "POST",
            "/config/providers/slack/app-registrations",
            json!({"app_name":"My Bot"}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    let link = value["registration_url"].as_str().unwrap();
    assert!(link.contains("My%20Bot"));
    assert!(!link.contains('+'));
}

#[tokio::test]
async fn slack_broken_project_never_falls_back_to_the_public_api() {
    let (f, double) = slack_fixture().await;
    let path = f.config().join("team/project.yml");
    f.write(&path, "[invalid yaml");
    for endpoint in [
        "/config/providers/slack/verify-tokens",
        "/config/members/agent1/avatar/slack",
    ] {
        let (status, v) = f
            .call(
                "POST",
                endpoint,
                json!({"person_id":"agent1","bot_token":"xoxb-synthetic"}),
            )
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{v}");
        assert_eq!(v["code"], "project_config_invalid");
        assert_eq!(v["context"]["path"], path.to_str().unwrap());
    }
    let registry = crate::setup_providers::Registry(Some(f.api.clone()));
    let checks = registry.diagnose_slack(&[], "en").await;
    assert_eq!(checks[0].code, "project_config_invalid");
    assert_eq!(checks[0].context["path"], path.to_str().unwrap());
    assert!(double.calls.lock().unwrap().is_empty());
}
