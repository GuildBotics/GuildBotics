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

const CLI_AGENT_HOMES: [(&str, &str, &str); 4] = [
    ("codex", "CODEX_HOME", ".codex"),
    ("claude", "CLAUDE_HOME", ".claude"),
    ("gemini", "GEMINI_HOME", ".gemini"),
    ("copilot", "COPILOT_HOME", ".copilot"),
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
fn cli_agent_skill_statuses() -> serde_json::Value {
    match home_dir() {
        Ok(home) => serde_json::json!({
            "agents": CLI_AGENT_HOMES
                .iter()
                .map(|(agent, env_name, default_dir)| cli_agent_skill_status(&home, agent, env_name, default_dir))
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
    let Some((agent_name, env_name, default_dir)) = CLI_AGENT_HOMES
        .iter()
        .find(|(candidate, _, _)| *candidate == agent)
    else {
        return Err(format!("unsupported CLI agent: {agent}"));
    };
    let Some(agent_home) = configured_agent_home(&home, env_name, default_dir) else {
        return Err(format!("skill home for {agent_name} was not detected"));
    };

    force_install_skill_file(&agent_home, GUILDBOTICS_SKILL).map_err(|error| error.to_string())?;
    Ok(cli_agent_skill_status(
        &home,
        agent_name,
        env_name,
        default_dir,
    ))
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

fn should_write_managed_skill(skill_path: &Path, metadata_path: &Path) -> io::Result<bool> {
    if !skill_path.exists() {
        return Ok(true);
    }

    let Some(previous_hash) = read_managed_skill_hash(metadata_path) else {
        return Ok(false);
    };

    let current_content = fs::read_to_string(skill_path)?;
    Ok(content_hash(&current_content) == previous_hash)
}

fn install_skill_file(root: &Path, skill_content: &str) -> io::Result<()> {
    let skill_dir = root.join("skills").join("guildbotics");
    let skill_path = skill_dir.join("SKILL.md");
    let metadata_path = skill_dir.join(MANAGED_SKILL_METADATA);

    if !should_write_managed_skill(&skill_path, &metadata_path)? {
        return Ok(());
    }

    write_managed_skill(&skill_dir, &skill_path, &metadata_path, skill_content)
}

fn force_install_skill_file(root: &Path, skill_content: &str) -> io::Result<()> {
    let skill_dir = root.join("skills").join("guildbotics");
    let skill_path = skill_dir.join("SKILL.md");
    let metadata_path = skill_dir.join(MANAGED_SKILL_METADATA);

    write_managed_skill(&skill_dir, &skill_path, &metadata_path, skill_content)
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

fn cli_agent_skill_status(
    home: &Path,
    agent: &str,
    env_name: &str,
    default_dir: &str,
) -> serde_json::Value {
    let Some(agent_home) = configured_agent_home(home, env_name, default_dir) else {
        return serde_json::json!({
            "agent": agent,
            "agent_home": null,
            "skill_path": null,
            "status": "agent_home_missing",
            "can_force_update": false,
        });
    };

    let skill_path = agent_home
        .join("skills")
        .join("guildbotics")
        .join("SKILL.md");
    let metadata_path = skill_path
        .parent()
        .expect("skill path has a parent")
        .join(MANAGED_SKILL_METADATA);

    if !skill_path.exists() {
        return serde_json::json!({
            "agent": agent,
            "agent_home": agent_home,
            "skill_path": skill_path,
            "status": "missing",
            "can_force_update": true,
        });
    }

    let Some(previous_hash) = read_managed_skill_hash(&metadata_path) else {
        return serde_json::json!({
            "agent": agent,
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
                "agent": agent,
                "agent_home": agent_home,
                "skill_path": skill_path,
                "status": status,
                "can_force_update": status != "up_to_date",
            })
        }
        Err(error) => serde_json::json!({
            "agent": agent,
            "agent_home": agent_home,
            "skill_path": skill_path,
            "status": "error",
            "can_force_update": false,
            "error": error.to_string(),
        }),
    }
}

fn install_cli_agent_assets() -> io::Result<()> {
    let home = home_dir()?;
    install_member_cli(&home)?;

    for (_, env_name, default_dir) in CLI_AGENT_HOMES {
        if let Some(agent_home) = configured_agent_home(&home, env_name, default_dir) {
            install_skill_file(&agent_home, GUILDBOTICS_SKILL)?;
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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

    fn installed_skill_path(root: &Path) -> PathBuf {
        root.join("skills").join("guildbotics").join("SKILL.md")
    }

    #[test]
    fn install_skill_file_writes_skill_and_metadata() -> io::Result<()> {
        let temp_dir = TestDir::new()?;

        install_skill_file(temp_dir.path(), "first")?;

        let skill_path = installed_skill_path(temp_dir.path());
        let metadata_path = skill_path
            .parent()
            .expect("skill directory")
            .join(MANAGED_SKILL_METADATA);
        assert_eq!(fs::read_to_string(skill_path)?, "first");
        assert_eq!(
            read_managed_skill_hash(&metadata_path),
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
            fs::read_to_string(installed_skill_path(temp_dir.path()))?,
            "second"
        );
        Ok(())
    }

    #[test]
    fn install_skill_file_does_not_update_edited_managed_skill() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let skill_path = installed_skill_path(temp_dir.path());

        install_skill_file(temp_dir.path(), "first")?;
        fs::write(&skill_path, "user edit")?;
        install_skill_file(temp_dir.path(), "second")?;

        assert_eq!(fs::read_to_string(skill_path)?, "user edit");
        Ok(())
    }

    #[test]
    fn install_skill_file_does_not_update_unmanaged_skill() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let skill_path = installed_skill_path(temp_dir.path());
        fs::create_dir_all(skill_path.parent().expect("skill directory"))?;
        fs::write(&skill_path, "user skill")?;

        install_skill_file(temp_dir.path(), "bundled")?;

        assert_eq!(fs::read_to_string(skill_path)?, "user skill");
        Ok(())
    }

    #[test]
    fn force_install_skill_file_overwrites_edited_skill() -> io::Result<()> {
        let temp_dir = TestDir::new()?;
        let skill_path = installed_skill_path(temp_dir.path());

        install_skill_file(temp_dir.path(), "first")?;
        fs::write(&skill_path, "user edit")?;
        force_install_skill_file(temp_dir.path(), "second")?;

        assert_eq!(fs::read_to_string(&skill_path)?, "second");
        assert_eq!(
            read_managed_skill_hash(
                &skill_path
                    .parent()
                    .expect("skill directory")
                    .join(MANAGED_SKILL_METADATA)
            ),
            Some(content_hash("second"))
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
}

pub fn run() {
    let token = uuid::Uuid::new_v4().to_string();
    let port = pick_free_port();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            backend_info,
            cli_agent_skill_statuses,
            force_update_cli_agent_skill,
        ])
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
