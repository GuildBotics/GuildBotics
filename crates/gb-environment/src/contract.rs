//! Resolve the shared/local filesystem contract before creating any directory.
use crate::{Mount, Sentence};
use serde::{Deserialize, Serialize};
use std::path::{Component, Path, PathBuf};

const FILE: &str = "cli_agent_filesystem_grants.yml";
const SENSITIVE: &[&str] = &[
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".docker",
    ".config/gh",
    ".local/share/keyrings",
    ".guildbotics",
    ".codex",
    ".claude",
    ".grok",
    ".copilot",
    ".gemini",
    ".agents",
];

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    pub path: String,
    pub access: Access,
}
#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Access {
    Read,
    ReadWrite,
}
#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Shared {
    #[serde(default)]
    pub documents: Vec<Grant>,
}
#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Local {
    #[serde(default)]
    pub paths: Vec<Grant>,
    #[serde(default)]
    pub deny: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Resolved {
    pub grants: Vec<Mount>,
    pub denied: Vec<PathBuf>,
    pub view: serde_json::Value,
}

/// A proposed edit's refusal, with the runtime's text and the editor's locale
/// sentence. It never includes credential contents.
#[derive(Debug)]
pub struct GrantProblem {
    text: String,
    pub sentence: Sentence,
}
impl std::fmt::Display for GrantProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.text)
    }
}
impl From<String> for GrantProblem {
    fn from(text: String) -> Self {
        Self {
            sentence: Sentence::Text(text.clone()),
            text,
        }
    }
}
impl From<&str> for GrantProblem {
    fn from(text: &str) -> Self {
        text.to_owned().into()
    }
}
fn grant_problem(key: &'static str, name: &str, text: impl Into<String>) -> GrantProblem {
    GrantProblem {
        text: text.into(),
        sentence: Sentence::Key(key, vec![("path", name.into())]),
    }
}
fn proposed_target(name: &str, home: &Path, absolute: bool) -> Result<PathBuf, GrantProblem> {
    if !absolute
        && (Path::new(name).is_absolute()
            || name.starts_with("\\\\")
            || (name.as_bytes().get(1) == Some(&b':')
                && matches!(name.as_bytes().get(2), Some(b'/' | b'\\'))))
    {
        return Err(grant_problem(
            "intelligences.agent_environment.grants.path_not_relative",
            name,
            format!("'{name}' is not a directory name"),
        ));
    }
    target(name, home, absolute).map_err(|text| {
        grant_problem(
            "intelligences.agent_environment.grants.path_not_a_directory_name",
            name,
            text,
        )
    })
}

/// Validate file spellings without requiring any of their directories to exist.
pub fn validate_names(shared: &Shared, local: &Local, home: &Path) -> Result<(), GrantProblem> {
    for grant in &shared.documents {
        proposed_target(&grant.path, home, false)?;
    }
    for name in local.paths.iter().map(|g| &g.path).chain(local.deny.iter()) {
        proposed_target(name, home, true)?;
    }
    Ok(())
}

fn load<T: serde::de::DeserializeOwned + Default>(path: &Path) -> Result<T, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => gb_config::yaml_value(&text)
            .map_err(|e| e.to_string())
            .and_then(|v| {
                if v.is_null() {
                    Ok(T::default())
                } else if v.is_object() {
                    serde_json::from_value(v).map_err(|e| e.to_string())
                } else {
                    Err(format!("{}: grants must be a mapping", path.display()))
                }
            }),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}
fn target(name: &str, home: &Path, absolute: bool) -> Result<PathBuf, String> {
    let path = Path::new(name);
    if name
        .replace('\\', "/")
        .trim_matches('/')
        .split('/')
        .any(|s| s.is_empty() || s == "." || s == "..")
        || (!absolute && name.contains('\\'))
        || name.contains("//")
        || name.contains('\0')
        || (!absolute && path.is_absolute())
        || path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
        || name
            .split('/')
            .filter(|s| !s.is_empty())
            .any(|s| s == "." || s == "..")
    {
        return Err(format!("'{name}' is not a directory name"));
    }
    gb_base::host_path::normalize(&if path.is_absolute() {
        path.to_path_buf()
    } else {
        home.join(path)
    })
    .map_err(|e| e.to_string())
}

/// A workspace may be selected to repair an invalid grant file, but may not
/// itself live inside a directory that file would grant to its commands.
pub fn workspace_location(path: &Path, home: &Path) -> Result<PathBuf, Sentence> {
    location(path, home, false)
}

/// As [`workspace_location`], for a workspace about to be created: its name
/// may end in directories that do not exist yet.
pub fn new_workspace_location(path: &Path, home: &Path) -> Result<PathBuf, Sentence> {
    location(path, home, true)
}

fn location(path: &Path, home: &Path, missing: bool) -> Result<PathBuf, Sentence> {
    let home =
        gb_base::host_path::normalize(home).map_err(|error| Sentence::Text(error.to_string()))?;
    let workspace = gb_base::host_path::directory(path, missing).map_err(|error| match error {
        gb_base::host_path::PathProblem::Unavailable(path, reason)
            if reason.kind() == std::io::ErrorKind::PermissionDenied =>
        {
            let mut args = vec![("path", path.display().to_string())];
            let key = if cfg!(target_os = "macos") && path.starts_with(home.join("Documents")) {
                args.push(("app", String::new()));
                "intelligences.agent_environment.filesystem.macos_documents"
            } else {
                "intelligences.agent_environment.filesystem.permission_denied"
            };
            Sentence::Key(key, args)
        }
        error => Sentence::Text(error.to_string()),
    })?;
    let shared: Shared = load(
        &workspace
            .join(".guildbotics/config/intelligences")
            .join(FILE),
    )
    .unwrap_or_default();
    let local: Local = load(&workspace.join(".guildbotics/local").join(FILE)).unwrap_or_default();
    let grants = std::iter::once(home.join(crate::EXCHANGE))
        .chain(
            shared
                .documents
                .into_iter()
                .filter_map(|grant| target(&grant.path, &home, false).ok()),
        )
        .chain(
            local
                .paths
                .into_iter()
                .filter_map(|grant| target(&grant.path, &home, true).ok()),
        );
    for grant in grants {
        if workspace_in_grant(&workspace, &grant) {
            return Err(Sentence::Key(
                "safe_paths.workspace_granted",
                vec![
                    ("path", workspace.display().to_string()),
                    ("granted", grant.display().to_string()),
                ],
            ));
        }
    }
    Ok(workspace)
}

fn workspace_in_grant(workspace: &Path, grant: &Path) -> bool {
    if cfg!(windows) {
        return windows_path_contains(grant, workspace);
    }
    // Compare at each level before inspecting the next grant component. This
    // also handles case-insensitive volumes without opening unrelated grants.
    let mut prefix = PathBuf::new();
    for (component, ancestor) in grant
        .components()
        .zip(workspace.ancestors().collect::<Vec<_>>().into_iter().rev())
    {
        prefix.push(component);
        if prefix != ancestor && !same_file::is_same_file(&prefix, ancestor).unwrap_or(false) {
            return false;
        }
    }
    grant.components().count() <= workspace.components().count()
}

/// A host-owned precise child may be mounted read-only; user grants must
/// neither contain protected state nor be contained by it.
pub fn validate_source(
    path: &Path,
    denied: &[PathBuf],
    user: bool,
    missing: bool,
) -> Result<PathBuf, String> {
    let source = gb_base::host_path::directory(path, missing).map_err(|e| e.to_string())?;
    for protected in denied {
        if protected.starts_with(&source) || (user && source.starts_with(protected)) {
            return Err(format!(
                "{} exposes protected directory {}",
                source.display(),
                protected.display()
            ));
        }
    }
    Ok(source)
}

/// Runtime-owned state may be a precise file or directory, reached through
/// no-follow directory capabilities. Precise host-owned state remains open
/// under a denied ancestor, as the declaration contract requires.
pub fn validate_mount_source(path: &Path, access: &Resolved) -> Result<PathBuf, String> {
    use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
    use cap_std::fs::{Dir, OpenOptions};
    let source = gb_base::host_path::normalize(path).map_err(|e| e.to_string())?;
    for protected in &access.denied {
        if protected.starts_with(&source) {
            return Err(format!(
                "{} exposes protected directory {}",
                source.display(),
                protected.display()
            ));
        }
    }
    let mut anchor = PathBuf::new();
    let mut parts = Vec::new();
    for part in source.components() {
        match part {
            Component::Prefix(_) | Component::RootDir => anchor.push(part),
            Component::Normal(name) => parts.push(name),
            _ => return Err("Invalid mount source".into()),
        }
    }
    let mut directory =
        Dir::open_ambient_dir(anchor, cap_std::ambient_authority()).map_err(|e| e.to_string())?;
    let (leaf, parents) = parts.split_last().ok_or("Cannot mount a filesystem root")?;
    for parent in parents {
        directory = directory
            .open_dir_nofollow(parent)
            .map_err(|e| e.to_string())?;
    }
    let kind = directory
        .symlink_metadata(leaf)
        .map_err(|e| e.to_string())?;
    if kind.is_dir() {
        directory
            .open_dir_nofollow(leaf)
            .map_err(|e| e.to_string())?;
    } else if kind.is_file() {
        let mut options = OpenOptions::new();
        options.read(true).follow(FollowSymlinks::No);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NONBLOCK);
        }
        let file = directory
            .open_with(leaf, &options)
            .map_err(|e| e.to_string())?;
        if !file.metadata().map_err(|e| e.to_string())?.is_file() {
            return Err("Mount source is not a regular file".into());
        }
    } else {
        return Err("Mount source is not a regular file or directory".into());
    }
    Ok(source)
}

pub fn resolve(
    workspace: &Path,
    home: &Path,
    registered: &[PathBuf],
    read_only: bool,
    create: bool,
) -> Result<Resolved, String> {
    resolve_with_probe(workspace, home, registered, read_only, create, |path| {
        for entry in std::fs::read_dir(path).map_err(|e| format!("{}: {e}", path.display()))? {
            entry.map_err(|e| format!("{}: {e}", path.display()))?;
        }
        Ok(())
    })
}

fn resolve_with_probe(
    workspace: &Path,
    home: &Path,
    registered: &[PathBuf],
    read_only: bool,
    create: bool,
    probe: impl Fn(&Path) -> Result<(), String>,
) -> Result<Resolved, String> {
    let shared: Shared = load(
        &workspace
            .join(".guildbotics/config/intelligences")
            .join(FILE),
    )?;
    let local: Local = load(&workspace.join(".guildbotics/local").join(FILE))?;
    resolve_values(
        workspace, home, registered, shared, local, read_only, create, probe,
    )
    .map_err(|e| e.to_string())
}

/// Resolve a proposed edit with the same validation as a command, without
/// writing it to the workspace first.
#[allow(clippy::too_many_arguments)]
pub fn resolve_values(
    workspace: &Path,
    home: &Path,
    registered: &[PathBuf],
    shared: Shared,
    local: Local,
    read_only: bool,
    create: bool,
    probe: impl Fn(&Path) -> Result<(), String>,
) -> Result<Resolved, GrantProblem> {
    let home = gb_base::host_path::directory(home, false).map_err(|e| e.to_string())?;
    let mut denied: Vec<_> = SENSITIVE.iter().map(|s| home.join(s)).collect();
    denied.push(workspace.join(".guildbotics"));
    denied.extend(registered.iter().map(|p| p.join(".guildbotics")));
    let builtin_count = denied.len();
    for name in local.deny {
        let p = proposed_target(&name, &home, true)?;
        if p == home || p.parent().is_none() {
            return Err(grant_problem(
                "intelligences.agent_environment.grants.deny_too_broad",
                &name,
                "Deny is too broad",
            ));
        }
        denied.push(p);
    }
    let mut planned = vec![(
        home.join(crate::EXCHANGE),
        Access::ReadWrite,
        true,
        crate::EXCHANGE.to_owned(),
        true,
    )];
    for grant in shared.documents {
        let p = proposed_target(&grant.path, &home, false)?;
        if p != home.join(crate::EXCHANGE) {
            planned.push((p, grant.access, true, grant.path, false));
        }
    }
    for grant in local.paths {
        let p = proposed_target(&grant.path, &home, true)?;
        if p == home || p.parent().is_none() {
            return Err(grant_problem(
                "intelligences.agent_environment.grants.local_path_too_broad",
                &grant.path,
                "Local path is too broad",
            ));
        }
        planned.push((p, grant.access, false, grant.path, false));
    }
    let mut expanded = Vec::new();
    let denied_view: Vec<_> = denied
        .iter()
        .enumerate()
        .map(|(i, path)| serde_json::json!({"path":path,"builtin":i<builtin_count}))
        .collect();
    for path in &denied {
        expanded.extend(protected_names(path));
    }
    denied = expanded;
    // Validate the whole population before creating even the built-in exchange.
    for (path, _, document, _, _) in &planned {
        let source =
            gb_base::host_path::directory(path, *document || !create).map_err(|e| e.to_string())?;
        for protected in &denied {
            if protected.starts_with(&source) || source.starts_with(protected) {
                let key = if protected.starts_with(workspace.join(".guildbotics"))
                    || registered
                        .iter()
                        .any(|p| protected.starts_with(p.join(".guildbotics")))
                {
                    "safe_paths.protected_workspace"
                } else if SENSITIVE
                    .iter()
                    .any(|s| protected.starts_with(home.join(s)))
                {
                    "safe_paths.protected_credentials"
                } else {
                    "safe_paths.protected_local"
                };
                return Err(GrantProblem {
                    text: format!(
                        "{} exposes protected directory {}",
                        source.display(),
                        protected.display()
                    ),
                    sentence: Sentence::Key(
                        key,
                        vec![
                            ("path", source.display().to_string()),
                            ("protected", protected.display().to_string()),
                        ],
                    ),
                });
            }
        }
    }
    for (path, _, document, _, _) in &planned {
        let mut present = path.as_path();
        while !present.exists() && *document {
            present = present.parent().ok_or("Grant has no existing parent")?;
        }
        if present.exists() {
            probe(present)?;
        }
    }
    let mut grants = Vec::new();
    let mut documents = Vec::new();
    let mut paths = Vec::new();
    // The exchange folder, the first of the plan when it is there.
    let mut exchange = None;
    for (path, access, document, grant, builtin) in planned {
        if create && document {
            std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
        }
        let present = path.exists();
        let row = serde_json::json!({"path":path,"grant":grant,"access":if matches!(access,Access::Read){"read"}else{"read_write"},"present":present,"builtin":builtin});
        if document {
            documents.push(row);
        } else {
            paths.push(row);
        }
        if !present {
            continue;
        }
        let checked = validate_source(&path, &denied, true, false)?;
        let mount = Mount {
            guest: crate::guest_path(&checked).map_err(|_| "Invalid guest path")?,
            host: Some(checked),
            readonly: read_only || matches!(access, Access::Read),
            quota_mib: None,
        };
        if builtin {
            exchange = mount.host.clone();
        }
        // What is in the exchange folder is mounted again under its `~`
        // spelling too, with the same access, so no grant is looser there.
        let again = exchange
            .as_ref()
            .and_then(|root| mount.host.as_ref()?.strip_prefix(root).ok())
            .map(|inner| Mount {
                guest: std::iter::once(crate::guest_exchange())
                    .chain(inner.iter().map(|part| part.to_string_lossy().into_owned()))
                    .collect::<Vec<_>>()
                    .join("/"),
                ..mount.clone()
            });
        grants.push(mount);
        grants.extend(again);
    }
    Ok(Resolved {
        grants,
        denied,
        view: serde_json::json!({"documents":documents,"paths":paths,"denied":denied_view,"problem":""}),
    })
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Network {
    #[serde(default)]
    pub mode: Mode,
    #[serde(default)]
    pub allowed_domains: Vec<String>,
    #[serde(default)]
    pub allow_local_network: bool,
}
#[derive(Debug, Clone, Copy, Default, PartialEq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    #[default]
    Deny,
    Allowlist,
    Unrestricted,
}
impl Network {
    pub fn validate(&self) -> Result<(), String> {
        if (self.mode == Mode::Allowlist) == self.allowed_domains.is_empty()
            || self
                .allowed_domains
                .iter()
                .any(|d| d.is_empty() || d.contains('/') || d.chars().any(char::is_whitespace))
        {
            return Err("Invalid network allowlist".into());
        }
        Ok(())
    }
}

/// Read the same declaration selection used for resources/image preflight.
pub fn network(
    config: &Path,
    templates: &Path,
    read_only: bool,
) -> Result<crate::runtime::Network, Sentence> {
    network_reading(config, templates, read_only).map(|(network, _)| network)
}

pub fn network_reading(
    config: &Path,
    templates: &Path,
    read_only: bool,
) -> Result<(crate::runtime::Network, serde_json::Value), Sentence> {
    network_with_resolvers(config, templates, read_only, device_nameservers)
}

fn network_with_resolvers(
    config: &Path,
    templates: &Path,
    read_only: bool,
    resolvers: impl FnOnce() -> Vec<String>,
) -> Result<(crate::runtime::Network, serde_json::Value), Sentence> {
    let own = config.join(gb_config::agent_environment::PATH);
    let path = if own.exists() {
        own
    } else {
        templates.join(gb_config::agent_environment::PATH)
    };
    let text = std::fs::read_to_string(&path).map_err(|e| Sentence::Text(e.to_string()))?;
    let value = gb_config::yaml_value(&text).map_err(|e| Sentence::Text(e.to_string()))?;
    let object = value
        .as_object()
        .ok_or_else(|| Sentence::Text("Environment declaration must be an object".into()))?;
    if object
        .keys()
        .any(|key| !["image", "resources", "network", "dns"].contains(&key.as_str()))
    {
        return Err(Sentence::Text(
            "Unknown environment declaration field".into(),
        ));
    }
    gb_config::agent_environment::resources(config, templates)
        .map_err(|e| Sentence::Text(e.to_string()))?;
    gb_config::agent_environment::image(config, templates)
        .map_err(|e| Sentence::Text(e.to_string()))?;
    let dns = object
        .get("dns")
        .and_then(serde_json::Value::as_object)
        .ok_or_else(|| Sentence::Text("DNS declaration is required".into()))?;
    if dns.keys().any(|key| key != "nameservers") {
        return Err(Sentence::Text("Unknown DNS declaration field".into()));
    }
    let policy: Network = match value.get("network") {
        None => Network::default(),
        Some(v) if v.is_object() => {
            serde_json::from_value(v.clone()).map_err(|e| Sentence::Text(e.to_string()))?
        }
        Some(_) => {
            return Err(Sentence::Text(
                "Network declaration must be a mapping".into(),
            ));
        }
    };
    policy.validate().map_err(Sentence::Text)?;
    let mut nameservers = Vec::new();
    match value.pointer("/dns/nameservers") {
        None => return Err(Sentence::Text("DNS nameservers are required".into())),
        Some(serde_json::Value::String(host)) if host == "host" => {
            nameservers = resolvers();
            if nameservers.is_empty() {
                return Err(Sentence::Key(
                    "intelligences.agent_environment.declaration.no_device_resolver",
                    vec![
                        ("where", gb_config::agent_environment::PATH.into()),
                        ("host", host.clone()),
                    ],
                ));
            }
        }
        Some(serde_json::Value::Array(list)) => {
            for entry in list {
                let ip: std::net::Ipv4Addr = entry
                    .as_str()
                    .ok_or_else(|| Sentence::Text("DNS must name IPv4 resolvers".into()))?
                    .parse()
                    .map_err(|_| Sentence::Text("DNS must name IPv4 resolvers".into()))?;
                nameservers.push(ip.to_string());
            }
        }
        _ => {
            return Err(Sentence::Text(
                "DNS must name IPv4 resolvers or host".into(),
            ));
        }
    }
    if nameservers.is_empty() {
        return Err(Sentence::AtPath(
            path,
            Box::new(Sentence::Key(
                "intelligences.agent_environment.declaration.no_nameservers",
                vec![("host", "host".into())],
            )),
        ));
    }
    let declared = match value
        .pointer("/dns/nameservers")
        .and_then(serde_json::Value::as_str)
    {
        Some("host") => "host".to_owned(),
        _ => nameservers.join(", "),
    };
    let view = serde_json::json!({"network":{"mode": match policy.mode { Mode::Deny=>"deny", Mode::Allowlist=>"allowlist", Mode::Unrestricted=>"unrestricted" },"allowed_domains":policy.allowed_domains,"allow_local_network":policy.allow_local_network},"dns":{"declared":declared,"nameservers":nameservers,"problem":""}});
    let network = crate::runtime::Network {
        unrestricted: !read_only && policy.mode == Mode::Unrestricted,
        domains: if read_only {
            vec![]
        } else {
            policy.allowed_domains
        },
        local_network: !read_only && policy.allow_local_network,
        host_ports: vec![],
        nameservers,
    };
    network.policy().map_err(Sentence::Text)?;
    Ok((network, view))
}

/// The platform's IPv4 DNS servers in original order, without duplicates.
fn resolver_addresses(text: &str, resolv_conf: bool) -> Vec<String> {
    let mut result = Vec::new();
    for line in text.lines() {
        let mut fields = line.split_whitespace();
        let candidate = if resolv_conf {
            if fields.next() != Some("nameserver") {
                continue;
            }
            fields.next()
        } else {
            fields.next()
        };
        if let Some(ip) = candidate.and_then(|s| s.parse::<std::net::Ipv4Addr>().ok()) {
            let ip = ip.to_string();
            if !result.contains(&ip) {
                result.push(ip);
            }
        }
    }
    result
}

#[cfg(any(windows, test))]
fn windows_dns_arguments() -> [&'static str; 4] {
    [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-DnsClientServerAddress -AddressFamily IPv4 | ForEach-Object { $_.ServerAddresses }",
    ]
}

fn device_nameservers() -> Vec<String> {
    #[cfg(windows)]
    let text = std::process::Command::new("powershell")
        .args(windows_dns_arguments())
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).into_owned());
    #[cfg(not(windows))]
    let text = std::fs::read_to_string("/etc/resolv.conf").ok();
    text.map(|text| resolver_addresses(&text, !cfg!(windows)))
        .unwrap_or_default()
}

/// Host temporary sources on Windows cannot be mounted by the runtime.
/// An empty guest cwd is fine: only actual host sources cross this boundary.
pub fn reject_windows_temp_mounts(
    mounts: &[Mount],
    windows: bool,
    local_app_data: Option<&Path>,
) -> Result<(), String> {
    if !windows {
        return Ok(());
    }
    let Some(local) = local_app_data else {
        return Ok(());
    };
    let temp = gb_base::host_path::normalize(&local.join("Temp")).map_err(|e| e.to_string())?;
    let temporary_names = protected_names(&temp);
    for source in mounts.iter().filter_map(|mount| mount.host.as_ref()) {
        let source_names = protected_names(source);
        if source_names.iter().any(|source| {
            temporary_names
                .iter()
                .any(|root| windows_path_contains(root, source))
        }) {
            return Err(format!(
                "Windows temporary directory {} cannot be mounted: {}",
                temp.display(),
                source.display()
            ));
        }
    }
    Ok(())
}

// Fold each component independently so sibling prefixes stay distinct. Use
// the same Unicode normalization/case policy as Windows worktree names.
fn windows_path_contains(root: &Path, path: &Path) -> bool {
    use unicode_normalization::UnicodeNormalization;
    let components = |path: &Path| {
        path.components()
            .map(|part| {
                unicase::UniCase::unicode(
                    part.as_os_str().to_string_lossy().nfd().collect::<String>(),
                )
                .to_folded_case()
            })
            .collect::<Vec<_>>()
    };
    components(path).starts_with(&components(root))
}

// Protect link spellings as well as their destinations, including absent tails.
fn protected_names(path: &Path) -> Vec<PathBuf> {
    let mut seen = vec![path.to_owned()];
    let mut current = path.to_owned();
    for _ in 0..40 {
        let components: Vec<_> = current.components().collect();
        let mut prefix = PathBuf::new();
        let mut changed = false;
        for (i, part) in components.iter().enumerate() {
            if matches!(part, Component::ParentDir) {
                prefix.pop();
            } else {
                prefix.push(part);
            }
            match std::fs::symlink_metadata(&prefix) {
                Ok(m) if m.file_type().is_symlink() => {
                    let Ok(dest) = std::fs::read_link(&prefix) else {
                        return seen;
                    };
                    let mut next = if dest.is_absolute() {
                        dest
                    } else {
                        prefix.parent().unwrap().join(dest)
                    };
                    for rest in &components[i + 1..] {
                        next.push(rest);
                    }
                    // Keep unresolved tails intact: a link before `..` must be
                    // followed before the parent component is interpreted.
                    if seen.contains(&next) {
                        return seen;
                    }
                    current = next;
                    seen.push(current.clone());
                    changed = true;
                    break;
                }
                Ok(metadata) if !metadata.is_dir() && i + 1 < components.len() => {
                    // A file cannot be traversed to interpret a later `..`.
                    // Close the inspected obstacle, not the unreachable tail.
                    if !seen.contains(&prefix) {
                        seen.push(prefix);
                    }
                    return seen;
                }
                Ok(_) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    if components[i + 1..].contains(&Component::ParentDir)
                        && !seen.contains(&prefix)
                    {
                        seen.push(prefix);
                    }
                    return seen;
                }
                Err(_) => return seen,
            }
        }
        if !changed {
            if !seen.contains(&prefix) {
                seen.push(prefix);
            }
            return seen;
        }
    }
    seen
}

/// Member checkouts are host-owned precise children of workspace state. This
/// exception never opens state itself or any credential directory as cwd.
pub fn working_directory(
    workspace: &Path,
    person: &str,
    cwd: &Path,
    denied: &[PathBuf],
) -> Result<PathBuf, String> {
    let cwd = gb_base::host_path::directory(cwd, false).map_err(|e| e.to_string())?;
    let clones = workspace.join(".guildbotics/local/clones").join(person);
    let managed = cwd
        .strip_prefix(&clones)
        .ok()
        .and_then(|p| p.components().next())
        .is_some_and(|c| {
            let text = c.as_os_str().to_str().unwrap_or_default();
            text.len() == 64
                && text
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        });
    if managed {
        let state = workspace.join(".guildbotics");
        let remaining: Vec<_> = denied.iter().filter(|p| **p != state).cloned().collect();
        validate_source(&cwd, &remaining, true, false)
    } else {
        validate_source(&cwd, denied, true, false)
    }
}

/// A copy cannot turn a covering read-only mount into a writable directory.
/// Every nested mount is excluded from both copying and writing back.
pub fn copy_plan(
    workspace: &Path,
    person: &str,
    cwd: &Path,
    access: &Resolved,
    mounts: &[Mount],
    lock: PathBuf,
) -> Result<crate::worktree::Copy, String> {
    let source = working_directory(workspace, person, cwd, &access.denied)?;
    if access
        .grants
        .iter()
        .filter(|m| m.host.as_ref().is_some_and(|p| source.starts_with(p)))
        .max_by_key(|m| m.host.as_ref().unwrap().components().count())
        .is_some_and(|m| m.readonly)
    {
        return Err("The working directory is inside a read-only grant".into());
    }
    let guest = crate::guest_path(&source).map_err(|_| "Invalid working directory guest path")?;
    let mut excluded = vec![".git".into(), ".guildbotics".into()];
    for mount in mounts.iter().chain(&access.grants) {
        if let Some(relative) = mount.guest.strip_prefix(&format!("{guest}/"))
            && !relative.is_empty()
        {
            excluded.push(relative.to_owned());
        }
    }
    excluded.sort();
    excluded.dedup();
    crate::worktree::Copy::new(source, guest, excluded, lock)
}

/// What a run may add to the member's clones: every repository the member
/// works in shares one mount, and a full Rust workspace build writes more
/// than the runtime's default.
pub const CLONE_QUOTA_MIB: u32 = 16 * 1024;

pub struct Plan {
    pub cwd: String,
    pub mounts: Vec<Mount>,
    pub copy: Option<crate::worktree::Copy>,
}
/// Resolve writable exposure once, so a copy cannot bypass a mounted grant.
pub fn plan(
    workspace: &Path,
    person: &str,
    cwd: &Path,
    access: &Resolved,
    mut mounts: Vec<Mount>,
    lock: PathBuf,
    read_only: bool,
) -> Result<Plan, String> {
    let cwd = gb_base::host_path::directory(cwd, read_only).map_err(|e| e.to_string())?;
    let guest = crate::guest_path(&cwd).map_err(|_| "Invalid working directory guest path")?;
    mounts.retain(|m| m.guest != guest || m.host.is_some());
    let clones = workspace.join(".guildbotics/local/clones").join(person);
    let managed = cwd
        .strip_prefix(&clones)
        .ok()
        .and_then(|p| p.components().next())
        .is_some_and(|c| {
            let v = c.as_os_str().to_str().unwrap_or_default();
            v.len() == 64
                && v.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        });
    if clones.exists() {
        let checked = validate_source(&clones, &access.denied, false, false)?;
        let mount = Mount {
            guest: crate::guest_path(&checked).map_err(|_| "Invalid clone guest path")?,
            host: Some(checked),
            readonly: read_only,
            quota_mib: Some(CLONE_QUOTA_MIB),
        };
        if !mounts.iter().any(|m| m.guest == mount.guest) {
            mounts.push(mount);
        }
    }
    let covering = access
        .grants
        .iter()
        .filter(|m| m.host.as_ref().is_some_and(|p| cwd.starts_with(p)))
        .max_by_key(|m| m.host.as_ref().unwrap().components().count());
    if !read_only && covering.is_some_and(|m| m.readonly) {
        return Err("The working directory is inside a read-only grant".into());
    }
    let copy = if read_only {
        if !mounts.iter().any(|m| m.guest == guest) {
            mounts.push(Mount {
                guest: guest.clone(),
                host: None,
                readonly: false,
                quota_mib: None,
            });
        }
        None
    } else if managed || covering.is_some() {
        None
    } else {
        Some(copy_plan(workspace, person, &cwd, access, &mounts, lock)?)
    };
    mounts.sort_by_key(|mount| Path::new(&mount.guest).components().count());
    Ok(Plan {
        cwd: guest,
        mounts,
        copy,
    })
}

#[cfg(test)]
mod frozen_tests {
    use super::*;
    fn put(root: &Path, relative: &str, value: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, value).unwrap();
    }
    const SHARED: &str = ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml";
    const LOCAL: &str = ".guildbotics/local/cli_agent_filesystem_grants.yml";
    #[test]
    fn local_view_keeps_written_spelling_and_document_drive_relative_names() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        std::fs::create_dir_all(home.join("cache")).unwrap();
        let workspace = dir.path().join("workspace");
        for path in [
            home.join("cache"),
            home.join("missing"),
            PathBuf::from("cache"),
        ] {
            put(
                &workspace,
                LOCAL,
                &serde_json::json!({"paths":[{"path":path,"access":"read"}]}).to_string(),
            );
            let resolved = resolve(&workspace, &home, &[], false, false).unwrap();
            assert_eq!(resolved.view["paths"][0]["grant"], serde_json::json!(path));
            assert_eq!(
                resolved.view["paths"][0]["present"],
                path != home.join("missing")
            );
        }
        #[cfg(not(windows))]
        {
            put(
                &workspace,
                SHARED,
                "documents: [{path: 'a:b', access: read}]",
            );
            assert!(resolve(&workspace, &home, &[], false, false).is_ok());
        }
    }
    #[test]
    fn grant_documents_local_paths_optional_files_and_invalid_population() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        std::fs::create_dir(&home).unwrap();
        let workspace = dir.path().join("workspace");
        for value in [None, Some("null"), Some("{}")] {
            if let Some(value) = value {
                put(&workspace, SHARED, value);
                put(&workspace, LOCAL, value);
            }
            let value = resolve(&workspace, &home, &[], false, false).unwrap();
            assert_eq!(value.view["documents"].as_array().unwrap().len(), 1);
            assert_eq!(value.view["paths"], serde_json::json!([]));
        }
        for path in [
            "/etc",
            "C:\\Users\\x",
            "",
            ".",
            "..",
            "Documents/../secrets",
            "a//b",
            "./a",
        ] {
            put(
                &workspace,
                SHARED,
                &serde_json::json!({"documents":[{"path":path,"access":"read"}]}).to_string(),
            );
            assert!(
                resolve(&workspace, &home, &[], false, true).is_err(),
                "{path}"
            );
            assert!(!home.join("Documents").exists());
        }
        for scope in [SHARED, LOCAL] {
            put(&workspace, SHARED, "{}");
            put(&workspace, LOCAL, "{}");
            for invalid in [
                "documents: [",
                "[]",
                "unknown: true",
                "paths: [{path: '..', access: read}]",
            ] {
                put(&workspace, scope, invalid);
                assert!(
                    resolve(&workspace, &home, &[], false, false).is_err(),
                    "scope={scope}, content={invalid:?}"
                );
            }
            put(&workspace, scope, "{}");
            assert!(resolve(&workspace, &home, &[], false, false).is_ok());
        }
        std::fs::create_dir(home.join("cache")).unwrap();
        let outside = dir.path().join("tools");
        std::fs::create_dir(&outside).unwrap();
        put(
            &workspace,
            SHARED,
            "documents: [{path: Documents/shared, access: read}]",
        );
        put(&workspace, LOCAL, &serde_json::json!({"paths":[{"path":"cache","access":"read_write"},{"path":outside,"access":"read"}],"deny":["cache/private"]}).to_string());
        assert!(resolve(&workspace, &home, &[], false, true).is_err()); // A grant containing a deny is refused.
        put(&workspace, LOCAL, &serde_json::json!({"paths":[{"path":"cache","access":"read_write"},{"path":outside,"access":"read"}]}).to_string());
        let value = resolve(&workspace, &home, &[], false, true).unwrap();
        assert_eq!(value.view["paths"][0]["grant"], "cache");
        assert_eq!(
            value.view["paths"][1]["grant"],
            outside.to_string_lossy().as_ref()
        );
        assert_eq!(value.view["documents"][1]["grant"], "Documents/shared");
        put(&workspace, LOCAL, "paths: [{path: absent, access: read}]");
        assert!(resolve(&workspace, &home, &[], false, true).is_err());
        assert_eq!(
            resolve(&workspace, &home, &[], false, false).unwrap().view["paths"][0]["present"],
            false
        );
    }
    #[test]
    fn preflight_enumerates_open_grants_and_existing_parents_before_creating_anything() {
        let dir = tempfile::tempdir().unwrap();
        // The probe sees resolved paths; macOS temporary directories sit behind a link.
        let root = dir.path().canonicalize().unwrap();
        let home = root.join("home");
        std::fs::create_dir(&home).unwrap();
        let workspace = root.join("workspace");
        std::fs::create_dir(home.join("Documents")).unwrap();
        let error = resolve_with_probe(&workspace, &home, &[], false, true, |path| {
            if path == home.join("Documents") {
                Err(format!("{}: permission denied", path.display()))
            } else {
                Ok(())
            }
        })
        .unwrap_err();
        assert!(error.contains("permission denied"));
        assert!(!home.join("Documents/GuildBotics").exists());
        for scope in [SHARED, LOCAL] {
            std::fs::create_dir_all(home.join("extra")).unwrap();
            put(
                &workspace,
                scope,
                if scope == SHARED {
                    "documents: [{path: extra, access: read}]"
                } else {
                    "paths: [{path: extra, access: read}]"
                },
            );
            assert!(
                resolve_with_probe(&workspace, &home, &[], false, false, |path| {
                    if path == home.join("extra") {
                        Err("changed into a file during enumeration".into())
                    } else {
                        Ok(())
                    }
                })
                .unwrap_err()
                .contains("changed into a file")
            );
            put(&workspace, scope, "{}");
        }
        put(&workspace, LOCAL, "paths: [{path: absent, access: read}]");
        assert!(
            resolve_with_probe(&workspace, &home, &[], false, false, |path| {
                assert_ne!(path, home.join("absent"));
                Ok(())
            })
            .is_ok()
        );
        std::fs::write(home.join("absent"), "not a directory").unwrap();
        assert!(resolve(&workspace, &home, &[], false, false).is_err());
    }
    #[test]
    fn windows_temp_guard_checks_host_sources_only_and_is_platform_specific() {
        let dir = tempfile::tempdir().unwrap();
        let local = dir.path().join("AppData/Local");
        std::fs::create_dir_all(local.join("Temp/workspace/.guildbotics/config")).unwrap();
        let source = local.join("Temp/workspace/.guildbotics/config");
        let mount = |host| Mount {
            host,
            guest: "/work".into(),
            readonly: true,
            quota_mib: None,
        };
        assert!(
            reject_windows_temp_mounts(&[mount(Some(source.clone()))], true, Some(&local)).is_err()
        );
        for spelling in [
            "TEMP/workspace/.guildbotics/config",
            "temp/workspace/.guildbotics/config",
            "TeMp/workspace/.guildbotics/config",
        ] {
            assert!(
                reject_windows_temp_mounts(
                    &[mount(Some(local.join(spelling)))],
                    true,
                    Some(&local)
                )
                .is_err(),
                "{spelling}"
            );
        }
        assert!(windows_path_contains(
            Path::new("/Users/é/AppData/Local/Temp"),
            Path::new("/users/É/appdata/local/TEMP/work")
        ));
        assert!(reject_windows_temp_mounts(&[mount(None)], true, Some(&local)).is_ok());
        assert!(reject_windows_temp_mounts(&[mount(Some(source))], false, Some(&local)).is_ok());
        assert!(
            reject_windows_temp_mounts(
                &[mount(Some(local.join("Temporary/work")))],
                true,
                Some(&local)
            )
            .is_ok()
        );
    }
    #[test]
    fn strict_environment_declarations_reject_unknown_fields_and_require_ipv4_dns() {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join("config");
        let templates = dir.path().join("templates");
        const PATH: &str = gb_config::agent_environment::PATH;
        put(&templates, PATH, "dns: {nameservers: [1.1.1.1]}");
        assert_eq!(
            network(&config, &templates, false).unwrap().nameservers,
            ["1.1.1.1"]
        );
        for invalid in [
            "[]",
            "{}",
            "dns: {}",
            "dns: {nameservers: [1.1.1.1], search: [corp]}",
            "dns: {nameservers: []}",
            "dns: {nameservers: [host]}",
            "dns: {nameservers: [one.one.one.one]}",
            "dns: {nameservers: [::1]}",
            "dns: {nameservers: system}",
            "dns: {nameservers: [1.1.1.1]}\npackages: {}",
            "dns: {nameservers: [1.1.1.1]}\nresources: {cpus: 0}",
            "dns: {nameservers: [1.1.1.1]}\nresources: {gpu: 1}",
            "dns: {nameservers: [1.1.1.1]}\nnetwork: {mode: allowlist}",
            "dns: {nameservers: [1.1.1.1]}\nimage: {reference: x}",
        ] {
            put(&config, PATH, invalid);
            assert!(network(&config, &templates, false).is_err(), "{invalid}");
        }
        put(
            &config,
            PATH,
            "dns: {nameservers: [10.0.0.53]}\nnetwork: {mode: unrestricted}",
        );
        assert_eq!(
            network(&config, &templates, false).unwrap().nameservers,
            ["10.0.0.53"]
        );
        assert!(network(&config, &templates, false).unwrap().unrestricted);
    }
    #[test]
    fn device_dns_parsing_ignores_ipv6_and_deduplicates_preserving_order() {
        assert!(windows_dns_arguments()[3].contains("IPv4"));
        let dir = tempfile::tempdir().unwrap();
        let templates = dir.path().join("templates");
        let config = dir.path().join("config");
        put(
            &templates,
            gb_config::agent_environment::PATH,
            "dns: {nameservers: host}",
        );
        for declaration in [&templates, &config] {
            put(
                declaration,
                gb_config::agent_environment::PATH,
                "dns: {nameservers: host}",
            );
            assert_eq!(
                network_with_resolvers(&config, &templates, false, Vec::new).unwrap_err(),
                Sentence::Key(
                    "intelligences.agent_environment.declaration.no_device_resolver",
                    vec![
                        ("where", gb_config::agent_environment::PATH.into()),
                        ("host", "host".into()),
                    ],
                )
            );
        }
        put(
            &config,
            gb_config::agent_environment::PATH,
            "dns: {nameservers: []}",
        );
        assert_eq!(
            network_with_resolvers(&config, &templates, false, || panic!(
                "explicit DNS must not read host"
            ))
            .unwrap_err(),
            Sentence::AtPath(
                config.join(gb_config::agent_environment::PATH),
                Box::new(Sentence::Key(
                    "intelligences.agent_environment.declaration.no_nameservers",
                    vec![("host", "host".into())],
                )),
            )
        );
        std::fs::remove_file(config.join(gb_config::agent_environment::PATH)).unwrap();
        assert_eq!(
            network_with_resolvers(&config, &templates, false, || vec!["192.168.3.1".into()])
                .unwrap()
                .0
                .nameservers,
            ["192.168.3.1"]
        );
        put(
            &templates,
            gb_config::agent_environment::PATH,
            "dns: {nameservers: [10.0.0.53]}",
        );
        assert_eq!(
            network_with_resolvers(&config, &templates, false, || panic!(
                "explicit DNS must not read host"
            ))
            .unwrap()
            .0
            .nameservers,
            ["10.0.0.53"]
        );

        assert_eq!(
            resolver_addresses(
                "search corp\nnameserver ::1\nnameserver 192.168.3.1\nnameserver 1.1.1.1\nnameserver 192.168.3.1\nnameserver\n",
                true
            ),
            ["192.168.3.1", "1.1.1.1"]
        );
        assert_eq!(
            resolver_addresses("10.0.0.53\r\n::1\r\n10.0.0.54\r\n10.0.0.53\r\n", false),
            ["10.0.0.53", "10.0.0.54"]
        );
        assert!(resolver_addresses("", true).is_empty());
    }
    #[cfg(unix)]
    #[test]
    fn unresolved_protected_aliases_close_names_without_blocking_unrelated_work() {
        use std::os::unix::fs::symlink;
        for shape in ["self", "mutual", "broken", "file"] {
            let dir = tempfile::tempdir().unwrap();
            let home = dir.path().join("home");
            std::fs::create_dir(&home).unwrap();
            let target = dir.path().join("target");
            if shape == "file" {
                std::fs::write(&target, "file").unwrap();
            }
            symlink(
                if shape == "self" {
                    home.join(".ssh")
                } else {
                    target.clone()
                },
                home.join(".ssh"),
            )
            .unwrap();
            if shape == "mutual" {
                symlink(home.join(".ssh"), &target).unwrap();
            }
            let access = resolve(&dir.path().join("workspace"), &home, &[], false, false).unwrap();
            let work = dir.path().join("work");
            std::fs::create_dir(&work).unwrap();
            assert!(validate_source(&work, &access.denied, true, false).is_ok());
            assert!(validate_source(&home, &access.denied, true, false).is_err());
            if shape == "self" {
                // A self-link never names this unrelated absent target.
                assert!(validate_source(&target, &access.denied, true, true).is_ok());
            } else {
                assert!(
                    validate_source(&target, &access.denied, true, true).is_err(),
                    "shape={shape}"
                );
            }
        }
    }
    #[cfg(unix)]
    #[test]
    fn untraversable_protected_targets_close_names_without_stopping_other_work() {
        use std::os::unix::fs::symlink;
        for name in [".ssh", ".config/gh", ".local/share/keyrings"] {
            for file in [false, true] {
                for tail in ["../ordinary", "child"] {
                    let dir = tempfile::tempdir().unwrap();
                    let protected = dir.path().join("home").join(name);
                    std::fs::create_dir_all(protected.parent().unwrap()).unwrap();
                    let obstacle = dir.path().join("obstacle");
                    if file {
                        std::fs::write(&obstacle, "not a directory").unwrap();
                    }
                    symlink(obstacle.join(tail), &protected).unwrap();
                    let ordinary = dir.path().join("ordinary");
                    std::fs::create_dir(&ordinary).unwrap();
                    let denied = protected_names(&protected);
                    assert!(
                        validate_source(&ordinary, &denied, true, false).is_ok(),
                        "name={name}, file={file}, tail={tail}, denied={denied:?}"
                    );
                    for blocked in [protected.parent().unwrap(), obstacle.as_path()] {
                        assert!(
                            validate_source(blocked, &denied, true, true).is_err(),
                            "name={name}, file={file}, tail={tail}, blocked={blocked:?}, denied={denied:?}"
                        );
                    }
                }
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn local_deny_alias_protects_both_spelling_and_target_and_cannot_deny_home() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        std::fs::create_dir(&home).unwrap();
        let private = dir.path().join("private");
        std::fs::create_dir(&private).unwrap();
        symlink(&private, home.join("deny")).unwrap();
        let workspace = dir.path().join("workspace");
        put(&workspace, LOCAL, "deny: [deny]");
        let access = resolve(&workspace, &home, &[], false, false).unwrap();
        for path in [&home, &private] {
            assert!(validate_source(path, &access.denied, true, false).is_err());
        }
        std::fs::remove_file(home.join("deny")).unwrap();
        symlink(&home, home.join("deny")).unwrap();
        assert!(resolve(&workspace, &home, &[], false, false).is_err());
    }
}
