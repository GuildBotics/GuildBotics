//! A declaration boots a command's microVM with the resources the Python
//! version reads from it: `records/golden/environment.json`, which
//! `tests/guildbotics/test_environment_golden.py` checks against the Python
//! version.

use std::path::Path;

use gb_config::agent_environment::{PATH, Resources, resources};
use serde_json::{Value, json};

fn fixture() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../records/golden/environment.json");
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

#[test]
fn a_declaration_boots_as_the_python_version_reads_it() {
    let templates = tempfile::tempdir().unwrap();
    for case in fixture()["resources"].as_array().unwrap() {
        let id = case["id"].as_str().unwrap();
        let config = tempfile::tempdir().unwrap();
        let path = config.path().join(PATH);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, case["declaration"].as_str().unwrap()).unwrap();
        let read = resources(config.path(), templates.path())
            .ok()
            .map(|r| json!({"memory_mib": r.memory_mib, "cpus": r.cpus}));
        let read = read.unwrap_or(Value::Null);
        if id == "refused_elsewhere" {
            // Python refuses it for its missing resolvers, which a command's
            // microVM does not read.
            assert_eq!(read, json!({"memory_mib": 4096, "cpus": 4}), "{id}");
        } else {
            assert_eq!(read, case["expected"], "{id}");
        }
    }
}

#[test]
fn without_its_own_the_workspace_boots_as_the_templates_declare() {
    let config = tempfile::tempdir().unwrap();
    let templates = gb_base::home::python_package().join("templates");
    assert_eq!(
        resources(config.path(), &templates).unwrap(),
        Resources {
            memory_mib: 4096,
            cpus: 2
        }
    );
}
