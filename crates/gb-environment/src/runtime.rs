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
    pub network: &'a Network,
    pub worktree: Option<&'a crate::worktree::Copy>,
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

/// Place the exact runtime carried by this executable, once per version.
pub fn ensure(home: &Path) -> Result<(), Failure> {
    place(
        home,
        [
            include_bytes!(env!("GB_MSB")),
            include_bytes!(env!("GB_KRUN")),
        ],
    )
}

pub fn doctor(home: &Path, bundled: &Path) -> Result<(), Failure> {
    let files = placed_files(bundled);
    let bytes: Vec<_> = files
        .iter()
        .map(std::fs::read)
        .collect::<Result<_, _>>()
        .map_err(|e| Failure::Command(Sentence::Text(e.to_string())))?;
    place(home, [&bytes[0], &bytes[1]])
}

fn place(home: &Path, bytes: [&[u8]; 2]) -> Result<(), Failure> {
    let failed = |e: std::io::Error| {
        Failure::Command(Sentence::Text(format!(
            "Cannot place microVM runtime at {}: {e}",
            home.display()
        )))
    };
    let _held = gb_base::files::FileLock::hold(&home.join("install.lock"), Duration::from_secs(30))
        .map_err(failed)?;
    if ready(home).is_ok() {
        return Ok(());
    }
    for (i, path) in placed_files(home).iter().enumerate() {
        gb_base::files::atomic_write(path, bytes[i]).map_err(failed)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if i == 0 {
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
                    .map_err(failed)?;
            }
        }
    }
    gb_base::files::atomic_write(&home.join("version"), RUNTIME_VERSION.as_bytes())
        .map_err(failed)?;
    ready(home)
}

/// The candidate population shared by the picker, declaration, and startup.
pub async fn images(
    home: &Path,
    default: &str,
) -> Result<Vec<(String, String, Option<u64>)>, Failure> {
    ensure(home)?;
    backend(home).await?;
    let mut found = Vec::new();
    let failed = |e: String| {
        Failure::Command(Sentence::Key(
            "intelligences.agent_environment.runtime.images_failed",
            vec![("error", e)],
        ))
    };
    for handle in microsandbox::Image::list()
        .await
        .map_err(|e| failed(e.to_string()))?
    {
        if eligible_image(
            handle.reference(),
            default,
            handle.architecture(),
            crate::image::device_architecture(),
        ) {
            let detail = microsandbox::Image::inspect(handle.reference())
                .await
                .map_err(|e| failed(e.to_string()))?;
            if !eligible_image(
                detail.handle.reference(),
                default,
                detail.handle.architecture(),
                crate::image::device_architecture(),
            ) {
                continue;
            }
            if let Some(config) = detail.config.filter(|c| !c.digest.is_empty()) {
                found.push((
                    handle.reference().to_owned(),
                    config.digest,
                    handle.size_bytes().and_then(|n| u64::try_from(n).ok()),
                ));
            }
        }
    }
    Ok(found)
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

/// Import into the local image store, without booting a microVM. Inspect
/// returned handles rather than comparing inventories: reloading a tag is valid.
pub async fn load_image(
    home: &Path,
    archive: &Path,
    tag: Option<String>,
) -> Result<Vec<(String, String)>, String> {
    crate::image_archive::validate(archive, crate::image::device_architecture())
        .map_err(|e| format!("{}: {e}", archive.display()))?;
    ready(home).map_err(|_| {
        format!(
            "The microVM runtime under {} is unavailable or is not version {RUNTIME_VERSION}.",
            home.display()
        )
    })?;
    backend(home).await.map_err(|e| format!("{e:?}"))?;
    let unpacked = crate::image_archive::uncompressed(archive).map_err(|e| e.to_string())?;
    let input = unpacked.as_ref().map(|file| file.path()).unwrap_or(archive);
    let handles = microsandbox::Image::load(input, tag.into_iter().collect())
        .await
        .map_err(|e| format!("{}: {e}", archive.display()))?;
    if handles.is_empty() {
        return Err(format!("No image was loaded from {}.", archive.display()));
    }
    let mut loaded = Vec::new();
    for handle in handles {
        let reference = handle.reference().to_owned();
        let detail = microsandbox::Image::inspect(&reference)
            .await
            .map_err(|e| e.to_string())?;
        let digest = detail
            .config
            .map(|c| c.digest)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("No configuration digest for {reference}"))?;
        loaded.push((reference, digest));
    }
    Ok(loaded)
}

/// Read a custom reference without pulling it or booting a microVM.
pub async fn held_image(
    home: &Path,
    reference: &str,
    default: &str,
) -> Result<Option<String>, Failure> {
    Ok(images(home, default)
        .await?
        .into_iter()
        .find(|(name, _, _)| name == reference)
        .map(|(_, digest, _)| digest))
}

fn eligible_image(
    reference: &str,
    default: &str,
    architecture: Option<&str>,
    device: &str,
) -> bool {
    reference != default && !reference.contains("@sha256:") && architecture == Some(device)
}

#[cfg(test)]
mod image_tests {
    use super::eligible_image;

    #[test]
    fn only_named_custom_images_for_this_cpu_are_candidates() {
        let default = "node:24.22.1-bookworm";
        assert!(eligible_image(
            "local/agent:1",
            default,
            Some("arm64"),
            "arm64"
        ));
        for (reference, architecture) in [
            (default, Some("arm64")),
            ("local/agent@sha256:abcd", Some("arm64")),
            ("local/agent:1", Some("amd64")),
            ("local/agent:1", None),
        ] {
            assert!(!eligible_image(reference, default, architecture, "arm64"));
        }
    }
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
    let sandbox = create_vm(boot).await?;
    let ran = async {
        // Disable IPv6 before tools use resolvers; the policy and upstreams are IPv4.
        capture(
            &sandbox,
            &[
                "sh".into(),
                "-ec".into(),
                "echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6".into(),
            ],
            &[],
            &[],
            1024,
        )
        .await?;
        let copied = match boot.worktree {
            None => None,
            Some(copy) => Some(
                capture(
                    &sandbox,
                    &worktree_argv("copy", copy),
                    &[],
                    process.env,
                    64 * 1024 * 1024,
                )
                .await?,
            ),
        };
        let result = execute(&sandbox, process).await;
        if let (Some(copy), Some(copied)) = (boot.worktree, copied) {
            let changes = capture(
                &sandbox,
                &worktree_argv("changes", copy),
                &copied,
                process.env,
                1024 * 1024 * 1024,
            )
            .await?;
            crate::worktree::write_back(copy, &copied, &changes)
                .map_err(|e| Failure::Command(Sentence::Text(e)))?;
        }
        result
    }
    .await;
    stop_vm(&sandbox).await;
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

/// Egress policy for a VM; host windows are restricted to their leased ports.
#[derive(Debug, Clone, Default)]
pub struct Network {
    pub unrestricted: bool,
    pub domains: Vec<String>,
    pub host_ports: Vec<u16>,
    pub local_network: bool,
    pub nameservers: Vec<String>,
}
impl Network {
    pub fn policy(&self) -> Result<microsandbox::NetworkPolicy, String> {
        use serde_json::json;
        let rule = |dest: serde_json::Value,
                    protocols: Vec<&str>,
                    ports: Vec<serde_json::Value>| json!({"direction":"egress","action":"allow","destination":dest,"protocols":protocols,"ports":ports});
        let mut rules = vec![rule(
            json!({"group":"host"}),
            vec!["udp", "tcp"],
            vec![json!({"start":53,"end":53})],
        )];
        for port in &self.host_ports {
            rules.push(rule(
                json!({"group":"host"}),
                vec!["tcp"],
                vec![json!({"start":port,"end":port})],
            ));
        }
        for domain in &self.domains {
            let dest = if let Some(suffix) = domain.strip_prefix("*.") {
                json!({"domain_suffix":suffix})
            } else {
                json!({"domain":domain})
            };
            rules.push(rule(dest, vec![], vec![]));
        }
        if self.local_network {
            for group in ["host", "private"] {
                rules.push(rule(json!({"group":group}), vec![], vec![]));
            }
        }
        serde_json::from_value(json!({"default_egress":if self.unrestricted{"allow"}else{"deny"},"default_ingress":"deny","rules":rules})).map_err(|e|e.to_string())
    }
}

/// Build from a named image, stream each step, then snapshot the stopped VM.
pub struct Build<'a> {
    pub home: &'a Path,
    pub directory: &'a Path,
    pub recipe: &'a crate::recipe::Recipe,
    pub image: &'a str,
    pub pull: bool,
    pub guest_home: &'a str,
    pub resources: gb_config::agent_environment::Resources,
    pub nameservers: &'a [String],
}

pub async fn build_snapshot(plan: Build<'_>, on_line: impl Fn(&str)) -> Result<PathBuf, String> {
    let Build {
        home,
        directory,
        recipe,
        image,
        pull,
        guest_home,
        resources,
        nameservers,
    } = plan;
    backend(home).await.map_err(|e| format!("{e:?}"))?;
    let policy = Network {
        unrestricted: true,
        nameservers: nameservers.to_vec(),
        ..Default::default()
    }
    .policy()?;
    let vm = Sandbox::builder("guildbotics-build")
        .image(image)
        .registry(|r| r.auth(microsandbox::RegistryAuth::Anonymous))
        .pull_policy(if pull {
            microsandbox::sandbox::PullPolicy::IfMissing
        } else {
            microsandbox::sandbox::PullPolicy::Never
        })
        .memory(resources.memory_mib)
        .cpus(resources.cpus)
        .workdir("/")
        .network(|n| {
            n.policy(policy).dns(|d| {
                d.nameservers(
                    nameservers
                        .iter()
                        .filter_map(|s| s.parse::<std::net::IpAddr>().ok()),
                )
            })
        })
        .create()
        .await
        .map_err(|e| e.to_string())?;
    let result = async {
        let built = serde_json::to_value(vm.config()).map_err(|e| e.to_string())?;
        let manifest = built["manifest_digest"]
            .as_str()
            .ok_or("Build VM has no image manifest")?;
        let mut held = None;
        for handle in microsandbox::Image::list()
            .await
            .map_err(|e| e.to_string())?
        {
            if handle.manifest_digest() == Some(manifest) {
                let inspected = microsandbox::Image::inspect(handle.reference())
                    .await
                    .map_err(|e| e.to_string())?;
                if inspected.handle.manifest_digest() == Some(manifest) {
                    held = inspected.config.map(|c| c.digest);
                }
                if held.is_some() {
                    break;
                }
            }
        }
        if !pull && held.is_none() {
            return Err("The image actually used for this build cannot be identified".into());
        }
        let name = recipe.name(if pull { None } else { held.as_deref() });
        capture(
            &vm,
            &[
                "sh".into(),
                "-ec".into(),
                "echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6".into(),
            ],
            &[],
            &[],
            1024,
        )
        .await
        .map_err(|e| format!("{e:?}"))?;
        for step in &recipe.steps {
            on_line(&format!("[{}]", step.label));
            let mut stream = vm
                .exec_stream_with("sh", |e| {
                    e.args(["-ec", step.script.as_str()])
                        .env("HOME", guest_home)
                        .env("DEBIAN_FRONTEND", "noninteractive")
                })
                .await
                .map_err(|e| e.to_string())?;
            let mut code = -1;
            while let Some(event) = stream.recv().await {
                match event {
                    ExecEvent::Stdout(bytes) | ExecEvent::Stderr(bytes) => {
                        on_line(&String::from_utf8_lossy(&bytes))
                    }
                    ExecEvent::Exited { code: c } => {
                        code = c;
                        break;
                    }
                    ExecEvent::Failed(error) => return Err(error.message),
                    _ => {}
                }
            }
            if code != 0 {
                return Err(format!("Build step {} exited with {code}", step.label));
            }
        }
        vm.stop_with_timeout(STOP_TIMEOUT)
            .await
            .map_err(|e| e.to_string())?;
        let snapshot = microsandbox::Snapshot::builder(name)
            .from_sandbox("guildbotics-build")
            .dest_dir(directory)
            .force()
            .create()
            .await
            .map_err(|e| e.to_string())?;
        let path = snapshot.path().to_owned();
        for entry in std::fs::read_dir(directory).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            if entry.path() != path
                && entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("guildbotics-")
                && entry.file_type().map_err(|e| e.to_string())?.is_dir()
            {
                microsandbox::Snapshot::remove(&entry.path().to_string_lossy(), true)
                    .await
                    .map_err(|e| e.to_string())?;
            }
        }
        Ok(path)
    }
    .await;
    if let Err(e) = vm.destroy().await {
        tracing::warn!("Build VM cleanup failed: {e}");
    }
    result
}

/// Remove SDK metadata together with artifacts while retaining provider logins.
pub async fn remove_snapshots(home: &Path, workspace: &Path) -> Result<(), String> {
    ensure(home).map_err(|e| format!("{e:?}"))?;
    backend(home).await.map_err(|e| format!("{e:?}"))?;
    crate::snapshot::remove(workspace, |path| async move {
        microsandbox::Snapshot::remove(&path.to_string_lossy(), true)
            .await
            .map_err(|e| e.to_string())
    })
    .await
}

fn worktree_argv(operation: &str, copy: &crate::worktree::Copy) -> Vec<String> {
    let mut argv = vec![
        "/opt/guildbotics/venv/bin/python".into(),
        "-B".into(),
        "-m".into(),
        "guildbotics.guest.worktree_copy".into(),
        operation.into(),
        crate::worktree::SOURCE.into(),
        copy.guest.clone(),
    ];
    argv.extend(copy.excluded.iter().cloned());
    argv
}

async fn capture(
    vm: &Sandbox,
    argv: &[String],
    input: &[u8],
    env: &[(String, String)],
    limit: usize,
) -> Result<Vec<u8>, Failure> {
    let fail = |e: String| Failure::Command(Sentence::Text(e));
    let (program, args) = argv.split_first().expect("program");
    let mut handle = vm
        .exec_stream_with(program, |e| {
            e.args(args.iter().cloned())
                .cwd("/")
                .envs(env.iter().cloned())
                .stdin_pipe()
        })
        .await
        .map_err(|e| fail(e.to_string()))?;
    let stdin = handle.take_stdin().expect("piped stdin");
    stdin.write(input).await.map_err(|e| fail(e.to_string()))?;
    stdin.close().await.map_err(|e| fail(e.to_string()))?;
    let mut output = Vec::new();
    let mut code = -1;
    while let Some(event) = handle.recv().await {
        match event {
            ExecEvent::Stdout(bytes) => {
                output.extend(bytes);
                if output.len() > limit {
                    return Err(fail("Worktree output exceeds its limit".into()));
                }
            }
            ExecEvent::Stderr(bytes) => tracing::warn!("{}", String::from_utf8_lossy(&bytes)),
            ExecEvent::Exited { code: c } => {
                code = c;
                break;
            }
            ExecEvent::Failed(e) => return Err(fail(e.message)),
            _ => {}
        }
    }
    if code != 0 {
        return Err(fail(format!("{program} exited with {code}")));
    }
    Ok(output)
}

async fn create_vm(boot: &Boot<'_>) -> Result<Sandbox, Failure> {
    backend(boot.home).await?;
    let name = format!(
        "{NAME_PREFIX}{}",
        &uuid::Uuid::new_v4().simple().to_string()[..12]
    );
    let policy = boot
        .network
        .policy()
        .map_err(|e| Failure::Command(Sentence::Text(e)))?;
    let nameservers = boot
        .network
        .nameservers
        .iter()
        .map(|s| s.parse::<std::net::IpAddr>())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| Failure::Command(Sentence::Text(e.to_string())))?;
    remove_stale_disks().await;
    let mut builder = Sandbox::builder(&name)
        .from_snapshot(boot.snapshot.to_string_lossy())
        .ephemeral(true)
        .memory(boot.memory_mib)
        .cpus(boot.cpus)
        .workdir("/")
        .network(|n| n.policy(policy).dns(|d| d.nameservers(nameservers)))
        .volume("/tmp", |m| {
            m.named_with(format!("{name}-0"), |v| v.create().disk().size(4096))
        });
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
    if let Some(copy) = boot.worktree {
        copy.validate()
            .map_err(|e| Failure::Command(Sentence::Text(e)))?;
        builder = builder.volume(crate::worktree::SOURCE, |m| m.bind(&copy.host).readonly());
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
    Ok(sandbox)
}

async fn stop_vm(sandbox: &Sandbox) {
    let name = sandbox.name();
    if let Err(e) = sandbox.stop_with_timeout(STOP_TIMEOUT).await {
        tracing::warn!("The microVM {name} did not stop, and is killed: {e}");
        if let Err(e) = sandbox.kill().await {
            tracing::warn!("The microVM {name} was not killed: {e}");
        }
    }
    let _ = sandbox.destroy().await;
    let _ = microsandbox::Volume::remove(&format!("{name}-0")).await;
}

/// Login/probe output is private and is never passed to the host's logger.
/// State mounts remain owned by the caller through shutdown and resealing.
pub async fn run_private(boot: &Boot<'_>, process: &Process<'_>) -> Result<Ended, Failure> {
    let sandbox = create_vm(boot).await?;
    let result = execute_private(&sandbox, process, |_| {}).await;
    stop_vm(&sandbox).await;
    result
}

async fn execute_private(
    sandbox: &Sandbox,
    process: &Process<'_>,
    on_output: impl Fn(&[u8]) + Send + Sync,
) -> Result<Ended, Failure> {
    execute_private_tty(sandbox, process, on_output, false).await
}

async fn execute_private_tty(
    sandbox: &Sandbox,
    process: &Process<'_>,
    on_output: impl Fn(&[u8]) + Send + Sync,
    interactive: bool,
) -> Result<Ended, Failure> {
    let failure = || Failure::Runtime(Sentence::Text("The private provider process failed".into()));
    let (program, args) = process.argv.split_first().ok_or_else(failure)?;
    let mut handle = sandbox
        .exec_stream_with(program, |e| {
            e.args(args.iter().cloned())
                .cwd(process.cwd)
                .envs(process.env.iter().cloned())
                .stdin_pipe()
                .tty(interactive)
        })
        .await
        .map_err(|_| failure())?;
    let stdin = handle.take_stdin().ok_or_else(failure)?;
    stdin.write(process.stdin).await.map_err(|_| failure())?;
    let mut bridge = if interactive {
        Some(InputBridge::start().map_err(|_| failure())?)
    } else {
        stdin.close().await.map_err(|_| failure())?;
        None
    };
    let result = tokio::time::timeout(Duration::from_secs(1800), async {
        let mut reply = Vec::new();
        let mut code = -1;
        loop {
            let event = if let Some(input) = &mut bridge {
                tokio::select! {
                    event = handle.recv() => event,
                    bytes = input.receiver.recv() => {
                        match bytes {
                            Some(bytes) => stdin.write(&bytes).await.map_err(|_| failure())?,
                            None => {
                                stdin.close().await.map_err(|_| failure())?;
                                bridge = None;
                            }
                        }
                        continue;
                    }
                }
            } else {
                handle.recv().await
            };
            let Some(event) = event else {
                break;
            };
            match event {
                ExecEvent::Stdout(data) => {
                    on_output(&data);
                    reply.extend(data);
                    if reply.len() > REPLY_LIMIT {
                        return Err(failure());
                    }
                }
                ExecEvent::Stderr(data) => on_output(&data),
                ExecEvent::Exited { code: c } => {
                    code = c;
                    break;
                }
                ExecEvent::Failed(_) => return Err(failure()),
                _ => {}
            }
        }
        Ok(Ended { reply, code })
    })
    .await;
    match result {
        Ok(Ok(ended)) => Ok(ended),
        failed => {
            let _ = handle.kill().await;
            // Capture refresh state only after its writer has stopped.
            let _ = tokio::time::timeout(STOP_TIMEOUT, async {
                while let Some(event) = handle.recv().await {
                    if matches!(event, ExecEvent::Exited { .. } | ExecEvent::Failed(_)) {
                        break;
                    }
                }
            })
            .await;
            match failed {
                Ok(Err(error)) => Err(error),
                _ => Err(Failure::Runtime(Sentence::Text(
                    "Private provider process timed out".into(),
                ))),
            }
        }
    }
}

pub struct PrivateSession {
    pub ended: Result<Ended, Failure>,
    pub files: Result<std::collections::BTreeMap<String, Vec<u8>>, Failure>,
}
const PRIVATE_FILES: &str = r#"
import base64,json,os,stat,sys
from pathlib import Path
request=json.load(sys.stdin)
def checked(name):
 p=Path(name)
 if not p.is_absolute() or '..' in p.parts: raise ValueError('invalid private path')
 for parent in [p,*p.parents]:
  if parent.is_symlink(): raise ValueError('private path is a link')
 return p
if sys.argv[1]=='put':
 for name,data in request.items():
  p=checked(name);p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
  fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as out: out.write(base64.b64decode(data,validate=True))
else:
 result={}
 for name in request:
  p=checked(name)
  candidates=[p] if p.is_file() else p.rglob('*') if p.exists() else []
  for entry in candidates:
   entry=checked(str(entry))
   if entry.is_dir(): continue
   info=entry.stat(follow_symlinks=False)
   if not stat.S_ISREG(info.st_mode): raise ValueError('private path is not regular')
   fd=os.open(entry,os.O_RDONLY|os.O_NOFOLLOW)
   with os.fdopen(fd,'rb') as source: result[str(entry)]=base64.b64encode(source.read()).decode()
 print(json.dumps(result))
"#;

/// Private credentials cross the host/guest boundary only in memory. Capture
/// precedes shutdown even when the provider fails or reaches its time limit.
pub async fn run_private_session(
    boot: &Boot<'_>,
    process: &Process<'_>,
    files: &std::collections::BTreeMap<String, Vec<u8>>,
    capture: &[String],
    on_output: impl Fn(&[u8]) + Send + Sync,
) -> Result<PrivateSession, Failure> {
    private_session_inner(boot, process, files, capture, on_output, false).await
}

pub async fn run_private_login(
    boot: &Boot<'_>,
    process: &Process<'_>,
    files: &std::collections::BTreeMap<String, Vec<u8>>,
    capture: &[String],
    on_output: impl Fn(&[u8]) + Send + Sync,
) -> Result<PrivateSession, Failure> {
    private_session_inner(boot, process, files, capture, on_output, true).await
}

async fn private_session_inner(
    boot: &Boot<'_>,
    process: &Process<'_>,
    files: &std::collections::BTreeMap<String, Vec<u8>>,
    capture: &[String],
    on_output: impl Fn(&[u8]) + Send + Sync,
    interactive: bool,
) -> Result<PrivateSession, Failure> {
    use base64::{Engine, engine::general_purpose::STANDARD};
    let vm = create_vm(boot).await?;
    let failed = || Failure::Runtime(Sentence::Text("Private credential transfer failed".into()));
    let argv = vec![
        "/opt/guildbotics/venv/bin/python".into(),
        "-B".into(),
        "-c".into(),
        PRIVATE_FILES.into(),
        "put".into(),
    ];
    let encoded: std::collections::BTreeMap<_, _> = files
        .iter()
        .map(|(name, data)| (name.clone(), STANDARD.encode(data)))
        .collect();
    let input = serde_json::to_vec(&encoded).map_err(|_| failed())?;
    let setup = execute_private(
        &vm,
        &Process {
            argv: &argv,
            cwd: "/",
            env: &[],
            stdin: &input,
        },
        |_| {},
    )
    .await;
    if !setup.is_ok_and(|ended| ended.code == 0) {
        stop_vm(&vm).await;
        return Err(failed());
    }
    let ended = execute_private_tty(&vm, process, on_output, interactive).await;
    let argv = vec![
        "/opt/guildbotics/venv/bin/python".into(),
        "-B".into(),
        "-c".into(),
        PRIVATE_FILES.into(),
        "get".into(),
    ];
    let input = serde_json::to_vec(capture).map_err(|_| failed())?;
    let files = match execute_private(
        &vm,
        &Process {
            argv: &argv,
            cwd: "/",
            env: &[],
            stdin: &input,
        },
        |_| {},
    )
    .await
    {
        Ok(output) if output.code == 0 => {
            serde_json::from_slice::<std::collections::BTreeMap<String, String>>(&output.reply)
                .map_err(|_| failed())
                .and_then(|files| {
                    files
                        .into_iter()
                        .map(|(name, data)| {
                            STANDARD
                                .decode(data)
                                .map(|value| (name, value))
                                .map_err(|_| failed())
                        })
                        .collect()
                })
        }
        _ => Err(failed()),
    };
    stop_vm(&vm).await;
    Ok(PrivateSession { ended, files })
}

async fn remove_stale_disks() {
    let age = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
        - 600_000;
    if let Ok(volumes) = microsandbox::Volume::list().await {
        for volume in volumes {
            let name = volume.name();
            let ours = name
                .strip_prefix(NAME_PREFIX)
                .and_then(|n| n.split_once('-'))
                .is_some_and(|(id, index)| {
                    id.len() == 12
                        && id.bytes().all(|b| b.is_ascii_hexdigit())
                        && index.parse::<u32>().is_ok()
                });
            if ours
                && volume.kind() == microsandbox::VolumeKind::Disk
                && volume
                    .created_at()
                    .is_some_and(|t| t.timestamp_millis() < age)
            {
                let _ = microsandbox::Volume::remove(name).await;
            }
        }
    }
}

struct InputBridge {
    receiver: tokio::sync::mpsc::Receiver<Vec<u8>>,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl InputBridge {
    #[cfg(unix)]
    fn start() -> std::io::Result<Self> {
        use std::sync::Arc;
        use std::sync::atomic::{AtomicBool, Ordering};
        let (sender, receiver) = tokio::sync::mpsc::channel(8);
        let stop = Arc::new(AtomicBool::new(false));
        let halted = stop.clone();
        let thread = std::thread::Builder::new()
            .name("guildbotics-login-input".into())
            .spawn(move || {
                let mut poll = libc::pollfd {
                    fd: 0,
                    events: libc::POLLIN,
                    revents: 0,
                };
                while !halted.load(Ordering::Relaxed) {
                    let ready = unsafe { libc::poll(&mut poll, 1, 100) };
                    if ready <= 0 {
                        continue;
                    }
                    let mut bytes = vec![0u8; 4096];
                    let n = unsafe { libc::read(0, bytes.as_mut_ptr().cast(), bytes.len()) };
                    if n <= 0 {
                        break;
                    }
                    bytes.truncate(n as usize);
                    if sender.blocking_send(bytes).is_err() {
                        break;
                    }
                }
            })?;
        Ok(Self {
            receiver,
            stop,
            thread: Some(thread),
        })
    }
    #[cfg(windows)]
    fn start() -> std::io::Result<Self> {
        use std::io::Read;
        use std::sync::{
            Arc,
            atomic::{AtomicBool, Ordering},
        };
        let (sender, receiver) = tokio::sync::mpsc::channel(8);
        let stop = Arc::new(AtomicBool::new(false));
        let halted = stop.clone();
        let thread = std::thread::Builder::new()
            .name("guildbotics-login-input".into())
            .spawn(move || {
                let mut input = std::io::stdin();
                while !halted.load(Ordering::Relaxed) {
                    let mut bytes = vec![0; 4096];
                    match input.read(&mut bytes) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => bytes.truncate(n),
                    }
                    if sender.blocking_send(bytes).is_err() {
                        break;
                    }
                }
            })?;
        Ok(Self {
            receiver,
            stop,
            thread: Some(thread),
        })
    }
    #[cfg(not(any(unix, windows)))]
    fn start() -> std::io::Result<Self> {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "Interactive login is unavailable on this platform",
        ))
    }
}
impl Drop for InputBridge {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        self.receiver.close();
        if let Some(thread) = self.thread.take() {
            #[cfg(windows)]
            {
                use std::os::windows::thread::JoinHandleExt;
                #[link(name = "kernel32")]
                unsafe extern "system" {
                    fn CancelSynchronousIo(thread: *mut std::ffi::c_void) -> i32;
                }
                // Interrupt only this owned reader before joining it. No input
                // thread may survive and consume the next terminal command.
                unsafe {
                    CancelSynchronousIo(thread.as_raw_handle());
                }
            }
            let _ = thread.join();
        }
    }
}

#[cfg(test)]
mod private_files_tests {
    use super::PRIVATE_FILES;
    use std::io::Write;
    #[test]
    fn credential_transport_is_stdin_stdout_only_and_refuses_links() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("tool/auth.json");
        let request = serde_json::json!({path.to_str().unwrap():"Zml4dHVyZQ=="});
        let invoke = |operation: &str, request: serde_json::Value| {
            let mut child = std::process::Command::new("python3")
                .args(["-I", "-c", PRIVATE_FILES, operation])
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()
                .unwrap();
            child
                .stdin
                .take()
                .unwrap()
                .write_all(serde_json::to_string(&request).unwrap().as_bytes())
                .unwrap();
            child.wait_with_output().unwrap()
        };
        assert!(invoke("put", request).status.success());
        assert_eq!(std::fs::read(&path).unwrap(), b"fixture");
        let result = invoke("get", serde_json::json!([path.parent().unwrap()]));
        assert!(result.status.success());
        let files: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(files[path.to_str().unwrap()], "Zml4dHVyZQ==");
        #[cfg(unix)]
        {
            std::fs::remove_file(&path).unwrap();
            std::os::unix::fs::symlink(dir.path(), &path).unwrap();
            assert!(!invoke("get", serde_json::json!([path])).status.success());
        }
    }
}
