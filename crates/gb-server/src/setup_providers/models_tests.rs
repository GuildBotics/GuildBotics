use super::*;
use gb_setup::StoredAccount;
use serde_json::json;

#[test]
fn slack_accounts_store_only_the_human_id_or_nonblank_agent_tokens() {
    let registry = Registry(None);
    for (human, form, info, secrets) in [
        (
            true,
            json!({"user_id":"U01234567","bot_token":"bot","app_token":"app"}),
            json!({"slack_user_id":"U01234567"}),
            json!({}),
        ),
        (
            false,
            json!({"user_id":"U01234567","bot_token":"bot","app_token":"app"}),
            json!({}),
            json!({"SLACK_BOT_TOKEN":"bot","SLACK_APP_TOKEN":"app"}),
        ),
        (false, json!({}), json!({}), json!({})),
    ] {
        let StoredAccount {
            account_info,
            secrets: held,
        } = registry
            .account("slack", &form)
            .unwrap()
            .stored("agent1", human)
            .unwrap();
        assert_eq!(serde_json::to_value(account_info).unwrap(), info);
        assert_eq!(serde_json::to_value(held).unwrap(), secrets);
    }
    assert_eq!(
        registry.view(
            "slack",
            &[("slack_user_id".into(), "U01234567".into())].into(),
            &|k| k == "SLACK_BOT_TOKEN"
        ),
        json!({"user_id":"U01234567","has_bot_token":true,"has_app_token":false})
    );
    assert_eq!(registry.account_info_keys("slack"), vec!["slack_user_id"]);
    assert_eq!(
        registry.account_info_keys("branvil"),
        vec!["branvil_username"]
    );
}

#[test]
fn provider_models_validate_items_and_keep_local_unmodelled_items() {
    let registry = Registry(None);
    for workspace in ["", "a/b", "..", "a b"] {
        assert!(
            registry
                .service(
                    ServiceKind::TicketService,
                    &json!({"name":"branvil","workspace":workspace})
                )
                .is_err()
        );
    }
    assert_eq!(
        registry
            .service(
                ServiceKind::TicketService,
                &json!({"name":"Branvil","workspace":"acme"})
            )
            .unwrap(),
        json!({"name":"branvil","workspace":"acme"})
    );
    assert_eq!(
        registry
            .service(
                ServiceKind::ChatService,
                &json!({"name":"Slack","base_url":"http://127.0.0.1/api"})
            )
            .unwrap(),
        json!({"name":"slack","base_url":"http://127.0.0.1/api"})
    );
    for name in ["bad name", "a/b", &"a".repeat(65)] {
        assert!(
            registry
                .account("branvil", &json!({"username":name}))
                .is_err()
        );
    }
}

#[test]
fn only_slack_channels_are_stored_as_ids() {
    let registry = Registry(None);
    let id_pattern = regex::Regex::new(r"^[CGD][A-Z0-9]{8,}$").unwrap();
    for reference in [
        "C01234567",
        "G01234567",
        "D01234567",
        " C01234567 ",
        "general",
        "Cshort",
        "C0123456a",
        "c0123456789",
    ] {
        let id = id_pattern.is_match(reference.trim());
        let expected = if id {
            json!({"channel_id":reference})
        } else {
            json!({"channel_name":reference})
        };
        assert_eq!(
            Value::Object(
                registry
                    .channel(&json!({"chat_service":{"name":"slack"}}), reference)
                    .unwrap()
            ),
            expected
        );
        assert_eq!(
            Value::Object(registry.channel(&json!({}), reference).unwrap()),
            json!({"channel_name":reference})
        );
    }
}

#[test]
fn registry_declares_exact_provider_accounts_secrets_and_service_models() {
    let registry = Registry(None);
    assert_eq!(registry.member_providers(), ["branvil", "slack"]);
    assert_eq!(
        registry.member_secret_names(),
        [
            "BRANVIL_TOKEN",
            "BRANVIL_TOKEN_ID",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN"
        ]
    );
    assert_eq!(
        registry
            .service(
                ServiceKind::CodeHostingService,
                &json!({"name":"Local","tracker":"acme","anything":[1,2]})
            )
            .unwrap(),
        json!({"name":"local","tracker":"acme","anything":[1,2]})
    );
    for provider in ["local", "github", "unknown"] {
        assert!(registry.account(provider, &json!({})).is_err());
    }
    for kind in [ServiceKind::CodeHostingService, ServiceKind::TicketService] {
        assert_eq!(
            registry
                .service(kind, &json!({"name":"Branvil","workspace":" acme "}))
                .unwrap(),
            json!({"name":"branvil","workspace":"acme"})
        );
        for form in [
            json!({"name":"branvil"}),
            json!({"name":"branvil","workspace":"acme","other":true}),
            json!({"name":"branvil","workspace":"acme","base_url":"http://hub"}),
            json!({"name":"branvil","workspace":"acme","board":"acme/board"}),
            json!({"name":"branvil","owner":"acme"}),
        ] {
            assert!(registry.service(kind, &form).is_err());
        }
    }
    assert_eq!(
        registry
            .service(ServiceKind::ChatService, &json!({"name":"slack"}))
            .unwrap(),
        json!({"name":"slack"})
    );
    assert!(
        registry
            .service(
                ServiceKind::ChatService,
                &json!({"name":"slack","other":true})
            )
            .is_err()
    );
    for (kind, name) in [
        (ServiceKind::ChatService, "branvil"),
        (ServiceKind::TicketService, "slack"),
        (ServiceKind::TicketService, "github"),
    ] {
        assert!(
            registry
                .service(kind, &json!({"name":name,"workspace":"acme"}))
                .is_err()
        );
    }
    for bot in [false, true] {
        for app in [false, true] {
            assert_eq!(
                registry.view("slack", &BTreeMap::new(), &|key| match key {
                    "SLACK_BOT_TOKEN" => bot,
                    "SLACK_APP_TOKEN" => app,
                    _ => panic!("undeclared secret"),
                }),
                json!({"user_id":"","has_bot_token":bot,"has_app_token":app})
            );
        }
    }
    let human = registry
        .account("slack", &json!({}))
        .unwrap()
        .stored("human1", true)
        .unwrap();
    assert!(human.account_info.is_empty());
    assert!(human.secrets.is_empty());
    let partial = registry
        .account("slack", &json!({"bot_token":"xoxb-synthetic"}))
        .unwrap()
        .stored("agent1", false)
        .unwrap();
    assert_eq!(
        partial
            .secrets
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["SLACK_BOT_TOKEN"]
    );
    for provider in ["branvil", "slack"] {
        assert!(
            registry
                .account(provider, &json!({"unknown":true}))
                .is_err()
        );
    }
    assert_eq!(
        registry
            .account("branvil", &json!({"username":" agent1 "}))
            .unwrap()
            .stored("agent1", false)
            .unwrap()
            .account_info["branvil_username"],
        "agent1"
    );
    assert_eq!(
        registry
            .account(
                "slack",
                &json!({"bot_token":" xoxb-synthetic ", "app_token":" \n"})
            )
            .unwrap()
            .stored("agent1", false)
            .unwrap()
            .secrets,
        [("SLACK_BOT_TOKEN".into(), "xoxb-synthetic".into())].into()
    );
    let blank = registry
        .account("branvil", &json!({}))
        .unwrap()
        .stored("agent1", false)
        .unwrap();
    assert!(blank.account_info.is_empty());
    assert!(blank.secrets.is_empty());
}
