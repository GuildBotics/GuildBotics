//! Setup HTTP contracts against temporary identity and Branvil services.
use super::*;
const SLACK_PICTURE: &[u8] = b"\x89PNG\r\n\x1a\nfixture";
async fn branvil(f: &Fixture) -> (branvil::api::Running, branvil::api::MemberApi, String) {
    let path = f.root.join("branvil");
    let token = branvil::api::initialize(&path, "owner1").unwrap();
    let running = branvil::api::Running::start(&path).await.unwrap();
    f.api.branvil.set(running.data.clone()).ok().unwrap();
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", &token);
    f.write(
        &f.root
            .join("identity/.guildbotics/config/connections/branvil.yml"),
        &json!({"base_url":running.data.base_url()}).to_string(),
    );
    let owner = running.data.authenticate(token.clone()).await.unwrap();
    (running, owner, token)
}
#[tokio::test]
async fn provider_listing_validation_localization_and_public_page() {
    let f = fixture();
    let providers = f.get("/config/providers").await;
    assert_eq!(
        providers,
        json!([
            {"name":"branvil","title":"Branvil","kinds":["ticket_service","code_hosting_service"],"avatar":false},
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
    let response = f
        .respond(
            Request::builder()
                .uri("/providers/branvil/pages/connected?state=forged")
                .header("host", "127.0.0.1:8765")
                .body(HttpBody::empty())
                .unwrap(),
        )
        .await;
    let (status, page) = answer(response).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(page.as_str().unwrap().contains(&crate::i18n::t(
        "en",
        "integrations.branvil.setup.expired",
        &[]
    )));
}
#[tokio::test]
async fn member_save_issues_keeps_replaces_and_removes_only_its_tokens() {
    let f = fixture();
    let (running, owner, _) = branvil(&f).await;
    f.init_with(json!({"language":"en","services":{"ticket_service":{"name":"branvil","workspace":"acme"}}})).await;
    let mut form = agent("agent1");
    form["accounts"] =
        json!({"branvil":{},"slack":{"bot_token":"xoxb-synthetic","app_token":"xapp-synthetic"}});
    f.add(form.clone()).await;
    let first = f.identity_secrets()["AGENT1_BRANVIL_TOKEN"].clone();
    assert_eq!(
        running
            .data
            .authenticate(first.clone())
            .await
            .unwrap()
            .identity()
            .await
            .unwrap()
            .name,
        "agent1"
    );
    let read = f.get("/config/members/agent1").await;
    assert_eq!(
        read["accounts"]["branvil"],
        json!({"username":"agent1","has_token":true})
    );
    assert_eq!(
        read["accounts"]["slack"],
        json!({"user_id":"","has_bot_token":true,"has_app_token":true})
    );
    form["accounts"]["branvil"] = json!({"username":"agent1"});
    form["accounts"]["slack"] = json!({});
    form["expected_revisions"] = read["revisions"].clone();
    assert_eq!(
        f.save("PUT", "/config/members/agent1", form.clone())
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(f.identity_secrets()["AGENT1_BRANVIL_TOKEN"], first);
    let current = f.get("/config/members/agent1").await;
    form["accounts"]["branvil"]["reissue"] = true.into();
    assert_eq!(
        f.save("PUT", "/config/members/agent1", form.clone())
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        running
            .data
            .authenticate(first.clone())
            .await
            .unwrap()
            .identity()
            .await
            .unwrap()
            .name,
        "agent1"
    );
    let tokens = owner
        .members_list_tokens(
            "agent1".into(),
            owner.page("tokens".into(), Some(100), None).unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(tokens.items.len(), 1);
    form["expected_revisions"] = current["revisions"].clone();
    assert_eq!(
        f.save("PUT", "/config/members/agent1", form).await.0,
        StatusCode::OK
    );
    let last = f.identity_secrets()["AGENT1_BRANVIL_TOKEN"].clone();
    assert_ne!(last, first);
    assert!(running.data.authenticate(first).await.is_err());
    assert_eq!(
        f.call("DELETE", "/config/members/agent1", Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    assert!(running.data.authenticate(last.clone()).await.is_ok());
    assert!(f.identities().join("agent1").is_dir());
    assert_eq!(
        f.call("DELETE", "/identities/agent1", Value::Null).await.0,
        StatusCode::OK
    );
    assert!(running.data.authenticate(last).await.is_err());
    assert!(!f.identities().join("agent1").exists());
    running.stop().await.unwrap();
}
#[tokio::test]
async fn disconnected_accounts_keep_input_humans_use_owner_and_wrong_kind_is_refused() {
    let f = fixture();
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{"username":"chosen"}});
    f.add(form.clone()).await;
    assert_eq!(
        f.get("/config/members/agent1").await["accounts"]["branvil"],
        json!({"username":"chosen","has_token":false})
    );
    let (running, _, _) = branvil(&f).await;
    let mut human = agent("human1");
    human["person_type"] = "human".into();
    human["accounts"] = json!({"branvil":{}});
    f.add(human).await;
    assert_eq!(
        f.get("/config/members/human1").await["accounts"]["branvil"]["username"],
        "owner1"
    );
    assert!(!f.identity_secrets().contains_key("HUMAN1_BRANVIL_TOKEN"));
    let mut wrong = agent("agent2");
    wrong["accounts"] = json!({"branvil":{"username":"owner1"}});
    assert_eq!(
        f.save("POST", "/config/members", wrong).await.0,
        StatusCode::BAD_REQUEST
    );
    assert!(!f.identities().join("agent2").exists());
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

async fn consent(
    f: &Fixture,
    running: &branvil::api::Running,
    owner_token: &str,
) -> (Value, String) {
    let (status, started) = f
        .call("POST", "/config/providers/branvil/connect", json!({}))
        .await;
    assert_eq!(status, StatusCode::OK, "{started}");
    let url = url::Url::parse(started["url"].as_str().unwrap()).unwrap();
    let query: BTreeMap<_, _> = url.query_pairs().into_owned().collect();
    assert_eq!(query["client"], "GuildBotics");
    assert_eq!(query["code_challenge_method"], "S256");
    let value:Value=reqwest::Client::new().post(format!("{}/api/v1/apps/authorizations",running.data.base_url())).bearer_auth(owner_token).json(&json!({"client":query["client"],"redirect_uri":query["redirect_uri"],"state":query["state"],"code_challenge":query["code_challenge"]})).send().await.unwrap().error_for_status().unwrap().json().await.unwrap();
    (started, value["redirect"].as_str().unwrap().to_owned())
}
async fn callback(f: &Fixture, url: &str) -> (StatusCode, Value) {
    let url = url::Url::parse(url).unwrap();
    answer(
        f.respond(
            Request::builder()
                .uri(format!("{}?{}", url.path(), url.query().unwrap_or("")))
                .header("host", "127.0.0.1:8765")
                .body(HttpBody::empty())
                .unwrap(),
        )
        .await,
    )
    .await
}
#[tokio::test]
async fn consent_pkce_callback_is_single_use_and_shared_across_workspaces() {
    let f = fixture();
    let (running, _, owner_token) = branvil(&f).await;
    let (started, redirect) = consent(&f, &running, &owner_token).await;
    let (_, pending) = f
        .call(
            "POST",
            "/config/providers/branvil/connection",
            json!({"state":started["state"]}),
        )
        .await;
    assert_eq!(pending["status"], "pending");
    let (status, page) = callback(&f, &redirect).await;
    assert_eq!(status, StatusCode::OK, "{page}");
    assert!(page.as_str().unwrap().contains(&crate::i18n::t(
        "en",
        "integrations.branvil.setup.connected",
        &[]
    )));
    let held = f.identity_secrets()["BRANVIL_ADMIN_TOKEN"].clone();
    assert_ne!(held, owner_token);
    assert_eq!(callback(&f, &redirect).await.0, StatusCode::OK);
    assert_eq!(f.identity_secrets()["BRANVIL_ADMIN_TOKEN"], held);
    let (_, connected) = f
        .call(
            "POST",
            "/config/providers/branvil/connection",
            json!({"state":started["state"]}),
        )
        .await;
    assert_eq!(connected["status"], "connected");
    assert_eq!(connected["owner"], "owner1");
    assert_eq!(connected["base_url"], running.data.base_url());
    let helper = f.api.home.join(".guildbotics/bin/guildbotics");
    let quoted = helper.to_string_lossy().replace('\'', "'\\''");
    assert_eq!(
        connected["git_config"],
        format!(
            "[credential \"{}\"]\n\thelper =\n\thelper = \"!'{quoted}' git-credential\"\n",
            running.data.base_url()
        )
    );
    assert_eq!(
        f.read(
            &f.root
                .join("identity/.guildbotics/config/connections/branvil.yml")
        ),
        json!({"base_url":running.data.base_url()})
    );
    assert_eq!(callback(&f, &redirect).await.0, StatusCode::BAD_REQUEST);
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
    let (_, link) = f
        .call("POST", "/config/providers/branvil/entry", json!({}))
        .await;
    assert!(
        link["url"]
            .as_str()
            .unwrap()
            .starts_with(&format!("{}/-/enter#", running.data.base_url()))
    );
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{}});
    f.add(form.clone()).await;
    let token = f.identity_secrets()["AGENT1_BRANVIL_TOKEN"].clone();
    let (_, agents) = f
        .call("POST", "/config/providers/branvil/agents", json!({}))
        .await;
    assert_eq!(agents["agents"], json!(["agent1"]));
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    assert_eq!(
        f.call("POST", "/workspace", json!({"workspace_dir":other}))
            .await
            .0,
        StatusCode::OK
    );
    let config = other.join(".guildbotics/config");
    assert_eq!(
        f.call(
            "POST",
            "/config/init",
            json!({"config_dir":config,"language":"en"})
        )
        .await
        .0,
        StatusCode::OK
    );
    let read = f.get("/config/members/agent1").await;
    assert_eq!(read["participating"], false);
    form["accounts"]["branvil"] = json!({"username":"agent1"});
    form["expected_revisions"] = read["revisions"].clone();
    form["config_dir"] = json!(config);
    assert_eq!(
        f.call("POST", "/config/members", form).await.0,
        StatusCode::OK
    );
    assert_eq!(f.identity_secrets()["AGENT1_BRANVIL_TOKEN"], token);
    assert_eq!(f.identity_secrets()["BRANVIL_ADMIN_TOKEN"], held);
    running.stop().await.unwrap();
}
#[tokio::test]
async fn denied_forged_spent_and_unkept_consents_do_not_connect() {
    let f = fixture();
    let (running, _, owner_token) = branvil(&f).await;
    for mode in ["denied", "forged", "spent", "locked"] {
        let (started, redirect) = consent(&f, &running, &owner_token).await;
        let old = f.identity_secrets()["BRANVIL_ADMIN_TOKEN"].clone();
        let redirect = match mode {
            "denied" => format!(
                "http://127.0.0.1:8765/providers/branvil/pages/connected?state={}&error=denied",
                started["state"].as_str().unwrap()
            ),
            "forged" => redirect.replace(started["state"].as_str().unwrap(), "forged"),
            "spent" => {
                let u = url::Url::parse(&redirect).unwrap();
                let q: BTreeMap<_, _> = u.query_pairs().into_owned().collect();
                assert!(
                    running
                        .data
                        .exchange(branvil::api::TokenRequest {
                            code: q["code"].clone(),
                            redirect_uri: "http://127.0.0.1:8765/providers/branvil/pages/connected"
                                .into(),
                            code_verifier: "wrong".into()
                        })
                        .await
                        .is_err()
                );
                redirect
            }
            _ => redirect,
        };
        if mode == "locked" {
            f.keys(Keys::Locked);
        }
        let (status, page) = callback(&f, &redirect).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{mode}: {page}");
        f.keys(Keys::Fixed);
        assert_eq!(f.identity_secrets()["BRANVIL_ADMIN_TOKEN"], old);
        if mode != "forged" {
            let (_, connection) = f
                .call(
                    "POST",
                    "/config/providers/branvil/connection",
                    json!({"state":started["state"]}),
                )
                .await;
            assert_eq!(connection["status"], "disconnected");
            let key = match mode {
                "denied" => "denied",
                "locked" => "not_kept",
                _ => "expired",
            };
            if mode != "spent" {
                let expected =
                    crate::i18n::t("en", &format!("integrations.branvil.setup.{key}"), &[]);
                assert_eq!(connection["message"], expected);
                assert!(page.as_str().unwrap().contains(&expected));
            }
            assert!(page.as_str().unwrap().contains(&crate::i18n::t(
                "en",
                "integrations.branvil.setup.retry",
                &[]
            )));
        }
    }
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
async fn provider_verify_and_diagnose_keep_credential_and_workspace_boundaries() {
    let f = fixture();
    let (running, owner, _) = branvil(&f).await;
    owner
        .repos_create_workspace(branvil::api::repos::NewWorkspace {
            name: "acme".into(),
        })
        .await
        .unwrap();
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{}});
    f.add(form).await;
    let registry = crate::setup_providers::Registry(Some(f.api.clone()));
    let id = "agent1".to_owned();
    let member = with_setup(&f.api, f.workspace(), move |s| s.saved_member(&id))
        .await
        .unwrap();
    let mut human = gb_setup::SavedMember {
        person_id: "human1".into(),
        name: "Human".into(),
        human: true,
        account_info: [("branvil_username".into(), "owner1".into())].into(),
        secrets: BTreeMap::new(),
        avatar: None,
    };
    for language in ["en", "ja"] {
        let checks = registry.verify("branvil", &member, false, language);
        assert_eq!(
            checks.iter().map(|c| c.target.as_str()).collect::<Vec<_>>(),
            ["account_info.branvil_username", "AGENT1_BRANVIL_TOKEN"]
        );
        assert!(checks.iter().all(|c| c.status == "ok" && !c.credential));
        assert_eq!(registry.verify("branvil", &human, false, language).len(), 1);
        assert!(registry.verify("slack", &human, true, language).is_empty());
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
    let mut unnamed = member.clone();
    unnamed.account_info.clear();
    assert_eq!(
        registry
            .verify("branvil", &unnamed, false, "en")
            .iter()
            .map(|c| c.status.as_str())
            .collect::<Vec<_>>(),
        ["error", "ok"]
    );
    assert!(
        registry
            .diagnose_branvil(std::slice::from_ref(&member), &json!({}), "en")
            .await
            .is_empty()
    );
    let both = json!({"code_hosting_service":{"name":"branvil","workspace":"acme"},"ticket_service":{"name":"branvil","workspace":"acme"}});
    let checks = registry
        .diagnose_branvil(&[member.clone(), member.clone()], &both, "en")
        .await;
    assert_eq!(
        checks
            .iter()
            .filter(|c| c.code == "branvil_workspace")
            .count(),
        1
    );
    let mut refused = member.clone();
    refused
        .secrets
        .insert("BRANVIL_TOKEN".into(), "refused".into());
    let checks = registry
        .diagnose_branvil(&[refused, human.clone()], &both, "en")
        .await;
    assert_eq!(
        checks
            .iter()
            .map(|c| (c.code.as_str(), c.status.as_str()))
            .collect::<Vec<_>>(),
        [
            ("branvil_reachable", "ok"),
            ("branvil_access", "error"),
            ("branvil_owner", "ok")
        ]
    );
    let held = f.identity_secrets()["BRANVIL_ADMIN_TOKEN"].clone();
    for (token, key) in [
        ("", "not_connected"),
        ("refused", "admin_refused"),
        (member.secret("BRANVIL_TOKEN"), "admin_refused"),
    ] {
        f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", token);
        let checks = registry
            .diagnose_branvil(&[human.clone()], &both, "ja")
            .await;
        assert_eq!(checks[1].status, "error");
        assert!(checks[1].credential);
        assert_eq!(checks[1].person_id, "human1");
        assert_eq!(
            checks[1].message,
            crate::i18n::t("ja", &format!("integrations.branvil.api.{key}"), &[])
        );
    }
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", &held);
    let services = json!({"code_hosting_service":{"name":"branvil","workspace":"missing"},"ticket_service":{"name":"branvil","workspace":"acme"}});
    let checks = registry
        .diagnose_branvil(&[member, human], &services, "en")
        .await;
    assert_eq!(
        checks
            .iter()
            .map(|c| (c.code.as_str(), c.status.as_str(), c.credential))
            .collect::<Vec<_>>(),
        [
            ("branvil_reachable", "ok", false),
            ("branvil_access", "ok", true),
            ("branvil_workspace", "error", false),
            ("branvil_workspace", "ok", false),
            ("branvil_owner", "ok", true)
        ]
    );
    human = gb_setup::SavedMember {
        person_id: "human1".into(),
        name: "Human".into(),
        human: true,
        account_info: [("branvil_username".into(), "someone".into())].into(),
        secrets: BTreeMap::new(),
        avatar: None,
    };
    assert_eq!(
        registry.diagnose_branvil(&[human], &services, "en").await[1].status,
        "error"
    );
    running.stop().await.unwrap();
    let stopped = registry.diagnose_branvil(&[], &services, "ja").await;
    assert_eq!(stopped.len(), 1);
    assert_eq!(
        stopped[0].message,
        crate::i18n::t("ja", "integrations.branvil.api.stopped", &[])
    );
    assert!(!stopped[0].credential);
}

#[tokio::test]
async fn account_settlement_preserves_later_saves_and_unrelated_tokens_and_shares_pictures() {
    use gb_setup::ProviderModels;
    let f = fixture();
    let (running, owner, _) = branvil(&f).await;
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{}});
    f.add(form).await;
    let registry = crate::setup_providers::Registry(Some(f.api.clone()));
    let mut member = with_setup(&f.api, f.workspace(), |s| s.saved_member("agent1"))
        .await
        .unwrap();
    let initial = member.secret("BRANVIL_TOKEN").to_owned();
    assert!(
        owner
            .members_get("agent1".into())
            .await
            .unwrap()
            .avatar_url
            .is_none()
    );
    let unrelated = owner
        .members_issue_token(
            "agent1".into(),
            branvil::api::members::NewToken {
                label: "cli".into(),
                replace: false,
            },
        )
        .await
        .unwrap();
    let mut unavailable = registry
        .account("branvil", &json!({"username":"agent1","reissue":true}))
        .unwrap();
    unavailable.prepare(&member).await.unwrap();
    let unrevoked = unavailable.stored("agent1", false).unwrap().secrets["BRANVIL_TOKEN"].clone();
    f.keys(Keys::Locked);
    unavailable.settled(false).await;
    f.keys(Keys::Fixed);
    assert!(running.data.authenticate(unrevoked).await.is_ok());
    let mut a = registry
        .account("branvil", &json!({"username":"agent1","reissue":true}))
        .unwrap();
    a.prepare(&member).await.unwrap();
    let stored_a = a.stored("agent1", false).unwrap();
    assert_eq!(
        running
            .data
            .authenticate(initial.clone())
            .await
            .unwrap()
            .identity()
            .await
            .unwrap()
            .name,
        "agent1"
    );
    let token_a = stored_a.secrets["BRANVIL_TOKEN"].clone();
    member.secrets.extend(stored_a.secrets);
    let mut b = registry
        .account("branvil", &json!({"username":"agent1","reissue":true}))
        .unwrap();
    b.prepare(&member).await.unwrap();
    let stored_b = b.stored("agent1", false).unwrap();
    let kept = stored_b.secrets["BRANVIL_TOKEN"].clone();
    b.settled(true).await;
    a.settled(true).await;
    assert!(running.data.authenticate(token_a).await.is_err());
    assert!(running.data.authenticate(initial).await.is_err());
    assert!(running.data.authenticate(kept.clone()).await.is_ok());
    assert!(
        running
            .data
            .authenticate(unrelated.token.clone())
            .await
            .is_ok()
    );
    member.secrets.extend(stored_b.secrets);
    let mut refused = registry
        .account("branvil", &json!({"username":"agent1","reissue":true}))
        .unwrap();
    refused.prepare(&member).await.unwrap();
    let refused_token = refused.stored("agent1", false).unwrap().secrets["BRANVIL_TOKEN"].clone();
    refused.settled(false).await;
    assert!(running.data.authenticate(refused_token).await.is_err());
    assert!(running.data.authenticate(kept.clone()).await.is_ok());
    let directory = f.identities().join("agent1");
    f.write(&directory.join("avatar.png"), "invalid image");
    member.avatar = Some(directory.join("avatar.png"));
    let mut failed = registry
        .account("branvil", &json!({"username":"agent1","reissue":true}))
        .unwrap();
    assert!(failed.prepare(&member).await.is_err());
    let issued = failed.stored("agent1", false).unwrap().secrets["BRANVIL_TOKEN"].clone();
    failed.settled(false).await;
    assert!(running.data.authenticate(issued).await.is_err());
    let png = b"\x89PNG\r\n\x1a\nfixture";
    std::fs::write(directory.join("avatar.png"), png).unwrap();
    let (status, _) = f.upload("agent1", "picture.png", png).await;
    assert_eq!(status, StatusCode::OK);
    registry.avatar_changed("branvil", &member).await.unwrap();
    let bytes = reqwest::Client::new()
        .get(format!(
            "{}/api/v1/members/agent1/avatar",
            running.data.base_url()
        ))
        .bearer_auth(&unrelated.token)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .bytes()
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), png);
    let mut chosen = registry
        .account("branvil", &json!({"username":"agent1"}))
        .unwrap();
    chosen.prepare(&member).await.unwrap();
    assert!(chosen.stored("other-id", false).unwrap().secrets.is_empty());
    chosen.settled(true).await;
    // Revocation of one token elsewhere never prevents removal of the rest.
    owner
        .members_revoke_token("agent1".into(), member.secret("BRANVIL_TOKEN_ID").into())
        .await
        .unwrap();
    registry.removed("branvil", &member).await.unwrap();
    assert!(
        running
            .data
            .authenticate(unrelated.token.clone())
            .await
            .is_ok()
    );
    assert!(owner.members_get("agent1".into()).await.is_ok());
    let tokens = owner
        .members_list_tokens(
            "agent1".into(),
            owner.page("tokens".into(), Some(100), None).unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        tokens
            .items
            .iter()
            .map(|t| t.label.as_str())
            .collect::<Vec<_>>(),
        ["cli"]
    );
    member.human = true;
    registry.removed("branvil", &member).await.unwrap();
    assert!(running.data.authenticate(unrelated.token).await.is_ok());
    running.stop().await.unwrap();
}

#[tokio::test]
async fn failed_writes_keep_any_token_already_stored_and_failed_preparation_rolls_back() {
    let f = fixture();
    let (running, owner, _) = branvil(&f).await;
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{}});
    f.add(form.clone()).await;
    let initial = f.identity_secrets()["AGENT1_BRANVIL_TOKEN"].clone();
    form["accounts"]["branvil"] = json!({"username":"agent1","reissue":true});
    f.write(&f.identities().join("agent1/avatar.png"), "bad image");
    assert_eq!(
        f.save("PUT", "/config/members/agent1", form.clone())
            .await
            .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(f.identity_secrets()["AGENT1_BRANVIL_TOKEN"], initial);
    let tokens = owner
        .members_list_tokens(
            "agent1".into(),
            owner.page("tokens".into(), Some(100), None).unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(tokens.items.len(), 1);
    std::fs::remove_file(f.identities().join("agent1/avatar.png")).unwrap();
    let path = f.config().join("team/members/agent1/member.yml");
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();
    // No revision was sent: the failure is after secret storage began.
    assert_eq!(
        f.save("PUT", "/config/members/agent1", form).await.0,
        StatusCode::INTERNAL_SERVER_ERROR
    );
    let stored = f.identity_secrets()["AGENT1_BRANVIL_TOKEN"].clone();
    assert_ne!(stored, initial);
    assert!(running.data.authenticate(stored).await.is_ok());
    running.stop().await.unwrap();
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
        avatar: None,
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
async fn absent_or_refused_connection_cannot_administer_save_or_delete_members() {
    let f = fixture();
    assert_eq!(
        f.call("POST", "/config/providers/branvil/connection", json!({}))
            .await
            .1["status"],
        "disconnected"
    );
    assert_eq!(
        f.call("POST", "/config/providers/branvil/workspaces", json!({}))
            .await
            .0,
        StatusCode::BAD_REQUEST
    );
    let (running, owner, _) = branvil(&f).await;
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{}});
    f.add(form).await;
    let held = f.identity_secrets()["BRANVIL_ADMIN_TOKEN"].clone();
    let agent_token = f.identity_secrets()["AGENT1_BRANVIL_TOKEN"].clone();
    for token in ["", "invalid", agent_token.as_str()] {
        f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", token);
        let (_, v) = f
            .call("POST", "/config/providers/branvil/connection", json!({}))
            .await;
        assert_eq!(v["status"], "disconnected");
        let key = if token.is_empty() {
            "not_connected"
        } else {
            "admin_refused"
        };
        assert_eq!(
            v["message"],
            crate::i18n::t("en", &format!("integrations.branvil.api.{key}"), &[])
        );
        for lang in ["en", "ja"] {
            let (status, body) = f
                .call_in(lang, "POST", "/config/providers/branvil/agents", json!({}))
                .await;
            assert_eq!(status, StatusCode::BAD_REQUEST);
            assert_eq!(
                body["message"],
                crate::i18n::t(lang, &format!("integrations.branvil.api.{key}"), &[])
            );
            let mut input = agent("rejected");
            input["accounts"] = json!({"branvil":{}});
            input["config_dir"] = json!(f.config());
            let (_, body) = f.call_in(lang, "POST", "/config/members", input).await;
            assert_eq!(
                body["message"],
                crate::i18n::t(lang, &format!("integrations.branvil.api.{key}"), &[])
            );
        }
        assert_eq!(
            f.call("POST", "/config/providers/branvil/agents", json!({}))
                .await
                .0,
            StatusCode::BAD_REQUEST
        );
        let mut form = agent("new_agent");
        form["accounts"] = json!({"branvil":{}});
        assert_eq!(
            f.save("POST", "/config/members", form).await.0,
            StatusCode::BAD_REQUEST
        );
        assert!(!f.identities().join("new_agent").exists());
        assert_eq!(
            f.call("DELETE", "/identities/agent1", Value::Null).await.0,
            StatusCode::BAD_REQUEST
        );
        assert!(f.identities().join("agent1").exists());
        assert!(f.config().join("team/members/agent1/member.yml").is_file());
        assert_eq!(f.identity_secrets()["AGENT1_BRANVIL_TOKEN"], agent_token);
        assert!(running.data.authenticate(agent_token.clone()).await.is_ok());
    }
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", &held);
    let mut chosen = agent("other_id");
    chosen["accounts"] = json!({"branvil":{"username":"agent1"}});
    f.add(chosen).await;
    assert!(owner.members_get("other_id".into()).await.is_err());
    let view = f.get("/config/members/other_id").await;
    assert_eq!(view["accounts"]["branvil"]["username"], "agent1");
    running.stop().await.unwrap();
}

#[tokio::test]
async fn every_provider_operation_requires_owner_authentication() {
    let f = fixture();
    for path in [
        "/config/providers/branvil/connect",
        "/config/providers/branvil/connection",
        "/config/providers/branvil/workspaces",
        "/config/providers/branvil/workspaces/create",
        "/config/providers/branvil/agents",
        "/config/providers/branvil/entry",
        "/config/providers/slack/app-registrations",
        "/config/providers/slack/verify-tokens",
        "/config/members/agent1/avatar/slack",
    ] {
        let response = f
            .respond(
                Request::builder()
                    .method("POST")
                    .uri(format!("/api{path}"))
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
async fn consent_exchange_and_storage_finish_after_the_browser_request_is_dropped() {
    let mut f = fixture();
    Arc::get_mut(&mut f.api).unwrap().write_timeout = Duration::from_secs(2);
    let (running, _, owner_token) = branvil(&f).await;
    let (_, redirect) = consent(&f, &running, &owner_token).await;
    let held = IdentityLock::acquire(&f.root.join("identity"), Duration::from_secs(1)).unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(100), callback(&f, &redirect))
            .await
            .is_err()
    );
    assert_eq!(f.api.work.len(), 1);
    drop(held);
    f.api.work.close();
    f.api.work.wait().await;
    let token = f.identity_secrets()["BRANVIL_ADMIN_TOKEN"].clone();
    assert_ne!(token, owner_token);
    assert!(running.data.authenticate(token).await.is_ok());
    assert_eq!(callback(&f, &redirect).await.0, StatusCode::OK);
    running.stop().await.unwrap();
}

#[tokio::test]
async fn account_hooks_preserve_each_reissue_settlement_picture_and_removal_branch() {
    use gb_setup::ProviderModels;
    let f = fixture();
    let (running, owner, admin_token) = branvil(&f).await;
    f.init().await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{}});
    f.add(form).await;
    let registry = crate::setup_providers::Registry(Some(f.api.clone()));
    let member = with_setup(&f.api, f.workspace(), |s| s.saved_member("agent1"))
        .await
        .unwrap();
    let mut other = registry
        .account("branvil", &json!({"username":"agent2"}))
        .unwrap();
    other.prepare(&member).await.unwrap();
    let other_token = other.stored("agent2", false).unwrap().secrets["BRANVIL_TOKEN"].clone();
    other.settled(true).await;
    // Revoked, another member's token, and explicit reissue all issue beside a held token.
    for (held, reissue) in [
        ("revoked", false),
        (other_token.as_str(), false),
        (member.secret("BRANVIL_TOKEN"), true),
    ] {
        let mut input = member.clone();
        input.secrets.insert("BRANVIL_TOKEN".into(), held.into());
        let mut account = registry
            .account("branvil", &json!({"username":"agent1","reissue":reissue}))
            .unwrap();
        account.prepare(&input).await.unwrap();
        let issued = account.stored("agent1", false).unwrap().secrets["BRANVIL_TOKEN"].clone();
        assert_ne!(issued, held);
        assert!(running.data.authenticate(issued.clone()).await.is_ok());
        account.settled(false).await;
        assert!(running.data.authenticate(issued).await.is_err());
    }
    for later_first in [false, true] {
        let mut earlier = registry
            .account("branvil", &json!({"username":"agent1","reissue":true}))
            .unwrap();
        earlier.prepare(&member).await.unwrap();
        let mut input = member.clone();
        input
            .secrets
            .extend(earlier.stored("agent1", false).unwrap().secrets);
        let mut later = registry
            .account("branvil", &json!({"username":"agent1","reissue":true}))
            .unwrap();
        later.prepare(&input).await.unwrap();
        let held = later.stored("agent1", false).unwrap().secrets["BRANVIL_TOKEN"].clone();
        if later_first {
            later.settled(true).await;
            earlier.settled(true).await;
        } else {
            earlier.settled(true).await;
            later.settled(true).await;
        }
        assert!(running.data.authenticate(held).await.is_ok());
    }
    let png = b"\x89PNG\r\n\x1a\nfixture";
    let path = f.root.join("picture.png");
    std::fs::write(&path, png).unwrap();
    for human in [false, true] {
        let mut input = member.clone();
        input.human = human;
        input.avatar = Some(path.clone());
        let mut account = registry.account("branvil", &json!({})).unwrap();
        account.prepare(&input).await.unwrap();
        let name = if human { "owner1" } else { "agent1" };
        assert!(
            owner
                .members_get(name.into())
                .await
                .unwrap()
                .avatar_url
                .is_some()
        );
        account.settled(false).await;
    }
    let mut unnamed = member.clone();
    unnamed.account_info.clear();
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", "refused");
    registry.avatar_changed("branvil", &unnamed).await.unwrap();
    registry.removed("branvil", &unnamed).await.unwrap();
    let disconnected = crate::setup_providers::Registry(None);
    disconnected
        .avatar_changed("branvil", &member)
        .await
        .unwrap();
    disconnected.removed("branvil", &member).await.unwrap();
    // Missing members are already removed; this does not turn deletion into an error.
    f.seed_identity_secret("BRANVIL_ADMIN_TOKEN", &admin_token);
    unnamed
        .account_info
        .insert("branvil_username".into(), "already_gone".into());
    registry.removed("branvil", &unnamed).await.unwrap();
    running.stop().await.unwrap();
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

#[tokio::test]
async fn provider_and_uploaded_avatars_are_given_to_the_named_branvil_member() {
    let (f, _double) = slack_fixture().await;
    let (running, _, _) = branvil(&f).await;
    let mut form = agent("agent1");
    form["accounts"] = json!({"branvil":{},"slack":{}});
    assert_eq!(
        f.save("PUT", "/config/members/agent1", form).await.0,
        StatusCode::OK
    );
    let token = f.identity_secrets()["AGENT1_BRANVIL_TOKEN"].clone();
    assert_eq!(
        f.call("POST", "/config/members/agent1/avatar/slack", Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    let remote = format!("{}/api/v1/members/agent1/avatar", running.data.base_url());
    let http = reqwest::Client::new();
    assert_eq!(
        http.get(&remote)
            .bearer_auth(&token)
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap()
            .bytes()
            .await
            .unwrap()
            .as_ref(),
        SLACK_PICTURE
    );
    let changed = b"\x89PNG\r\n\x1a\nchanged";
    assert_eq!(
        f.upload("agent1", "changed.png", changed).await.0,
        StatusCode::OK
    );
    assert_eq!(
        http.get(&remote)
            .bearer_auth(&token)
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap()
            .bytes()
            .await
            .unwrap()
            .as_ref(),
        changed
    );
    running.stop().await.unwrap();
}

#[tokio::test]
async fn callback_keeps_the_initiating_language_and_retry_in_browser_and_poll() {
    let f = fixture();
    let (running, _, _) = branvil(&f).await;
    for lang in ["en", "ja"] {
        let (status, started) = f
            .call_in(lang, "POST", "/config/providers/branvil/connect", json!({}))
            .await;
        assert_eq!(status, StatusCode::OK);
        let redirect = format!(
            "http://127.0.0.1:8765/providers/branvil/pages/connected?state={}&error=denied",
            started["state"].as_str().unwrap()
        );
        let (status, page) = callback(&f, &redirect).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        for key in ["failed", "denied", "retry"] {
            assert!(page.as_str().unwrap().contains(&crate::i18n::t(
                lang,
                &format!("integrations.branvil.setup.{key}"),
                &[]
            )));
        }
        let (_, polled) = f
            .call(
                "POST",
                "/config/providers/branvil/connection",
                json!({"state":started["state"]}),
            )
            .await;
        assert_eq!(
            polled["message"],
            crate::i18n::t(lang, "integrations.branvil.setup.denied", &[])
        );
    }
    running.stop().await.unwrap();
}

#[tokio::test]
async fn expired_consents_cannot_poll_or_exchange_without_another_connect() {
    let f = fixture();
    let (running, _, _) = branvil(&f).await;
    let held = f.identity_secrets()["BRANVIL_ADMIN_TOKEN"].clone();
    for poll_first in [false, true] {
        let (_, started) = f
            .call_in("ja", "POST", "/config/providers/branvil/connect", json!({}))
            .await;
        let state = started["state"].as_str().unwrap();
        f.api.consents.lock().await.get_mut(state).unwrap().created -= Duration::from_secs(601);
        let expired = crate::i18n::t("ja", "integrations.branvil.setup.expired", &[]);
        if poll_first {
            let (_, polled) = f
                .call(
                    "POST",
                    "/config/providers/branvil/connection",
                    json!({"state":state}),
                )
                .await;
            assert_eq!(polled["status"], "disconnected");
            assert_eq!(polled["message"], expired);
        } else {
            let url = format!(
                "http://127.0.0.1:8765/providers/branvil/pages/connected?state={state}&error=denied"
            );
            let (status, page) = callback(&f, &url).await;
            assert_eq!(status, StatusCode::BAD_REQUEST);
            assert!(page.as_str().unwrap().contains(&expired));
        }
        assert_eq!(f.identity_secrets()["BRANVIL_ADMIN_TOKEN"], held);
    }
    running.stop().await.unwrap();
}

#[tokio::test]
async fn completed_consents_keep_their_outcomes_after_the_pending_deadline() {
    let f = fixture();
    let (running, _, owner) = branvil(&f).await;
    for mode in ["connected", "denied", "not_kept"] {
        let (started, redirect) = consent(&f, &running, &owner).await;
        let state = started["state"].as_str().unwrap();
        let redirect = if mode == "denied" {
            format!(
                "http://127.0.0.1:8765/providers/branvil/pages/connected?state={state}&error=denied"
            )
        } else {
            redirect
        };
        if mode == "not_kept" {
            f.keys(Keys::Locked);
        }
        let initial = callback(&f, &redirect).await;
        f.keys(Keys::Fixed);
        assert_eq!(
            initial.0,
            if mode == "connected" {
                StatusCode::OK
            } else {
                StatusCode::BAD_REQUEST
            }
        );
        assert!(initial.1.as_str().unwrap().contains(&crate::i18n::t(
            "en",
            &format!("integrations.branvil.setup.{mode}"),
            &[]
        )));
        let stored = f.identity_secrets();
        f.api.consents.lock().await.get_mut(state).unwrap().created -= Duration::from_secs(601);
        assert_eq!(
            callback(&f, &redirect).await,
            initial,
            "{mode}: completed page"
        );
        assert_eq!(
            f.identity_secrets(),
            stored,
            "replay must not exchange or store again"
        );
        let (_, polled) = f
            .call(
                "POST",
                "/config/providers/branvil/connection",
                json!({"state":state}),
            )
            .await;
        assert_eq!(
            polled["status"],
            if mode == "connected" {
                "connected"
            } else {
                "disconnected"
            }
        );
        assert_eq!(
            polled["message"],
            if mode == "connected" {
                String::new()
            } else {
                crate::i18n::t("en", &format!("integrations.branvil.setup.{mode}"), &[])
            }
        );
        assert!(
            !f.api.consents.lock().await.contains_key(state),
            "poll consumes the result"
        );
    }
    // Starting another consent still discards old completed entries, as the frozen start does.
    let (_, old) = consent(&f, &running, &owner).await;
    let old_url = url::Url::parse(&old).unwrap();
    let state = old_url
        .query_pairs()
        .find(|(key, _)| key == "state")
        .unwrap()
        .1
        .into_owned();
    callback(
        &f,
        &format!(
            "http://127.0.0.1:8765/providers/branvil/pages/connected?state={state}&error=denied"
        ),
    )
    .await;
    f.api.consents.lock().await.get_mut(&state).unwrap().created -= Duration::from_secs(601);
    f.call("POST", "/config/providers/branvil/connect", json!({}))
        .await;
    assert!(!f.api.consents.lock().await.contains_key(&state));
    running.stop().await.unwrap();
}
