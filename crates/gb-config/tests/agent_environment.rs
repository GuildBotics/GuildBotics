//! A declaration boots a command's microVM with the resources the Python
//! version reads from it: `records/golden/environment.json`, which
//! `tests/guildbotics/test_environment_golden.py` checks against the Python
//! version.

use std::path::Path;

use gb_config::agent_environment::{PATH, Resources, resources};
use serde_json::{Value, json};

#[test]
fn invalid_declarations_name_the_component_and_the_selected_file() {
    use gb_config::agent_environment::image;
    let config = tempfile::tempdir().unwrap();
    let templates = tempfile::tempdir().unwrap();
    let path = config.path().join(PATH);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    let resource_error = format!(
        "The resources in {} must be positive integers: memory_mib and cpus.",
        path.display()
    );
    let image_error = format!(
        "The base image in {} must declare a reference and SHA256 digests by architecture.",
        path.display()
    );
    for declaration in [
        "resources: {cpus: 0}",
        "resources: {cpus: true}",
        "resources: {cpus: 1.5}",
        "resources: {cpus: '2'}",
        "resources: {memory_mib: -1}",
        "resources: {unknown: 1}",
        "resources: []",
        "[",
        "[]",
    ] {
        std::fs::write(&path, declaration).unwrap();
        assert_eq!(
            resources(config.path(), templates.path())
                .unwrap_err()
                .to_string(),
            resource_error,
            "{declaration}"
        );
    }
    for declaration in [
        "image: {}",
        "image: {reference: local/agent:1, digests: {}}",
        "image: []",
        "[",
        "[]",
    ] {
        std::fs::write(&path, declaration).unwrap();
        assert_eq!(
            image(config.path(), templates.path())
                .unwrap_err()
                .to_string(),
            image_error,
            "{declaration}"
        );
    }
    // An I/O failure uses the same component and the fallback file's path.
    std::fs::remove_file(&path).unwrap();
    let fallback = templates.path().join(PATH);
    assert_eq!(
        resources(config.path(), templates.path())
            .unwrap_err()
            .to_string(),
        resource_error.replace(&path.display().to_string(), &fallback.display().to_string())
    );
    assert_eq!(
        image(config.path(), templates.path())
            .unwrap_err()
            .to_string(),
        image_error.replace(&path.display().to_string(), &fallback.display().to_string())
    );
}

#[test]
fn a_custom_image_has_a_reference_and_content_digests() {
    use gb_config::agent_environment::image;
    let config = tempfile::tempdir().unwrap();
    let templates = tempfile::tempdir().unwrap();
    let path = config.path().join(PATH);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    let valid = json!({"reference": "local/agent:1", "digests": {"arm64": format!("sha256:{}", "a".repeat(64))}});
    std::fs::write(&path, json!({"image": valid}).to_string()).unwrap();
    let read = image(config.path(), templates.path()).unwrap().unwrap();
    assert_eq!(read.reference, "local/agent:1");
    assert_eq!(read.digests["arm64"], format!("sha256:{}", "a".repeat(64)));
    for field in ["reference", "digests"] {
        let mut absent = valid.clone();
        absent.as_object_mut().unwrap().remove(field);
        std::fs::write(&path, json!({"image": absent}).to_string()).unwrap();
        assert!(image(config.path(), templates.path()).is_err());
    }
    for reference in ["", "-option", "two words", "line\nbreak"] {
        let mut invalid = valid.clone();
        invalid["reference"] = json!(reference);
        std::fs::write(&path, json!({"image": invalid}).to_string()).unwrap();
        assert!(image(config.path(), templates.path()).is_err());
    }
    for digests in [
        json!({}),
        json!({"arm64": "sha256:short"}),
        json!({"arm64": format!("sha256:{}", "A".repeat(64))}),
        json!({"ARM": format!("sha256:{}", "a".repeat(64))}),
        json!({"x86_64": format!("sha256:{}", "a".repeat(64))}),
    ] {
        let mut invalid = valid.clone();
        invalid["digests"] = digests;
        std::fs::write(&path, json!({"image": invalid}).to_string()).unwrap();
        assert!(image(config.path(), templates.path()).is_err());
    }
    for declaration in [json!({}), json!({"image": null})] {
        std::fs::write(&path, declaration.to_string()).unwrap();
        assert_eq!(image(config.path(), templates.path()).unwrap(), None);
    }
    std::fs::remove_file(&path).unwrap();
    let template = templates.path().join(PATH);
    std::fs::create_dir_all(template.parent().unwrap()).unwrap();
    std::fs::write(&template, json!({"image": valid}).to_string()).unwrap();
    assert_eq!(image(config.path(), templates.path()).unwrap(), Some(read));
}

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
