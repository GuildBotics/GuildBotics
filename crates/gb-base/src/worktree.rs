//! Host-side selection of a command's working-directory copy.
use anyhow::{Context, Result, bail};
use cap_fs_ext::DirExt;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, path::Path, process::Command};
use unicode_normalization::UnicodeNormalization;

/// Paths are relative, spelled as on disk, and contain no links or special files.
#[derive(Debug, Serialize, Deserialize)]
pub struct Manifest {
    pub directories: Vec<String>,
    pub files: Vec<String>,
    pub repository: bool,
}

fn composed(value: &str) -> String {
    value.nfc().collect()
}

fn git(root: &Path, global: &Path, args: &[&str]) -> Result<BTreeSet<String>> {
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
        ])
        // Keep disk spelling even on macOS; the index may use NFC.
        .args(["-c", "core.precomposeunicode=false"])
        .arg(format!("--git-dir={}", root.join(".git").display()))
        .arg(format!("--work-tree={}", root.display()))
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
        .map(|s| Ok(std::str::from_utf8(s)?.trim_end_matches('/').to_owned()))
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
        let tracked: BTreeSet<_> = git(root, global.path(), &["--cached"])?
            .iter()
            .map(|s| composed(s))
            .collect();
        let holding: BTreeSet<_> = tracked
            .iter()
            .flat_map(|s| s.match_indices('/').map(|(index, _)| s[..index].to_owned()))
            .collect();
        let flags = ["--others", "--ignored", "--exclude-standard"];
        for path in git(
            root,
            global.path(),
            &[flags.as_slice(), &["--directory"]].concat(),
        )? {
            if holding.contains(&composed(&path)) {
                ignored.extend(git(
                    root,
                    global.path(),
                    &[flags.as_slice(), &["--", &format!("{path}/")]].concat(),
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
    walk(&dir, "", excluded, &ignored, &mut manifest)?;
    Ok(manifest)
}

fn walk(
    dir: &cap_std::fs::Dir,
    prefix: &str,
    excluded: &[String],
    ignored: &BTreeSet<String>,
    out: &mut Manifest,
) -> Result<()> {
    let mut entries = dir.entries()?.collect::<std::io::Result<Vec<_>>>()?;
    entries.sort_by_key(|e| e.file_name());
    for entry in entries {
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| anyhow::anyhow!("Non-UTF-8 worktree path"))?;
        let path = if prefix.is_empty() {
            name.clone()
        } else {
            format!("{prefix}/{name}")
        };
        if name.eq_ignore_ascii_case(".git")
            || path == ".guildbotics"
            || excluded.contains(&path)
            || ignored.contains(&path)
        {
            continue;
        }
        let kind = entry.file_type()?;
        if kind.is_dir() {
            out.directories.push(path.clone());
            walk(
                &dir.open_dir_nofollow(&name)?,
                &path,
                excluded,
                ignored,
                out,
            )?;
        } else if kind.is_file() {
            out.files.push(path);
        }
    }
    Ok(())
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
}
