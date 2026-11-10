//! Branvil: a lightweight local code hosting service (Git, tickets, pull
//! requests) for GuildBotics.
//!
//! The `branvil` executable runs `main`; another executable may run the same
//! commands under one of its own (`main` with its `Invocation`) and the
//! service in its own process (`serve_with_data_api`), each with the vault
//! of that executable (`vault::Vault`).

pub mod api;
mod app;
mod auth;
mod autoland;
mod backup;
pub mod browser_session;
mod ci;
mod cli;
mod config;
mod domain;
mod error;
mod events;
mod git;
mod hooks;
mod http;
pub mod hub;
mod invocation;
mod landing;
mod model;
mod rank;
mod references;
mod refs;
mod runner;
mod smart_http;
mod store;
mod upgrade;
mod upstream;
mod util;
pub mod vault;
mod webhook;

use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Arc;

use clap::{Args, CommandFactory, FromArgMatches, Parser, Subcommand};

pub use cli::stop_signals;
pub use invocation::Invocation;
pub use runner::executor::set_runtime_binary;
pub use upstream::askpass_main;

#[derive(Parser)]
#[command(
    name = "branvil",
    version,
    about = "Branvil: local code hosting (Git over Smart HTTP, tickets, boards, pull requests, CI) for GuildBotics",
    long_about = "Branvil: local code hosting (Git over Smart HTTP, tickets, boards, pull requests, CI) for GuildBotics.\n\n\
First run: `branvil init` creates the data directory, the configuration, and the owner, the one person who uses Branvil; \
the owner's token is kept in <data dir>/secrets/owner.token. Then `branvil serve` starts the service, and `branvil open` opens the GUI \
without a sign-in screen. The member, workspace, repo, backup, and open commands talk to the running service with the owner's token \
from that file (or --token / BRANVIL_TOKEN)."
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

/// The CLI definition, also used by GuildBotics' generated reference.
pub fn cli_command() -> clap::Command {
    Cli::command()
}

/// Names the data directory when no `--data-dir` does.
const DATA_DIR_ENV: &str = "BRANVIL_DATA_DIR";

#[derive(Args, Clone)]
struct DataArgs {
    /// Configuration file [default: <data dir>/branvil.toml]
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    /// Data directory [default: the OS data directory for "branvil"]
    #[arg(long, env = DATA_DIR_ENV, global = true)]
    data_dir: Option<PathBuf>,
}

#[derive(Args, Clone)]
pub struct ClientArgs {
    /// Base URL of the running service [default: base_url of the configuration, else http://127.0.0.1:7878]
    #[arg(long, env = "BRANVIL_URL", global = true)]
    url: Option<String>,
    /// Token [default, except for `branvil runner`, `runner pull`, and `runner doctor`: the owner's token in <data dir>/secrets/owner.token]
    #[arg(long, env = "BRANVIL_TOKEN", hide_env_values = true, global = true)]
    token: Option<String>,
}

#[derive(Subcommand)]
enum Command {
    /// Create the data directory, the configuration, and the owner (its token goes to <data dir>/secrets/owner.token)
    Init {
        /// Member name of the owner, the one person who uses Branvil
        #[arg(long, default_value = "owner")]
        owner: String,
        /// Display name of the owner
        #[arg(long)]
        display_name: Option<String>,
        /// Loopback address written to the configuration
        #[arg(long, default_value = config::DEFAULT_LISTEN)]
        listen: String,
        /// Permanent base URL written to the configuration [default: http://<listen>]
        #[arg(long)]
        base_url: Option<String>,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Run the service
    Serve {
        /// Loopback address to listen on (port 0 picks a free port) [default: listen of the configuration, else 127.0.0.1:7878]
        #[arg(long)]
        listen: Option<String>,
        /// Permanent base URL [default: base_url of the configuration, else http://<bound address>]
        #[arg(long)]
        base_url: Option<String>,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Open Branvil with a single-use link (the shared GuildBotics entry on an integrated server)
    Open {
        /// Print the link (valid once, for 60 seconds) instead of opening a browser
        #[arg(long)]
        print: bool,
        #[command(flatten)]
        client: ClientArgs,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Manage agents, runners, and tokens (through the running service)
    Member {
        #[command(subcommand)]
        command: cli::MemberCommand,
        #[command(flatten)]
        client: ClientArgs,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Manage repositories (through the running service)
    Repo {
        #[command(subcommand)]
        command: cli::RepoCommand,
        #[command(flatten)]
        client: ClientArgs,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Manage workspaces (through the running service)
    Workspace {
        #[command(subcommand)]
        command: cli::WorkspaceCommand,
        #[command(flatten)]
        client: ClientArgs,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Run the CI runner, a separate process that executes jobs in microVMs (docs/ci.md)
    #[command(args_conflicts_with_subcommands = true)]
    Runner {
        #[command(subcommand)]
        command: Option<runner::RunnerCommand>,
        /// Per-job work directories and the dependency cache [default: ci.executor.work_dir of the configuration, else ~/.branvil/runner]
        #[arg(long)]
        work_dir: Option<PathBuf>,
        #[command(flatten)]
        client: ClientArgs,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Offline maintenance of the data directory (the service must be stopped)
    Admin {
        #[command(subcommand)]
        command: cli::AdminCommand,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Write a consistent backup of the running service into a new directory on its machine
    Backup {
        /// Directory to create (must not exist or be empty; on the machine the service runs on)
        #[arg(long)]
        to: PathBuf,
        #[command(flatten)]
        client: ClientArgs,
        #[command(flatten)]
        data: DataArgs,
    },
    /// Restore a backup into a new data directory and check it (the service need not run)
    Restore {
        /// Backup directory written by `branvil backup`
        #[arg(long)]
        from: PathBuf,
        /// New data directory to fill (must not exist or be empty)
        #[arg(long, env = "BRANVIL_DATA_DIR")]
        data_dir: PathBuf,
    },
    /// Git hook entry point (run by Git inside a push; not for direct use)
    #[command(hide = true)]
    Hook { name: String },
}

/// Runs the command line `argv` (the program first) as Branvil's commands,
/// started as `invocation` names them. A process that is Git's `GIT_ASKPASS`
/// runs `askpass_main` first.
pub fn main(
    invocation: Invocation,
    vault: Arc<dyn vault::Vault>,
    argv: impl IntoIterator<Item = OsString>,
) -> std::process::ExitCode {
    main_with_client_url(invocation, vault, argv, None)
}

/// Run commands with the embedding host's default client origin.
pub fn main_with_client_url(
    invocation: Invocation,
    vault: Arc<dyn vault::Vault>,
    argv: impl IntoIterator<Item = OsString>,
    client_url: Option<String>,
) -> std::process::ExitCode {
    // An executable that runs Branvil under its own may have set its own.
    let _ = tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("BRANVIL_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("branvil=info")),
        )
        .with_writer(std::io::stderr)
        .try_init();
    invocation.install();
    let mut cli = match invocation.bin_name() {
        None => Cli::parse_from(argv),
        Some(name) => {
            let mut command = Cli::command().bin_name(name);
            let matches = command
                .try_get_matches_from_mut(argv)
                .unwrap_or_else(|e| e.exit());
            Cli::from_arg_matches(&matches).unwrap_or_else(|e| e.format(&mut command).exit())
        }
    };
    match &mut cli.command {
        Command::Open { client, .. }
        | Command::Member { client, .. }
        | Command::Repo { client, .. }
        | Command::Workspace { client, .. }
        | Command::Runner { client, .. }
        | Command::Backup { client, .. } => {
            client.url = client.url.take().or(client_url);
        }
        _ => {}
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    let result = runtime.block_on(run(cli, vault));
    match result {
        Ok(code) => std::process::ExitCode::from(code),
        Err(e) => {
            eprintln!("branvil: {e:#}");
            std::process::ExitCode::FAILURE
        }
    }
}

async fn run(cli: Cli, vault: Arc<dyn vault::Vault>) -> anyhow::Result<u8> {
    match cli.command {
        Command::Init {
            owner,
            display_name,
            listen,
            base_url,
            data,
        } => cli::init(
            data.config.as_deref(),
            data.data_dir.as_deref(),
            &owner,
            display_name,
            &listen,
            base_url.as_deref(),
        )
        .map(|_| 0),
        Command::Serve {
            listen,
            base_url,
            data,
        } => {
            let config = config::Config::load(
                data.config.as_deref(),
                data.data_dir.as_deref(),
                listen.as_deref(),
                base_url.as_deref(),
            )?;
            let (interrupt, terminate) = cli::stop_signals()?;
            cli::serve(config, vault, interrupt, terminate, true).await
        }
        Command::Open {
            print,
            client,
            data,
        } => cli::open(print, &client, &data_config(&data))
            .await
            .map(|_| 0),
        Command::Member {
            command,
            client,
            data,
        } => cli::member(command, &client, &data_config(&data))
            .await
            .map(|_| 0),
        Command::Repo {
            command,
            client,
            data,
        } => cli::repo(command, &client, &data_config(&data))
            .await
            .map(|_| 0),
        Command::Workspace {
            command,
            client,
            data,
        } => cli::workspace(command, &client, &data_config(&data))
            .await
            .map(|_| 0),
        Command::Runner {
            command,
            work_dir,
            client,
            data,
        } => {
            // A configuration named explicitly (the supervised runner gets the
            // service's) has to be read: its limits apply to every job.
            let config = match &data.config {
                Some(file) => Some(config::Config::load(
                    Some(file),
                    data.data_dir.as_deref(),
                    None,
                    None,
                )?),
                None => data_config(&data),
            };
            let settings =
                runner::Settings::new(client.url, client.token, work_dir, config.as_ref())?;
            match command {
                None => runner::run(settings).await.map(|_| 0),
                Some(runner::RunnerCommand::Register { name }) => {
                    let token = cli::client_token(settings.token.clone(), &config)?;
                    runner::register(&settings.url, &token, &name)
                        .await
                        .map(|_| 0)
                }
                Some(runner::RunnerCommand::Pull { image }) => {
                    runner::pull(&settings, &image).await.map(|_| 0)
                }
                Some(runner::RunnerCommand::Doctor) => runner::doctor(&settings).await,
                Some(runner::RunnerCommand::InstallMsb { from }) => {
                    runner::install_msb(&from).map(|_| 0)
                }
            }
        }
        Command::Admin { command, data } => {
            cli::admin(command, data.config.as_deref(), data.data_dir.as_deref()).await
        }
        Command::Backup { to, client, data } => cli::backup(&to, &client, &data_config(&data))
            .await
            .map(|_| 0),
        Command::Restore { from, data_dir } => backup::restore(&from, &data_dir).await,
        Command::Hook { name } => Ok(hooks::run_hook(&name).await as u8),
    }
}

pub use cli::Embedded;

/// Prepare an embedded service without binding. All advertised URLs use the host origin.
pub async fn prepare_embedded(
    invocation: Invocation,
    vault: Arc<dyn vault::Vault>,
    base_url: &str,
    session: Arc<dyn browser_session::BrowserSession>,
) -> anyhow::Result<Embedded> {
    invocation.install();
    let base_url = config::normalize_base_url(base_url)?;
    let embedded = cli::prepare(serve_config()?, vault, base_url.clone(), base_url).await?;
    let _ = embedded.app.browser_session.set(session);
    Ok(embedded)
}

/// Whether `branvil init` has made the data directory `serve` would use.
pub fn initialized() -> anyhow::Result<bool> {
    Ok(serve_config()?.layout().db().exists())
}

/// The configuration `branvil serve` uses without options.
fn serve_config() -> anyhow::Result<config::Config> {
    let data_dir = match std::env::var_os(DATA_DIR_ENV) {
        // As `--data-dir` refuses it: not the current directory.
        Some(dir) if dir.is_empty() => anyhow::bail!("{DATA_DIR_ENV} is empty"),
        dir => dir.map(PathBuf::from),
    };
    config::Config::load(None, data_dir.as_deref(), None, None)
}

/// The configuration, when one can be read, for default client settings.
fn data_config(data: &DataArgs) -> Option<config::Config> {
    config::Config::load(data.config.as_deref(), data.data_dir.as_deref(), None, None).ok()
}

/// The HTTP route inventory, independent of the embedding host.
pub use http::{embedded_route_inventory, route_inventory};

/// Exact hook paths whose shared-secret authentication replaces Host/Origin checks.
pub fn hook_paths() -> impl Iterator<Item = &'static str> {
    http::HOOK_ROUTES.iter().map(|route| route.path)
}
