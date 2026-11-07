//! The command screens' reading of commands: the options a member may pick,
//! what each needs to run, and whether a source is a valid command, as the
//! Python version reads them (`app_api/command_catalog.py`,
//! `commands/validation.py`). A command declares what it uses beside its
//! brain in `requires:`; the Python version guessed that from a Python
//! command's imports.

use std::collections::{BTreeSet, HashSet};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

use crate::command_files::{
    argument_definitions, collect, command_words, default_label, display_metadata, input_policy,
    inputs, python_text, screen_arguments, truthy,
};
use crate::commands::{CommandRoots, EXTENSIONS, command_metadata, declared_access, frontmatter};
use crate::{python_literal, resolved_guest, yaml};

/// What a command may declare it uses, beside the isolated environment every
/// command needs.
pub const REQUIREMENTS: &[&str] = &[
    "llm",
    "cli_agent",
    "code_hosting_service",
    "ticket_service",
    "chat_service",
];

/// What a command needs to run: each kind, with the AI CLI tool it names ("" for
/// none and for every other kind).
pub type Needs = BTreeSet<(String, String)>;

/// A command a member may pick, without its requirements, and what it needs.
pub struct CommandOption {
    pub option: Value,
    pub needs: Needs,
}

/// Why a source is no valid command, as the screens report it.
#[derive(Debug, PartialEq)]
pub struct InvalidSource {
    pub code: &'static str,
    pub message: String,
    pub context: Map<String, Value>,
}

impl InvalidSource {
    fn new(message: impl Into<String>) -> InvalidSource {
        InvalidSource {
            code: "command_file_invalid_source",
            message: message.into(),
            context: Map::new(),
        }
    }

    fn with(mut self, key: &str, value: impl ToString) -> InvalidSource {
        self.context.insert(key.into(), value.to_string().into());
        self
    }
}

impl CommandRoots {
    /// The commands the member `person` may pick, by name: every name of the
    /// member's and the shared commands (and of the bundled ones, for
    /// `routine`) that runs a file, and for `routine` only the ones whose
    /// metadata declares `routine: true`.
    pub fn options(
        &self,
        language: &str,
        person: Option<&str>,
        routine: bool,
    ) -> anyhow::Result<Vec<CommandOption>> {
        let mut directories = Vec::new();
        if let Some(person) = person {
            directories.push(self.config.join(format!("team/members/{person}/commands")));
        }
        directories.push(self.config.join("commands"));
        if routine {
            directories.push(self.templates.join("commands"));
        }
        let mut names = BTreeSet::new();
        for directory in &directories {
            collect(directory, directory, language, &mut names)?;
        }
        let mut options = Vec::new();
        for name in names {
            let Ok(path) = self.resolve(&name, language, person) else {
                continue;
            };
            let metadata = display_metadata(&path, language);
            if routine && metadata.get("routine") != Some(&Value::Bool(true)) {
                continue;
            }
            let arguments = screen_arguments(&path, &metadata, &name);
            let inputs = inputs(&metadata);
            let eligible = !routine
                || (inputs["message"] != "required"
                    && (inputs["defined_args"] == "hidden"
                        || !arguments
                            .as_array()
                            .into_iter()
                            .flatten()
                            .any(|a| a["required"] == true)));
            let label = metadata
                .get("name")
                .filter(|v| truthy(v))
                .map(python_text)
                .unwrap_or_else(|| default_label(&name));
            let category = match name.split('/').next() {
                Some("workflows") if name.contains('/') => "workflow",
                Some("functions") if name.contains('/') => "function",
                Some("examples") if name.contains('/') => "example",
                _ => "custom",
            };
            let source = if path.starts_with(&self.templates) {
                "template"
            } else {
                "workspace"
            };
            options.push(CommandOption {
                needs: self.needs(&path, language, person),
                option: json!({"command": name, "label": label,
                    "description": metadata.get("description").map(python_text).unwrap_or_default(),
                    "category": category, "source": source, "path": path,
                    "arguments": arguments, "inputs": inputs, "routine_eligible": eligible}),
            });
        }
        Ok(options)
    }

    /// What the command at `path` needs to run for `person`: the isolated
    /// environment, what it declares, what its brain runs, and what its
    /// subcommands need.
    pub fn needs(&self, path: &Path, language: &str, person: Option<&str>) -> Needs {
        let mut needs = Needs::from([("environment".into(), String::new())]);
        self.collect_needs(path, language, person, &mut HashSet::new(), &mut needs);
        needs
    }

    fn collect_needs(
        &self,
        path: &Path,
        language: &str,
        person: Option<&str>,
        seen: &mut HashSet<PathBuf>,
        needs: &mut Needs,
    ) {
        let Ok(real) = path.canonicalize() else {
            return;
        };
        if !seen.insert(real) {
            return;
        }
        // A declaration that cannot be read declares nothing, and a Markdown
        // command's brain is then its default.
        let metadata = Value::Object(command_metadata(path).unwrap_or_default().metadata);
        if path.extension().is_some_and(|e| e == "md") {
            self.brain_needs(metadata.get("brain"), person, needs);
        }
        for kind in requires(&metadata).unwrap_or_default() {
            needs.insert((kind, String::new()));
        }
        let base = path.parent().unwrap_or(Path::new(""));
        for entry in entries(&metadata) {
            let reference = match &entry {
                Value::String(text) => Some(text.clone()),
                Value::Object(o) if o.contains_key("prompt") || o.contains_key("print") => {
                    if !o.contains_key("print") {
                        self.brain_needs(o.get("brain"), person, needs);
                    }
                    None
                }
                Value::Object(o)
                    if ["python", "script", "to_html", "to_pdf"]
                        .iter()
                        .any(|k| o.contains_key(*k)) =>
                {
                    None
                }
                Value::Object(o) => o
                    .get("command")
                    .filter(|v| !v.is_null())
                    .or_else(|| {
                        ["path", "name"]
                            .iter()
                            .find_map(|k| o.get(*k).filter(|v| truthy(v)))
                    })
                    .map(python_text),
                _ => None,
            };
            let Some(name) = reference
                .and_then(|text| command_words(&text))
                .map(|mut words| words.remove(0))
                .filter(|name| !name.is_empty())
            else {
                continue;
            };
            if let Some(child) = self.reference(base, &name, language, person) {
                self.collect_needs(&child, language, person, seen, needs);
            }
        }
    }

    /// What a brain named `brain` (the default one when none is named) runs:
    /// nothing when it is disabled or its mapping cannot be read, which the
    /// run itself reports.
    fn brain_needs(&self, brain: Option<&Value>, person: Option<&str>, needs: &mut Needs) {
        let brain = brain.map(python_text).unwrap_or_else(|| "default".into());
        let brain = brain.trim();
        if ["none", "-", "null", "disabled"].contains(&brain.to_lowercase().as_str()) {
            return;
        }
        match resolved_guest::brain_tool(self, person.unwrap_or(""), brain) {
            Ok(None) => {
                needs.insert(("llm".into(), String::new()));
            }
            Ok(Some(tool)) => {
                needs.insert(("cli_agent".into(), tool));
            }
            Err(_) => {}
        }
    }

    /// The file a subcommand reference beside `base` names: a file there, or
    /// a command by its name.
    fn reference(
        &self,
        base: &Path,
        name: &str,
        language: &str,
        person: Option<&str>,
    ) -> Option<PathBuf> {
        let beside = base.join(name);
        let mut candidates = Vec::new();
        if Path::new(name).extension().is_none() {
            for extension in EXTENSIONS {
                candidates.push(PathBuf::from(format!("{}{extension}", beside.display())));
            }
        }
        candidates.push(beside);
        candidates
            .into_iter()
            .find(|path| path.exists())
            .or_else(|| self.resolve(name, language, person).ok())
    }
}

/// The subcommand entries `metadata` declares under `commands`.
fn entries(metadata: &Value) -> Vec<Value> {
    match metadata.get("commands") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(entries)) => entries.clone(),
        Some(entry) => vec![entry.clone()],
    }
}

/// What `metadata` declares under `requires`, or why that is no declaration.
fn requires(metadata: &Value) -> Result<Vec<String>, String> {
    let invalid = || {
        format!(
            "Command 'requires' must be a list of: {}.",
            REQUIREMENTS.join(", ")
        )
    };
    match metadata.get("requires") {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(kinds)) => kinds
            .iter()
            .map(|kind| {
                kind.as_str()
                    .filter(|k| REQUIREMENTS.contains(k))
                    .map(str::to_owned)
                    .ok_or_else(invalid)
            })
            .collect(),
        Some(_) => Err(invalid()),
    }
}

/// Whether `content` is a valid command for a file with `extension` (with its
/// dot), as the Python version's `validate_command_source` decides it. A
/// Python command is read as tokens: a mistake inside a statement is reported
/// when it runs.
pub fn validate_source(extension: &str, content: &str) -> Result<(), InvalidSource> {
    // As Python splits lines: at `\r\n`, `\r` and `\n`.
    let content = &content.replace("\r\n", "\n").replace('\r', "\n");
    let lowered = extension.to_lowercase();
    match lowered.as_str() {
        ".md" => {
            let Some(front) = frontmatter(content).filter(|f| !f.trim().is_empty()) else {
                return Ok(());
            };
            let metadata = match parsed(front, "Command frontmatter is not valid YAML.")? {
                Value::Null => Value::Object(Map::new()),
                Value::Object(m) => Value::Object(m),
                _ => return Err(InvalidSource::new("Command frontmatter must be a mapping.")),
            };
            declarations(&metadata, true)
        }
        ".yaml" | ".yml" => match parsed(content, "Command YAML is not valid.")? {
            Value::Null => Err(InvalidSource::new("Command YAML must define a mapping.")),
            metadata @ Value::Object(_) => declarations(&metadata, true),
            _ => Err(InvalidSource::new("Command YAML root must be a mapping.")),
        },
        ".py" => {
            let syntax =
                || InvalidSource::new("Python command has a syntax error: invalid syntax.");
            if !python_literal::defines_main(content).map_err(|_| syntax())? {
                return Err(InvalidSource::new(
                    "Python command must define a top-level 'main' function.",
                ));
            }
            let declared = python_literal::command_metadata(content).map_err(|_| {
                InvalidSource::new(
                    "Python command must declare COMMAND_METADATA at most once, as a static \
                     literal mapping with string keys.",
                )
            })?;
            declarations(&Value::Object(declared.metadata), false)
        }
        ".sh" => Ok(()),
        _ => Err(InvalidSource {
            code: "command_file_unsupported_format",
            message: format!("Unsupported command extension: '{extension}'."),
            context: Map::new(),
        }
        .with("extension", &lowered)),
    }
}

/// [`validate_source`], and what a command an AI wrote must also hold: a
/// Markdown command names its brain, and one that requires a message lets
/// something read it.
pub fn validate_generated_source(extension: &str, content: &str) -> Result<(), InvalidSource> {
    validate_source(extension, content)?;
    if extension.to_lowercase() != ".md" {
        return Ok(());
    }
    // The body follows the frontmatter's closing line.
    let (front, body) = match frontmatter(content) {
        Some(front) => {
            let closing = front.as_ptr() as usize - content.as_ptr() as usize + front.len();
            let rest = &content[closing..];
            (front, rest.split_once('\n').map_or("", |(_, body)| body))
        }
        None => ("", content),
    };
    let metadata = yaml::value(front).unwrap_or(Value::Null);
    let Some(brain) = metadata.get("brain") else {
        return Err(InvalidSource::new(
            "AI-generated Markdown commands must explicitly declare 'brain' as a non-empty string.",
        )
        .with("reason", "generated_brain_not_explicit"));
    };
    if brain.as_str().is_none_or(|b| b.trim().is_empty()) {
        return Err(InvalidSource::new(
            "AI-generated Markdown commands must set 'brain' to a non-empty string.",
        )
        .with("reason", "generated_brain_invalid"));
    }
    let disabled = ["none", "-", "null", "disabled"]
        .contains(&brain.as_str().unwrap_or("").trim().to_lowercase().as_str());
    let reads_pipe = metadata.get("template_engine").and_then(Value::as_str) == Some("jinja2")
        && regex::Regex::new(r#"context\s*(\.\s*pipe\b|\[\s*['"]pipe['"]\s*\])"#)
            .unwrap()
            .is_match(body);
    if metadata["inputs"]["message"] == "required"
        && disabled
        && !metadata.get("commands").is_some_and(truthy)
        && !reads_pipe
    {
        return Err(InvalidSource::new(
            "Markdown command declares a required input message, but brain: none does not \
             consume it and no child command can do so. Set 'brain: default' to use the \
             configured LLM or add an input-consuming child command.",
        )
        .with("reason", "required_message_not_consumed"));
    }
    Ok(())
}

/// The YAML `text` holds, or why it holds none, at the line and column YAML
/// stopped.
fn parsed(text: &str, message: &str) -> Result<Value, InvalidSource> {
    yaml::value(text).map_err(|()| {
        let invalid = InvalidSource::new(message);
        match yaml::syntax_error(text) {
            Some((line, column)) => invalid.with("line", line).with("column", column),
            None => invalid,
        }
    })
}

/// Whether the declarations of `metadata` are valid: inputs, arguments,
/// access, requirements and, for a command that may have them, the shape of
/// its subcommand entries.
fn declarations(metadata: &Value, with_commands: bool) -> Result<(), InvalidSource> {
    input_policy(metadata.get("inputs")).map_err(InvalidSource::new)?;
    argument_definitions(metadata).map_err(InvalidSource::new)?;
    let declared = python_literal::Declaration {
        metadata: metadata.as_object().cloned().unwrap_or_default(),
        tuples: Vec::new(),
    };
    declared_access(&declared).map_err(|e| InvalidSource::new(e.to_string()))?;
    requires(metadata).map_err(InvalidSource::new)?;
    if !with_commands {
        return Ok(());
    }
    for entry in entries(metadata) {
        match entry {
            Value::Object(o) => {
                let inline = [
                    "prompt", "python", "script", "print", "to_html", "to_pdf", "command", "path",
                    "name",
                ];
                if !inline.iter().any(|k| o.contains_key(*k)) {
                    return Err(InvalidSource::new(
                        "Command entry requires 'command', 'path', 'name' or an inline command \
                         key (e.g. 'prompt', 'python', 'script', 'print', 'to_html', 'to_pdf').",
                    ));
                }
            }
            Value::String(text) => {
                if command_words(&text).is_none() {
                    return Err(InvalidSource::new(if text.trim().is_empty() {
                        "Command entry string cannot be empty."
                    } else {
                        "Command entry could not be parsed: No closing quotation."
                    }));
                }
            }
            _ => {
                return Err(InvalidSource::new(
                    "Command entry must be a mapping or string.",
                ));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn refused(extension: &str, content: &str) -> String {
        validate_source(extension, content).unwrap_err().message
    }

    #[test]
    fn a_source_is_validated_as_its_extension_reads_it() {
        for (extension, content) in [
            (".md", "No frontmatter.\n"),
            (".md", "---\n---\nEmpty frontmatter.\n"),
            (
                ".md",
                "---\nbrain: none\nrequires: [llm, chat_service]\n---\n",
            ),
            (
                ".yml",
                "commands:\n  - print: done\n  - other arg\n  - command: x\n",
            ),
            (
                ".py",
                "COMMAND_METADATA = {\"requires\": [\"cli_agent\"]}\n\nasync def main(context):\n    pass\n",
            ),
            (".sh", "this is not checked\0"),
        ] {
            assert_eq!(validate_source(extension, content), Ok(()), "{content}");
        }
        for (extension, content, message) in [
            (
                ".md",
                "---\n- a\n---\n",
                "Command frontmatter must be a mapping.",
            ),
            (".yaml", "", "Command YAML must define a mapping."),
            (".yaml", "- a\n", "Command YAML root must be a mapping."),
            (".yaml", "inputs: [", "Command YAML is not valid."),
            (
                ".py",
                "x = 1\n",
                "Python command must define a top-level 'main' function.",
            ),
            (
                ".py",
                "def main(:\n    '''\n",
                "Python command has a syntax error: invalid syntax.",
            ),
            (
                ".md",
                "---\ninputs: x\n---\n",
                "Command 'inputs' must be a mapping.",
            ),
            (
                ".md",
                "---\ninputs:\n  z: 1\n  a: 2\n---\n",
                "Command 'inputs' has unsupported fields: a, z.",
            ),
            (
                ".md",
                "---\ninputs:\n  message: x\n---\n",
                "Command 'inputs.message' must be one of: hidden, optional, required.",
            ),
            (
                ".md",
                "---\nargs:\n  bad-name: {}\n---\n",
                "Invalid command argument name: 'bad-name'.",
            ),
            (
                ".md",
                "---\nargs:\n  a: 1\n---\n",
                "Command argument 'a' must be a mapping.",
            ),
            (
                ".md",
                "---\nargs:\n  a: {z: 1, y: 2}\n---\n",
                "Command argument 'a' has unsupported fields: y, z.",
            ),
            (
                ".md",
                "---\nargs:\n  a: {default: [1]}\n---\n",
                "Command argument 'a' default must be a scalar value.",
            ),
            (
                ".md",
                "---\nargs:\n  a: {required: yes please}\n---\n",
                "Command argument 'a' required must be true or false.",
            ),
            (
                ".md",
                "---\nargs:\n  a: {required: true, default: x}\n---\n",
                "Command argument 'a' cannot be required and have a default.",
            ),
            (
                ".md",
                "---\nread_only: maybe\n---\n",
                "Command 'read_only' must be true or false.",
            ),
            (
                ".md",
                "---\ninspects: [network]\n---\n",
                "Command 'inspects' must be a list of: diagnostics, config.",
            ),
            (
                ".md",
                "---\nrequires: llm\n---\n",
                "Command 'requires' must be a list of: llm, cli_agent, code_hosting_service, ticket_service, chat_service.",
            ),
            (
                ".yml",
                "commands:\n  - {unknown: 1}\n",
                "Command entry requires 'command', 'path', 'name' or an inline command key (e.g. 'prompt', 'python', 'script', 'print', 'to_html', 'to_pdf').",
            ),
            (
                ".yml",
                "commands:\n  - ''\n",
                "Command entry string cannot be empty.",
            ),
            (
                ".yml",
                "commands:\n  - \"a 'b\"\n",
                "Command entry could not be parsed: No closing quotation.",
            ),
            (
                ".yml",
                "commands:\n  - 1\n",
                "Command entry must be a mapping or string.",
            ),
        ] {
            assert_eq!(refused(extension, content), message, "{content}");
        }
        assert_eq!(
            refused(".md", "---\rinputs: x\r---\r"),
            "Command 'inputs' must be a mapping."
        );
        assert_eq!(
            refused(
                ".py",
                "COMMAND_METADATA = {}\nCOMMAND_METADATA = {}\ndef main(c):\n    pass\n"
            ),
            "Python command must declare COMMAND_METADATA at most once, as a static literal mapping with string keys."
        );
        assert_eq!(
            refused(".TOML", ""),
            "Unsupported command extension: '.TOML'."
        );
        let unsupported = validate_source(".toml", "").unwrap_err();
        assert_eq!(unsupported.code, "command_file_unsupported_format");
        assert_eq!(unsupported.context["extension"], ".toml");
        let syntax = validate_source(".yaml", "a: 1\nb: [\n").unwrap_err();
        assert!(syntax.context.contains_key("line") && syntax.context.contains_key("column"));
    }

    #[test]
    fn a_generated_markdown_command_names_its_brain_and_reads_a_required_message() {
        let generated = |content| {
            validate_generated_source(".md", content).map_err(|e| e.context["reason"].clone())
        };
        assert_eq!(
            generated("Body.\n"),
            Err("generated_brain_not_explicit".into())
        );
        assert_eq!(
            generated("---\nbrain: ' '\n---\n"),
            Err("generated_brain_invalid".into())
        );
        assert_eq!(generated("---\nbrain: default\n---\n"), Ok(()));
        let unread = "---\nbrain: none\ninputs:\n  message: required\n---\nBody.\n";
        assert_eq!(
            generated(unread),
            Err("required_message_not_consumed".into())
        );
        for read in [
            "---\nbrain: none\ninputs:\n  message: required\ncommands:\n  - print: x\n---\n",
            "---\nbrain: none\ntemplate_engine: jinja2\ninputs:\n  message: required\n---\n{{ context.pipe }}\n",
            "---\nbrain: none\ntemplate_engine: jinja2\ninputs:\n  message: required\n---\n{{ context['pipe'] }}\n",
        ] {
            assert_eq!(generated(read), Ok(()), "{read}");
        }
        assert_eq!(validate_generated_source(".sh", "x"), Ok(()));
        assert!(validate_generated_source(".md", "---\ninputs: x\n---\n").is_err());
    }
}
