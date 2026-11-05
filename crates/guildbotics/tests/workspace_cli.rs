use serde_json::Value;
use std::{
    path::Path,
    process::{Command, Output},
};

fn workspace(root: &Path, args: &[&str], path: Option<&Path>) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_guildbotics"));
    command
        .env("HOME", root)
        .env("GUILDBOTICS_HOME", root.join(".guildbotics"))
        .env("LANGUAGE", "en")
        .env_remove("GUILDBOTICS_WORKSPACE_ROOT")
        .env_remove("GUILDBOTICS_CONFIG_DIR")
        .args(args);
    if let Some(path) = path {
        command.arg(path);
    }
    command.output().unwrap()
}

fn output(result: Output) -> Value {
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    serde_json::from_slice(&result.stdout).unwrap()
}

#[test]
fn workspace_current_requires_a_selection_and_status_does_not() {
    let root = tempfile::tempdir().unwrap();
    let root = root.path().canonicalize().unwrap();
    let status = output(workspace(
        &root,
        &["workspace", "status", "--format", "json"],
        None,
    ));
    assert_eq!(status["configured"], false);
    let current = workspace(&root, &["workspace", "current"], None);
    assert!(!current.status.success());
    assert!(current.stdout.is_empty());
    assert!(
        String::from_utf8_lossy(&current.stderr)
            .contains("No active GuildBotics workspace is configured.")
    );
    assert!(!root.join(".guildbotics").exists());
}

#[test]
fn workspace_use_persists_a_workspace_whose_grants_need_repair() {
    let root = tempfile::tempdir().unwrap();
    let root = root.path().canonicalize().unwrap();
    for scope in ["config/intelligences", "local"] {
        let project = root.join(scope.replace('/', "-"));
        let settings = project.join(".guildbotics").join(scope);
        std::fs::create_dir_all(&settings).unwrap();
        std::fs::write(
            settings.join("cli_agent_filesystem_grants.yml"),
            "documents: [",
        )
        .unwrap();
        let used = output(workspace(
            &root,
            &["workspace", "use", "--format", "json"],
            Some(&project),
        ));
        assert_eq!(used["workspace"], project.to_str().unwrap());
        assert_eq!(used["workspace_exists"], true);
        let current = output(workspace(
            &root,
            &["workspace", "current", "--format", "json"],
            None,
        ));
        assert_eq!(current, used);
        assert!(Path::new(used["state_file"].as_str().unwrap()).is_file());
    }
}

#[test]
fn cli_workspace_selection_refuses_exchange_locations_before_registering_them() {
    let root = tempfile::tempdir().unwrap();
    let root = root.path().canonicalize().unwrap();
    let project = root.join("Documents/GuildBotics/project");
    std::fs::create_dir_all(&project).unwrap();
    let refused = workspace(&root, &["workspace", "use"], Some(&project));
    assert!(!refused.status.success());
    assert!(String::from_utf8_lossy(&refused.stderr).contains("inside granted directory"));
    for explicit in [true, false] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_guildbotics"));
        command
            .env("HOME", &root)
            .env("GUILDBOTICS_HOME", root.join(".guildbotics"))
            .env("LANGUAGE", "en")
            .env_remove("GUILDBOTICS_CONFIG_DIR")
            .arg("environment");
        if explicit {
            command
                .env_remove("GUILDBOTICS_WORKSPACE_ROOT")
                .arg("--workspace")
                .arg(&project);
        } else {
            command.env("GUILDBOTICS_WORKSPACE_ROOT", &project);
        }
        let refused = command.arg("status").output().unwrap();
        assert!(!refused.status.success());
        assert!(String::from_utf8_lossy(&refused.stderr).contains("inside granted directory"));
    }
    assert!(!root.join(".guildbotics/data/workspaces.json").exists());
    assert!(
        !root
            .join(".guildbotics/data/active-workspace.json")
            .exists()
    );
}
