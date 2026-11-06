//! The instruction's arguments are accepted by the actual executable.
use std::process::Command;

#[test]
fn image_load_help_and_validation_reach_the_real_entry() {
    let cli = env!("CARGO_BIN_EXE_guildbotics");
    let help = Command::new(cli)
        .args(["environment", "image", "load", "--help"])
        .output()
        .unwrap();
    assert!(help.status.success());
    let help = String::from_utf8(help.stdout).unwrap();
    assert!(help.contains("<ARCHIVE>") && help.contains("--tag"));
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("not an image.tar");
    std::fs::write(&path, "invalid archive").unwrap();
    let output = Command::new(cli)
        .env("HOME", dir.path())
        .env("GUILDBOTICS_HOME", dir.path())
        .env("LANGUAGE", "en")
        .args(["environment", "image", "load"])
        .arg(&path)
        .args(["--tag", "local/agent:1"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    let error = String::from_utf8(output.stderr).unwrap();
    assert!(
        error.contains("Could not read image archive")
            && error.contains("not an image.tar")
            && error.contains("failed to read entire block"),
        "{error}"
    );
    assert!(!error.contains("unrecognized"), "{error}");
    assert!(!dir.path().join("data/msb").exists());
}

#[test]
fn managed_instruction_preserves_the_absolute_path_as_one_shell_argument() {
    let home = std::path::Path::new("/home/a user's $device");
    let instruction = gb_environment::image::load_command(home).unwrap();
    let command = instruction.strip_suffix(" <archive.tar>").unwrap();
    let output = Command::new("sh")
        .args(["-c", &format!("set -- {command}; printf '%s\\n' \"$@\"")])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "/home/a user's $device/.guildbotics/bin/guildbotics\nenvironment\nimage\nload\n"
    );
    assert!(gb_environment::image::load_command(std::path::Path::new("relative")).is_err());
}

#[test]
fn loads_and_reloads_real_archives_without_booting_a_vm() {
    use sha2::{Digest, Sha256};
    use std::io::Write;
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("data/msb");
    // Placeholder files make an accidental VM invocation fail immediately.
    for file in gb_environment::placed_files(&home) {
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        std::fs::write(file, "not an executable").unwrap();
    }
    std::fs::write(home.join("version"), "0.6.17").unwrap();
    let cpu = gb_environment::image::device_architecture();
    let config = format!(
        r#"{{"architecture":"{cpu}","os":"linux","rootfs":{{"type":"layers","diff_ids":[]}},"config":{{}}}}"#
    );
    let hash = Sha256::digest(config.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let digest = format!("sha256:{hash}");
    std::fs::write(dir.path().join("config.json"), config).unwrap();
    std::fs::write(
        dir.path().join("manifest.json"),
        r#"[{"Config":"config.json","RepoTags":["local/agent:1"],"Layers":[]}]"#,
    )
    .unwrap();
    for gzip in [false, true] {
        let path = dir.path().join("image.tar");
        let file = std::fs::File::create(&path).unwrap();
        let writer: Box<dyn Write> = if gzip {
            Box::new(flate2::write::GzEncoder::new(
                file,
                flate2::Compression::default(),
            ))
        } else {
            Box::new(file)
        };
        let mut builder = tar::Builder::new(writer);
        for name in ["config.json", "manifest.json"] {
            builder
                .append_path_with_name(dir.path().join(name), name)
                .unwrap();
        }
        drop(builder.into_inner().unwrap());
        for _ in 0..2 {
            let output = Command::new(env!("CARGO_BIN_EXE_guildbotics"))
                .env("HOME", dir.path())
                .env("GUILDBOTICS_HOME", dir.path())
                .env("MSB_CONFIG_PATH", dir.path().join("no-config.json"))
                .args(["environment", "image", "load"])
                .arg(&path)
                .args(["--tag", "local/extra:1"])
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            let text = String::from_utf8(output.stdout).unwrap();
            for reference in ["local/agent:1", "local/extra:1"] {
                assert!(
                    text.contains(&format!("loaded {reference} {digest}")),
                    "{text}"
                );
            }
        }
    }
}

#[test]
fn environment_help_and_status_reach_the_real_entry_without_starting_a_vm() {
    let cli = env!("CARGO_BIN_EXE_guildbotics");
    let help = Command::new(cli)
        .args(["environment", "--help"])
        .output()
        .unwrap();
    assert!(help.status.success());
    let text = String::from_utf8(help.stdout).unwrap();
    for command in ["status", "build", "remove", "login", "usage", "image"] {
        assert!(text.contains(command), "{text}");
    }
    let root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(root.path().join("data")).unwrap();
    std::fs::write(root.path().join("data/msb"), "cannot be a runtime").unwrap();
    let workspace = root.path().join("workspace");
    std::fs::create_dir_all(workspace.join(".guildbotics/config")).unwrap();
    let status = |arguments: &[&str]| {
        Command::new(cli)
            .env("HOME", root.path())
            .env("GUILDBOTICS_HOME", root.path())
            .env("LANGUAGE", "en")
            .args(["environment", "--workspace"])
            .arg(&workspace)
            .arg("status")
            .args(arguments)
            .output()
            .unwrap()
    };
    let output = status(&[]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let text = String::from_utf8(output.stdout).unwrap();
    let json = status(&["--format", "json"]);
    assert!(
        json.status.success(),
        "{}",
        String::from_utf8_lossy(&json.stderr)
    );
    let json: serde_json::Value = serde_json::from_slice(&json.stdout).unwrap();
    assert_eq!(json["setting"], "runtime");
    assert_eq!(json["runtime"]["available"], false);
    let refusal = json["refusal"].as_str().unwrap();
    assert!(!refusal.is_empty());
    assert!(text.contains(&format!("runtime: {refusal}")));
    assert!(text.contains(&format!("runtime: unavailable: {refusal}")));
    assert!(text.contains(&format!(
        "location: {}",
        json["snapshot"]["path"].as_str().unwrap()
    )));
    assert!(
        json["tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|tool| tool["label"] == "Claude Code")
    );
}

#[test]
fn image_load_localizes_the_wrong_cpu_before_placing_the_runtime() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("wrong-cpu.tar");
    let device = gb_environment::image::device_architecture();
    let other = if device == "arm64" { "amd64" } else { "arm64" };
    let index = format!(r#"{{"manifests":[{{"platform":{{"architecture":"{other}"}}}}]}}"#);
    let mut tar = tar::Builder::new(std::fs::File::create(&path).unwrap());
    let mut header = tar::Header::new_gnu();
    header.set_size(index.len() as u64);
    header.set_mode(0o644);
    header.set_cksum();
    tar.append_data(&mut header, "index.json", index.as_bytes())
        .unwrap();
    tar.finish().unwrap();
    for (language, message) in [("en", "cannot run"), ("ja", "では動きません")] {
        let output = Command::new(env!("CARGO_BIN_EXE_guildbotics"))
            .env("GUILDBOTICS_HOME", dir.path())
            .env("LANGUAGE", language)
            .args(["environment", "image", "load"])
            .arg(&path)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        let error = String::from_utf8_lossy(&output.stderr);
        for part in ["wrong-cpu.tar", device, other, message] {
            assert!(error.contains(part), "{error}");
        }
        assert!(!dir.path().join("data/msb").exists());
    }
}

#[test]
fn image_declare_merges_architectures_replaces_the_reference_and_can_reset() {
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("workspace");
    let config = workspace.join(".guildbotics/config");
    let declaration = config.join("intelligences/agent_environment.yml");
    std::fs::create_dir_all(declaration.parent().unwrap()).unwrap();
    std::fs::write(&declaration, "dns:\n  nameservers: [1.1.1.1, 8.8.8.8]\n").unwrap();
    let invoke = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_guildbotics"))
            .env("HOME", root.path())
            .env("GUILDBOTICS_HOME", root.path().join("machine"))
            .args(["environment", "--workspace"])
            .arg(&workspace)
            .args(["image", "declare"])
            .args(args)
            .output()
            .unwrap()
    };
    let arm = format!("sha256:{}", "1".repeat(64));
    let amd = format!("sha256:{}", "2".repeat(64));
    let first = invoke(&["local/agent:1", "--digest", &format!("arm64={arm}")]);
    assert!(first.status.success(), "{first:?}");
    assert_eq!(
        String::from_utf8(first.stdout).unwrap(),
        format!("local/agent:1 arm64 {arm}\n")
    );
    let merged = invoke(&["local/agent:1", "--digest", &format!("amd64={amd}")]);
    assert!(merged.status.success(), "{merged:?}");
    assert_eq!(
        String::from_utf8(merged.stdout).unwrap(),
        format!("local/agent:1 amd64 {amd}\nlocal/agent:1 arm64 {arm}\n")
    );
    let replaced = invoke(&["local/agent:2", "--digest", &format!("amd64={amd}")]);
    assert!(replaced.status.success(), "{replaced:?}");
    let stored = std::fs::read_to_string(&declaration).unwrap();
    assert!(
        stored.contains("local/agent:2") && stored.contains(&amd) && !stored.contains(&arm),
        "{stored}"
    );
    assert!(
        stored.contains("1.1.1.1") && stored.contains("8.8.8.8"),
        "{stored}"
    );
    for args in [
        &[][..],
        &["local/agent:1", "--default"][..],
        &["local/agent:1", "--digest", "invalid"][..],
    ] {
        let invalid = invoke(args);
        assert_eq!(invalid.status.code(), Some(2), "{invalid:?}");
    }
    let invalid = invoke(&["local/agent:1", "--digest", "amd64=sha256:x"]);
    assert_eq!(invalid.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&invalid.stderr).contains("sha256:x"));
    assert_eq!(std::fs::read_to_string(&declaration).unwrap(), stored);
    let default = invoke(&["--default"]);
    assert!(default.status.success(), "{default:?}");
    assert!(String::from_utf8_lossy(&default.stdout).contains("GuildBotics default"));
    let stored = std::fs::read_to_string(&declaration).unwrap();
    assert!(!stored.contains("local/agent:2"));
    assert!(stored.contains("8.8.8.8"));
}

#[test]
fn ci_image_load_requires_a_workspace_and_a_real_archive_before_looking_for_a_runner() {
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("workspace");
    let team = workspace.join(".guildbotics/config/team");
    std::fs::create_dir_all(&team).unwrap();
    std::fs::write(
        team.join("project.yml"),
        "name: Test\nservices:\n  code_hosting_service:\n    name: local\n",
    )
    .unwrap();
    let archive = root.path().join("image.tar");
    std::fs::write(&archive, "The local provider must not read this archive.").unwrap();
    let invoke = |workspace: Option<&std::path::Path>, archive: &std::path::Path| {
        let mut cli = Command::new(env!("CARGO_BIN_EXE_guildbotics"));
        cli.env("HOME", root.path())
            .env("GUILDBOTICS_HOME", root.path().join("machine"))
            .env("LANGUAGE", "en")
            .env_remove("GUILDBOTICS_WORKSPACE_ROOT")
            .env_remove("GUILDBOTICS_CONFIG_DIR")
            .current_dir(root.path())
            .arg("environment");
        if let Some(workspace) = workspace {
            cli.arg("--workspace").arg(workspace);
        }
        cli.args(["image", "load-ci"])
            .arg(archive)
            .output()
            .unwrap()
    };
    let unselected = invoke(None, &archive);
    assert_eq!(unselected.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&unselected.stderr)
            .contains("No GuildBotics workspace is selected.")
    );
    let absent = invoke(Some(&workspace), &root.path().join("missing.tar"));
    assert_eq!(absent.status.code(), Some(2));
    let no_runner = invoke(Some(&workspace), &archive);
    assert!(no_runner.status.success(), "{no_runner:?}");
    assert_eq!(
        String::from_utf8(no_runner.stdout).unwrap(),
        "The workspace's code host has no CI runner GuildBotics loads images into; there is nothing to load.\n"
    );
    assert!(!root.path().join("machine/data/msb").exists());
}

#[test]
fn status_shares_declaration_runtime_snapshot_and_logins_between_text_and_json() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = dir.path().join("workspace");
    let declaration = workspace.join(".guildbotics/config/intelligences/agent_environment.yml");
    std::fs::create_dir_all(declaration.parent().unwrap()).unwrap();
    std::fs::write(&declaration, "resources:\n  memory_mib: 4096\n  cpus: 2\nnetwork:\n  mode: allowlist\n  allowed_domains: [pypi.org, api.github.com]\ndns:\n  nameservers: [1.1.1.1, 8.8.8.8]\n").unwrap();
    let invoke = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_guildbotics"))
            .env("HOME", dir.path())
            .env("GUILDBOTICS_HOME", dir.path().join("machine"))
            .env("LANGUAGE", "en")
            .args(["environment", "--workspace"])
            .arg(&workspace)
            .args(args)
            .output()
            .unwrap()
    };
    let read = || {
        let markdown = invoke(&["status"]);
        let json = invoke(&["status", "--format", "json"]);
        assert!(
            markdown.status.success() && json.status.success(),
            "{markdown:?} {json:?}"
        );
        (
            String::from_utf8(markdown.stdout).unwrap(),
            serde_json::from_slice::<serde_json::Value>(&json.stdout).unwrap(),
        )
    };
    let (text, facts) = read();
    let runtime_version = facts["runtime"]["version"].as_str().unwrap();
    assert!(facts["runtime"]["available"] == true && !runtime_version.is_empty());
    let snapshot = facts["snapshot"]["name"].as_str().unwrap();
    let snapshot = format!("snapshot: missing {snapshot} (run `guildbotics environment build`)");
    for line in [
        &format!("runtime: available {runtime_version}"),
        "resources: 4096 MiB, 2 vCPU",
        "GuildBotics default",
        &snapshot,
        "dns: 1.1.1.1, 8.8.8.8 -> 1.1.1.1, 8.8.8.8",
        "network: allowlist (pypi.org, api.github.com)",
    ] {
        assert!(text.contains(line), "{line} missing from {text}");
    }
    for name in ["codex", "grok"] {
        let row = facts["tools"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["name"] == name)
            .unwrap();
        assert_eq!(row["credentials_saved"], false);
        assert_eq!(row["authentication_failed"], false);
        let problem = row["problem"].as_str().unwrap();
        assert!(problem.contains("No credentials are saved") && text.contains(problem));
    }
    std::fs::write(
        &declaration,
        "network: []\ndns:\n  nameservers: [1.1.1.1]\n",
    )
    .unwrap();
    let (text, facts) = read();
    for field in ["network", "resources"] {
        assert!(facts[field].is_null(), "{field}: {facts}");
        assert!(text.contains(&format!("{field}: unavailable")));
    }
    let runtime = dir.path().join("machine/data/msb");
    std::fs::remove_dir_all(&runtime).unwrap();
    std::fs::write(&runtime, "unplaceable runtime").unwrap();
    let (text, facts) = read();
    let reason = facts["runtime"]["reason"].as_str().unwrap();
    assert!(!reason.is_empty());
    assert!(text.contains(&format!("runtime: unavailable: {reason}")));
    for args in [
        &["build"][..],
        &["remove"],
        &["image", "list"],
        &["image", "declare", "fixture"],
    ] {
        let refused = invoke(args);
        assert_eq!(refused.status.code(), Some(1), "{refused:?}");
        assert!(String::from_utf8_lossy(&refused.stderr).contains(reason));
    }
}
