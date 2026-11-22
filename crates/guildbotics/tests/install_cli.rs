#![cfg(unix)]

use std::{fs, path::Path, process::Command};

const BIN: &str = env!("CARGO_BIN_EXE_guildbotics");
const SKILL: &[u8] = include_bytes!("../../../skills/guildbotics/SKILL.md");

fn install(home: &Path) -> String {
    install_at(home, &home.join(".guildbotics"), 7878)
}

fn install_at(home: &Path, machine: &Path, port: u16) -> String {
    let result = Command::new(BIN)
        .args(["install"])
        .env("HOME", home)
        .env("GIT_CONFIG_GLOBAL", home.join(".gitconfig"))
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env_remove("XDG_CONFIG_HOME")
        .env("GUILDBOTICS_HOME", machine)
        .env("GUILDBOTICS_SERVER_PORT", port.to_string())
        .env_remove("CODEX_HOME")
        .env_remove("CLAUDE_CONFIG_DIR")
        .env_remove("GROK_HOME")
        .env("LANGUAGE", "en")
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    String::from_utf8(result.stdout).unwrap()
}

#[cfg(all(unix, feature = "hub"))]
#[test]
fn installation_replaces_managed_cli_paths_without_touching_the_old_directory() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path();
    let programs = home.join(".guildbotics/programs");
    fs::create_dir_all(&programs).unwrap();
    fs::write(programs.join("guildbotics"), "old").unwrap();
    let bin = home.join(".guildbotics/bin");
    std::os::unix::fs::symlink(&programs, &bin).unwrap();
    let on_path = home.join(".local/bin/guildbotics");
    fs::create_dir_all(on_path.parent().unwrap()).unwrap();
    fs::write(
        &on_path,
        "#!/bin/sh\n# Managed by GuildBotics desktop.\nexec old\n",
    )
    .unwrap();
    install(home);
    assert!(!bin.is_symlink());
    assert!(bin.join("guildbotics").is_file());
    assert_eq!(fs::read_link(&on_path).unwrap(), bin.join("guildbotics"));
    assert_eq!(
        fs::read_to_string(programs.join("guildbotics")).unwrap(),
        "old"
    );
    assert_eq!(
        on_path.canonicalize().unwrap(),
        bin.canonicalize().unwrap().join("guildbotics")
    );
    for cli in [bin.join("guildbotics"), on_path] {
        let result = Command::new(cli)
            .args(["workspace", "status", "--format", "json"])
            .env("HOME", home)
            .env("GIT_CONFIG_GLOBAL", home.join(".gitconfig"))
            .env_remove("XDG_CONFIG_HOME")
            .env(gb_base::home::USER_HOME_ENV, home)
            .env("GUILDBOTICS_HOME", home.join(".guildbotics"))
            .output()
            .unwrap();
        assert!(result.status.success());
        let state: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(state["configured"], false);
    }
}

#[cfg(feature = "hub")]
#[test]
fn managed_cli_infers_its_machine_root_through_both_entry_links() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().canonicalize().unwrap();
    let machine = home.join("extra disk /machine");
    let fixed = home.join(".guildbotics/bin/guildbotics");
    let on_path = home.join(".local/bin/guildbotics");
    let managed = machine.join("bin/guildbotics");
    install_at(&home, &machine, 7878);
    assert_eq!(fs::read_link(&fixed).unwrap(), managed);
    assert_eq!(fs::read_link(&on_path).unwrap(), fixed);

    let state_path = |cli: &Path, explicit: Option<&Path>| {
        let mut command = Command::new(cli);
        command
            .args(["workspace", "status", "--format", "json"])
            .env("HOME", &home)
            .env("LANGUAGE", "en")
            .env_remove("GUILDBOTICS_HOME");
        if let Some(root) = explicit {
            command.env("GUILDBOTICS_HOME", root);
        }
        let output = command.output().unwrap();
        assert!(output.status.success(), "{output:?}");
        let state: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        state["state_file"].as_str().unwrap().to_owned()
    };
    let expected = |root: &Path| {
        root.join("data/active-workspace.json")
            .to_str()
            .unwrap()
            .to_owned()
    };
    for cli in [&managed, &fixed, &on_path] {
        assert_eq!(state_path(cli, None), expected(&machine));
    }
    let explicit = home.join("override");
    assert_eq!(state_path(&on_path, Some(&explicit)), expected(&explicit));

    let unmanaged = home.join("usr/local/bin/guildbotics");
    fs::create_dir_all(unmanaged.parent().unwrap()).unwrap();
    fs::copy(&managed, &unmanaged).unwrap();
    assert_eq!(
        state_path(&unmanaged, None),
        expected(&home.join(".guildbotics"))
    );
}

#[test]
fn custom_install_keeps_the_existing_fixed_entry_and_path_link() {
    use std::os::unix::fs::symlink;

    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().canonicalize().unwrap();
    let fixed = home.join(".guildbotics/bin/guildbotics");
    let on_path = home.join(".local/bin/guildbotics");
    let machine = home.join("development");
    fs::create_dir_all(fixed.parent().unwrap()).unwrap();
    fs::write(&fixed, "#!/bin/sh\nexec production\n").unwrap();
    install_at(&home, &machine, 7878);
    assert_eq!(
        fs::read_to_string(&fixed).unwrap(),
        "#!/bin/sh\nexec production\n"
    );
    assert_eq!(fs::read_link(&on_path).unwrap(), fixed);

    fs::remove_file(&fixed).unwrap();
    let other = home.join("other/bin/guildbotics");
    // The other machine root can be on a disk that is not mounted yet.
    symlink(&other, &fixed).unwrap();
    install_at(&home, &machine, 7878);
    assert_eq!(fs::read_link(&fixed).unwrap(), other);
    assert_eq!(fs::read_link(&on_path).unwrap(), fixed);
    assert!(machine.join("bin/guildbotics").is_file());
}

#[cfg(target_os = "linux")]
#[test]
fn git_invokes_the_installed_helper_without_a_home_override() {
    use std::io::Write;
    use std::process::Stdio;

    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let machine = home.join("extra disk/machine");
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    install_at(home, &machine, port);
    let fill = |explicit_root: Option<&Path>| {
        let mut command = Command::new("git");
        command
            .args(["credential", "fill"])
            .current_dir(home)
            .env("GIT_CONFIG_GLOBAL", home.join(".gitconfig"))
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env_remove("HOME")
            .env_remove("GUILDBOTICS_HOME")
            .env("GUILDBOTICS_SERVER_PORT", port.to_string())
            .env("LANGUAGE", "en")
            .env("GIT_TERMINAL_PROMPT", "0")
            // Keep the probe independent of any session keychain.
            .env(
                "DBUS_SESSION_BUS_ADDRESS",
                format!("unix:path={}", home.join("no-keychain.sock").display()),
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(root) = explicit_root {
            command.env("GUILDBOTICS_HOME", root);
        }
        let mut git = command.spawn().unwrap();
        write!(
            git.stdin.take().unwrap(),
            "protocol=http\nhost=127.0.0.1:{port}\n\n"
        )
        .unwrap();
        git.wait_with_output().unwrap()
    };

    // The fixture has no credentials. A backend can report either an
    // unavailable keychain or a missing entry. Both invocations should reach
    // the same credential lookup after resolving the root.
    let explicit = fill(Some(&machine));
    let inferred = fill(None);
    assert!(!explicit.status.success());
    assert_eq!(inferred.status, explicit.status);
    assert!(explicit.stdout.is_empty());
    assert!(inferred.stdout.is_empty());
    let expected = String::from_utf8(explicit.stderr).unwrap();
    let unreachable = gb_server::i18n::t("en", "cli.server.unreachable", &[]);
    assert!(
        expected.contains("OS keychain") || expected.contains(&unreachable),
        "{expected}"
    );
    assert_eq!(String::from_utf8(inferred.stderr).unwrap(), expected);
}

#[cfg(unix)]
#[test]
fn installation_preserves_user_commands_and_edited_skills() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path();
    let on_path = home.join(".local/bin/guildbotics");
    fs::create_dir_all(on_path.parent().unwrap()).unwrap();
    fs::write(&on_path, "#!/bin/sh\nexec mine\n").unwrap();
    for path in [home.join(".codex"), home.join(".claude")] {
        fs::create_dir_all(path).unwrap();
    }
    let grok = home.join(".grok/skills/other/SKILL.md");
    fs::create_dir_all(grok.parent().unwrap()).unwrap();
    fs::write(&grok, "user-owned").unwrap();
    install(home);
    let paths = [
        home.join(".agents/skills/guildbotics/SKILL.md"),
        home.join(".claude/skills/guildbotics/SKILL.md"),
        home.join(".grok/skills/guildbotics/SKILL.md"),
    ];
    for path in &paths {
        assert_eq!(fs::read(path).unwrap(), SKILL);
    }
    fs::write(&paths[1], [0xff]).unwrap();
    // FNV-1a 64 for a managed skill containing just "a".
    fs::write(&paths[2], b"a").unwrap();
    fs::write(
        paths[2].with_file_name(".guildbotics-managed.json"),
        r#"{"content_hash":"af63dc4c8601ec8c","manager":"GuildBotics","skill":"guildbotics"}"#,
    )
    .unwrap();
    install(home);
    assert_eq!(fs::read(&paths[1]).unwrap(), [0xff]);
    assert_eq!(fs::read(&paths[2]).unwrap(), SKILL);
    assert_eq!(fs::read_to_string(grok).unwrap(), "user-owned");
    assert_eq!(
        fs::read_to_string(on_path).unwrap(),
        "#!/bin/sh\nexec mine\n"
    );
}

#[test]
fn helper_uses_this_machines_managed_cli_and_is_idempotent() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("a home 'quoted'");
    fs::create_dir_all(&home).unwrap();
    for _ in 0..2 {
        install(&home);
    }
    let result = Command::new("git")
        .args([
            "config",
            "--global",
            "--get-all",
            "credential.http://127.0.0.1:7878.helper",
        ])
        .env("HOME", &home)
        .env("GIT_CONFIG_GLOBAL", home.join(".gitconfig"))
        .env_remove("XDG_CONFIG_HOME")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()
        .unwrap();
    assert!(result.status.success());
    let binary = home.join(if cfg!(windows) {
        ".guildbotics/bin/guildbotics.exe"
    } else {
        ".guildbotics/bin/guildbotics"
    });
    let binary = binary.canonicalize().unwrap();
    #[cfg(windows)]
    let binary = std::path::PathBuf::from(
        binary
            .to_string_lossy()
            .trim_start_matches(r"\\?\")
            .replace('\\', "/"),
    );
    let expected = format!(
        "\n!{}\n",
        gb_base::shell::managed_cli_command(&binary, &["git-credential"])
    );
    assert_eq!(String::from_utf8(result.stdout).unwrap(), expected);
}

#[cfg(not(feature = "hub"))]
#[test]
fn cleanup_deletes_only_the_list_after_confirmation() {
    let temp = tempfile::tempdir().unwrap();
    let machine = temp.path().join("machine");
    let workspace = temp.path().join("workspace");
    let identity = machine.join("identity");
    for path in [
        workspace.join(".guildbotics/config"),
        identity.join("config"),
        machine.join("data"),
        machine.join("bin"),
    ] {
        fs::create_dir_all(path).unwrap();
    }
    fs::write(
        machine.join("data/workspaces.json"),
        serde_json::json!([workspace]).to_string(),
    )
    .unwrap();
    fs::write(workspace.join("notes.txt"), "mine").unwrap();
    fs::write(machine.join("data/device.json"), "keep").unwrap();
    fs::write(machine.join("bin/guildbotics"), "keep").unwrap();
    fs::create_dir_all(identity.join(".guildbotics/local")).unwrap();
    fs::create_dir_all(identity.join(".guildbotics/config")).unwrap();
    fs::write(
        identity.join(".guildbotics/local/secrets.json"),
        r#"{"store_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
    )
    .unwrap();
    fs::write(
        identity.join(".guildbotics/config/secrets.yml"),
        "keys: [EXAMPLE_TOKEN]\n",
    )
    .unwrap();
    let plan = gb_server::client::install::Cleanup::discover(&machine, &identity, "en").unwrap();
    assert_eq!(plan.directories.len(), 2);
    plan.apply(false, |_, _| panic!("deleting before confirmation"))
        .unwrap();
    assert!(workspace.join(".guildbotics").is_dir());
    assert!(identity.is_dir());
    assert!(
        plan.apply(true, |_, _| anyhow::bail!("fixture locked keychain"))
            .is_err()
    );
    assert!(workspace.join(".guildbotics").is_dir());
    assert!(identity.is_dir());
    let mut removed = vec![];
    plan.apply(true, |service, account| {
        removed.push((service.to_owned(), account.to_owned()));
        Ok(())
    })
    .unwrap();
    assert_eq!(
        removed,
        vec![
            (
                "GuildBotics/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
                "EXAMPLE_TOKEN".into()
            ),
            ("GuildBotics".into(), "agent-environment-credentials".into())
        ]
    );
    assert!(!workspace.join(".guildbotics").exists());
    assert!(!identity.exists());
    for path in [
        workspace.join("notes.txt"),
        machine.join("data/device.json"),
        machine.join("bin/guildbotics"),
    ] {
        assert!(path.is_file());
    }
}

#[cfg(not(feature = "hub"))]
#[test]
fn installer_lists_copies_but_eof_does_not_delete() {
    let temp = tempfile::tempdir().unwrap();
    let machine = temp.path().join(".guildbotics");
    let workspace = temp.path().join("work");
    fs::create_dir_all(machine.join("data")).unwrap();
    fs::create_dir_all(workspace.join(".guildbotics")).unwrap();
    fs::write(
        machine.join("data/workspaces.json"),
        serde_json::json!([workspace]).to_string(),
    )
    .unwrap();
    let output = install(temp.path());
    assert!(
        output.contains(
            &workspace
                .join(".guildbotics")
                .canonicalize()
                .unwrap()
                .to_string_lossy()
                .to_string()
        )
    );
    assert!(output.contains("DELETE"));
    assert!(workspace.join(".guildbotics").exists());
}
