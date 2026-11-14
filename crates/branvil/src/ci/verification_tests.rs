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
        let (temp, ci, mut repo) = tests::setup();
        repo.required_checks.clear();
        ci.inner.layout.create().unwrap();
        ci.inner
            .store
            .write_sync(|w| w.put(store::REPOS, &Repo::key(&repo.id), &repo))
            .unwrap();
        let dir = ci.inner.git.repo_dir(&repo.path);
        ci.inner.git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        ci.inner
            .git
            .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        std::fs::create_dir(work.join(".branvil")).unwrap();
        let pipeline: String = names
            .iter()
            .map(|name| format!("[[jobs]]\nname='{name}'\nimage='alpine'\ncommands=['true']\n"))
            .collect();
        std::fs::write(work.join(".branvil/ci.toml"), pipeline).unwrap();
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
                    0,
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
        let reg =
            f.ci.registration(&f.repo, "feature", &f.sha, false)
                .await
                .unwrap();
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
                Class::Protected
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
                    sha: "missing-commit".into(),
                    base: None,
                    state: "running".into(),
                    started_at: util::now(),
                    finished_at: None,
                    failed_jobs: vec![],
                    failing_since: None,
                    last_verified: None,
                },
            )
        })
        .unwrap();
    assert!(f.ci.advance_verification(&f.repo, true).await.is_err());
    f.ci.reconcile(&app).await.unwrap();
    let run =
        f.ci.inner
            .store
            .read_sync(|r| queue::get_run(r, &f.repo.id, &f.sha))
            .unwrap()
            .unwrap();
    assert_eq!(run.branches, vec!["feature"]);
    assert_eq!(f.record().state, "running");
}
