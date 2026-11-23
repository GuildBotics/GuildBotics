//! 42 → 43: tie execution settings to the existing workspace and update stored item URLs.
use std::{collections::BTreeMap, path::PathBuf};

use crate::config::Layout;
use crate::model::{Comment, Pull};
use crate::store::{self, Rd, Store, Tbl, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 42;
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
        let pulls = update_rows::<Pull>(w, store::PULLS, |pull| {
            let mut changed = false;
            for ticket in &mut pull.tickets {
                if let Some((repo, number)) = &ticket.local {
                    let repo = crate::domain::repo_by_id(w, repo)?;
                    let mut url = reqwest::Url::parse(&ticket.url)?;
                    url.set_path(&crate::util::item_path(&repo.path, "tickets", number));
                    if ticket.url != url.as_str() {
                        ticket.url = url.to_string();
                        changed = true;
                    }
                }
            }
            Ok(changed)
        })?;
        let comments = update_rows::<Comment>(w, store::COMMENTS, |comment| {
            let target = comment.meta.as_mut().and_then(|meta| meta.get_mut("work_identity"));
            Ok(target.is_some_and(rewrite_value))
        })?;
        Ok(format!("updated local links in {pulls} pull requests and workflow metadata in {comments} comments"))
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

fn update_rows<T>(
    w: &Wtx,
    table: Tbl,
    mut update: impl FnMut(&mut T) -> anyhow::Result<bool>,
) -> anyhow::Result<usize>
where
    T: serde::de::DeserializeOwned + serde::Serialize,
{
    let mut after = None;
    let mut changed = 0;
    loop {
        let rows = w.prefix_raw(table, &[], after.as_deref(), 512)?;
        if rows.is_empty() {
            return Ok(changed);
        }
        for (key, raw) in rows {
            let mut value = store::decode(&raw)?;
            if update(&mut value)? {
                w.put(table, &key, &value)?;
                changed += 1;
            }
            after = Some(key);
        }
    }
}

pub fn rewrite_value(value: &mut serde_json::Value) -> bool {
    let Some(url) = value.as_str().and_then(legacy_item_url) else {
        return false;
    };
    *value = url.into();
    true
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
    use super::legacy_item_url;

    #[test]
    fn upgrade_preserves_workspace_repository_and_ticket_relations() {
        use super::*;
        use crate::model::{Repo, Ticket, Workspace};
        use serde_json::json;
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let (workspace, repo) = db.write_sync(|w| {
            let workspace = crate::domain::repos::insert_workspace(w, "acme".into(), None)?;
            let repo = Repo {
                id: "repo_original".into(), path: "acme/demo".into(), workspace: workspace.id,
                default_branch: "main".into(), protected_branches: vec![],
                post_merge_jobs: vec![], required_checks: vec![], required_approvals: 0,
                auto_land: false, hold_while_failing: false, ci_network: false, responder: None,
                labels: vec![], upstream: None, revision: 1, created_at: crate::util::now(),
            };
            w.put(store::REPOS, &Repo::key(&repo.id), &repo)?;
            let ticket = json!({"workspace":repo.workspace, "repo":repo.id, "number":3,
                "parent":[repo.id,1],"blocked_by":[[repo.id,2]],"lane":"ready","rank":"0|zz"});
            w.put(store::TICKETS, &Ticket::key(&repo.id, 3), &ticket)?;
            let pull = json!({"repo":repo.id,"number":2,"title":"Keep this pull",
                "body":"http://localhost/acme/demo/-/tickets/3","state":"open","merged":false,
                "draft":false,"author":"owner","head":"topic","base":"main","reviewers":[],
                "tickets":[{"url":"http://localhost/acme/demo/-/tickets/3","closes":true,"local":[repo.id,3]}],
                "labels":[],"reviewed_by":[],"created_at":"fixture","updated_at":"fixture","revision":1});
            w.put(store::PULLS, &Pull::key(&repo.id, 2), &pull)?;
            w.put(store::META, &store::Key::new().s("schema_version").bytes(), &FROM)?;
            Ok((repo.workspace, repo.id))
        }).unwrap();
        drop(db);
        let root = temporary.path().join("execution");
        upgrade_with_roots(&layout, [("acme".into(), root.clone())].into())
            .unwrap()
            .unwrap();
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.read_sync(|r| {
            let workspace_record: Workspace = r
                .get(store::WORKSPACES, &Workspace::key(&workspace))?
                .unwrap();
            assert_eq!(workspace_record.id, workspace);
            assert_eq!(workspace_record.execution_root, Some(root.clone()));
            let repo_record: Repo = r.get(store::REPOS, &Repo::key(&repo))?.unwrap();
            assert_eq!(repo_record.workspace, workspace);
            let ticket: serde_json::Value = r.get(store::TICKETS, &Ticket::key(&repo, 3))?.unwrap();
            assert_eq!(
                ticket,
                json!({"workspace":workspace,"repo":repo,"number":3,
                "parent":[repo,1],"blocked_by":[[repo,2]],"lane":"ready","rank":"0|zz"})
            );
            let pull: Pull = r.get(store::PULLS, &Pull::key(&repo, 2))?.unwrap();
            assert_eq!(pull.body, "http://localhost/acme/demo/-/tickets/3");
            assert_eq!(
                pull.tickets[0].url,
                "http://localhost/workspaces/acme/repos/demo/tickets/3"
            );
            assert_eq!(pull.tickets[0].local, Some((repo.clone(), 3)));
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
