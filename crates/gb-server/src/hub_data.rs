//! Offline data boundary for Hub updates. Archive paths never select live roots.

use gb_base::{files, home, host_path, shared_writes::IdentityLock};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

#[derive(Clone, Copy)]
pub enum Operation {
    Backup,
    Check,
    Upgrade,
}

fn roots() -> anyhow::Result<Vec<PathBuf>> {
    registered_roots(&home::data()?, &home::identity_root()?)
}

fn registered_roots(data: &Path, identity: &Path) -> anyhow::Result<Vec<PathBuf>> {
    let mut roots = vec![host_path::directory(identity, true)?];
    roots.extend(gb_workspace::selection::registered(data)?);
    roots.sort();
    roots.dedup();
    Ok(roots)
}

pub fn execute(
    operation: Operation,
    archive: Option<&Path>,
    to: Option<&Path>,
) -> anyhow::Result<()> {
    match operation {
        Operation::Backup => {
            anyhow::ensure!(
                archive.is_none(),
                "backup cannot select an archive as input"
            );
            backup(
                &home::identity_root()?,
                &roots()?,
                to.ok_or_else(|| anyhow::anyhow!("backup requires --to"))?,
            )
        }
        Operation::Check | Operation::Upgrade => {
            anyhow::ensure!(to.is_none(), "--to is only valid for backup");
            // No conversion is necessary for the additive schemas through version 4.
            // A future incompatible version adds its immediate-predecessor conversion here.
            if let Some(archive) = archive {
                check_archive(archive)
            } else {
                for root in roots()? {
                    check_root(&root)?;
                }
                Ok(())
            }
        }
    }
}

fn excluded(relative: &Path) -> bool {
    [
        "local/clones",
        "local/cache",
        "local/run",
        "local/agent_environment/snapshots",
        "local/msb",
    ]
    .iter()
    .any(|path| relative.starts_with(path))
}

/// Refuse links and special files rather than following them outside the data tree.
fn walk(
    root: &Path,
    relative: &Path,
    visit: &mut impl FnMut(&Path, &Path) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    if excluded(relative) {
        return Ok(());
    }
    let path = root.join(relative);
    let meta = match fs::symlink_metadata(&path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    anyhow::ensure!(
        !meta.file_type().is_symlink(),
        "data contains a symbolic link: {}",
        path.display()
    );
    if meta.is_dir() {
        visit(&path, relative)?;
        for entry in fs::read_dir(&path)? {
            walk(root, &relative.join(entry?.file_name()), visit)?;
        }
    } else {
        anyhow::ensure!(
            meta.is_file(),
            "data contains a special file: {}",
            path.display()
        );
        visit(&path, relative)?;
    }
    Ok(())
}

fn each(
    root: &Path,
    visit: &mut impl FnMut(&Path, &Path) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    // Check ancestors as well, so .guildbotics itself cannot redirect a rehearsal.
    host_path::directory(root, true)?;
    let data = root.join(".guildbotics");
    host_path::directory(&data, true)?;
    for part in ["config", "state", "local"] {
        walk(&data, Path::new(part), visit)?;
    }
    Ok(())
}

fn backup(identity_root: &Path, roots: &[PathBuf], destination: &Path) -> anyhow::Result<()> {
    backup_with(identity_root, roots, destination, &mut |file| {
        fs::File::open(file)?.sync_all()
    })
}

/// [`backup`], making each copied file durable with `flush`.
fn backup_with(
    identity_root: &Path,
    roots: &[PathBuf],
    destination: &Path,
    flush: &mut impl FnMut(&Path) -> std::io::Result<()>,
) -> anyhow::Result<()> {
    let destination = host_path::directory(destination, true)?;
    let destination = destination.as_path();
    fs::create_dir_all(destination)?;
    anyhow::ensure!(
        fs::read_dir(destination)?.next().is_none(),
        "backup destination must be empty"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(destination, fs::Permissions::from_mode(0o700))?;
    }
    for root in roots {
        anyhow::ensure!(
            !destination.starts_with(root.join(".guildbotics")),
            "backup destination is inside source data"
        );
    }
    // Copy under the writers' locks, so that the backup is one consistent
    // state. Writers give up after SHARED_WRITE_TIMEOUT, so the locks cover
    // only the copy, which clones on APFS; making it durable waits until after.
    let mut copies = Vec::new();
    {
        let mut identity = IdentityLock::acquire(identity_root, files::SHARED_WRITE_TIMEOUT)?;
        for (index, root) in roots.iter().enumerate() {
            let _workspace = identity.workspace(root, files::SHARED_WRITE_TIMEOUT)?;
            let copied = destination.join(index.to_string()).join(".guildbotics");
            fs::create_dir_all(&copied)?;
            each(root, &mut |source, relative| {
                let target = copied.join(relative);
                if source.is_dir() {
                    fs::create_dir_all(&target)?;
                } else {
                    fs::create_dir_all(target.parent().expect("data file has parent"))?;
                    // Copies the permissions too; the destination is private.
                    fs::copy(source, &target)?;
                    copies.push(target);
                }
                Ok(())
            })?;
        }
    }
    // The manifest marks the backup complete, after every copy is durable.
    for copy in &copies {
        flush(copy)?;
    }
    files::atomic_write(
        &destination.join("manifest.json"),
        files::dump_shared_json(&json!({"version":1,"roots":roots})).as_bytes(),
    )?;
    Ok(())
}

fn check_archive(archive: &Path) -> anyhow::Result<()> {
    host_path::directory(archive, false)?;
    let bytes = host_path::read_file_if_present(&archive.join("manifest.json"))?
        .ok_or_else(|| anyhow::anyhow!("backup manifest is missing"))?;
    let manifest: Value =
        serde_json::from_slice(&bytes).map_err(|_| anyhow::anyhow!("invalid backup manifest"))?;
    anyhow::ensure!(manifest["version"] == 1, "unsupported backup manifest");
    let roots = manifest["roots"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid backup roots"))?;
    anyhow::ensure!(!roots.is_empty(), "backup has no identity root");
    for index in 0..roots.len() {
        let root = archive.join(index.to_string());
        anyhow::ensure!(root.join(".guildbotics").is_dir(), "backup root is missing");
        check_root(&root)?;
    }
    Ok(())
}

fn check_root(root: &Path) -> anyhow::Result<()> {
    each(root, &mut |path, relative| {
        if path.is_dir() {
            return Ok(());
        }
        // Encrypted stores are copied as opaque bytes; never decrypt them here.
        if relative == Path::new("local/secrets.enc.json") {
            return Ok(());
        }
        let extension = path.extension().and_then(|s| s.to_str()).unwrap_or("");
        if !matches!(extension, "json" | "jsonl" | "yaml" | "yml") {
            return Ok(());
        }
        let bytes = fs::read(path)?;
        let invalid = || anyhow::anyhow!("invalid data file: {}", path.display());
        let values: Vec<Value> = match extension {
            "yaml" | "yml" => vec![
                gb_config::yaml_value(std::str::from_utf8(&bytes).map_err(|_| invalid())?)
                    .map_err(|_| invalid())?,
            ],
            // Match the journal readers: a torn row does not invalidate other records.
            "jsonl" => String::from_utf8_lossy(&bytes)
                .lines()
                .filter_map(|line| serde_json::from_str::<Value>(line).ok())
                .filter(Value::is_object)
                .collect(),
            _ => vec![serde_json::from_slice(&bytes).map_err(|_| invalid())?],
        };
        for value in values {
            if let Some(version) = value.get("schema_version") {
                anyhow::ensure!(
                    version
                        .as_u64()
                        .is_some_and(|v| (1..=files::SHARED_RECORD_SCHEMA_VERSION).contains(&v)),
                    "unsupported record schema: {}",
                    path.display()
                );
            }
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(root: &Path, name: &str, bytes: &str) {
        let file = root.join(".guildbotics").join(name);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(file, bytes).unwrap();
    }

    #[test]
    fn archive_checks_only_numbered_copies_and_preserves_local_data() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("workspace");
        write(&source, "config/project.yml", "name: test");
        write(&source, "state/run.json", r#"{"schema_version":4}"#);
        write(&source, "local/secrets.enc.json", "opaque encrypted bytes");
        write(&source, "local/clones/huge", "excluded");
        write(
            &source,
            "local/agent_environment/snapshots/huge",
            "excluded",
        );
        fs::create_dir_all(source.join(".guildbotics/config/empty")).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                source.join(".guildbotics/config/project.yml"),
                fs::Permissions::from_mode(0o700),
            )
            .unwrap();
        }
        let archive = temp.path().join("backup");
        backup(
            &temp.path().join("identity"),
            std::slice::from_ref(&source),
            &archive,
        )
        .unwrap();
        write(&source, "state/run.json", r#"{"schema_version":99}"#);
        check_archive(&archive).unwrap();
        assert!(check_root(&source).is_err());
        assert!(archive.join("0/.guildbotics/config/empty").is_dir());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(archive.join("0/.guildbotics/config/project.yml"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
        }
        assert!(!archive.join("0/.guildbotics/local/clones").exists());
        assert!(
            !archive
                .join("0/.guildbotics/local/agent_environment/snapshots")
                .exists()
        );
        assert_eq!(
            fs::read(archive.join("0/.guildbotics/local/secrets.enc.json")).unwrap(),
            b"opaque encrypted bytes"
        );
        write(&archive.join("0"), "state/run.json", "malformed");
        assert!(check_archive(&archive).is_err());
    }

    #[test]
    fn writers_wait_only_for_the_copy_not_for_making_it_durable() {
        let temp = tempfile::tempdir().unwrap();
        let identity = temp.path().join("identity");
        let source = temp.path().join("workspace");
        write(&identity, "state/member.json", "{}");
        write(&source, "state/run.json", "{}");
        let roots = [identity.clone(), source.clone()];
        let mut flushed = 0;
        backup_with(
            &identity,
            &roots,
            &temp.path().join("backup"),
            &mut |file| {
                // A writer takes both locks, in the order writers take them.
                let mut lock = IdentityLock::acquire(&identity, std::time::Duration::ZERO).unwrap();
                lock.workspace(&source, std::time::Duration::ZERO).unwrap();
                assert!(file.is_file());
                flushed += 1;
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(flushed, 2);
        assert!(temp.path().join("backup/manifest.json").is_file());
    }

    #[test]
    fn selects_all_registered_workspaces_and_identity_once() {
        let temp = tempfile::tempdir().unwrap();
        let data = temp.path().join("data");
        let identity = temp.path().join("identity");
        let one = temp.path().join("one");
        let two = temp.path().join("two");
        gb_workspace::selection::register(&data, &one).unwrap();
        gb_workspace::selection::register(&data, &two).unwrap();
        gb_workspace::selection::register(&data, &identity).unwrap();
        let roots = registered_roots(&data, &identity).unwrap();
        assert_eq!(roots.len(), 3);
        for root in [&one, &two, &identity] {
            assert!(roots.contains(root));
        }
        for root in &roots {
            write(root, "state/item.json", r#"{"schema_version":4}"#);
        }
        let archive = temp.path().join("archive");
        backup(&identity, &roots, &archive).unwrap();
        check_archive(&archive).unwrap();
        for i in 0..3 {
            assert!(
                archive
                    .join(i.to_string())
                    .join(".guildbotics/state/item.json")
                    .is_file()
            );
        }
        assert!(backup(&identity, &roots, &archive).is_err());
        fs::write(data.join("workspaces.json"), "invalid").unwrap();
        assert!(registered_roots(&data, &identity).is_err());
    }

    #[test]
    fn checks_record_versions_and_all_serialized_formats() {
        let temp = tempfile::tempdir().unwrap();
        for (name, text) in [
            ("state/item.json", r#"{"schema_version":5}"#),
            ("config/item.yml", "key: ["),
            ("state/item.json", r#"{"schema_version":0}"#),
            ("state/item.json", r#"{"schema_version":"4"}"#),
        ] {
            write(temp.path(), name, text);
            assert!(check_root(temp.path()).is_err(), "{name}");
            fs::remove_file(temp.path().join(".guildbotics").join(name)).unwrap();
        }
        for version in 1..=4 {
            write(
                temp.path(),
                "state/item.jsonl",
                &format!("{{\"schema_version\":{version}}}\n"),
            );
            check_root(temp.path()).unwrap();
        }
    }

    #[test]
    fn jsonl_skips_unreadable_rows_but_checks_every_readable_record_version() {
        let temp = tempfile::tempdir().unwrap();
        for name in [
            "state/documents/memory_events/device.jsonl",
            "state/activity/events.jsonl",
            "local/services/chat/channel.jsonl",
        ] {
            for content in [
                "{bad}\n",
                "{\"schema_version\":4}\n{\"schema_ver",
                "{bad}\n{\"schema_version\":4}\n{bad}\n{\"schema_version\":4}\n",
                "\n[]\nnull\n42\n",
            ] {
                write(temp.path(), name, content);
                check_root(temp.path()).unwrap();
            }
            for version in ["5", "0", "-1", "1.5", "null", "\"4\""] {
                write(
                    temp.path(),
                    name,
                    &format!("{{bad}}\n{{\"schema_version\":{version}}}\n{{bad}}"),
                );
                assert!(check_root(temp.path()).is_err(), "{name}: {version}");
            }
            // A torn UTF-8 sequence must not hide a readable future-version row.
            let path = temp.path().join(".guildbotics").join(name);
            fs::write(&path, b"{\"schema_version\":4}\n{\"text\":\"\xe3").unwrap();
            check_root(temp.path()).unwrap();
            fs::write(&path, b"{\"schema_version\":5,\"text\":\"\xe3\"}\n").unwrap();
            assert!(check_root(temp.path()).is_err());
            fs::remove_file(path).unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn refuses_links_in_data_and_missing_archive_roots() {
        let temp = tempfile::tempdir().unwrap();
        let outside = temp.path().join("outside");
        fs::create_dir(&outside).unwrap();
        let root = temp.path().join("root");
        fs::create_dir_all(root.join(".guildbotics")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join(".guildbotics/config")).unwrap();
        assert!(check_root(&root).is_err());
        fs::write(
            temp.path().join("manifest.json"),
            r#"{"version":1,"roots":["/never/read"]}"#,
        )
        .unwrap();
        assert!(check_archive(temp.path()).is_err());
    }
}
