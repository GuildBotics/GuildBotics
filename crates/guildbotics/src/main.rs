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
    /// Manage this device's isolated environment
    Environment {
        #[command(subcommand)]
        command: EnvironmentCommand,
    },
    /// Run the server
    Server {
        #[command(subcommand)]
        command: ServerCommand,
    },
    /// Open the GuildBotics screens in the browser. The browser enters with a
    /// single-use link that expires after 60 seconds
    Open {
        /// Print the link instead of opening it, to paste into a browser on
        /// another machine
        #[arg(long)]
        print: bool,
    },
    /// Branvil's commands (code hosting: `guildbotics branvil --help`)
    #[command(disable_help_flag = true)]
    Branvil {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<OsString>,
    },
}

#[derive(Subcommand)]
enum EnvironmentCommand {
    /// Load a base image into this device's runtime
    Image {
        #[command(subcommand)]
        command: ImageCommand,
    },
}

#[derive(Subcommand)]
enum ImageCommand {
    /// Load a Docker save or OCI archive built for this device's CPU
    Load {
        archive: std::path::PathBuf,
        /// Add a reference to the first loaded image, keeping its own tags
        #[arg(long)]
        tag: Option<String>,
    },
}

fn environment(command: EnvironmentCommand) -> anyhow::Result<()> {
    let EnvironmentCommand::Image {
        command: ImageCommand::Load { archive, tag },
    } = command;
    let home = gb_base::home::data()?.join("msb");
    let loaded = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(gb_environment::load_image(&home, &archive, tag))
        .map_err(anyhow::Error::msg)?;
    for (reference, digest) in loaded {
        println!("loaded {reference} {digest}");
    }
    Ok(())
}

#[derive(Subcommand)]
enum ServerCommand {
    /// Run the server in the foreground until SIGINT or SIGTERM (a first
    /// SIGINT waits for the commands running, SIGTERM or a second SIGINT
    /// cancels them): GuildBotics
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
        Command::Environment { command } => match environment(command) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("guildbotics: {e:#}");
                ExitCode::FAILURE
            }
        },
        Command::Branvil { args } => {
            branvil::main(BRANVIL, std::iter::once(OsString::new()).chain(args))
        }
        Command::Open { print } => open(print),
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

fn start() -> anyhow::Result<u8> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("GUILDBOTICS_LOG").unwrap_or_else(|_| {
                tracing_subscriber::EnvFilter::new("branvil=info,gb_server=info,gb_workspace=info,gb_observability=info,guildbotics=info")
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
    // Both told of every signal from here on: one that comes while the
    // server starts stops Branvil too.
    let (signal, cancel) = server_stops()?;
    let branvil_stops = branvil::stop_signals()?;
    let branvil_stops = branvil::initialized()?.then_some(branvil_stops);
    let server = gb_server::Server::start().await?;
    let Some((branvil_interrupt, branvil_terminate)) = branvil_stops else {
        tracing::info!("Branvil is not set up; `guildbotics branvil init` sets it up");
        let (_held, served) = server.serve(signal, cancel).await;
        served?;
        return Ok(0);
    };
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
    let (_held, served) = server.serve(stop, cancel).await;
    // On a signal Branvil has had it too, and stops as that signal asks.
    if served.is_err() {
        guildbotics_failed.notify_one();
    }
    let code = branvil.await??;
    served?;
    code
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
fn server_stops() -> std::io::Result<(
    impl Future<Output = ()> + Send + 'static,
    impl Future<Output = ()> + Send + 'static,
)> {
    let (told, stops) = tokio::sync::watch::channel(Stop::None);
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
            told.send_replace(if cancel {
                Stop::Cancelling
            } else {
                Stop::Waiting
            });
        }
    });
    let reached = |stop: Stop| {
        let mut stops = stops.clone();
        async move {
            let _ = stops
                .wait_for(|now| *now == stop || *now == Stop::Cancelling)
                .await;
        }
    };
    Ok((reached(Stop::Waiting), reached(Stop::Cancelling)))
}
