use std::fs;
use std::io;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

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
    child: Mutex<Option<CommandChild>>,
}

const GUILDBOTICS_SKILL: &str = include_str!("../../../skills/guildbotics/SKILL.md");

#[tauri::command]
fn backend_info(state: tauri::State<'_, BackendState>) -> serde_json::Value {
    serde_json::json!({
        "port": state.port,
        "token": state.token,
    })
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
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "HOME is not set"))
}

fn desktop_target_triple() -> &'static str {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "aarch64-apple-darwin",
        ("macos", "x86_64") => "x86_64-apple-darwin",
        _ => "aarch64-apple-darwin",
    }
}

fn bundled_cli_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(dir) = current_exe.parent() {
            candidates.push(dir.join("guildbotics-cli"));
            candidates.push(dir.join(format!("guildbotics-cli-{}", desktop_target_triple())));
        }
    }
    candidates.push(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join(format!("guildbotics-cli-{}", desktop_target_triple())),
    );
    candidates
}

fn find_bundled_cli() -> io::Result<PathBuf> {
    bundled_cli_candidates()
        .into_iter()
        .find(|path| path.is_file())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "bundled guildbotics-cli binary was not found",
            )
        })
}

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

fn install_member_cli(home: &Path) -> io::Result<()> {
    let source = find_bundled_cli()?;
    let managed_dir = home.join(".guildbotics").join("bin");
    let managed_cli = managed_dir.join("guildbotics");
    fs::create_dir_all(&managed_dir)?;
    fs::copy(source, &managed_cli)?;
    make_executable(&managed_cli)?;

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
    Ok(())
}

fn should_write_managed_shim(path: &Path) -> bool {
    if !path.exists() {
        return true;
    }
    fs::read_to_string(path)
        .map(|content| content.contains("Managed by GuildBotics desktop."))
        .unwrap_or(false)
}

fn install_skill_file(root: PathBuf) -> io::Result<()> {
    let skill_dir = root.join("skills").join("guildbotics");
    fs::create_dir_all(&skill_dir)?;
    fs::write(skill_dir.join("SKILL.md"), GUILDBOTICS_SKILL)
}

fn install_cli_agent_assets() -> io::Result<()> {
    let home = home_dir()?;
    install_member_cli(&home)?;

    let codex_home = std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".codex"));
    install_skill_file(codex_home)?;

    let claude_home = std::env::var_os("CLAUDE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".claude"));
    install_skill_file(claude_home)?;

    Ok(())
}

pub fn run() {
    let token = uuid::Uuid::new_v4().to_string();
    let port = pick_free_port();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![backend_info])
        .setup(move |app| {
            if let Err(error) = install_cli_agent_assets() {
                eprintln!("failed to install GuildBotics CLI agent assets: {error}");
            }

            let port_arg = port.to_string();
            let (mut rx, child) = app
                .shell()
                .sidecar("guildbotics-app-api")?
                // The sidecar is a PyInstaller one-file binary whose worker can
                // outlive a killed bootloader. Hand it our PID so it can exit on
                // its own if this app ever dies without a clean teardown.
                .env(
                    "GUILDBOTICS_APP_API_PARENT_PID",
                    std::process::id().to_string(),
                )
                .args([
                    "--host",
                    "127.0.0.1",
                    "--port",
                    &port_arg,
                    "--token",
                    &token,
                ])
                .spawn()?;

            // Keep the child's stdout/stderr pipe drained so it never blocks.
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Terminated(_) = event {
                        break;
                    }
                }
            });

            app.manage(BackendState {
                token,
                port,
                child: Mutex::new(Some(child)),
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building GuildBotics desktop application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendState>() {
                    // Tolerate a poisoned mutex so the app can still exit cleanly;
                    // recover the guard and kill the child if one is present.
                    let mut guard = match state.child.lock() {
                        Ok(guard) => guard,
                        Err(poisoned) => poisoned.into_inner(),
                    };
                    if let Some(child) = guard.take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
