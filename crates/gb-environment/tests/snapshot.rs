//! Recipe identity and state are checked without starting a microVM.

use gb_base::files::FileLock;
use gb_environment::Sentence;
use gb_environment::recipe::{Recipe, Step};
use gb_environment::snapshot::{State, ready, snapshots_dir, status};
use std::path::Path;

const NAME: &str = "guildbotics-0123456789abcdef";
const OTHER: &str = "guildbotics-fedcba9876543210";

#[test]
fn recipe_matches_the_frozen_python_scripts_and_digest() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let golden: serde_json::Value = serde_json::from_slice(
        &std::fs::read(root.join("records/golden/snapshot_recipe.json")).unwrap(),
    )
    .unwrap();
    let recipe = Recipe::load(&root.join("guildbotics/templates")).unwrap();
    let expected: Vec<Step> = serde_json::from_value(golden["steps"].clone()).unwrap();
    assert_eq!(recipe.image, golden["image"]);
    assert_eq!(recipe.steps.len(), expected.len());
    for (actual, expected) in recipe.steps.iter().zip(expected) {
        assert_eq!(actual, &expected, "{}", actual.label);
    }
    for case in golden["names"].as_array().unwrap() {
        assert_eq!(recipe.name(case["held"].as_str()), case["expected"]);
    }
    let mut changed = recipe.clone();
    changed.steps[3].script.push_str("\nchanged dependency");
    assert_ne!(recipe.name(None), changed.name(None));
    changed = recipe.clone();
    changed.image.push_str("-new");
    assert_ne!(recipe.name(None), changed.name(None));
    changed = recipe.clone();
    changed.steps[5].script.push_str("\nchanged CLI pin");
    assert_ne!(recipe.name(None), changed.name(None));
}

#[test]
fn current_recipe_wins_over_old_snapshots_and_failures() {
    let workspace = tempfile::tempdir().unwrap();
    let root = workspace.path();
    let directory = snapshots_dir(root);
    assert_eq!(status(root, NAME).unwrap().state, State::Missing);
    std::fs::create_dir_all(directory.join(OTHER)).unwrap();
    assert_eq!(status(root, NAME).unwrap().state, State::Stale);
    std::fs::write(directory.join(format!("{OTHER}.failed")), "old failure").unwrap();
    std::fs::create_dir(directory.join(NAME)).unwrap();
    assert_eq!(ready(root, NAME).unwrap(), directory.join(NAME));
    std::fs::write(
        directory.join(format!("{NAME}.failed")),
        "current failure\n",
    )
    .unwrap();
    assert_eq!(
        ready(root, NAME),
        Err(Sentence::Key(
            "intelligences.agent_environment.snapshot.failed",
            vec![("detail", "current failure".into())]
        ))
    );
    let lock = FileLock::try_hold(&directory.join("build.lock"))
        .unwrap()
        .unwrap();
    assert_eq!(status(root, NAME).unwrap().state, State::Building);
    drop(lock);
    assert_eq!(
        status(root, NAME).unwrap().state,
        State::Failed("current failure".into())
    );
}

#[test]
fn non_snapshot_entries_do_not_make_a_stale_recipe() {
    let workspace = tempfile::tempdir().unwrap();
    let directory = snapshots_dir(workspace.path());
    std::fs::create_dir_all(directory.join("other")).unwrap();
    std::fs::write(directory.join(OTHER), "not a directory").unwrap();
    std::fs::write(directory.join(format!("{OTHER}.failed")), "old failure").unwrap();
    assert_eq!(
        status(workspace.path(), NAME).unwrap().state,
        State::Missing
    );
    for name in [
        "../escape",
        "guildbotics-../../escape",
        "guildbotics-a",
        "guildbotics-0123456789abcdeF",
    ] {
        assert!(status(workspace.path(), name).is_err());
    }
    std::fs::create_dir(directory.join(format!("{NAME}.failed"))).unwrap();
    assert!(status(workspace.path(), NAME).is_err());
}

#[cfg(unix)]
#[test]
fn a_link_is_not_a_built_snapshot() {
    let workspace = tempfile::tempdir().unwrap();
    let elsewhere = tempfile::tempdir().unwrap();
    let directory = snapshots_dir(workspace.path());
    std::fs::create_dir_all(&directory).unwrap();
    std::os::unix::fs::symlink(elsewhere.path(), directory.join(NAME)).unwrap();
    assert_eq!(
        status(workspace.path(), NAME).unwrap().state,
        State::Missing
    );
}
