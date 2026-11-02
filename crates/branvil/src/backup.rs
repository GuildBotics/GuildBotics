//! Consistent backups of a running service and restores into a new data
//! directory (`docs/backup-restore.md`).
//!
//! A backup holds every repository lock (so no merge, push recording, sync
//! fetch, or repository creation runs), copies the database as of one read
//! transaction, and writes a `git bundle` of every repository. Pushes that Git
//! accepts while the locks are held may be in a bundle without being in the
//! database: that is the state of a push the service had not recorded yet,
//! and the restored service records it at startup like any other.

use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, bail};
use axum::extract::State;
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};

use crate::app::{App, Shared};
use crate::auth::Caller;
use crate::config::{Layout, restrict};
use crate::error::{AppError, AppResult};
use crate::git::Git;
use crate::http::J;
use crate::model::Repo;
use crate::store::{self, Rd, Store};
use crate::util;

pub const MANIFEST: &str = "manifest.json";
const FORMAT: u32 = 1;
const DB_FILE: &str = "branvil.redb";
const CONFIG_FILE: &str = "branvil.toml";
/// Limit of one `git bundle` or restore fetch.
const BUNDLE_TIMEOUT_SECS: u64 = 3600;

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Manifest {
    pub format: u32,
    pub service: String,
    pub branvil_version: String,
    pub schema_version: u64,
    pub git_version: String,
    pub created_at: String,
    pub base_url: String,
    /// Tables of the database copy and their entry counts.
    pub tables: BTreeMap<String, u64>,
    pub repositories: Vec<RepoEntry>,
    /// Every file of the backup except this manifest.
    pub files: Vec<FileEntry>,
    /// What is deliberately not in the backup.
    pub excluded: Vec<String>,
    /// Members' upstream accounts whose credential (a token or an app's
    /// key) must be set again after a restore.
    pub account_tokens_excluded: Vec<AccountEntry>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct RepoEntry {
    pub id: String,
    pub path: String,
    pub default_branch: String,
    /// `repos/<id>.bundle`, or none for a repository without refs.
    pub bundle: Option<String>,
    /// The refs the bundle carries.
    pub refs: BTreeMap<String, String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct FileEntry {
    /// Relative to the backup directory, `/`-separated.
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct AccountEntry {
    pub member: String,
    pub provider: String,
}

const EXCLUDED: &[&str] = &[
    "secrets/ (cursor signing key and owner token: the first start makes new ones; old list cursors and every GUI session end)",
    "secrets/accounts/ (members' upstream credentials: set them again after a restore)",
    "secrets/webhook.url (the webhook told of automatic publishes: set it again after a restore)",
    "hooks/ and git/ (written again at every start)",
    "Git reflogs and repository-local Git configuration (Branvil re-applies its own at start)",
];

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct BackupInput {
    /// Absolute path on the machine the service runs on; must not exist or be
    /// an empty directory.
    pub to: String,
}

/// `POST /api/v1/admin/backup` (owner).
pub async fn http_backup(
    State(app): State<Shared>,
    c: Caller,
    J(input): J<BackupInput>,
) -> AppResult<Response> {
    c.require_owner()?;
    let manifest = create(&app, Path::new(&input.to)).await?;
    Ok(axum::Json(manifest).into_response())
}

fn overlaps(a: &Path, b: &Path) -> bool {
    a.starts_with(b) || b.starts_with(a)
}

/// The canonical form of `path`, resolving the parts that exist.
fn resolve(path: &Path) -> PathBuf {
    let mut existing = path.to_path_buf();
    let mut rest = Vec::new();
    while !existing.exists() {
        match (existing.file_name(), existing.parent()) {
            (Some(name), Some(parent)) => {
                rest.push(name.to_os_string());
                existing = parent.to_path_buf();
            }
            _ => break,
        }
    }
    let mut out = existing.canonicalize().unwrap_or(existing);
    for part in rest.iter().rev() {
        out.push(part);
    }
    out
}

fn empty_or_missing(dir: &Path) -> std::io::Result<bool> {
    match std::fs::read_dir(dir) {
        Ok(mut entries) => Ok(entries.next().is_none()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(true),
        Err(e) => Err(e),
    }
}

/// Makes a backup of the running service at `to`.
pub async fn create(app: &App, to: &Path) -> AppResult<Manifest> {
    if !to.is_absolute() {
        return Err(AppError::invalid("The backup path must be absolute."));
    }
    let target = resolve(to);
    if overlaps(&target, &resolve(&app.layout.root)) {
        return Err(AppError::invalid(
            "The backup directory must be outside the data directory.",
        ));
    }
    if !empty_or_missing(&target).map_err(AppError::internal)? {
        return Err(AppError::conflict(format!(
            "{} exists and is not empty.",
            target.display()
        )));
    }
    std::fs::create_dir_all(target.join("repos")).map_err(AppError::internal)?;
    restrict(&target, true).map_err(AppError::internal)?;

    // No deletion from the database copy until the files are copied. Taken
    // first, so that a running deletion does not keep the repositories locked.
    let deletions = app.file_deletions.write().await;
    // Quiesce ref changes.
    let (repos, guards) = app.quiesce().await?;
    let tables = app.store.export_to(&target.join(DB_FILE)).await?;
    // Its own Git runner: Smart HTTP children hold the shared permits while
    // their post-receive waits for the locks held here.
    let git = Git::new(
        app.layout.clone(),
        1,
        std::time::Duration::from_secs(BUNDLE_TIMEOUT_SECS),
    );
    let mut entries = Vec::new();
    for repo in &repos {
        entries.push(bundle(&git, repo, &target).await?);
    }
    drop(guards);

    // CI logs and artifacts and tickets' attachments are written before they
    // are recorded, and retention and the attachment sweep delete them only
    // while `deletions` is not held, so copying them after the snapshot
    // keeps every reference.
    for dir in [
        app.layout.ci_logs(),
        app.layout.ci_artifacts(),
        app.layout.attachments(),
    ] {
        let rel = dir
            .strip_prefix(&app.layout.root)
            .map_err(AppError::internal)?
            .to_path_buf();
        copy_tree(&dir, &target.join(&rel)).map_err(AppError::internal)?;
    }
    drop(deletions);
    if app.layout.config_file().exists() {
        std::fs::copy(app.layout.config_file(), target.join(CONFIG_FILE))
            .map_err(AppError::internal)?;
    }
    let members: Vec<crate::model::Member> = app
        .store
        .read(|r| r.prefix_all(store::MEMBERS, &[]))
        .await?;
    let account_tokens_excluded = members
        .iter()
        .flat_map(|m| {
            m.accounts.iter().map(|a| AccountEntry {
                member: m.name.clone(),
                provider: a.provider.clone(),
            })
        })
        .collect();
    let root = target.clone();
    let files = tokio::task::spawn_blocking(move || checksum_tree(&root))
        .await
        .map_err(AppError::internal)?
        .map_err(AppError::internal)?;
    let manifest = Manifest {
        format: FORMAT,
        service: "branvil".into(),
        branvil_version: env!("CARGO_PKG_VERSION").into(),
        schema_version: store::SCHEMA_VERSION,
        git_version: app.git.version().await.unwrap_or_default(),
        created_at: util::now(),
        base_url: app.base_url.clone(),
        tables: tables.into_iter().collect(),
        repositories: entries,
        files,
        excluded: EXCLUDED.iter().map(|s| s.to_string()).collect(),
        account_tokens_excluded,
    };
    let text = serde_json::to_string_pretty(&manifest).map_err(AppError::internal)?;
    write_manifest(&target, &text).map_err(AppError::internal)?;
    tracing::info!(
        "backup written to {} ({} repositories)",
        target.display(),
        manifest.repositories.len()
    );
    Ok(manifest)
}

async fn bundle(git: &Git, repo: &Repo, target: &Path) -> AppResult<RepoEntry> {
    let dir = git.repo_dir(&repo.path);
    let mut entry = RepoEntry {
        id: repo.id.clone(),
        path: repo.path.clone(),
        default_branch: repo.default_branch.clone(),
        bundle: None,
        refs: BTreeMap::new(),
    };
    if git.all_refs(&dir).await?.is_empty() {
        return Ok(entry);
    }
    let rel = format!("repos/{}.bundle", repo.id);
    let file = target.join(&rel);
    let file_text = file.to_string_lossy().to_string();
    git.run_ok(Some(&dir), &["bundle", "create", &file_text, "--all"])
        .await?;
    // The refs as the bundle has them (pushes may land meanwhile).
    let heads = git
        .run_ok(Some(&dir), &["bundle", "list-heads", &file_text])
        .await?;
    for line in heads.lines() {
        if let Some((sha, name)) = line.split_once(' ')
            && name.starts_with("refs/")
        {
            entry.refs.insert(name.to_string(), sha.to_string());
        }
    }
    entry.bundle = Some(rel);
    Ok(entry)
}

/// Copies regular files and directories; symlinks are not followed or copied.
/// A file renamed or removed meanwhile (an upload finishing or failing) is
/// left out.
fn copy_tree(from: &Path, to: &Path) -> std::io::Result<()> {
    if !from.exists() {
        return Ok(());
    }
    std::fs::create_dir_all(to)?;
    for entry in std::fs::read_dir(from)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        let dest = to.join(entry.file_name());
        if kind.is_dir() {
            copy_tree(&entry.path(), &dest)?;
        } else if kind.is_file() {
            match std::fs::copy(entry.path(), &dest) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                copied => {
                    copied?;
                }
            }
        }
    }
    Ok(())
}

/// Writes the manifest last and under its name only once it is complete: a
/// backup with a manifest is a whole backup (scripts/update.sh counts those).
fn write_manifest(target: &Path, text: &str) -> std::io::Result<()> {
    let partial = target.join(format!("{MANIFEST}.partial"));
    let mut file = std::fs::File::create(&partial)?;
    std::io::Write::write_all(&mut file, text.as_bytes())?;
    file.sync_all()?;
    std::fs::rename(&partial, target.join(MANIFEST))
}

fn sha256_file(path: &Path) -> std::io::Result<(u64, String)> {
    use sha2::{Digest, Sha256};
    let mut f = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    let mut total = 0u64;
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        hasher.update(&buf[..n]);
    }
    Ok((total, util::hex(&hasher.finalize())))
}

fn checksum_tree(root: &Path) -> std::io::Result<Vec<FileEntry>> {
    fn walk(root: &Path, dir: &Path, out: &mut Vec<FileEntry>) -> std::io::Result<()> {
        let mut entries: Vec<_> = std::fs::read_dir(dir)?.collect::<Result<_, _>>()?;
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let path = entry.path();
            let kind = entry.file_type()?;
            if kind.is_dir() {
                walk(root, &path, out)?;
            } else if kind.is_file() {
                let rel = path.strip_prefix(root).unwrap_or(&path);
                let rel = rel
                    .components()
                    .map(|c| c.as_os_str().to_string_lossy().into_owned())
                    .collect::<Vec<_>>()
                    .join("/");
                if rel == MANIFEST {
                    continue;
                }
                let (bytes, sha256) = sha256_file(&path)?;
                out.push(FileEntry {
                    path: rel,
                    bytes,
                    sha256,
                });
            }
        }
        Ok(())
    }
    let mut out = Vec::new();
    walk(root, root, &mut out)?;
    Ok(out)
}

/// A manifest path as a relative path inside the backup, or an error for
/// anything that could leave it.
fn inside(root: &Path, rel: &str) -> anyhow::Result<PathBuf> {
    let p = Path::new(rel);
    if rel.is_empty()
        || p.is_absolute()
        || !p.components().all(|c| matches!(c, Component::Normal(_)))
    {
        bail!("the manifest names a path outside the backup: {rel}");
    }
    Ok(root.join(p))
}

// ---------------------------------------------------------------- restore

/// `branvil restore`: verifies the backup, fills a new data directory, and
/// checks the result. Returns the exit code (1 when the check finds problems).
pub async fn restore(from: &Path, data_dir: &Path) -> anyhow::Result<u8> {
    let text = std::fs::read_to_string(from.join(MANIFEST))
        .with_context(|| format!("reading {}", from.join(MANIFEST).display()))?;
    let manifest: Manifest = serde_json::from_str(&text).context("reading the manifest")?;
    if manifest.format != FORMAT || manifest.service != "branvil" {
        bail!(
            "{} is not a Branvil backup of format {FORMAT}",
            from.display()
        );
    }
    if ![store::SCHEMA_VERSION, crate::upgrade::FROM].contains(&manifest.schema_version) {
        bail!(
            "the backup has schema version {}; this build restores only version {} (and upgrades version {})",
            manifest.schema_version,
            store::SCHEMA_VERSION,
            crate::upgrade::FROM
        );
    }
    for f in &manifest.files {
        let path = inside(from, &f.path)?;
        let (bytes, sum) =
            sha256_file(&path).with_context(|| format!("reading {}", path.display()))?;
        if bytes != f.bytes || sum != f.sha256 {
            bail!(
                "{} does not match its checksum; the backup is damaged",
                f.path
            );
        }
    }
    println!(
        "backup verified: {} files, created {} by Branvil {}",
        manifest.files.len(),
        manifest.created_at,
        manifest.branvil_version
    );
    if !empty_or_missing(data_dir)? {
        bail!(
            "{} exists and is not empty; restore only into a new directory",
            data_dir.display()
        );
    }
    let layout = Layout::new(data_dir);
    layout.create()?;
    std::fs::copy(inside(from, DB_FILE)?, layout.db()).context("copying the database")?;
    if from.join(CONFIG_FILE).exists() {
        std::fs::copy(from.join(CONFIG_FILE), layout.config_file())?;
    }
    for dir in [
        layout.ci_logs(),
        layout.ci_artifacts(),
        layout.attachments(),
    ] {
        let rel = dir.strip_prefix(&layout.root)?.to_path_buf();
        copy_tree(&from.join(&rel), &dir)?;
    }
    let git = Git::new(
        layout.clone(),
        1,
        std::time::Duration::from_secs(BUNDLE_TIMEOUT_SECS),
    );
    for repo in &manifest.repositories {
        util::validate_path(&repo.path).map_err(|e| anyhow::anyhow!("{}", e.message))?;
        let dir = layout.repo_dir(&repo.path);
        git.init_bare(&dir, &repo.default_branch)
            .await
            .map_err(|e| anyhow::anyhow!("{}: {}", repo.path, e.message))?;
        if let Some(rel) = &repo.bundle {
            let file = inside(from, rel)?.to_string_lossy().to_string();
            git.run_ok(
                Some(&dir),
                &[
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--update-head-ok",
                    &file,
                    "+refs/*:refs/*",
                ],
            )
            .await
            .map_err(|e| anyhow::anyhow!("{}: {}", repo.path, e.message))?;
        }
        let refs = git
            .all_refs(&dir)
            .await
            .map_err(|e| anyhow::anyhow!("{}: {}", repo.path, e.message))?;
        if refs != repo.refs {
            bail!("{}: the restored refs differ from the manifest", repo.path);
        }
        println!("repository {}: {} refs", repo.path, refs.len());
    }
    if manifest.schema_version == crate::upgrade::FROM {
        let done = crate::upgrade::upgrade(&layout)?;
        println!("{}", store::upgrade_report(&layout.db(), &done));
    }
    for a in &manifest.account_tokens_excluded {
        println!(
            "warning: the {} account of {} had a credential, which backups do not carry; set it again (PUT .../members/{}/accounts/{})",
            a.provider, a.member, a.member, a.provider
        );
    }
    let problems = check(&layout, &manifest)?;
    if problems.is_empty() {
        let next = if store::needs_check(&layout.db())? {
            store::check_step(&layout.db())
        } else {
            format!(
                "`branvil serve --data-dir {}` starts it",
                data_dir.display()
            )
        };
        println!("restored into {}; {next}", data_dir.display());
        Ok(0)
    } else {
        for p in &problems {
            println!("problem: {p}");
        }
        Ok(1)
    }
}

/// The restore's own `admin check`: database structure and indexes, and a
/// repository directory for every repository record.
fn check(layout: &Layout, manifest: &Manifest) -> anyhow::Result<Vec<String>> {
    let store = Store::open(&layout.db(), 16 << 20, 1)?;
    let mut problems = store
        .read_sync(crate::model::index_problems)
        .map_err(|e| anyhow::anyhow!("{}", e.message))?;
    let repos: Vec<Repo> = store
        .read_sync(|r| r.prefix_all(store::REPOS, &[]))
        .map_err(|e| anyhow::anyhow!("{}", e.message))?;
    for repo in &repos {
        if !layout.repo_dir(&repo.path).is_dir() {
            problems.push(format!("repository {} has no Git directory", repo.path));
        }
        if !manifest.repositories.iter().any(|e| e.id == repo.id) {
            problems.push(format!("repository {} is not in the manifest", repo.path));
        }
    }
    if !store.check_integrity()? {
        problems.push("the database structure needed a repair".into());
    }
    println!(
        "check: {} repositories, {} problem(s)",
        repos.len(),
        problems.len()
    );
    Ok(problems)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ci::queue;
    use crate::config::Config;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering::SeqCst};

    fn app(data: &Path) -> crate::app::Shared {
        let config = Config::load(None, Some(data), None, None).unwrap();
        config.layout().create().unwrap();
        let store = Store::open(&config.layout().db(), 1 << 20, 1).unwrap();
        App::new(
            config,
            "http://127.0.0.1:7878".into(),
            store,
            vec![],
            "http://127.0.0.1:7878".into(),
        )
    }

    /// The files of an attachment no ticket refers to and of a CI attempt,
    /// both past their time.
    fn expired_files(app: &App) -> Vec<PathBuf> {
        let old = "2026-01-01T00:00:00.000Z";
        let attachment = crate::model::Attachment {
            id: "atc_01JABCDEFGHJKMNPQRSTVWXYZ0".into(),
            repo: "repo_1".into(),
            name: "x.png".into(),
            media_type: "image/png".into(),
            size: 1,
            author: "member1".into(),
            created_at: old.into(),
        };
        let attempt = app
            .store
            .write_sync(|w| {
                w.put(
                    store::ATTACHMENTS,
                    &crate::model::Attachment::key(&attachment.repo, &attachment.id),
                    &attachment,
                )?;
                let job = crate::ci::pipeline::JobDef {
                    paths: Vec::new(),
                    name: "test".into(),
                    image: "alpine".into(),
                    commands: vec!["true".into()],
                    timeout_secs: None,
                    artifacts: vec![],
                    cache: None,
                };
                let registration = queue::Registration {
                    selection: None,
                    repo: "repo_1".into(),
                    repo_path: "acme/demo".into(),
                    sha: "a".repeat(40),
                    tree: None,
                    branch: "main".into(),
                    protected: true,
                    pipeline: queue::PipelineState::Jobs { jobs: vec![job] },
                    network: false,
                };
                queue::register(w, registration, 10, old)?;
                let c = queue::claim(w, "runner1", 60, old)?.unwrap();
                queue::finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "success",
                    None,
                    queue::ResultInfo::default(),
                    10,
                    old,
                )?;
                Ok(c.attempt.id)
            })
            .unwrap();
        let files = vec![
            app.layout.attachments().join(&attachment.id),
            app.ci.log_path(&attempt),
        ];
        for file in &files {
            std::fs::write(file, "x").unwrap();
        }
        files
    }

    #[tokio::test]
    async fn every_deletion_of_referenced_files_is_left_to_its_next_pass_during_a_backup() {
        let dir = tempfile::tempdir().unwrap();
        let app = app(dir.path());
        let files = expired_files(&app);
        let backup = app.file_deletions.write().await;
        assert_eq!(crate::domain::attachments::sweep(&app).await.unwrap(), 0);
        assert_eq!(crate::ci::files::apply_retention(&app).await.unwrap(), 0);
        for file in &files {
            assert!(
                file.exists(),
                "{} was deleted during a backup",
                file.display()
            );
        }
        drop(backup);
        assert_eq!(crate::domain::attachments::sweep(&app).await.unwrap(), 1);
        assert_eq!(crate::ci::files::apply_retention(&app).await.unwrap(), 1);
        for file in &files {
            assert!(!file.exists(), "{} was kept", file.display());
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_backup_lets_no_deletion_run_from_its_database_copy_until_its_files_are_copied() {
        let dir = tempfile::tempdir().unwrap();
        let app = app(dir.path());
        // Enough CI logs, copied before the attachments, that the copy takes
        // a while.
        for i in 0..2000 {
            std::fs::write(app.layout.ci_logs().join(format!("{i}.log")), "x").unwrap();
        }
        std::fs::write(app.layout.attachments().join("atc_last"), "x").unwrap();
        let out = tempfile::tempdir().unwrap();
        let target = out.path().join("backup");
        let done = Arc::new(AtomicBool::new(false));
        // A deletion would start whenever it gets the lock.
        let deletion = {
            let (app, target, done) = (app.clone(), target.clone(), done.clone());
            tokio::spawn(async move {
                while !done.load(SeqCst) {
                    let held = app.file_deletions.read().await;
                    if target.join(DB_FILE).exists() {
                        assert!(
                            target.join("attachments/atc_last").exists(),
                            "a deletion could run after the database copy but before the file copy"
                        );
                    }
                    drop(held);
                    tokio::task::yield_now().await;
                }
            })
        };
        create(&app, &target).await.unwrap();
        done.store(true, SeqCst);
        deletion.await.unwrap();
    }

    #[test]
    fn manifest_paths_stay_inside_the_backup() {
        let root = Path::new("/b");
        assert!(inside(root, "repos/x.bundle").is_ok());
        assert!(inside(root, "../x").is_err());
        assert!(inside(root, "/etc/passwd").is_err());
        assert!(inside(root, "repos/../../x").is_err());
        assert!(inside(root, "").is_err());
    }

    #[test]
    fn the_manifest_gets_its_name_only_when_complete() {
        let dir = tempfile::tempdir().unwrap();
        let partial = dir.path().join(format!("{MANIFEST}.partial"));
        // What a write that was cut off left behind.
        std::fs::write(&partial, "{\"serv").unwrap();
        write_manifest(dir.path(), "{}").unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.path().join(MANIFEST)).unwrap(),
            "{}"
        );
        assert!(!partial.exists());
    }

    #[test]
    fn checksums_cover_every_file_but_the_manifest() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("a/b")).unwrap();
        std::fs::write(dir.path().join("a/b/c.txt"), "hello").unwrap();
        std::fs::write(dir.path().join(MANIFEST), "{}").unwrap();
        let files = checksum_tree(dir.path()).unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].path, "a/b/c.txt");
        assert_eq!(files[0].bytes, 5);
        assert_eq!(
            files[0].sha256,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }
}
