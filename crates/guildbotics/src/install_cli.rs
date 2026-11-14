use std::{
    fs,
    path::{Path, PathBuf},
};

use gb_base::files::{FileLock, SHARED_WRITE_TIMEOUT, atomic_write};
use gb_server::{client, i18n};
use serde_json::{Value, json};

const SKILL: &[u8] = include_bytes!("../../../skills/guildbotics/SKILL.md");
const PROVISION: &str =
    include_str!("../../../guildbotics/templates/environment/cli_provision.json");
const METADATA: &str = ".guildbotics-managed.json";
#[cfg(unix)]
const EXECUTABLE: &str = "guildbotics";
#[cfg(windows)]
const EXECUTABLE: &str = "guildbotics.exe";

pub async fn run() -> anyhow::Result<()> {
    let language = client::language().await;
    let home = gb_base::home::user_home()
        .ok_or_else(|| anyhow::anyhow!(i18n::t(language, "cli.install.home_required", &[])))?;
    let root = gb_base::home::machine_root()?;
    let _install = FileLock::hold(&root.join("install.lock"), SHARED_WRITE_TIMEOUT)?;
    let bin = root.join("bin");
    if bin.is_symlink() {
        #[cfg(unix)]
        fs::remove_file(&bin)?;
        #[cfg(windows)]
        fs::remove_dir(&bin)?;
    }
    fs::create_dir_all(&bin)?;
    let managed = bin.canonicalize()?.join(EXECUTABLE);
    install_binary(&std::env::current_exe()?, &managed)?;
    #[cfg(unix)]
    install_on_path(&home.join(".local/bin/guildbotics"), &managed)?;
    #[cfg(windows)]
    windows_path(&bin)?;
    println!(
        "{}",
        i18n::t(
            language,
            "cli.install.cli",
            &[("path", &managed.to_string_lossy())]
        )
    );
    for agent in serde_json::from_str::<Vec<Value>>(PROVISION)? {
        let name = agent["name"].as_str().unwrap_or_default();
        let provision = &agent["provision"];
        let state_root = provision["state_root"].as_str().unwrap_or_default();
        let skills = provision["user_skills"].as_str().unwrap_or_default();
        let configured = provision["state_root_env"]
            .as_str()
            .filter(|key| !key.is_empty())
            .and_then(std::env::var_os)
            .filter(|value| !value.is_empty());
        let root = configured
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(state_root));
        if skills.is_empty() || !root.is_dir() {
            println!(
                "{}",
                i18n::t(language, "cli.install.not_installed", &[("tool", name)])
            );
            continue;
        }
        let directory = Path::new(skills)
            .strip_prefix(state_root)
            .map(|under| root.join(under))
            .unwrap_or_else(|_| home.join(skills))
            .join("guildbotics");
        let skill = directory.join("SKILL.md");
        let key = if install_skill(&directory, SKILL)? {
            "cli.install.skill"
        } else {
            "cli.install.kept"
        };
        println!(
            "{}",
            i18n::t(
                language,
                key,
                &[("tool", name), ("path", &skill.to_string_lossy())]
            )
        );
    }
    Ok(())
}

fn install_binary(source: &Path, destination: &Path) -> anyhow::Result<()> {
    if fs::symlink_metadata(destination).is_ok_and(|metadata| metadata.is_file())
        && source.canonicalize()? == destination
    {
        return Ok(());
    }
    let mut file = fs::File::open(source)?;
    let mut pending = tempfile::NamedTempFile::new_in(destination.parent().unwrap())?;
    std::io::copy(&mut file, pending.as_file_mut())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        pending
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o755))?;
    }
    pending.as_file().sync_all()?;
    pending.persist(destination)?;
    Ok(())
}

#[cfg(unix)]
fn install_on_path(path: &Path, managed: &Path) -> anyhow::Result<()> {
    use std::io::Read;
    use std::os::unix::fs::symlink;

    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_file() => {
            let mut header = Vec::new();
            fs::File::open(path)?.take(512).read_to_end(&mut header)?;
            if !header.starts_with(b"#!/bin/sh\n")
                || !header
                    .windows(b"Managed by GuildBotics".len())
                    .any(|part| part == b"Managed by GuildBotics")
            {
                return Ok(());
            }
            let pending = tempfile::tempdir_in(path.parent().unwrap())?;
            let link = pending.path().join("guildbotics");
            symlink(managed, &link)?;
            fs::rename(link, path)?;
        }
        Ok(_) => (),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir_all(path.parent().unwrap())?;
            symlink(managed, path)?;
        }
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

fn install_skill(directory: &Path, content: &[u8]) -> anyhow::Result<bool> {
    let skill = directory.join("SKILL.md");
    let sidecar = directory.join(METADATA);
    let previous = match fs::symlink_metadata(&skill) {
        Ok(metadata) if metadata.is_file() => {
            let earlier = fs::read(&sidecar)
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .unwrap_or_default();
            let current = content_hash(&fs::read(&skill)?);
            if earlier["content_hash"] != current && earlier["pending_content_hash"] != current {
                return Ok(false);
            }
            Some(current)
        }
        Ok(_) => return Ok(false),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error.into()),
    };
    let mut metadata = json!({
        "manager": "GuildBotics",
        "skill": "guildbotics",
        "content_hash": previous,
        "pending_content_hash": content_hash(content),
    });
    let write_metadata = |metadata: &Value| {
        atomic_write(
            &sidecar,
            gb_base::files::dump_shared_json(metadata).as_bytes(),
        )
    };
    // A retry recognizes the managed bytes on either side of the skill write.
    write_metadata(&metadata)?;
    atomic_write(&skill, content)?;
    let pending = metadata
        .as_object_mut()
        .unwrap()
        .remove("pending_content_hash")
        .unwrap();
    metadata["content_hash"] = pending;
    write_metadata(&metadata)?;
    Ok(true)
}

fn content_hash(content: &[u8]) -> String {
    let hash = content.iter().fold(0xcbf29ce484222325_u64, |hash, byte| {
        (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3)
    });
    format!("{hash:016x}")
}

#[cfg(test)]
mod tests {
    use super::{METADATA, PROVISION, content_hash, install_skill};
    use serde_json::Value;
    use std::fs;

    #[test]
    fn retry_finishes_an_interrupted_managed_skill_install() {
        let directory = tempfile::tempdir().unwrap();
        let skill = directory.path().join("SKILL.md");
        let sidecar = directory.path().join(METADATA);
        fs::create_dir(&sidecar).unwrap();
        assert!(install_skill(directory.path(), b"new").is_err());
        assert!(!skill.exists());
        fs::remove_dir(&sidecar).unwrap();

        for existing in [b"old", b"new"] {
            fs::write(&skill, existing).unwrap();
            let pending = serde_json::json!({
                "content_hash": content_hash(b"old"),
                "pending_content_hash": content_hash(b"new"),
            });
            fs::write(&sidecar, pending.to_string()).unwrap();
            assert!(install_skill(directory.path(), b"new").unwrap());
            let finished: Value = serde_json::from_slice(&fs::read(&sidecar).unwrap()).unwrap();
            assert_eq!(finished["content_hash"], content_hash(b"new"));
            assert!(finished.get("pending_content_hash").is_none());
        }
        assert_eq!(fs::read(skill).unwrap(), b"new");
    }

    #[test]
    fn every_tool_in_the_provision_catalog_names_its_user_skills_directory() {
        let entries: Vec<Value> = serde_json::from_str(PROVISION).unwrap();
        assert!(!entries.is_empty());
        for tool in entries {
            assert!(
                tool["provision"]["user_skills"]
                    .as_str()
                    .is_some_and(|path| !path.is_empty()),
                "{} does not declare a user skills directory",
                tool["name"],
            );
        }
    }

    #[test]
    fn the_content_hash_is_fnv_1a_64() {
        assert_eq!(content_hash(b""), "cbf29ce484222325");
        assert_eq!(content_hash(b"a"), "af63dc4c8601ec8c");
    }
}

/// Preserve the user's existing entries and compare Windows path spellings.
#[cfg(any(windows, test))]
fn user_path(current: &str, bin: &str) -> Option<String> {
    let normalize = |value: &str| {
        value
            .trim()
            .trim_matches('"')
            .replace('/', "\\")
            .trim_end_matches('\\')
            .to_lowercase()
    };
    if current
        .split(';')
        .any(|entry| normalize(entry) == normalize(bin))
    {
        None
    } else if current.is_empty() {
        Some(bin.into())
    } else {
        Some(format!("{};{bin}", current.trim_end_matches(';')))
    }
}

#[cfg(windows)]
fn windows_path(bin: &Path) -> anyhow::Result<()> {
    use std::process::Command;
    let read = Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); [Console]::Write([Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment').GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames))"])
        .output()?;
    anyhow::ensure!(
        read.status.success(),
        "{}",
        String::from_utf8_lossy(&read.stderr)
    );
    let current = String::from_utf8(read.stdout)?;
    if let Some(updated) = user_path(&current, &bin.to_string_lossy()) {
        let write = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", "$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment'); $kind = if ($key.GetValueNames() -contains 'Path') { $key.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }; $key.SetValue('Path', $env:GUILDBOTICS_INSTALL_PATH, $kind)"])
            .env("GUILDBOTICS_INSTALL_PATH", updated)
            .output()?;
        anyhow::ensure!(
            write.status.success(),
            "{}",
            String::from_utf8_lossy(&write.stderr)
        );
        #[link(name = "user32")]
        unsafe extern "system" {
            fn SendMessageTimeoutW(
                window: isize,
                message: u32,
                wparam: usize,
                lparam: isize,
                flags: u32,
                timeout: u32,
                result: *mut usize,
            ) -> isize;
        }
        let environment: Vec<u16> = "Environment\0".encode_utf16().collect();
        // SAFETY: the terminated UTF-16 buffer stays alive through this bounded
        // broadcast. Receivers reread the user's environment for new processes.
        unsafe {
            SendMessageTimeoutW(
                0xffff,
                0x1a,
                0,
                environment.as_ptr() as isize,
                2,
                1000,
                std::ptr::null_mut(),
            );
        }
    }
    Ok(())
}

#[cfg(test)]
mod path_tests {
    #[test]
    fn windows_path_preserves_entries_and_is_idempotent() {
        let bin = r"C:\Users\Person\.guildbotics\bin";
        assert_eq!(super::user_path("", bin), Some(bin.into()));
        assert_eq!(
            super::user_path(r"%SystemRoot%\bin;D:\Tools;", bin),
            Some(format!(r"%SystemRoot%\bin;D:\Tools;{bin}"))
        );
        assert_eq!(
            super::user_path(r"D:\Tools;C:/USERS/Person/.guildbotics/bin/", bin),
            None
        );
        assert_eq!(super::user_path(&format!("\"{bin}\""), bin), None);
    }
}
