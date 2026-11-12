//! A bounded data copy; no client path is opened by the receiving host.
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Seek, Write},
    path::Path,
    process::{Command, Stdio},
    sync::{Arc, Mutex},
};

pub const LIMIT: usize = 16 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Upload {
    pub directories: Vec<String>,
    pub files: Vec<File>,
    pub nested: Vec<String>,
    pub git: Option<Git>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct File {
    pub path: String,
    pub content: String,
    pub executable: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Git {
    pub bundle: String,
    #[serde(default)]
    pub bases: Vec<String>,
    pub object_format: String,
    pub head: String,
    pub refs: BTreeMap<String, String>,
    pub index: Option<String>,
    pub exclude: Option<String>,
    pub remotes: BTreeMap<String, String>,
    pub objects: BTreeMap<String, String>,
    pub shared_indexes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Default)]
pub struct Returned(pub Arc<Mutex<Option<Vec<u8>>>>);
impl PartialEq for Returned {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }
}

fn git(root: &Path, args: &[&str], input: &[u8]) -> Result<Vec<u8>, String> {
    let empty = tempfile::NamedTempFile::new().map_err(|e| e.to_string())?;
    let mut cmd = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            cmd.env_remove(key);
        }
    }
    let mut data = tempfile::tempfile().map_err(|e| e.to_string())?;
    data.write_all(input).map_err(|e| e.to_string())?;
    data.rewind().map_err(|e| e.to_string())?;
    let child = cmd
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", empty.path())
        .env("GIT_TERMINAL_PROMPT", "0")
        .args([
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=",
            "-c",
            "credential.helper=",
        ])
        .args(args)
        .current_dir(root)
        .stdin(Stdio::from(data))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err("Worktree Git export failed".into());
    }
    Ok(output.stdout)
}
pub fn remotes(root: &Path) -> Result<BTreeMap<String, String>, String> {
    if !root.join(".git").exists() {
        return Ok(BTreeMap::new());
    }
    let names = git(root, &["remote"], &[])?;
    let mut remotes = BTreeMap::new();
    for name in String::from_utf8_lossy(&names).lines() {
        let raw = git(root, &["remote", "get-url", name], &[])?;
        let raw = String::from_utf8_lossy(&raw);
        let raw = raw.trim();
        let clean = if let Ok(mut url) = url::Url::parse(raw) {
            let _ = url.set_username("");
            let _ = url.set_password(None);
            url.to_string()
        } else if !Path::new(raw).is_absolute()
            && raw
                .split_once(':')
                .is_some_and(|(host, _)| host.contains('@'))
        {
            raw.split_once('@').unwrap().1.to_owned()
        } else {
            raw.to_owned()
        };
        remotes.insert(name.to_owned(), clean);
    }
    Ok(remotes)
}
fn optional(root: &Path, name: &str) -> Result<Option<String>, CaptureError> {
    let path = root.join(name);
    match std::fs::symlink_metadata(&path) {
        Ok(m) if m.len() > LIMIT as u64 => {
            return Err(CaptureError::Limit {
                files: 0,
                history: usize::try_from(m.len()).unwrap_or(usize::MAX),
            });
        }
        Ok(m) if !m.is_file() => {
            return Err("Unsafe Git metadata file".into());
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.to_string().into()),
        _ => {}
    }
    use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt};
    use std::io::Read;
    let directory = gb_base::host_path::open_directory(path.parent().unwrap(), false)
        .map_err(|e| e.to_string())?;
    let mut options = cap_std::fs::OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    let file = directory
        .open_with(path.file_name().unwrap(), &options)
        .map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    file.take((LIMIT + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > LIMIT {
        return Err(CaptureError::Limit {
            files: 0,
            history: bytes.len(),
        });
    }
    Ok(Some(STANDARD.encode(bytes)))
}
#[derive(Debug)]
pub enum CaptureError {
    Limit { files: usize, history: usize },
    Other(String),
}
impl From<String> for CaptureError {
    fn from(value: String) -> Self {
        Self::Other(value)
    }
}
impl From<&str> for CaptureError {
    fn from(value: &str) -> Self {
        Self::Other(value.into())
    }
}
impl CaptureError {
    pub fn sentence(self) -> crate::Sentence {
        match self {
            Self::Limit { files, history } => crate::Sentence::Key(
                "cli.run.worktree_limit",
                vec![
                    ("files", files.to_string()),
                    ("history", history.to_string()),
                    ("limit", LIMIT.to_string()),
                ],
            ),
            Self::Other(reason) => {
                crate::Sentence::Key("cli.run.worktree_refused", vec![("reason", reason)])
            }
        }
    }
}
impl Upload {
    pub fn capture(copy: &super::Copy) -> Result<Self, CaptureError> {
        Self::capture_incremental(copy, &[])
    }
    pub fn capture_incremental(
        copy: &super::Copy,
        accepted_remotes: &[String],
    ) -> Result<Self, CaptureError> {
        copy.validate()?;
        let manifest =
            gb_base::worktree::list(&copy.host, &copy.excluded).map_err(|e| e.to_string())?;
        let root =
            gb_base::host_path::open_directory(&copy.host, false).map_err(|e| e.to_string())?;
        let mut total = 0;
        let mut files = Vec::new();
        use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
        for path in manifest.files {
            let mut options = cap_std::fs::OpenOptions::new();
            options.read(true).follow(FollowSymlinks::No);
            let mut directory = root.try_clone().map_err(|e| e.to_string())?;
            let relative = Path::new(&path);
            for part in relative.parent().unwrap().components() {
                directory = directory
                    .open_dir_nofollow(part.as_os_str())
                    .map_err(|e| e.to_string())?;
            }
            let mut file = directory
                .open_with(relative.file_name().unwrap(), &options)
                .map_err(|e| e.to_string())?;
            let metadata = file.metadata().map_err(|e| e.to_string())?;
            if !metadata.is_file() {
                return Err("Worktree file changed type".into());
            }
            total += usize::try_from(metadata.len()).map_err(|e| e.to_string())?;
            if total > LIMIT {
                return Err(CaptureError::Limit {
                    files: total,
                    history: 0,
                });
            }
            let mut bytes = Vec::new();
            use std::io::Read;
            std::io::Read::by_ref(&mut file)
                .take((LIMIT + 1) as u64)
                .read_to_end(&mut bytes)
                .map_err(|e| e.to_string())?;
            if bytes.len() > LIMIT {
                return Err(CaptureError::Limit {
                    files: total,
                    history: 0,
                });
            }
            #[cfg(unix)]
            let executable = {
                use cap_std::fs::PermissionsExt;
                metadata.permissions().mode() & 0o111 != 0
            };
            #[cfg(not(unix))]
            let executable = false;
            files.push(File {
                path,
                content: STANDARD.encode(bytes),
                executable,
            });
        }
        let nested = manifest
            .directories
            .iter()
            .filter(|p| copy.host.join(p).join(".git").symlink_metadata().is_ok())
            .cloned()
            .collect();
        let repository = if manifest.repository {
            let text = String::from_utf8(git(
                &copy.host,
                &[
                    "for-each-ref",
                    "--format=%(refname) %(objectname)",
                    "refs/heads",
                    "refs/remotes",
                    "refs/tags",
                ],
                &[],
            )?)
            .map_err(|e| e.to_string())?;
            let refs: BTreeMap<String, String> = text
                .lines()
                .filter_map(|l| l.split_once(' '))
                .map(|(a, b)| (a.to_owned(), b.to_owned()))
                .collect();
            let head_oid = git(&copy.host, &["rev-parse", "--verify", "HEAD"], &[])
                .ok()
                .map(|b| String::from_utf8_lossy(&b).trim().to_owned());
            let symbolic = git(&copy.host, &["symbolic-ref", "-q", "HEAD"], &[]).ok();
            let head = symbolic
                .map(|b| String::from_utf8_lossy(&b).trim().to_owned())
                .unwrap_or_else(|| head_oid.clone().unwrap_or_default());
            let tmp = tempfile::tempdir().map_err(|e| e.to_string())?;
            let bundle_path = tmp.path().join("history.bundle");
            let mut revisions = refs.keys().cloned().collect::<Vec<_>>();
            if let Some(oid) = &head_oid {
                revisions.push("HEAD".into());
                revisions.push(oid.clone());
            }
            revisions.extend(refs.values().cloned());
            let bases: Vec<String> = refs
                .keys()
                .filter(|name| {
                    accepted_remotes
                        .iter()
                        .any(|remote| name.starts_with(&format!("refs/remotes/{remote}/")))
                })
                .cloned()
                .collect();
            revisions.extend(bases.iter().map(|name| format!("^{name}")));
            let pending = !revisions.is_empty()
                && (bases.is_empty()
                    || !git(
                        &copy.host,
                        &["rev-list", "--objects", "--max-count=1", "--stdin"],
                        (revisions.join("\n") + "\n").as_bytes(),
                    )?
                    .is_empty());
            let bundle = if !pending {
                Vec::new()
            } else {
                git(
                    &copy.host,
                    &[
                        "bundle",
                        "create",
                        bundle_path.to_str().ok_or("Invalid bundle path")?,
                        "--stdin",
                    ],
                    (revisions.join("\n") + "\n").as_bytes(),
                )?;
                let bundle_size = usize::try_from(
                    std::fs::metadata(&bundle_path)
                        .map_err(|e| e.to_string())?
                        .len(),
                )
                .map_err(|e| e.to_string())?;
                if bundle_size > LIMIT - total {
                    return Err(CaptureError::Limit {
                        files: total,
                        history: bundle_size,
                    });
                }
                git(
                    &copy.host,
                    &["bundle", "verify", bundle_path.to_str().unwrap()],
                    &[],
                )?;
                std::fs::read(bundle_path).map_err(|e| e.to_string())?
            };
            let remotes = remotes(&copy.host)?;
            let present: BTreeSet<String> = if let Some(oid) = &head_oid {
                git(
                    &copy.host,
                    &["ls-tree", "-r", "-z", "--format=%(objectname)", oid],
                    &[],
                )?
                .split(|b| *b == 0)
                .filter(|b| !b.is_empty())
                .map(|b| String::from_utf8_lossy(b).into_owned())
                .collect()
            } else {
                BTreeSet::new()
            };
            let staged = git(&copy.host, &["ls-files", "--stage", "-z"], &[])?;
            let mut needed = BTreeSet::new();
            for entry in staged.split(|b| *b == 0).filter(|b| !b.is_empty()) {
                let header = String::from_utf8_lossy(
                    entry
                        .split(|b| *b == b'\t')
                        .next()
                        .ok_or("Invalid index record")?,
                );
                let mut fields = header.split(' ');
                if fields.next() == Some("160000") {
                    continue;
                }
                let object = fields.next().ok_or("Invalid index object")?;
                if !present.contains(object) {
                    needed.insert(object.to_owned());
                }
            }
            let mut objects = BTreeMap::new();
            if !needed.is_empty() {
                let input =
                    (needed.iter().cloned().collect::<Vec<_>>().join("\n") + "\n").into_bytes();
                let sizes = git(
                    &copy.host,
                    &["cat-file", "--batch-check=%(objectsize)"],
                    &input,
                )?;
                let extra =
                    String::from_utf8_lossy(&sizes)
                        .lines()
                        .try_fold(0usize, |sum, line| {
                            let size = line.parse::<usize>().map_err(|e| e.to_string())?;
                            sum.checked_add(size)
                                .ok_or_else(|| "Worktree size overflow".to_owned())
                        })?;
                if total + bundle.len() + extra > LIMIT {
                    return Err(CaptureError::Limit {
                        files: total,
                        history: bundle.len() + extra,
                    });
                }
                let bytes = git(&copy.host, &["cat-file", "--batch"], &input)?;
                let mut rest = bytes.as_slice();
                for object in needed {
                    let end = rest
                        .iter()
                        .position(|b| *b == b'\n')
                        .ok_or("Invalid batch header")?;
                    let header = std::str::from_utf8(&rest[..end]).map_err(|e| e.to_string())?;
                    let fields = header.split(' ').collect::<Vec<_>>();
                    if fields.len() != 3 || fields[0] != object || fields[1] != "blob" {
                        return Err("Invalid batch object".into());
                    }
                    let size = fields[2].parse::<usize>().map_err(|e| e.to_string())?;
                    rest = &rest[end + 1..];
                    if rest.len() <= size || rest[size] != b'\n' {
                        return Err("Invalid batch body".into());
                    }
                    objects.insert(object, STANDARD.encode(&rest[..size]));
                    rest = &rest[size + 1..];
                }
                if !rest.is_empty() {
                    return Err("Unexpected batch data".into());
                }
            }
            let mut shared_indexes = BTreeMap::new();
            let shared = git(&copy.host, &["rev-parse", "--shared-index-path"], &[])?;
            let shared = String::from_utf8_lossy(&shared);
            if !shared.trim().is_empty() {
                let name = Path::new(shared.trim())
                    .file_name()
                    .and_then(|n| n.to_str())
                    .ok_or("Invalid shared index")?;
                if !name.starts_with("sharedindex.") {
                    return Err("Invalid shared index".into());
                }
                shared_indexes.insert(
                    name.to_owned(),
                    optional(&copy.host, &format!(".git/{name}"))?.ok_or("Missing shared index")?,
                );
            }
            Some(Git {
                object_format: String::from_utf8_lossy(&git(
                    &copy.host,
                    &["rev-parse", "--show-object-format"],
                    &[],
                )?)
                .trim()
                .to_owned(),
                shared_indexes,
                objects,
                bundle: STANDARD.encode(bundle),
                bases,
                head,
                refs,
                index: optional(&copy.host, ".git/index")?,
                exclude: optional(&copy.host, ".git/info/exclude")?,
                remotes,
            })
        } else {
            None
        };
        let upload = Self {
            directories: manifest.directories,
            files,
            nested,
            git: repository,
        };
        let file_size = upload
            .files
            .iter()
            .map(|f| {
                STANDARD
                    .decode(&f.content)
                    .map(|b| b.len())
                    .map_err(|e| e.to_string())
            })
            .collect::<Result<Vec<_>, _>>()?
            .iter()
            .sum::<usize>();
        let history_size = match &upload.git {
            None => 0,
            Some(g) => [Some(&g.bundle), g.index.as_ref(), g.exclude.as_ref()]
                .into_iter()
                .flatten()
                .chain(g.objects.values())
                .chain(g.shared_indexes.values())
                .map(|b| {
                    STANDARD
                        .decode(b)
                        .map(|b| b.len())
                        .map_err(|e| e.to_string())
                })
                .collect::<Result<Vec<_>, _>>()?
                .iter()
                .sum::<usize>(),
        };
        if file_size + history_size > LIMIT {
            return Err(CaptureError::Limit {
                files: file_size,
                history: history_size,
            });
        }
        upload.validate()?;
        Ok(upload)
    }
    pub fn validate(&self) -> Result<(), String> {
        let mut size = 0usize;
        let mut seen = std::collections::HashSet::new();
        for path in self
            .directories
            .iter()
            .chain(self.files.iter().map(|f| &f.path))
        {
            super::name(path, &[".guildbotics".into()], true)?;
            if !seen.insert(path) {
                return Err("Repeated worktree path".into());
            }
        }
        for file in &self.files {
            size = size
                .checked_add(
                    STANDARD
                        .decode(&file.content)
                        .map_err(|e| e.to_string())?
                        .len(),
                )
                .ok_or("Worktree size overflow")?;
        }
        if let Some(git) = &self.git {
            for data in [Some(&git.bundle), git.index.as_ref(), git.exclude.as_ref()]
                .into_iter()
                .flatten()
                .chain(git.objects.values())
                .chain(git.shared_indexes.values())
            {
                size = size
                    .checked_add(STANDARD.decode(data).map_err(|e| e.to_string())?.len())
                    .ok_or("Worktree size overflow")?;
            }
        }
        if size > LIMIT {
            return Err("Worktree upload exceeds 16 MiB".into());
        }
        Ok(())
    }
    pub fn copied(&self) -> Vec<u8> {
        use sha2::{Digest, Sha256};
        self.files
            .iter()
            .flat_map(|f| {
                let bytes = STANDARD.decode(&f.content).expect("validated upload");
                let hash = Sha256::digest(bytes)
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>();
                (serde_json::json!({"path":f.path,"sha256":hash,"executable":f.executable})
                    .to_string()
                    + "\n")
                    .into_bytes()
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn upload() -> Upload {
        Upload {
            directories: vec![],
            files: vec![],
            nested: vec![],
            git: None,
        }
    }
    #[test]
    fn incremental_capture_excludes_tracking_history_and_handles_no_new_commits() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path();
        git(path, &["init", "-q", "-b", "main"], &[]).unwrap();
        std::fs::write(path.join("file"), "base").unwrap();
        git(path, &["add", "."], &[]).unwrap();
        let commit = |message| {
            git(
                path,
                &[
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    message,
                ],
                &[],
            )
            .unwrap();
        };
        commit("base");
        let base = String::from_utf8(git(path, &["rev-parse", "HEAD"], &[]).unwrap()).unwrap();
        git(
            path,
            &["update-ref", "refs/remotes/origin/main", base.trim()],
            &[],
        )
        .unwrap();
        git(
            path,
            &[
                "remote",
                "add",
                "origin",
                "https://example.invalid/repo.git",
            ],
            &[],
        )
        .unwrap();
        let copy =
            super::super::Copy::new(path.into(), "/work".into(), vec![], path.join("write.lock"))
                .unwrap();
        let capture = || Upload::capture_incremental(&copy, &["origin".into()]).unwrap();
        let empty = capture();
        assert!(empty.git.as_ref().unwrap().bundle.is_empty());
        assert_eq!(
            empty.git.as_ref().unwrap().bases,
            ["refs/remotes/origin/main"]
        );
        git(
            path,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "tag",
                "-a",
                "tag-only",
                "-m",
                "tag",
            ],
            &[],
        )
        .unwrap();
        assert!(!capture().git.as_ref().unwrap().bundle.is_empty());
        std::fs::write(path.join("file"), "local").unwrap();
        git(path, &["add", "."], &[]).unwrap();
        commit("local");
        let full = Upload::capture(&copy).unwrap();
        let delta = capture();
        assert_eq!(delta.files, full.files);
        assert_eq!(
            delta.git.as_ref().unwrap().refs,
            full.git.as_ref().unwrap().refs
        );
        let bundle = root.path().join("delta.bundle");
        std::fs::write(
            &bundle,
            STANDARD
                .decode(&delta.git.as_ref().unwrap().bundle)
                .unwrap(),
        )
        .unwrap();
        let bytes = std::fs::read(bundle).unwrap();
        assert!(String::from_utf8_lossy(&bytes).contains(&format!("-{} ", base.trim())));
        assert!(full.git.as_ref().unwrap().bases.is_empty());
    }
    #[test]
    fn received_plan_never_opens_client_directory_and_rejects_protected_population() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("home");
        std::fs::create_dir(&home).unwrap();
        let workspace = root.path().join("workspace");
        let other = root.path().join("other");
        let local = workspace.join(".guildbotics/local");
        std::fs::create_dir_all(&local).unwrap();
        std::fs::write(
            local.join("cli_agent_filesystem_grants.yml"),
            "deny: [private]",
        )
        .unwrap();
        let access = crate::contract::resolve(
            &workspace,
            &home,
            std::slice::from_ref(&other),
            false,
            false,
        )
        .unwrap();
        let plan = |cwd: &Path, read_only, payload| {
            crate::contract::received_plan(
                &workspace,
                "aiko",
                cwd,
                &access,
                vec![],
                read_only,
                payload,
                Default::default(),
            )
        };
        let absent = root.path().join("client-only/work");
        let result = plan(&absent, false, Some(Arc::new(upload()))).unwrap();
        assert!(!absent.exists());
        assert!(result.copy.unwrap().host.as_os_str().is_empty());
        assert!(result.mounts.iter().all(|m| m.host.is_none()));
        assert!(plan(&absent, true, None).unwrap().copy.is_none());
        assert!(plan(&absent, true, Some(Arc::new(upload()))).is_err());
        for protected in [
            home.join(".ssh"),
            home.join(".aws"),
            home.join("private"),
            workspace.join(".guildbotics"),
            other.join(".guildbotics"),
        ] {
            for cwd in [
                &protected,
                &protected.join("child"),
                protected.parent().unwrap(),
            ] {
                assert!(
                    plan(cwd, false, Some(Arc::new(upload()))).is_err(),
                    "{}",
                    cwd.display()
                );
            }
        }
    }
    #[test]
    fn exported_fingerprints_keep_client_writeback_atomic_on_conflict() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("a"), "before").unwrap();
        std::fs::write(root.path().join("b"), "before").unwrap();
        let copy = super::super::Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![".guildbotics".into()],
            root.path().join("write.lock"),
        )
        .unwrap();
        let sent = Upload::capture(&copy).unwrap();
        std::fs::write(root.path().join("b"), "local edit").unwrap();
        let changes = format!(
            "{}\n{}\n",
            serde_json::json!({"path":"a","content":STANDARD.encode(b"result")}),
            serde_json::json!({"path":"b","deleted":true})
        );
        assert!(super::super::write_back(&copy, &sent.copied(), changes.as_bytes()).is_err());
        assert_eq!(
            std::fs::read_to_string(root.path().join("a")).unwrap(),
            "before"
        );
        assert_eq!(
            std::fs::read_to_string(root.path().join("b")).unwrap(),
            "local edit"
        );
    }
    #[test]
    fn transfer_limits_and_names_are_checked_before_guest_launch() {
        for path in [
            "../escape",
            "/absolute",
            ".git/config",
            ".guildbotics/private",
            "a/../b",
        ] {
            let mut value = upload();
            value.files.push(File {
                path: path.into(),
                content: String::new(),
                executable: false,
            });
            assert!(value.validate().is_err(), "{path}");
        }
        let mut value = upload();
        value.files.push(File {
            path: "file".into(),
            content: STANDARD.encode(vec![0; LIMIT + 1]),
            executable: false,
        });
        assert!(value.validate().is_err());
    }
}
