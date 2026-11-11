//! The `guildbotics` executable: the server, with Branvil in the same
//! process, and Branvil's commands as `guildbotics branvil`.

use std::ffi::OsString;
use std::future::Future;
use std::io::{IsTerminal, Read};
use std::process::ExitCode;
use std::sync::Arc;

use clap::{Parser, Subcommand};
use tokio::sync::watch;

/// How Branvil's commands are started on this executable: the hook scripts
/// and the automatic runner start them again.
const BRANVIL: branvil::Invocation = branvil::Invocation::under(&["branvil"]);

#[derive(Parser)]
#[command(name = "guildbotics", version, about = "GuildBotics")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    #[command(hide = true)]
    GenerateCliReference {
        #[arg(long)]
        check: bool,
    },
    /// Manage this device's isolated environment
    Environment {
        /// Select the workspace whose environment declaration to use
        #[arg(long, global = true)]
        workspace: Option<std::path::PathBuf>,
        #[command(subcommand)]
        command: EnvironmentCommand,
    },
    /// Run the server
    Server {
        #[command(subcommand)]
        command: ServerCommand,
    },
    /// Operate as a configured member through the running server
    #[command(disable_help_flag = true)]
    Member {
        /// Member subcommand and its options
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        arguments: Vec<String>,
    },
    /// Supply Git with this machine's credential for the remote
    GitCredential {
        /// Credential helper action from Git
        action: String,
    },
    /// Install this executable and the GuildBotics skill for this user
    Install,
    /// Select the active workspace for the server and AI CLI tools
    Workspace {
        #[command(subcommand)]
        command: workspace_cli::WorkspaceCommand,
    },
    /// Run a command in the selected workspace's isolated environment
    Run {
        /// Member ID or name. Overrides a member specified as COMMAND@PERSON
        #[arg(long)]
        person: Option<String>,
        /// Working directory for the command
        #[arg(long)]
        cwd: Option<std::path::PathBuf>,
        /// Command name, optionally followed by @PERSON
        command: String,
        /// Arguments for the command
        args: Vec<String>,
    },
    /// Read a workspace's recorded diagnostics
    Diagnostics(diagnostics_cli::DiagnosticsCommand),
    /// Print the binary's version
    Version,
    /// Manage workspace or shared identity secrets
    Secrets(secrets_cli::Secrets),
    /// Start or stop scheduled commands, ticket patrol, and chat events
    Service {
        #[command(subcommand)]
        command: service_cli::ServiceCommand,
    },
    /// Connect this machine's CLI through the server's consent screen
    Connect,
    /// Open the GuildBotics screens in the browser. The browser enters with a
    /// shared single-use link that expires after 60 seconds. The session also opens Branvil
    Open {
        /// Print the link instead of opening it, to paste into a browser on
        /// another machine
        #[arg(long)]
        print: bool,
        /// Open the Branvil screens
        #[arg(long)]
        branvil: bool,
    },
    /// Branvil's commands (code hosting: `guildbotics branvil --help`)
    #[command(disable_help_flag = true)]
    Branvil {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<OsString>,
    },
}

mod cli_reference;
mod diagnostics_cli;
mod environment_cli;
mod hub_cli;
mod hub_data;
#[cfg(unix)]
mod hub_update;
mod install_cli;
mod secrets_cli;
mod service_cli;
mod startup;
mod workspace_cli;
use environment_cli::EnvironmentCommand;

#[derive(Subcommand)]
enum ServerCommand {
    /// Back up or validate Hub records without starting a server.
    #[command(hide = true)]
    HubData {
        #[arg(value_enum)]
        operation: hub_data::Operation,
        #[arg(long)]
        data_dir: Option<std::path::PathBuf>,
        #[arg(long)]
        to: Option<std::path::PathBuf>,
    },
    /// Read commit checks on the local Hub; credentials are read only from disk
    #[command(hide = true)]
    HubChecks {
        #[arg(
            long,
            conflicts_with = "checkout",
            required_unless_present = "checkout"
        )]
        repo: Option<String>,
        #[arg(long)]
        checkout: Option<std::path::PathBuf>,
        #[arg(long)]
        sha: String,
    },
    /// Send stdin to the identity store's webhook, even with the server stopped
    #[command(hide = true)]
    HubNotify,
    /// Print the manual-update declaration embedded in this binary
    #[command(hide = true)]
    HubMigration,
    /// Watch origin/main of the Hub checkout and update the Hub (its LaunchAgent)
    #[command(hide = true)]
    HubAutoUpdate {
        /// The Hub checkout
        #[arg(long)]
        checkout: std::path::PathBuf,
        /// Inspect once instead of every interval
        #[arg(long)]
        once: bool,
    },
    /// Remove the Hub's build source trees that no process holds, except those kept
    #[command(hide = true)]
    HubPrune {
        /// The name of a build directory to keep
        #[arg(long)]
        keep: Vec<std::ffi::OsString>,
        /// The directory of the builds
        builds: std::path::PathBuf,
    },
    /// Run the server in the foreground until SIGINT or SIGTERM (a first
    /// SIGINT waits for the commands running, SIGTERM or a second SIGINT
    /// cancels them): GuildBotics
    /// on GUILDBOTICS_SERVER_PORT (default 7878) with its records under
    /// GUILDBOTICS_HOME (default ~/.guildbotics), and Branvil, once
    /// `guildbotics branvil init` has set it up, on the same listener
    /// (BRANVIL_DATA_DIR chooses its data directory; Branvil's listen setting
    /// is used only by `guildbotics branvil serve`)
    Start {
        /// Start services even when automatic startup is disabled
        #[arg(long)]
        services: bool,
    },
    /// Stop the server and wait for its work to end
    Stop {
        /// Cancel in-flight work and force termination if teardown takes too long
        #[arg(long)]
        force: bool,
    },
    /// Report the lock holder to the Hub's LaunchAgent script.
    #[command(hide = true)]
    HubProbe {
        #[arg(long)]
        ready: bool,
        #[arg(long, conflicts_with = "ready")]
        exit_timeout: bool,
        /// Print the source tree this executable was built from.
        #[arg(long, conflicts_with_all = ["ready", "exit_timeout"])]
        source: bool,
    },
}

fn main() -> ExitCode {
    startup::initialize();
    #[cfg(unix)]
    gb_base::home::hold_source_tree();
    // Started by Git as GIT_ASKPASS when Branvil talks to an upstream host.
    if let Some(code) = branvil::askpass_main() {
        return code;
    }
    if let Ok(data) = gb_base::home::data() {
        let binary = gb_environment::placed_files(&data.join("msb"))[0].clone();
        if let Err(error) =
            branvil::set_runtime_binary(std::path::absolute(binary).unwrap_or_default())
        {
            eprintln!("guildbotics: {error}");
            return ExitCode::FAILURE;
        }
    }
    match Cli::parse().command {
        Command::GenerateCliReference { check } => outcome(cli_reference::run(check).map(|()| 0)),
        Command::Environment { command, workspace } => {
            outcome(block(environment_cli::run(command, workspace)).map(|()| 0))
        }
        Command::Branvil { args } => branvil_main(std::iter::once(OsString::new()).chain(args)),
        Command::Member { arguments } => outcome(block(gb_server::client::member::run(arguments))),
        Command::GitCredential { action } => {
            outcome(block(gb_server::client::git_credential(&action)).map(|()| 0))
        }
        Command::Install => outcome(block(install_cli::run()).map(|()| 0)),
        Command::Workspace { command } => outcome(block(workspace_cli::run(command)).map(|()| 0)),
        Command::Run {
            person,
            cwd,
            command,
            args,
        } => outcome(run_command(&command, args, person, cwd).map(|()| 0)),
        Command::Diagnostics(command) => outcome(block(diagnostics_cli::run(command)).map(|()| 0)),
        Command::Version => {
            println!("{}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Command::Secrets(command) => outcome(block(secrets_cli::run(command)).map(|()| 0)),
        Command::Service { command } => outcome(block(service_cli::run(command)).map(|()| 0)),
        Command::Connect => outcome(
            block(gb_server::client::connect::run(|url| {
                browse(url);
            }))
            .map(|()| 0),
        ),
        Command::Open {
            print,
            branvil: true,
        } => {
            let mut args = vec![OsString::new(), OsString::from("open")];
            if print {
                args.push("--print".into());
            }
            branvil_main(args)
        }
        Command::Open {
            print,
            branvil: false,
        } => open(print),
        Command::Server {
            command:
                ServerCommand::HubChecks {
                    repo,
                    checkout,
                    sha,
                },
        } => outcome(block(hub_cli::checks(repo, checkout, sha)).map(|()| 0)),
        Command::Server {
            command: ServerCommand::HubNotify,
        } => outcome(block(hub_cli::notify()).map(|()| 0)),
        Command::Server {
            command: ServerCommand::HubMigration,
        } => outcome(hub_cli::migration().map(|()| 0)),
        #[cfg(unix)]
        Command::Server {
            command: ServerCommand::HubAutoUpdate { checkout, once },
        } => outcome(hub_update::run(checkout, once).map(|()| 0)),
        #[cfg(not(unix))]
        Command::Server {
            command: ServerCommand::HubAutoUpdate { .. },
        } => outcome(Err(anyhow::anyhow!("The Hub runs only on macOS"))),
        Command::Server {
            command: ServerCommand::HubPrune { keep, builds },
        } => outcome(hub_cli::prune(&builds, &keep).map(|()| 0)),
        Command::Server {
            command:
                ServerCommand::HubData {
                    operation,
                    data_dir,
                    to,
                },
        } => outcome(block(hub_data::run(operation, data_dir, to)).map(|()| 0)),
        Command::Server {
            command: ServerCommand::Start { services },
        } => outcome(start(services)),
        Command::Server {
            command: ServerCommand::Stop { force },
        } => outcome(block(service_cli::stop(force)).map(|()| 0)),
        Command::Server {
            command:
                ServerCommand::HubProbe {
                    ready,
                    exit_timeout,
                    source,
                },
        } => {
            if source {
                let package = gb_base::home::python_package();
                println!("{}", package.parent().expect("the checkout").display());
                ExitCode::SUCCESS
            } else if exit_timeout {
                println!("{}", service_cli::FORCED_STOP_TIMEOUT.as_secs());
                ExitCode::SUCCESS
            } else {
                outcome(block(service_cli::hub_probe(ready)))
            }
        }
    }
}

fn block<T>(command: impl Future<Output = anyhow::Result<T>>) -> anyhow::Result<T> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(command)
}

fn outcome(result: anyhow::Result<u8>) -> ExitCode {
    match result {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("Error: {error:#}");
            ExitCode::FAILURE
        }
    }
}

fn run_command(
    name: &str,
    args: Vec<String>,
    person: Option<String>,
    cwd: Option<std::path::PathBuf>,
) -> anyhow::Result<()> {
    let message = || {
        let mut stdin = std::io::stdin();
        let mut message = String::new();
        if !stdin.is_terminal() {
            stdin.read_to_string(&mut message)?;
        }
        Ok(message)
    };
    let output = block(gb_server::client::run_command(
        name, args, person, message, cwd,
    ))?;
    if !output.is_empty() {
        println!("{output}");
    }
    Ok(())
}

/// Opens the screens with an entry link, or prints it when asked to or when
/// no browser opens it.
fn open(print: bool) -> ExitCode {
    let link = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| e.to_string())
        .and_then(|runtime| runtime.block_on(gb_server::client::entry_link()));
    match link {
        Ok(url) => {
            if print || !browse(&url) {
                println!("{url}");
            }
            ExitCode::SUCCESS
        }
        Err(sentence) => {
            eprintln!("Error: {sentence}");
            ExitCode::FAILURE
        }
    }
}

/// Whether the system's browser took `url`, which is one argument of its
/// handler: no shell is involved.
fn browse(url: &str) -> bool {
    #[cfg(target_os = "macos")]
    let mut command = std::process::Command::new("open");
    #[cfg(windows)]
    let mut command = {
        let mut command = std::process::Command::new("rundll32");
        command.arg("url.dll,FileProtocolHandler");
        command
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = std::process::Command::new("xdg-open");
    command
        .arg(url)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// Branvil's commands, keeping its secrets in the identity store.
fn branvil_main(args: impl IntoIterator<Item = OsString>) -> ExitCode {
    let vault = Arc::new(gb_server::branvil_vault::IdentityVault::system());
    let url = match gb_server::port() {
        Ok(port) => format!("http://127.0.0.1:{port}"),
        Err(error) => return outcome(Err(error)),
    };
    branvil::main_with_client_url(BRANVIL, vault, args, Some(url))
}

fn start(services: bool) -> anyhow::Result<u8> {
    use tracing_subscriber::prelude::*;
    let configured = std::env::var("GUILDBOTICS_LOG").unwrap_or_default();
    let filter = gb_server::diagnostic_logging::log_filter(&configured, None);
    let (filter, reload) = tracing_subscriber::reload::Layer::new(
        gb_server::diagnostic_logging::LogFilter::new(filter),
    );
    gb_server::diagnostic_logging::install_reload(Arc::new(move |level| {
        let filter = gb_server::diagnostic_logging::log_filter(&configured, level);
        let _ = reload.reload(gb_server::diagnostic_logging::LogFilter::new(filter));
    }));
    tracing_subscriber::registry()
        .with(filter)
        .with(
            tracing_subscriber::fmt::layer()
                .with_writer(std::io::stderr)
                .with_ansi(std::io::stderr().is_terminal()),
        )
        .with(gb_server::diagnostic_logging::DiagnosticLayer::default())
        .init();
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(serve(services))
}

/// Runs GuildBotics' port and Branvil until a signal stops them, or until
/// either one ends, which stops the other. The machine's lock comes first and
/// goes last: whoever stops the server waits for Branvil too.
async fn serve(services: bool) -> anyhow::Result<u8> {
    let signals = server_stops(gb_server::client::language().await)?;
    let branvil_initialized = branvil::initialized()?;
    let mut server = gb_server::Server::start().await?;
    server.start_services(services);
    let stop = wait_for_stop(signals.clone(), Stop::Waiting);
    let cancel = wait_for_stop(signals.clone(), Stop::Cancelling);

    if !branvil_initialized {
        tracing::info!("Branvil is not set up; `guildbotics branvil init` sets it up");
        let (_held, served) = server.serve(stop, cancel).await;
        served?;
        return Ok(0);
    }
    let base_url = server.base_url();
    let session = server.browser_session();
    let vault = Arc::new(gb_server::branvil_vault::IdentityVault::system());
    let (_held, served) = server
        .serve_embedded(stop, cancel, async move {
            branvil::prepare_embedded(BRANVIL, vault, &base_url, session).await
        })
        .await;
    served?;
    // Branvil logs CI jobs interrupted at its deadline. An orderly stop of
    // the combined server must exit successfully so launchd leaves it stopped.
    Ok(0)
}

/// How far a stop has gone: none yet, one that waits for the work running,
/// or one that cancels it.
#[derive(Clone, Copy, PartialEq)]
enum Stop {
    None,
    Waiting,
    Cancelling,
}

/// When GuildBotics' port stops, and when it cancels the work it runs: the
/// first SIGINT stops it once that work ends, and SIGTERM, which a service
/// manager sends, or a second SIGINT cancels the work.
fn server_stops(language: &'static str) -> std::io::Result<watch::Receiver<Stop>> {
    let (told, stops) = watch::channel(Stop::None);
    #[cfg(unix)]
    let (mut interrupt, mut terminate) = {
        use tokio::signal::unix::{SignalKind, signal};
        (
            signal(SignalKind::interrupt())?,
            signal(SignalKind::terminate())?,
        )
    };
    #[cfg(windows)]
    let mut interrupt = tokio::signal::windows::ctrl_c()?;
    tokio::spawn(async move {
        loop {
            #[cfg(unix)]
            let cancel = tokio::select! {
                _ = interrupt.recv() => *told.borrow() != Stop::None,
                _ = terminate.recv() => true,
            };
            #[cfg(windows)]
            let cancel = {
                interrupt.recv().await;
                *told.borrow() != Stop::None
            };
            let (next, message) = if cancel {
                (Stop::Cancelling, "runtime.service_lock.cancelling")
            } else {
                (Stop::Waiting, "runtime.service_lock.stopping_after_work")
            };
            if *told.borrow() != next {
                eprintln!("{}", gb_server::i18n::t(language, message, &[]));
                told.send_replace(next);
            }
        }
    });
    Ok(stops)
}

async fn wait_for_stop(mut signals: watch::Receiver<Stop>, stop: Stop) {
    let _ = signals
        .wait_for(|now| *now == stop || *now == Stop::Cancelling)
        .await;
}
