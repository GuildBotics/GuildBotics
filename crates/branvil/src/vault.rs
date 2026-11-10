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

use crate::util::{fold, hex, random_bytes, sha256, unb64};

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

/// The directory `Sealed::from_env` keeps values in.
pub const VAULT_ENV: &str = "BRANVIL_VAULT";
/// Its AES-256-GCM key: 32 bytes, base64url without padding.
pub const VAULT_KEY_ENV: &str = "BRANVIL_VAULT_KEY";

/// The `branvil` executable's vault: a directory with one file per name, each
/// sealed with a key that is not on disk and bound to its name. A write
/// replaces one file whole, so services sharing the directory never lose
/// each other's values. Without the directory or the key it keeps nothing.
pub struct Sealed(Result<(PathBuf, LessSafeKey), String>);

impl Sealed {
    pub fn new(dir: &Path, key: &[u8]) -> anyhow::Result<Self> {
        let key = UnboundKey::new(&AES_256_GCM, key)
            .map_err(|_| anyhow::anyhow!("{VAULT_KEY_ENV} must be 32 bytes"))?;
        Ok(Self(Ok((dir.to_path_buf(), LessSafeKey::new(key)))))
    }

    /// The vault `BRANVIL_VAULT` and `BRANVIL_VAULT_KEY` name, or one that
    /// refuses every use, saying which is missing.
    pub fn from_env() -> Self {
        Self::from_env_with(
            std::env::var_os(VAULT_ENV),
            std::env::var(VAULT_KEY_ENV).ok(),
        )
    }

    fn from_env_with(dir: Option<std::ffi::OsString>, key: Option<String>) -> Self {
        let named = || -> Result<Self, String> {
            let dir = dir
                .filter(|p| !p.is_empty())
                .ok_or_else(|| format!("{VAULT_ENV} names no vault directory"))?;
            let key = key
                .and_then(|k| unb64(k.trim()))
                .ok_or_else(|| format!("{VAULT_KEY_ENV} holds no base64url key"))?;
            Self::new(Path::new(&dir), &key).map_err(|e| e.to_string())
        };
        named().unwrap_or_else(|why| Self(Err(format!("this service has no vault: {why}"))))
    }

    /// The file of `name`, and the key.
    fn entry(&self, name: &str) -> anyhow::Result<(PathBuf, &LessSafeKey)> {
        let (dir, key) = self.0.as_ref().map_err(|why| anyhow::anyhow!("{why}"))?;
        anyhow::ensure!(
            !name.is_empty()
                && name
                    .bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_'),
            "not a vault name"
        );
        Ok((dir.join(name), key))
    }
}

fn aad(name: &str) -> Vec<u8> {
    [b"branvil.vault.v1\0".as_slice(), name.as_bytes()].concat()
}

impl Vault for Sealed {
    fn get(&self, name: &str) -> anyhow::Result<Option<String>> {
        let (path, key) = self.entry(name)?;
        let mut data = match std::fs::read(&path) {
            Ok(data) => data,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => anyhow::bail!("the vault file cannot be read"),
        };
        anyhow::ensure!(
            data.len() >= NONCE_LEN + AES_256_GCM.tag_len(),
            "the vault file is malformed"
        );
        let mut nonce = [0; NONCE_LEN];
        nonce.copy_from_slice(&data[..NONCE_LEN]);
        let plain = key
            .open_in_place(
                Nonce::assume_unique_for_key(nonce),
                Aad::from(aad(name)),
                &mut data[NONCE_LEN..],
            )
            .map_err(|_| anyhow::anyhow!("the vault file does not open with {VAULT_KEY_ENV}"))?;
        String::from_utf8(plain.to_vec())
            .map(Some)
            .map_err(|_| anyhow::anyhow!("the vault file is malformed"))
    }

    fn set(&self, name: &str, value: &str) -> anyhow::Result<()> {
        use std::io::Write;
        // A value it cannot open (another key, a damaged file) stays: the
        // right key still reads it.
        self.get(name)?;
        let (path, key) = self.entry(name)?;
        let nonce = random_bytes::<NONCE_LEN>();
        let mut data = value.as_bytes().to_vec();
        key.seal_in_place_append_tag(
            Nonce::assume_unique_for_key(nonce),
            Aad::from(aad(name)),
            &mut data,
        )
        .map_err(|_| anyhow::anyhow!("the vault cannot seal"))?;
        let dir = path.parent().expect("a vault file has its directory");
        let mut made = std::fs::DirBuilder::new();
        made.recursive(true);
        #[cfg(unix)]
        std::os::unix::fs::DirBuilderExt::mode(&mut made, 0o700);
        made.create(dir)
            .context("the vault directory cannot be made")?;
        // A name of its own: never the file it replaces, nor another writer's.
        let temporary = dir.join(format!(".{name}.{}.tmp", hex(&random_bytes::<8>())));
        let written = (|| {
            let mut options = std::fs::OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
            let mut file = options.open(&temporary)?;
            file.write_all(&nonce)?;
            file.write_all(&data)?;
            file.sync_all()?;
            std::fs::rename(&temporary, &path)
        })();
        if written.is_err() {
            let _ = std::fs::remove_file(&temporary);
        }
        written.context("the vault file cannot be written")
    }

    fn delete(&self, name: &str) -> anyhow::Result<()> {
        self.get(name)?;
        let (path, _) = self.entry(name)?;
        match std::fs::remove_file(path) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
                Err(e).context("the vault file cannot be removed")
            }
            _ => Ok(()),
        }
    }
}

/// The service's secrets in its vault, under names of its data directory:
/// the vault may hold another directory's too.
#[derive(Clone)]
pub struct Secrets {
    vault: Arc<dyn Vault>,
    /// The data directory's path, as the OS spells it.
    place: Vec<u8>,
}

impl Secrets {
    /// `data_dir` exists: it is named by its canonical path, so another
    /// spelling of it (a trailing `/`, a symbolic link) finds the same secrets.
    pub fn new(vault: Arc<dyn Vault>, data_dir: &Path) -> Self {
        let dir = std::fs::canonicalize(data_dir).unwrap_or_else(|_| data_dir.to_path_buf());
        Self {
            vault,
            place: dir.into_os_string().into_encoded_bytes(),
        }
    }

    fn name(&self, kind: &str, parts: &[&str]) -> String {
        let mut text = self.place.clone();
        for part in parts {
            text.push(0);
            text.extend_from_slice(part.as_bytes());
        }
        format!("{kind}_{}", &hex(&sha256(&text))[..32])
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

    /// Every file under `dir`, read.
    fn contents(dir: &Path) -> Vec<(PathBuf, Vec<u8>)> {
        std::fs::read_dir(dir)
            .unwrap()
            .map(|e| {
                let path = e.unwrap().path();
                let bytes = std::fs::read(&path).unwrap();
                (path, bytes)
            })
            .collect()
    }

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
        // Services on the same directory (other processes), writing at once:
        // neither loses what the other keeps.
        let other_service = Sealed::new(&path, &key).unwrap();
        std::thread::scope(|scope| {
            for (vault, side) in [(&vault, "a"), (&other_service, "b")] {
                scope.spawn(move || {
                    for i in 0..40 {
                        vault
                            .set(&format!("account_{side}_{i}"), "secret-n")
                            .unwrap();
                    }
                });
            }
        });
        for side in ["a", "b"] {
            for i in 0..40 {
                let name = format!("account_{side}_{i}");
                assert_eq!(vault.get(&name).unwrap().as_deref(), Some("secret-n"));
            }
        }
        other_service.set("account_github_y", "secret-2").unwrap();
        vault.set("account_github_z", "secret-3").unwrap();
        for (name, value) in [
            ("webhook_x", "https://hooks.example/secret-1"),
            ("account_github_y", "secret-2"),
            ("account_github_z", "secret-3"),
        ] {
            assert_eq!(other_service.get(name).unwrap().as_deref(), Some(value));
        }
        for (file, bytes) in contents(&path) {
            assert!(
                !String::from_utf8_lossy(&bytes).contains("secret"),
                "{file:?}"
            );
        }
        vault.delete("webhook_x").unwrap();
        assert_eq!(vault.get("webhook_x").unwrap(), None);
        vault.delete("webhook_x").unwrap();

        // A value moved to another name, another key, or a damaged file:
        // reading, replacing, and removing it are refused, saying why, and it
        // stays as it was: the right key still reads a value of its own.
        std::fs::copy(path.join("account_github_y"), path.join("webhook_x")).unwrap();
        let error = vault.get("webhook_x").unwrap_err().to_string();
        assert!(error.contains("does not open"), "{error}");
        let other = Sealed::new(&path, &random_bytes::<32>()).unwrap();
        let error = other.get("account_github_y").unwrap_err().to_string();
        assert!(error.contains("does not open"), "{error}");
        let error = other.set("account_github_y", "secret-x").unwrap_err();
        assert!(error.to_string().contains("does not open"), "{error}");
        let error = other.delete("account_github_y").unwrap_err();
        assert!(error.to_string().contains("does not open"), "{error}");
        assert_eq!(
            vault.get("account_github_y").unwrap().as_deref(),
            Some("secret-2")
        );
        std::fs::write(path.join("account_github_z"), b"short").unwrap();
        let error = vault.get("account_github_z").unwrap_err().to_string();
        assert!(error.contains("malformed"), "{error}");
        assert!(vault.set("account_github_z", "secret-y").is_err());
        assert!(vault.delete("account_github_z").is_err());
        assert_eq!(
            std::fs::read(path.join("account_github_z")).unwrap(),
            b"short"
        );
        // Only `[a-z0-9_]` names: none is a path, nor a writer's temporary
        // file (`.<name>.<random>.tmp`), nor the file it replaces.
        for name in ["../escape", "Upper", "webhook_x.tmp", ".webhook_x"] {
            assert!(vault.get(name).is_err() && vault.set(name, "secret-4").is_err());
        }
        let none = Sealed::from_env_with(None, None);
        let error = none.set("webhook_x", "secret-5").unwrap_err().to_string();
        assert!(error.contains("no vault"), "{error}");
        assert!(Sealed::new(&path, &[0; 16]).is_err());

        // A write that cannot finish leaves the value it would replace.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let tmp_named = dir.path().join("vault.tmp");
            let vault = Sealed::new(&tmp_named, &key).unwrap();
            vault.set("webhook_x", "secret-6").unwrap();
            std::fs::set_permissions(&tmp_named, std::fs::Permissions::from_mode(0o500)).unwrap();
            assert!(vault.set("webhook_x", "secret-7").is_err());
            std::fs::set_permissions(&tmp_named, std::fs::Permissions::from_mode(0o700)).unwrap();
            assert_eq!(vault.get("webhook_x").unwrap().as_deref(), Some("secret-6"));
            assert_eq!(contents(&tmp_named).len(), 1, "no temporary file is left");
        }
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
        // Paths that differ only in bytes that are not UTF-8 stay apart.
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStrExt;
            let at = |byte: u8| {
                let path = dir.path().join(std::ffi::OsStr::from_bytes(&[b'd', byte]));
                Secrets::new(vault.clone(), &path)
            };
            assert_ne!(at(0x80).webhook(), at(0x81).webhook());
            assert_ne!(
                at(0x80).account("aiko", "github"),
                at(0x81).account("aiko", "github")
            );
        }
        assert!(names[4].starts_with("webhook_"));
    }
}
