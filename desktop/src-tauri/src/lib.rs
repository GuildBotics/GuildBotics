use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{LogicalPosition, LogicalSize, Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

mod hotkeys;
mod tray;

/// Holds the Local API sidecar process and the connection details the frontend
/// needs to talk to it.
///
/// The sidecar is spawned once per app process and torn down when the app
/// exits, so a closed/reopened window reuses the same backend instead of
/// starting a second one. A freshly picked port avoids colliding with a sidecar
/// that may have been orphaned by a previous force-quit.
struct BackendState {
    token: String,
    port: u16,
    boot_log_path: PathBuf,
    sidecar: Mutex<Option<Sidecar>>,
}

struct Sidecar {
    child: CommandChild,
    /// Disconnects once the process has terminated.
    exited: Receiver<()>,
}

const BACKEND_REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
/// What the process still needs after its teardown has finished: the server
/// closing down.
/// How long the teardown itself may take is the backend's to say.
const BACKEND_EXIT_MARGIN: Duration = Duration::from_secs(5);

const BOOT_LOG_MAX_BYTES: usize = 1024 * 1024;
const BOOT_LOG_COMPACT_BYTES: usize = BOOT_LOG_MAX_BYTES / 2;
const BOOT_LOG_TAIL_BYTES: usize = 64 * 1024;
const BOOT_LOG_TAIL_LINES: usize = 100;

/// An AI CLI tool the desktop app installs the GuildBotics skill for.
struct CliAgent {
    name: &'static str,
    home_env: &'static str,
    home_dir: &'static str,
    /// Skills root relative to the user home directory, for tools that read user
    /// skills from a shared location instead of their own home directory.
    user_skills_dir: Option<&'static str>,
}

const CLI_AGENTS: [CliAgent; 5] = [
    CliAgent {
        name: "codex",
        home_env: "CODEX_HOME",
        home_dir: ".codex",
        user_skills_dir: Some(".agents/skills"),
    },
    CliAgent {
        name: "claude",
        home_env: "CLAUDE_HOME",
        home_dir: ".claude",
        user_skills_dir: None,
    },
    CliAgent {
        name: "grok",
        home_env: "GROK_HOME",
        home_dir: ".grok",
        user_skills_dir: None,
    },
    CliAgent {
        name: "antigravity",
        home_env: "ANTIGRAVITY_HOME",
        home_dir: ".gemini/config",
        user_skills_dir: None,
    },
    CliAgent {
        name: "copilot",
        home_env: "COPILOT_HOME",
        home_dir: ".copilot",
        user_skills_dir: None,
    },
];
const GUILDBOTICS_SKILL: &str = include_str!("../../../skills/guildbotics/SKILL.md");
const MANAGED_SKILL_METADATA: &str = ".guildbotics-managed.json";

#[tauri::command]
fn backend_info(state: tauri::State<'_, BackendState>) -> serde_json::Value {
    serde_json::json!({
        "port": state.port,
        "token": state.token,
    })
}

#[tauri::command]
fn bootstrap_log(state: tauri::State<'_, BackendState>) -> serde_json::Value {
    let tail = read_boot_log_tail(&state.boot_log_path).unwrap_or_default();
    serde_json::json!({
        "path": state.boot_log_path.display().to_string(),
        "tail": tail,
    })
}

fn append_boot_log(path: &Path, bytes: &[u8]) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    file.write_all(bytes)?;
    if !bytes.ends_with(b"\n") {
        file.write_all(b"\n")?;
    }
    file.flush()?;
    if file.metadata()?.len() as usize <= BOOT_LOG_MAX_BYTES {
        return Ok(());
    }
    drop(file);
    let mut content = Vec::new();
    fs::File::open(path)?.read_to_end(&mut content)?;
    let start = content.len().saturating_sub(BOOT_LOG_COMPACT_BYTES);
    fs::write(path, &content[start..])
}

/// Record a host-side event next to the backend's own output, so one log tells
/// how a session ended: who asked for the quit, and whether it was confirmed.
pub(crate) fn log_host_event(app: &tauri::AppHandle, message: &str) {
    if let Some(state) = app.try_state::<BackendState>() {
        let _ = append_boot_log(&state.boot_log_path, message.as_bytes());
    }
}

fn read_boot_log_tail(path: &Path) -> io::Result<String> {
    let mut content = Vec::new();
    fs::File::open(path)?.read_to_end(&mut content)?;
    let start = content.len().saturating_sub(BOOT_LOG_TAIL_BYTES);
    let bounded = String::from_utf8_lossy(&content[start..]);
    let lines = bounded.lines().collect::<Vec<_>>();
    let first = lines.len().saturating_sub(BOOT_LOG_TAIL_LINES);
    Ok(lines[first..].join("\n"))
}

#[tauri::command]
fn cli_agent_skill_statuses() -> serde_json::Value {
    match home_dir() {
        Ok(home) => serde_json::json!({
            "agents": CLI_AGENTS
                .iter()
                .map(|agent| cli_agent_skill_status(&home, agent))
                .collect::<Vec<_>>()
        }),
        Err(error) => serde_json::json!({
            "agents": [],
            "error": error.to_string(),
        }),
    }
}

#[tauri::command]
fn force_update_cli_agent_skill(agent: String) -> Result<serde_json::Value, String> {
    let home = home_dir().map_err(|error| error.to_string())?;
    let Some(agent) = CLI_AGENTS.iter().find(|candidate| candidate.name == agent) else {
        return Err(format!("unsupported AI CLI tool: {agent}"));
    };
    let Some(agent_home) = configured_agent_home(&home, agent.home_env, agent.home_dir) else {
        return Err(format!("skill home for {} was not detected", agent.name));
    };

    force_install_skill_file(&skill_dir(&home, agent, &agent_home), GUILDBOTICS_SKILL)
        .map_err(|error| error.to_string())?;
    Ok(cli_agent_skill_status(&home, agent))
}

/// One request to the Local API over loopback: the status code and the body.
fn backend_request(port: u16, token: &str, method: &str, path: &str) -> io::Result<(u16, String)> {
    let mut stream = TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], port)),
        BACKEND_REQUEST_TIMEOUT,
    )?;
    stream.set_read_timeout(Some(BACKEND_REQUEST_TIMEOUT))?;
    stream.set_write_timeout(Some(BACKEND_REQUEST_TIMEOUT))?;
    write!(
        stream,
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\
         X-GuildBotics-Session-Token: {token}\r\n\
         Content-Length: 0\r\nConnection: close\r\n\r\n"
    )?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    let (head, body) = response.split_once("\r\n\r\n").unwrap_or((&response, ""));
    let status = head
        .split(' ')
        .nth(1)
        .and_then(|code| code.parse().ok())
        .ok_or_else(|| io::Error::other("malformed response from the backend"))?;
    Ok((status, body.to_owned()))
}

/// Ask the Local API to exit through its own shutdown path, so its teardown
/// (scheduler stop, sync deactivation, session finish) runs before it goes.
///
/// Returns how long the backend says that teardown may take. The host waits
/// that long rather than keeping a number of its own, which would go stale the
/// moment the teardown gained a step and cut an orderly teardown short.
fn request_backend_shutdown(port: u16, token: &str) -> io::Result<Duration> {
    let (status, body) = backend_request(port, token, "POST", "/shutdown")?;
    if status != 202 {
        return Err(io::Error::other(format!(
            "unexpected shutdown response: {status}"
        )));
    }
    serde_json::from_str::<serde_json::Value>(&body)
        .ok()
        .and_then(|accepted| accepted.get("teardown_budget_seconds")?.as_f64())
        .and_then(|seconds| Duration::try_from_secs_f64(seconds).ok())
        .ok_or_else(|| io::Error::other("the backend did not state its teardown budget"))
}

/// Whether the backend has work that a quit would cut off. Anything short of
/// a clear "no" counts as yes: an unreadable state says nothing about the work.
#[cfg(any(target_os = "macos", test))]
fn backend_has_active_work(port: u16, token: &str) -> bool {
    let Ok((200, body)) = backend_request(port, token, "GET", "/scheduler/status") else {
        return true;
    };
    serde_json::from_str::<serde_json::Value>(&body)
        .ok()
        .and_then(|status| status.get("has_active_work")?.as_bool())
        .unwrap_or(true)
}

/// Whether a quit the app did not start itself (the Dock's Quit, logout) has
/// to go through the frontend's guard first. An idle app lets it proceed, so
/// it never holds up a logout it has no reason to.
#[cfg(target_os = "macos")]
pub(crate) fn quit_needs_confirmation(app: &tauri::AppHandle) -> bool {
    app.try_state::<BackendState>()
        .is_none_or(|state| backend_has_active_work(state.port, &state.token))
}

fn wait_for_exit(exited: &Receiver<()>, timeout: Duration) -> io::Result<()> {
    match exited.recv_timeout(timeout) {
        Err(RecvTimeoutError::Disconnected) => Ok(()),
        _ => Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "backend did not exit in time",
        )),
    }
}

/// Stop the sidecar gracefully, killing it only when that fails.
///
/// This runs on `RunEvent::Exit`, the one point every way of quitting passes
/// through. macOS can terminate the app without asking it first (the Dock's
/// Quit, logout), so the quit confirmation cannot be what keeps the backend
/// from being cut off mid-write.
fn stop_backend(state: &BackendState) {
    // Tolerate a poisoned mutex so the app can still exit cleanly.
    let mut guard = match state.sidecar.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    let Some(sidecar) = guard.take() else {
        return;
    };
    let stopped = request_backend_shutdown(state.port, &state.token)
        .and_then(|budget| wait_for_exit(&sidecar.exited, budget + BACKEND_EXIT_MARGIN));
    let outcome = match stopped {
        Ok(()) => "backend shut down gracefully".to_owned(),
        Err(error) => {
            let _ = sidecar.child.kill();
            format!("backend killed after a failed graceful shutdown: {error}")
        }
    };
    let _ = append_boot_log(&state.boot_log_path, outcome.as_bytes());
}

/// Reserve a free loopback TCP port by binding to port 0 and reading back the
/// assigned port. Falls back to the historical default if the probe fails.
fn pick_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .ok()
        .and_then(|listener| listener.local_addr().ok())
        .map(|addr| addr.port())
        .unwrap_or(8765)
}

fn home_dir() -> io::Result<PathBuf> {
    #[allow(deprecated)]
    resolve_home_dir(std::env::home_dir())
}

fn resolve_home_dir(discovered: Option<PathBuf>) -> io::Result<PathBuf> {
    discovered
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "home directory not found"))
}

/// The resource directory holding the bundled Python programs: the member CLI
/// `guildbotics`, the Local API `guildbotics-app-api`, and the `_internal/`
/// directory they share.
const PROGRAMS_DIR: &str = "guildbotics";
/// Names the build a programs directory came from.
const BUILD_ID_FILE: &str = "build-id";

#[cfg(unix)]
fn make_executable(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn make_executable(_path: &Path) -> io::Result<()> {
    Ok(())
}

fn install_member_cli(home: &Path, programs: &Path) -> io::Result<()> {
    install_programs(programs, &home.join(".guildbotics"))?;

    if should_install_shell_shim(std::env::consts::OS) {
        let local_bin = home.join(".local").join("bin");
        let local_cli = local_bin.join("guildbotics");
        fs::create_dir_all(&local_bin)?;
        if should_write_managed_shim(&local_cli) {
            fs::write(
                &local_cli,
                "#!/bin/sh\n# Managed by GuildBotics desktop.\nexec \"$HOME/.guildbotics/bin/guildbotics\" \"$@\"\n",
            )?;
            make_executable(&local_cli)?;
        }
    }
    Ok(())
}

/// Install the bundled build under `root` and point `root/bin` at it.
///
/// Each build is its own directory, `root/programs/<build id>`, which appears
/// complete (it is copied under another name first) and is never modified, and
/// `bin` is a link to one of them. `bin` moves only once the new link exists,
/// and the build it pointed at is removed on a later launch, so there is always
/// a working CLI: an interrupted step is simply run again on the next launch.
///
/// A build still in use is never taken away. While a program runs from an old
/// build, `bin` keeps pointing at it until a later launch, since on Windows a
/// program started through the junction reads its files through `bin`.
fn install_programs(source: &Path, root: &Path) -> io::Result<()> {
    let build_id = fs::read_to_string(source.join(BUILD_ID_FILE))?;
    let build_id = build_id.trim();
    let programs = root.join("programs");
    let current = programs.join(build_id);
    if !current.exists() {
        let staging = programs.join(format!("{build_id}.staging"));
        if staging.exists() {
            fs::remove_dir_all(&staging)?;
        }
        copy_dir(source, &staging)?;
        fs::rename(&staging, &current)?;
    }
    let mut older = Vec::new();
    for entry in fs::read_dir(&programs)? {
        let program = entry?.path();
        if program != current {
            older.push(program);
        }
    }
    let bin = root.join("bin");
    let linked = fs::read_link(&bin).ok();
    let linked = linked.as_deref().and_then(Path::file_name);
    if linked != current.file_name() {
        if older.iter().any(|program| is_running(program)) {
            eprintln!("GuildBotics CLI update deferred: a program runs from an older build");
            return Ok(());
        }
        point_link(&bin, &current)?;
    }
    for program in older {
        // What `bin` pointed at until now may have just been started through
        // it and not yet hold its lock; it goes on a later launch, which no
        // new program reaches it by.
        if program.file_name() != linked && !is_running(&program) {
            // Retried on the next launch if something still holds a file.
            let _ = fs::remove_dir_all(program);
        }
    }
    Ok(())
}

/// Whether a program runs from `build`, answering yes when that cannot be
/// ruled out. A running CLI holds a shared lock on its executable for its
/// whole life (`desktop/sidecar/hold_program_lock.py`).
#[cfg(unix)]
fn is_running(build: &Path) -> bool {
    match fs::File::open(build.join("guildbotics")) {
        Ok(executable) => executable.try_lock().is_err(),
        Err(error) => error.kind() != io::ErrorKind::NotFound,
    }
}

/// Whether a program runs from `build`, answering yes when that cannot be
/// ruled out. Windows refuses to open a running executable for writing.
#[cfg(windows)]
fn is_running(build: &Path) -> bool {
    match fs::OpenOptions::new()
        .write(true)
        .open(build.join("guildbotics.exe"))
    {
        Ok(_) => false,
        Err(error) => error.kind() != io::ErrorKind::NotFound,
    }
}

fn sibling(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

/// Replace whatever `link` is with a link to `target`, in one rename.
#[cfg(unix)]
fn point_link(link: &Path, target: &Path) -> io::Result<()> {
    let pending = sibling(link, ".pending");
    let _ = fs::remove_file(&pending);
    std::os::unix::fs::symlink(target, &pending)?;
    if fs::symlink_metadata(link).is_ok_and(|metadata| metadata.is_dir()) {
        // A directory is not renamed over; only an earlier layout left one.
        fs::remove_dir_all(link)?;
    }
    fs::rename(&pending, link)
}

/// Replace whatever `link` is with a junction to `target` (a symbolic link
/// needs a privilege on Windows). A directory cannot be renamed over, so the
/// old one steps aside first and comes back if the new one cannot take its
/// place.
#[cfg(windows)]
fn point_link(link: &Path, target: &Path) -> io::Result<()> {
    let pending = sibling(link, ".pending");
    let previous = sibling(link, ".previous");
    for leftover in [&pending, &previous] {
        if fs::symlink_metadata(leftover).is_ok() {
            // Removes a junction itself, not what it points at.
            fs::remove_dir_all(leftover)?;
        }
    }
    junction::create(target, &pending)?;
    if fs::symlink_metadata(link).is_ok() {
        fs::rename(link, &previous)?;
    }
    if let Err(error) = fs::rename(&pending, link) {
        let _ = fs::rename(&previous, link);
        return Err(error);
    }
    let _ = fs::remove_dir_all(&previous);
    Ok(())
}

/// Copy a directory tree, keeping symbolic links as links rather than following
/// them, since a PyInstaller build on a POSIX system may lay libraries out so.
fn copy_dir(source: &Path, target: &Path) -> io::Result<()> {
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let destination = target.join(entry.file_name());
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            copy_symlink(&entry.path(), &destination)?;
        } else if file_type.is_dir() {
            copy_dir(&entry.path(), &destination)?;
        } else {
            fs::copy(entry.path(), destination)?;
        }
    }
    Ok(())
}

#[cfg(unix)]
fn copy_symlink(source: &Path, target: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(fs::read_link(source)?, target)
}

#[cfg(not(unix))]
fn copy_symlink(source: &Path, target: &Path) -> io::Result<()> {
    fs::copy(source, target).map(drop)
}

fn executable_name_for(base: &str, os: &str) -> String {
    if os == "windows" {
        format!("{base}.exe")
    } else {
        base.to_owned()
    }
}

fn platform_executable_name(base: &str) -> String {
    executable_name_for(base, std::env::consts::OS)
}

fn should_install_shell_shim(os: &str) -> bool {
    os != "windows"
}

fn should_write_managed_shim(path: &Path) -> bool {
    if !path.exists() {
        return true;
    }
    fs::read_to_string(path)
        .map(|content| content.contains("Managed by GuildBotics desktop."))
        .unwrap_or(false)
}

fn content_hash(content: &str) -> String {
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in content.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn read_managed_skill_hash(path: &Path) -> Option<String> {
    let content = fs::read_to_string(path).ok()?;
    let metadata: serde_json::Value = serde_json::from_str(&content).ok()?;
    metadata
        .get("content_hash")
        .and_then(|value| value.as_str())
        .map(str::to_owned)
}

fn is_unedited_managed_skill(skill_path: &Path, metadata_path: &Path) -> io::Result<bool> {
    let Some(previous_hash) = read_managed_skill_hash(metadata_path) else {
        return Ok(false);
    };

    let current_content = fs::read_to_string(skill_path)?;
    Ok(content_hash(&current_content) == previous_hash)
}

fn should_write_managed_skill(skill_path: &Path, metadata_path: &Path) -> io::Result<bool> {
    if !skill_path.exists() {
        return Ok(true);
    }

    is_unedited_managed_skill(skill_path, metadata_path)
}

fn install_skill_file(skill_dir: &Path, skill_content: &str) -> io::Result<()> {
    let skill_path = skill_dir.join("SKILL.md");
    let metadata_path = skill_dir.join(MANAGED_SKILL_METADATA);

    if !should_write_managed_skill(&skill_path, &metadata_path)? {
        return Ok(());
    }

    write_managed_skill(skill_dir, &skill_path, &metadata_path, skill_content)
}

fn force_install_skill_file(skill_dir: &Path, skill_content: &str) -> io::Result<()> {
    let skill_path = skill_dir.join("SKILL.md");
    let metadata_path = skill_dir.join(MANAGED_SKILL_METADATA);

    write_managed_skill(skill_dir, &skill_path, &metadata_path, skill_content)
}

/// Drop a skill copy this app wrote at a location the tool no longer reads user
/// skills from, so the same skill is not registered twice. Skills the user
/// created or edited stay untouched.
fn remove_managed_skill(skill_dir: &Path) -> io::Result<()> {
    let skill_path = skill_dir.join("SKILL.md");
    let metadata_path = skill_dir.join(MANAGED_SKILL_METADATA);

    if !skill_path.exists() || !is_unedited_managed_skill(&skill_path, &metadata_path)? {
        return Ok(());
    }

    fs::remove_file(&skill_path)?;
    fs::remove_file(&metadata_path)?;
    let _ = fs::remove_dir(skill_dir);
    Ok(())
}

fn write_managed_skill(
    skill_dir: &Path,
    skill_path: &Path,
    metadata_path: &Path,
    skill_content: &str,
) -> io::Result<()> {
    fs::create_dir_all(skill_dir)?;
    fs::write(skill_path, skill_content)?;
    let metadata = serde_json::json!({
        "manager": "GuildBotics desktop",
        "skill": "guildbotics",
        "content_hash": content_hash(skill_content),
    });
    let metadata_content = serde_json::to_string_pretty(&metadata)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    fs::write(metadata_path, format!("{metadata_content}\n"))
}

fn configured_agent_home(home: &Path, env_name: &str, default_dir: &str) -> Option<PathBuf> {
    if let Some(path) = std::env::var_os(env_name)
        .map(PathBuf::from)
        .filter(|path| !path.as_os_str().is_empty())
    {
        return Some(path);
    }

    let default_path = home.join(default_dir);
    default_path.exists().then_some(default_path)
}

/// Resolve the directory the GuildBotics skill is installed in for one tool.
fn skill_dir(home: &Path, agent: &CliAgent, agent_home: &Path) -> PathBuf {
    match agent.user_skills_dir {
        Some(relative) => home.join(relative),
        None => agent_home.join("skills"),
    }
    .join("guildbotics")
}

fn cli_agent_skill_status(home: &Path, agent: &CliAgent) -> serde_json::Value {
    let Some(agent_home) = configured_agent_home(home, agent.home_env, agent.home_dir) else {
        return serde_json::json!({
            "agent": agent.name,
            "agent_home": null,
            "skill_path": null,
            "status": "agent_home_missing",
            "can_force_update": false,
        });
    };

    let skill_dir = skill_dir(home, agent, &agent_home);
    let skill_path = skill_dir.join("SKILL.md");
    let metadata_path = skill_dir.join(MANAGED_SKILL_METADATA);

    if !skill_path.exists() {
        return serde_json::json!({
            "agent": agent.name,
            "agent_home": agent_home,
            "skill_path": skill_path,
            "status": "missing",
            "can_force_update": true,
        });
    }

    let Some(previous_hash) = read_managed_skill_hash(&metadata_path) else {
        return serde_json::json!({
            "agent": agent.name,
            "agent_home": agent_home,
            "skill_path": skill_path,
            "status": "unmanaged",
            "can_force_update": true,
        });
    };

    match fs::read_to_string(&skill_path) {
        Ok(current_content) => {
            let current_hash = content_hash(&current_content);
            let bundled_hash = content_hash(GUILDBOTICS_SKILL);
            let status = if current_hash != previous_hash {
                "user_modified"
            } else if current_hash != bundled_hash {
                "outdated"
            } else {
                "up_to_date"
            };
            serde_json::json!({
                "agent": agent.name,
                "agent_home": agent_home,
                "skill_path": skill_path,
                "status": status,
                "can_force_update": status != "up_to_date",
            })
        }
        Err(error) => serde_json::json!({
            "agent": agent.name,
            "agent_home": agent_home,
            "skill_path": skill_path,
            "status": "error",
            "can_force_update": false,
            "error": error.to_string(),
        }),
    }
}

fn install_cli_agent_assets(programs: &Path) -> io::Result<()> {
    let home = home_dir()?;
    install_member_cli(&home, programs)?;

    for agent in &CLI_AGENTS {
        let Some(agent_home) = configured_agent_home(&home, agent.home_env, agent.home_dir) else {
            continue;
        };
        install_skill_file(&skill_dir(&home, agent, &agent_home), GUILDBOTICS_SKILL)?;
        if agent.user_skills_dir.is_some() {
            remove_managed_skill(&agent_home.join("skills").join("guildbotics"))?;
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};

    struct TestDir {
        path: PathBuf,
    }

    impl TestDir {
        fn new() -> io::Result<Self> {
            let path = std::env::temp_dir()
                .join(format!("guildbotics-desktop-test-{}", uuid::Uuid::new_v4()));
            fs::create_dir_all(&path)?;
            Ok(Self { path })
        }

        fn path(&self) -> &Path {
            &self.path
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    fn agent(name: &'static str) -> &'static CliAgent {
        CLI_AGENTS
            .iter()
            .find(|candidate| candidate.name == name)
            .expect("known AI CLI tool")
    }

    /// Serve one canned HTTP response and hand back the request that came in.
    fn serve_once(response: &'static str) -> (u16, std::thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept");
            let mut request = String::new();
            let mut reader = BufReader::new(stream.try_clone().expect("clone"));
            while !request.ends_with("\r\n\r\n") {
                reader.read_line(&mut request).expect("read");
            }
            stream.write_all(response.as_bytes()).expect("write");
            request
        });
        (port, server)
    }

    #[test]
    fn shutdown_request_posts_the_session_token() {
        let (port, server) =
            serve_once("HTTP/1.1 202 Accepted\r\n\r\n{\"teardown_budget_seconds\":60.5}");

        let budget = request_backend_shutdown(port, "session-token").expect("accepted");

        assert_eq!(budget, Duration::from_millis(60_500));

        let request = server.join().expect("server");
        assert!(request.starts_with("POST /shutdown HTTP/1.1\r\n"));
        assert!(request.contains("\r\nX-GuildBotics-Session-Token: session-token\r\n"));
    }

    #[test]
    fn shutdown_request_fails_unless_the_backend_accepts_it() {
        let (port, server) = serve_once("HTTP/1.1 401 Unauthorized\r\ncontent-length: 0\r\n\r\n");

        let error = request_backend_shutdown(port, "stale").unwrap_err();

        server.join().expect("server");
        assert!(error.to_string().contains("401"));
    }

    #[test]
    fn active_work_is_read_from_the_backend_status() {
        let (port, server) = serve_once(
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"has_active_work\":false}",
        );

        assert!(!backend_has_active_work(port, "session-token"));

        let request = server.join().expect("server");
        assert!(request.starts_with("GET /scheduler/status HTTP/1.1\r\n"));
        assert!(request.contains("\r\nX-GuildBotics-Session-Token: session-token\r\n"));
    }

    #[test]
    fn anything_but_a_clear_no_counts_as_active_work() {
        for response in [
            "HTTP/1.1 200 OK\r\n\r\n{\"has_active_work\":true}",
            "HTTP/1.1 200 OK\r\n\r\n{\"scheduler\":{}}",
            "HTTP/1.1 200 OK\r\n\r\nnot json",
            "HTTP/1.1 401 Unauthorized\r\n\r\n{\"has_active_work\":false}",
        ] {
            let (port, server) = serve_once(response);
            assert!(backend_has_active_work(port, "token"), "{response}");
            server.join().expect("server");
        }
        assert!(backend_has_active_work(pick_free_port(), "token"));
    }

    #[test]
    fn shutdown_request_fails_without_a_usable_teardown_budget() {
        for response in [
            "HTTP/1.1 202 Accepted\r\n\r\n",
            "HTTP/1.1 202 Accepted\r\n\r\n{\"teardown_budget_seconds\":-1}",
            "HTTP/1.1 202 Accepted\r\n\r\n{\"teardown_budget_seconds\":\"soon\"}",
        ] {
            let (port, server) = serve_once(response);
            assert!(
                request_backend_shutdown(port, "token").is_err(),
                "{response}"
            );
            server.join().expect("server");
        }
    }

    #[test]
    fn shutdown_request_fails_when_nothing_listens() {
        let port = pick_free_port();

        assert!(request_backend_shutdown(port, "token").is_err());
    }

    #[test]
    fn wait_for_exit_tells_an_exited_backend_from_a_running_one() {
        let (running, exited) = mpsc::channel::<()>();
        assert_eq!(
            wait_for_exit(&exited, Duration::from_millis(10))
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );

        drop(running);
        assert!(wait_for_exit(&exited, Duration::from_millis(10)).is_ok());
    }

    #[test]
    fn boot_log_is_bounded_to_one_mebibyte() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let path = temp_dir.path().join("bootstrap.log");
        append_boot_log(&path, &vec![b'a'; BOOT_LOG_MAX_BYTES - 1])?;
        append_boot_log(&path, b"last line")?;

        assert!(fs::metadata(&path)?.len() as usize <= BOOT_LOG_COMPACT_BYTES);
        assert!(fs::read_to_string(path)?.ends_with("last line\n"));
        Ok(())
    }

    #[test]
    fn boot_log_tail_returns_at_most_one_hundred_lines() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let path = temp_dir.path().join("bootstrap.log");
        let content = (0..120)
            .map(|index| format!("line-{index}"))
            .collect::<Vec<_>>()
            .join("\n");
        fs::write(&path, content)?;

        let tail = read_boot_log_tail(&path)?;

        assert_eq!(tail.lines().count(), BOOT_LOG_TAIL_LINES);
        assert!(tail.starts_with("line-20\n"));
        assert!(tail.ends_with("line-119"));
        Ok(())
    }

    #[test]
    fn install_skill_file_writes_skill_and_metadata() -> io::Result<()> {
        let temp_dir = TestDir::new()?;

        install_skill_file(temp_dir.path(), "first")?;

        assert_eq!(
            fs::read_to_string(temp_dir.path().join("SKILL.md"))?,
            "first"
        );
        assert_eq!(
            read_managed_skill_hash(&temp_dir.path().join(MANAGED_SKILL_METADATA)),
            Some(content_hash("first"))
        );
        Ok(())
    }

    #[test]
    fn install_skill_file_updates_unedited_managed_skill() -> io::Result<()> {
        let temp_dir = TestDir::new()?;

        install_skill_file(temp_dir.path(), "first")?;
        install_skill_file(temp_dir.path(), "second")?;

        assert_eq!(
            fs::read_to_string(temp_dir.path().join("SKILL.md"))?,
            "second"
        );
        Ok(())
    }

    #[test]
    fn install_skill_file_does_not_update_edited_managed_skill() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let skill_path = temp_dir.path().join("SKILL.md");

        install_skill_file(temp_dir.path(), "first")?;
        fs::write(&skill_path, "user edit")?;
        install_skill_file(temp_dir.path(), "second")?;

        assert_eq!(fs::read_to_string(skill_path)?, "user edit");
        Ok(())
    }

    #[test]
    fn install_skill_file_does_not_update_unmanaged_skill() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let skill_path = temp_dir.path().join("SKILL.md");
        fs::write(&skill_path, "user skill")?;

        install_skill_file(temp_dir.path(), "bundled")?;

        assert_eq!(fs::read_to_string(skill_path)?, "user skill");
        Ok(())
    }

    #[test]
    fn force_install_skill_file_overwrites_edited_skill() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let skill_path = temp_dir.path().join("SKILL.md");

        install_skill_file(temp_dir.path(), "first")?;
        fs::write(&skill_path, "user edit")?;
        force_install_skill_file(temp_dir.path(), "second")?;

        assert_eq!(fs::read_to_string(&skill_path)?, "second");
        assert_eq!(
            read_managed_skill_hash(&temp_dir.path().join(MANAGED_SKILL_METADATA)),
            Some(content_hash("second"))
        );
        Ok(())
    }

    #[test]
    fn codex_skill_dir_is_the_shared_user_skills_directory() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let home = temp_dir.path();

        assert_eq!(
            skill_dir(home, agent("codex"), &home.join(".codex")),
            home.join(".agents").join("skills").join("guildbotics")
        );
        Ok(())
    }

    #[test]
    fn other_agents_keep_skills_under_their_own_home() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let home = temp_dir.path();

        for name in ["claude", "grok", "antigravity", "copilot"] {
            let agent_home = home.join("custom-home");
            assert_eq!(
                skill_dir(home, agent(name), &agent_home),
                agent_home.join("skills").join("guildbotics"),
                "{name} must keep its skill under its own home"
            );
        }
        Ok(())
    }

    #[test]
    fn remove_managed_skill_deletes_only_unedited_managed_copies() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let managed = temp_dir.path().join("managed");
        let edited = temp_dir.path().join("edited");
        let unmanaged = temp_dir.path().join("unmanaged");

        install_skill_file(&managed, "bundled")?;
        install_skill_file(&edited, "bundled")?;
        fs::write(edited.join("SKILL.md"), "user edit")?;
        fs::create_dir_all(&unmanaged)?;
        fs::write(unmanaged.join("SKILL.md"), "user skill")?;

        remove_managed_skill(&managed)?;
        remove_managed_skill(&edited)?;
        remove_managed_skill(&unmanaged)?;
        remove_managed_skill(&temp_dir.path().join("absent"))?;

        assert!(!managed.exists());
        assert_eq!(fs::read_to_string(edited.join("SKILL.md"))?, "user edit");
        assert_eq!(
            fs::read_to_string(unmanaged.join("SKILL.md"))?,
            "user skill"
        );
        Ok(())
    }

    #[test]
    fn configured_agent_home_uses_env_or_existing_default_only() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let explicit = temp_dir.path().join("custom");
        let env_name = format!("GUILDBOTICS_TEST_HOME_{}", uuid::Uuid::new_v4());

        std::env::set_var(&env_name, &explicit);
        assert_eq!(
            configured_agent_home(temp_dir.path(), &env_name, ".missing"),
            Some(explicit)
        );

        std::env::remove_var(&env_name);
        assert_eq!(
            configured_agent_home(temp_dir.path(), &env_name, ".missing"),
            None
        );

        let default_path = temp_dir.path().join(".existing");
        fs::create_dir_all(&default_path)?;
        assert_eq!(
            configured_agent_home(temp_dir.path(), &env_name, ".existing"),
            Some(default_path)
        );
        Ok(())
    }

    fn write_build(dir: &Path, build_id: &str) -> io::Result<()> {
        if dir.exists() {
            fs::remove_dir_all(dir)?;
        }
        fs::create_dir_all(dir.join("_internal"))?;
        fs::write(dir.join(BUILD_ID_FILE), build_id)?;
        fs::write(dir.join(platform_executable_name("guildbotics")), build_id)?;
        fs::write(dir.join("_internal").join("data"), build_id)
    }

    /// What `bin` runs, and every entry left under `programs`.
    fn installed(root: &Path) -> io::Result<(String, Vec<String>)> {
        let running = fs::read_to_string(
            root.join("bin")
                .join(platform_executable_name("guildbotics")),
        )?;
        let mut programs = fs::read_dir(root.join("programs"))?
            .map(|entry| Ok(entry?.file_name().to_string_lossy().into_owned()))
            .collect::<io::Result<Vec<_>>>()?;
        programs.sort();
        Ok((running, programs))
    }

    #[test]
    fn install_programs_points_bin_at_each_new_build_and_drops_the_old_one_later() -> io::Result<()>
    {
        let temp_dir = TestDir::new()?;
        let source = temp_dir.path().join("resources");
        let root = temp_dir.path().join("home");

        write_build(&source, "build-1")?;
        install_programs(&source, &root)?;
        assert_eq!(
            installed(&root)?,
            ("build-1".into(), vec!["build-1".into()])
        );

        // The same build is left alone.
        let data = root
            .join("programs")
            .join("build-1")
            .join("_internal")
            .join("data");
        fs::write(&data, "untouched")?;
        install_programs(&source, &root)?;
        assert_eq!(fs::read_to_string(&data)?, "untouched");

        // What `bin` pointed at may just have been started through it, and
        // stays until a later launch.
        write_build(&source, "build-2")?;
        install_programs(&source, &root)?;
        assert_eq!(
            installed(&root)?,
            ("build-2".into(), vec!["build-1".into(), "build-2".into()])
        );
        install_programs(&source, &root)?;
        assert_eq!(
            installed(&root)?,
            ("build-2".into(), vec!["build-2".into()])
        );
        Ok(())
    }

    /// Hold the build's executable the way a program running from it does.
    #[cfg(unix)]
    fn run_from(build: &Path) -> io::Result<fs::File> {
        let executable = fs::File::open(build.join("guildbotics"))?;
        executable.lock_shared()?;
        Ok(executable)
    }

    #[cfg(windows)]
    fn run_from(build: &Path) -> io::Result<fs::File> {
        use std::os::windows::fs::OpenOptionsExt;

        const FILE_SHARE_READ: u32 = 1;
        fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .open(build.join("guildbotics.exe"))
    }

    #[test]
    fn install_programs_waits_while_a_program_runs_from_the_old_build() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let source = temp_dir.path().join("resources");
        let root = temp_dir.path().join("home");
        write_build(&source, "build-1")?;
        install_programs(&source, &root)?;

        let running = run_from(&root.join("programs").join("build-1"))?;
        write_build(&source, "build-2")?;
        install_programs(&source, &root)?;
        assert_eq!(
            installed(&root)?,
            ("build-1".into(), vec!["build-1".into(), "build-2".into()])
        );

        drop(running);
        install_programs(&source, &root)?;
        assert_eq!(
            installed(&root)?,
            ("build-2".into(), vec!["build-1".into(), "build-2".into()])
        );
        Ok(())
    }

    #[test]
    fn install_programs_removes_an_old_build_only_once_nothing_runs_from_it() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let source = temp_dir.path().join("resources");
        let root = temp_dir.path().join("home");
        write_build(&source, "build-1")?;
        install_programs(&source, &root)?;
        let old = root.join("programs").join("build-0");
        write_build(&old, "build-0")?;

        let running = run_from(&old)?;
        install_programs(&source, &root)?;
        assert_eq!(
            installed(&root)?,
            ("build-1".into(), vec!["build-0".into(), "build-1".into()])
        );

        drop(running);
        install_programs(&source, &root)?;
        assert_eq!(
            installed(&root)?,
            ("build-1".into(), vec!["build-1".into()])
        );
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn install_programs_keeps_the_old_link_when_the_new_one_cannot_be_made() -> io::Result<()> {
        use std::os::unix::fs::PermissionsExt;

        let temp_dir = TestDir::new()?;
        let source = temp_dir.path().join("resources");
        let root = temp_dir.path().join("home");
        write_build(&source, "build-1")?;
        install_programs(&source, &root)?;
        write_build(&source, "build-2")?;
        copy_dir(&source, &root.join("programs").join("build-2"))?;

        fs::set_permissions(&root, fs::Permissions::from_mode(0o555))?;
        let result = install_programs(&source, &root);
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755))?;

        assert!(result.is_err());
        assert_eq!(
            installed(&root)?,
            ("build-1".into(), vec!["build-1".into(), "build-2".into()])
        );
        Ok(())
    }

    #[cfg(unix)]
    fn link_to(target: &Path, link: &Path) -> io::Result<()> {
        std::os::unix::fs::symlink(target, link)
    }

    #[cfg(windows)]
    fn link_to(target: &Path, link: &Path) -> io::Result<()> {
        junction::create(target, link)
    }

    #[test]
    fn install_programs_recovers_from_any_interrupted_or_earlier_state() -> io::Result<()> {
        type Arrange = fn(&Path) -> io::Result<()>;
        let states: [(&str, Arrange); 6] = [
            ("staging left over", |root| {
                let staging = root.join("programs").join("build-2.staging");
                fs::create_dir_all(&staging)?;
                fs::write(staging.join("partial"), "")
            }),
            ("new link left pending", |root| {
                link_to(
                    &root.join("programs").join("build-1"),
                    &root.join("bin.pending"),
                )
            }),
            ("old link stepped aside", |root| {
                fs::rename(root.join("bin"), root.join("bin.previous"))
            }),
            ("bin left dangling", |root| {
                fs::remove_dir_all(root.join("programs").join("build-1"))
            }),
            ("stale entry left over", |root| {
                fs::create_dir_all(root.join("programs").join("build-0.gc"))
            }),
            ("bin is a directory of an earlier layout", |root| {
                fs::remove_dir_all(root.join("bin"))?;
                fs::create_dir_all(root.join("bin"))?;
                fs::write(root.join("bin").join("guildbotics"), "one-file")
            }),
        ];
        for (state, arrange) in states {
            let temp_dir = TestDir::new()?;
            let source = temp_dir.path().join("resources");
            let root = temp_dir.path().join("home");
            write_build(&source, "build-1")?;
            install_programs(&source, &root)?;
            write_build(&source, "build-2")?;
            arrange(&root)?;

            install_programs(&source, &root)?;
            install_programs(&source, &root)?;

            assert_eq!(
                installed(&root)?,
                ("build-2".into(), vec!["build-2".into()]),
                "{state}"
            );
            assert!(!root.join("bin").join("partial").exists(), "{state}");
        }
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn install_programs_keeps_symbolic_links() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let source = temp_dir.path().join("resources");
        let root = temp_dir.path().join("home");
        write_build(&source, "build-1")?;
        std::os::unix::fs::symlink("_internal/data", source.join("link"))?;

        install_programs(&source, &root)?;

        assert_eq!(
            fs::read_link(root.join("bin").join("link"))?,
            PathBuf::from("_internal/data")
        );
        Ok(())
    }

    #[test]
    fn windows_cli_names_include_executable_suffix_and_skip_shell_shim() {
        assert_eq!(
            executable_name_for("guildbotics", "windows"),
            "guildbotics.exe"
        );
        assert!(!should_install_shell_shim("windows"));
        assert!(should_install_shell_shim("macos"));
    }

    #[test]
    fn home_resolution_accepts_windows_profile_paths_without_home_env() {
        let profile = PathBuf::from(r"C:\Users\Aiko Example");
        assert_eq!(resolve_home_dir(Some(profile.clone())).unwrap(), profile);
        assert_eq!(
            resolve_home_dir(None).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }
}

/// Preferred initial window height (logical px) chosen so the Service and
/// Diagnostics screens fit without a vertical scrollbar when the display has the
/// room for it.
const PREFERRED_WINDOW_HEIGHT: f64 = 1040.0;

/// Grow the main window toward `PREFERRED_WINDOW_HEIGHT` when the monitor's work
/// area can accommodate it, then place it inside the work area. The window is
/// only ever made taller (never shorter than the configured default) and the
/// width is left untouched.
///
/// Positioning is done explicitly instead of `WebviewWindow::center` because
/// `center` centers against the full monitor resolution, not the work area, so
/// on a display with a dock/menu bar the window drifts low enough for its bottom
/// edge to slip under the dock — which clips the content and brings back a
/// vertical scrollbar. Centering within the work area (clamped to its top-left
/// when the window is larger than the available space) keeps the whole window on
/// screen.
fn fit_main_window(window: &tauri::WebviewWindow) {
    let Ok(Some(monitor)) = window.current_monitor() else {
        return;
    };
    let scale = monitor.scale_factor();
    if scale <= 0.0 {
        return;
    }
    let work_area = monitor.work_area();
    let work_x = work_area.position.x as f64 / scale;
    let work_y = work_area.position.y as f64 / scale;
    let work_width = work_area.size.width as f64 / scale;
    let work_height = work_area.size.height as f64 / scale;

    let (Ok(inner), Ok(outer)) = (window.inner_size(), window.outer_size()) else {
        return;
    };
    let inner_width = inner.width as f64 / scale;
    let inner_height = inner.height as f64 / scale;
    let outer_width = outer.width as f64 / scale;
    // The chrome (title bar / borders) is the difference between the outer and
    // inner size; the inner content must fit within the work area minus chrome.
    let chrome_height = (outer.height as f64 / scale - inner_height).max(0.0);
    let max_inner_height = (work_height - chrome_height).max(0.0);
    let target_inner_height = PREFERRED_WINDOW_HEIGHT.min(max_inner_height);

    let final_inner_height = if target_inner_height > inner_height {
        let _ = window.set_size(LogicalSize::new(inner_width, target_inner_height));
        target_inner_height
    } else {
        inner_height
    };

    // Centre within the work area using the resulting outer size, clamping so a
    // window taller/wider than the work area sits at its top-left corner.
    let final_outer_height = final_inner_height + chrome_height;
    let pos_x = work_x + ((work_width - outer_width) / 2.0).max(0.0);
    let pos_y = work_y + ((work_height - final_outer_height) / 2.0).max(0.0);
    let _ = window.set_position(LogicalPosition::new(pos_x, pos_y));
}

pub fn run() {
    let token = uuid::Uuid::new_v4().to_string();
    let port = pick_free_port();

    tauri::Builder::default()
        // `tray::build` installs the app menu: the default one quits through
        // `terminate:`, which no quit guard gets to see.
        .enable_macos_default_menu(false)
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(hotkeys::plugin())
        .invoke_handler(tauri::generate_handler![
            backend_info,
            bootstrap_log,
            cli_agent_skill_statuses,
            force_update_cli_agent_skill,
            hotkeys::sync_hotkeys,
            hotkeys::suspend_hotkeys,
            hotkeys::resume_hotkeys,
            hotkeys::hide_quick_window,
            hotkeys::clipboard_watch_supported,
            hotkeys::poll_clipboard,
            hotkeys::show_main_window,
            hotkeys::open_main_window,
            tray::set_tray_labels,
            tray::quit_app,
        ])
        .setup(move |app| {
            app.manage(hotkeys::HotkeyState::default());
            hotkeys::init(app.handle());
            app.manage(tray::TrayState::default());
            tray::build(app.handle())?;

            let programs = app.path().resource_dir()?.join(PROGRAMS_DIR);
            if let Err(error) = install_cli_agent_assets(&programs) {
                eprintln!("failed to install GuildBotics AI CLI tool assets: {error}");
            }

            if let Some(window) = app.get_webview_window("main") {
                fit_main_window(&window);
            }

            let port_arg = port.to_string();
            let boot_log_path = app.path().app_log_dir()?.join("bootstrap.log");
            let _ = append_boot_log(&boot_log_path, b"--- GuildBotics backend start ---");
            let command = app
                .shell()
                .command(programs.join(platform_executable_name("guildbotics-app-api")))
                // Hand the sidecar our PID so it can exit on its own if this app
                // ever dies without a clean teardown.
                .env(
                    "GUILDBOTICS_APP_API_PARENT_PID",
                    std::process::id().to_string(),
                )
                // Handed over through the environment, never argv: on a
                // shared host `ps` exposes another user's command line.
                .env("GUILDBOTICS_APP_API_TOKEN", &token);
            // In `tauri dev` the webview is served from the Vite dev server
            // (`build.devUrl` in tauri.conf.json), so API requests carry that
            // origin instead of tauri://localhost.
            #[cfg(debug_assertions)]
            let command = command.env(
                "GUILDBOTICS_APP_API_ALLOWED_ORIGINS",
                "http://127.0.0.1:1420",
            );
            let spawn_result = command
                .args(["--host", "127.0.0.1", "--port", &port_arg])
                .spawn();
            let sidecar = match spawn_result {
                Ok((mut rx, child)) => {
                    // Keep the child's stdout/stderr pipe drained so it never blocks.
                    let event_log_path = boot_log_path.clone();
                    let (running, exited) = mpsc::channel::<()>();
                    tauri::async_runtime::spawn(async move {
                        // Dropped when this task ends, which is how
                        // `stop_backend` sees the process exit.
                        let _running = running;
                        while let Some(event) = rx.recv().await {
                            match event {
                                CommandEvent::Stderr(bytes) => {
                                    let _ = append_boot_log(&event_log_path, &bytes);
                                }
                                CommandEvent::Error(error) => {
                                    let _ = append_boot_log(
                                        &event_log_path,
                                        format!("sidecar error: {error}").as_bytes(),
                                    );
                                }
                                CommandEvent::Terminated(status) => {
                                    let _ = append_boot_log(
                                        &event_log_path,
                                        format!("sidecar terminated: {status:?}").as_bytes(),
                                    );
                                    break;
                                }
                                _ => {}
                            }
                        }
                    });
                    Some(Sidecar { child, exited })
                }
                Err(error) => {
                    let _ = append_boot_log(
                        &boot_log_path,
                        format!("sidecar spawn failed: {error}").as_bytes(),
                    );
                    None
                }
            };

            app.manage(BackendState {
                token,
                port,
                boot_log_path,
                sidecar: Mutex::new(sidecar),
            });
            Ok(())
        })
        .on_window_event(tray::on_window_event)
        .build(tauri::generate_context!())
        .expect("error while building GuildBotics desktop application")
        .run(|app_handle, event| {
            // Closing the window only hides it, so the dock icon stays; clicking
            // it must bring the window back rather than do nothing.
            #[cfg(target_os = "macos")]
            if let RunEvent::Reopen { .. } = event {
                hotkeys::show_main_window(app_handle.clone());
            }
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendState>() {
                    stop_backend(&state);
                }
            }
        });
}
