//! Observe the pinned binaries using the same stand-in formats as a turn.

#[path = "recorder.rs"]
mod recorder;

use super::*;
use gb_environment::gateway::{Policy, Provider};
use recorder::{HOST, Recorder, Seen, reply};

const STAND_IN_SUFFIX: &str = "53594e544845544943343539";
const ANTIGRAVITY_HOSTS: &[&str] = &[
    "www.googleapis.com",
    "lh3.googleusercontent.com",
    "oauth2.googleapis.com",
];
const COPILOT_MODELS: &[(&str, &str, &str)] = &[
    ("gpt-5.2-codex", "/responses", "OpenAI"),
    ("claude-sonnet-4.5", "/v1/messages", "Anthropic"),
    ("gpt-5-mini", "/chat/completions", "OpenAI"),
];

struct Probe {
    device: Device,
    catalog: Value,
    recorder: Recorder,
}

impl Probe {
    async fn start(
        tool: &str,
        names: &[&str],
        answer: impl Fn(&Seen) -> Option<hyper::Response<axum::body::Body>> + Send + Sync + 'static,
    ) -> Self {
        let device = Device::new();
        let catalog = device
            .catalog()
            .into_iter()
            .find(|entry| entry["name"] == tool)
            .unwrap();
        let recorder = Recorder::start(names, answer).await;
        Self {
            device,
            catalog,
            recorder,
        }
    }

    fn broker(&self) -> &Value {
        &self.catalog["provision"]["credential_broker"]
    }

    fn prepare(&self, case: &str, files: Files, proxy: bool) -> PrivatePlan {
        let mut plan = private_plan(
            &self.device.vm,
            &self.catalog,
            files,
            vec![
                PYTHON.into(),
                "-B".into(),
                "-c".into(),
                include_str!("run_cli.py").into(),
                case.into(),
            ],
            false,
        )
        .unwrap();
        plan.network = Network {
            host_ports: vec![self.recorder.http, self.recorder.tls],
            ..Network::default()
        };
        let ca = format!("{}/contract-ca.pem", plan.home);
        plan.injection.insert(ca.clone(), self.recorder.ca.clone());
        plan.env.extend([
            ("NODE_EXTRA_CA_CERTS".into(), ca.clone()),
            ("SSL_CERT_FILE".into(), ca),
            ("CONTRACT_HTTP_URL".into(), self.recorder.url(false)),
            ("CONTRACT_TLS_PORT".into(), self.recorder.tls.to_string()),
        ]);
        if proxy {
            for name in ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"] {
                plan.env.push((name.into(), self.recorder.url(false)));
            }
            for name in ["NO_PROXY", "no_proxy"] {
                plan.env.push((name.into(), HOST.into()));
            }
        }
        let broker = self.broker();
        let base_url =
            self.recorder.url(broker["tls"] == true) + broker["base_url_path"].as_str().unwrap();
        for name in broker["base_url_env"].as_array().unwrap() {
            plan.env
                .push((name.as_str().unwrap().into(), base_url.clone()));
        }
        plan
    }

    fn turn(&self, case: &str, proxy: bool) -> (PrivatePlan, String) {
        let auth = self.catalog["provision"]["auth"].as_str().unwrap();
        let files = credentials_for(&self.catalog);
        let mut document: Value = serde_json::from_slice(&files[auth]).unwrap();
        match case {
            "codex" => {
                let claims = json!({
                    "email": "synthetic@example.com", "exp": 4102444800_u64,
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "plus", "chatgpt_account_id": "account-459",
                    }
                });
                document["tokens"]["account_id"] = json!("account-459");
                document["last_refresh"] = json!(chrono::Utc::now().to_rfc3339());
                document["tokens"]["id_token"] = json!(format!(
                    "{}.{}.synthetic",
                    URL_SAFE_NO_PAD.encode(br#"{"alg":"none"}"#),
                    URL_SAFE_NO_PAD.encode(serde_json::to_vec(&claims).unwrap()),
                ));
            }
            "claude" => {
                document["claudeAiOauth"]["scopes"] = json!(["user:inference", "user:profile"]);
            }
            _ => {}
        }
        let broker = self.broker();
        let prefix = broker["stand_in_prefix"].as_str().unwrap();
        let (document, token) =
            stand_in(&document, broker, &format!("{prefix}{STAND_IN_SUFFIX}")).unwrap();
        let env_token = broker["stand_in_env"].as_str().unwrap();
        let env_command = broker["stand_in_command_env"].as_str().unwrap();
        let files = if env_token.is_empty() && env_command.is_empty() {
            Files::from([(auth.to_owned(), serde_json::to_vec(&document).unwrap())])
        } else {
            Files::new()
        };
        let mut plan = self.prepare(case, files, proxy);
        if !env_token.is_empty() {
            plan.env.push((env_token.into(), token.clone()));
        }
        if !env_command.is_empty() {
            // The command reads a file of its own, as in a brokered turn.
            let path = format!("{}/stand-in.json", plan.root);
            plan.injection.insert(
                path.clone(),
                serde_json::to_vec(&json!({"access_token":token, "expires_in":31536000})).unwrap(),
            );
            plan.env.push((
                env_command.into(),
                format!("cat '{}'", path.replace('\'', "'\"'\"'")),
            ));
        }
        for name in broker["stand_in_also_env"].as_array().unwrap() {
            plan.env
                .push((name.as_str().unwrap().into(), token.clone()));
        }
        for pair in broker["turn_environment"].as_array().unwrap() {
            plan.env.push((
                pair[0].as_str().unwrap().into(),
                pair[1].as_str().unwrap().into(),
            ));
        }
        (plan, token)
    }

    async fn run(&self, plan: PrivatePlan) -> (Value, Files) {
        let runtime_home = self.device.vm.data.join("msb");
        let diagnostics = Mutex::new(Vec::new());
        let result = tokio::time::timeout(
            Duration::from_secs(160),
            runtime::run_private_session(
                &plan.boot(&self.device.vm, &runtime_home),
                &plan.process(),
                &plan.injection,
                &plan.capture,
                |chunk| {
                    let mut output = diagnostics.lock().unwrap();
                    let remaining = 16_384_usize.saturating_sub(output.len());
                    output.extend_from_slice(&chunk[..chunk.len().min(remaining)]);
                },
            ),
        )
        .await
        .expect("provider contract probe timed out")
        .unwrap();
        let ended = result.ended.unwrap();
        assert_eq!(
            ended.code,
            0,
            "{}; requests: {:?}",
            String::from_utf8_lossy(&diagnostics.into_inner().unwrap()),
            self.recorder.seen()
        );
        (
            serde_json::from_slice(&ended.reply).unwrap(),
            result.files.unwrap(),
        )
    }

    fn policy(&self) -> Policy {
        let broker = self.broker();
        Policy {
            provider: Provider::OpenAi,
            origin: broker["upstream"].as_str().unwrap().into(),
            routes: broker["routes"]
                .as_array()
                .unwrap()
                .iter()
                .map(|route| {
                    let (method, path) = route.as_str().unwrap().split_once(' ').unwrap();
                    (method.into(), path.into())
                })
                .collect(),
        }
    }
}

fn assert_bearer(seen: &[Seen], method: &str, path: &str, token: &str) {
    let matches: Vec<_> = seen
        .iter()
        .filter(|request| request.method == method && request.path == path)
        .collect();
    assert!(!matches.is_empty(), "missing {method} {path} in {seen:?}");
    let expected = format!("Bearer {token}");
    assert!(
        matches
            .iter()
            .all(|request| request.authorization == expected),
        "{matches:?}"
    );
}

fn assert_token_hosts(seen: &[Seen], token: &str, expected: &[&str]) {
    use std::collections::BTreeSet;
    let hosts: BTreeSet<_> = seen
        .iter()
        .filter(|r| r.authorization.contains(token))
        .map(|r| r.host.as_str())
        .collect();
    assert_eq!(hosts, expected.iter().copied().collect(), "{seen:?}");
}

fn json_reply(status: u16, body: Value) -> hyper::Response<axum::body::Body> {
    reply(status, "application/json", body.to_string())
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_claude_separates_inference_and_account_traffic() {
    let probe = Probe::start("claude", &["api.anthropic.com"], |_| None).await;
    let (plan, token) = probe.turn("claude", true);
    probe.run(plan).await;
    let seen = probe.recorder.seen();
    let (inference, usage) = seen.split_at(
        seen.iter()
            .position(|r| r.path == "/fixture-usage")
            .unwrap(),
    );
    assert_bearer(inference, "POST", "/v1/messages", &token);
    assert!(
        inference
            .iter()
            .filter(|r| r.path == "/v1/messages")
            .all(|r| r.host == HOST)
    );
    let direct: std::collections::BTreeSet<_> = inference
        .iter()
        .filter(|r| r.kind != "connect" && r.host != HOST)
        .map(|r| (r.host.as_str(), r.method.as_str(), r.path.as_str()))
        .collect();
    assert_eq!(
        direct,
        [("api.anthropic.com", "GET", "/api/oauth/profile")].into(),
        "{inference:?}"
    );
    assert!(
        usage
            .iter()
            .any(|r| r.host == "api.anthropic.com" && r.authorization == format!("Bearer {token}")),
        "{usage:?}"
    );
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_grok_uses_external_auth_and_refuses_billing() {
    let probe = Probe::start("grok", &[], |_| None).await;
    let (plan, token) = probe.turn("grok", true);
    let (out, _) = probe.run(plan).await;
    assert_eq!(out["billing"], "Authentication required");
    let seen = probe.recorder.seen();
    for path in ["/v1/user", "/v1/settings", "/v1/models"] {
        assert_bearer(&seen, "GET", path, &token);
    }
    assert!(
        seen.iter()
            .filter(|r| r.path.starts_with("/v1/") && r.path != "/v1/login-config")
            .all(|r| r.authorization == format!("Bearer {token}")),
        "{seen:?}"
    );
    assert_token_hosts(&seen, &token, &[HOST]);
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_codex_uses_its_model_provider_and_chatgpt_base() {
    let probe = Probe::start("codex", &[], |r| {
        (r.path == "/backend-api/wham/accounts/check").then(|| json_reply(200, json!({
            "accounts": [{
                "id": "account-459", "name": null, "plan_type": "plus", "structure": "personal",
                "workspace_backend_origin": "NO_CONSTRAINT", "account_routing_override": "NO_CONSTRAINT",
                "profile_picture_url": null,
            }],
            "default_account_id": "account-459", "account_ordering": ["account-459"],
        })))
    }).await;
    let (plan, token) = probe.turn("codex", true);
    let (out, _) = probe.run(plan).await;
    assert_eq!(out["account"]["email"], "synthetic@example.com");
    assert_eq!(out["account"]["planType"], "plus");
    let seen = probe.recorder.seen();
    assert_bearer(&seen, "POST", "/backend-api/codex/responses", &token);
    for path in [
        "/backend-api/wham/usage",
        "/backend-api/wham/accounts/check",
    ] {
        assert_bearer(&seen, "GET", path, &token);
    }
    assert!(
        seen.iter()
            .filter(|r| r.path == "/backend-api/codex/responses")
            .all(|r| r.headers.contains_key("chatgpt-account-id"))
    );
    let policy = probe.policy();
    for request in seen.iter().filter(|r| r.path.starts_with("/backend-api/")) {
        assert_eq!(request.kind, "tls");
        if !request.path.contains("analytics")
            && (request.path.starts_with("/backend-api/codex/")
                || matches!(
                    request.path.as_str(),
                    "/backend-api/wham/usage" | "/backend-api/wham/accounts/check"
                ))
        {
            assert!(
                policy.target(&request.method, &request.path).is_ok(),
                "{request:?}"
            );
        }
    }
    assert_token_hosts(&seen, &token, &[HOST]);
    assert!(
        !seen
            .iter()
            .any(|r| r.path == "/oauth/token"
                || (r.kind == "connect" && r.host == "auth.openai.com")),
        "{seen:?}"
    );
}

fn copilot_answer(request: &Seen) -> Option<hyper::Response<axum::body::Body>> {
    let body = match request.path.as_str() {
        "/copilot_internal/user" => json!({
            "login": "synthetic459", "id": 459, "copilot_plan": "individual", "chat_enabled": true,
            "endpoints": {
                "api": "https://api.individual.githubcopilot.com",
                "telemetry": "https://telemetry.individual.githubcopilot.com",
            },
            "quota_snapshots": {"chat": {"unlimited": true}},
        }),
        "/copilot_internal/managed_settings" => json!({}),
        "/models" => json!({"data": COPILOT_MODELS.iter().map(|(name, endpoint, vendor)| json!({
            "id": name, "name": name, "vendor": vendor, "version": "1", "object": "model",
            "preview": false, "model_picker_enabled": true, "policy": {"state": "enabled"},
            "supported_endpoints": [endpoint],
            "capabilities": {
                "type": "chat", "family": name, "supports": {"streaming": true, "tool_calls": true},
                "limits": {"max_prompt_tokens": 100000, "max_output_tokens": 8192},
            },
        })).collect::<Vec<_>>()}),
        _ => return None,
    };
    Some(json_reply(200, body))
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_copilot_uses_github_and_all_three_inference_routes() {
    let probe = Probe::start("copilot", &[], copilot_answer).await;
    let (mut plan, token) = probe.turn("copilot", true);
    plan.argv
        .extend(COPILOT_MODELS.iter().map(|(model, _, _)| (*model).into()));
    let (output, _) = probe.run(plan).await;
    let seen = probe.recorder.seen();
    for path in ["/copilot_internal/user", "/models"] {
        assert_bearer(&seen, "GET", path, &token);
    }
    for (model, endpoint, _) in COPILOT_MODELS {
        assert!(
            seen.iter()
                .any(|r| r.method == "POST" && r.path == *endpoint),
            "{model} did not reach {endpoint}: {}",
            output[model]
        );
        assert_bearer(&seen, "POST", endpoint, &token);
    }
    let policy = probe.policy();
    // The CLI can ask for team customizations, which the gateway refuses.
    let customizations = "/copilot_internal/team_customizations";
    assert!(policy.target("GET", customizations).is_err());
    assert!(
        seen.iter()
            .filter(|r| !r.authorization.is_empty())
            .filter(|r| !(r.method == "GET" && r.path == customizations))
            .all(|r| policy.target(&r.method, &r.path).is_ok()),
        "{seen:?}"
    );
    assert_token_hosts(&seen, &token, &[HOST]);
}

fn antigravity_answer(request: &Seen) -> Option<hyper::Response<axum::body::Body>> {
    if request.host == "lh3.googleusercontent.com" {
        use base64::engine::general_purpose::STANDARD;
        let image = STANDARD.decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==").unwrap();
        return Some(reply(200, "image/png", image));
    }
    let body = match request.path.as_str() {
        "/v1internal:loadCodeAssist" => json!({
            "currentTier": {"id":"standard-tier"}, "cloudaicompanionProject":"synthetic-project-459",
        }),
        "/v1internal:fetchAvailableModels" => json!({
            "models": {"synthetic-model":{
                "displayName":"Synthetic model", "model":"MODEL_PLACEHOLDER_M50",
                "apiProvider":"API_PROVIDER_GOOGLE_GEMINI", "modelProvider":"MODEL_PROVIDER_GOOGLE",
                "maxTokens":1048576, "maxOutputTokens":8192,
            }},
            "defaultAgentModelId":"synthetic-model",
            "agentModelSorts":[{"groups":[{"modelIds":["synthetic-model"]}]}],
        }),
        "/oauth2/v2/userinfo" => json!({
            "id":"459", "email":"synthetic@example.com", "picture":"https://lh3.googleusercontent.com/a/synthetic",
        }),
        "/token" if request.host == "oauth2.googleapis.com" => json!({
            "access_token":"NEW-ACCESS-459", "expires_in":3599, "token_type":"Bearer",
        }),
        path if path.starts_with("/v1internal:") && !path.contains(":streamGenerateContent") => {
            json!({})
        }
        _ => return None,
    };
    Some(json_reply(200, body))
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_antigravity_reports_quota_without_waiting_for_retry() {
    let capture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/guildbotics/guest/fixtures/antigravity_quota_1_2_13.json"
    )))
    .unwrap();
    let mut upstream = capture["upstream"].clone();
    let details = upstream["error"]["details"].as_array_mut().unwrap();
    let delay: f64 = details
        .iter()
        .find(|detail| detail["@type"] == "type.googleapis.com/google.rpc.RetryInfo")
        .unwrap()["retryDelay"]
        .as_str()
        .unwrap()
        .strip_suffix('s')
        .unwrap()
        .parse()
        .unwrap();
    let reset = chrono::Utc::now() + chrono::Duration::milliseconds((delay * 1000.0) as i64);
    details
        .iter_mut()
        .find(|detail| detail["@type"] == "type.googleapis.com/google.rpc.ErrorInfo")
        .unwrap()["metadata"]["quotaResetTimeStamp"] = json!(reset.to_rfc3339());
    let probe = Probe::start("antigravity", ANTIGRAVITY_HOSTS, move |r| {
        if r.path == "/v1internal:streamGenerateContent" {
            Some(json_reply(429, upstream.clone()))
        } else {
            antigravity_answer(r)
        }
    })
    .await;
    let (mut plan, token) = probe.turn("antigravity-quota", false);
    let broker = probe.broker();
    let hosts: Vec<_> = ["relayed_hosts", "turn_domains"]
        .iter()
        .flat_map(|field| broker[*field].as_array().unwrap())
        .collect();
    plan.env.push((
        "GUILDBOTICS_GATEWAY_RELAYS".into(),
        json!([{"hosts":hosts,"port":probe.recorder.tls}]).to_string(),
    ));
    let (out, _) = probe.run(plan).await;
    assert!(out["seconds"].as_f64().unwrap() < 30.0, "{out}");
    for (key, value) in capture["result"].as_object().unwrap() {
        assert_eq!(&out["result"][key], value, "{key}: {out}");
    }
    let seen = probe.recorder.seen();
    assert_bearer(&seen, "POST", "/v1internal:streamGenerateContent", &token);
    assert_bearer(&seen, "GET", "/oauth2/v2/userinfo", &token);
    assert!(
        seen.iter()
            .filter(|r| r.path.starts_with("/v1internal:"))
            .all(|r| r.authorization == format!("Bearer {token}")),
        "{seen:?}"
    );
    assert_token_hosts(&seen, &token, &[HOST, "www.googleapis.com"]);
    let policy = probe.policy();
    for request in seen.iter().filter(|r| !r.authorization.is_empty()) {
        let target = policy.target(&request.method, &request.path).unwrap();
        let expected = if target.starts_with(&policy.origin) {
            HOST
        } else {
            "www.googleapis.com"
        };
        assert_eq!(request.host, expected, "{request:?}");
        assert_eq!(request.kind, "tls", "{request:?}");
    }
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_antigravity_refreshes_expired_login_when_reading_usage() {
    let probe = Probe::start("antigravity", ANTIGRAVITY_HOSTS, antigravity_answer).await;
    let files = expire(credentials_for(&probe.catalog), &probe.catalog).unwrap();
    assert!(expiring(&files, &probe.catalog));
    let mut plan = probe.prepare("antigravity-refresh", files, true);
    plan.argv.extend(
        probe.broker()["refresh"]
            .as_array()
            .unwrap()
            .iter()
            .map(|arg| arg.as_str().unwrap().into()),
    );
    let auth = probe.catalog["provision"]["auth"].as_str().unwrap();
    let guest_auth = format!("{}/{auth}", plan.root);
    let (_, captured) = probe.run(plan).await;
    let files = Files::from([(auth.to_owned(), captured[&guest_auth].clone())]);
    assert_eq!(access(&files, &probe.catalog).unwrap(), "NEW-ACCESS-459");
    let doc = credential_document(&files[auth]).unwrap();
    assert!(expiry(&doc, probe.broker()).unwrap() > chrono::Utc::now().timestamp() as f64 + 3000.0);
    let seen = probe.recorder.seen();
    let token = seen
        .iter()
        .find(|r| r.path == "/token")
        .expect("OAuth refresh did not reach the recorder");
    assert_eq!(
        (token.kind, token.host.as_str(), token.method.as_str()),
        ("tls", "oauth2.googleapis.com", "POST")
    );
}
