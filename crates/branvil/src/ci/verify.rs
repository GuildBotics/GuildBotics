//! Durable, serial verification of the default branch.
use super::{Ci, queue};
use crate::error::AppResult;
use crate::model::Repo;
use crate::store::{self, Key, Rd};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Verified {
    pub sha: String,
    pub at: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FailedJob {
    pub name: String,
    pub attempt_id: Option<String>,
    pub details: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Verification {
    pub sha: String,
    pub base: Option<String>,
    pub state: String,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub failed_jobs: Vec<FailedJob>,
    pub failing_since: Option<String>,
    pub last_verified: Option<Verified>,
}

pub fn get(r: &impl Rd, repo: &str) -> AppResult<Option<Verification>> {
    r.get(store::CI_VERIFICATIONS, &Key::new().s(repo).bytes())
}

pub fn latest_verified(r: &impl Rd, repo: &str) -> AppResult<Option<Verified>> {
    Ok(get(r, repo)?.and_then(|v| v.last_verified))
}

pub fn running(r: &impl Rd, repo: &str, sha: &str) -> AppResult<bool> {
    Ok(get(r, repo)?.is_some_and(|v| v.state == "running" && v.sha == sha))
}

impl Ci {
    /// The caller holds the repository lock. Persist intent before registering work.
    pub(super) async fn advance_verification(
        &self,
        repo: &Repo,
        recover: bool,
    ) -> AppResult<usize> {
        let id = repo.id.clone();
        let mut record = self.inner.store.read(move |r| get(r, &id)).await?;
        let dir = self.inner.git.repo_dir(&repo.path);
        let tip = self
            .inner
            .git
            .branch_tips(&dir)
            .await?
            .get(&repo.default_branch)
            .cloned();
        let mut changed = 0;
        if let Some(v) = record.as_mut().filter(|v| v.state == "running") {
            if recover {
                let reg = self
                    .registration(repo, &repo.default_branch, &v.sha, false)
                    .await?;
                changed += usize::from(self.register(repo, &v.sha, reg).await?);
            }
            let (id, sha) = (repo.id.clone(), v.sha.clone());
            let snapshot = self
                .inner
                .store
                .read(move |r| {
                    let Some(run) = queue::get_run(r, &id, &sha)? else {
                        return Ok(None);
                    };
                    let jobs = match &run.pipeline {
                        queue::PipelineState::Jobs { jobs } => jobs.as_slice(),
                        _ => &[],
                    };
                    let latest = queue::current_attempts(r, &id, &sha, jobs)?
                        .into_iter()
                        .map(|a| Ok((a.clone(), queue::source_of(r, &a)?)))
                        .collect::<AppResult<Vec<_>>>()?;
                    Ok(Some((run, latest)))
                })
                .await?;
            if let Some((run, latest)) = snapshot {
                let checks = self.run_checks(repo, &run, latest)?.checks;
                if checks.iter().all(|c| c.status == "completed") {
                    let now = crate::util::now();
                    let passed = checks.iter().all(|c| c.succeeded());
                    if passed {
                        let name = format!("refs/branvil/verified/{}", v.sha);
                        let refs = self.inner.git.all_refs(&dir).await?;
                        if refs.get(&name) != Some(&v.sha) {
                            let created = self
                                .inner
                                .git
                                .update_ref(
                                    &dir,
                                    &name,
                                    &v.sha,
                                    crate::git::ZERO_SHA,
                                    "verify default branch",
                                )
                                .await?;
                            if !created {
                                return Err(crate::error::AppError::conflict(
                                    "Verified ref changed while recording success.",
                                ));
                            }
                        }
                        v.state = "passed".into();
                        v.last_verified = Some(Verified {
                            sha: v.sha.clone(),
                            at: now.clone(),
                        });
                        v.failing_since = None;
                    } else {
                        v.state = "failed".into();
                        v.failing_since.get_or_insert(v.sha.clone());
                        v.failed_jobs = checks
                            .into_iter()
                            .filter(|c| !c.succeeded())
                            .map(|c| FailedJob {
                                name: c.name,
                                attempt_id: c.attempt_id,
                                details: c.details,
                            })
                            .collect();
                    }
                    v.finished_at = Some(now.clone());
                    let (id, done) = (repo.id.clone(), v.clone());
                    self.inner
                        .store
                        .write(move |w| {
                            w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &done)
                        })
                        .await?;
                }
            }
        }
        // Also repairs a crash after recording completion but before detaching main.
        if let Some(v) = &record {
            if v.state == "running" {
                return Ok(changed);
            }
            if tip.as_ref() != Some(&v.sha) {
                let (id, sha, branch, by) = (
                    repo.id.clone(),
                    v.sha.clone(),
                    repo.default_branch.clone(),
                    tip.clone().unwrap_or_default(),
                );
                self.inner
                    .store
                    .write(move |w| {
                        queue::branch_left(w, &id, &sha, &branch, &by, &crate::util::now())
                    })
                    .await?;
            }
        }
        if let Some(sha) = tip.filter(|sha| record.as_ref().is_none_or(|v| &v.sha != sha)) {
            let last_verified = record.as_ref().and_then(|v| v.last_verified.clone());
            let v = Verification {
                sha: sha.clone(),
                base: last_verified.as_ref().map(|v| v.sha.clone()),
                state: "running".into(),
                started_at: crate::util::now(),
                finished_at: None,
                failed_jobs: vec![],
                failing_since: record.and_then(|v| v.failing_since),
                last_verified,
            };
            let id = repo.id.clone();
            self.inner
                .store
                .write(move |w| w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &v))
                .await?;
            let reg = self
                .registration(repo, &repo.default_branch, &sha, false)
                .await?;
            changed += usize::from(self.register(repo, &sha, reg).await?);
            self.publish(&[(repo.path.clone(), sha)]);
        }
        Ok(changed)
    }
}

pub fn start(app: crate::app::Shared) {
    use futures_util::StreamExt;
    let mut events = Box::pin(app.events.subscribe());
    tokio::spawn(async move {
        while let Some(notice) = events.next().await {
            let path = match notice {
                crate::events::Notice::Change(c) if c.kind == "check" => Some(c.path),
                crate::events::Notice::Reset => None,
                _ => continue,
            };
            let repos = app
                .store
                .read(move |r| r.prefix_all::<Repo>(store::REPOS, &[]))
                .await;
            if let Ok(repos) = repos {
                for repo in repos
                    .into_iter()
                    .filter(|r| path.as_ref().is_none_or(|p| p == &r.path))
                {
                    let id = repo.id.clone();
                    if let Err(e) = app
                        .locked(&id, move |app| async move {
                            app.ci.advance_verification(&repo, false).await
                        })
                        .await
                    {
                        tracing::error!("CI verification failed: {e}");
                    }
                }
            }
        }
    });
}
