//! The `guildbotics` executable: the server, with Branvil in the same
//! process, and Branvil's commands as `guildbotics branvil`.

use std::ffi::OsString;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

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
    /// Run the server in the foreground until SIGINT or SIGTERM: Branvil as
    /// `guildbotics branvil serve` without options runs it (BRANVIL_DATA_DIR
    /// chooses its data directory)
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
            tracing_subscriber::EnvFilter::try_from_env("GUILDBOTICS_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("branvil=info")),
        )
        .with_writer(std::io::stderr)
        .init();
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(async {
            let (interrupt, terminate) = branvil::stop_signals()?;
            branvil::serve(BRANVIL, interrupt, terminate).await
        })
}
