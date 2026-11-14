#![cfg(unix)]

use std::{fs, path::Path, process::Command};

const BIN: &str = env!("CARGO_BIN_EXE_guildbotics");
const SKILL: &[u8] = include_bytes!("../../../skills/guildbotics/SKILL.md");

fn install(home: &Path, copilot: &Path) {
    let result = Command::new(BIN)
        .args(["install"])
        .env("HOME", home)
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
}

#[cfg(feature = "hub")]
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
            .env("GUILDBOTICS_HOME", home.join(".guildbotics"))
            .output()
            .unwrap();
        assert!(result.status.success());
        let state: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(state["configured"], false);
    }
}

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
