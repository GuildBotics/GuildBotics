//! A real VM, synthetic login, and loopback-only upstream.

#[path = "real_device/provider_contracts.rs"]
mod provider_contracts;

mod device_disk {
    include!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../gb-environment/tests/support/device_disk.rs"
    ));
}

use super::*;
use axum::{Router, extract::Request, routing::any};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use device_disk::{OutputMarker, assert_private_disk, disk_matches, sandboxes};
use gb_environment::{
    Mount,
    runtime::{self, Boot, Network, Process},
};
use std::{fs, sync::Mutex, time::Duration};
use tracing::instrument::WithSubscriber;

const CANARY: &str = "synthetic-device-login-never-in-turn";
const DISK_CANARY: &str = "guildbotics-real-device-positive-control";
const PYTHON: &str = "/opt/guildbotics/venv/bin/python";
const COMMAND_NETWORK: &str = "network: {mode: deny}\ndns: {nameservers: [127.0.0.1]}\n";

#[test]
fn synthetic_commands_use_a_loopback_network_declaration() {
    let config = tempfile::tempdir().unwrap();
    let path = config.path().join("intelligences/agent_environment.yml");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, COMMAND_NETWORK).unwrap();
    let network = gb_environment::contract::network(config.path(), config.path(), false).unwrap();
    assert!(!network.unrestricted && !network.local_network && network.domains.is_empty());
    assert_eq!(network.nameservers, ["127.0.0.1"]);
}

#[derive(Clone, Default)]
struct CommandLog(Arc<Mutex<String>>);

impl tracing::Subscriber for CommandLog {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        struct Fields<'a>(&'a mut String);
        impl tracing::field::Visit for Fields<'_> {
            fn record_debug(&mut self, _: &tracing::field::Field, value: &dyn std::fmt::Debug) {
                use std::fmt::Write;
                writeln!(self.0, "{value:?}").unwrap();
            }
        }
        event.record(&mut Fields(&mut self.0.lock().unwrap()));
    }
}

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
    fn runtime_home(&self) -> PathBuf {
        self.vm.data.join("msb")
    }

    async fn command(
        &self,
        catalog: Vec<Value>,
        port: Option<u16>,
        source: &str,
        pipe: Value,
        checkout: Option<&Path>,
    ) -> String {
        let workspace = self.dir.path().join("workspace");
        let config = workspace.join(".guildbotics/config");
        let identity = self.dir.path().join("identity");
        let person = "probe";
        let put = |path: PathBuf, text: &str| {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, text).unwrap();
        };
        put(
            config.join("team/project.yml"),
            "name: Probe\nlanguage: en\n",
        );
        put(
            config.join("intelligences/agent_environment.yml"),
            COMMAND_NETWORK,
        );
        put(
            config.join("team/members/probe/member.yml"),
            "is_active: true\nprofile: {roles: {}}\n",
        );
        put(
            identity.join(".guildbotics/config/team/members/probe/identity.yml"),
            "person_id: probe\nname: Probe\nperson_type: agent\n",
        );
        let mut brains = String::new();
        let mut slots = String::new();
        for tool in &catalog {
            let name = tool["name"].as_str().unwrap();
            brains.push_str(&format!("{name}: {{kind: ai_cli, cli_agent: {name}}}\n"));
            slots.push_str(&format!("{name}: cli_agents/{name}/default.yml\n"));
            let provision = &tool["provision"];
            let root = self
                .vm
                .data
                .join("agent_environment")
                .join(name)
                .join(provision["state_root"].as_str().unwrap());
            for entry in provision["persisted"].as_array().unwrap() {
                let entry = entry.as_str().unwrap();
                if !entry.ends_with('/') {
                    put(root.join(entry), "{}\n");
                }
            }
        }
        put(
            config.join("intelligences/brain_mapping.yml"),
            if brains.is_empty() { "{}\n" } else { &brains },
        );
        put(
            config.join("intelligences/cli_agent_mapping.yml"),
            if slots.is_empty() { "{}\n" } else { &slots },
        );
        let snapshots = gb_environment::snapshot::snapshots_dir(&workspace);
        fs::create_dir_all(snapshots.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink(self.vm.snapshot.parent().unwrap(), &snapshots).unwrap();
        let cwd = if let Some(checkout) = checkout {
            checkout.to_owned()
        } else {
            let work = workspace.join("work");
            fs::create_dir_all(work.join("package")).unwrap();
            work
        };
        let path = config.join("commands/probe.py");
        put(
            path.clone(),
            &format!(
                "{source}\nimport sys as _device_sys\nprint('guildbotics-real-device-' + 'positive-control', file=_device_sys.stderr, flush=True)\n",
            ),
        );
        let host = gb_environment::Host {
            home: self.vm.home.clone(),
            package: checkout
                .map_or_else(|| self.vm.package.clone(), |path| path.join("guildbotics")),
            data: self.vm.data.clone(),
            identity_root: identity,
        };
        let roots = gb_config::commands::CommandRoots {
            config,
            templates: host.package.join("templates"),
        };
        let command = gb_environment::Command {
            invocation: None,
            wants_result: false,
            person_id: person.into(),
            run_id: "real-device".into(),
            trace_id: "real-device".into(),
            work_kind: "manual".into(),
            work_identity: "real-device".into(),
            path,
            name: "probe".into(),
            access: gb_wire::CommandAccess {
                read_only: false,
                inspects: vec![],
            },
            args: vec![],
            cwd,
            pipe: pipe.to_string(),
        };
        let device = gb_environment::device::read(&host, &workspace).await;
        assert!(device.refusal.is_none(), "{:?}", device.refusal);
        let test = crate::host_window::CommandTestServices {
            catalog: json!(catalog),
            keychain: Arc::new(FakeKeychain),
            recorder_port: port,
        };
        let services = crate::host_window::prepare_command(
            &host,
            &workspace,
            &command,
            &roots,
            &device,
            None,
            Some(&test),
        )
        .await
        .unwrap();
        let mounts = gb_environment::shape(&host, &workspace, &command, None)
            .unwrap()
            .mounts;
        let log = CommandLog::default();
        let reply = tokio::time::timeout(
            Duration::from_secs(180),
            gb_environment::run(&host, &workspace, command, |_| {}, &services.session),
        )
        .with_subscriber(log.clone())
        .await
        .expect("command probe timed out")
        .unwrap();
        services.settle().await;
        let log = log.0.lock().unwrap();
        assert!(reply.failure.is_none(), "{:?}\n{log}", reply.failure);
        assert!(log.contains(DISK_CANARY));
        assert!(!log.contains(CANARY), "a login reached the host log");
        for source in services
            .session
            .mounts
            .iter()
            .filter_map(|mount| mount.host.as_ref())
        {
            assert!(
                disk_matches(source, CANARY).is_empty(),
                "a login reached a host mount"
            );
        }
        for source in mounts
            .iter()
            .filter_map(|mount| mount.host.as_ref())
            .filter(|source| source.starts_with(&host.home))
        {
            assert!(
                disk_matches(source, CANARY).is_empty(),
                "a login reached a guest HOME mount"
            );
        }
        reply.text_output.trim().to_owned()
    }

    fn seal(&self, catalog: &Value, files: &Files) {
        let name = catalog["name"].as_str().unwrap();
        let format = catalog["provision"]["credential_broker"]["format"]
            .as_str()
            .unwrap();
        let key = DeviceKey {
            machine_root: self.dir.path().into(),
            keychain: &FakeKeychain,
        };
        credential_vault::seal(
            &key,
            &path(&self.vm.data, name),
            &label(name, format),
            files,
        )
        .unwrap();
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

fn live_vm(runtime_home: &Path) -> String {
    let live = sandboxes(runtime_home);
    assert_eq!(live.len(), 1, "expected one command VM");
    live[0].file_name().unwrap().to_string_lossy().into_owned()
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_checkout_and_code_mount_are_distinct() {
    use std::process::{Command, Stdio};

    let device = Device::new();
    let checkout = device.dir.path().join("checkout");
    fs::create_dir(&checkout).unwrap();
    // Copy the checked-in package without bringing build output, credentials, or local files.
    let mut archive = Command::new("git")
        .current_dir(device.vm.package.parent().unwrap())
        .args(["archive", "HEAD", "guildbotics"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let unpacked = Command::new("tar")
        .current_dir(&checkout)
        .args(["-xf", "-"])
        .stdin(archive.stdout.take().unwrap())
        .status()
        .unwrap();
    assert!(archive.wait().unwrap().success());
    assert!(unpacked.success());
    let marker = "real-device-overlap-probe";
    let path = checkout.join("guildbotics").join(marker);
    fs::write(&path, "before").unwrap();
    for args in [["init", "-q", "--template="], ["add", "--", "guildbotics"]] {
        assert!(
            Command::new("git")
                .current_dir(&checkout)
                .args(args)
                .status()
                .unwrap()
                .success()
        );
    }
    let output = device
        .command(
            vec![],
            None,
            r#"
import errno
import json
from pathlib import Path

def main(context):
    package = Path("/opt/guildbotics/code/guildbotics")
    name = json.loads(context.pipe)
    target = Path("guildbotics") / name
    code = package / name
    assert target.read_text() == "before", "the writable copy lost the checkout marker"
    assert code.read_text() == "before", "the code mount lost the same checkout marker"
    target.write_text("changed in the VM")
    assert code.read_text() == "before", "the write to the copy changed the code mount"
    try:
        with code.open("a"):
            pass
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM), str(error)
    else:
        raise AssertionError("the same checkout is writable through the code mount")
    return "writable checkout"
"#,
            json!(marker),
            Some(&checkout),
        )
        .await;
    assert_eq!(output, "writable checkout");
    assert_eq!(fs::read_to_string(path).unwrap(), "changed in the VM");
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_provider_turn_keeps_the_login_on_the_host() {
    let device = Device::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let origin = format!("http://127.0.0.1:{port}");
    let seen = Arc::new(Mutex::new(Vec::new()));
    let runtime_home = device.runtime_home();
    let recorder = {
        let seen = seen.clone();
        tokio::spawn(async move {
            let app = Router::new()
                .route(
                    "/vm",
                    axum::routing::get(move || async move { live_vm(&runtime_home) }),
                )
                .fallback(any(move |request: Request| {
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
    let mut tools = device.catalog();
    let mut probes = Vec::new();
    let mut expected = Vec::new();
    for catalog in &mut tools {
        let files = credentials_for(catalog);
        let actual = access(&files, catalog).unwrap();
        device.seal(catalog, &files);
        let name = catalog["name"].as_str().unwrap().to_owned();
        let provision = &mut catalog["provision"];
        let auth = provision["auth"].clone();
        let state_root = provision["state_root"].clone();
        let persisted = provision["persisted"].clone();
        let broker = &mut provision["credential_broker"];
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
        broker["refresh"] = json!([]);
        broker["turn_domains"] = json!([]);
        probes.push(json!({
            "name": name, "auth": auth, "state_root": state_root, "persisted": persisted,
            "base_env": broker["base_url_env"][0], "base_suffix": broker["base_url_path"],
            "access_path": broker["access_token"], "stand_in_env": broker["stand_in_env"],
            "stand_in_command": broker["stand_in_command_env"], "routes": routes,
        }));
    }
    let output = device
        .command(
            tools,
            Some(port),
            include_str!("gateway_probe.py"),
            json!({"probes":probes,"vm":format!("http://host.microsandbox.internal:{port}/vm")}),
            None,
        )
        .await;
    assert_eq!(output, "gateway-probe-ok");
    assert!(
        sandboxes(&device.runtime_home()).is_empty(),
        "the command VM survived"
    );
    let marker = device.dir.path().join("disk-scan-canary");
    fs::write(&marker, DISK_CANARY).unwrap();
    assert_eq!(disk_matches(device.dir.path(), DISK_CANARY), [marker]);
    assert!(
        disk_matches(device.dir.path(), CANARY).is_empty(),
        "a login reached host storage"
    );
    let mut observed = seen.lock().unwrap().clone();
    observed.sort();
    expected.sort();
    assert_eq!(observed, expected);
    recorder.abort();
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_private_login_keeps_synthetic_credentials_in_memory() {
    let device = Device::new();
    let runtime_home = device.runtime_home();
    for catalog in device.catalog() {
        let provision = &catalog["provision"];
        let refresh = provision["credential_broker"]["refresh"]
            .as_array()
            .unwrap();
        let mut plan = private_plan(
            &device.vm,
            &catalog,
            credentials_for(&catalog),
            vec![],
            false,
        )
        .unwrap();
        plan.network = Network::default();
        plan.argv =
            vec![
            "sh".into(), "-ec".into(),
            concat!(
                "test -s \"$1\"; shift; test \"$(stat -f -c %T \"$HOME\")\" = tmpfs; ",
                "command -v \"$1\" >/dev/null; timeout 30 \"$@\" </dev/null >/dev/null 2>&1 || :; ",
                "printf %s \"$GB_DISK_CANARY\" > /var/tmp/real-device-positive-control; ",
                "sync; printf 'scan-ready\n'",
            ).into(),
            "probe".into(),
            format!("{}/{}", plan.root, provision["auth"].as_str().unwrap()),
        ];
        plan.env.push(("GB_DISK_CANARY".into(), DISK_CANARY.into()));
        if refresh.is_empty() {
            assert_eq!(catalog["name"], "copilot");
            plan.argv.extend(
                ["copilot", "--no-auto-update", "-p", "Synthetic fixture."].map(str::to_owned),
            );
        } else {
            plan.argv
                .extend(refresh.iter().map(|arg| arg.as_str().unwrap().to_owned()));
        }
        let ready = OutputMarker::new(b"scan-ready\n");
        let scanned = Mutex::new(None);
        let result = runtime::run_private_session(
            &plan.boot(&device.vm, &runtime_home),
            &plan.process(),
            &plan.injection,
            &plan.capture,
            |chunk| {
                if ready.observed(chunk) {
                    *scanned.lock().unwrap() = Some(std::panic::catch_unwind(|| {
                        assert_private_disk(&runtime_home, DISK_CANARY, CANARY);
                    }));
                }
            },
        )
        .await
        .unwrap();
        let ended = result.ended.unwrap();
        assert_eq!(ended.code, 0, "{}", catalog["name"]);
        assert_eq!(ended.reply, b"scan-ready\n");
        if let Err(failure) = scanned
            .into_inner()
            .unwrap()
            .expect("the live VM was not scanned")
        {
            std::panic::resume_unwind(failure);
        }
        assert!(sandboxes(&runtime_home).is_empty(), "the login VM survived");
        assert!(disk_matches(&device.vm.data, CANARY).is_empty());
    }
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_cancelled_private_login_discards_the_guest() {
    use gb_environment::{Failure, Sentence};

    let device = Device::new();
    let catalog = device
        .catalog()
        .into_iter()
        .find(|tool| tool["name"] == "claude")
        .unwrap();
    let mut plan = private_plan(
        &device.vm,
        &catalog,
        credentials_for(&catalog),
        vec![
            "sh".into(),
            "-ec".into(),
            "test -s \"$CLAUDE_CONFIG_DIR/.credentials.json\"; printf 'ready\n'; sleep 300".into(),
        ],
        false,
    )
    .unwrap();
    plan.network = Network::default();
    let runtime_home = device.runtime_home();
    for expected in ["cancelled", "timed out"] {
        let ready = OutputMarker::new(b"ready\n");
        let cancellation = tokio_util::sync::CancellationToken::new();
        let live = Mutex::new(Vec::new());
        let result = tokio::time::timeout(
            Duration::from_secs(180),
            runtime::run_private_session_cancelled(
                &plan.boot(&device.vm, &runtime_home),
                &plan.process(),
                &plan.injection,
                &plan.capture,
                |chunk| {
                    if ready.observed(chunk) {
                        *live.lock().unwrap() = sandboxes(&runtime_home);
                        if expected == "cancelled" {
                            cancellation.cancel();
                        }
                    }
                },
                cancellation.cancelled(),
            ),
        )
        .await
        .expect("the private session did not return")
        .unwrap();
        assert!(
            matches!(
                result.ended,
                Err(Failure::Runtime(Sentence::Text(ref message)))
                    if *message == format!("Private provider process {expected}")
            ),
            "expected {expected}"
        );
        assert_eq!(live.into_inner().unwrap().len(), 1);
        assert!(
            sandboxes(&runtime_home).is_empty(),
            "the login VM survived {expected}"
        );
        assert_eq!(result.files.unwrap(), plan.injection);
        assert!(disk_matches(&device.vm.data, CANARY).is_empty());
    }
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_snapshot_has_no_saved_login() {
    let device = Device::new();
    // The descriptor is the positive control inside the same artifact.
    assert_eq!(
        disk_matches(&device.vm.snapshot, "\"artifact\":\"snapshot\""),
        [device.vm.snapshot.join("snapshot.json")]
    );
    for mark in [CANARY, "SYNTH459SECRET"] {
        assert!(disk_matches(&device.vm.snapshot, mark).is_empty());
    }
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
    assert!(sandboxes(&device.runtime_home()).is_empty());
}

#[tokio::test]
#[ignore = "real_device: owner approval, disposable snapshot and loopback recorder"]
async fn real_device_turns_share_the_commands_microvm() {
    let device = Device::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let runtime_home = device.runtime_home();
    let recorder = tokio::spawn(async move {
        let app = Router::new()
            .route(
                "/vm",
                axum::routing::get(move || async move { live_vm(&runtime_home) }),
            )
            .fallback(any(|| async { "test response" }));
        axum::serve(listener, app).await.unwrap();
    });
    let mut tools = device
        .catalog()
        .into_iter()
        .filter(|catalog| matches!(catalog["name"].as_str(), Some("antigravity" | "claude")))
        .collect::<Vec<_>>();
    for catalog in &mut tools {
        device.seal(catalog, &credentials_for(catalog));
        let broker = &mut catalog["provision"]["credential_broker"];
        broker["upstream"] = json!(format!("http://127.0.0.1:{port}"));
        broker["routes"] = json!(["GET /test"]);
        broker["refresh"] = json!([]);
        broker["turn_domains"] = json!([]);
    }
    let output = device
        .command(
            tools,
            Some(port),
            include_str!("shared_turns.py"),
            json!(format!("http://host.microsandbox.internal:{port}/vm")),
            None,
        )
        .await;
    assert_eq!(output, "shared VM");
    assert!(
        sandboxes(&device.runtime_home()).is_empty(),
        "the command VM survived"
    );
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
