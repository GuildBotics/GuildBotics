//! Exercise real installer test entry points with hostile parent Git paths.
use std::{fs, process::Command};

pub fn preserves_parent_config(test: &str, global: bool) {
    let temp = tempfile::tempdir().unwrap();
    let global_path = temp.path().join("parent.gitconfig");
    let xdg = temp.path().join("xdg");
    let xdg_path = xdg.join("git/config");
    fs::create_dir_all(xdg_path.parent().unwrap()).unwrap();
    let sentinel = "[credential \"http://127.0.0.1:7878\"]\n\thelper = parent-fixture\n";
    fs::write(&global_path, sentinel).unwrap();
    fs::write(&xdg_path, sentinel).unwrap();
    let mut child = Command::new(std::env::current_exe().unwrap());
    child
        .args(["--exact", test])
        .env("XDG_CONFIG_HOME", &xdg)
        .env("GUILDBOTICS_TEST_INSTALL_CHILD", "1");
    if global {
        child.env("GIT_CONFIG_GLOBAL", &global_path);
    } else {
        child.env_remove("GIT_CONFIG_GLOBAL");
    }
    let result = child.output().unwrap();
    assert_eq!(
        fs::read_to_string(&global_path).unwrap(),
        sentinel,
        "parent global config changed by {test}"
    );
    assert_eq!(
        fs::read_to_string(&xdg_path).unwrap(),
        sentinel,
        "parent XDG config changed by {test}"
    );
    assert!(result.status.success(), "{result:?}");
    assert!(
        String::from_utf8_lossy(&result.stdout).contains("1 passed"),
        "child test did not execute: {result:?}"
    );
}

#[cfg(unix)]
#[test]
fn install_helper_ignores_parent_global_config() {
    preserves_parent_config(
        "install_cli::helper_uses_this_machines_managed_cli_and_is_idempotent",
        true,
    );
}
#[cfg(unix)]
#[test]
fn install_helper_ignores_parent_xdg_config() {
    preserves_parent_config(
        "install_cli::helper_uses_this_machines_managed_cli_and_is_idempotent",
        false,
    );
}
#[cfg(all(unix, not(feature = "hub")))]
#[test]
fn relocated_install_ignores_parent_global_config() {
    preserves_parent_config(
        "client_only::relocated_client_installs_embedded_skills_and_has_only_client_commands",
        true,
    );
}
#[cfg(all(unix, not(feature = "hub")))]
#[test]
fn relocated_install_ignores_parent_xdg_config() {
    preserves_parent_config(
        "client_only::relocated_client_installs_embedded_skills_and_has_only_client_commands",
        false,
    );
}
