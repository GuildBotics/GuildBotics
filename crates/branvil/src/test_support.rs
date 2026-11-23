//! Embedded service fixtures for downstream tests. No external runner is needed.
pub(crate) mod routes;
mod service;
pub use service::{
    FileVault, OWNER_TOKEN, Running, SESSION_COOKIE, SESSION_CSRF, TestHost, initialize, serve,
};

use crate::api::{DataApi, MemberApi};
use crate::ci::pipeline::{self, PIPELINE_PATH};
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use std::{path::Path, sync::Arc};

/// Keeps a fixture on its own runtime for synchronous downstream setup code.
pub struct Background {
    pub data: DataApi,
    pub owner: MemberApi,
    pub root: std::path::PathBuf,
    stop: Option<tokio::sync::oneshot::Sender<()>>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Background {
    pub fn start(
        workspace: &str,
        repositories: &[&str],
        members: &[&str],
        hook_test: &str,
    ) -> Self {
        let hook_test = hook_test.to_owned();
        let workspace = workspace.to_owned();
        let repositories = repositories
            .iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>();
        let members = members.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let (ready, started) = std::sync::mpsc::sync_channel(1);
        let (stop, stopped) = tokio::sync::oneshot::channel();
        let thread = std::thread::spawn(move || {
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .unwrap()
                .block_on(async move {
                    let fixture = Fixture::new("owner").await;
                    fixture.install_test_hooks(&hook_test);
                    fixture.workspace(&workspace).await;
                    for repository in repositories {
                        fixture.repository(&repository).await;
                    }
                    for member in members {
                        fixture.member(&member).await;
                    }
                    ready
                        .send((
                            fixture.data(),
                            fixture.owner.clone(),
                            fixture.root().to_path_buf(),
                        ))
                        .unwrap();
                    let _ = stopped.await;
                    fixture.stop().await;
                });
        });
        let (data, owner, root) = started.recv().expect("fixture startup");
        Self {
            data,
            owner,
            root,
            stop: Some(stop),
            thread: Some(thread),
        }
    }
}
impl Drop for Background {
    fn drop(&mut self) {
        self.stop.take();
        if let Some(thread) = self.thread.take() {
            thread.join().expect("fixture shutdown");
        }
    }
}

// Fixture locks have the same process lifetime as the service lock. Only paths
// explicitly registered by this module can use more than one lock per process.
static LOCKS: std::sync::LazyLock<
    std::sync::Mutex<std::collections::BTreeMap<std::path::PathBuf, Option<std::fs::File>>>,
> = std::sync::LazyLock::new(Default::default);
pub(crate) fn hold_fixture(
    layout: &crate::config::Layout,
    file: std::fs::File,
) -> Result<(), std::fs::File> {
    let mut locks = LOCKS.lock().unwrap();
    if let Some(slot) = locks.get_mut(&layout.git_config()) {
        *slot = Some(file);
        Ok(())
    } else {
        Err(file)
    }
}
#[cfg(unix)]
pub(crate) fn lock_fd(config: &Path) -> Option<std::os::fd::RawFd> {
    use std::os::fd::AsRawFd;
    LOCKS
        .lock()
        .unwrap()
        .get(config)
        .and_then(|slot| slot.as_ref().map(AsRawFd::as_raw_fd))
}

/// The member a bearer token authenticates as over HTTP; `None` when refused.
pub async fn token_member(data: &DataApi, token: &str) -> Option<String> {
    let response = reqwest::Client::builder()
        .no_proxy()
        .build()
        .unwrap()
        .get(format!("{}/api/v1/me", data.base_url()))
        .bearer_auth(token)
        .send()
        .await
        .unwrap();
    if response.status() == reqwest::StatusCode::UNAUTHORIZED {
        return None;
    }
    let me: Value = response.error_for_status().unwrap().json().await.unwrap();
    Some(me["name"].as_str().unwrap().into())
}

const CHECK_PIPELINE: &str = r#"
[[jobs]]
name = "test"
image = "docker.io/library/alpine:3.22"
commands = ["true"]
"#;

pub fn input<T: DeserializeOwned>(value: Value) -> T {
    serde_json::from_value(value).expect("fixture input")
}

/// Owns the service and its temporary storage, in shutdown order.
pub struct Fixture {
    pub running: Running,
    pub owner: MemberApi,
    /// A Branvil token of the owner, for Git and the HTTP API.
    pub owner_token: String,
    root: tempfile::TempDir,
}

impl Fixture {
    /// A downstream libtest entry point can dispatch the ordinary Rust hook.
    pub fn install_test_hooks(&self, test_name: &str) {
        let quote = |s: &str| format!("'{}'", s.replace('\'', "'\\''"));
        let exe = quote(std::env::current_exe().unwrap().to_str().unwrap());
        for hook in ["pre-receive", "post-receive"] {
            let path = self.root.path().join("hooks").join(hook);
            std::fs::write(
                path,
                format!(
                    "#!/bin/sh\nGUILDBOTICS_TEST_HOOK={hook} exec {exe} --exact {} --nocapture\n",
                    quote(test_name)
                ),
            )
            .unwrap();
        }
    }
    pub async fn new(owner: &str) -> Self {
        Self::configured(owner, |_| ()).await.0
    }

    /// Configure upstream stubs before the service opens its configuration.
    pub async fn configured<T>(owner: &str, configure: impl FnOnce(&Path) -> T) -> (Self, T) {
        let owner_name = owner.to_string();
        let root = tempfile::tempdir().unwrap();
        initialize(root.path(), owner).unwrap();
        LOCKS
            .lock()
            .unwrap()
            .insert(crate::config::Layout::new(root.path()).git_config(), None);
        let configured = configure(root.path());
        let running = Running::start(root.path(), Arc::new(crate::vault::Memory::default()))
            .await
            .unwrap();
        let owner = running.data.owner();
        // A token of the owner's own, as Git over Smart HTTP takes it.
        let owner_token = owner
            .members_issue_token(owner_name.clone(), input(json!({"label": "fixture"})))
            .await
            .unwrap()
            .token;
        (
            Self {
                running,
                owner,
                owner_token,
                root,
            },
            configured,
        )
    }

    pub fn root(&self) -> &Path {
        self.root.path()
    }
    pub fn data(&self) -> DataApi {
        self.running.data.clone()
    }

    pub async fn workspace(&self, name: &str) {
        self.owner
            .repos_create_workspace(input(json!({"name":name})))
            .await
            .unwrap();
    }

    pub async fn repository(&self, path: &str) {
        self.owner
            .repos_create(input(json!({"path":path,"labels":["bug"]})))
            .await
            .unwrap();
    }

    /// An agent named `name`, acted as in-process.
    pub async fn member(&self, name: &str) -> MemberApi {
        self.running.data.agent(name, name).await.unwrap()
    }

    pub async fn ticket(
        &self,
        repo: &str,
        request: Value,
        assignee: Option<&str>,
        lane: &str,
    ) -> String {
        let ticket = self
            .owner
            .tickets_create_ticket(repo, input(request))
            .await
            .unwrap();
        self.owner
            .tickets_set_assignee(repo, &ticket.key, assignee.map(Into::into))
            .await
            .unwrap();
        self.owner
            .tickets_put_on_board(repo, &ticket.key, input(json!({"lane":lane})))
            .await
            .unwrap();
        ticket.url
    }

    /// Make real, distinct Git trees so the pull can also be merged.
    pub fn commit(
        &self,
        repo: &str,
        branch: &str,
        parent: Option<&str>,
        file: &str,
        body: &str,
    ) -> String {
        let git = |args: &[&str]| self.git(repo, args);
        let file_path = self.root.path().join("fixture-content");
        std::fs::write(&file_path, body).unwrap();
        let blob = git(&["hash-object", "-w", file_path.to_str().unwrap()]);
        git(&["read-tree", parent.unwrap_or("--empty")]);
        git(&[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("100644,{blob},{file}"),
        ]);
        let tree = git(&["write-tree"]);
        let mut args = vec!["commit-tree", &tree, "-m", branch];
        if let Some(parent) = parent {
            args.extend(["-p", parent]);
        }
        let sha = git(&args);
        git(&["update-ref", &format!("refs/heads/{branch}"), &sha]);
        sha
    }

    /// Commit the single-job pipeline used by tests that drive checks.
    pub fn commit_check_pipeline(&self, repo: &str, branch: &str, parent: Option<&str>) -> String {
        self.commit(repo, branch, parent, PIPELINE_PATH, CHECK_PIPELINE)
    }

    pub fn git(&self, repo: &str, args: &[&str]) -> String {
        fixture_git(self.root(), repo, args)
    }

    pub async fn pull(&self, actor: &MemberApi, repo: &str, request: Value) -> String {
        actor
            .pulls_create(repo, input(request))
            .await
            .unwrap()
            .pull
            .url
    }

    /// Register and finish a check through the queue's ordinary transitions.
    /// `None` leaves the check queued; no runner process is started.
    pub async fn check(&self, repo: &str, branch: &str, sha: &str, conclusion: Option<&str>) {
        check(&self.data(), self.root(), repo, branch, sha, conclusion).await;
    }

    pub async fn merge(&self, repo: &str, number: u64, head: &str, base: &str) {
        merge(&self.owner, repo, number, head, base).await;
    }

    pub async fn stop(self) {
        self.running.stop().await.unwrap();
    }
}

/// Drive the commit's single CI job on an embedded service without starting a runner.
pub async fn check(
    data: &DataApi,
    root: &Path,
    repo: &str,
    branch: &str,
    sha: &str,
    conclusion: Option<&str>,
) {
    use crate::ci::queue::{self, PipelineState, Registration, ResultInfo};
    let app = &data.app;
    let repository = app
        .store
        .read({
            let repo = repo.to_owned();
            move |r| crate::domain::repo_by_path(r, &repo)
        })
        .await
        .unwrap();
    let pipeline = pipeline::read(
        &app.git,
        &app.git.repo_dir(repo),
        sha,
        &repository.post_merge_jobs,
    )
    .await
    .unwrap();
    assert!(
        matches!(&pipeline, PipelineState::Jobs { jobs } if jobs.len() == 1),
        "fixture commit {sha} must define one job in {PIPELINE_PATH}: {pipeline:?}"
    );
    let registration = Registration {
        order: vec![],
        selection: None,
        repo: repository.id,
        repo_path: repo.into(),
        sha: sha.into(),
        tree: Some(fixture_git(
            root,
            repo,
            &["rev-parse", &format!("{sha}^{{tree}}")],
        )),
        branch: branch.into(),
        protected: branch == "main",
        network: false,
        inputs: Default::default(),
        pipeline,
    };
    let conclusion = conclusion.map(str::to_owned);
    let queue_max = app.ci.config().queue_max;
    let target = (repo.to_owned(), sha.to_owned());
    app.store
        .write(move |w| {
            let now = crate::util::now();
            let repo_id = registration.repo.clone();
            queue::register(w, registration, queue_max, &now)?;
            let run = queue::get_run(w, &repo_id, &target.1)?.unwrap();
            if let PipelineState::Jobs { jobs } = &run.pipeline {
                for attempt in queue::current_attempts(w, &repo_id, &target.1, jobs)? {
                    if attempt.status == "completed" {
                        queue::retry(w, &attempt.id, queue_max, &now)?;
                    }
                }
            }
            if let Some(conclusion) = conclusion {
                let claimed = queue::claim_fixture(w, (&target.0, &target.1), &now)?;
                queue::finish(
                    w,
                    &claimed.attempt.id,
                    &claimed.lease,
                    &conclusion,
                    None,
                    ResultInfo {
                        peak_memory_mib: None,
                        image_digest: Some(format!("sha256:{}", "2".repeat(64))),
                        reused_from: None,
                        cache_key: None,
                    },
                    &now,
                )?;
            }
            Ok(())
        })
        .await
        .unwrap();
}

/// Supply the owner approval and merge on an already embedded service.
pub async fn merge(owner: &MemberApi, repo: &str, number: u64, head: &str, base: &str) {
    owner
        .pulls_review(
            repo,
            number,
            input(json!({"verdict":"approve","commit":head})),
        )
        .await
        .unwrap();
    owner
        .pulls_merge(
            repo,
            number,
            input(json!({"expected_head":head,"expected_base":base})),
        )
        .await
        .unwrap();
}

fn fixture_git(root: &Path, repo: &str, args: &[&str]) -> String {
    let output = std::process::Command::new("git")
        .args(["-c", "credential.helper="])
        .arg("--git-dir")
        .arg(root.join(format!("repos/{repo}.git")))
        .args(args)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", root)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", root.join("empty-config"))
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "fixture Git: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}

/// Called by the downstream test named in `install_test_hooks`.
pub fn hook() {
    let Ok(name) = std::env::var("GUILDBOTICS_TEST_HOOK") else {
        return;
    };
    assert!(["pre-receive", "post-receive"].contains(&name.as_str()));
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    std::process::exit(runtime.block_on(crate::hooks::run_hook(&name)));
}

#[cfg(test)]
mod tests;

/// Collect every wire page while asserting that its continuation advances.
pub async fn ticket_items(
    t: &crate::agent::TicketAgent,
    tracker: &str,
    parameters: &Value,
) -> Vec<Value> {
    let mut items = vec![];
    let mut cursor = String::new();
    loop {
        let p = t
            .read(
                crate::agent::port::Resource::TrackerTickets,
                tracker,
                Some(parameters),
                &cursor,
            )
            .await
            .unwrap();
        assert!(p.target().is_none());
        items.extend_from_slice(p.items());
        match p.continuation() {
            Some(next) => {
                assert_ne!(next, cursor);
                cursor = next.into()
            }
            None => break,
        }
    }
    items
}
