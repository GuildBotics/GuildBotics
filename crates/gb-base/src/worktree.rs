//! Host-side selection of a command's working-directory copy.
use anyhow::{Context, Result, bail};
use cap_fs_ext::DirExt;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    path::{Path, PathBuf},
    process::Command,
};
use unicode_normalization::UnicodeNormalization;

/// Paths are relative, spelled as on disk, and contain no links or special files.
#[derive(Debug, Serialize, Deserialize)]
pub struct Manifest {
    pub directories: Vec<String>,
    pub files: Vec<String>,
    pub repository: bool,
}

fn composed(value: &Path) -> PathBuf {
    value
        .to_str()
        .map(|s| PathBuf::from(s.nfc().collect::<String>()))
        .unwrap_or_else(|| value.to_owned())
}

fn git(root: &Path, global: &Path, args: &[OsString]) -> Result<BTreeSet<PathBuf>> {
    let mut command = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            command.env_remove(key);
        }
    }
    let output = command
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", global)
        .args([
            "--literal-pathspecs",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=",
            "-c",
        ])
        .arg({
            let mut setting = OsString::from("core.excludesFile=");
            setting.push(global);
            setting
        })
        // Keep disk spelling even on macOS; the index may use NFC.
        .args(["-c", "core.precomposeunicode=false"])
        .arg("--git-dir")
        .arg(root.join(".git"))
        .arg("--work-tree")
        .arg(root)
        .args(["ls-files", "-z"])
        .args(args)
        .current_dir(root)
        .output()
        .context("Listing worktree with git")?;
    if !output.status.success() {
        bail!("git ls-files: {}", String::from_utf8_lossy(&output.stderr));
    }
    output
        .stdout
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| {
            let s = s.strip_suffix(b"/").unwrap_or(s);
            #[cfg(unix)]
            let name = {
                use std::os::unix::ffi::OsStringExt;
                OsString::from_vec(s.to_vec())
            };
            #[cfg(not(unix))]
            let name = OsString::from(std::str::from_utf8(s)?);
            Ok(PathBuf::from(name))
        })
        .collect()
}

/// Select files without user/system Git configuration or inherited Git environment.
/// A gitfile at the root deliberately does not enable repository filtering.
pub fn list(root: &Path, excluded: &[String]) -> Result<Manifest> {
    let dir = crate::host_path::open_directory(root, false)?;
    let repository = dir.symlink_metadata(".git").is_ok_and(|m| m.is_dir());
    let mut ignored = BTreeSet::new();
    if repository {
        let global = tempfile::NamedTempFile::new()?;
        let tracked: BTreeSet<_> = git(root, global.path(), &["--cached".into()])?
            .iter()
            .map(|s| composed(s))
            .collect();
        let holding: BTreeSet<_> = tracked
            .iter()
            .flat_map(|s| s.ancestors().skip(1).map(Path::to_owned))
            .collect();
        let flags: Vec<OsString> = ["--others", "--ignored", "--exclude-standard"]
            .map(Into::into)
            .to_vec();
        for path in git(
            root,
            global.path(),
            &[flags.as_slice(), &["--directory".into()]].concat(),
        )? {
            if holding.contains(&composed(&path)) {
                ignored.extend(git(
                    root,
                    global.path(),
                    &[
                        flags.as_slice(),
                        &["--".into(), {
                            let mut p = path.as_os_str().to_owned();
                            p.push("/");
                            p
                        }],
                    ]
                    .concat(),
                )?);
            } else {
                ignored.insert(path);
            }
        }
        ignored.retain(|s| !tracked.contains(&composed(s)));
    }
    let mut manifest = Manifest {
        directories: vec![],
        files: vec![],
        repository,
    };
    walk(&dir, Path::new(""), excluded, &ignored, &mut manifest)?;
    Ok(manifest)
}

fn walk(
    dir: &cap_std::fs::Dir,
    prefix: &Path,
    excluded: &[String],
    ignored: &BTreeSet<PathBuf>,
    out: &mut Manifest,
) -> Result<()> {
    let mut entries = dir.entries()?.collect::<std::io::Result<Vec<_>>>()?;
    entries.sort_by_key(|e| e.file_name());
    for entry in entries {
        let name = entry.file_name();
        let path = prefix.join(&name);
        if name
            .to_str()
            .is_some_and(|s| s.eq_ignore_ascii_case(".git"))
            || path == Path::new(".guildbotics")
            || excluded.iter().any(|s| Path::new(s) == path)
            || ignored.contains(&path)
        {
            continue;
        }
        let kind = entry.file_type()?;
        if kind.is_dir() {
            out.directories.push(manifest_path(&path)?);
            walk(
                &dir.open_dir_nofollow(&name)?,
                &path,
                excluded,
                ignored,
                out,
            )?;
        } else if kind.is_file() {
            out.files.push(manifest_path(&path)?);
        }
    }
    Ok(())
}

fn manifest_path(path: &Path) -> Result<String> {
    path.iter()
        .map(|part| part.to_str().context("Non-UTF-8 worktree path"))
        .collect::<Result<Vec<_>>>()
        .map(|parts| parts.join("/"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_directory_selection_preserves_empty_parents_and_excludes_mounts() {
        let root = tempfile::tempdir().unwrap();
        for path in ["empty", "child/.GIT", ".guildbotics", "mount/sub"] {
            std::fs::create_dir_all(root.path().join(path)).unwrap();
        }
        for path in [
            "child/file",
            "child/.GIT/config",
            ".guildbotics/private",
            "mount/sub/file",
        ] {
            std::fs::write(root.path().join(path), "data").unwrap();
        }
        let manifest = list(root.path(), &["mount".into()]).unwrap();
        assert!(!manifest.repository);
        assert_eq!(manifest.directories, ["child", "empty"]);
        assert_eq!(manifest.files, ["child/file"]);
        let wire = serde_json::to_vec(&manifest).unwrap();
        let decoded: Manifest = serde_json::from_slice(&wire).unwrap();
        assert_eq!(decoded.files, manifest.files);
    }

    #[test]
    fn invalid_repository_is_an_error_instead_of_an_unfiltered_copy() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join(".git")).unwrap();
        assert!(
            list(root.path(), &[])
                .unwrap_err()
                .to_string()
                .contains("git ls-files")
        );
    }

    fn fixture_git(root: &Path, args: &[&str]) -> String {
        let output = Command::new("git")
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap())
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env(
                "GIT_CONFIG_GLOBAL",
                tempfile::NamedTempFile::new().unwrap().path(),
            )
            .args(args)
            .current_dir(root)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    }

    fn files(root: &Path, names: &[(&str, &str)]) {
        for &(name, data) in names {
            let target = root.join(name);
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();
            std::fs::write(target, data).unwrap();
        }
    }

    #[test]
    fn repository_selects_tracked_and_unignored_files() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        fixture_git(root, &["init", "-q"]);
        files(
            root,
            &[
                (".gitignore", "target/\n*.pyc\n"),
                ("a.txt", "a"),
                ("target/kept", "k"),
                ("target/ignored", "i"),
                ("src/b.py", "b"),
                ("src/b.pyc", "c"),
                ("sub/.GIT/hooks/h", "x"),
                (".guildbotics/private", "x"),
            ],
        );
        fixture_git(
            root,
            &["add", "-f", "--", ".gitignore", "a.txt", "target/kept"],
        );
        let manifest = list(root, &[]).unwrap();
        assert!(manifest.repository);
        assert_eq!(
            manifest.files,
            [".gitignore", "a.txt", "src/b.py", "target/kept"]
        );
    }

    #[test]
    fn a_gitfile_does_not_enable_ignore_filtering() {
        let root = tempfile::tempdir().unwrap();
        files(
            root.path(),
            &[
                (".git", "gitdir: /elsewhere\n"),
                (".gitignore", "build/\n"),
                ("build/a", "a"),
            ],
        );
        let manifest = list(root.path(), &[]).unwrap();
        assert!(!manifest.repository);
        assert_eq!(manifest.files, [".gitignore", "build/a"]);
    }

    #[test]
    fn tracked_files_preserve_the_on_disk_spelling() {
        for (indexed, on_disk) in [
            ("README.md", "Readme.md"),
            ("Src/b.py", "src/b.py"),
            ("が.txt", "か\u{3099}.txt"),
            ("ぶ/c.txt", "ふ\u{3099}/c.txt"),
        ] {
            let root = tempfile::tempdir().unwrap();
            let root = root.path();
            fixture_git(root, &["init", "-q"]);
            fixture_git(root, &["config", "core.ignorecase", "true"]);
            fixture_git(root, &["config", "core.precomposeunicode", "false"]);
            let directory = Path::new(on_disk).parent().unwrap().to_str().unwrap();
            let ignores = if directory.is_empty() {
                "*.md\n*.txt\n".to_owned()
            } else {
                format!("{directory}/\n*.txt\n")
            };
            files(
                root,
                &[
                    (".gitignore", &ignores),
                    (on_disk, "kept"),
                    ("ignored.txt", "out"),
                ],
            );
            let oid = fixture_git(root, &["hash-object", "-w", "--", on_disk]);
            fixture_git(
                root,
                &[
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    &format!("100644,{},{}", oid.trim(), indexed),
                ],
            );
            assert_eq!(list(root, &[]).unwrap().files, [".gitignore", on_disk]);
        }
    }

    #[test]
    fn host_selection_ignores_user_system_and_inherited_configuration() {
        // Set the environment on an isolated test process so unrelated tests
        // can run concurrently with their own Git configuration.
        const TEST: &str =
            "worktree::tests::host_selection_ignores_user_system_and_inherited_configuration";
        const CHILD: &str = "GB_TEST_WORKTREE_CONFIG_ROOT";
        if let Some(root) = std::env::var_os(CHILD) {
            assert_eq!(
                list(Path::new(&root), &[]).unwrap().files,
                [
                    ".gitignore",
                    "injected",
                    "system-ignored",
                    "tracked",
                    "user-ignored"
                ]
            );
            return;
        }
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("repo");
        files(
            &root,
            &[
                (".gitignore", "repo-ignored\n"),
                ("repo-ignored", "r"),
                ("tracked", "t"),
                ("user-ignored", "u"),
                ("system-ignored", "s"),
                ("injected", "i"),
            ],
        );
        fixture_git(&root, &["init", "-q"]);
        fixture_git(&root, &["add", "--", "tracked"]);
        let user = temp.path().join("user.gitconfig");
        let system = temp.path().join("system.gitconfig");
        let user_excludes = temp.path().join("user-excludes");
        let system_excludes = temp.path().join("system-excludes");
        let injected = temp.path().join("injected-excludes");
        std::fs::write(&user_excludes, "user-ignored\n").unwrap();
        std::fs::write(&system_excludes, "system-ignored\n").unwrap();
        std::fs::write(&injected, "injected\n").unwrap();
        std::fs::write(
            &user,
            format!("[core]\nexcludesFile = {}\n", user_excludes.display()),
        )
        .unwrap();
        std::fs::write(
            &system,
            format!("[core]\nexcludesFile = {}\n", system_excludes.display()),
        )
        .unwrap();
        files(temp.path(), &[("xdg/git/ignore", "user-ignored\n")]);

        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", TEST])
            .env(CHILD, &root)
            .env("HOME", temp.path())
            .env("XDG_CONFIG_HOME", temp.path().join("xdg"))
            .env("GIT_CONFIG_GLOBAL", &user)
            .env("GIT_CONFIG_SYSTEM", &system)
            .env("GIT_CONFIG_COUNT", "1")
            .env("GIT_CONFIG_KEY_0", "core.excludesFile")
            .env("GIT_CONFIG_VALUE_0", &injected)
            .env("GIT_DIR", temp.path().join("elsewhere"))
            .env("GIT_WORK_TREE", temp.path().join("elsewhere"))
            .env("GIT_INDEX_FILE", temp.path().join("elsewhere"))
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(
            output.status.success() && stdout.contains("1 passed"),
            "{stdout}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn selection_does_not_start_the_repository_fsmonitor() {
        use std::os::unix::fs::PermissionsExt;

        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("repo");
        files(&root, &[("tracked", "t")]);
        fixture_git(&root, &["init", "-q"]);
        fixture_git(&root, &["add", "--", "tracked"]);
        let probe = temp.path().join("fsmonitor");
        std::fs::write(
            &probe,
            "#!/bin/sh\ntouch fsmonitor-ran\nprintf 'token\\0'\n",
        )
        .unwrap();
        std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o755)).unwrap();
        fixture_git(
            &root,
            &["config", "core.fsmonitor", probe.to_str().unwrap()],
        );
        fixture_git(
            &root,
            &[
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
            ],
        );
        std::fs::remove_file(root.join("fsmonitor-ran")).unwrap();

        assert_eq!(list(&root, &[]).unwrap().files, ["tracked"]);
        assert!(!root.join("fsmonitor-ran").exists());
    }

    #[cfg(unix)]
    #[test]
    fn only_selected_non_utf8_names_are_errors() {
        use std::os::unix::{ffi::OsStrExt, fs::symlink};

        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        fixture_git(root, &["init", "-q"]);
        files(root, &[(".gitignore", "*.tmp\n"), ("tracked", "t")]);
        fixture_git(root, &["add", "--", "tracked"]);
        let raw = |name: &[u8]| root.join(std::ffi::OsStr::from_bytes(name));
        std::fs::write(raw(b"file\xff.tmp"), "i").unwrap();
        std::fs::create_dir(raw(b"dir\xff.tmp")).unwrap();
        std::fs::write(raw(b"dir\xff.tmp/child"), "i").unwrap();
        symlink("missing", raw(b"link\xff")).unwrap();
        std::fs::create_dir(root.join(".guildbotics")).unwrap();
        std::fs::write(raw(b".guildbotics/file\xff"), "i").unwrap();

        assert_eq!(list(root, &[]).unwrap().files, [".gitignore", "tracked"]);
        std::fs::write(raw(b"selected\xff"), "s").unwrap();
        assert!(
            list(root, &[])
                .unwrap_err()
                .to_string()
                .contains("Non-UTF-8")
        );
    }
}
