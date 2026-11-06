use gb_base::shared_writes::{IdentityLock, SharedWriteBusyError, SharedWrites};
use gb_config::repository::*;
use std::{fs, path::Path, time::Duration};

fn repository(root: &Path) -> ConfigRepository {
    let mut writes = SharedWrites::new(root.join("identity"), Some(root.join("workspace")));
    writes.timeout = Duration::from_millis(20);
    ConfigRepository::new(writes).unwrap()
}

#[test]
fn git_blob_ids_and_one_call_compare_apply_report() {
    assert_eq!(blob_id(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391");
    assert_eq!(
        blob_id(b"hello\n"),
        "ce013625030ba8dba906f756967f9e9ca394464a"
    );
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let old = repo.revisions(&["team/a.yml"]).unwrap();
    assert_eq!(old["team/a.yml"], "");
    let receipt = repo
        .write(
            &old,
            |view, w| {
                w.write_text(&view.path("team/a.yml")?, "name: 名\n")?;
                Ok(42)
            },
            |view| view.revisions(&["team/a.yml"]),
        )
        .unwrap();
    assert_eq!(receipt.result, 42);
    assert_eq!(
        receipt.revisions["team/a.yml"],
        blob_id("name: 名\n".as_bytes())
    );
    let snapshot = repo.read_config("team/a.yml").unwrap().unwrap();
    assert_eq!(snapshot.content, "name: 名\n");
    assert_eq!(snapshot.relative_path, "config/team/a.yml");
    let error = repo
        .write(
            &old,
            |_, _| -> anyhow::Result<()> { panic!("stale must not write") },
            |_| panic!("stale must not report"),
        )
        .err()
        .unwrap();
    let stale = error.downcast_ref::<StaleConfigWriteError>().unwrap();
    assert_eq!(stale.relative_path, "config/team/a.yml");
    assert_eq!(stale.revisions, receipt.revisions);
}

#[test]
fn directory_membership_and_contents_are_independent_revisions() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let absent = repo.tree_revisions("commands").unwrap();
    assert_eq!(absent["commands/"], blob_id(b""));
    let first = repo
        .write(
            &absent,
            |v, w| w.write_text(&v.path("commands/nested/a.md")?, "a"),
            |v| v.tree_revisions("commands"),
        )
        .unwrap()
        .revisions;
    assert_eq!(first["commands/"], blob_id(b"commands/nested/a.md\n"));
    let second = repo
        .write(
            &first,
            |v, w| w.write_text(&v.path("commands/nested/a.md")?, "b"),
            |v| v.tree_revisions("commands"),
        )
        .unwrap()
        .revisions;
    assert_eq!(first["commands/"], second["commands/"]);
    assert_ne!(
        first["commands/nested/a.md"],
        second["commands/nested/a.md"]
    );
    repo.write(
        &Revisions::new(),
        |v, w| w.write_text(&v.path("commands/new.md")?, "new"),
        |v| v.tree_revisions("commands"),
    )
    .unwrap();
    assert!(
        repo.write(
            &second,
            |_, _| -> anyhow::Result<()> { panic!("must not prune new file") },
            |v| v.tree_revisions("commands")
        )
        .is_err()
    );
}

#[test]
fn receipt_is_observed_while_both_locks_are_held_and_busy_is_not_io() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    repo.write(
        &Revisions::new(),
        |_, _| Ok(()),
        |v| {
            let other = repository(dir.path());
            let error = other
                .write(
                    &Revisions::new(),
                    |_, _| -> anyhow::Result<()> { panic!("locked") },
                    |_| Ok(Revisions::new()),
                )
                .err()
                .unwrap();
            assert!(error.is::<SharedWriteBusyError>());
            assert!(!error.is::<std::io::Error>());
            v.revisions(&["a"])
        },
    )
    .unwrap();
    IdentityLock::acquire(&dir.path().join("identity"), Duration::ZERO).unwrap();
}

#[test]
fn paths_cannot_escape_config_and_io_errors_are_not_contention() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    for name in ["", " ", "../a", "/a", "C:/a", "\\\\server\\a"] {
        assert!(repo.read_config(name).is_err(), "{name}");
    }
    let config = dir.path().join("workspace/.guildbotics/config");
    fs::create_dir_all(&config).unwrap();
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(dir.path(), config.join("outside")).unwrap();
        assert!(repo.read_config("outside/absent").is_err());
        assert!(repo.tree_revisions("outside").is_err());
    }
    fs::write(config.join("file"), "not a directory").unwrap();
    let error = repo.read_config("file/child").err().unwrap();
    assert!(error.is::<std::io::Error>());
    assert!(!error.is::<SharedWriteBusyError>());
}

#[test]
fn only_the_revisions_sent_are_compared_each_in_its_own_root() {
    let dir = tempfile::tempdir().unwrap();
    let repo = repository(dir.path());
    let write = |expected: &Revisions| {
        repo.write_checked(
            expected,
            &["team/members/a/identity.yml"],
            |view, w| w.write_text(&view.path("team/b.yml")?, "b\n"),
            |view| view.revisions(&["team/b.yml"]),
        )
    };
    // Nothing sent compares nothing, whatever exists.
    write(&Revisions::new()).unwrap();
    write(&Revisions::new()).unwrap();
    let identity = dir
        .path()
        .join("identity/.guildbotics/config/team/members/a/identity.yml");
    fs::create_dir_all(identity.parent().unwrap()).unwrap();
    fs::write(&identity, "person_id: a\n").unwrap();
    // An identity path is compared in the identity root, and a refusal
    // names it under config/ as every other one, with the revisions sent.
    let absent = Revisions::from([("team/members/a/identity.yml".to_owned(), String::new())]);
    let stale = write(&absent).err().unwrap();
    let stale = stale.downcast_ref::<StaleConfigWriteError>().unwrap();
    assert_eq!(stale.relative_path, "config/team/members/a/identity.yml");
    assert_eq!(
        stale.revisions,
        Revisions::from([(
            "team/members/a/identity.yml".to_owned(),
            blob_id(b"person_id: a\n")
        )])
    );
    write(&stale.revisions).unwrap();
    for outside in ["../state/workspace.json", "/etc/passwd", ""] {
        let expected = Revisions::from([(outside.to_owned(), "abc".to_owned())]);
        let error = write(&expected).err().unwrap();
        assert!(error.is::<InvalidRevisionPath>(), "{outside}: {error:#}");
    }
}

#[test]
fn identity_config_compares_and_locks_without_a_workspace() {
    let dir = tempfile::tempdir().unwrap();
    let identity = dir.path().join("identity");
    let mut writes = SharedWrites::new(identity.clone(), Some(dir.path().join("unused")));
    writes.timeout = Duration::from_millis(20);
    let repo = ConfigRepository::for_identity(writes);
    let old = repo.revisions(&["connections/branvil.yml"]).unwrap();
    let receipt = repo
        .write(
            &old,
            |view, writer| {
                writer.write_text(
                    &view.path("connections/branvil.yml")?,
                    "base_url: http://127.0.0.1:7878\n",
                )
            },
            |view| view.revisions(&["connections/branvil.yml"]),
        )
        .unwrap();
    assert_ne!(receipt.revisions, old);
    assert!(
        identity
            .join(".guildbotics/config/connections/branvil.yml")
            .is_file()
    );
    assert!(!dir.path().join("unused").exists());
    assert!(
        repo.write(
            &old,
            |_, _| -> anyhow::Result<()> { panic!("stale write") },
            |_| Ok(Revisions::new())
        )
        .unwrap_err()
        .is::<StaleConfigWriteError>()
    );
    let _held = IdentityLock::acquire(&identity, Duration::from_secs(1)).unwrap();
    assert!(
        repo.write(
            &receipt.revisions,
            |_, _| -> anyhow::Result<()> { panic!("locked write") },
            |_| Ok(Revisions::new())
        )
        .unwrap_err()
        .is::<SharedWriteBusyError>()
    );
}
