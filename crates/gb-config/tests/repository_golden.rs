//! Expected revisions come from Python, never a second Rust implementation.
#![cfg(unix)]
use gb_base::shared_writes::SharedWrites;
use gb_config::repository::ConfigRepository;
use serde_json::{Value, json};
use std::{fs, path::Path};

#[test]
fn config_directory_revisions_match_python() {
    let fixture: Value = serde_json::from_slice(
        &fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../records/golden/config_repository.json"),
        )
        .unwrap(),
    )
    .unwrap();
    for case in fixture.as_array().unwrap() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("workspace");
        let config = workspace.join(".guildbotics/config");
        fs::create_dir_all(&config).unwrap();
        let outside = root.path().join("outside");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("file"), "outside").unwrap();
        for (name, text) in case["files"].as_object().unwrap() {
            let path = config.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, text.as_str().unwrap()).unwrap();
        }
        if let Some(links) = case["links"].as_object() {
            for (name, target) in links {
                let target = target.as_str().unwrap();
                let destination = match target.strip_prefix("<outside>") {
                    Some(rest) => outside.join(rest.trim_start_matches('/')),
                    None => config.join(target),
                };
                std::os::unix::fs::symlink(destination, config.join(name)).unwrap();
            }
        }
        let repo = ConfigRepository::new(SharedWrites::new(
            root.path().join("identity"),
            Some(workspace),
        ))
        .unwrap();
        for directory in case["dirs"].as_array().unwrap() {
            let directory = directory.as_str().unwrap();
            let actual = match repo.tree_revisions(directory) {
                Ok(revisions) => json!({"revisions": revisions}),
                Err(error) => {
                    assert!(
                        error.to_string().contains("config path escapes"),
                        "{error:#}"
                    );
                    json!({"error": "invalid_path"})
                }
            };
            assert_eq!(
                actual, case["expected"][directory],
                "{}: {directory}",
                case["name"]
            );
        }
    }
}
