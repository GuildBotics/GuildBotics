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
}
