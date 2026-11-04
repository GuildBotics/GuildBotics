//! Per-tool sealed credential loans. Only a stand-in crosses into an agent VM.
use crate::host_window::{Gateway, TokenSource};
use gb_config::{
    credential_vault::{self, Files},
    secrets::{DeviceKey, SystemKeychain},
};
use gb_environment::gateway::{Lease, Policy, Provider};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::Arc,
};

struct Held {
    files: Files,
    _lock: gb_base::files::FileLock,
    _lease: Lease,
}
pub struct ToolLogin {
    pub name: String,
    pub guest_root: String,
    pub host_root: tempfile::TempDir,
    pub gateway: Gateway,
    data: PathBuf,
    catalog: Value,
    state: Arc<tokio::sync::Mutex<Option<Held>>>,
}
fn path(data: &Path, name: &str) -> PathBuf {
    data.join("agent_environment")
        .join(name)
        .join("login.sealed")
}
fn label(name: &str, format: &str) -> String {
    format!("{name}:default:{format}")
}
pub fn at<'a>(value: &'a Value, path: &Value) -> Option<&'a Value> {
    let mut current = value;
    for part in path.as_array()? {
        let key = part.as_str()?;
        current = if key == "*" {
            let object = current.as_object()?;
            if object.len() != 1 {
                return None;
            }
            object.values().next()?
        } else {
            current.get(key)?
        };
    }
    Some(current)
}
fn resolved_path(value: &Value, path: &Value) -> Result<Vec<String>, String> {
    let mut node = value;
    let mut result = vec![];
    for part in path.as_array().ok_or("Invalid credential field.")? {
        let mut key = part.as_str().ok_or("Invalid credential field.")?.to_owned();
        if key == "*" {
            let obj = node.as_object().ok_or("Ambiguous credential account.")?;
            if obj.len() != 1 {
                return Err("Ambiguous credential account.".into());
            }
            key = obj.keys().next().unwrap().clone();
        }
        result.push(key.clone());
        node = node.get(&key).unwrap_or(&Value::Null);
    }
    Ok(result)
}
fn put(value: &mut Value, path: &[String], found: Value) {
    if let Some((first, rest)) = path.split_first() {
        if !value.is_object() {
            *value = json!({});
        }
        if rest.is_empty() {
            value[first] = found;
        } else {
            if value.get(first).is_none() {
                value[first] = json!({});
            }
            put(&mut value[first], rest, found);
        }
    }
}
fn access(files: &Files, catalog: &Value) -> Result<String, String> {
    let auth = catalog["provision"]["auth"]
        .as_str()
        .ok_or("Credential auth file missing.")?;
    let document: Value = serde_json::from_slice(
        files
            .get(auth)
            .ok_or("Credentials missing. Log in again.")?,
    )
    .map_err(|_| "Credentials malformed.")?;
    at(
        &document,
        &catalog["provision"]["credential_broker"]["access_token"],
    )
    .and_then(Value::as_str)
    .filter(|s| !s.is_empty())
    .map(str::to_owned)
    .ok_or("Credentials contain no access token.".into())
}
fn stand_in(document: &Value, broker: &Value, token: &str) -> Result<(Value, String), String> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    let mut result = json!({});
    for path in broker["turn_fields"].as_array().into_iter().flatten() {
        if let Some(value) = at(document, path) {
            put(&mut result, &resolved_path(document, path)?, value.clone());
        }
    }
    let token = if broker["expires_format"] == "jwt" {
        let mut claims =
            json!({"exp":(chrono::Utc::now()+chrono::Duration::days(365)).timestamp()});
        if let Some(raw) = at(document, &broker["stand_in_claims_from"]).and_then(Value::as_str)
            && let Some(segment) = raw.split('.').nth(1)
            && let Ok(decoded) = URL_SAFE_NO_PAD.decode(segment)
            && let Ok(old) = serde_json::from_slice::<Value>(&decoded)
        {
            for path in broker["stand_in_claims"].as_array().into_iter().flatten() {
                if let Some(value) = at(&old, path) {
                    put(&mut claims, &resolved_path(&old, path)?, value.clone());
                }
            }
        }
        let token = format!(
            "{}.{}.{}",
            URL_SAFE_NO_PAD.encode(br#"{"alg":"none","typ":"JWT"}"#),
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(&claims).map_err(|_| "Invalid stand-in.")?),
            token
        );
        if broker["stand_in_claims_from"]
            .as_array()
            .is_some_and(|p| !p.is_empty())
        {
            put(
                &mut result,
                &resolved_path(document, &broker["stand_in_claims_from"])?,
                token.clone().into(),
            );
        }
        token
    } else {
        token.to_owned()
    };
    put(
        &mut result,
        &resolved_path(document, &broker["access_token"])?,
        token.clone().into(),
    );
    if broker["empty_refresh_token"] == true {
        put(
            &mut result,
            &resolved_path(document, &broker["refresh_token"])?,
            "".into(),
        );
    }
    if broker["expires_format"] != "jwt"
        && broker["expires_at"]
            .as_array()
            .is_some_and(|p| !p.is_empty())
    {
        let expiry = chrono::Utc::now() + chrono::Duration::days(365);
        let value = if broker["expires_format"] == "rfc3339" {
            json!(expiry.to_rfc3339())
        } else {
            json!(expiry.timestamp_millis())
        };
        put(
            &mut result,
            &resolved_path(document, &broker["expires_at"])?,
            value,
        );
    }
    Ok((result, token))
}
impl ToolLogin {
    pub async fn prepare(
        data: &Path,
        home: &Path,
        catalog: Value,
        vm: LoginVm,
    ) -> Result<Arc<Self>, String> {
        let name = catalog["name"]
            .as_str()
            .ok_or("Tool name missing.")?
            .to_owned();
        let broker = &catalog["provision"]["credential_broker"];
        let state = Arc::new(tokio::sync::Mutex::new(None::<Held>));
        let shared = state.clone();
        let source_catalog = catalog.clone();
        let source_data = data.to_path_buf();
        let source_name = name.clone();
        let source: TokenSource = Arc::new(move |refused| {
            let shared = shared.clone();
            let catalog = source_catalog.clone();
            let vm = vm.clone();
            let data = source_data.clone();
            let name = source_name.clone();
            Box::pin(async move {
                let held = shared.clone().lock_owned().await;
                let login = held.as_ref().ok_or(())?;
                let token = access(&login.files, &catalog).map_err(|_| ())?;
                if refused.as_deref() != Some(&token) && !expiring(&login.files, &catalog) {
                    return Ok(token);
                }
                private_tasks()
                    .spawn(async move {
                        let mut held = held;
                        let login = held.as_mut().ok_or(())?;
                        let broker = &catalog["provision"]["credential_broker"];
                        let argv = broker["refresh"]
                            .as_array()
                            .filter(|v| !v.is_empty())
                            .ok_or(())?
                            .iter()
                            .map(|v| v.as_str().map(str::to_owned).ok_or(()))
                            .collect::<Result<Vec<_>, _>>()?;
                        let expired = expire(login.files.clone(), &catalog).map_err(|_| ())?;
                        let handed = expired.clone();
                        let (_, files, _) = private_run(vm, catalog.clone(), expired, argv, false)
                            .await
                            .map_err(|_| ())?;
                        let token = access(&files, &catalog).map_err(|_| ())?;
                        if files == handed {
                            return Err(());
                        }
                        let keychain = SystemKeychain;
                        let device = DeviceKey {
                            machine_root: data.parent().unwrap_or(&data).into(),
                            keychain: &keychain,
                        };
                        credential_vault::seal(
                            &device,
                            &path(&data, &name),
                            &label(&name, broker["format"].as_str().ok_or(())?),
                            &files,
                        )
                        .map_err(|_| ())?;
                        login.files = files;
                        Ok(token)
                    })
                    .await
                    .map_err(|_| ())?
            })
        });
        let routes = broker["routes"]
            .as_array()
            .ok_or("Credential routes missing.")?
            .iter()
            .map(|route| {
                route
                    .as_str()
                    .and_then(|s| s.split_once(' '))
                    .map(|(m, p)| (m.into(), p.into()))
                    .ok_or("Invalid credential route.".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;
        let origin = broker["upstream"]
            .as_str()
            .ok_or("Credential upstream missing.")?
            .to_owned();
        // CLI OAuth gateways all authenticate the stand-in through Authorization.
        let policy = Policy {
            provider: Provider::OpenAi,
            origin,
            routes,
        };
        let gateway = if broker["tls"] == true {
            let mut names = vec!["host.microsandbox.internal".into()];
            names.extend(
                broker["relayed_hosts"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_owned),
            );
            Gateway::start_tls(policy, source, names).await
        } else {
            Gateway::start(policy, source).await
        }
        .map_err(|_| "The credential gateway cannot start.")?;
        gateway.revoke();
        let root = tempfile::tempdir().map_err(|_| "Cannot hold the tool's stand-in.")?;
        let guest_root = home
            .join(
                catalog["provision"]["state_root"]
                    .as_str()
                    .ok_or("Tool state root missing.")?,
            )
            .to_string_lossy()
            .into_owned();
        Ok(Arc::new(Self {
            name,
            guest_root,
            host_root: root,
            gateway,
            data: data.into(),
            catalog,
            state,
        }))
    }
    pub async fn begin(&self) -> Result<BTreeMap<String, String>, String> {
        let mut active = self.state.lock().await;
        if active.is_some() {
            return Err("The tool is already lent.".into());
        }
        let provision = &self.catalog["provision"];
        let broker = &provision["credential_broker"];
        let data = self.data.clone();
        let name = self.name.clone();
        let format = broker["format"].as_str().unwrap_or("").to_owned();
        let (held, files) = tokio::task::spawn_blocking(move || -> Result<_, String> {
            let path = path(&data, &name);
            let held = credential_vault::hold(&path, std::time::Duration::from_secs(30))
                .map_err(|_| "The login is busy.")?;
            let keychain = SystemKeychain;
            let device = DeviceKey {
                machine_root: data.parent().unwrap_or(&data).into(),
                keychain: &keychain,
            };
            let files = credential_vault::unseal(&device, &path, &label(&name, &format))
                .map_err(|_| "The sealed login cannot be opened. Log in again.")?
                .ok_or("Credentials missing. Log in again.")?;
            Ok((held, files))
        })
        .await
        .map_err(|_| "The sealed login cannot be read.")??;
        access(&files, &self.catalog)?;
        let auth = provision["auth"]
            .as_str()
            .ok_or("Tool auth file missing.")?;
        let document: Value =
            serde_json::from_slice(files.get(auth).ok_or("Credential file missing.")?)
                .map_err(|_| "Credential file malformed.")?;
        let random = format!(
            "{}{}",
            broker["stand_in_prefix"].as_str().unwrap_or(""),
            gb_environment::gateway::Leases::default().lend().stand_in
        );
        let (document, token) = stand_in(&document, broker, &random)?;
        let lease = self.gateway.lend(token.clone());
        let target = self.host_root.path().join(auth);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|_| "Cannot hold stand-in.")?;
        }
        gb_base::files::atomic_write(
            &target,
            &serde_json::to_vec(&document).map_err(|_| "Invalid stand-in.")?,
        )
        .map_err(|_| "Cannot hold stand-in.")?;
        let mut env = BTreeMap::new();
        if let Some(name) = provision["state_root_env"]
            .as_str()
            .filter(|v| !v.is_empty())
        {
            env.insert(name.into(), self.guest_root.clone());
        }
        if let Some(name) = provision["auth_env"].as_str().filter(|v| !v.is_empty()) {
            env.insert(name.into(), format!("{}/{auth}", self.guest_root));
        }
        for name in broker["base_url_env"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            env.insert(
                name.into(),
                format!(
                    "{}{}",
                    self.gateway.guest_url(),
                    broker["base_url_path"].as_str().unwrap_or("")
                ),
            );
        }
        for name in broker["stand_in_also_env"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .chain(broker["stand_in_env"].as_str().filter(|s| !s.is_empty()))
        {
            env.insert(name.into(), token.clone());
        }
        if !self.gateway.ca_pem.is_empty() {
            std::fs::write(self.host_root.path().join("ca.pem"), &self.gateway.ca_pem)
                .map_err(|_| "Cannot hold gateway CA.")?;
            for name in ["NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"] {
                env.insert(name.into(), format!("{}/ca.pem", self.guest_root));
            }
        }
        for pair in broker["turn_environment"].as_array().into_iter().flatten() {
            if let (Some(k), Some(v)) = (pair[0].as_str(), pair[1].as_str()) {
                env.insert(k.into(), v.into());
            }
        }
        if let Some(name) = broker["stand_in_command_env"]
            .as_str()
            .filter(|s| !s.is_empty())
        {
            let path = self.host_root.path().join("stand-in.json");
            gb_base::files::atomic_write(
                &path,
                &serde_json::to_vec(&json!({"access_token":token,"expires_in":31536000}))
                    .map_err(|_| "Invalid stand-in.")?,
            )
            .map_err(|_| "Cannot hold stand-in.")?;
            let command = format!(
                "cat '{}'",
                format!("{}/stand-in.json", self.guest_root).replace('\'', "'\"'\"'")
            );
            env.insert(name.into(), command);
        }
        *active = Some(Held {
            files,
            _lock: held,
            _lease: lease,
        });
        Ok(env)
    }
    pub async fn end(&self) {
        self.gateway.revoke();
        *self.state.lock().await = None;
    }
}
impl Drop for Held {
    fn drop(&mut self) {
        for bytes in self.files.values_mut() {
            bytes.fill(0);
        }
    }
}

impl ToolLogin {
    pub fn persisted_mounts(&self, read_only: bool) -> Result<Vec<gb_environment::Mount>, String> {
        let provision = &self.catalog["provision"];
        let state = provision["state_root"]
            .as_str()
            .ok_or("Tool state root missing.")?;
        let shared = self
            .data
            .join("agent_environment")
            .join(&self.name)
            .join(state);
        let store = if read_only {
            self.data
                .join("agent_environment")
                .join(&self.name)
                .join("read-only")
                .join(state)
        } else {
            shared.clone()
        };
        let mut mounts = vec![];
        for entry in provision["persisted"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            let name = entry.trim_end_matches('/');
            if name.is_empty()
                || !std::path::Path::new(name)
                    .components()
                    .all(|c| matches!(c, std::path::Component::Normal(_)))
            {
                return Err("Invalid persisted tool path.".into());
            }
            let path = store.join(name);
            if entry.ends_with('/') {
                gb_base::host_path::directory(&path, true)
                    .map_err(|_| "Invalid persisted tool directory.")?;
                std::fs::create_dir_all(&path)
                    .map_err(|_| "Cannot create persisted tool directory.")?;
                gb_base::host_path::directory(&path, false)
                    .map_err(|_| "Invalid persisted tool directory.")?;
            } else if read_only {
                let source = shared.join(name);
                if !persisted_file(&source)? {
                    continue;
                }
                let data =
                    std::fs::read(&source).map_err(|_| "Cannot read persisted tool settings.")?;
                gb_base::files::atomic_write(&path, &data)
                    .map_err(|_| "Cannot copy persisted tool settings.")?;
            } else if !persisted_file(&path)? {
                continue;
            }
            mounts.push(gb_environment::Mount {
                guest: format!("{}/{name}", self.guest_root),
                host: Some(path),
                readonly: false,
            });
        }
        Ok(mounts)
    }
}

fn persisted_file(path: &Path) -> Result<bool, String> {
    let parent = path.parent().ok_or("Invalid persisted tool file.")?;
    gb_base::host_path::directory(parent, true).map_err(|_| "Invalid persisted tool directory.")?;
    match std::fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_symlink() => {
            Err("A persisted tool file cannot be a link.".into())
        }
        Ok(meta) if meta.is_file() => Ok(true),
        Ok(_) => Err("A persisted tool file is not a regular file.".into()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err("Cannot inspect persisted tool file.".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stand_ins_copy_only_allowlisted_fields_and_never_refresh_tokens() {
        let original = json!({"account":{"id":"visible-id"},"credentials":{"access":"synthetic-private-access","refresh":"synthetic-private-refresh","expires":2},"unknown_future_secret":"synthetic-future"});
        let broker = json!({"access_token":["credentials","access"],"refresh_token":["credentials","refresh"],"expires_at":["credentials","expires"],"expires_format":"epoch_ms","empty_refresh_token":true,"turn_fields":[["account","id"]]});
        let (stub, token) = stand_in(&original, &broker, "synthetic-stand-in").unwrap();
        assert_eq!(token, "synthetic-stand-in");
        assert_eq!(stub["credentials"]["access"], token);
        assert_eq!(stub["credentials"]["refresh"], "");
        assert_eq!(stub["account"]["id"], "visible-id");
        assert!(
            stub["credentials"]["expires"].as_i64().unwrap()
                > chrono::Utc::now().timestamp_millis()
        );
        assert!(!stub.to_string().contains("private"));
        assert!(stub.get("unknown_future_secret").is_none());
    }
    #[test]
    fn wildcard_accounts_must_be_unambiguous() {
        assert_eq!(
            at(
                &json!({"accounts":{"one":{"token":"synthetic"}}}),
                &json!(["accounts", "*", "token"])
            ),
            Some(&json!("synthetic"))
        );
        assert!(
            at(
                &json!({"accounts":{"one":{},"two":{}}}),
                &json!(["accounts", "*", "token"])
            )
            .is_none()
        );
    }
}

#[derive(Clone)]
pub struct LoginVm {
    pub data: PathBuf,
    pub home: PathBuf,
    pub package: PathBuf,
    pub snapshot: PathBuf,
    pub memory_mib: u32,
    pub cpus: u8,
    pub nameservers: Vec<String>,
}
fn private_tasks() -> &'static tokio_util::task::TaskTracker {
    static TASKS: std::sync::OnceLock<tokio_util::task::TaskTracker> = std::sync::OnceLock::new();
    TASKS.get_or_init(tokio_util::task::TaskTracker::new)
}
/// Shutdown waits for token capture and resealing, even if the HTTP caller
/// went away while the login CLI consumed a one-use refresh token.
pub async fn drain_private() {
    private_tasks().close();
    private_tasks().wait().await;
}
async fn private_run(
    vm: LoginVm,
    catalog: Value,
    files: Files,
    argv: Vec<String>,
    show: bool,
) -> Result<(i32, Files, Vec<u8>), String> {
    let provision = &catalog["provision"];
    let root = vm.home.join(
        provision["state_root"]
            .as_str()
            .ok_or("Tool state root missing.")?,
    );
    let home = vm.home.to_string_lossy().into_owned();
    let mut env = vec![
        ("HOME".into(), home.clone()),
        ("AGNO_TELEMETRY".into(), "false".into()),
        ("PYTHONPATH".into(), gb_environment::CODE_ROOT.into()),
    ];
    if let Some(name) = provision["state_root_env"]
        .as_str()
        .filter(|s| !s.is_empty())
    {
        env.push((name.into(), root.to_string_lossy().into_owned()));
    }
    if let Some(name) = provision["auth_env"].as_str().filter(|s| !s.is_empty()) {
        env.push((
            name.into(),
            root.join(provision["auth"].as_str().unwrap_or(""))
                .to_string_lossy()
                .into_owned(),
        ));
    }
    let mounts = vec![
        gb_environment::Mount {
            guest: home.clone(),
            host: None,
            readonly: false,
        },
        gb_environment::Mount {
            guest: gb_environment::CODE_ROOT.into(),
            host: Some(vm.package.clone()),
            readonly: true,
        },
    ];
    let network = gb_environment::runtime::Network {
        unrestricted: true,
        domains: vec![],
        host_ports: vec![],
        local_network: false,
        nameservers: vm.nameservers.clone(),
    };
    let injection = files
        .into_iter()
        .map(|(name, value)| (root.join(name).to_string_lossy().into_owned(), value))
        .collect();
    let capture = vec![
        root.join(
            provision["auth"]
                .as_str()
                .ok_or("Tool auth file missing.")?,
        )
        .to_string_lossy()
        .into_owned(),
    ];
    let boot = gb_environment::runtime::Boot {
        home: &vm.data.join("msb"),
        snapshot: &vm.snapshot,
        memory_mib: vm.memory_mib,
        cpus: vm.cpus,
        mounts: &mounts,
        network: &network,
        worktree: None,
    };
    let process = gb_environment::runtime::Process {
        argv: &argv,
        cwd: &home,
        env: &env,
        stdin: b"",
    };
    let output = move |chunk: &[u8]| {
        if show {
            use std::io::Write;
            let _ = std::io::stdout().write_all(chunk);
            let _ = std::io::stdout().flush();
        }
    };
    let result = if show {
        gb_environment::runtime::run_private_login(&boot, &process, &injection, &capture, output)
            .await
    } else {
        gb_environment::runtime::run_private_session(&boot, &process, &injection, &capture, output)
            .await
    }
    .map_err(|_| "The private credential environment could not start.")?;
    let kept = result
        .files
        .map_err(|_| "The private credential environment could not return its login.")?
        .into_iter()
        .filter_map(|(path, value)| {
            Path::new(&path)
                .strip_prefix(&root)
                .ok()
                .map(|relative| (relative.to_string_lossy().into_owned(), value))
        })
        .collect();
    match result.ended {
        Ok(ended) => Ok((ended.code, kept, ended.reply)),
        Err(_) => Ok((-1, kept, vec![])),
    }
}

pub async fn login(
    host: &gb_environment::Host,
    workspace: &Path,
    tool: &str,
    usage: bool,
) -> Result<Value, String> {
    let catalog: Value = serde_json::from_slice(
        &std::fs::read(
            host.package
                .join("templates/environment/cli_provision.json"),
        )
        .map_err(|_| "Tool catalog unavailable.")?,
    )
    .map_err(|_| "Tool catalog invalid.")?;
    let catalog = catalog
        .as_array()
        .into_iter()
        .flatten()
        .find(|entry| entry["name"] == tool)
        .cloned()
        .ok_or("Unknown provisioned tool.")?;
    let status = gb_environment::device::read(host, workspace).await;
    if let Some(problem) = status.refusal {
        return Err(crate::commands::say(problem, "en"));
    }
    let vm = LoginVm {
        data: host.data.clone(),
        home: host.home.clone(),
        package: host.package.clone(),
        snapshot: status
            .snapshot
            .ok_or("Build the agent environment first.")?,
        memory_mib: status.resources.memory_mib,
        cpus: status.resources.cpus,
        nameservers: status.view["dns"]["nameservers"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect(),
    };
    let tool = tool.to_owned();
    let task = private_tasks().spawn(async move {
        let path = path(&vm.data, &tool);
        let label = label(
            &tool,
            catalog["provision"]["credential_broker"]["format"]
                .as_str()
                .ok_or("Tool has no credential broker.")?,
        );
        let _held = credential_vault::hold(&path, std::time::Duration::from_secs(30))
            .map_err(|_| "The login is busy.")?;
        let keychain = SystemKeychain;
        let device = DeviceKey {
            machine_root: vm.data.parent().unwrap_or(&vm.data).into(),
            keychain: &keychain,
        };
        let files = credential_vault::unseal(&device, &path, &label)
            .map_err(|_| "The login cannot be opened.")?
            .unwrap_or_default();
        if usage {
            access(&files, &catalog)?;
        }
        let argv = if usage {
            vec![
                "/opt/guildbotics/venv/bin/python".into(),
                "-B".into(),
                "-m".into(),
                "guildbotics.guest.usage_probe".into(),
                tool.clone(),
            ]
        } else {
            catalog["provision"]["login"]
                .as_array()
                .ok_or("Login command missing.")?
                .iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .ok_or("Invalid login command.")
                })
                .collect::<Result<Vec<_>, _>>()?
        };
        let (code, files, reply) =
            private_run(vm.clone(), catalog.clone(), files, argv, !usage).await?;
        // Capture and save precede the process outcome: refresh tokens may have
        // rotated even when the provider subsequently failed or timed out.
        if access(&files, &catalog).is_ok() {
            credential_vault::seal(&device, &path, &label, &files)
                .map_err(|_| "The refreshed login could not be sealed.")?;
        }
        if code != 0 {
            return Err("The provider login or usage probe failed.".to_owned());
        }
        if usage {
            serde_json::from_slice(&reply)
                .map_err(|_| "The usage probe did not return JSON.".into())
        } else {
            access(&files, &catalog)?;
            Ok(json!({"tool":tool,"saved":true}))
        }
    });
    task.await
        .map_err(|_| "The private credential task failed.".to_owned())?
}

fn expiry(document: &Value, broker: &Value) -> Option<f64> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    let raw = at(document, &broker["expires_at"])?;
    match broker["expires_format"].as_str() {
        Some("rfc3339") => chrono::DateTime::parse_from_rfc3339(raw.as_str()?)
            .ok()
            .map(|d| d.timestamp() as f64),
        Some("jwt") => {
            let segment = raw.as_str()?.split('.').nth(1)?;
            let bytes = URL_SAFE_NO_PAD.decode(segment).ok()?;
            serde_json::from_slice::<Value>(&bytes).ok()?["exp"].as_f64()
        }
        _ => raw.as_f64().map(|ms| ms / 1000.0),
    }
}
fn expiring(files: &Files, catalog: &Value) -> bool {
    let broker = &catalog["provision"]["credential_broker"];
    if broker["expires_at"].as_array().is_none_or(|v| v.is_empty()) {
        return false;
    }
    let auth = catalog["provision"]["auth"].as_str().unwrap_or("");
    files
        .get(auth)
        .and_then(|raw| serde_json::from_slice::<Value>(raw).ok())
        .and_then(|document| expiry(&document, broker))
        .is_none_or(|seconds| seconds < chrono::Utc::now().timestamp() as f64 + 300.0)
}
fn expire(mut files: Files, catalog: &Value) -> Result<Files, String> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    let broker = &catalog["provision"]["credential_broker"];
    let auth = catalog["provision"]["auth"]
        .as_str()
        .ok_or("Auth file missing.")?;
    let mut document: Value =
        serde_json::from_slice(files.get(auth).ok_or("Credentials missing.")?)
            .map_err(|_| "Credentials malformed.")?;
    let path = resolved_path(&document, &broker["expires_at"])?;
    let value = match broker["expires_format"].as_str() {
        Some("rfc3339") => json!("1970-01-01T00:00:00Z"),
        Some("jwt") => {
            let original = at(&document, &broker["expires_at"])
                .and_then(Value::as_str)
                .ok_or("JWT missing.")?;
            let parts = original.split('.').collect::<Vec<_>>();
            if parts.len() != 3 {
                return Err("JWT malformed.".into());
            }
            let bytes = URL_SAFE_NO_PAD
                .decode(parts[1])
                .map_err(|_| "JWT malformed.")?;
            let mut claims: Value = serde_json::from_slice(&bytes).map_err(|_| "JWT malformed.")?;
            claims["exp"] = 0.into();
            json!(format!(
                "{}.{}.{}",
                parts[0],
                URL_SAFE_NO_PAD.encode(serde_json::to_vec(&claims).map_err(|_| "JWT malformed.")?),
                parts[2]
            ))
        }
        _ => json!(0),
    };
    put(&mut document, &path, value);
    files.insert(
        auth.into(),
        serde_json::to_vec(&document).map_err(|_| "Credentials malformed.")?,
    );
    Ok(files)
}
impl ToolLogin {
    pub fn relayed_hosts(&self) -> Vec<String> {
        self.catalog["provision"]["credential_broker"]["relayed_hosts"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect()
    }
    pub fn turn_domains(&self) -> Vec<String> {
        self.catalog["provision"]["credential_broker"]["turn_domains"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect()
    }
}

impl ToolLogin {
    pub fn authentication_outcome(&self, failed: bool) -> Result<(), String> {
        let marker = self
            .data
            .join("agent_environment")
            .join(&self.name)
            .join("authentication-failed");
        if failed {
            gb_base::files::atomic_write(&marker, b"authentication failed\n")
                .map_err(|_| "Cannot record authentication outcome.".into())
        } else {
            match std::fs::remove_file(marker) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(_) => Err("Cannot record authentication outcome.".into()),
            }
        }
    }
}
