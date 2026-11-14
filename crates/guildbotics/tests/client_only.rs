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
    let bin = home.join(".guildbotics/bin");
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
