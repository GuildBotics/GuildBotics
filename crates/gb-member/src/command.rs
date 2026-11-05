//! The backend owns the grammar; clients ask it how to transfer their inputs.
use crate::{MemberError, Result};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::OnceLock,
};

#[derive(Deserialize)]
pub struct Grammar {
    pub options: Vec<OptionSpec>,
    pub read_only: bool,
    pub content_required: bool,
}
#[derive(Deserialize)]
pub struct OptionSpec {
    pub spelling: String,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub multiple: bool,
    #[serde(default)]
    pub flag: bool,
    #[serde(default)]
    pub flag_value: Option<Value>,
    pub default: Option<Value>,
    pub choices: Option<Vec<String>>,
    #[serde(default)]
    pub integer: bool,
    pub min: Option<i64>,
    pub max: Option<i64>,
    pub path: Option<String>,
}
impl OptionSpec {
    fn primary(&self) -> &str {
        self.spelling.split('/').next().unwrap()
    }
    fn key(&self) -> &str {
        self.primary().trim_start_matches('-')
    }
    fn matches(&self, spelling: &str) -> bool {
        self.spelling.split('/').any(|v| v == spelling)
    }
}
pub fn grammar() -> &'static BTreeMap<String, Grammar> {
    static GRAMMAR: OnceLock<BTreeMap<String, Grammar>> = OnceLock::new();
    GRAMMAR.get_or_init(|| {
        serde_json::from_str(include_str!(
            "../../../guildbotics/templates/member/grammar.json"
        ))
        .expect("member grammar")
    })
}
pub struct Parsed {
    pub command: String,
    pub options: Map<String, Value>,
    pub provided: BTreeSet<String>,
    pub help: bool,
    pub read_only: bool,
    pub content_required: bool,
    pub rewritten: Vec<String>,
    pub file_source: Option<Value>,
}
fn error(key: &'static str, option: &str) -> MemberError {
    MemberError::CapabilityArgs {
        key,
        args: [("option".into(), option.into())].into(),
    }
}
impl Parsed {
    pub fn text(&self, key: &str) -> &str {
        self.options.get(key).and_then(Value::as_str).unwrap_or("")
    }
    pub fn optional(&self, key: &str) -> Option<String> {
        self.options
            .get(key)
            .filter(|v| !v.is_null())
            .and_then(Value::as_str)
            .map(str::to_owned)
    }
    pub fn flag(&self, key: &str) -> bool {
        self.options.get(key) == Some(&Value::Bool(true))
    }
    pub fn list(&self, key: &str) -> Vec<String> {
        self.options
            .get(key)
            .and_then(Value::as_array)
            .map(|v| {
                v.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default()
    }
    pub fn integer(&self, key: &str) -> Option<i64> {
        self.options.get(key).and_then(Value::as_i64)
    }
    pub fn content(&self, stdin: &str) -> Result<Option<String>> {
        if self.provided.contains("content-file") {
            return Err(MemberError::Capability(
                "cli.member.content.file_in_environment",
            ));
        }
        let selected = self.flag("content-stdin");
        if self.content_required && !selected {
            return Err(MemberError::Capability("cli.member.content.required"));
        }
        if selected && stdin.trim().is_empty() {
            if matches!(self.command.as_str(), "ticket update" | "pr update") {
                return Ok(Some(String::new()));
            }
            return Err(MemberError::Capability("cli.member.content.empty"));
        }
        Ok(selected.then(|| stdin.into()))
    }
    pub fn plan(&self) -> Value {
        let file = self.optional("content-file");
        let current = self.command.starts_with("git ") && self.text("workspace-mode") == "current";
        let git = if current {
            let mut params = Map::new();
            params.insert(
                "operation".into(),
                self.command.split_whitespace().last().unwrap().into(),
            );
            for (key, value) in &self.options {
                let key = if key == "format" {
                    "output_format".into()
                } else {
                    key.replace('-', "_")
                };
                params.insert(key, value.clone());
            }
            Value::Object(params)
        } else {
            Value::Null
        };
        json!({"arguments":self.rewritten,"output_format":self.text("format"),"download":matches!(self.command.as_str(),"pr artifact download"|"ticket attachment download"),"content_file":file,"file_source":self.file_source,"stdin":file.is_some()||self.flag("content-stdin"),"memory":self.command.starts_with("memory "),"git":git})
    }
}
pub fn parse(arguments: &[String]) -> Result<Parsed> {
    let offset = arguments
        .iter()
        .position(|v| v.starts_with('-'))
        .unwrap_or(arguments.len());
    let command = arguments[..offset].join(" ");
    let schema = grammar().get(&command);
    let group = schema.is_none()
        && grammar()
            .keys()
            .any(|v| v.starts_with(&(command.clone() + " ")));
    let mut parsed = Parsed {
        command: command.clone(),
        options: Map::new(),
        provided: BTreeSet::new(),
        help: arguments.is_empty() || arguments == ["--help"],
        read_only: schema.is_some_and(|v| v.read_only),
        content_required: schema.is_some_and(|v| v.content_required),
        rewritten: arguments[..offset].to_vec(),
        file_source: None,
    };
    if parsed.help
        || arguments.is_empty()
        || group && arguments[offset..].iter().all(|v| v == "--help")
    {
        parsed.help = true;
        return Ok(parsed);
    }
    let schema = schema.ok_or_else(|| error("cli.member.unknown_command", &command))?;
    for spec in &schema.options {
        let default = if spec.multiple {
            json!([])
        } else {
            spec.default
                .clone()
                .unwrap_or(if spec.flag { json!(false) } else { Value::Null })
        };
        parsed.options.insert(spec.key().into(), default);
    }
    let mut index = offset;
    while index < arguments.len() {
        let token = &arguments[index];
        if token == "--help" {
            parsed.help = true;
            index += 1;
            continue;
        }
        let (spelling, inline) = token
            .split_once('=')
            .map_or((token.as_str(), None), |(k, v)| (k, Some(v)));
        let spec = schema
            .options
            .iter()
            .find(|s| s.matches(spelling))
            .ok_or_else(|| error("cli.member.unknown_option", spelling))?;
        let value = if spec.flag {
            if inline.is_some() {
                return Err(error("cli.member.option_flag", spelling));
            }
            spec.flag_value
                .clone()
                .unwrap_or_else(|| Value::Bool(spelling == spec.primary()))
        } else {
            let raw = match inline {
                Some(raw) => raw,
                None => arguments
                    .get(index + 1)
                    .map(String::as_str)
                    .ok_or_else(|| error("cli.member.option_value", spelling))?,
            };
            if spec
                .choices
                .as_ref()
                .is_some_and(|values| !values.iter().any(|v| v == raw))
            {
                return Err(error("cli.member.option_choice", spelling));
            }
            if spec.integer {
                let number = raw
                    .parse::<i64>()
                    .map_err(|_| error("cli.member.option_integer", spelling))?;
                if spec.min.is_some_and(|min| number < min)
                    || spec.max.is_some_and(|max| number > max)
                {
                    return Err(error("cli.member.option_integer", spelling));
                }
                number.into()
            } else {
                raw.into()
            }
        };
        let count = if spec.flag || inline.is_some() { 1 } else { 2 };
        if spec.key() == "content-file" {
            parsed.file_source = Some(
                json!({"index":if inline.is_some(){index}else{index+1},"offset":if inline.is_some(){spelling.len()+1}else{0}}),
            );
            parsed.rewritten.push("--content-stdin".into());
        } else {
            parsed
                .rewritten
                .extend_from_slice(&arguments[index..index + count]);
        }
        if spec.multiple {
            parsed
                .options
                .get_mut(spec.key())
                .unwrap()
                .as_array_mut()
                .unwrap()
                .push(value);
        } else {
            parsed.options.insert(spec.key().into(), value);
        }
        parsed.provided.insert(spec.key().into());
        index += count;
    }
    if parsed.help {
        return Ok(parsed);
    }
    for spec in &schema.options {
        if spec.required && !parsed.provided.contains(spec.key()) {
            return Err(error("cli.member.option_required", spec.primary()));
        }
    }
    if parsed.flag("content-stdin") && parsed.provided.contains("content-file") {
        return Err(MemberError::Capability("cli.member.content.exclusive"));
    }
    Ok(parsed)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| (*v).into()).collect()
    }
    #[test]
    fn content_plans_rewrite_only_the_file_option_and_preserve_option_like_values() {
        for file in ["--content-file=note.md", "--content-file"] {
            let mut input = args(&[
                "ticket",
                "create",
                "--person",
                "aiko",
                "--tracker",
                "o/r",
                "--title",
                "--content-file",
                file,
            ]);
            if file == "--content-file" {
                input.push("note.md".into());
            }
            let parsed = parse(&input).unwrap();
            let plan = parsed.plan();
            assert_eq!(plan["content_file"], "note.md");
            assert_eq!(plan["arguments"][7], "--content-file");
            assert_eq!(plan["arguments"][8], "--content-stdin");
            assert!(parsed.content("body").is_err());
        }
        let input = args(&[
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "--workspace",
            "--content-stdin",
        ]);
        assert_eq!(parse(&input).unwrap().text("url"), "--workspace");
    }
    #[test]
    fn option_errors_and_optional_body_selection_fail_before_dispatch() {
        for input in [
            args(&["ticket", "comment", "--person"]),
            args(&["memory", "recall", "--person", "aiko", "--limit", "0"]),
            args(&[
                "pr", "review", "--person", "aiko", "--url", "url", "--head", "head", "--event",
                "unknown",
            ]),
        ] {
            assert!(parse(&input).is_err());
        }
        let parsed = parse(&args(&[
            "memory", "update", "--person", "aiko", "--id", "doc", "--title", "title",
        ]))
        .unwrap();
        assert_eq!(parsed.content("unused").unwrap(), None);
        let parsed = parse(&args(&[
            "ticket", "comment", "--person", "aiko", "--url", "url",
        ]))
        .unwrap();
        assert!(parsed.content("unused").is_err());
        let parsed = parse(&args(&[
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "url",
            "--content-stdin",
        ]))
        .unwrap();
        assert!(parsed.content(" ").is_err());
        assert_eq!(parsed.content("本文\n").unwrap(), Some("本文\n".into()));
    }
    #[test]
    fn grammar_classifies_writes_by_metadata_and_current_git_plans_preserve_defaults() {
        for (path, schema) in grammar() {
            assert!(!schema.options.is_empty() || path == "help");
            if path.ends_with(" read") || path == "git show" {
                assert!(schema.read_only);
            }
        }
        let parsed = parse(&args(&[
            "git",
            "publish",
            "--person",
            "aiko",
            "--repo-path",
            "/caller",
            "--workspace-mode",
            "current",
            "--content-stdin",
        ]))
        .unwrap();
        assert_eq!(parsed.plan()["git"]["operation"], "publish");
        assert_eq!(parsed.plan()["git"]["repo_path"], "/caller");
        assert!(parse(&args(&["pr", "review", "--help"])).unwrap().help);
    }
    #[test]
    fn every_path_option_is_classified_by_its_transfer_owner() {
        let mut paths = BTreeMap::new();
        let mut content = 0;
        for (command, grammar) in grammar() {
            for option in &grammar.options {
                if let Some(kind) = &option.path {
                    if kind == "content" {
                        assert_eq!(option.key(), "content-file");
                        content += 1;
                    } else {
                        paths.insert((command.as_str(), option.key()), kind.as_str());
                    }
                }
            }
        }
        assert!(content > 0);
        assert_eq!(
            paths,
            [
                (("git commit", "repo-path"), "git"),
                (("git push", "repo-path"), "git"),
                (("git publish", "repo-path"), "git"),
                (("pr artifact download", "dest"), "download"),
                (("ticket attachment download", "dest"), "download")
            ]
            .into()
        );
    }
}
