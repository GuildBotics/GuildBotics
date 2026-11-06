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
    let missing = dir.path().join("missing");
    std::os::unix::fs::symlink(&missing, dir.path().join("broken")).unwrap();
    assert_eq!(
        resolved(&dir.path().join("broken/child")).unwrap(),
        missing.join("child")
    );
    std::os::unix::fs::symlink("cycle", dir.path().join("cycle")).unwrap();
    assert!(resolved(&dir.path().join("cycle")).is_err());
    fs::write(dir.path().join("regular"), "file").unwrap();
    for (link, target) in [("bad-parent", "regular/.."), ("bad-directory", "regular/")] {
        std::os::unix::fs::symlink(target, dir.path().join(link)).unwrap();
        assert_eq!(
            resolved(&dir.path().join(link)).unwrap_err().kind(),
            std::io::ErrorKind::NotADirectory
        );
    }
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

/// Every way a writer changes a file, applied to `path` (a directory
/// `tree` for the removal of one).
#[allow(clippy::type_complexity)]
fn every_change<'a>(
    path: &'a Path,
    tree: &'a Path,
) -> Vec<(
    &'static str,
    Box<dyn Fn(&mut Writer<'_>) -> anyhow::Result<()> + 'a>,
)> {
    vec![
        ("write_bytes", Box::new(move |w| w.write_bytes(path, b"x"))),
        ("write_text", Box::new(move |w| w.write_text(path, "x"))),
        ("write_json", Box::new(move |w| w.write_json(path, &1))),
        ("delete", Box::new(move |w| w.delete(path))),
        ("remove_tree", Box::new(move |w| w.remove_tree(tree))),
        ("append_text", Box::new(move |w| w.append_text(path, "x"))),
        (
            "update_text",
            Box::new(move |w| w.update_text(path, |_| Ok(Some("x".into()))).map(drop)),
        ),
        (
            "update_json",
            Box::new(move |w| {
                w.update_json(path, |_| Ok(Some(serde_json::json!(1))))
                    .map(drop)
            }),
        ),
    ]
}

#[test]
fn every_change_waits_for_the_other_writer_and_succeeds_alone() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let workspace = writes.workspace.clone().unwrap();
    let path = workspace.join(".guildbotics/state/a.json");
    let tree = workspace.join(".guildbotics/state/tree");
    let local = workspace.join(".guildbotics/local/a");
    {
        let _held = FileLock::hold(
            &writes
                .identity
                .join(".guildbotics/local/run/shared-write.lock"),
            Duration::ZERO,
        )
        .unwrap();
        for (name, change) in every_change(&path, &tree) {
            let called = std::cell::Cell::new(false);
            let refused = writes
                .at(&path, |w| {
                    called.set(true);
                    change(w)
                })
                .unwrap_err();
            assert!(refused.is::<SharedWriteBusyError>(), "{name}");
            assert!(
                !called.get(),
                "{name}: nothing is read or written while waiting"
            );
            // A writer for a device-local path cannot reach a shared one.
            assert!(writes.at(&local, |w| change(w)).is_err(), "{name}");
            assert!(!path.exists() && !tree.exists(), "{name}");
        }
    }
    for (name, change) in every_change(&path, &tree) {
        fs::create_dir_all(tree.join("child")).unwrap();
        fs::write(&path, "1").unwrap();
        writes
            .at(&path, |w| change(w))
            .unwrap_or_else(|e| panic!("{name}: {e:#}"));
    }
    writes
        .at(&path, |w| {
            w.write_text(&path, "a")?;
            w.update_text(&path, |_| Ok(None))
        })
        .unwrap();
    assert!(
        !path.exists(),
        "a rewrite answering nothing removes the file"
    );
}

#[test]
fn a_shared_target_under_a_missing_parent_stays_locked_and_is_created_after() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let workspace = writes.workspace.clone().unwrap();
    let path = workspace.join(".guildbotics/state/new/thing.json");
    assert_eq!(
        shared_relative_path(&path, Some(&workspace))
            .unwrap()
            .unwrap(),
        Path::new("state/new/thing.json")
    );
    {
        let _held = FileLock::hold(
            &writes
                .identity
                .join(".guildbotics/local/run/shared-write.lock"),
            Duration::ZERO,
        )
        .unwrap();
        assert!(
            writes
                .at(&path, |w| w.write_json(&path, &1))
                .unwrap_err()
                .is::<SharedWriteBusyError>()
        );
        assert!(!path.parent().unwrap().exists());
    }
    writes.at(&path, |w| w.write_json(&path, &1)).unwrap();
    writes
        .at(&path, |w| {
            w.update_json(&path, |old| {
                Ok(Some(serde_json::json!(old.unwrap().as_u64().unwrap() + 1)))
            })
        })
        .unwrap();
    assert_eq!(fs::read_to_string(&path).unwrap(), "2\n");
}

#[cfg(unix)]
#[test]
fn an_unreadable_ancestor_or_file_stops_before_writing() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let state = writes.workspace.clone().unwrap().join(".guildbotics/state");
    fs::create_dir_all(state.join("locked")).unwrap();
    fs::write(state.join("regular"), "file").unwrap();
    fs::set_permissions(state.join("locked"), fs::Permissions::from_mode(0o000)).unwrap();
    for (path, kind) in [
        (
            state.join("locked/child/a.json"),
            std::io::ErrorKind::PermissionDenied,
        ),
        (
            state.join("regular/a.json"),
            std::io::ErrorKind::NotADirectory,
        ),
    ] {
        let error = writes.at(&path, |w| w.write_json(&path, &1)).unwrap_err();
        assert!(
            !error.is::<SharedWriteBusyError>(),
            "{}: {error:#}",
            path.display()
        );
        let io = error
            .chain()
            .find_map(|e| e.downcast_ref::<std::io::Error>())
            .unwrap_or_else(|| panic!("{error:#}"));
        assert_eq!(io.kind(), kind, "{}", path.display());
    }
    fs::set_permissions(state.join("locked"), fs::Permissions::from_mode(0o755)).unwrap();
    assert!(!state.join("locked/child").exists());
    let unreadable = state.join("unreadable.json");
    fs::write(&unreadable, "{}").unwrap();
    fs::set_permissions(&unreadable, fs::Permissions::from_mode(0o000)).unwrap();
    let refused = writes.at(&unreadable, |w| {
        assert!(
            w.update_text(&unreadable, |_| panic!("apply must not run"))
                .is_err()
        );
        assert!(
            w.update_json(&unreadable, |_| panic!("apply must not run"))
                .is_err()
        );
        Ok(())
    });
    fs::set_permissions(&unreadable, fs::Permissions::from_mode(0o644)).unwrap();
    refused.unwrap();
    assert_eq!(fs::read_to_string(&unreadable).unwrap(), "{}");
}

#[cfg(unix)]
#[test]
fn a_dangling_link_to_a_shared_file_keeps_the_lock() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let state = writes.workspace.clone().unwrap().join(".guildbotics/state");
    fs::create_dir_all(&state).unwrap();
    let alias = dir.path().join("alias.jsonl");
    std::os::unix::fs::symlink(state.join("new.jsonl"), &alias).unwrap();
    assert!(
        shared_relative_path(&alias, writes.workspace.as_deref())
            .unwrap()
            .is_some()
    );
    {
        let _held = FileLock::hold(
            &writes
                .identity
                .join(".guildbotics/local/run/shared-write.lock"),
            Duration::ZERO,
        )
        .unwrap();
        assert!(
            writes
                .at(&alias, |w| w.append_text(&alias, "{}\n"))
                .unwrap_err()
                .is::<SharedWriteBusyError>()
        );
        assert!(!state.join("new.jsonl").exists());
    }
    writes
        .at(&alias, |w| w.append_text(&alias, "{}\n"))
        .unwrap();
    assert_eq!(fs::read_to_string(state.join("new.jsonl")).unwrap(), "{}\n");
}
