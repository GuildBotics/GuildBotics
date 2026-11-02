//! The per-job work directory on the runner's machine: checking out the
//! source, collecting artifacts, and the dependency cache. Everything inside
//! the work directory was written by the job, so nothing here follows a
//! symbolic link: links are skipped (artifacts) or copied as links (cache).

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::RwLock;
use std::time::{Duration, SystemTime};

use crate::ci::pipeline::{ArtifactDef, CacheDef, JobDef, check_relative};

use super::zip::ZipWriter;

/// Checks the commit out of its Git bundle (`GET …/source`) into the empty
/// directory `work`: a clone detached at the bundle's `HEAD`, with its
/// history and tags, and then without the remote that names the bundle. Git
/// runs with `global_config` (written empty) and writes no reflog, so nothing
/// of this machine (a path, its user) reaches the job. Git never runs in the
/// work directory once the job has had it.
pub async fn checkout(bundle: &Path, work: &Path, global_config: &Path) -> io::Result<()> {
    fs::write(global_config, "")?;
    let mut clone = crate::git::base_command(global_config);
    clone
        // Inside the VM the files have another owner and other inodes than
        // the index records: comparing only times, sizes, and modes keeps
        // the index clean there (`git diff-index --quiet HEAD`).
        .args([
            "-c",
            "core.logAllRefUpdates=false",
            "clone",
            "--quiet",
            "-c",
            "core.checkStat=minimal",
            "--",
        ])
        .arg(bundle)
        .arg(work);
    let mut unlink = crate::git::base_command(global_config);
    unlink
        .arg("-C")
        .arg(work)
        .args(["remote", "remove", "origin"]);
    for mut git in [clone, unlink] {
        let out = git
            .stdout(std::process::Stdio::null())
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
    }
    Ok(())
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
/// image it runs on (a new version of a tag does not reuse what the previous
/// one built), the user it runs as (the files keep the owner the VM saw),
/// cached paths, and the content of the key files.
pub fn cache_key(
    repo: &str,
    job: &JobDef,
    image_digest: &str,
    cache: &CacheDef,
    work: &Path,
) -> String {
    let user = super::executor::JOB_USER_ID.to_string();
    let mut h = Vec::new();
    for part in [repo, &job.name, image_digest, &user] {
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

/// Copies a tree without following symbolic links (links are recreated as
/// links on Unix and skipped elsewhere). Returns the bytes copied.
pub fn copy_tree(from: &Path, to: &Path) -> io::Result<u64> {
    let m = fs::symlink_metadata(from)?;
    // Never write through a link at the destination (the work directory's
    // content comes from the commit and the job): replace the link itself.
    if fs::symlink_metadata(to).is_ok_and(|d| d.file_type().is_symlink()) {
        fs::remove_file(to)?;
    }
    if m.file_type().is_symlink() {
        #[cfg(unix)]
        {
            let target = fs::read_link(from)?;
            let _ = fs::remove_file(to);
            std::os::unix::fs::symlink(target, to)?;
        }
        return Ok(0);
    }
    if m.is_dir() {
        fs::create_dir_all(to)?;
        let mut total = 0;
        for e in fs::read_dir(from)?.flatten() {
            total += copy_tree(&e.path(), &to.join(e.file_name()))?;
        }
        Ok(total)
    } else if m.is_file() {
        if let Some(parent) = to.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(from, to)
    } else {
        Ok(0)
    }
}

fn tree_size(path: &Path) -> u64 {
    let Ok(m) = fs::symlink_metadata(path) else {
        return 0;
    };
    if m.is_dir() {
        fs::read_dir(path)
            .map(|rd| rd.flatten().map(|e| tree_size(&e.path())).sum())
            .unwrap_or(0)
    } else {
        m.len()
    }
}

const USED: &str = ".branvil-used";
const SAVED: &str = ".branvil-saved";
const OWNER: &str = ".branvil-owner";
pub const CACHE_REFRESH_SECS: u64 = 24 * 60 * 60;

/// The repository and job whose cache generations share an eviction priority.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
pub struct CacheOwner {
    pub repo: String,
    pub job: String,
}

fn record_use(entry: &Path, used: SystemTime) -> io::Result<()> {
    fs::File::create(entry.join(USED))?.set_modified(used)
}

/// Creates the missing parent directories of `rel` below `root`, refusing a
/// symbolic link anywhere on the way.
fn make_parents(root: &Path, rel: &str) -> io::Result<()> {
    let mut p = root.to_path_buf();
    let parts: Vec<&str> = rel.split('/').collect();
    for c in &parts[..parts.len().saturating_sub(1)] {
        p.push(c);
        match fs::symlink_metadata(&p) {
            Ok(m) if m.file_type().is_symlink() => {
                return Err(io::Error::other(format!(
                    "{rel} goes through a symbolic link"
                )));
            }
            Ok(_) => {}
            Err(_) => fs::create_dir(&p)?,
        }
    }
    Ok(())
}

/// The runner's dependency cache. The jobs a runner runs at once share it:
/// a save replaces its entry and evicts others only while no restore copies
/// from the cache, so a restore copies an entry whole, the old one or the new
/// one. One runner uses a work directory (`runner.lock`), so a lock of the
/// process is enough.
pub struct Cache {
    root: PathBuf,
    lock: RwLock<()>,
}

impl Cache {
    /// Age of the last complete save, independent of reads. Older entries
    /// without a save marker need a fresh execution before they can be reused.
    pub fn saved_age(&self, key: &str, now: SystemTime) -> io::Result<Option<Duration>> {
        let _reading = self.lock.read().unwrap_or_else(|e| e.into_inner());
        match fs::metadata(self.root.join(key).join(SAVED)) {
            Ok(meta) => Ok(now.duration_since(meta.modified()?).ok()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            lock: RwLock::new(()),
        }
    }

    /// Restores the entry of `key` into the work directory. Returns the
    /// bytes copied, or none when there is no such entry.
    pub fn restore(&self, key: &str, cache: &CacheDef, work: &Path) -> io::Result<Option<u64>> {
        let _reading = self.lock.read().unwrap_or_else(|e| e.into_inner());
        let entry = self.root.join(key);
        if !entry.is_dir() {
            return Ok(None);
        }
        let mut total = 0;
        for p in &cache.paths {
            let src = entry.join("paths").join(p.trim_end_matches('/'));
            if fs::symlink_metadata(&src).is_ok() {
                let rel = p.trim_end_matches('/');
                make_parents(work, rel)?;
                total += copy_tree(&src, &work.join(rel))?;
            }
        }
        record_use(&entry, SystemTime::now())?;
        Ok(Some(total))
    }

    /// Saves the cached paths of a successful job under `key`, then removes
    /// superseded generations before current entries beyond `max_bytes`.
    pub fn save(
        &self,
        key: &str,
        owner: &CacheOwner,
        cache: &CacheDef,
        work: &Path,
        max_bytes: u64,
    ) -> io::Result<u64> {
        self.save_with_clock(key, owner, cache, work, max_bytes, SystemTime::now)
    }

    fn save_with_clock(
        &self,
        key: &str,
        owner: &CacheOwner,
        cache: &CacheDef,
        work: &Path,
        max_bytes: u64,
        now: impl FnOnce() -> SystemTime,
    ) -> io::Result<u64> {
        fs::create_dir_all(&self.root)?;
        // Copied aside without the lock (the copy is the long part), then
        // put in place under it.
        let tmp = self
            .root
            .join(format!(".{key}.{}", crate::util::random_secret::<6>()));
        let saved = (|| {
            let mut total = 0;
            for p in &cache.paths {
                if let Ok(src) = no_links_below(work, p) {
                    total += copy_tree(&src, &tmp.join("paths").join(p.trim_end_matches('/')))?;
                }
            }
            fs::create_dir_all(&tmp)?;
            fs::write(tmp.join(OWNER), serde_json::to_vec(owner)?)?;
            let saved_at = now();
            record_use(&tmp, saved_at)?;
            fs::File::create(tmp.join(SAVED))?.set_modified(saved_at)?;
            let _writing = self.lock.write().unwrap_or_else(|e| e.into_inner());
            let entry = self.root.join(key);
            let _ = fs::remove_dir_all(&entry);
            fs::rename(&tmp, &entry)?;
            evict(&self.root, max_bytes, &entry)?;
            Ok(total)
        })();
        if saved.is_err() {
            let _ = fs::remove_dir_all(&tmp);
        }
        saved
    }
}

fn evict(cache_root: &Path, max_bytes: u64, saved_entry: &Path) -> io::Result<()> {
    struct Entry {
        owner: CacheOwner,
        used: SystemTime,
        size: u64,
        path: PathBuf,
    }
    let mut entries = Vec::new();
    let mut current = BTreeMap::new();
    for e in fs::read_dir(cache_root)?.flatten() {
        let path = e.path();
        if e.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        let Some(owner) = fs::read(path.join(OWNER))
            .ok()
            .and_then(|data| serde_json::from_slice::<CacheOwner>(&data).ok())
        else {
            // Missing, unreadable, or invalid records cannot be grouped.
            // Remove the entry even below budget so it cannot stall eviction.
            fs::remove_dir_all(&path)?;
            continue;
        };
        if let Ok(saved) = fs::metadata(path.join(SAVED)).and_then(|m| m.modified()) {
            // saved_at is recorded before the write lock: the save that just
            // acquired it wins over even a later timestamp in its group.
            let candidate = (path == saved_entry, saved, path.clone());
            let active = current.entry(owner.clone()).or_insert(candidate.clone());
            if candidate > *active {
                *active = candidate;
            }
        }
        let used = fs::metadata(path.join(USED))
            .and_then(|m| m.modified())
            .unwrap_or(SystemTime::UNIX_EPOCH);
        entries.push(Entry {
            owner,
            used,
            size: tree_size(&path),
            path,
        });
    }
    let is_current = |e: &Entry| current.get(&e.owner).is_some_and(|v| v.2 == e.path);
    // Exhaust superseded generations before evicting any current entry.
    // A save is the latest use when timestamps tie (e.g. on a coarse
    // filesystem). Other ties are broken by key, independently of size.
    entries.sort_by(|a, b| {
        (is_current(a), a.used, a.path == saved_entry, &a.path).cmp(&(
            is_current(b),
            b.used,
            b.path == saved_entry,
            &b.path,
        ))
    });
    let mut total: u64 = entries.iter().map(|e| e.size).sum();
    for entry in entries {
        if total <= max_bytes {
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

    fn owner(repo: &str, job: &str) -> CacheOwner {
        CacheOwner {
            repo: repo.into(),
            job: job.into(),
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

    #[test]
    fn cache_save_age_tracks_complete_saves_and_survives_restore_and_eviction() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path().join("cache"));
        let work = dir.path().join("work");
        fs::create_dir_all(work.join("target")).unwrap();
        fs::write(work.join("target/file"), "content").unwrap();
        let def = CacheDef {
            paths: vec!["target".into()],
            key_files: vec![],
        };
        let saved = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        assert_eq!(cache.saved_age("k", saved).unwrap(), None);
        cache
            .save_with_clock("k", &owner("r", "j"), &def, &work, 1 << 20, || saved)
            .unwrap();
        let now = saved + Duration::from_secs(CACHE_REFRESH_SECS);
        assert_eq!(
            cache.saved_age("k", now).unwrap(),
            Some(Duration::from_secs(CACHE_REFRESH_SECS))
        );
        let restored = dir.path().join("restored");
        fs::create_dir(&restored).unwrap();
        cache.restore("k", &def, &restored).unwrap();
        assert_eq!(
            cache.saved_age("k", now).unwrap(),
            Some(Duration::from_secs(CACHE_REFRESH_SECS))
        );
        assert_eq!(
            cache
                .saved_age("k", saved - Duration::from_secs(1))
                .unwrap(),
            None,
            "a future timestamp is not a fresh save"
        );
        // A cache written before save timestamps were recorded needs a new save.
        fs::remove_file(cache.root.join("k").join(SAVED)).unwrap();
        assert_eq!(cache.saved_age("k", now).unwrap(), None);
        cache
            .save_with_clock("k", &owner("r", "j"), &def, &work, 0, || now)
            .unwrap();
        assert_eq!(
            cache.saved_age("k", now).unwrap(),
            None,
            "evicted entries have no save"
        );
    }

    /// The machine's Git attributes never change the checkout. Git finds
    /// them through the environment, so the checkout runs in a child test
    /// process whose `XDG_CONFIG_HOME` and `HOME` hold `* text eol=crlf`.
    #[tokio::test]
    async fn the_checkout_ignores_the_machines_git_attributes() {
        if std::env::var_os("BRANVIL_TEST_CHILD").is_some() {
            let dir = PathBuf::from(std::env::var_os("BRANVIL_TEST_DIR").unwrap());
            let work = dir.join("work");
            checkout(&dir.join("b.bundle"), &work, &dir.join("gitconfig"))
                .await
                .unwrap();
            assert_eq!(fs::read(work.join("f")).unwrap(), b"a\n");
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
                ("BRANVIL_TEST_DIR", d.as_os_str()),
                ("XDG_CONFIG_HOME", d.join("xdg").as_os_str()),
                ("HOME", d.join("home").as_os_str()),
            ],
        );
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
            paths: Vec::new(),
            name: "t".into(),
            image: "alpine".into(),
            commands: vec!["true".into()],
            timeout_secs: None,
            artifacts: vec![],
            cache: None,
        };
        let cache = CacheDef {
            paths: vec!["target".into()],
            key_files: vec!["Cargo.lock".into()],
        };
        let k1 = cache_key("acme/demo", &job, IMAGE, &cache, &work);
        assert_ne!(
            k1,
            cache_key("acme/demo", &job, &IMAGE.replace('1', "2"), &cache, &work),
            "the image's digest is in the key"
        );
        Cache::new(root.clone())
            .save(&k1, &owner("acme/demo", &job.name), &cache, &work, 1 << 20)
            .unwrap();
        let fresh = dir.path().join("fresh");
        fs::create_dir_all(&fresh).unwrap();
        fs::write(fresh.join("Cargo.lock"), "v1").unwrap();
        assert_eq!(cache_key("acme/demo", &job, IMAGE, &cache, &fresh), k1);
        assert_eq!(
            Cache::new(root.clone())
                .restore(&k1, &cache, &fresh)
                .unwrap(),
            Some(6)
        );
        assert_eq!(
            fs::read_to_string(fresh.join("target/a.o")).unwrap(),
            "object"
        );
        fs::write(fresh.join("Cargo.lock"), "v2").unwrap();
        let k2 = cache_key("acme/demo", &job, IMAGE, &cache, &fresh);
        assert_ne!(k1, k2, "key files are in the key");
        // A limit that fits only one entry keeps the newest.
        Cache::new(root.clone())
            .save(
                &k2,
                &owner("acme/demo", &job.name),
                &cache,
                &fresh,
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
            let store = Cache::new(dir.path().join("cache"));
            let work = dir.path().join("work");
            fs::create_dir_all(work.join("target")).unwrap();
            fs::write(work.join("target/file"), b"object").unwrap();
            let def = CacheDef {
                paths: vec!["target".into()],
                key_files: vec![],
            };
            let first = owner("r1", "build");
            let time = |n| SystemTime::UNIX_EPOCH + Duration::from_secs(n);
            store
                .save_with_clock("k1", &first, &def, &work, u64::MAX, || time(1))
                .unwrap();
            store
                .save_with_clock("other", &other, &def, &work, u64::MAX, || time(2))
                .unwrap();
            // Restoring an old generation makes it newer by use than the other
            // group's only entry. Plain LRU would evict that current entry.
            record_use(&store.root.join("k1"), time(3)).unwrap();
            let budget = tree_size(&store.root.join("k1")) + tree_size(&store.root.join("other"));
            store
                .save_with_clock("k2", &first, &def, &work, budget, || time(4))
                .unwrap();
            assert!(!store.root.join("k1").exists());
            assert!(store.root.join("other").exists());
            assert!(store.root.join("k2").exists());
        }
    }

    #[test]
    fn cache_save_retains_and_restores_old_generations_when_they_fit() {
        let dir = tempfile::tempdir().unwrap();
        let store = Cache::new(dir.path().join("cache"));
        let work = dir.path().join("work");
        fs::create_dir_all(work.join("target")).unwrap();
        let def = CacheDef {
            paths: vec!["target".into()],
            key_files: vec![],
        };
        let owner = owner("r", "j");
        fs::write(work.join("target/file"), b"old").unwrap();
        store.save("old", &owner, &def, &work, u64::MAX).unwrap();
        fs::write(work.join("target/file"), b"new").unwrap();
        store.save("new", &owner, &def, &work, u64::MAX).unwrap();
        assert!(store.root.join("new").exists());
        assert_eq!(store.restore("old", &def, &work).unwrap(), Some(3));
        assert_eq!(fs::read(work.join("target/file")).unwrap(), b"old");
    }

    #[test]
    fn cache_save_is_current_even_when_another_generation_has_a_later_save_time() {
        let dir = tempfile::tempdir().unwrap();
        let store = Cache::new(dir.path().join("cache"));
        let work = dir.path().join("work");
        fs::create_dir_all(work.join("target")).unwrap();
        fs::write(work.join("target/file"), b"object").unwrap();
        let def = CacheDef {
            paths: vec!["target".into()],
            key_files: vec![],
        };
        let owner = owner("r", "j");
        let time = |n| SystemTime::UNIX_EPOCH + Duration::from_secs(n);
        store
            .save_with_clock("later", &owner, &def, &work, u64::MAX, || time(20))
            .unwrap();
        let budget = tree_size(&store.root.join("later"));
        store
            .save_with_clock("saved", &owner, &def, &work, budget, || time(10))
            .unwrap();
        assert!(!store.root.join("later").exists());
        assert!(store.root.join("saved").exists());
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
            evict(root, budget, &root.join("saved")).unwrap();
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
            evict(root, total - 1, &root.join("none")).unwrap();
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
                fs::create_dir_all(invalid.join("paths")).unwrap();
                fs::write(invalid.join("paths/file"), b"old").unwrap();
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
                let work = dir.path().join("work");
                fs::create_dir_all(work.join("target")).unwrap();
                fs::write(work.join("target/object"), b"object").unwrap();
                let def = CacheDef {
                    paths: vec!["target".into()],
                    key_files: vec![],
                };
                let store = Cache::new(root.clone());
                // The invalid entry must not poison this or a later save.
                for key in ["new", "next"] {
                    assert_eq!(
                        store
                            .save(key, &owner("r", "j"), &def, &work, budget)
                            .unwrap(),
                        6,
                        "owner: {bad_owner}, budget: {budget}"
                    );
                    assert!(!invalid.exists(), "owner: {bad_owner}, budget: {budget}");
                    let restored = dir.path().join(format!("restore-{key}"));
                    assert_eq!(
                        store.restore(key, &def, &restored).unwrap(),
                        (budget != 0).then_some(6)
                    );
                    if budget != 0 {
                        assert_eq!(fs::read(restored.join("target/object")).unwrap(), b"object");
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
            mark_saved(&entry, SystemTime::UNIX_EPOCH);
            record_use(&entry, SystemTime::UNIX_EPOCH + Duration::from_secs(used)).unwrap();
        }
        evict(root, tree_size(&root.join("new")), &root.join("new")).unwrap();
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
            &root.join("c"),
        )
        .unwrap();
        assert!(!root.join("a").exists(), "equal timestamps evict by key");
        assert!(root.join("b").exists());
        assert!(root.join("c").exists());
    }

    #[test]
    fn cache_save_after_restore_keeps_the_save_when_timestamps_tie() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("cache");
        let work = dir.path().join("work");
        let fresh = dir.path().join("fresh");
        let cache = CacheDef {
            paths: vec!["target".into()],
            key_files: vec![],
        };
        fs::create_dir_all(work.join("target")).unwrap();
        fs::create_dir(&fresh).unwrap();
        fs::write(work.join("target/object"), b"object").unwrap();
        Cache::new(root.clone())
            .save("z-old", &owner("r", "old"), &cache, &work, 100)
            .unwrap();
        assert_eq!(
            Cache::new(root.clone())
                .restore("z-old", &cache, &fresh)
                .unwrap(),
            Some(6)
        );
        // Fix the restored entry's use time so the test never depends on
        // how far the wall clock advances between restore and save.
        let restored_at = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000);
        fs::File::open(root.join("z-old").join(USED))
            .unwrap()
            .set_modified(restored_at)
            .unwrap();

        // Simulate a coarse clock: the save has exactly the restore's
        // timestamp, and its key sorts before the restored entry's key.
        assert_eq!(
            Cache::new(root.clone())
                .save_with_clock(
                    "a-new",
                    &owner("r", "new"),
                    &cache,
                    &fresh,
                    tree_size(&root.join("z-old")),
                    || restored_at
                )
                .unwrap(),
            6
        );
        assert!(!root.join("z-old").exists());
        assert_eq!(
            fs::read(root.join("a-new/paths/target/object")).unwrap(),
            b"object"
        );
        assert_eq!(
            fs::metadata(root.join("a-new").join(USED))
                .unwrap()
                .modified()
                .unwrap(),
            restored_at,
            "save explicitly records the same clock as restore"
        );

        // The saved entry is still evicted when it cannot fit the budget.
        evict(&root, 5, &root.join("a-new")).unwrap();
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
            &root.join("c"),
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
    /// save replaces the same entry copies the old entry or the new one,
    /// whole, and saves of one key leave no temporary directory behind.
    #[test]
    fn concurrent_restores_and_saves_of_one_key_see_whole_entries() {
        let dir = tempfile::tempdir().unwrap();
        let cache = std::sync::Arc::new(Cache::new(dir.path().join("cache")));
        let def = CacheDef {
            paths: vec!["target".into()],
            key_files: vec![],
        };
        let saves: Vec<_> = (0..4)
            .map(|n| {
                let work = dir.path().join(format!("work{n}"));
                for f in 0..200 {
                    let file = work.join("target").join(format!("{f:03}"));
                    fs::create_dir_all(file.parent().unwrap()).unwrap();
                    fs::write(file, format!("v{n}")).unwrap();
                }
                let (cache, def) = (cache.clone(), def.clone());
                std::thread::spawn(move || {
                    for _ in 0..10 {
                        cache
                            .save("k", &owner("r", "j"), &def, &work, 1 << 30)
                            .unwrap();
                    }
                })
            })
            .collect();
        let restores: Vec<_> = (0..4)
            .map(|n| {
                let (cache, def, root) = (cache.clone(), def.clone(), dir.path().to_path_buf());
                std::thread::spawn(move || {
                    for i in 0..20 {
                        let fresh = root.join(format!("fresh{n}-{i}"));
                        fs::create_dir_all(&fresh).unwrap();
                        if cache.restore("k", &def, &fresh).unwrap().is_none() {
                            continue;
                        }
                        let versions: std::collections::BTreeSet<String> =
                            fs::read_dir(fresh.join("target"))
                                .unwrap()
                                .map(|e| fs::read_to_string(e.unwrap().path()).unwrap())
                                .collect();
                        let files = fs::read_dir(fresh.join("target")).unwrap().count();
                        assert_eq!((versions.len(), files), (1, 200), "{versions:?}");
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
