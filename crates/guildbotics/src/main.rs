//! The `guildbotics` executable: the server, with Branvil in the same
//! process, and Branvil's commands as `guildbotics branvil`.

use std::ffi::OsString;
use std::io::IsTerminal;
use std::process::ExitCode;
use std::sync::Arc;

use clap::{Parser, Subcommand};
use tokio::sync::Notify;

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
    /// Run the server
    Server {
        #[command(subcommand)]
        command: ServerCommand,
    },
    /// Branvil's commands (code hosting: `guildbotics branvil --help`)
    #[command(disable_help_flag = true)]
    Branvil {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<OsString>,
    },
}

#[derive(Subcommand)]
enum ServerCommand {
    /// Run the server in the foreground until SIGINT or SIGTERM: GuildBotics
    /// on GUILDBOTICS_SERVER_PORT (default 8765) with its records under
    /// GUILDBOTICS_HOME (default ~/.guildbotics), and Branvil, once
    /// `guildbotics branvil init` has set it up, as `guildbotics branvil serve`
    /// without options runs it (BRANVIL_DATA_DIR chooses its data directory)
    Start,
}

fn main() -> ExitCode {
    // Started by Git as GIT_ASKPASS when Branvil talks to an upstream host.
    if let Some(code) = branvil::askpass_main() {
        return code;
    }
    match Cli::parse().command {
        Command::Branvil { args } => {
            branvil::main(BRANVIL, std::iter::once(OsString::new()).chain(args))
        }
        Command::Server {
            command: ServerCommand::Start,
        } => match start() {
            Ok(code) => ExitCode::from(code),
            Err(e) => {
                eprintln!("guildbotics: {e:#}");
                ExitCode::FAILURE
            }
        },
    }
}

fn start() -> anyhow::Result<u8> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("GUILDBOTICS_LOG").unwrap_or_else(|_| {
                tracing_subscriber::EnvFilter::new("branvil=info,gb_server=info,guildbotics=info")
            }),
        )
        .with_writer(std::io::stderr)
        .with_ansi(std::io::stderr().is_terminal())
        .init();
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(serve())
}

/// Runs GuildBotics' port and Branvil until a signal stops them, or until
/// either one ends, which stops the other. The machine's lock comes first and
/// goes last: whoever stops the server waits for Branvil too.
async fn serve() -> anyhow::Result<u8> {
    let (interrupt, terminate) = branvil::stop_signals()?;
    let signal = async move {
        tokio::select! {
            () = interrupt => {},
            () = terminate => {},
        }
    };
    let branvil_set_up = branvil::initialized()?;
    let server = gb_server::Server::start().await?;
    if !branvil_set_up {
        tracing::info!("Branvil is not set up; `guildbotics branvil init` sets it up");
        let (_held, served) = server.serve(signal).await;
        served?;
        return Ok(0);
    }
    let (branvil_interrupt, branvil_terminate) = branvil::stop_signals()?;
    let guildbotics_failed = Arc::new(Notify::new());
    let branvil_ended = Arc::new(Notify::new());
    let interrupt = {
        let guildbotics_failed = guildbotics_failed.clone();
        async move {
            tokio::select! {
                () = branvil_interrupt => {},
                () = guildbotics_failed.notified() => {},
            }
        }
    };
    let running = tokio::spawn(branvil::serve(BRANVIL, interrupt, branvil_terminate));
    // Told however Branvil ends, a panic included.
    let branvil = tokio::spawn({
        let branvil_ended = branvil_ended.clone();
        async move {
            let ended = running.await;
            branvil_ended.notify_one();
            ended
        }
    });
    let stop = async move {
        tokio::select! {
            () = signal => {},
            () = branvil_ended.notified() => {},
        }
    };
    let (_held, served) = server.serve(stop).await;
    // On a signal Branvil has had it too, and stops as that signal asks.
    if served.is_err() {
        guildbotics_failed.notify_one();
    }
    let code = branvil.await??;
    served?;
    code
}
