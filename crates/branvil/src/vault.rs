//! Where the service keeps the secrets of other systems it uses: members'
//! upstream credentials and the webhook's URL. The process that runs the
//! service provides the vault, and Branvil never writes these values to its
//! data directory. (Collection cursors are signed with a key made per
//! process; the owner's token stays a file, since reading it is what proves
//! the owner.)

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use anyhow::Context;
use aws_lc_rs::aead::{AES_256_GCM, Aad, LessSafeKey, NONCE_LEN, Nonce, UnboundKey};
use serde::{Deserialize, Serialize};

use crate::util::{b64, fold, hex, random_bytes, sha256, unb64};

/// A store of named secrets. Names are `[a-z0-9_]`, at most 64 bytes, and
/// tell one data directory's secrets from another's (`Secrets`). Errors say
/// why without the value. A vault that cannot keep a value refuses: it
/// never keeps it in a weaker place.
pub trait Vault: Send + Sync {
    fn get(&self, name: &str) -> anyhow::Result<Option<String>>;
    fn set(&self, name: &str, value: &str) -> anyhow::Result<()>;
    fn delete(&self, name: &str) -> anyhow::Result<()>;
}

/// Keeps values in memory only, for a service whose secrets end with it.
#[derive(Default)]
pub struct Memory(Mutex<BTreeMap<String, String>>);

impl Vault for Memory {
    fn get(&self, name: &str) -> anyhow::Result<Option<String>> {
        Ok(self.0.lock().unwrap().get(name).cloned())
    }
    fn set(&self, name: &str, value: &str) -> anyhow::Result<()> {
        self.0.lock().unwrap().insert(name.into(), value.into());
        Ok(())
    }
    fn delete(&self, name: &str) -> anyhow::Result<()> {
        self.0.lock().unwrap().remove(name);
        Ok(())
    }
}

/// The file `Sealed::from_env` keeps values in.
pub const VAULT_ENV: &str = "BRANVIL_VAULT";
/// Its AES-256-GCM key: 32 bytes, base64url without padding.
pub const VAULT_KEY_ENV: &str = "BRANVIL_VAULT_KEY";
const AAD: &[u8] = b"branvil.vault.v1";

/// The `branvil` executable's vault: one file of every value, sealed with a
/// key that is not on disk. Without them it keeps nothing.
pub struct Sealed {
    file: Result<(PathBuf, LessSafeKey), String>,
    /// Reads and writes of the file, one at a time.
    lock: Mutex<()>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct SealedFile {
    nonce: String,
    ciphertext: String,
}

impl Sealed {
    pub fn new(path: &Path, key: &[u8]) -> anyhow::Result<Self> {
        let key = UnboundKey::new(&AES_256_GCM, key)
            .map_err(|_| anyhow::anyhow!("{VAULT_KEY_ENV} must be 32 bytes"))?;
        Ok(Self {
            file: Ok((path.to_path_buf(), LessSafeKey::new(key))),
            lock: Mutex::default(),
        })
    }

    /// The vault `BRANVIL_VAULT` and `BRANVIL_VAULT_KEY` name, or one that
    /// refuses every use, saying which is missing.
    pub fn from_env() -> Self {
        let named = || -> Result<Self, String> {
            let path = std::env::var_os(VAULT_ENV)
                .filter(|p| !p.is_empty())
                .ok_or_else(|| format!("{VAULT_ENV} names no vault file"))?;
            let key = std::env::var(VAULT_KEY_ENV)
                .ok()
                .and_then(|k| unb64(k.trim()))
                .ok_or_else(|| format!("{VAULT_KEY_ENV} holds no base64url key"))?;
            Self::new(Path::new(&path), &key).map_err(|e| e.to_string())
        };
        named().unwrap_or_else(|why| Self {
            file: Err(format!("this service has no vault: {why}")),
            lock: Mutex::default(),
        })
    }

    fn file(&self) -> anyhow::Result<&(PathBuf, LessSafeKey)> {
        self.file.as_ref().map_err(|why| anyhow::anyhow!("{why}"))
    }

    fn read(&self) -> anyhow::Result<BTreeMap<String, String>> {
        let (path, key) = self.file()?;
        let bytes = match std::fs::read(path) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
            Err(_) => anyhow::bail!("the vault file cannot be read"),
        };
        let sealed: SealedFile =
            serde_json::from_slice(&bytes).context("the vault file is malformed")?;
        let nonce = unb64(&sealed.nonce)
            .and_then(|n| <[u8; NONCE_LEN]>::try_from(n).ok())
            .context("the vault file is malformed")?;
        let mut data = unb64(&sealed.ciphertext).context("the vault file is malformed")?;
        let plain = key
            .open_in_place(
                Nonce::assume_unique_for_key(nonce),
                Aad::from(AAD),
                &mut data,
            )
            .map_err(|_| anyhow::anyhow!("the vault file does not open with {VAULT_KEY_ENV}"))?;
        serde_json::from_slice(plain).context("the vault file is malformed")
    }

    fn update(&self, apply: impl FnOnce(&mut BTreeMap<String, String>)) -> anyhow::Result<()> {
        let _held = self.lock.lock().unwrap();
        let mut values = self.read()?;
        apply(&mut values);
        let (path, key) = self.file()?;
        let nonce = random_bytes::<NONCE_LEN>();
        let mut data = serde_json::to_vec(&values)?;
        key.seal_in_place_append_tag(
            Nonce::assume_unique_for_key(nonce),
            Aad::from(AAD),
            &mut data,
        )
        .map_err(|_| anyhow::anyhow!("the vault cannot seal"))?;
        let sealed = serde_json::to_vec(&SealedFile {
            nonce: b64(&nonce),
            ciphertext: b64(&data),
        })?;
        crate::config::write_secret(path, &sealed).context("the vault file cannot be written")
    }
}

impl Vault for Sealed {
    fn get(&self, name: &str) -> anyhow::Result<Option<String>> {
        let _held = self.lock.lock().unwrap();
        Ok(self.read()?.remove(name))
    }
    fn set(&self, name: &str, value: &str) -> anyhow::Result<()> {
        self.update(|values| {
            values.insert(name.into(), value.into());
        })
    }
    fn delete(&self, name: &str) -> anyhow::Result<()> {
        self.update(|values| {
            values.remove(name);
        })
    }
}

/// The service's secrets in its vault, under names of its data directory:
/// the vault may hold another directory's too.
#[derive(Clone)]
pub struct Secrets {
    vault: Arc<dyn Vault>,
    place: String,
}

impl Secrets {
    /// `data_dir` exists: it is named by its canonical path, so another
    /// spelling of it (a trailing `/`, a symbolic link) finds the same secrets.
    pub fn new(vault: Arc<dyn Vault>, data_dir: &Path) -> Self {
        let dir = std::fs::canonicalize(data_dir).unwrap_or_else(|_| data_dir.to_path_buf());
        Self {
            vault,
            place: dir.to_string_lossy().into_owned(),
        }
    }

    fn name(&self, kind: &str, parts: &[&str]) -> String {
        let mut text = self.place.clone();
        for part in parts {
            text.push('\0');
            text.push_str(part);
        }
        format!("{kind}_{}", &hex(&sha256(text.as_bytes()))[..32])
    }

    /// A member's credential on a host.
    pub fn account(&self, member: &str, provider: &str) -> String {
        self.name(
            &format!("account_{provider}"),
            &["account", &fold(member), provider],
        )
    }

    /// The webhook told of automatic publishes.
    pub fn webhook(&self) -> String {
        self.name("webhook", &["webhook"])
    }

    async fn run<T: Send + 'static>(
        &self,
        op: impl FnOnce(&dyn Vault) -> anyhow::Result<T> + Send + 'static,
    ) -> Result<T, String> {
        let vault = self.vault.clone();
        tokio::task::spawn_blocking(move || op(vault.as_ref()))
            .await
            .map_err(|_| "the vault stopped".to_string())?
            .map_err(|e| format!("{e:#}"))
    }

    pub async fn get(&self, name: String) -> Result<Option<String>, String> {
        self.run(move |v| v.get(&name)).await
    }
    pub async fn set(&self, name: String, value: String) -> Result<(), String> {
        self.run(move |v| v.set(&name, &value)).await
    }
    pub async fn delete(&self, name: String) -> Result<(), String> {
        self.run(move |v| v.delete(&name)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sealed_vault_keeps_values_only_as_ciphertext_and_refuses_what_it_cannot_open() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("vault");
        let key = random_bytes::<32>();
        let vault = Sealed::new(&path, &key).unwrap();
        assert_eq!(vault.get("webhook_x").unwrap(), None);
        vault
            .set("webhook_x", "https://hooks.example/secret-1")
            .unwrap();
        vault.set("account_github_y", "secret-2").unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("secret"));
        let again = Sealed::new(&path, &key).unwrap();
        assert_eq!(
            again.get("webhook_x").unwrap().as_deref(),
            Some("https://hooks.example/secret-1")
        );
        again.delete("webhook_x").unwrap();
        assert_eq!(again.get("webhook_x").unwrap(), None);

        // Another key, a corrupted file, or no key at all: refused, saying why.
        let other = Sealed::new(&path, &random_bytes::<32>()).unwrap();
        let error = other.get("account_github_y").unwrap_err().to_string();
        assert!(error.contains("does not open"), "{error}");
        assert!(other.set("account_github_y", "secret-3").is_err());
        assert!(!String::from_utf8_lossy(&std::fs::read(&path).unwrap()).contains("secret"));
        std::fs::write(&path, b"{\"nonce\":").unwrap();
        let error = again.get("account_github_y").unwrap_err().to_string();
        assert!(error.contains("malformed"), "{error}");
        assert!(again.set("account_github_y", "secret-4").is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"{\"nonce\":");
        let none = Sealed {
            file: Err("this service has no vault: BRANVIL_VAULT names no vault file".into()),
            lock: Mutex::default(),
        };
        let error = none.set("webhook_x", "secret-5").unwrap_err().to_string();
        assert!(error.contains("no vault"), "{error}");
        assert!(Sealed::new(&path, &[0; 16]).is_err());
    }

    #[test]
    fn names_tell_data_directories_members_and_providers_apart() {
        let dir = tempfile::tempdir().unwrap();
        let (a_dir, b_dir) = (dir.path().join("a"), dir.path().join("b"));
        std::fs::create_dir(&a_dir).unwrap();
        std::fs::create_dir(&b_dir).unwrap();
        let vault: Arc<dyn Vault> = Arc::new(Memory::default());
        let a = Secrets::new(vault.clone(), &a_dir);
        let b = Secrets::new(vault.clone(), &b_dir);
        // Other spellings of a directory name it the same.
        #[cfg(unix)]
        std::os::unix::fs::symlink(&a_dir, dir.path().join("link")).unwrap();
        #[cfg(unix)]
        for spelling in [
            dir.path().join("a/"),
            dir.path().join("b/../a"),
            dir.path().join("link"),
        ] {
            let same = Secrets::new(vault.clone(), &spelling);
            assert_eq!(same.webhook(), a.webhook(), "{}", spelling.display());
            assert_eq!(same.account("aiko", "github"), a.account("aiko", "github"));
        }
        let names = [
            a.account("aiko", "github"),
            a.account("Aiko", "github"),
            a.account("ken", "github"),
            a.account("aiko", "gitlab"),
            a.webhook(),
            b.account("aiko", "github"),
            b.webhook(),
        ];
        // Member names fold as everywhere else; everything else differs.
        assert_eq!(names[0], names[1]);
        let distinct: std::collections::BTreeSet<_> = names.iter().collect();
        assert_eq!(distinct.len(), names.len() - 1);
        for name in &names {
            assert!(name.len() <= 64, "{name}");
            assert!(
                name.bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_'),
                "{name}"
            );
        }
        assert!(names[0].starts_with("account_github_"));
        assert!(names[4].starts_with("webhook_"));
    }
}
