//! 46 → 47: the configuration file is named `config.toml` (was `branvil.toml`,
//! kept for the previous build until the owner removes it),
//! and the automatic runner is the member `hub-runner` (was `branvil-runner`).
//! Every upgrade also binds the GuildBotics workspaces' execution roots.
use std::{collections::BTreeMap, path::PathBuf};

use crate::ci::supervise::{AUTO_RUNNER, AUTO_RUNNER_DISPLAY_NAME};
use crate::config::Layout;
use crate::model::{Member, Token};
use crate::store::{self, Rd, Store, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 46;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);
/// The configuration file's name in the data directory of version `FROM`.
pub const FROM_CONFIG_FILE: &str = "branvil.toml";
/// The automatic runner's member name and the display name given to it
/// before version 47.
const FROM_AUTO_RUNNER: &str = "branvil-runner";
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
        let runner = rename_auto_runner(w)?;
        // Copied, not moved: the previous build, which a failed or rolled-back
        // update runs again, still reads the file under its old name.
        let legacy = layout.root.join(FROM_CONFIG_FILE);
        let config = match std::fs::copy(&legacy, layout.config_file()) {
            Ok(_) => {
                rename_runner_limits(&layout.config_file())?;
                format!("copied {FROM_CONFIG_FILE} to config.toml")
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                "no configuration file to copy".into()
            }
            Err(e) => return Err(e.into()),
        };
        Ok(if runner {
            format!("{config}; renamed the automatic runner {FROM_AUTO_RUNNER} to {AUTO_RUNNER}")
        } else {
            config
        })
    })
    .map(Some)
}

/// Moves the automatic runner to its new name. Its tokens go: the service
/// mints the automatic token again at every start. Its past attempts keep the
/// name they ran under.
fn rename_auto_runner(w: &Wtx) -> anyhow::Result<bool> {
    let new = Member::key(AUTO_RUNNER);
    anyhow::ensure!(
        !w.exists(store::MEMBERS, &new)?,
        "the member {AUTO_RUNNER} exists; rename it with the previous build before the upgrade gives the automatic runner that name"
    );
    let old = Member::key(FROM_AUTO_RUNNER);
    let Some(mut runner) = w.get::<Member>(store::MEMBERS, &old)? else {
        return Ok(false);
    };
    for token in w.prefix_all::<Token>(store::TOKENS, &[])? {
        if crate::util::fold(&token.member) == FROM_AUTO_RUNNER {
            crate::auth::revoke(w, &token)?;
        }
    }
    if let Some(avatar) = w.get_raw(store::AVATARS, &old)? {
        w.put_raw(store::AVATARS, &new, &avatar)?;
        w.del(store::AVATARS, &old)?;
    }
    w.del(store::MEMBERS, &old)?;
    runner.name = AUTO_RUNNER.into();
    if runner.display_name == FROM_RUNNER_DISPLAY_NAME {
        runner.display_name = AUTO_RUNNER_DISPLAY_NAME.into();
    }
    w.put(store::MEMBERS, &new, &runner)?;
    Ok(true)
}

/// Moves the automatic runner's saved limits to its new name.
fn rename_runner_limits(file: &std::path::Path) -> anyhow::Result<()> {
    let text = std::fs::read_to_string(file)?;
    let mut doc: toml_edit::DocumentMut = text.parse()?;
    let limits = doc
        .get_mut("ci")
        .and_then(|ci| ci.get_mut("runner_limits"))
        .and_then(toml_edit::Item::as_table_like_mut);
    if let Some(limits) = limits
        && let Some(saved) = limits.remove(FROM_AUTO_RUNNER)
    {
        limits.insert(AUTO_RUNNER, saved);
        std::fs::write(file, doc.to_string())?;
    }
    Ok(())
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
    fn upgrade_copies_the_configuration_file_and_renames_the_automatic_runner() {
        use crate::model::Workspace;
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let workspace = db
            .write_sync(|w| {
                let workspace = crate::domain::repos::insert_workspace(w, "acme".into(), None)?;
                insert_runner(w, FROM_RUNNER_DISPLAY_NAME)?;
                crate::auth::issue_token(w, FROM_AUTO_RUNNER, crate::auth::AUTO_LABEL)?;
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
        let settings = "[ci]\nqueue_max = 9 # kept\n\n[ci.runner_limits.branvil-runner]\nmax_cpus = 3\nmax_memory_mib = 2048\nwork_quota_mib = 4096\nmax_timeout_secs = 600\ncache_max_mib = 1024\nmax_jobs = 1\n";
        std::fs::write(&legacy, settings).unwrap();
        let root = temporary.path().join("execution");
        let done = upgrade_with_roots(&layout, [("acme".into(), root.clone())].into())
            .unwrap()
            .unwrap();
        assert_eq!(
            done.summary,
            "copied branvil.toml to config.toml; renamed the automatic runner branvil-runner to hub-runner"
        );
        assert_eq!(std::fs::read_to_string(&legacy).unwrap(), settings);
        let copied = std::fs::read_to_string(temporary.path().join("config.toml")).unwrap();
        assert_eq!(copied, settings.replace("branvil-runner", "hub-runner"));
        let config = crate::config::Config::load(None, temporary.path()).unwrap();
        assert_eq!(config.ci.queue_max, 9);
        assert_eq!(config.ci.runner_limits[AUTO_RUNNER].max_cpus, 3);
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let record: Workspace = db
            .read_sync(|r| r.get(store::WORKSPACES, &Workspace::key(&workspace)))
            .unwrap()
            .unwrap();
        assert_eq!(record.execution_root, Some(root));
        let (runner, old, tokens) = db
            .read_sync(|r| {
                Ok((
                    r.get::<Member>(store::MEMBERS, &Member::key(AUTO_RUNNER))?,
                    r.get::<Member>(store::MEMBERS, &Member::key(FROM_AUTO_RUNNER))?,
                    r.prefix_all::<Token>(store::TOKENS, &[])?,
                ))
            })
            .unwrap();
        let runner = runner.unwrap();
        assert_eq!(runner.name, "hub-runner");
        assert_eq!(runner.display_name, "GuildBotics runner (automatic)");
        assert!(old.is_none());
        assert!(tokens.is_empty());
        assert!(
            db.read_sync(crate::model::index_problems)
                .unwrap()
                .is_empty()
        );
    }

    fn insert_runner(w: &Wtx, display_name: &str) -> crate::error::AppResult<Member> {
        w.put(
            store::MEMBERS,
            &Member::key(FROM_AUTO_RUNNER),
            &serde_json::json!({"name": FROM_AUTO_RUNNER, "display_name": display_name,
                "kind": "runner", "accounts": [], "reviewers": [], "created_at": "fixture"}),
        )?;
        Ok(w.get(store::MEMBERS, &Member::key(FROM_AUTO_RUNNER))?
            .unwrap())
    }

    #[test]
    fn upgrade_refuses_to_take_the_name_of_another_member() {
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.write_sync(|w| {
            crate::domain::members::insert_member(
                w,
                &crate::domain::members::NewMember {
                    name: AUTO_RUNNER.into(),
                    display_name: None,
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
        let error = upgrade(&layout).err().unwrap().to_string();
        assert!(error.contains("the member hub-runner exists"), "{error}");
        assert!(store::check_version(&layout.db()).is_err());
    }

    #[test]
    fn upgrade_keeps_the_defaults_and_a_runner_name_the_owner_chose() {
        let temporary = tempfile::tempdir().unwrap();
        let layout = Layout::new(temporary.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.write_sync(|w| {
            insert_runner(w, "Hub runner")?;
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
        assert_eq!(
            done.summary,
            "no configuration file to copy; renamed the automatic runner branvil-runner to hub-runner"
        );
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
