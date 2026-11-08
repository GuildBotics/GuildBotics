//! A command's ephemeral host and member endpoints, separate from owner API auth.
use axum::{
    Json, Router,
    body::Body,
    extract::{DefaultBodyLimit, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
};
use gb_environment::{
    broker::{Broker, Request as MemberRequest},
    gateway::{Lease, Leases, Mask, Policy},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, future::Future, pin::Pin, sync::Arc};
use tokio::task::JoinHandle;

type Answer = Pin<Box<dyn Future<Output = Result<Value, HostError>> + Send>>;
pub type HostCalls = Arc<dyn Fn(String, Value) -> Answer + Send + Sync>;
pub type TokenSource = Arc<
    dyn Fn(Option<String>) -> Pin<Box<dyn Future<Output = Result<String, String>> + Send>>
        + Send
        + Sync,
>;

pub struct HostError {
    pub category: &'static str,
    pub message: String,
}
impl HostError {
    pub fn refused(message: impl Into<String>) -> Self {
        Self {
            category: "refused",
            message: message.into(),
        }
    }
}
impl IntoResponse for HostError {
    fn into_response(self) -> Response {
        let status = if self.category == "refused" {
            StatusCode::FORBIDDEN
        } else {
            StatusCode::UNPROCESSABLE_ENTITY
        };
        (
            status,
            Json(json!({"error":{"category":self.category,"message":self.message,"details":{}}})),
        )
            .into_response()
    }
}

pub struct HttpHost {
    pub port: u16,
    pub token: String,
    task: JoinHandle<()>,
    alive: Arc<std::sync::atomic::AtomicBool>,
    broker: Broker,
}
impl Drop for HttpHost {
    fn drop(&mut self) {
        self.alive.store(false, std::sync::atomic::Ordering::SeqCst);
        self.broker.close();
        self.task.abort();
    }
}
#[derive(Clone)]
struct HostState {
    token: String,
    alive: Arc<std::sync::atomic::AtomicBool>,
    calls: HostCalls,
    broker: Broker,
}
impl HttpHost {
    pub async fn start(calls: HostCalls, broker: Broker) -> std::io::Result<Self> {
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let port = listener.local_addr()?.port();
        let token = uuid_token();
        let alive = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let state = HostState {
            token: token.clone(),
            alive: alive.clone(),
            calls,
            broker: broker.clone(),
        };
        let router = Router::new()
            .route("/host/{call}", post(call_host))
            .route("/mcp", post(mcp))
            .layer(DefaultBodyLimit::max(8 * 1024 * 1024))
            .with_state(state);
        let task = tokio::spawn(async move {
            let _ = axum::serve(listener, router).await;
        });
        Ok(Self {
            port,
            token,
            task,
            alive,
            broker,
        })
    }
    pub fn guest_url(&self) -> String {
        format!("http://host.microsandbox.internal:{}/host", self.port)
    }
    pub fn mcp_url(&self) -> String {
        format!("http://host.microsandbox.internal:{}/mcp", self.port)
    }
}
fn uuid_token() -> String {
    gb_environment::gateway::Leases::default()
        .lend()
        .stand_in
        .clone()
}
fn admitted(headers: &HeaderMap, token: &str) -> bool {
    let authorization = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let expected = format!("Bearer {token}");
    let mut difference = authorization.len() ^ expected.len();
    for (a, b) in authorization.bytes().zip(expected.bytes()) {
        difference |= usize::from(a ^ b);
    }
    let host = headers
        .get("host")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let host = host
        .rsplit_once(':')
        .filter(|(_, port)| port.bytes().all(|b| b.is_ascii_digit()))
        .map(|(host, _)| host)
        .unwrap_or(host);
    difference == 0
        && matches!(
            host,
            "127.0.0.1" | "localhost" | "[::1]" | "host.microsandbox.internal"
        )
}
async fn call_host(
    State(state): State<HostState>,
    Path(call): Path<String>,
    headers: HeaderMap,
    Json(args): Json<Value>,
) -> Response {
    if !state.alive.load(std::sync::atomic::Ordering::SeqCst) || !admitted(&headers, &state.token) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    if !args.is_object() {
        return HostError::refused("A call takes a JSON object.").into_response();
    }
    match (state.calls)(call, args).await {
        Ok(result) => Json(json!({"result":result})).into_response(),
        Err(error) => error.into_response(),
    }
}
async fn mcp(
    State(state): State<HostState>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> Response {
    if !state.alive.load(std::sync::atomic::Ordering::SeqCst) || !admitted(&headers, &state.token) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let result = match request["method"].as_str() {
        Some("initialize") => {
            json!({"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"guildbotics-member","version":"1"}})
        }
        Some("notifications/initialized") => return StatusCode::ACCEPTED.into_response(),
        Some("tools/list") => {
            json!({"tools":[{"name":"guildbotics_member","description":"Run a trusted member capability.","inputSchema":{"type":"object","properties":{"arguments":{"type":"array","items":{"type":"string"}},"stdin":{"type":"string"},"turn_grant":{"type":"string"}},"required":["arguments","turn_grant"],"additionalProperties":false}}]})
        }
        Some("tools/call") if request["params"]["name"] == "guildbotics_member" => {
            let arguments:MemberRequest=match serde_json::from_value(request["params"]["arguments"].clone()){Ok(args)=>args,Err(_)=>return Json(json!({"jsonrpc":"2.0","id":id,"error":{"code":-32602,"message":"Invalid tool arguments."}})).into_response()};
            let result = state.broker.execute(arguments).await;
            json!({"content":[{"type":"text","text":serde_json::to_string(&result).expect("member result JSON")}],"isError":false})
        }
        _ => return Json(
            json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"Unknown method."}}),
        )
        .into_response(),
    };
    Json(json!({"jsonrpc":"2.0","id":id,"result":result})).into_response()
}

#[derive(Clone)]
struct GatewayState {
    policy: Policy,
    source: TokenSource,
    leases: Leases,
    refused: Arc<std::sync::Mutex<std::collections::BTreeSet<(u64, String, String)>>>,
    client: reqwest::Client,
}
/// Owns a command-level lease; dropping the listener revokes it. Turn-level
/// borrowers may instead create one gateway for each turn.
pub struct Gateway {
    pub port: u16,
    pub lease: Lease,
    pub ca_pem: Vec<u8>,
    tls: bool,
    leases: Leases,
    task: JoinHandle<()>,
}
impl Drop for Gateway {
    fn drop(&mut self) {
        self.leases.revoke();
        self.task.abort();
    }
}
impl Gateway {
    pub fn revoke(&self) {
        self.leases.revoke();
    }
    pub fn lend(&self, token: String) -> Lease {
        self.leases.lend_token(token)
    }
    pub async fn start(
        policy: Policy,
        source: TokenSource,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let port = listener.local_addr()?.port();
        let leases = Leases::default();
        let lease = leases.lend();
        let state = GatewayState {
            policy,
            source,
            leases: leases.clone(),
            refused: Arc::default(),
            client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(std::time::Duration::from_secs(600))
                .build()?,
        };
        let router = Router::new()
            .fallback(forward)
            .layer(DefaultBodyLimit::max(64 * 1024 * 1024))
            .with_state(state);
        let task = tokio::spawn(async move {
            let _ = axum::serve(listener, router).await;
        });
        Ok(Self {
            port,
            lease,
            ca_pem: vec![],
            tls: false,
            leases,
            task,
        })
    }
    pub fn guest_url(&self) -> String {
        format!(
            "{}://host.microsandbox.internal:{}",
            if self.tls { "https" } else { "http" },
            self.port
        )
    }

    pub async fn start_tls(
        policy: Policy,
        source: TokenSource,
        names: Vec<String>,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        // A CA of the gateway's own signs a server certificate for `names`; only
        // the CA is handed to the turn, as its trust anchor.
        let now = time::OffsetDateTime::now_utc();
        let lifetime = |parameters: &mut rcgen::CertificateParams| {
            parameters.not_before = now - time::Duration::minutes(5);
            parameters.not_after = now + time::Duration::days(2);
        };
        let ca_key = rcgen::KeyPair::generate()?;
        let mut ca = rcgen::CertificateParams::default();
        ca.distinguished_name
            .push(rcgen::DnType::CommonName, "GuildBotics turn gateway");
        ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Constrained(0));
        ca.key_usages = vec![
            rcgen::KeyUsagePurpose::KeyCertSign,
            rcgen::KeyUsagePurpose::CrlSign,
        ];
        lifetime(&mut ca);
        let ca_pem = ca.self_signed(&ca_key)?.pem().into_bytes();
        let key = rcgen::KeyPair::generate()?;
        let subject = names.first().cloned().unwrap_or_default();
        let mut parameters = rcgen::CertificateParams::new(names)?;
        parameters
            .distinguished_name
            .push(rcgen::DnType::CommonName, subject);
        parameters.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
        parameters.use_authority_key_identifier_extension = true;
        lifetime(&mut parameters);
        let certificate = parameters.signed_by(&key, &rcgen::Issuer::new(ca, ca_key))?;
        let _ = tokio_rustls::rustls::crypto::ring::default_provider().install_default();
        let config = tokio_rustls::rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(
                vec![certificate.der().clone()],
                tokio_rustls::rustls::pki_types::PrivatePkcs8KeyDer::from(key.serialize_der())
                    .into(),
            )?;
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(config));
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let port = listener.local_addr()?.port();
        let leases = Leases::default();
        let lease = leases.lend();
        let state = GatewayState {
            policy,
            source,
            leases: leases.clone(),
            refused: Arc::default(),
            client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(std::time::Duration::from_secs(600))
                .build()?,
        };
        let router = Router::new()
            .fallback(forward)
            .layer(DefaultBodyLimit::max(64 * 1024 * 1024))
            .with_state(state);
        let task = tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    break;
                };
                let acceptor = acceptor.clone();
                let router = router.clone();
                tokio::spawn(async move {
                    let Ok(stream) = acceptor.accept(stream).await else {
                        return;
                    };
                    let io = hyper_util::rt::TokioIo::new(stream);
                    let service = hyper_util::service::TowerToHyperService::new(router);
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(io, service)
                        .await;
                });
            }
        });
        Ok(Self {
            port,
            lease,
            ca_pem,
            tls: true,
            leases,
            task,
        })
    }
}
fn authentication_refusal(reason: String) -> Response {
    (
        StatusCode::UNAUTHORIZED,
        Json(json!({"type":"error","error":{"type":"authentication_error","message":reason}})),
    )
        .into_response()
}
async fn forward(State(state): State<GatewayState>, request: axum::extract::Request) -> Response {
    let incoming = state.policy.provider.credential_header();
    let presented = request
        .headers()
        .get(incoming)
        .and_then(|v| v.to_str().ok())
        .and_then(|value| {
            if incoming == "authorization" {
                value.strip_prefix("Bearer ")
            } else {
                Some(value)
            }
        })
        .unwrap_or("");
    let Some(generation) = state.leases.authorize(presented) else {
        return authentication_refusal("The login is invalid or expired.".into());
    };
    let stand_in = presented.to_owned();
    let target = match state.policy.target(
        request.method().as_str(),
        request
            .uri()
            .path_and_query()
            .map(|v| v.as_str())
            .unwrap_or("/"),
    ) {
        Ok(target) => target,
        Err(_) => {
            let method = request.method().as_str();
            let path = request.uri().path();
            if state.refused.lock().expect("gateway refusal log").insert((
                generation,
                method.into(),
                path.into(),
            )) {
                tracing::warn!("Gateway refused {method} {path}");
            }
            return StatusCode::FORBIDDEN.into_response();
        }
    };
    let supplied: BTreeMap<String, String> = request
        .headers()
        .iter()
        .filter_map(|(k, v)| v.to_str().ok().map(|v| (k.to_string(), v.to_owned())))
        .collect();
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    let body = match axum::body::to_bytes(request.into_body(), 64 * 1024 * 1024).await {
        Ok(body) => body,
        Err(_) => return StatusCode::PAYLOAD_TOO_LARGE.into_response(),
    };
    let mut refused = None;
    let (response, token) = loop {
        let token = match (state.source)(refused.clone()).await {
            Ok(token) if !token.is_empty() => token,
            Ok(_) => return authentication_refusal("The login is invalid or expired.".into()),
            Err(reason) => return authentication_refusal(reason),
        };
        if !state.leases.accepts(generation, &stand_in) {
            return authentication_refusal("The login is invalid or expired.".into());
        }
        let mut upstream = state
            .client
            .request(method.clone(), &target)
            .body(body.clone());
        for (k, v) in state.policy.headers(&supplied, &token) {
            upstream = upstream.header(k, v);
        }
        let answer = match upstream.send().await {
            Ok(answer) => answer,
            Err(_) => return StatusCode::BAD_GATEWAY.into_response(),
        };
        if answer.status() == StatusCode::UNAUTHORIZED && refused.is_none() {
            refused = Some(token);
            continue;
        }
        if answer.status() == StatusCode::UNAUTHORIZED
            && let Err(reason) = (state.source)(Some(token.clone())).await
        {
            return authentication_refusal(reason);
        }
        break (answer, token);
    };
    if response
        .headers()
        .get("content-encoding")
        .is_some_and(|v| v != "identity")
    {
        tracing::warn!("Gateway refused an encoded answer to {} {}", method, path);
        return StatusCode::BAD_GATEWAY.into_response();
    }
    let status = response.status();
    let mut headers = HeaderMap::new();
    let connection_names = response
        .headers()
        .get_all("connection")
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .map(|v| v.trim().to_ascii_lowercase())
        .collect::<Vec<_>>();
    for (name, value) in response.headers() {
        if !matches!(
            name.as_str(),
            "content-length"
                | "connection"
                | "transfer-encoding"
                | "keep-alive"
                | "upgrade"
                | "trailer"
                | "te"
                | "proxy-authenticate"
                | "proxy-authorization"
        ) && !connection_names.iter().any(|n| n == name.as_str())
        {
            let mut mask = Mask::new(&token);
            let safe = mask.feed(value.as_bytes(), true);
            if let Ok(value) = axum::http::HeaderValue::from_bytes(&safe) {
                headers.append(name, value);
            }
        }
    }
    let mask = Mask::new(&token);
    let stream = futures_util::stream::unfold(
        (response, mask, state.leases, generation, stand_in, false),
        |(mut response, mut mask, leases, generation, stand_in, finished)| async move {
            if finished || !leases.accepts(generation, &stand_in) {
                return None;
            }
            match response.chunk().await {
                Ok(Some(chunk)) => {
                    if !leases.accepts(generation, &stand_in) {
                        return None;
                    }
                    let bytes = mask.feed(&chunk, false);
                    Some((
                        Ok::<_, std::io::Error>(bytes),
                        (response, mask, leases, generation, stand_in, false),
                    ))
                }
                Ok(None) => {
                    let bytes = mask.feed(&[], true);
                    if bytes.is_empty() {
                        None
                    } else {
                        Some((
                            Ok(bytes),
                            (response, mask, leases, generation, stand_in, true),
                        ))
                    }
                }
                Err(_) => Some((
                    Err(std::io::Error::other("Provider response interrupted.")),
                    (response, mask, leases, generation, stand_in, true),
                )),
            }
        },
    );
    let mut answer = Response::new(Body::from_stream(stream));
    *answer.status_mut() = status;
    *answer.headers_mut() = headers;
    answer
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn the_guest_is_told_of_the_configuration_and_the_workspace_not_of_the_copy() {
        struct Keys;
        impl gb_config::secrets::Keychain for Keys {
            fn read(&self) -> anyhow::Result<Option<String>> {
                Ok(Some("ExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExM=".into()))
            }
            fn create(&self, _: &str) -> anyhow::Result<()> {
                Ok(())
            }
        }
        let root = tempfile::tempdir().unwrap();
        let root = gb_base::host_path::normalize(root.path()).unwrap();
        let workspace = root.join("workspace");
        let host = gb_environment::Host {
            data: root.join("data"),
            home: root.join("home"),
            package: gb_base::home::python_package(),
            identity_root: root.join("identity"),
        };
        let config = workspace.join(".guildbotics/config");
        for (path, text) in [
            ("team/project.yml", "name: test\nlanguage: en\n"),
            ("team/members/aiko/member.yml", "is_active: true\n"),
            (
                "team/members/aiko/commands/probe.md",
                "---\nbrain: none\n---\n",
            ),
            (
                "intelligences/brain_mapping.yml",
                "default:\n  kind: model\n  model: broken\n",
            ),
            (
                "intelligences/model_mapping.yml",
                "broken: models/broken/model.yml\n",
            ),
            ("intelligences/models/broken/model.yml", "[\n"),
        ] {
            std::fs::create_dir_all(config.join(path).parent().unwrap()).unwrap();
            std::fs::write(config.join(path), text).unwrap();
        }
        let identity = host
            .identity_root
            .join(".guildbotics/config/team/members/aiko/identity.yml");
        std::fs::create_dir_all(identity.parent().unwrap()).unwrap();
        std::fs::write(
            &identity,
            "person_id: aiko\nname: Aiko\nperson_type: agent\n",
        )
        .unwrap();
        let clones = workspace.join(".guildbotics/local/clones/aiko");
        std::fs::create_dir_all(clones.join("repo")).unwrap();
        std::fs::create_dir_all(&host.home).unwrap();
        let copy = Arc::new(gb_environment::Configuration::copy(&host, &workspace).unwrap());
        let command = gb_environment::Command {
            path: copy.path().join("team/members/aiko/commands/probe.md"),
            config: copy.clone(),
            wants_result: false,
            invocation: None,
            person_id: "aiko".into(),
            run_id: "run".into(),
            trace_id: "trace".into(),
            work_kind: "manual".into(),
            work_identity: "work".into(),
            name: "probe".into(),
            access: gb_wire::CommandAccess {
                read_only: true,
                inspects: vec![],
            },
            args: vec![],
            cwd: workspace.clone(),
            pipe: String::new(),
        };
        let preflight = gb_environment::device::read(&host, &workspace).await;
        let test = CommandTestServices {
            catalog: json!([]),
            recorder_port: None,
        };
        let services = prepare_command(
            &host,
            &workspace,
            &command,
            &preflight,
            Arc::new(Keys),
            None,
            Some(&test),
        )
        .await
        .unwrap();
        let resolved = &services.session.resolved;
        let spelled = |path: &std::path::Path| gb_environment::guest_path(path).unwrap();
        assert_eq!(
            resolved["commands"]["probe"],
            spelled(&config.join("team/members/aiko/commands/probe.md"))
        );
        assert_eq!(
            resolved["clone_paths"],
            json!({"aiko": clones.display().to_string(),
                "aiko/repo": clones.join("repo").display().to_string()})
        );
        // Even what the guest is told of a broken file names the file.
        let copied = copy.path().display().to_string();
        assert!(!resolved.to_string().contains(&copied), "{resolved}");
        assert!(
            resolved.to_string().contains(&config.display().to_string()),
            "{resolved}"
        );
        // Exercise the production preparation path with saved credentials;
        // an empty store would not consult the vault at all.
        let writes = gb_base::shared_writes::SharedWrites::new(
            host.identity_root.clone(),
            Some(workspace.clone()),
        );
        let store = gb_config::secrets::SecretStore::new(
            &writes,
            gb_config::secrets::Scope::Workspace,
            gb_config::secrets::DeviceKey {
                machine_root: root.clone(),
                keychain: &Keys,
            },
        )
        .unwrap();
        store
            .set("TYPESAFE_API_KEY", "synthetic-jev-value")
            .unwrap();
        std::fs::write(
            config.join("intelligences/brain_mapping.yml"),
            "default: {kind: jev, model: fixture}\n",
        )
        .unwrap();
        std::fs::write(
            config.join("intelligences/jev.yml"),
            "base_url: http://127.0.0.1:1\n",
        )
        .unwrap();
        let mut command = command;
        command.config = Arc::new(gb_environment::Configuration::copy(&host, &workspace).unwrap());
        let services = prepare_command(
            &host,
            &workspace,
            &command,
            &preflight,
            Arc::new(Keys),
            None,
            Some(&test),
        )
        .await
        .unwrap();
        assert!(
            services.session.resolved["inference_keys"]["jev"]
                .as_str()
                .is_some()
        );
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let response: Value = client
            .post(format!(
                "http://127.0.0.1:{}/host/required_io",
                services._host.port
            ))
            .bearer_auth(&services._host.token)
            .json(&json!({"record_id":"abc1","payload":{"prompt":"synthetic-jev-value"}}))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert!(response.get("error").is_none(), "{response}");
        let recorded: Value = serde_json::from_slice(
            &std::fs::read(workspace.join(".guildbotics/local/run/required-io/abc1.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(recorded["payload"]["prompt"], "***");
    }

    #[tokio::test]
    async fn command_preparation_preserves_config_file_and_parse_reason() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("workspace");
        let host = gb_environment::Host {
            data: root.path().join("data"),
            home: root.path().join("home"),
            package: root.path().join("package"),
            identity_root: root.path().join("identity"),
        };
        let project = workspace.join(".guildbotics/config/team/project.yml");
        std::fs::create_dir_all(project.parent().unwrap()).unwrap();
        std::fs::write(&project, "[").unwrap();
        let command = gb_environment::Command {
            config: Arc::new(gb_environment::Configuration::copy(&host, &workspace).unwrap()),
            wants_result: false,
            invocation: None,
            person_id: "aiko".into(),
            run_id: "run".into(),
            trace_id: "trace".into(),
            work_kind: "manual".into(),
            work_identity: "work".into(),
            path: root.path().join("probe.py"),
            name: "probe".into(),
            access: gb_wire::CommandAccess {
                read_only: true,
                inspects: vec![],
            },
            args: vec![],
            cwd: workspace.clone(),
            pipe: String::new(),
        };
        let preflight = gb_environment::device::read(&host, &workspace).await;
        let error = match prepare_command(
            &host,
            &workspace,
            &command,
            &preflight,
            Arc::new(RecordingKeychain),
            None,
            None,
        )
        .await
        {
            Ok(_) => panic!("malformed configuration must refuse the command"),
            Err(error) => error,
        };
        // Named as the file a person edits, not as the run's copy of it.
        let project = gb_base::host_path::normalize(&project).unwrap();
        assert_eq!(
            error,
            format!("{}: configuration is not a YAML mapping", project.display())
        );
    }

    #[test]
    fn every_configured_brain_adapter_is_available_once_to_the_command() {
        let resolved = json!({"brains":{
            "default":{"kind":"ai_cli","definition":{"adapter":"claude"}},
            "review":{"kind":"ai_cli","definition":{"adapter":"codex"}},
            "again":{"kind":"ai_cli","definition":{"adapter":"claude"}},
            "future":{"kind":"ai_cli","definition":{"adapter":"future-provider"}},
            "model":{"kind":"model","definition":{"parameters":{"id":"fixture"}}},
            "invalid":{"kind":"invalid","definition":{"error":"unsupported"}},
            "malformed":{"definition":{"adapter":5}}
        }});
        assert_eq!(
            configured_tools(&resolved),
            ["claude", "codex", "future-provider"]
                .into_iter()
                .map(str::to_owned)
                .collect()
        );
        for absent in [Value::Null, json!({}), json!({"brains":[]})] {
            assert!(configured_tools(&absent).is_empty());
        }
    }

    #[test]
    fn guest_work_admission_uses_posix_boundaries_on_every_host() {
        let mounts = BTreeMap::from([("/c/work".into(), true), ("/c/work/config".into(), false)]);
        for cwd in ["/c/work", "/c/work/src", "/c/work/with space"] {
            assert!(admits(&mounts, cwd), "{cwd}");
        }
        for cwd in [
            "C:\\work",
            "c/work",
            "/c/work/../secret",
            "/c/work/config",
            "/c/work/config/token",
            "/c/work/./config/token",
            "/c/work//config/token",
            "/c/work-other",
            "//c/work",
            "/c/work\0",
        ] {
            assert!(!admits(&mounts, cwd), "{cwd}");
        }
    }
    #[test]
    fn work_admission_and_turn_readonly_access_are_distinct_contracts() {
        let mounts = vec![
            gb_environment::Mount {
                guest: "/code".into(),
                host: Some("/host/code".into()),
                readonly: true,
                quota_mib: None,
            },
            gb_environment::Mount {
                guest: "/work/config".into(),
                host: Some("/host/config".into()),
                readonly: true,
                quota_mib: None,
            },
            gb_environment::Mount {
                guest: "/exchange".into(),
                host: Some("/host/exchange".into()),
                readonly: false,
                quota_mib: None,
            },
            gb_environment::Mount {
                guest: "/member/clone".into(),
                host: Some("/host/clone".into()),
                readonly: false,
                quota_mib: None,
            },
            gb_environment::Mount {
                guest: "/readonly-grant".into(),
                host: Some("/host/grant".into()),
                readonly: true,
                quota_mib: None,
            },
        ];
        let extra = vec![gb_environment::Mount {
            guest: "/home/stand-in".into(),
            host: Some("/host/stub".into()),
            readonly: false,
            quota_mib: None,
        }];
        let directories = turn_directories(&mounts, &extra, Some("/copied-cwd"));
        assert!(directories["/code"]);
        assert!(directories["/work/config"]);
        assert!(!directories["/exchange"]);
        assert!(!directories["/member/clone"]);
        assert!(directories["/readonly-grant"]);
        assert!(!directories["/copied-cwd"]);
        assert!(!directories["/home/stand-in"]);
        let facts = BTreeMap::from([
            ("/code".into(), false),
            ("/work/config".into(), false),
            ("/exchange".into(), true),
            ("/member/clone".into(), true),
            ("/readonly-grant".into(), true),
            ("/copied-cwd".into(), true),
            ("/exchange/closed".into(), false),
        ]);
        assert!(admits(&facts, "/exchange/file"));
        assert!(admits(&facts, "/readonly-grant"));
        assert!(admits(&facts, "/copied-cwd"));
        assert!(!admits(&facts, "/code"));
        assert!(!admits(&facts, "/exchange/closed/file"));
        assert!(!admits(&facts, "/unknown"));
    }
    #[test]
    fn command_preparation_never_constructs_an_os_keychain() {
        // Cover the whole command boundary, including required IO and every
        // inference provider, rather than only today's reported call sites.
        let source = include_str!("host_window.rs");
        for forbidden in [
            concat!("System", "Keychain"),
            concat!("get_os_", "credential"),
            concat!("keep_os_", "credential"),
        ] {
            assert!(
                !source.contains(forbidden),
                "command boundary bypasses its caller-owned vault"
            );
        }
    }
    struct RecordingKeychain;
    impl gb_config::secrets::Keychain for RecordingKeychain {
        fn read(&self) -> anyhow::Result<Option<String>> {
            use base64::Engine;
            Ok(Some(
                base64::engine::general_purpose::STANDARD.encode([19; 32]),
            ))
        }
        fn create(&self, _: &str) -> anyhow::Result<()> {
            panic!("key exists")
        }
    }
    #[test]
    fn stored_secrets_are_masked_in_activity_even_when_names_overlap() {
        use gb_config::secrets::{DeviceKey, Scope, SecretStore};
        let dir = tempfile::tempdir().unwrap();
        let workspace = dir.path().join("workspace");
        let host = gb_environment::Host {
            data: dir.path().join("data"),
            home: dir.path().join("home"),
            package: dir.path().join("package"),
            identity_root: dir.path().join("identity"),
        };
        let writes = gb_base::shared_writes::SharedWrites::new(
            host.identity_root.clone(),
            Some(workspace.clone()),
        );
        for (scope, value) in [
            (Scope::Identity, "synthetic-identity-value"),
            (Scope::Workspace, "synthetic-workspace-value"),
        ] {
            SecretStore::new(
                &writes,
                scope,
                DeviceKey {
                    machine_root: dir.path().into(),
                    keychain: &RecordingKeychain,
                },
            )
            .unwrap()
            .set("STORE_ONLY_TEST_KEY", value)
            .unwrap();
        }
        let redactor = command_redactor(&host, &workspace, &RecordingKeychain).unwrap();
        let value = redactor.local(record_entry(json!({"type":"event", "span":null, "event":{"kind":"failed", "name":"failed", "message":"synthetic-identity-value / synthetic-workspace-value"}}), "trace", "run", "aiko", "test").unwrap());
        let encoded = serde_json::to_string(&value).unwrap();
        assert!(!encoded.contains("synthetic-identity-value"));
        assert!(!encoded.contains("synthetic-workspace-value"));
        assert!(encoded.contains("*** / ***"));
        gb_observability::Diagnostics::open(&workspace).record(value.as_object().unwrap().clone());
        let saved =
            std::fs::read_to_string(workspace.join(".guildbotics/local/run/sessions/trace.jsonl"))
                .unwrap();
        assert!(saved.contains("*** / ***"));
        assert!(!saved.contains("synthetic-identity-value"));
        assert!(!saved.contains("synthetic-workspace-value"));
    }
    #[test]
    fn locked_recording_store_keeps_environment_masks_without_blocking_commands() {
        const CHILD: &str = "GUILDBOTICS_REDACTOR_TEST_CHILD";
        if std::env::var_os(CHILD).is_none() {
            let result = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "host_window::tests::locked_recording_store_keeps_environment_masks_without_blocking_commands", "--nocapture"])
                .env(CHILD,"1").env("STORE_ONLY_TEST_KEY","synthetic-env-value").status().unwrap();
            assert!(result.success());
            return;
        }
        struct Locked;
        impl gb_config::secrets::Keychain for Locked {
            fn read(&self) -> anyhow::Result<Option<String>> {
                Err(gb_config::secrets::KeychainFailure::Locked.into())
            }
            fn create(&self, _: &str) -> anyhow::Result<()> {
                panic!("recording must not create keys")
            }
        }
        let dir = tempfile::tempdir().unwrap();
        let workspace = dir.path().join("workspace");
        let host = gb_environment::Host {
            data: dir.path().join("data"),
            home: dir.path().join("home"),
            package: dir.path().join("package"),
            identity_root: dir.path().join("identity"),
        };
        let writes = gb_base::shared_writes::SharedWrites::new(
            host.identity_root.clone(),
            Some(workspace.clone()),
        );
        for scope in [
            gb_config::secrets::Scope::Identity,
            gb_config::secrets::Scope::Workspace,
        ] {
            gb_config::secrets::SecretStore::new(
                &writes,
                scope,
                gb_config::secrets::DeviceKey {
                    machine_root: dir.path().into(),
                    keychain: &RecordingKeychain,
                },
            )
            .unwrap()
            .set("STORE_ONLY_TEST_KEY", "synthetic-store-value")
            .unwrap();
        }
        let redactor = command_redactor(&host, &workspace, &Locked).unwrap();
        assert_eq!(redactor.text("output synthetic-env-value"), "output ***");
    }
    #[test]
    fn recorded_io_retains_local_detail_and_span_summary_keeps_its_schema() {
        let text = "synthetic-private-key".repeat(60);
        let io=record_entry(json!({"type":"io","io_type":"llm.request","span":{"span_id":"span","call_id":"call","parent_id":null,"name":"writer"},"payload":{"prompt":text,"messages":["hello"]}}),"trace","run","aiko","write").unwrap();
        assert_eq!(io["kind"], "io");
        assert_eq!(io["type"], "llm.request");
        assert_eq!(io["span_id"], "span");
        let redactor = gb_observability::redaction::Redactor::new(["synthetic-private-key".into()]);
        let safe = redactor.local(io);
        assert_eq!(safe["payload"]["prompt"], "***".repeat(60));
        assert_eq!(safe["payload"]["messages"][0], "hello");
        let summary=record_entry(json!({"type":"summary","span":null,"slot":"writer","tool":"openai","model_specified":true,"status":"finished","model":"model","effort":"low","duration_ms":20,"usage":{"input_tokens":4}}),"trace","run","aiko","write").unwrap();
        assert_eq!(summary["type"], "span.finished");
        assert_eq!(summary["attributes"]["agent.kind"], "llm");
        assert_eq!(summary["payload"]["usage"]["input_tokens"], 4);
    }
    #[test]
    fn recorded_provider_event_uses_kind_and_never_impersonates_a_command_boundary() {
        let entry = json!({"type":"event","span":null,"conversation":{"person_id":"aiko","adapter":"codex","work_kind":"ticket","work_identity":"87"},"generation":2,"context_cursor":"cursor","event":{"kind":"assistant","name":"completed","message":"reply","command":"","path":"","approval":"","usage":{},"details":{},"provider_session_id":"session","provider_turn_id":"turn"}});
        let record = record_entry(entry.clone(), "trace", "run", "aiko", "write").unwrap();
        assert_eq!(record["type"], "agent_runtime.assistant");
        assert_eq!(record["payload"]["name"], "completed");
        assert_eq!(record["attributes"]["agent.conversation_generation"], 2);
        let mut delta = entry;
        delta["event"]["name"] = "delta".into();
        assert!(record_entry(delta, "trace", "run", "aiko", "write").is_none());
    }
    #[test]
    fn failed_agent_events_mask_credentials_and_preserve_command_correlation() {
        let entry = json!({"type":"event","span":null,"conversation":{"person_id":"aiko","adapter":"codex","work_kind":"ticket","work_identity":"issue-300"},"generation":2,"context_cursor":"cursor-1","event":{"kind":"failed","name":"failed","message":"Authorization: Bearer top-secret","command":"tool --token command-secret","details":{"access_token":"secret","nested":{"password":"secret"},"output":"API_KEY=output-secret"}}});
        let record = record_entry(entry, "trace-1", "run-1", "aiko", "write").unwrap();
        assert_eq!(record["type"], "agent_runtime.failed");
        assert_eq!(record["trace_id"], "trace-1");
        assert_eq!(record["person_id"], "aiko");
        assert_eq!(record["attributes"]["agent.run_id"], "run-1");
        assert_eq!(record["attributes"]["agent.conversation_generation"], 2);
        assert_eq!(record["attributes"]["agent.context_cursor"], "cursor-1");
        assert_eq!(record["payload"]["message"], "Authorization: ***");
        assert_eq!(record["payload"]["command"], "tool --token ***");
        assert_eq!(
            record["payload"]["details"],
            json!({"access_token":"***","nested":{"password":"***"},"output":"API_KEY=***"})
        );
    }

    #[tokio::test]
    async fn required_judgment_io_is_acknowledged_only_after_its_own_scoped_file_is_written() {
        let root = tempfile::tempdir().unwrap();
        let mut context = command_context(root.path());
        context.required_io = Some(Arc::new(crate::required_io::RequiredIo::new(
            root.path().into(),
            "trace-1".into(),
            "aiko".into(),
            || {
                Ok(gb_observability::redaction::Redactor::with_min_length(
                    ["private-test".into()],
                    1,
                ))
            },
        )));
        let host = HttpHost::start_context(context).await.unwrap();
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let url = format!("http://127.0.0.1:{}/host/required_io", host.port);
        let body = json!({"record_id":"abc0","payload":{"prompt":"private-test"}});
        let denied = client.post(&url).json(&body).send().await.unwrap();
        assert_eq!(denied.status(), reqwest::StatusCode::UNAUTHORIZED);
        let accepted = client
            .post(&url)
            .bearer_auth(&host.token)
            .json(&body)
            .send()
            .await
            .unwrap()
            .json::<Value>()
            .await
            .unwrap();
        assert!(accepted.get("result").is_some());
        assert!(accepted.get("error").is_none());
        let path = root
            .path()
            .join(".guildbotics/local/run/required-io/abc0.json");
        let saved: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["trace_id"], "trace-1");
        assert_eq!(saved["payload"]["prompt"], "***");
        for id in ["abc0", "../escape"] {
            let rejected = client
                .post(&url)
                .bearer_auth(&host.token)
                .json(&json!({"record_id":id,"payload":{}}))
                .send()
                .await
                .unwrap()
                .json::<Value>()
                .await
                .unwrap();
            assert_eq!(rejected["error"]["category"], "refused");
        }
        assert_eq!(
            serde_json::from_slice::<Value>(&std::fs::read(path).unwrap()).unwrap(),
            saved
        );
    }

    #[tokio::test]
    async fn evaluated_decisions_keep_host_correlation_even_without_required_io() {
        let root = tempfile::tempdir().unwrap();
        let mut context = command_context(root.path());
        let records = Arc::new(std::sync::Mutex::new(Vec::new()));
        let captured = records.clone();
        context.record = Arc::new(move |entry| {
            captured
                .lock()
                .unwrap()
                .push(record_entry(entry, "trace-1", "run-1", "aiko", "chat").unwrap());
        });
        let host = HttpHost::start_context(context).await.unwrap();
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let url = format!("http://127.0.0.1:{}/host/decision_evaluated", host.port);
        let metadata = json!({"evaluation_id":"evaluation", "brain":"chat_decision", "model":"configured-model", "route":"agent", "reason":"invalid", "response_effort":null, "error":"recording_failed", "duration_ms":12.5});
        let mut payload = metadata.clone();
        payload["person_id"] = "foreign".into();
        payload["trace_id"] = "foreign".into();
        payload["input"] = "raw source".into();
        let body = json!({"payload":payload});
        let denied = client.post(&url).json(&body).send().await.unwrap();
        assert_eq!(denied.status(), reqwest::StatusCode::UNAUTHORIZED);
        assert!(records.lock().unwrap().is_empty());
        let accepted = client
            .post(&url)
            .bearer_auth(&host.token)
            .json(&body)
            .send()
            .await
            .unwrap()
            .json::<Value>()
            .await
            .unwrap();
        assert!(accepted.get("result").is_some());
        let records = records.lock().unwrap();
        assert_eq!(records.len(), 1);
        let event = &records[0];
        assert_eq!(event["type"], "decision.evaluated");
        assert_eq!(event["trace_id"], "trace-1");
        assert_eq!(event["person_id"], "aiko");
        assert_eq!(event["attributes"]["agent.run_id"], "run-1");
        assert_eq!(event["payload"], metadata);
    }

    #[test]
    fn evaluated_decision_metadata_is_bounded_and_redacted() {
        let event = record_entry(
            json!({"type":"decision_evaluated","payload":{
                "evaluation_id":"evaluation", "brain":"chat_decision",
                "model":"api_key=synthetic-key", "route":"agent",
                "reason":"token=synthetic-token", "response_effort":null,
                "error": "x".repeat(9000), "duration_ms":1,
                "input":"raw source", "person_id":"foreign"
            }}),
            "trace",
            "run",
            "aiko",
            "chat",
        )
        .unwrap();
        assert_eq!(event["payload"].as_object().unwrap().len(), 8);
        assert_eq!(event["payload"]["model"], "api_key=***");
        assert_eq!(event["payload"]["reason"], "token=***");
        assert_eq!(
            event["payload"]["error"].as_str().unwrap().chars().count(),
            8192
        );
    }

    fn command_context(root: &std::path::Path) -> Context {
        let invocation = gb_environment::broker::Invocation {
            chat_subject: None,
            person_id: "aiko".into(),
            run_id: "run-1".into(),
            trace_id: "trace-1".into(),
            work_kind: "ticket".into(),
            work_identity: "issue-300".into(),
            cwd: root.into(),
            access: gb_wire::CommandAccess {
                read_only: false,
                inspects: vec![],
            },
        };
        Context {
            broker: Broker::new(root.join("unused-cli"), invocation.clone()),
            invocation,
            workspace: root.into(),
            home: root.to_string_lossy().into(),
            mounts: BTreeMap::new(),
            turn_mounts: BTreeMap::new(),
            tools: ["codex".into()].into(),
            summary_tools: ["codex".into()].into(),
            loans: BTreeMap::new(),
            rate_limits: BTreeMap::new(),
            record: Arc::new(|_| {}),
            required_io: None,
        }
    }

    #[test]
    fn conversation_snapshots_persist_and_rotate_only_within_the_commands_authority() {
        let root = tempfile::tempdir().unwrap();
        let context = command_context(root.path());
        let key = json!({"person_id":"aiko","adapter":"codex","work_kind":"ticket","work_identity":"issue-300"});
        assert!(conversation(&context, "resolve", &json!({"key":key,"policy":"resume"})).is_err());
        let mut record = conversation(
            &context,
            "resolve",
            &json!({"key":key,"policy":"auto","model":"model-a"}),
        )
        .map_err(|error| error.message)
        .unwrap();
        assert_eq!(record["generation"], 0);
        assert_eq!(record["provider_session_id"], "");
        record["provider_session_id"] = "provider-session".into();
        record["provider_turn_id"] = "provider-turn".into();
        record["context_cursor"] = "cursor-1".into();
        record["last_event_id"] = "event-1".into();
        record["last_run_id"] = "run-1".into();
        record["effective_model"] = "model-a".into();
        record["effective_effort"] = "high".into();
        record["turn_count"] = 1.into();
        record["input_tokens"] = 20.into();
        let saved = conversation(&context, "save", &json!({"record":record}))
            .map_err(|error| error.message)
            .unwrap();
        record["context_cursor"] = "uncommitted".into();
        let resumed = conversation(&context, "resolve", &json!({"key":key,"policy":"resume"}))
            .map_err(|error| error.message)
            .unwrap();
        for field in [
            "provider_session_id",
            "provider_turn_id",
            "context_cursor",
            "last_event_id",
            "last_run_id",
            "effective_model",
            "effective_effort",
            "turn_count",
            "input_tokens",
            "generation",
            "created_at",
        ] {
            assert_eq!(resumed[field], saved[field], "{field}");
        }
        assert_eq!(resumed["context_cursor"], "cursor-1");
        assert!(resumed.get("version").is_none());
        let unhealthy = conversation(
            &context,
            "mark_unhealthy",
            &json!({"record":resumed,"reason":"context_compaction"}),
        )
        .map_err(|error| error.message)
        .unwrap();
        assert_eq!(unhealthy["healthy"], false);
        let rotated = conversation(&context, "resolve", &json!({"key":key,"policy":"auto"}))
            .map_err(|error| error.message)
            .unwrap();
        assert_eq!(rotated["generation"], 1);
        assert_eq!(rotated["provider_session_id"], "");
        assert_eq!(rotated["context_cursor"], "");
        assert_eq!(rotated["rotation_reason"], "context_compaction");
        let fresh = conversation(&context, "resolve", &json!({"key":key,"policy":"fresh"}))
            .map_err(|error| error.message)
            .unwrap();
        assert_eq!(fresh["generation"], 2);
        assert_eq!(fresh["rotation_reason"], "fresh");
        for (field, value) in [
            ("person_id", "kenji"),
            ("adapter", "claude"),
            ("work_kind", "chat"),
            ("work_identity", "../other-work"),
        ] {
            let mut foreign = key.clone();
            foreign[field] = value.into();
            assert!(
                conversation(&context, "resolve", &json!({"key":foreign,"policy":"auto"})).is_err()
            );
            let mut foreign_record = fresh.clone();
            foreign_record["key"] = foreign;
            assert!(conversation(&context, "save", &json!({"record":foreign_record})).is_err());
        }
        let parent = root
            .path()
            .join(".guildbotics/local/agent-runtime/conversations/aiko/codex");
        let files = std::fs::read_dir(parent)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|ext| ext == "json"))
            .collect::<Vec<_>>();
        assert_eq!(files.len(), 1);
        let stored: Value = serde_json::from_slice(&std::fs::read(&files[0]).unwrap()).unwrap();
        assert_eq!(stored["version"], 1);
        assert_eq!(stored["generation"], 2);
        std::fs::write(&files[0], "not JSON").unwrap();
        assert!(conversation(&context, "resolve", &json!({"key":key,"policy":"auto"})).is_err());
    }

    #[tokio::test]
    async fn next_turn_waits_for_the_active_turn_and_cancelled_waiters_keep_its_grant() {
        let serial = Arc::new(tokio::sync::Mutex::new(()));
        let current = Arc::new(tokio::sync::Mutex::new(Some("first".into())));
        let released = Arc::new(tokio::sync::Notify::new());
        let second = wait_for_turn(&serial, &current, &released);
        tokio::pin!(second);
        assert!(futures_util::poll!(second.as_mut()).is_pending());
        let cancelled = {
            let serial = serial.clone();
            let current = current.clone();
            let released = released.clone();
            tokio::spawn(async move { wait_for_turn(&serial, &current, &released).await })
        };
        tokio::task::yield_now().await;
        cancelled.abort();
        assert!(cancelled.await.unwrap_err().is_cancelled());
        assert_eq!(current.lock().await.as_deref(), Some("first"));
        let ending = serial.lock().await;
        *current.lock().await = None;
        released.notify_waiters();
        drop(ending);
        let second_guard = second.await;
        *current.lock().await = Some("second".into());
        assert_eq!(current.lock().await.as_deref(), Some("second"));
        drop(second_guard);
    }

    #[tokio::test]
    async fn host_calls_require_the_command_token_and_preserve_the_wire_envelope() {
        let dir = tempfile::tempdir().unwrap();
        let broker = Broker::new(
            dir.path().join("unused-cli"),
            gb_environment::broker::Invocation {
                chat_subject: None,
                person_id: "aiko".into(),
                run_id: "run".into(),
                trace_id: "trace".into(),
                work_kind: "ticket".into(),
                work_identity: "87".into(),
                cwd: dir.path().into(),
                access: gb_wire::CommandAccess {
                    read_only: true,
                    inspects: vec![],
                },
            },
        );
        let calls: HostCalls = Arc::new(|name, args| {
            Box::pin(async move {
                if name == "record" {
                    Ok(args)
                } else {
                    Err(HostError::refused("unknown call"))
                }
            })
        });
        let host = HttpHost::start(calls, broker).await.unwrap();
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let url = format!("http://127.0.0.1:{}/host/record", host.port);
        assert_eq!(
            client
                .post(&url)
                .json(&json!({}))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let response = client
            .post(&url)
            .bearer_auth(&host.token)
            .json(&json!({"entries":[]}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.json::<Value>().await.unwrap(),
            json!({"result":{"entries":[]}})
        );
        assert_eq!(
            client
                .post(&url)
                .bearer_auth(&host.token)
                .header("Host", "evil.test")
                .json(&json!({}))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let denied = client
            .post(format!("http://127.0.0.1:{}/host/unknown", host.port))
            .bearer_auth(&host.token)
            .json(&json!({}))
            .send()
            .await
            .unwrap();
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            denied.json::<Value>().await.unwrap()["error"]["category"],
            "refused"
        );
    }
}

/// Authority resolved by the host once for the entire command. Guest calls
/// may supply data, but cannot change this member, work, or mounted paths.
#[derive(Clone)]
pub struct Context {
    pub invocation: gb_environment::broker::Invocation,
    pub workspace: std::path::PathBuf,
    pub home: String,
    /// Work admission, from CommandFacts: true admits a turn cwd.
    pub mounts: BTreeMap<String, bool>,
    /// Actual guest mount access, from Shape: true means read-only.
    pub turn_mounts: BTreeMap<String, bool>,
    pub tools: std::collections::BTreeSet<String>,
    pub summary_tools: std::collections::BTreeSet<String>,
    pub broker: Broker,
    pub loans: BTreeMap<String, Arc<crate::tool_login::ToolLogin>>,
    pub rate_limits: BTreeMap<String, usize>,
    pub record: Arc<dyn Fn(Value) + Send + Sync>,
    pub required_io: Option<Arc<crate::required_io::RequiredIo>>,
}
async fn wait_for_turn(
    serial: &Arc<tokio::sync::Mutex<()>>,
    current: &tokio::sync::Mutex<Option<String>>,
    released: &tokio::sync::Notify,
) -> tokio::sync::OwnedMutexGuard<()> {
    loop {
        let changed = released.notified();
        tokio::pin!(changed);
        let _ = changed.as_mut().enable();
        let guard = serial.clone().lock_owned().await;
        if current.lock().await.is_none() {
            return guard;
        }
        // End-turn calls need the serial lock to revoke the active grant.
        drop(guard);
        changed.await;
    }
}

impl HttpHost {
    pub async fn start_context(context: Context) -> std::io::Result<Self> {
        let endpoint = Arc::new(tokio::sync::OnceCell::<(String, String)>::new());
        let current = Arc::new(tokio::sync::Mutex::new(None::<String>));
        let serial = Arc::new(tokio::sync::Mutex::new(()));
        let released = Arc::new(tokio::sync::Notify::new());
        let broker = context.broker.clone();
        let stored = endpoint.clone();
        let calls: HostCalls = Arc::new(move |name, args| {
            let context = context.clone();
            let endpoint = endpoint.clone();
            let current = current.clone();
            let serial = serial.clone();
            let released = released.clone();
            Box::pin(async move {
                let _serial = if name == "begin_turn" {
                    wait_for_turn(&serial, &current, &released).await
                } else {
                    serial.lock_owned().await
                };
                match name.as_str() {
                    "begin_turn" => {
                        let tool = args["tool"]
                            .as_str()
                            .ok_or_else(|| HostError::refused("Tool required."))?;
                        if !context.tools.contains(tool) {
                            return Err(HostError::refused("The command does not run that tool."));
                        }
                        let cwd = args["cwd"]
                            .as_str()
                            .ok_or_else(|| HostError::refused("Working directory required."))?;
                        if !admits(&context.mounts, cwd) {
                            return Err(HostError::refused("Working directory is not mounted."));
                        }
                        let mut active = current.lock().await;
                        if active.is_some() {
                            return Err(HostError::refused("A turn of this command is running."));
                        }
                        let env = if let Some(loan) = context.loans.get(tool) {
                            loan.begin().await.map_err(HostError::refused)?
                        } else {
                            return Err(HostError::refused("The tool has no credential broker."));
                        };
                        let grant = context.broker.activate().await;
                        *active = Some(grant.clone());
                        let (url, token) =
                            endpoint.get().expect("endpoint installed before serving");
                        Ok(
                            json!({"turn_grant":grant,"env":env,"cwd":cwd,"home":context.home,"mounts":context.turn_mounts,"member":{"name":"guildbotics_member","url":url,"authorization":format!("Bearer {token}")}}),
                        )
                    }
                    "end_turn" => {
                        let mut active = current.lock().await;
                        if active.as_deref() != args["turn_grant"].as_str() {
                            return Err(HostError::refused(
                                "The turn grant is invalid or expired.",
                            ));
                        }
                        context.broker.deactivate().await;
                        let mut refusal = String::new();
                        for loan in context.loans.values() {
                            let reason = loan.end().await;
                            if !reason.is_empty() {
                                refusal = reason;
                            }
                        }
                        *active = None;
                        released.notify_waiters();
                        Ok(json!({"refusal":refusal}))
                    }
                    "member" => {
                        let existing = current.lock().await.clone();
                        let temporary = existing.is_none();
                        let grant = match existing {
                            Some(grant) => grant,
                            None => context.broker.activate().await,
                        };
                        let result = context
                            .broker
                            .execute(MemberRequest {
                                arguments: serde_json::from_value(args["arguments"].clone())
                                    .map_err(|_| HostError::refused("Invalid member arguments."))?,
                                stdin: args["stdin"].as_str().unwrap_or("").into(),
                                turn_grant: grant,
                            })
                            .await;
                        if temporary {
                            context.broker.deactivate().await;
                        }
                        serde_json::to_value(result)
                            .map_err(|_| HostError::refused("Invalid member result."))
                    }
                    "acquire_inference" => {
                        let name = args["model_name"]
                            .as_str()
                            .ok_or_else(|| HostError::refused("Model name required."))?;
                        let limit = *context.rate_limits.get(name).ok_or_else(|| {
                            HostError::refused("The command does not use that model.")
                        })?;
                        acquire_inference(name, limit).await;
                        Ok(Value::Null)
                    }
                    "require_completion" | "evidence" => {
                        let run = gb_observability::runs::RunStore::open(&context.workspace)
                            .read(&context.invocation.run_id)
                            .map_err(|_| HostError::refused("Run record unavailable."))?;
                        if name == "evidence" {
                            return Ok(json!(run.map(|r| r.provider_evidence).unwrap_or_default()));
                        }
                        if run.and_then(|r| r.result).is_none() {
                            return Err(HostError::refused(
                                "Run completion has not been recorded.",
                            ));
                        }
                        Ok(Value::Null)
                    }
                    "record_completed" | "record_completion_missing" => {
                        (context.record)(json!({"type":name,"payload":args}));
                        Ok(Value::Null)
                    }
                    "decision_evaluated" => {
                        (context.record)(
                            json!({"type":"decision_evaluated","payload":args["payload"]}),
                        );
                        Ok(Value::Null)
                    }
                    "required_io" => {
                        let store = context
                            .required_io
                            .clone()
                            .ok_or_else(|| HostError::refused("Required IO store unavailable"))?;
                        let id = args["record_id"]
                            .as_str()
                            .ok_or_else(|| HostError::refused("Invalid IO record ID"))?
                            .to_owned();
                        let payload = args["payload"].clone();
                        tokio::task::spawn_blocking(move || store.write(&id, payload))
                            .await
                            .map_err(|_| HostError::refused("Required IO recording task failed"))?
                            .map_err(HostError::refused)?;
                        Ok(Value::Null)
                    }
                    "record" => {
                        let entries = args["entries"]
                            .as_array()
                            .ok_or_else(|| HostError::refused("Entries must be an array."))?;
                        for entry in entries {
                            serde_json::from_value::<gb_wire::Entry>(entry.clone())
                                .map_err(|_| HostError::refused("Invalid record entry."))?;
                            match entry["type"].as_str() {
                                Some("event") => check_key(&context, &entry["conversation"])?,
                                Some("credential") | Some("summary") => {
                                    let allowed = if entry["type"] == "summary" {
                                        &context.summary_tools
                                    } else {
                                        &context.tools
                                    };
                                    if !allowed.contains(entry["tool"].as_str().unwrap_or("")) {
                                        return Err(HostError::refused(
                                            "The command does not run that tool.",
                                        ));
                                    }
                                }
                                Some("io")
                                    if matches!(
                                        entry["io_type"].as_str(),
                                        Some(
                                            "cli_agent.request"
                                                | "cli_agent.response"
                                                | "llm.request"
                                                | "llm.response"
                                                | "jev.request"
                                                | "jev.response"
                                        )
                                    ) => {}
                                _ => return Err(HostError::refused("Invalid record entry.")),
                            }
                        }
                        for entry in entries {
                            if entry["type"] == "credential"
                                && let Some(loan) =
                                    context.loans.get(entry["tool"].as_str().unwrap_or(""))
                            {
                                loan.authentication_outcome(entry["failed"] == true)
                                    .map_err(HostError::refused)?;
                            }
                            (context.record)(entry.clone());
                        }
                        Ok(Value::Null)
                    }
                    "resolve" | "save" | "mark_unhealthy" => conversation(&context, &name, &args),
                    _ => Err(HostError::refused("Unknown host call.")),
                }
            })
        });
        let host = Self::start(calls, broker).await?;
        // The listener is unpublished until this function returns.
        let _ = stored.set((host.mcp_url(), host.token.clone()));
        Ok(host)
    }
}
fn admits(mounts: &BTreeMap<String, bool>, cwd: &str) -> bool {
    if !cwd.starts_with('/')
        || cwd.starts_with("//")
        || cwd.contains('\0')
        || cwd.split('/').any(|part| part == "..")
    {
        return false;
    }
    let cwd = format!(
        "/{}",
        cwd.split('/')
            .filter(|part| !part.is_empty() && *part != ".")
            .collect::<Vec<_>>()
            .join("/")
    );
    mounts
        .iter()
        .filter(|(root, _)| {
            root.as_str() == "/"
                || cwd
                    .strip_prefix(root.as_str())
                    .is_some_and(|tail| tail.is_empty() || tail.starts_with('/'))
        })
        .max_by_key(|(root, _)| root.split('/').filter(|part| !part.is_empty()).count())
        .is_some_and(|(_, open)| *open)
}

fn turn_directories(
    mounts: &[gb_environment::Mount],
    tool_mounts: &[gb_environment::Mount],
    copy: Option<&str>,
) -> BTreeMap<String, bool> {
    let mut directories = mounts
        .iter()
        .chain(tool_mounts)
        .map(|mount| (mount.guest.clone(), mount.readonly))
        .collect::<BTreeMap<_, _>>();
    if let Some(copy) = copy {
        directories.insert(copy.to_owned(), false);
    }
    directories
}

fn check_key(context: &Context, key: &Value) -> Result<(), HostError> {
    if key["person_id"] != context.invocation.person_id
        || key["work_kind"] != context.invocation.work_kind
        || key["work_identity"] != context.invocation.work_identity
        || !context
            .tools
            .contains(key["adapter"].as_str().unwrap_or(""))
    {
        return Err(HostError::refused("The conversation is not this work's."));
    }
    Ok(())
}
fn conversation(context: &Context, name: &str, args: &Value) -> Result<Value, HostError> {
    use sha2::{Digest, Sha256};
    let key = if name == "resolve" {
        &args["key"]
    } else {
        &args["record"]["key"]
    };
    check_key(context, key)?;
    let source = ["person_id", "adapter", "work_kind", "work_identity"]
        .map(|name| key[name].as_str().unwrap_or(""))
        .join("\0");
    let digest = Sha256::digest(source.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    // All untrusted names are hashed; no path component comes from the guest.
    let path = context
        .workspace
        .join(".guildbotics/local/agent-runtime/conversations")
        .join(&context.invocation.person_id)
        .join(key["adapter"].as_str().unwrap_or(""))
        .join(format!("{digest}.json"));
    let _lock = gb_base::files::FileLock::hold(
        &path.with_extension("lock"),
        std::time::Duration::from_secs(30),
    )
    .map_err(|_| HostError::refused("Conversation is busy."))?;
    let stored = gb_base::files::read_if_present(&path)
        .map_err(|_| HostError::refused("Conversation unavailable."))?;
    let previous = stored
        .map(|v| serde_json::from_slice::<Value>(&v))
        .transpose()
        .map_err(|_| HostError::refused("Conversation corrupt."))?;
    let mut record = if name == "resolve" {
        let policy = args["policy"].as_str().unwrap_or("auto");
        let usable = previous.as_ref().is_some_and(|r| {
            r["healthy"] == true
                && r["provider_session_id"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
        });
        if policy == "resume" && !usable {
            return Err(HostError::refused(
                "The conversation has no healthy provider session to resume.",
            ));
        }
        if !matches!(policy, "auto" | "fresh" | "resume") {
            return Err(HostError::refused("Invalid resume policy."));
        }
        let reason = if policy == "fresh" {
            "fresh"
        } else if policy == "auto" {
            previous.as_ref().map(rotation_reason).unwrap_or("")
        } else {
            ""
        }
        .to_owned();
        let generation = previous
            .as_ref()
            .and_then(|r| r["generation"].as_u64())
            .map(|g| g + u64::from(!reason.is_empty()))
            .unwrap_or(0);
        match previous {
            Some(record) if reason.is_empty() => record,
            _ => {
                json!({"key":key,"generation":generation,"healthy":true,"provider_session_id":"","provider_turn_id":"","context_cursor":"","last_event_id":"","last_run_id":"","provider":"","model":args["model"].as_str().unwrap_or(""),"effective_model":"","effective_effort":"","turn_count":0,"input_tokens":0,"output_tokens":0,"context_used_tokens":0,"context_size_tokens":0,"rotation_reason":reason})
            }
        }
    } else {
        args["record"].clone()
    };
    if name == "mark_unhealthy" {
        record["healthy"] = false.into();
        record["rotation_reason"] = args["reason"].clone();
    }
    let now = chrono::Utc::now().to_rfc3339();
    if record["created_at"].as_str().unwrap_or("").is_empty() {
        record["created_at"] = now.clone().into();
    }
    record["updated_at"] = now.into();
    if name == "resolve" && args["model"].as_str().is_some_and(|s| !s.is_empty()) {
        record["model"] = args["model"].clone();
    }
    let mut persisted = record.clone();
    persisted["version"] = 1.into();
    if let Some(object) = record.as_object_mut() {
        object.remove("version");
    }
    gb_base::files::atomic_write(
        &path,
        &serde_json::to_vec(&persisted).map_err(|_| HostError::refused("Invalid conversation."))?,
    )
    .map_err(|_| HostError::refused("Conversation unavailable."))?;
    Ok(record)
}

/// Read the same sealed login a turn would use. The UI never receives the
/// login contents, upstream token, or OS keychain's error text.
pub fn tool_status(
    data: &std::path::Path,
    home: &std::path::Path,
    catalog: &Value,
    keychain: &dyn gb_config::secrets::Keychain,
) -> Value {
    let device = gb_config::secrets::DeviceKey {
        machine_root: data.parent().unwrap_or(data).into(),
        keychain,
    };
    let tools=catalog.as_array().into_iter().flatten().filter_map(|tool|{
  let name=tool["name"].as_str()?;let provision=&tool["provision"];let broker=&provision["credential_broker"];
  let path=data.join("agent_environment").join(name).join("login.sealed");
  let label=format!("{name}:default:{}",broker["format"].as_str().unwrap_or(""));
  let command=gb_base::shell::managed_cli_command(&home.join(".guildbotics/bin/guildbotics"), &["environment", "login", name]);
  let mut status=gb_config::credential_vault::status(&device,&path,&label,command.clone());
  if status.saved {
    let valid=gb_config::credential_vault::unseal(&device,&path,&label).ok().flatten().is_some_and(|files| crate::tool_login::valid(&files,tool));
    if !valid {status.saved=false;status.state="corrupt";}
  }
  Some(json!({"name":name,"provisioned":!provision["package"].as_str().unwrap_or("").is_empty()||!provision["install"].as_str().unwrap_or("").is_empty(),"saved":status.saved,"credentials":status.state,"authentication_failed":data.join("agent_environment").join(name).join("authentication-failed").is_file(),"login_command":command,"usage":{"state":"unchecked"}}))
 }).collect::<Vec<_>>();
    json!(tools)
}

/// The listeners live exactly as long as the command, including all turns.
pub struct CommandServices {
    pub session: gb_environment::Session,
    _host: HttpHost,
    _gateways: Vec<Gateway>,
}
impl CommandServices {
    pub async fn settle(&self) {
        self._host.broker.settle().await;
    }
}
#[cfg(test)]
pub struct CommandTestServices {
    pub catalog: Value,
    pub recorder_port: Option<u16>,
}

fn configured_tools(resolved: &Value) -> std::collections::BTreeSet<String> {
    resolved["brains"]
        .as_object()
        .into_iter()
        .flat_map(|v| v.values())
        .filter_map(|brain| brain["definition"]["adapter"].as_str())
        .map(str::to_owned)
        .collect()
}

/// Every string of `value` that names a file of the copy `config`, named as
/// the configuration's file.
fn spoken_strings(config: &gb_environment::Configuration, value: &mut Value) {
    match value {
        Value::String(text) => *text = config.spoken(text),
        Value::Array(items) => items.iter_mut().for_each(|v| spoken_strings(config, v)),
        Value::Object(fields) => fields.values_mut().for_each(|v| spoken_strings(config, v)),
        _ => {}
    }
}

pub async fn prepare_command(
    host: &gb_environment::Host,
    workspace: &std::path::Path,
    command: &gb_environment::Command,
    preflight: &gb_environment::device::Device,
    keychain: Arc<dyn gb_config::secrets::Keychain>,
    member: Option<&crate::member::Backend>,
    #[cfg(test)] test: Option<&CommandTestServices>,
) -> Result<CommandServices, String> {
    if !command.access.read_only {
        gb_base::host_path::open_directory(
            &workspace
                .join(".guildbotics/local/clones")
                .join(&command.person_id),
            true,
        )
        .map_err(|e| e.to_string())?;
    }
    let roots = gb_config::commands::CommandRoots {
        config: command.config.path(),
        templates: host.package.join("templates"),
    };
    let mut resolved = gb_config::resolved_guest::resolve(
        &roots,
        workspace,
        &host.identity_root,
        &command.person_id,
    )
    .map_err(|e| command.config.spoken(&e.to_string()))?;
    // The guest is told of the configuration, not of the run's copy of it.
    spoken_strings(&command.config, &mut resolved);
    if let Some(commands) = resolved["commands"].as_object_mut() {
        for value in commands.values_mut() {
            if let Some(path) = value.as_str() {
                *value =
                    gb_environment::command_path(host, &command.config, std::path::Path::new(path))
                        .map_err(|_| "Invalid command guest path.")?
                        .into();
            }
        }
    }
    let shape = gb_environment::shape(
        host,
        workspace,
        command,
        Some(resolved["language"].as_str().unwrap_or("en").into()),
    )
    .map_err(|_| "The command access contract cannot be resolved.".to_owned())?;
    let invocation = broker_invocation(command);
    let program = std::env::var_os("GUILDBOTICS_MEMBER_CLI")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| host.home.join(".guildbotics/bin/guildbotics"));
    let guest_process = gb_environment::guest_process::Channel::new();
    let broker = match member.and_then(|backend| {
        backend
            .executor
            .broker(invocation.clone(), guest_process.clone())
    }) {
        Some(handler) => Broker::native(invocation.clone(), handler)
            .tracked(member.and_then(|backend| backend.executor.tracker())),
        None => Broker::new(program, invocation.clone()),
    };
    let tools = configured_tools(&resolved);
    let redactor = command_redactor(host, workspace, keychain.as_ref())?;
    let diagnostics = gb_observability::Diagnostics::open(workspace);
    let trace_id = command.trace_id.clone();
    let person_id = command.person_id.clone();
    let command_name = command.name.clone();
    let run_id = command.run_id.clone();
    let full_transcript = std::fs::read_to_string(roots.config.join("transcripts.yml"))
        .ok()
        .and_then(|text| gb_config::yaml_value(&text).ok())
        .is_some_and(|v| {
            v["detail"]
                .as_str()
                .is_some_and(|s| s.trim().eq_ignore_ascii_case("full"))
        });
    let record = Arc::new(move |entry: Value| {
        if let Some(value) = record_entry_detail(
            entry,
            &trace_id,
            &run_id,
            &person_id,
            &command_name,
            full_transcript,
        ) && let Value::Object(value) = redactor.local(value)
        {
            if value["type"] == "workflow.completed" {
                if let Err(error) = diagnostics.record_once(value) {
                    tracing::warn!("cannot record workflow completion: {error}");
                }
            } else {
                diagnostics.record(value);
            }
        }
    });
    let catalog: Value = serde_json::from_slice(
        &std::fs::read(
            host.package
                .join("templates/environment/cli_provision.json"),
        )
        .map_err(|_| "Tool catalog unavailable.")?,
    )
    .map_err(|_| "Tool catalog invalid.")?;
    #[cfg(test)]
    let catalog = test.map_or(catalog, |test| test.catalog.clone());
    let mut loans = BTreeMap::new();
    let mut tool_mounts = vec![];
    let mut tool_ports = vec![];
    #[cfg(test)]
    if let Some(port) = test.and_then(|test| test.recorder_port) {
        tool_ports.push(port);
    }
    let mut relays = vec![];
    let mut turn_domains = vec![];
    for tool in catalog.as_array().into_iter().flatten() {
        let name = tool["name"].as_str().unwrap_or("");
        if !tools.contains(name) {
            continue;
        }
        let loan = crate::tool_login::ToolLogin::prepare_with_keychain(
            &host.data,
            &host.home,
            tool.clone(),
            resolved["language"].as_str().unwrap_or("en"),
            crate::tool_login::LoginVm {
                data: host.data.clone(),
                home: host.home.clone(),
                package: host.package.clone(),
                snapshot: preflight
                    .snapshot
                    .clone()
                    .ok_or("The snapshot is unavailable.")?,
                memory_mib: preflight.resources.memory_mib,
                cpus: preflight.resources.cpus,
                nameservers: preflight.view["dns"]["nameservers"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect(),
            },
            keychain.clone(),
        )
        .await?;
        tool_mounts.push(gb_environment::Mount {
            guest: loan.guest_root.clone(),
            host: Some(loan.host_root.path().into()),
            readonly: false,
            quota_mib: None,
        });
        tool_mounts.extend(loan.persisted_mounts(command.access.read_only)?);
        tool_ports.push(loan.gateway.port);
        let hosts = loan.relayed_hosts();
        if !hosts.is_empty() {
            relays.push(json!({"hosts":hosts,"port":loan.gateway.port}));
        }
        turn_domains.extend(loan.turn_domains());
        loans.insert(name.to_owned(), loan);
    }
    let providers = resolved["brains"]
        .as_object()
        .into_iter()
        .flat_map(|brains| brains.values())
        .filter_map(|brain| {
            if brain["kind"] == "jev" {
                return Some("jev".to_owned());
            }
            let class = brain["definition"]["model_class"]
                .as_str()?
                .to_ascii_lowercase();
            ["openai", "anthropic", "gemini"]
                .into_iter()
                .find(|name| class.contains(name))
                .map(str::to_owned)
        })
        .collect::<std::collections::BTreeSet<String>>();
    let required_host = gb_environment::Host {
        data: host.data.clone(),
        home: host.home.clone(),
        package: host.package.clone(),
        identity_root: host.identity_root.clone(),
    };
    let required_workspace = workspace.to_path_buf();
    let required_keychain = keychain.clone();
    let mut required_io = crate::required_io::RequiredIo::new(
        workspace.into(),
        command.trace_id.clone(),
        command.person_id.clone(),
        move || {
            recording_redactor(
                &required_host,
                &required_workspace,
                required_keychain.as_ref(),
                true,
            )
        },
    );
    if let Some(subject) = invocation.chat_subject.clone() {
        let person = command.person_id.clone();
        let store = crate::chat_state::Store {
            workspace: workspace.into(),
            writes: Arc::new(gb_base::shared_writes::SharedWrites::new(
                host.identity_root.clone(),
                Some(workspace.into()),
            )),
            limit: 500,
        };
        required_io = required_io.with_adoption(move |payload| {
            store
                .adopt_effort(&subject, &person, payload)
                .map_err(|_| "Judgment effort could not be persisted".into())
        });
    }
    let required_io = Arc::new(required_io);
    let context = Context {
        invocation,
        workspace: workspace.into(),
        home: gb_environment::guest_path(&host.home).map_err(|_| "Invalid guest home.")?,
        mounts: serde_json::from_str::<gb_wire::CommandFacts>(
            &shape
                .env
                .iter()
                .find(|(key, _)| key == "GUILDBOTICS_COMMAND")
                .ok_or("Command facts are missing.")?
                .1,
        )
        .map_err(|_| "Command facts are invalid.")?
        .mounts
        .into_iter()
        .collect(),
        turn_mounts: turn_directories(
            &shape.mounts,
            &tool_mounts,
            shape.copy.as_ref().map(|copy| copy.guest.as_str()),
        ),
        summary_tools: tools
            .iter()
            .cloned()
            .chain(providers.iter().cloned())
            .collect(),
        tools,
        broker,
        loans,
        rate_limits: resolved["brains"]
            .as_object()
            .into_iter()
            .flat_map(|brains| brains.values())
            .filter(|brain| brain["kind"] == "model")
            .filter_map(|brain| {
                Some((
                    brain["definition"]["name"].as_str()?.to_owned(),
                    brain["definition"]["rate_limit"]["max_requests_per_minute"]
                        .as_u64()
                        .unwrap_or(0) as usize,
                ))
            })
            .collect(),
        record,
        required_io: Some(required_io),
    };
    let window = HttpHost::start_context(context)
        .await
        .map_err(|_| "The command host window cannot start.".to_owned())?;
    let mut gateways = vec![];
    let mut urls = serde_json::Map::new();
    let mut keys = serde_json::Map::new();
    for (name, provider, origin, key, route) in [
        (
            "openai",
            gb_environment::gateway::Provider::OpenAi,
            "https://api.openai.com",
            "OPENAI_API_KEY",
            "/v1/*",
        ),
        (
            "anthropic",
            gb_environment::gateway::Provider::Anthropic,
            "https://api.anthropic.com",
            "ANTHROPIC_API_KEY",
            "/v1/*",
        ),
        (
            "gemini",
            gb_environment::gateway::Provider::Gemini,
            "https://generativelanguage.googleapis.com",
            "GOOGLE_API_KEY",
            "/v1beta/*",
        ),
    ] {
        if !providers.contains(name) {
            continue;
        }
        let key = resolved["brains"]
            .as_object()
            .into_iter()
            .flat_map(|brains| brains.values())
            .map(|brain| &brain["definition"])
            .find(|definition| {
                definition["model_class"]
                    .as_str()
                    .is_some_and(|class| class.to_ascii_lowercase().contains(name))
            })
            .and_then(|definition| definition["api_key_env"].as_str())
            .unwrap_or(key);
        let value = host_secret_in(host, workspace, key, keychain.as_ref())?;
        let source: TokenSource = Arc::new(move |_| {
            let value = value.clone();
            Box::pin(async move {
                value.ok_or_else(|| "The inference credential is unavailable.".to_owned())
            })
        });
        let gateway = Gateway::start(
            Policy {
                provider,
                origin: origin.into(),
                routes: vec![("POST".into(), route.into()), ("GET".into(), route.into())],
            },
            source,
        )
        .await
        .map_err(|_| "The inference gateway cannot start.".to_owned())?;
        let base = if name == "openai" {
            format!("{}/v1", gateway.guest_url())
        } else {
            gateway.guest_url()
        };
        urls.insert(name.into(), base.into());
        keys.insert(name.into(), gateway.lease.stand_in.clone().into());
        gateways.push(gateway);
    }
    if let Some(definition) = resolved["brains"]
        .as_object()
        .into_iter()
        .flat_map(|brains| brains.values())
        .find(|brain| brain["kind"] == "jev")
        .map(|brain| &brain["definition"])
    {
        let base = definition["base_url"]
            .as_str()
            .ok_or("Jev base_url is missing.")?;
        let parsed = url::Url::parse(base).map_err(|_| "Jev base_url is invalid.")?;
        let route = format!("{}/systemone", parsed.path().trim_end_matches('/'));
        let value = host_secret_in(
            host,
            workspace,
            definition["api_key_env"]
                .as_str()
                .unwrap_or("TYPESAFE_API_KEY"),
            keychain.as_ref(),
        )?;
        let source: TokenSource = Arc::new(move |_| {
            let value = value.clone();
            Box::pin(async move {
                value.ok_or_else(|| "The inference credential is unavailable.".to_owned())
            })
        });
        let gateway = Gateway::start(
            Policy {
                provider: gb_environment::gateway::Provider::Jev,
                origin: parsed.origin().ascii_serialization(),
                routes: vec![("POST".into(), route)],
            },
            source,
        )
        .await
        .map_err(|_| "The Jev gateway cannot start.".to_owned())?;
        urls.insert(
            "jev".into(),
            format!(
                "{}{}",
                gateway.guest_url(),
                parsed.path().trim_end_matches('/')
            )
            .into(),
        );
        keys.insert("jev".into(), gateway.lease.stand_in.clone().into());
        gateways.push(gateway);
    }
    resolved["inference_urls"] = urls.into();
    resolved["inference_keys"] = keys.into();
    let session = gb_environment::Session {
        guest_process,
        window_url: window.guest_url(),
        window_token: window.token.clone(),
        resolved,
        mounts: tool_mounts,
        env: vec![
            (
                "GUILDBOTICS_GATEWAY_RELAYS".into(),
                serde_json::to_string(&relays).map_err(|_| "Invalid gateway relays.")?,
            ),
            (
                "GUILDBOTICS_TURN_DOMAINS".into(),
                serde_json::to_string(&turn_domains).map_err(|_| "Invalid tool domains.")?,
            ),
        ],
        ports: std::iter::once(window.port)
            .chain(tool_ports)
            .chain(gateways.iter().map(|g| g.port))
            .collect(),
    };
    Ok(CommandServices {
        session,
        _host: window,
        _gateways: gateways,
    })
}

pub(crate) fn command_redactor(
    host: &gb_environment::Host,
    workspace: &std::path::Path,
    keychain: &dyn gb_config::secrets::Keychain,
) -> Result<gb_observability::redaction::Redactor, String> {
    recording_redactor(host, workspace, keychain, false)
}

fn recording_redactor(
    host: &gb_environment::Host,
    workspace: &std::path::Path,
    keychain: &dyn gb_config::secrets::Keychain,
    required: bool,
) -> Result<gb_observability::redaction::Redactor, String> {
    let writes = gb_base::shared_writes::SharedWrites::new(
        host.identity_root.clone(),
        Some(workspace.into()),
    );
    let mut values = Vec::new();
    let mut names = std::collections::BTreeSet::from_iter(
        [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "JEV_API_KEY",
            "TYPESAFE_API_KEY",
        ]
        .map(str::to_owned),
    );
    for scope in [
        gb_config::secrets::Scope::Identity,
        gb_config::secrets::Scope::Workspace,
    ] {
        let root = match scope {
            gb_config::secrets::Scope::Identity => &host.identity_root,
            gb_config::secrets::Scope::Workspace => workspace,
        };
        match gb_config::secrets::read_secret_index(&root.join(".guildbotics/config")) {
            Ok(index) => names.extend(index),
            Err(_) if required => return Err("Required IO redaction index unavailable".into()),
            Err(_) => tracing::warn!("The recording secret index could not be read."),
        }
        match gb_config::secrets::SecretStore::new(
            &writes,
            scope,
            gb_config::secrets::DeviceKey {
                machine_root: host.data.parent().unwrap_or(&host.data).into(),
                keychain,
            },
        )
        .and_then(|store| store.values())
        {
            Ok(found) => {
                for (name, value) in found {
                    names.insert(name);
                    values.push(value);
                }
            }
            Err(_) if required => return Err("Required IO redaction values unavailable".into()),
            Err(_) => tracing::warn!(
                "The recording secret store could not be opened; retaining available masks."
            ),
        }
    }
    values.extend(
        names
            .into_iter()
            .filter_map(|name| std::env::var(name).ok()),
    );
    Ok(gb_observability::redaction::Redactor::with_min_length(
        values,
        if required { 1 } else { 8 },
    ))
}

pub(crate) fn host_secret_in(
    host: &gb_environment::Host,
    workspace: &std::path::Path,
    name: &str,
    keychain: &dyn gb_config::secrets::Keychain,
) -> Result<Option<String>, String> {
    if let Ok(value) = std::env::var(name)
        && !value.is_empty()
    {
        return Ok(Some(value));
    }
    let writes = gb_base::shared_writes::SharedWrites::new(
        host.identity_root.clone(),
        Some(workspace.into()),
    );
    let mut value = None;
    for scope in [
        gb_config::secrets::Scope::Identity,
        gb_config::secrets::Scope::Workspace,
    ] {
        let store = gb_config::secrets::SecretStore::new(
            &writes,
            scope,
            gb_config::secrets::DeviceKey {
                machine_root: host.data.parent().unwrap_or(&host.data).into(),
                keychain,
            },
        )
        .map_err(|_| "The host secret store is unavailable.".to_owned())?;
        if let Some(found) = store
            .get(name)
            .map_err(|_| "The host secret store could not open.".to_owned())?
        {
            value = Some(found);
        }
    }
    Ok(value)
}

async fn acquire_inference(name: &str, limit: usize) {
    use std::collections::{HashMap, VecDeque};
    use std::sync::{Mutex, OnceLock};
    static WINDOWS: OnceLock<Mutex<HashMap<String, VecDeque<std::time::Instant>>>> =
        OnceLock::new();
    if limit == 0 {
        return;
    }
    loop {
        let wait = {
            let mut windows = WINDOWS
                .get_or_init(Default::default)
                .lock()
                .expect("rate limit mutex poisoned");
            let queue = windows.entry(name.to_owned()).or_default();
            let now = std::time::Instant::now();
            while queue.front().is_some_and(|instant| {
                now.duration_since(*instant) >= std::time::Duration::from_secs(60)
            }) {
                queue.pop_front();
            }
            if queue.len() < limit {
                queue.push_back(now);
                return;
            }
            std::time::Duration::from_secs(60)
                .saturating_sub(now.duration_since(*queue.front().unwrap()))
        };
        tokio::time::sleep(wait).await;
    }
}

#[cfg(test)]
mod provider_http_tests {
    use super::*;
    #[tokio::test]
    async fn every_sdk_auth_header_reaches_only_its_pinned_upstream_and_masks_echoes() {
        for provider in [
            gb_environment::gateway::Provider::OpenAi,
            gb_environment::gateway::Provider::Anthropic,
            gb_environment::gateway::Provider::Gemini,
        ] {
            let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
                .await
                .unwrap();
            let port = listener.local_addr().unwrap().port();
            let expected = provider.credential_header();
            let upstream = Router::new().route(
                "/v1/messages",
                post(move |headers: HeaderMap| async move {
                    let token = headers.get(expected).unwrap().to_str().unwrap();
                    assert!(token.contains("synthetic-private"));
                    assert_eq!(headers["anthropic-beta"], "test-beta");
                    (
                        StatusCode::OK,
                        [("x-echo", "synthetic-private")],
                        "echo synthetic-private",
                    )
                }),
            );
            let task = tokio::spawn(async move {
                axum::serve(listener, upstream).await.unwrap();
            });
            let source: TokenSource =
                Arc::new(|_| Box::pin(async { Ok("synthetic-private".into()) }));
            let gateway = Gateway::start(
                Policy {
                    provider,
                    origin: format!("http://127.0.0.1:{port}"),
                    routes: vec![("POST".into(), "/v1/messages".into())],
                },
                source,
            )
            .await
            .unwrap();
            let client = reqwest::Client::builder().no_proxy().build().unwrap();
            let stand_in = if expected == "authorization" {
                format!("Bearer {}", gateway.lease.stand_in)
            } else {
                gateway.lease.stand_in.clone()
            };
            let response = client
                .post(format!(
                    "http://127.0.0.1:{}/v1/messages?beta=true",
                    gateway.port
                ))
                .header(expected, stand_in)
                .header("anthropic-beta", "test-beta")
                .header("cookie", "ignored")
                .send()
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::OK);
            assert!(
                !response.headers()["x-echo"]
                    .to_str()
                    .unwrap()
                    .contains("synthetic-private")
            );
            assert!(!response.text().await.unwrap().contains("synthetic-private"));
            task.abort();
        }
    }
}

/// Keep the canonical transcript envelope: IO remains local IO, summaries
/// close spans, and provider events retain their attribution and span IDs.
#[cfg(test)]
fn record_entry(
    entry: Value,
    trace: &str,
    run: &str,
    person: &str,
    command: &str,
) -> Option<Value> {
    record_entry_detail(entry, trace, run, person, command, false)
}
fn record_entry_detail(
    entry: Value,
    trace: &str,
    run: &str,
    person: &str,
    command: &str,
    full: bool,
) -> Option<Value> {
    use sha2::{Digest, Sha256};
    let span = &entry["span"];
    let mut attributes = json!({});
    let mut kind = "event";
    let (event_type, source, mut payload) = match entry["type"].as_str()? {
        "io" => {
            kind = "io";
            (
                entry["io_type"].as_str()?.to_owned(),
                "intelligence",
                entry["payload"].clone(),
            )
        }
        "event" => {
            let event = &entry["event"];
            let event_kind = event["kind"].as_str()?;
            let name = event["name"].as_str().unwrap_or("");
            // The standard transcript omits deltas/thinking; completed text
            // and interrupted partial text remain available to readers.
            let lower = format!("{event_kind} {name}").to_lowercase();
            if !full
                && ["delta", "thinking", "reasoning"]
                    .iter()
                    .any(|part| lower.contains(part))
            {
                return None;
            }
            let key = &entry["conversation"];
            let source = ["person_id", "adapter", "work_kind", "work_identity"]
                .map(|k| key[k].as_str().unwrap_or(""))
                .join("\0");
            let stable = Sha256::digest(source.as_bytes())
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>();
            attributes = json!({"agent.adapter":key["adapter"],"agent.run_id":run,"agent.conversation_id":stable,"agent.conversation_generation":entry["generation"],"agent.provider_session_id":event["provider_session_id"],"agent.provider_turn_id":event["provider_turn_id"],"agent.context_cursor":entry["context_cursor"]});
            let mut payload = event.clone();
            if let Some(map) = payload.as_object_mut() {
                for key in ["kind", "provider_session_id", "provider_turn_id", "item_id"] {
                    map.remove(key);
                }
            }
            (
                format!("agent_runtime.{event_kind}"),
                "agent_runtime",
                event_redact(payload),
            )
        }
        "credential" => {
            let failed = entry["failed"] == true;
            attributes =
                json!({"credential.provider":"cli_agent","credential.cli_agent":entry["tool"]});
            let mut payload =
                json!({"provider":"cli_agent","cli_agent":entry["tool"],"person_id":person});
            if failed {
                payload["code"] = "authentication".into();
                attributes["error.category"] = "authentication".into();
            }
            (
                if failed {
                    "credential.failed"
                } else {
                    "credential.verified"
                }
                .into(),
                "cli_agent",
                payload,
            )
        }
        "summary" => {
            let tool = entry["tool"].as_str().unwrap_or("");
            let agent_kind = if matches!(tool, "openai" | "anthropic" | "gemini") {
                "llm"
            } else if tool == "jev" {
                "jev"
            } else {
                "cli_agent"
            };
            attributes =
                json!({"agent.kind":agent_kind,"agent.slot":entry["slot"],"agent.adapter":tool});
            let mut payload = json!({"model":entry["model"],"model_specified":entry["model_specified"],"effort":entry["effort"],"usage":entry.get("usage").filter(|v|!v.is_null()).cloned().unwrap_or(json!({}))});
            if !entry["duration_ms"].is_null() {
                payload["duration_ms"] = entry["duration_ms"].clone();
            }
            (
                format!("span.{}", entry["status"].as_str().unwrap_or("finished")),
                "intelligence",
                payload,
            )
        }
        "decision_evaluated" => {
            attributes["agent.run_id"] = run.into();
            let mut payload = serde_json::Map::new();
            for key in [
                "evaluation_id",
                "brain",
                "model",
                "route",
                "reason",
                "response_effort",
                "error",
                "duration_ms",
            ] {
                payload.insert(key.into(), entry["payload"][key].clone());
            }
            (
                "decision.evaluated".into(),
                "decision",
                event_redact(Value::Object(payload)),
            )
        }
        "record_completed" => (
            "workflow.completed".into(),
            "workflow",
            entry["payload"].clone(),
        ),
        "record_completion_missing" => (
            "workflow.completion_missing".into(),
            "workflow",
            entry["payload"].clone(),
        ),
        _ => return None,
    };
    if !full && event_type == "cli_agent.response" {
        let stderr = payload["stderr"].as_str().unwrap_or("");
        let start = stderr.len().saturating_sub(8192);
        let mut boundary = start;
        while !stderr.is_char_boundary(boundary) {
            boundary += 1;
        }
        let kept = stderr[boundary..].to_owned();
        payload["stderr_truncated"] = (boundary > 0).into();
        payload["stderr"] = kept.into();
    }
    Some(
        json!({"kind":kind,"type":event_type,"trace_id":trace,"span_id":span["span_id"],"call_id":span["call_id"],"parent_id":span["parent_id"],"span":span["name"].as_str().unwrap_or(""),"source":source,"person_id":person,"command":command,"workflow":"","timestamp":chrono::Utc::now().to_rfc3339(),"attributes":attributes,"payload":payload}),
    )
}

fn event_redact(value: Value) -> Value {
    static INLINE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"(?i)(?P<label>(?:--)?(?:access[-_]?token|api[-_]?key|token|password|secret|authorization))(?P<separator>\s*(?:=|:)\s*|\s+)(?P<value>(?:bearer\s+)?[^\s,;]+)").expect("constant secret pattern")
    });
    match value {
        Value::String(text) => INLINE
            .replace_all(
                &text.chars().take(8192).collect::<String>(),
                "${label}${separator}***",
            )
            .into_owned()
            .into(),
        Value::Array(items) => items.into_iter().take(100).map(event_redact).collect(),
        Value::Object(items) => Value::Object(
            items
                .into_iter()
                .map(|(key, value)| {
                    let lower = key.to_ascii_lowercase();
                    let value = if ["token", "secret", "password", "credential", "authorization"]
                        .iter()
                        .any(|part| lower.contains(part))
                    {
                        json!("***")
                    } else {
                        event_redact(value)
                    };
                    (key, value)
                })
                .collect(),
        ),
        scalar => scalar,
    }
}

fn rotation_reason(record: &Value) -> &str {
    if record["healthy"] == false {
        return record["rotation_reason"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("unhealthy_session");
    }
    if record["turn_count"].as_u64().unwrap_or(0) >= 100 {
        return "turn_limit";
    }
    if record["input_tokens"]
        .as_u64()
        .unwrap_or(0)
        .saturating_add(record["output_tokens"].as_u64().unwrap_or(0))
        >= 1_000_000
    {
        return "usage_limit";
    }
    let size = record["context_size_tokens"].as_u64().unwrap_or(0);
    if size > 0 && record["context_used_tokens"].as_u64().unwrap_or(0) as f64 / size as f64 >= 0.9 {
        return "context_limit";
    }
    if record["updated_at"]
        .as_str()
        .and_then(|t| chrono::DateTime::parse_from_rfc3339(t).ok())
        .is_some_and(|time| {
            chrono::Utc::now().signed_duration_since(time) > chrono::Duration::days(7)
        })
    {
        return "ttl_expired";
    }
    ""
}

#[cfg(test)]
#[path = "gateway_tests.rs"]
mod gateway_contract_tests;

pub(crate) fn broker_invocation(
    command: &gb_environment::Command,
) -> gb_environment::broker::Invocation {
    gb_environment::broker::Invocation {
        chat_subject: command
            .invocation
            .as_ref()
            .and_then(|v| v.work.as_ref())
            .and_then(|w| w.chat.as_ref())
            .cloned(),
        person_id: command.person_id.clone(),
        run_id: command.run_id.clone(),
        trace_id: command.trace_id.clone(),
        work_kind: command.work_kind.clone(),
        work_identity: command.work_identity.clone(),
        cwd: command.cwd.clone(),
        access: command.access.clone(),
    }
}
