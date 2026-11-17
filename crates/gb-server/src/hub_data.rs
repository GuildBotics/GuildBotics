//! Offline data boundary for Hub updates. Archive paths never select live roots.

use gb_base::{
    files, home, host_path,
    shared_writes::{IdentityLock, SharedWrites},
};
use gb_config::secrets::{DeviceKey, Scope, SecretStore, SystemKeychain, read_secret_index};
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

/// The identity root first, then each registered workspace once.
fn registered_roots(data: &Path, identity: &Path) -> anyhow::Result<Vec<PathBuf>> {
    let identity = host_path::directory(identity, true)?;
    let mut workspaces = gb_workspace::selection::registered(data)?;
    workspaces.retain(|root| *root != identity);
    workspaces.sort();
    workspaces.dedup();
    Ok([vec![identity], workspaces].concat())
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
                &roots()?,
                to.ok_or_else(|| anyhow::anyhow!("backup requires --to"))?,
            )
        }
        Operation::Check | Operation::Upgrade => {
            anyhow::ensure!(to.is_none(), "--to is only valid for backup");
            let roots = match archive {
                Some(archive) => archive_roots(archive)?,
                None => roots()?,
            };
            if let Operation::Upgrade = operation {
                let device = DeviceKey {
                    machine_root: home::machine_root()?,
                    keychain: &SystemKeychain,
                };
                upgrade(&roots, &device)?;
            }
            for root in &roots {
                check_root(root, files::SHARED_RECORD_SCHEMA_VERSION)?;
            }
            Ok(())
        }
    }
}

/// Converts the identity root, the first of `roots`: every record the
/// conversions change lives there. Nothing changes until every root has
/// passed the check (no link, no unreadable file, no newer version) and
/// everything the conversion reads has been read, the secret store decrypted.
fn upgrade(roots: &[PathBuf], device: &DeviceKey) -> anyhow::Result<()> {
    for root in roots {
        check_root(root, files::SHARED_RECORD_SCHEMA_VERSION)?;
    }
    let identity = roots
        .first()
        .ok_or_else(|| anyhow::anyhow!("no identity root"))?;
    upgrade_root(identity, device, false)?;
    upgrade_root(identity, device, true)
}

/// Where a converted root records the version of its data, so that the
/// check of a build that reads only older versions stops on it.
const SCHEMA_RECORD: &str = ".guildbotics/state/schema.json";

/// 4 → 5 (versions 1–4 only added fields): GuildBotics no longer keeps a
/// connection, an owner token, or member tokens and user names of its own for
/// the built-in code host. Version 4 keeps them only in the identity root.
/// Repeating it changes nothing more, and nothing changes before every file
/// it reads and the secret store have been read.
/// With `write` false, only reads what the conversion reads.
fn upgrade_root(root: &Path, device: &DeviceKey, write: bool) -> anyhow::Result<()> {
    let writes = SharedWrites::new(root.to_path_buf(), None);
    let config = root.join(".guildbotics/config");
    let connections = config.join("connections");
    let connection = connections.join("branvil.yml");
    let decrypt = writes.transaction(|writer| {
        let mut secrets = vec!["BRANVIL_ADMIN_TOKEN".to_owned()];
        let mut identities = Vec::new();
        let members = match fs::read_dir(config.join("team/members")) {
            Ok(entries) => entries.collect::<Result<Vec<_>, _>>()?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(error) => return Err(error.into()),
        };
        for member in members {
            if !member.file_type()?.is_dir() {
                continue;
            }
            let file = member.path().join("identity.yml");
            let Some(bytes) = files::read_if_present(&file)? else {
                continue;
            };
            // Every member's keys go, so a prefix two members share needs no check.
            let id = member.file_name().to_string_lossy().into_owned();
            for name in ["BRANVIL_TOKEN", "BRANVIL_TOKEN_ID"] {
                secrets.push(gb_config::team::person_secret_name(&id, name));
            }
            let invalid = || anyhow::anyhow!("invalid data file: {}", file.display());
            let mut identity = std::str::from_utf8(&bytes)
                .ok()
                .and_then(|text| gb_config::yaml_value(text).ok())
                .ok_or_else(invalid)?;
            let fields = identity.as_object_mut().ok_or_else(invalid)?;
            let Some(info) = fields
                .get_mut("account_info")
                .and_then(Value::as_object_mut)
            else {
                continue;
            };
            if info.remove("branvil_username").is_some() {
                if info.is_empty() {
                    fields.remove("account_info");
                }
                identities.push((file, identity));
            }
        }
        let index = read_secret_index(&config)?;
        secrets.retain(|name| index.contains(name));
        if secrets.is_empty() && identities.is_empty() && !connection.exists() {
            return Ok(false);
        }
        // Decrypting the store is the last read; its first write the first change.
        if !write {
            // The store is decrypted below, outside the lock it takes itself.
            return Ok(!secrets.is_empty());
        }
        if !secrets.is_empty() {
            SecretStore::new(&writes, Scope::Identity, device.clone())?
                .delete_many_with(writer, &secrets)?;
        }
        writer.write_json(
            &root.join(SCHEMA_RECORD),
            &json!({"schema_version": files::SHARED_RECORD_SCHEMA_VERSION}),
        )?;
        for (file, identity) in identities {
            writer.write_json(&file, &identity)?;
        }
        writer.delete(&connection)?;
        if connections.is_dir() && fs::read_dir(&connections)?.next().is_none() {
            writer.remove_tree(&connections)?;
        }
        Ok(false)
    })?;
    if decrypt {
        SecretStore::new(&writes, Scope::Identity, device.clone())?.values()?;
    }
    Ok(())
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

/// Copies `roots`, the identity root first, as `registered_roots` lists them.
fn backup(roots: &[PathBuf], destination: &Path) -> anyhow::Result<()> {
    backup_with(roots, destination, &mut |file| {
        fs::File::open(file)?.sync_all()
    })
}

/// [`backup`], making each copied file durable with `flush`.
fn backup_with(
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
        let identity_root = roots
            .first()
            .ok_or_else(|| anyhow::anyhow!("no identity root"))?;
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
        files::dump_shared_json(&json!({"version":2,"roots":roots})).as_bytes(),
    )?;
    Ok(())
}

/// The numbered copies of a backup, never the roots its manifest names.
fn archive_roots(archive: &Path) -> anyhow::Result<Vec<PathBuf>> {
    host_path::directory(archive, false)?;
    let bytes = host_path::read_file_if_present(&archive.join("manifest.json"))?
        .ok_or_else(|| anyhow::anyhow!("backup manifest is missing"))?;
    let manifest: Value =
        serde_json::from_slice(&bytes).map_err(|_| anyhow::anyhow!("invalid backup manifest"))?;
    // Version 1 listed the roots in path order, not the identity root first.
    anyhow::ensure!(manifest["version"] == 2, "unsupported backup manifest");
    let roots = manifest["roots"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid backup roots"))?;
    anyhow::ensure!(!roots.is_empty(), "backup has no identity root");
    (0..roots.len())
        .map(|index| {
            let root = archive.join(index.to_string());
            anyhow::ensure!(root.join(".guildbotics").is_dir(), "backup root is missing");
            Ok(root)
        })
        .collect()
}

/// Checks the records under `root` against `newest`, the latest version a
/// build reads.
fn check_root(root: &Path, newest: u64) -> anyhow::Result<()> {
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
                    version.as_u64().is_some_and(|v| (1..=newest).contains(&v)),
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

    fn check(root: &Path) -> anyhow::Result<()> {
        check_root(root, files::SHARED_RECORD_SCHEMA_VERSION)
    }

    fn check_archive(archive: &Path) -> anyhow::Result<()> {
        execute(Operation::Check, Some(archive), None)
    }

    /// A keychain holding the device key in memory, never the machine's.
    #[derive(Default)]
    struct Keys(std::sync::Mutex<Option<String>>);
    impl gb_config::secrets::Keychain for Keys {
        fn read(&self) -> anyhow::Result<Option<String>> {
            Ok(self.0.lock().unwrap().clone())
        }
        fn create(&self, encoded_key: &str) -> anyhow::Result<()> {
            *self.0.lock().unwrap() = Some(encoded_key.into());
            Ok(())
        }
    }

    /// A keychain no conversion may reach.
    struct Untouchable;
    impl gb_config::secrets::Keychain for Untouchable {
        fn read(&self) -> anyhow::Result<Option<String>> {
            panic!("the keychain was reached")
        }
        fn create(&self, _: &str) -> anyhow::Result<()> {
            panic!("the keychain was reached")
        }
    }

    fn device<'a>(machine: &Path, keychain: &'a dyn gb_config::secrets::Keychain) -> DeviceKey<'a> {
        DeviceKey {
            machine_root: machine.into(),
            keychain,
        }
    }

    fn store<'a>(writes: &'a SharedWrites, device: DeviceKey<'a>) -> SecretStore<'a> {
        SecretStore::new(writes, Scope::Identity, device).unwrap()
    }

    /// Every file under `root`, by path, as bytes.
    fn snapshot(root: &Path) -> std::collections::BTreeMap<PathBuf, Vec<u8>> {
        let mut out = std::collections::BTreeMap::new();
        for entry in fs::read_dir(root).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                out.extend(snapshot(&path));
            } else {
                out.insert(path.clone(), fs::read(&path).unwrap());
            }
        }
        out
    }

    fn yaml(path: &Path) -> Value {
        gb_config::yaml_value(&fs::read_to_string(path).unwrap()).unwrap()
    }

    /// An identity root as version 4 left it: the code host's connection and
    /// owner token, and members' tokens and user names, beside other data.
    fn version_4_identity(root: &Path, keys: &Keys) {
        write(
            root,
            "config/connections/branvil.yml",
            "base_url: http://127.0.0.1:8866\n",
        );
        write(
            root,
            "config/team/members/alice/identity.yml",
            "person_id: alice\naccount_info:\n  git_user: Alice\n  branvil_username: alice-bot\n",
        );
        write(
            root,
            "config/team/members/aiko-bot/identity.yml",
            r#"{"person_id":"aiko-bot","account_info":{"branvil_username":"aiko"}}"#,
        );
        write(
            root,
            "config/team/members/ken/identity.yml",
            "person_id: ken\n",
        );
        // A stray file beside the member directories is not a member.
        write(root, "config/team/members/.DS_Store", "");
        write(root, "state/item.json", r#"{"schema_version":4}"#);
        let writes = SharedWrites::new(root.into(), None);
        store(&writes, device(root, keys))
            .set_many(
                &[
                    ("BRANVIL_ADMIN_TOKEN", "bvt_owner"),
                    ("ALICE_BRANVIL_TOKEN", "bvt_alice"),
                    ("ALICE_BRANVIL_TOKEN_ID", "tok_alice"),
                    ("AIKO_BOT_BRANVIL_TOKEN", "bvt_aiko"),
                    ("ALICE_SLACK_BOT_TOKEN", "xoxb-synthetic"),
                    ("branvil_upstream_github", "upstream-synthetic"),
                ]
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect(),
            )
            .unwrap();
    }

    #[test]
    fn upgrade_of_a_backup_converts_only_its_copies_once_and_stops_an_older_check() {
        let temp = tempfile::tempdir().unwrap();
        let keys = Keys::default();
        let identity = temp.path().join("identity");
        let workspace = temp.path().join("workspace");
        version_4_identity(&identity, &keys);
        write(&workspace, "config/project.yml", "name: test");
        let archive = temp.path().join("backup");
        backup(&[identity.clone(), workspace.clone()], &archive).unwrap();
        let originals = [snapshot(&identity), snapshot(&workspace)];

        let roots = archive_roots(&archive).unwrap();
        upgrade(&roots, &device(temp.path(), &keys)).unwrap();
        check_archive(&archive).unwrap();
        assert_eq!([snapshot(&identity), snapshot(&workspace)], originals);

        let copy = archive.join("0");
        let config = copy.join(".guildbotics/config");
        assert!(!config.join("connections").exists());
        assert_eq!(
            yaml(&config.join("team/members/alice/identity.yml")),
            json!({"person_id":"alice","account_info":{"git_user":"Alice"}})
        );
        assert_eq!(
            yaml(&config.join("team/members/aiko-bot/identity.yml")),
            json!({"person_id":"aiko-bot"})
        );
        assert_eq!(
            fs::read_to_string(config.join("team/members/ken/identity.yml")).unwrap(),
            "person_id: ken\n"
        );
        let writes = SharedWrites::new(copy.clone(), None);
        assert_eq!(
            store(&writes, device(temp.path(), &keys))
                .values()
                .unwrap()
                .into_iter()
                .collect::<Vec<_>>(),
            [
                ("ALICE_SLACK_BOT_TOKEN".into(), "xoxb-synthetic".into()),
                (
                    "branvil_upstream_github".into(),
                    "upstream-synthetic".into()
                ),
            ]
        );
        assert_eq!(
            read_secret_index(&config).unwrap(),
            ["ALICE_SLACK_BOT_TOKEN", "branvil_upstream_github"]
        );
        // Only the converted root is marked, and a build that reads only
        // version 4 stops on it.
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(copy.join(SCHEMA_RECORD)).unwrap()).unwrap(),
            json!({"schema_version": 5})
        );
        assert!(!archive.join("1").join(SCHEMA_RECORD).exists());
        let older = check_root(&copy, 4).unwrap_err().to_string();
        assert!(older.contains("schema.json"), "{older}");
        check_root(&archive.join("1"), 4).unwrap();

        // Repeating it neither changes anything nor reads the keychain.
        let converted = snapshot(&archive);
        upgrade(&roots, &device(temp.path(), &Untouchable)).unwrap();
        assert_eq!(snapshot(&archive), converted);
    }

    #[test]
    fn an_upgrade_converts_the_identity_root_and_reads_no_workspace_secret_index() {
        let temp = tempfile::tempdir().unwrap();
        // Roots are compared as the registry stores them, links resolved.
        let temp = temp.path().canonicalize().unwrap();
        let keys = Keys::default();
        let data = temp.join("data");
        // The identity root sorts after the workspace: it comes first because
        // it is the identity root, not because of its path.
        let identity = temp.join("z-identity");
        let workspace = temp.join("a-workspace");
        version_4_identity(&identity, &keys);
        // A workspace an older build left: no current build reads its index.
        write(
            &workspace,
            "config/secrets.yml",
            "store_id: abc\nkeys:\n  OPENAI_API_KEY:\n    generation: 2\n",
        );
        gb_workspace::selection::register(&data, &workspace).unwrap();
        let roots = registered_roots(&data, &identity).unwrap();
        assert_eq!(roots, [identity.clone(), workspace.clone()]);
        let archive = temp.join("backup");
        backup(&roots, &archive).unwrap();
        let copies = archive_roots(&archive).unwrap();
        let workspace_before = snapshot(&copies[1]);

        upgrade(&copies, &device(&temp, &keys)).unwrap();
        check_archive(&archive).unwrap();
        assert!(!copies[0].join(".guildbotics/config/connections").exists());
        assert!(copies[0].join(SCHEMA_RECORD).is_file());
        assert_eq!(snapshot(&copies[1]), workspace_before);

        // A backup by an older build listed its roots in path order.
        let manifest = archive.join("manifest.json");
        let mut older: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
        older["version"] = json!(1);
        fs::write(&manifest, older.to_string()).unwrap();
        let error = archive_roots(&archive).unwrap_err().to_string();
        assert!(error.contains("unsupported backup manifest"), "{error}");
    }

    #[cfg(unix)]
    #[test]
    fn an_upgrade_changes_no_root_until_every_root_passes_the_check() {
        let temp = tempfile::tempdir().unwrap();
        let keys = Keys::default();
        let good = temp.path().join("good");
        version_4_identity(&good, &keys);
        let before = snapshot(&good);
        // A newer version is refused, not overwritten with version 5.
        let newer = temp.path().join("newer");
        write(&newer, "config/connections/branvil.yml", "base_url: x\n");
        write(&newer, "state/schema.json", r#"{"schema_version":6}"#);
        let newer_before = snapshot(&newer);
        let error = upgrade(&[good.clone(), newer.clone()], &device(temp.path(), &keys))
            .unwrap_err()
            .to_string();
        assert!(error.contains("unsupported record schema"), "{error}");
        assert_eq!(snapshot(&good), before);
        assert_eq!(snapshot(&newer), newer_before);
        // A root that passes through a link leaves what the link names alone.
        let outside = temp.path().join("outside");
        write(&outside, "config/connections/branvil.yml", "base_url: x\n");
        let target = snapshot(&outside);
        let linked = temp.path().join("linked");
        fs::create_dir_all(&linked).unwrap();
        std::os::unix::fs::symlink(outside.join(".guildbotics"), linked.join(".guildbotics"))
            .unwrap();
        let error = upgrade(&[good.clone(), linked], &device(temp.path(), &keys))
            .unwrap_err()
            .to_string();
        assert!(error.contains("passes through a link"), "{error}");
        assert_eq!(snapshot(&outside), target);
        assert_eq!(snapshot(&good), before);
        upgrade(std::slice::from_ref(&good), &device(temp.path(), &keys)).unwrap();
        assert_ne!(snapshot(&good), before);
    }

    #[test]
    fn an_upgrade_that_cannot_read_the_secret_store_changes_nothing() {
        let temp = tempfile::tempdir().unwrap();
        let identity = temp.path().join("identity");
        version_4_identity(&identity, &Keys::default());
        let before = snapshot(&identity);
        // Another machine's keychain does not hold this store's key.
        let error = upgrade(
            std::slice::from_ref(&identity),
            &device(temp.path(), &Keys::default()),
        )
        .unwrap_err()
        .to_string();
        assert!(error.contains("device key missing"), "{error}");
        assert_eq!(snapshot(&identity), before);
        check_root(&identity, 4).unwrap();
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
        backup(std::slice::from_ref(&source), &archive).unwrap();
        write(&source, "state/run.json", r#"{"schema_version":99}"#);
        check_archive(&archive).unwrap();
        assert!(check(&source).is_err());
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
        backup_with(&roots, &temp.path().join("backup"), &mut |file| {
            // A writer takes both locks, in the order writers take them.
            let mut lock = IdentityLock::acquire(&identity, std::time::Duration::ZERO).unwrap();
            lock.workspace(&source, std::time::Duration::ZERO).unwrap();
            assert!(file.is_file());
            flushed += 1;
            Ok(())
        })
        .unwrap();
        assert_eq!(flushed, 2);
        assert!(temp.path().join("backup/manifest.json").is_file());
    }

    #[test]
    fn selects_all_registered_workspaces_and_identity_once() {
        let temp = tempfile::tempdir().unwrap();
        let temp = temp.path().canonicalize().unwrap();
        let data = temp.join("data");
        let identity = temp.join("identity");
        let one = temp.join("one");
        let two = temp.join("two");
        gb_workspace::selection::register(&data, &two).unwrap();
        gb_workspace::selection::register(&data, &identity).unwrap();
        gb_workspace::selection::register(&data, &one).unwrap();
        let roots = registered_roots(&data, &identity).unwrap();
        assert_eq!(roots, [identity.clone(), one, two]);
        for root in &roots {
            write(root, "state/item.json", r#"{"schema_version":4}"#);
        }
        let archive = temp.join("archive");
        backup(&roots, &archive).unwrap();
        check_archive(&archive).unwrap();
        for i in 0..3 {
            assert!(
                archive
                    .join(i.to_string())
                    .join(".guildbotics/state/item.json")
                    .is_file()
            );
        }
        assert!(backup(&roots, &archive).is_err());
        fs::write(data.join("workspaces.json"), "invalid").unwrap();
        assert!(registered_roots(&data, &identity).is_err());
    }

    #[test]
    fn checks_record_versions_and_all_serialized_formats() {
        let temp = tempfile::tempdir().unwrap();
        for (name, text) in [
            ("state/item.json", r#"{"schema_version":6}"#),
            ("config/item.yml", "key: ["),
            ("state/item.json", r#"{"schema_version":0}"#),
            ("state/item.json", r#"{"schema_version":"4"}"#),
        ] {
            write(temp.path(), name, text);
            assert!(check(temp.path()).is_err(), "{name}");
            fs::remove_file(temp.path().join(".guildbotics").join(name)).unwrap();
        }
        for version in 1..=5 {
            write(
                temp.path(),
                "state/item.jsonl",
                &format!("{{\"schema_version\":{version}}}\n"),
            );
            check(temp.path()).unwrap();
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
                check(temp.path()).unwrap();
            }
            for version in ["6", "0", "-1", "1.5", "null", "\"4\""] {
                write(
                    temp.path(),
                    name,
                    &format!("{{bad}}\n{{\"schema_version\":{version}}}\n{{bad}}"),
                );
                assert!(check(temp.path()).is_err(), "{name}: {version}");
            }
            // A torn UTF-8 sequence must not hide a readable future-version row.
            let path = temp.path().join(".guildbotics").join(name);
            fs::write(&path, b"{\"schema_version\":4}\n{\"text\":\"\xe3").unwrap();
            check(temp.path()).unwrap();
            fs::write(&path, b"{\"schema_version\":6,\"text\":\"\xe3\"}\n").unwrap();
            assert!(check(temp.path()).is_err());
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
        assert!(check(&root).is_err());
        fs::write(
            temp.path().join("manifest.json"),
            r#"{"version":2,"roots":["/never/read"]}"#,
        )
        .unwrap();
        assert!(check_archive(temp.path()).is_err());
    }
}
