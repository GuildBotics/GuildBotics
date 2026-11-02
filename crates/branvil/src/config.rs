//! Configuration file, data directory layout, and the fixed base URL.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};

use anyhow::{Context, bail};
use serde::{Deserialize, Serialize};

pub const DEFAULT_LISTEN: &str = "127.0.0.1:7878";

/// `branvil.toml`. Every field is optional; a missing file means defaults.
#[derive(Serialize, Deserialize, Clone, Debug, Default)]
#[serde(deny_unknown_fields)]
pub struct ConfigFile {
    /// Loopback address to listen on.
    pub listen: Option<String>,
    /// The permanent base URL every machine uses (`http://127.0.0.1:7878`).
    pub base_url: Option<String>,
    #[serde(default)]
    pub storage: StorageConfig,
    #[serde(default)]
    pub limits: LimitsConfig,
    #[serde(default)]
    pub ci: crate::ci::config::CiConfig,
    #[serde(default)]
    pub upstream: UpstreamConfig,
}

/// The upstream code host (`docs/architecture.md`, "Upstream").
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields, default)]
pub struct UpstreamConfig {
    /// Seconds between polls while a published pull request is open.
    pub poll_active_secs: u64,
    /// Seconds between polls otherwise.
    pub poll_idle_secs: u64,
    /// Seconds after a push or pull request event within which the host may
    /// still register checks of the head.
    pub checks_registration_secs: u64,
    pub github: GitHubConfig,
}

impl Default for UpstreamConfig {
    fn default() -> Self {
        Self {
            poll_active_secs: 60,
            poll_idle_secs: 900,
            checks_registration_secs: 60,
            github: GitHubConfig::default(),
        }
    }
}

/// Where GitHub is (changed only to point tests at a stand-in).
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields, default)]
pub struct GitHubConfig {
    pub api_url: String,
    pub web_url: String,
    /// Repositories are `<git_url>/<owner>/<name>.git`.
    pub git_url: String,
}

impl Default for GitHubConfig {
    fn default() -> Self {
        Self {
            api_url: "https://api.github.com".into(),
            web_url: "https://github.com".into(),
            git_url: "https://github.com".into(),
        }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields, default)]
pub struct StorageConfig {
    /// redb page cache in MiB (not a limit on the process memory).
    pub cache_size_mib: usize,
    /// Concurrent read transactions on the blocking pool.
    pub read_concurrency: usize,
}

impl Default for StorageConfig {
    fn default() -> Self {
        Self {
            cache_size_mib: 32,
            read_concurrency: 8,
        }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields, default)]
pub struct LimitsConfig {
    /// Concurrent service-side Git commands.
    pub git_concurrency: usize,
    /// Concurrent Smart HTTP transfers (clone, fetch, push). Kept apart from
    /// the service commands: a push's hook waits for the service to record it,
    /// which runs service commands while the push holds its transfer slot.
    pub git_http_concurrency: usize,
    /// Wall-clock limit of one Smart HTTP request, in seconds.
    pub git_http_timeout_secs: u64,
    /// Wall-clock limit of one service-side Git command, in seconds.
    pub git_command_timeout_secs: u64,
    /// Largest accepted push body, in MiB.
    pub git_push_max_mib: u64,
    /// Largest JSON request body, in KiB.
    pub api_body_max_kib: usize,
    /// GUI session lifetime, in hours.
    pub session_hours: i64,
}

impl Default for LimitsConfig {
    fn default() -> Self {
        Self {
            git_concurrency: 8,
            git_http_concurrency: 8,
            git_http_timeout_secs: 600,
            git_command_timeout_secs: 120,
            git_push_max_mib: 1024,
            api_body_max_kib: 1024,
            session_hours: 720,
        }
    }
}

/// The effective configuration of one process.
#[derive(Clone, Debug)]
pub struct Config {
    pub data_dir: PathBuf,
    /// The configuration file: the one read, or where it would be.
    pub file: PathBuf,
    pub listen: SocketAddr,
    /// Set when configured or given; otherwise derived from the bound address.
    pub base_url: Option<String>,
    pub storage: StorageConfig,
    pub limits: LimitsConfig,
    pub ci: crate::ci::config::CiConfig,
    pub upstream: UpstreamConfig,
}

/// Where everything lives inside the data directory.
#[derive(Clone, Debug)]
pub struct Layout {
    pub root: PathBuf,
}

impl Layout {
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
        }
    }
    pub fn config_file(&self) -> PathBuf {
        self.root.join("branvil.toml")
    }
    pub fn db(&self) -> PathBuf {
        self.root.join("branvil.redb")
    }
    pub fn repos(&self) -> PathBuf {
        self.root.join("repos")
    }
    /// Branvil-owned hooks; every repository's `core.hooksPath` points here.
    pub fn hooks(&self) -> PathBuf {
        self.root.join("hooks")
    }
    /// An empty global Git configuration for the service's Git processes.
    pub fn git_config(&self) -> PathBuf {
        self.root.join("git").join("gitconfig")
    }
    /// Held by the service and by every Git it started (`git::hold`).
    pub fn git_lock(&self) -> PathBuf {
        self.root.join("git").join("service.lock")
    }
    pub fn ci_logs(&self) -> PathBuf {
        self.root.join("ci").join("logs")
    }
    pub fn ci_artifacts(&self) -> PathBuf {
        self.root.join("ci").join("artifacts")
    }
    /// The bytes of tickets' attachments, one file per attachment id.
    pub fn attachments(&self) -> PathBuf {
        self.root.join("attachments")
    }
    pub fn secrets(&self) -> PathBuf {
        self.root.join("secrets")
    }
    pub fn cursor_key(&self) -> PathBuf {
        self.secrets().join("cursor.key")
    }
    /// The owner's token (`0600`), for the CLI on this machine.
    pub fn owner_token(&self) -> PathBuf {
        self.secrets().join("owner.token")
    }
    /// Members' credentials for upstream hosts (`0600` files).
    pub fn accounts(&self) -> PathBuf {
        self.secrets().join("accounts")
    }
    /// The webhook told of automatic publishes (`0600`): a credential of
    /// another system.
    pub fn webhook(&self) -> PathBuf {
        self.secrets().join("webhook.url")
    }
    pub fn repo_dir(&self, path: &str) -> PathBuf {
        let mut dir = self.repos();
        let mut parts: Vec<&str> = path.split('/').collect();
        let last = parts.pop().unwrap_or_default();
        for p in parts {
            dir.push(p);
        }
        dir.push(format!("{last}.git"));
        dir
    }

    /// Creates the directories (the root and secrets with owner-only permissions).
    pub fn create(&self) -> anyhow::Result<()> {
        for dir in [
            self.root.clone(),
            self.repos(),
            self.hooks(),
            self.git_config().parent().unwrap().to_path_buf(),
            self.ci_logs(),
            self.ci_artifacts(),
            self.attachments(),
            self.secrets(),
        ] {
            std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
        }
        // Only the service's user reads the database, repositories, and logs.
        restrict(&self.root, true)?;
        restrict(&self.secrets(), true)?;
        if !self.git_config().exists() {
            std::fs::write(self.git_config(), "")?;
        }
        Ok(())
    }

    /// The key that signs collection cursors, created on first use.
    pub fn load_cursor_key(&self) -> anyhow::Result<Vec<u8>> {
        let path = self.cursor_key();
        if let Ok(key) = std::fs::read(&path)
            && key.len() >= 32
        {
            return Ok(key);
        }
        let key = crate::util::random_bytes::<32>().to_vec();
        std::fs::write(&path, &key)?;
        restrict(&path, false)?;
        Ok(key)
    }
}

/// Writes a secret to its `0600` file: a new file, then renamed over the old
/// one.
pub fn write_secret(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let tmp = path.with_extension("tmp");
    let _ = std::fs::remove_file(&tmp);
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut f = options.open(&tmp)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    std::fs::rename(&tmp, path)
}

#[cfg(unix)]
pub fn restrict(path: &Path, dir: bool) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let mode = if dir { 0o700 } else { 0o600 };
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(not(unix))]
pub fn restrict(_path: &Path, _dir: bool) -> anyhow::Result<()> {
    // Windows: the data directory inherits the user profile's ACL.
    Ok(())
}

/// The default data directory: the OS convention for application data.
pub fn default_data_dir() -> anyhow::Result<PathBuf> {
    let dirs = directories::ProjectDirs::from("", "", "branvil")
        .context("cannot determine the home directory; pass --data-dir")?;
    Ok(dirs.data_dir().to_path_buf())
}

/// Validates a base URL: `http(s)://host:port` with nothing after it.
pub fn normalize_base_url(value: &str) -> anyhow::Result<String> {
    let trimmed = value.trim_end_matches('/');
    let rest = trimmed
        .strip_prefix("http://")
        .or_else(|| trimmed.strip_prefix("https://"))
        .context("base_url must start with http:// or https://")?;
    if rest.is_empty()
        || rest.contains('/')
        || rest.contains('?')
        || rest.contains('#')
        || rest.contains('@')
    {
        bail!("base_url must be scheme://host:port with no path, query, or credentials");
    }
    Ok(trimmed.to_string())
}

impl Config {
    pub fn load(
        config_path: Option<&Path>,
        data_dir: Option<&Path>,
        listen: Option<&str>,
        base_url: Option<&str>,
    ) -> anyhow::Result<Config> {
        let data_dir = match data_dir {
            Some(d) => d.to_path_buf(),
            None => default_data_dir()?,
        };
        let layout = Layout::new(&data_dir);
        let file_path = config_path
            .map(Path::to_path_buf)
            .unwrap_or_else(|| layout.config_file());
        let file: ConfigFile = match std::fs::read_to_string(&file_path) {
            Ok(text) => {
                toml::from_str(&text).with_context(|| format!("reading {}", file_path.display()))?
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && config_path.is_none() => {
                ConfigFile::default()
            }
            Err(e) => return Err(e).with_context(|| format!("reading {}", file_path.display())),
        };
        let listen_text = listen
            .map(str::to_string)
            .or(file.listen.clone())
            .unwrap_or_else(|| DEFAULT_LISTEN.to_string());
        let listen: SocketAddr = listen_text
            .parse()
            .with_context(|| format!("listen address '{listen_text}' is not host:port"))?;
        if !listen.ip().is_loopback() {
            bail!(
                "Branvil listens on loopback only ({listen} is not); other machines reach it through OpenSSH port forwarding"
            );
        }
        let base_url = match base_url.map(str::to_string).or(file.base_url.clone()) {
            Some(url) => Some(normalize_base_url(&url)?),
            None if listen.port() != 0 => Some(format!("http://{listen}")),
            None => None,
        };
        Ok(Config {
            data_dir,
            file: file_path,
            listen,
            base_url,
            storage: file.storage,
            limits: file.limits,
            ci: file.ci,
            upstream: file.upstream,
        })
    }

    pub fn layout(&self) -> Layout {
        Layout::new(&self.data_dir)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base_url_is_scheme_host_port_only() {
        assert_eq!(
            normalize_base_url("http://127.0.0.1:7878/").unwrap(),
            "http://127.0.0.1:7878"
        );
        assert!(normalize_base_url("http://127.0.0.1:7878/x").is_err());
        assert!(normalize_base_url("ftp://x").is_err());
        assert!(normalize_base_url("http://u:p@x:1").is_err());
    }

    #[test]
    fn non_loopback_listen_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        assert!(Config::load(None, Some(dir.path()), Some("0.0.0.0:7878"), None).is_err());
        let c = Config::load(None, Some(dir.path()), Some("127.0.0.1:9000"), None).unwrap();
        assert_eq!(c.base_url.as_deref(), Some("http://127.0.0.1:9000"));
        assert_eq!(c.storage.cache_size_mib, 32);
    }

    #[test]
    fn repo_dirs_keep_the_workspace() {
        let l = Layout::new(Path::new("/d"));
        assert_eq!(l.repo_dir("acme/demo"), Path::new("/d/repos/acme/demo.git"));
    }
}
