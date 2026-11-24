//! 42 → 43: required checks move from the repository record to each branch's
//! `.guildbotics-ci.toml`.
use std::collections::BTreeSet;

use anyhow::Context;
use serde_json::Value;

use crate::ci::pipeline::{self, RequiredJobs};
use crate::config::Layout;
use crate::git::Git;
use crate::store::{self, Rd, Tbl, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 42;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    // Before `finish`: a refusal leaves no copy of the database behind.
    let (repos, pulls) = db.read(|r| Ok((rows(r, store::REPOS)?, rows(r, store::PULLS)?)))?;
    let problems = unmoved(layout, &repos, &pulls)?;
    if !problems.is_empty() {
        anyhow::bail!(
            "required checks are not in .guildbotics-ci.toml yet, so pull requests would land without them, and nothing was changed: {}",
            problems.join("; ")
        );
    }
    db.finish(|w| {
        let repos = update_rows(w, store::REPOS, |repo| {
            repo.as_object_mut()
                .context("repository record is not an object")?
                .remove("required_checks")
                .context("repository record has no required_checks")?;
            Ok(())
        })?;
        // An empty base makes the next sweep judge the pair again.
        let landings = update_rows(w, store::LANDING, |landing| {
            let landing = landing
                .as_object_mut()
                .context("landing record is not an object")?;
            landing.insert("required_jobs".into(), Value::Array(Vec::new()));
            landing.insert("base_sha".into(), Value::String(String::new()));
            Ok(())
        })?;
        Ok(format!(
            "moved required checks out of {repos} repositories and reset {landings} landing records"
        ))
    })
    .map(Some)
}

fn rows(r: &impl Rd, table: Tbl) -> anyhow::Result<Vec<Value>> {
    let mut out = Vec::new();
    let mut after = None;
    loop {
        let page = r.prefix_raw(table, &[], after.as_deref(), 512)?;
        let Some((last, _)) = page.last() else {
            return Ok(out);
        };
        after = Some(last.clone());
        for (_, raw) in &page {
            out.push(store::decode(raw)?);
        }
    }
}

/// Every branch that receives pull requests or is protected, for each repository
/// with required checks, must already require them in its own pipeline. Otherwise the switch
/// would silently leave its pull requests with fewer required checks.
fn unmoved(layout: &Layout, repos: &[Value], pulls: &[Value]) -> anyhow::Result<Vec<String>> {
    let git = Git::new(layout.clone(), 4, std::time::Duration::from_secs(60));
    // The caller may run inside a runtime, which cannot block on another.
    std::thread::scope(|scope| {
        scope
            .spawn(|| {
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()?
                    .block_on(unmoved_in(&git, repos, pulls))
            })
            .join()
            .map_err(|_| anyhow::anyhow!("checking the required checks panicked"))?
    })
}

async fn unmoved_in(git: &Git, repos: &[Value], pulls: &[Value]) -> anyhow::Result<Vec<String>> {
    let mut problems = Vec::new();
    for repo in repos {
        let names: Vec<String> = serde_json::from_value(repo["required_checks"].clone())
            .context("repository record has no required_checks")?;
        if names.is_empty() {
            continue;
        }
        let path = repo["path"].as_str().context("repository without a path")?;
        let mut bases: BTreeSet<&str> = pulls
            .iter()
            .filter(|p| p["repo"] == repo["id"] && p["state"] == "open")
            .filter_map(|p| p["base"].as_str())
            .collect();
        bases.insert(
            repo["default_branch"]
                .as_str()
                .context("repository without a default branch")?,
        );
        bases.extend(
            repo["protected_branches"]
                .as_array()
                .context("repository without protected branches")?
                .iter()
                .filter_map(Value::as_str),
        );
        let dir = git.repo_dir(path);
        let tips = git
            .branch_tips(&dir)
            .await
            .map_err(|e| anyhow::anyhow!("{path}: {}", e.message))?;
        for base in bases {
            let Some(tip) = tips.get(base) else { continue };
            let required = pipeline::read_required(git, &dir, tip)
                .await
                .map_err(|e| anyhow::anyhow!("{path} {base}: {}", e.message))?;
            let missing: Vec<&str> = names
                .iter()
                .filter(|n| !matches!(&required, RequiredJobs::Jobs(jobs) if jobs.contains(n)))
                .map(String::as_str)
                .collect();
            if !missing.is_empty() {
                problems.push(format!("{path} {base}: {}", missing.join(", ")));
            }
        }
    }
    Ok(problems)
}

fn update_rows(
    w: &Wtx,
    table: Tbl,
    update: impl Fn(&mut Value) -> anyhow::Result<()>,
) -> anyhow::Result<usize> {
    let mut count = 0;
    let mut after = None;
    loop {
        let rows = w.prefix_raw(table, &[], after.as_deref(), 512)?;
        if rows.is_empty() {
            return Ok(count);
        }
        for (key, raw) in rows {
            let mut value = store::decode(&raw)?;
            update(&mut value)?;
            w.put(table, &key, &value)?;
            count += 1;
            after = Some(key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{Key, Store};
    use serde_json::json;

    const PIPELINE: &str = "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n";

    async fn fixture(dir: &std::path::Path, main: &str, feature: &str, open: bool) -> Layout {
        let layout = Layout::new(dir);
        layout.create().unwrap();
        let git = Git::new(layout.clone(), 2, std::time::Duration::from_secs(10));
        let bare = git.repo_dir("acme/demo");
        git.init_bare(&bare, "main").await.unwrap();
        let work = dir.join("work");
        let w = work.to_str().unwrap();
        git.run_ok(None, &["init", "-q", "-b", "main", w])
            .await
            .unwrap();
        for (branch, text) in [("main", main), ("feature", feature)] {
            git.run_ok(None, &["-C", w, "checkout", "-q", "-B", branch])
                .await
                .unwrap();
            std::fs::write(work.join(pipeline::PIPELINE_PATH), text).unwrap();
            git.run_ok(None, &["-C", w, "add", "-A"]).await.unwrap();
            let out = git
                .run_env(
                    None,
                    &["-C", w, "commit", "--allow-empty", "-qm", branch],
                    &[
                        ("GIT_AUTHOR_NAME", "Test"),
                        ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
                        ("GIT_COMMITTER_NAME", "Test"),
                        ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
                    ],
                )
                .await
                .unwrap();
            assert!(out.ok(), "{}", out.stderr);
        }
        git.run_ok(
            None,
            &[
                "-C",
                w,
                "push",
                "-q",
                bare.to_str().unwrap(),
                "main",
                "feature",
            ],
        )
        .await
        .unwrap();
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.write_sync(|w| {
            w.put(
                store::REPOS,
                b"repo",
                &json!({"id": "r", "path": "acme/demo", "default_branch": "main",
                        "protected_branches": if open { json!([]) } else { json!(["feature"]) },
                        "required_checks": ["test"], "post_merge_jobs": []}),
            )?;
            w.put(
                store::PULLS,
                b"open",
                &json!({"repo": "r", "state": if open { "open" } else { "closed" }, "base": "feature"}),
            )?;
            w.put(
                store::LANDING,
                b"landing",
                &json!({"repo": "r", "number": 1, "head_sha": "h", "base_sha": "b",
                        "conflicts": [], "told": ["conflict"], "notified_at": null, "error": null}),
            )?;
            w.put(store::META, &Key::new().s("schema_version").bytes(), &FROM)
        })
        .unwrap();
        drop(db);
        layout
    }

    fn version(layout: &Layout) -> u64 {
        let db = redb::Database::open(layout.db()).unwrap();
        let r = redb::ReadableDatabase::begin_read(&db).unwrap();
        r.get::<u64>(store::META, &Key::new().s("schema_version").bytes())
            .unwrap()
            .unwrap()
    }

    #[tokio::test]
    async fn upgrade_moves_required_checks_once_every_receiving_branch_declares_them() {
        let dir = tempfile::tempdir().unwrap();
        let required = format!("{PIPELINE}required=true\n");
        let layout = fixture(dir.path(), &required, &required, true).await;

        let upgraded = upgrade(&layout).unwrap().unwrap();
        assert_eq!(upgraded.from, FROM);
        assert_eq!(version(&layout), store::SCHEMA_VERSION);
        let db = redb::Database::open(layout.db()).unwrap();
        let r = redb::ReadableDatabase::begin_read(&db).unwrap();
        let repo: Value = r.get(store::REPOS, b"repo").unwrap().unwrap();
        assert!(repo.get("required_checks").is_none());
        let landing: Value = r.get(store::LANDING, b"landing").unwrap().unwrap();
        assert_eq!(landing["required_jobs"], json!([]));
        assert_eq!(landing["base_sha"], "");
        assert_eq!(landing["head_sha"], "h");
        assert_eq!(landing["told"], json!(["conflict"]));
    }

    #[tokio::test]
    async fn upgrade_stops_when_a_receiving_branch_would_lose_a_required_check() {
        for (main, feature, open, named) in [
            (PIPELINE, "required", true, "acme/demo main: test"),
            ("required", PIPELINE, true, "acme/demo feature: test"),
            ("required", "not toml", true, "acme/demo feature: test"),
            ("required", PIPELINE, false, "acme/demo feature: test"),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let marked = format!("{PIPELINE}required=true\n");
            let text = |t: &str| {
                if t == "required" {
                    marked.clone()
                } else {
                    t.into()
                }
            };
            let layout = fixture(dir.path(), &text(main), &text(feature), open).await;

            let error = upgrade(&layout).err().unwrap().to_string();
            assert!(error.contains(named), "{error}");
            assert_eq!(version(&layout), FROM, "{named}");
            let db = redb::Database::open(layout.db()).unwrap();
            let r = redb::ReadableDatabase::begin_read(&db).unwrap();
            let repo: Value = r.get(store::REPOS, b"repo").unwrap().unwrap();
            assert_eq!(repo["required_checks"], json!(["test"]));
            let copies = std::fs::read_dir(layout.db().parent().unwrap())
                .unwrap()
                .filter(|e| {
                    let name = e.as_ref().unwrap().file_name();
                    name.to_string_lossy().starts_with("branvil-schema-")
                })
                .count();
            assert_eq!(copies, 0, "a refusal leaves no copy: {named}");
        }
    }
}
