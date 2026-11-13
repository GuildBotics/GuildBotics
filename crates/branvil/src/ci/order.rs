//! Shared first-parent history for estimates and admission order.
use std::collections::BTreeMap;

use super::{Ci, ESTIMATE_DEPTH, PipelineState, queue};
use crate::model::Repo;

pub(super) struct History {
    pub took: BTreeMap<String, u64>,
    previous: Option<(bool, Vec<queue::Attempt>)>,
}

impl Ci {
    /// Estimates include this commit; the previous run strictly excludes it.
    pub(super) async fn job_history(&self, repo: &Repo, sha: &str) -> Option<History> {
        #[cfg(test)]
        self.inner
            .history_reads
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = self.inner.git.repo_dir(&repo.path);
        let depth = format!("--max-count={}", ESTIMATE_DEPTH + 1);
        let ancestors = match self
            .inner
            .git
            .run_ok(Some(&dir), &["rev-list", "--first-parent", &depth, sha])
            .await
        {
            Ok(out) => out.lines().map(str::to_string).collect::<Vec<_>>(),
            Err(e) => {
                tracing::warn!("CI: cannot list the ancestors of {sha} for job history: {e}");
                return None;
            }
        };
        let id = repo.id.clone();
        match self
            .inner
            .store
            .read(move |r| {
                let mut history = History {
                    took: BTreeMap::new(),
                    previous: None,
                };
                for (i, sha) in ancestors.iter().enumerate() {
                    if i > 0
                        && history.previous.is_none()
                        && let Some(run) = queue::get_run(r, &id, sha)?
                    {
                        let jobs = match &run.pipeline {
                            PipelineState::Jobs { jobs } => jobs.as_slice(),
                            _ => &[],
                        };
                        history.previous = Some((
                            run.cache_scope == "protected",
                            queue::current_attempts(r, &id, sha, jobs)?,
                        ));
                    }
                    if i < ESTIMATE_DEPTH {
                        for a in queue::attempts_of(r, &id, sha)?.into_iter().rev() {
                            if a.conclusion.as_deref() == Some("success")
                                && a.reused_from.is_none()
                                && let Some(secs) = queue::duration_secs(&a)
                            {
                                history.took.entry(a.job.name).or_insert(secs);
                            }
                        }
                    }
                }
                Ok(history)
            })
            .await
        {
            Ok(history) => Some(history),
            Err(e) => {
                tracing::warn!("CI: cannot read job history: {e}");
                None
            }
        }
    }

    pub(super) async fn admission_order(
        &self,
        repo: &Repo,
        sha: &str,
        pipeline: &PipelineState,
    ) -> Vec<String> {
        let PipelineState::Jobs { jobs } = pipeline else {
            return Vec::new();
        };
        let Some(history) = self.job_history(repo, sha).await else {
            return Vec::new();
        };
        history.order(jobs)
    }
}

impl History {
    fn order(&self, jobs: &[super::pipeline::JobDef]) -> Vec<String> {
        let mut jobs: Vec<_> = jobs.iter().collect();
        jobs.sort_by_key(|job| {
            let group = self
                .previous
                .as_ref()
                .and_then(|(protected, attempts)| {
                    attempts
                        .iter()
                        .find(|a| a.job.name == job.name)
                        .map(|a| group(*protected, a))
                })
                .unwrap_or(1);
            let took = self.took.get(&job.name).copied();
            (group, took.is_none(), took)
        });
        jobs.into_iter().map(|j| j.name.clone()).collect()
    }
}

fn group(protected: bool, a: &queue::Attempt) -> u8 {
    if a.status != "completed" || a.path_skip.is_some() || a.superseded {
        return 1;
    }
    match a.conclusion.as_deref() {
        Some("success") => 2,
        Some("failure" | "timed_out" | "startup_failure" | "cancelled") if !protected => 0,
        _ => 1,
    }
}

#[cfg(test)]
mod tests {
    use super::super::pipeline;
    use super::super::tests::{commit_all, setup};
    use super::*;
    use crate::store;

    const T0: &str = "2026-11-14T00:00:00Z";
    const T1: &str = "2026-11-14T00:00:10Z";
    const T2: &str = "2026-11-14T00:00:30Z";

    fn pipeline() -> PipelineState {
        PipelineState::Jobs { jobs: pipeline::parse("[[jobs]]\nname='a'\nimage='alpine'\ncommands=['true']\n[[jobs]]\nname='b'\nimage='alpine'\ncommands=['true']\n[[jobs]]\nname='c'\nimage='alpine'\ncommands=['true']\n[[jobs]]\nname='d'\nimage='alpine'\ncommands=['true']\n").unwrap() }
    }

    fn reg(repo: &Repo, sha: &str, protected: bool) -> queue::Registration {
        queue::Registration {
            order: vec!["c".into(), "a".into(), "d".into(), "b".into()],
            inputs: BTreeMap::new(),
            selection: None,
            repo: repo.id.clone(),
            repo_path: repo.path.clone(),
            sha: sha.into(),
            tree: None,
            branch: "main".into(),
            protected,
            pipeline: pipeline(),
            network: false,
        }
    }

    fn names(r: &impl store::Rd, repo: &Repo, sha: &str) -> Vec<String> {
        let mut attempts = queue::attempts_of(r, &repo.id, sha).unwrap();
        attempts.sort_by_key(|a| a.seq);
        attempts.into_iter().map(|a| a.job.name).collect()
    }

    #[tokio::test]
    async fn history_orders_new_heads_and_default_registration_and_waiting_admission() {
        let (temp, mut ci, repo) = setup();
        std::sync::Arc::get_mut(&mut ci.inner)
            .unwrap()
            .config
            .queue_max = 4;
        ci.inner
            .store
            .write_sync(|w| w.put(store::REPOS, &Repo::key(&repo.id), &repo))
            .unwrap();
        let git = ci.inner.git.clone();
        let dir = git.repo_dir(&repo.path);
        git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        git.run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        std::fs::create_dir_all(work.join(".branvil")).unwrap();
        let PipelineState::Jobs { jobs } = pipeline() else {
            unreachable!()
        };
        let text = jobs
            .iter()
            .map(|j| {
                format!(
                    "[[jobs]]\nname='{}'\nimage='alpine'\ncommands=['true']\n",
                    j.name
                )
            })
            .collect::<String>();
        std::fs::write(work.join(".branvil/ci.toml"), text).unwrap();
        let h1 = commit_all(&git, &work, "H1").await;
        let gap = commit_all(&git, &work, "no run here").await;
        let h2 = commit_all(&git, &work, "H2").await;
        git.run_ok(
            None,
            &[
                "-C",
                work.to_str().unwrap(),
                "push",
                "-q",
                dir.to_str().unwrap(),
                "main",
            ],
        )
        .await
        .unwrap();
        assert_eq!(
            ci.admission_order(&repo, &h1, &pipeline()).await,
            ["a", "b", "c", "d"],
            "no recorded ancestor"
        );
        let (id, sha, registration) = (repo.id.clone(), h1.clone(), reg(&repo, &h1, false));
        ci.inner
            .store
            .write(move |w| {
                queue::register(w, registration, 10, T0)?;
                for (conclusion, end) in [("failure", T1), ("success", T2), ("success", T1)] {
                    let c = queue::claim(w, "r", 60, T0)?.unwrap();
                    queue::finish(
                        w,
                        &c.attempt.id,
                        &c.lease,
                        conclusion,
                        None,
                        queue::ResultInfo::default(),
                        10,
                        end,
                    )?;
                }
                queue::branch_left(w, &id, &sha, "main", "next", T2)?;
                Ok(())
            })
            .await
            .unwrap();
        assert_eq!(
            ci.admission_order(&repo, &h2, &pipeline()).await,
            ["c", "b", "d", "a"]
        );
        assert_eq!(
            ci.admission_order(&repo, &gap, &pipeline()).await,
            ["c", "b", "d", "a"]
        );
        // A new head above a protected failure puts it in B, after b on
        // their pipeline-order tie, rather than ahead of b.
        for protected in [true, false] {
            let (id, sha) = (repo.id.clone(), h1.clone());
            ci.inner
                .store
                .write(move |w| {
                    let mut run = queue::get_run(w, &id, &sha)?.unwrap();
                    run.cache_scope = if protected {
                        "protected"
                    } else {
                        "branch:feature"
                    }
                    .into();
                    queue::put_run(w, &run)
                })
                .await
                .unwrap();
            let expected = if protected {
                ["b", "c", "d", "a"]
            } else {
                ["c", "b", "d", "a"]
            };
            assert_eq!(ci.admission_order(&repo, &h2, &pipeline()).await, expected);
        }
        // The default-branch registration uses the very same order.
        ci.consider(&repo, "refs/heads/main", &h2).await.unwrap();
        let (r, s) = (repo.clone(), h2.clone());
        let before = ci
            .inner
            .store
            .read(move |rd| queue::attempts_of(rd, &r.id, &s))
            .await
            .unwrap();
        let mut by_seq = before.clone();
        by_seq.sort_by_key(|a| a.seq);
        assert_eq!(
            by_seq
                .iter()
                .map(|a| a.job.name.as_str())
                .collect::<Vec<_>>(),
            ["c", "b", "d", "a"]
        );
        let reads = ci
            .inner
            .history_reads
            .load(std::sync::atomic::Ordering::Relaxed);
        ci.consider(&repo, "refs/heads/main", &h2).await.unwrap();
        assert_eq!(
            ci.inner
                .history_reads
                .load(std::sync::atomic::Ordering::Relaxed),
            reads,
            "reconciliation does not read history"
        );
        let (r, s) = (repo.clone(), h2.clone());
        assert_eq!(
            ci.inner
                .store
                .read(move |rd| queue::attempts_of(rd, &r.id, &s))
                .await
                .unwrap(),
            before
        );
        let waiting = reg(&repo, &gap, false);
        ci.inner
            .store
            .write(move |w| queue::register(w, waiting, 4, T2))
            .await
            .unwrap();
        let reads = ci
            .inner
            .history_reads
            .load(std::sync::atomic::Ordering::Relaxed);
        ci.admit_waiting().await;
        assert_eq!(
            ci.inner
                .history_reads
                .load(std::sync::atomic::Ordering::Relaxed),
            reads,
            "no Git history is read for runs without room"
        );
        let (r, s) = (repo.clone(), h2.clone());
        ci.inner
            .store
            .write(move |w| {
                for a in queue::attempts_of(w, &r.id, &s)? {
                    queue::cancel(w, &a.id, "test", T2)?;
                }
                Ok(())
            })
            .await
            .unwrap();
        ci.admit_waiting().await;
        let (r, s) = (repo.clone(), gap.clone());
        assert_eq!(
            ci.inner
                .store
                .read(move |rd| Ok(names(rd, &r, &s)))
                .await
                .unwrap(),
            ["c", "b", "d", "a"]
        );
        assert!(ci.job_history(&repo, "missing-commit").await.is_none());
        assert!(
            ci.admission_order(&repo, "missing-commit", &pipeline())
                .await
                .is_empty()
        );
    }

    #[test]
    fn classification_covers_all_results_and_protected_failures() {
        let (_temp, ci, repo) = setup();
        ci.inner
            .store
            .write_sync(|w| {
                queue::register(w, reg(&repo, "sha", false), 10, T0)?;
                let mut a = queue::attempts_of(w, &repo.id, "sha")?.remove(0);
                assert_eq!(group(false, &a), 1);
                a.status = "completed".into();
                for (result, expected) in [
                    ("success", 2),
                    ("failure", 0),
                    ("timed_out", 0),
                    ("startup_failure", 0),
                    ("cancelled", 0),
                    ("skipped", 1),
                ] {
                    a.conclusion = Some(result.into());
                    assert_eq!(group(false, &a), expected, "{result}");
                    assert_eq!(group(true, &a), if result == "success" { 2 } else { 1 });
                    a.superseded = true;
                    assert_eq!(group(false, &a), 1);
                    a.superseded = false;
                }
                a.conclusion = Some("failure".into());
                let mut h = History {
                    took: BTreeMap::from([("a".into(), 2), ("b".into(), 2)]),
                    previous: None,
                };
                let PipelineState::Jobs { jobs } = pipeline() else {
                    unreachable!()
                };
                assert_eq!(
                    h.order(&jobs),
                    ["a", "b", "c", "d"],
                    "equal estimates and unknowns are stable without a previous run"
                );
                a.job.name = "d".into();
                h.previous = Some((false, vec![a.clone()]));
                assert_eq!(
                    h.order(&jobs),
                    ["d", "a", "b", "c"],
                    "new jobs are B, behind a failure"
                );
                h.previous = Some((true, vec![a.clone()]));
                assert_eq!(
                    h.order(&jobs),
                    ["a", "b", "c", "d"],
                    "protected failures are B"
                );
                a.conclusion = Some("success".into());
                a.reused_from = Some("source".into());
                h.previous = Some((false, vec![a.clone()]));
                assert_eq!(h.order(&jobs), ["a", "b", "c", "d"], "reused success is C");
                a.path_skip = Some(queue::PathSkip {
                    paths: Vec::new(),
                    bases: Vec::new(),
                });
                assert_eq!(group(false, &a), 1);
                Ok(())
            })
            .unwrap();
    }
}
