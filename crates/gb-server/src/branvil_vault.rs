//! Branvil's secrets -- members' upstream credentials and the webhook's URL
//! -- in the identity store: sealed with the machine's key, like the
//! members' own credentials, and never in Branvil's data directory.

use std::path::PathBuf;
use std::sync::Arc;

use gb_base::{home, shared_writes::SharedWrites};
use gb_config::secrets::{DeviceKey, Keychain, Scope, SecretStore, SystemKeychain};

pub struct IdentityVault {
    /// The identity root and the machine root, or why they are unknown (told
    /// at the first use: Branvil's commands that keep no secret still run).
    roots: Result<(PathBuf, PathBuf), String>,
    keychain: Arc<dyn Keychain>,
}

impl IdentityVault {
    pub fn new(identity: PathBuf, machine: PathBuf, keychain: Arc<dyn Keychain>) -> Self {
        Self {
            roots: Ok((identity, machine)),
            keychain,
        }
    }

    /// This machine's identity store and keychain.
    pub fn system() -> Self {
        Self {
            roots: home::identity_root()
                .and_then(|identity| Ok((identity, home::machine_root()?)))
                .map_err(|e| e.to_string()),
            keychain: Arc::new(SystemKeychain),
        }
    }

    fn with<T>(&self, op: impl FnOnce(&SecretStore) -> anyhow::Result<T>) -> anyhow::Result<T> {
        let (identity, machine) = self
            .roots
            .as_ref()
            .map_err(|why| anyhow::anyhow!("{why}"))?;
        let writes = SharedWrites::new(identity.clone(), None);
        let store = SecretStore::new(
            &writes,
            Scope::Identity,
            DeviceKey {
                machine_root: machine.clone(),
                keychain: self.keychain.as_ref(),
            },
        )?;
        op(&store)
    }
}

/// A member's secret is `<PERSON>_<NAME>` in capitals (`person_secret_prefix`),
/// so a name in lower case is never one.
fn key(name: &str) -> String {
    format!("branvil_{name}")
}

impl branvil::vault::Vault for IdentityVault {
    fn get(&self, name: &str) -> anyhow::Result<Option<String>> {
        self.with(|store| store.get(&key(name)))
    }
    fn set(&self, name: &str, value: &str) -> anyhow::Result<()> {
        self.with(|store| store.set(&key(name), value))
    }
    fn delete(&self, name: &str) -> anyhow::Result<()> {
        self.with(|store| store.delete(&key(name)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use branvil::vault::Vault;
    use std::sync::Mutex;

    /// A keychain whose device key a test can take away.
    #[derive(Default)]
    struct Keys(Mutex<Option<String>>);
    impl Keychain for Keys {
        fn read(&self) -> anyhow::Result<Option<String>> {
            Ok(self.0.lock().unwrap().clone())
        }
        fn create(&self, encoded_key: &str) -> anyhow::Result<()> {
            *self.0.lock().unwrap() = Some(encoded_key.into());
            Ok(())
        }
    }

    fn vault(root: &std::path::Path, keys: Arc<Keys>) -> IdentityVault {
        IdentityVault::new(root.join("identity"), root.join("machine"), keys)
    }

    fn files(dir: &std::path::Path) -> Vec<(PathBuf, Vec<u8>)> {
        let mut out = Vec::new();
        for entry in std::fs::read_dir(dir).into_iter().flatten() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                out.extend(files(&path));
            } else {
                let bytes = std::fs::read(&path).unwrap();
                out.push((path, bytes));
            }
        }
        out
    }

    #[tokio::test]
    async fn the_integrated_branvil_keeps_its_webhook_sealed_in_the_identity_store() {
        let root = tempfile::tempdir().unwrap();
        let data = root.path().join("branvil");
        let owner = branvil::api::initialize(&data, "owner1").unwrap();
        let keys = Arc::new(Keys::default());
        let running =
            branvil::api::Running::start(&data, Arc::new(vault(root.path(), keys.clone())))
                .await
                .unwrap();
        let url = "https://hooks.example.test/services/T0/hook-71c3";
        let webhook = format!("{}/api/v1/admin/webhook", running.data.base_url());
        let response = reqwest::Client::new()
            .put(&webhook)
            .bearer_auth(owner.trim())
            .json(&serde_json::json!({ "url": url }))
            .send()
            .await
            .unwrap();
        assert!(response.status().is_success(), "{response:?}");
        running.stop().await.unwrap();

        // The value is in the identity store under a Branvil name only.
        let store = vault(root.path(), keys);
        let index =
            std::fs::read_to_string(root.path().join("identity/.guildbotics/config/secrets.yml"))
                .unwrap();
        let name = index
            .split('"')
            .find(|n| n.starts_with("branvil_webhook_"))
            .expect("the webhook's name in the index");
        assert_eq!(store.with(|s| s.get(name)).unwrap().as_deref(), Some(url));
        // Neither the identity store nor Branvil's data holds it in plain text,
        // and Branvil's secrets/ holds the owner's token alone.
        for (path, bytes) in files(root.path()) {
            assert!(
                !String::from_utf8_lossy(&bytes).contains("hook-71c3"),
                "{}",
                path.display()
            );
        }
        // Branvil's secrets/ holds one file, the owner's token.
        let kept: Vec<_> = files(&data.join("secrets"))
            .into_iter()
            .map(|(_, bytes)| bytes)
            .collect();
        assert_eq!(kept, [owner.into_bytes()]);
    }

    #[test]
    fn names_are_no_members_and_failures_never_fall_back_to_plain_text() {
        let root = tempfile::tempdir().unwrap();
        let keys = Arc::new(Keys::default());
        let store = vault(root.path(), keys.clone());
        store.set("account_github_0f1e", "secret-1").unwrap();
        assert_eq!(
            store.get("account_github_0f1e").unwrap().as_deref(),
            Some("secret-1")
        );
        let names = store.with(|s| s.keys()).unwrap();
        assert_eq!(names, ["branvil_account_github_0f1e"]);
        assert!(names.iter().all(|n| n != &n.to_uppercase()));

        // No device key: refused, saying why; nothing written in plain text.
        let kept = keys.0.lock().unwrap().take();
        let error = store.get("account_github_0f1e").unwrap_err().to_string();
        assert!(error.contains("device key missing"), "{error}");
        assert!(store.set("webhook_1a2b", "secret-0").is_err());
        assert!(store.delete("account_github_0f1e").is_err());
        *keys.0.lock().unwrap() = kept;
        assert_eq!(
            store.get("account_github_0f1e").unwrap().as_deref(),
            Some("secret-1")
        );
        // A corrupted store: refused too.
        let sealed = root
            .path()
            .join("identity/.guildbotics/local/secrets.enc.json");
        std::fs::write(&sealed, b"{}").unwrap();
        let error = store.get("account_github_0f1e").unwrap_err().to_string();
        assert!(error.contains("malformed"), "{error}");
        assert!(store.set("webhook_1a2b", "secret-2").is_err());
        assert!(store.delete("account_github_0f1e").is_err());
        for (path, bytes) in files(root.path()) {
            assert!(
                !String::from_utf8_lossy(&bytes).contains("secret-"),
                "{}",
                path.display()
            );
        }
    }
}
