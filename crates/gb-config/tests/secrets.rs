use base64::{Engine, engine::general_purpose::STANDARD as B64};
use gb_base::shared_writes::SharedWrites;
use gb_config::secrets::{
    DeviceKey, Keychain, Scope, SecretStore, read_secret_index, read_store_id,
};
use std::{
    fs,
    path::Path,
    sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};

#[derive(Default)]
struct Fake {
    key: Mutex<Option<String>>,
    creates: AtomicUsize,
    unavailable: Mutex<bool>,
}
impl Keychain for Fake {
    fn read(&self) -> anyhow::Result<Option<String>> {
        anyhow::ensure!(!*self.unavailable.lock().unwrap(), "unavailable");
        Ok(self.key.lock().unwrap().clone())
    }
    fn create(&self, key: &str) -> anyhow::Result<()> {
        let mut stored = self.key.lock().unwrap();
        anyhow::ensure!(stored.is_none(), "must never replace a key");
        *stored = Some(key.to_owned());
        self.creates.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}
fn writes(root: &Path) -> SharedWrites {
    SharedWrites::new(root.join("identity"), Some(root.join("workspace")))
}
fn store<'a>(
    root: &Path,
    writes: &'a SharedWrites,
    fake: &'a Fake,
    scope: Scope,
) -> SecretStore<'a> {
    SecretStore::new(
        writes,
        scope,
        DeviceKey {
            machine_root: root.join("machine"),
            keychain: fake,
        },
    )
    .unwrap()
}
fn assert_no_plaintext(path: &Path, needle: &[u8]) {
    for entry in fs::read_dir(path).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            assert_no_plaintext(&path, needle);
        } else {
            assert!(
                !fs::read(path)
                    .unwrap()
                    .windows(needle.len())
                    .any(|part| part == needle)
            );
        }
    }
}

#[test]
fn two_stores_share_one_immutable_key_and_persist_only_ciphertext() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let fake = Fake::default();
    let identity = store(dir.path(), &writes, &fake, Scope::Identity);
    let workspace = store(dir.path(), &writes, &fake, Scope::Workspace);
    identity
        .set("TOKEN", "synthetic-test-credential-identity")
        .unwrap();
    workspace
        .set("TOKEN", "synthetic-test-credential-workspace")
        .unwrap();
    assert_eq!(
        identity.get("TOKEN").unwrap().unwrap(),
        "synthetic-test-credential-identity"
    );
    assert_eq!(
        workspace.get("TOKEN").unwrap().unwrap(),
        "synthetic-test-credential-workspace"
    );
    workspace
        .set("SECOND", "synthetic-test-credential-second")
        .unwrap();
    assert_eq!(workspace.keys().unwrap(), ["SECOND", "TOKEN"]);
    workspace.delete("TOKEN").unwrap();
    assert!(workspace.get("TOKEN").unwrap().is_none());
    assert_eq!(fake.creates.load(Ordering::SeqCst), 1);
    assert_no_plaintext(dir.path(), b"synthetic-test-credential");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let meta = fs::metadata(
            dir.path()
                .join("workspace/.guildbotics/local/secrets.enc.json"),
        )
        .unwrap();
        assert_eq!(meta.permissions().mode() & 0o777, 0o600);
    }
}

#[test]
fn python_device_key_is_reused_and_unavailable_keychain_leaves_no_records() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let fake = Fake::default();
    // Python stores exactly this base64 encoding of a 32-byte AES key.
    *fake.key.lock().unwrap() = Some(B64.encode([19; 32]));
    let store = store(dir.path(), &writes, &fake, Scope::Workspace);
    store.ensure_initialized().unwrap();
    store.set("KEY", "synthetic-credential").unwrap();
    assert_eq!(fake.creates.load(Ordering::SeqCst), 0);
    assert_eq!(
        fake.key.lock().unwrap().as_deref(),
        Some(B64.encode([19; 32]).as_str())
    );
    let before = fs::read(
        dir.path()
            .join("workspace/.guildbotics/local/secrets.enc.json"),
    )
    .unwrap();
    *fake.unavailable.lock().unwrap() = true;
    assert!(store.ensure_initialized().is_err());
    assert_eq!(
        fs::read(
            dir.path()
                .join("workspace/.guildbotics/local/secrets.enc.json")
        )
        .unwrap(),
        before
    );
}

#[test]
fn unreadable_indices_and_failed_record_write_are_not_an_empty_store() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let fake = Fake::default();
    let store = store(dir.path(), &writes, &fake, Scope::Workspace);
    let config = dir.path().join("workspace/.guildbotics/config");
    fs::create_dir_all(config.join("secrets.yml")).unwrap();
    assert!(store.ensure_initialized().is_err());
    fs::remove_dir(config.join("secrets.yml")).unwrap();
    let local = dir.path().join("workspace/.guildbotics/local");
    fs::create_dir_all(local.join("secrets.json")).unwrap();
    assert!(store.ensure_initialized().is_err());
    fs::remove_dir(local.join("secrets.json")).unwrap();
    fs::create_dir_all(local.join("secrets.enc.json")).unwrap();
    assert!(store.set("KEY", "synthetic-credential").is_err());
    assert_eq!(fake.creates.load(Ordering::SeqCst), 0);
    assert_no_plaintext(dir.path(), b"synthetic-credential");
}

#[test]
fn unusable_keys_and_files_stop_reads_and_writes_without_replacement() {
    for damage in [
        "missing-key",
        "bad-key",
        "wrong-key",
        "locked",
        "json",
        "nonce",
        "ciphertext",
        "version",
        "missing-file",
        "namespace",
        "index",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let writes = writes(dir.path());
        let fake = Fake::default();
        let store = store(dir.path(), &writes, &fake, Scope::Workspace);
        store.set("TOKEN", "synthetic-test-credential").unwrap();
        let local = dir.path().join("workspace/.guildbotics/local");
        let enc = local.join("secrets.enc.json");
        match damage {
            "missing-key" => *fake.key.lock().unwrap() = None,
            "bad-key" => *fake.key.lock().unwrap() = Some("bad".into()),
            "wrong-key" => *fake.key.lock().unwrap() = Some(B64.encode([42; 32])),
            "locked" => *fake.unavailable.lock().unwrap() = true,
            "json" => fs::write(&enc, "broken").unwrap(),
            "missing-file" => fs::remove_file(&enc).unwrap(),
            "namespace" => fs::write(local.join("secrets.json"), "{}").unwrap(),
            "index" => fs::write(
                dir.path().join("workspace/.guildbotics/config/secrets.yml"),
                "keys: []\n",
            )
            .unwrap(),
            field => {
                let mut value: serde_json::Value =
                    serde_json::from_slice(&fs::read(&enc).unwrap()).unwrap();
                value[field] = if field == "version" {
                    2.into()
                } else {
                    "!bad!".into()
                };
                fs::write(&enc, serde_json::to_vec(&value).unwrap()).unwrap();
            }
        }
        let before = fs::read(&enc).ok();
        assert!(store.values().is_err(), "{damage}");
        assert!(
            store.set("OTHER", "synthetic-test-new-credential").is_err(),
            "{damage}"
        );
        assert_eq!(fs::read(&enc).ok(), before, "{damage}");
        assert_eq!(fake.creates.load(Ordering::SeqCst), 1, "{damage}");
    }
}

#[test]
fn namespace_is_authenticated_and_cannot_be_swapped_between_stores() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let fake = Fake::default();
    let one = store(dir.path(), &writes, &fake, Scope::Identity);
    let two = store(dir.path(), &writes, &fake, Scope::Workspace);
    one.set("A", "synthetic-one").unwrap();
    two.set("A", "synthetic-two").unwrap();
    fs::copy(
        dir.path()
            .join("identity/.guildbotics/local/secrets.enc.json"),
        dir.path()
            .join("workspace/.guildbotics/local/secrets.enc.json"),
    )
    .unwrap();
    assert!(
        two.values()
            .unwrap_err()
            .to_string()
            .contains("authentication")
    );
}

#[test]
fn concurrent_updates_keep_every_key_and_create_device_key_once() {
    let dir = tempfile::tempdir().unwrap();
    let writes = writes(dir.path());
    let fake = Fake::default();
    std::thread::scope(|scope| {
        for n in 0..8 {
            let store = store(
                dir.path(),
                &writes,
                &fake,
                if n % 2 == 0 {
                    Scope::Identity
                } else {
                    Scope::Workspace
                },
            );
            scope.spawn(move || store.set(&format!("KEY_{n}"), "synthetic-value").unwrap());
        }
    });
    for kind in [Scope::Identity, Scope::Workspace] {
        assert_eq!(
            store(dir.path(), &writes, &fake, kind)
                .keys()
                .unwrap()
                .len(),
            4
        );
    }
    assert_eq!(fake.creates.load(Ordering::SeqCst), 1);
}

#[test]
fn python_written_indices_have_the_same_names_and_namespace() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("../../../records/golden/secrets.json")).unwrap();
    for case in fixture.as_array().unwrap() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("secrets.yml"),
            case["index"].as_str().unwrap(),
        )
        .unwrap();
        fs::write(
            dir.path().join("secrets.json"),
            case["local"].as_str().unwrap(),
        )
        .unwrap();
        assert_eq!(
            serde_json::to_value(read_secret_index(dir.path()).unwrap()).unwrap(),
            case["keys"]
        );
        assert_eq!(
            read_store_id(dir.path()).unwrap().unwrap(),
            case["store_id"]
        );
    }
}

#[test]
fn malformed_indices_are_never_empty_and_invalid_keys_never_reach_keychain() {
    let dir = tempfile::tempdir().unwrap();
    for text in [
        "",
        "null",
        "[]",
        "keys: string",
        "keys: [1]",
        "keys: []\nvalue: hidden",
        "keys: [",
    ] {
        fs::write(dir.path().join("secrets.yml"), text).unwrap();
        assert!(read_secret_index(dir.path()).is_err(), "{text}");
    }
    for text in [
        "",
        "null",
        "[]",
        "{}",
        "{\"store_id\":1}",
        "{\"store_id\":\"\"}",
    ] {
        fs::write(dir.path().join("secrets.json"), text).unwrap();
        assert!(read_store_id(dir.path()).is_err());
    }
    let writes = writes(dir.path());
    let fake = Fake::default();
    let store = store(dir.path(), &writes, &fake, Scope::Workspace);
    for key in ["", "bad-key", "名", &"A".repeat(129)] {
        assert!(store.set(key, "synthetic-value").is_err());
    }
    assert_eq!(fake.creates.load(Ordering::SeqCst), 0);
}
