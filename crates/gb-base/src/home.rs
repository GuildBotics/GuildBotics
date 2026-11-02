//! Where GuildBotics keeps what belongs to the machine rather than to a
//! workspace, as the Python version does: `~/.guildbotics`.

use std::ffi::OsString;
use std::path::PathBuf;

/// Names another machine root than `~/.guildbotics`, so a version under
/// development never touches the machine's own records.
pub const HOME_ENV: &str = "GUILDBOTICS_HOME";

/// `GUILDBOTICS_HOME`, else `~/.guildbotics`.
pub fn machine_root() -> anyhow::Result<PathBuf> {
    root_from(std::env::var_os(HOME_ENV), std::env::var_os("HOME"))
}

fn root_from(configured: Option<OsString>, home: Option<OsString>) -> anyhow::Result<PathBuf> {
    match configured {
        Some(root) if root.is_empty() => anyhow::bail!("{HOME_ENV} is empty"),
        Some(root) => Ok(root.into()),
        None => match home {
            Some(home) if !home.is_empty() => Ok(PathBuf::from(home).join(".guildbotics")),
            _ => anyhow::bail!("HOME is not set; set {HOME_ENV}"),
        },
    }
}

/// The machine's own state: `<machine root>/data`.
pub fn data() -> anyhow::Result<PathBuf> {
    Ok(machine_root()?.join("data"))
}

/// Names the root of the members' identities, as the Python version reads it.
pub const IDENTITY_ROOT_ENV: &str = "GUILDBOTICS_IDENTITY_ROOT";

/// The root of the members' identities, which every workspace of the machine
/// shares: `GUILDBOTICS_IDENTITY_ROOT`, else `<machine root>/identity`.
pub fn identity_root() -> anyhow::Result<PathBuf> {
    match std::env::var(IDENTITY_ROOT_ENV) {
        Ok(root) if !root.trim().is_empty() => Ok(root.trim().into()),
        _ => Ok(machine_root()?.join("identity")),
    }
}

/// The Python package of the checkout this executable was built from, which
/// the server runs from: its bundled templates, and the code a command's
/// isolated environment runs.
pub fn python_package() -> PathBuf {
    let crates = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates/");
    crates.parent().expect("the checkout").join("guildbotics")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_root_is_the_configured_one_or_under_home() {
        assert_eq!(
            root_from(Some("/x".into()), Some("/home/o".into())).unwrap(),
            PathBuf::from("/x")
        );
        assert_eq!(
            root_from(None, Some("/home/o".into())).unwrap(),
            PathBuf::from("/home/o/.guildbotics")
        );
        assert!(root_from(Some("".into()), Some("/home/o".into())).is_err());
        assert!(root_from(None, None).is_err());
        assert!(root_from(None, Some("".into())).is_err());
    }
}
