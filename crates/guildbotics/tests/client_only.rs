//! A distributed executable must work without looking up the build checkout.
use std::process::Command;

#[test]
fn relocated_client_installs_embedded_skills_and_has_only_client_commands() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("home");
    std::fs::create_dir_all(home.join(".codex")).unwrap();
    let exe = temp.path().join(if cfg!(windows) {
        "client.exe"
    } else {
        "client"
    });
    std::fs::copy(env!("CARGO_BIN_EXE_guildbotics"), &exe).unwrap();
    let run = |args: &[&str]| {
        Command::new(&exe)
            .args(args)
            .current_dir(temp.path())
            .env(gb_base::home::USER_HOME_ENV, &home)
            .env_remove("GUILDBOTICS_HOME")
            .env("CODEX_HOME", home.join(".codex"))
            .env_remove("CLAUDE_CONFIG_DIR")
            .env_remove("GROK_HOME")
            .env_remove("COPILOT_HOME")
            .env("LANGUAGE", "en")
            .output()
            .unwrap()
    };
    let help = run(&["--help"]);
    assert!(help.status.success());
    let help = String::from_utf8(help.stdout).unwrap();
    for name in [
        "member",
        "run",
        "git-credential",
        "connect",
        "open",
        "install",
        "version",
    ] {
        assert!(help.contains(name), "{help}");
    }
    for name in [
        "server",
        "service",
        "environment",
        "secrets",
        "workspace",
        "diagnostics",
        "branvil",
    ] {
        assert!(!run(&[name, "--help"]).status.success(), "{name}");
    }
    assert!(run(&["version"]).status.success());
    #[cfg(unix)]
    {
        let installed = run(&["install"]);
        assert!(
            installed.status.success(),
            "{}",
            String::from_utf8_lossy(&installed.stderr)
        );
        assert_eq!(
            std::fs::read(home.join(".agents/skills/guildbotics/SKILL.md")).unwrap(),
            include_bytes!("../../../skills/guildbotics/SKILL.md")
        );
    }
}

#[cfg(windows)]
#[test]
fn windows_install_uses_userprofile_and_updates_user_path_once() {
    if std::env::var_os("CI").is_none() {
        return;
    }
    fn path(value: Option<&str>) -> String {
        let mut command = Command::new("powershell.exe");
        command.args(["-NoProfile", "-NonInteractive", "-Command"]);
        if let Some(value) = value {
            command
                .arg("$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment'); $kind = if ($key.GetValueNames() -contains 'Path') { $key.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }; $key.SetValue('Path', $env:GB_TEST_PATH, $kind)")
                .env("GB_TEST_PATH", value);
        } else {
            command.arg("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); [Console]::Write([Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment').GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames))");
        }
        let result = command.output().unwrap();
        assert!(result.status.success(), "{result:?}");
        String::from_utf8(result.stdout).unwrap()
    }
    struct Restore(String);
    impl Drop for Restore {
        fn drop(&mut self) {
            path(Some(&self.0));
        }
    }
    let original = path(None);
    let _restore = Restore(original.clone());
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("利用者 space");
    std::fs::create_dir_all(home.join(".codex")).unwrap();
    let install = || {
        let result = Command::new(env!("CARGO_BIN_EXE_guildbotics"))
            .arg("install")
            .env("USERPROFILE", &home)
            .env("HOME", temp.path().join("wrong-home"))
            .env_remove("GUILDBOTICS_HOME")
            .env("CODEX_HOME", home.join(".codex"))
            .env_remove("CLAUDE_CONFIG_DIR")
            .env_remove("GROK_HOME")
            .env_remove("COPILOT_HOME")
            .output()
            .unwrap();
        assert!(result.status.success(), "{result:?}");
    };
    install();
    let bin = home.join(".guildbotics").join("bin");
    let expected = if original.is_empty() {
        bin.display().to_string()
    } else {
        format!("{};{}", original.trim_end_matches(';'), bin.display())
    };
    assert_eq!(path(None), expected);
    install();
    assert_eq!(path(None), expected);
    assert!(bin.join("guildbotics.exe").is_file());
    assert_eq!(
        std::fs::read(home.join(".agents/skills/guildbotics/SKILL.md")).unwrap(),
        include_bytes!("../../../skills/guildbotics/SKILL.md")
    );
}

#[cfg(unix)]
#[test]
fn source_free_ci_step_preserves_the_checkout_and_restores_partial_moves() {
    use std::{fs, os::unix::fs::PermissionsExt};
    let workflow = include_str!("../../../.github/workflows/ci.yml");
    let script = workflow
        .split("- name: Run without the build checkout")
        .nth(1)
        .unwrap()
        .split("run: |\n")
        .nth(1)
        .unwrap()
        .split("\n      - uses:")
        .next()
        .unwrap()
        .lines()
        .map(|line| line.strip_prefix("          ").unwrap_or(line))
        .collect::<Vec<_>>()
        .join("\n");
    for failure in ["none", "install", "skills"] {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("checkout space");
        let tools = temp.path().join("tools");
        fs::create_dir_all(&tools).unwrap();
        for name in [
            "guildbotics",
            "skills",
            "target/x86_64-pc-windows-gnu/release",
        ] {
            fs::create_dir_all(root.join(name)).unwrap();
        }
        for name in ["guildbotics", "skills"] {
            fs::write(root.join(name).join("original"), name).unwrap();
        }
        let write_executable = |path: &std::path::Path, content: &str| {
            fs::write(path, content).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        };
        let binary = root.join("target/x86_64-pc-windows-gnu/release/guildbotics.exe");
        let client = r#"#!/bin/bash
set -eu
 test -d "$GITHUB_WORKSPACE"
 test ! -e "$GITHUB_WORKSPACE/guildbotics"
 test ! -e "$GITHUB_WORKSPACE/skills"
 if [ "$1" = install ]; then
   [ "$FAIL_STEP" != install ] || exit 71
   mkdir -p "$USERPROFILE/.agents/skills/guildbotics"
   touch "$USERPROFILE/.agents/skills/guildbotics/SKILL.md"
 fi
"#;
        write_executable(&binary, client);
        write_executable(
            &tools.join("cygpath"),
            "#!/bin/bash\nprintf '%s\\n' \"$2\"\n",
        );
        write_executable(
            &tools.join("mv"),
            r#"#!/bin/bash
set -eu
 [ "$1" != "$GITHUB_WORKSPACE" ] || exit 72
 if [ "$FAIL_STEP" = skills ] && [ "$1" = "$GITHUB_WORKSPACE/skills" ]; then exit 73; fi
 exec /bin/mv "$@"
"#,
        );
        let output = Command::new("bash")
            .args(["-e", "-o", "pipefail", "-c", &script])
            .current_dir(&root)
            .env("GITHUB_WORKSPACE", &root)
            .env("FAIL_STEP", failure)
            .env("TMPDIR", temp.path())
            .env("PATH", format!("{}:/usr/bin:/bin", tools.display()))
            .output()
            .unwrap();
        assert_eq!(
            output.status.code(),
            Some(match failure {
                "none" => 0,
                "install" => 71,
                _ => 73,
            }),
            "{failure}: {output:?}"
        );
        for name in ["guildbotics", "skills"] {
            assert_eq!(
                fs::read_to_string(root.join(name).join("original")).unwrap(),
                name
            );
            assert!(!root.join(format!("{name}.source-away")).exists());
        }
        assert_eq!(fs::read_to_string(&binary).unwrap(), client);
    }
}
