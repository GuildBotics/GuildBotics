//! Recipe identity and state are checked without starting a microVM.

use gb_base::files::FileLock;
use gb_environment::Sentence;
use gb_environment::recipe::{Recipe, Step};
use gb_environment::snapshot::{State, ready, snapshots_dir, status};
use std::path::Path;

const NAME: &str = "guildbotics-0123456789abcdef";
const OTHER: &str = "guildbotics-fedcba9876543210";
const KEPT: &str = "guildbotics-00000000000000ff";

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
fn a_status_reader_does_not_look_like_a_snapshot_writer() {
    let workspace = tempfile::tempdir().unwrap();
    let root = workspace.path();
    let directory = snapshots_dir(root);
    std::fs::create_dir_all(directory.join(NAME)).unwrap();
    let lock_path = directory.join("build.lock");
    std::fs::File::create(&lock_path).unwrap();
    let reader = std::fs::File::open(&lock_path).unwrap();
    reader.try_lock_shared().unwrap();

    assert_eq!(ready(root, NAME).unwrap(), directory.join(NAME));
    assert!(FileLock::try_hold(&lock_path).unwrap().is_none());
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
async fn prebuild_and_upkeep_rebuild_preserve_both_recipes_until_activation() {
    let root = tempfile::tempdir().unwrap();
    let directory = snapshots_dir(root.path());
    std::fs::create_dir_all(directory.join(OTHER)).unwrap();
    for name in [NAME, OTHER] {
        gb_environment::snapshot::build(
            root.path(),
            name,
            "timeout".into(),
            move |directory, _| async move {
                let path = directory.join(name);
                std::fs::create_dir_all(&path).unwrap();
                Ok(path)
            },
        )
        .await
        .unwrap();
        assert_eq!(status(root.path(), NAME).unwrap().state, State::Ready);
        assert_eq!(status(root.path(), OTHER).unwrap().state, State::Ready);
    }
    // A failed activation can return to the old recipe without rebuilding it.
    assert_eq!(ready(root.path(), OTHER).unwrap(), directory.join(OTHER));
    std::fs::write(directory.join(format!("{OTHER}.failed")), "old failure").unwrap();
    std::fs::create_dir(directory.join("unrelated")).unwrap();
    std::fs::create_dir(directory.join(KEPT)).unwrap();
    let lock_directory = directory.clone();
    let removed = gb_environment::snapshot::prune(root.path(), NAME, move |path| {
        assert!(
            FileLock::try_hold(&lock_directory.join("build.lock"))
                .unwrap()
                .is_none()
        );
        // A recipe the caller keeps stays, and is not reported.
        let keep = path.ends_with(KEPT);
        async move {
            if keep {
                return Ok(false);
            }
            std::fs::remove_dir_all(path).map_err(|e| e.to_string())?;
            Ok(true)
        }
    })
    .await
    .unwrap();
    assert_eq!(removed, vec![OTHER]);
    assert!(directory.join(KEPT).exists());
    assert!(!directory.join(OTHER).exists());
    assert!(!directory.join(format!("{OTHER}.failed")).exists());
    assert!(directory.join("unrelated").exists());
    assert_eq!(status(root.path(), NAME).unwrap().state, State::Ready);
}

#[tokio::test]
async fn pruning_requires_a_ready_active_recipe_and_keeps_it_on_removal_failure() {
    let root = tempfile::tempdir().unwrap();
    let directory = snapshots_dir(root.path());
    std::fs::create_dir_all(directory.join(OTHER)).unwrap();
    for failed in [false, true] {
        if failed {
            std::fs::create_dir_all(directory.join(NAME)).unwrap();
            std::fs::write(directory.join(format!("{NAME}.failed")), "failed").unwrap();
        }
        assert!(
            gb_environment::snapshot::prune(root.path(), NAME, |_| async {
                panic!("unready recipe must not prune")
            })
            .await
            .is_err()
        );
        assert!(directory.join(OTHER).exists());
    }
    std::fs::remove_file(directory.join(format!("{NAME}.failed"))).unwrap();
    let held = FileLock::try_hold(&directory.join("build.lock"))
        .unwrap()
        .unwrap();
    assert!(
        gb_environment::snapshot::prune(root.path(), NAME, |_| async {
            panic!("concurrent build must not prune")
        })
        .await
        .is_err()
    );
    drop(held);
    assert!(
        gb_environment::snapshot::prune(root.path(), NAME, |_| async {
            Err("runtime removal failed".into())
        })
        .await
        .is_err()
    );
    assert_eq!(status(root.path(), NAME).unwrap().state, State::Ready);
    assert!(directory.join(OTHER).exists());
}

#[tokio::test]
async fn failed_builds_are_remembered_and_preserve_old_snapshots() {
    let root = tempfile::tempdir().unwrap();
    let old = snapshots_dir(root.path()).join(OTHER);
    std::fs::create_dir_all(&old).unwrap();
    assert!(
        gb_environment::snapshot::build(root.path(), NAME, "timeout".into(), |_, _| async {
            Err("broken".into())
        })
        .await
        .is_err()
    );
    assert_eq!(
        status(root.path(), NAME).unwrap().state,
        State::Failed("broken".into())
    );
    assert!(old.exists());
    assert!(!gb_environment::snapshot::needs_build(&State::Failed(
        "broken".into()
    )));
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
    let names = gb_environment::snapshot::remove(root.path(), |path| {
        removed.push(path.clone());
        async move { std::fs::remove_dir_all(path).map_err(|e| e.to_string()) }
    })
    .await
    .unwrap();
    assert_eq!(names, [NAME]);
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
