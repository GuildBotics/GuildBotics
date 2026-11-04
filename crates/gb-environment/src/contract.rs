//! Resolve the shared/local filesystem contract before creating any directory.
use crate::Mount;
use serde::Deserialize;
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

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    pub path: String,
    pub access: Access,
}
#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Access {
    Read,
    ReadWrite,
}
#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Shared {
    #[serde(default)]
    documents: Vec<Grant>,
}
#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Local {
    #[serde(default)]
    paths: Vec<Grant>,
    #[serde(default)]
    deny: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Resolved {
    pub grants: Vec<Mount>,
    pub denied: Vec<PathBuf>,
    pub view: serde_json::Value,
}

fn load<T: serde::de::DeserializeOwned + Default>(path: &Path) -> Result<T, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => gb_config::yaml_value(&text)
            .map_err(|e| e.to_string())
            .and_then(|v| serde_json::from_value(v).map_err(|e| e.to_string())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}
fn target(name: &str, home: &Path, absolute: bool) -> Result<PathBuf, String> {
    let path = Path::new(name);
    if name.is_empty()
        || name.contains('\\')
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
    let home = gb_base::host_path::directory(home, false).map_err(|e| e.to_string())?;
    let shared: Shared = load(
        &workspace
            .join(".guildbotics/config/intelligences")
            .join(FILE),
    )?;
    let local: Local = load(&workspace.join(".guildbotics/local").join(FILE))?;
    let mut denied: Vec<_> = SENSITIVE.iter().map(|s| home.join(s)).collect();
    denied.push(workspace.join(".guildbotics"));
    denied.extend(registered.iter().map(|p| p.join(".guildbotics")));
    let builtin_count = denied.len();
    for name in local.deny {
        let p = target(&name, &home, true)?;
        if p == home || p.parent().is_none() {
            return Err("Deny is too broad".into());
        }
        denied.push(p);
    }
    let mut planned = vec![(
        home.join("Documents/GuildBotics"),
        Access::ReadWrite,
        true,
        "Documents/GuildBotics".to_owned(),
        true,
    )];
    for grant in shared.documents {
        let p = target(&grant.path, &home, false)?;
        if p != home.join("Documents/GuildBotics") {
            planned.push((p, grant.access, true, grant.path, false));
        }
    }
    for grant in local.paths {
        let p = target(&grant.path, &home, true)?;
        if p == home || p.parent().is_none() {
            return Err("Local path is too broad".into());
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
        expanded.extend(protected_names(path)?);
    }
    denied = expanded;
    // Validate the whole population before creating even the built-in exchange.
    for (path, _, document, _, _) in &planned {
        validate_source(path, &denied, true, *document || !create)?;
    }
    let mut grants = Vec::new();
    let mut documents = Vec::new();
    let mut paths = Vec::new();
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
        grants.push(Mount {
            guest: checked.to_str().ok_or("Path is not UTF-8")?.into(),
            host: Some(checked),
            readonly: read_only || matches!(access, Access::Read),
        });
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
) -> Result<crate::runtime::Network, String> {
    let own = config.join(gb_config::agent_environment::PATH);
    let path = if own.exists() {
        own
    } else {
        templates.join(gb_config::agent_environment::PATH)
    };
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    let value = gb_config::yaml_value(&text).map_err(|e| e.to_string())?;
    let policy: Network = match value.get("network") {
        None => Network::default(),
        Some(v) => serde_json::from_value(v.clone()).map_err(|e| e.to_string())?,
    };
    policy.validate()?;
    let mut nameservers = Vec::new();
    match value.pointer("/dns/nameservers") {
        None => nameservers = vec!["1.1.1.1".into(), "8.8.8.8".into()],
        Some(serde_json::Value::String(host)) if host == "host" => {
            let text = std::fs::read_to_string("/etc/resolv.conf").map_err(|e| e.to_string())?;
            for line in text.lines() {
                let mut parts = line.split_whitespace();
                if parts.next() == Some("nameserver")
                    && let Some(ip) = parts
                        .next()
                        .and_then(|s| s.parse::<std::net::Ipv4Addr>().ok())
                {
                    nameservers.push(ip.to_string());
                }
            }
        }
        Some(serde_json::Value::Array(list)) => {
            for entry in list {
                let ip: std::net::Ipv4Addr = entry
                    .as_str()
                    .ok_or("DNS must name IPv4 resolvers")?
                    .parse()
                    .map_err(|_| "DNS must name IPv4 resolvers")?;
                nameservers.push(ip.to_string());
            }
        }
        _ => return Err("DNS must name IPv4 resolvers or host".into()),
    }
    if nameservers.is_empty() {
        return Err("No IPv4 DNS resolver is available".into());
    }
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
    network.policy()?;
    Ok(network)
}

// Protect link spellings as well as their destinations, including absent tails.
fn protected_names(path: &Path) -> Result<Vec<PathBuf>, String> {
    let mut seen = vec![path.to_owned()];
    let mut current = path.to_owned();
    for _ in 0..40 {
        let components: Vec<_> = current.components().collect();
        let mut prefix = PathBuf::new();
        let mut changed = false;
        for (i, part) in components.iter().enumerate() {
            prefix.push(part);
            match std::fs::symlink_metadata(&prefix) {
                Ok(m) if m.file_type().is_symlink() => {
                    let dest = std::fs::read_link(&prefix).map_err(|e| e.to_string())?;
                    let mut next = if dest.is_absolute() {
                        dest
                    } else {
                        prefix.parent().unwrap().join(dest)
                    };
                    for rest in &components[i + 1..] {
                        next.push(rest);
                    }
                    let next = std::path::absolute(next).map_err(|e| e.to_string())?;
                    let mut normalized = PathBuf::new();
                    for component in next.components() {
                        match component {
                            Component::ParentDir => {
                                normalized.pop();
                            }
                            Component::CurDir => {}
                            other => normalized.push(other),
                        }
                    }
                    current = normalized;
                    seen.push(current.clone());
                    changed = true;
                    break;
                }
                Ok(_) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(seen),
                Err(e) => return Err(e.to_string()),
            }
        }
        if !changed {
            return Ok(seen);
        }
    }
    Err("Protected path passes through too many links".into())
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
    let mut excluded = vec![".git".into(), ".guildbotics".into()];
    for mount in mounts.iter().chain(&access.grants) {
        if let Ok(relative) = Path::new(&mount.guest).strip_prefix(&source)
            && !relative.as_os_str().is_empty()
        {
            excluded.push(
                relative
                    .to_str()
                    .ok_or("Mount path is not UTF-8")?
                    .to_owned(),
            );
        }
    }
    excluded.sort();
    excluded.dedup();
    crate::worktree::Copy::new(
        source.clone(),
        source
            .to_str()
            .ok_or("Working directory is not UTF-8")?
            .into(),
        excluded,
        lock,
    )
}

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
    mounts.retain(|m| m.guest != cwd.to_string_lossy() || m.host.is_some());
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
            guest: checked.to_str().ok_or("Clone path is not UTF-8")?.into(),
            host: Some(checked),
            readonly: read_only,
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
        if !mounts.iter().any(|m| m.guest == cwd.to_string_lossy()) {
            mounts.push(Mount {
                guest: cwd.to_str().ok_or("cwd is not UTF-8")?.into(),
                host: None,
                readonly: false,
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
        cwd: cwd.to_str().ok_or("cwd is not UTF-8")?.into(),
        mounts,
        copy,
    })
}
