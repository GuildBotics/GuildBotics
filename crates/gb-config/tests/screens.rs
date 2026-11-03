//! The Python command-file answers, including localized and shadowed files.
use gb_config::commands::CommandRoots;
use serde_json::Value;
use std::path::Path;

#[test]
fn command_files_match_python() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../records/golden/screens.json")).unwrap();
    let root = tempfile::tempdir().unwrap();
    for (path, content) in fixture["config"].as_object().unwrap() {
        let path = root.path().join(path);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content.as_str().unwrap()).unwrap();
    }
    let roots = CommandRoots {
        config: root.path().to_owned(),
        templates: Path::new(env!("CARGO_MANIFEST_DIR")).join("../../guildbotics/templates"),
    };
    let identity = root.path().join("identity");
    for (name, content) in fixture["identity"].as_object().unwrap() {
        let path = identity.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content.as_str().unwrap()).unwrap();
    }
    let team = gb_config::team::Team::load(root.path(), &identity).unwrap();
    assert_eq!(
        team.screen_summary(root.path(), &identity).unwrap(),
        fixture["expected"]["team"]
    );
    assert_eq!(
        roots.shared_files("ja").unwrap(),
        fixture["expected"]["files"]
    );
    for detail in fixture["expected"]["details"].as_array().unwrap() {
        let path = roots
            .selected_file(detail["id"].as_str().unwrap(), "ja")
            .unwrap();
        assert_eq!(
            roots.file_detail(&path, "ja").unwrap(),
            *detail,
            "{}",
            detail["command"]
        );
    }
    for invalid in [
        "Li4vY29tbWFuZHMvbG9vay5qYS5tZA",
        "L2V0Yy9wYXNzd2Q",
        "aWdub3JlZC5tZXRhZGF0YS55bWw",
        "bG9vay5mci5tZA",
        "bG9vay5tZA",
        "???",
    ] {
        assert!(roots.selected_file(invalid, "ja").is_none(), "{invalid}");
    }
}
