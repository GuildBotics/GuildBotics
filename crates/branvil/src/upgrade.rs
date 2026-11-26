//! 47 → 48: repositories have `review_rounds`, the heads a member reviews
//! before its automatic re-review stops (3, the limit every repository had).
//! Every upgrade also binds the GuildBotics workspaces' execution roots.
use std::{collections::BTreeMap, path::PathBuf};

use crate::config::Layout;
use crate::store::{self, Rd, Store, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 47;
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
        for key in w.prefix_keys(store::REPOS, &[])? {
            let mut repo: serde_json::Value = w.get(store::REPOS, &key)?.unwrap();
            repo["review_rounds"] = 3.into();
            w.put(store::REPOS, &key, &repo)?;
        }
        Ok("added review_rounds=3 to repositories".into())
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
    fn upgrade_gives_every_repository_three_review_rounds_and_binds_execution_roots() {
        use crate::model::{Repo, Workspace};
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let workspace = db
            .write_sync(|w| {
                let workspace = crate::domain::repos::insert_workspace(w, "acme".into(), None)?;
                for (id, path) in [("repo_1", "acme/one"), ("repo_2", "acme/two")] {
                    w.put(
                        store::REPOS,
                        &Repo::key(id),
                        &serde_json::json!({"id":id,"path":path,"workspace":workspace.id,
                            "default_branch":"main","protected_branches":[],"required_approvals":1,
                            "auto_land":false,"hold_while_failing":false,"ci_network":false,
                            "responder":null,"labels":[],"upstream":null,"revision":1,"created_at":"0"}),
                    )?;
                }
                w.put(
                    store::META,
                    &store::Key::new().s("schema_version").bytes(),
                    &FROM,
                )?;
                Ok(workspace.id)
            })
            .unwrap();
        drop(db);
        let root = temporary.path().join("execution");
        let done = upgrade_with_roots(&layout, [("acme".into(), root.clone())].into())
            .unwrap()
            .unwrap();
        assert_eq!(done.summary, "added review_rounds=3 to repositories");
        assert!(upgrade(&layout).unwrap().is_none());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let repos: Vec<Repo> = db.read_sync(|r| r.prefix_all(store::REPOS, &[])).unwrap();
        assert_eq!(repos.len(), 2);
        assert!(repos.iter().all(|r| r.review_rounds == 3));
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
