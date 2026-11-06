//! The shared command files the screens may select, using the runtime resolver.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use regex::Regex;
use serde_json::{Value, json};
use sha2::Digest;

use crate::commands::{CommandRoots, command_metadata, frontmatter, is_command_file};

pub fn revision(bytes: &[u8]) -> String {
    sha2::Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

pub fn file_id(relative: &str) -> String {
    URL_SAFE_NO_PAD.encode(relative)
}

pub fn logical_name(relative: &Path, language: &str) -> Option<String> {
    let stemmed = relative.with_extension("");
    let name = stemmed.file_name()?.to_str()?;
    if let Some((base, locale)) = name.rsplit_once('.') {
        if locale != language && locale != "en" {
            return None;
        }
        Some(stemmed.with_file_name(base).to_string_lossy().into_owned())
    } else {
        Some(stemmed.to_string_lossy().into_owned())
    }
}

fn within(path: &Path, root: &Path) -> bool {
    path.canonicalize()
        .ok()
        .zip(root.canonicalize().ok())
        .is_some_and(|(p, r)| p.starts_with(r))
}

impl CommandRoots {
    pub fn shared_files(&self, language: &str) -> anyhow::Result<Value> {
        let root = self.config.join("commands");
        let mut names = BTreeSet::new();
        collect(&root, &root, language, &mut names)?;
        let mut files = Vec::new();
        for name in names {
            if let Ok(path) = self.resolve(&name, language, None)
                && within(&path, &root)
            {
                files.push(self.file_summary(&path, &name, language)?);
            }
        }
        Ok(json!({"files": files}))
    }

    /// Resolve an opaque ID only if it is still an effective shared command.
    pub fn selected_file(&self, id: &str, language: &str) -> Option<PathBuf> {
        let bytes = URL_SAFE_NO_PAD.decode(id.trim_end_matches('=')).ok()?;
        let relative = PathBuf::from(String::from_utf8(bytes).ok()?);
        if relative.is_absolute() || !is_command_file(&relative) {
            return None;
        }
        let root = self.config.join("commands");
        let path = root.join(&relative);
        if !path.is_file() || !within(&path, &root) {
            return None;
        }
        let name = logical_name(&relative, language)?;
        let resolved = self.resolve(&name, language, None).ok()?;
        (resolved.canonicalize().ok()? == path.canonicalize().ok()?).then_some(path)
    }

    fn file_summary(&self, path: &Path, name: &str, language: &str) -> anyhow::Result<Value> {
        let metadata = display_metadata(path, language);
        let relative = path
            .strip_prefix(self.config.join("commands"))?
            .to_string_lossy()
            .replace('\\', "/");
        let fallback = default_label(name);
        let label = metadata
            .get("name")
            .filter(|v| truthy(v))
            .map(python_text)
            .unwrap_or(fallback);
        let format = match path.extension().and_then(|s| s.to_str()).unwrap_or("") {
            "md" => "markdown",
            "py" => "python",
            "sh" => "shell",
            _ => "yaml",
        };
        Ok(
            json!({"id": file_id(&relative), "command": name, "label": label,
            "description": metadata.get("description").map(python_text).unwrap_or_default(),
            "relative_path": relative, "format": format}),
        )
    }

    pub fn file_detail(&self, path: &Path, language: &str) -> anyhow::Result<Value> {
        let relative = path.strip_prefix(self.config.join("commands"))?;
        let name = logical_name(relative, language).unwrap_or_default();
        let mut result = self.file_summary(path, &name, language)?;
        let bytes = std::fs::read(path)?;
        let metadata = display_metadata(path, language);
        result["content"] = String::from_utf8_lossy(&bytes).into_owned().into();
        result["revision"] = revision(&bytes).into();
        result["arguments"] = screen_arguments(path, &metadata, &name);
        result["inputs"] = inputs(&metadata);
        Ok(result)
    }
}

/// The arguments a caller may give the command at `path`, as the screens
/// show them: none when its declaration is invalid.
pub(crate) fn screen_arguments(path: &Path, metadata: &Value, name: &str) -> Value {
    if path.extension().is_some_and(|s| s == "py") {
        let source = std::fs::read(path).unwrap_or_default();
        crate::python_literal::screen_arguments(&String::from_utf8_lossy(&source))
    } else {
        arguments(metadata, name).unwrap_or_else(|| json!([]))
    }
}

pub(crate) fn collect(
    root: &Path,
    directory: &Path,
    language: &str,
    names: &mut BTreeSet<String>,
) -> anyhow::Result<()> {
    let entries = match std::fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        // Do not traverse symlink directories or allow a file outside the shared root.
        if entry.file_type()?.is_dir() {
            collect(root, &path, language, names)?;
        } else if path.is_file()
            && is_command_file(&path)
            && within(&path, root)
            && let Some(name) = logical_name(path.strip_prefix(root)?, language)
        {
            names.insert(name);
        }
    }
    Ok(())
}

pub(crate) fn display_metadata(path: &Path, language: &str) -> Value {
    let mut metadata = command_metadata(path).unwrap_or_default().metadata;
    if let Ok(source) = std::fs::read_to_string(path) {
        if path.extension().is_some_and(|s| s == "md") {
            let body = frontmatter(&source)
                .and_then(|_| source[4..].find("\n---").map(|i| &source[4 + i + 4..]))
                .unwrap_or(&source)
                .trim_start_matches('\n');
            metadata.insert("body".into(), body.into());
        } else if path.extension().is_some_and(|s| s == "py")
            && !metadata.contains_key("description")
        {
            metadata.insert(
                "description".into(),
                crate::python_literal::main_description(&source).into(),
            );
        }
    }
    for key in ["name", "description"] {
        if let Some(Value::Object(choices)) = metadata.get(key) {
            let selected = choices
                .get(language)
                .or_else(|| choices.get("en"))
                .or_else(|| choices.values().next())
                .cloned();
            metadata.remove(key);
            if let Some(v) = selected {
                metadata.insert(key.into(), v);
            }
        }
    }
    metadata.into()
}

pub(crate) fn python_text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        _ => value.to_string(),
    }
}

/// The command's input policy; the defaults when its declaration is invalid.
pub(crate) fn inputs(metadata: &Value) -> Value {
    input_policy(metadata.get("inputs")).unwrap_or_else(
        |_| json!({"defined_args":"auto", "extra_args":"hidden", "message":"optional"}),
    )
}

/// The input policy `value` declares, or why it is no valid one.
pub(crate) fn input_policy(value: Option<&Value>) -> Result<Value, String> {
    let mut result = json!({"defined_args":"auto", "extra_args":"hidden", "message":"optional"});
    let Some(value) = value.filter(|v| !v.is_null()) else {
        return Ok(result);
    };
    let raw = value
        .as_object()
        .ok_or("Command 'inputs' must be a mapping.")?;
    let fields: [(&str, &[&str]); 3] = [
        ("defined_args", &["auto", "hidden"]),
        ("extra_args", &["hidden", "optional"]),
        ("message", &["hidden", "optional", "required"]),
    ];
    let unknown: BTreeSet<_> = raw
        .keys()
        .filter(|k| !fields.iter().any(|(f, _)| f == k))
        .collect();
    if !unknown.is_empty() {
        let names: Vec<_> = unknown.into_iter().map(String::as_str).collect();
        return Err(format!(
            "Command 'inputs' has unsupported fields: {}.",
            names.join(", ")
        ));
    }
    for (key, allowed) in fields {
        if let Some(value) = raw.get(key) {
            if !value.as_str().is_some_and(|s| allowed.contains(&s)) {
                return Err(format!(
                    "Command 'inputs.{key}' must be one of: {}.",
                    allowed.join(", ")
                ));
            }
            result[key] = value.clone();
        }
    }
    Ok(result)
}

/// The arguments `metadata` declares under `args`: name, whether required,
/// and the default; or why they are no valid declaration.
pub(crate) fn argument_definitions(
    metadata: &Value,
) -> Result<Vec<(String, bool, Option<String>)>, String> {
    let Some(raw) = metadata.get("args").filter(|v| !v.is_null()) else {
        return Ok(Vec::new());
    };
    let raw = raw.as_object().ok_or("Command 'args' must be a mapping.")?;
    let valid = Regex::new(r"^(?:[A-Za-z_]\w*|\d+)$").unwrap();
    let mut definitions = Vec::new();
    for (name, definition) in raw {
        if !valid.is_match(name) {
            return Err(format!("Invalid command argument name: '{name}'."));
        }
        let definition = definition
            .as_object()
            .ok_or_else(|| format!("Command argument '{name}' must be a mapping."))?;
        let unknown: BTreeSet<_> = definition
            .keys()
            .filter(|k| *k != "default" && *k != "required")
            .map(String::as_str)
            .collect();
        let unknown: Vec<_> = unknown.into_iter().collect();
        if !unknown.is_empty() {
            return Err(format!(
                "Command argument '{name}' has unsupported fields: {}.",
                unknown.join(", ")
            ));
        }
        let default = definition.get("default");
        if default.is_some_and(|v| v.is_null() || v.is_array() || v.is_object()) {
            return Err(format!(
                "Command argument '{name}' default must be a scalar value."
            ));
        }
        let required = definition
            .get("required")
            .map(Value::as_bool)
            .unwrap_or(Some(default.is_none()))
            .ok_or_else(|| format!("Command argument '{name}' required must be true or false."))?;
        if required && default.is_some() {
            return Err(format!(
                "Command argument '{name}' cannot be required and have a default."
            ));
        }
        definitions.push((name.clone(), required, default.map(python_text)));
    }
    Ok(definitions)
}

fn argument(name: &str, required: bool, default: String) -> Value {
    json!({"name":name, "kind":if name.chars().all(|c| c.is_ascii_digit()) {"positional"} else {"keyword"}, "required":required, "default":default})
}

fn arguments(metadata: &Value, command: &str) -> Option<Value> {
    let mut result = Vec::new();
    let mut declared = BTreeSet::new();
    for (name, required, default) in argument_definitions(metadata).ok()? {
        result.push(argument(&name, required, default.unwrap_or_default()));
        declared.insert(name);
    }
    let text = ["body", "description"]
        .iter()
        .filter_map(|k| metadata.get(k))
        .map(python_text)
        .collect::<Vec<_>>()
        .join("\n");
    let mut found = BTreeSet::new();
    for pattern in [
        r"\$\{\s*([A-Za-z_]\w*|\d+)\s*\}",
        r"\{\{\s*([A-Za-z_]\w*)\s*\}\}",
        r"\{([A-Za-z_]\w*)\}",
    ] {
        let pattern = Regex::new(pattern).unwrap();
        for hit in pattern.captures_iter(&text) {
            let whole = hit.get(0).unwrap();
            // The single-brace spelling excludes dollar/double-brace forms.
            if pattern.as_str().starts_with(r"\{(")
                && (text[..whole.start()].ends_with(['{', '$'])
                    || text[whole.end()..].starts_with('}'))
            {
                continue;
            }
            found.insert(hit[1].to_owned());
        }
    }
    for reserved in ["context", "now", "effort"] {
        found.remove(reserved);
    }
    // Named subcommand outputs are runtime values, not caller arguments.
    if let Some(entries) = metadata.get("commands").filter(|v| !v.is_null()) {
        let entries = entries
            .as_array()
            .cloned()
            .unwrap_or_else(|| vec![entries.clone()]);
        for (i, entry) in entries.iter().enumerate() {
            found.remove(&output_name(entry, command, i + 1)?);
        }
    }
    let mut positional: Vec<_> = found
        .iter()
        .filter_map(|s| s.parse::<u64>().ok())
        .filter(|i| *i > 0)
        .filter(|i| !declared.contains(&i.to_string()))
        .collect();
    positional.sort();
    for i in positional {
        result.push(argument(&i.to_string(), true, String::new()));
    }
    for name in found {
        if !declared.contains(&name) && !name.chars().all(|c| c.is_ascii_digit()) {
            result.push(argument(&name, true, String::new()));
        }
    }
    Some(result.into())
}

pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

pub(crate) fn default_label(name: &str) -> String {
    let source = name
        .rsplit('/')
        .next()
        .unwrap_or(name)
        .replace(['_', '-'], " ");
    // Whole-string lowercasing preserves context-sensitive final sigma.
    let lowercase = source.to_lowercase();
    let mut lower = lowercase.chars();
    let cased = Regex::new(r"^\p{Cased}$").unwrap();
    let mut previous_cased = false;
    let mut result = String::new();
    for ch in source.chars() {
        let lowered: String = lower.by_ref().take(ch.to_lowercase().count()).collect();
        let title = unicode_case_mapping::to_titlecase(ch);
        if previous_cased {
            result.push_str(&lowered);
        } else if title == [0; 3] {
            result.push(ch);
        } else {
            result.extend(
                title
                    .into_iter()
                    .filter(|n| *n != 0)
                    .filter_map(char::from_u32),
            );
        }
        previous_cased = cased.is_match(&ch.to_string());
    }
    result
}

// Python shlex.split(comments=False, posix=True). Validate every word, even
// though naming needs only the first; invalid entries invalidate arguments.
pub(crate) fn command_words(source: &str) -> Option<Vec<String>> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut started = false;
    let mut chars = source.chars();
    while let Some(ch) = chars.next() {
        match (quote, ch) {
            (Some(q), c) if q == c => quote = None,
            (None, '\'' | '"') => {
                quote = Some(ch);
                started = true;
            }
            (Some('\''), _) => word.push(ch),
            (_, '\\') => {
                let next = chars.next()?;
                if quote == Some('"') && next != '"' && next != '\\' {
                    word.push('\\');
                }
                word.push(next);
                started = true;
            }
            (None, ' ' | '\t' | '\r' | '\n') => {
                if started {
                    words.push(std::mem::take(&mut word));
                    started = false;
                }
            }
            _ => {
                word.push(ch);
                started = true;
            }
        }
    }
    if quote.is_some() {
        return None;
    }
    if started {
        words.push(word);
    }
    (!words.is_empty()).then_some(words)
}

fn output_name(entry: &Value, command: &str, index: usize) -> Option<String> {
    let mut normalized = match entry {
        Value::String(s) => {
            serde_json::Map::from_iter([("path".into(), command_words(s)?.remove(0).into())])
        }
        Value::Object(o) => {
            let mut normalized = if let Some(s) = o.get("command") {
                serde_json::Map::from_iter([(
                    "path".into(),
                    command_words(&python_text(s))?.remove(0).into(),
                )])
            } else {
                serde_json::Map::new()
            };
            normalized.extend(o.clone());
            normalized
        }
        _ => return None,
    };
    if let Some(name) = normalized.remove("name").filter(truthy) {
        return Some(python_text(&name));
    }
    if let Some(path) = normalized.remove("path").filter(truthy) {
        return Some(
            Path::new(&python_text(&path))
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned(),
        );
    }
    // Match command_source_name even when a logical name ends in an extension.
    let path = Path::new(command);
    let suffix = path
        .extension()
        .map(|s| format!(".{}", s.to_string_lossy().to_lowercase()));
    let anchor = if suffix
        .as_deref()
        .is_some_and(|s| crate::commands::EXTENSIONS.contains(&s))
    {
        path.file_stem()
    } else {
        path.file_name()
    }
    .unwrap_or_default()
    .to_string_lossy();
    let anchor = anchor.as_ref();
    let anchor = anchor.strip_suffix(".en").unwrap_or(anchor);
    Some(format!("{anchor}__{index}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subcommand_words_follow_python_shlex() {
        for (source, expected) in [
            (r#""sub/my_tool" a"#, vec!["sub/my_tool", "a"]),
            (r"sub/escaped\ tool a", vec!["sub/escaped tool", "a"]),
            ("#tool a", vec!["#tool", "a"]),
            (r#""a\$b" ''"#, vec![r"a\$b", ""]),
            ("a\\\nb", vec!["a\nb"]),
        ] {
            assert_eq!(
                command_words(source),
                Some(expected.into_iter().map(str::to_owned).collect()),
                "{source:?}"
            );
        }
        for source in ["", "  ", "tool 'unfinished", "tool\\"] {
            assert_eq!(command_words(source), None, "{source:?}");
        }
    }
}
