//! Where the service keeps the secrets of other systems it uses: members'
//! upstream credentials and the webhook's URL. The process that runs the
//! service provides the vault, and Branvil never writes these values to its
//! data directory. (Collection cursors are signed with a key made per
//! process.)

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::util::{fold, hex, sha256};

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
