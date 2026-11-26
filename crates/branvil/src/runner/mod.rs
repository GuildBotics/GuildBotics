pub use gb_runner::runner::*;
#[cfg(test)]
mod integration {
    use super::*;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::Ordering;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use tokio::sync::{mpsc, watch};

    use executor::{Executor, Health, JobRun, Microsandbox, Outcome};

    #[cfg(test)]
    mod tests {
        //! The runner loop against an in-process service, with a fake executor
        //! that interprets a few words (`echo`, `write`, `exit`, `wait`, and
        //! `git`, which runs the host's Git in the work directory) and never
        //! starts a VM.

        use super::*;
        use executor::ImageLock;
        use std::sync::atomic::AtomicBool;

        #[cfg(feature = "host")]
        #[tokio::test]
        async fn hub_runner_defers_old_versions_to_the_snapshot_sweep() {
            use crate::ci::images;
            let dir = tempfile::tempdir().unwrap();
            let ms = Microsandbox::hub(dir.path().to_owned(), "test");
            let _images = ms.lock_images(true, || {}).await.unwrap();
            ms.load(images::tests::docker(images::architecture(), "1").path())
                .await
                .unwrap();
            let first = ms.images().await.unwrap().pop().unwrap();
            let pin = format!("docker.io/test/image@{}", first.digest);

            let second = images::tests::docker_tagged(
                images::architecture(),
                "2",
                "docker.io/test/image:dev",
            );
            ms.load(second.path()).await.unwrap();
            let current = ms.inspect(&first.reference).await.unwrap().unwrap();
            assert_ne!(current.digest, first.digest);
            let alias = ms
                .inspect("docker.io/test/image:dev")
                .await
                .unwrap()
                .unwrap();
            assert_eq!(alias.config_digest, current.config_digest);
            assert!(ms.inspect(&pin).await.unwrap().is_some());

            ms.collect().await;
            assert_eq!(
                ms.inspect(&pin).await.unwrap().unwrap().digest,
                first.digest
            );

            // An interrupted GuildBotics registration can leave the runtime's
            // cache updated with one spelling while another catalog row is stale.
            let third = images::tests::docker_tagged(
                images::architecture(),
                "3",
                "docker.io/test/image:dev",
            );
            let output = ms
                .command()
                .unwrap()
                .args(["load", "--quiet", "--input"])
                .arg(third.path())
                .output()
                .await
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            assert!(ms.inspect(&first.reference).await.is_err());
        }

        #[cfg(unix)]
        #[tokio::test]
        async fn hub_archives_follow_cpu_and_digest_without_reloading_equal_versions() {
            use crate::ci::images;
            let s = service("").await;
            let native = images::architecture();
            let foreign = if native == "arm64" { "amd64" } else { "arm64" };
            let archive = images::tests::docker(native, "1");
            let saved = images::save(&s.app.layout.root, archive.path()).unwrap();
            images::save(
                &s.app.layout.root,
                images::tests::docker(foreign, "1").path(),
            )
            .unwrap();
            let dir = tempfile::tempdir().unwrap();
            let msb = dir.path().join("msb");
            let digest = &saved.images[0].digest;
            executor::test_support::write_script(
                &msb,
                &format!(
                    r#"#!/bin/sh
case "$1 $2" in
"load --quiet") echo loaded >> "$MSB_HOME/loads"; echo '{digest}' > "$MSB_HOME/digest" ;;
"image inspect") [ -f "$MSB_HOME/digest" ] || exit 1; printf '{{"reference":"%s","digest":"sha256:manifest","config":{{"digest":"%s"}}}}' "$5" "$(cat "$MSB_HOME/digest")" ;;
"image list") echo '[{{"reference":"test/image:dev","digest":"m"}}]' ;;
esac
"#
                ),
            );
            let ms = Microsandbox::new(executor::Msb::Found(msb), dir.path().join("cache"), "test");
            let api = Api::new(&s.url, &s.runner_token);
            api.sync_images(&ms).await.unwrap();
            let blob = images::path(&s.app.layout.root, &saved.id).unwrap();
            std::fs::remove_file(&blob).unwrap();
            // Equal digests work even when the archive cannot be fetched.
            api.sync_images(&ms).await.unwrap();
            std::fs::copy(archive.path(), &blob).unwrap();
            assert_eq!(
                std::fs::read_to_string(ms.home().join("loads")).unwrap(),
                "loaded\n"
            );
            std::fs::write(ms.home().join("digest"), "sha256:old").unwrap();
            api.sync_images(&ms).await.unwrap();
            assert_eq!(
                std::fs::read_to_string(ms.home().join("loads")).unwrap(),
                "loaded\nloaded\n"
            );
            let mut catalog = images::list(&s.app.layout.root).unwrap();
            catalog.insert(
                0,
                images::Archive {
                    id: "0".repeat(64),
                    images: vec![images::Image {
                        reference: "broken:dev".into(),
                        architecture: native.into(),
                        digest: "sha256:broken".into(),
                    }],
                },
            );
            std::fs::write(
                s.app.layout.root.join("ci/images/catalog.json"),
                serde_json::to_vec(&catalog).unwrap(),
            )
            .unwrap();
            std::fs::write(ms.home().join("digest"), "sha256:old").unwrap();
            assert!(
                api.sync_images(&ms)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("broken:dev")
            );
            assert_eq!(
                std::fs::read_to_string(ms.home().join("digest"))
                    .unwrap()
                    .trim(),
                digest
            );
        }

        #[cfg(unix)]
        #[tokio::test]
        async fn a_download_cannot_overwrite_a_concurrently_installed_version() {
            use crate::ci::images;
            let dir = tempfile::tempdir().unwrap();
            let home = dir.path().join("cache");
            std::fs::create_dir_all(&home).unwrap();
            std::fs::write(home.join("digest"), "sha256:old").unwrap();
            let archive = images::tests::docker(images::architecture(), "1");
            let bytes = std::fs::read(archive.path()).unwrap();
            let saved = images::Archive {
                id: crate::util::hex(&crate::util::sha256(&bytes)),
                images: images::inspect(archive.path()).unwrap(),
            };
            let msb = dir.path().join("msb");
            executor::test_support::write_script(
                &msb,
                r#"#!/bin/sh
case "$1 $2" in
"image inspect") printf '{"reference":"test/image:dev","digest":"m","config":{"digest":"%s"}}' "$(cat "$MSB_HOME/digest")" ;;
"load --quiet") touch "$MSB_HOME/overwritten" ;;
"image list") echo '[{"reference":"test/image:dev","digest":"m"}]' ;;
esac
"#,
            );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let target = home.join("digest");
            let catalog_target = target.clone();
            let router = axum::Router::new()
                .route(
                    "/api/v1/runner/images",
                    axum::routing::get(move || {
                        let saved = saved.clone();
                        let target = catalog_target.clone();
                        async move {
                            std::fs::write(target, "sha256:concurrent").unwrap();
                            axum::Json(vec![saved])
                        }
                    }),
                )
                .route(
                    "/api/v1/runner/images/{id}",
                    axum::routing::get(move || {
                        let bytes = bytes.clone();
                        let target = target.clone();
                        async move {
                            std::fs::write(target, "sha256:concurrent").unwrap();
                            bytes
                        }
                    }),
                );
            let server = tokio::spawn(axum::serve(listener, router).into_future());
            let ms = Microsandbox::new(executor::Msb::Found(msb), home, "t");
            Api::new(&url, "unused").sync_images(&ms).await.unwrap();
            assert!(!ms.home().join("overwritten").exists());
            assert_eq!(
                std::fs::read_to_string(ms.home().join("digest")).unwrap(),
                "sha256:concurrent"
            );
            server.abort();
        }

        #[cfg(unix)]
        #[tokio::test]
        async fn inventory_preserves_good_tags_and_reports_each_bad_tag() {
            let dir = tempfile::tempdir().unwrap();
            let msb = dir.path().join("msb");
            executor::test_support::write_script(
                &msb,
                r#"#!/bin/sh
if [ "$2" = list ]; then
  echo '[{"reference":"failed","digest":"m"},{"reference":"malformed","digest":"m"},{"reference":"good","digest":"m"}]'
else
  case "$5" in
    failed) echo 'permission denied' >&2; exit 1 ;;
    malformed) echo 'not json' ;;
    good) echo '{"reference":"good","digest":"m","config":{"digest":"sha256:c"}}' ;;
  esac
fi
"#,
            );
            let ms = Microsandbox::new(executor::Msb::Found(msb), dir.path().join("cache"), "t");
            let inventory = ms
                .inventory(&Api::new("http://127.0.0.1:1", "unused"))
                .await
                .unwrap();
            assert_eq!(inventory.images.len(), 1);
            assert_eq!(inventory.images[0].reference, "good");
            let detail = inventory.detail.unwrap();
            assert!(
                detail.contains("permission denied") && detail.contains("malformed"),
                "{detail}"
            );
        }

        #[tokio::test]
        async fn synchronization_does_not_block_cached_jobs_or_runner_reports() {
            for blocked in [false, true] {
                let s =
                    service("[[jobs]]\nname = 'test'\nimage = 'alpine'\ncommands = ['echo ok']\n")
                        .await;
                let fake = Arc::new(Fake::new());
                fake.sync_blocked.store(blocked, Ordering::SeqCst);
                fake.sync_failed.store(!blocked, Ordering::SeqCst);
                let runner = Arc::new(
                    Runner::new(
                        Api::new(&s.url, &s.runner_token),
                        s.dir.path().join("runner"),
                    )
                    .idle_wait(TEST_IDLE_WAIT),
                );
                let running = tokio::spawn(runner.serve({
                    let fake = fake.clone();
                    move || fake.clone()
                }));
                let sha = s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone();
                tokio::time::timeout(Duration::from_secs(15), async {
                    loop {
                        if result_of(&s, &sha).await.conclusion.as_deref() == Some("success") {
                            break;
                        }
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
                tokio::time::timeout(Duration::from_secs(5), async {
                    loop {
                        if s.app
                            .ci
                            .diagnostics()
                            .await
                            .to_string()
                            .contains(if blocked {
                                "synchronization in progress"
                            } else {
                                "broken archive"
                            })
                        {
                            break;
                        }
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
                s.revoke_runner_token().await;
                assert!(
                    tokio::time::timeout(Duration::from_secs(5), running)
                        .await
                        .unwrap()
                        .unwrap()
                        .is_err()
                );
            }
        }

        /// What a run got: (image, cpus, memory MiB, timeout s, work quota MiB).
        type Ran = (String, u32, u64, u64, u64);

        struct Fake {
            ready: AtomicBool,
            controlled: bool,
            finishes: Mutex<std::collections::BTreeMap<String, Arc<tokio::sync::Notify>>>,
            sync_blocked: AtomicBool,
            sync_failed: AtomicBool,
            /// The image cache is held (by a pull or a load): checks wait.
            hold_images: AtomicBool,
            changed_image: AtomicBool,
            /// What every run got.
            ran: std::sync::Mutex<Vec<Ran>>,
            /// Jobs that reached `wait`, after source fetching and checkout.
            waiting: std::sync::atomic::AtomicUsize,
            /// `wait`s that saw the stop (an aborted run never does).
            stopped: std::sync::atomic::AtomicUsize,
        }

        impl Fake {
            fn new() -> Self {
                Self {
                    ready: AtomicBool::new(true),
                    controlled: false,
                    finishes: Default::default(),
                    sync_blocked: AtomicBool::new(false),
                    sync_failed: AtomicBool::new(false),
                    hold_images: AtomicBool::new(false),
                    changed_image: AtomicBool::new(false),
                    ran: Default::default(),
                    waiting: Default::default(),
                    stopped: Default::default(),
                }
            }
        }

        impl Fake {
            async fn wait(&self, id: &str, mut cancel: watch::Receiver<bool>) -> Outcome {
                self.waiting.fetch_add(1, Ordering::SeqCst);
                let finish = Arc::new(tokio::sync::Notify::new());
                if self.controlled {
                    self.finishes
                        .lock()
                        .unwrap()
                        .insert(id.into(), finish.clone());
                }
                tokio::select! {
                    biased;
                    _ = cancel.wait_for(|stop| *stop) => {
                        self.stopped.fetch_add(1, Ordering::SeqCst);
                        Outcome::Cancelled
                    }
                    _ = finish.notified(), if self.controlled => Outcome::Exited(0),
                }
            }
        }

        #[tokio::test]
        async fn fake_wait_observes_cancellation_in_every_mode() {
            use std::task::Poll;
            for controlled in [false, true] {
                for initial in [false, true] {
                    let fake = Fake {
                        controlled,
                        ..Fake::new()
                    };
                    let (send, recv) = watch::channel(initial);
                    let mut wait = Box::pin(fake.wait("attempt", recv));
                    if !initial {
                        assert!(futures_util::poll!(&mut wait).is_pending());
                        send.send(false).unwrap();
                        assert!(futures_util::poll!(&mut wait).is_pending());
                        send.send(true).unwrap();
                    }
                    // Even if both signals are ready, a stop must be visible.
                    if controlled && !initial {
                        fake.finishes.lock().unwrap()["attempt"].notify_one();
                    }
                    assert!(matches!(
                        futures_util::poll!(&mut wait),
                        Poll::Ready(Outcome::Cancelled)
                    ));
                    assert_eq!(fake.stopped.load(Ordering::SeqCst), 1);
                    assert_eq!(fake.waiting.load(Ordering::SeqCst), 1);
                }
                let fake = Fake {
                    controlled,
                    ..Fake::new()
                };
                let (send, recv) = watch::channel(false);
                drop(send);
                assert!(matches!(
                    fake.wait("closed", recv).await,
                    Outcome::Cancelled
                ));
                assert_eq!(fake.stopped.load(Ordering::SeqCst), 1);
            }
            let fake = Fake {
                controlled: true,
                ..Fake::new()
            };
            let (_send, recv) = watch::channel(false);
            let mut wait = Box::pin(fake.wait("finish", recv));
            assert!(futures_util::poll!(&mut wait).is_pending());
            fake.finishes.lock().unwrap()["finish"].notify_one();
            assert!(matches!(
                futures_util::poll!(&mut wait),
                Poll::Ready(Outcome::Exited(0))
            ));
            assert_eq!(fake.stopped.load(Ordering::SeqCst), 0);
        }

        /// The configuration digest the fake cache holds `image` as.
        fn digest_of(image: &str) -> String {
            format!(
                "sha256:{}",
                crate::util::hex(&crate::util::sha256(image.as_bytes()))
            )
        }

        impl Executor for Fake {
            async fn synchronize(&self, _api: &Api) -> Result<(), ApiError> {
                if self.sync_blocked.load(Ordering::SeqCst) {
                    std::future::pending::<()>().await;
                }
                if self.sync_failed.load(Ordering::SeqCst) {
                    return Err(ApiError::Other("broken archive".into()));
                }
                Ok(())
            }
            async fn inventory(&self, _api: &Api) -> Result<executor::Inventory, ApiError> {
                Ok(executor::Inventory {
                    detail: None,
                    images: vec![crate::ci::images::Image {
                        reference: "alpine".into(),
                        architecture: crate::ci::images::architecture().into(),
                        digest: "sha256:test".into(),
                    }],
                })
            }
            fn name(&self) -> &'static str {
                "fake"
            }
            async fn health(&self) -> Health {
                let ready = self.ready.load(Ordering::SeqCst);
                Health {
                    ready,
                    detail: (!ready).then(|| "fake executor switched off".to_string()),
                }
            }
            async fn resolve_image(
                &self,
                image: &str,
                _: &mpsc::Sender<Vec<u8>>,
            ) -> Result<(Option<String>, ImageLock), String> {
                if self.hold_images.load(Ordering::SeqCst) {
                    std::future::pending::<()>().await;
                }
                let digest = if self.changed_image.load(Ordering::SeqCst) {
                    digest_of(&format!("updated-{image}"))
                } else {
                    digest_of(image)
                };
                Ok((Some(digest), ImageLock::default()))
            }
            async fn run(
                &self,
                job: JobRun<'_>,
                log: mpsc::Sender<Vec<u8>>,
                cancel: watch::Receiver<bool>,
            ) -> Outcome {
                self.ran.lock().unwrap().push((
                    job.job.image.clone(),
                    job.cpus,
                    job.memory_mib,
                    job.timeout_secs,
                    job.work_quota_mib,
                ));
                for c in &job.job.commands {
                    let words: Vec<&str> = c.split_whitespace().collect();
                    match words.as_slice() {
                        ["echo", rest @ ..] => {
                            let _ = log.send(format!("{}\n", rest.join(" ")).into_bytes()).await;
                        }
                        ["write", path, content] => {
                            let p = job.work.join(path);
                            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
                            std::fs::write(p, content).unwrap();
                        }
                        ["git", args @ ..] => {
                            let _ = log.send(git(job.work, args).into_bytes()).await;
                        }
                        ["exit", code] => return Outcome::Exited(code.parse().unwrap()),
                        ["wait"] => return self.wait(job.attempt_id, cancel).await,
                        _ => return Outcome::Exited(127),
                    }
                }
                // The image of `/work` the collecting VM left for the cache.
                if job.save_cache && job.job.cache.is_some() {
                    std::fs::write(job.cache_disk, b"cached").unwrap();
                    *job.kept_data = Some(6);
                }
                Outcome::Exited(0)
            }
            async fn sweep(&self) {}
        }

        /// Runs Git isolated from the user's configuration; returns its output.
        fn git(dir: &Path, args: &[&str]) -> String {
            let home = dir.join("..").join("git-home");
            std::fs::create_dir_all(&home).unwrap();
            let global = home.join("gitconfig");
            std::fs::write(&global, "").unwrap();
            let out = std::process::Command::new("git")
                .current_dir(dir)
                .args(["-c", "credential.helper=", "-c", "core.askPass="])
                .args(args)
                .env("HOME", &home)
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .env("GIT_CONFIG_GLOBAL", &global)
                .env("GIT_TERMINAL_PROMPT", "0")
                .env("GIT_ASKPASS", "/usr/bin/false")
                .env("GIT_AUTHOR_NAME", "Test")
                .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
                .env("GIT_COMMITTER_NAME", "Test")
                .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "git {args:?}: {}",
                String::from_utf8_lossy(&out.stderr)
            );
            String::from_utf8(out.stdout).unwrap()
        }

        /// An in-process service with the owner alice, the runner member runner1,
        /// and the repository acme/demo, whose main (tagged v1; the tag `later`
        /// is of a commit it does not reach) has `pipeline`, recorded so that its
        /// run is queued.
        const TEST_IDLE_WAIT: Duration = Duration::from_millis(50);

        async fn observe_more_claims(claims: &std::sync::atomic::AtomicUsize) {
            let before = claims.load(Ordering::SeqCst);
            tokio::time::timeout(Duration::from_secs(10), async {
                while claims.load(Ordering::SeqCst) - before < 3 {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .expect("the runner made three more claim requests");
        }

        struct Service {
            dir: tempfile::TempDir,
            app: crate::app::Shared,
            url: String,
            owner: String,
            runner_token: String,
            claims: Arc<std::sync::atomic::AtomicUsize>,
            http: reqwest::Client,
            repo: crate::model::Repo,
            bare: PathBuf,
        }

        impl Service {
            async fn revoke_runner_token(&self) {
                let tokens: serde_json::Value = self
                    .http
                    .get(format!("{}/api/v1/members/runner1/tokens", self.url))
                    .bearer_auth(&self.owner)
                    .send()
                    .await
                    .unwrap()
                    .json()
                    .await
                    .unwrap();
                for t in tokens["items"].as_array().unwrap() {
                    let resp = self
                        .http
                        .delete(format!(
                            "{}/api/v1/members/runner1/tokens/{}",
                            self.url,
                            t["id"].as_str().unwrap()
                        ))
                        .bearer_auth(&self.owner)
                        .send()
                        .await
                        .unwrap();
                    assert!(resp.status().is_success(), "{}", resp.status());
                }
            }
        }

        /// Hold source requests before authentication, so revocation reliably
        /// makes them fail with 401 rather than depending on checkout speed.
        struct SourceGate {
            requested: mpsc::Sender<tokio::sync::oneshot::Sender<()>>,
            refused: Arc<tokio::sync::Notify>,
            source_refusals: Arc<std::sync::atomic::AtomicUsize>,
        }

        async fn service(pipeline: &str) -> Service {
            service_with_source_gate(pipeline, None).await
        }

        async fn service_with_source_gate(pipeline: &str, gate: Option<SourceGate>) -> Service {
            service_with_lease(pipeline, gate, 3).await
        }

        async fn service_with_lease(
            pipeline: &str,
            gate: Option<SourceGate>,
            lease_secs: u64,
        ) -> Service {
            let dir = tempfile::tempdir().unwrap();
            let data = dir.path().join("data");
            std::fs::create_dir_all(&data).unwrap();
            std::fs::write(
                data.join("config.toml"),
                format!("[ci]\nlease_secs = {lease_secs}\n"),
            )
            .unwrap();
            let config = crate::config::Config::load(None, &data).unwrap();
            let layout = config.layout();
            layout.create().unwrap();
            let store = crate::store::Store::open(&layout.db(), 1 << 20, 2).unwrap();
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let (owner, runner_token) = store
                .write_sync(|w| {
                    use crate::domain::members::{NewMember, insert_member};
                    use crate::model::MemberKind;
                    for (name, kind) in [
                        ("alice", MemberKind::Owner),
                        ("runner1", MemberKind::Runner),
                    ] {
                        insert_member(
                            w,
                            &NewMember {
                                name: name.into(),
                                display_name: None,
                                kind,
                            },
                        )?;
                    }
                    Ok((
                        crate::auth::issue_token(w, "alice", "t")?.1,
                        crate::auth::issue_token(w, "runner1", "t")?.1,
                    ))
                })
                .unwrap();
            let app = crate::app::App::new(
                config,
                url.clone(),
                store,
                std::sync::Arc::new(crate::vault::Memory::default()),
                url.clone(),
            );
            // The test server's routes too: the test opens its PR over HTTP.
            let claims = Arc::new(std::sync::atomic::AtomicUsize::new(0));
            let counted = claims.clone();
            let mut router =
                crate::http::router_with(app.clone(), crate::test_support::routes::routes()).layer(
                    axum::middleware::from_fn(
                        move |request: axum::extract::Request, next: axum::middleware::Next| {
                            let counted = counted.clone();
                            async move {
                                let claim = request.uri().path() == "/api/v1/runner/claim";
                                let response = next.run(request).await;
                                if claim {
                                    counted.fetch_add(1, Ordering::SeqCst);
                                }
                                response
                            }
                        },
                    ),
                );
            if let Some(gate) = gate {
                router = router.layer(axum::middleware::from_fn(
                    move |request: axum::extract::Request, next: axum::middleware::Next| {
                        let (requested, refused) = (gate.requested.clone(), gate.refused.clone());
                        let source_refusals = gate.source_refusals.clone();
                        async move {
                            let path = request.uri().path();
                            let source = path.ends_with("/source");
                            let claim =
                                path.ends_with("/runner/claim") || path.ends_with("/runner/health");
                            if source {
                                let (resume, held) = tokio::sync::oneshot::channel();
                                requested.send(resume).await.unwrap();
                                held.await.unwrap();
                            }
                            let response = next.run(request).await;
                            if response.status() == axum::http::StatusCode::UNAUTHORIZED {
                                if claim {
                                    refused.notify_one();
                                }
                                if source {
                                    source_refusals.fetch_add(1, Ordering::SeqCst);
                                }
                            }
                            response
                        }
                    },
                ));
            }
            tokio::spawn(axum::serve(listener, router).into_future());
            let http = reqwest::Client::new();
            for (path, body) in [
                ("workspaces", serde_json::json!({"name": "acme"})),
                ("repos", serde_json::json!({"path": "acme/demo"})),
            ] {
                let resp = http
                    .post(format!("{url}/api/v1/{path}"))
                    .bearer_auth(&owner)
                    .json(&body)
                    .send()
                    .await
                    .unwrap();
                assert_eq!(resp.status(), 201);
            }

            // A commit of main with a pipeline, pushed into the bare repository
            // directly (no hooks: the hooks directory is empty) and recorded.
            let work = dir.path().join("work");
            std::fs::create_dir_all(&work).unwrap();
            std::fs::write(work.join(".guildbotics-ci.toml"), pipeline).unwrap();
            git(&work, &["init", "-q", "-b", "main"]);
            git(&work, &["add", "."]);
            git(&work, &["commit", "-qm", "pipeline"]);
            // A tag of the commit, and one of a commit it does not reach.
            git(&work, &["tag", "-a", "v1", "-m", "v1"]);
            git(&work, &["checkout", "-qb", "side"]);
            git(&work, &["commit", "-q", "--allow-empty", "-m", "later"]);
            git(&work, &["tag", "later"]);
            git(&work, &["checkout", "-q", "main"]);
            let bare = layout.repo_dir("acme/demo");
            git(
                &work,
                &["push", "-q", bare.to_str().unwrap(), "main", "v1", "later"],
            );
            let repo = app
                .store
                .read(|r| crate::domain::repo_by_path(r, "acme/demo"))
                .await
                .unwrap();
            crate::refs::record_refs(&app, &repo).await.unwrap();
            Service {
                dir,
                app,
                url,
                owner,
                runner_token,
                claims,
                http,
                repo,
                bare,
            }
        }

        async fn empty_main(s: &Service, message: &str) -> String {
            let work = s.dir.path().join("work");
            git(&work, &["commit", "-q", "--allow-empty", "-m", message]);
            git(&work, &["push", "-q", s.bare.to_str().unwrap(), "main"]);
            crate::refs::record_refs(&s.app, &s.repo).await.unwrap();
            s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone()
        }

        async fn result_of(s: &Service, sha: &str) -> crate::ci::queue::Attempt {
            let (repo, sha) = (s.repo.id.clone(), sha.to_string());
            s.app
                .store
                .read(move |r| {
                    Ok(crate::ci::queue::attempts_of(r, &repo, &sha)?
                        .into_iter()
                        .rfind(|a| a.purpose == crate::ci::queue::Purpose::Check)
                        .unwrap())
                })
                .await
                .unwrap()
        }

        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn the_runner_reports_the_release_points_its_job_printed() {
            let service = service(
                "[[jobs]]\nname = 'schema'\nimage = 'alpine'\ncommands = ['echo build', 'echo ::release-point:: storage 47 to 48', 'echo say ::release-point::inside']\n",
            )
            .await;
            let runner = Runner::new(
                Api::new(&service.url, &service.runner_token),
                service.dir.path().join("runner"),
            )
            .idle_wait(TEST_IDLE_WAIT);
            assert!(runner.once(&Fake::new()).await.unwrap());
            let id = service.repo.id.clone();
            let sha = service.app.git.branch_tips(&service.bare).await.unwrap()["main"].clone();
            let attempts = service
                .app
                .store
                .read_sync(|r| crate::ci::queue::attempts_of(r, &id, &sha))
                .unwrap();
            assert_eq!(attempts[0].release_points, ["storage 47 to 48"]);
        }

        /// The same runner must recover on the next attempt after msb arrives.
        #[cfg(unix)]
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn a_runtime_installed_after_startup_runs_the_next_job() {
            // Isolate the resolver's fallback paths without changing this process's
            // environment or depending on an msb installed in the developer's home.
            const CHILD: &str = "GUILDBOTICS_TEST_LATE_RUNTIME_CHILD";
            if std::env::var_os(CHILD).is_none() {
                let run_child = |mode: &'static str| async move {
                    let home = tempfile::tempdir().unwrap();
                    let (_, module) = module_path!().split_once("::").unwrap();
                    let name =
                        format!("{module}::a_runtime_installed_after_startup_runs_the_next_job");
                    let output = tokio::process::Command::new(std::env::current_exe().unwrap())
                        .args(["--exact", &name, "--nocapture"])
                        .env(CHILD, mode)
                        .env("HOME", home.path())
                        .env("USERPROFILE", home.path())
                        .env("GUILDBOTICS_HOME", home.path().join(".guildbotics"))
                        .output()
                        .await
                        .unwrap();
                    assert!(output.status.success(), "{mode}: {output:?}");
                    let stdout = String::from_utf8(output.stdout).unwrap();
                    assert!(stdout.contains("1 passed"), "{mode}: {stdout}");
                };
                tokio::join!(run_child("ready"), run_child("doctor-failure"));
                return;
            }
            let doctor_fails = std::env::var(CHILD).unwrap() == "doctor-failure";
            let s = service(
                "[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"echo ok\"]\n",
            )
            .await;
            // This stand-in never starts a VM. Give its claims a budget that
            // fits even on a small test runner.
            let limits = crate::ci::config::JobLimits {
                max_cpus: 1,
                max_memory_mib: 512,
                ..s.app.ci.settings().limits()
            };
            s.app
                .ci
                .settings()
                .set_limits(Some("runner1"), limits)
                .await
                .unwrap();
            let msb = s.dir.path().join("msb");
            let home = s.dir.path().join("msb-home");
            let mut settings = Settings::new(
                s.url.clone(),
                Some(s.runner_token.clone()),
                Some(s.dir.path().join("runner")),
                None,
                None,
            )
            .unwrap();
            settings.msb_path = Some(msb.clone());
            settings.msb_home = home.clone();
            settings.idle_wait = TEST_IDLE_WAIT;
            let serving = tokio::spawn(run(settings));
            let sha = s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone();
            let settled = |sha: String| {
                let s = &s;
                async move {
                    let result = tokio::time::timeout(Duration::from_secs(20), async {
                        loop {
                            let result = result_of(s, &sha).await;
                            if result.conclusion.is_some() {
                                return result;
                            }
                            tokio::time::sleep(Duration::from_millis(20)).await;
                        }
                    })
                    .await;
                    match result {
                        Ok(result) => result,
                        Err(_) => panic!(
                            "attempt {sha} did not finish: {}",
                            s.app.ci.diagnostics().await
                        ),
                    }
                }
            };
            tokio::time::timeout(Duration::from_secs(10), async {
                loop {
                    if s.app.ci.diagnostics().await["state"] == "executor_unavailable" {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            assert_eq!(result_of(&s, &sha).await.status, "queued");
            // Install atomically, as the embedding host does. No VM is started.
            let staged = s.dir.path().join("msb-staged");
            executor::test_support::write_script(
                &staged,
                r#"#!/bin/sh
echo "$1" >> "$0.calls"
case "$1" in
--version) echo 'msb 0.6.17' ;;
doctor) [ ! -f "$MSB_HOME/doctor-fails" ] ;;
remove) exit 0 ;;
volume)
  case "$2" in
  create) : > "$MSB_HOME/disk-$3" ;;
  inspect) printf 'Path:           %s\n' "$MSB_HOME/disk-$3" ;;
  *) echo '[]' ;;
  esac ;;
image)
  if [ "$2" = list ]; then
    echo '[{"reference":"alpine","digest":"sha256:m"}]'
  else
    printf '{"reference":"alpine","digest":"sha256:m","config":{"digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
  fi ;;
run)
  prev=''
  for arg in "$@"; do
    if [ "$prev" = '--mount-dir' ]; then
      case "$arg" in *:/.branvil-log:*) printf '\036branvil:started\nok\n' > "${arg%%:/.branvil-log:*}/out" ;; esac
    fi
    prev="$arg"
  done
  touch "$MSB_HOME/ran" ;;
*) exit 1 ;;
esac
"#,
            );
            if doctor_fails {
                std::fs::create_dir_all(&home).unwrap();
                std::fs::write(home.join("doctor-fails"), "").unwrap();
            }
            std::fs::rename(staged, &msb).unwrap();
            if doctor_fails {
                tokio::time::timeout(Duration::from_secs(10), async {
                    loop {
                        if std::fs::read_to_string(msb.with_extension("calls"))
                            .unwrap_or_default()
                            .lines()
                            .any(|l| l == "doctor")
                        {
                            break;
                        }
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
                assert_eq!(result_of(&s, &sha).await.status, "queued");
            } else {
                let result = settled(sha.clone()).await;
                assert_eq!(
                    result.conclusion.as_deref(),
                    Some("success"),
                    "{:?}",
                    result.details
                );
            }
            assert_eq!(home.join("ran").is_file(), !doctor_fails);
            assert!(!serving.is_finished(), "the same runner is still serving");
            if !doctor_fails {
                let another_sha = empty_main(&s, "runtime still ready").await;
                let result = settled(another_sha).await;
                assert_eq!(
                    result.conclusion.as_deref(),
                    Some("success"),
                    "{:?}",
                    result.details
                );
            }
            observe_more_claims(&s.claims).await;
            let calls = std::fs::read_to_string(msb.with_extension("calls")).unwrap();
            assert_eq!(
                calls.lines().filter(|c| *c == "doctor").count(),
                1,
                "{calls}"
            );
            assert_eq!(
                calls.lines().filter(|c| *c == "--version").count(),
                1,
                "{calls}"
            );
            s.revoke_runner_token().await;
            assert!(
                tokio::time::timeout(Duration::from_secs(10), serving)
                    .await
                    .expect("the runner stops")
                    .unwrap()
                    .is_err()
            );
        }

        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn a_pull_requests_head_reuses_a_result_by_the_contents_of_paths() {
            let s = service("[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"write out/report yes\"]\npaths = [\"src/**\"]\n").await;
            let runner = Runner::new(
                Api::new(&s.url, &s.runner_token),
                s.dir.path().join("runner"),
            )
            .idle_wait(TEST_IDLE_WAIT);
            let fake = Fake::new();
            let first_sha = s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone();
            runner.once(&fake).await.unwrap();
            let first = result_of(&s, &first_sha).await;
            assert!(first.inputs.is_some());
            // A pull request's head that changes only docs.
            let work = s.dir.path().join("work");
            git(&work, &["checkout", "-qb", "docs"]);
            std::fs::write(work.join("notes.md"), "docs\n").unwrap();
            git(&work, &["add", "notes.md"]);
            git(&work, &["commit", "-qm", "docs"]);
            git(&work, &["push", "-q", s.bare.to_str().unwrap(), "docs"]);
            let sha = git(&work, &["rev-parse", "HEAD"]).trim().to_string();
            let tree = s.app.git.tree_oid(&s.bare, &sha).await.unwrap();
            let (repo, at) = (s.repo.id.clone(), sha.clone());
            let source_sha = first_sha.clone();
            s.app
                .store
                .write(move |w| {
                    let main = crate::ci::queue::get_run(w, &repo, &source_sha)?.unwrap();
                    let reg = crate::ci::queue::Registration {
                        order: Vec::new(),
                        selection: None,
                        repo,
                        repo_path: "acme/demo".into(),
                        sha: at,
                        tree,
                        branch: "docs".into(),
                        protected: false,
                        pipeline: main.pipeline,
                        network: false,
                        inputs: main.inputs,
                    };
                    crate::ci::queue::register(w, reg, 10, &crate::util::now())
                })
                .await
                .unwrap();
            runner.once(&fake).await.unwrap();
            let reused = result_of(&s, &sha).await;
            assert_eq!(reused.reused_from.as_deref(), Some(first.id.as_str()));
            assert_eq!(fake.ran.lock().unwrap().len(), 1);
            let log = s.app.ci.log_tail(&reused.id, 1 << 20).await.unwrap().0;
            assert!(
                log.contains(&format!(
                    "reused result of {} ({first_sha}, the same contents of paths)",
                    first.id
                )),
                "{log}"
            );
            let checks = s.app.ci.head_checks(&s.repo, &sha).await.unwrap().checks;
            let from = checks[0].reused_from.as_ref().unwrap();
            assert_eq!(
                (from.sha.as_str(), from.basis),
                (first_sha.as_str(), crate::ci::queue::Basis::Paths)
            );
        }

        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn reuse_skips_source_and_vm_but_digest_changes_and_retry_execute() {
            let s = service("[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"write out/report yes\"]\n").await;
            let runner = Runner::new(
                Api::new(&s.url, &s.runner_token),
                s.dir.path().join("runner"),
            )
            .idle_wait(TEST_IDLE_WAIT);
            let fake = Fake::new();
            let first_sha = s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone();
            runner.once(&fake).await.unwrap();
            let first = result_of(&s, &first_sha).await;
            let sha = empty_main(&s, "same tree").await;
            runner.once(&fake).await.unwrap();
            let reused = result_of(&s, &sha).await;
            assert_eq!(reused.reused_from.as_deref(), Some(first.id.as_str()));
            assert_eq!(reused.image_digest, first.image_digest);
            assert!(reused.cache_key.is_none());
            assert_eq!(fake.ran.lock().unwrap().len(), 1);
            let log = s.app.ci.log_tail(&reused.id, 1 << 20).await.unwrap().0;
            assert!(
                log.contains(&format!(
                    "reused result of {} ({first_sha}, the same tree)",
                    first.id
                )),
                "{log}"
            );
            assert!(
                !log.contains("source fetched") && !log.contains("sandbox started"),
                "{log}"
            );
            assert!(reused.details.is_none());
            let checks = s.app.ci.head_checks(&s.repo, &sha).await.unwrap().checks;
            assert!(checks[0].succeeded());
            assert!(checks[0].details.is_none());
            assert!(checks[0].artifact_names.is_empty());
            assert_eq!(checks[0].reused_from.as_ref().unwrap().attempt_id, first.id);
            assert_eq!(
                checks[0].reused_from.as_ref().unwrap().basis,
                crate::ci::queue::Basis::Tree
            );
            let view: serde_json::Value = s
                .http
                .get(format!(
                    "{}/api/v1/repos/acme%2Fdemo/jobs/{}",
                    s.url, reused.id
                ))
                .bearer_auth(&s.owner)
                .send()
                .await
                .unwrap()
                .json()
                .await
                .unwrap();
            assert_eq!(view["reused_from"]["sha"], first_sha);
            assert_eq!(view["reused_from"]["basis"], "tree");
            assert_eq!(
                view["reused_from"]["url"],
                format!("{}/workspaces/acme/repos/demo/jobs/{}", s.url, first.id)
            );
            assert_eq!(view["artifact_names"], serde_json::json!([]));
            assert!(view["details"].is_null());

            fake.changed_image.store(true, Ordering::SeqCst);
            let changed_sha = empty_main(&s, "new digest").await;
            runner.once(&fake).await.unwrap();
            let changed = result_of(&s, &changed_sha).await;
            assert!(changed.reused_from.is_none());
            assert_eq!(fake.ran.lock().unwrap().len(), 2);
            let log = s.app.ci.log_tail(&changed.id, 1 << 20).await.unwrap().0;
            assert!(
                log.contains("image digest differs") && log.contains("source fetched"),
                "{log}"
            );
            let next_sha = empty_main(&s, "reuse new digest").await;
            runner.once(&fake).await.unwrap();
            let next = result_of(&s, &next_sha).await;
            assert_eq!(next.reused_from.as_deref(), Some(changed.id.as_str()));
            let id = next.id.clone();
            s.app
                .store
                .write(move |w| crate::ci::queue::retry(w, &id, 10, &crate::util::now()))
                .await
                .unwrap();
            runner.once(&fake).await.unwrap();
            let retried = result_of(&s, &next_sha).await;
            assert!(retried.reused_from.is_none());
            let retry_log = s.app.ci.log_tail(&retried.id, 1 << 20).await.unwrap().0;
            assert!(
                retry_log.contains("reuse unavailable: no reuse offered"),
                "{retry_log}"
            );
            assert!(
                !retry_log.contains("no matching successful execution"),
                "{retry_log}"
            );
            assert_eq!(fake.ran.lock().unwrap().len(), 3);
        }

        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn artifact_jobs_execute_each_commit_and_keep_retention_per_execution() {
            let s = service("[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"write out/report yes\"]\nartifacts = [{name = \"report\", paths = [\"out/\"]}]\n").await;
            let runner = Runner::new(
                Api::new(&s.url, &s.runner_token),
                s.dir.path().join("runner"),
            )
            .idle_wait(TEST_IDLE_WAIT);
            let fake = Fake::new();
            let first_sha = s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone();
            runner.once(&fake).await.unwrap();
            let first = result_of(&s, &first_sha).await;
            let sha = empty_main(&s, "same tree").await;
            runner.once(&fake).await.unwrap();
            let current = result_of(&s, &sha).await;
            assert!(current.reused_from.is_none());
            assert_eq!(fake.ran.lock().unwrap().len(), 2);
            let log = s.app.ci.log_tail(&current.id, 1 << 20).await.unwrap().0;
            assert!(log.contains("source fetched"), "{log}");
            let checks = s.app.ci.head_checks(&s.repo, &sha).await.unwrap().checks;
            assert!(checks[0].succeeded());
            assert!(checks[0].reused_from.is_none());
            assert_eq!(checks[0].artifact_names, vec!["report"]);
            let response = s
                .http
                .get(format!(
                    "{}/api/v1/repos/acme%2Fdemo/commits/{sha}/artifacts/report",
                    s.url
                ))
                .bearer_auth(&s.owner)
                .send()
                .await
                .unwrap();
            assert_eq!(response.status(), 200);
            assert_eq!(
                response.bytes().await.unwrap().as_ref(),
                std::fs::read(s.app.ci.artifact_path(&current.id, "report")).unwrap()
            );

            // Expiring the previous commit's execution cannot expire these artifacts.
            let id = first.id.clone();
            s.app
                .store
                .write(move |w| {
                    let mut expired = first;
                    expired.finished_at =
                        Some(crate::ci::queue::earlier(&crate::util::now(), 31 * 86_400));
                    w.put(
                        crate::store::CI_ATTEMPTS,
                        &crate::ci::queue::Attempt::key(&expired.id),
                        &expired,
                    )
                })
                .await
                .unwrap();
            crate::ci::files::apply_retention(&s.app).await.unwrap();
            assert!(!s.app.ci.artifact_path(&id, "report").exists());
            assert!(s.app.ci.artifact_path(&current.id, "report").exists());
            assert_eq!(
                s.app.ci.head_checks(&s.repo, &sha).await.unwrap().checks[0].artifact_names,
                vec!["report"]
            );
        }

        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn runner_publishes_only_the_merged_heads_pending_image() {
            let s = service("[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"write target/out built\"]\ncache = { paths = [\"target\"], key_files = [\"Cargo.lock\"] }\n").await;
            let settings = s
                .http
                .patch(format!("{}/api/v1/repos/acme%2Fdemo", s.url))
                .bearer_auth(&s.owner)
                .json(&serde_json::json!({"revision":s.repo.revision,"required_approvals":0}))
                .send()
                .await
                .unwrap();
            assert_eq!(settings.status(), 200, "{}", settings.text().await.unwrap());
            let runner = Runner::new(
                Api::new(&s.url, &s.runner_token),
                s.dir.path().join("runner"),
            )
            .idle_wait(TEST_IDLE_WAIT);
            let fake = Fake::new();
            assert!(runner.once(&fake).await.unwrap());
            let main = s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone();
            let published = result_of(&s, &main).await.cache_key.unwrap();
            let work = s.dir.path().join("work");
            let cache = runner.test_cache();
            let restored = s.dir.path().join("restored");
            for (branch, merge) in [("abandoned", false), ("landed", true)] {
                git(&work, &["checkout", "-qb", branch, "main"]);
                std::fs::write(work.join("Cargo.lock"), branch).unwrap();
                git(&work, &["add", "."]);
                git(&work, &["commit", "-qm", branch]);
                git(&work, &["push", "-q", s.bare.to_str().unwrap(), branch]);
                crate::refs::record_refs(&s.app, &s.repo).await.unwrap();
                let pulls = format!("{}/api/v1/repos/acme%2Fdemo/pulls", s.url);
                let response = s
                    .http
                    .post(&pulls)
                    .bearer_auth(&s.owner)
                    .json(&serde_json::json!({"title": branch, "head": branch, "base": "main"}))
                    .send()
                    .await
                    .unwrap();
                assert_eq!(response.status(), 201);
                let pull: serde_json::Value = response.json().await.unwrap();
                let number = pull["pull"]["number"].as_u64().unwrap();
                let head = s.app.git.branch_tips(&s.bare).await.unwrap()[branch].clone();
                assert!(runner.once(&fake).await.unwrap());
                let attempt = result_of(&s, &head).await;
                let key = attempt.cache_key.unwrap();
                assert_ne!(key, published);
                assert_eq!(
                    cache.pending().unwrap().as_slice(),
                    std::slice::from_ref(&attempt.id)
                );
                assert!(cache.restore(&key, &restored).unwrap().is_none());
                assert_eq!(cache.restore(&published, &restored).unwrap(), Some(6));
                std::fs::remove_file(&restored).unwrap();
                let log = s.app.ci.log_tail(&attempt.id, 1 << 20).await.unwrap().0;
                assert!(
                    log.contains(&format!("latest generation {published} (requested {key})")),
                    "{log}"
                );
                let action = if merge { "merge" } else { "close" };
                let response = s
                    .http
                    .post(format!("{pulls}/{number}/{action}"))
                    .bearer_auth(&s.owner)
                    .json(&if merge {
                        serde_json::json!({"expected_head":head,"expected_base":main})
                    } else {
                        serde_json::json!({})
                    })
                    .send()
                    .await
                    .unwrap();
                assert_eq!(response.status(), 200, "{}", response.text().await.unwrap());
                let executions = fake.ran.lock().unwrap().len();
                runner.once(&fake).await.unwrap();
                assert!(!runner.once(&fake).await.unwrap());
                assert_eq!(fake.ran.lock().unwrap().len(), executions);
                assert!(cache.pending().unwrap().is_empty());
                assert_eq!(cache.restore(&key, &restored).unwrap(), merge.then_some(6));
            }
        }

        /// Up to `max_jobs` attempts run at once, each to its own end. The
        /// runner receives the limit from the Hub on every claim, so
        /// a new limit applies to the next claim and stops no running job.
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn the_runner_runs_up_to_max_jobs_attempts_at_once() {
            let pipeline: String = ["a", "b", "c", "d"]
                .iter()
                .map(|n| {
                    format!(
                        "[[jobs]]\nname = \"{n}\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n\n"
                    )
                })
                .collect();
            let Service {
                dir,
                app,
                url,
                owner,
                runner_token,
                claims,
                http,
                repo,
                bare,
            } = service_with_lease(&pipeline, None, 60).await;
            let max_jobs = |n| {
                let app = app.clone();
                async move {
                    let limits = crate::ci::config::JobLimits {
                        max_jobs: n,
                        ..app.ci.settings().limits()
                    };
                    app.ci
                        .settings()
                        .set_limits(Some("runner1"), limits)
                        .await
                        .unwrap();
                }
            };
            max_jobs(2).await;
            let runner = Runner::new(Api::new(&url, &runner_token), dir.path().join("runner"))
                .idle_wait(TEST_IDLE_WAIT);
            // This tests scheduling, not heartbeat frequency. A three-second
            // lease can expire under CI contention and this fixture has no reaper.
            // Complete jobs explicitly; cancellation delivery is tested separately.
            let fake = Arc::new(Fake {
                controlled: true,
                ..Fake::new()
            });
            let serving = tokio::spawn(Arc::new(runner).serve({
                let fake = fake.clone();
                move || fake.clone()
            }));
            let sha = app.git.branch_tips(&bare).await.unwrap()["main"].clone();
            // The checks once `done` holds of them (by name: status, attempt).
            let settle = |phase: &'static str, done: fn(&[(String, String, u64)]) -> bool| {
                let (app, repo, sha) = (app.clone(), repo.clone(), sha.clone());
                let fake = fake.clone();
                async move {
                    let mut last = Vec::new();
                    for _ in 0..100 {
                        let mut seen: Vec<(String, String, u64, String)> = app
                            .ci
                            .head_checks(&repo, &sha)
                            .await
                            .unwrap()
                            .checks
                            .into_iter()
                            .map(|c| {
                                let status = c.conclusion.unwrap_or(c.status);
                                (
                                    c.name,
                                    status,
                                    c.attempt.unwrap_or(0),
                                    c.attempt_id.unwrap_or_default(),
                                )
                            })
                            .collect();
                        seen.sort();
                        let states: Vec<_> = seen
                            .iter()
                            .map(|c| (c.0.clone(), c.1.clone(), c.2))
                            .collect();
                        if done(&states)
                            && seen
                                .iter()
                                .filter(|c| c.1 == "in_progress")
                                .all(|c| fake.finishes.lock().unwrap().contains_key(&c.3))
                        {
                            return seen;
                        }
                        last = seen;
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                    let mut logs = Vec::new();
                    for check in &last {
                        if !check.3.is_empty() {
                            let log = app.ci.log_tail(&check.3, 4096).await.unwrap().0;
                            logs.push((check.0.clone(), log));
                        }
                    }
                    panic!(
                        "{phase}: checks={last:?}; logs={logs:?}; executor started={}, stopped={}",
                        fake.waiting.load(Ordering::SeqCst),
                        fake.stopped.load(Ordering::SeqCst)
                    );
                }
            };
            let cancel = |id: String| {
                let (http, url, owner) = (http.clone(), url.clone(), owner.clone());
                let fake = fake.clone();
                async move {
                    let resp = http
                        .post(format!("{url}/api/v1/repos/acme%2Fdemo/jobs/{id}/cancel"))
                        .bearer_auth(&owner)
                        .send()
                        .await
                        .unwrap();
                    assert_eq!(resp.status(), 200);
                    fake.finishes
                        .lock()
                        .unwrap()
                        .get(&id)
                        .expect("executor started")
                        .notify_one();
                }
            };
            fn count(s: &[(String, String, u64)], status: &str) -> usize {
                s.iter().filter(|c| c.1 == status).count()
            }

            // Two run at once; the others wait in the queue, and the end of one
            // starts the next.
            let seen = settle("initial two claims", |s| {
                count(s, "in_progress") == 2 && count(s, "queued") == 2
            })
            .await;
            cancel(
                seen.iter()
                    .find(|c| c.1 == "in_progress")
                    .unwrap()
                    .3
                    .clone(),
            )
            .await;
            let seen = settle("replacement after cancellation", |s| {
                count(s, "cancelled") == 1
                    && count(s, "in_progress") == 2
                    && count(s, "queued") == 1
            })
            .await;
            let running: Vec<_> = seen.iter().filter(|c| c.1 == "in_progress").collect();
            // With the limit lowered to one, the end of one job leaves the other
            // running and the last queued.
            max_jobs(1).await;
            cancel(running[0].3.clone()).await;
            settle("lowered limit after cancellation", |s| {
                count(s, "cancelled") == 2 && count(s, "in_progress") == 1
            })
            .await;
            observe_more_claims(&claims).await;
            let seen = settle("lowered limit stays effective", |s| {
                count(s, "in_progress") == 1 && count(s, "queued") == 1
            })
            .await;
            assert!(
                seen.iter()
                    .any(|c| c.0 == running[1].0 && c.1 == "in_progress" && c.2 == 1),
                "{seen:?}"
            );
            // Raised again, the last starts next to it.
            max_jobs(2).await;
            let seen = settle("raised limit starts last job", |s| {
                count(s, "in_progress") == 2 && count(s, "cancelled") == 2
            })
            .await;
            for c in seen.iter().filter(|c| c.1 == "in_progress") {
                assert_eq!(c.2, 1, "never interrupted: {seen:?}");
                cancel(c.3.clone()).await;
            }
            settle("all cancelled", |s| count(s, "cancelled") == 4).await;
            serving.abort();
        }

        /// A refused token stops the runner, but only once the jobs it runs have
        /// ended on their own (each stopping and removing its microVM), not by
        /// dropping them with their VMs.
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn a_refused_token_lets_the_running_jobs_end_first() {
            let pipeline = "[[jobs]]\nname = \"a\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n\n[[jobs]]\nname = \"b\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n";
            let s = service(pipeline).await;
            let limits = crate::ci::config::JobLimits {
                max_jobs: 2,
                ..s.app.ci.settings().limits()
            };
            s.app
                .ci
                .settings()
                .set_limits(Some("runner1"), limits)
                .await
                .unwrap();
            let runner = Runner::new(
                Api::new(&s.url, &s.runner_token),
                s.dir.path().join("runner"),
            )
            .idle_wait(TEST_IDLE_WAIT);
            let fake = Arc::new(Fake::new());
            let serving = tokio::spawn(Arc::new(runner).serve({
                let fake = fake.clone();
                move || fake.clone()
            }));
            // `in_progress` means claimed, not that the executor has started.
            tokio::time::timeout(Duration::from_secs(10), async {
                while fake.waiting.load(Ordering::SeqCst) != 2 {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .expect("both jobs reached the executor's wait");
            s.revoke_runner_token().await;
            let stopped = tokio::time::timeout(Duration::from_secs(30), serving)
                .await
                .expect("the runner stops")
                .unwrap();
            assert!(stopped.is_err());
            assert_eq!(
                fake.stopped.load(Ordering::SeqCst),
                2,
                "both jobs ended on their own"
            );
        }

        /// Source fetching is part of the attempt's lifetime too: even after a
        /// claim is refused, each held request must return and clean up its job
        /// directory, without ever starting the executor on a failed fetch.
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn a_refused_token_lets_source_fetches_end_first() {
            let pipeline: String = ["a", "b", "c"]
                .iter()
                .map(|name| {
                    format!(
                        "[[jobs]]\nname = \"{name}\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n\n"
                    )
                })
                .collect();
            let (requested, mut requests) = mpsc::channel(2);
            let refused = Arc::new(tokio::sync::Notify::new());
            let source_refusals = Arc::new(std::sync::atomic::AtomicUsize::new(0));
            let s = service_with_source_gate(
                &pipeline,
                Some(SourceGate {
                    requested,
                    refused: refused.clone(),
                    source_refusals: source_refusals.clone(),
                }),
            )
            .await;
            let limits = crate::ci::config::JobLimits {
                // Leave capacity for another claim to observe the refusal
                // while all three source requests remain held.
                max_jobs: 4,
                ..s.app.ci.settings().limits()
            };
            s.app
                .ci
                .settings()
                .set_limits(Some("runner1"), limits)
                .await
                .unwrap();
            let runner = Arc::new(
                Runner::new(
                    Api::new(&s.url, &s.runner_token),
                    s.dir.path().join("runner"),
                )
                .idle_wait(TEST_IDLE_WAIT),
            );
            let fake = Arc::new(Fake::new());
            let serving = tokio::spawn(runner.clone().serve({
                let fake = fake.clone();
                move || fake.clone()
            }));
            let mut held = Vec::new();
            for _ in 0..3 {
                held.push(
                    tokio::time::timeout(Duration::from_secs(10), requests.recv())
                        .await
                        .expect("the source request arrives")
                        .unwrap(),
                );
            }
            s.revoke_runner_token().await;
            tokio::time::timeout(Duration::from_secs(10), refused.notified())
                .await
                .expect("the runner observes the refused token");
            // One source fetch returns 401 and cleans up; the runner must still
            // let the other two finish instead of dropping their tasks.
            held.pop().unwrap().send(()).unwrap();
            tokio::time::timeout(Duration::from_secs(10), async {
                while std::fs::read_dir(runner.work_dir.join("jobs"))
                    .unwrap()
                    .count()
                    != held.len()
                {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .expect("the first source fetch ended and removed its directory");
            assert!(!serving.is_finished(), "source requests are still held");
            assert!(fake.ran.lock().unwrap().is_empty());
            for resume in held {
                resume.send(()).unwrap();
            }
            let result = tokio::time::timeout(Duration::from_secs(10), serving)
                .await
                .expect("the runner stops after all source fetches end")
                .unwrap();
            assert!(
                result
                    .unwrap_err()
                    .to_string()
                    .contains("token is not valid")
            );
            assert!(fake.ran.lock().unwrap().is_empty(), "no executor started");
            assert_eq!(source_refusals.load(Ordering::SeqCst), 3);
            assert_eq!(fake.stopped.load(Ordering::SeqCst), 0);
            assert_eq!(
                std::fs::read_dir(runner.work_dir.join("jobs"))
                    .unwrap()
                    .count(),
                0,
                "every attempt removed its directory before the runner returned"
            );
        }

        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        async fn the_runner_loop_reports_results_cancellation_and_an_unavailable_executor() {
            let Service {
            dir,
            app,
            url,
            owner,
            runner_token,
            http,
            repo,
            bare,
            ..
        } = service(
            "[[jobs]]\nname = \"ok\"\nimage = \"alpine\"\ncommands = [\"echo hello from the job\", \"write out/r.txt report\", \"git rev-parse HEAD\", \"git describe --tags\", \"git tag --list\", \"git ls-files\", \"git config --list --local\"]\nartifacts = [{name = \"report\", paths = [\"out/\"]}]\n\n\
             [[jobs]]\nname = \"slow\"\nimage = \"alpine\"\ncommands = [\"wait\"]\ntimeout_secs = 5000\n",
        )
        .await;

            let limits = crate::ci::config::JobLimits {
                max_cpus: 3,
                max_memory_mib: 2048,
                work_quota_mib: 8192,
                max_timeout_secs: 100,
                ..app.ci.settings().limits()
            };
            app.ci
                .settings()
                .set_limits(Some("runner1"), limits)
                .await
                .unwrap();
            let runner = Runner::new(Api::new(&url, &runner_token), dir.path().join("runner"))
                .idle_wait(TEST_IDLE_WAIT);
            let fake = Fake::new();
            let job = |id: &str| {
                let (http, url, owner, id) =
                    (http.clone(), url.clone(), owner.clone(), id.to_string());
                async move {
                    http.get(format!("{url}/api/v1/repos/acme%2Fdemo/jobs/{id}"))
                        .bearer_auth(&owner)
                        .send()
                        .await
                        .unwrap()
                        .json::<serde_json::Value>()
                        .await
                        .unwrap()
                }
            };
            let sha = app.git.branch_tips(&bare).await.unwrap()["main"].clone();
            let checks = || {
                let (app, repo, sha) = (app.clone(), repo.clone(), sha.clone());
                async move { app.ci.head_checks(&repo, &sha).await.unwrap().checks }
            };

            // Job "ok": output streamed, artifact collected, success.
            assert!(runner.once(&fake).await.unwrap());
            let ok = checks().await.into_iter().find(|c| c.name == "ok").unwrap();
            assert_eq!(ok.conclusion.as_deref(), Some("success"), "{ok:?}");
            // The executor runs the tag with the runner's CPUs, memory, and
            // work directory quota, and the runner's time limit for a job that
            // gives none.
            assert_eq!(
                fake.ran.lock().unwrap()[0],
                ("alpine".to_string(), 3, 2048, 100, 8192)
            );
            assert_eq!(ok.artifact_names, vec!["report".to_string()]);
            let id = ok.attempt_id.clone().unwrap();
            // The attempt records the image it ran on, and the log says so first.
            let digest = digest_of("alpine");
            assert_eq!(job(&id).await["image_digest"], digest.as_str());
            let (log, _) = app.ci.log_tail(&id, 1 << 20).await.unwrap();
            let (first, rest) = log.split_once("\n[guildbotics] queued for ").unwrap();
            assert_eq!(
                first,
                format!(
                    "[guildbotics] attempt 1 of job ok on acme/demo {sha} (executor fake, image alpine, 3 cpu, 2048 MiB, timeout 100 s, network off)"
                )
            );
            assert!(
                rest.split_once(" s\n")
                    .is_some_and(|(secs, rest)| secs.parse::<u64>().is_ok()
                        && rest.starts_with(&format!("[guildbotics] image alpine is {digest}\n"))),
                "{log}"
            );
            // Each step of the runner's own says how long it took.
            for step in [
                "[guildbotics] source fetched in ",
                "[guildbotics] checked out in ",
                "[guildbotics] artifacts handled in ",
            ] {
                assert!(log.contains(step), "{step} in {log}");
            }
            assert!(log.contains("hello from the job"), "{log}");
            // The work directory is a checkout of the commit with its history
            // and the tags it reaches, and without a remote.
            assert!(
                log.contains(&format!("\n{sha}\nv1\nv1\n.guildbotics-ci.toml\n")),
                "{log}"
            );
            assert!(!log.contains("later") && !log.contains("remote."), "{log}");
            assert!(
                log.contains("artifact report uploaded (1 file(s))"),
                "{log}"
            );
            assert!(
                !runner.work_dir.join("jobs").join(&id).exists(),
                "work directory removed"
            );

            // Job "slow": cancelled through the API while it runs, then again
            // while its check waits for the image cache.
            let cancel_slow = |runner: Runner, fake: Fake| {
                let (checks, http, url, owner) = (checks, http.clone(), url.clone(), owner.clone());
                async move {
                    let running = tokio::spawn(async move {
                        let r = runner.once(&fake).await;
                        (runner, fake, r)
                    });
                    let mut slow_id = None;
                    for _ in 0..50 {
                        if let Some(c) = checks()
                            .await
                            .into_iter()
                            .find(|c| c.name == "slow" && c.status == "in_progress")
                        {
                            slow_id = c.attempt_id;
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                    let slow_id = slow_id.expect("slow job running");
                    let resp = http
                        .post(format!(
                            "{url}/api/v1/repos/acme%2Fdemo/jobs/{slow_id}/cancel"
                        ))
                        .bearer_auth(&owner)
                        .send()
                        .await
                        .unwrap();
                    assert_eq!(resp.status(), 200);
                    let (runner, fake, r) = tokio::time::timeout(Duration::from_secs(20), running)
                        .await
                        .unwrap()
                        .unwrap();
                    assert!(r.unwrap());
                    (runner, fake, slow_id)
                }
            };
            let (runner, fake, slow_id) = cancel_slow(runner, fake).await;
            let slow = job(&slow_id).await;
            assert_eq!(slow["conclusion"], "cancelled", "{slow}");
            assert_eq!(slow["image_digest"], digest.as_str());
            // A larger time limit is lowered to the runner's, and the log says so.
            assert_eq!(fake.ran.lock().unwrap()[1].3, 100);
            let (log, _) = app.ci.log_tail(&slow_id, 1 << 20).await.unwrap();
            assert!(
                log.contains(
                    "[guildbotics] timeout_secs 5000 exceeds the runner limit 100; using 100\n"
                ),
                "{log}"
            );
            fake.hold_images.store(true, Ordering::SeqCst);
            let resp = http
                .post(format!(
                    "{url}/api/v1/repos/acme%2Fdemo/jobs/{slow_id}/retry"
                ))
                .bearer_auth(&owner)
                .send()
                .await
                .unwrap();
            assert_eq!(resp.status(), 200);
            let (runner, fake, waiting_id) = cancel_slow(runner, fake).await;
            let waiting = job(&waiting_id).await;
            assert_eq!(waiting["conclusion"], "cancelled", "{waiting}");
            assert!(waiting["image_digest"].is_null(), "{waiting}");
            // Its log still names the attempt it was.
            let (log, _) = app.ci.log_tail(&waiting_id, 1 << 20).await.unwrap();
            assert!(
                log.starts_with("[guildbotics] attempt 2 of job slow on acme/demo ")
                    && log.contains("job ended: cancelled while waiting for the image cache"),
                "{log}"
            );
            assert_eq!(
                fake.ran.lock().unwrap().len(),
                2,
                "the waiting job never ran"
            );
            fake.hold_images.store(false, Ordering::SeqCst);

            // An unusable executor does not take a job away from a ready runner.
            fake.ready.store(false, Ordering::SeqCst);
            let resp = http
                .post(format!("{url}/api/v1/repos/acme%2Fdemo/jobs/{id}/retry"))
                .bearer_auth(&owner)
                .send()
                .await
                .unwrap();
            assert_eq!(resp.status(), 200);
            assert!(!runner.once(&fake).await.unwrap());
            let ok = checks().await.into_iter().find(|c| c.name == "ok").unwrap();
            assert_eq!(ok.attempt, Some(2));
            assert_eq!(ok.status, "queued");
            let diag = app.ci.diagnostics().await;
            assert_eq!(diag["state"], "executor_unavailable", "{diag}");
            assert!(!runner.once(&fake).await.unwrap(), "nothing left");
        }
        #[test]
        fn only_declared_jobs_receive_verified_sha_in_the_executor_environment() {
            for declared in [false, true] {
                for verified in [None, Some("verified")] {
                    let c: Claim = serde_json::from_value(serde_json::json!({
                    "attempt_id":"att", "lease":"lease", "lease_secs":60,
                    "repo":"acme/demo", "sha":"head", "attempt":1,
                    "job":{"name":"test", "image":"alpine", "commands":["true"], "paths":[],
                        "timeout_secs":null, "tmpfs_mib":null, "ram_workspace":null, "artifacts":[], "cache":null, "verified_sha":declared},
                    "verified_sha":verified, "cache_write":true, "network":false,
                    "queued_secs":0, "reuse":null, "reuse_only":false,
                    "limits": crate::ci::config::JobLimits::from(&crate::ci::config::ExecutorConfig::default()),
                    "artifacts_max_mib":64
                }))
                .unwrap();
                    let env = job_environment(&c);
                    let value = env
                        .iter()
                        .find(|(k, _)| k == "GB_CI_VERIFIED_SHA")
                        .map(|(_, v)| v.as_str());
                    assert_eq!(value, declared.then_some(verified.unwrap_or_default()));
                    assert!(env.contains(&("GB_CI_SHA".into(), "head".into())));
                }
            }
        }
    }
}
