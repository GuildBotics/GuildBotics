//! Regression coverage of verification across registration and process boundaries.
use super::priority::Class;
use super::*;

struct Fixture {
    temp: tempfile::TempDir,
    ci: Ci,
    repo: Repo,
    sha: String,
}

impl Fixture {
    async fn new(names: &[&str]) -> Self {
        let (temp, ci, repo) = tests::setup();
        ci.inner.layout.create().unwrap();
        ci.inner
            .store
            .write_sync(|w| {
                w.put(store::REPOS, &Repo::key(&repo.id), &repo)?;
                crate::model::rebuild_indexes(w)?;
                Ok(())
            })
            .unwrap();
        let dir = ci.inner.git.repo_dir(&repo.path);
        ci.inner.git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        ci.inner
            .git
            .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        let pipeline: String = names
            .iter()
            .map(|name| format!("[[jobs]]\nname='{name}'\nimage='alpine'\ncommands=['true']\n"))
            .collect();
        std::fs::write(work.join(".guildbotics-ci.toml"), pipeline).unwrap();
        let sha = tests::commit_all(&ci.inner.git, &work, "initial").await;
        ci.inner
            .git
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
        ci.inner
            .store
            .write_sync(|w| {
                w.put(
                    store::REFS,
                    &Key::new().s(&repo.id).s("refs/heads/main").bytes(),
                    &sha,
                )
            })
            .unwrap();
        Self {
            temp,
            ci,
            repo,
            sha,
        }
    }

    async fn advance(&self) {
        self.ci
            .advance_verification(&self.repo, true)
            .await
            .unwrap();
    }

    fn record(&self) -> verify::Verification {
        self.ci
            .inner
            .store
            .read_sync(|r| verify::get(r, &self.repo.id))
            .unwrap()
            .unwrap()
    }

    fn claim(&self) -> queue::Claimed {
        self.ci
            .inner
            .store
            .write_sync(|w| queue::claim(w, "runner", 60, &util::now()))
            .unwrap()
            .unwrap()
    }

    fn finish(&self, c: queue::Claimed, conclusion: &str) {
        self.ci
            .inner
            .store
            .write_sync(|w| {
                queue::finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    conclusion,
                    None,
                    queue::ResultInfo::default(),
                    &util::now(),
                )
            })
            .unwrap();
    }

    async fn marked(&self) -> bool {
        self.ci
            .inner
            .git
            .all_refs(&self.ci.inner.git.repo_dir(&self.repo.path))
            .await
            .unwrap()
            .contains_key(&format!("refs/branvil/verified/{}", self.sha))
    }

    async fn push_next(&self) -> String {
        let work = self.temp.path().join("work");
        let sha = tests::commit_all(&self.ci.inner.git, &work, "next").await;
        self.ci
            .inner
            .git
            .run_ok(
                None,
                &[
                    "-C",
                    work.to_str().unwrap(),
                    "push",
                    "-q",
                    self.ci
                        .inner
                        .git
                        .repo_dir(&self.repo.path)
                        .to_str()
                        .unwrap(),
                    "HEAD:refs/heads/main",
                ],
            )
            .await
            .unwrap();
        sha
    }

    fn restart(mut self) -> Self {
        let (git, layout, config) = (
            self.ci.inner.git.clone(),
            self.ci.inner.layout.clone(),
            self.ci.inner.config.clone(),
        );
        drop(self.ci);
        let store = Store::open(&self.temp.path().join("t.redb"), 1 << 20, 1).unwrap();
        self.ci = Ci::new(
            store,
            git,
            Events::new(),
            "http://127.0.0.1:7878".into(),
            layout,
            config,
            "/nonexistent/branvil.toml".into(),
        );
        self
    }
}

#[tokio::test]
async fn verification_promotion_retries_failed_and_cancelled_heads_but_keeps_actual_success() {
    for conclusion in ["failure", "cancelled", "timed_out", "startup_failure"] {
        let f = Fixture::new(&["lint", "test"]).await;
        let reg = f.ci.registration(&f.repo, "feature", &f.sha).await.unwrap();
        f.ci.inner
            .store
            .write_sync(|w| queue::register(w, reg, 10, &util::now()))
            .unwrap();
        // Head admission is limited to one job in this fixture.
        let first = f.claim();
        let failed_name = first.attempt.job.name.clone();
        f.finish(first, conclusion);
        let success = f.claim();
        let success_id = success.attempt.id.clone();
        f.finish(success, "success");
        f.advance().await;
        let run =
            f.ci.inner
                .store
                .read_sync(|r| queue::get_run(r, &f.repo.id, &f.sha))
                .unwrap()
                .unwrap();
        let PipelineState::Jobs { jobs } = run.pipeline else {
            panic!("jobs")
        };
        let current =
            f.ci.inner
                .store
                .read_sync(|r| queue::current_attempts(r, &f.repo.id, &f.sha, &jobs))
                .unwrap();
        assert_eq!(
            current.iter().find(|a| a.id == success_id).unwrap().status,
            "completed"
        );
        f.advance().await;
        assert_eq!(
            f.ci.inner
                .store
                .read_sync(|r| queue::attempts_of(r, &f.repo.id, &f.sha))
                .unwrap()
                .len(),
            3
        );
        let retry = current.iter().find(|a| a.job.name == failed_name).unwrap();
        assert_eq!((retry.status.as_str(), retry.number), ("queued", 2));
        f.finish(f.claim(), "success");
        f.advance().await;
        assert_eq!(f.record().state, "passed");
        assert!(f.marked().await);
    }
}

#[tokio::test]
async fn verification_reopened_retry_finishes_with_a_mark_or_fails_again() {
    for recover in [false, true] {
        let f = Fixture::new(&["lint"]).await;
        f.advance().await;
        let mut last = String::new();
        for _ in 0..=queue::MAX_AUTO_RETRIES {
            let c = f.claim();
            last = c.attempt.id.clone();
            f.finish(c, "failure");
        }
        f.advance().await;
        assert_eq!(f.record().state, "failed");
        f.ci.inner
            .store
            .write_sync(|w| queue::retry(w, &last, 0, &util::now()))
            .unwrap();
        assert_eq!(f.record().state, "running");
        let pending = f.claim();
        assert_eq!(
            f.ci.inner
                .store
                .read_sync(|r| priority::class_of(r, &pending.attempt))
                .unwrap()
                .class,
            Class::Protected
        );
        assert!(f.record().failed_jobs.is_empty());
        assert_eq!(f.record().failing_since.as_deref(), Some(f.sha.as_str()));
        let mut pending = Some(pending);
        for n in 0..=queue::MAX_AUTO_RETRIES {
            let c = pending.take().unwrap_or_else(|| f.claim());
            f.finish(c, if recover { "success" } else { "failure" });
            f.advance().await;
            if recover {
                break;
            }
            assert_eq!(
                f.record().state,
                if n < queue::MAX_AUTO_RETRIES {
                    "running"
                } else {
                    "failed"
                }
            );
        }
        assert_eq!(f.record().state, if recover { "passed" } else { "failed" });
        assert_eq!(f.marked().await, recover);
        if recover {
            assert_eq!(f.record().last_verified.unwrap().sha, f.sha);
            assert!(f.record().failing_since.is_none());
        } else {
            assert_eq!(f.record().failed_jobs.len(), 1);
            assert_eq!(f.record().failing_since.as_deref(), Some(f.sha.as_str()));
            let next = f.push_next().await;
            f.advance().await;
            let c = f.claim();
            assert_eq!(c.attempt.sha, next);
            assert_eq!(
                f.ci.inner
                    .store
                    .read_sync(|r| priority::class_of(r, &c.attempt))
                    .unwrap()
                    .class,
                Class::Verify
            );
            f.finish(c, "success");
            f.advance().await;
            assert!(f.record().failing_since.is_none());
            assert_eq!(f.record().last_verified.unwrap().sha, next);
        }
        // Both a reopened success and a later repair restore healthy priority.
        let next = f.push_next().await;
        f.advance().await;
        let c = f.claim();
        assert_eq!(c.attempt.sha, next);
        assert_eq!(
            f.ci.inner
                .store
                .read_sync(|r| priority::class_of(r, &c.attempt))
                .unwrap()
                .class,
            Class::Verify
        );
    }
}

#[tokio::test]
async fn verification_interruptions_and_database_restart_keep_the_same_running_record() {
    for expire in [false, true] {
        let f = Fixture::new(&["lint"]).await;
        f.advance().await;
        let before = f.record();
        let c = f.claim();
        f.ci.inner
            .store
            .write_sync(|w| {
                if expire {
                    queue::expire(w, &queue::later(&util::now(), 120))
                } else {
                    queue::interrupt_running(w, None, "service stopped", &util::now())
                }
            })
            .unwrap();
        f.advance().await;
        assert_eq!(f.record().state, "running");
        assert!(!f.marked().await);
        let f = f.restart();
        assert_eq!(f.record().started_at, before.started_at);
        assert_eq!(f.record().sha, before.sha);
        f.advance().await;
        let next = f.claim();
        assert!(next.attempt.after_interrupt);
        assert_ne!(next.attempt.id, c.attempt.id);
        f.finish(next, "success");
        f.advance().await;
        assert_eq!(f.record().state, "passed");
        assert!(f.marked().await);
    }
}

#[tokio::test]
async fn reconciliation_isolates_verification_and_repository_errors() {
    for (verification_error, narrowing_error) in [(true, false), (false, true), (true, true)] {
        let f = Fixture::new(&["lint"]).await;
        let app = tests::app(&f.temp, &f.ci);
        let dir = f.ci.inner.git.repo_dir(&f.repo.path);
        f.ci.inner
            .git
            .update_ref(&dir, "refs/heads/feature", &f.sha, ZERO_SHA, "test")
            .await
            .unwrap();
        let mut missing = f.repo.clone();
        missing.id = "repo_0".into();
        missing.path = "acme/missing".into();
        f.ci.inner
            .store
            .write_sync(|w| {
                w.put(store::REPOS, &Repo::key(&missing.id), &missing)?;
                tests::put_pull(w, &tests::open_pull(&f.repo.id, 1, "feature", "main"))?;
                w.put(
                    store::CI_VERIFICATIONS,
                    &Key::new().s(&f.repo.id).bytes(),
                    &verify::Verification {
                        sha: if verification_error {
                            "missing-commit".into()
                        } else {
                            f.sha.clone()
                        },
                        base: None,
                        state: if verification_error {
                            "running"
                        } else {
                            "passed"
                        }
                        .into(),
                        started_at: util::now(),
                        finished_at: None,
                        failed_jobs: vec![],
                        failing_since: None,
                        last_verified: None,
                    },
                )?;
                if narrowing_error {
                    let v = verify::Verification {
                        sha: f.sha.clone(),
                        base: Some("missing-base".into()),
                        state: "failed".into(),
                        started_at: util::now(),
                        finished_at: None,
                        failed_jobs: vec![verify::FailedJob {
                            name: "lint".into(),
                            attempt_id: Some("failed-attempt".into()),
                            details: None,
                        }],
                        failing_since: None,
                        last_verified: None,
                    };
                    narrow::start(w, &f.repo.id, &v, &util::now())?;
                }
                Ok(())
            })
            .unwrap();
        assert_eq!(
            f.ci.advance_verification(&f.repo, true).await.is_err(),
            verification_error
        );
        assert_eq!(
            f.ci.advance_narrowings(&f.repo).await.is_err(),
            narrowing_error
        );
        f.ci.reconcile(&app).await.unwrap();
        let run =
            f.ci.inner
                .store
                .read_sync(|r| queue::get_run(r, &f.repo.id, &f.sha))
                .unwrap()
                .unwrap();
        assert_eq!(run.branches, vec!["feature"]);
        assert_eq!(
            f.record().state,
            if verification_error {
                "running"
            } else {
                "passed"
            }
        );
        assert_eq!(
            f.ci.advance_narrowings(&f.repo).await.is_err(),
            narrowing_error
        );
    }
}

/// Actual Git history, normal checks, persistent probes, and a restarted controller.
#[tokio::test]
async fn narrowing_finds_first_failure_without_changing_verified_checks() {
    let mut f = Fixture::new(&["test"]).await;
    f.advance().await;
    f.finish(f.claim(), "success");
    f.advance().await;
    let base = f.sha.clone();
    let before = f.ci.head_checks(&f.repo, &base).await.unwrap();
    let mut commits = vec![base.clone()];
    for _ in 0..4 {
        commits.push(f.push_next().await);
    }
    f.advance().await;
    // main verification retries failures; every one is retained as evidence.
    for _ in 0..=queue::MAX_AUTO_RETRIES {
        f.finish(f.claim(), "failure");
    }
    f.advance().await;
    assert_eq!(f.record().state, "failed");
    let mut positions = vec![];
    loop {
        f.ci.advance_narrowings(&f.repo).await.unwrap();
        let done =
            f.ci.inner
                .store
                .read_sync(|r| narrow::concluded(r, &f.repo.id, &base, "test"))
                .unwrap();
        if let Some(done) = done {
            assert_eq!(done.conclusion, "found");
            assert_eq!(
                done.boundary,
                Some((commits[2].clone(), commits[3].clone()))
            );
            break;
        }
        let c = f.claim();
        assert_eq!(c.attempt.purpose, queue::Purpose::Probe);
        assert_eq!(c.attempt.cache_scope, "probe");
        assert!(!c.attempt.allow_reuse);
        assert!(c.reuse.is_none());
        let pos = commits.iter().position(|s| s == &c.attempt.sha).unwrap();
        positions.push(pos);
        let id = c.attempt.id.clone();
        f.finish(c, if pos < 3 { "success" } else { "failure" });
        assert!(
            f.ci.inner
                .store
                .write_sync(|w| queue::retry(w, &id, 256, &util::now()))
                .is_err()
        );
        f = f.restart();
        assert!(positions.len() <= 6);
    }
    assert_eq!(positions, [0, 2, 3, 2, 3]);
    let after = f.ci.head_checks(&f.repo, &base).await.unwrap();
    assert_eq!(
        serde_json::to_value(before).unwrap(),
        serde_json::to_value(after).unwrap()
    );
    assert!(f.marked().await);
    assert!(
        f.ci.inner
            .store
            .read_sync(crate::model::index_problems)
            .unwrap()
            .is_empty()
    );
}

async fn failed_narrowing() -> (Fixture, Vec<String>) {
    let f = Fixture::new(&["test"]).await;
    f.advance().await;
    f.finish(f.claim(), "success");
    f.advance().await;
    let mut commits = vec![f.sha.clone()];
    for _ in 0..4 {
        commits.push(f.push_next().await);
    }
    f.advance().await;
    for _ in 0..=queue::MAX_AUTO_RETRIES {
        f.finish(f.claim(), "failure");
    }
    f.advance().await;
    (f, commits)
}

#[tokio::test]
async fn narrowing_outside_inconsistent_undetermined_dedup_and_abandonment() {
    for (second, expected) in [
        ("failure", "outside"),
        ("success", "inconsistent"),
        ("cancelled", "undetermined"),
    ] {
        let (f, commits) = failed_narrowing().await;
        for outcome in [
            if second == "cancelled" {
                "cancelled"
            } else {
                "failure"
            },
            second,
        ] {
            f.ci.advance_narrowings(&f.repo).await.unwrap();
            let c = f.claim();
            assert_eq!(c.attempt.sha, commits[0]);
            f.finish(c, outcome);
        }
        f.ci.advance_narrowings(&f.repo).await.unwrap();
        let n =
            f.ci.inner
                .store
                .read_sync(|r| narrow::concluded(r, &f.repo.id, &commits[0], "test"))
                .unwrap()
                .unwrap();
        assert_eq!(n.conclusion, expected);
        assert_eq!(n.results[0].len(), 2);
        // Repeating the same failed verification cannot reset a concluded search.
        let record = f.record();
        f.ci.inner
            .store
            .write_sync(|w| narrow::start(w, &f.repo.id, &record, &util::now()))
            .unwrap();
        assert_eq!(
            f.ci.inner
                .store
                .read_sync(|r| narrow::concluded(r, &f.repo.id, &commits[0], "test"))
                .unwrap()
                .unwrap()
                .conclusion,
            expected
        );
    }
    let (f, commits) = failed_narrowing().await;
    f.ci.advance_narrowings(&f.repo).await.unwrap();
    let queued =
        f.ci.inner
            .store
            .read_sync(|r| queue::scheduled(r, false))
            .unwrap()[0]
            .0
            .clone();
    // A later passing verification closes active searches in its transaction.
    let next = f.push_next().await;
    f.advance().await;
    let main =
        f.ci.inner
            .store
            .write_sync(|w| queue::claim_fixture(w, (&f.repo.path, &next), &util::now()))
            .unwrap();
    assert_eq!(main.attempt.purpose, queue::Purpose::Check);
    f.finish(main, "success");
    f.advance().await;
    let n =
        f.ci.inner
            .store
            .read_sync(|r| narrow::concluded(r, &f.repo.id, &commits[0], "test"))
            .unwrap()
            .unwrap();
    assert_eq!(n.conclusion, "abandoned");
    assert_eq!(
        f.ci.inner
            .store
            .read_sync(|r| queue::get_attempt(r, &queued.id))
            .unwrap()
            .unwrap()
            .conclusion
            .as_deref(),
        Some("cancelled")
    );
    assert!(
        f.ci.inner
            .store
            .read_sync(crate::model::index_problems)
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn narrowing_missing_job_is_not_run_and_boundary_disagreement_keeps_attempts() {
    for missing in [false, true] {
        let f = Fixture::new(&["test"]).await;
        f.advance().await;
        f.finish(f.claim(), "success");
        f.advance().await;
        let mut commits = vec![f.sha.clone()];
        for i in 1..=4 {
            let pipeline = if missing && i == 2 {
                "invalid pipeline"
            } else {
                "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n"
            };
            std::fs::write(f.temp.path().join("work/.guildbotics-ci.toml"), pipeline).unwrap();
            commits.push(f.push_next().await);
        }
        f.advance().await;
        for _ in 0..=queue::MAX_AUTO_RETRIES {
            f.finish(f.claim(), "failure");
        }
        f.advance().await;
        let mut at_three = 0;
        for _ in 0..7 {
            f.ci.advance_narrowings(&f.repo).await.unwrap();
            if let Some(n) =
                f.ci.inner
                    .store
                    .read_sync(|r| narrow::concluded(r, &f.repo.id, &commits[0], "test"))
                    .unwrap()
            {
                assert_eq!(n.conclusion, if missing { "found" } else { "inconsistent" });
                if missing {
                    assert_eq!(n.results[2][0].outcome, "not_run");
                } else {
                    assert_eq!(n.results[3].len(), 2);
                    assert!(n.results[3].iter().all(|r| r.attempt.is_some()));
                }
                break;
            }
            let c = f.claim();
            let pos = commits.iter().position(|s| s == &c.attempt.sha).unwrap();
            if pos == 3 {
                at_three += 1;
            }
            f.finish(
                c,
                if pos < 3 || (!missing && at_three == 2) {
                    "success"
                } else {
                    "failure"
                },
            );
        }
        assert!(
            f.ci.inner
                .store
                .read_sync(|r| narrow::concluded(r, &f.repo.id, &commits[0], "test"))
                .unwrap()
                .is_some()
        );
    }
}

#[tokio::test]
async fn diagnostic_history_failure_preserves_failed_verification_and_repair() {
    let f = Fixture::new(&["lint"]).await;
    f.advance().await;
    for _ in 0..=queue::MAX_AUTO_RETRIES {
        f.finish(f.claim(), "failure");
    }
    let mut record = f.record();
    record.base = Some("missing-base".into());
    f.ci.inner
        .store
        .write_sync(|w| {
            w.put(
                store::CI_VERIFICATIONS,
                &Key::new().s(&f.repo.id).bytes(),
                &record,
            )
        })
        .unwrap();
    f.advance().await;
    assert_eq!(f.record().state, "failed");
    assert!(
        f.ci.inner
            .store
            .read_sync(|r| r.get_raw(
                store::REPAIR_PENDING,
                &Key::new().s(&f.repo.id).s(&f.sha).bytes()
            ))
            .unwrap()
            .is_some()
    );
    assert!(f.ci.advance_narrowings(&f.repo).await.is_err());
    assert!(
        f.ci.inner
            .store
            .read_sync(|r| narrow::concluded(r, &f.repo.id, "missing-base", "lint"))
            .unwrap()
            .is_none()
    );
    assert_eq!(f.record().state, "failed");
}

#[tokio::test]
async fn narrowing_history_retries_without_losing_failure_evidence() {
    let (f, commits) = failed_narrowing().await;
    let snapshot = || {
        f.ci.inner
            .store
            .read_sync(|r| r.prefix_all::<narrow::Narrowing>(store::CI_NARROWINGS, &[]))
            .unwrap()
            .remove(0)
    };
    let before = snapshot();
    assert!(!before.history_ready);
    let evidence = before.results.last().unwrap().clone();
    assert_eq!(evidence.len(), (queue::MAX_AUTO_RETRIES + 1) as usize);
    let dir = f.ci.inner.git.repo_dir(&f.repo.path);
    let unavailable = f.temp.path().join("unavailable.git");
    std::fs::rename(&dir, &unavailable).unwrap();
    let failed = f.ci.advance_narrowings(&f.repo).await;
    std::fs::rename(&unavailable, &dir).unwrap();
    assert!(failed.is_err());
    assert_eq!(
        serde_json::to_value(snapshot()).unwrap(),
        serde_json::to_value(&before).unwrap()
    );
    let f = f.restart();
    f.ci.advance_narrowings(&f.repo).await.unwrap();
    let after =
        f.ci.inner
            .store
            .read_sync(|r| r.prefix_all::<narrow::Narrowing>(store::CI_NARROWINGS, &[]))
            .unwrap()
            .remove(0);
    assert!(after.history_ready);
    assert_eq!(after.commits, commits);
    assert_eq!(
        serde_json::to_value(after.results.last().unwrap()).unwrap(),
        serde_json::to_value(evidence).unwrap()
    );
    assert_eq!(after.pending.as_ref().unwrap().0, 0);
    assert_eq!(f.record().state, "failed");
}
