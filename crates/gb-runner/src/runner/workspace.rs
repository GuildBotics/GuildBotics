//! The per-job directories on the runner's machine: checking out the
//! source, collecting artifacts, and the dependency cache. What comes back
//! from a VM was written by the job, so nothing here follows a symbolic link:
//! links are skipped (artifacts), and the cache is an archive the host never
//! unpacks.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::RwLock;
use std::time::SystemTime;

use crate::ci::pipeline::{ArtifactDef, CacheDef, JobDef, check_relative};

use super::zip::ZipWriter;

/// Settings the VM's Git needs whatever the runner's machine is: a clone
/// records what Git found there (no exec bits or links on Windows, a
/// case-insensitive or normalizing file system on macOS), while the VM's
/// `/work` has all of them. Inside the VM the files also have another owner,
/// other inodes, and other change times than the index records: comparing
/// only modification times, sizes, and modes keeps the index clean there
/// (`git diff-index --quiet HEAD`).
const VM_GIT_CONFIG: &str = "[core]\n\tfilemode = true\n\tsymlinks = true\n\tignorecase = false\n\tprecomposeunicode = false\n\tcheckStat = minimal\n\ttrustCtime = false\n";

/// Checks the commit out of its Git bundle (`GET …/source`) into the empty
/// directory `work`, a clone detached at the bundle's `HEAD` with its history
/// and tags and without the remote that names the bundle, and packs it into
/// the archive `into` for the VM ([`pack_checkout`]). Git runs with
/// `global_config` (written empty) and writes no reflog, so nothing of this
/// machine (a path, its user) reaches the job. Git never runs in the work
/// directory once the job has had it. Returns the archive's size and what
/// the commit tracks.
pub async fn checkout(
    bundle: &Path,
    work: &Path,
    global_config: &Path,
    into: &Path,
) -> io::Result<(u64, Tree)> {
    fs::write(global_config, "")?;
    let mut clone = crate::git::base_command(global_config);
    clone
        .args([
            "-c",
            "core.logAllRefUpdates=false",
            "clone",
            "--quiet",
            "--",
        ])
        .arg(bundle)
        .arg(work);
    let mut unlink = crate::git::base_command(global_config);
    unlink
        .arg("-C")
        .arg(work)
        .args(["remote", "remove", "origin"]);
    let mut list = crate::git::base_command(global_config);
    list.arg("-C").arg(work).args(["ls-files", "-s", "-z"]);
    let mut index = Vec::new();
    for mut git in [clone, unlink, list] {
        let out = git
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .output()
            .await
            .map_err(|e| io::Error::other(format!("cannot run git: {e}")))?;
        if !out.status.success() {
            return Err(io::Error::other(format!(
                "git: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            )));
        }
        index = out.stdout;
    }
    // Later keys win: the settings the clone recorded are overridden.
    fs::OpenOptions::new()
        .append(true)
        .open(work.join(".git/config"))
        .and_then(|mut f| io::Write::write_all(&mut f, VM_GIT_CONFIG.as_bytes()))?;
    let tree = Tree::from_index(&index)?;
    let (work, into) = (work.to_path_buf(), into.to_path_buf());
    let size = tokio::task::spawn_blocking(move || pack_checkout(&work, &index, &into))
        .await
        .map_err(io::Error::other)??;
    Ok((size, tree))
}

/// Writes the names of the members of the archive `tar` to `into`, one per
/// line, as the bytes of their headers (`tar -t` escapes some): what
/// unpacking it creates. A name that holds a newline, which a line cannot
/// carry, fails it.
pub fn list_members(tar: &Path, into: &Path) -> io::Result<()> {
    let mut out = io::BufWriter::new(fs::File::create(into)?);
    let mut archive = tar::Archive::new(io::BufReader::new(fs::File::open(tar)?));
    for entry in archive.entries()? {
        let entry = entry?;
        let name = entry.path_bytes();
        if name.contains(&b'\n') {
            return Err(io::Error::other(
                "a name in the checkout holds a newline, which the list of its members cannot carry",
            ));
        }
        io::Write::write_all(&mut out, &name)?;
        io::Write::write_all(&mut out, b"\n")?;
    }
    io::Write::flush(&mut out)
}

/// What a commit tracks, as its index records it (whatever the machine's
/// file system could make of it, and byte for byte): the paths of its
/// symbolic links.
#[derive(Debug, Default)]
pub struct Tree {
    links: std::collections::BTreeSet<String>,
}

impl Tree {
    /// From the output of `git ls-files -s -z`.
    pub fn from_index(index: &[u8]) -> io::Result<Self> {
        let mut tree = Self::default();
        for record in index.split(|b| *b == 0).filter(|r| !r.is_empty()) {
            let record = std::str::from_utf8(record)
                .map_err(|_| io::Error::other("a tracked path is not valid UTF-8"))?;
            let (meta, path) = record
                .split_once('\t')
                .ok_or_else(|| io::Error::other(format!("unexpected index entry {record:?}")))?;
            if meta.starts_with("120000 ") {
                tree.links.insert(path.to_string());
            }
        }
        Ok(tree)
    }

    /// The first of `cached` that goes through a symbolic link of the
    /// commit, the last component included.
    pub fn link_on<'a>(&self, cached: impl IntoIterator<Item = &'a str>) -> Option<&'a str> {
        cached.into_iter().find(|p| {
            let p = p.trim_end_matches('/');
            p.match_indices('/')
                .map(|(at, _)| &p[..at])
                .chain([p])
                .any(|prefix| self.links.contains(prefix))
        })
    }
}

/// Packs the checkout in `work` into one tar archive: each file Git tracks
/// (`index`, the output of `git ls-files -s -z`) with the mode and the kind
/// the index records, rather than what the machine's file system can tell,
/// and `.git` as it is. Returns the archive's size.
fn pack_checkout(work: &Path, index: &[u8], into: &Path) -> io::Result<u64> {
    let mut tar = tar::Builder::new(io::BufWriter::new(fs::File::create(into)?));
    tar.follow_symlinks(false);
    for record in index.split(|b| *b == 0).filter(|r| !r.is_empty()) {
        let record = std::str::from_utf8(record)
            .map_err(|_| io::Error::other("a tracked path is not valid UTF-8"))?;
        let (meta, path) = record
            .split_once('\t')
            .ok_or_else(|| io::Error::other(format!("unexpected index entry {record:?}")))?;
        let file = work.join(path);
        let stat = fs::symlink_metadata(&file)?;
        let mut header = tar::Header::new_gnu();
        header.set_mtime(
            stat.modified()?
                .duration_since(SystemTime::UNIX_EPOCH)
                .map_or(0, |d| d.as_secs()),
        );
        match meta.split(' ').next() {
            // A link the machine could not make is a file holding its target.
            // The target goes as it is (the blob's bytes): no normalizing.
            Some("120000") => {
                let target = if stat.file_type().is_symlink() {
                    fs::read_link(&file)?.into_os_string().into_encoded_bytes()
                } else {
                    fs::read(&file)?
                };
                let short = header.as_old().linkname.len();
                if target.len() >= short {
                    let mut long = tar::Header::new_gnu();
                    long.as_old_mut().name[..13].copy_from_slice(b"././@LongLink");
                    long.set_entry_type(tar::EntryType::GNULongLink);
                    long.set_mode(0o644);
                    long.set_size(target.len() as u64 + 1);
                    long.set_cksum();
                    tar.append(&long, [&target[..], b"\0"].concat().as_slice())?;
                }
                header.set_link_name_literal(&target[..target.len().min(short - 1)])?;
                header.set_entry_type(tar::EntryType::Symlink);
                header.set_mode(0o777);
                header.set_size(0);
                tar.append_data(&mut header, path, io::empty())?;
            }
            // A submodule's commit: an empty directory, as a clone leaves it.
            Some("160000") => {
                header.set_entry_type(tar::EntryType::Directory);
                header.set_mode(0o755);
                header.set_size(0);
                tar.append_data(&mut header, path, io::empty())?;
            }
            Some(mode) => {
                header.set_entry_type(tar::EntryType::Regular);
                header.set_mode(if mode == "100755" { 0o755 } else { 0o644 });
                header.set_size(stat.len());
                tar.append_data(&mut header, path, fs::File::open(&file)?)?;
            }
            None => {
                return Err(io::Error::other(format!(
                    "unexpected index entry {record:?}"
                )));
            }
        }
    }
    tar.append_dir_all(".git", work.join(".git"))?;
    tar.into_inner()?.into_inner().map_err(|e| e.into_error())?;
    Ok(fs::metadata(into)?.len())
}

/// Opens `path`, which a VM could write, for reading: the file itself, never
/// a symbolic link's target, without waiting for a writer when it is a FIFO,
/// and only when it is a regular file of at most `max` bytes; no more than
/// the size it had when opened is read. Returns none when there is no such
/// file.
pub fn open_returned(path: &Path, max: u64) -> io::Result<Option<io::Take<fs::File>>> {
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::custom_flags(
        &mut options,
        libc::O_NOFOLLOW | libc::O_NONBLOCK,
    );
    // FILE_FLAG_OPEN_REPARSE_POINT: a link itself, not what it points to.
    #[cfg(windows)]
    std::os::windows::fs::OpenOptionsExt::custom_flags(&mut options, 0x0020_0000);
    let file = match options.open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io::Error::other(format!("{}: {e}", path.display()))),
    };
    let meta = file.metadata()?;
    if !meta.is_file() {
        return Err(io::Error::other(format!(
            "{} is not a regular file",
            path.display()
        )));
    }
    if meta.len() > max {
        return Err(io::Error::other(format!(
            "{} has {} bytes, more than {max}",
            path.display(),
            meta.len()
        )));
    }
    Ok(Some(io::Read::take(file, meta.len())))
}

/// Unpacks the archive of what a VM returned (`tar`, written by the job's
/// image, read as [`open_returned`] reads it, nothing when there is none)
/// into the empty directory `into`: directories, regular files, and hard
/// links between them, each of which stays inside `into`, and nothing else:
/// a symbolic link is reported (an artifact never takes one), so none is
/// there for a later entry to go through. The job's modes are not kept (an
/// artifact is zipped as a readable file): directories get 0755 and files
/// 0644, so that nothing stops their unpacking, reading, or removal. Returns
/// the notes for the log and bytes written to regular files.
pub fn unpack_returned(tar: &Path, max: u64, into: &Path) -> io::Result<(Vec<String>, u64)> {
    use tar::EntryType as T;
    let mut notes = Vec::new();
    let mut bytes = 0;
    let Some(file) = open_returned(tar, max)? else {
        return Ok((notes, bytes));
    };
    let mut archive = tar::Archive::new(io::BufReader::new(file));
    for entry in archive.entries()? {
        let mut entry = entry?;
        let kind = entry.header().entry_type();
        let path = entry.path()?.into_owned();
        let name = path.to_string_lossy().into_owned();
        match kind {
            T::Directory | T::Regular | T::Link => {}
            T::Symlink => {
                notes.push(format!("{name} is a symbolic link; skipped"));
                continue;
            }
            _ => {
                notes.push(format!(
                    "{name} is not a file, a directory, or a hard link; skipped"
                ));
                continue;
            }
        }
        match entry.unpack_in(into) {
            Ok(true) => {
                if kind == T::Regular {
                    bytes += entry.size();
                }
                // Where `unpack_in` put it: a root or `.` component is
                // dropped (one with `..` is refused), and a name of nothing
                // else is `into` itself, which it leaves alone.
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    use std::path::Component;
                    let at: PathBuf = path
                        .components()
                        .filter(|c| matches!(c, Component::Normal(_)))
                        .collect();
                    if kind != T::Link && !at.as_os_str().is_empty() {
                        let mode = if kind == T::Directory { 0o755 } else { 0o644 };
                        fs::set_permissions(into.join(at), fs::Permissions::from_mode(mode))?;
                    }
                }
            }
            Ok(false) => notes.push(format!("{name} is outside the work directory; skipped")),
            Err(e) => notes.push(format!("{name}: {e}; skipped")),
        }
    }
    Ok((notes, bytes))
}

/// Fails when any component of `path` below `root` is a symbolic link or is
/// missing.
pub fn no_links_below(root: &Path, rel: &str) -> io::Result<PathBuf> {
    let mut p = root.to_path_buf();
    for c in rel.trim_end_matches('/').split('/') {
        p.push(c);
        let m = fs::symlink_metadata(&p)?;
        if m.file_type().is_symlink() {
            return Err(io::Error::other(format!(
                "{rel} goes through a symbolic link"
            )));
        }
    }
    Ok(p)
}

/// Every regular file under `path` (itself when it is a file), relative to
/// `root`, `/`-separated. Symbolic links are skipped and reported.
fn files_under(
    root: &Path,
    path: &Path,
    out: &mut Vec<(String, PathBuf)>,
    skipped: &mut Vec<String>,
) -> io::Result<()> {
    let m = fs::symlink_metadata(path)?;
    let rel = path
        .strip_prefix(root)
        .map(|r| r.to_string_lossy().replace('\\', "/"))
        .unwrap_or_default();
    if m.file_type().is_symlink() {
        skipped.push(rel);
    } else if m.is_dir() {
        let mut entries: Vec<_> = fs::read_dir(path)?.flatten().map(|e| e.path()).collect();
        entries.sort();
        for e in entries {
            files_under(root, &e, out, skipped)?;
        }
    } else if m.is_file() {
        out.push((rel, path.to_path_buf()));
    }
    Ok(())
}

/// A zipped artifact ready for upload.
pub struct Packed {
    pub name: String,
    pub data: Vec<u8>,
    pub files: usize,
}

/// Zips one artifact's paths. `budget` is what is left of the attempt's
/// artifact allowance; it shrinks by what this artifact takes. Notes for the
/// log go to `notes`.
pub fn pack(
    work: &Path,
    a: &ArtifactDef,
    budget: &mut u64,
    notes: &mut Vec<String>,
) -> io::Result<Option<Packed>> {
    let mut files = Vec::new();
    let mut skipped = Vec::new();
    for p in &a.paths {
        if check_relative(p).is_err() {
            notes.push(format!(
                "artifact {}: {p} is not a relative path; skipped",
                a.name
            ));
            continue;
        }
        match no_links_below(work, p) {
            Ok(full) => files_under(work, &full, &mut files, &mut skipped)?,
            Err(e) => notes.push(format!("artifact {}: {p}: {e}; skipped", a.name)),
        }
    }
    for s in skipped {
        notes.push(format!(
            "artifact {}: {s} is a symbolic link; skipped",
            a.name
        ));
    }
    if files.is_empty() {
        notes.push(format!("artifact {}: no files found", a.name));
        return Ok(None);
    }
    let total: u64 = files
        .iter()
        .map(|(_, p)| fs::symlink_metadata(p).map(|m| m.len()).unwrap_or(0))
        .sum();
    if total > *budget {
        notes.push(format!(
            "artifact {}: {total} bytes exceed the remaining artifact allowance of {} bytes; not uploaded",
            a.name, *budget
        ));
        return Ok(None);
    }
    *budget -= total;
    let mut z = ZipWriter::new(io::Cursor::new(Vec::new()));
    for (rel, path) in &files {
        // Opened without following a link that replaced the file meanwhile
        // is not expressible portably; the job has ended, so nothing writes
        // the directory any more.
        z.add(rel, fs::File::open(path)?)?;
    }
    Ok(Some(Packed {
        name: a.name.clone(),
        data: z.finish()?.into_inner(),
        files: files.len(),
    }))
}

// ---------------------------------------------------------------- cache

/// The cache key of a job: repository, job, the configuration digest of the
/// image it runs on, the user it runs as (the files keep the owner the VM saw),
/// the runner's work quota (a job's `/work` is no smaller than the entry's
/// image, so a changed quota takes a new entry), cached paths, and the
/// content of the key files.
pub fn cache_key(
    repo: &str,
    job: &JobDef,
    image_digest: &str,
    cache: &CacheDef,
    work_quota_mib: u64,
    work: &Path,
) -> String {
    let (user, quota) = (
        super::executor::JOB_USER_ID.to_string(),
        work_quota_mib.to_string(),
    );
    let mut h = Vec::new();
    for part in [repo, &job.name, image_digest, &user, &quota] {
        h.extend_from_slice(part.as_bytes());
        h.push(0);
    }
    for p in &cache.paths {
        h.extend_from_slice(p.as_bytes());
        h.push(0);
    }
    for k in &cache.key_files {
        h.extend_from_slice(k.as_bytes());
        h.push(0);
        match no_links_below(work, k).and_then(fs::read) {
            Ok(content) => h.extend_from_slice(&crate::util::sha256(&content)),
            Err(_) => h.extend_from_slice(b"<missing>"),
        }
        h.push(0);
    }
    crate::util::hex(&crate::util::sha256(&h))
}

/// What `path` takes on its disk: a sparse image counts its blocks of data.
fn tree_size(path: &Path) -> u64 {
    let Ok(m) = fs::symlink_metadata(path) else {
        return 0;
    };
    if m.is_dir() {
        fs::read_dir(path)
            .map(|rd| rd.flatten().map(|e| tree_size(&e.path())).sum())
            .unwrap_or(0)
    } else {
        super::disk::allocated(path).unwrap_or(m.len())
    }
}

const USED: &str = ".branvil-used";
const SAVED: &str = ".branvil-saved";
const OWNER: &str = ".branvil-owner";
const KEY: &str = ".branvil-key";
const PENDING: &str = ".pending";
/// The bytes of data the file system of an entry's image holds, as the VM
/// that kept its cached paths measured them.
const DATA: &str = ".branvil-data";
/// The disk image an entry holds: a job's `/work` that kept only the cached
/// paths.
const IMAGE: &str = "disk.raw";

pub(super) use crate::ci::pipeline::CacheOwner;

fn record_use(entry: &Path, used: SystemTime) -> io::Result<()> {
    fs::File::create(entry.join(USED))?.set_modified(used)
}

/// The runner's dependency cache: per key, a disk image containing cached
/// paths from a job's `/work`, which the host never mounts. The jobs a runner runs at once
/// share it: a save replaces its entry and evicts others only while no
/// restore reads from the cache, so a restore gets an entry whole, the old
/// one or the new one. One runner uses a work directory (`runner.lock`), so a
/// lock of the process is enough.
pub struct Cache {
    root: PathBuf,
    lock: RwLock<()>,
}

impl Cache {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            lock: RwLock::new(()),
        }
    }

    /// Removes the entries of an earlier form (a tree of the cached paths, an
    /// archive of them), which no restore reads, and what a save that was
    /// stopped left aside. Only when no save runs (at the runner's start).
    pub fn remove_earlier_forms(&self) -> io::Result<()> {
        let _writing = self.lock.write().unwrap_or_else(|e| e.into_inner());
        let Ok(entries) = fs::read_dir(&self.root) else {
            return Ok(());
        };
        for e in entries.flatten() {
            let path = e.path();
            let aside = e.file_name().to_string_lossy().starts_with('.');
            if e.file_name() == PENDING {
                continue;
            }
            if path.is_dir()
                && (aside || !path.join(IMAGE).is_file() || entry_owner(&path).is_none())
            {
                fs::remove_dir_all(&path)?;
            }
        }
        Ok(())
    }

    /// Hard-link a published raw backing image into the attempt directory.
    /// The caller must open the image only for reading.
    pub fn restore(&self, key: &str, into: &Path) -> io::Result<Option<u64>> {
        let _reading = self.lock.read().unwrap_or_else(|e| e.into_inner());
        restore_entry(&self.root.join(key), into)
    }

    /// Hard-link the exact published image, or the newest compatible one.
    /// The link survives eviction once the read lock has been released.
    /// Returns the generation's key and the bytes of cached data it holds.
    pub fn restore_generation(
        &self,
        key: &str,
        owner: &CacheOwner,
        into: &Path,
    ) -> io::Result<Option<(String, u64)>> {
        let _reading = self.lock.read().unwrap_or_else(|e| e.into_inner());
        let exact = self.root.join(key);
        if entry_owner(&exact).as_ref() == Some(owner)
            && let Some(size) = restore_entry(&exact, into)?
        {
            return Ok(Some((key.into(), size)));
        }
        let latest = dirs(&self.root)?
            .into_iter()
            .filter(|p| {
                p != &exact && entry_owner(p).as_ref() == Some(owner) && p.join(IMAGE).is_file()
            })
            .filter_map(|p| Some((saved_at(&p)?, p)))
            .max();
        let Some((_, entry)) = latest else {
            return Ok(None);
        };
        let generation = entry
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        Ok(restore_entry(&entry, into)?.map(|size| (generation, size)))
    }

    /// Put aside a successful execution before reporting it to the Hub.
    /// Staging can reclaim superseded and pending images. Returns whether
    /// the entry was kept and the bytes written to its metadata files.
    pub fn stage(
        &self,
        attempt: &str,
        key: &str,
        owner: &CacheOwner,
        (image, data): (&Path, u64),
        max_bytes: u64,
    ) -> io::Result<(bool, u64)> {
        if data > max_bytes {
            return Ok((false, 0));
        }
        let pending = self.root.join(PENDING);
        fs::create_dir_all(&pending)?;
        let tmp = tempfile::Builder::new()
            .prefix(".stage-")
            .tempdir_in(&self.root)?;
        let metadata_bytes = write_entry(tmp.path(), owner, image, data)?;
        fs::write(tmp.path().join(KEY), key)?;
        record_use(tmp.path(), SystemTime::now())?;
        let _writing = self.lock.write().unwrap_or_else(|e| e.into_inner());
        let entry = pending.join(attempt);
        fs::rename(tmp.path(), &entry)?;
        evict(&self.root, max_bytes, None)?;
        Ok((entry.is_dir(), metadata_bytes + key.len() as u64))
    }

    pub fn pending(&self) -> io::Result<Vec<String>> {
        let _reading = self.lock.read().unwrap_or_else(|e| e.into_inner());
        Ok(dirs(&self.root.join(PENDING))?
            .into_iter()
            .filter_map(|path| path.file_name()?.to_str().map(str::to_owned))
            .collect())
    }

    pub(super) fn published_groups(
        &self,
    ) -> io::Result<Option<std::collections::BTreeSet<String>>> {
        let _reading = self.lock.read().unwrap_or_else(|e| e.into_inner());
        let entries = dirs(&self.root)?;
        // Bound the report and the metadata reads on the polling path.
        if entries.len() > 256 {
            return Ok(None);
        }
        Ok(Some(
            entries
                .into_iter()
                .filter(|path| path.join(IMAGE).is_file() && saved_at(path).is_some())
                .filter_map(|path| entry_owner(&path).map(|owner| owner.fingerprint()))
                .collect(),
        ))
    }

    /// Publish with the Hub's timestamp so a delayed decision cannot
    /// replace a more recent save of the same key. Missing images are fine.
    pub fn settle(
        &self,
        attempt: &str,
        publish: Option<SystemTime>,
        max_bytes: u64,
    ) -> io::Result<()> {
        let _writing = self.lock.write().unwrap_or_else(|e| e.into_inner());
        let pending = self.root.join(PENDING).join(attempt);
        if !pending.is_dir() {
            return Ok(());
        }
        if let Some(at) = publish
            && entry_size(&pending) <= max_bytes
        {
            let key = fs::read_to_string(pending.join(KEY))?;
            let entry = self.root.join(key);
            if saved_at(&entry).is_none_or(|saved| saved < at) {
                fs::File::create(pending.join(SAVED))?.set_modified(at)?;
                record_use(&pending, SystemTime::now())?;
                if entry.is_dir() {
                    fs::remove_dir_all(&entry)?;
                }
                fs::rename(&pending, &entry)?;
                return evict(&self.root, max_bytes, Some(&entry));
            }
        }
        fs::remove_dir_all(pending)
    }

    #[cfg(test)]
    fn save(
        &self,
        key: &str,
        owner: &CacheOwner,
        (image, data): (&Path, u64),
        max_bytes: u64,
    ) -> io::Result<u64> {
        self.save_with_clock(key, owner, (image, data), max_bytes, SystemTime::now)
    }

    #[cfg(test)]
    fn save_with_clock(
        &self,
        key: &str,
        owner: &CacheOwner,
        (image, data): (&Path, u64),
        max_bytes: u64,
        now: impl FnOnce() -> SystemTime,
    ) -> io::Result<u64> {
        fs::create_dir_all(&self.root)?;
        let tmp = tempfile::Builder::new()
            .prefix(".stage-")
            .tempdir_in(&self.root)?;
        write_entry(tmp.path(), owner, image, data)?;
        let allocated = super::disk::allocated(&tmp.path().join(IMAGE))?;
        let saved_at = now();
        record_use(tmp.path(), saved_at)?;
        fs::File::create(tmp.path().join(SAVED))?.set_modified(saved_at)?;
        let _writing = self.lock.write().unwrap_or_else(|e| e.into_inner());
        let entry = self.root.join(key);
        let _ = fs::remove_dir_all(&entry);
        fs::rename(tmp.path(), &entry)?;
        evict(&self.root, max_bytes, Some(&entry))?;
        Ok(allocated)
    }
}

fn dirs(root: &Path) -> io::Result<Vec<PathBuf>> {
    let listed = match fs::read_dir(root) {
        Ok(listed) => listed,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut dirs = Vec::new();
    for item in listed {
        let item = item?;
        if !item.file_name().to_string_lossy().starts_with('.') && item.file_type()?.is_dir() {
            dirs.push(item.path());
        }
    }
    Ok(dirs)
}

fn write_entry(entry: &Path, owner: &CacheOwner, image: &Path, data: u64) -> io::Result<u64> {
    if !fs::symlink_metadata(image).is_ok_and(|m| m.is_file()) {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("{} is missing", image.display()),
        ));
    }
    // The attempt image and cache are under the same runner work directory.
    // Staging must not fall back to copying the entire image.
    fs::rename(image, entry.join(IMAGE))?;
    let data = data.to_string();
    let owner = serde_json::to_vec(owner)?;
    fs::write(entry.join(DATA), &data)?;
    fs::write(entry.join(OWNER), &owner)?;
    Ok((data.len() + owner.len()) as u64)
}

fn entry_owner(entry: &Path) -> Option<CacheOwner> {
    serde_json::from_slice(&fs::read(entry.join(OWNER)).ok()?).ok()
}

fn entry_data(entry: &Path) -> Option<u64> {
    fs::read_to_string(entry.join(DATA))
        .ok()?
        .trim()
        .parse()
        .ok()
}

fn entry_size(entry: &Path) -> u64 {
    entry_data(entry).unwrap_or_else(|| tree_size(entry))
}

fn saved_at(entry: &Path) -> Option<SystemTime> {
    fs::metadata(entry.join(SAVED)).ok()?.modified().ok()
}

fn last_use(entry: &Path) -> SystemTime {
    fs::metadata(entry.join(USED))
        .and_then(|m| m.modified())
        .unwrap_or(SystemTime::UNIX_EPOCH)
}

fn restore_entry(entry: &Path, into: &Path) -> io::Result<Option<u64>> {
    let image = entry.join(IMAGE);
    if !fs::symlink_metadata(&image).is_ok_and(|m| m.is_file()) {
        return Ok(None);
    }
    let data = match entry_data(entry) {
        Some(data) => data,
        None => super::disk::allocated(&image)?,
    };
    fs::hard_link(&image, into)?;
    if let Err(error) = record_use(entry, SystemTime::now()) {
        let _ = fs::remove_file(into);
        return Err(error);
    }
    Ok(Some(data))
}

/// Only publication may evict a current generation.
fn evict(cache_root: &Path, max_bytes: u64, saved_entry: Option<&Path>) -> io::Result<()> {
    #[derive(PartialEq, Eq, PartialOrd, Ord)]
    enum Order {
        Superseded,
        Pending,
        Current,
    }
    struct Entry {
        owner: Option<CacheOwner>,
        used: SystemTime,
        size: u64,
        path: PathBuf,
    }
    let entry = |path: PathBuf, owner| Entry {
        used: last_use(&path),
        size: entry_size(&path),
        path,
        owner,
    };
    let mut entries: Vec<_> = dirs(&cache_root.join(PENDING))?
        .into_iter()
        .map(|path| entry(path, None))
        .collect();
    let mut current = BTreeMap::new();
    for path in dirs(cache_root)? {
        let Some(owner) = entry_owner(&path) else {
            // Missing, unreadable, or invalid records cannot be grouped.
            // Remove the entry even below budget so it cannot stall eviction.
            fs::remove_dir_all(&path)?;
            continue;
        };
        if let Some(saved) = saved_at(&path) {
            let candidate = (saved, path.clone());
            let active = current.entry(owner.clone()).or_insert(candidate.clone());
            if candidate > *active {
                *active = candidate;
            }
        }
        entries.push(entry(path, Some(owner)));
    }
    let order = |e: &Entry| match &e.owner {
        Some(owner) if current.get(owner).is_some_and(|v| v.1 == e.path) => Order::Current,
        Some(_) => Order::Superseded,
        None => Order::Pending,
    };
    // A save is the latest use when timestamps tie (e.g. on a coarse
    // filesystem). Other ties are broken by key, independently of size.
    let key = |e: &Entry| (order(e), e.used, saved_entry == Some(e.path.as_path()));
    entries.sort_by(|a, b| key(a).cmp(&key(b)).then_with(|| a.path.cmp(&b.path)));
    let mut total: u64 = entries.iter().map(|e| e.size).sum();
    for entry in entries {
        if total <= max_bytes || (saved_entry.is_none() && order(&entry) == Order::Current) {
            break;
        }
        fs::remove_dir_all(&entry.path)?;
        total -= entry.size;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn superseded_images_make_room_for_pending_before_current_images() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path().join("cache"));
        let first = owner("repo", "first");
        let second = owner("repo", "second");
        for (key, elapsed) in [("previous", 1), ("current", 2)] {
            let image = packed(dir.path(), key.as_bytes());
            assert!(cache.stage(key, key, &first, (&image, 30), 100).unwrap().0);
            let at = SystemTime::UNIX_EPOCH + Duration::from_secs(elapsed);
            cache.settle(key, Some(at), 100).unwrap();
        }
        let restored = dir.path().join("restored");
        assert_eq!(cache.restore("previous", &restored).unwrap(), Some(30));
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let published = fs::metadata(cache.root.join("previous").join(IMAGE)).unwrap();
            assert_eq!(fs::metadata(&restored).unwrap().ino(), published.ino());
        }

        let image = packed(dir.path(), b"pending");
        assert!(
            cache
                .stage("next", "next", &second, (&image, 50), 100)
                .unwrap()
                .0
        );
        assert_eq!(cache.pending().unwrap(), ["next"]);
        assert!(!cache.root.join("previous").exists());
        assert_eq!(fs::read(&restored).unwrap(), b"previous");
        fs::remove_file(&restored).unwrap();
        assert!(cache.restore("previous", &restored).unwrap().is_none());
        assert_eq!(cache.restore("current", &restored).unwrap(), Some(30));
        fs::remove_file(&restored).unwrap();

        let at = SystemTime::UNIX_EPOCH + Duration::from_secs(3);
        cache.settle("next", Some(at), 100).unwrap();
        assert_eq!(cache.restore("current", &restored).unwrap(), Some(30));
        fs::remove_file(&restored).unwrap();
        assert_eq!(cache.restore("next", &restored).unwrap(), Some(50));
    }

    #[test]
    fn a_lower_limit_leaves_current_data_intact_during_staging_and_publication() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path().join("cache"));
        let owner = owner("repo", "job");
        let original = packed(dir.path(), b"original");
        let small = packed(dir.path(), b"small");
        let newer = packed(dir.path(), b"newer");
        let at = SystemTime::UNIX_EPOCH + Duration::from_secs(1);
        assert!(
            cache
                .stage("first", "key", &owner, (&original, 20), 100)
                .unwrap()
                .0
        );
        cache.settle("first", Some(at), 100).unwrap();
        // A lowered budget may already be smaller than the published image.
        assert!(
            !cache
                .stage("small", "key", &owner, (&small, 5), 15)
                .unwrap()
                .0
        );
        assert!(
            cache
                .stage("second", "key", &owner, (&newer, 70), 100)
                .unwrap()
                .0
        );
        cache
            .settle("second", Some(at + Duration::from_secs(1)), 50)
            .unwrap();
        let restored = dir.path().join("restored");
        assert_eq!(cache.restore("key", &restored).unwrap(), Some(20));
        assert_eq!(fs::read(restored).unwrap(), b"original");
        assert!(cache.pending().unwrap().is_empty());
    }

    #[test]
    fn pending_images_cannot_be_restored_and_are_evicted_before_saved_images() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path().join("cache"));
        let owner = owner("repo", "job");
        let image = dir.path().join("image");
        let restored = dir.path().join("restored");
        fs::write(&image, b"merged").unwrap();
        cache
            .stage("att_1", "first", &owner, (&image, 10), 20)
            .unwrap();
        assert!(cache.published_groups().unwrap().unwrap().is_empty());
        assert!(
            cache
                .restore_generation("first", &owner, &restored)
                .unwrap()
                .is_none()
        );
        cache
            .settle("att_1", Some(SystemTime::UNIX_EPOCH), 20)
            .unwrap();
        assert_eq!(
            cache.published_groups().unwrap().unwrap(),
            [owner.fingerprint()].into()
        );
        fs::write(&image, b"unmerged").unwrap();
        cache
            .stage("att_2", "second", &owner, (&image, 11), 20)
            .unwrap();
        assert!(cache.pending().unwrap().is_empty());
        let generation = cache
            .restore_generation("second", &owner, &restored)
            .unwrap()
            .unwrap();
        assert_eq!(generation, ("first".into(), 10));
        assert_eq!(fs::read(restored).unwrap(), b"merged");
    }

    #[test]
    fn cache_lookup_prefers_exact_then_latest_compatible_key() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path().join("cache"));
        let work = dir.path();
        let owner = owner("repo", "job");
        let time = |n| SystemTime::UNIX_EPOCH + Duration::from_secs(n);
        for (attempt, key, at) in [("a", "old", 1), ("b", "new", 2), ("c", "retired", 3)] {
            let image = packed(work, key.as_bytes());
            assert!(
                cache
                    .stage(attempt, key, &owner, (&image, 10), 100)
                    .unwrap()
                    .0
            );
            cache
                .settle(attempt, (at != 3).then(|| time(at)), 100)
                .unwrap();
        }
        let restore = |key: &str, owner: &CacheOwner| {
            let into = dir.path().join(format!("lookup-{key}"));
            cache
                .restore_generation(key, owner, &into)
                .unwrap()
                .map(|(key, bytes)| {
                    assert_eq!(fs::read(&into).unwrap(), key.as_bytes());
                    fs::remove_file(into).unwrap();
                    (key, bytes)
                })
        };
        assert_eq!(restore("old", &owner), Some(("old".into(), 10)));
        assert_eq!(restore("missing", &owner), Some(("new".into(), 10)));
        assert_eq!(restore("retired", &owner), Some(("new".into(), 10)));
        for incompatible in [
            CacheOwner {
                user: owner.user + 1,
                ..owner.clone()
            },
            CacheOwner {
                paths: vec!["other".into()],
                ..owner.clone()
            },
            CacheOwner {
                repo: "other".into(),
                ..owner.clone()
            },
            CacheOwner {
                job: "other".into(),
                ..owner.clone()
            },
        ] {
            assert_eq!(restore("old", &incompatible), None);
        }
    }

    fn owner(repo: &str, job: &str) -> CacheOwner {
        CacheOwner {
            repo: repo.into(),
            job: job.into(),
            user: super::super::executor::JOB_USER_ID,
            paths: vec!["target".into()],
        }
    }

    fn mark_owner(entry: &Path, repo: &str, job: &str) {
        fs::write(
            entry.join(OWNER),
            serde_json::to_vec(&owner(repo, job)).unwrap(),
        )
        .unwrap();
    }

    fn mark_saved(entry: &Path, saved: SystemTime) {
        fs::File::create(entry.join(SAVED))
            .unwrap()
            .set_modified(saved)
            .unwrap();
    }

    impl Cache {
        /// A save of `image` whose file system holds what the file takes.
        fn save_taken(
            &self,
            key: &str,
            owner: &CacheOwner,
            image: &Path,
            max_bytes: u64,
        ) -> io::Result<u64> {
            self.save_taken_with_clock(key, owner, image, max_bytes, SystemTime::now)
        }

        fn save_taken_with_clock(
            &self,
            key: &str,
            owner: &CacheOwner,
            image: &Path,
            max_bytes: u64,
            now: impl FnOnce() -> SystemTime,
        ) -> io::Result<u64> {
            let data = super::super::disk::allocated(image).unwrap_or(0);
            self.save_with_clock(key, owner, (image, data), max_bytes, now)
        }
    }

    /// What a file of `content` takes on the test's disk: whole blocks.
    fn taken(dir: &Path, content: &[u8]) -> u64 {
        let path = packed(dir, content);
        let n = super::super::disk::allocated(&path).unwrap();
        fs::remove_file(path).unwrap();
        n
    }

    /// A disk image holding `content`, ready for a save to take.
    fn packed(dir: &Path, content: &[u8]) -> PathBuf {
        let path = dir.join(format!("packed-{}", crate::util::random_secret::<6>()));
        fs::write(&path, content).unwrap();
        path
    }

    /// The host takes the artifacts' archive a VM returned, and a disk image
    /// for the cache, only as a regular file, without following a link to
    /// the host's file or waiting for a FIFO's writer; the artifacts' archive
    /// also not beyond its limit. Nothing of the host's file is read.
    #[test]
    fn returned_files_are_read_only_as_regular_files() {
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path().to_path_buf();
        fs::write(d.join("host-secret"), "secret").unwrap();
        let mut planted: Vec<(&str, PathBuf)> = Vec::new();
        let directory = d.join("directory");
        fs::create_dir(&directory).unwrap();
        planted.push(("a directory", directory));
        #[cfg(unix)]
        {
            let link = d.join("link");
            std::os::unix::fs::symlink(d.join("host-secret"), &link).unwrap();
            planted.push(("a link", link));
            let fifo = d.join("fifo");
            let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
            assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o644) }, 0);
            planted.push(("a FIFO", fifo));
        }
        let mut tar = tar::Builder::new(Vec::new());
        let mut h = tar::Header::new_gnu();
        h.set_size(1);
        tar.append_data(&mut h, "big", &b"x"[..]).unwrap();
        let large = packed(&d, &tar.into_inner().unwrap());
        let (done, results) = std::sync::mpsc::channel();
        let readers = std::thread::spawn(move || {
            let into = d.join("into");
            for (what, path) in &planted {
                fs::create_dir_all(&into).unwrap();
                let unpacked = unpack_returned(path, 1 << 20, &into);
                let saved =
                    Cache::new(d.join("cache")).save_taken("k", &owner("r", "j"), path, 1 << 20);
                let restored = Cache::new(d.join("cache")).restore("k", &d.join("given"));
                done.send((
                    what.to_string(),
                    unpacked.is_err(),
                    saved.is_err(),
                    restored,
                ))
                .unwrap();
                fs::remove_dir_all(&into).unwrap();
            }
            fs::create_dir_all(&into).unwrap();
            let too_large = unpack_returned(&large, 1024, &into);
            let refused = too_large.is_err() && !into.join("big").exists();
            done.send(("large".into(), refused, true, Ok(None)))
                .unwrap();
        });
        for _ in 0..if cfg!(unix) { 4 } else { 2 } {
            let (what, unpack_refused, save_refused, restored) = results
                .recv_timeout(Duration::from_secs(30))
                .expect("a reader is waiting");
            assert!(unpack_refused, "unpacking {what}");
            assert!(save_refused, "saving {what}");
            assert_eq!(restored.unwrap(), None, "{what} became an entry");
        }
        readers.join().unwrap();
    }

    #[test]
    fn cache_entries_of_an_earlier_form_are_not_restored_and_are_removed() {
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path();
        let cache = Cache::new(d.join("cache"));
        cache
            .save_taken("new", &owner("r", "j"), &packed(d, b"new"), 1 << 20)
            .unwrap();
        let tree = d.join("cache/tree");
        fs::create_dir_all(tree.join("paths/target")).unwrap();
        fs::write(tree.join("paths/target/file"), b"old").unwrap();
        let archive = d.join("cache/archive");
        fs::create_dir_all(&archive).unwrap();
        fs::write(archive.join("cache.tar"), b"old").unwrap();
        for old in [&tree, &archive] {
            mark_owner(old, "r", "old");
            mark_saved(old, SystemTime::now());
            let into = d.join("old.raw");
            assert_eq!(
                cache
                    .restore(&old.file_name().unwrap().to_string_lossy(), &into)
                    .unwrap(),
                None
            );
            assert!(!into.exists());
        }
        // A save that was stopped left its image aside.
        let aside = d.join("cache/.new.abcdef");
        fs::create_dir_all(&aside).unwrap();
        fs::write(aside.join(IMAGE), b"part").unwrap();
        cache.remove_earlier_forms().unwrap();
        assert!(!tree.exists() && !archive.exists() && !aside.exists());
        assert!(cache.restore("new", &d.join("new.raw")).unwrap().is_some());
        assert_eq!(fs::read(d.join("new.raw")).unwrap(), b"new");
    }

    /// The members are listed one per line, as their headers' bytes; a name
    /// with a newline is refused rather than read as two.
    #[test]
    fn members_are_listed_as_their_bytes_or_refused() {
        let dir = tempfile::tempdir().unwrap();
        let (archive, list) = (dir.path().join("c.tar"), dir.path().join("members"));
        let pack = |names: &[&[u8]]| {
            let mut tar = tar::Builder::new(Vec::new());
            for name in names {
                let mut h = tar::Header::new_gnu();
                h.set_size(0);
                h.as_old_mut().name[..name.len()].copy_from_slice(name);
                h.set_cksum();
                tar.append(&h, &b""[..]).unwrap();
            }
            fs::write(&archive, tar.into_inner().unwrap()).unwrap();
        };
        pack(&["日本/a".as_bytes(), b"a\\b"]);
        list_members(&archive, &list).unwrap();
        assert_eq!(fs::read(&list).unwrap(), "日本/a\na\\b\n".as_bytes());
        pack(&[b"a\nb/x"]);
        assert!(list_members(&archive, &list).is_err());
    }

    /// The commit's links come from its index, byte for byte: a link the
    /// machine made a file is a link, and `Target` is not `target`.
    #[test]
    fn the_tree_is_the_indexs() {
        let index = b"120000 aa 0\ttarget\x00100644 bb 0\tvendor/sub/a.rs\x00120000 cc 0\tTop\x00100644 dd 0\tsrc/main.rs\x00160000 ee 0\tdeps/module\x00";
        let tree = Tree::from_index(index).unwrap();
        assert_eq!(tree.link_on(["deps", "target/debug"]), Some("target/debug"));
        assert_eq!(tree.link_on(["target/"]), Some("target/"));
        assert_eq!(tree.link_on(["top", "vendor", "src/main.rs/x"]), None);
        assert!(Tree::from_index(b"no tab\x00").is_err());
    }

    /// The machine's Git attributes never change the checkout. Git finds
    /// them through the environment, so the checkout runs in a child test
    /// process whose `XDG_CONFIG_HOME` and `HOME` hold `* text eol=crlf`.
    #[tokio::test]
    async fn the_checkout_ignores_the_machines_git_attributes() {
        if std::env::var_os("GUILDBOTICS_TEST_CHILD").is_some() {
            let dir = PathBuf::from(std::env::var_os("GUILDBOTICS_TEST_DIR").unwrap());
            let work = dir.join("work");
            checkout(
                &dir.join("b.bundle"),
                &work,
                &dir.join("gitconfig"),
                &dir.join("checkout.tar"),
            )
            .await
            .unwrap();
            assert_eq!(fs::read(work.join("f")).unwrap(), b"a\n");
            let config = fs::read_to_string(work.join(".git/config")).unwrap();
            assert!(config.ends_with(VM_GIT_CONFIG), "{config}");
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path();
        for attributes in ["xdg/git/attributes", "home/.config/git/attributes"] {
            fs::create_dir_all(d.join(attributes).parent().unwrap()).unwrap();
            fs::write(d.join(attributes), "* text eol=crlf\n").unwrap();
        }
        fs::write(d.join("empty"), "").unwrap();
        let git = |args: &[&str]| {
            let out = std::process::Command::new("git")
                .current_dir(d)
                .args(["-c", "credential.helper="])
                .args(args)
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .env("GIT_CONFIG_GLOBAL", d.join("empty"))
                .env("GIT_TERMINAL_PROMPT", "0")
                .env("XDG_CONFIG_HOME", d.join("xdg"))
                .env("HOME", d.join("home"))
                .env("GIT_AUTHOR_NAME", "Test")
                .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
                .env("GIT_COMMITTER_NAME", "Test")
                .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
        };
        fs::create_dir(d.join("r")).unwrap();
        fs::write(d.join("r/f"), "a\n").unwrap();
        git(&["-C", "r", "init", "-q"]);
        git(&["-C", "r", "add", "f"]);
        git(&["-C", "r", "commit", "-qm", "f"]);
        git(&["-C", "r", "bundle", "create", "-q", "../b.bundle", "HEAD"]);
        // Without the isolation, the attributes apply.
        git(&["clone", "-q", "b.bundle", "plain"]);
        assert_eq!(fs::read(d.join("plain/f")).unwrap(), b"a\r\n");
        super::super::pass_in_child(
            &format!(
                "{}::the_checkout_ignores_the_machines_git_attributes",
                module_path!()
            ),
            &[
                ("GUILDBOTICS_TEST_DIR", d.as_os_str()),
                ("XDG_CONFIG_HOME", d.join("xdg").as_os_str()),
                ("HOME", d.join("home").as_os_str()),
            ],
        );
    }

    /// The archive takes each tracked file's mode and kind from the index,
    /// not from the machine's file system: a machine without exec bits or
    /// links (Windows, where a clone leaves a link as its target in a file)
    /// packs the same archive.
    #[test]
    fn the_checkout_archive_takes_modes_and_links_from_the_index() {
        let dir = tempfile::tempdir().unwrap();
        let work = dir.path().join("work");
        fs::create_dir_all(work.join(".git")).unwrap();
        fs::write(work.join(".git/config"), "[core]\n").unwrap();
        fs::write(work.join("run.sh"), "#!/bin/sh\n").unwrap();
        fs::write(work.join("file.txt"), "text").unwrap();
        fs::write(work.join("link"), "file.txt").unwrap();
        fs::create_dir(work.join("module")).unwrap();
        // Targets as the blobs hold them, unnormalized, however long.
        let long = format!("{}/x", "d".repeat(150));
        fs::write(work.join("odd"), "a//./b/").unwrap();
        fs::write(work.join("long"), &long).unwrap();
        let index = b"100755 0a 0\trun.sh\x00100644 0b 0\tfile.txt\x00120000 0c 0\tlink\x00160000 0d 0\tmodule\x00120000 0e 0\todd\x00120000 0f 0\tlong\x00";
        let into = dir.path().join("checkout.tar");
        pack_checkout(&work, index, &into).unwrap();
        let mut archive = tar::Archive::new(fs::File::open(&into).unwrap());
        let entries: Vec<_> = archive
            .entries()
            .unwrap()
            .map(|e| {
                let e = e.unwrap();
                let h = e.header();
                let link = e.link_name().unwrap().map(|l| l.display().to_string());
                (
                    e.path().unwrap().display().to_string(),
                    h.entry_type(),
                    h.mode().unwrap(),
                    link,
                )
            })
            .collect();
        use tar::EntryType as T;
        let link =
            |name: &str, target: &str| (name.to_string(), T::Symlink, 0o777, Some(target.into()));
        assert_eq!(
            entries[..6],
            [
                ("run.sh".to_string(), T::Regular, 0o755, None),
                ("file.txt".to_string(), T::Regular, 0o644, None),
                link("link", "file.txt"),
                ("module".to_string(), T::Directory, 0o755, None),
                link("odd", "a//./b/"),
                link("long", &long),
            ]
        );
        assert!(
            entries[6..].iter().any(|e| e.0 == ".git/config"),
            "{entries:?}"
        );
    }

    /// What a VM returns was written by the job: nothing of it lands outside
    /// the directory it is unpacked into, and only files, directories, and
    /// hard links between them do.
    #[test]
    fn returned_archives_unpack_only_inside_their_directory() {
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path();
        let outside = d.join("outside");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("secret"), "secret").unwrap();
        #[cfg(unix)]
        for (p, mode) in [(outside.clone(), 0o700), (outside.join("secret"), 0o600)] {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(p, fs::Permissions::from_mode(mode)).unwrap();
        }
        let (outside_dir, secret) = (
            outside.to_string_lossy().into_owned(),
            outside.join("secret").to_string_lossy().into_owned(),
        );
        let mut tar = tar::Builder::new(Vec::new());
        let mut add = |name: &str, kind: tar::EntryType, link: Option<&Path>, data: &[u8]| {
            let mut h = tar::Header::new_gnu();
            h.as_old_mut().name[..name.len()].copy_from_slice(name.as_bytes());
            h.set_entry_type(kind);
            h.set_mode(0o644);
            h.set_size(data.len() as u64);
            if let Some(l) = link {
                h.set_link_name(l).unwrap();
            }
            // A sparse file of a TiB whose archive holds its last byte.
            if kind == tar::EntryType::GNUSparse {
                let gnu = h.as_gnu_mut().unwrap();
                gnu.set_real_size(1 << 40);
                gnu.sparse[0].set_offset((1 << 40) - data.len() as u64);
                gnu.sparse[0].set_length(data.len() as u64);
            }
            h.set_cksum();
            tar.append(&h, data).unwrap();
        };
        use tar::EntryType as T;
        add("out/a.txt", T::Regular, None, b"a");
        add("out/b.txt", T::Link, Some(Path::new("out/a.txt")), b"");
        add("../escape.txt", T::Regular, None, b"x");
        add("out/d", T::Symlink, Some(&outside), b"");
        add("out/d", T::Directory, None, b"");
        add("out/d/x", T::Regular, None, b"x");
        add("out/fifo", T::Fifo, None, b"");
        add("out/sparse", T::GNUSparse, None, b"s");
        add("out/contiguous", T::Continuous, None, b"c");
        // Absolute names land inside too, and the modes set after the
        // unpacking change nothing outside.
        add(&outside_dir, T::Directory, None, b"");
        add(&secret, T::Regular, None, b"x");
        // A name of nothing but a root leaves the directory as it is.
        add("/", T::Regular, None, b"");
        add("out/last.txt", T::Regular, None, b"l");
        let archive = d.join("returned.tar");
        fs::write(&archive, tar.into_inner().unwrap()).unwrap();
        let into = d.join("work");
        fs::create_dir(&into).unwrap();
        let (notes, bytes) = unpack_returned(&archive, 1 << 20, &into).unwrap();
        assert!(bytes > 0);
        assert_eq!(fs::read(into.join("out/a.txt")).unwrap(), b"a");
        assert_eq!(fs::read(into.join("out/b.txt")).unwrap(), b"a");
        assert!(!d.join("escape.txt").exists());
        assert!(!outside.join("x").exists());
        assert!(into.join("out/d").is_dir() && !into.join("out/d").is_symlink());
        assert_eq!(fs::read(into.join("out/d/x")).unwrap(), b"x");
        for kind in ["fifo", "sparse", "contiguous"] {
            assert!(!into.join("out").join(kind).exists(), "{kind}");
            assert!(
                notes
                    .iter()
                    .any(|n| n.starts_with(&format!("out/{kind} is not a file"))),
                "{notes:?}"
            );
        }
        assert_eq!(fs::read(into.join("out/last.txt")).unwrap(), b"l");
        assert!(notes.iter().any(|n| n.contains("escape.txt")), "{notes:?}");
        assert!(
            notes.iter().any(|n| n.contains("out/d is a symbolic link")),
            "{notes:?}"
        );
        assert_eq!(fs::read(outside.join("secret")).unwrap(), b"secret");
        #[cfg(unix)]
        for (p, mode) in [(outside.clone(), 0o700), (outside.join("secret"), 0o600)] {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&p).unwrap().permissions().mode() & 0o777, mode);
        }
        // The job's modes are not kept: none stops the unpacking, the
        // reading, or the removal.
        let mut tar = tar::Builder::new(Vec::new());
        for (name, kind, mode, data) in [
            ("ro", T::Directory, 0o555, &b""[..]),
            ("ro/f", T::Regular, 0o000, &b"f"[..]),
        ] {
            let mut h = tar::Header::new_gnu();
            h.set_entry_type(kind);
            h.set_mode(mode);
            h.set_size(data.len() as u64);
            tar.append_data(&mut h, name, data).unwrap();
        }
        fs::write(&archive, tar.into_inner().unwrap()).unwrap();
        let into = d.join("modes");
        fs::create_dir(&into).unwrap();
        assert_eq!(
            unpack_returned(&archive, 1 << 20, &into).unwrap(),
            (Vec::<String>::new(), 1)
        );
        assert_eq!(fs::read(into.join("ro/f")).unwrap(), b"f");
        fs::remove_dir_all(&into).unwrap();
    }

    #[test]
    fn artifacts_skip_links_and_stay_inside_the_work_directory() {
        let dir = tempfile::tempdir().unwrap();
        let work = dir.path().join("work");
        fs::create_dir_all(work.join("out/sub")).unwrap();
        fs::write(work.join("out/report.txt"), "ok").unwrap();
        fs::write(work.join("out/sub/x.txt"), "x").unwrap();
        fs::write(dir.path().join("secret.txt"), "secret").unwrap();
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(dir.path().join("secret.txt"), work.join("out/leak.txt"))
                .unwrap();
            std::os::unix::fs::symlink(dir.path(), work.join("outside")).unwrap();
        }
        let def = ArtifactDef {
            name: "report".into(),
            paths: vec!["out/".into(), "outside/secret.txt".into()],
            junit: None,
        };
        let mut budget = 1 << 20;
        let mut notes = Vec::new();
        let packed = pack(&work, &def, &mut budget, &mut notes).unwrap().unwrap();
        let names = super::super::zip::names(&packed.data);
        assert_eq!(names, vec!["out/report.txt", "out/sub/x.txt"]);
        #[cfg(unix)]
        {
            assert!(
                notes.iter().any(|n| n.contains("out/leak.txt")),
                "{notes:?}"
            );
            assert!(
                notes.iter().any(|n| n.contains("outside/secret.txt")),
                "{notes:?}"
            );
        }
        let mut tiny = 1;
        assert!(pack(&work, &def, &mut tiny, &mut notes).unwrap().is_none());
    }

    #[test]
    fn cache_round_trip_and_eviction() {
        const IMAGE: &str =
            "sha256:1111111111111111111111111111111111111111111111111111111111111111";
        let dir = tempfile::tempdir().unwrap();
        let work = dir.path().join("work");
        let root = dir.path().join("cache");
        fs::create_dir_all(work.join("target")).unwrap();
        fs::write(work.join("Cargo.lock"), "v1").unwrap();
        fs::write(work.join("target/a.o"), "object").unwrap();
        let job = JobDef {
            verified_sha: false,
            paths: Vec::new(),
            name: "t".into(),
            image: "alpine".into(),
            commands: vec!["true".into()],
            timeout_secs: None,
            tmpfs_mib: None,
            ram_workspace: None,
            artifacts: vec![],
            cache: None,
        };
        let cache = CacheDef {
            paths: vec!["target".into()],
            key_files: vec!["Cargo.lock".into()],
        };
        let k1 = cache_key("acme/demo", &job, IMAGE, &cache, 2048, &work);
        assert_ne!(
            k1,
            cache_key(
                "acme/demo",
                &job,
                &IMAGE.replace('1', "2"),
                &cache,
                2048,
                &work
            ),
            "the image's digest is in the key"
        );
        assert_ne!(
            k1,
            cache_key("acme/demo", &job, IMAGE, &cache, 1024, &work),
            "the work quota is in the key"
        );
        let archive = packed(dir.path(), b"object");
        Cache::new(root.clone())
            .save_taken(&k1, &owner("acme/demo", &job.name), &archive, 1 << 20)
            .unwrap();
        // Staging moves the original image out of the attempt directory.
        // Reusing that path does not affect the published image.
        assert!(!archive.exists());
        fs::write(&archive, "changed").unwrap();
        let fresh = dir.path().join("fresh");
        fs::create_dir_all(&fresh).unwrap();
        fs::write(fresh.join("Cargo.lock"), "v1").unwrap();
        assert_eq!(
            cache_key("acme/demo", &job, IMAGE, &cache, 2048, &fresh),
            k1
        );
        for _ in 0..2 {
            assert_eq!(
                Cache::new(root.clone())
                    .restore(&k1, &fresh.join("cache.raw"))
                    .unwrap(),
                Some(taken(dir.path(), b"object"))
            );
            assert_eq!(
                fs::read_to_string(fresh.join("cache.raw")).unwrap(),
                "object"
            );
            fs::remove_file(fresh.join("cache.raw")).unwrap();
        }
        fs::write(fresh.join("Cargo.lock"), "v2").unwrap();
        // What the saving VM measured its file system holds is what a
        // restore says, whatever the image takes.
        Cache::new(root.clone())
            .save(
                &k1,
                &owner("acme/demo", &job.name),
                (&packed(dir.path(), b"object"), 200 << 20),
                1 << 30,
            )
            .unwrap();
        assert_eq!(
            Cache::new(root.clone())
                .restore(&k1, &fresh.join("cache.raw"))
                .unwrap(),
            Some(200 << 20)
        );
        fs::remove_file(fresh.join("cache.raw")).unwrap();
        let k2 = cache_key("acme/demo", &job, IMAGE, &cache, 2048, &fresh);
        assert_ne!(k1, k2, "key files are in the key");
        // A limit that fits only one entry keeps the newest.
        Cache::new(root.clone())
            .save_taken(
                &k2,
                &owner("acme/demo", &job.name),
                &packed(dir.path(), b"object"),
                tree_size(&root.join(&k1)),
            )
            .unwrap();
        assert!(!root.join(&k1).exists());
        assert!(root.join(&k2).exists());
    }

    #[test]
    fn cache_eviction_removes_superseded_generations_before_other_repositories_and_jobs() {
        for other in [owner("r2", "build"), owner("r1", "tests")] {
            let dir = tempfile::tempdir().unwrap();
            let d = dir.path();
            let store = Cache::new(d.join("cache"));
            let first = owner("r1", "build");
            let time = |n| SystemTime::UNIX_EPOCH + Duration::from_secs(n);
            store
                .save_taken_with_clock("k1", &first, &packed(d, b"object"), u64::MAX, || time(1))
                .unwrap();
            store
                .save_taken_with_clock("other", &other, &packed(d, b"object"), u64::MAX, || time(2))
                .unwrap();
            // Restoring an old generation makes it newer by use than the other
            // group's only entry. Plain LRU would evict that current entry.
            record_use(&store.root.join("k1"), time(3)).unwrap();
            let budget = entry_size(&store.root.join("k1")) + entry_size(&store.root.join("other"));
            store
                .save_taken_with_clock("k2", &first, &packed(d, b"object"), budget, || time(4))
                .unwrap();
            assert!(!store.root.join("k1").exists());
            assert!(store.root.join("other").exists());
            assert!(store.root.join("k2").exists());
        }
    }

    #[test]
    fn cache_save_retains_and_restores_old_generations_when_they_fit() {
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path();
        let store = Cache::new(d.join("cache"));
        let owner = owner("r", "j");
        store
            .save_taken("old", &owner, &packed(d, b"old"), u64::MAX)
            .unwrap();
        store
            .save_taken("new", &owner, &packed(d, b"new"), u64::MAX)
            .unwrap();
        assert!(store.root.join("new").exists());
        assert_eq!(
            store.restore("old", &d.join("old.raw")).unwrap(),
            Some(taken(d, b"old"))
        );
        assert_eq!(fs::read(d.join("old.raw")).unwrap(), b"old");
    }

    #[test]
    fn a_delayed_save_does_not_evict_a_newer_generation() {
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path();
        let store = Cache::new(d.join("cache"));
        let owner = owner("r", "j");
        let time = |n| SystemTime::UNIX_EPOCH + Duration::from_secs(n);
        store
            .save_taken_with_clock("later", &owner, &packed(d, b"object"), u64::MAX, || {
                time(20)
            })
            .unwrap();
        let budget = entry_size(&store.root.join("later"));
        store
            .save_taken_with_clock("saved", &owner, &packed(d, b"object"), budget, || time(10))
            .unwrap();
        assert!(store.root.join("later").exists());
        assert!(!store.root.join("saved").exists());
    }

    #[test]
    fn cache_eviction_selects_other_groups_by_save_time_then_key() {
        for (a_saved, b_saved, current) in [
            (Some(3), Some(2), "a"),
            (Some(2), Some(3), "b"),
            (Some(2), Some(2), "b"),
            (None, Some(2), "b"),
            (Some(2), None, "a"),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path();
            for (key, saved) in [("a", a_saved), ("b", b_saved), ("saved", Some(1))] {
                let entry = root.join(key);
                fs::create_dir(&entry).unwrap();
                fs::write(entry.join("data"), b"object").unwrap();
                mark_owner(&entry, "r", if key == "saved" { "other" } else { "j" });
                if let Some(n) = saved {
                    mark_saved(&entry, SystemTime::UNIX_EPOCH + Duration::from_secs(n));
                }
                // The superseded entry has the most recent use; save time
                // and key order, rather than use, must select the current one.
                record_use(
                    &entry,
                    SystemTime::UNIX_EPOCH
                        + Duration::from_secs(if key == current { 1 } else { 10 }),
                )
                .unwrap();
            }
            let superseded = if current == "a" { "b" } else { "a" };
            let budget = tree_size(&root.join(current)) + tree_size(&root.join("saved"));
            evict(root, budget, Some(&root.join("saved"))).unwrap();
            assert!(root.join(current).exists(), "current: {current}");
            assert!(!root.join(superseded).exists());
            assert!(root.join("saved").exists());
        }
    }

    #[test]
    fn cache_eviction_orders_superseded_entries_by_use_then_key() {
        for tied in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path();
            // No save timestamps: all are superseded even though each has
            // a different owner. Size never breaks timestamp ties.
            for (key, size, seconds) in [("a", 4, 3), ("b", 1, 2), ("c", 2, 1)] {
                let entry = root.join(key);
                fs::create_dir(&entry).unwrap();
                fs::write(entry.join("data"), vec![0; size]).unwrap();
                mark_owner(&entry, "r", key);
                record_use(
                    &entry,
                    SystemTime::UNIX_EPOCH + Duration::from_secs(if tied { 1 } else { seconds }),
                )
                .unwrap();
            }
            let total: u64 = ["a", "b", "c"]
                .iter()
                .map(|k| tree_size(&root.join(k)))
                .sum();
            evict(root, total - 1, Some(&root.join("none"))).unwrap();
            for key in ["a", "b", "c"] {
                assert_eq!(root.join(key).exists(), key != if tied { "a" } else { "c" });
            }
        }
    }

    #[test]
    fn cache_save_removes_ungroupable_entries_and_continues_enforcing_the_budget() {
        for bad_owner in [
            "missing",
            "empty",
            "malformed",
            "missing-job",
            "wrong-type",
            "directory",
        ] {
            for budget in [u64::MAX, 0] {
                let dir = tempfile::tempdir().unwrap();
                let root = dir.path().join("cache");
                let invalid = root.join("invalid");
                fs::create_dir_all(&invalid).unwrap();
                fs::write(invalid.join(IMAGE), b"old").unwrap();
                mark_saved(&invalid, SystemTime::now());
                match bad_owner {
                    "missing" => {}
                    "directory" => fs::create_dir(invalid.join(OWNER)).unwrap(),
                    _ => fs::write(
                        invalid.join(OWNER),
                        match bad_owner {
                            "empty" => "",
                            "malformed" => "{",
                            "missing-job" => r#"{"repo":"r"}"#,
                            "wrong-type" => r#"{"repo":"r","job":1}"#,
                            _ => unreachable!(),
                        },
                    )
                    .unwrap(),
                }
                let store = Cache::new(root.clone());
                // The invalid entry must not poison this or a later save.
                for key in ["new", "next"] {
                    assert_eq!(
                        store
                            .save_taken(
                                key,
                                &owner("r", "j"),
                                &packed(dir.path(), b"object"),
                                budget
                            )
                            .unwrap(),
                        taken(dir.path(), b"object"),
                        "owner: {bad_owner}, budget: {budget}"
                    );
                    assert!(!invalid.exists(), "owner: {bad_owner}, budget: {budget}");
                    let restored = dir.path().join(format!("restore-{key}.raw"));
                    assert_eq!(
                        store.restore(key, &restored).unwrap(),
                        (budget != 0).then(|| taken(dir.path(), b"object"))
                    );
                    if budget != 0 {
                        assert_eq!(fs::read(&restored).unwrap(), b"object");
                    }
                }
            }
        }
    }

    #[test]
    fn cache_eviction_falls_back_to_current_lru_after_removing_all_superseded_entries() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        for (key, job, used) in [("old", "j", 3), ("new", "j", 4), ("other", "other", 1)] {
            let entry = root.join(key);
            fs::create_dir(&entry).unwrap();
            fs::write(entry.join("data"), b"object").unwrap();
            mark_owner(&entry, "r", job);
            mark_saved(
                &entry,
                SystemTime::UNIX_EPOCH + Duration::from_secs(u64::from(key == "new")),
            );
            record_use(&entry, SystemTime::UNIX_EPOCH + Duration::from_secs(used)).unwrap();
        }
        evict(root, tree_size(&root.join("new")), Some(&root.join("new"))).unwrap();
        assert!(!root.join("old").exists());
        assert!(!root.join("other").exists());
        assert!(root.join("new").exists());
    }

    #[test]
    fn cache_eviction_breaks_timestamp_ties_by_key_not_size() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let used = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000);
        for (key, size) in [("a", 4), ("b", 1), ("c", 2)] {
            let entry = root.join(key);
            fs::create_dir(&entry).unwrap();
            fs::write(entry.join("data"), vec![0; size]).unwrap();
            // Distinct jobs: no superseded generations, so eviction is LRU.
            mark_owner(&entry, "r", key);
            mark_saved(&entry, SystemTime::UNIX_EPOCH);
            fs::File::create(entry.join(USED))
                .unwrap()
                .set_modified(used)
                .unwrap();
        }
        evict(
            root,
            tree_size(&root.join("a")) + tree_size(&root.join("b")) + tree_size(&root.join("c"))
                - 1,
            Some(&root.join("c")),
        )
        .unwrap();
        assert!(!root.join("a").exists(), "equal timestamps evict by key");
        assert!(root.join("b").exists());
        assert!(root.join("c").exists());
    }

    #[test]
    fn cache_save_after_restore_keeps_the_save_when_timestamps_tie() {
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path();
        let root = d.join("cache");
        Cache::new(root.clone())
            .save_taken("z-old", &owner("r", "old"), &packed(d, b"object"), u64::MAX)
            .unwrap();
        assert_eq!(
            Cache::new(root.clone())
                .restore("z-old", &d.join("restored.raw"))
                .unwrap(),
            Some(taken(d, b"object"))
        );
        // Fix the restored entry's use time so the test never depends on
        // how far the wall clock advances between restore and save.
        let restored_at = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000);
        record_use(&root.join("z-old"), restored_at).unwrap();

        // Simulate a coarse clock: the save has exactly the restore's
        // timestamp, and its key sorts before the restored entry's key.
        assert_eq!(
            Cache::new(root.clone())
                .save_taken_with_clock(
                    "a-new",
                    &owner("r", "new"),
                    &packed(d, b"object"),
                    entry_size(&root.join("z-old")),
                    || restored_at
                )
                .unwrap(),
            taken(d, b"object")
        );
        assert!(!root.join("z-old").exists());
        assert_eq!(fs::read(root.join("a-new").join(IMAGE)).unwrap(), b"object");
        assert_eq!(
            fs::metadata(root.join("a-new").join(USED))
                .unwrap()
                .modified()
                .unwrap(),
            restored_at,
            "save explicitly records the same clock as restore"
        );

        // The saved entry is still evicted when it cannot fit the budget.
        evict(&root, 5, Some(&root.join("a-new"))).unwrap();
        assert!(!root.join("a-new").exists());
    }

    #[test]
    fn cache_eviction_orders_distinct_timestamps_before_keys_and_size() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        for (key, size, seconds) in [("a", 1, 3), ("b", 2, 2), ("c", 4, 1)] {
            let entry = root.join(key);
            fs::create_dir(&entry).unwrap();
            fs::write(entry.join("data"), vec![0; size]).unwrap();
            // Distinct jobs: no superseded generations, so eviction is LRU.
            mark_owner(&entry, "r", key);
            mark_saved(&entry, SystemTime::UNIX_EPOCH);
            record_use(
                &entry,
                SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(seconds),
            )
            .unwrap();
        }
        evict(
            root,
            tree_size(&root.join("a")) + tree_size(&root.join("b")) + tree_size(&root.join("c"))
                - 1,
            Some(&root.join("c")),
        )
        .unwrap();
        assert!(root.join("a").exists());
        assert!(root.join("b").exists());
        assert!(
            !root.join("c").exists(),
            "timestamp precedes save preference"
        );
    }

    /// The jobs a runner runs at once share its cache: a restore while a
    /// save replaces the same entry gets the old archive or the new one,
    /// whole, and keeps it when a later save replaces the entry; saves of one
    /// key leave no temporary directory behind.
    #[test]
    fn concurrent_restores_and_saves_of_one_key_see_whole_entries() {
        let dir = tempfile::tempdir().unwrap();
        let cache = std::sync::Arc::new(Cache::new(dir.path().join("cache")));
        let saves: Vec<_> = (0..4)
            .map(|n| {
                let (cache, root) = (cache.clone(), dir.path().to_path_buf());
                std::thread::spawn(move || {
                    for _ in 0..10 {
                        let archive = packed(&root, format!("v{n}").repeat(1000).as_bytes());
                        cache
                            .save_taken("k", &owner("r", "j"), &archive, 1 << 30)
                            .unwrap();
                    }
                })
            })
            .collect();
        let restores: Vec<_> = (0..4)
            .map(|n| {
                let (cache, root) = (cache.clone(), dir.path().to_path_buf());
                std::thread::spawn(move || {
                    let mut kept = Vec::new();
                    for i in 0..20 {
                        let into = root.join(format!("fresh{n}-{i}.raw"));
                        if cache.restore("k", &into).unwrap().is_some() {
                            kept.push(into);
                        }
                    }
                    for into in kept {
                        let content = fs::read_to_string(&into).unwrap();
                        let one = content[..2].repeat(1000);
                        assert_eq!(content, one, "{into:?}");
                    }
                })
            })
            .collect();
        for t in saves.into_iter().chain(restores) {
            t.join().unwrap();
        }
        let names: Vec<_> = fs::read_dir(dir.path().join("cache"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, vec!["k".to_string()]);
    }
}
