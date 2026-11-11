//! Access contracts are checked before side effects and without a VM.
use gb_environment::contract;
use std::path::Path;
fn write(root: &Path, path: &str, text: &str) {
    let p = root.join(path);
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(p, text).unwrap();
}
/// A temporary root as host paths name it: macOS reaches its temporary
/// directory through a link, and host paths are compared resolved.
fn temporary() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    (dir, root)
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
        "documents:\n  - path: Documents/project\n    access: read_write\n  - path: Documents/GuildBotics/reference\n    access: read\n",
    );
    for read_only in [true, false] {
        let plan = contract::resolve(&workspace, &home, &[], read_only, true).unwrap();
        assert!(home.join("Documents/project").is_dir());
        // Every grant is mounted once, at its host path, with its own
        // access; `~` reaches those under the host's home through links,
        // the outermost of nested ones alone.
        let exchange = home.join(gb_environment::EXCHANGE).canonicalize().unwrap();
        let project = home.join("Documents/project").canonicalize().unwrap();
        let reference = exchange.join("reference");
        let guests: Vec<_> = plan
            .grants
            .iter()
            .map(|m| (m.guest.clone(), m.host.clone().unwrap(), m.readonly))
            .collect();
        let spelled = |path: &Path| gb_environment::guest_path(path).unwrap();
        assert_eq!(
            guests,
            [
                (spelled(&exchange), exchange.clone(), read_only),
                (spelled(&project), project.clone(), read_only),
                (spelled(&reference), reference, true),
            ]
        );
        assert_eq!(
            plan.links,
            [
                contract::Link {
                    name: "Documents/GuildBotics".into(),
                    target: spelled(&exchange),
                },
                contract::Link {
                    name: "Documents/project".into(),
                    target: spelled(&project),
                },
            ]
        );
    }
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
    let (_dir, root) = temporary();
    let workspace = root.join("workspace");
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
    let (_d, root) = temporary();
    let workspace = root.join("workspace");
    let home = root.join("home");
    std::fs::create_dir_all(home.join("Documents/project/nested")).unwrap();
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: Documents/project\n    access: read\n",
    );
    let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
    let cwd = home.join("Documents/project");
    assert!(
        contract::copy_plan(&workspace, "aiko", &cwd, &access, &[], root.join("lock")).is_err()
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
        quota_mib: None,
    }];
    let plan = contract::copy_plan(
        &workspace,
        "aiko",
        &cwd,
        &access,
        &mounts,
        root.join("lock"),
    )
    .unwrap();
    assert!(plan.excluded.contains(&"nested".into()));
    assert!(plan.excluded.contains(&".git".into()));
    assert!(plan.excluded.contains(&".guildbotics".into()));
}

#[test]
fn managed_clones_remain_open_under_denied_ancestors() {
    let (_dir, root) = temporary();
    let home = root.join("home");
    let workspace = root.join("workspace");
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
        root.join("lock"),
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
                root.join("lock"),
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
    let (_dir, root) = temporary();
    let home = root.join("home");
    let workspace = root.join("workspace");
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
        std::os::unix::fs::symlink(&state, root.join("parent-link")).unwrap();
        assert!(
            contract::validate_mount_source(&root.join("parent-link/.claude.json"), &access)
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

#[test]
fn client_copies_shadow_only_their_cwd_inside_grants_and_member_clones() {
    use gb_environment::worktree::transfer::{Returned, Upload};
    use std::sync::Arc;
    let (_dir, root) = temporary();
    let home = root.join("home");
    let workspace = root.join("workspace");
    let exchange = home.join(gb_environment::EXCHANGE);
    let clones = workspace.join(".guildbotics/local/clones/aiko");
    let clone = clones.join("a".repeat(64));
    let child = exchange.join("repo");
    for path in [&clone, &child] {
        std::fs::create_dir_all(path).unwrap();
    }
    let access = contract::resolve(&workspace, &home, &[], false, false).unwrap();
    let upload = Arc::new(Upload {
        directories: vec![],
        files: vec![],
        nested: vec![],
        git: None,
    });
    for cwd in [&exchange, &child, &clone, &clone.join("subdir")] {
        let plan = contract::received_plan(
            &workspace,
            "aiko",
            cwd,
            &access,
            access.grants.clone(),
            false,
            Some(upload.clone()),
            Returned::default(),
        )
        .unwrap();
        let guest = gb_environment::guest_path(cwd).unwrap();
        assert!(plan.copy.as_ref().unwrap().upload.is_some());
        assert_eq!(plan.cwd, guest);
        let at_cwd: Vec<_> = plan.mounts.iter().filter(|m| m.guest == guest).collect();
        assert_eq!(at_cwd.len(), 1);
        assert!(at_cwd[0].host.is_none());
        assert!(!at_cwd[0].readonly);
        assert!(plan.mounts.iter().any(|m| m.host.as_ref() == Some(&clones)));
        if cwd != &exchange {
            assert!(
                plan.mounts
                    .iter()
                    .any(|m| m.host.as_ref() == Some(&exchange))
            );
        }
        let readonly = contract::received_plan(
            &workspace,
            "aiko",
            cwd,
            &access,
            access.grants.clone(),
            true,
            None,
            Returned::default(),
        )
        .unwrap();
        assert!(readonly.copy.is_none());
        assert!(
            readonly
                .mounts
                .iter()
                .any(|m| m.guest == guest && m.host.is_none())
        );
    }
    let mut readonly_access = access.clone();
    readonly_access.grants[0].readonly = true;
    assert!(
        contract::received_plan(
            &workspace,
            "aiko",
            &child,
            &readonly_access,
            readonly_access.grants.clone(),
            false,
            Some(upload.clone()),
            Returned::default()
        )
        .is_err()
    );
    assert!(
        contract::received_plan(
            &workspace,
            "other",
            &clone,
            &access,
            access.grants.clone(),
            false,
            Some(upload),
            Returned::default()
        )
        .is_err()
    );
}
