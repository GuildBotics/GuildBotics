//! A command's isolated environment: the microVM a command runs in, booted
//! from the workspace's snapshot, and the entry inside it that runs the
//! command and says how it ended, as the Python version has them
//! (`guildbotics/environment/command_environment.py`, `spec.py`).
//!
//! So far a read-only command runs here, which works on nothing of the
//! host's: its working directory is an empty directory of the microVM's own,
//! and what it reads -- the code, the workspace's configuration, the members'
//! identities -- is mounted read-only. Nothing it asks of the host is served
//! yet (the host window, the member broker, the providers' gateways), so its
//! microVM opens no network: a call of it fails as one the host did not
//! answer.

pub mod language;
mod runtime;
pub mod snapshot;

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use gb_wire::{CommandAccess, CommandFacts, CommandReply, CommandRequest, InspectsItem};

pub use runtime::{Mount, placed_files};

/// Where the code every microVM mounts is inside it.
pub const CODE_ROOT: &str = "/opt/guildbotics/code/guildbotics";
/// The command execution machinery's entry inside the microVM.
pub const ENTRY: &[&str] = &[
    "/opt/guildbotics/venv/bin/python",
    // Nothing is written beside the read-only code.
    "-B",
    "-m",
    "guildbotics.guest.entry",
];
/// Where the entry asks the host for what only the host holds. The window is
/// not served yet, and the microVM reaches no host: every call fails as
/// unavailable.
const UNSERVED_WINDOW: &str = "http://host.microsandbox.internal:1/host";

/// A sentence a person reads: one of the locale files' (`a.b.c`, with its
/// parameters), or one given as it is.
#[derive(Debug, Clone, PartialEq)]
pub enum Sentence {
    Key(&'static str, Vec<(&'static str, String)>),
    Text(String),
}

impl Sentence {
    pub fn key(key: &'static str) -> Sentence {
        Sentence::Key(key, vec![])
    }
}

/// Why a command did not run to its end: refused or failed as a command
/// (the Python version's `CommandError`), or failed otherwise.
#[derive(Debug, Clone, PartialEq)]
pub enum Failure {
    Command(Sentence),
    Runtime(Sentence),
}

/// Where the host keeps what a command's microVM is made of.
pub struct Host {
    /// The machine's own state: `<machine root>/data`, which holds the
    /// runtime under `msb`.
    pub data: PathBuf,
    /// The person's home directory, which is the microVM's too: the snapshot
    /// was built with it.
    pub home: PathBuf,
    /// The Python package of the checkout the server runs from.
    pub package: PathBuf,
    /// The root of the members' identities.
    pub identity_root: PathBuf,
}

/// One command to run: who runs it, as which run and work, the file the host
/// resolved for it, what it declared, and its arguments, working directory
/// (as the host spells it) and input.
pub struct Command {
    pub person_id: String,
    pub run_id: String,
    pub trace_id: String,
    pub work_kind: String,
    pub work_identity: String,
    pub path: PathBuf,
    pub name: String,
    pub access: CommandAccess,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub pipe: String,
}

/// What a command's microVM is made of, and what its entry is given.
#[derive(Debug, PartialEq)]
pub struct Shape {
    pub mounts: Vec<Mount>,
    pub cwd: String,
    pub env: Vec<(String, String)>,
    pub request: CommandRequest,
}

/// Runs `command` of the workspace `workspace` in a microVM of its own, which
/// is gone when this returns, and says how the command ended.
pub async fn run(host: &Host, workspace: &Path, command: Command) -> Result<CommandReply, Failure> {
    if !command.access.read_only {
        return Err(Failure::Command(Sentence::Text(format!(
            "'{}' is not declared read-only, and only read-only commands run here so far.",
            command.name
        ))));
    }
    // As the device's status refuses: the runtime, the declaration, then the
    // snapshot.
    runtime::ready(&host.data.join("msb"))?;
    let resources = gb_config::agent_environment::resources(
        &config_dir(workspace),
        &host.package.join("templates"),
    )
    .map_err(|e| Failure::Command(Sentence::Text(e.to_string())))?;
    let snapshot = snapshot::ready(workspace).map_err(Failure::Command)?;
    let shape = shape(host, workspace, &command, language::ui_language().await)?;
    let stdin = serde_json::to_vec(&shape.request).expect("JSON");
    let argv: Vec<String> = ENTRY.iter().map(|s| s.to_string()).collect();
    let ended = runtime::run_once(
        &runtime::Boot {
            home: &host.data.join("msb"),
            snapshot: &snapshot,
            memory_mib: resources.memory_mib,
            cpus: resources.cpus,
            mounts: &shape.mounts,
        },
        &runtime::Process {
            argv: &argv,
            cwd: &shape.cwd,
            env: &shape.env,
            stdin: &stdin,
        },
    )
    .await?;
    serde_json::from_slice(&ended.reply).map_err(|_| {
        Failure::Command(Sentence::Key(
            "intelligences.agent_environment.runtime.no_reply",
            vec![("code", ended.code.to_string())],
        ))
    })
}

/// The workspace's configuration directory.
fn config_dir(workspace: &Path) -> PathBuf {
    workspace.join(".guildbotics/config")
}

/// The microVM `command` runs in, and what its entry is given, told the
/// host's UI `language`. Every host path is taken as the host spells it
/// (macOS's `/tmp` is `/private/tmp`), and so is it inside the microVM.
pub fn shape(
    host: &Host,
    workspace: &Path,
    command: &Command,
    language: Option<String>,
) -> Result<Shape, Failure> {
    let host = &Host {
        data: host.data.clone(),
        home: normalized(&host.home)?,
        package: normalized(&host.package)?,
        identity_root: normalized(&host.identity_root)?,
    };
    let workspace = &normalized(workspace)?;
    let cwd = guest_path(&normalized(&command.cwd)?)?;
    // The working directory is the microVM's own, and work happens only
    // there. The command reads the workspace's configuration, as it would on
    // the host; it is told of it only when it inspects it.
    let mut mounts = vec![
        Mount {
            guest: cwd.clone(),
            host: None,
            readonly: false,
        },
        Mount {
            guest: CODE_ROOT.into(),
            host: Some(checked_directory(&host.package)?),
            readonly: true,
        },
    ];
    let mut read = vec![InspectsItem::Config];
    read.extend(command.access.inspects.iter().copied());
    for (_, mount) in inspected_mounts(host, workspace, &read)? {
        // What GuildBotics binds over the working directory is what is there.
        match mounts.iter_mut().find(|m| m.guest == mount.guest) {
            Some(over) => *over = mount,
            None => mounts.push(mount),
        }
    }
    let mut inspected: HashMap<String, String> =
        inspected_mounts(host, workspace, &command.access.inspects)?
            .into_iter()
            .map(|(name, mount)| (name.to_string(), mount.guest))
            .collect();
    if command.access.inspects.contains(&InspectsItem::Config) {
        inspected.insert(
            "templates".into(),
            code_path(host, &host.package.join("templates"))?,
        );
    }
    let facts = CommandFacts {
        access: command.access.clone(),
        inspected,
        mounts: mounts
            .iter()
            .map(|mount| {
                let open = mount.host.is_none() && mount.guest == cwd;
                (mount.guest.clone(), open)
            })
            .collect(),
        person_id: command.person_id.clone(),
        run_id: command.run_id.clone(),
        trace_id: command.trace_id.clone(),
        work_identity: command.work_identity.clone(),
        work_kind: command.work_kind.clone(),
    };
    let mut env = vec![("HOME".to_string(), guest_path(&host.home)?)];
    if let Some(language) = language {
        env.push(("LANGUAGE".into(), language));
    }
    env.extend([
        ("GUILDBOTICS_HOST_URL".into(), UNSERVED_WINDOW.into()),
        (
            "GUILDBOTICS_HOST_TOKEN".into(),
            uuid::Uuid::new_v4().simple().to_string(),
        ),
        (
            "GUILDBOTICS_COMMAND".into(),
            serde_json::to_string(&facts).expect("JSON"),
        ),
        ("GUILDBOTICS_WORKSPACE_ROOT".into(), guest_path(workspace)?),
        (
            "GUILDBOTICS_CONFIG_DIR".into(),
            guest_path(&config_dir(workspace))?,
        ),
        (
            "GUILDBOTICS_IDENTITY_ROOT".into(),
            guest_path(&host.identity_root)?,
        ),
    ]);
    let request = CommandRequest {
        args: command.args.clone(),
        cwd: cwd.clone(),
        invocation: None,
        name: command.name.clone(),
        path: command_path(host, &normalized(&command.path)?)?,
        pipe: command.pipe.clone(),
        wants_result: false,
    };
    Ok(Shape {
        mounts,
        cwd,
        env,
        request,
    })
}

/// The workspace's own directories a command inspecting `scopes` mounts,
/// read-only at their own paths, by name: those that exist.
fn inspected_mounts(
    host: &Host,
    workspace: &Path,
    scopes: &[InspectsItem],
) -> Result<Vec<(&'static str, Mount)>, Failure> {
    let mut sorted = scopes.to_vec();
    sorted.sort_by_key(|scope| scope.to_string());
    sorted.dedup();
    let mut mounts = Vec::new();
    for scope in sorted {
        let directories: Vec<(&'static str, PathBuf)> = match scope {
            InspectsItem::Config => vec![
                ("config", config_dir(workspace)),
                // The members' identities, which every workspace shares: only
                // their directory, not the identities' Secrets index.
                (
                    "identities",
                    host.identity_root.join(".guildbotics/config/team/members"),
                ),
            ],
            InspectsItem::Diagnostics => {
                vec![("diagnostics", workspace.join(".guildbotics/local/run"))]
            }
        };
        for (name, path) in directories {
            if !path.exists() {
                continue;
            }
            let checked = checked_directory(&path)?;
            mounts.push((
                name,
                Mount {
                    guest: guest_path(&checked)?,
                    host: Some(checked),
                    readonly: true,
                },
            ));
        }
    }
    Ok(mounts)
}

/// `path` as the host spells it.
fn normalized(path: &Path) -> Result<PathBuf, Failure> {
    gb_base::host_path::normalize(path).map_err(|e| Failure::Command(Sentence::Text(e.to_string())))
}

/// A directory to bind, every component of which opens as a directory
/// without following a link.
fn checked_directory(path: &Path) -> Result<PathBuf, Failure> {
    gb_base::host_path::directory(path, false)
        .map_err(|e| Failure::Command(Sentence::Text(e.to_string())))
}

/// Where a command file is inside the microVM: a packaged one in the code
/// every microVM mounts, a workspace one at its own path.
fn command_path(host: &Host, path: &Path) -> Result<String, Failure> {
    if path.starts_with(&host.package) {
        code_path(host, path)
    } else {
        guest_path(path)
    }
}

/// Where a file of the package is inside the microVM.
fn code_path(host: &Host, path: &Path) -> Result<String, Failure> {
    let relative = path.strip_prefix(&host.package).expect("under the package");
    let relative = relative.to_str().ok_or_else(|| unnamed(path))?;
    Ok(format!("{CODE_ROOT}/{relative}"))
}

/// Where a host path is inside the microVM: at its own spelling.
pub fn guest_path(path: &Path) -> Result<String, Failure> {
    if !path.is_absolute() {
        return Err(Failure::Command(Sentence::Text(format!(
            "'{}' is not an absolute path",
            path.display()
        ))));
    }
    path.to_str()
        .map(str::to_owned)
        .ok_or_else(|| unnamed(path))
}

fn unnamed(path: &Path) -> Failure {
    Failure::Command(Sentence::Text(format!(
        "'{}' cannot be named inside the environment.",
        path.display()
    )))
}
