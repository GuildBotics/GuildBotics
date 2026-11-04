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

#[tokio::test]
async fn failed_builds_are_remembered_and_only_success_prunes_old_snapshots() {
    let root = tempfile::tempdir().unwrap();
    let name = "guildbotics-0123456789abcdef";
    let old =
        gb_environment::snapshot::snapshots_dir(root.path()).join("guildbotics-1111111111111111");
    std::fs::create_dir_all(&old).unwrap();
    assert!(
        gb_environment::snapshot::build(
            root.path(),
            name,
            "timeout".into(),
            |_, _| async { Err("broken".into()) },
            |_| async { panic!("failed build must not prune") }
        )
        .await
        .is_err()
    );
    assert_eq!(
        gb_environment::snapshot::status(root.path(), name)
            .unwrap()
            .state,
        gb_environment::snapshot::State::Failed("broken".into())
    );
    assert!(old.exists());
    assert!(!gb_environment::snapshot::needs_build(
        &gb_environment::snapshot::State::Failed("broken".into())
    ));
    let removed = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let removed_by_build = removed.clone();
    let result = gb_environment::snapshot::build(
        root.path(),
        name,
        "timeout".into(),
        move |directory, _| async move {
            let path = directory.join(name);
            std::fs::create_dir(&path).unwrap();
            Ok(path)
        },
        move |path| {
            removed_by_build.lock().unwrap().push(path.clone());
            async move { std::fs::remove_dir_all(path).map_err(|e| e.to_string()) }
        },
    )
    .await
    .unwrap();
    assert_eq!(*removed.lock().unwrap(), vec![old.clone()]);
    assert_eq!(result.state, gb_environment::snapshot::State::Ready);
    assert!(!old.exists());
}

#[tokio::test]
async fn cleanup_runs_under_build_lock_and_propagates_runtime_failure() {
    let root = tempfile::tempdir().unwrap();
    let directory = snapshots_dir(root.path());
    let old = directory.join(OTHER);
    std::fs::create_dir_all(&old).unwrap();
    let cleanup_directory = directory.clone();
    let cleanup_old = old.clone();
    let result = gb_environment::snapshot::build(
        root.path(),
        NAME,
        "timeout".into(),
        move |directory, _| async move {
            let path = directory.join(NAME);
            std::fs::create_dir(&path).unwrap();
            Ok(path)
        },
        move |path| {
            assert_eq!(path, cleanup_old);
            assert!(
                FileLock::try_hold(&cleanup_directory.join("build.lock"))
                    .unwrap()
                    .is_none()
            );
            async { Err("runtime metadata removal failed".into()) }
        },
    )
    .await;
    assert_eq!(result, Err("runtime metadata removal failed".into()));
    assert!(old.is_dir());
    assert!(directory.join(NAME).is_dir());
}

#[tokio::test]
async fn a_second_build_is_refused_without_running_or_pruning() {
    let root = tempfile::tempdir().unwrap();
    let directory = snapshots_dir(root.path());
    let _held = FileLock::try_hold(&directory.join("build.lock"))
        .unwrap()
        .unwrap();
    let result = gb_environment::snapshot::build(
        root.path(),
        NAME,
        "timeout".into(),
        |_, _| async { panic!("locked build must not run") },
        |_| async { panic!("locked build must not prune") },
    )
    .await;
    assert_eq!(
        result,
        Err("An environment build is already running".into())
    );
}

#[test]
fn doctor_places_the_pinned_bundle_and_repairs_missing_files() {
    let dir = tempfile::tempdir().unwrap();
    let bundle = dir.path().join("bundle");
    let home = dir.path().join("home");
    for file in gb_environment::placed_files(&bundle) {
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        std::fs::write(file, b"runtime").unwrap();
    }
    gb_environment::runtime::doctor(&home, &bundle).unwrap();
    gb_environment::runtime::ready(&home).unwrap();
    std::fs::remove_file(&gb_environment::placed_files(&home)[1]).unwrap();
    gb_environment::runtime::doctor(&home, &bundle).unwrap();
    gb_environment::runtime::ready(&home).unwrap();
}

#[tokio::test]
async fn removing_snapshots_uses_metadata_callback_and_keeps_login_files() {
    let root = tempfile::tempdir().unwrap();
    let directory = snapshots_dir(root.path());
    std::fs::create_dir_all(directory.join(NAME)).unwrap();
    std::fs::write(directory.join(format!("{NAME}.failed")), "broken").unwrap();
    let login = root
        .path()
        .join(".guildbotics/local/agent_environment/logins");
    std::fs::create_dir_all(&login).unwrap();
    std::fs::write(login.join("fixture"), b"fake-login-state").unwrap();
    let mut removed = Vec::new();
    gb_environment::snapshot::remove(root.path(), |path| {
        removed.push(path.clone());
        async move { std::fs::remove_dir_all(path).map_err(|e| e.to_string()) }
    })
    .await
    .unwrap();
    assert_eq!(removed, vec![directory.join(NAME)]);
    assert_eq!(
        std::fs::read(login.join("fixture")).unwrap(),
        b"fake-login-state"
    );
    assert!(!directory.join(format!("{NAME}.failed")).exists());
    let held = gb_base::files::FileLock::try_hold(&directory.join("build.lock"))
        .unwrap()
        .unwrap();
    assert!(
        gb_environment::snapshot::remove(root.path(), |_| async { panic!("locked") })
            .await
            .is_err()
    );
    drop(held);
}
