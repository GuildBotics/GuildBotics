//! The microVM a command runs in, driven through the microsandbox crate, as
//! the Python version drives it through the SDK
//! (`guildbotics/environment/runtime.py`). This is the only module that uses
//! the crate.
//!
//! The runtime is the one the Python version placed: `msb` and `libkrunfw`
//! under `<machine root>/data/msb`, which is also where the runtime keeps its
//! state. It is told so through the backend, never through the process'
//! environment, which the processes the server starts inherit.

use std::path::{Path, PathBuf};
use std::time::Duration;

use microsandbox::{ExecEvent, LocalBackend, Sandbox};
use tokio::sync::OnceCell;

use crate::{Failure, Sentence};

/// How a microVM GuildBotics boots is named, so a stale one is recognisable.
const NAME_PREFIX: &str = "guildbotics-";
/// Size of an empty directory of the microVM's own: a read-only command's
/// working directory.
const SCRATCH_MIB: u32 = 64;
/// The most of the entry's reply the host reads.
const REPLY_LIMIT: usize = 10 * 1024 * 1024;
/// How long the entry's log is read after its reply: a process the command
/// left running holds it open, and ends with the microVM.
const LOG_DRAIN: Duration = Duration::from_secs(2);
/// How long the microVM has to stop before it is killed.
const STOP_TIMEOUT: Duration = Duration::from_secs(5);
/// The runtime this crate speaks to: the version of the `microsandbox` crate
/// and of the Python SDK, whose `msb` the Python version placed and marked
/// with its version.
pub const RUNTIME_VERSION: &str = "0.6.17";

/// The runtime's home, which every microVM of this process boots through.
static BACKEND: OnceCell<PathBuf> = OnceCell::const_new();

/// One mount of the microVM: a host directory bound at `guest`, or an empty
/// directory of the microVM's own.
#[derive(Debug, Clone, PartialEq)]
pub struct Mount {
    pub guest: String,
    pub host: Option<PathBuf>,
    pub readonly: bool,
}

/// What a microVM boots from and with.
pub struct Boot<'a> {
    pub home: &'a Path,
    pub snapshot: &'a Path,
    pub memory_mib: u32,
    pub cpus: u8,
    pub mounts: &'a [Mount],
}

/// One process the microVM runs: its program and arguments, where, with
/// what, and what it reads on standard input.
pub struct Process<'a> {
    pub argv: &'a [String],
    pub cwd: &'a str,
    pub env: &'a [(String, String)],
    pub stdin: &'a [u8],
}

/// The files the Python version places under the runtime's `home`: `msb`
/// and `libkrunfw`, as the runtime names them on this OS.
pub fn placed_files(home: &Path) -> [PathBuf; 2] {
    let os = std::env::consts::OS;
    [
        home.join(microsandbox_utils::BIN_SUBDIR)
            .join(microsandbox_utils::msb_binary_filename(os)),
        home.join(microsandbox_utils::LIB_SUBDIR)
            .join(microsandbox_utils::libkrunfw_filename(os)),
    ]
}

/// Whether the runtime the Python version placed under `home` is there, and
/// is the one this speaks to.
pub fn ready(home: &Path) -> Result<(), Failure> {
    if !placed_files(home).iter().all(|file| file.is_file()) {
        return Err(Failure::Command(Sentence::key(
            "intelligences.agent_environment.runtime.not_installed",
        )));
    }
    let placed = std::fs::read_to_string(home.join("version")).unwrap_or_default();
    if placed.trim() != RUNTIME_VERSION {
        return Err(Failure::Command(Sentence::Text(format!(
            "The microVM runtime under {} is version '{}', not {RUNTIME_VERSION}.",
            home.display(),
            placed.trim()
        ))));
    }
    Ok(())
}

/// Speaks to the runtime under `home` from now on, in this process.
async fn backend(home: &Path) -> Result<(), Failure> {
    let installed = BACKEND
        .get_or_try_init(|| async {
            let backend = LocalBackend::builder()
                .home(home)
                .build()
                .await
                .map_err(|e| {
                    Failure::Runtime(Sentence::Text(format!(
                        "The microVM runtime cannot start: {e}"
                    )))
                })?;
            microsandbox::set_default_backend(backend);
            Ok::<_, Failure>(home.to_path_buf())
        })
        .await?;
    // One process has one runtime home, as the runtime has one per process.
    if installed != home {
        return Err(Failure::Runtime(Sentence::Text(format!(
            "The microVM runtime is at {}, not {}.",
            installed.display(),
            home.display()
        ))));
    }
    Ok(())
}

/// How a process ended: what it wrote on standard output up to its first
/// line's end, and its exit code when it was seen to end (-1 otherwise).
pub struct Ended {
    pub reply: Vec<u8>,
    pub code: i32,
}

/// Boots a microVM as `boot` says, runs `process` in it, and stops it,
/// relaying the process' log line by line.
pub async fn run_once(boot: &Boot<'_>, process: &Process<'_>) -> Result<Ended, Failure> {
    backend(boot.home).await?;
    let name = format!(
        "{NAME_PREFIX}{}",
        &uuid::Uuid::new_v4().simple().to_string()[..12]
    );
    let mut builder = Sandbox::builder(&name)
        .from_snapshot(boot.snapshot.to_string_lossy())
        .ephemeral(true)
        .memory(boot.memory_mib)
        .cpus(boot.cpus)
        .workdir("/")
        // Nothing the command reaches is served yet: no host window, no
        // provider, no other host.
        .disable_network();
    for mount in boot.mounts {
        let mount = mount.clone();
        builder = builder.volume(mount.guest.clone(), move |m| {
            let m = match &mount.host {
                Some(host) => m.bind(host),
                None => m.tmpfs().size(SCRATCH_MIB),
            };
            if mount.readonly { m.readonly() } else { m }
        });
    }
    // Dropped as it is, the microVM ends with the process' watchdog.
    let sandbox = builder.create().await.map_err(|e| {
        Failure::Runtime(Sentence::Key(
            "intelligences.agent_environment.runtime.start_failed",
            vec![
                ("memory_mib", boot.memory_mib.to_string()),
                ("cpus", boot.cpus.to_string()),
                ("error", e.to_string()),
            ],
        ))
    })?;
    let ran = execute(&sandbox, process).await;
    if let Err(e) = sandbox.stop_with_timeout(STOP_TIMEOUT).await {
        tracing::warn!("The microVM {name} did not stop, and is killed: {e}");
        if let Err(e) = sandbox.kill().await {
            tracing::warn!("The microVM {name} was not killed: {e}");
        }
    }
    ran
}

async fn execute(sandbox: &Sandbox, process: &Process<'_>) -> Result<Ended, Failure> {
    let (program, args) = process.argv.split_first().expect("a program");
    let exec_failed = |error: String| {
        Failure::Command(Sentence::Key(
            "intelligences.agent_environment.runtime.exec_failed",
            vec![("command", program.clone()), ("error", error)],
        ))
    };
    let mut handle = sandbox
        .exec_stream_with(program, |e| {
            e.args(args.iter().cloned())
                .cwd(process.cwd)
                .envs(process.env.iter().cloned())
                .stdin_pipe()
        })
        .await
        .map_err(|e| exec_failed(e.to_string()))?;
    let stdin = handle.take_stdin().expect("a piped standard input");
    stdin
        .write(process.stdin)
        .await
        .map_err(|e| exec_failed(e.to_string()))?;
    stdin
        .close()
        .await
        .map_err(|e| exec_failed(e.to_string()))?;
    let mut reply = Vec::new();
    let mut log = Log::default();
    let mut code = -1;
    while !reply.ends_with(b"\n") {
        match handle.recv().await {
            Some(ExecEvent::Stdout(data)) => {
                reply.extend_from_slice(&data);
                if reply.len() > REPLY_LIMIT {
                    return Err(Failure::Command(Sentence::key(
                        "intelligences.agent_environment.runtime.reply_too_large",
                    )));
                }
            }
            Some(ExecEvent::Stderr(data)) => log.feed(&data),
            Some(ExecEvent::Exited { code: exited }) => {
                code = exited;
                break;
            }
            Some(ExecEvent::Failed(failed)) => return Err(exec_failed(failed.message)),
            Some(_) => {}
            None => break,
        }
    }
    // The reply is the first line; what follows it is the log's alone.
    if let Some(end) = reply.iter().position(|b| *b == b'\n') {
        reply.truncate(end + 1);
        let _ = tokio::time::timeout(LOG_DRAIN, async {
            while let Some(event) = handle.recv().await {
                match event {
                    ExecEvent::Stderr(data) => log.feed(&data),
                    ExecEvent::Exited { code: exited } => {
                        code = exited;
                        break;
                    }
                    ExecEvent::Failed(_) => break,
                    _ => {}
                }
            }
        })
        .await;
    }
    log.flush();
    Ok(Ended { reply, code })
}

/// What the entry logs, relayed line by line at its own level; a line that
/// names none (a traceback's) goes at the level of the one before.
#[derive(Default)]
struct Log {
    pending: Vec<u8>,
    level: Level,
}

#[derive(Default, Clone, Copy)]
enum Level {
    Debug,
    #[default]
    Info,
    Warning,
    Error,
}

/// The most of one line the log keeps.
const MAX_LOG_LINE: usize = 1 << 14;

impl Log {
    /// Logs every line `data` ends; of each, only its first
    /// [`MAX_LOG_LINE`] bytes.
    fn feed(&mut self, data: &[u8]) {
        self.pending.extend_from_slice(data);
        while let Some(end) = self.pending.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.pending.drain(..=end).collect();
            self.line(&line[..line.len().min(MAX_LOG_LINE)]);
        }
        self.pending.truncate(MAX_LOG_LINE);
    }

    fn flush(&mut self) {
        let line = std::mem::take(&mut self.pending);
        self.line(&line);
    }

    fn line(&mut self, line: &[u8]) {
        let text = String::from_utf8_lossy(line);
        let mut text = text.trim_end();
        if let Some((name, rest)) = text.split_once(' ') {
            let named = match name {
                "DEBUG" => Some(Level::Debug),
                "INFO" => Some(Level::Info),
                "WARNING" => Some(Level::Warning),
                "ERROR" | "CRITICAL" => Some(Level::Error),
                _ => None,
            };
            if let Some(level) = named {
                self.level = level;
                text = rest;
            }
        }
        if text.is_empty() {
            return;
        }
        match self.level {
            Level::Debug => tracing::debug!("{text}"),
            Level::Info => tracing::info!("{text}"),
            Level::Warning => tracing::warn!("{text}"),
            Level::Error => tracing::error!("{text}"),
        }
    }
}
