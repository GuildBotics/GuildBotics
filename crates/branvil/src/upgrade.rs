//! 46 → 47: the configuration file is named `config.toml` (was `branvil.toml`,
//! kept for the previous build until the owner removes it),
//! and the automatic runner's display name no longer says Branvil.
//! Every upgrade also binds the GuildBotics workspaces' execution roots.
use std::{collections::BTreeMap, path::PathBuf};

use crate::ci::supervise::{AUTO_RUNNER, AUTO_RUNNER_DISPLAY_NAME};
use crate::config::Layout;
use crate::model::Member;
use crate::store::{self, Rd, Store, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 46;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);
/// The configuration file's name in the data directory of version `FROM`.
pub const FROM_CONFIG_FILE: &str = "branvil.toml";
/// The automatic runner's display name given before version 47.
const FROM_RUNNER_DISPLAY_NAME: &str = "Branvil runner (automatic)";

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
        let key = Member::key(AUTO_RUNNER);
        if let Some(mut runner) = w.get::<Member>(store::MEMBERS, &key)?
            && runner.display_name == FROM_RUNNER_DISPLAY_NAME
        {
            runner.display_name = AUTO_RUNNER_DISPLAY_NAME.into();
            w.put(store::MEMBERS, &key, &runner)?;
        }
        // Copied, not moved: the previous build, which a failed or rolled-back
        // update runs again, still reads the file under its old name.
        let legacy = layout.root.join(FROM_CONFIG_FILE);
        Ok(match std::fs::copy(&legacy, layout.config_file()) {
            Ok(_) => format!("copied {FROM_CONFIG_FILE} to config.toml"),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                "no configuration file to copy".into()
            }
            Err(e) => return Err(e.into()),
        })
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
    use super::legacy_item_url;

    #[test]
    fn upgrade_copies_the_configuration_file_and_renames_the_automatic_runner() {
        use super::*;
        use crate::model::Workspace;
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let workspace = db
            .write_sync(|w| {
                let workspace = crate::domain::repos::insert_workspace(w, "acme".into(), None)?;
                crate::domain::members::insert_member(
                    w,
                    &crate::domain::members::NewMember {
                        name: AUTO_RUNNER.into(),
                        display_name: Some(FROM_RUNNER_DISPLAY_NAME.into()),
                        kind: crate::model::MemberKind::Runner,
                    },
                )?;
                w.put(
                    store::META,
                    &store::Key::new().s("schema_version").bytes(),
                    &FROM,
                )?;
                Ok(workspace.id)
            })
            .unwrap();
        drop(db);
        let legacy = temporary.path().join(FROM_CONFIG_FILE);
        std::fs::write(&legacy, "[ci]\nqueue_max = 9\n").unwrap();
        let root = temporary.path().join("execution");
        let done = upgrade_with_roots(&layout, [("acme".into(), root.clone())].into())
            .unwrap()
            .unwrap();
        assert_eq!(done.summary, "copied branvil.toml to config.toml");
        assert_eq!(
            std::fs::read_to_string(&legacy).unwrap(),
            "[ci]\nqueue_max = 9\n"
        );
        assert_eq!(
            std::fs::read_to_string(temporary.path().join("config.toml")).unwrap(),
            "[ci]\nqueue_max = 9\n"
        );
        let config = crate::config::Config::load(None, temporary.path()).unwrap();
        assert_eq!(config.ci.queue_max, 9);
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let record: Workspace = db
            .read_sync(|r| r.get(store::WORKSPACES, &Workspace::key(&workspace)))
            .unwrap()
            .unwrap();
        assert_eq!(record.execution_root, Some(root));
        let runner: Member = db
            .read_sync(|r| r.get(store::MEMBERS, &Member::key(AUTO_RUNNER)))
            .unwrap()
            .unwrap();
        assert_eq!(runner.display_name, "GuildBotics runner (automatic)");
    }

    #[test]
    fn upgrade_keeps_the_defaults_and_a_runner_name_the_owner_chose() {
        use super::*;
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.write_sync(|w| {
            crate::domain::members::insert_member(
                w,
                &crate::domain::members::NewMember {
                    name: AUTO_RUNNER.into(),
                    display_name: Some("Hub runner".into()),
                    kind: crate::model::MemberKind::Runner,
                },
            )?;
            w.put(
                store::META,
                &store::Key::new().s("schema_version").bytes(),
                &FROM,
            )?;
            Ok(())
        })
        .unwrap();
        drop(db);
        let done = upgrade(&layout).unwrap().unwrap();
        assert_eq!(done.summary, "no configuration file to copy");
        assert!(!layout.config_file().exists());
        assert!(upgrade(&layout).unwrap().is_none());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let runner: Member = db
            .read_sync(|r| r.get(store::MEMBERS, &Member::key(AUTO_RUNNER)))
            .unwrap()
            .unwrap();
        assert_eq!(runner.display_name, "Hub runner");
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
