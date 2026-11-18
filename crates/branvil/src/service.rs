//! The service on its data directory: prepared for its host, which owns the
//! listener, and the offline maintenance of the data (the server must be
//! stopped; redb refuses a second opener).

use std::path::Path;
use std::sync::Arc;

use anyhow::bail;

use crate::app::App;
use crate::config::Config;
use crate::domain::members::{self, NewMember};
use crate::model::MemberKind;
use crate::store::{self, Rd, Store};

/// The owner's member name, given to the owner the service creates.
const OWNER: &str = "owner";

/// Offline maintenance of the data directory.
#[derive(Clone, Copy, Debug)]
pub enum Maintenance {
    /// Check the database structure and that every index matches the
    /// records; a passing check also clears the mark an upgrade leaves,
    /// which the server refuses.
    Check,
    /// Rebuild every derived index from the records.
    Reindex,
    /// Upgrade the database from the previous schema version, keeping a copy
    /// of it as it was.
    Upgrade,
}

/// A prepared service. Its host owns the listener and shutdown ordering.
pub struct Embedded {
    pub(crate) app: Arc<App>,
}

impl Embedded {
    pub fn router(&self) -> axum::Router {
        crate::http::router(self.app.clone())
    }

    pub fn data_api(&self) -> crate::api::DataApi {
        crate::api::DataApi::new(self.app.clone())
    }

    pub async fn drain(&self) -> anyhow::Result<usize> {
        let cut = self.app.ci.stop(false).await?;
        if cut > 0 {
            tracing::warn!("stopped with {cut} CI job(s) interrupted at the drain deadline");
        }
        Ok(cut)
    }

    pub fn close_events(&self) {
        self.app.events.close();
    }

    pub async fn close(self) {
        self.app.close().await;
    }
}

pub(crate) async fn prepare(
    config: Config,
    vault: Arc<dyn crate::vault::Vault>,
    base_url: String,
    hook_url: String,
    active_agents: crate::agent::ActiveAgents,
) -> anyhow::Result<Embedded> {
    let store = open_service(&config).await?;
    recover(
        config,
        vault,
        base_url,
        hook_url,
        store,
        Some(active_agents),
    )
    .await
}

pub(crate) async fn open_service(config: &Config) -> anyhow::Result<Store> {
    let layout = config.layout();
    layout.create()?;
    let store = Store::open(
        &layout.db(),
        config.storage.cache_size_mib << 20,
        config.storage.read_concurrency,
    )?;
    if store.needs_check()? {
        bail!(
            "{} was upgraded and has not passed `guildbotics server hub-data check` since; run it with the server stopped, then start the server",
            layout.db().display()
        );
    }
    crate::git::hold(&layout).await?;
    // A new data directory gets its owner, the host's owner.
    store.write_sync(|w| {
        if crate::auth::owner(w).is_err() {
            members::insert_member(
                w,
                &NewMember {
                    name: OWNER.into(),
                    display_name: None,
                    kind: MemberKind::Owner,
                },
            )?;
        }
        Ok(())
    })?;
    crate::hooks::install(&layout)?;
    Ok(store)
}

pub(crate) async fn recover(
    config: Config,
    vault: Arc<dyn crate::vault::Vault>,
    base_url: String,
    hook_url: String,
    store: Store,
    active_agents: Option<crate::agent::ActiveAgents>,
) -> anyhow::Result<Embedded> {
    let hook_base = hook_url.clone();
    let app = App::new(config, base_url, store, vault, hook_url);
    if let Some(roster) = active_agents {
        let _ = app.active_agents.set(roster);
    }
    app.git
        .version()
        .await
        .map_err(|e| anyhow::anyhow!("git is not usable: {}", e.message))?;

    // Every managed repository carries Branvil's configuration (hooks path,
    // hidden refs) even if it was changed on disk, and no lock file a killed
    // Git left (no Git of a stopped service runs any more: `git::hold`).
    let repos: Vec<crate::model::Repo> =
        app.store.read(|r| r.prefix_all(store::REPOS, &[])).await?;
    for repo in &repos {
        let dir = app.git.repo_dir(&repo.path);
        #[cfg(unix)]
        crate::git::remove_stale_locks(&dir)?;
        app.git.configure(&dir).await?;
        app.git.set_head(&dir, &repo.default_branch).await?;
    }
    let report = crate::refs::recover(&app).await?;
    tracing::info!(
        "startup recovery: {} operation(s) completed, {} dropped, {} ref change(s) recorded",
        report.completed,
        report.rolled_back,
        report.rescanned_changes
    );

    crate::ci::Ci::start(app.clone());
    crate::ci::supervise::start(app.clone(), hook_base);
    crate::upstream::start(app.clone());
    crate::autoland::start(app.clone());
    crate::domain::attachments::start(app.clone());

    Ok(Embedded { app })
}

/// Maintains the data directory `data_dir` while no server opens it. A
/// directory without a database has nothing yet: the server's first start
/// creates it.
pub fn maintain(operation: Maintenance, data_dir: &Path) -> anyhow::Result<()> {
    let config = Config::load(None, data_dir)?;
    let layout = config.layout();
    if !layout.db().exists() {
        println!("{} has no data yet", data_dir.display());
        return Ok(());
    }
    let open = || Store::open(&layout.db(), config.storage.cache_size_mib << 20, 1);
    match operation {
        Maintenance::Upgrade => {
            let done = crate::upgrade::upgrade(&layout)?;
            println!("{}", store::upgrade_report(&layout.db(), &done));
            Ok(())
        }
        Maintenance::Check => {
            let store = open()?;
            let problems = store.read_sync(crate::model::index_problems)?;
            let pending = store.read_sync(|r| Ok(r.prefix_keys(store::JOURNAL, &[])?.len()))?;
            let structure = store.check_integrity()?;
            println!(
                "database structure: {}",
                if structure { "ok" } else { "repaired" }
            );
            println!("pending ref operations: {pending} (finished or dropped at the next start)");
            if problems.is_empty() {
                println!("indexes: ok");
                open()?.checked()?;
                Ok(())
            } else {
                for p in &problems {
                    println!("index problem: {p}");
                }
                bail!(
                    "{} index problem(s); `guildbotics server hub-data reindex` rebuilds the indexes",
                    problems.len()
                )
            }
        }
        Maintenance::Reindex => {
            let n = open()?.write_sync(crate::model::rebuild_indexes)?;
            println!("rebuilt {n} index entries");
            Ok(())
        }
    }
}
