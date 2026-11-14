//! Which file a command name runs, and what the command declares of its
//! access, as the Python version decides them
//! (`guildbotics/commands/discovery.py`, `metadata.py`).

use std::path::{Path, PathBuf};

use gb_wire::{CommandAccess, InspectsItem};
use serde_json::Value;

use crate::team::valid_person_id;
use crate::{python_literal, yaml};

/// The command files' extensions, in the order a name without one tries them.
pub const EXTENSIONS: &[&str] = &[".md", ".py", ".sh", ".yaml", ".yml"];

/// Where commands are looked for: the workspace's configuration, then the
/// templates bundled with GuildBotics.
pub struct CommandRoots {
    pub config: PathBuf,
    pub templates: PathBuf,
}

/// Why a command cannot run as named or declared.
#[derive(Debug, PartialEq)]
pub enum CommandProblem {
    InvalidPerson(String),
    /// A name that is no command's: absolute, or climbing out with `..`.
    InvalidName(String),
    NotFound(String),
    /// A declaration that cannot be read, which is refused rather than read
    /// as declaring nothing: that would run a command declared read-only as
    /// one that writes.
    UnreadableDeclaration(PathBuf),
    InvalidReadOnly,
    InvalidInspects,
}

impl std::fmt::Display for CommandProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CommandProblem::InvalidPerson(person) => write!(f, "'{person}' is not a member's ID."),
            CommandProblem::InvalidName(name) => write!(f, "'{name}' is not a command's name."),
            CommandProblem::NotFound(name) => write!(f, "Unable to locate command '{name}'."),
            CommandProblem::UnreadableDeclaration(path) => {
                write!(f, "The declaration of {} cannot be read.", path.display())
            }
            CommandProblem::InvalidReadOnly => {
                f.write_str("Command 'read_only' must be true or false.")
            }
            CommandProblem::InvalidInspects => {
                f.write_str("Command 'inspects' must be a list of: diagnostics, config.")
            }
        }
    }
}

impl std::error::Error for CommandProblem {}

impl CommandRoots {
    /// The file `name` runs for the member `person_id` in a project in
    /// `language`: the first existing command file of the candidates, in the
    /// order of [`CommandRoots::candidates`].
    pub fn resolve(
        &self,
        name: &str,
        language: &str,
        person_id: Option<&str>,
    ) -> Result<PathBuf, CommandProblem> {
        self.candidates(name, language, person_id)?
            .into_iter()
            .find(|path| path.exists() && is_command_file(path))
            .ok_or_else(|| CommandProblem::NotFound(name.into()))
    }

    /// Every file `name` may run, in order: by extension (unless the name has
    /// one), the member's own commands before the shared ones, each in the
    /// project's language, then English, then without one, in the workspace
    /// before the templates.
    pub fn candidates(
        &self,
        name: &str,
        language: &str,
        person_id: Option<&str>,
    ) -> Result<Vec<PathBuf>, CommandProblem> {
        // Only the commands of the roots run, never a file named by a path.
        let named = Path::new(name);
        if name.is_empty()
            || named.has_root()
            || named
                .components()
                .any(|c| !matches!(c, std::path::Component::Normal(_)))
        {
            return Err(CommandProblem::InvalidName(name.into()));
        }
        let extensions: &[&str] = if Path::new(name).extension().is_some() {
            &[""]
        } else {
            EXTENSIONS
        };
        let mut bases = Vec::new();
        if let Some(person) = person_id {
            if !valid_person_id(person) {
                return Err(CommandProblem::InvalidPerson(person.into()));
            }
            bases.push(format!("team/members/{person}/commands/{name}"));
        }
        bases.push(format!("commands/{name}"));
        let mut candidates = Vec::new();
        for extension in extensions {
            for base in &bases {
                for variant in locale_variants(&format!("{base}{extension}"), language) {
                    candidates.push(self.config.join(&variant));
                    candidates.push(self.templates.join(&variant));
                }
            }
        }
        Ok(candidates)
    }
}

/// `base` in `language`, in English, and as it is.
fn locale_variants(base: &str, language: &str) -> Vec<String> {
    let path = Path::new(base);
    let named = |suffix: &str| {
        let stem = path.file_stem().unwrap_or_default().to_string_lossy();
        let extension = path
            .extension()
            .map(|e| format!(".{}", e.to_string_lossy()))
            .unwrap_or_default();
        path.with_file_name(format!("{stem}.{suffix}{extension}"))
            .to_string_lossy()
            .into_owned()
    };
    let mut variants = Vec::new();
    if !language.is_empty() {
        variants.push(named(language));
        variants.push(named("en"));
    }
    variants.push(base.into());
    variants
}

/// A command file: one of [`EXTENSIONS`], and not a reserved `.metadata` file.
pub(crate) fn is_command_file(path: &Path) -> bool {
    let extension = path
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
        .unwrap_or_default();
    let stem = path.file_stem().unwrap_or_default().to_string_lossy();
    let reserved = stem.ends_with(".metadata") || stem.contains(".metadata.");
    EXTENSIONS.contains(&extension.as_str()) && !reserved
}

/// What the command at `path` declares of the access of its AI CLI turns.
pub fn command_access(path: &Path) -> Result<CommandAccess, CommandProblem> {
    declared_access(&command_metadata(path)?)
}

/// What `declared` says of the access of the command's AI CLI turns.
pub(crate) fn declared_access(
    declared: &python_literal::Declaration,
) -> Result<CommandAccess, CommandProblem> {
    let metadata = &declared.metadata;
    let read_only = match metadata.get("read_only") {
        None => false,
        Some(Value::Bool(read_only)) => *read_only,
        Some(_) => return Err(CommandProblem::InvalidReadOnly),
    };
    let mut inspects = Vec::new();
    match metadata.get("inspects") {
        None => {}
        Some(Value::Array(_)) if declared.tuples.iter().any(|k| k == "inspects") => {
            return Err(CommandProblem::InvalidInspects);
        }
        Some(Value::Array(scopes)) => {
            for scope in scopes {
                let scope = match scope.as_str() {
                    Some("diagnostics") => InspectsItem::Diagnostics,
                    Some("config") => InspectsItem::Config,
                    _ => return Err(CommandProblem::InvalidInspects),
                };
                // A set of scopes, as the Python version keeps them.
                if !inspects.contains(&scope) {
                    inspects.push(scope);
                }
            }
        }
        Some(_) => return Err(CommandProblem::InvalidInspects),
    }
    Ok(CommandAccess {
        inspects,
        read_only,
    })
}

/// The metadata the command file holds: a Markdown file's frontmatter, a YAML
/// file's mapping, a Python file's `COMMAND_METADATA`; empty for any other.
/// A field without a value is no field, as the Python version reads it.
pub fn command_metadata(path: &Path) -> Result<python_literal::Declaration, CommandProblem> {
    let unreadable = || CommandProblem::UnreadableDeclaration(path.into());
    let extension = path.extension().map(|e| e.to_string_lossy().into_owned());
    if !matches!(extension.as_deref(), Some("md" | "yml" | "yaml" | "py")) {
        return Ok(Default::default());
    }
    let text = std::fs::read_to_string(path).map_err(|_| unreadable())?;
    source_metadata(extension.as_deref().unwrap_or(""), &text).map_err(|()| unreadable())
}

/// The metadata `text` holds as the source of a command with `extension`.
pub(crate) fn source_metadata(
    extension: &str,
    text: &str,
) -> Result<python_literal::Declaration, ()> {
    // As the Python version splits lines: at `\r\n`, `\r` and `\n`.
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut declared = match extension {
        "py" => python_literal::command_metadata(&text).map_err(|_| ())?,
        "md" => mapping(frontmatter(&text).map_or(Ok(Value::Null), yaml::value)?),
        "yml" | "yaml" => mapping(yaml::value(&text)?),
        _ => Default::default(),
    };
    declared.metadata.retain(|_, value| !value.is_null());
    Ok(declared)
}

fn mapping(value: Value) -> python_literal::Declaration {
    match value {
        Value::Object(metadata) => python_literal::Declaration {
            metadata,
            tuples: Vec::new(),
        },
        _ => Default::default(),
    }
}

/// A Markdown file's YAML frontmatter: from a first line `---` to the next.
pub(crate) fn frontmatter(text: &str) -> Option<&str> {
    let mut lines = text.split_inclusive('\n');
    if lines.next()?.trim_end_matches(['\r', '\n']) != "---" {
        return None;
    }
    let start = text.find('\n')? + 1;
    let mut offset = start;
    for line in lines {
        if line.trim_end_matches(['\r', '\n']) == "---" {
            return Some(&text[start..offset]);
        }
        offset += line.len();
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frontmatter_runs_from_the_first_line_to_the_next_rule() {
        assert_eq!(frontmatter("---\na: 1\n---\nbody"), Some("a: 1\n"));
        assert_eq!(frontmatter("---\r\na: 1\r\n---\r\n"), Some("a: 1\r\n"));
        assert_eq!(frontmatter("---\n---\n"), Some(""));
        assert_eq!(frontmatter("--- \na: 1\n---\n"), None);
        assert_eq!(frontmatter("---\na: 1\n"), None);
        assert_eq!(frontmatter("body\n---\na: 1\n---\n"), None);
    }

    #[test]
    fn a_name_is_tried_by_extension_member_language_and_root() {
        let roots = CommandRoots {
            config: "/w".into(),
            templates: "/t".into(),
        };
        let candidates: Vec<PathBuf> = roots
            .candidates("x", "ja", Some("aiko"))
            .unwrap()
            .iter()
            .take(12)
            .cloned()
            .collect();
        assert_eq!(
            candidates,
            [
                "/w/team/members/aiko/commands/x.ja.md",
                "/t/team/members/aiko/commands/x.ja.md",
                "/w/team/members/aiko/commands/x.en.md",
                "/t/team/members/aiko/commands/x.en.md",
                "/w/team/members/aiko/commands/x.md",
                "/t/team/members/aiko/commands/x.md",
                "/w/commands/x.ja.md",
                "/t/commands/x.ja.md",
                "/w/commands/x.en.md",
                "/t/commands/x.en.md",
                "/w/commands/x.md",
                "/t/commands/x.md",
            ]
            .map(PathBuf::from)
        );
        let named = roots.candidates("x.py", "", None).unwrap();
        assert_eq!(
            named,
            [PathBuf::from("/w/commands/x.py"), "/t/commands/x.py".into()]
        );
        assert_eq!(
            roots.candidates("x", "ja", Some("../x")),
            Err(CommandProblem::InvalidPerson("../x".into()))
        );
        for name in ["/tmp/x.md", "../x", "a/../../x", "./x", ""] {
            assert_eq!(
                roots.candidates(name, "ja", Some("aiko")),
                Err(CommandProblem::InvalidName(name.into())),
                "{name}"
            );
        }
    }

    #[test]
    fn metadata_files_are_no_commands() {
        assert!(is_command_file(Path::new("a/x.MD")));
        assert!(!is_command_file(Path::new("a/x.metadata.yml")));
        assert!(!is_command_file(Path::new("a/x.metadata.ja.yml")));
        assert!(!is_command_file(Path::new("a/x.txt")));
    }
}
