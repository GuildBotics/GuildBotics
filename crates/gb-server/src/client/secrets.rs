//! Host CLI operations on the workspace and identity secret stores.

use std::{
    collections::BTreeMap,
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};

use gb_config::secrets::{
    DeviceKey, Keychain, KeychainFailure, Scope, SecretStore, SystemKeychain,
};

use crate::{client, i18n};

pub enum Command {
    Status,
    List,
    Set {
        key: String,
        value: Option<String>,
        from_file: Option<PathBuf>,
    },
    Delete {
        key: String,
    },
    Export {
        file: Option<PathBuf>,
    },
    Import {
        file: PathBuf,
    },
}

pub async fn run(
    mut command: Command,
    workspace: Option<PathBuf>,
    identities: bool,
    prompt: impl FnOnce(&str) -> anyhow::Result<String>,
) -> anyhow::Result<()> {
    let language = client::language().await;
    let machine = gb_base::home::machine_root()?;
    let identity = gb_base::home::identity_root()?;
    let workspace = if identities {
        None
    } else {
        Some(crate::workspace::resolve(
            &machine.join("data"),
            workspace,
            language,
        )?)
    };
    let root = workspace.as_ref().unwrap_or(&identity);
    let writes = gb_base::shared_writes::SharedWrites::new(identity.clone(), workspace.clone());
    let keychain = SystemKeychain;
    let device = DeviceKey {
        machine_root: machine,
        keychain: &keychain,
    };
    let scope = if identities {
        Scope::Identity
    } else {
        Scope::Workspace
    };
    let store = SecretStore::new(&writes, scope, device)?;
    if let Command::Set {
        key,
        value,
        from_file: None,
    } = &mut command
        && value.is_none()
    {
        *value = Some(prompt(&i18n::t(
            language,
            "cli.secrets.prompt",
            &[("key", key)],
        ))?);
    }
    execute(command, &store, &keychain, root, &identity, scope, language)
}

fn execute(
    command: Command,
    store: &SecretStore<'_>,
    keychain: &dyn Keychain,
    root: &Path,
    identity: &Path,
    scope: Scope,
    language: &str,
) -> anyhow::Result<()> {
    let text =
        |key, params: &[(&str, &str)]| i18n::t(language, &format!("cli.secrets.{key}"), params);
    let store_error = |error: anyhow::Error| {
        anyhow::anyhow!(text("store_failed", &[("error", &error.to_string())]))
    };
    let set = |values: &BTreeMap<String, String>| -> anyhow::Result<()> {
        if matches!(scope, Scope::Workspace) {
            let prefixes = member_prefixes(identity)?;
            let misplaced = values
                .keys()
                .filter(|key| prefixes.iter().any(|p| key.starts_with(p)))
                .map(String::as_str)
                .collect::<Vec<_>>();
            anyhow::ensure!(
                misplaced.is_empty(),
                text("member_key", &[("keys", &misplaced.join(", "))])
            );
        }
        store.set_many(values).map_err(store_error)
    };
    match command {
        Command::Status => {
            let keychain_result = keychain.read().map(|_| ());
            let key_count = store
                .keys()
                .map(|keys| keys.len().to_string())
                .unwrap_or_else(|error| text("unknown_count", &[("error", &error.to_string())]));
            let locked = keychain_result
                .as_ref()
                .err()
                .and_then(|e| e.downcast_ref::<KeychainFailure>())
                == Some(&KeychainFailure::Locked);
            println!(
                "{}",
                text(
                    "status",
                    &[
                        ("workspace", &root.to_string_lossy()),
                        (
                            "available",
                            &text(
                                if keychain_result.is_ok() {
                                    "available"
                                } else {
                                    "unavailable"
                                },
                                &[]
                            )
                        ),
                        ("locked", &text(if locked { "yes" } else { "no" }, &[])),
                        (
                            "path",
                            &root
                                .join(".guildbotics/config/secrets.yml")
                                .to_string_lossy()
                        ),
                        ("count", &key_count),
                    ]
                )
            );
        }
        Command::List => {
            for key in store.keys().map_err(store_error)? {
                println!("{key}");
            }
        }
        Command::Set {
            key,
            value,
            from_file,
        } => {
            let value = match (value, from_file) {
                (Some(value), _) => value,
                (_, Some(path)) => fs::read_to_string(path)
                    .map_err(|_| anyhow::anyhow!(text("read_failed", &[])))?,
                _ => anyhow::bail!(text("read_failed", &[])),
            };
            set(&BTreeMap::from([(key.clone(), value)]))?;
            println!("{}", text("stored", &[("key", &key)]));
        }
        Command::Delete { key } => {
            store.delete(&key).map_err(store_error)?;
            println!("{}", text("deleted", &[("key", &key)]));
        }
        Command::Export { file } => {
            let values = store.values().map_err(store_error)?;
            let contents = dotenv(&values);
            if let Some(path) = file {
                let path = gb_base::host_path::normalize(&path)?;
                gb_base::files::atomic_write(&path, contents.as_bytes())?;
                println!(
                    "{}",
                    text(
                        "exported",
                        &[
                            ("count", &values.len().to_string()),
                            ("path", &path.to_string_lossy())
                        ]
                    )
                );
            } else {
                io::stdout().write_all(contents.as_bytes())?;
            }
        }
        Command::Import { file } => {
            let input =
                fs::File::open(&file).map_err(|_| anyhow::anyhow!(text("read_failed", &[])))?;
            let values =
                read_dotenv(input).map_err(|_| anyhow::anyhow!(text("invalid_dotenv", &[])))?;
            anyhow::ensure!(
                !values.is_empty(),
                text("empty_dotenv", &[("path", &file.to_string_lossy())])
            );
            set(&values)?;
            println!(
                "{}",
                text(
                    "imported",
                    &[
                        ("count", &values.len().to_string()),
                        ("path", &file.to_string_lossy())
                    ]
                )
            );
        }
    }
    Ok(())
}

fn member_prefixes(identity: &Path) -> anyhow::Result<Vec<String>> {
    let directory = identity.join(".guildbotics/config/team/members");
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(error) => return Err(error.into()),
    };
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry?;
        let name = entry.file_name();
        if let Some(name) = name.to_str()
            && gb_config::team::valid_person_id(name)
            && entry.file_type()?.is_dir()
            && entry.path().join("identity.yml").is_file()
        {
            names.push(format!("{}_", name.replace('-', "_").to_ascii_uppercase()));
        }
    }
    Ok(names)
}

fn read_dotenv(input: impl io::Read) -> Result<BTreeMap<String, String>, dotenvy::Error> {
    dotenvy::from_read_iter(input).collect()
}

fn dotenv(values: &BTreeMap<String, String>) -> String {
    let mut out = String::new();
    for (key, value) in values {
        out.push_str(key);
        out.push('=');
        if value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_./:@+-".contains(&byte))
        {
            out.push_str(value);
        } else {
            out.push('"');
            for character in value.chars() {
                match character {
                    '\\' | '"' | '$' => {
                        out.push('\\');
                        out.push(character);
                    }
                    '\n' => out.push_str("\\n"),
                    _ => out.push(character),
                }
            }
            out.push('"');
        }
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct MemoryKeychain(std::sync::Mutex<Option<String>>);
    impl Keychain for MemoryKeychain {
        fn read(&self) -> anyhow::Result<Option<String>> {
            Ok(self.0.lock().unwrap().clone())
        }
        fn create(&self, key: &str) -> anyhow::Result<()> {
            let mut stored = self.0.lock().unwrap();
            anyhow::ensure!(stored.is_none());
            *stored = Some(key.to_owned());
            Ok(())
        }
    }

    #[test]
    fn secrets_commands_keep_values_out_of_records_and_export_with_private_permissions() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        let identity = root.join("identity");
        let workspace = root.join("workspace");
        std::fs::create_dir_all(&identity).unwrap();
        std::fs::create_dir_all(&workspace).unwrap();
        let writes =
            gb_base::shared_writes::SharedWrites::new(identity.clone(), Some(workspace.clone()));
        let keychain = MemoryKeychain::default();
        let scope = Scope::Workspace;
        let store = SecretStore::new(
            &writes,
            scope,
            DeviceKey {
                machine_root: root.clone(),
                keychain: &keychain,
            },
        )
        .unwrap();
        let command = |command| {
            execute(
                command, &store, &keychain, &workspace, &identity, scope, "en",
            )
        };
        let source = root.join("source");
        std::fs::write(&source, "fake-private-value\nsecond line").unwrap();
        command(Command::Set {
            key: "SECRET".into(),
            value: None,
            from_file: Some(source),
        })
        .unwrap();
        let config = workspace.join(".guildbotics/config/secrets.yml");
        assert_eq!(store.keys().unwrap(), ["SECRET"]);
        assert!(
            !std::fs::read_to_string(&config)
                .unwrap()
                .contains("fake-private-value")
        );
        let exported = root.join("exported.env");
        command(Command::Export {
            file: Some(exported.clone()),
        })
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&exported).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        command(Command::Delete {
            key: "SECRET".into(),
        })
        .unwrap();
        assert!(store.keys().unwrap().is_empty());
        command(Command::Import { file: exported }).unwrap();
        assert_eq!(
            store.get("SECRET").unwrap().unwrap(),
            "fake-private-value\nsecond line"
        );
        std::fs::write(config, "broken: [").unwrap();
        let error = command(Command::List).unwrap_err().to_string();
        assert!(error.contains("could not be used"));
        assert!(!error.contains("fake-private-value"));
    }

    #[test]
    fn secrets_status_and_stdout_export_report_the_store_without_a_file() {
        const FIXTURE: &str = "GUILDBOTICS_TEST_SECRETS_STDOUT_ROOT";
        if let Some(root) = std::env::var_os(FIXTURE) {
            let root = PathBuf::from(root);
            let keychain = MemoryKeychain::default();
            let writes =
                gb_base::shared_writes::SharedWrites::new(root.clone(), Some(root.clone()));
            let scope = Scope::Workspace;
            let store = SecretStore::new(
                &writes,
                scope,
                DeviceKey {
                    machine_root: root.clone(),
                    keychain: &keychain,
                },
            )
            .unwrap();
            let secret = format!(
                "-----BEGIN PRIVATE KEY-----\n{}\n-----END PRIVATE KEY-----\n",
                "x".repeat(1679)
            );
            let source = root.join("import.env");
            std::fs::write(
                &source,
                dotenv(&BTreeMap::from([
                    ("OPENAI_API_KEY".into(), "sk-fixture".into()),
                    ("ALICE_GITHUB_PRIVATE_KEY".into(), secret.clone()),
                ])),
            )
            .unwrap();
            for command in [
                Command::Import { file: source },
                Command::Status,
                Command::Export { file: None },
            ] {
                execute(command, &store, &keychain, &root, &root, scope, "en").unwrap();
            }
            assert_eq!(store.get("ALICE_GITHUB_PRIVATE_KEY").unwrap(), Some(secret));
            return;
        }
        let root = tempfile::tempdir().unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .env(FIXTURE, root.path().canonicalize().unwrap())
            .args(["--exact", "client::secrets::tests::secrets_status_and_stdout_export_report_the_store_without_a_file", "--nocapture"])
            .output().unwrap();
        assert!(output.status.success(), "{output:?}");
        let stdout = String::from_utf8(output.stdout).unwrap();
        for expected in [
            "os_secret_store: available",
            "locked: no",
            "OPENAI_API_KEY=sk-fixture",
        ] {
            assert!(stdout.contains(expected), "{stdout}");
        }
        assert!(!root.path().join("export.env").exists());
    }

    #[test]
    fn dotenv_exports_plain_and_multiline_values_without_interpolation() {
        let values = BTreeMap::from([
            ("KEY".into(), "abc-12".into()),
            (
                "PEM".into(),
                "-----BEGIN KEY-----\na\\b\r\nc\n-----END KEY-----\n".into(),
            ),
            ("QUOTED".into(), "a \"b\" # c ${KEY}".into()),
            ("EMPTY".into(), String::new()),
        ]);
        let exported = dotenv(&values);
        assert!(exported.contains("KEY=abc-12\n"));
        assert_eq!(read_dotenv(exported.as_bytes()).unwrap(), values);
    }
}
