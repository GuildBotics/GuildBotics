//! Changes to the shared command files, as the Python version's
//! `CommandFileService` makes them (`app_api/command_files.py`): only the
//! effective shared files are changed, a change names the revision it read,
//! and a new file must be what its name runs. A draft is saved whether it is
//! a valid command or not; a change an AI proposed must be one.

use std::path::{Path, PathBuf};

use gb_base::shared_writes::{Writer, resolved};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::catalog::validate_generated_source;
use crate::command_files::revision;
use crate::commands::{CommandRoots, EXTENSIONS};

/// A file's bytes and mode, as a write found it.
type Before = (Vec<u8>, u32);

/// The most a command file may hold, in bytes.
pub const MAX_BYTES: usize = 1_048_576;

/// A change refused, as the screens are told it: the status, the code, the
/// sentence (`app_api.errors.<key>`, or one already written) and its context.
#[derive(Debug)]
pub struct Refused {
    pub status: u16,
    pub code: &'static str,
    pub key: &'static str,
    pub params: Vec<(&'static str, String)>,
    pub reason: Option<String>,
    pub context: Value,
}

impl std::fmt::Display for Refused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.code)
    }
}

impl std::error::Error for Refused {}

fn refused(status: u16, code: &'static str, key: &'static str) -> Refused {
    Refused {
        status,
        code,
        key,
        params: Vec::new(),
        reason: None,
        context: json!({}),
    }
}

impl Refused {
    fn param(mut self, name: &'static str, value: impl Into<String>) -> Refused {
        self.params.push((name, value.into()));
        self
    }

    fn context(mut self, context: Value) -> Refused {
        self.context = context;
        self
    }
}

/// A file one of the formats is written as.
pub fn extension(format: &str) -> Option<&'static str> {
    match format {
        "markdown" => Some(".md"),
        "python" => Some(".py"),
        "shell" => Some(".sh"),
        "yaml" => Some(".yaml"),
        _ => None,
    }
}

/// A change an AI proposed, as the screen sends it back reviewed.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Change {
    pub operation: Operation,
    pub command: String,
    pub format: Format,
    pub relative_path: String,
    pub content: String,
    #[serde(default)]
    pub file_id: String,
    #[serde(default)]
    pub expected_revision: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Operation {
    Create,
    Update,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Format {
    Markdown,
    Python,
    Shell,
    Yaml,
}

impl Format {
    pub fn name(self) -> &'static str {
        match self {
            Format::Markdown => "markdown",
            Format::Python => "python",
            Format::Shell => "shell",
            Format::Yaml => "yaml",
        }
    }

    pub fn extension(self) -> &'static str {
        extension(self.name()).expect("every format has an extension")
    }

    /// What a new file of this format starts with.
    fn initial(self) -> &'static str {
        match self {
            Format::Markdown => {
                "---\nname: New command\nbrain: none\ninputs:\n  message: hidden\n---\nTODO\n"
            }
            Format::Python => {
                "COMMAND_METADATA = {\"inputs\": {\"message\": \"hidden\"}}\n\n\ndef main(context) -> str:\n    return \"\"\n"
            }
            Format::Shell => "#!/usr/bin/env bash\nset -euo pipefail\n",
            Format::Yaml => "commands: []\n",
        }
    }
}

impl CommandRoots {
    fn shared(&self) -> PathBuf {
        self.config.join("commands")
    }

    /// Where a file the command `path` runs comes from, beside the shared one
    /// it was meant to be: a template, a member's own, or the workspace's.
    pub fn shadow_source(&self, path: Option<&Path>) -> &'static str {
        match path {
            Some(path) if path.starts_with(&self.templates) => "template",
            Some(path) if !path.starts_with(self.shared()) => "member",
            _ => "workspace",
        }
    }

    /// Creates the shared command `command` in `format`, with `content` or
    /// what a new file of that format starts with.
    pub fn create_file(
        &self,
        writer: &mut Writer<'_>,
        command: &str,
        format: Format,
        content: Option<&str>,
        language: &str,
    ) -> anyhow::Result<Value> {
        let target = self.prospective(command, format, language)?;
        let content = content.unwrap_or(format.initial());
        check_content(content)?;
        write(writer, &target, content.as_bytes(), new_mode(format))?;
        self.file_detail(&target, language)
    }

    /// Replaces the shared file `id`, if it is still the revision `expected`.
    pub fn update_file(
        &self,
        writer: &mut Writer<'_>,
        id: &str,
        content: &str,
        expected: &str,
        language: &str,
    ) -> anyhow::Result<Value> {
        let path = self.existing(id, language)?;
        check_content(content)?;
        current(&path, expected, "command_file_changed.reloaded")?;
        write(writer, &path, content.as_bytes(), mode(&path))?;
        self.file_detail(&path, language)
    }

    /// Deletes the shared file `id`, if it is still the revision `expected`,
    /// and lists the shared files left.
    pub fn delete_file(
        &self,
        writer: &mut Writer<'_>,
        id: &str,
        expected: &str,
        language: &str,
    ) -> anyhow::Result<Value> {
        let path = self.existing(id, language)?;
        current(&path, expected, "command_file_changed.reloaded")?;
        writer.delete(&path)?;
        self.shared_files(language)
    }

    /// Applies every one of the reviewed `changes`, or none: each is checked
    /// before any is written, and a write that fails puts back the ones
    /// written before it.
    pub fn apply_changes(
        &self,
        writer: &mut Writer<'_>,
        changes: &[Change],
        language: &str,
    ) -> anyhow::Result<Value> {
        let mut commands = std::collections::HashSet::new();
        for change in changes {
            if !commands.insert(change.command.trim()) {
                Err(
                    refused(409, "command_file_exists", "command_file_exists.duplicate")
                        .param("command", &change.command),
                )?;
            }
        }
        let mut targets: Vec<(PathBuf, &Change, u32)> = Vec::new();
        for change in changes {
            let relative = valid_name(&change.command)?;
            let expected = format!("{relative}{}", change.format.extension());
            if change.relative_path != expected {
                Err(refused(
                    400,
                    "command_file_invalid_name",
                    "command_file_invalid_name.path",
                )
                .context(json!({"relative_path": change.relative_path})))?;
            }
            check_content(&change.content)?;
            if let Err(invalid) =
                validate_generated_source(change.format.extension(), &change.content)
            {
                Err(Refused {
                    reason: Some(invalid.message),
                    context: Value::Object(invalid.context),
                    ..refused(400, invalid.code, invalid.code)
                })?;
            }
            let (target, mode) = match change.operation {
                Operation::Update => {
                    let path = self.existing(&change.file_id, language)?;
                    let relative = path
                        .strip_prefix(self.shared())?
                        .to_string_lossy()
                        .replace('\\', "/");
                    if relative != change.relative_path {
                        Err(refused(
                            409,
                            "command_file_changed",
                            "command_file_changed.retargeted",
                        ))?;
                    }
                    current(
                        &path,
                        &change.expected_revision,
                        "command_file_changed.proposal",
                    )?;
                    let mode = mode(&path);
                    (path, mode)
                }
                Operation::Create => (
                    self.prospective(&change.command, change.format, language)?,
                    new_mode(change.format),
                ),
            };
            if targets.iter().any(|(path, ..)| *path == target) {
                Err(
                    refused(409, "command_file_exists", "command_file_exists.duplicate")
                        .param("command", &change.command),
                )?;
            }
            targets.push((target, change, mode));
        }
        // Each file written, with its bytes and mode before, if it was there.
        let mut written: Vec<(PathBuf, Option<Before>)> = Vec::new();
        for (target, change, mode) in &targets {
            let before = std::fs::read(target).ok().map(|b| (b, *mode));
            if let Err(error) = write(writer, target, change.content.as_bytes(), *mode) {
                for (path, before) in written.into_iter().rev() {
                    let _ = match before {
                        Some((bytes, mode)) => write(writer, &path, &bytes, mode),
                        None => writer.delete(&path),
                    };
                }
                return Err(error);
            }
            written.push((target.clone(), before));
        }
        let files = targets
            .iter()
            .map(|(path, ..)| self.file_detail(path, language))
            .collect::<anyhow::Result<Vec<_>>>()?;
        Ok(json!({"files": files}))
    }

    /// The effective shared file `id` names.
    fn existing(&self, id: &str, language: &str) -> anyhow::Result<PathBuf> {
        self.selected_file(id, language).ok_or_else(|| {
            refused(
                404,
                "command_file_not_found",
                "command_file_not_found.unknown",
            )
            .param("file_id", id)
            .into()
        })
    }

    /// Where a new shared command `command` in `format` is written: inside
    /// the shared commands, beside no file of the same name, and the file the
    /// name will run.
    fn prospective(
        &self,
        command: &str,
        format: Format,
        language: &str,
    ) -> anyhow::Result<PathBuf> {
        let name = valid_name(command)?;
        let relative = format!("{name}{}", format.extension());
        let root = self.shared();
        let target = root.join(&relative);
        let within = resolved(&target)
            .ok()
            .zip(resolved(&root).ok())
            .is_some_and(|(target, root)| target.starts_with(root));
        if !within {
            Err(refused(
                404,
                "command_file_not_found",
                "command_file_not_found.escapes",
            )
            .param("relative", &relative))?;
        }
        for extension in EXTENSIONS {
            let sibling = root.join(format!("{command}{extension}"));
            if sibling.symlink_metadata().is_ok() {
                let found = sibling.strip_prefix(&root).unwrap_or(&sibling);
                Err(
                    refused(409, "command_file_exists", "command_file_exists.exists")
                        .param("command", command)
                        .context(
                            json!({"relative_path": found.to_string_lossy().replace('\\', "/")}),
                        ),
                )?;
            }
        }
        let runs = self
            .candidates(name, language, None)
            .ok()
            .and_then(|candidates| {
                candidates
                    .into_iter()
                    .find(|path| *path == target || path.is_file())
            });
        if runs.as_ref() != Some(&target) {
            Err(
                refused(409, "command_file_shadowed", "command_file_shadowed")
                    .param("command", command)
                    .context(json!({"shadow_source": self.shadow_source(runs.as_deref())})),
            )?;
        }
        Ok(target)
    }
}

/// The relative name a shared command `command` is written under, or why it
/// is none: empty, absolute, with a backslash or NUL, or with an empty or
/// dotted part.
fn valid_name(command: &str) -> anyhow::Result<&str> {
    let name = command.trim();
    let invalid = name.is_empty()
        || name.starts_with('/')
        || name.contains(['\\', '\0'])
        || name
            .split('/')
            .any(|part| part.is_empty() || part.contains('.'));
    if invalid {
        Err(refused(
            400,
            "command_file_invalid_name",
            "command_file_invalid_name.value",
        )
        .param("command", command)
        .context(json!({"command": command})))?;
    }
    Ok(name)
}

fn check_content(content: &str) -> anyhow::Result<()> {
    if content.contains('\0') {
        Err(refused(
            400,
            "command_file_invalid_source",
            "command_file_invalid_source",
        )
        .context(json!({"reason": "nul_byte"})))?;
    }
    if content.len() > MAX_BYTES {
        Err(
            refused(413, "command_file_too_large", "command_file_too_large")
                .context(json!({"max_bytes": MAX_BYTES.to_string()})),
        )?;
    }
    Ok(())
}

/// Refuses a change to `path` unless it is still the revision `expected`.
fn current(path: &Path, expected: &str, key: &'static str) -> anyhow::Result<()> {
    let now = revision(&std::fs::read(path)?);
    if now != expected {
        Err(refused(409, "command_file_changed", key).context(json!({"current_revision": now})))?;
    }
    Ok(())
}

fn new_mode(format: Format) -> u32 {
    if format == Format::Shell {
        0o755
    } else {
        0o644
    }
}

#[cfg(unix)]
fn mode(path: &Path) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path).map_or(0o644, |m| m.permissions().mode() & 0o7777)
}

#[cfg(not(unix))]
fn mode(_: &Path) -> u32 {
    0o644
}

fn write(writer: &mut Writer<'_>, path: &Path, data: &[u8], mode: u32) -> anyhow::Result<()> {
    writer.write_bytes(path, data)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode))?;
    }
    #[cfg(not(unix))]
    let _ = mode;
    Ok(())
}
