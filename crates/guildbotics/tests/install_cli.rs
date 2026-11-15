#![cfg(unix)]

use std::{fs, path::Path, process::Command};

const BIN: &str = env!("CARGO_BIN_EXE_guildbotics");
const SKILL: &[u8] = include_bytes!("../../../skills/guildbotics/SKILL.md");

fn install(home: &Path, copilot: &Path) -> String {
    let result = Command::new(BIN)
        .args(["install"])
        .env("HOME", home)
        .env("GIT_CONFIG_GLOBAL", home.join(".gitconfig"))
        .env_remove("XDG_CONFIG_HOME")
        .env(gb_base::home::USER_HOME_ENV, home)
        .env("GUILDBOTICS_HOME", home.join(".guildbotics"))
        .env_remove("CODEX_HOME")
        .env_remove("CLAUDE_CONFIG_DIR")
        .env_remove("GROK_HOME")
        .env("COPILOT_HOME", copilot)
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
    install(home, &home.join("copilot"));
    assert!(!bin.is_symlink());
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

#[cfg(unix)]
#[test]
fn installation_preserves_user_commands_and_edited_skills() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path();
    let on_path = home.join(".local/bin/guildbotics");
    fs::create_dir_all(on_path.parent().unwrap()).unwrap();
    fs::write(&on_path, "#!/bin/sh\nexec mine\n").unwrap();
    let copilot = home.join("copilot-overridden");
    for path in [
        home.join(".codex"),
        home.join(".claude"),
        home.join(".gemini"),
        copilot.clone(),
    ] {
        fs::create_dir_all(path).unwrap();
    }
    let grok = home.join(".grok/skills/guildbotics/SKILL.md");
    fs::create_dir_all(grok.parent().unwrap()).unwrap();
    fs::write(&grok, "user-owned").unwrap();
    install(home, &copilot);
    let paths = [
        home.join(".agents/skills/guildbotics/SKILL.md"),
        home.join(".claude/skills/guildbotics/SKILL.md"),
        home.join(".gemini/config/skills/guildbotics/SKILL.md"),
        copilot.join("skills/guildbotics/SKILL.md"),
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
    install(home, &copilot);
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
        install(&home, &home.join("copilot"));
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
    let output = install(temp.path(), &temp.path().join("copilot"));
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
