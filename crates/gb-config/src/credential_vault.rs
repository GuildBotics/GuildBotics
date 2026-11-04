//! Device-local AI CLI logins, sealed with the existing OS keychain key.
use crate::secrets::DeviceKey;
use aes_gcm::{
    Aes256Gcm, KeyInit, Nonce,
    aead::{Aead, AeadCore, OsRng, Payload},
};
use anyhow::{Context, ensure};
use base64::{Engine, engine::general_purpose::STANDARD as B64};
use std::{collections::BTreeMap, path::Path};
use zeroize::Zeroizing;

/// File names are relative to the tool's state root, never host paths.
pub type Files = BTreeMap<String, Vec<u8>>;

fn aad(label: &str) -> String {
    format!("guildbotics.agent-credentials.v1\0{label}")
}
fn checked(files: &Files) -> anyhow::Result<()> {
    ensure!(
        files.keys().all(|name| !name.is_empty()
            && !name.contains('\\')
            && !name.contains('\0')
            && Path::new(name)
                .components()
                .all(|c| matches!(c, std::path::Component::Normal(_)))),
        "invalid credential file name"
    );
    Ok(())
}

/// A caller that can refresh the login holds this lock for the entire turn,
/// through resealing, so refresh tokens cannot be spent twice concurrently.
pub fn hold(
    path: &Path,
    timeout: std::time::Duration,
) -> std::io::Result<gb_base::files::FileLock> {
    gb_base::files::FileLock::hold(&path.with_extension("lock"), timeout)
}

pub fn seal(device: &DeviceKey<'_>, path: &Path, label: &str, files: &Files) -> anyhow::Result<()> {
    checked(files)?;
    let key = device.key(true)?;
    let encoded: BTreeMap<_, _> = files
        .iter()
        .map(|(k, v)| (k.clone(), B64.encode(v)))
        .collect();
    let plaintext = Zeroizing::new(serde_json::to_vec(&encoded)?);
    let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
    let cipher = Aes256Gcm::new_from_slice(&key).expect("device key length checked");
    let ciphertext = cipher
        .encrypt(
            &nonce,
            Payload {
                msg: &plaintext,
                aad: aad(label).as_bytes(),
            },
        )
        .map_err(|_| anyhow::anyhow!("credentials could not be sealed"))?;
    gb_base::files::atomic_write(
        path,
        &serde_json::to_vec(
            &serde_json::json!({"version":1,"nonce":B64.encode(nonce),"ciphertext":B64.encode(ciphertext)}),
        )?,
    )?;
    Ok(())
}

pub fn unseal(device: &DeviceKey<'_>, path: &Path, label: &str) -> anyhow::Result<Option<Files>> {
    let Some(raw) = gb_base::files::read_if_present(path)? else {
        return Ok(None);
    };
    let key = device.key(false)?;
    let value: serde_json::Value = serde_json::from_slice(&raw).context("credentials malformed")?;
    ensure!(value["version"] == 1, "credentials version unsupported");
    let nonce: [u8; 12] = B64
        .decode(
            value["nonce"]
                .as_str()
                .context("credentials nonce missing")?,
        )
        .context("credentials nonce malformed")?
        .try_into()
        .map_err(|_| anyhow::anyhow!("credentials nonce malformed"))?;
    let encrypted = B64
        .decode(
            value["ciphertext"]
                .as_str()
                .context("credentials ciphertext missing")?,
        )
        .context("credentials ciphertext malformed")?;
    let cipher = Aes256Gcm::new_from_slice(&key).expect("device key length checked");
    let plaintext = Zeroizing::new(
        cipher
            .decrypt(
                &Nonce::from(nonce),
                Payload {
                    msg: &encrypted,
                    aad: aad(label).as_bytes(),
                },
            )
            .map_err(|_| anyhow::anyhow!("credentials authentication failed"))?,
    );
    let encoded: BTreeMap<String, String> =
        serde_json::from_slice(&plaintext).context("credentials payload malformed")?;
    let files: Files = encoded
        .into_iter()
        .map(|(name, value)| {
            Ok((
                name,
                B64.decode(value).context("credentials payload malformed")?,
            ))
        })
        .collect::<anyhow::Result<_>>()?;
    checked(&files)?;
    Ok(Some(files))
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fake;
    impl crate::secrets::Keychain for Fake {
        fn read(&self) -> anyhow::Result<Option<String>> {
            Ok(Some(B64.encode([13; 32])))
        }
        fn create(&self, _: &str) -> anyhow::Result<()> {
            panic!("key must already exist")
        }
    }
    #[test]
    fn sealed_files_are_bound_to_label_and_corruption_never_falls_back() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("credentials.json");
        let device = DeviceKey {
            machine_root: dir.path().into(),
            keychain: &Fake,
        };
        assert!(
            unseal(&device, &path, "tool/account/format")
                .unwrap()
                .is_none()
        );
        let files = Files::from([("auth.json".into(), b"synthetic credential".to_vec())]);
        seal(&device, &path, "tool/account/format", &files).unwrap();
        assert_eq!(
            unseal(&device, &path, "tool/account/format").unwrap(),
            Some(files)
        );
        assert!(unseal(&device, &path, "another/account/format").is_err());
        let raw = std::fs::read_to_string(&path).unwrap();
        assert!(!raw.contains("synthetic"));
        std::fs::write(&path, b"broken").unwrap();
        assert!(unseal(&device, &path, "tool/account/format").is_err());
        assert!(
            seal(
                &device,
                &path,
                "tool",
                &Files::from([("../auth".into(), vec![])])
            )
            .is_err()
        );
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct Status {
    pub state: &'static str,
    pub saved: bool,
    pub login_command: String,
}
pub fn status(device: &DeviceKey<'_>, path: &Path, label: &str, login_command: String) -> Status {
    let state = match unseal(device, path, label) {
        Ok(Some(_)) => "saved",
        Ok(None) => "missing",
        Err(error) => {
            let message = error.to_string();
            if message.contains("keychain") {
                "unavailable"
            } else {
                "corrupt"
            }
        }
    };
    Status {
        state,
        saved: state == "saved",
        login_command,
    }
}

/// The host's in-memory login while a provider may rotate its tokens. The
/// lock survives through the atomic save; callers never lend these bytes to
/// an agent turn. Login-only VMs may mount a temporary materialization.
pub struct Login<'a> {
    device: &'a DeviceKey<'a>,
    path: std::path::PathBuf,
    label: String,
    pub files: Files,
    _held: gb_base::files::FileLock,
}
impl<'a> Login<'a> {
    pub fn open(
        device: &'a DeviceKey<'a>,
        path: &Path,
        label: &str,
        timeout: std::time::Duration,
    ) -> anyhow::Result<Self> {
        let held = hold(path, timeout)?;
        let files = unseal(device, path, label)?.unwrap_or_default();
        Ok(Self {
            device,
            path: path.into(),
            label: label.into(),
            files,
            _held: held,
        })
    }
    pub fn save(&self) -> anyhow::Result<()> {
        seal(self.device, &self.path, &self.label, &self.files)
    }
    pub fn json(&self, name: &str) -> anyhow::Result<serde_json::Value> {
        serde_json::from_slice(self.files.get(name).context("credential file missing")?)
            .context("credential file malformed")
    }
    pub fn replace_json(&mut self, name: String, value: &serde_json::Value) -> anyhow::Result<()> {
        self.files.insert(name, serde_json::to_vec(value)?);
        checked(&self.files)
    }
}
impl Drop for Login<'_> {
    fn drop(&mut self) {
        for data in self.files.values_mut() {
            data.fill(0);
        }
    }
}
