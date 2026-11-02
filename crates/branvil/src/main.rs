//! Branvil: a lightweight local code hosting service (Git, tickets, pull
//! requests) for GuildBotics.

mod app;
mod auth;
mod autoland;
mod backup;
mod ci;
mod cli;
mod config;
mod domain;
mod error;
mod events;
mod git;
mod hooks;
mod http;
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
mod webhook;

use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};

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

#[derive(Args, Clone)]
struct DataArgs {
    /// Configuration file [default: <data dir>/branvil.toml]
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    /// Data directory [default: the OS data directory for "branvil"]
    #[arg(long, env = "BRANVIL_DATA_DIR", global = true)]
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
    /// Open the GUI in the browser with a single-use link (on the Hub machine, as the owner)
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

fn main() -> std::process::ExitCode {
    // Started by Git as GIT_ASKPASS when talking to an upstream host.
    if let Some(code) = upstream::askpass_main() {
        return code;
    }
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("BRANVIL_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("branvil=info")),
        )
        .with_writer(std::io::stderr)
        .init();
    let cli = Cli::parse();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    let result = runtime.block_on(run(cli));
    match result {
        Ok(code) => std::process::ExitCode::from(code),
        Err(e) => {
            eprintln!("branvil: {e:#}");
            std::process::ExitCode::FAILURE
        }
    }
}

async fn run(cli: Cli) -> anyhow::Result<u8> {
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
            cli::serve(config).await
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

/// The configuration, when one can be read, for default client settings.
fn data_config(data: &DataArgs) -> Option<config::Config> {
    config::Config::load(data.config.as_deref(), data.data_dir.as_deref(), None, None).ok()
}
