//! Setup HTTP contracts against temporary identity and Branvil services.
use super::*;
const SLACK_PICTURE: &[u8] = b"\x89PNG\r\n\x1a\nfixture";
/// The embedded service, without any connection setup.
async fn branvil(f: &Fixture) -> branvil::test_support::Running {
    let path = f.root.join("branvil");
    branvil::test_support::initialize(&path, "owner1").unwrap();
    let running = branvil::test_support::Running::start(
        &path,
        std::sync::Arc::new(branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    f.api.branvil.set(running.data.clone()).ok().unwrap();
    running
}
#[tokio::test]
async fn provider_listing_validation_localization_and_public_page() {
    let f = fixture();
    let providers = f.get("/config/providers").await;
    assert_eq!(
        providers,
        json!([
            {"name":"branvil","title":"GuildBotics","kinds":["ticket_service","code_hosting_service"],"avatar":false},
            {"name":"slack","title":"Slack","kinds":["chat_service"],"avatar":true}
        ])
    );
    for lang in ["en", "ja"] {
        let (status, v) = f
            .call_in(
                lang,
                "POST",
                "/config/providers/slack/app-registrations",
                json!({"app_name":"  "}),
            )
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(v["code"], "invalid_slack_app_name");
        assert_eq!(
            v["message"],
            crate::i18n::t(lang, "integrations.slack.setup.invalid_app_name", &[])
        );
    }
    assert_eq!(
        f.call(
            "POST",
            "/config/providers/slack/app-registrations",
            json!({})
        )
        .await
        .0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
}
#[tokio::test]
async fn members_have_no_branvil_account_and_save_and_delete_without_branvil() {
    let f = fixture();
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{}});
    assert_eq!(
        f.save("POST", "/config/members", form).await.0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    assert!(!f.identities().join("agent1").exists());
    // No Branvil runs here: saving, reading and deleting ask nothing of it.
    f.add(agent("agent1")).await;
    assert!(
        f.get("/config/members/agent1").await["accounts"]
            .get("branvil")
            .is_none()
    );
    assert_eq!(
        f.call("DELETE", "/identities/agent1", Value::Null).await.0,
        StatusCode::OK
    );
    assert!(!f.identities().join("agent1").exists());
}
#[tokio::test]
async fn a_saved_agent_is_known_to_the_code_host_before_it_acts() {
    let f = fixture();
    f.init().await;
    let running = branvil(&f).await;
    f.add(agent("agent1")).await;
    let mut human = agent("person1");
    human["person_type"] = "human".into();
    f.add(human).await;
    let owner = running.data.owner();
    let found = owner.members_get("agent1".into()).await.unwrap();
    assert_eq!(found.kind, branvil::api::MemberKind::Agent);
    assert_eq!(found.display_name, "Test");
    // Only agents become the code host's members; the human is its owner.
    assert_eq!(
        owner
            .members_get("person1".into())
            .await
            .err()
            .unwrap()
            .code,
        "not_found"
    );
    running.stop().await.unwrap();
}
type Calls = Arc<Mutex<Vec<(String, String, BTreeMap<String, String>)>>>;
pub(super) struct SlackDouble {
    pub(super) base: String,
    pub(super) calls: Calls,
    pub(super) mode: Arc<Mutex<String>>,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for SlackDouble {
    fn drop(&mut self) {
        self.task.abort();
    }
}
async fn slack() -> SlackDouble {
    use axum::{
        Router,
        extract::{Form, Path as P, State as S},
        routing::{get, post},
    };
    let calls: Calls = Arc::default();
    let mode = Arc::new(Mutex::new(String::new()));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    async fn call(
        S((calls, mode, base)): S<(Calls, Arc<Mutex<String>>, String)>,
        P(method): P<String>,
        headers: HeaderMap,
        Form(form): Form<BTreeMap<String, String>>,
    ) -> Response {
        calls.lock().unwrap().push((
            method.clone(),
            headers
                .get("authorization")
                .and_then(|h| h.to_str().ok())
                .unwrap_or("")
                .into(),
            form.clone(),
        ));
        let mode = mode.lock().unwrap().clone();
        if mode == "http_error" {
            return StatusCode::BAD_GATEWAY.into_response();
        }
        if (mode == "auth_error" || mode == "both_errors") && method == "auth.test" {
            return Json(json!({"ok":false,"error":"invalid_auth"})).into_response();
        }
        if mode == "both_errors" && method == "apps.connections.open" {
            return Json(json!({"ok":false,"error":"not_allowed_token_type"})).into_response();
        }
        if mode == "app_error" && method == "apps.connections.open" {
            return Json(json!({"ok":false,"error":"invalid_auth"})).into_response();
        }
        if mode == "missing_scope" && method == "conversations.list" {
            return Json(
                json!({"ok":false,"error":"missing_scope","needed":"channels:read,groups:read"}),
            )
            .into_response();
        }
        if mode == "missing_scope" && method == "users.info" {
            return Json(json!({"ok":false,"error":"missing_scope"})).into_response();
        }
        if mode == "not_joined" && method == "conversations.history" {
            return Json(json!({"ok":false,"error":"not_in_channel"})).into_response();
        }
        let value = match method.as_str() {
            "auth.test" => {
                json!({"ok":true,"user_id":"U01234567","user":"Test bot","team":"Test workspace"})
            }
            "apps.connections.open" => json!({"ok":true,"url":"wss://example.invalid/socket"}),
            "conversations.list" => {
                json!({"ok":true,"channels":[{"id":"C01234567","name":"general"}],"response_metadata":{"next_cursor":""}})
            }
            "conversations.history" => json!({"ok":true,"messages":[]}),
            "users.info" => {
                json!({"ok":true,"user":{"profile":{"image_512":format!("{base}/image")}}})
            }
            _ => json!({"ok":false,"error":"unknown_method"}),
        };
        Json(value).into_response()
    }
    let image_mode = mode.clone();
    let app = Router::new()
        .route("/api/{method}", post(call))
        .route(
            "/image",
            get(move || {
                let mode = image_mode.clone();
                async move {
                    while *mode.lock().unwrap() == "wait" {
                        tokio::time::sleep(Duration::from_millis(1)).await;
                    }
                    let bytes = if *mode.lock().unwrap() == "large" {
                        vec![1u8; gb_setup::AVATAR_LIMIT + 1]
                    } else {
                        SLACK_PICTURE.to_vec()
                    };
                    ([("content-type", "image/png")], bytes)
                }
            }),
        )
        .with_state((calls.clone(), mode.clone(), base.clone()));
    let task = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    SlackDouble {
        base: format!("{base}/api"),
        calls,
        mode,
        task,
    }
}
pub(super) async fn slack_fixture() -> (Fixture, SlackDouble) {
    let f = fixture();
    let double = slack().await;
    f.init_with(json!({"language":"en","services":{"chat_service":{"name":"slack","base_url":double.base}}})).await;
    let mut form = agent("agent1");
    form["accounts"] =
        json!({"slack":{"bot_token":"xoxb-synthetic-bot","app_token":"xapp-synthetic-app"}});
    f.add(form).await;
    (f, double)
}
#[tokio::test]
async fn slack_verification_probes_effective_tokens_scopes_and_channels_without_disclosing_them() {
    let (f, double) = slack_fixture().await;
    let before = f.api.events.snapshot();
    let request = json!({"person_id":"agent1","channels":["#general","C01234567","unknown"," "]});
    let (status, v) = f
        .call(
            "POST",
            "/config/providers/slack/verify-tokens",
            request.clone(),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["bot_ok"], true);
    assert_eq!(v["app_token_ok"], true);
    assert_eq!(v["bot_source"], "stored");
    assert_eq!(v["app_token_source"], "stored");
    assert_eq!(v["bot_user_id"], "U01234567");
    assert_eq!(v["bot_display_name"], "Test bot");
    assert_eq!(v["workspace"], "Test workspace");
    assert_eq!(v["scopes_ok"], true);
    assert_eq!(
        v["channels"],
        json!([{"channel":"#general","ok":true,"error":""},{"channel":"C01234567","ok":true,"error":""},{"channel":"unknown","ok":false,"error":"not_found"}])
    );
    let calls = double.calls.lock().unwrap().clone();
    assert!(calls.iter().all(|(m, t, _)| t
        == if m == "apps.connections.open" {
            "Bearer xapp-synthetic-app"
        } else {
            "Bearer xoxb-synthetic-bot"
        }));
    let probe = calls
        .iter()
        .find(|(m, _, f)| m == "conversations.list" && f.get("limit").is_some_and(|s| s == "1"))
        .unwrap();
    assert_eq!(probe.2["types"], "public_channel,private_channel");
    assert_eq!(
        calls
            .iter()
            .filter(|(m, _, _)| m == "conversations.list")
            .count(),
        3
    );
    assert!(!v.to_string().contains("synthetic"));
    assert_eq!(f.api.events.snapshot(), before);
    for (mode, field, code) in [
        ("auth_error", "bot_error", "invalid_auth"),
        ("app_error", "app_token_error", "invalid_auth"),
        ("missing_scope", "scope_error", "missing_scope"),
        ("http_error", "bot_error", "unreachable"),
        ("not_joined", "unused", ""),
    ] {
        *double.mode.lock().unwrap() = mode.into();
        double.calls.lock().unwrap().clear();
        let (_, v) = f
            .call(
                "POST",
                "/config/providers/slack/verify-tokens",
                request.clone(),
            )
            .await;
        if mode == "not_joined" {
            assert_eq!(v["channels"][0]["error"], "not_in_channel");
        } else {
            assert_eq!(v[field], code, "{mode}: {v}");
        }
        if mode == "auth_error" {
            assert_eq!(v["app_token_ok"], true);
            assert!(
                !double
                    .calls
                    .lock()
                    .unwrap()
                    .iter()
                    .any(|(m, _, _)| m == "conversations.list")
            );
        }
        if mode == "app_error" {
            assert_eq!(v["bot_ok"], true);
        }
        if mode == "missing_scope" {
            assert_eq!(v["bot_ok"], true);
            assert_eq!(v["scope_needed"], "channels:read,groups:read");
            assert!(v["channels"].as_array().unwrap().is_empty());
        }
        assert!(!v.to_string().contains("synthetic"));
    }
    *double.mode.lock().unwrap() = "both_errors".into();
    let (_, v) = f
        .call(
            "POST",
            "/config/providers/slack/verify-tokens",
            request.clone(),
        )
        .await;
    assert_eq!(v["bot_error"], "invalid_auth");
    assert_eq!(v["app_token_error"], "not_allowed_token_type");
    *double.mode.lock().unwrap() = String::new();
    double.calls.lock().unwrap().clear();
    let (_, v) = f
        .call(
            "POST",
            "/config/providers/slack/verify-tokens",
            json!({"bot_token":"xapp-swapped","app_token":"xoxb-swapped"}),
        )
        .await;
    assert_eq!(v["bot_error"], "wrong_token_type");
    assert_eq!(v["app_token_error"], "wrong_token_type");
    assert!(double.calls.lock().unwrap().is_empty());
    for id in ["", "new"] {
        let (_, v) = f
            .call(
                "POST",
                "/config/providers/slack/verify-tokens",
                json!({"person_id":id}),
            )
            .await;
        assert_eq!(v["bot_error"], "missing");
        assert_eq!(v["app_token_error"], "missing");
        assert_eq!(v["bot_source"], "none");
        assert!(double.calls.lock().unwrap().is_empty());
    }
    let (_, v) = f
        .call(
            "POST",
            "/config/providers/slack/verify-tokens",
            json!({"person_id":"agent1","bot_token":" xoxb-entered ","app_token":" "}),
        )
        .await;
    assert_eq!(v["bot_source"], "input");
    assert_eq!(v["app_token_source"], "stored");
    assert!(
        double
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|(m, t, _)| m == "auth.test" && t == "Bearer xoxb-entered")
    );
    double.task.abort();
    let (_, v) = f
        .call("POST", "/config/providers/slack/verify-tokens", request)
        .await;
    assert_eq!(v["bot_error"], "unreachable");
    assert_eq!(v["app_token_error"], "unreachable");
    assert!(!v.to_string().contains("synthetic"));
    assert_eq!(f.api.events.snapshot(), before);
}
#[tokio::test]
async fn slack_manifest_name_limits_scopes_events_and_url_roundtrip() {
    let f = fixture();
    for name in ["a", "Human Readable NAME!", "日本語", ".", &"a".repeat(35)] {
        let (status, v) = f
            .call(
                "POST",
                "/config/providers/slack/app-registrations",
                json!({"app_name":format!(" {name} ")}),
            )
            .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(v["app_name"], name);
        assert_eq!(v["app_directory_url"], "https://api.slack.com/apps");
        let url = url::Url::parse(v["registration_url"].as_str().unwrap()).unwrap();
        assert!(url.as_str().len() < 4096);
        let query: BTreeMap<_, _> = url.query_pairs().into_owned().collect();
        assert_eq!(query["new_app"], "1");
        let manifest: Value = serde_json::from_str(&query["manifest_json"]).unwrap();
        assert_eq!(manifest["display_information"]["name"], name);
        assert_eq!(manifest["features"]["bot_user"]["always_online"], true);
        assert_eq!(manifest["settings"]["socket_mode_enabled"], true);
        assert_eq!(manifest["settings"]["org_deploy_enabled"], false);
        assert_eq!(manifest["settings"]["token_rotation_enabled"], false);
        assert_eq!(
            manifest["oauth_config"]["scopes"]["bot"],
            json!([
                "chat:write",
                "reactions:write",
                "channels:history",
                "groups:history",
                "im:history",
                "mpim:history",
                "channels:read",
                "groups:read",
                "users:read"
            ])
        );
        assert_eq!(
            manifest["settings"]["event_subscriptions"]["bot_events"],
            json!([
                "message.channels",
                "message.groups",
                "message.im",
                "message.mpim"
            ])
        );
        let display = manifest["features"]["bot_user"]["display_name"]
            .as_str()
            .unwrap();
        assert!(!display.is_empty());
        assert!(
            display
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"-_.".contains(&c))
        );
    }
    for (name, status, code) in [
        (
            "a".repeat(36),
            StatusCode::BAD_REQUEST,
            "invalid_slack_app_name",
        ),
        (
            String::new(),
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
        ),
    ] {
        let (actual, body) = f
            .call(
                "POST",
                "/config/providers/slack/app-registrations",
                json!({"app_name":name}),
            )
            .await;
        assert_eq!(actual, status);
        assert_eq!(body["code"], code);
    }
}

#[tokio::test]
async fn branvil_workspaces_and_git_config_need_no_connection() {
    let f = fixture();
    for route in [
        "/config/providers/branvil/workspaces",
        "/config/providers/branvil/git_config",
    ] {
        let (_, v) = f.call("POST", route, json!({})).await;
        assert_eq!(
            v["message"],
            crate::i18n::t("en", "integrations.branvil.api.stopped", &[]),
            "{route}"
        );
    }
    let running = branvil(&f).await;
    let (status, v) = f
        .call("POST", "/config/providers/branvil/workspaces", json!({}))
        .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v, json!({"workspaces":[]}));
    for name in ["acme", "other"] {
        let (status, v) = f
            .call(
                "POST",
                "/config/providers/branvil/workspaces/create",
                json!({"name":name}),
            )
            .await;
        assert_eq!(status, StatusCode::OK, "{v}");
        assert!(v["workspaces"].as_array().unwrap().contains(&json!(name)));
    }
    assert_eq!(
        f.call(
            "POST",
            "/config/providers/branvil/workspaces/create",
            json!({"name":"acme"})
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    let (status, v) = f
        .call("POST", "/config/providers/branvil/git_config", json!({}))
        .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    let helper = f.root.join("machine/bin/guildbotics");
    let quoted = gb_base::shell::managed_cli_command(&helper, &["git-credential"])
        .replace('\\', "\\\\")
        .replace('"', "\\\"");
    assert_eq!(
        v,
        json!({"git_config": format!(
            "[credential \"{}\"]\n\thelper =\n\thelper = \"!{quoted}\"\n",
            running.data.base_url()
        )})
    );
    running.stop().await.unwrap();
}
#[tokio::test]
async fn slack_avatar_import_uses_the_members_identity_bounds_downloads_and_sanitizes_failures() {
    let (f, double) = slack_fixture().await;
    let before = f.api.events.snapshot();
    let uri = "/config/members/agent1/avatar/slack";
    let (status, v) = f.call("POST", uri, Value::Null).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    let path = f.identities().join("agent1/avatar.png");
    assert_eq!(std::fs::read(&path).unwrap(), SLACK_PICTURE);
    assert!(v["avatar_timestamp"].as_i64().unwrap() > 0);
    let calls = double.calls.lock().unwrap().clone();
    assert_eq!(
        calls.iter().map(|c| c.0.as_str()).collect::<Vec<_>>(),
        ["auth.test", "users.info"]
    );
    assert_eq!(calls[1].2["user"], "U01234567");
    assert!(calls.iter().all(|c| c.1 == "Bearer xoxb-synthetic-bot"));
    assert_eq!(f.api.events.snapshot(), before);
    let mut form = agent("human1");
    form["person_type"] = "human".into();
    form["accounts"] = json!({"slack":{"user_id":"U0HUMAN"}});
    f.add(form).await;
    double.calls.lock().unwrap().clear();
    assert_eq!(
        f.call("POST", "/config/members/human1/avatar/slack", Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    let calls = double.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, "users.info");
    assert_eq!(calls[0].2["user"], "U0HUMAN");
    assert_eq!(calls[0].1, "Bearer xoxb-synthetic-bot");
    for (mode, status, code) in [
        (
            "missing_scope",
            StatusCode::BAD_REQUEST,
            "avatar_missing_scope",
        ),
        (
            "http_error",
            StatusCode::INTERNAL_SERVER_ERROR,
            "avatar_import_failed",
        ),
        ("large", StatusCode::BAD_REQUEST, "avatar_invalid"),
    ] {
        *double.mode.lock().unwrap() = mode.into();
        let (actual, v) = f.call_in("ja", "POST", uri, Value::Null).await;
        assert_eq!(actual, status, "{mode}: {v}");
        assert_eq!(v["code"], code);
        assert!(!v.to_string().contains("synthetic"));
        assert_eq!(std::fs::read(&path).unwrap(), SLACK_PICTURE);
        if mode == "missing_scope" {
            let message = v["message"].as_str().unwrap();
            assert!(message.contains("users:read"));
            assert!(message.contains("再インストール"));
        }
    }
    *double.mode.lock().unwrap() = String::new();
    for provider in ["github", "branvil", "unknown"] {
        assert_eq!(
            f.call(
                "POST",
                &format!("/config/members/agent1/avatar/{provider}"),
                Value::Null
            )
            .await
            .0,
            StatusCode::NOT_FOUND
        );
    }
    assert_eq!(
        f.call("POST", "/config/members/absent/avatar/slack", Value::Null)
            .await
            .1["code"],
        "person_not_found"
    );
    let mut no_id = agent("human2");
    no_id["person_type"] = "human".into();
    no_id["accounts"] = json!({"slack":{}});
    f.add(no_id).await;
    for lang in ["en", "ja"] {
        let (status, answer) = f
            .call_in(
                lang,
                "POST",
                "/config/members/human2/avatar/slack",
                Value::Null,
            )
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(answer["code"], "avatar_account_missing");
        assert_eq!(
            answer["message"],
            crate::i18n::t(lang, "integrations.slack.setup.user_id_missing", &[])
        );
    }
    f.add(agent("agent2")).await;
    double.calls.lock().unwrap().clear();
    assert_eq!(
        f.call("POST", "/config/members/agent2/avatar/slack", Value::Null)
            .await
            .1["code"],
        "avatar_token_missing"
    );
    assert!(double.calls.lock().unwrap().is_empty());
    f.add(agent("alice-bak")).await;
    f.write(
        &f.identities().join("alice_bak/identity.yml"),
        "person_id: alice_bak\nname: Other\n",
    );
    f.seed_identity_secret("ALICE_BAK_SLACK_BOT_TOKEN", "xoxb-colliding");
    f.keys(Keys::Untouchable);
    for requested in ["alice-bak", "agent1"] {
        let (status, answer) = f
            .call(
                "POST",
                &format!("/config/members/{requested}/avatar/slack"),
                Value::Null,
            )
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(answer["code"], "member_config_invalid");
    }
    assert!(double.calls.lock().unwrap().is_empty());
}
#[tokio::test]
async fn avatar_import_answers_both_config_locks_with_503() {
    let (f, _double) = slack_fixture().await;
    for root in [f.root.join("identity"), f.workspace()] {
        let _held = IdentityLock::acquire(&root, Duration::from_secs(1)).unwrap();
        let (status, v) = f
            .call("POST", "/config/members/agent1/avatar/slack", Value::Null)
            .await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{v}");
        assert_eq!(v["code"], "config_busy");
        assert!(!f.identities().join("agent1/avatar.png").exists());
    }
}
#[tokio::test]
async fn provider_verify_requires_only_the_slack_tokens_of_subscribed_agents() {
    let f = fixture();
    f.init().await;
    f.add(agent("agent1")).await;
    let registry = crate::setup_providers::Registry(Some(f.api.clone()));
    let member = with_setup(&f.api, f.workspace(), |s| s.saved_member("agent1"))
        .await
        .unwrap();
    let human = gb_setup::SavedMember {
        person_id: "human1".into(),
        name: "Human".into(),
        human: true,
        account_info: BTreeMap::new(),
        secrets: BTreeMap::new(),
    };
    for language in ["en", "ja"] {
        for subscribed in [false, true] {
            assert!(
                registry
                    .verify("branvil", &member, subscribed, language)
                    .is_empty()
            );
            assert!(
                registry
                    .verify("branvil", &human, subscribed, language)
                    .is_empty()
            );
            assert!(
                registry
                    .verify("slack", &human, subscribed, language)
                    .is_empty()
            );
        }
        assert!(
            registry
                .verify("slack", &member, false, language)
                .is_empty()
        );
        assert_eq!(
            registry
                .verify("slack", &member, true, language)
                .iter()
                .map(|c| c.status.as_str())
                .collect::<Vec<_>>(),
            ["error", "error"]
        );
    }
}
#[tokio::test]
async fn slack_diagnosis_distinguishes_humans_subscriptions_credentials_and_unjoined_channels() {
    let (f, double) = slack_fixture().await;
    let registry = crate::setup_providers::Registry(Some(f.api.clone()));
    let member = with_setup(&f.api, f.workspace(), |s| s.saved_member("agent1"))
        .await
        .unwrap();
    for (mode, expected) in [
        (
            "",
            vec!["slack_app_token", "slack_bot_auth", "slack_channel_history"],
        ),
        (
            "app_error",
            vec![
                "slack_app_token_invalid",
                "slack_bot_auth",
                "slack_channel_history",
            ],
        ),
        ("auth_error", vec!["slack_app_token", "slack_access"]),
        (
            "not_joined",
            vec![
                "slack_app_token",
                "slack_bot_auth",
                "slack_channel_not_joined",
            ],
        ),
    ] {
        *double.mode.lock().unwrap() = mode.into();
        let checks = registry
            .diagnose_slack(&[(&member, vec![json!({"channel_name":"general"})])], "en")
            .await;
        assert_eq!(
            checks.iter().map(|c| c.code.as_str()).collect::<Vec<_>>(),
            expected,
            "{mode}"
        );
        assert!(checks.iter().all(|c| c.person_id == "agent1"));
        if mode.is_empty() {
            assert_eq!(checks[1].context["bot_user_id"], "U01234567");
            assert_eq!(
                checks
                    .iter()
                    .filter(|c| c.credential)
                    .map(|c| c.code.as_str())
                    .collect::<Vec<_>>(),
                vec!["slack_app_token", "slack_bot_auth"]
            );
            assert!(checks.iter().all(|c| c.status == "ok"));
        }
        if mode == "not_joined" {
            assert_eq!(
                checks[2].context,
                json!({"channel_id":"C01234567","channel":"general","bot_name":"Test bot"})
            );
            assert!(!checks[2].credential);
        }
        if mode == "auth_error" {
            assert_eq!(checks[1].context["error_type"], "SlackApiError");
            assert!(checks[1].credential);
        }
        assert!(
            !serde_json::to_string(&checks)
                .unwrap()
                .contains("synthetic")
        );
    }
    double.calls.lock().unwrap().clear();
    assert_eq!(
        registry.diagnose_slack(&[(&member, vec![])], "en").await[0].code,
        "slack_not_configured"
    );
    assert!(double.calls.lock().unwrap().is_empty());
    let mut human = gb_setup::SavedMember {
        person_id: "human1".into(),
        name: "Human".into(),
        human: true,
        account_info: BTreeMap::new(),
        secrets: BTreeMap::new(),
    };
    for (id, status) in [("", "warning"), ("U01234567", "ok"), ("invalid", "error")] {
        human.account_info.insert("slack_user_id".into(), id.into());
        let checks = registry.diagnose_slack(&[(&human, vec![])], "ja").await;
        assert_eq!(checks.len(), 1);
        assert_eq!(checks[0].status, status);
        assert_eq!(checks[0].target, id);
        assert!(!checks[0].credential);
    }
    assert!(double.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn every_provider_operation_requires_owner_authentication() {
    let f = fixture();
    for path in [
        "/config/providers/branvil/workspaces",
        "/config/providers/branvil/workspaces/create",
        "/config/providers/branvil/git_config",
        "/intelligences/agent-environment/ci-images",
        "/intelligences/agent-environment/ci-images/state",
        "/config/providers/slack/app-registrations",
        "/config/providers/slack/verify-tokens",
        "/config/members/agent1/avatar/slack",
    ] {
        let response = f
            .respond(
                Request::builder()
                    .method("POST")
                    .uri(format!("/-/guildbotics/api{path}"))
                    .header("host", "127.0.0.1:8765")
                    .header("content-type", "application/json")
                    .body(HttpBody::from("{}"))
                    .unwrap(),
            )
            .await;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{path}");
    }
}
#[tokio::test]
async fn provider_account_views_report_unavailable_secrets_without_values() {
    for language in ["en", "ja"] {
        let f = fixture();
        f.init().await;
        f.add(agent("agent1")).await;
        f.seed_identity_secret("AGENT1_SLACK_BOT_TOKEN", "xoxb-synthetic-held");
        f.keys(Keys::Locked);
        let (status, v) = f
            .call_in(language, "GET", "/config/members/agent1", Value::Null)
            .await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(v["code"], "secret_store_unavailable");
        assert!(!v.to_string().contains("synthetic-held"));
        assert_eq!(v["context"], json!({}));
    }
}

#[tokio::test]
async fn slack_accounts_are_identity_scoped_blank_fields_keep_secrets_and_another_id_is_distinct() {
    let f = fixture();
    f.init_with(json!({"language":"en","provider_api_keys":{"openai":"sk-synthetic-workspace"}}))
        .await;
    let mut first = agent("agent1");
    first["accounts"] = json!({"slack":{"bot_token":"xoxb-synthetic-held"}});
    f.add(first.clone()).await;
    let held = f.identity_secrets();
    assert_eq!(held.len(), 1);
    assert_eq!(held["AGENT1_SLACK_BOT_TOKEN"], "xoxb-synthetic-held");
    let view = f.get("/config/members/agent1").await;
    assert_eq!(
        view["accounts"]["slack"],
        json!({"user_id":"","has_bot_token":true,"has_app_token":false})
    );
    let mut other = agent("agent2");
    other["accounts"] = json!({"slack":{}});
    f.add(other).await;
    assert_eq!(f.identity_secrets(), held);
    assert_eq!(
        f.get("/config/members/agent2").await["accounts"]["slack"]["has_bot_token"],
        false
    );
    for id in ["agent1", "agent2"] {
        let bytes = std::fs::read_to_string(f.identities().join(id).join("identity.yml")).unwrap();
        assert!(!bytes.contains("xoxb-synthetic-held"));
    }
    assert_eq!(std::fs::read_dir(f.identities()).unwrap().count(), 2);
    first["accounts"] = json!({"slack":{"bot_token":"","app_token":""}});
    first["expected_revisions"] = view["revisions"].clone();
    assert_eq!(
        f.save("PUT", "/config/members/agent1", first).await.0,
        StatusCode::OK
    );
    assert_eq!(f.identity_secrets(), held);
    assert_eq!(
        f.workspace_secrets()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["OPENAI_API_KEY"]
    );
    assert!(!f.workspace().join(".env").exists());
    assert_eq!(
        f.call("DELETE", "/config/members/agent1", Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(f.identity_secrets(), held);
    assert_eq!(
        f.call("DELETE", "/identities/agent1", Value::Null).await.0,
        StatusCode::OK
    );
    assert!(f.identity_secrets().is_empty());
    assert_eq!(
        f.workspace_secrets()["OPENAI_API_KEY"],
        "sk-synthetic-workspace"
    );
    assert!(f.identities().join("agent2").is_dir());
}

#[tokio::test]
async fn avatar_import_finishes_in_the_workspace_it_started_reading() {
    let (f, double) = slack_fixture().await;
    *double.mode.lock().unwrap() = "wait".into();
    let switch = async {
        tokio::time::timeout(Duration::from_secs(2), async {
            while !double
                .calls
                .lock()
                .unwrap()
                .iter()
                .any(|(method, _, _)| method == "users.info")
            {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap();
        f.api.select(Selected::default());
        *double.mode.lock().unwrap() = String::new();
    };
    let ((status, answer), ()) = tokio::join!(
        f.call("POST", "/config/members/agent1/avatar/slack", Value::Null),
        switch
    );
    assert_eq!(status, StatusCode::OK, "{answer}");
    assert_eq!(
        std::fs::read(f.identities().join("agent1/avatar.png")).unwrap(),
        SLACK_PICTURE
    );
}
