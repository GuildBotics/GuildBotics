//! Host-only encrypted secrets. The keychain holds one immutable device key;
//! config holds names only. No operation falls back to plaintext storage.
//!
//! Each root's `.guildbotics/local/secrets.enc.json` is a version-1 record
//! with base64 nonce (12 bytes) and ciphertext (including the GCM tag).
//! Its plaintext is a JSON object of name/string-value pairs. The AAD is
//! `guildbotics.secrets.v1\0<store_id>`, binding it to local/secrets.json.
//! The device key is Python credential_vault's base64 32-byte key, under
//! GuildBotics / agent-environment-credentials and the same device-wide lock.
//! Updates replace ciphertext, namespace, then names under the shared lock.
//! Interrupted multi-file writes that disagree stop rather than lose values.
//! Moving legacy keychain values is a separate, explicit migration operation.
use aes_gcm::{
    Aes256Gcm, KeyInit, Nonce,
    aead::{Aead, AeadCore, OsRng, Payload},
};
use anyhow::{Context, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD as B64};
use gb_base::{
    files::{FileLock, SHARED_WRITE_TIMEOUT, read_if_present},
    shared_writes::SharedWrites,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};
use zeroize::Zeroizing;

const SERVICE: &str = "GuildBotics";
const ACCOUNT: &str = "agent-environment-credentials";

/// Implementations must not include secret data in errors. Creation must never
/// replace an existing item; callers serialize first use with the Python lock.
pub trait Keychain: Send + Sync {
    fn read(&self) -> anyhow::Result<Option<String>>;
    fn create(&self, encoded_key: &str) -> anyhow::Result<()>;
}

pub struct SystemKeychain;
impl Keychain for SystemKeychain {
    fn read(&self) -> anyhow::Result<Option<String>> {
        let entry = keyring::Entry::new(SERVICE, ACCOUNT)
            .map_err(|_| anyhow::anyhow!("OS keychain unavailable"))?;
        match entry.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => bail!("OS keychain locked or unavailable"),
        }
    }
    fn create(&self, encoded_key: &str) -> anyhow::Result<()> {
        #[cfg(target_os = "macos")]
        {
            // Add, never set: set_generic_password updates an existing item.
            security_framework::os::macos::keychain::SecKeychain::default()
                .and_then(|chain| {
                    chain.add_generic_password(SERVICE, ACCOUNT, encoded_key.as_bytes())
                })
                .map_err(|_| anyhow::anyhow!("OS keychain could not create device key"))?;
        }
        #[cfg(not(target_os = "macos"))]
        {
            ensure!(self.read()?.is_none(), "device key already exists");
            keyring::Entry::new(SERVICE, ACCOUNT)
                .and_then(|entry| entry.set_password(encoded_key))
                .map_err(|_| anyhow::anyhow!("OS keychain could not create device key"))?;
        }
        Ok(())
    }
}

/// Explicit roots keep the machine key lock separate from workspace state.
pub struct DeviceKey<'a> {
    pub machine_root: PathBuf,
    pub keychain: &'a dyn Keychain,
}
impl DeviceKey<'_> {
    fn read(&self) -> anyhow::Result<Option<Zeroizing<Vec<u8>>>> {
        let stored = self
            .keychain
            .read()
            .map_err(|_| anyhow::anyhow!("OS keychain locked or unavailable"))?;
        stored
            .map(|stored| {
                let stored = Zeroizing::new(stored);
                let key = Zeroizing::new(
                    B64.decode(stored.as_bytes())
                        .map_err(|_| anyhow::anyhow!("device key malformed"))?,
                );
                ensure!(key.len() == 32, "device key malformed");
                Ok(key)
            })
            .transpose()
    }
    fn key(&self, create: bool) -> anyhow::Result<Zeroizing<Vec<u8>>> {
        if let Some(key) = self.read()? {
            return Ok(key);
        }
        ensure!(create, "device key missing");
        let _lock = FileLock::hold(
            &self.machine_root.join("data/agent_environment/key.lock"),
            SHARED_WRITE_TIMEOUT,
        )?;
        if let Some(key) = self.read()? {
            return Ok(key);
        }
        let key = Zeroizing::new(Aes256Gcm::generate_key(&mut OsRng).to_vec());
        let encoded = Zeroizing::new(B64.encode(&key));
        self.keychain
            .create(&encoded)
            .map_err(|_| anyhow::anyhow!("OS keychain could not create device key"))?;
        Ok(key)
    }
}

#[derive(Clone, Copy)]
pub enum Scope {
    Identity,
    Workspace,
}

/// A store borrows the shared writer configuration; all reads and updates
/// hold its identity/workspace locks for the entire operation.
pub struct SecretStore<'a> {
    writes: &'a SharedWrites,
    root: PathBuf,
    device: DeviceKey<'a>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Sealed {
    version: u8,
    nonce: String,
    ciphertext: String,
}

/// Read Python's name-only index. Existing invalid files are never empty.
pub fn read_secret_index(config: &Path) -> anyhow::Result<Vec<String>> {
    let Some(raw) = read_if_present(&config.join("secrets.yml"))? else {
        return Ok(vec![]);
    };
    let value = std::str::from_utf8(&raw)
        .ok()
        .and_then(|text| crate::yaml::value(text).ok())
        .ok_or_else(|| anyhow::anyhow!("secret index unreadable"))?;
    let obj = value
        .as_object()
        .context("secret index must contain only keys")?;
    ensure!(obj.len() == 1, "secret index must contain only keys");
    let names = obj
        .get("keys")
        .and_then(Value::as_array)
        .context("secret index must contain only keys")?;
    names
        .iter()
        .map(|v| {
            v.as_str()
                .map(str::to_owned)
                .context("secret index must contain names")
        })
        .collect::<anyhow::Result<BTreeSet<_>>>()
        .map(|names| names.into_iter().collect())
}

/// Read Python's namespace without changing or discarding it.
pub fn read_store_id(local: &Path) -> anyhow::Result<Option<String>> {
    let Some(raw) = read_if_present(&local.join("secrets.json"))? else {
        return Ok(None);
    };
    let value: Value =
        serde_json::from_slice(&raw).map_err(|_| anyhow::anyhow!("secret namespace unreadable"))?;
    let id = value
        .get("store_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .context("secret namespace missing store_id")?;
    Ok(Some(id.to_owned()))
}

fn valid_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 128
        && key.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
}

impl<'a> SecretStore<'a> {
    pub fn new(
        writes: &'a SharedWrites,
        scope: Scope,
        device: DeviceKey<'a>,
    ) -> anyhow::Result<Self> {
        let root = match scope {
            Scope::Identity => writes.identity.clone(),
            Scope::Workspace => writes.workspace.clone().context("No workspace selected")?,
        }
        .join(".guildbotics");
        Ok(Self {
            writes,
            root,
            device,
        })
    }
    fn aad(id: &str) -> String {
        format!("guildbotics.secrets.v1\0{id}")
    }
    fn load(&self) -> anyhow::Result<(Option<String>, BTreeMap<String, String>)> {
        let names = read_secret_index(&self.root.join("config"))?;
        let id = read_store_id(&self.root.join("local"))?;
        let raw = read_if_present(&self.root.join("local/secrets.enc.json"))?;
        let Some(raw) = raw else {
            ensure!(
                names.is_empty(),
                "encrypted secret file missing; indexed values unavailable"
            );
            return Ok((id, BTreeMap::new()));
        };
        let id = id.context("encrypted secret file has no namespace")?;
        let key = self.device.key(false)?;
        let sealed: Sealed = serde_json::from_slice(&raw)
            .map_err(|_| anyhow::anyhow!("encrypted secret file malformed"))?;
        ensure!(sealed.version == 1, "encrypted secret version unsupported");
        let nonce = B64
            .decode(sealed.nonce)
            .map_err(|_| anyhow::anyhow!("encrypted secret nonce malformed"))?;
        let nonce: [u8; 12] = nonce
            .try_into()
            .map_err(|_| anyhow::anyhow!("encrypted secret nonce malformed"))?;
        let nonce = Nonce::from(nonce);
        let ciphertext = B64
            .decode(sealed.ciphertext)
            .map_err(|_| anyhow::anyhow!("encrypted secret ciphertext malformed"))?;
        let cipher = Aes256Gcm::new_from_slice(&key).expect("32 byte key");
        let plain = Zeroizing::new(
            cipher
                .decrypt(
                    &nonce,
                    Payload {
                        msg: &ciphertext,
                        aad: Self::aad(&id).as_bytes(),
                    },
                )
                .map_err(|_| anyhow::anyhow!("encrypted secret authentication failed"))?,
        );
        let values: BTreeMap<String, String> = serde_json::from_slice(&plain)
            .map_err(|_| anyhow::anyhow!("encrypted secret payload malformed"))?;
        ensure!(
            values.keys().all(|k| valid_key(k)),
            "encrypted secret payload has invalid names"
        );
        ensure!(
            values.keys().eq(names.iter()),
            "secret index and encrypted file disagree"
        );
        Ok((Some(id), values))
    }
    pub fn values(&self) -> anyhow::Result<BTreeMap<String, String>> {
        self.writes
            .transaction(|_| self.load().map(|(_, values)| values))
    }
    pub fn keys(&self) -> anyhow::Result<Vec<String>> {
        Ok(self.values()?.into_keys().collect())
    }
    pub fn get(&self, key: &str) -> anyhow::Result<Option<String>> {
        Ok(self.values()?.remove(key))
    }
    pub fn set(&self, key: &str, value: &str) -> anyhow::Result<()> {
        self.set_many(&BTreeMap::from([(key.to_owned(), value.to_owned())]))
    }
    pub fn set_many(&self, values: &BTreeMap<String, String>) -> anyhow::Result<()> {
        ensure!(
            values.keys().all(|key| valid_key(key)),
            "invalid secret key name"
        );
        self.update(|current| current.extend(values.clone()))
    }
    pub fn delete(&self, key: &str) -> anyhow::Result<()> {
        self.update(|current| {
            current.remove(key);
        })
    }
    pub fn ensure_initialized(&self) -> anyhow::Result<()> {
        self.update(|_| ())
    }
    fn update(&self, apply: impl FnOnce(&mut BTreeMap<String, String>)) -> anyhow::Result<()> {
        self.writes.transaction(|writer| {
            let (id, mut values) = self.load()?;
            let id = id.unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
            apply(&mut values);
            let key = self.device.key(true)?;
            let cipher = Aes256Gcm::new_from_slice(&key).expect("32 byte key");
            let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
            let plain = Zeroizing::new(serde_json::to_vec(&values)?);
            let ciphertext = cipher
                .encrypt(
                    &nonce,
                    Payload {
                        msg: &plain,
                        aad: Self::aad(&id).as_bytes(),
                    },
                )
                .map_err(|_| anyhow::anyhow!("secret encryption failed"))?;
            // Only ciphertext ever reaches an atomic writer or temporary file.
            writer.write_json(
                &self.root.join("local/secrets.enc.json"),
                &Sealed {
                    version: 1,
                    nonce: B64.encode(nonce),
                    ciphertext: B64.encode(ciphertext),
                },
            )?;
            writer.write_json(
                &self.root.join("local/secrets.json"),
                &json!({"schema_version": 1, "store_id": id}),
            )?;
            // JSON is a YAML subset and quotes all key names unambiguously.
            writer.write_json(
                &self.root.join("config/secrets.yml"),
                &json!({"keys": values.keys().collect::<Vec<_>>()}),
            )?;
            Ok(())
        })
    }
}
