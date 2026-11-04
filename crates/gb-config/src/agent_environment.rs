//! The declaration of the workspace's isolated environment
//! (`intelligences/agent_environment.yml`), as far as booting a command's
//! microVM reads it: the memory and virtual CPUs it boots with, as the Python
//! version reads them (`guildbotics/environment/toolchain.py`).
//!
//! The rest of the declaration (the image, the network, the resolvers) is
//! read with the device's status, as it is carried over: a command's microVM
//! boots from the snapshot already built from the image, and opens no network.

use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::yaml;

/// Where the declaration is, under the workspace's configuration or else the
/// templates.
pub const PATH: &str = "intelligences/agent_environment.yml";

/// What a microVM boots with.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Resources {
    pub memory_mib: u32,
    pub cpus: u8,
}

impl Default for Resources {
    fn default() -> Resources {
        Resources {
            memory_mib: 4096,
            cpus: 2,
        }
    }
}

/// A declaration that cannot be read, which nothing boots from.
#[derive(Debug, PartialEq)]
pub enum DeclarationProblem {
    Resources(PathBuf),
    Image(PathBuf),
}

/// The custom base image, whose actual local contents identify a snapshot.
#[derive(Debug, Clone, PartialEq, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BaseImage {
    pub reference: String,
    pub digests: std::collections::BTreeMap<String, String>,
}

pub fn image(config: &Path, templates: &Path) -> Result<Option<BaseImage>, DeclarationProblem> {
    let own = config.join(PATH);
    let path = if own.exists() {
        own
    } else {
        templates.join(PATH)
    };
    let problem = || DeclarationProblem::Image(path.clone());
    let text = std::fs::read_to_string(&path).map_err(|_| problem())?;
    let declaration = yaml::value(&text).map_err(|_| problem())?;
    let declaration = declaration.as_object().ok_or_else(problem)?;
    let Some(value) = declaration.get("image").filter(|v| !v.is_null()) else {
        return Ok(None);
    };
    let image: BaseImage = serde_json::from_value(value.clone()).map_err(|_| problem())?;
    if image.reference.is_empty()
        || image.reference.starts_with('-')
        || image.reference.chars().any(char::is_whitespace)
        || image.digests.is_empty()
        || image.digests.iter().any(|(arch, digest)| {
            arch.is_empty()
                || !arch
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
                || !digest.strip_prefix("sha256:").is_some_and(|hex| {
                    hex.len() == 64
                        && hex
                            .bytes()
                            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                })
        })
    {
        return Err(problem());
    }
    Ok(Some(image))
}

impl std::fmt::Display for DeclarationProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Resources(path) => write!(
                f,
                "The resources in {} must be positive integers: memory_mib and cpus.",
                path.display()
            ),
            Self::Image(path) => write!(
                f,
                "The base image in {} must declare a reference and SHA256 digests by architecture.",
                path.display()
            ),
        }
    }
}

impl std::error::Error for DeclarationProblem {}

/// The resources the workspace whose configuration is `config` declares, or
/// the templates' declaration without one of its own.
pub fn resources(config: &Path, templates: &Path) -> Result<Resources, DeclarationProblem> {
    let own = config.join(PATH);
    let path = if own.exists() {
        own
    } else {
        templates.join(PATH)
    };
    let problem = || DeclarationProblem::Resources(path.clone());
    let text = std::fs::read_to_string(&path).map_err(|_| problem())?;
    let declaration = yaml::value(&text).map_err(|_| problem())?;
    let Value::Object(declaration) = declaration else {
        return Err(problem());
    };
    let Some(declared) = declaration.get("resources") else {
        return Ok(Resources::default());
    };
    let Value::Object(declared) = declared else {
        return Err(problem());
    };
    let mut resources = Resources::default();
    for (key, value) in declared {
        // Integers only, as pydantic's strict mode has them: not a boolean,
        // a float, or a string of digits.
        let number = value.as_u64().filter(|n| *n >= 1).ok_or_else(problem)?;
        match key.as_str() {
            "memory_mib" => resources.memory_mib = number.try_into().map_err(|_| problem())?,
            "cpus" => resources.cpus = number.try_into().map_err(|_| problem())?,
            _ => return Err(problem()),
        }
    }
    Ok(resources)
}
