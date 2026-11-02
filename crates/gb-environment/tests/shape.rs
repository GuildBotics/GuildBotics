//! A read-only command's microVM works on nothing of the host's, reads the
//! code, the configuration and the identities read-only, and is told the
//! command as the Python version's entry reads it
//! (`guildbotics/environment/command_environment.py`, `host_window.py`).

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use gb_environment::{Command, Host, Mount, shape};
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

fn spelled(path: &Path) -> String {
    path.to_str().unwrap().to_owned()
}

fn env<'a>(env: &'a [(String, String)], name: &str) -> Option<&'a str> {
    env.iter().find(|(n, _)| n == name).map(|(_, v)| v.as_str())
}

#[test]
fn a_read_only_command_reads_and_works_on_its_own() {
    let machine = machine();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/get_os_ui_language.py");
    let made = shape(
        &machine.host,
        &machine.workspace,
        &command(&machine, path, vec![]),
        Some("ja_JP".into()),
    )
    .unwrap();
    let cwd = spelled(&named(&machine.workspace.join("somewhere")));
    let config = named(&machine.workspace.join(".guildbotics/config"));
    let members = named(
        &machine
            .host
            .identity_root
            .join(".guildbotics/config/team/members"),
    );
    assert_eq!(
        made.mounts,
        vec![
            Mount {
                guest: cwd.clone(),
                host: None,
                readonly: false
            },
            Mount {
                guest: "/opt/guildbotics/code/guildbotics".into(),
                host: Some(named(&machine.host.package)),
                readonly: true
            },
            Mount {
                guest: spelled(&config),
                host: Some(config.clone()),
                readonly: true
            },
            Mount {
                guest: spelled(&members),
                host: Some(members.clone()),
                readonly: true
            },
        ]
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

    assert_eq!(
        env(&made.env, "HOME"),
        Some(spelled(&named(&machine.host.home)).as_str())
    );
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
    assert!(env(&made.env, "GUILDBOTICS_HOST_URL").is_some());
    assert!(env(&made.env, "GUILDBOTICS_HOST_TOKEN").is_some());
}

#[test]
fn an_inspecting_command_is_told_where_to_look() {
    let machine = machine();
    let path = machine
        .workspace
        .join(".guildbotics/config/commands/look.md");
    let made = shape(
        &machine.host,
        &machine.workspace,
        &command(
            &machine,
            path.clone(),
            vec![InspectsItem::Diagnostics, InspectsItem::Config],
        ),
        None,
    )
    .unwrap();
    let run = named(&machine.workspace.join(".guildbotics/local/run"));
    let config = named(&machine.workspace.join(".guildbotics/config"));
    let members = named(
        &machine
            .host
            .identity_root
            .join(".guildbotics/config/team/members"),
    );
    // A workspace command is where it is on the host.
    assert_eq!(made.request.path, spelled(&named(&path)));
    assert_eq!(
        made.mounts
            .iter()
            .skip(2)
            .map(|m| m.guest.clone())
            .collect::<Vec<_>>(),
        vec![spelled(&config), spelled(&members), spelled(&run)]
    );
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
    std::fs::remove_dir_all(machine.workspace.join(".guildbotics/local/run")).unwrap();
    let path = machine
        .host
        .package
        .join("templates/commands/functions/x.py");
    let made = shape(
        &machine.host,
        &machine.workspace,
        &command(&machine, path, vec![InspectsItem::Diagnostics]),
        None,
    )
    .unwrap();
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
    assert_eq!(
        at_cwd,
        [&Mount {
            guest: spelled(&config),
            host: Some(config.clone()),
            readonly: true
        }]
    );
    let facts: CommandFacts =
        serde_json::from_str(env(&made.env, "GUILDBOTICS_COMMAND").unwrap()).unwrap();
    assert!(!facts.mounts[&spelled(&config)]);
}
