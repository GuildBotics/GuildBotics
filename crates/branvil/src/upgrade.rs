//! 53 → 54: CI attempts record explicitly disabled runners.
//! Every upgrade also binds the GuildBotics workspaces' execution roots.
use std::{collections::BTreeMap, path::PathBuf};

use crate::config::Layout;
use crate::error::AppError;
use crate::store::{self, Rd, Store, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 53;
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
        for key in w.prefix_keys(store::CI_ATTEMPTS, &[])? {
            let mut attempt: serde_json::Value = w
                .get(store::CI_ATTEMPTS, &key)?
                .ok_or_else(|| AppError::internal("CI attempt disappeared during upgrade"))?;
            attempt
                .as_object_mut()
                .ok_or_else(|| AppError::internal("invalid CI attempt"))?
                .insert("ci_disabled_runners".into(), serde_json::json!([]));
            w.put(store::CI_ATTEMPTS, &key, &attempt)?;
        }
        Ok("initialized disabled runner names on existing CI attempts".into())
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
    fn upgrade_initializes_disabled_runner_names_and_binds_roots() {
        use crate::model::Workspace;
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let attempt = serde_json::json!({"id": "att_old", "details": "Executor unavailable"});
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
        assert!(store::check_version(&layout.db()).is_err());
        let root = temporary.path().join("execution");
        let done = upgrade_with_roots(&layout, [("acme".into(), root.clone())].into())
            .unwrap()
            .unwrap();
        assert!(done.copy.is_file());
        assert!(upgrade(&layout).unwrap().is_none());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let upgraded: serde_json::Value = db
            .read_sync(|r| r.get(store::CI_ATTEMPTS, b"attempt"))
            .unwrap()
            .unwrap();
        let mut expected = attempt;
        expected["ci_disabled_runners"] = serde_json::json!([]);
        assert_eq!(upgraded, expected);
        let record: Workspace = db
            .read_sync(|r| r.get(store::WORKSPACES, &Workspace::key(&workspace)))
            .unwrap()
            .unwrap();
        assert_eq!(record.execution_root, Some(root));
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
