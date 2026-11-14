//! A read-only command's microVM works on nothing of the host's, reads the
//! code, the configuration and the identities read-only, and is told the
//! command as the Python version's entry reads it
//! (`guildbotics/environment/command_environment.py`, `host_window.py`).

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use gb_environment::{Command, Configuration, Host, Mount, shape};
use gb_wire::{CommandAccess, CommandFacts, InspectsItem};

struct Machine {
    _root: tempfile::TempDir,
    host: Host,
    workspace: PathBuf,
}

fn machine() -> Machine {
    let root = tempfile::tempdir().unwrap();
    // Not as the host spells it: with a `.`, and, as the OS hands it out on
    // macOS, under /var, which the host spells /private/var.
    let base = root.path().join(".");
    let package = base.join("checkout/guildbotics");
    let workspace = base.join("workspace");
    let identity_root = base.join("identity");
    for directory in [
        base.join("home"),
        package.join("templates/commands/functions"),
        workspace.join(".guildbotics/config"),
        workspace.join(".guildbotics/local/run"),
        identity_root.join(".guildbotics/config/team/members"),
    ] {
        std::fs::create_dir_all(directory).unwrap();
    }
    Machine {
        host: Host {
            data: base.join("data"),
            home: base.join("home"),
            package,
            identity_root,
        },
        workspace,
        _root: root,
    }
}

fn command(machine: &Machine, path: PathBuf, inspects: Vec<InspectsItem>) -> Command {
    Command {
        client_copy: false,
        remote_copy: false,
        files: None,
        upload: None,
        returned: Default::default(),
        config: std::sync::Arc::new(
            Configuration::copy(&machine.host, &machine.workspace).unwrap(),
        ),
        wants_result: false,
        invocation: None,
        person_id: "yuki".into(),
        run_id: "trace-1".into(),
        trace_id: "trace-1".into(),
        work_kind: "manual".into(),
        work_identity: "trace-1".into(),
        path,
        name: "functions/get_os_ui_language".into(),
        access: CommandAccess {
            inspects,
            read_only: true,
        },
        args: vec!["a=1".into()],
        cwd: machine.workspace.join("somewhere"),
        pipe: "input".into(),
    }
}

/// `path` as the host spells it.
fn named(path: &Path) -> PathBuf {
    gb_base::host_path::normalize(path).unwrap()
}

fn scratch(guest: impl Into<String>) -> Mount {
    Mount {
        guest: guest.into(),
        host: None,
        readonly: false,
        quota_mib: None,
    }
}

fn read(guest: impl Into<String>, host: &Path) -> Mount {
    Mount {
        guest: guest.into(),
        host: Some(host.to_path_buf()),
        readonly: true,
        quota_mib: None,
    }
}

fn sorted(mut mounts: Vec<Mount>) -> Vec<Mount> {
    mounts.sort_by(|a, b| a.guest.cmp(&b.guest));
    mounts
}
fn spelled(path: &Path) -> String {
    gb_environment::guest_path(path).unwrap()
}

fn env<'a>(env: &'a [(String, String)], name: &str) -> Option<&'a str> {
    env.iter().find(|(n, _)| n == name).map(|(_, v)| v.as_str())
}

#[test]
fn host_debug_levels_become_valid_python_logging_names() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/get_os_ui_language.py");
    let run = command(&machine, path, vec![]);
    let debug = machine.workspace.join(".guildbotics/local/debug.env");
    for (input, expected) in [
        ("TRACE", "DEBUG"),
        ("WARN", "WARNING"),
        ("FATAL", "CRITICAL"),
        ("mystery", "INFO"),
        ("\"DEBUG\" # comment", "DEBUG"),
    ] {
        std::fs::write(&debug, format!("LOG_LEVEL={input}\nAGNO_DEBUG=true\n")).unwrap();
        let made = shape(&machine.host, &machine.workspace, &run, None).unwrap();
        assert_eq!(env(&made.env, "LOG_LEVEL"), Some(expected));
        assert_eq!(env(&made.env, "AGNO_DEBUG"), Some("true"));
    }
}

#[test]
fn selected_workflow_crosses_the_vm_boundary_with_its_payload_and_work_identity() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/get_os_ui_language.py");
    let mut command = command(&machine, path, vec![]);
    let invocation = serde_json::json!({
        "command":"workflows/ticket_driven_workflow","person_id":"yuki",
        "source":"routine","trigger_type":"ticket","run_id":"trace-1",
        "payload":{"ticket_url":"local://tickets/90","max_completion_attempts":5},
        "work":{"kind":"ticket","identity":"local://tickets/90"}
    });
    command.invocation = Some(serde_json::from_value(invocation.clone()).unwrap());
    command.wants_result = true;
    let made = shape(
        &machine.host,
        &machine.workspace,
        &command,
        Some("ja".into()),
    )
    .unwrap();
    assert_eq!(
        serde_json::to_value(made.request.invocation.unwrap()).unwrap(),
        invocation
    );
    assert!(made.request.wants_result);
}

#[test]
fn a_read_only_command_reads_and_works_on_its_own() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/get_os_ui_language.py");
    let run = command(&machine, path, vec![]);
    let made = shape(
        &machine.host,
        &machine.workspace,
        &run,
        Some("ja_JP".into()),
    )
    .unwrap();
    assert!(
        made.mounts
            .windows(2)
            .all(|pair| Path::new(&pair[0].guest).components().count()
                <= Path::new(&pair[1].guest).components().count())
    );
    let cwd = spelled(&named(&machine.workspace.join("somewhere")));
    let config = named(&machine.workspace.join(".guildbotics/config"));
    let members = named(
        &machine
            .host
            .identity_root
            .join(".guildbotics/config/team/members"),
    );
    assert_eq!(
        sorted(made.mounts.clone()),
        sorted(
            vec![
                scratch(cwd.clone()),
                read(
                    "/opt/guildbotics/code/guildbotics",
                    &named(&machine.host.package)
                ),
                // The run's copy of the configuration, at the configuration's place.
                read(spelled(&config), &run.config.path()),
                read(spelled(&members), &members),
            ]
            .into_iter()
            .collect()
        )
    );
    assert_eq!(made.cwd, cwd);
    assert_eq!(made.request.cwd, cwd);
    assert_eq!(
        made.request.path,
        "/opt/guildbotics/code/guildbotics/templates/commands/functions/get_os_ui_language.py"
    );
    assert_eq!(made.request.name, "functions/get_os_ui_language");
    assert_eq!(made.request.args, vec!["a=1".to_string()]);
    assert_eq!(made.request.pipe, "input");
    assert_eq!(made.request.invocation, None);
    assert!(!made.request.wants_result);

    let facts: CommandFacts =
        serde_json::from_str(env(&made.env, "GUILDBOTICS_COMMAND").unwrap()).unwrap();
    assert_eq!(
        facts.mounts,
        HashMap::from([
            (cwd.clone(), true),
            ("/opt/guildbotics/code/guildbotics".into(), false),
            (spelled(&config), false),
            (spelled(&members), false),
        ])
    );
    assert_eq!(facts.inspected, HashMap::new());
    assert_eq!(
        (facts.run_id.as_str(), facts.trace_id.as_str()),
        ("trace-1", "trace-1")
    );
    assert_eq!(
        (facts.work_kind.as_str(), facts.work_identity.as_str()),
        ("manual", "trace-1")
    );
    assert_eq!(facts.person_id, "yuki");
    assert!(facts.access.read_only);

    // The microVM's home is its own, never the host's.
    assert_eq!(env(&made.env, "HOME"), Some(gb_environment::GUEST_HOME));
    assert_eq!(env(&made.env, "LANGUAGE"), Some("ja_JP"));
    assert_eq!(
        env(&made.env, "GUILDBOTICS_WORKSPACE_ROOT"),
        Some(spelled(&named(&machine.workspace)).as_str())
    );
    assert_eq!(
        env(&made.env, "GUILDBOTICS_CONFIG_DIR"),
        Some(spelled(&config).as_str())
    );
    assert_eq!(
        env(&made.env, "GUILDBOTICS_IDENTITY_ROOT"),
        Some(spelled(&named(&machine.host.identity_root)).as_str())
    );
}

#[test]
fn an_inspecting_command_is_told_where_to_look() {
    let machine = machine();
    let path = machine
        .workspace
        .join(".guildbotics/config/commands/look.md");
    let mut inspecting = command(
        &machine,
        path.clone(),
        vec![InspectsItem::Diagnostics, InspectsItem::Config],
    );
    // The host resolved the command in the run's copy of the configuration.
    let copy = inspecting.config.path();
    inspecting.path = copy.join("commands/look.md");
    let made = shape(&machine.host, &machine.workspace, &inspecting, None).unwrap();
    let run = named(&machine.workspace.join(".guildbotics/local/run"));
    let config = named(&machine.workspace.join(".guildbotics/config"));
    let members = named(
        &machine
            .host
            .identity_root
            .join(".guildbotics/config/team/members"),
    );
    // A command of the copy is where the configuration's own is.
    assert_eq!(made.request.path, spelled(&named(&path)));
    let expected = vec![
        scratch(spelled(&named(&machine.workspace.join("somewhere")))),
        read(
            "/opt/guildbotics/code/guildbotics",
            &named(&machine.host.package),
        ),
        read(spelled(&config), &copy),
        read(spelled(&members), &members),
        read(spelled(&run), &run),
    ]
    .into_iter()
    .collect::<Vec<_>>();
    assert_eq!(sorted(made.mounts.clone()), sorted(expected));
    let facts: CommandFacts =
        serde_json::from_str(env(&made.env, "GUILDBOTICS_COMMAND").unwrap()).unwrap();
    assert_eq!(
        facts.inspected,
        HashMap::from([
            ("config".into(), spelled(&config)),
            ("identities".into(), spelled(&members)),
            ("diagnostics".into(), spelled(&run)),
            (
                "templates".into(),
                "/opt/guildbotics/code/guildbotics/templates".into()
            ),
        ])
    );
    // The host's UI language that is not known is not told.
    assert_eq!(env(&made.env, "LANGUAGE"), None);
}

#[test]
fn what_is_not_there_is_not_mounted() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/x.py");
    // Taking the copy of the configuration takes the workspace's write lock,
    // whose file is in `local/run`: gone after, nothing is there to inspect.
    let run = command(&machine, path, vec![InspectsItem::Diagnostics]);
    std::fs::remove_dir_all(machine.workspace.join(".guildbotics/local/run")).unwrap();
    let made = shape(&machine.host, &machine.workspace, &run, None).unwrap();
    let facts: CommandFacts =
        serde_json::from_str(env(&made.env, "GUILDBOTICS_COMMAND").unwrap()).unwrap();
    assert_eq!(made.mounts.len(), 4);
    assert_eq!(facts.inspected, HashMap::new());
}

#[test]
fn what_is_bound_over_the_working_directory_is_what_is_there() {
    let machine = machine();
    let config = named(&machine.workspace.join(".guildbotics/config"));
    let mut run = command(
        &machine,
        machine
            .host
            .package
            .join("templates/commands/functions/x.py"),
        vec![],
    );
    run.cwd = config.clone();
    let made = shape(&machine.host, &machine.workspace, &run, None).unwrap();
    let at_cwd: Vec<&Mount> = made
        .mounts
        .iter()
        .filter(|m| m.guest == spelled(&config))
        .collect();
    assert_eq!(at_cwd, [&read(spelled(&config), &run.config.path())]);
    let facts: CommandFacts =
        serde_json::from_str(env(&made.env, "GUILDBOTICS_COMMAND").unwrap()).unwrap();
    assert!(!facts.mounts[&spelled(&config)]);
}

#[test]
fn copied_worktree_is_admitted_as_workable_in_command_facts() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/get_os_ui_language.py");
    let mut request = command(&machine, path, vec![]);
    request.access.read_only = false;
    std::fs::create_dir_all(&request.cwd).unwrap();
    let made = shape(&machine.host, &machine.workspace, &request, None).unwrap();
    let copy = made.copy.as_ref().expect("ungranted cwd must use a copy");
    let facts: CommandFacts =
        serde_json::from_str(env(&made.env, "GUILDBOTICS_COMMAND").unwrap()).unwrap();
    assert_eq!(facts.mounts.get(&copy.guest), Some(&true));
    assert_eq!(copy.guest, made.cwd);
    let mount = made.mounts.iter().find(|m| m.guest == made.cwd).unwrap();
    assert!(mount.host.is_none());
    assert!(!mount.readonly);
    assert_eq!(mount.quota_mib, Some(16 * 1024));
}

#[test]
fn guest_paths_keep_posix_spelling_and_map_windows_drives() {
    for (path, guest) in [
        ("/Users/x/repo", "/Users/x/repo"),
        ("/Users/x/./repo", "/Users/x/./repo"),
        (r"C:\tmp\gb\repo", "/c/tmp/gb/repo"),
        (r"d:\", "/d"),
        ("C:/work", "/c/work"),
        (r"D:\Work Area\資料", "/d/Work Area/資料"),
    ] {
        assert_eq!(gb_environment::guest_path(Path::new(path)).unwrap(), guest);
    }
}

#[test]
fn host_paths_never_reach_what_the_microvm_keeps_for_itself() {
    for path in [
        "/",
        "/opt",
        "/opt/guildbotics",
        "/opt/guildbotics/home",
        "/opt/guildbotics/home/work",
        "/opt/guildbotics/venv/",
        "/opt//guildbotics/home",
        "/opt/./guildbotics",
        "/run",
        "/run/guildbotics/resolved",
    ] {
        assert!(
            gb_environment::guest_path(Path::new(path)).is_err(),
            "{path} is admitted"
        );
    }
    for path in ["/opt/homebrew", "/opt/guildbotics-other", "/runner"] {
        assert_eq!(gb_environment::guest_path(Path::new(path)).unwrap(), path);
    }
}

#[test]
fn a_command_cannot_work_in_what_the_microvm_keeps_for_itself() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/x.py");
    for read_only in [true, false] {
        let mut request = command(&machine, path.clone(), vec![]);
        request.access.read_only = read_only;
        request.cwd = PathBuf::from("/opt/guildbotics/home");
        let Err(failure) = shape(&machine.host, &machine.workspace, &request, None) else {
            panic!("a working directory over the microVM's home is admitted");
        };
        let gb_environment::Sentence::Text(text) = failure.sentence() else {
            panic!("the refusal says no reason");
        };
        assert!(text.contains("/opt/guildbotics"), "{text}");
    }
}

#[test]
fn relative_and_network_paths_have_no_guest_path() {
    for path in [
        "relative",
        "",
        "C:relative",
        r"\work",
        r"\\srv\share\x",
        "//srv/share/x",
        r"\\?\C:\work",
        r"\\.\device",
    ] {
        assert!(
            gb_environment::guest_path(Path::new(path)).is_err(),
            "{path}"
        );
    }
}

#[test]
fn shape_keeps_guest_spelling_through_plan_copy_and_packaged_command_paths() {
    let machine = machine();
    let package_command = machine
        .host
        .package
        .join("templates/commands/functions/x.py");
    for read_only in [true, false] {
        let mut request = command(&machine, package_command.clone(), vec![]);
        request.access.read_only = read_only;
        std::fs::create_dir_all(&request.cwd).unwrap();
        let made = shape(&machine.host, &machine.workspace, &request, None).unwrap();
        let source = gb_base::host_path::normalize(&request.cwd).unwrap();
        let guest = gb_environment::guest_path(&source).unwrap();
        assert_eq!(made.cwd, guest);
        assert_eq!(made.request.cwd, guest);
        assert_eq!(
            made.request.path,
            format!(
                "{}/templates/commands/functions/x.py",
                gb_environment::CODE_ROOT
            )
        );
        assert_eq!(env(&made.env, "HOME"), Some(gb_environment::GUEST_HOME));
        if read_only {
            assert!(
                made.mounts
                    .iter()
                    .any(|m| m.guest == guest && m.host.is_none())
            );
        } else {
            let copy = made.copy.unwrap();
            assert_eq!(copy.host, source);
            assert_eq!(copy.guest, guest);
        }
        #[cfg(windows)]
        {
            assert!(!made.cwd.contains('\\'));
            assert!(!made.cwd.contains(':'));
        }
    }
}

#[test]
fn only_the_clone_mount_lets_the_guest_add_more_than_the_runtime_default() {
    let machine = machine();
    let clones = machine.workspace.join(".guildbotics/local/clones/yuki");
    let clone = clones.join("a".repeat(64));
    std::fs::create_dir_all(&clone).unwrap();
    for granted in ["Documents/read", "Documents/write"] {
        std::fs::create_dir_all(machine.host.home.join(granted)).unwrap();
    }
    let grants = machine
        .workspace
        .join(".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml");
    std::fs::create_dir_all(grants.parent().unwrap()).unwrap();
    std::fs::write(
        &grants,
        "documents:\n  - path: Documents/read\n    access: read\n  - path: Documents/write\n    access: read_write\n",
    )
    .unwrap();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/get_os_ui_language.py");
    let clone_guest = spelled(&named(&clones));
    let home = |relative: &str| spelled(&named(&machine.host.home.join(relative)));
    for read_only in [true, false] {
        let mut request = command(
            &machine,
            path.clone(),
            vec![InspectsItem::Config, InspectsItem::Diagnostics],
        );
        request.access.read_only = read_only;
        request.cwd = clone.clone();
        let made = shape(&machine.host, &machine.workspace, &request, None).unwrap();
        // Every kind of mount is there: the code, what the command inspects,
        // the grants of either access, the clone, and
        // a read-only command's empty working directory.
        let mut kinds = vec![
            "/opt/guildbotics/code/guildbotics".to_string(),
            spelled(&named(&machine.workspace.join(".guildbotics/config"))),
            spelled(&named(&machine.workspace.join(".guildbotics/local/run"))),
            home("Documents/read"),
            home("Documents/write"),
            clone_guest.clone(),
        ];
        let sccache = spelled(&named(&gb_environment::contract::sccache_dir(
            &machine.workspace,
            "yuki",
        )));
        if read_only {
            kinds.push(spelled(&named(&clone)));
        } else {
            kinds.push(sccache.clone());
        }
        for guest in &kinds {
            assert!(
                made.mounts.iter().any(|m| &m.guest == guest),
                "{guest} is not mounted ({read_only})"
            );
        }
        // `~` reaches each grant under the host's home through a link of
        // the same name; the microVM's home is never a mount.
        assert_eq!(
            made.links,
            ["Documents/read", "Documents/write"].map(|name| {
                gb_environment::contract::Link {
                    name: name.into(),
                    target: home(name),
                }
            })
        );
        assert!(
            made.mounts
                .iter()
                .all(|m| !m.guest.starts_with(gb_environment::GUEST_HOME))
        );
        for mount in &made.mounts {
            let expected = if mount.guest == clone_guest {
                Some(gb_environment::contract::CLONE_QUOTA_MIB)
            } else if mount.guest == sccache {
                Some(gb_environment::contract::SCCACHE_QUOTA_MIB)
            } else {
                None
            };
            assert_eq!(mount.quota_mib, expected, "{mount:?}");
        }
    }
    assert_eq!(gb_environment::contract::CLONE_QUOTA_MIB, 16 * 1024);
}

#[test]
fn only_a_writable_command_binds_its_own_members_sccache_directory() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/get_os_ui_language.py");
    let caches = named(&machine.workspace.join(".guildbotics/local/build-cache"));
    let cached = |made: &gb_environment::Shape| -> Vec<Mount> {
        made.mounts
            .iter()
            .filter(|m| m.host.as_ref().is_some_and(|h| h.starts_with(&caches)))
            .cloned()
            .collect()
    };
    // The second member's command runs after the first one's directory
    // exists, and still sees only its own.
    for person in ["yuki", "aiko"] {
        let mut request = command(&machine, path.clone(), vec![]);
        request.person_id = person.into();
        request.access.read_only = false;
        std::fs::create_dir_all(&request.cwd).unwrap();
        let made = shape(&machine.host, &machine.workspace, &request, None).unwrap();
        let own = caches.join(person).join("sccache");
        assert!(own.is_dir(), "{person}");
        assert_eq!(
            cached(&made),
            [Mount {
                guest: spelled(&own),
                host: Some(own.clone()),
                readonly: false,
                quota_mib: Some(gb_environment::contract::SCCACHE_QUOTA_MIB),
            }],
            "{person}"
        );
        assert_eq!(env(&made.env, "SCCACHE_DIR"), Some(spelled(&own).as_str()));
        // GuildBotics binds it for the builds; work does not happen there.
        let facts: CommandFacts =
            serde_json::from_str(env(&made.env, "GUILDBOTICS_COMMAND").unwrap()).unwrap();
        assert_eq!(facts.mounts.get(&spelled(&own)), Some(&false));

        // A read-only command's sccache keeps its cache on the microVM's disk.
        request.access.read_only = true;
        let read = shape(&machine.host, &machine.workspace, &request, None).unwrap();
        assert_eq!(cached(&read), [], "{person}");
        assert_eq!(env(&read.env, "SCCACHE_DIR"), None);
    }
}

#[test]
fn a_private_run_cwd_mounts_only_its_own_directory() {
    let machine = machine();
    let id = "a".repeat(32);
    let other_lease =
        gb_environment::run_files::prepare(&machine.host.data, &"b".repeat(32)).unwrap();
    let lease = gb_environment::run_files::prepare(&machine.host.data, &id).unwrap();
    let cwd = lease.path.clone();
    let other = other_lease.path.clone();
    let mut request = command(
        &machine,
        machine
            .host
            .package
            .join("templates/commands/functions/work.py"),
        vec![],
    );
    request.run_id = id;
    request.cwd = cwd.clone();
    request.access.read_only = false;
    let made = shape(&machine.host, &machine.workspace, &request, None).unwrap();
    assert!(made.copy.is_none());
    assert!(
        made.mounts
            .iter()
            .any(|m| m.host.as_ref() == Some(&named(&cwd)) && !m.readonly)
    );
    assert!(
        !made
            .mounts
            .iter()
            .any(|m| m.host.as_ref().is_some_and(|p| other.starts_with(p)))
    );
    assert!(!machine.host.home.join("Documents/GuildBotics").exists());
    request.access.read_only = true;
    let read = shape(&machine.host, &machine.workspace, &request, None).unwrap();
    assert!(
        !read
            .mounts
            .iter()
            .any(|m| m.host.as_ref() == Some(&named(&cwd)))
    );
}

#[cfg(unix)]
#[test]
fn remote_copy_keeps_the_client_name_even_when_the_hub_has_a_symlink_there() {
    let machine = machine();
    let alias = named(&machine.host.home).join("client-name");
    std::os::unix::fs::symlink(&machine.workspace, &alias).unwrap();
    let mut run = command(
        &machine,
        machine
            .host
            .package
            .join("templates/commands/functions/probe.py"),
        vec![],
    );
    run.client_copy = true;
    run.remote_copy = true;
    run.cwd = alias.clone();
    let result = shape(&machine.host, &machine.workspace, &run, None).unwrap();
    assert_eq!(result.cwd, alias.to_str().unwrap());
    assert!(
        result
            .mounts
            .iter()
            .any(|mount| mount.guest == alias.to_str().unwrap() && mount.host.is_none())
    );
}
