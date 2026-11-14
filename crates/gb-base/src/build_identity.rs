//! Build identity detection, also exercised against isolated repositories.
use std::{path::Path, process::Command};

pub fn identity(root: &Path) -> (String, bool) {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success());
    let (commit, dirty) = if let Some(output) = output {
        let dirty = !Command::new("git")
            .arg("-C")
            .arg(root)
            .args(["diff", "--quiet", "HEAD"])
            .status()
            .expect("git diff")
            .success();
        (
            String::from_utf8(output.stdout).expect("commit UTF-8"),
            dirty,
        )
    } else {
        (
            std::fs::read_to_string(root.join(".build-commit"))
                .expect("build needs a checkout or git archive"),
            false,
        )
    };
    let commit = commit.trim().to_owned();
    assert!(commit.len() == 40 && commit.bytes().all(|b| b.is_ascii_hexdigit()));
    (commit, dirty)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn commit_is_target_independent_and_only_tracked_changes_are_development() {
        let root = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| {
            let out = Command::new("git")
                .arg("-C")
                .arg(root.path())
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .env("GIT_CONFIG_GLOBAL", "")
                .args(args)
                .output()
                .unwrap();
            assert!(out.status.success(), "{out:?}");
        };
        git(&["init"]);
        std::fs::write(root.path().join("source"), "one").unwrap();
        git(&["add", "source"]);
        git(&[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "fixture",
        ]);
        let (commit, dirty) = identity(root.path());
        assert!(!dirty);
        std::fs::create_dir(root.path().join(".ci")).unwrap();
        std::fs::write(root.path().join(".ci/cache"), "cache").unwrap();
        assert_eq!(identity(root.path()), (commit.clone(), false));
        std::fs::write(root.path().join("source"), "two").unwrap();
        assert_eq!(identity(root.path()), (commit.clone(), true));
        git(&["add", "source"]);
        assert_eq!(identity(root.path()), (commit.clone(), true));
        let archive = tempfile::tempdir().unwrap();
        std::fs::write(archive.path().join(".build-commit"), &commit).unwrap();
        assert_eq!(identity(archive.path()), (commit, false));
    }
}
