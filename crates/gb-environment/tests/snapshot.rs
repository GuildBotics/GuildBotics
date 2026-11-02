//! A command boots from the workspace's only snapshot, and from none while a
//! build runs or after one failed, in the Python version's words.

use std::path::Path;

use gb_base::files::FileLock;
use gb_environment::Sentence;
use gb_environment::snapshot::{ready, snapshots_dir};

fn key(refusal: Result<std::path::PathBuf, Sentence>) -> &'static str {
    match refusal {
        Err(Sentence::Key(key, _)) => key.rsplit('.').next().unwrap(),
        other => panic!("{other:?}"),
    }
}

fn snapshot(workspace: &Path, name: &str) {
    std::fs::create_dir_all(snapshots_dir(workspace).join(name)).unwrap();
}

#[test]
fn the_only_snapshot_is_the_one() {
    let workspace = tempfile::tempdir().unwrap();
    assert_eq!(key(ready(workspace.path())), "missing");
    snapshot(workspace.path(), "guildbotics-a");
    // Not a snapshot: another name, a file.
    snapshot(workspace.path(), "other");
    std::fs::write(snapshots_dir(workspace.path()).join("guildbotics-b"), "").unwrap();
    assert_eq!(
        ready(workspace.path()).unwrap(),
        snapshots_dir(workspace.path()).join("guildbotics-a")
    );
    snapshot(workspace.path(), "guildbotics-c");
    assert_eq!(key(ready(workspace.path())), "stale");
}

#[test]
fn a_failed_build_is_refused_with_its_reason() {
    let workspace = tempfile::tempdir().unwrap();
    snapshot(workspace.path(), "guildbotics-a");
    std::fs::write(
        snapshots_dir(workspace.path()).join("guildbotics-b.failed"),
        "no image\n",
    )
    .unwrap();
    assert_eq!(
        ready(workspace.path()),
        Err(Sentence::Key(
            "intelligences.agent_environment.snapshot.failed",
            vec![("detail", "no image".into())]
        ))
    );
}

#[test]
fn a_running_build_is_waited_for() {
    let workspace = tempfile::tempdir().unwrap();
    snapshot(workspace.path(), "guildbotics-a");
    let lock = snapshots_dir(workspace.path()).join("build.lock");
    let held = FileLock::try_hold(&lock).unwrap().unwrap();
    assert_eq!(key(ready(workspace.path())), "building");
    drop(held);
    assert!(ready(workspace.path()).is_ok());
}
