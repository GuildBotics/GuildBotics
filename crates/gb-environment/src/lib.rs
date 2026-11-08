//! A command's isolated environment: the microVM a command runs in, booted
//! from the workspace's snapshot, and the entry inside it that runs the
//! command and says how it ended, as the Python version has them
//! (`guildbotics/environment/command_environment.py`, `spec.py`).
//!
//! The host resolves configuration and serves scoped host, member and
//! provider endpoints. Read-only commands use a private working directory;
//! writing commands use an authorized mount or a checked copy with write-back.

pub mod broker;
pub mod configuration;
pub mod contract;
pub mod device;
pub mod gateway;
pub mod guest_process;
pub mod image;
pub mod image_archive;
pub mod language;
pub mod recipe;
pub mod runtime;
pub mod snapshot;
pub mod worktree;

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use gb_wire::{CommandAccess, CommandFacts, CommandReply, CommandRequest, InspectsItem};

pub use configuration::Configuration;
pub use runtime::{Mount, load_image, placed_files};

/// Where the code every microVM mounts is inside it.
pub const CODE_ROOT: &str = "/opt/guildbotics/code/guildbotics";
/// The home of every microVM, the snapshot's build included: the microVM's
/// own, at a path no host has, so a path under it is never taken for the
/// host's. What the host mounts keeps its host path; a grant under the
/// host's home is linked from the same place under this one.
pub const GUEST_HOME: &str = "/opt/guildbotics/home";
/// The exchange folder, relative to the host's home.
pub const EXCHANGE: &str = "Documents/GuildBotics";
/// GuildBotics' own roots in the microVM: its code, its venv, the home and
/// its run state. No host path is taken over or under them.
const RESERVED: [&str; 2] = ["/opt/guildbotics", "/run/guildbotics"];
/// The command execution machinery's entry inside the microVM.
pub const ENTRY: &[&str] = &[
    "/opt/guildbotics/venv/bin/python",
    // Nothing is written beside the read-only code.
    "-B",
    "-m",
    "guildbotics.guest.entry",
];
/// A sentence a person reads: one of the locale files' (`a.b.c`, with its
/// parameters), or one given as it is.
#[derive(Debug, Clone, PartialEq)]
pub enum Sentence {
    Key(&'static str, Vec<(&'static str, String)>),
    AtPath(PathBuf, Box<Sentence>),
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

impl Failure {
    pub fn sentence(self) -> Sentence {
        match self {
            Self::Command(sentence) | Self::Runtime(sentence) => sentence,
        }
    }
}

/// Where the host keeps what a command's microVM is made of.
#[derive(Clone)]
pub struct Host {
    /// The machine's own state: `<machine root>/data`, which holds the
    /// runtime under `msb`.
    pub data: PathBuf,
    /// The person's home directory on the host, which grants are relative
    /// to. The microVM's home is [`GUEST_HOME`].
    pub home: PathBuf,
    /// The Python package of the checkout the server runs from.
    pub package: PathBuf,
    /// The root of the members' identities.
    pub identity_root: PathBuf,
}

/// One command to run: who runs it, as which run and work, the file the host
/// resolved for it in the run's copy of the configuration, what it declared,
/// and its arguments, working directory (as the host spells it) and input.
pub struct Command {
    /// The configuration the run reads, once: its file is in it.
    pub config: std::sync::Arc<Configuration>,
    pub wants_result: bool,
    pub invocation: Option<gb_wire::WorkflowInvocation>,
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
    pub copy: Option<worktree::Copy>,
    pub mounts: Vec<Mount>,
    pub links: Vec<contract::Link>,
    pub cwd: String,
    pub env: Vec<(String, String)>,
    pub request: CommandRequest,
}

/// Per-command host windows and non-secret data, owned until the VM stops.
pub struct Session {
    pub guest_process: guest_process::Channel,
    pub window_url: String,
    pub window_token: String,
    pub resolved: serde_json::Value,
    pub mounts: Vec<Mount>,
    pub env: Vec<(String, String)>,
    pub ports: Vec<u16>,
}

/// Runs `command` of the workspace `workspace` in a microVM of its own, which
/// is gone when this returns, and says how the command ended.
pub async fn run(
    host: &Host,
    workspace: &Path,
    command: Command,
    on_warning: impl Fn(Sentence),
    session: &Session,
) -> Result<CommandReply, Failure> {
    let device = device::read(host, workspace).await;
    if let Some(refusal) = device.refusal {
        return Err(Failure::Command(refusal));
    }
    if let Some(warning) = device.warning {
        on_warning(warning);
    }
    let snapshot = device
        .snapshot
        .ok_or_else(|| Failure::Command(Sentence::Text("No ready snapshot".into())))?;
    let resources = device.resources;
    let mut shape = shape(host, workspace, &command, language::ui_language().await)?;
    let resolved_dir =
        tempfile::tempdir().map_err(|e| Failure::Runtime(Sentence::Text(e.to_string())))?;
    let resolved_path = resolved_dir.path().join("resolved.json");
    std::fs::write(
        &resolved_path,
        serde_json::to_vec(&session.resolved).expect("resolved JSON"),
    )
    .map_err(|e| Failure::Runtime(Sentence::Text(e.to_string())))?;
    shape.mounts.push(Mount {
        guest: "/run/guildbotics/resolved".into(),
        host: Some(resolved_dir.path().into()),
        readonly: true,
        quota_mib: None,
    });
    shape.mounts.extend(session.mounts.clone());
    shape.env.extend(session.env.clone());
    shape.env.extend([
        ("GUILDBOTICS_HOST_URL".into(), session.window_url.clone()),
        (
            "GUILDBOTICS_HOST_TOKEN".into(),
            session.window_token.clone(),
        ),
        (
            "GUILDBOTICS_RESOLVED_GUEST".into(),
            "/run/guildbotics/resolved/resolved.json".into(),
        ),
        ("AGNO_TELEMETRY".into(), "false".into()),
    ]);
    let mut network = contract::network(
        command.config.source(),
        &host.package.join("templates"),
        command.access.read_only,
    )
    .map_err(Failure::Command)?;
    network.host_ports.extend(session.ports.iter().copied());
    for (key, value) in &session.env {
        if key == "GUILDBOTICS_TURN_DOMAINS" {
            let domains: Vec<String> = serde_json::from_str(value).map_err(|e| {
                Failure::Command(Sentence::Text(format!(
                    "Invalid host-resolved turn domains: {e}"
                )))
            })?;
            network.domains.extend(domains);
        }
    }
    let registered = device::registered_workspaces(&host.data)
        .map_err(|e| Failure::Command(Sentence::Text(e)))?;
    let access = contract::resolve(
        workspace,
        &host.home,
        &registered,
        command.access.read_only,
        false,
    )
    .map_err(|e| Failure::Command(Sentence::Text(e)))?;
    contract::reject_windows_temp_mounts(
        &shape.mounts,
        cfg!(windows),
        std::env::var_os("LOCALAPPDATA").as_deref().map(Path::new),
    )
    .map_err(|e| Failure::Command(Sentence::Text(e)))?;
    for mount in &mut shape.mounts {
        if let Some(source) = &mut mount.host {
            *source = contract::validate_mount_source(source, &access)
                .map_err(|e| Failure::Command(Sentence::Text(e)))?;
        }
    }
    let stdin = serde_json::to_vec(&shape.request).expect("JSON");
    let argv: Vec<String> = ENTRY.iter().map(|s| s.to_string()).collect();
    runtime::run_once_with_guest(
        &runtime::Boot {
            home: &host.data.join("msb"),
            snapshot: &snapshot,
            memory_mib: resources.memory_mib,
            cpus: resources.cpus,
            mounts: &shape.mounts,
            links: &shape.links,
            network: &network,
            worktree: shape.copy.as_ref(),
        },
        &runtime::Process {
            argv: &argv,
            cwd: &shape.cwd,
            env: &shape.env,
            stdin: &stdin,
        },
        &session.guest_process,
    )
    .await
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
    let mut mounts = vec![Mount {
        guest: CODE_ROOT.into(),
        host: Some(checked_directory(&host.package)?),
        readonly: true,
        quota_mib: None,
    }];
    if command.access.read_only {
        mounts.push(Mount {
            guest: cwd.clone(),
            host: None,
            readonly: false,
            quota_mib: None,
        });
    }
    let registered = device::registered_workspaces(&host.data)
        .map_err(|e| Failure::Command(Sentence::Text(e)))?;
    let access = contract::resolve(
        workspace,
        &host.home,
        &registered,
        command.access.read_only,
        true,
    )
    .map_err(|e| Failure::Command(Sentence::Text(e)))?;
    mounts.extend(access.grants.clone());
    let mut read = vec![InspectsItem::Config];
    read.extend(command.access.inspects.iter().copied());
    for (_, mount) in inspected_mounts(host, workspace, &command.config, &read)? {
        // What GuildBotics binds over the working directory is what is there.
        match mounts.iter_mut().find(|m| m.guest == mount.guest) {
            Some(over) => *over = mount,
            None => mounts.push(mount),
        }
    }
    let mut inspected: HashMap<String, String> =
        inspected_mounts(host, workspace, &command.config, &command.access.inspects)?
            .into_iter()
            .map(|(name, mount)| (name.to_string(), mount.guest))
            .collect();
    if command.access.inspects.contains(&InspectsItem::Config) {
        inspected.insert(
            "templates".into(),
            code_path(host, &host.package.join("templates"))?,
        );
    }
    let planned = contract::plan(
        workspace,
        &command.person_id,
        &command.cwd,
        &access,
        mounts,
        host.data.join("run/worktree-write-back.lock"),
        command.access.read_only,
    )
    .map_err(|e| Failure::Command(Sentence::Text(e)))?;
    let mounts = planned.mounts;
    let cwd = planned.cwd;
    let copy = planned.copy;
    let clone_parent = guest_path(
        &workspace
            .join(".guildbotics/local/clones")
            .join(&command.person_id),
    )?;
    let facts = CommandFacts {
        access: command.access.clone(),
        inspected,
        mounts: mounts
            .iter()
            .map(|mount| {
                let open = (mount.host.is_none() && mount.guest == cwd)
                    || access.grants.iter().any(|g| g.guest == mount.guest)
                    || mount.guest == clone_parent;
                (mount.guest.clone(), open)
            })
            .chain(copy.iter().map(|working| (working.guest.clone(), true)))
            .collect(),
        person_id: command.person_id.clone(),
        run_id: command.run_id.clone(),
        trace_id: command.trace_id.clone(),
        work_identity: command.work_identity.clone(),
        work_kind: command.work_kind.clone(),
    };
    let mut env = vec![
        ("HOME".to_string(), GUEST_HOME.into()),
        ("TZ".into(), language::time_zone()),
        ("AGNO_TELEMETRY".into(), "false".into()),
    ];
    if let Some(language) = language {
        env.push(("LANGUAGE".into(), language));
    }
    let debug = gb_config::debug::read(Some(workspace));
    env.push(("LOG_LEVEL".into(), debug.guest_log_level().into()));
    env.push(("AGNO_DEBUG".into(), debug.agno_debug.to_string()));
    env.extend([
        (
            "GUILDBOTICS_COMMAND".into(),
            serde_json::to_string(&facts).expect("JSON"),
        ),
        ("GUILDBOTICS_WORKSPACE_ROOT".into(), guest_path(workspace)?),
        (
            "GUILDBOTICS_CONFIG_DIR".into(),
            guest_path(command.config.source())?,
        ),
        (
            "GUILDBOTICS_IDENTITY_ROOT".into(),
            guest_path(&host.identity_root)?,
        ),
    ]);
    let request = CommandRequest {
        args: command.args.clone(),
        cwd: cwd.clone(),
        invocation: command.invocation.clone(),
        name: command.name.clone(),
        path: command_path(host, &command.config, &normalized(&command.path)?)?,
        pipe: command.pipe.clone(),
        wants_result: command.wants_result,
    };
    Ok(Shape {
        copy,
        mounts,
        links: access.links.clone(),
        cwd,
        env,
        request,
    })
}

/// The workspace's own directories a command inspecting `scopes` mounts,
/// read-only at their own paths, by name: those that exist. The
/// configuration is the run's copy of it.
fn inspected_mounts(
    host: &Host,
    workspace: &Path,
    config: &Configuration,
    scopes: &[InspectsItem],
) -> Result<Vec<(&'static str, Mount)>, Failure> {
    let mut sorted = scopes.to_vec();
    sorted.sort_by_key(|scope| scope.to_string());
    sorted.dedup();
    let mut mounts = Vec::new();
    for scope in sorted {
        let directories: Vec<(&'static str, PathBuf)> = match scope {
            InspectsItem::Config => vec![
                ("config", config.path()),
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
            let at = config.original(&checked).unwrap_or_else(|| checked.clone());
            mounts.push((
                name,
                Mount {
                    guest: guest_path(&at)?,
                    host: Some(checked),
                    readonly: true,
                    quota_mib: None,
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
/// every microVM mounts, one of the run's copy of the configuration at the
/// configuration's own path, any other at its own path.
pub fn command_path(host: &Host, config: &Configuration, path: &Path) -> Result<String, Failure> {
    if path.starts_with(&host.package) {
        code_path(host, path)
    } else {
        guest_path(&config.original(path).unwrap_or_else(|| path.to_owned()))
    }
}

/// Where a file of the package is inside the microVM.
fn code_path(host: &Host, path: &Path) -> Result<String, Failure> {
    let relative = path.strip_prefix(&host.package).expect("under the package");
    let relative = relative
        .components()
        .map(|part| part.as_os_str().to_str().ok_or_else(|| unnamed(path)))
        .collect::<Result<Vec<_>, _>>()?
        .join("/");
    Ok(format!("{CODE_ROOT}/{relative}"))
}

/// POSIX paths retain their spelling; Windows drives become `/c/...`.
pub fn guest_path(path: &Path) -> Result<String, Failure> {
    let text = path.to_str().ok_or_else(|| unnamed(path))?;
    if text.starts_with("\\\\") || text.starts_with("//") {
        return Err(Failure::Command(Sentence::Text(format!(
            "'{text}' is a network path and cannot be mounted"
        ))));
    }
    let bytes = text.as_bytes();
    if bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'/' | b'\\')
    {
        let drive = char::from(bytes[0]).to_ascii_lowercase();
        let mut guest = format!("/{drive}");
        for part in text[3..]
            .split(['/', '\\'])
            .filter(|s| !s.is_empty() && *s != ".")
        {
            guest.push('/');
            guest.push_str(part);
        }
        return Ok(guest);
    }
    if !text.starts_with('/') {
        return Err(Failure::Command(Sentence::Text(format!(
            "'{text}' is not an absolute path"
        ))));
    }
    // Containing a reserved path or contained by one alike, by components.
    if let Some(reserved) = RESERVED
        .iter()
        .find(|reserved| path.starts_with(reserved) || Path::new(reserved).starts_with(path))
    {
        return Err(Failure::Command(Sentence::Text(format!(
            "'{text}' overlaps {reserved}, which the environment keeps for itself"
        ))));
    }
    Ok(text.to_owned())
}

fn unnamed(path: &Path) -> Failure {
    Failure::Command(Sentence::Text(format!(
        "'{}' cannot be named inside the environment.",
        path.display()
    )))
}
