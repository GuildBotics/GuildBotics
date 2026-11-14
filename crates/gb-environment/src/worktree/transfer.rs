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
    pub source: Source,
    pub executable: bool,
}
/// Where the receiver gets a file's bytes: sent, or the index blob it equals,
/// which the received history holds. Only sent bytes count against [`LIMIT`].
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum Source {
    Content(String),
    Blob { oid: String, sha256: String },
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
/// The blob each stage-0 regular file of the index names, by the NFC form of
/// its path: macOS Git composes the names it indexes, not the disk's.
fn index_blobs(staged: &[u8]) -> Result<BTreeMap<String, String>, String> {
    let mut blobs = BTreeMap::new();
    for entry in staged.split(|b| *b == 0).filter(|b| !b.is_empty()) {
        let (header, path) = entry
            .iter()
            .position(|b| *b == b'\t')
            .map(|at| (&entry[..at], &entry[at + 1..]))
            .ok_or("Invalid index record")?;
        let header = String::from_utf8_lossy(header);
        let fields = header.split(' ').collect::<Vec<_>>();
        if let ([mode, oid, "0"], Ok(path)) = (fields.as_slice(), std::str::from_utf8(path))
            && mode.starts_with("100")
        {
            blobs.insert(composed(path), (*oid).to_owned());
        }
    }
    Ok(blobs)
}
fn composed(path: &str) -> String {
    use unicode_normalization::UnicodeNormalization;
    path.nfc().collect()
}
/// Read a file of `len` bytes once. When its bytes are `blob` (in `format`),
/// it is named; otherwise its bytes are sent, if they fit in `room`. Returns
/// the source and the bytes it sends, or `None` when they do not fit.
fn read_source(
    file: &mut impl std::io::Read,
    format: &str,
    len: u64,
    blob: Option<&str>,
    room: usize,
    chunk: &mut [u8],
) -> Result<Option<(Source, usize)>, CaptureError> {
    use sha2::Digest;
    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }
    if blob.is_none() && len > room as u64 {
        return Ok(None);
    }
    let header = format!("blob {len}\0");
    let mut sha1 =
        (blob.is_some() && format == "sha1").then(|| sha1::Sha1::new_with_prefix(&header));
    let mut sha256_oid =
        (blob.is_some() && format == "sha256").then(|| sha2::Sha256::new_with_prefix(&header));
    let mut sha256 = blob.map(|_| sha2::Sha256::new());
    let mut bytes = Some(Vec::new());
    let mut read = 0u64;
    loop {
        let n = match file.read(chunk) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e.to_string().into()),
        };
        let part = &chunk[..n];
        read += n as u64;
        sha1.iter_mut().for_each(|h| h.update(part));
        sha256_oid.iter_mut().for_each(|h| h.update(part));
        sha256.iter_mut().for_each(|h| h.update(part));
        if let Some(kept) = &mut bytes {
            if kept.len() + n > room {
                bytes = None;
            } else {
                kept.extend_from_slice(part);
            }
        }
    }
    if read != len {
        return Err("Worktree file changed while it was read".into());
    }
    let oid = sha1
        .map(|h| hex(&h.finalize()))
        .or_else(|| sha256_oid.map(|h| hex(&h.finalize())));
    if let (Some(oid), Some(sha256)) = (oid, sha256)
        && Some(oid.as_str()) == blob
    {
        return Ok(Some((
            Source::Blob {
                oid,
                sha256: hex(&sha256.finalize()),
            },
            0,
        )));
    }
    Ok(bytes.map(|bytes| {
        let sent = bytes.len();
        (Source::Content(STANDARD.encode(bytes)), sent)
    }))
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
        let (object_format, staged) = if manifest.repository {
            (
                String::from_utf8_lossy(&git(
                    &copy.host,
                    &["rev-parse", "--show-object-format"],
                    &[],
                )?)
                .trim()
                .to_owned(),
                git(&copy.host, &["ls-files", "--stage", "-z"], &[])?,
            )
        } else {
            (String::new(), Vec::new())
        };
        let blobs = index_blobs(&staged)?;
        let mut total = 0;
        let mut files = Vec::new();
        let mut chunk = vec![0; 1 << 20];
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
            let read = read_source(
                &mut file,
                &object_format,
                metadata.len(),
                blobs.get(&composed(&path)).map(String::as_str),
                LIMIT - total,
                &mut chunk,
            )?;
            #[cfg(unix)]
            let executable = {
                use cap_std::fs::PermissionsExt;
                metadata.permissions().mode() & 0o111 != 0
            };
            #[cfg(not(unix))]
            let executable = false;
            let Some((source, sent)) = read else {
                return Err(CaptureError::Limit {
                    files: total
                        .saturating_add(usize::try_from(metadata.len()).unwrap_or(usize::MAX)),
                    history: 0,
                });
            };
            total += sent;
            files.push(File {
                path,
                source,
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
                object_format,
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
        let (files, history) = upload.sizes()?;
        if files + history > LIMIT {
            return Err(CaptureError::Limit { files, history });
        }
        upload.validate()?;
        Ok(upload)
    }
    /// The decoded bytes of the sent file contents, and of the sent history
    /// and index data.
    pub fn sizes(&self) -> Result<(usize, usize), String> {
        let decoded = |data: &String| {
            STANDARD
                .decode(data)
                .map(|b| b.len())
                .map_err(|e| e.to_string())
        };
        let files = self
            .files
            .iter()
            .filter_map(|f| match &f.source {
                Source::Content(content) => Some(decoded(content)),
                Source::Blob { .. } => None,
            })
            .sum::<Result<usize, _>>()?;
        let history = match &self.git {
            None => 0,
            Some(g) => [Some(&g.bundle), g.index.as_ref(), g.exclude.as_ref()]
                .into_iter()
                .flatten()
                .chain(g.objects.values())
                .chain(g.shared_indexes.values())
                .map(decoded)
                .sum::<Result<usize, _>>()?,
        };
        Ok((files, history))
    }
    /// The upload's shape: safe, distinct names, decodable data, and blobs
    /// named in the repository's object format.
    pub fn validate(&self) -> Result<(), String> {
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
        let hex = |value: &str, len: usize| {
            value.len() == len
                && value
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        for file in &self.files {
            if let Source::Blob { oid, sha256 } = &file.source {
                let len = match self.git.as_ref().map(|g| g.object_format.as_str()) {
                    Some("sha1") => 40,
                    Some("sha256") => 64,
                    _ => return Err("Worktree blob without a repository".into()),
                };
                if !hex(oid, len) || !hex(sha256, 64) {
                    return Err("Invalid worktree blob".into());
                }
            }
        }
        self.sizes().map(|_| ())
    }
    /// What a client may send: [`Self::validate`] within [`LIMIT`]. History a
    /// server completes from the code host is not the client's to count.
    pub fn validate_sent(&self) -> Result<(), String> {
        self.validate()?;
        let (files, history) = self.sizes()?;
        if files + history > LIMIT {
            return Err("Worktree upload exceeds 16 MiB".into());
        }
        Ok(())
    }
    pub fn copied(&self) -> Vec<u8> {
        use sha2::{Digest, Sha256};
        self.files
            .iter()
            .flat_map(|f| {
                let hash = match &f.source {
                    Source::Content(content) => {
                        Sha256::digest(STANDARD.decode(content).expect("validated upload"))
                            .iter()
                            .map(|b| format!("{b:02x}"))
                            .collect::<String>()
                    }
                    Source::Blob { sha256, .. } => sha256.clone(),
                };
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
                false,
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
                source: Source::Content(String::new()),
                executable: false,
            });
            assert!(value.validate().is_err(), "{path}");
        }
        let mut value = upload();
        value.files.push(File {
            path: "file".into(),
            source: Source::Content(STANDARD.encode(vec![0; LIMIT + 1])),
            executable: false,
        });
        assert!(value.validate().is_ok());
        assert!(value.validate_sent().is_err());
    }
    #[test]
    fn files_equal_to_their_index_blob_are_named_and_only_sent_bytes_count() {
        use sha2::Digest;
        let root = tempfile::tempdir().unwrap();
        let path = root.path();
        let run = |args: &[&str]| {
            git(
                path,
                &[
                    &[
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=f@example.invalid",
                    ],
                    args,
                ]
                .concat(),
                &[],
            )
            .unwrap()
        };
        run(&["init", "-q", "-b", "main"]);
        // Larger than the limit, yet unchanged: only its name crosses.
        std::fs::write(path.join("large"), vec![7u8; LIMIT + 1]).unwrap();
        std::fs::write(path.join("edited"), "committed").unwrap();
        std::fs::write(path.join("staged"), "committed").unwrap();
        run(&["add", "."]);
        run(&["commit", "-qm", "base"]);
        std::fs::write(path.join("edited"), "edited").unwrap();
        std::fs::write(path.join("staged"), "staged").unwrap();
        run(&["add", "staged"]);
        std::fs::write(path.join("new"), "new").unwrap();
        let copy =
            super::super::Copy::new(path.into(), "/work".into(), vec![], path.join("write.lock"))
                .unwrap();
        let upload = Upload::capture(&copy).unwrap();
        let source = |name: &str| &upload.files.iter().find(|f| f.path == name).unwrap().source;
        let index = |name: &str| {
            String::from_utf8(git(path, &["rev-parse", &format!(":{name}")], &[]).unwrap())
                .unwrap()
                .trim()
                .to_owned()
        };
        let sha256 = |bytes: &[u8]| {
            sha2::Sha256::digest(bytes)
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        };
        assert_eq!(
            source("large"),
            &Source::Blob {
                oid: index("large"),
                sha256: sha256(&vec![7u8; LIMIT + 1])
            }
        );
        assert!(matches!(source("staged"), Source::Blob { oid, .. } if *oid == index("staged")));
        assert!(
            upload
                .git
                .as_ref()
                .unwrap()
                .objects
                .contains_key(&index("staged"))
        );
        assert_eq!(
            source("edited"),
            &Source::Content(STANDARD.encode("edited"))
        );
        assert_eq!(source("new"), &Source::Content(STANDARD.encode("new")));
        assert_eq!(upload.sizes().unwrap().0, "edited".len() + "new".len());
        let receipts = String::from_utf8(upload.copied()).unwrap();
        assert!(receipts.contains(&sha256(&vec![7u8; LIMIT + 1])));
        assert!(receipts.contains(&sha256(b"edited")));
        // A tracked file edited beyond the limit is sent, and refused.
        std::fs::write(path.join("large"), vec![8u8; LIMIT + 1]).unwrap();
        assert!(matches!(
            Upload::capture(&copy),
            Err(CaptureError::Limit { .. })
        ));
    }
    #[test]
    fn index_names_match_by_composed_form_conflicts_send_bytes_and_sha256_repositories_name_blobs()
    {
        let fixture = |format: &str| {
            let root = tempfile::tempdir().unwrap();
            git(
                root.path(),
                &["init", "-q", &format!("--object-format={format}")],
                &[],
            )
            .unwrap();
            root
        };
        let capture = |path: &Path| {
            let copy =
                super::super::Copy::new(path.into(), "/work".into(), vec![], path.join("lock"))
                    .unwrap();
            Upload::capture(&copy).unwrap()
        };
        let blob = |path: &Path, bytes: &[u8]| {
            String::from_utf8(git(path, &["hash-object", "-w", "--stdin"], bytes).unwrap())
                .unwrap()
                .trim()
                .to_owned()
        };
        // macOS Git indexes the composed name of a file the disk spells decomposed.
        let root = fixture("sha1");
        let decomposed = "\u{304b}\u{3099}.txt";
        std::fs::write(root.path().join(decomposed), "kana").unwrap();
        let oid = blob(root.path(), b"kana");
        let index = |entries: &str| {
            git(
                root.path(),
                &["update-index", "--index-info"],
                entries.as_bytes(),
            )
            .unwrap();
        };
        index(&format!("100644 {oid} 0\t\u{304c}.txt\n"));
        let upload = capture(root.path());
        assert!(matches!(&upload.files[0].source, Source::Blob { oid: o, .. } if *o == oid));
        // A conflicted entry names no single blob: its bytes are sent.
        let composed = "\u{304c}.txt";
        let zero = "0".repeat(40);
        index(&format!(
            "0 {zero}\t{composed}\n100644 {oid} 2\t{composed}\n100644 {oid} 3\t{composed}\n"
        ));
        let upload = capture(root.path());
        assert_eq!(
            upload.files[0].source,
            Source::Content(STANDARD.encode("kana"))
        );
        let root = fixture("sha256");
        std::fs::write(root.path().join("file"), "content").unwrap();
        git(root.path(), &["add", "file"], &[]).unwrap();
        let upload = capture(root.path());
        assert!(matches!(&upload.files[0].source,
            Source::Blob { oid, .. } if oid.len() == 64 && *oid == blob(root.path(), b"content")));
        upload.validate().unwrap();
    }
    #[test]
    fn untracked_bytes_beyond_the_limit_are_refused() {
        let root = tempfile::tempdir().unwrap();
        git(root.path(), &["init", "-q"], &[]).unwrap();
        std::fs::write(root.path().join("small"), "small").unwrap();
        std::fs::write(root.path().join("large"), vec![0u8; LIMIT]).unwrap();
        let copy = super::super::Copy::new(
            root.path().into(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        assert!(matches!(
            Upload::capture(&copy),
            Err(CaptureError::Limit { files, history: 0 }) if files == LIMIT + "small".len()
        ));
    }
    #[test]
    fn blobs_need_a_repository_and_completed_history_is_not_the_senders() {
        let mut value = upload();
        value.files.push(File {
            path: "file".into(),
            source: Source::Blob {
                oid: "a".repeat(40),
                sha256: "b".repeat(64),
            },
            executable: false,
        });
        assert!(value.validate().is_err());
        let git = |format: &str, bundle: String| Git {
            bundle,
            bases: vec![],
            object_format: format.into(),
            head: "refs/heads/main".into(),
            refs: BTreeMap::new(),
            index: None,
            exclude: None,
            remotes: BTreeMap::new(),
            objects: BTreeMap::new(),
            shared_indexes: BTreeMap::new(),
        };
        value.git = Some(git("sha256", String::new()));
        assert!(value.validate().is_err());
        value.git = Some(git("sha1", STANDARD.encode(vec![0; LIMIT + 1])));
        assert!(value.validate().is_ok());
        assert!(value.validate_sent().is_err());
    }
}
