//! Observe the pinned binaries using the same stand-in formats as a turn.

#[path = "recorder.rs"]
mod recorder;

use super::*;
use gb_environment::gateway::{Policy, Provider};
use recorder::{HOST, Recorder, Seen, reply};

const STAND_IN_SUFFIX: &str = "53594e544845544943343539";

struct Probe {
    device: Device,
    catalog: Value,
    recorder: Recorder,
}

impl Probe {
    async fn start(
        tool: &str,
        answer: impl Fn(&Seen) -> Option<hyper::Response<axum::body::Body>> + Send + Sync + 'static,
    ) -> Self {
        let device = Device::new();
        let catalog = device
            .catalog()
            .into_iter()
            .find(|entry| entry["name"] == tool)
            .unwrap();
        let recorder = Recorder::start(answer).await;
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
                document["last_refresh"] = json!("2026-01-01T00:00:00Z");
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
        let env_command = broker["stand_in_command_env"].as_str().unwrap();
        let files = if env_command.is_empty() {
            Files::from([(auth.to_owned(), serde_json::to_vec(&document).unwrap())])
        } else {
            Files::new()
        };
        let mut plan = self.prepare(case, files, proxy);
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
    let probe = Probe::start("claude", |_| None).await;
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
    let probe = Probe::start("grok", |_| None).await;
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
    let probe = Probe::start("codex", |r| {
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
                || request.path.starts_with("/backend-api/wham/accounts/")
                || request.path == "/backend-api/wham/usage")
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
