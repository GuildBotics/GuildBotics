use std::{
    fs,
    path::{Path, PathBuf},
};

use gb_base::files::atomic_write;
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
    let home = std::env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .ok_or_else(|| anyhow::anyhow!(i18n::t(language, "cli.install.home_required", &[])))?;
    let root = gb_base::home::machine_root()?;
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
    match fs::symlink_metadata(&skill) {
        Ok(metadata) if metadata.is_file() => {
            let earlier = fs::read(&sidecar)
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .unwrap_or_default();
            if earlier["content_hash"] != content_hash(&fs::read(&skill)?) {
                return Ok(false);
            }
        }
        Ok(_) => return Ok(false),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
        Err(error) => return Err(error.into()),
    }
    let metadata = json!({
        "manager": "GuildBotics",
        "skill": "guildbotics",
        "content_hash": content_hash(content),
    });
    atomic_write(&skill, content)?;
    atomic_write(
        &sidecar,
        gb_base::files::dump_shared_json(&metadata).as_bytes(),
    )?;
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
    use super::content_hash;

    #[test]
    fn the_content_hash_is_fnv_1a_64() {
        assert_eq!(content_hash(b""), "cbf29ce484222325");
        assert_eq!(content_hash(b"a"), "af63dc4c8601ec8c");
    }
}
