use std::{path::Path, process::Stdio};
pub fn base_command_sync(global_config: &Path) -> std::process::Command {
    let mut cmd = std::process::Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(&key);
        }
    }
    cmd.env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", global_config)
        .env("GIT_ATTR_NOSYSTEM", "1")
        .env("XDG_CONFIG_HOME", global_config)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("LC_ALL", "C")
        .stdin(Stdio::null());
    cmd
}

pub fn base_command(path: &Path) -> tokio::process::Command {
    let mut cmd = tokio::process::Command::from(base_command_sync(path));
    cmd.kill_on_drop(true);
    #[cfg(unix)]
    cmd.process_group(0);
    #[cfg(windows)]
    cmd.creation_flags(0x0000_0200);
    cmd
}
