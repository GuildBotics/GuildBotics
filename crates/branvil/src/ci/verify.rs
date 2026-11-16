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

#[derive(Debug, Serialize)]
pub struct Retry {
    pub number: u64,
    pub max: u64,
}

#[derive(Debug, Serialize)]
pub struct Current {
    pub sha: String,
    pub base: Option<String>,
    pub state: String,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub failed_jobs: Vec<FailedJob>,
    pub retry: Retry,
}

#[derive(Debug, Serialize)]
pub struct LatestVerified {
    pub sha: String,
    pub verified_at: String,
}

#[derive(Debug, Serialize)]
pub struct RepairTicket {
    pub number: u64,
    pub assignee: Option<String>,
    pub url: String,
}

#[derive(Debug, Serialize)]
pub struct MainState {
    pub repair: Option<RepairTicket>,
    pub branch: String,
    pub tip: Option<String>,
    pub state: &'static str,
    pub ahead: Option<u64>,
    pub latest_verified: Option<LatestVerified>,
    pub current: Option<Current>,
    pub failing_since: Option<String>,
}

/// Shared view for HTTP and internal consumers. The repository lock keeps refs
/// and the verification record in the same observation.
pub async fn main_state(app: &crate::app::App, repo: &Repo) -> AppResult<MainState> {
    let dir = app.git.repo_dir(&repo.path);
    let tip = app
        .git
        .branch_tips(&dir)
        .await?
        .get(&repo.default_branch)
        .cloned();
    let id = repo.id.clone();
    let (record, number, repair) = app
        .store
        .read(move |r| {
            let record = get(r, &id)?;
            let mut number = 0;
            if let Some(v) = &record {
                number = queue::attempts_of(r, &id, &v.sha)?
                    .iter()
                    .filter(|a| a.purpose == queue::Purpose::Check)
                    .filter_map(|a| a.auto_retry.as_ref().map(|r| r.number))
                    .max()
                    .unwrap_or(0);
            }
            Ok((record, number, crate::repair::open_ticket(r, &id)?))
        })
        .await?;
    let latest_verified = record
        .as_ref()
        .and_then(|v| v.last_verified.as_ref())
        .map(|v| LatestVerified {
            sha: v.sha.clone(),
            verified_at: v.at.clone(),
        });
    let ahead = if let Some(tip) = &tip {
        let range = latest_verified
            .as_ref()
            .map_or_else(|| tip.clone(), |v| format!("{}..{tip}", v.sha));
        Some(
            app.git
                .run_ok(
                    Some(&dir),
                    &["rev-list", "--first-parent", "--count", &range, "--"],
                )
                .await?
                .parse()
                .map_err(crate::error::AppError::internal)?,
        )
    } else {
        None
    };
    let state = if tip.is_none() {
        "empty"
    } else if record.as_ref().is_some_and(|v| v.state == "running") {
        "verifying"
    } else if record.as_ref().is_some_and(|v| v.state == "failed") {
        "failing"
    } else if ahead == Some(0) {
        "verified"
    } else {
        "pending"
    };
    let failing_since = record.as_ref().and_then(|v| v.failing_since.clone());
    Ok(MainState {
        repair: repair.map(|t| RepairTicket {
            number: t.number,
            assignee: t.assignee.map(|a| a.name),
            url: format!("{}/{}/-/tickets/{}", app.base_url, repo.path, t.number),
        }),
        branch: repo.default_branch.clone(),
        tip,
        state,
        ahead,
        latest_verified,
        current: record.map(|record| Current {
            sha: record.sha,
            base: record.base,
            state: record.state,
            started_at: record.started_at,
            finished_at: record.finished_at,
            failed_jobs: record.failed_jobs,
            retry: Retry {
                number,
                max: queue::MAX_AUTO_RETRIES,
            },
        }),
        failing_since,
    })
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
                    .registration(repo, &repo.default_branch, &v.sha)
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
                    let closed = self
                        .inner
                        .store
                        .write(move |w| {
                            w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &done)?;
                            let mut closed = Vec::new();
                            if done.state == "passed" {
                                closed = w.prefix_keys(
                                    store::IX_OPEN_REPAIRS,
                                    &Key::new().s(&id).bytes(),
                                )?;
                                crate::repair::passed(w, &id, &done.sha)?;
                                super::narrow::abandon(w, &id, &now)?;
                            } else {
                                crate::repair::failed(w, &id, &done.sha, &done.failed_jobs)?;
                                super::narrow::start(w, &id, &done, &now)?;
                            }
                            Ok(closed)
                        })
                        .await?;
                    for key in closed {
                        if let Some(parts) = store::decode_key(&key, "su") {
                            self.inner.events.publish(
                                "ticket",
                                &repo.path,
                                parts[1].u().to_string(),
                            );
                        }
                        self.inner.events.publish(
                            "board",
                            crate::domain::tickets::workspace_of(&repo.path),
                            "",
                        );
                    }
                    self.inner.events.publish("verification", &repo.path, "");
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
            self.inner.events.publish("verification", &repo.path, "");
            let reg = self.registration(repo, &repo.default_branch, &sha).await?;
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
                    let repo_path = repo.path.clone();
                    if let Err(e) = app
                        .locked(&id, move |app| async move {
                            let count = app.ci.advance_verification(&repo, false).await?;
                            app.ci.advance_narrowings(&repo).await?;
                            Ok::<_, crate::error::AppError>(count)
                        })
                        .await
                    {
                        tracing::error!("CI verification failed: {e}");
                    }
                    app.events.publish("verification", &repo_path, "");
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ci::tests::{app, commit_all, setup};

    #[tokio::test]
    async fn main_state_precedence_distance_and_nulls() {
        let (temp, ci, repo) = setup();
        let app = app(&temp, &ci);
        let dir = app.git.repo_dir(&repo.path);
        app.git.init_bare(&dir, "main").await.unwrap();
        let empty = main_state(&app, &repo).await.unwrap();
        assert_eq!(empty.state, "empty");
        assert!(
            empty.tip.is_none()
                && empty.ahead.is_none()
                && empty.current.is_none()
                && empty.latest_verified.is_none()
        );
        let work = temp.path().join("work");
        app.git
            .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        let mut shas = vec![];
        for i in 0..4 {
            shas.push(commit_all(&app.git, &work, &format!("commit {i}")).await);
        }
        app.git
            .run_ok(
                None,
                &[
                    "-C",
                    work.to_str().unwrap(),
                    "push",
                    "-q",
                    dir.to_str().unwrap(),
                    "HEAD:refs/heads/main",
                ],
            )
            .await
            .unwrap();
        let pending = main_state(&app, &repo).await.unwrap();
        assert_eq!(pending.state, "pending");
        assert_eq!(pending.ahead, Some(4));
        assert!(pending.latest_verified.is_none());
        for (status, verified, expected, ahead) in [
            ("running", None, "verifying", 4),
            ("failed", None, "failing", 4),
            ("passed", Some(0), "pending", 3),
            ("passed", Some(3), "verified", 0),
        ] {
            let record = Verification {
                sha: shas[0].clone(),
                base: None,
                state: status.into(),
                started_at: "start".into(),
                finished_at: None,
                failed_jobs: vec![],
                failing_since: None,
                last_verified: verified.map(|i| Verified {
                    sha: shas[i].clone(),
                    at: "verified-time".into(),
                }),
            };
            let id = repo.id.clone();
            app.store
                .write(move |w| w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &record))
                .await
                .unwrap();
            let state = main_state(&app, &repo).await.unwrap();
            assert_eq!(state.state, expected);
            assert_eq!(state.ahead, Some(ahead));
            assert_eq!(
                state.latest_verified.as_ref().map(|v| v.sha.as_str()),
                verified.map(|i| shas[i].as_str())
            );
            assert_eq!(state.current.as_ref().unwrap().retry.number, 0);
            assert_eq!(state.current.unwrap().retry.max, 3);
        }
        use futures_util::StreamExt;
        let mut events = Box::pin(app.events.subscribe());
        app.ci.advance_verification(&repo, false).await.unwrap();
        let notice = tokio::time::timeout(std::time::Duration::from_secs(1), events.next())
            .await
            .unwrap()
            .unwrap();
        let crate::events::Notice::Change(change) = notice else {
            panic!("expected change")
        };
        assert_eq!(change.kind, "verification");
        assert_eq!(change.path, repo.path);
        assert_eq!(main_state(&app, &repo).await.unwrap().state, "verifying");
    }
}
