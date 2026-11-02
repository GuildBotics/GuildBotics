//! Commands resolve, declare, and run in their team as the Python version has
//! them: `records/golden/commands.json`, which
//! `tests/guildbotics/test_commands_golden.py` checks against the Python
//! version.

use std::path::{Path, PathBuf};

use gb_config::commands::{CommandProblem, CommandRoots, command_access, command_metadata};
use gb_config::team::Team;
use serde_json::{Value, json};

fn fixture() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../records/golden/commands.json");
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

fn templates() -> PathBuf {
    gb_base::home::python_package().join("templates")
}

/// `path` as the fixture names it: under `config` or `templates`.
fn relative(path: &Path, roots: &[(&str, &Path)]) -> String {
    for (name, root) in roots {
        if let Ok(rest) = path.strip_prefix(root) {
            return format!("{name}/{}", rest.to_string_lossy());
        }
    }
    panic!("{}", path.display())
}

/// What the command declares as the fixture spells it; null when refused.
fn access(path: &Path) -> Value {
    json!(command_access(path).ok().map(|access| {
        let mut inspects: Vec<Value> = access
            .inspects
            .iter()
            .map(|scope| serde_json::to_value(scope).unwrap())
            .collect();
        inspects.sort_by_key(|scope| scope.to_string());
        json!({"read_only": access.read_only, "inspects": inspects})
    }))
}

fn metadata(path: &Path) -> Value {
    json!(
        command_metadata(path)
            .ok()
            .map(|d| Value::Object(d.metadata))
    )
}

/// What the Python version's parse declares, as a command's metadata reads
/// it: a field without a value is no field.
fn declared(parsed: &Value) -> Value {
    match parsed {
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .filter(|(_, value)| !value.is_null())
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect(),
        ),
        other => other.clone(),
    }
}

#[test]
fn the_bundled_commands_resolve_and_declare_as_the_fixture_has_them() {
    let fixture = fixture();
    let bundled = &fixture["bundled"];
    let empty = tempfile::tempdir().unwrap();
    let roots = CommandRoots {
        config: empty.path().join("config"),
        templates: templates(),
    };
    for (key, expected) in bundled["resolved"].as_object().unwrap() {
        let (name, language) = key.split_once('@').unwrap();
        let found = roots
            .resolve(name, language, None)
            .ok()
            .map(|p| relative(&p, &[("templates", &roots.templates)]));
        assert_eq!(json!(found), *expected, "{key}");
    }
    for (file, expected) in bundled["access"].as_object().unwrap() {
        let path = roots
            .templates
            .join(file.strip_prefix("templates/").unwrap());
        assert_eq!(access(&path), *expected, "{file}");
    }
    for (file, expected) in bundled["python_metadata"].as_object().unwrap() {
        let path = roots
            .templates
            .join(file.strip_prefix("templates/").unwrap());
        assert_eq!(metadata(&path), declared(expected), "{file}");
    }
}

#[test]
fn a_module_declares_as_the_fixture_has_it() {
    for case in fixture()["python_sources"].as_array().unwrap() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("command.py");
        std::fs::write(&path, case["source"].as_str().unwrap()).unwrap();
        let expected = &case["expected"];
        assert_eq!(
            metadata(&path),
            declared(&expected["metadata"]),
            "{}",
            case["id"]
        );
        assert_eq!(access(&path), expected["access"], "{}", case["id"]);
    }
}

fn write(root: &Path, files: &Value) {
    for (name, text) in files.as_object().unwrap() {
        let path = root.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text.as_str().unwrap()).unwrap();
    }
}

fn team(config: &Path, identity: &Path) -> Value {
    json!(Team::load(config, identity).ok().map(|team| {
        json!({
            "language": team.language,
            "default_person_id": team.default_person_id(),
            "members": team.members.iter().map(|m| json!({
                "person_id": m.person_id,
                "name": m.name,
                "person_type": m.person_type,
                "is_active": m.is_active,
            })).collect::<Vec<_>>(),
        })
    }))
}

#[test]
fn a_workspace_is_read_as_the_fixture_has_it() {
    for case in fixture()["workspaces"].as_array().unwrap() {
        let root = tempfile::tempdir().unwrap();
        let (config, identity) = (root.path().join("config"), root.path().join("identity"));
        write(&config, &case["config"]);
        write(&identity.join(".guildbotics/config"), &case["identity"]);
        let roots = CommandRoots {
            config: config.clone(),
            templates: templates(),
        };
        let mut resolved = serde_json::Map::new();
        for each in case["resolve"].as_array().unwrap() {
            let (name, language) = (each[0].as_str().unwrap(), each[1].as_str().unwrap());
            let person = each[2].as_str();
            let key = format!("{name}@{language}@{}", person.unwrap_or("None"));
            let found = match roots.resolve(name, language, person) {
                Ok(path) => json!(relative(
                    &path,
                    &[("config", &config), ("templates", &roots.templates)]
                )),
                Err(CommandProblem::InvalidPerson(_)) => json!("invalid person"),
                Err(_) => Value::Null,
            };
            resolved.insert(key, found);
        }
        let commands = case["config"].as_object().unwrap().keys().filter(|name| {
            let extension = Path::new(name)
                .extension()
                .map(|e| e.to_string_lossy().into_owned());
            name.starts_with("commands/")
                && matches!(
                    extension.as_deref(),
                    Some("md" | "py" | "sh" | "yaml" | "yml")
                )
                && !name.contains(".metadata.")
        });
        let declared: serde_json::Map<String, Value> = commands
            .map(|name| (name.clone(), access(&config.join(name))))
            .collect();
        let read =
            json!({"resolved": resolved, "access": declared, "team": team(&config, &identity)});
        assert_eq!(read, case["expected"], "{}", case["id"]);
    }
}
