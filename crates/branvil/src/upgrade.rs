//! 31 → 32: replace serial landing gates with per-pull conditions.
use crate::config::Layout;
use crate::model::{Landing, Pull, Repo};
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 31;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);
fn gate(name: &str) -> bool {
    name.starts_with("refs/branvil/gate/") || name.starts_with("refs/branvil/failed-gate/")
}
pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    let layout = layout.clone();
    Ok(Some(db.finish(move |w| {
        let repos: Vec<Repo> = w.prefix_all(store::REPOS, &[])?;
        let mut pairs = Vec::new();
        for old in w.prefix_all::<serde_json::Value>(store::LANDING, &[])? {
            let repo = old["repo"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("missing repo"))?;
            let n = old["number"]
                .as_u64()
                .ok_or_else(|| anyhow::anyhow!("missing number"))?;
            if let Some(p) = w
                .get::<Pull>(store::PULLS, &Pull::key(repo, n))?
                .filter(|p| p.state == "open")
            {
                pairs.push((
                    p,
                    old["head_sha"].as_str().unwrap_or_default().to_string(),
                    old["base_sha"].as_str().unwrap_or_default().to_string(),
                ));
            }
        }
        let conflicts = std::thread::spawn(
            move || -> anyhow::Result<std::collections::BTreeMap<(String, u64), Vec<String>>> {
                tokio::runtime::Runtime::new()?.block_on(async move {
                    let git = crate::git::Git::new(layout, 1, std::time::Duration::from_secs(60));
                    let mut conflicts = std::collections::BTreeMap::new();
                    for repo in repos {
                        let dir = git.repo_dir(&repo.path);
                        let refs = git.all_refs(&dir).await?;
                        for name in refs.keys().filter(|n| gate(n)) {
                            git.run_ok(Some(&dir), &["update-ref", "-d", name]).await?;
                        }
                        for (p, head, base) in pairs.iter().filter(|(p, _, _)| p.repo == repo.id) {
                            let current = refs.get(&format!("refs/heads/{}", p.head)) == Some(head)
                                && refs.get(&format!("refs/heads/{}", p.base)) == Some(base);
                            let files = match git.conflicts(&dir, base, head).await {
                                Ok(files) => files,
                                Err(_) if !current => vec![],
                                Err(e) => return Err(e.into()),
                            };
                            conflicts.insert((repo.id.clone(), p.number), files);
                        }
                    }
                    Ok(conflicts)
                })
            },
        )
        .join()
        .map_err(|_| anyhow::anyhow!("checking landing pairs panicked"))??;
        for (key, bytes) in w.prefix_raw(store::LANDING, &[], None, usize::MAX)? {
            let old: serde_json::Value = store::decode(&bytes)?;
            let repo = old["repo"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("landing repo missing"))?;
            let number = old["number"]
                .as_u64()
                .ok_or_else(|| anyhow::anyhow!("landing number missing"))?;
            let p: Option<Pull> = w.get(store::PULLS, &Pull::key(repo, number))?;
            if p.is_none_or(|p| p.state != "open") {
                w.del(store::LANDING, &key)?;
                continue;
            }
            let e = converted(
                &old,
                conflicts
                    .get(&(repo.to_string(), number))
                    .cloned()
                    .ok_or_else(|| anyhow::anyhow!("missing landing pair"))?,
            )?;
            w.put(store::LANDING, &key, &e)?;
        }
        for (key, _) in w.prefix_raw(store::COUNTERS, &[], None, usize::MAX)? {
            if store::decode_key(&key, "s").is_some_and(|p| p[0].s().starts_with("landing:")) {
                w.del(store::COUNTERS, &key)?;
            }
        }
        for (key, _) in w.prefix_raw(store::REFS, &[], None, usize::MAX)? {
            if store::decode_key(&key, "ss").is_some_and(|p| gate(p[1].s())) {
                w.del(store::REFS, &key)?;
            }
        }
        let runs: Vec<crate::ci::queue::Run> = w.prefix_all(store::CI_RUNS, &[])?;
        for run in runs {
            for branch in run.branches.iter().filter(|b| b.starts_with("gate/")) {
                crate::ci::queue::branch_left(
                    w,
                    &run.repo,
                    &run.sha,
                    branch,
                    "the landing gate was removed",
                    &crate::util::now(),
                )?;
            }
        }
        Ok("removed landing gates and queues".into())
    })?))
}

fn converted(old: &serde_json::Value, conflicts: Vec<String>) -> anyhow::Result<Landing> {
    let mut told = vec![];
    if old["state"] == "left" {
        if old["reason"]["code"] == "conflict" {
            told.push("conflict".into());
        }
        if old["reason"]["code"] == "host_failed"
            && let Some(jobs) = old["reason"]["jobs"].as_array()
        {
            told.extend(
                jobs.iter()
                    .filter_map(|j| j.as_str())
                    .map(|j| format!("host:{j}")),
            );
        }
    }
    Ok(Landing {
        repo: old["repo"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing repo"))?
            .into(),
        number: old["number"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("missing number"))?,
        head_sha: old["head_sha"].as_str().unwrap_or_default().into(),
        base_sha: old["base_sha"].as_str().unwrap_or_default().into(),
        conflicts,
        told,
        notified_at: if old["state"] == "passed" {
            serde_json::from_value(old["notified_at"].clone())?
        } else {
            None
        },
        error: None,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn every_old_state_keeps_only_its_per_head_notice_history() {
        for (state, code, told, notified) in [
            ("waiting", "", vec![], None),
            ("gating", "", vec![], None),
            ("passed", "", vec![], Some("sent")),
            ("left", "conflict", vec!["conflict"], None),
            ("left", "gate_failed", vec![], None),
            ("left", "host_failed", vec!["host:test"], None),
        ] {
            let old = json!({"repo":"r","number":1,"state":state,"head_sha":"head","base_sha":"base","reason":{"code":code,"jobs":["test"]},"notified_at":"sent","error":"old gate error"});
            let e = converted(&old, vec!["a.py".into()]).unwrap();
            assert_eq!(e.head_sha, "head");
            assert_eq!(e.base_sha, "base");
            assert_eq!(e.conflicts, vec!["a.py"]);
            assert_eq!(e.told, told);
            assert_eq!(e.notified_at.as_deref(), notified);
            assert_eq!(e.error, None);
        }
    }
}
