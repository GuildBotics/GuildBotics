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
    let output = Command::new(cli)
        .env("HOME", root.path())
        .env("GUILDBOTICS_HOME", root.path())
        .args(["environment", "--workspace"])
        .arg(workspace)
        .arg("status")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let text = String::from_utf8(output.stdout).unwrap();
    let (guidance, document) = text
        .split_once("\n{")
        .expect("guidance precedes JSON status");
    let view: serde_json::Value = serde_json::from_str(&format!("{{{document}")).unwrap();
    assert_eq!(guidance, view["problem"].as_str().unwrap());
    assert_eq!(view["problem_setting"], "runtime");
    assert_eq!(view["runtime"]["available"], false);
    assert!(
        view["tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|tool| tool["label"] == "Claude Code")
    );
}
