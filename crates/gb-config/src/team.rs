//! The team a command runs in, as far as running one needs it, read as the
//! Python version reads it (`guildbotics/loader/yaml/yaml_team_loader.py`,
//! `guildbotics/entities/team.py`): the project's language and default member,
//! and each member the workspace holds a participation (`member.yml`) of,
//! joined with its identity (`identity.yml`) in the machine's identity root.
//!
//! Only the fields running a command reads are checked here. The members' and
//! the project's other settings (roles, schedules, accounts, services, the
//! secret namespaces of the members' IDs) are checked by whatever reads them,
//! as it is carried over.

use std::path::{Path, PathBuf};

use anyhow::Context as _;
use serde_json::{Map, Value};

const IDENTITY_FIELDS: &[&str] = &[
    "person_id",
    "name",
    "person_type",
    "account_info",
    "profile",
    "speaking_style",
    "relationships",
];
const PARTICIPATION_FIELDS: &[&str] = &[
    "is_active",
    "profile",
    "message_channels",
    "routine_commands",
    "task_schedules",
];

/// One member, as running a command names it.
#[derive(Debug, Clone, PartialEq)]
pub struct Member {
    pub person_id: String,
    pub name: String,
    pub person_type: String,
    pub is_active: bool,
}

/// The team of a workspace.
#[derive(Debug, Clone, PartialEq)]
pub struct Team {
    /// The project's language, as the language code of its tag (`ja`).
    pub language: String,
    configured_default: String,
    pub members: Vec<Member>,
}

/// A member's ID: a directory name on every operating system.
pub fn valid_person_id(value: &str) -> bool {
    let reserved = ["con", "prn", "aux", "nul"].contains(&value)
        || (value.len() == 4
            && (value.starts_with("com") || value.starts_with("lpt"))
            && matches!(value.as_bytes()[3], b'1'..=b'9'));
    !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
        && !reserved
}

impl Team {
    /// The command executor picker, using the same participation and identity files.
    pub fn screen_summary(&self, config: &Path, identity_root: &Path) -> anyhow::Result<Value> {
        let project = yaml_mapping(&config.join("team/project.yml"))?;
        let mut members = Vec::new();
        for member in &self.members {
            let identity = yaml_mapping(
                &identity_root
                    .join(".guildbotics/config/team/members")
                    .join(&member.person_id)
                    .join("identity.yml"),
            )?;
            let participation = yaml_mapping(
                &config
                    .join("team/members")
                    .join(&member.person_id)
                    .join("member.yml"),
            )?;
            let mut roles = std::collections::BTreeSet::new();
            for profile in [identity.get("profile"), participation.get("profile")]
                .into_iter()
                .flatten()
            {
                if let Some(mapping) = profile["roles"].as_object() {
                    roles.extend(mapping.keys().cloned());
                }
                if let Some(list) = profile["roles"].as_array() {
                    roles.extend(list.iter().filter_map(Value::as_str).map(str::to_owned));
                }
            }
            members.push(
                serde_json::json!({"person_id":member.person_id,"name":member.name,
                "person_type":member.person_type,"is_active":member.is_active,"roles":roles}),
            );
        }
        Ok(
            serde_json::json!({"project":{"name":project.get("name").and_then(Value::as_str).unwrap_or(""),
            "language_code":self.language,"language_name":match self.language.as_str() {"ja"=>"日本語","en"=>"English", other=>other}},
            "members":members,"default_person_id":self.default_person_id().unwrap_or(""),"problem":""}),
        )
    }

    /// Reads the team of the workspace whose configuration is `config`, with
    /// its members' identities under `identity_root`. A member whose identity
    /// this machine does not have is left out, as the Python version leaves
    /// it out; a member's file that is not its own is refused.
    pub fn load(config: &Path, identity_root: &Path) -> anyhow::Result<Team> {
        let team = config.join("team");
        let project = yaml_mapping(&team.join("project.yml"))?;
        let text = |key: &str| match project.get(key) {
            None => Ok(String::new()),
            Some(Value::String(text)) => Ok(text.clone()),
            Some(_) => Err(crate::ConfigFile::wrap(
                team.join("project.yml"),
                anyhow::anyhow!("{key} must be a string"),
            )),
        };
        let identities = identity_root.join(".guildbotics/config/team/members");
        let mut members = Vec::new();
        for directory in participants(&team.join("members"))? {
            let name = directory
                .file_name()
                .expect("a name")
                .to_string_lossy()
                .into_owned();
            let identity_file = identities.join(&name).join("identity.yml");
            if !identity_file.is_file() {
                tracing::warn!("Member {name} has no identity; it is left out of the team.");
                continue;
            }
            anyhow::ensure!(
                valid_person_id(&name),
                "{} is not a member's directory",
                directory.display()
            );
            let identity = member_file(&identity_file, IDENTITY_FIELDS)?;
            let participation = member_file(&directory.join("member.yml"), PARTICIPATION_FIELDS)?;
            let string = |file: &Map<String, Value>, key: &str, path: &Path| match file.get(key) {
                None => Ok(String::new()),
                Some(Value::String(s)) => Ok(s.clone()),
                Some(_) => Err(crate::ConfigFile::wrap(
                    path,
                    anyhow::anyhow!("{key} must be a string"),
                )),
            };
            let person_id = string(&identity, "person_id", &identity_file)?;
            if person_id != name {
                return Err(crate::ConfigFile::wrap(
                    &identity_file,
                    anyhow::anyhow!("the member's ID is not its directory's name"),
                ));
            }
            let name_value = identity
                .get("name")
                .and_then(Value::as_str)
                .context("name is required")
                .map_err(|error| crate::ConfigFile::wrap(&identity_file, error))?;
            let is_active = match participation.get("is_active") {
                None => false,
                Some(value) => lax_bool(value)
                    .context("is_active must be true or false")
                    .map_err(|error| {
                        crate::ConfigFile::wrap(directory.join("member.yml"), error)
                    })?,
            };
            members.push(Member {
                person_id,
                name: name_value.into(),
                person_type: string(&identity, "person_type", &identity_file)?,
                is_active,
            });
        }
        Ok(Team {
            language: language_code(&text("language")?),
            configured_default: text("default_person_id")?,
            members,
        })
    }

    /// The member a run that names none runs as: the configured default, else
    /// the first active member that is not a human, by ID.
    pub fn default_person_id(&self) -> Option<&str> {
        if !self.configured_default.is_empty() {
            return Some(&self.configured_default);
        }
        let mut candidates: Vec<&str> = self
            .members
            .iter()
            .filter(|m| m.is_active && m.person_type != "human")
            .map(|m| m.person_id.as_str())
            .collect();
        candidates.sort_unstable();
        candidates.first().copied()
    }

    /// The member `identifier` names: by ID, else by name, ignoring case (as
    /// lowercase, where the Python version folds case, which differs only for
    /// a few letters such as `ß`).
    pub fn find(&self, identifier: &str) -> Option<&Member> {
        let wanted = identifier.to_lowercase();
        self.members
            .iter()
            .find(|m| m.person_id.to_lowercase() == wanted)
            .or_else(|| {
                self.members
                    .iter()
                    .find(|m| m.name.to_lowercase() == wanted)
            })
    }
}

/// A boolean as pydantic reads one: `true`, `1`, or a word such as `yes`.
fn lax_bool(value: &Value) -> Option<bool> {
    match value {
        Value::Bool(b) => Some(*b),
        Value::Number(n) => match n.as_f64() {
            Some(0.0) => Some(false),
            Some(1.0) => Some(true),
            _ => None,
        },
        Value::String(s) => match s.to_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
            "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
            _ => None,
        },
        _ => None,
    }
}

/// The language code of a tag (`ja-JP` is `ja`), English when it names none.
fn language_code(tag: &str) -> String {
    let primary = tag.trim().split(['-', '_']).next().unwrap_or("");
    if (2..=3).contains(&primary.len()) && primary.bytes().all(|b| b.is_ascii_alphabetic()) {
        primary.to_ascii_lowercase()
    } else {
        "en".into()
    }
}

/// The members' directories: each child holding `member.yml`, by name.
fn participants(members: &Path) -> anyhow::Result<Vec<PathBuf>> {
    let Ok(entries) = std::fs::read_dir(members) else {
        return Ok(Vec::new());
    };
    let mut directories: Vec<PathBuf> = entries
        .map(|e| e.map(|e| e.path()))
        .collect::<Result<_, _>>()?;
    directories.retain(|d| d.is_dir() && d.join("member.yml").is_file());
    directories.sort();
    Ok(directories)
}

fn yaml_mapping(path: &Path) -> anyhow::Result<Map<String, Value>> {
    (|| {
        let text = std::fs::read_to_string(path).context("cannot read configuration")?;
        match crate::yaml::value(&text) {
            Ok(Value::Object(mapping)) => Ok(mapping),
            _ => anyhow::bail!("configuration is not a YAML mapping"),
        }
    })()
    .map_err(|error| crate::ConfigFile::wrap(path, error))
}

/// One of a member's two files, holding only the fields it is for.
fn member_file(path: &Path, fields: &[&str]) -> anyhow::Result<Map<String, Value>> {
    (|| {
        let file = yaml_mapping(path)?;
        anyhow::ensure!(
            file.get("profile").is_none_or(Value::is_object),
            "profile is not a YAML mapping"
        );
        let misplaced: Vec<&str> = file
            .keys()
            .map(String::as_str)
            .filter(|key| !fields.contains(key))
            .collect();
        anyhow::ensure!(
            misplaced.is_empty(),
            "fields that belong in the other file: {}",
            misplaced.join(", ")
        );
        Ok(file)
    })()
    .map_err(|error| crate::ConfigFile::wrap(path, error))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unreadable_config_retains_file_reason_and_io_source() {
        let root = tempfile::tempdir().unwrap();
        let config = root.path().join("config");
        let path = config.join("team/project.yml");
        let error = Team::load(&config, &root.path().join("identity")).unwrap_err();
        let file = error.downcast_ref::<crate::ConfigFile>().unwrap();
        assert_eq!(file.path(), path);
        let source = error.downcast_ref::<std::io::Error>().unwrap();
        assert_eq!(source.kind(), std::io::ErrorKind::NotFound);
        assert_eq!(
            error.to_string(),
            format!("{}: cannot read configuration: {source}", path.display())
        );
    }

    #[test]
    fn config_errors_identify_the_file_and_validation_reason_once() {
        for (target, content, reason) in [
            ("project", "language: 3", "language must be a string"),
            (
                "project",
                "default_person_id: []",
                "default_person_id must be a string",
            ),
            ("project", "[", "not a YAML mapping"),
            ("identity", "[", "not a YAML mapping"),
            (
                "identity",
                "person_id: aiko\nname: Aiko\nprofile: []",
                "profile is not a YAML mapping",
            ),
            (
                "identity",
                "person_id: aiko\nname: Aiko\nis_active: true",
                "fields that belong in the other file: is_active",
            ),
            (
                "identity",
                "person_id: 3\nname: Aiko",
                "person_id must be a string",
            ),
            (
                "identity",
                "person_id: other\nname: Aiko",
                "ID is not its directory's name",
            ),
            ("identity", "person_id: aiko", "name is required"),
            (
                "identity",
                "person_id: aiko\nname: Aiko\nperson_type: []",
                "person_type must be a string",
            ),
            ("participation", "[", "not a YAML mapping"),
            (
                "participation",
                "name: Aiko",
                "fields that belong in the other file: name",
            ),
            (
                "participation",
                "is_active: []",
                "is_active must be true or false",
            ),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let config = dir.path().join("config");
            let identity = dir.path().join("identity");
            let project = config.join("team/project.yml");
            let participation = config.join("team/members/aiko/member.yml");
            let member = identity.join(".guildbotics/config/team/members/aiko/identity.yml");
            for (path, content) in [
                (&project, "language: en"),
                (&participation, "is_active: true"),
                (&member, "person_id: aiko\nname: Aiko"),
            ] {
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(path, content).unwrap();
            }
            let path = match target {
                "project" => &project,
                "identity" => &member,
                _ => &participation,
            };
            std::fs::write(path, content).unwrap();
            let error = Team::load(&config, &identity).unwrap_err();
            let file = error.downcast_ref::<crate::ConfigFile>().unwrap();
            assert_eq!(file.path(), path, "{target}: {content}");
            let message = error.to_string();
            assert!(message.contains(reason), "{message}");
            assert_eq!(
                message.matches(path.to_str().unwrap()).count(),
                1,
                "{message}"
            );
        }
    }

    #[test]
    fn a_member_id_is_a_directory_name_everywhere() {
        for valid in ["aiko", "a_b-1", "com0", "lpt10"] {
            assert!(valid_person_id(valid), "{valid}");
        }
        for invalid in ["", "Aiko", "a b", "../x", "con", "com1", "lpt9"] {
            assert!(!valid_person_id(invalid), "{invalid}");
        }
    }

    #[test]
    fn the_language_is_the_code_of_the_tag() {
        assert_eq!(language_code("ja"), "ja");
        assert_eq!(language_code("ja-JP"), "ja");
        assert_eq!(language_code("EN"), "en");
        assert_eq!(language_code(""), "en");
        assert_eq!(language_code("日本語"), "en");
    }
}
