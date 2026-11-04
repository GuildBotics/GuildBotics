//! Access contracts are checked before side effects and without a VM.
use gb_environment::contract;
use std::path::Path;
fn write(root: &Path, path: &str, text: &str) {
    let p = root.join(path);
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(p, text).unwrap();
}
#[test]
fn grants_protect_credentials_and_are_readonly_for_readonly_commands() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    std::fs::create_dir(&home).unwrap();
    let workspace = dir.path().join("workspace");
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: Documents/project\n    access: read_write\n",
    );
    let plan = contract::resolve(&workspace, &home, &[], true, true).unwrap();
    assert_eq!(plan.grants.len(), 2);
    assert!(plan.grants.iter().all(|m| m.readonly));
    assert!(home.join("Documents/project").is_dir());
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: .ssh\n    access: read\n",
    );
    assert!(contract::resolve(&workspace, &home, &[], false, true).is_err());
}
#[test]
fn every_planned_grant_is_checked_before_creating_any() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    std::fs::create_dir(&home).unwrap();
    let workspace = dir.path().join("workspace");
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: Documents/new\n    access: read_write\n  - path: .aws\n    access: read\n",
    );
    assert!(contract::resolve(&workspace, &home, &[], false, true).is_err());
    assert!(!home.join("Documents").exists());
}
#[cfg(unix)]
#[test]
fn a_sensitive_alias_protects_the_destination_too() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    std::fs::create_dir(&home).unwrap();
    std::fs::create_dir(home.join("keys")).unwrap();
    std::os::unix::fs::symlink("keys", home.join(".ssh")).unwrap();
    let workspace = dir.path().join("workspace");
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: keys\n    access: read\n",
    );
    assert!(contract::resolve(&workspace, &home, &[], false, true).is_err());
}
#[test]
fn network_is_closed_for_readonly_and_rejects_inconsistent_declarations() {
    let dir = tempfile::tempdir().unwrap();
    let config = dir.path().join("config");
    let templates = dir.path().join("templates");
    write(
        &templates,
        "intelligences/agent_environment.yml",
        "network:\n  mode: allowlist\n  allowed_domains: [example.org]\ndns:\n  nameservers: [1.1.1.1]\n",
    );
    let network = contract::network(&config, &templates, true).unwrap();
    assert!(network.domains.is_empty());
    assert!(!network.unrestricted);
    assert!(!network.local_network);
    assert_eq!(network.nameservers, ["1.1.1.1"]);
    network.policy().unwrap();
    write(
        &templates,
        "intelligences/agent_environment.yml",
        "network:\n  mode: deny\n  allowed_domains: [example.org]\n",
    );
    assert!(contract::network(&config, &templates, false).is_err());
}

#[test]
fn only_managed_member_clones_are_precise_host_owned_working_directories() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = dir.path().join("workspace");
    let hash = "a".repeat(64);
    let clone = workspace.join(".guildbotics/local/clones/aiko").join(hash);
    std::fs::create_dir_all(&clone).unwrap();
    let denied = vec![workspace.join(".guildbotics")];
    assert!(contract::working_directory(&workspace, "aiko", &clone, &denied).is_ok());
    for rejected in [
        workspace.join(".guildbotics"),
        workspace.join(".guildbotics/local"),
        workspace.join(".guildbotics/local/clones/aiko"),
        workspace.join(".guildbotics/local/clones/aiko/ordinary"),
    ] {
        std::fs::create_dir_all(&rejected).unwrap();
        assert!(contract::working_directory(&workspace, "aiko", &rejected, &denied).is_err());
    }
    assert!(contract::working_directory(&workspace, "other", &clone, &denied).is_err());
}

#[test]
fn worktree_planning_preserves_covering_readonly_and_nested_mount_boundaries() {
    let d = tempfile::tempdir().unwrap();
    let workspace = d.path().join("workspace");
    let home = d.path().join("home");
    std::fs::create_dir_all(home.join("Documents/project/nested")).unwrap();
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: Documents/project\n    access: read\n",
    );
    let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
    let cwd = home.join("Documents/project");
    assert!(
        contract::copy_plan(
            &workspace,
            "aiko",
            &cwd,
            &access,
            &[],
            d.path().join("lock")
        )
        .is_err()
    );
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: Documents/project\n    access: read_write\n",
    );
    let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
    let mounts = [gb_environment::Mount {
        guest: cwd.join("nested").to_str().unwrap().into(),
        host: Some(cwd.join("nested")),
        readonly: true,
    }];
    let plan = contract::copy_plan(
        &workspace,
        "aiko",
        &cwd,
        &access,
        &mounts,
        d.path().join("lock"),
    )
    .unwrap();
    assert!(plan.excluded.contains(&"nested".into()));
    assert!(plan.excluded.contains(&".git".into()));
    assert!(plan.excluded.contains(&".guildbotics".into()));
}

#[test]
fn managed_clones_remain_open_under_denied_ancestors() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let workspace = root.path().join("workspace");
    let clone = workspace
        .join(".guildbotics/local/clones/aiko")
        .join("a".repeat(64));
    std::fs::create_dir_all(&home).unwrap();
    std::fs::create_dir_all(&clone).unwrap();
    let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
    let plan = contract::plan(
        &workspace,
        "aiko",
        &clone,
        &access,
        vec![],
        root.path().join("lock"),
        false,
    )
    .unwrap();
    assert!(plan.copy.is_none());
    for denied in [
        workspace.join(".guildbotics"),
        workspace.join(".guildbotics/local"),
        workspace.join(".guildbotics/local/clones"),
    ] {
        write(
            &workspace,
            ".guildbotics/local/cli_agent_filesystem_grants.yml",
            &format!("deny: [{}]\n", serde_json::to_string(&denied).unwrap()),
        );
        let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
        assert!(
            contract::plan(
                &workspace,
                "aiko",
                &clone,
                &access,
                vec![],
                root.path().join("lock"),
                false
            )
            .is_ok(),
            "{}",
            denied.display()
        );
    }
}

#[test]
fn runtime_mounts_accept_precise_files_under_denied_ancestors_but_never_links() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let workspace = root.path().join("workspace");
    std::fs::create_dir_all(&home).unwrap();
    let state = workspace.join(".guildbotics/local/tool-state");
    std::fs::create_dir_all(&state).unwrap();
    let file = state.join(".claude.json");
    std::fs::write(&file, b"fake-login-state").unwrap();
    let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
    assert_eq!(
        contract::validate_mount_source(&file, &access).unwrap(),
        file
    );
    assert_eq!(
        contract::validate_mount_source(&state, &access).unwrap(),
        state
    );
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(&file, state.join("file-link")).unwrap();
        assert!(contract::validate_mount_source(&state.join("file-link"), &access).is_err());
        std::os::unix::fs::symlink(&state, root.path().join("parent-link")).unwrap();
        assert!(
            contract::validate_mount_source(&root.path().join("parent-link/.claude.json"), &access)
                .is_err()
        );
    }
    write(
        &workspace,
        ".guildbotics/local/cli_agent_filesystem_grants.yml",
        &format!(
            "deny: [{}]\n",
            serde_json::to_string(&workspace.join(".guildbotics/local")).unwrap()
        ),
    );
    let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
    assert!(contract::validate_mount_source(&file, &access).is_ok());
}
