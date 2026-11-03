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
        let fallback = name
            .rsplit('/')
            .next()
            .unwrap_or(name)
            .replace(['_', '-'], " ")
            .split_whitespace()
            .map(|s| {
                let mut c = s.chars();
                c.next()
                    .map(|f| f.to_uppercase().collect::<String>() + &c.as_str().to_lowercase())
                    .unwrap_or_default()
            })
            .collect::<Vec<_>>()
            .join(" ");
        let label = metadata
            .get("name")
            .filter(|v| !v.is_null() && **v != "")
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
        result["arguments"] = if path.extension().is_some_and(|s| s == "py") {
            crate::python_literal::screen_arguments(&String::from_utf8_lossy(&bytes))
        } else {
            arguments(&metadata, &name).unwrap_or_else(|| json!([]))
        };
        result["inputs"] = inputs(&metadata);
        Ok(result)
    }
}

fn collect(
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

fn display_metadata(path: &Path, language: &str) -> Value {
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

fn inputs(metadata: &Value) -> Value {
    let defaults = json!({"defined_args":"auto", "extra_args":"hidden", "message":"optional"});
    let Some(raw) = metadata.get("inputs") else {
        return defaults;
    };
    let Some(raw) = raw.as_object() else {
        return defaults;
    };
    let mut result = defaults.clone();
    for (key, value) in raw {
        let allowed: &[&str] = match key.as_str() {
            "defined_args" => &["auto", "hidden"],
            "extra_args" => &["hidden", "optional"],
            "message" => &["hidden", "optional", "required"],
            _ => return defaults,
        };
        if !value.as_str().is_some_and(|s| allowed.contains(&s)) {
            return defaults;
        }
        result[key] = value.clone();
    }
    result
}

fn argument(name: &str, required: bool, default: String) -> Value {
    json!({"name":name, "kind":if name.chars().all(|c| c.is_ascii_digit()) {"positional"} else {"keyword"}, "required":required, "default":default})
}

fn arguments(metadata: &Value, command: &str) -> Option<Value> {
    let mut result = Vec::new();
    let mut declared = BTreeSet::new();
    let valid = Regex::new(r"^(?:[A-Za-z_]\w*|\d+)$").unwrap();
    if let Some(raw) = metadata.get("args") {
        for (name, definition) in raw.as_object()? {
            if !valid.is_match(name) {
                return None;
            }
            let definition = definition.as_object()?;
            if definition.keys().any(|k| k != "default" && k != "required") {
                return None;
            }
            let default = definition.get("default");
            if default.is_some_and(|v| v.is_null() || v.is_array() || v.is_object()) {
                return None;
            }
            let required = definition
                .get("required")
                .map(Value::as_bool)
                .unwrap_or(Some(default.is_none()))?;
            if required && default.is_some() {
                return None;
            }
            result.push(argument(
                name,
                required,
                default.map(python_text).unwrap_or_default(),
            ));
            declared.insert(name.clone());
        }
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
    if let Some(entries) = metadata.get("commands") {
        let entries = entries
            .as_array()
            .cloned()
            .unwrap_or_else(|| vec![entries.clone()]);
        for (i, entry) in entries.iter().enumerate() {
            let output = entry
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .or_else(|| {
                    entry
                        .get("path")
                        .and_then(Value::as_str)
                        .or_else(|| entry.as_str().and_then(|s| s.split_whitespace().next()))
                        .and_then(|s| Path::new(s).file_stem()?.to_str().map(str::to_owned))
                })
                .unwrap_or_else(|| format!("{command}__{}", i + 1));
            found.remove(&output);
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
