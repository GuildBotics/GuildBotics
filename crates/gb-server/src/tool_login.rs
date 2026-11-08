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
    refreshed: bool,
    refusal: Option<&'static str>,
    _lease: LoanLease,
}
#[derive(Clone)]
struct LoanLease(Arc<std::sync::Mutex<Option<Lease>>>);
impl LoanLease {
    fn new(lease: Lease) -> Self {
        Self(Arc::new(std::sync::Mutex::new(Some(lease))))
    }
    fn revoke(&self) {
        self.0.lock().expect("lease mutex poisoned").take();
    }
    fn active(&self) -> bool {
        self.0.lock().unwrap().as_ref().is_some_and(Lease::active)
    }
}
struct BeginGuard {
    state: Arc<tokio::sync::Mutex<Option<Held>>>,
    lease: Option<LoanLease>,
}
impl Drop for BeginGuard {
    fn drop(&mut self) {
        if let Some(lease) = self.lease.take() {
            lease.revoke();
            let state = self.state.clone();
            spawn_private(private_tasks(), async move {
                let mut held = state.lock().await;
                if held
                    .as_ref()
                    .is_some_and(|held| Arc::ptr_eq(&held._lease.0, &lease.0))
                {
                    held.take();
                }
            });
        }
    }
}
pub struct ToolLogin {
    pub name: String,
    pub guest_root: String,
    pub host_root: tempfile::TempDir,
    pub gateway: Gateway,
    source: TokenSource,
    keychain: Arc<dyn gb_config::secrets::Keychain>,
    data: PathBuf,
    catalog: Value,
    language: String,
    command: String,
    state: Arc<tokio::sync::Mutex<Option<Held>>>,
}

fn login_problem(catalog: &Value, key: &str, command: &str, language: &str) -> String {
    crate::i18n::t(
        language,
        &format!("intelligences.agent_environment.tool.{key}"),
        &[
            (
                "tool",
                catalog["label"]
                    .as_str()
                    .or_else(|| catalog["name"].as_str())
                    .unwrap_or("tool"),
            ),
            ("command", command),
        ],
    )
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
fn credential_document(raw: &[u8]) -> Result<Value, String> {
    let raw = std::str::from_utf8(raw).map_err(|_| "Credentials malformed.")?;
    serde_json::from_str(
        &raw.lines()
            .filter(|line| !line.trim_start().starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .map_err(|_| "Credentials malformed.".into())
}
pub(crate) fn valid(files: &Files, catalog: &Value) -> bool {
    access(files, catalog).is_ok()
}
fn access(files: &Files, catalog: &Value) -> Result<String, String> {
    let auth = catalog["provision"]["auth"]
        .as_str()
        .ok_or("Credential auth file missing.")?;
    let document = credential_document(
        files
            .get(auth)
            .ok_or("Credentials missing. Log in again.")?,
    )?;
    let broker = &catalog["provision"]["credential_broker"];
    if broker["refresh_token"]
        .as_array()
        .is_some_and(|path| !path.is_empty())
        && at(&document, &broker["refresh_token"])
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        return Err("Credentials contain no refresh token.".into());
    }
    if broker["expires_at"]
        .as_array()
        .is_some_and(|path| !path.is_empty())
        && expiry(&document, broker).is_none()
    {
        return Err("Credentials contain no valid expiry.".into());
    }
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
/// A turn reads these files through the already-mounted tool directory.
/// Replacing an inode would leave a recently opened guest path pointing at
/// the unlinked file until the mount's cache expires.
fn write_turn_file(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt};
    use cap_std::fs::OpenOptions;
    use std::io::Write;

    let directory =
        gb_base::host_path::open_directory(path.parent().expect("turn file has a parent"), true)
            .map_err(std::io::Error::other)?;
    let mut options = OpenOptions::new();
    options.write(true).create(true).follow(FollowSymlinks::No);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NONBLOCK);
    }
    let mut file =
        directory.open_with(path.file_name().expect("turn file has a name"), &options)?;
    if !file.metadata()?.is_file() {
        return Err(std::io::ErrorKind::InvalidInput.into());
    }
    file.set_len(0)?;
    file.write_all(bytes)
}

impl ToolLogin {
    pub async fn prepare(
        data: &Path,
        home: &Path,
        catalog: Value,
        language: &str,
        vm: LoginVm,
    ) -> Result<Arc<Self>, String> {
        Self::prepare_with_keychain(data, home, catalog, language, vm, Arc::new(SystemKeychain))
            .await
    }

    pub(crate) async fn prepare_with_keychain(
        data: &Path,
        home: &Path,
        catalog: Value,
        language: &str,
        vm: LoginVm,
        keychain: Arc<dyn gb_config::secrets::Keychain>,
    ) -> Result<Arc<Self>, String> {
        let name = catalog["name"]
            .as_str()
            .ok_or("Tool name missing.")?
            .to_owned();
        let broker = &catalog["provision"]["credential_broker"];
        let command = gb_base::shell::managed_cli_command(
            &home.join(".guildbotics/bin/guildbotics"),
            &["environment", "login", &name],
        );
        let source_language = language.to_owned();
        let source_command = command.clone();
        let state = Arc::new(tokio::sync::Mutex::new(None::<Held>));
        let shared = state.clone();
        let source_catalog = catalog.clone();
        let source_data = data.to_path_buf();
        let source_name = name.clone();
        let source_keychain = keychain.clone();
        let source: TokenSource = Arc::new(move |refused| {
            let shared = shared.clone();
            let catalog = source_catalog.clone();
            let vm = vm.clone();
            let data = source_data.clone();
            let name = source_name.clone();
            let language = source_language.clone();
            let command = source_command.clone();
            let keychain = source_keychain.clone();
            Box::pin(async move {
                spawn_private(private_tasks(), async move {
                    let mut held = shared.lock().await;
                    let login = held.as_mut().ok_or_else(|| {
                        login_problem(&catalog, "credentials_missing", &command, &language)
                    })?;
                    let had_refusal = login.refusal.is_some();
                    let rejected_login = !had_refusal
                        && refused.is_some()
                        && (login.refreshed
                            || catalog["provision"]["credential_broker"]["refresh"]
                                .as_array()
                                .is_none_or(|v| v.is_empty()));
                    let refresh_catalog = catalog.clone();
                    let refresh_data = data.clone();
                    let refresh_name = name.clone();
                    let refresh_keychain = keychain.clone();
                    let result = lent_access(login, &catalog, refused, move |stale| async move {
                        let device = DeviceKey {
                            machine_root: refresh_data.parent().unwrap_or(&refresh_data).into(),
                            keychain: refresh_keychain.as_ref(),
                        };
                        refresh_login(
                            &device,
                            &refresh_data,
                            &refresh_name,
                            &refresh_catalog,
                            &stale,
                            |files| {
                                let argv =
                                    refresh_catalog["provision"]["credential_broker"]["refresh"]
                                        .as_array()
                                        .into_iter()
                                        .flatten()
                                        .filter_map(Value::as_str)
                                        .map(str::to_owned)
                                        .collect();
                                private_run(
                                    vm,
                                    refresh_catalog.clone(),
                                    files,
                                    argv,
                                    false,
                                    tokio_util::sync::CancellationToken::new(),
                                )
                            },
                        )
                        .await
                    })
                    .await;
                    if let Err(mut key) = result {
                        if key == "refresh_failed" && !had_refusal {
                            let device = DeviceKey {
                                machine_root: data.parent().unwrap_or(&data).into(),
                                keychain: keychain.as_ref(),
                            };
                            let status = credential_vault::status(
                                &device,
                                &path(&data, &name),
                                &label(
                                    &name,
                                    catalog["provision"]["credential_broker"]["format"]
                                        .as_str()
                                        .unwrap_or(""),
                                ),
                                command.clone(),
                            );
                            key = match status.state {
                                "locked" => "credentials_locked",
                                "unavailable" => "credentials_unavailable",
                                "corrupt" => "credentials_corrupt",
                                "missing" => "credentials_missing",
                                _ => key,
                            };
                            login.refusal = Some(key);
                        }
                        if rejected_login {
                            authentication_outcome(&data, &name, true)?;
                        }
                        return Err(login_problem(&catalog, key, &command, &language));
                    }
                    result.map_err(str::to_owned)
                })
                .await
                .map_err(|_| "The private login task failed.".to_owned())?
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
            Gateway::start_tls(policy, source.clone(), names).await
        } else {
            Gateway::start(policy, source.clone()).await
        }
        .map_err(|_| "The credential gateway cannot start.")?;
        gateway.revoke();
        let root = tempfile::tempdir().map_err(|_| "Cannot hold the tool's stand-in.")?;
        let guest_root = state_root(&catalog["provision"])?;
        Ok(Arc::new(Self {
            name,
            guest_root,
            host_root: root,
            gateway,
            source,
            keychain,
            data: data.into(),
            catalog,
            language: language.into(),
            command,
            state,
        }))
    }
    pub async fn begin(&self) -> Result<BTreeMap<String, String>, String> {
        self.begin_with_keychain(self.keychain.clone()).await
    }
    async fn begin_with_keychain(
        &self,
        keychain: Arc<dyn gb_config::secrets::Keychain>,
    ) -> Result<BTreeMap<String, String>, String> {
        let mut active = self.state.lock().await;
        if active.as_ref().is_some_and(|held| held._lease.active()) {
            return Err("The tool is already lent.".into());
        }
        // A cancelled begin may still be capturing its rotated login. Taking
        // this mutex waits for that writer before borrowing the sealed result.
        active.take();
        let provision = &self.catalog["provision"];
        let broker = &provision["credential_broker"];
        let data = self.data.clone();
        let name = self.name.clone();
        let format = broker["format"].as_str().unwrap_or("").to_owned();
        let problem_catalog = self.catalog.clone();
        let problem_language = self.language.clone();
        let command = self.command.clone();
        let files = tokio::task::spawn_blocking(move || -> Result<_, String> {
            let path = path(&data, &name);
            let device = DeviceKey {
                machine_root: data.parent().unwrap_or(&data).into(),
                keychain: keychain.as_ref(),
            };
            let label = label(&name, &format);
            let problem = || {
                let status = credential_vault::status(&device, &path, &label, command.clone());
                let key = match status.state {
                    "locked" => "credentials_locked",
                    "unavailable" => "credentials_unavailable",
                    "missing" => "credentials_missing",
                    _ => "credentials_corrupt",
                };
                login_problem(&problem_catalog, key, &command, &problem_language)
            };
            let files = credential_vault::unseal(&device, &path, &label)
                .map_err(|_| problem())?
                .ok_or_else(problem)?;
            Ok(files)
        })
        .await
        .map_err(|_| "The sealed login cannot be read.")??;
        access(&files, &self.catalog).map_err(|_| {
            login_problem(
                &self.catalog,
                "credentials_corrupt",
                &self.command,
                &self.language,
            )
        })?;
        let auth = provision["auth"]
            .as_str()
            .ok_or("Tool auth file missing.")?;
        let document = credential_document(files.get(auth).ok_or("Credential file missing.")?)?;
        let random = format!(
            "{}{}",
            broker["stand_in_prefix"].as_str().unwrap_or(""),
            gb_environment::gateway::Leases::default().lend().stand_in
        );
        let (document, token) = stand_in(&document, broker, &random)?;
        let lease = LoanLease::new(self.gateway.lend(token.clone()));
        if broker["stand_in_command_env"]
            .as_str()
            .is_none_or(str::is_empty)
            && broker["stand_in_env"].as_str().is_none_or(str::is_empty)
        {
            let target = self.host_root.path().join(auth);
            write_turn_file(
                &target,
                &serde_json::to_vec(&document).map_err(|_| "Invalid stand-in.")?,
            )
            .map_err(|_| "Cannot hold stand-in.")?;
        }
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
            write_turn_file(&self.host_root.path().join("ca.pem"), &self.gateway.ca_pem)
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
            write_turn_file(
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
        let mut beginning = BeginGuard {
            state: self.state.clone(),
            lease: Some(lease.clone()),
        };
        *active = Some(Held {
            files,
            refreshed: false,
            refusal: None,
            _lease: lease,
        });
        drop(active);
        if let Err(reason) = (self.source)(None).await {
            self.end().await;
            return Err(reason);
        }
        beginning.lease = None;
        Ok(env)
    }
    pub async fn end(&self) -> String {
        self.gateway.revoke();
        let held = self.state.lock().await.take();
        held.and_then(|held| held.refusal)
            .map(|key| login_problem(&self.catalog, key, &self.command, &self.language))
            .unwrap_or_default()
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
                    persisted_file(&path)?;
                    match std::fs::remove_file(&path) {
                        Ok(()) => {}
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                        Err(_) => return Err("Cannot remove obsolete tool settings.".into()),
                    }
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
                quota_mib: None,
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

    #[cfg(unix)]
    #[path = "real_device.rs"]
    mod real_device;
    #[cfg(unix)]
    #[test]
    fn a_turn_file_keeps_its_inode_and_never_writes_through_a_link() {
        use std::os::unix::fs::{MetadataExt, symlink};

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state/auth.json");
        write_turn_file(&path, b"previous turn").unwrap();
        let inode = std::fs::metadata(&path).unwrap().ino();
        write_turn_file(&path, b"next turn").unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().ino(), inode);
        assert_eq!(std::fs::read(&path).unwrap(), b"next turn");

        let other = dir.path().join("untouched");
        std::fs::write(&other, b"outside").unwrap();
        std::fs::remove_file(&path).unwrap();
        symlink(&other, &path).unwrap();
        assert!(write_turn_file(&path, b"must not write").is_err());
        assert_eq!(std::fs::read(&other).unwrap(), b"outside");
    }

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
    fn catalog() -> Value {
        json!({"provision":{"state_root":".tool","auth":"auth.json","persisted":["settings.json","sessions/"],"api_domains":["api.example.test"],"credential_broker":{"access_token":["access"],"expires_at":["expires"],"expires_format":"epoch_ms","format":"test"}}})
    }
    fn credentials(token: &str) -> Files {
        Files::from([(
            "auth.json".into(),
            serde_json::to_vec(&json!({"access":token,"expires":4102444800000i64})).unwrap(),
        )])
    }
    #[test]
    fn private_network_and_capture_follow_operation_boundaries() {
        let catalog = catalog();
        for interactive in [false, true] {
            let network = private_network(&catalog, &["127.0.0.1".into()], interactive);
            assert_eq!(network.unrestricted, interactive);
            assert_eq!(
                network.domains,
                if interactive {
                    vec![]
                } else {
                    vec!["api.example.test".to_owned()]
                }
            );
            assert!(!network.local_network);
            assert!(network.host_ports.is_empty());
            assert_eq!(network.nameservers, ["127.0.0.1"]);
            assert_eq!(
                capture_names(&catalog, interactive).unwrap(),
                if interactive {
                    vec!["auth.json", "settings.json"]
                } else {
                    vec!["auth.json"]
                }
            );
        }
    }
    #[test]
    fn refresh_requires_changed_token_not_changed_json_bytes() {
        let handed = expire(credentials("synthetic-old"), &catalog()).unwrap();
        let mut rewritten = handed.clone();
        rewritten.get_mut("auth.json").unwrap().push(b' ');
        assert!(!refresh_changed(&rewritten, &handed, &catalog()));
        assert!(refresh_changed(
            &credentials("synthetic-new"),
            &handed,
            &catalog()
        ));
        assert!(!refresh_changed(&Files::new(), &handed, &catalog()));
    }
    #[test]
    fn account_logins_require_refresh_tokens_and_valid_expiry() {
        let mut catalog = catalog();
        catalog["provision"]["credential_broker"]["refresh_token"] = json!(["refresh"]);
        for document in [
            json!({"access":"synthetic","expires":4102444800000i64}),
            json!({"access":"synthetic","refresh":"","expires":4102444800000i64}),
            json!({"access":"synthetic","refresh":"synthetic"}),
            json!({"access":"synthetic","refresh":"synthetic","expires":"invalid"}),
        ] {
            let files = Files::from([("auth.json".into(), serde_json::to_vec(&document).unwrap())]);
            assert!(access(&files, &catalog).is_err());
        }
    }
    #[test]
    fn commented_account_documents_remain_valid_across_read_and_expiry() {
        let mut files = credentials("synthetic");
        let raw = files.get_mut("auth.json").unwrap();
        raw.splice(..0, b"// generated account file\n".iter().copied());
        assert_eq!(access(&files, &catalog()).unwrap(), "synthetic");
        assert!(!expiring(&files, &catalog()));
        assert!(expiring(&expire(files, &catalog()).unwrap(), &catalog()));
    }
    struct FakeKeychain;
    impl gb_config::secrets::Keychain for FakeKeychain {
        fn read(&self) -> anyhow::Result<Option<String>> {
            use base64::Engine;
            Ok(Some(
                base64::engine::general_purpose::STANDARD.encode([7; 32]),
            ))
        }
        fn create(&self, _: &str) -> anyhow::Result<()> {
            panic!("existing key")
        }
    }
    fn held(files: Files) -> Held {
        Held {
            files,
            refreshed: false,
            refusal: None,
            _lease: LoanLease::new(gb_environment::gateway::Leases::default().lend()),
        }
    }
    #[tokio::test]
    async fn one_refresh_per_turn_and_refusal_is_sticky() {
        let mut catalog = catalog();
        catalog["provision"]["credential_broker"]["refresh"] = json!(["refresh"]);
        let mut login = held(credentials("synthetic-old"));
        for refused in [None, Some("synthetic-older".into())] {
            assert_eq!(
                lent_access(&mut login, &catalog, refused, |_| async {
                    panic!("fresh token must not refresh")
                })
                .await
                .unwrap(),
                "synthetic-old"
            );
        }
        let token = lent_access(
            &mut login,
            &catalog,
            Some("synthetic-old".into()),
            |_| async { Ok(credentials("synthetic-new")) },
        )
        .await
        .unwrap();
        assert_eq!(token, "synthetic-new");
        assert_eq!(
            lent_access(
                &mut login,
                &catalog,
                Some("synthetic-old".into()),
                |_| async { panic!("already refreshed") }
            )
            .await
            .unwrap(),
            token
        );
        assert!(
            lent_access(&mut login, &catalog, Some(token), |_| async {
                panic!("second refresh")
            })
            .await
            .is_err()
        );
        assert!(
            lent_access(&mut login, &catalog, None, |_| async {
                panic!("failed turn")
            })
            .await
            .is_err()
        );
        let mut login = held(credentials("synthetic-old"));
        assert!(
            lent_access(
                &mut login,
                &catalog,
                Some("synthetic-old".into()),
                |_| async { Err("capture failed".into()) }
            )
            .await
            .is_err()
        );
        assert!(
            lent_access(&mut login, &catalog, None, |_| async {
                panic!("retry after loss")
            })
            .await
            .is_err()
        );
    }
    #[tokio::test]
    async fn refresh_reloads_latest_store_and_preserves_rotations_even_after_process_failure() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let catalog = catalog();
        let vault = path(dir.path(), "tool");
        credential_vault::seal(
            &device,
            &vault,
            "tool:default:test",
            &credentials("synthetic-new"),
        )
        .unwrap();
        let latest = refresh_login(
            &device,
            dir.path(),
            "tool",
            &catalog,
            "synthetic-old",
            |_| async { panic!("another turn already rotated") },
        )
        .await
        .unwrap();
        assert_eq!(access(&latest, &catalog).unwrap(), "synthetic-new");
        let returned = refresh_login(
            &device,
            dir.path(),
            "tool",
            &catalog,
            "synthetic-new",
            |handed| {
                let catalog = &catalog;
                async move {
                    assert!(expiring(&handed, catalog));
                    // The CLI may fail after spending its refresh token.
                    Ok((1, credentials("synthetic-next"), vec![]))
                }
            },
        )
        .await
        .unwrap();
        assert_eq!(access(&returned, &catalog).unwrap(), "synthetic-next");
        assert_eq!(
            credential_vault::unseal(&device, &vault, "tool:default:test")
                .unwrap()
                .unwrap(),
            returned
        );
        let before = std::fs::read(&vault).unwrap();
        assert!(
            refresh_login(
                &device,
                dir.path(),
                "tool",
                &catalog,
                "synthetic-next",
                |mut handed| async move {
                    handed.get_mut("auth.json").unwrap().push(b' ');
                    Ok((0, handed, vec![]))
                }
            )
            .await
            .is_err()
        );
        assert_eq!(std::fs::read(&vault).unwrap(), before);
    }
    #[test]
    fn capture_loss_marks_authentication_failure_and_login_clears_it() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let marker = dir
            .path()
            .join("agent_environment/tool/authentication-failed");
        for captured in [
            Err("capture failed".into()),
            Ok(Files::new()),
            Ok(Files::from([("auth.json".into(), b"invalid".to_vec())])),
        ] {
            assert!(keep_capture(&device, dir.path(), "tool", &catalog(), captured).is_err());
            assert!(marker.exists());
            keep_capture(
                &device,
                dir.path(),
                "tool",
                &catalog(),
                Ok(credentials("synthetic")),
            )
            .unwrap();
            assert!(marker.exists());
            assert!(
                complete_login(
                    &device,
                    dir.path(),
                    &catalog(),
                    "tool",
                    1,
                    &credentials("synthetic"),
                    "en"
                )
                .is_err()
            );
            assert!(marker.exists());
            complete_login(
                &device,
                dir.path(),
                &catalog(),
                "tool",
                0,
                &credentials("synthetic"),
                "en",
            )
            .unwrap();
            assert!(!marker.exists());
        }
        // A vault write failure may lose a one-use refresh token too.
        std::fs::remove_file(path(dir.path(), "tool")).unwrap();
        std::fs::create_dir_all(path(dir.path(), "tool")).unwrap();
        assert!(
            keep_capture(
                &device,
                dir.path(),
                "tool",
                &catalog(),
                Ok(credentials("synthetic"))
            )
            .is_err()
        );
        assert!(marker.exists());
    }
    #[test]
    fn login_persisted_files_stay_outside_vault_and_turn_credentials() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let catalog = catalog();
        let mut files = credentials("synthetic");
        files.insert("settings.json".into(), b"account setup".to_vec());
        files.insert("unexpected".into(), b"discard".to_vec());
        keep_persisted(dir.path(), "tool", &catalog, &files).unwrap();
        let kept = keep_capture(&device, dir.path(), "tool", &catalog, Ok(files)).unwrap();
        assert_eq!(kept.len(), 1);
        assert_eq!(
            std::fs::read(
                dir.path()
                    .join("agent_environment/tool/.tool/settings.json")
            )
            .unwrap(),
            b"account setup"
        );
        assert!(
            !dir.path()
                .join("agent_environment/tool/.tool/unexpected")
                .exists()
        );
    }
    #[tokio::test]
    async fn parallel_refreshes_spend_the_refresh_token_once() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let catalog = catalog();
        credential_vault::seal(
            &device,
            &path(dir.path(), "tool"),
            "tool:default:test",
            &credentials("synthetic-old"),
        )
        .unwrap();
        let calls = std::sync::atomic::AtomicUsize::new(0);
        let run = |_| async {
            calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            tokio::task::yield_now().await;
            Ok((0, credentials("synthetic-new"), vec![]))
        };
        let (first, second) = tokio::join!(
            refresh_login(&device, dir.path(), "tool", &catalog, "synthetic-old", run),
            refresh_login(&device, dir.path(), "tool", &catalog, "synthetic-old", run)
        );
        assert_eq!(first.unwrap(), second.unwrap());
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    }
    #[tokio::test]
    async fn parallel_turns_borrow_without_holding_the_writer_lock() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let mut catalog = catalog();
        catalog["name"] = json!("tool");
        catalog["provision"]["credential_broker"]["routes"] = json!(["POST /v1/test"]);
        catalog["provision"]["credential_broker"]["upstream"] = json!("https://api.example.test");
        credential_vault::seal(
            &device,
            &path(dir.path(), "tool"),
            "tool:default:test",
            &credentials("synthetic-old"),
        )
        .unwrap();
        let vm = LoginVm {
            data: dir.path().into(),
            package: dir.path().join("package"),
            snapshot: dir.path().join("snapshot"),
            memory_mib: 512,
            cpus: 1,
            nameservers: vec![],
        };
        let first = ToolLogin::prepare(
            dir.path(),
            &dir.path().join("home"),
            catalog.clone(),
            "en",
            vm.clone(),
        )
        .await
        .unwrap();
        let second = ToolLogin::prepare(
            dir.path(),
            &dir.path().join("home"),
            catalog,
            "en",
            vm.clone(),
        )
        .await
        .unwrap();
        first
            .begin_with_keychain(Arc::new(FakeKeychain))
            .await
            .unwrap();
        second
            .begin_with_keychain(Arc::new(FakeKeychain))
            .await
            .unwrap();
        let writer =
            credential_vault::hold(&path(dir.path(), "tool"), std::time::Duration::ZERO).unwrap();
        assert!(first.state.lock().await.as_ref().unwrap()._lease.active());
        assert!(second.state.lock().await.as_ref().unwrap()._lease.active());
        first.end().await;
        assert!(second.state.lock().await.as_ref().unwrap()._lease.active());
        drop(writer);
        second.end().await;
        assert!(second.state.lock().await.is_none());
    }
    #[tokio::test]
    async fn interactive_login_starts_empty_even_with_a_corrupt_vault_and_keeps_new_account_state()
    {
        for corrupt in [true, false] {
            let dir = tempfile::tempdir().unwrap();
            let device = DeviceKey {
                machine_root: dir.path().into(),
                keychain: &FakeKeychain,
            };
            let vault = path(dir.path(), "tool");
            if corrupt {
                std::fs::create_dir_all(vault.parent().unwrap()).unwrap();
                std::fs::write(&vault, b"corrupt old vault").unwrap();
            } else {
                credential_vault::seal(
                    &device,
                    &vault,
                    "tool:default:test",
                    &credentials("synthetic-old"),
                )
                .unwrap();
            }
            authentication_outcome(dir.path(), "tool", true).unwrap();
            let result = login_operation(
                &device,
                dir.path(),
                &catalog(),
                "tool",
                false,
                "en",
                |handed| async move {
                    assert!(handed.is_empty());
                    let mut captured = credentials("synthetic-new");
                    captured.insert("settings.json".into(), b"new account settings".to_vec());
                    Ok((0, captured, vec![]))
                },
            )
            .await
            .unwrap();
            assert_eq!(result, json!({"tool":"tool", "saved":true}));
            let saved = credential_vault::unseal(&device, &vault, "tool:default:test")
                .unwrap()
                .unwrap();
            assert_eq!(access(&saved, &catalog()).unwrap(), "synthetic-new");
            assert_eq!(saved.len(), 1);
            assert_eq!(
                std::fs::read(
                    dir.path()
                        .join("agent_environment/tool/.tool/settings.json")
                )
                .unwrap(),
                b"new account settings"
            );
            assert!(
                !dir.path()
                    .join("agent_environment/tool/authentication-failed")
                    .exists()
            );
            let before = std::fs::read(&vault).unwrap();
            for (code, files) in [(0, Files::new()), (1, credentials("synthetic-other"))] {
                assert!(
                    login_operation(
                        &device,
                        dir.path(),
                        &catalog(),
                        "tool",
                        false,
                        "en",
                        |handed| async move {
                            assert!(handed.is_empty());
                            Ok((code, files, vec![]))
                        }
                    )
                    .await
                    .is_err()
                );
                assert_eq!(std::fs::read(&vault).unwrap(), before);
                assert_eq!(
                    std::fs::read(
                        dir.path()
                            .join("agent_environment/tool/.tool/settings.json")
                    )
                    .unwrap(),
                    b"new account settings"
                );
            }
        }
    }
    #[tokio::test]
    async fn private_environment_start_failures_preserve_the_login_without_marking_authentication_failed()
     {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let vault = path(dir.path(), "tool");
        credential_vault::seal(
            &device,
            &vault,
            "tool:default:test",
            &credentials("synthetic-old"),
        )
        .unwrap();
        let before = std::fs::read(&vault).unwrap();
        let marker = dir
            .path()
            .join("agent_environment/tool/authentication-failed");
        for usage in [false, true] {
            let error = login_operation(
                &device,
                dir.path(),
                &catalog(),
                "tool",
                usage,
                "en",
                |_| async { Err("VM failed to start".into()) },
            )
            .await
            .unwrap_err();
            assert_eq!(error, "VM failed to start");
            assert!(!marker.exists());
            assert_eq!(std::fs::read(&vault).unwrap(), before);
        }
        let error = refresh_login(
            &device,
            dir.path(),
            "tool",
            &catalog(),
            "synthetic-old",
            |_| async { Err("VM failed to start".into()) },
        )
        .await
        .unwrap_err();
        assert_eq!(error, "VM failed to start");
        assert!(!marker.exists());
        assert_eq!(std::fs::read(&vault).unwrap(), before);
    }
    #[tokio::test]
    async fn usage_operation_unseals_and_saves_rotation_before_a_failing_exit() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let vault = path(dir.path(), "tool");
        credential_vault::seal(
            &device,
            &vault,
            "tool:default:test",
            &credentials("synthetic-old"),
        )
        .unwrap();
        let error = login_operation(
            &device,
            dir.path(),
            &catalog(),
            "tool",
            true,
            "en",
            |handed| async move {
                assert_eq!(access(&handed, &catalog()).unwrap(), "synthetic-old");
                Ok((1, credentials("synthetic-rotated"), vec![]))
            },
        )
        .await
        .unwrap_err();
        assert!(error.contains("probe failed"));
        let saved = credential_vault::unseal(&device, &vault, "tool:default:test")
            .unwrap()
            .unwrap();
        assert_eq!(access(&saved, &catalog()).unwrap(), "synthetic-rotated");
    }
    #[tokio::test]
    async fn completed_login_rejects_api_keys_with_account_guidance_without_replacing_the_vault() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let mut catalog = catalog();
        catalog["label"] = json!("Tool");
        catalog["provision"]["credential_broker"]["refresh_token"] = json!(["refresh"]);
        let vault = path(dir.path(), "tool");
        std::fs::create_dir_all(vault.parent().unwrap()).unwrap();
        std::fs::write(&vault, b"corrupt previous vault").unwrap();
        authentication_outcome(dir.path(), "tool", true).unwrap();
        for language in ["en", "ja"] {
            let error = login_operation(
                &device,
                dir.path(),
                &catalog,
                "tool",
                false,
                language,
                |handed| async move {
                    assert!(handed.is_empty());
                    Ok((0, credentials("synthetic-api-key"), vec![]))
                },
            )
            .await
            .unwrap_err();
            assert_eq!(
                error,
                crate::i18n::t(
                    language,
                    "intelligences.agent_environment.tool.login_not_an_account",
                    &[("tool", "Tool")]
                )
            );
        }
        assert_eq!(std::fs::read(&vault).unwrap(), b"corrupt previous vault");
        assert!(
            dir.path()
                .join("agent_environment/tool/authentication-failed")
                .exists()
        );
    }
    #[test]
    fn only_a_completed_login_clears_a_failure() {
        for code in [0, 1] {
            let dir = tempfile::tempdir().unwrap();
            let device = DeviceKey {
                machine_root: dir.path().into(),
                keychain: &FakeKeychain,
            };
            authentication_outcome(dir.path(), "tool", true).unwrap();
            let result = complete_login(
                &device,
                dir.path(),
                &catalog(),
                "tool",
                code,
                &credentials("synthetic"),
                "en",
            );
            assert_eq!(result.is_ok(), code == 0);
            assert_eq!(
                dir.path()
                    .join("agent_environment/tool/authentication-failed")
                    .exists(),
                code != 0
            );
            assert_eq!(
                credential_vault::unseal(&device, &path(dir.path(), "tool"), "tool:default:test")
                    .unwrap()
                    .is_some(),
                code == 0
            );
        }
    }
    async fn tool(data: &Path) -> Arc<ToolLogin> {
        let mut catalog = catalog();
        catalog["name"] = json!("tool");
        catalog["provision"]["credential_broker"]["routes"] = json!(["POST /v1/test"]);
        catalog["provision"]["credential_broker"]["upstream"] = json!("https://api.example.test");
        let vm = LoginVm {
            data: data.into(),
            package: data.join("package"),
            snapshot: data.join("snapshot"),
            memory_mib: 512,
            cpus: 1,
            nameservers: vec![],
        };
        ToolLogin::prepare(data, &data.join("home"), catalog, "en", vm.clone())
            .await
            .unwrap()
    }
    #[tokio::test]
    async fn expiring_logins_refresh_again_until_a_provider_rejects_the_refreshed_token() {
        let mut catalog = catalog();
        catalog["provision"]["credential_broker"]["refresh"] = json!(["refresh"]);
        let mut login = held(expire(credentials("synthetic-old"), &catalog).unwrap());
        login.refreshed = true;
        assert_eq!(
            lent_access(&mut login, &catalog, None, |_| async {
                Ok(credentials("synthetic-new"))
            })
            .await
            .unwrap(),
            "synthetic-new"
        );
        assert_eq!(
            lent_access(
                &mut login,
                &catalog,
                Some("synthetic-new".into()),
                |_| async { panic!("rejected refreshed login must stop") }
            )
            .await
            .unwrap_err(),
            "refresh_failed"
        );
    }
    struct SwitchKeychain(std::sync::atomic::AtomicBool);
    impl gb_config::secrets::Keychain for SwitchKeychain {
        fn read(&self) -> anyhow::Result<Option<String>> {
            anyhow::ensure!(
                !self.0.load(std::sync::atomic::Ordering::SeqCst),
                "keychain locked"
            );
            FakeKeychain.read()
        }
        fn create(&self, _: &str) -> anyhow::Result<()> {
            panic!("existing key")
        }
    }
    #[tokio::test]
    async fn operation_failures_preserve_vault_and_mark_only_lost_existing_logins() {
        for usage in [false, true] {
            for had_marker in [false, true] {
                for failure in ["start", "capture", "seal"] {
                    let dir = tempfile::tempdir().unwrap();
                    let keychain = SwitchKeychain(std::sync::atomic::AtomicBool::new(false));
                    let device = DeviceKey {
                        machine_root: dir.path().into(),
                        keychain: &keychain,
                    };
                    let vault = path(dir.path(), "tool");
                    credential_vault::seal(
                        &device,
                        &vault,
                        "tool:default:test",
                        &credentials("synthetic-old"),
                    )
                    .unwrap();
                    let before = std::fs::read(&vault).unwrap();
                    let marker = vault.with_file_name("authentication-failed");
                    if had_marker {
                        std::fs::write(&marker, b"prior outcome").unwrap();
                    }
                    let result = login_operation(
                        &device,
                        dir.path(),
                        &catalog(),
                        "tool",
                        usage,
                        "en",
                        |_| async {
                            match failure {
                                "start" => Err(PrivateError::Start("start failed".into())),
                                "capture" => Err(PrivateError::Capture("capture failed".into())),
                                _ => {
                                    keychain.0.store(true, std::sync::atomic::Ordering::SeqCst);
                                    Ok((0, credentials("synthetic-rotated"), vec![]))
                                }
                            }
                        },
                    )
                    .await;
                    assert!(result.is_err(), "{usage} {failure}");
                    assert_eq!(std::fs::read(&vault).unwrap(), before);
                    assert_eq!(
                        marker.exists(),
                        had_marker || (usage && failure != "start"),
                        "{usage} {failure}"
                    );
                    if had_marker && (!usage || failure == "start") {
                        assert_eq!(std::fs::read(&marker).unwrap(), b"prior outcome");
                    }
                }
            }
        }
    }
    #[tokio::test]
    async fn refresh_capture_and_seal_failures_mark_but_start_failure_preserves_state() {
        for failure in ["start", "capture", "seal"] {
            let dir = tempfile::tempdir().unwrap();
            let keychain = SwitchKeychain(std::sync::atomic::AtomicBool::new(false));
            let device = DeviceKey {
                machine_root: dir.path().into(),
                keychain: &keychain,
            };
            let vault = path(dir.path(), "tool");
            credential_vault::seal(
                &device,
                &vault,
                "tool:default:test",
                &credentials("synthetic-old"),
            )
            .unwrap();
            let before = std::fs::read(&vault).unwrap();
            assert!(
                refresh_login(
                    &device,
                    dir.path(),
                    "tool",
                    &catalog(),
                    "synthetic-old",
                    |_| async {
                        match failure {
                            "start" => Err(PrivateError::Start("start failed".into())),
                            "capture" => Err(PrivateError::Capture("capture failed".into())),
                            _ => {
                                keychain.0.store(true, std::sync::atomic::Ordering::SeqCst);
                                Ok((0, credentials("synthetic-rotated"), vec![]))
                            }
                        }
                    }
                )
                .await
                .is_err()
            );
            assert_eq!(std::fs::read(&vault).unwrap(), before);
            assert_eq!(
                vault.with_file_name("authentication-failed").exists(),
                failure != "start"
            );
        }
    }
    #[tokio::test]
    async fn cancelled_begin_revokes_its_lease_and_waits_for_tracked_refresh_capture() {
        for tracked in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let mut loan = tool(dir.path()).await;
            let device = DeviceKey {
                machine_root: dir.path().into(),
                keychain: &FakeKeychain,
            };
            credential_vault::seal(
                &device,
                &path(dir.path(), "tool"),
                "tool:default:test",
                &credentials("synthetic-old"),
            )
            .unwrap();
            let (start, started) = tokio::sync::oneshot::channel();
            let (release, released) = tokio::sync::oneshot::channel();
            let channels = Arc::new(std::sync::Mutex::new(Some((start, released))));
            let state = loan.state.clone();
            let data = dir.path().to_owned();
            let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
            let observed = calls.clone();
            Arc::get_mut(&mut loan).unwrap().source = Arc::new(move |_| {
                let state = state.clone();
                let data = data.clone();
                let channels = channels.clone();
                let first = observed.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0;
                Box::pin(async move {
                    if !first {
                        return Ok("synthetic-rotated".into());
                    }
                    let (start, released) = channels.lock().unwrap().take().unwrap();
                    let work = async move {
                        let mut held = state.lock().await;
                        let lease = held.as_ref().unwrap()._lease.clone();
                        let device = DeviceKey {
                            machine_root: data.clone(),
                            keychain: &FakeKeychain,
                        };
                        let files = refresh_login(
                            &device,
                            &data,
                            "tool",
                            &catalog(),
                            "synthetic-old",
                            |_| async {
                                start
                                    .send(lease)
                                    .unwrap_or_else(|_| panic!("caller waiting"));
                                released.await.unwrap();
                                Ok((0, credentials("synthetic-rotated"), vec![]))
                            },
                        )
                        .await?;
                        held.as_mut().unwrap().files = files;
                        Ok("synthetic-rotated".into())
                    };
                    if tracked {
                        spawn_private(private_tasks(), work).await.unwrap()
                    } else {
                        work.await
                    }
                })
            });
            let caller_loan = loan.clone();
            let (expire, expired) = tokio::sync::oneshot::channel();
            let caller = tokio::spawn(async move {
                let beginning = caller_loan.begin_with_keychain(Arc::new(FakeKeychain));
                tokio::pin!(beginning);
                tokio::select! {
                    _ = beginning.as_mut() => panic!("source must remain pending"),
                    _ = expired => tokio::time::timeout(std::time::Duration::ZERO, beginning).await,
                }
            });
            let lease = started.await.unwrap();
            assert!(lease.active());
            expire.send(()).unwrap();
            assert!(caller.await.unwrap().is_err());
            assert!(
                !lease.active(),
                "cancellation must revoke before refresh completes"
            );
            let retry = loan.begin_with_keychain(Arc::new(FakeKeychain));
            tokio::pin!(retry);
            if tracked {
                assert!(futures_util::poll!(retry.as_mut()).is_pending());
                release.send(()).unwrap();
            } else {
                drop(release);
            }
            tokio::time::timeout(std::time::Duration::from_secs(2), retry)
                .await
                .unwrap()
                .unwrap();
            if tracked {
                let saved = credential_vault::unseal(
                    &device,
                    &path(dir.path(), "tool"),
                    "tool:default:test",
                )
                .unwrap()
                .unwrap();
                assert_eq!(access(&saved, &catalog()).unwrap(), "synthetic-rotated");
            }
            assert!(loan.state.lock().await.as_ref().unwrap()._lease.active());
            loan.end().await;
        }
    }
    #[tokio::test]
    async fn begin_waits_for_the_token_source_before_giving_the_turn_its_grant() {
        let dir = tempfile::tempdir().unwrap();
        let mut loan = tool(dir.path()).await;
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        credential_vault::seal(
            &device,
            &path(dir.path(), "tool"),
            "tool:default:test",
            &credentials("synthetic-old"),
        )
        .unwrap();
        let state = loan.state.clone();
        let called = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let observed = called.clone();
        Arc::get_mut(&mut loan).unwrap().source = Arc::new(move |refused| {
            assert!(refused.is_none());
            let state = state.clone();
            let observed = observed.clone();
            Box::pin(async move {
                assert!(state.lock().await.is_some());
                observed.store(true, std::sync::atomic::Ordering::SeqCst);
                Err("synthetic refresh refusal".into())
            })
        });
        assert_eq!(
            loan.begin_with_keychain(Arc::new(FakeKeychain))
                .await
                .unwrap_err(),
            "synthetic refresh refusal"
        );
        assert!(called.load(std::sync::atomic::Ordering::SeqCst));
        assert!(loan.state.lock().await.is_none());
    }
    #[tokio::test]
    async fn a_sticky_vm_start_refusal_does_not_mark_authentication_when_old_requests_finish() {
        let dir = tempfile::tempdir().unwrap();
        let loan = tool(dir.path()).await;
        let mut login = held(credentials("synthetic-old"));
        login.refreshed = true;
        login.refusal = Some("refresh_failed");
        *loan.state.lock().await = Some(login);
        for refused in [None, Some("synthetic-old".into())] {
            assert_eq!(
                (loan.source)(refused).await.unwrap_err(),
                login_problem(&loan.catalog, "refresh_failed", &loan.command, "en")
            );
            assert!(
                !dir.path()
                    .join("agent_environment/tool/authentication-failed")
                    .exists()
            );
        }
        assert_eq!(
            loan.end().await,
            login_problem(&loan.catalog, "refresh_failed", &loan.command, "en")
        );
    }
    #[tokio::test]
    async fn host_end_turn_returns_the_lent_login_refusal_and_revokes_the_grant() {
        let dir = tempfile::tempdir().unwrap();
        let mut loan = tool(dir.path()).await;
        Arc::get_mut(&mut loan).unwrap().keychain = Arc::new(FakeKeychain);
        credential_vault::seal(
            &DeviceKey {
                machine_root: dir.path().into(),
                keychain: &FakeKeychain,
            },
            &path(dir.path(), "tool"),
            "tool:default:test",
            &credentials("synthetic"),
        )
        .unwrap();
        let invocation = gb_environment::broker::Invocation {
            chat_subject: None,
            person_id: "aiko".into(),
            run_id: "run".into(),
            trace_id: "trace".into(),
            work_kind: "ticket".into(),
            work_identity: "87".into(),
            cwd: dir.path().into(),
            access: gb_wire::CommandAccess {
                read_only: false,
                inspects: vec![],
            },
        };
        let host = crate::host_window::HttpHost::start_context(crate::host_window::Context {
            broker: gb_environment::broker::Broker::new(
                dir.path().join("unused-cli"),
                invocation.clone(),
            ),
            invocation,
            workspace: dir.path().into(),
            mounts: BTreeMap::from([(gb_environment::guest_path(dir.path()).unwrap(), true)]),
            turn_mounts: BTreeMap::new(),
            tools: ["tool".into()].into(),
            summary_tools: ["tool".into()].into(),
            loans: BTreeMap::from([("tool".into(), loan.clone())]),
            rate_limits: BTreeMap::new(),
            record: Arc::new(|_| {}),
            required_io: None,
        })
        .await
        .unwrap();
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let begin = client
            .post(format!("http://127.0.0.1:{}/host/begin_turn", host.port))
            .bearer_auth(&host.token)
            .json(&json!({"tool":"tool","cwd":gb_environment::guest_path(dir.path()).unwrap()}))
            .send()
            .await
            .unwrap()
            .json::<Value>()
            .await
            .unwrap();
        let grant = begin["result"]["turn_grant"].as_str().unwrap();
        loan.state.lock().await.as_mut().unwrap().refusal = Some("refresh_failed");
        let end = || {
            client
                .post(format!("http://127.0.0.1:{}/host/end_turn", host.port))
                .bearer_auth(&host.token)
                .json(&json!({"turn_grant":grant}))
        };
        let answer = end().send().await.unwrap();
        assert_eq!(answer.status(), reqwest::StatusCode::OK);
        let answer = answer.json::<Value>().await.unwrap();
        assert_eq!(
            answer["result"]["refusal"],
            login_problem(&loan.catalog, "refresh_failed", &loan.command, "en")
        );
        assert!(loan.state.lock().await.is_none());
        assert_eq!(
            end().send().await.unwrap().status(),
            reqwest::StatusCode::FORBIDDEN
        );
    }
    #[tokio::test]
    async fn end_preserves_the_localized_sticky_refusal_before_releasing_credentials() {
        let dir = tempfile::tempdir().unwrap();
        let tool = tool(dir.path()).await;
        for key in [
            "refresh_failed",
            "login_refused",
            "credentials_corrupt",
            "credentials_locked",
            "credentials_unavailable",
        ] {
            let mut login = held(credentials("synthetic"));
            login.refusal = Some(key);
            *tool.state.lock().await = Some(login);
            assert_eq!(
                tool.end().await,
                login_problem(&tool.catalog, key, &tool.command, "en")
            );
            assert!(tool.state.lock().await.is_none());
            assert_eq!(tool.end().await, "");
        }
        let mut login = held(credentials("synthetic"));
        let mut catalog = catalog();
        catalog["provision"]["credential_broker"]["refresh"] = json!(["refresh"]);
        let reason = lent_access(&mut login, &catalog, Some("synthetic".into()), |_| async {
            Err("private failure".into())
        })
        .await
        .unwrap_err();
        assert_eq!(reason, "refresh_failed");
        assert_eq!(login.refusal, Some("refresh_failed"));
        for language in ["ja", "en"] {
            let text = login_problem(
                &catalog,
                reason,
                "guildbotics environment login tool",
                language,
            );
            assert!(text.contains("guildbotics environment login tool"));
            assert!(!text.contains("%{"));
            assert!(!text.contains("intelligences.agent_environment"));
        }
    }
    #[tokio::test]
    async fn persisted_state_is_allowlisted_and_readonly_account_copies_are_fresh() {
        let dir = tempfile::tempdir().unwrap();
        let tool = tool(dir.path()).await;
        let shared = dir.path().join("agent_environment/tool/.tool");
        // A turn finds the tool's state under the microVM's own home, never
        // under a host path.
        let root = format!("{}/.tool", gb_environment::GUEST_HOME);
        assert_eq!(tool.guest_root, root);
        let mounts = tool.persisted_mounts(false).unwrap();
        assert_eq!(mounts.len(), 1);
        assert_eq!(mounts[0].guest, format!("{root}/sessions"));
        assert_eq!(
            mounts[0].host.as_deref(),
            Some(shared.join("sessions").as_path())
        );
        assert!(shared.join("sessions").is_dir());
        std::fs::write(shared.join("auth.json"), b"ignored legacy plaintext").unwrap();
        std::fs::write(shared.join("settings.json"), b"account").unwrap();
        let ordinary = tool.persisted_mounts(false).unwrap();
        assert_eq!(ordinary.len(), 2);
        assert!(
            ordinary
                .iter()
                .all(|mount| !mount.guest.ends_with("auth.json"))
        );
        let readonly = tool.persisted_mounts(true).unwrap();
        let account = readonly[0].host.as_ref().unwrap();
        assert!(!account.starts_with(&shared));
        std::fs::write(account, b"changed").unwrap();
        tool.persisted_mounts(true).unwrap();
        assert_eq!(std::fs::read(account).unwrap(), b"account");
        assert_eq!(
            std::fs::read(shared.join("settings.json")).unwrap(),
            b"account"
        );
        std::fs::remove_file(shared.join("settings.json")).unwrap();
        assert_eq!(tool.persisted_mounts(true).unwrap().len(), 1);
        assert!(!account.exists());
    }
    #[tokio::test]
    async fn private_shutdown_deadline_does_not_abort_a_late_credential_writer() {
        let tasks = tokio_util::task::TaskTracker::new();
        let (release, released) = tokio::sync::oneshot::channel();
        let worker = spawn_private(&tasks, async {
            released.await.unwrap();
        });
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            drain_private_tasks(&tasks, std::time::Duration::from_millis(10)),
        )
        .await
        .unwrap();
        assert!(!worker.is_finished());
        release.send(()).unwrap();
        worker.await.unwrap();
        tasks.wait().await;
    }
    #[tokio::test]
    async fn delayed_begin_cleanup_cannot_remove_or_revoke_a_newer_loan() {
        let dir = tempfile::tempdir().unwrap();
        let loan = tool(dir.path()).await;
        let old = LoanLease::new(loan.gateway.lend("synthetic-old-stand-in".into()));
        let guard = BeginGuard {
            state: loan.state.clone(),
            lease: Some(old),
        };
        let mut active = loan.state.lock().await;
        let new = LoanLease::new(loan.gateway.lend("synthetic-new-stand-in".into()));
        let mut login = held(credentials("synthetic-new"));
        login._lease = new.clone();
        *active = Some(login);
        drop(guard);
        assert!(new.active());
        drop(active);
        // The cleanup was queued first and must retain the newer identity.
        tokio::task::yield_now().await;
        assert!(loan.state.lock().await.as_ref().unwrap()._lease.active());
        loan.end().await;
    }
    #[tokio::test]
    async fn caller_cancellation_does_not_cancel_capture_and_shutdown_drains_the_writer() {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path().to_path_buf();
        let tasks = tokio_util::task::TaskTracker::new();
        let (started, begun) = tokio::sync::oneshot::channel();
        let (release, released) = tokio::sync::oneshot::channel();
        let worker = spawn_private(&tasks, async move {
            let device = DeviceKey {
                machine_root: data.clone(),
                keychain: &FakeKeychain,
            };
            let _held = hold_login(path(&data, "tool")).await.unwrap();
            started.send(()).unwrap();
            released.await.unwrap();
            keep_capture(
                &device,
                &data,
                "tool",
                &catalog(),
                Ok(credentials("synthetic-rotated")),
            )
            .unwrap();
        });
        let caller = tokio::spawn(async move { worker.await.unwrap() });
        begun.await.unwrap();
        caller.abort();
        assert!(caller.await.unwrap_err().is_cancelled());
        tasks.close();
        let drain = tasks.wait();
        tokio::pin!(drain);
        assert!(futures_util::poll!(drain.as_mut()).is_pending());
        release.send(()).unwrap();
        drain.await;
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        assert_eq!(
            access(
                &credential_vault::unseal(&device, &path(dir.path(), "tool"), "tool:default:test")
                    .unwrap()
                    .unwrap(),
                &catalog()
            )
            .unwrap(),
            "synthetic-rotated"
        );
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn persisted_files_and_directories_cannot_be_links() {
        let dir = tempfile::tempdir().unwrap();
        let tool = tool(dir.path()).await;
        let shared = dir.path().join("agent_environment/tool/.tool");
        std::fs::create_dir_all(&shared).unwrap();
        let outside = dir.path().join("outside");
        std::fs::write(&outside, b"private").unwrap();
        std::os::unix::fs::symlink(&outside, shared.join("settings.json")).unwrap();
        assert!(tool.persisted_mounts(false).is_err());
        std::fs::remove_file(shared.join("settings.json")).unwrap();
        std::os::unix::fs::symlink(dir.path(), shared.join("sessions")).unwrap();
        assert!(tool.persisted_mounts(false).is_err());
        assert_eq!(std::fs::read(outside).unwrap(), b"private");
    }

    #[tokio::test]
    async fn every_catalog_provider_lends_only_its_configured_standin_shape() {
        use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
        let catalogs: Value = serde_json::from_slice(include_bytes!(
            "../../../guildbotics/templates/environment/cli_provision.json"
        ))
        .unwrap();
        for catalog in catalogs.as_array().unwrap() {
            let dir = tempfile::tempdir().unwrap();
            let broker = &catalog["provision"]["credential_broker"];
            let mut document = json!({});
            let field = |path: &Value| {
                path.as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(|s| if s == "*" { "account".into() } else { s.into() })
                    .collect::<Vec<String>>()
            };
            let jwt=format!("header.{}.synthetic-signature",URL_SAFE_NO_PAD.encode(serde_json::to_vec(&json!({"exp":4102444800i64,"private":"synthetic-hidden-claim","email":"public@example.test","https://api.openai.com/auth":{"chatgpt_plan_type":"pro","chatgpt_account_id":"account-id"}})).unwrap()));
            let real = if broker["expires_format"] == "jwt" {
                jwt.clone()
            } else {
                "synthetic-real-access".into()
            };
            put(&mut document, &field(&broker["access_token"]), json!(real));
            if broker["refresh_token"]
                .as_array()
                .is_some_and(|p| !p.is_empty())
            {
                put(
                    &mut document,
                    &field(&broker["refresh_token"]),
                    json!("synthetic-real-refresh"),
                );
            }
            if broker["expires_format"] != "jwt"
                && broker["expires_at"]
                    .as_array()
                    .is_some_and(|p| !p.is_empty())
            {
                put(
                    &mut document,
                    &field(&broker["expires_at"]),
                    if broker["expires_format"] == "rfc3339" {
                        json!("2100-01-01T00:00:00Z")
                    } else {
                        json!(4102444800000i64)
                    },
                );
            }
            if broker["stand_in_claims_from"]
                .as_array()
                .is_some_and(|p| !p.is_empty())
            {
                put(
                    &mut document,
                    &field(&broker["stand_in_claims_from"]),
                    json!(jwt),
                );
            }
            for path in broker["turn_fields"].as_array().into_iter().flatten() {
                put(&mut document, &field(path), json!("public-account-field"));
            }
            let auth = catalog["provision"]["auth"].as_str().unwrap();
            let files = Files::from([(auth.into(), serde_json::to_vec(&document).unwrap())]);
            assert!(valid(&files, catalog), "{}", catalog["name"]);
            if broker["expires_at"]
                .as_array()
                .is_some_and(|p| !p.is_empty())
            {
                let expired = expire(files.clone(), catalog).unwrap();
                assert!(expiring(&expired, catalog));
                let handed = credential_document(&expired[auth]).unwrap();
                if broker["refresh_token"]
                    .as_array()
                    .is_some_and(|p| !p.is_empty())
                {
                    assert_eq!(
                        at(&handed, &broker["refresh_token"]),
                        at(&document, &broker["refresh_token"])
                    );
                }
                if broker["expires_format"] == "jwt" {
                    let original = at(&document, &broker["access_token"])
                        .unwrap()
                        .as_str()
                        .unwrap()
                        .split('.')
                        .collect::<Vec<_>>();
                    let expired = at(&handed, &broker["access_token"])
                        .unwrap()
                        .as_str()
                        .unwrap()
                        .split('.')
                        .collect::<Vec<_>>();
                    assert_eq!((original[0], original[2]), (expired[0], expired[2]));
                }
            } else {
                assert!(!expiring(&files, catalog));
            }

            let device = DeviceKey {
                machine_root: dir.path().into(),
                keychain: &FakeKeychain,
            };
            let name = catalog["name"].as_str().unwrap();
            credential_vault::seal(
                &device,
                &path(dir.path(), name),
                &label(name, broker["format"].as_str().unwrap()),
                &files,
            )
            .unwrap();
            let vm = LoginVm {
                data: dir.path().into(),
                package: dir.path().join("package"),
                snapshot: dir.path().join("snapshot"),
                memory_mib: 512,
                cpus: 1,
                nameservers: vec![],
            };
            let tool = ToolLogin::prepare(
                dir.path(),
                &dir.path().join("home"),
                catalog.clone(),
                "en",
                vm.clone(),
            )
            .await
            .unwrap();
            let environment = tool
                .begin_with_keychain(Arc::new(FakeKeychain))
                .await
                .unwrap();
            let by_command = broker["stand_in_command_env"]
                .as_str()
                .is_some_and(|v| !v.is_empty());
            let by_environment = broker["stand_in_env"]
                .as_str()
                .is_some_and(|v| !v.is_empty());
            assert_eq!(
                tool.host_root.path().join(auth).exists(),
                !by_command && !by_environment,
                "{}",
                name
            );
            for value in environment.values() {
                assert!(!value.contains("synthetic-real"));
            }
            if by_command {
                assert!(tool.host_root.path().join("stand-in.json").exists());
            }
            if !by_command && !by_environment {
                let stub = std::fs::read(tool.host_root.path().join(auth)).unwrap();
                assert!(!String::from_utf8_lossy(&stub).contains("synthetic-real"));
                assert!(!String::from_utf8_lossy(&stub).contains("synthetic-signature"));
                let parsed = credential_document(&stub).unwrap();
                for path in broker["turn_fields"].as_array().into_iter().flatten() {
                    assert_eq!(at(&parsed, path), at(&document, path));
                }
                if broker["expires_at"]
                    .as_array()
                    .is_some_and(|p| !p.is_empty())
                {
                    assert!(
                        expiry(&parsed, broker).unwrap()
                            > chrono::Utc::now().timestamp() as f64 + 300.0 * 86400.0
                    );
                }
            }
            tool.end().await;
        }
    }
    #[test]
    fn unchanged_usage_probes_never_reseal_the_login() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let files = credentials("synthetic-current");
        let vault = path(dir.path(), "tool");
        credential_vault::seal(&device, &vault, "tool:default:test", &files).unwrap();
        let before = std::fs::read(&vault).unwrap();
        complete_probe(&device, dir.path(), &catalog(), "tool", 0, &files, &files).unwrap();
        assert_eq!(std::fs::read(&vault).unwrap(), before);
        assert!(
            complete_probe(
                &device,
                dir.path(),
                &catalog(),
                "tool",
                1,
                &files,
                &credentials("synthetic-new")
            )
            .is_err()
        );
        assert_eq!(
            access(
                &credential_vault::unseal(&device, &vault, "tool:default:test")
                    .unwrap()
                    .unwrap(),
                &catalog()
            )
            .unwrap(),
            "synthetic-new"
        );
    }

    #[tokio::test]
    async fn refresh_lock_and_store_failures_refuse_before_starting_a_private_process() {
        let dir = tempfile::tempdir().unwrap();
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &FakeKeychain,
        };
        let vault = path(dir.path(), "tool");
        std::fs::create_dir_all(vault.with_extension("lock")).unwrap();
        let refused = refresh_login(
            &device,
            dir.path(),
            "tool",
            &catalog(),
            "synthetic",
            |_| async { panic!("unlocked login must not run") },
        )
        .await
        .unwrap_err();
        assert!(refused.contains("busy"));
        std::fs::remove_dir(vault.with_extension("lock")).unwrap();
        let refused = refresh_login(
            &device,
            dir.path(),
            "tool",
            &catalog(),
            "synthetic",
            |_| async { panic!("missing login must not run") },
        )
        .await
        .unwrap_err();
        assert!(refused.contains("missing"));
    }
    #[tokio::test]
    async fn a_login_waits_for_refresh_and_its_newer_account_remains_sealed() {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path().to_path_buf();
        let device = DeviceKey {
            machine_root: data.clone(),
            keychain: &FakeKeychain,
        };
        credential_vault::seal(
            &device,
            &path(&data, "tool"),
            "tool:default:test",
            &credentials("synthetic-old"),
        )
        .unwrap();
        let (start, started) = tokio::sync::oneshot::channel();
        let (release, released) = tokio::sync::oneshot::channel();
        let worker_data = data.clone();
        let refreshing = tokio::spawn(async move {
            let device = DeviceKey {
                machine_root: worker_data.clone(),
                keychain: &FakeKeychain,
            };
            refresh_login(
                &device,
                &worker_data,
                "tool",
                &catalog(),
                "synthetic-old",
                |_| async move {
                    start.send(()).unwrap();
                    released.await.unwrap();
                    Ok((0, credentials("synthetic-refreshed"), vec![]))
                },
            )
            .await
            .unwrap();
        });
        started.await.unwrap();
        let login_started = std::cell::Cell::new(false);
        let logging_in = async {
            login_operation(&device, &data, &catalog(), "tool", false, "en", |handed| {
                login_started.set(true);
                assert!(handed.is_empty());
                async { Ok((0, credentials("synthetic-relogin"), vec![])) }
            })
            .await
            .unwrap();
        };
        tokio::pin!(logging_in);
        assert!(futures_util::poll!(logging_in.as_mut()).is_pending());
        assert!(
            login_started.get(),
            "Interactive login runs before acquiring the save lock"
        );
        release.send(()).unwrap();
        refreshing.await.unwrap();
        logging_in.await;
        assert_eq!(
            access(
                &credential_vault::unseal(&device, &path(&data, "tool"), "tool:default:test")
                    .unwrap()
                    .unwrap(),
                &catalog()
            )
            .unwrap(),
            "synthetic-relogin"
        );
    }
    #[tokio::test]
    async fn authentication_markers_are_scoped_to_tool_and_device_and_never_mounted() {
        let device = tempfile::tempdir().unwrap();
        let other = tempfile::tempdir().unwrap();
        authentication_outcome(device.path(), "tool", true).unwrap();
        authentication_outcome(other.path(), "tool", false).unwrap();
        assert!(
            device
                .path()
                .join("agent_environment/tool/authentication-failed")
                .exists()
        );
        assert!(
            !device
                .path()
                .join("agent_environment/other/authentication-failed")
                .exists()
        );
        let tool = tool(device.path()).await;
        assert!(tool.persisted_mounts(false).unwrap().iter().all(|mount| {
            !mount
                .host
                .as_ref()
                .unwrap()
                .ends_with("authentication-failed")
        }));
        authentication_outcome(device.path(), "tool", false).unwrap();
        assert!(
            !device
                .path()
                .join("agent_environment/tool/authentication-failed")
                .exists()
        );
    }
    #[tokio::test]
    async fn nonexpiring_logins_are_refused_without_attempting_refresh_and_jwts_require_expiry() {
        let mut nonexpiring = catalog();
        nonexpiring["provision"]["credential_broker"]["expires_at"] = json!([]);
        let mut login = held(credentials("synthetic"));
        assert_eq!(
            lent_access(&mut login, &nonexpiring, None, |_| async {
                panic!("nonexpiring login")
            })
            .await
            .unwrap(),
            "synthetic"
        );
        assert!(
            lent_access(
                &mut login,
                &nonexpiring,
                Some("synthetic".into()),
                |_| async { panic!("no refresh command") }
            )
            .await
            .is_err()
        );
        assert!(
            lent_access(&mut login, &nonexpiring, None, |_| async {
                panic!("refused login")
            })
            .await
            .is_err()
        );
        let mut jwt = catalog();
        jwt["provision"]["credential_broker"]["expires_at"] = json!(["access"]);
        jwt["provision"]["credential_broker"]["expires_format"] = json!("jwt");
        let files = Files::from([(
            "auth.json".into(),
            br#"{"access":"header.e30.signature"}"#.to_vec(),
        )]);
        assert!(!valid(&files, &jwt));
    }
    #[test]
    fn private_plans_keep_home_and_state_root_in_the_microvm_on_any_host_and_first_login() {
        // A Windows host spells its paths with a drive; none of them is the
        // microVM's home, which is the same on every host.
        let vm = LoginVm {
            data: PathBuf::from(r"C:\data"),
            package: PathBuf::from(r"C:\checkout\guildbotics"),
            snapshot: PathBuf::from(r"C:\snapshot"),
            memory_mib: 777,
            cpus: 3,
            nameservers: vec!["192.0.2.53".into()],
        };
        let mut catalog = catalog();
        catalog["provision"]["state_root_env"] = json!("TOOL_CONFIG_DIR");
        catalog["provision"]["auth_env"] = json!("TOOL_AUTH_PATH");
        let home = gb_environment::GUEST_HOME;
        let root = format!("{home}/.tool");
        for (files, first) in [(credentials("synthetic"), false), (Files::new(), true)] {
            for interactive in [true, false] {
                let plan = private_plan(
                    &vm,
                    &catalog,
                    files.clone(),
                    vec!["tool".into()],
                    interactive,
                )
                .unwrap();
                assert_eq!(plan.home, home);
                assert_eq!(plan.root, root);
                // A first login puts no file there, and the tool still finds
                // its state root.
                for (mount, guest) in plan.mounts.iter().zip([home, root.as_str()]) {
                    assert_eq!(mount.guest, guest);
                    assert!(mount.host.is_none() && !mount.readonly);
                }
                assert_eq!(plan.mounts[2].host.as_ref(), Some(&vm.package));
                assert_eq!(plan.process().cwd, home);
                let env = plan.env.iter().cloned().collect::<BTreeMap<_, _>>();
                assert_eq!(env["HOME"], home);
                assert_eq!(env["TOOL_CONFIG_DIR"], root);
                assert_eq!(env["TOOL_AUTH_PATH"], format!("{root}/auth.json"));
                assert_eq!(
                    plan.injection.contains_key(&format!("{root}/auth.json")),
                    !first
                );
                assert_eq!(plan.capture[0], format!("{root}/auth.json"));
            }
        }
    }
    #[test]
    fn private_login_and_probe_plans_preserve_exact_process_and_boot_contracts() {
        let root = tempfile::tempdir().unwrap();
        let vm = LoginVm {
            data: root.path().join("data"),
            package: root.path().join("package"),
            snapshot: root.path().join("expected-snapshot"),
            memory_mib: 777,
            cpus: 3,
            nameservers: vec!["192.0.2.53".into()],
        };
        let mut catalog = catalog();
        catalog["provision"]["state_root_env"] = json!("TOOL_CONFIG_DIR");
        catalog["provision"]["auth_env"] = json!("TOOL_AUTH_PATH");
        for interactive in [true, false] {
            let argv = vec![
                "tool".into(),
                if interactive {
                    "login".into()
                } else {
                    "usage".into()
                },
            ];
            let files = credentials("synthetic-real-login");
            let plan =
                private_plan(&vm, &catalog, files.clone(), argv.clone(), interactive).unwrap();
            let runtime_home = vm.data.join("msb");
            let boot = plan.boot(&vm, &runtime_home);
            let process = plan.process();
            assert_eq!(boot.home, runtime_home);
            assert_eq!(boot.snapshot, vm.snapshot);
            assert_eq!((boot.memory_mib, boot.cpus), (777, 3));
            assert!(boot.worktree.is_none());
            let home = gb_environment::GUEST_HOME;
            for (mount, guest) in boot.mounts.iter().zip([home, &format!("{home}/.tool")]) {
                assert_eq!(mount.guest, guest);
                assert!(mount.host.is_none() && !mount.readonly);
            }
            assert_eq!(boot.mounts[2].host.as_deref(), Some(vm.package.as_path()));
            assert!(boot.mounts[2].readonly);
            assert_eq!(plan.interactive, interactive);
            assert_eq!(boot.network.unrestricted, interactive);
            assert_eq!(boot.network.nameservers, ["192.0.2.53"]);
            assert_eq!(process.argv, argv);
            assert_eq!(process.cwd, home);
            assert!(process.stdin.is_empty());
            let env = process.env.iter().cloned().collect::<BTreeMap<_, _>>();
            assert_eq!(env["HOME"], home);
            assert_eq!(env["TOOL_CONFIG_DIR"], format!("{home}/.tool"));
            assert_eq!(env["TOOL_AUTH_PATH"], format!("{home}/.tool/auth.json"));
            assert_eq!(env["AGNO_TELEMETRY"], "false");
            assert_eq!(env["PYTHONPATH"], gb_environment::CODE_ROOT);
            assert_eq!(
                plan.injection,
                Files::from([(
                    format!("{home}/.tool/auth.json"),
                    files["auth.json"].clone()
                )])
            );
            assert_eq!(plan.capture.len(), if interactive { 2 } else { 1 });
        }
    }
    #[test]
    fn completed_usage_requires_windows_or_explicit_gate_before_clearing_authentication_failure() {
        for (reply, succeeded) in [
            (b"{".as_slice(), false),
            (br#"{"windows":[],"limit_reached":false}"#.as_slice(), false),
            (
                br#"{"windows":{},"limit_reached":"true"}"#.as_slice(),
                false,
            ),
            (br#"{"windows":[],"limit_reached":true}"#.as_slice(), true),
            (
                br#"{"windows":[{"remaining":50}],"limit_reached":false}"#.as_slice(),
                true,
            ),
        ] {
            let root = tempfile::tempdir().unwrap();
            let device = DeviceKey {
                machine_root: root.path().into(),
                keychain: &FakeKeychain,
            };
            authentication_outcome(root.path(), "tool", true).unwrap();
            let before = credentials("synthetic-before");
            let after = credentials("synthetic-rotated");
            complete_probe(&device, root.path(), &catalog(), "tool", 0, &before, &after).unwrap();
            let result = complete_usage(root.path(), "tool", reply);
            assert_eq!(result.is_ok(), succeeded);
            assert_eq!(
                root.path()
                    .join("agent_environment/tool/authentication-failed")
                    .exists(),
                !succeeded
            );
            assert_eq!(
                access(
                    &credential_vault::unseal(
                        &device,
                        &path(root.path(), "tool"),
                        "tool:default:test"
                    )
                    .unwrap()
                    .unwrap(),
                    &catalog()
                )
                .unwrap(),
                "synthetic-rotated"
            );
        }
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
    pub package: PathBuf,
    pub snapshot: PathBuf,
    pub memory_mib: u32,
    pub cpus: u8,
    pub nameservers: Vec<String>,
}
fn spawn_private<T: Send + 'static>(
    tasks: &tokio_util::task::TaskTracker,
    task: impl std::future::Future<Output = T> + Send + 'static,
) -> tokio::task::JoinHandle<T> {
    tasks.spawn(task)
}
fn private_tasks() -> &'static tokio_util::task::TaskTracker {
    static TASKS: std::sync::OnceLock<tokio_util::task::TaskTracker> = std::sync::OnceLock::new();
    TASKS.get_or_init(tokio_util::task::TaskTracker::new)
}
/// Shutdown waits for token capture and resealing, even if the HTTP caller
/// went away while the login CLI consumed a one-use refresh token.
pub async fn drain_private() {
    drain_private_tasks(private_tasks(), std::time::Duration::from_secs(5)).await;
}
async fn drain_private_tasks(tasks: &tokio_util::task::TaskTracker, grace: std::time::Duration) {
    tasks.close();
    // Boot or provider I/O may never resolve. Give all credential writers one
    // shared grace period before VM shutdown continues.
    let _ = tokio::time::timeout(grace, tasks.wait()).await;
}
async fn lent_access<F, Fut>(
    login: &mut Held,
    catalog: &Value,
    refused: Option<String>,
    refresh: F,
) -> Result<String, &'static str>
where
    F: FnOnce(String) -> Fut,
    Fut: std::future::Future<Output = Result<Files, String>>,
{
    if let Some(reason) = login.refusal {
        return Err(reason);
    }
    let token = access(&login.files, catalog).map_err(|_| {
        login.refusal = Some("credentials_corrupt");
        "credentials_corrupt"
    })?;
    if refused.as_ref().is_some_and(|refused| refused != &token)
        || (refused.is_none() && !expiring(&login.files, catalog))
    {
        return Ok(token);
    }
    if refused.is_some()
        && (login.refreshed
            || catalog["provision"]["credential_broker"]["refresh"]
                .as_array()
                .is_none_or(|v| v.is_empty()))
    {
        let key = if login.refreshed {
            "refresh_failed"
        } else {
            "login_refused"
        };
        login.refusal = Some(key);
        return Err(key);
    }
    login.refreshed = true;
    match refresh(token).await {
        Ok(files) => {
            login.files = files;
            access(&login.files, catalog).map_err(|_| {
                login.refusal = Some("credentials_corrupt");
                "credentials_corrupt"
            })
        }
        Err(_) => {
            login.refusal = Some("refresh_failed");
            Err("refresh_failed")
        }
    }
}
async fn hold_login(path: PathBuf) -> Result<gb_base::files::FileLock, String> {
    tokio::task::spawn_blocking(move || {
        credential_vault::hold(&path, std::time::Duration::from_secs(270))
    })
    .await
    .map_err(|_| "The login lock failed.")?
    .map_err(|_| "The login is busy.".into())
}
enum PrivateError {
    Start(String),
    Capture(String),
}
impl From<String> for PrivateError {
    fn from(error: String) -> Self {
        Self::Start(error)
    }
}
impl From<&str> for PrivateError {
    fn from(error: &str) -> Self {
        Self::Start(error.into())
    }
}
fn private_result<T>(
    result: Result<T, PrivateError>,
    data: &Path,
    tool: &str,
    interactive: bool,
) -> Result<T, String> {
    result.map_err(|error| match error {
        PrivateError::Start(error) => error,
        PrivateError::Capture(error) => {
            if !interactive && let Err(marker) = authentication_outcome(data, tool, true) {
                return marker;
            }
            error
        }
    })
}
async fn refresh_login<F, Fut>(
    device: &DeviceKey<'_>,
    data: &Path,
    name: &str,
    catalog: &Value,
    stale: &str,
    run: F,
) -> Result<Files, String>
where
    F: FnOnce(Files) -> Fut,
    Fut: std::future::Future<Output = Result<(i32, Files, Vec<u8>), PrivateError>>,
{
    let vault = path(data, name);
    let _held = hold_login(vault.clone()).await?;
    let label = label(
        name,
        catalog["provision"]["credential_broker"]["format"]
            .as_str()
            .ok_or("Credential format missing.")?,
    );
    let files = credential_vault::unseal(device, &vault, &label)
        .map_err(|_| "The login cannot be opened.")?
        .ok_or("Credentials missing.")?;
    if access(&files, catalog)? != stale && !expiring(&files, catalog) {
        return Ok(files);
    }
    let handed = expire(files, catalog)?;
    let (_, files, _) = private_result(run(handed.clone()).await, data, name, false)?;
    let captured = Ok(files);
    if let Ok(files) = &captured
        && access(files, catalog).is_ok()
        && !refresh_changed(files, &handed, catalog)
    {
        return Err("The provider did not refresh the login.".into());
    }
    keep_capture(device, data, name, catalog, captured)
}
fn refresh_changed(files: &Files, handed: &Files, catalog: &Value) -> bool {
    access(files, catalog)
        .ok()
        .zip(access(handed, catalog).ok())
        .is_some_and(|(after, before)| after != before)
}
fn keep_capture(
    device: &DeviceKey<'_>,
    data: &Path,
    name: &str,
    catalog: &Value,
    captured: Result<Files, String>,
) -> Result<Files, String> {
    let result = seal_capture(device, data, name, catalog, captured);
    if result.is_err() {
        authentication_outcome(data, name, true)?;
    }
    result
}
fn seal_capture(
    device: &DeviceKey<'_>,
    data: &Path,
    name: &str,
    catalog: &Value,
    captured: Result<Files, String>,
) -> Result<Files, String> {
    let files = captured?;
    access(&files, catalog)?;
    let auth = catalog["provision"]["auth"]
        .as_str()
        .ok_or("Tool auth file missing.")?;
    let files = Files::from([(auth.into(), files[auth].clone())]);
    credential_vault::seal(
        device,
        &path(data, name),
        &label(
            name,
            catalog["provision"]["credential_broker"]["format"]
                .as_str()
                .ok_or("Credential format missing.")?,
        ),
        &files,
    )
    .map_err(|_| "The refreshed login could not be sealed.")?;
    Ok(files)
}
fn complete_probe(
    device: &DeviceKey<'_>,
    data: &Path,
    catalog: &Value,
    tool: &str,
    code: i32,
    before: &Files,
    files: &Files,
) -> Result<(), String> {
    if access(before, catalog)
        .ok()
        .zip(access(files, catalog).ok())
        .is_none_or(|(before, after)| before != after)
    {
        keep_capture(device, data, tool, catalog, Ok(files.clone()))?;
    }
    if code != 0 {
        return Err("The provider login or usage probe failed.".into());
    }
    Ok(())
}
fn complete_usage(data: &Path, tool: &str, reply: &[u8]) -> Result<Value, String> {
    let snapshot: Value =
        serde_json::from_slice(reply).map_err(|_| "The usage probe did not return JSON.")?;
    if snapshot["windows"]
        .as_array()
        .is_none_or(|windows| windows.is_empty())
        && snapshot["limit_reached"] != true
    {
        return Err("The usage probe reported no usage windows.".into());
    }
    authentication_outcome(data, tool, false)?;
    Ok(snapshot)
}
fn complete_login(
    device: &DeviceKey<'_>,
    data: &Path,
    catalog: &Value,
    tool: &str,
    code: i32,
    files: &Files,
    language: &str,
) -> Result<(), String> {
    if code != 0 {
        return Err("The provider login or usage probe failed.".into());
    }
    access(files, catalog)
        .map_err(|_| login_problem(catalog, "login_not_an_account", "", language))?;
    seal_capture(device, data, tool, catalog, Ok(files.clone()))?;
    keep_persisted(data, tool, catalog, files)?;
    authentication_outcome(data, tool, false)?;
    crate::environment::forget_usage(data, tool);
    Ok(())
}

/// Interactive login starts afresh; only a usage probe opens the existing login.
async fn login_operation<F, Fut>(
    device: &DeviceKey<'_>,
    data: &Path,
    catalog: &Value,
    tool: &str,
    usage: bool,
    language: &str,
    run: F,
) -> Result<Value, String>
where
    F: FnOnce(Files) -> Fut,
    Fut: std::future::Future<Output = Result<(i32, Files, Vec<u8>), PrivateError>>,
{
    let vault = path(data, tool);
    let _held = if usage {
        Some(hold_login(vault.clone()).await?)
    } else {
        None
    };
    let files = if usage {
        let label = label(
            tool,
            catalog["provision"]["credential_broker"]["format"]
                .as_str()
                .ok_or("Tool has no credential broker.")?,
        );
        let files = credential_vault::unseal(device, &vault, &label)
            .map_err(|_| "The login cannot be opened.")?
            .unwrap_or_default();
        access(&files, catalog)?;
        files
    } else {
        Files::new()
    };
    let before = files.clone();
    let (code, files, reply) = private_result(run(files).await, data, tool, !usage)?;
    // A probe can rotate a refresh token before returning a failing exit code.
    if usage {
        complete_probe(device, data, catalog, tool, code, &before, &files)?;
        complete_usage(data, tool, &reply)
    } else {
        if code != 0 {
            return Err("The provider login or usage probe failed.".into());
        }
        access(&files, catalog)
            .map_err(|_| login_problem(catalog, "login_not_an_account", "", language))?;
        let _held = hold_login(vault).await?;
        complete_login(device, data, catalog, tool, code, &files, language)?;
        Ok(json!({"tool":tool,"saved":true}))
    }
}

fn private_network(
    catalog: &Value,
    nameservers: &[String],
    interactive: bool,
) -> gb_environment::runtime::Network {
    gb_environment::runtime::Network {
        unrestricted: interactive,
        domains: if interactive {
            vec![]
        } else {
            catalog["provision"]["api_domains"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        },
        host_ports: vec![],
        local_network: false,
        nameservers: nameservers.to_vec(),
    }
}
fn capture_names(catalog: &Value, interactive: bool) -> Result<Vec<String>, String> {
    let mut names = vec![
        catalog["provision"]["auth"]
            .as_str()
            .ok_or("Tool auth file missing.")?
            .to_owned(),
    ];
    if interactive {
        names.extend(
            catalog["provision"]["persisted"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .filter(|s| !s.ends_with('/'))
                .map(str::to_owned),
        );
    }
    Ok(names)
}
fn keep_persisted(data: &Path, name: &str, catalog: &Value, files: &Files) -> Result<(), String> {
    let root = data.join("agent_environment").join(name).join(
        catalog["provision"]["state_root"]
            .as_str()
            .ok_or("Tool state root missing.")?,
    );
    for name in capture_names(catalog, true)?.into_iter().skip(1) {
        if !Path::new(&name)
            .components()
            .all(|c| matches!(c, std::path::Component::Normal(_)))
        {
            return Err("Invalid persisted tool path.".into());
        }
        if let Some(bytes) = files.get(&name) {
            let target = root.join(name);
            persisted_file(&target)?;
            gb_base::files::atomic_write(&target, bytes)
                .map_err(|_| "Cannot keep persisted tool settings.")?;
        }
    }
    Ok(())
}
struct PrivatePlan {
    root: String,
    home: String,
    env: Vec<(String, String)>,
    mounts: Vec<gb_environment::Mount>,
    network: gb_environment::runtime::Network,
    injection: Files,
    capture: Vec<String>,
    argv: Vec<String>,
    interactive: bool,
}
impl PrivatePlan {
    fn boot<'a>(
        &'a self,
        vm: &'a LoginVm,
        runtime_home: &'a Path,
    ) -> gb_environment::runtime::Boot<'a> {
        gb_environment::runtime::Boot {
            home: runtime_home,
            snapshot: &vm.snapshot,
            memory_mib: vm.memory_mib,
            cpus: vm.cpus,
            mounts: &self.mounts,
            network: &self.network,
            worktree: None,
        }
    }
    fn process(&self) -> gb_environment::runtime::Process<'_> {
        gb_environment::runtime::Process {
            argv: &self.argv,
            cwd: &self.home,
            env: &self.env,
            stdin: b"",
        }
    }
}
/// Where a tool keeps its state inside a microVM: under the microVM's home.
fn state_root(provision: &Value) -> Result<String, String> {
    let state = provision["state_root"]
        .as_str()
        .ok_or("Tool state root missing.")?;
    Ok(format!("{}/{state}", gb_environment::GUEST_HOME))
}
fn private_plan(
    vm: &LoginVm,
    catalog: &Value,
    files: Files,
    argv: Vec<String>,
    interactive: bool,
) -> Result<PrivatePlan, String> {
    let provision = &catalog["provision"];
    let home = gb_environment::GUEST_HOME.to_owned();
    let root = state_root(provision)?;
    let mut env = vec![
        ("HOME".into(), home.clone()),
        ("AGNO_TELEMETRY".into(), "false".into()),
        ("PYTHONPATH".into(), gb_environment::CODE_ROOT.into()),
    ];
    if let Some(name) = provision["state_root_env"]
        .as_str()
        .filter(|s| !s.is_empty())
    {
        env.push((name.into(), root.clone()));
    }
    if let Some(name) = provision["auth_env"].as_str().filter(|s| !s.is_empty()) {
        env.push((
            name.into(),
            format!("{root}/{}", provision["auth"].as_str().unwrap_or("")),
        ));
    }
    // The home and the tool's state root are the microVM's own, in memory:
    // the root exists even when no file is put in it, as on a first login.
    let mounts = vec![
        gb_environment::Mount {
            guest: home.clone(),
            host: None,
            readonly: false,
            quota_mib: None,
        },
        gb_environment::Mount {
            guest: root.clone(),
            host: None,
            readonly: false,
            quota_mib: None,
        },
        gb_environment::Mount {
            guest: gb_environment::CODE_ROOT.into(),
            host: Some(vm.package.clone()),
            readonly: true,
            quota_mib: None,
        },
    ];
    let network = private_network(catalog, &vm.nameservers, interactive);
    let injection = files
        .into_iter()
        .map(|(name, value)| (format!("{root}/{name}"), value))
        .collect();
    let capture = capture_names(catalog, interactive)?
        .iter()
        .map(|name| format!("{root}/{name}"))
        .collect::<Vec<_>>();
    Ok(PrivatePlan {
        root,
        home,
        env,
        mounts,
        network,
        injection,
        capture,
        argv,
        interactive,
    })
}
async fn private_run(
    vm: LoginVm,
    catalog: Value,
    files: Files,
    argv: Vec<String>,
    show: bool,
    cancelled: tokio_util::sync::CancellationToken,
) -> Result<(i32, Files, Vec<u8>), PrivateError> {
    let plan = private_plan(&vm, &catalog, files, argv, show)?;
    let runtime_home = vm.data.join("msb");
    let boot = plan.boot(&vm, &runtime_home);
    let process = plan.process();
    let output = move |chunk: &[u8]| {
        if show {
            use std::io::Write;
            let _ = std::io::stdout().write_all(chunk);
            let _ = std::io::stdout().flush();
        }
    };
    let result = if plan.interactive {
        gb_environment::runtime::run_private_login(
            &boot,
            &process,
            &plan.injection,
            &plan.capture,
            output,
        )
        .await
    } else {
        gb_environment::runtime::run_private_session_cancelled(
            &boot,
            &process,
            &plan.injection,
            &plan.capture,
            output,
            cancelled.cancelled_owned(),
        )
        .await
    }
    .map_err(|_| "The private credential environment could not start.")?;
    let captured = result.files.map_err(|_| {
        PrivateError::Capture(
            "The private credential environment could not return its login.".into(),
        )
    })?;
    let kept = captured
        .into_iter()
        .filter_map(|(path, value)| {
            path.strip_prefix(&format!("{}/", plan.root))
                .map(|relative| (relative.to_owned(), value))
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
    login_with_cancellation(
        host,
        workspace,
        tool,
        usage,
        tokio_util::sync::CancellationToken::new(),
    )
    .await
}
pub async fn login_usage(
    host: &gb_environment::Host,
    workspace: &Path,
    tool: &str,
    cancelled: tokio_util::sync::CancellationToken,
) -> Result<Value, String> {
    login_with_cancellation(host, workspace, tool, true, cancelled).await
}
async fn login_with_cancellation(
    host: &gb_environment::Host,
    workspace: &Path,
    tool: &str,
    usage: bool,
    cancelled: tokio_util::sync::CancellationToken,
) -> Result<Value, String> {
    if usage && cancelled.is_cancelled() {
        return Err("The usage probe was cancelled.".into());
    }
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
    let language = std::fs::read_to_string(workspace.join(".guildbotics/config/team/project.yml"))
        .ok()
        .and_then(|text| gb_config::yaml_value(&text).ok())
        .and_then(|value| value["language"].as_str().map(crate::i18n::language))
        .unwrap_or("en");
    let tool = tool.to_owned();
    let task = spawn_private(private_tasks(), async move {
        let keychain = SystemKeychain;
        let device = DeviceKey {
            machine_root: vm.data.parent().unwrap_or(&vm.data).into(),
            keychain: &keychain,
        };
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
        login_operation(
            &device,
            &vm.data,
            &catalog,
            &tool,
            usage,
            language,
            |files| private_run(vm.clone(), catalog.clone(), files, argv, !usage, cancelled),
        )
        .await
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
        .and_then(|raw| credential_document(raw).ok())
        .and_then(|document| expiry(&document, broker))
        .is_none_or(|seconds| seconds < chrono::Utc::now().timestamp() as f64 + 300.0)
}
fn expire(mut files: Files, catalog: &Value) -> Result<Files, String> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    let broker = &catalog["provision"]["credential_broker"];
    let auth = catalog["provision"]["auth"]
        .as_str()
        .ok_or("Auth file missing.")?;
    let mut document = credential_document(files.get(auth).ok_or("Credentials missing.")?)?;
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
        authentication_outcome(&self.data, &self.name, failed)
    }
}
fn authentication_outcome(data: &Path, name: &str, failed: bool) -> Result<(), String> {
    let marker = data
        .join("agent_environment")
        .join(name)
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
