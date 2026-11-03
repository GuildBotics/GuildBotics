use gb_base::{
    files::{self, FileLock},
    shared_writes::*,
};
use std::{fs, path::Path, time::Duration};

fn writes(root: &Path) -> SharedWrites {
    let mut writes = SharedWrites::new(root.join("identity"), Some(root.join("workspace")));
    writes.timeout = Duration::from_millis(40);
    writes
}

#[test]
fn local_and_unselected_files_do_not_wait_but_shared_files_do() {
    let dir = tempfile::tempdir().unwrap();
    let mut writes = writes(dir.path());
    let lockpath = writes
        .identity
        .join(".guildbotics/local/run/shared-write.lock");
    let _held = FileLock::hold(&lockpath, Duration::ZERO).unwrap();
    for path in [
        writes
            .workspace
            .as_ref()
            .unwrap()
            .join(".guildbotics/local/a"),
        dir.path().join("elsewhere/a"),
    ] {
        writes.at(&path, |w| w.write_text(&path, "local")).unwrap();
    }
    let path = writes
        .workspace
        .as_ref()
        .unwrap()
        .join(".guildbotics/config/a");
    let local = dir.path().join("elsewhere/a");
    assert!(
        writes
            .at(&local, |w| w.write_text(&path, "bypass"))
            .is_err()
    );
    assert!(
        writes
            .at(&path, |w| w.write_text(&path, "shared"))
            .unwrap_err()
            .is::<SharedWriteBusyError>()
    );
    assert!(!path.exists());
    assert!(
        !writes
            .workspace
            .as_ref()
            .unwrap()
            .join(".guildbotics/local/run/shared-write.lock")
            .exists(),
        "the identity lock must be acquired before attempting the workspace lock"
    );
    writes.workspace = None;
    writes
        .at(&path, |w| w.write_text(&path, "unselected"))
        .unwrap();
    let identity = writes.identity.join(".guildbotics/config/a");
    assert!(
        writes
            .at(&identity, |_| Ok(()))
            .unwrap_err()
            .is::<SharedWriteBusyError>()
    );
}

#[test]
fn lock_order_scope_and_release_are_explicit() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let root = writes.workspace.as_ref().unwrap();
    let held = FileLock::hold(
        &root.join(".guildbotics/local/run/shared-write.lock"),
        Duration::ZERO,
    )
    .unwrap();
    assert!(
        writes
            .transaction(|_| Ok(()))
            .unwrap_err()
            .is::<SharedWriteBusyError>()
    );
    // Failure while acquiring the second lock releases the first.
    let mut identity = IdentityLock::acquire(&writes.identity, Duration::ZERO).unwrap();
    drop(held);
    let mut workspace = identity.workspace(root, Duration::ZERO).unwrap();
    assert!(
        workspace
            .writer()
            .write_text(&dir.path().join("wrong/config/a"), "x")
            .is_err()
    );
    let path = root.join(".guildbotics/state/a");
    workspace.writer().write_text(&path, "x").unwrap();
    drop(workspace);
    // Equal identity/workspace roots reuse the capability without relocking.
    identity
        .workspace(&writes.identity, Duration::ZERO)
        .unwrap();
}

#[test]
fn closure_updates_preserve_every_concurrent_increment_and_delete() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let path = root.join("workspace/.guildbotics/state/count.json");
    std::thread::scope(|scope| {
        for _ in 0..4 {
            let path = &path;
            scope.spawn(move || {
                let mut writes = writes(root);
                writes.timeout = Duration::from_secs(3);
                for _ in 0..8 {
                    writes
                        .at(path, |w| {
                            w.update_json(path, |old| {
                                let n = old.and_then(|v| v.as_u64()).unwrap_or(0);
                                std::thread::yield_now();
                                Ok(Some(serde_json::json!(n + 1)))
                            })
                        })
                        .unwrap();
                }
            });
        }
    });
    assert_eq!(fs::read_to_string(&path).unwrap(), "32\n");
    let writes = writes(root);
    writes
        .at(&path, |w| {
            #[cfg(unix)]
            let inode = {
                use std::os::unix::fs::MetadataExt;
                fs::metadata(&path)?.ino()
            };
            w.update_text(&path, |old| {
                assert_eq!(old, Some("32\n"));
                Ok(Some("32\n".into()))
            })?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                assert_eq!(
                    fs::metadata(&path)?.ino(),
                    inode,
                    "identical text must not be replaced"
                );
            }
            w.update_json(&path, |_| Ok(None))?;
            w.append_text(&path, "a")?;
            w.append_text(&path, "b")?;
            assert_eq!(fs::read_to_string(&path)?, "ab");
            w.delete(&path)
        })
        .unwrap();
    assert!(!path.exists());
}

#[test]
fn invalid_json_and_closure_failure_leave_the_original_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let path = writes
        .workspace
        .as_ref()
        .unwrap()
        .join(".guildbotics/state/a");
    writes
        .at(&path, |w| {
            w.write_text(&path, "bad json")?;
            assert!(
                w.update_json(&path, |_| panic!("invalid JSON must not call apply"))
                    .is_err()
            );
            assert!(
                w.update_text(&path, |_| anyhow::bail!("apply failed"))
                    .is_err()
            );
            assert_eq!(fs::read_to_string(&path)?, "bad json");
            Ok(())
        })
        .unwrap();
}

#[test]
fn process_contender() {
    let Some(root) = std::env::var_os("GB_TEST_SHARED_LOCK_ROOT") else {
        return;
    };
    let writes = writes(Path::new(&root));
    assert!(
        writes
            .transaction(|_| Ok(()))
            .unwrap_err()
            .is::<SharedWriteBusyError>()
    );
}

#[test]
fn another_process_observes_the_same_lock() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let _identity = IdentityLock::acquire(&writes.identity, Duration::ZERO).unwrap();
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "process_contender", "--nocapture"])
        .env("GB_TEST_SHARED_LOCK_ROOT", dir.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
}

#[cfg(unix)]
#[test]
fn existing_symlink_ancestors_decide_the_lock_scope() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let workspace = writes.workspace.as_ref().unwrap();
    fs::create_dir_all(workspace.join(".guildbotics/config")).unwrap();
    std::os::unix::fs::symlink(
        workspace.join(".guildbotics/config"),
        dir.path().join("alias"),
    )
    .unwrap();
    let path = dir.path().join("alias/new/a");
    assert_eq!(
        shared_relative_path(&dir.path().join("alias/missing/../new/a"), Some(workspace))
            .unwrap()
            .unwrap(),
        Path::new("config/new/a")
    );
    assert_eq!(
        shared_relative_path(&path, Some(workspace))
            .unwrap()
            .unwrap(),
        Path::new("config/new/a")
    );
    let _held = FileLock::hold(
        &workspace.join(".guildbotics/local/run/shared-write.lock"),
        Duration::ZERO,
    )
    .unwrap();
    assert!(
        writes
            .at(&path, |_| Ok(()))
            .unwrap_err()
            .is::<SharedWriteBusyError>()
    );
    assert_eq!(
        files::dump_shared_json(&serde_json::json!({"z":{"b":1,"a":"名"},"a":0})),
        "{\n  \"a\": 0,\n  \"z\": {\n    \"a\": \"名\",\n    \"b\": 1\n  }\n}\n"
    );
}
