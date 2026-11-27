//! 50 → 51: add an empty table for JUnit test failure history.
//! Every upgrade also binds the GuildBotics workspaces' execution roots.
use std::{collections::BTreeMap, path::PathBuf};

use crate::config::Layout;
use crate::store::{self, Rd, Store, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 50;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    upgrade_with_roots(layout, BTreeMap::new())
}

pub(crate) fn upgrade_with_roots(
    layout: &Layout,
    roots: BTreeMap<String, PathBuf>,
) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        if !roots.is_empty() {
            let store = Store::open(&layout.db(), 1 << 20, 1)?;
            store.write_sync(|w| execution_roots(w, roots))?;
        }
        return Ok(None);
    };
    db.finish(|w| {
        execution_roots(w, roots)?;
        Ok("added CI test failure history".into())
    })
    .map(Some)
}

fn execution_roots(w: &Wtx, roots: BTreeMap<String, PathBuf>) -> crate::error::AppResult<()> {
    let registered: std::collections::BTreeSet<_> = w
        .prefix_all::<crate::model::Workspace>(store::WORKSPACES, &[])?
        .into_iter()
        .filter_map(|ws| ws.execution_root)
        .collect();
    for (name, root) in roots {
        if !registered.contains(&root) {
            crate::domain::repos::assign_execution_root(w, name, root)?;
        }
    }
    Ok(())
}

/// Offline conversion for structured links to the embedded code host.
pub fn legacy_item_url(value: &str) -> Option<String> {
    let mut url = reqwest::Url::parse(value).ok()?;
    if url.scheme() != "http"
        || !matches!(url.host_str()?, "127.0.0.1" | "[::1]" | "localhost")
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return None;
    }
    let path: Vec<_> = url.path_segments()?.collect();
    let [
        workspace,
        repo,
        "-",
        kind @ ("tickets" | "pulls" | "commits" | "jobs"),
        id,
    ] = path.as_slice()
    else {
        return None;
    };
    if !crate::util::valid_name(workspace) || !crate::util::valid_name(repo) || id.is_empty() {
        return None;
    }
    let path = format!("/workspaces/{workspace}/repos/{repo}/{kind}/{id}");
    url.set_path(&path);
    Some(url.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upgrade_creates_empty_test_failure_history_and_binds_execution_roots() {
        use redb::ReadableDatabase;

        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let machine = serde_json::json!({
            "logical_cpus": 4,
            "total_memory_mib": 8192,
            "available_memory_mib": 4096,
            "commit_available_mib": null,
        });
        let attempt = serde_json::json!({
            "runner_claim": {"machine": machine, "memory_samples": 0, "budget": null},
            "runner_deferrals": {"small": {"machine": machine}},
        });
        let workspace = db
            .write_sync(|w| {
                let workspace = crate::domain::repos::insert_workspace(w, "acme".into(), None)?;
                w.put(store::CI_ATTEMPTS, b"attempt", &attempt)?;
                w.put(
                    store::META,
                    &store::Key::new().s("schema_version").bytes(),
                    &FROM,
                )?;
                Ok(workspace.id)
            })
            .unwrap();
        drop(db);
        let db = redb::Database::open(layout.db()).unwrap();
        let write = db.begin_write().unwrap();
        assert!(write.delete_table(store::CI_TEST_FAILURES).unwrap());
        write.commit().unwrap();
        drop(db);
        assert!(store::check_version(&layout.db()).is_err());
        let root = temporary.path().join("execution");
        let done = upgrade_with_roots(&layout, [("acme".into(), root.clone())].into())
            .unwrap()
            .unwrap();
        assert_eq!(done.summary, "added CI test failure history");
        assert!(done.copy.is_file());
        assert!(upgrade(&layout).unwrap().is_none());
        let db = redb::Database::open(layout.db()).unwrap();
        assert!(
            db.begin_read()
                .unwrap()
                .open_table(store::CI_TEST_FAILURES)
                .is_ok()
        );
        drop(db);
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.read_sync(|r| {
            assert!(r.prefix_keys(store::CI_TEST_FAILURES, &[])?.is_empty());
            let upgraded = r.get::<serde_json::Value>(store::CI_ATTEMPTS, b"attempt")?;
            assert_eq!(upgraded.as_ref(), Some(&attempt));
            let record: crate::model::Workspace = r
                .get(store::WORKSPACES, &crate::model::Workspace::key(&workspace))?
                .unwrap();
            assert_eq!(record.execution_root, Some(root));
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn upgrades_local_item_urls_and_keeps_queries_and_fragments() {
        let old = "http://127.0.0.1:7878/acme/demo/-/pulls/7?tab=diff#comment";
        let new = "http://127.0.0.1:7878/workspaces/acme/repos/demo/pulls/7?tab=diff#comment";
        assert_eq!(legacy_item_url(old).as_deref(), Some(new));
        assert_eq!(legacy_item_url(new), None);
        assert_eq!(
            legacy_item_url("https://example.com/acme/demo/-/pulls/7"),
            None
        );
        assert_eq!(
            legacy_item_url("http://localhost/api/v1/repos/acme%2Fdemo/pulls/7"),
            None
        );
    }
}
