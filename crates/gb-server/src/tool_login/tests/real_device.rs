//! A real VM, synthetic login, and loopback-only upstream.

#[path = "real_device/provider_contracts.rs"]
mod provider_contracts;

use super::*;
use axum::{Router, extract::Request, routing::any};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use gb_environment::{
    Mount,
    runtime::{self, Boot, Network, Process},
};
use std::{fs, sync::Mutex, time::Duration};

const CANARY: &str = "synthetic-device-login-never-in-turn";
const PYTHON: &str = "/opt/guildbotics/venv/bin/python";

struct Device {
    dir: tempfile::TempDir,
    vm: LoginVm,
}

impl Device {
    fn new() -> Self {
        assert!(
            std::env::var_os("CI").is_none(),
            "real_device requires an attending owner"
        );
        let root = fs::canonicalize(
            std::env::var_os("GUILDBOTICS_REAL_DEVICE_ROOT").expect("verification root required"),
        )
        .unwrap();
        let home = root.join("guest-home");
        assert!(home.is_dir());
        let package = gb_base::home::python_package();
        let recipe = gb_environment::recipe::Recipe::load(&package.join("templates")).unwrap();
        let snapshot = gb_environment::snapshot::ready(&root.join("workspace"), &recipe.name(None))
            .expect("build the snapshot from this checkout first");
        let dir = tempfile::Builder::new()
            .prefix("gb")
            .rand_bytes(6)
            .tempdir_in("/tmp")
            .unwrap();
        let data = dir.path().join("data");
        runtime::ensure(&data.join("msb")).unwrap();
        let vm = LoginVm {
            data,
            home,
            package,
            snapshot,
            memory_mib: 4096,
            cpus: 2,
            nameservers: vec![],
        };
        Self { dir, vm }
    }

    fn boot<'a>(&'a self, home: &'a Path, mounts: &'a [Mount], network: &'a Network) -> Boot<'a> {
        Boot {
            home,
            snapshot: &self.vm.snapshot,
            memory_mib: self.vm.memory_mib,
            cpus: self.vm.cpus,
            mounts,
            network,
            worktree: None,
        }
    }

    fn catalog(&self) -> Vec<Value> {
        serde_json::from_slice(
            &fs::read(
                self.vm
                    .package
                    .join("templates/environment/cli_provision.json"),
            )
            .unwrap(),
        )
        .unwrap()
    }
}

fn credentials_for(catalog: &Value) -> Files {
    let jwt = format!(
        "{}.{}.{CANARY}",
        URL_SAFE_NO_PAD.encode(br#"{"alg":"none"}"#),
        URL_SAFE_NO_PAD.encode(br#"{"exp":4102444800}"#),
    );
    let refresh = format!("{CANARY}-refresh");
    let value = match catalog["name"].as_str().unwrap() {
        "codex" => json!({
            "auth_mode": "chatgpt", "tokens": {"access_token": jwt, "id_token": jwt,
            "refresh_token": refresh, "account_id": "test"},
        }),
        "claude" => json!({"claudeAiOauth": {
            "accessToken": CANARY, "refreshToken": refresh, "expiresAt": 4102444800000_u64,
            "scopes": ["user:inference"], "subscriptionType": "max",
        }}),
        "grok" => json!({"https://auth.x.ai::test": {
            "key": CANARY, "refresh_token": refresh, "expires_at": "2100-01-01T00:00:00Z",
            "auth_mode": "oidc",
        }}),
        "copilot" => json!({"authTokens": {"https://github.com:test": {"token": CANARY}}}),
        "antigravity" => json!({
            "token": {"access_token": CANARY, "refresh_token": refresh,
            "expiry": "2100-01-01T00:00:00Z", "token_type": "Bearer"},
            "auth_method": "consumer", "id_token": format!("{CANARY}-identity"),
        }),
        _ => panic!("add the provisioned tool's synthetic account shape"),
    };
    Files::from([(
        catalog["provision"]["auth"].as_str().unwrap().to_owned(),
        serde_json::to_vec(&value).unwrap(),
    )])
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_provider_turn_keeps_the_login_on_the_host() {
    let device = Device::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let recorder = {
        let seen = seen.clone();
        tokio::spawn(async move {
            let app = Router::new().fallback(any(move |request: Request| {
                let seen = seen.clone();
                async move {
                    let auth = request.headers()["authorization"]
                        .to_str()
                        .unwrap()
                        .to_owned();
                    seen.lock().unwrap().push((
                        request.method().to_string(),
                        request.uri().path().to_owned(),
                        auth.clone(),
                    ));
                    (
                        [(
                            axum::http::header::HeaderName::from_static("x-echo"),
                            auth.clone(),
                        )],
                        auth,
                    )
                }
            }));
            axum::serve(listener, app).await.unwrap();
        })
    };
    let keychain = FakeKeychain;
    let key = DeviceKey {
        machine_root: device.dir.path().into(),
        keychain: &keychain,
    };
    let runtime_home = device.vm.data.join("msb");
    let mut mounts = Vec::new();
    let mut network = Network::default();
    let mut loans = Vec::new();
    let mut probes = Vec::new();
    let mut expected = Vec::new();
    for mut catalog in device.catalog() {
        let name = catalog["name"].as_str().unwrap().to_owned();
        let files = credentials_for(&catalog);
        let actual = access(&files, &catalog).unwrap();
        let broker = &mut catalog["provision"]["credential_broker"];
        let format = broker["format"].as_str().unwrap().to_owned();
        let mut routes = Vec::new();
        for rule in broker["routes"].as_array_mut().unwrap() {
            let (method, target) = rule.as_str().unwrap().split_once(' ').unwrap();
            let path = if target.starts_with("https://") {
                reqwest::Url::parse(target).unwrap().path().to_owned()
            } else {
                target.to_owned()
            };
            let sample = path.replace('*', "probe");
            expected.push((
                method.to_owned(),
                sample.clone(),
                format!("Bearer {actual}"),
            ));
            routes.push(json!([method, sample]));
            *rule = json!(format!("{method} {path}"));
        }
        broker["upstream"] = json!(origin);
        // A synthetic recorder must never trigger a real provider refresh.
        broker["refresh"] = json!([]);
        let vault = path(&device.vm.data, &name);
        credential_vault::seal(&key, &vault, &label(&name, &format), &files).unwrap();
        let loan = ToolLogin::prepare(
            &device.vm.data,
            &device.vm.home,
            catalog.clone(),
            "en",
            device.vm.clone(),
        )
        .await
        .unwrap();
        let env = loan
            .begin_with_keychain(Arc::new(FakeKeychain))
            .await
            .unwrap();
        let provision = &catalog["provision"];
        let broker = &provision["credential_broker"];
        probes.push(json!({
            "name": name, "env": env, "base": loan.gateway.guest_url(),
            "root": loan.guest_root, "auth": provision["auth"],
            "access_path": broker["access_token"], "stand_in_env": broker["stand_in_env"],
            "stand_in_command": broker["stand_in_command_env"], "routes": routes,
        }));
        mounts.push(Mount {
            guest: loan.guest_root.clone(),
            host: Some(gb_base::host_path::directory(loan.host_root.path(), false).unwrap()),
            readonly: true,
        });
        network.host_ports.push(loan.gateway.port);
        assert!(!String::from_utf8_lossy(&fs::read(vault).unwrap()).contains(CANARY));
        loans.push(loan);
    }
    let argv = vec![
        PYTHON.into(),
        "-I".into(),
        "-c".into(),
        include_str!("gateway_probe.py").into(),
    ];
    let input = serde_json::to_vec(&probes).unwrap();
    let result = tokio::time::timeout(
        Duration::from_secs(180),
        runtime::run_private(
            &device.boot(&runtime_home, &mounts, &network),
            &Process {
                argv: &argv,
                cwd: "/",
                env: &[],
                stdin: &input,
            },
        ),
    )
    .await
    .expect("gateway probe timed out")
    .unwrap();
    assert_eq!(result.code, 0, "guest gateway probe failed");
    assert_eq!(result.reply, b"gateway-probe-ok\n");
    let mut observed = seen.lock().unwrap().clone();
    observed.sort();
    expected.sort();
    assert_eq!(observed, expected);
    for loan in loans {
        assert!(loan.end().await.is_empty());
    }
    recorder.abort();
}

async fn private_login(cancel: bool) {
    let device = Device::new();
    let catalog = device
        .catalog()
        .into_iter()
        .find(|c| c["name"] == "claude")
        .unwrap();
    let files = credentials_for(&catalog);
    let mut plan = private_plan(
        &device.vm, &catalog, files.clone(),
        vec![
            "sh".into(), "-ec".into(),
            if cancel {
                "test -s \"$CLAUDE_CONFIG_DIR/.credentials.json\"; printf 'ready\\n'; sleep 300"
            } else {
                "test -s \"$CLAUDE_CONFIG_DIR/.credentials.json\"; test \"$(stat -f -c %T \"$HOME\")\" = tmpfs; printf 'ready\\n'"
            }.into(),
        ],
        false,
    ).unwrap();
    // No upstream domains are needed for the shell probe.
    plan.network = Network::default();
    let cancellation = tokio_util::sync::CancellationToken::new();
    let runtime_home = device.vm.data.join("msb");
    let result = tokio::time::timeout(
        Duration::from_secs(180),
        runtime::run_private_session_cancelled(
            &plan.boot(&device.vm, &runtime_home),
            &plan.process(),
            &plan.injection,
            &plan.capture,
            |chunk| {
                if cancel && chunk.windows(5).any(|part| part == b"ready") {
                    cancellation.cancel();
                }
            },
            cancellation.cancelled(),
        ),
    )
    .await
    .expect("private login probe timed out")
    .unwrap();
    if cancel {
        assert!(
            cancellation.is_cancelled(),
            "the private process never started"
        );
        assert!(
            result.ended.is_err(),
            "cancellation must interrupt the private process"
        );
    } else {
        let ended = result.ended.unwrap();
        assert_eq!(ended.code, 0);
        assert_eq!(ended.reply, b"ready\n");
    }
    assert_eq!(result.files.unwrap(), plan.injection);
    assert!(!device.vm.home.join(".claude/.credentials.json").exists());
    assert!(!device.vm.data.join("agent_environment").exists());
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_private_login_keeps_synthetic_credentials_in_memory() {
    private_login(false).await;
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_cancelled_private_login_discards_the_guest() {
    private_login(true).await;
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_snapshot_has_no_saved_login() {
    let device = Device::new();
    let home = gb_environment::guest_path(&device.vm.home).unwrap();
    let auth: Vec<_> = device
        .catalog()
        .iter()
        .map(|catalog| {
            let provision = &catalog["provision"];
            format!(
                "{home}/{}/{}",
                provision["state_root"].as_str().unwrap(),
                provision["auth"].as_str().unwrap(),
            )
        })
        .collect();
    let argv = vec![
        PYTHON.into(),
        "-I".into(),
        "-c".into(),
        "import json, pathlib, sys; assert not any(pathlib.Path(p).exists() for p in json.load(sys.stdin)); print('no login')".into(),
    ];
    let input = serde_json::to_vec(&auth).unwrap();
    let result = tokio::time::timeout(
        Duration::from_secs(180),
        runtime::run_private(
            &device.boot(&device.vm.data.join("msb"), &[], &Network::default()),
            &Process {
                argv: &argv,
                cwd: "/",
                env: &[],
                stdin: &input,
            },
        ),
    )
    .await
    .expect("snapshot inspection timed out")
    .unwrap();
    assert_eq!(result.code, 0);
    assert_eq!(result.reply, b"no login\n");
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_turns_share_the_commands_microvm() {
    use crate::host_window::{Context, HttpHost};
    use gb_environment::broker::{Broker, Invocation};

    let device = Device::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let recorder = tokio::spawn(async move {
        let app = Router::new().fallback(any(|| async { "test response" }));
        axum::serve(listener, app).await.unwrap();
    });
    let key = DeviceKey {
        machine_root: device.dir.path().into(),
        keychain: &FakeKeychain,
    };
    let mut network = Network::default();
    let mut mounts = vec![Mount {
        guest: "/work".into(),
        host: None,
        readonly: false,
    }];
    let mut loans = BTreeMap::new();
    for mut catalog in device.catalog() {
        let name = catalog["name"].as_str().unwrap().to_owned();
        if !matches!(name.as_str(), "antigravity" | "claude") {
            continue;
        }
        let files = credentials_for(&catalog);
        let broker = &mut catalog["provision"]["credential_broker"];
        let format = broker["format"].as_str().unwrap();
        credential_vault::seal(
            &key,
            &path(&device.vm.data, &name),
            &label(&name, format),
            &files,
        )
        .unwrap();
        broker["upstream"] = json!(origin);
        broker["routes"] = json!(["GET /test"]);
        broker["refresh"] = json!([]);
        let mut loan = ToolLogin::prepare(
            &device.vm.data,
            &device.vm.home,
            catalog,
            "en",
            device.vm.clone(),
        )
        .await
        .unwrap();
        Arc::get_mut(&mut loan).unwrap().keychain = Arc::new(FakeKeychain);
        mounts.push(Mount {
            guest: loan.guest_root.clone(),
            host: Some(gb_base::host_path::directory(loan.host_root.path(), false).unwrap()),
            readonly: true,
        });
        network.host_ports.push(loan.gateway.port);
        loans.insert(name, loan);
    }

    let invocation = Invocation {
        person_id: "tester".into(),
        run_id: "real-device".into(),
        trace_id: "real-device".into(),
        work_kind: "manual".into(),
        work_identity: "real-device".into(),
        cwd: device.dir.path().into(),
        access: gb_wire::CommandAccess {
            read_only: false,
            inspects: vec![],
        },
    };
    let host = HttpHost::start_context(Context {
        broker: Broker::new(PathBuf::new(), invocation.clone()),
        invocation,
        workspace: device.dir.path().into(),
        home: gb_environment::guest_path(&device.vm.home).unwrap(),
        mounts: BTreeMap::from([("/work".into(), true)]),
        turn_mounts: BTreeMap::new(),
        tools: loans.keys().cloned().collect(),
        summary_tools: Default::default(),
        loans,
        rate_limits: BTreeMap::new(),
        record: Arc::new(|_| {}),
    })
    .await
    .unwrap();
    network.host_ports.push(host.port);
    let argv = vec![
        PYTHON.into(),
        "-I".into(),
        "-c".into(),
        include_str!("shared_turns.py").into(),
    ];
    let input = serde_json::to_vec(&json!({"url": host.guest_url(), "token": host.token})).unwrap();
    let result = tokio::time::timeout(
        Duration::from_secs(180),
        runtime::run_private(
            &device.boot(&device.vm.data.join("msb"), &mounts, &network),
            &Process {
                argv: &argv,
                cwd: "/work",
                env: &[],
                stdin: &input,
            },
        ),
    )
    .await
    .expect("shared turn probe timed out")
    .unwrap();
    assert_eq!(result.code, 0, "{}", String::from_utf8_lossy(&result.reply));
    assert_eq!(result.reply, b"shared VM\n");
    recorder.abort();
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_codex_refreshes_an_expired_synthetic_login() {
    use axum::{Json, routing::post};

    let device = Device::new();
    let catalog = device
        .catalog()
        .into_iter()
        .find(|tool| tool["name"] == "codex")
        .unwrap();
    let fresh = credentials_for(&catalog);
    let fresh_token = access(&fresh, &catalog).unwrap();
    let expired = expire(fresh, &catalog).unwrap();
    assert!(expiring(&expired, &catalog));
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let recorder = {
        let token = fresh_token.clone();
        let calls = calls.clone();
        tokio::spawn(async move {
            let app = Router::new().route(
                "/oauth/token",
                post(move || async move {
                    calls.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    Json(json!({
                        "access_token": token, "id_token": token,
                        "refresh_token": "synthetic-new-refresh",
                    }))
                }),
            );
            axum::serve(listener, app).await.unwrap();
        })
    };
    let argv = catalog["provision"]["credential_broker"]["refresh"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| arg.as_str().unwrap().to_owned())
        .collect();
    let mut plan = private_plan(&device.vm, &catalog, expired, argv, false).unwrap();
    plan.network = Network {
        host_ports: vec![port],
        ..Network::default()
    };
    plan.env.push((
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE".into(),
        format!("http://host.microsandbox.internal:{port}/oauth/token"),
    ));
    let runtime_home = device.vm.data.join("msb");
    let result = tokio::time::timeout(
        Duration::from_secs(180),
        runtime::run_private_session(
            &plan.boot(&device.vm, &runtime_home),
            &plan.process(),
            &plan.injection,
            &plan.capture,
            |_| {},
        ),
    )
    .await
    .expect("Codex refresh probe timed out")
    .unwrap();
    let files = result.files.unwrap();
    let auth = catalog["provision"]["auth"].as_str().unwrap();
    let captured = Files::from([(auth.into(), files[&format!("{}/{auth}", plan.root)].clone())]);
    assert_eq!(calls.load(std::sync::atomic::Ordering::Relaxed), 1);
    assert_eq!(access(&captured, &catalog).unwrap(), fresh_token);
    assert!(!expiring(&captured, &catalog));
    recorder.abort();
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_copilot_reads_quota_from_its_synthetic_account() {
    use axum::Json;

    let device = Device::new();
    let catalog = device
        .catalog()
        .into_iter()
        .find(|tool| tool["name"] == "copilot")
        .unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let observed = Arc::new(Mutex::new(Vec::new()));
    let recorder = {
        let observed = observed.clone();
        tokio::spawn(async move {
            let app = Router::new().fallback(any(move |request: Request| {
                let observed = observed.clone();
                async move {
                    let path = request.uri().path();
                    let value = if path.starts_with("/copilot_internal/user") {
                        observed.lock().unwrap().push(
                            request
                                .headers()
                                .get("authorization")
                                .and_then(|value| value.to_str().ok())
                                .unwrap_or_default()
                                .to_owned(),
                        );
                        json!({
                            "login": "synthetic459", "id": 459, "copilot_plan": "individual",
                            "chat_enabled": true,
                            "endpoints": {"api": "https://api.individual.githubcopilot.com"},
                            "quota_snapshots": {"chat": {"unlimited": true}},
                        })
                    } else {
                        json!({})
                    };
                    Json(value)
                }
            }));
            axum::serve(listener, app).await.unwrap();
        })
    };
    let account = json!({"host":"https://github.com", "login":"synthetic459"});
    let auth = json!({
        "authTokens": {"https://github.com:synthetic459": {"token":"gho_R4"}},
        "lastLoggedInUser": account,
        "loggedInUsers": [account],
    });
    let files = Files::from([(
        "config.json".into(),
        format!("// This file is managed automatically.\n{auth}").into_bytes(),
    )]);
    let argv = vec![
        PYTHON.into(),
        "-B".into(),
        "-c".into(),
        include_str!("copilot_quota.py").into(),
    ];
    let mut plan = private_plan(&device.vm, &catalog, files, argv, false).unwrap();
    plan.network = Network {
        host_ports: vec![port],
        ..Network::default()
    };
    let base_url = format!("http://host.microsandbox.internal:{port}");
    plan.env.extend([
        ("COPILOT_DEBUG_GITHUB_API_URL".into(), base_url.clone()),
        ("COPILOT_API_URL".into(), base_url),
    ]);
    let runtime_home = device.vm.data.join("msb");
    let diagnostics = Mutex::new(Vec::new());
    let result = tokio::time::timeout(
        Duration::from_secs(180),
        runtime::run_private_session(
            &plan.boot(&device.vm, &runtime_home),
            &plan.process(),
            &plan.injection,
            &plan.capture,
            |chunk| diagnostics.lock().unwrap().extend_from_slice(chunk),
        ),
    )
    .await
    .expect("Copilot quota probe timed out")
    .unwrap();
    let ended = result.ended.unwrap();
    assert_eq!(
        ended.code,
        0,
        "Copilot quota probe failed: {}",
        String::from_utf8_lossy(&diagnostics.into_inner().unwrap())
    );
    assert_eq!(ended.reply, b"quota protocol responded\n");
    let observed = observed.lock().unwrap();
    assert!(!observed.is_empty());
    assert!(observed.iter().all(|value| value == "Bearer gho_R4"));
    recorder.abort();
}
