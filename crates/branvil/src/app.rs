//! The process-wide state shared by every request.

use std::collections::HashMap;
use std::future::Future;
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use crate::ci::Ci;
use crate::config::{Config, Layout};
use crate::domain::members::Codes;
use crate::error::AppResult;
use crate::events::Events;
use crate::git::Git;
use crate::model::Repo;
use crate::store::{self, Rd, Store};

pub struct App {
    pub config: Config,
    /// The permanent base URL (`http://127.0.0.1:7878`), never ending in `/`.
    pub base_url: String,
    pub layout: Layout,
    pub store: Store,
    pub git: Git,
    /// Smart HTTP transfers in progress (see `LimitsConfig::git_http_concurrency`).
    pub transfers: Arc<tokio::sync::Semaphore>,
    pub events: Events,
    pub ci: Ci,
    /// The upstream code host: following it and publishing to it.
    pub upstream: crate::upstream::Upstream,
    /// Landing pull requests automatically, and the webhook told of it.
    pub autoland: crate::autoland::Autoland,
    /// Signs collection cursors.
    pub cursor_key: Vec<u8>,
    /// Shared with the hook processes this service spawns (through the Git
    /// child's environment); authenticates their callbacks.
    pub hook_secret: String,
    /// Loopback URL the hooks call back on (the bound address).
    pub hook_url: String,
    /// The one boundary per repository for ref changes Branvil makes and for
    /// recording ref changes: `locked`, `lock_repo` inside an operation, and
    /// `quiesce` for a backup take it.
    repo_locks: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    /// Held (read) by every operation `finish` runs; `close` takes it for
    /// good, and none starts once it waits for it.
    operations: Arc<tokio::sync::RwLock<()>>,
    /// Held (read) by whatever deletes files the database refers to (the
    /// attachment sweep, CI retention) from its decision until the files are
    /// gone, and left to its next pass while a backup holds it (write) from
    /// its database copy until it has copied the files, so the backup has
    /// every file its copy refers to.
    pub file_deletions: tokio::sync::RwLock<()>,
    /// This app, for the tasks `finish` spawns.
    me: Weak<App>,
    /// Requests that carry the same `Idempotency-Key` of the same member run
    /// one at a time; other keys do not wait for them.
    pub idempotency: KeyedLocks,
    /// When a token's `last_used_at` was last written (to write it at most
    /// once a minute).
    pub token_touched: Mutex<HashMap<String, std::time::Instant>>,
    /// Unused entry-link codes, in memory only: a restart invalidates them.
    pub entry_codes: Mutex<Codes<String>>,
    /// Unused codes of local apps the owner allowed, in memory only.
    pub app_codes: Mutex<Codes<crate::domain::apps::Grant>>,
    /// (repo dir, base, head) → changed file count.
    pub changed_files: Mutex<HashMap<(String, String, String), u64>>,
    /// Test-only crash point (`BRANVIL_FAILPOINT`).
    failpoint: Option<String>,
}

pub type Shared = Arc<App>;

tokio::task_local! {
    /// Set inside the task of an operation (`App::finish`).
    static IN_OPERATION: ();
}

/// One async lock per key. An entry lives only while someone holds or waits
/// for its lock, so the map never grows past the requests in flight.
#[derive(Default)]
pub struct KeyedLocks(Mutex<HashMap<Vec<u8>, Arc<tokio::sync::Mutex<()>>>>);

pub struct KeyedGuard<'a> {
    locks: &'a KeyedLocks,
    key: Vec<u8>,
    guard: Option<tokio::sync::OwnedMutexGuard<()>>,
}

impl KeyedLocks {
    pub async fn lock(&self, key: &[u8]) -> KeyedGuard<'_> {
        let mutex = self
            .0
            .lock()
            .unwrap()
            .entry(key.to_vec())
            .or_default()
            .clone();
        // Created before waiting, so a cancelled waiter removes its entry too.
        let mut held = KeyedGuard {
            locks: self,
            key: key.to_vec(),
            guard: None,
        };
        held.guard = Some(mutex.lock_owned().await);
        held
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.0.lock().unwrap().len()
    }
}

impl Drop for KeyedGuard<'_> {
    fn drop(&mut self) {
        let mut map = self.locks.0.lock().unwrap();
        self.guard.take();
        if map
            .get(&self.key)
            .is_some_and(|m| Arc::strong_count(m) == 1)
        {
            map.remove(&self.key);
        }
    }
}

impl App {
    pub fn new(
        config: Config,
        base_url: String,
        store: Store,
        cursor_key: Vec<u8>,
        hook_url: String,
    ) -> Shared {
        let layout = config.layout();
        let git = Git::new(
            layout.clone(),
            config.limits.git_concurrency,
            Duration::from_secs(config.limits.git_command_timeout_secs),
        );
        let events = Events::new();
        let ci = Ci::new(
            store.clone(),
            git.clone(),
            events.clone(),
            base_url.clone(),
            layout.clone(),
            config.ci.clone(),
            config.file.clone(),
        );
        Arc::new_cyclic(|me| Self {
            base_url,
            layout,
            store,
            git,
            transfers: Arc::new(tokio::sync::Semaphore::new(
                config.limits.git_http_concurrency.max(1),
            )),
            events,
            ci,
            upstream: crate::upstream::Upstream::new(&config.upstream),
            autoland: Default::default(),
            cursor_key,
            hook_secret: crate::util::random_secret::<32>(),
            hook_url,
            repo_locks: Mutex::new(HashMap::new()),
            operations: Arc::default(),
            file_deletions: Default::default(),
            me: me.clone(),
            idempotency: KeyedLocks::default(),
            token_touched: Mutex::new(HashMap::new()),
            entry_codes: Mutex::default(),
            app_codes: Mutex::default(),
            changed_files: Mutex::new(HashMap::new()),
            failpoint: std::env::var("BRANVIL_FAILPOINT")
                .ok()
                .filter(|v| !v.is_empty()),
            config,
        })
    }

    fn repo_lock(&self, repo_id: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut locks = self.repo_locks.lock().unwrap();
        locks
            .entry(repo_id.to_string())
            .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
            .clone()
    }

    /// Runs `op` under the repository's lock (`finish`). Waiting for the lock
    /// stays cancellable: nothing has started then.
    pub async fn locked<T, F>(
        &self,
        repo_id: &str,
        op: impl FnOnce(Shared) -> F + Send + 'static,
    ) -> T
    where
        F: Future<Output = T> + Send + 'static,
        T: Send + 'static,
    {
        self.run(Some(repo_id), op).await
    }

    /// Runs `op`, a write of several steps (Git, the database, the upstream
    /// host), to its end in a task of its own: a caller that stops waiting (a
    /// closed request, a task dropped by a stopping service) cannot leave it
    /// halfway, nor kill its Git in the middle of a ref update (which leaves
    /// lock files that refuse every later update of the ref). A stopping
    /// service starts no new one and waits for the running ones (`close`).
    /// `op` never calls `finish` or `locked` itself: once `close` waits, that
    /// call would wait for ever (checked in debug builds).
    pub async fn finish<T, F>(&self, op: impl FnOnce(Shared) -> F + Send + 'static) -> T
    where
        F: Future<Output = T> + Send + 'static,
        T: Send + 'static,
    {
        self.run(None, op).await
    }

    /// The repository's lock, for an operation (`finish`) that takes it only
    /// around its own ref and record changes, never while it waits for the
    /// upstream host.
    pub async fn lock_repo(&self, repo_id: &str) -> tokio::sync::OwnedMutexGuard<()> {
        debug_assert!(
            IN_OPERATION.try_with(|_| ()).is_ok(),
            "a repository lock is taken only in an operation"
        );
        self.repo_lock(repo_id).lock_owned().await
    }

    /// Holds the repository's lock, as an operation in progress would.
    #[cfg(test)]
    pub async fn hold_lock(&self, repo_id: &str) -> tokio::sync::OwnedMutexGuard<()> {
        self.repo_lock(repo_id).lock_owned().await
    }

    async fn run<T, F>(
        &self,
        repo_id: Option<&str>,
        op: impl FnOnce(Shared) -> F + Send + 'static,
    ) -> T
    where
        F: Future<Output = T> + Send + 'static,
        T: Send + 'static,
    {
        debug_assert!(
            IN_OPERATION.try_with(|_| ()).is_err(),
            "an operation must not start another one"
        );
        let lock = match repo_id {
            Some(id) => Some(self.repo_lock(id).lock_owned().await),
            None => None,
        };
        // Never waited for while the lock is held: an operation waiting for
        // its lock has not started, and `close` waits only for started ones.
        let Ok(open) = self.operations.clone().try_read_owned() else {
            drop(lock);
            return std::future::pending().await;
        };
        let app = self.me.upgrade().expect("the app outlives its requests");
        let run = IN_OPERATION.scope((), async move {
            let _held = (open, lock);
            op(app).await
        });
        match tokio::spawn(run).await {
            Ok(value) => value,
            Err(e) => std::panic::resume_unwind(e.into_panic()),
        }
    }

    /// Holds every repository lock (`<create>` first, then the repositories by
    /// id; every other holder takes one at a time) for a consistent copy of
    /// the repositories and the database (a backup); returns the repositories
    /// with their locks.
    pub async fn quiesce(&self) -> AppResult<(Vec<Repo>, Vec<tokio::sync::OwnedMutexGuard<()>>)> {
        let mut guards = vec![self.repo_lock("<create>").lock_owned().await];
        let mut repos: Vec<Repo> = self.store.read(|r| r.prefix_all(store::REPOS, &[])).await?;
        repos.sort_by(|a, b| a.id.cmp(&b.id));
        for repo in &repos {
            guards.push(self.repo_lock(&repo.id).lock_owned().await);
        }
        Ok((repos, guards))
    }

    /// Starts no operation (`finish`) from now on: later ones wait until the
    /// process exits. Then waits until the running ones have ended.
    pub async fn close(&self) {
        // Never released: the service is about to exit.
        std::mem::forget(self.operations.clone().write_owned().await);
    }

    /// Simulates a crash at a named point when `BRANVIL_FAILPOINT` names it.
    /// Used by the recovery tests; inert otherwise.
    pub fn failpoint(&self, name: &str) {
        if self.failpoint.as_deref() == Some(name) {
            eprintln!("branvil: failpoint {name} reached, aborting");
            std::process::abort();
        }
    }

    pub fn pull_url(&self, repo: &str, number: u64) -> String {
        format!("{}/{repo}/-/pulls/{number}", self.base_url)
    }
    pub fn repo_url(&self, repo: &str) -> String {
        format!("{}/{repo}", self.base_url)
    }
    pub fn clone_url(&self, repo: &str) -> String {
        format!("{}/git/{repo}.git", self.base_url)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
    use tokio::sync::oneshot;

    fn app(dir: &std::path::Path) -> Shared {
        let config = Config::load(None, Some(dir), None, None).unwrap();
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

    /// An operation that waits for `release` once it has said it started,
    /// then sets `done`.
    fn held_op(
        started: oneshot::Sender<()>,
        release: oneshot::Receiver<()>,
        done: Arc<AtomicBool>,
    ) -> impl FnOnce(Shared) -> std::pin::Pin<Box<dyn Future<Output = ()> + Send>> {
        move |_| {
            Box::pin(async move {
                let _ = started.send(());
                let _ = release.await;
                done.store(true, SeqCst);
            })
        }
    }

    const SOON: Duration = Duration::from_millis(50);

    #[tokio::test]
    async fn an_operation_runs_to_its_end_when_its_caller_stops_waiting() {
        let temp = tempfile::tempdir().unwrap();
        let app = app(temp.path());
        let (started, release, done) = (oneshot::channel(), oneshot::channel(), Arc::default());
        let caller = app.locked("r", held_op(started.0, release.1, Arc::clone(&done)));
        tokio::select! {
            _ = caller => panic!("the operation ended before its release"),
            _ = started.1 => {}
        }
        // Its caller is gone; it still holds the lock until it ends.
        let next = app.locked("r", |_| async {});
        tokio::pin!(next);
        assert!(tokio::time::timeout(SOON, next.as_mut()).await.is_err());
        let _ = release.0.send(());
        tokio::time::timeout(Duration::from_secs(5), next)
            .await
            .expect("the lock is released when the operation ends");
        assert!(done.load(SeqCst), "the operation ran to its end");
    }

    #[tokio::test]
    async fn a_caller_that_stops_waiting_for_the_lock_starts_nothing() {
        let temp = tempfile::tempdir().unwrap();
        let app = app(temp.path());
        let held = app.hold_lock("r").await;
        let ran = Arc::new(AtomicBool::new(false));
        let r = Arc::clone(&ran);
        let waiting = app.locked("r", move |_| async move { r.store(true, SeqCst) });
        assert!(tokio::time::timeout(SOON, waiting).await.is_err());
        drop(held);
        app.locked("r", |_| async {}).await;
        assert!(!ran.load(SeqCst), "an operation never started does not run");
    }

    #[tokio::test]
    async fn close_waits_for_the_running_operations_and_starts_no_new_one() {
        let temp = tempfile::tempdir().unwrap();
        let app = app(temp.path());
        let (started, release, done) = (oneshot::channel(), oneshot::channel(), Arc::default());
        let a = app.clone();
        let running = tokio::spawn(async move {
            a.finish(held_op(started.0, release.1, Arc::clone(&done)))
                .await;
            done
        });
        started.1.await.unwrap();
        let close = app.close();
        tokio::pin!(close);
        assert!(tokio::time::timeout(SOON, close.as_mut()).await.is_err());
        let later = app.finish(|_| async {});
        assert!(
            tokio::time::timeout(SOON, later).await.is_err(),
            "no operation starts once closing"
        );
        let _ = release.0.send(());
        tokio::time::timeout(Duration::from_secs(5), close)
            .await
            .expect("close ends with the running operation");
        assert!(running.await.unwrap().load(SeqCst));
        let after = app.finish(|_| async {});
        assert!(tokio::time::timeout(SOON, after).await.is_err());
    }

    #[tokio::test]
    async fn close_does_not_wait_for_an_operation_waiting_for_its_lock() {
        let temp = tempfile::tempdir().unwrap();
        let app = app(temp.path());
        // A backup holds the lock (`quiesce`); an operation waits for it.
        let held = app.hold_lock("r").await;
        let ran = Arc::new(AtomicBool::new(false));
        let (a, r) = (app.clone(), Arc::clone(&ran));
        let waiting = tokio::spawn(async move {
            a.locked("r", move |_| async move { r.store(true, SeqCst) })
                .await
        });
        tokio::time::sleep(SOON).await;
        tokio::time::timeout(Duration::from_secs(5), app.close())
            .await
            .expect("close waits only for started operations");
        drop(held);
        tokio::time::sleep(SOON).await;
        assert!(
            !waiting.is_finished() && !ran.load(SeqCst),
            "it never starts"
        );
        waiting.abort();
    }

    #[cfg(debug_assertions)]
    #[tokio::test]
    #[should_panic(expected = "an operation must not start another one")]
    async fn an_operation_inside_an_operation_is_refused() {
        let temp = tempfile::tempdir().unwrap();
        let app = app(temp.path());
        app.finish(|app| async move { app.locked("r", |_| async {}).await })
            .await;
    }

    #[cfg(debug_assertions)]
    #[tokio::test]
    #[should_panic(expected = "a repository lock is taken only in an operation")]
    async fn a_repository_lock_outside_an_operation_is_refused() {
        let temp = tempfile::tempdir().unwrap();
        let _ = app(temp.path()).lock_repo("r").await;
    }

    #[tokio::test]
    async fn keyed_locks_serialize_one_key_only_and_forget_idle_keys() {
        let locks = Arc::new(KeyedLocks::default());
        let a = locks.lock(b"a").await;
        // Another key is not blocked by `a`.
        let b = tokio::time::timeout(Duration::from_secs(1), locks.lock(b"b"))
            .await
            .expect("another key waits for nobody");
        drop(b);
        // The same key waits until `a` is released.
        let l2 = locks.clone();
        let waiter = tokio::spawn(async move {
            let _g = l2.lock(b"a").await;
        });
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(!waiter.is_finished(), "the same key must wait");
        drop(a);
        waiter.await.unwrap();
        assert_eq!(locks.len(), 0, "released keys are forgotten");
        // A waiter that gives up leaves nothing behind either.
        let held = locks.lock(b"c").await;
        let gave_up = tokio::time::timeout(Duration::from_millis(20), locks.lock(b"c")).await;
        assert!(gave_up.is_err());
        drop(held);
        assert_eq!(locks.len(), 0);
    }
}
