//! Command-line operations: `init` and `admin` work on the data directory
//! directly (the service must be stopped; redb refuses a second opener), and
//! `serve` runs the service. The member, workspace, repo, backup, and
//! open commands are clients of the running service's API, so administration
//! never bypasses the domain rules and works while the service runs.

use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{Context, bail};
use clap::Subcommand;
use serde_json::{Value, json};

use crate::ClientArgs;
use crate::app::App;
use crate::config::{self, Config, ConfigFile, Layout};
use crate::domain::members::{self, NewMember};
use crate::model::MemberKind;
use crate::store::{self, Rd, Store};
use crate::util::encode_segment;

#[derive(Subcommand)]
pub enum MemberCommand {
    /// List members
    List,
    /// Create an agent or a runner
    Create {
        name: String,
        /// Shown name [default: the name]
        #[arg(long)]
        display_name: Option<String>,
        /// agent or runner
        #[arg(long, default_value = "agent")]
        kind: String,
    },
    /// Issue a token for a member (printed once)
    Token {
        name: String,
        #[arg(long, default_value = "cli")]
        label: String,
    },
    /// Set a member's account on an upstream host: a token, or a GitHub App's identity (owner)
    Account {
        name: String,
        #[arg(long, default_value = "github")]
        provider: String,
        /// Name of an environment variable holding the account's token (never pass it as an argument)
        #[arg(long, required_unless_present = "app_id", conflicts_with = "app_id")]
        token_env: Option<String>,
        /// The app's id (with --installation-id and --private-key-file)
        #[arg(long, requires_all = ["installation_id", "private_key_file"])]
        app_id: Option<String>,
        /// The id of the app's installation that reaches the repositories
        #[arg(long, requires = "app_id")]
        installation_id: Option<String>,
        /// The app's private key (PEM file)
        #[arg(long, requires = "app_id")]
        private_key_file: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
pub enum RepoCommand {
    /// Create an empty managed repository in a workspace
    Create {
        /// Repository path <workspace>/<name>, e.g. acme/demo
        path: String,
        #[arg(long, default_value = "main")]
        default_branch: String,
        /// A label its tickets may carry (repeatable)
        #[arg(long = "label")]
        labels: Vec<String>,
    },
    /// Make a repository follow an upstream repository and publish to it; an empty repository is filled with its default branch (owner)
    Upstream {
        /// Repository path <workspace>/<name>, e.g. acme/demo
        path: String,
        /// The upstream repository, e.g. acme/demo on GitHub
        #[arg(long)]
        repository: String,
        #[arg(long, default_value = "github")]
        provider: String,
        /// The member whose account reads it [default: kept, or picked for a new upstream]
        #[arg(long)]
        reader: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum WorkspaceCommand {
    /// Create a workspace: its repositories live under it, and it has one board
    Create {
        /// Workspace name, e.g. acme
        name: String,
    },
}

#[derive(Subcommand)]
pub enum AdminCommand {
    /// Send text to the configured webhook without opening the database;
    /// works while the service is running or stopped; no-op if unset
    Notify { text: String },
    /// Check the database structure and that every index matches the records;
    /// a passing check also clears the mark an upgrade leaves, which
    /// `branvil serve` refuses
    Check,
    /// Rebuild every derived index from the records
    Reindex,
    /// Upgrade the database from the previous schema version, keeping a copy
    /// of it as it was (stop the service and take a backup first). Exits 3
    /// when the database was upgraded, now or before, and has not passed
    /// `branvil admin check` since; 0 when it already has this version and
    /// is checked; 1 when it failed, which leaves it unchanged
    Upgrade,
}

/// The exit status of `branvil admin upgrade` for a database that was
/// upgraded and still needs `branvil admin check`.
pub const UPGRADED: u8 = 3;

pub fn init(
    config_path: Option<&Path>,
    data_dir: Option<&Path>,
    owner: &str,
    display_name: Option<String>,
    listen: &str,
    base_url: Option<&str>,
) -> anyhow::Result<()> {
    let data_dir = match data_dir {
        Some(d) => d.to_path_buf(),
        None => config::default_data_dir()?,
    };
    let layout = Layout::new(&data_dir);
    layout.create()?;
    let config_file = config_path
        .map(Path::to_path_buf)
        .unwrap_or_else(|| layout.config_file());
    if !config_file.exists() {
        let listen_addr: std::net::SocketAddr =
            listen.parse().context("--listen must be host:port")?;
        if !listen_addr.ip().is_loopback() {
            bail!("Branvil listens on loopback only");
        }
        let file = ConfigFile {
            listen: Some(listen.to_string()),
            // With port 0 (tests) the base URL follows the bound address.
            base_url: match base_url {
                Some(u) => Some(config::normalize_base_url(u)?),
                None if listen_addr.port() == 0 => None,
                None => Some(format!("http://{listen}")),
            },
            ..ConfigFile::default()
        };
        std::fs::write(&config_file, toml::to_string_pretty(&file)?)?;
    }
    let config = Config::load(Some(&config_file), Some(&data_dir), None, None)?;
    let store = Store::open(&layout.db(), config.storage.cache_size_mib << 20, 1)?;
    let existing = store.read_sync(|r| Ok(r.prefix_keys(store::MEMBERS, &[])?.len()))?;
    if existing > 0 {
        bail!(
            "{} already has members; use `branvil member` against the running service",
            data_dir.display()
        );
    }
    let input = NewMember {
        name: owner.to_string(),
        display_name,
        kind: MemberKind::Owner,
    };
    store.write_sync(|w| members::insert_member(w, &input))?;
    crate::auth::ensure_owner_token(&store, &layout)?;
    println!("Initialized Branvil in {}", data_dir.display());
    println!("Configuration: {}", config_file.display());
    println!("Owner: {owner}");
    println!("Owner token: {}", layout.owner_token().display());
    Ok(())
}

/// Runs the service until a signal stops it; the exit status is 3 when the
/// stop interrupted running CI jobs (`Ci::stop`).
pub async fn serve(config: Config) -> anyhow::Result<u8> {
    let (interrupt, terminate) = stop_signals()?;
    let layout = config.layout();
    layout.create()?;
    let store = Store::open(
        &layout.db(),
        config.storage.cache_size_mib << 20,
        config.storage.read_concurrency,
    )?;
    if store.needs_check()? {
        bail!(
            "{} was upgraded and has not passed `branvil admin check` since; run it with the service stopped, then start the service",
            layout.db().display()
        );
    }
    // After the database: a service still running refuses the start there.
    let (mut interrupt, mut terminate) = (Box::pin(interrupt), Box::pin(terminate));
    tokio::select! {
        held = crate::git::hold(&layout) => held?,
        _ = &mut interrupt => return Ok(0),
        _ = &mut terminate => return Ok(0),
    }
    let cursor_key = layout.load_cursor_key()?;
    crate::auth::ensure_owner_token(&store, &layout)?;
    crate::hooks::install(&layout)?;
    let listener = tokio::net::TcpListener::bind(config.listen)
        .await
        .with_context(|| format!("cannot listen on {}", config.listen))?;
    let bound = listener.local_addr()?;
    let base_url = config
        .base_url
        .clone()
        .unwrap_or_else(|| format!("http://{bound}"));
    let app = App::new(
        config,
        base_url.clone(),
        store,
        cursor_key,
        format!("http://{bound}"),
    );
    app.git
        .version()
        .await
        .map_err(|e| anyhow::anyhow!("git is not usable: {}", e.message))?;

    // Every managed repository carries Branvil's configuration (hooks path,
    // hidden refs) even if it was changed on disk, and no lock file a killed
    // Git left (no Git of a stopped service runs any more: `git::hold`).
    let repos: Vec<crate::model::Repo> =
        app.store.read(|r| r.prefix_all(store::REPOS, &[])).await?;
    for repo in &repos {
        let dir = app.git.repo_dir(&repo.path);
        #[cfg(unix)]
        crate::git::remove_stale_locks(&dir)?;
        app.git.configure(&dir).await?;
        app.git.set_head(&dir, &repo.default_branch).await?;
    }
    let report = crate::refs::recover(&app).await?;
    tracing::info!(
        "startup recovery: {} operation(s) completed, {} dropped, {} ref change(s) recorded",
        report.completed,
        report.rolled_back,
        report.rescanned_changes
    );

    crate::ci::Ci::start(app.clone());
    crate::ci::supervise::start(app.clone(), format!("http://{bound}"));
    crate::upstream::start(app.clone());
    crate::autoland::start(app.clone());
    crate::domain::attachments::start(app.clone());

    let router = crate::http::router(app.clone());
    println!("branvil listening on http://{bound} (base URL {base_url})");
    std::io::stdout().flush()?;
    // SIGTERM stops CI first (`Ci::stop`: no new jobs, the running ones
    // finish) while the server keeps serving the runners and everyone else;
    // SIGINT (Ctrl-C), also during that wait, skips it. Then the event streams
    // end and the server stops accepting; requests in flight get
    // `SHUTDOWN_GRACE` to finish, then the stop goes on without waiting for
    // them (their tasks live until the process exits). The repository
    // operations already started (`App::finish`), a dropped request's or a
    // background task's included, end first, each within its own limits; none
    // starts meanwhile. It exits with 3 when the stop interrupted jobs.
    let (stopping, mut stopped) = tokio::sync::watch::channel(None);
    let events = app.events.clone();
    let ci = app.ci.clone();
    let server = axum::serve(listener, router).with_graceful_shutdown(async move {
        tokio::pin!(interrupt);
        let mut cut = 0;
        tokio::select! {
            _ = &mut interrupt => {},
            _ = terminate => {
                tracing::info!("stopping: no new CI jobs; waiting for the running ones (SIGINT skips the wait)");
                tokio::select! {
                    _ = &mut interrupt => {},
                    stopped = ci.stop() => match stopped {
                        Ok(n) => cut = n,
                        Err(e) => tracing::warn!("stopping: cannot wait for the running CI jobs: {e}"),
                    },
                }
            },
        }
        tracing::info!("shutting down");
        events.close();
        stopping.send_replace(Some(cut));
    });
    let served = tokio::select! {
        served = server => served,
        _ = async {
            let _ = stopped.wait_for(Option::is_some).await;
            tokio::time::sleep(SHUTDOWN_GRACE).await;
        } => {
            tracing::warn!(
                "requests still open {} s after the shutdown signal; exiting",
                SHUTDOWN_GRACE.as_secs()
            );
            Ok(())
        }
    };
    let cut = stopped.borrow().unwrap_or(0);
    let code = if cut > 0 { 3 } else { 0 };
    if cut > 0 {
        tracing::warn!(
            "stopped with {cut} CI job(s) still running after ci.stop_wait_secs: they were interrupted, and queued again in their place unless interrupted before"
        );
    }
    let close = app.close();
    tokio::pin!(close);
    if futures_util::poll!(close.as_mut()).is_pending() {
        // scripts/serve.sh waits for a serve that says this (as for CI jobs).
        let mut out = std::io::stdout().lock();
        let _ = writeln!(
            out,
            "branvil stopping: waiting for the started repository operations to finish"
        )
        .and_then(|_| out.flush());
        drop(out);
        tracing::info!("stopping: waiting for the started repository operations");
        close.await;
    }
    served?;
    Ok(code)
}

/// How long requests in flight may take after a shutdown signal.
const SHUTDOWN_GRACE: std::time::Duration = std::time::Duration::from_secs(5);

/// SIGINT and SIGTERM, taken over before `serve` starts any repository work:
/// their default action would end the process in the middle of it.
fn stop_signals() -> std::io::Result<(impl Future<Output = ()>, impl Future<Output = ()>)> {
    #[cfg(unix)]
    let signals = {
        use tokio::signal::unix::{SignalKind, signal};
        let mut interrupt = signal(SignalKind::interrupt())?;
        let mut terminate = signal(SignalKind::terminate())?;
        (
            async move {
                interrupt.recv().await;
            },
            async move {
                terminate.recv().await;
            },
        )
    };
    #[cfg(windows)]
    let signals = {
        // Registered now, unlike `tokio::signal::ctrl_c`.
        let mut interrupt = tokio::signal::windows::ctrl_c()?;
        (
            async move {
                interrupt.recv().await;
            },
            std::future::pending::<()>(),
        )
    };
    Ok(signals)
}

pub async fn admin(
    command: AdminCommand,
    config_path: Option<&Path>,
    data_dir: Option<&Path>,
) -> anyhow::Result<u8> {
    if let AdminCommand::Notify { text } = command {
        let dir = data_dir
            .map(Path::to_path_buf)
            .map(Ok)
            .unwrap_or_else(config::default_data_dir)?;
        crate::webhook::send(&crate::webhook::client(), &Layout::new(&dir), &text)
            .await
            .map_err(anyhow::Error::msg)?;
        return Ok(0);
    }
    let config = Config::load(config_path, data_dir, None, None)?;
    let layout = config.layout();
    if !layout.db().exists() {
        bail!(
            "{} does not exist; run `branvil init` first",
            layout.db().display()
        );
    }
    let open = || Store::open(&layout.db(), config.storage.cache_size_mib << 20, 1);
    match command {
        AdminCommand::Notify { .. } => unreachable!(),
        AdminCommand::Upgrade => {
            let done = crate::upgrade::upgrade(&layout)?;
            println!("{}", store::upgrade_report(&layout.db(), &done));
            if done.is_some() {
                return Ok(UPGRADED);
            }
            if store::needs_check(&layout.db())? {
                println!(
                    "{} was upgraded and has not passed `branvil admin check` since",
                    layout.db().display()
                );
                return Ok(UPGRADED);
            }
            Ok(0)
        }
        AdminCommand::Check => {
            let store = open()?;
            let problems = store.read_sync(crate::model::index_problems)?;
            let pending = store.read_sync(|r| Ok(r.prefix_keys(store::JOURNAL, &[])?.len()))?;
            let structure = store.check_integrity()?;
            println!(
                "database structure: {}",
                if structure { "ok" } else { "repaired" }
            );
            println!("pending ref operations: {pending} (finished or dropped at the next start)");
            if problems.is_empty() {
                println!("indexes: ok");
                open()?.checked()?;
                Ok(0)
            } else {
                for p in &problems {
                    println!("index problem: {p}");
                }
                println!(
                    "{} index problem(s); `branvil admin reindex` rebuilds the indexes",
                    problems.len()
                );
                Ok(1)
            }
        }
        AdminCommand::Reindex => {
            let n = open()?.write_sync(crate::model::rebuild_indexes)?;
            println!("rebuilt {n} index entries");
            Ok(0)
        }
    }
}

struct Client {
    url: String,
    token: String,
    http: reqwest::Client,
}

impl Client {
    fn new(args: &ClientArgs, config: &Option<Config>) -> anyhow::Result<Self> {
        let url = args
            .url
            .clone()
            .or_else(|| config.as_ref().and_then(|c| c.base_url.clone()))
            .unwrap_or_else(|| format!("http://{}", config::DEFAULT_LISTEN));
        let token = client_token(args.token.clone(), config)?;
        Ok(Self {
            url: config::normalize_base_url(&url)?,
            token,
            http: reqwest::Client::new(),
        })
    }

    async fn call(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> anyhow::Result<Value> {
        let mut req = self
            .http
            .request(method, format!("{}/api/v1{path}", self.url))
            .bearer_auth(&self.token);
        if let Some(b) = body {
            req = req.json(&b);
        }
        let resp = req
            .send()
            .await
            .with_context(|| format!("Branvil is not reachable at {}", self.url))?;
        let status = resp.status();
        let value: Value = resp.json().await.unwrap_or(Value::Null);
        if !status.is_success() {
            let message = value["error"]["message"]
                .as_str()
                .unwrap_or("request failed");
            bail!("{} ({})", message, status.as_u16());
        }
        Ok(value)
    }
}

/// The token a client command uses: `--token` or `BRANVIL_TOKEN`, else the
/// owner's token file of the data directory (on the Hub machine).
pub fn client_token(given: Option<String>, config: &Option<Config>) -> anyhow::Result<String> {
    if let Some(token) = given {
        return Ok(token);
    }
    let path = config
        .as_ref()
        .context("a token is needed: pass --token or set BRANVIL_TOKEN")?
        .layout()
        .owner_token();
    let text = std::fs::read_to_string(&path).with_context(|| {
        format!(
            "a token is needed: pass --token, set BRANVIL_TOKEN, or run on the Hub machine ({} was not readable)",
            path.display()
        )
    })?;
    Ok(text.trim().to_string())
}

/// `branvil open`: asks the service for a single-use entry link of the
/// owner and opens it with the system's handler (or prints it).
pub async fn open(print: bool, args: &ClientArgs, config: &Option<Config>) -> anyhow::Result<()> {
    let c = Client::new(args, config)?;
    let v = c
        .call(reqwest::Method::POST, "/session/links", None)
        .await?;
    let url = v["url"].as_str().context("the service returned no link")?;
    if print {
        println!("{url}");
        return Ok(());
    }
    // The URL is one argument of the handler; no shell is involved.
    #[cfg(target_os = "macos")]
    let mut cmd = std::process::Command::new("open");
    #[cfg(windows)]
    let mut cmd = {
        let mut c = std::process::Command::new("rundll32");
        c.arg("url.dll,FileProtocolHandler");
        c
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut cmd = std::process::Command::new("xdg-open");
    let status = cmd
        .arg(url)
        .status()
        .context("cannot start the browser; use `branvil open --print`")?;
    if !status.success() {
        bail!("the browser did not open the link ({status}); use `branvil open --print`");
    }
    println!("Opened the Branvil GUI in the browser.");
    Ok(())
}

fn print(value: &Value) {
    println!(
        "{}",
        serde_json::to_string_pretty(value).unwrap_or_default()
    );
}

pub async fn member(
    command: MemberCommand,
    args: &ClientArgs,
    config: &Option<Config>,
) -> anyhow::Result<()> {
    let c = Client::new(args, config)?;
    match command {
        MemberCommand::List => print(
            &c.call(reqwest::Method::GET, "/members?limit=100", None)
                .await?,
        ),
        MemberCommand::Create {
            name,
            display_name,
            kind,
        } => print(
            &c.call(
                reqwest::Method::POST,
                "/members",
                Some(json!({"name": name, "display_name": display_name, "kind": kind})),
            )
            .await?,
        ),
        MemberCommand::Token { name, label } => {
            let v = c
                .call(
                    reqwest::Method::POST,
                    &format!("/members/{}/tokens", encode_segment(&name)),
                    Some(json!({"label": label})),
                )
                .await?;
            println!("{}", v["token"].as_str().unwrap_or_default());
        }
        MemberCommand::Account {
            name,
            provider,
            token_env,
            app_id,
            installation_id,
            private_key_file,
        } => {
            let body = match (token_env, app_id, installation_id, private_key_file) {
                (Some(var), ..) => json!({"token": std::env::var(&var)
                    .with_context(|| format!("environment variable {var} is not set"))?}),
                (None, Some(app_id), Some(installation_id), Some(file)) => {
                    let private_key = std::fs::read_to_string(&file)
                        .with_context(|| format!("cannot read {}", file.display()))?;
                    json!({"app": {"app_id": app_id, "installation_id": installation_id, "private_key": private_key}})
                }
                _ => unreachable!("clap requires one complete form"),
            };
            print(
                &c.call(
                    reqwest::Method::PUT,
                    &format!(
                        "/members/{}/accounts/{}",
                        encode_segment(&name),
                        encode_segment(&provider)
                    ),
                    Some(body),
                )
                .await?,
            );
        }
    }
    Ok(())
}

pub async fn repo(
    command: RepoCommand,
    args: &ClientArgs,
    config: &Option<Config>,
) -> anyhow::Result<()> {
    let c = Client::new(args, config)?;
    match command {
        RepoCommand::Create {
            path,
            default_branch,
            labels,
        } => print(
            &c.call(
                reqwest::Method::POST,
                "/repos",
                Some(json!({"path": path, "default_branch": default_branch, "labels": labels})),
            )
            .await?,
        ),
        RepoCommand::Upstream {
            path,
            repository,
            provider,
            reader,
        } => {
            let mut body = json!({"provider": provider, "repository": repository});
            if let Some(reader) = reader {
                body["reader"] = json!(reader);
            }
            print(
                &c.call(
                    reqwest::Method::PUT,
                    &format!("/repos/{}/upstream", encode_segment(&path)),
                    Some(body),
                )
                .await?,
            )
        }
    }
    Ok(())
}

/// `branvil backup`: asks the running service to write a backup. The
/// directory is on the service's machine; a relative path is made absolute
/// here first.
pub async fn backup(to: &Path, args: &ClientArgs, config: &Option<Config>) -> anyhow::Result<()> {
    let c = Client::new(args, config)?;
    let to = if to.is_absolute() {
        to.to_path_buf()
    } else {
        std::env::current_dir()?.join(to)
    };
    let v = c
        .call(
            reqwest::Method::POST,
            "/admin/backup",
            Some(json!({"to": to})),
        )
        .await?;
    println!("backup written to {}", to.display());
    println!(
        "repositories: {}, files: {}",
        v["repositories"].as_array().map_or(0, Vec::len),
        v["files"].as_array().map_or(0, Vec::len)
    );
    for a in v["account_tokens_excluded"]
        .as_array()
        .into_iter()
        .flatten()
    {
        println!(
            "note: the {} credential of {} is not in the backup",
            a["provider"].as_str().unwrap_or(""),
            a["member"].as_str().unwrap_or("")
        );
    }
    Ok(())
}

pub async fn workspace(
    command: WorkspaceCommand,
    args: &ClientArgs,
    config: &Option<Config>,
) -> anyhow::Result<()> {
    let c = Client::new(args, config)?;
    match command {
        WorkspaceCommand::Create { name } => print(
            &c.call(
                reqwest::Method::POST,
                "/workspaces",
                Some(json!({"name": name})),
            )
            .await?,
        ),
    }
    Ok(())
}
