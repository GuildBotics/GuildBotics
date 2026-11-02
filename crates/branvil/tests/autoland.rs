//! Landing pull requests automatically against a real service: a repository
//! with `auto_land` merges (or, with an upstream, publishes) an open pull
//! request that is not a draft once its required approvals and checks are
//! there, publishes one once, tells the webhook of an automatic publish once,
//! and keeps the webhook's URL a secret.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use axum::extract::State;
use axum::http::StatusCode as Status;
use axum::routing::post;
use reqwest::StatusCode;
use serde_json::{Value, json};

use crate::ci::{Runner, eventually, pipeline, sync_main};
use crate::common::github::Refusal;
use crate::common::*;
use crate::pulls::review;
use crate::upstream::{
    GH, branch_on_github, commit_on_github, followed, publish, refresh, with_aiko_account,
};

const OTHER: &str = "acme/other";

/// Changes `acme/demo`'s settings (at its current revision).
async fn settings(w: &World, patch: Value) -> Value {
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let mut body = patch;
    body["revision"] = repo["revision"].clone();
    let (st, v) = w.server.patch(&w.repo_path(), &w.owner, body).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    v
}

async fn pull(w: &World, n: u64) -> Value {
    w.server
        .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
        .await
}

async fn merged(w: &World, n: u64) {
    eventually(&format!("!{n} merged"), || async {
        pull(w, n).await["merged"] == true
    })
    .await;
}

fn number(p: &Value) -> u64 {
    p["number"].as_u64().unwrap()
}

/// Another repository of the workspace and its clone.
struct Other {
    path: &'static str,
    dir: PathBuf,
}

/// The repository `path` with a `main` of its own, needing no approval and
/// landing by itself when `auto_land`.
async fn other_repo(w: &World, path: &'static str, auto_land: bool) -> Other {
    let s = &w.server;
    let (st, v) = s.post("/repos", &w.owner, json!({"path": path})).await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let (st, v) = s
        .patch(
            &format!("/repos/{}", encode(path)),
            &w.owner,
            json!({"revision": 1, "required_approvals": 0, "auto_land": auto_land}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let dir = w.dir.path().join(path.replace('/', "-"));
    std::fs::create_dir_all(&dir).unwrap();
    git_ok(&dir, &["init", "-q", "-b", "main"]);
    std::fs::write(dir.join("README.md"), "other\n").unwrap();
    git_ok(&dir, &["add", "README.md"]);
    git_ok(&dir, &["commit", "-qm", "initial"]);
    // The first push of a protected branch is the owner's.
    let remote = s.remote(path);
    git_as_ok(
        &dir,
        "alice",
        &w.owner,
        &["push", "-q", &remote, "HEAD:refs/heads/main"],
    );
    Other { path, dir }
}

/// Pushes the clone's `HEAD` to `branch` of the repository, at the service's
/// current address.
fn push(w: &World, other: &Other, branch: &str) {
    let to = format!("HEAD:refs/heads/{branch}");
    let remote = w.server.remote(other.path);
    git_as_ok(&other.dir, "aiko", &w.aiko, &["push", "-q", &remote, &to]);
}

/// Commits a file on the clone's `HEAD`, pushes it as `branch`, and opens a
/// pull request of it.
async fn open_in(w: &World, other: &Other, branch: &str) -> Value {
    std::fs::write(other.dir.join(format!("{branch}.txt")), branch).unwrap();
    git_ok(&other.dir, &["add", "."]);
    git_ok(&other.dir, &["commit", "-qm", branch]);
    push(w, other, branch);
    let (st, v) = w
        .server
        .post(
            &format!("/repos/{}/pulls", encode(other.path)),
            &w.aiko,
            json!({"head": branch, "title": branch}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    v["pull"].clone()
}

async fn other_pull(w: &World, other: &Other, n: u64) -> Value {
    let at = format!("/repos/{}/pulls/{n}", encode(other.path));
    w.server.ok(&at, &w.owner).await
}

/// Returns once everything that changed before was swept: a pull request
/// opened in `other` (landing by itself) lands, and one task sweeps the
/// changes in the order they came.
async fn swept(w: &World, other: &Other) {
    let k = git_ok(&other.dir, &["rev-list", "--count", "HEAD"]);
    let n = number(&open_in(w, other, &format!("s{k}")).await);
    eventually("the sweep", || async {
        other_pull(w, other, n).await["merged"] == true
    })
    .await;
}

/// Turns `auto_land` of `path` on in the database of the stopped service.
fn auto_land_on(w: &World, path: &str) {
    edit_records(&w.server.data.join("branvil.redb"), "repos", |r| {
        if r["path"] == path {
            r["auto_land"] = json!(true);
        }
    });
}

/// A Slack-like Incoming Webhook: keeps what it receives (on arrival) and,
/// once let through, answers with `answers` in turn, then 200.
struct Hook {
    received: Arc<Mutex<Vec<(u16, Value)>>>,
    answers: Arc<Mutex<VecDeque<u16>>>,
    gate: Arc<tokio::sync::Semaphore>,
}

type HookState = (
    Arc<Mutex<Vec<(u16, Value)>>>,
    Arc<Mutex<VecDeque<u16>>>,
    Arc<tokio::sync::Semaphore>,
);

async fn hook_receive(
    State((received, answers, gate)): State<HookState>,
    axum::Json(body): axum::Json<Value>,
) -> Status {
    let status = answers.lock().unwrap().pop_front().unwrap_or(200);
    received.lock().unwrap().push((status, body));
    gate.acquire().await.unwrap().forget();
    Status::from_u16(status).unwrap()
}

impl Hook {
    /// The service's webhook, answering at once.
    async fn start(w: &World, answers: &[u16]) -> Hook {
        let hook = Hook::held(w, answers).await;
        hook.release();
        hook
    }

    /// The service's webhook, answering nothing until `release`.
    async fn held(w: &World, answers: &[u16]) -> Hook {
        let received = Arc::new(Mutex::new(Vec::new()));
        let gate = Arc::new(tokio::sync::Semaphore::new(0));
        let answers = Arc::new(Mutex::new(answers.iter().copied().collect()));
        let state: HookState = (received.clone(), answers.clone(), gate.clone());
        let app = axum::Router::new()
            .route("/services/T0/hook-9f3c", post(hook_receive))
            .with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!(
            "http://{}/services/T0/hook-9f3c",
            listener.local_addr().unwrap()
        );
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let (st, v) = w
            .server
            .put("/admin/webhook", &w.owner, json!({"url": url}))
            .await;
        assert_eq!(st, StatusCode::OK, "{v}");
        Hook {
            received,
            answers,
            gate,
        }
    }

    fn release(&self) {
        self.gate.add_permits(1 << 20);
    }

    fn received(&self) -> Vec<(u16, Value)> {
        self.received.lock().unwrap().clone()
    }
}

#[tokio::test]
async fn protected_required_failure_is_sent_on_check_event_and_not_repeated_after_restart() {
    let mut w = World::new().await;
    let hook = Hook::start(&w, &[]).await;
    let runner = Runner::new(&w, "runner1").await;
    crate::ci::land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    settings(&w, json!({"required_checks": ["test"]})).await;
    let barrier = other_repo(&w, OTHER, true).await;
    let mut c = runner.claim().await.unwrap();
    for _ in 0..3 {
        assert_eq!(runner.finish(&c, "failure").await.0, 200);
        swept(&w, &barrier).await;
        assert!(
            hook.received().is_empty(),
            "automatic retry defers final failure notice"
        );
        c = runner.claim().await.unwrap();
    }
    assert_eq!(runner.finish(&c, "failure").await.0, 200);
    eventually("required-check failure notification", || async {
        hook.received().len() == 1
    })
    .await;
    let text = hook.received()[0].1["text"].as_str().unwrap().to_string();
    assert!(text.contains("acme/demo [main]"), "{text}");
    assert!(text.contains(c["sha"].as_str().unwrap()), "{text}");
    assert!(text.contains(c["attempt_id"].as_str().unwrap()), "{text}");
    swept(&w, &barrier).await;
    w.restart(&[]);
    swept(&w, &barrier).await;
    assert_eq!(hook.received().len(), 1);
    w.server.stop();
    let runs = records(&w.server.data.join("branvil.redb"), "ci_runs");
    assert!(runs.iter().find(|r| r["sha"] == c["sha"]).unwrap()["failure_notified_at"].is_string());
}

#[tokio::test]
async fn a_pull_request_merges_itself_once_approved_and_its_required_check_succeeded() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    settings(
        &w,
        json!({"auto_land": true, "required_checks": ["test"], "required_approvals": 1}),
    )
    .await;
    std::fs::create_dir_all(w.work.join(".branvil")).unwrap();
    std::fs::write(w.work.join(".branvil/ci.toml"), pipeline(&["test"])).unwrap();
    git_ok(&w.work, &["add", ".branvil/ci.toml"]);

    // The approval first, then the check.
    let head = w.push_change("one", "one.txt", "1\n");
    let n = number(&w.open_pull("one", json!([])).await);
    let at = format!("{}/pulls/{n}", w.repo_path());
    assert_eq!(review(&w, &at, &w.ken, "approve", &head).await, 201);
    // `test` has not run, so nothing can land.
    assert_eq!(pull(&w, n).await["state"], "open");
    assert!(runner.drain("success").await >= 1);
    merged(&w, n).await;
    assert_eq!(pull(&w, n).await["merge_sha"], head.as_str());

    // The check first, then the approval.
    sync_main(&w);
    let head = w.push_change("two", "two.txt", "2\n");
    let n = number(&w.open_pull("two", json!([])).await);
    let at = format!("{}/pulls/{n}", w.repo_path());
    assert!(runner.drain("success").await >= 1);
    // 0 of 1 approvals, so nothing can land.
    assert_eq!(pull(&w, n).await["state"], "open");
    assert_eq!(review(&w, &at, &w.ken, "approve", &head).await, 201);
    merged(&w, n).await;
}

#[tokio::test]
async fn a_draft_lands_only_once_it_is_marked_ready() {
    let w = World::new().await;
    let other = other_repo(&w, OTHER, true).await;
    settings(&w, json!({"auto_land": true})).await;
    w.push_change("draft", "d.txt", "d\n");
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head": "draft", "title": "Draft", "draft": true}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let n = number(&v["pull"]);
    swept(&w, &other).await;
    assert_eq!(pull(&w, n).await["state"], "open");
    let (st, v) = w
        .server
        .patch(
            &format!("{}/pulls/{n}", w.repo_path()),
            &w.owner,
            json!({"draft": false}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    merged(&w, n).await;
}

#[tokio::test]
async fn only_the_owner_turns_landing_on_and_it_lands_what_waits_even_at_start() {
    let mut w = World::new().await;
    let other = other_repo(&w, OTHER, true).await;
    // Off: a pull request that can land stays.
    let n = {
        w.push_change("one", "one.txt", "1\n");
        number(&w.open_pull("one", json!([])).await)
    };
    swept(&w, &other).await;
    assert_eq!(pull(&w, n).await["state"], "open");
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let (st, _) = w
        .server
        .patch(
            &w.repo_path(),
            &w.aiko,
            json!({"revision": repo["revision"], "auto_land": true}),
        )
        .await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    // On: it lands.
    assert_eq!(
        settings(&w, json!({"auto_land": true})).await["auto_land"],
        true
    );
    merged(&w, n).await;

    // A pull request that can land while the service is down lands at start.
    settings(&w, json!({"auto_land": false})).await;
    sync_main(&w);
    w.push_change("two", "two.txt", "2\n");
    let n = number(&w.open_pull("two", json!([])).await);
    w.server.stop();
    auto_land_on(&w, REPO);
    w.restart(&[]);
    merged(&w, n).await;
}

#[tokio::test]
async fn a_head_behind_its_base_lands_through_a_gate_without_changing_its_head() {
    let w = World::new().await;
    settings(&w, json!({"auto_land": true})).await;
    let head = w.push_change("b", "b.txt", "b\n");
    git_ok(&w.work, &["checkout", "-q", "main"]);
    let base = w.push_change("a", "a.txt", "a\n");
    merged(&w, number(&w.open_pull("a", json!([])).await)).await;
    let n = number(&w.open_pull("b", json!([])).await);
    merged(&w, n).await;
    let p = pull(&w, n).await;
    assert_eq!(p["head_sha"], head);
    assert_ne!(p["merge_sha"], head);
    assert_eq!(p["landing"]["gate"], p["merge_sha"]);
    git_as_ok(&w.work, "alice", &w.owner, &["fetch", "-q", "origin"]);
    assert_eq!(
        git_ok(&w.work, &["rev-parse", "origin/main^1"]).trim(),
        base
    );
    assert_eq!(
        git_ok(&w.work, &["rev-parse", "origin/main^2"]).trim(),
        head
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pull_request_publishes_itself_as_its_author_and_the_webhook_is_told_once() {
    let (w, gh) = followed().await;
    let hook = Hook::start(&w, &[]).await;
    let other = other_repo(&w, OTHER, true).await;
    settings(&w, json!({"auto_land": true})).await;
    let head = w.push_change("feat", "f.txt", "one\n");
    let n = number(&w.open_pull("feat", json!([])).await);
    // Its author has no GitHub account yet; once it has, it is published.
    swept(&w, &other).await;
    assert!(pull(&w, n).await["upstream"].is_null());
    with_aiko_account(&w).await;
    eventually("the publish", || async {
        pull(&w, n).await["upstream"]["number"].is_u64()
    })
    .await;
    let p = pull(&w, n).await;
    let x = &p["upstream"];
    assert_eq!((&x["auto"], &x["account"]), (&json!(true), &json!("aiko")));
    let there = gh.pull(x["number"].as_u64().unwrap());
    assert_eq!(there.author, "aiko-bot");
    assert_eq!(gh.tip(GH, "feat").as_deref(), Some(head.as_str()));
    eventually("the notice", || async {
        pull(&w, n).await["landing"]["notified_at"].is_string()
    })
    .await;
    let received = hook.received();
    assert_eq!(received.len(), 1, "{received:?}");
    let text = received[0].1["text"].as_str().unwrap();
    for part in [
        p["url"].as_str().unwrap(),
        x["url"].as_str().unwrap(),
        "PR feat",
    ] {
        assert!(text.contains(part), "{text}");
    }

    // A new head is sent immediately and its next gate pass is notified again.
    w.push_change("feat", "f.txt", "two\n");
    refresh(&w).await;
    swept(&w, &other).await;
    eventually("the new gate notice", || async {
        hook.received().len() == 2
    })
    .await;
    assert_eq!(pull(&w, n).await["state"], "open", "people merge it there");
}

/// Polls the host, sweeps, and checks that pull request `n` still waits for
/// its checks there, untold.
async fn waits_for_host_checks(w: &World, other: &Other, n: u64, what: &str) {
    refresh(w).await;
    swept(w, other).await;
    let p = pull(w, n).await;
    assert_eq!(p["landing"]["state"], "passed", "{what}: {p}");
    assert_eq!(p["landing"]["first"]["next"], "host_checks", "{what}: {p}");
    assert!(p["landing"]["notified_at"].is_null(), "{what}: {p}");
}

/// The names of pull request `n`'s checks.
async fn check_names(w: &World, n: u64) -> Vec<Value> {
    let checks = w
        .server
        .ok(&format!("{}/pulls/{n}/checks", w.repo_path()), &w.aiko)
        .await;
    checks["checks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["name"].clone())
        .collect()
}

/// Ends the time the host was given to register checks, as if it passed
/// (the service is restarted to change its record).
fn registration_over(w: &mut World) {
    w.server.stop();
    edit_records(&w.server.data.join("branvil.redb"), "pulls", |p| {
        if p["upstream"]["checks_registered_by"].is_string() {
            p["upstream"]["checks_registered_by"] = json!("2000-01-01T00:00:00.000Z");
        }
    });
    w.restart(&[]);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_registration_deadline_polls_without_waiting_for_the_regular_interval() {
    let (mut w, gh) = followed().await;
    // Both regular intervals are an hour. Only the registration deadline
    // can poll the already-finished checks in time to send this notice.
    let config = w.server.data.join("branvil.toml");
    let text = std::fs::read_to_string(&config).unwrap();
    std::fs::write(
        &config,
        text.replace(
            "checks_registration_secs = 0",
            "checks_registration_secs = 5",
        ),
    )
    .unwrap();
    w.restart(&[]);
    let hook = Hook::start(&w, &[]).await;
    with_aiko_account(&w).await;
    settings(&w, json!({"auto_land": true})).await;
    let mut n = None;
    for (i, content) in ["one\n", "two\n"].iter().enumerate() {
        let event = std::time::Instant::now();
        let head = w.push_change("feat", "f.txt", content);
        gh.check(&head, 70, "fast", "success", "ok\n");
        gh.workflow_run(&head, 1, "completed", Some("success"));
        let number = match n {
            Some(n) => n,
            None => {
                let created = number(&w.open_pull("feat", json!([])).await);
                n = Some(created);
                created
            }
        };
        eventually("the head's publish", || async {
            let p = pull(&w, number).await;
            p["upstream"]["pushed"] == head && p["upstream"]["number"].is_u64()
        })
        .await;
        assert!(check_names(&w, number).await.contains(&json!("github")));
        assert_eq!(hook.received().len(), i);
        tokio::time::timeout(std::time::Duration::from_secs(10), async {
            eventually("the deadline's notice", || async {
                let received = hook.received().len();
                if event.elapsed() < std::time::Duration::from_secs(5) {
                    assert_eq!(received, i, "not before registration ends");
                }
                received == i + 1
            })
            .await;
        })
        .await
        .expect("the deadline must not wait for the regular poll");
        let p = pull(&w, number).await;
        assert_eq!(p["landing"]["first"]["next"], "owner_merge_host", "{p}");
        assert!(!check_names(&w, number).await.contains(&json!("github")));
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn the_webhook_is_told_only_once_every_host_check_of_the_head_succeeded() {
    let (mut w, gh) = followed().await;
    // GitHub takes its time to register the checks of a new head.
    let config = w.server.data.join("branvil.toml");
    let text = std::fs::read_to_string(&config).unwrap();
    let text = text.replace(
        "checks_registration_secs = 0",
        "checks_registration_secs = 3600",
    );
    std::fs::write(&config, text).unwrap();
    w.restart(&[]);
    let hook = Hook::start(&w, &[]).await;
    let other = other_repo(&w, OTHER, true).await;
    with_aiko_account(&w).await;
    settings(&w, json!({"auto_land": true})).await;
    let head = w.push_change("feat", "f.txt", "one\n");
    // A check of the push is there before the pull request's own checks; an
    // app that never reports leaves a suite without any.
    gh.check(&head, 70, "push-fast", "success", "ok\n");
    gh.suite(&head, 1, "completed", 1);
    gh.suite(&head, 9, "queued", 0);
    let n = number(&w.open_pull("feat", json!([])).await);
    eventually("the publish", || async {
        pull(&w, n).await["upstream"]["number"].is_u64()
    })
    .await;
    waits_for_host_checks(&w, &other, n, "more may be registered").await;
    assert_eq!(
        check_names(&w, n).await,
        [json!("github/push-fast"), json!("github")]
    );
    // The pull request's checks: one job runs, another waits for it.
    gh.running(&head, 71, "pr-slow");
    gh.suite(&head, 2, "in_progress", 1);
    waits_for_host_checks(&w, &other, n, "running").await;
    registration_over(&mut w);
    // A read that fails decides nothing.
    gh.set(|st| {
        st.refusal = Some(Refusal {
            status: 502,
            headers: vec![],
            message: "Server Error",
        })
    });
    waits_for_host_checks(&w, &other, n, "a failed read").await;
    assert!(pull(&w, n).await["upstream"]["error"].is_string());
    gh.set(|st| st.refusal = None);
    gh.check(&head, 71, "pr-slow", "success", "ok\n");
    waits_for_host_checks(&w, &other, n, "a job still to start").await;
    assert!(hook.received().is_empty());
    gh.check(&head, 72, "pr-after", "success", "ok\n");
    gh.suite(&head, 2, "completed", 2);
    // A workflow waits in its concurrency group: no job, no check run yet.
    gh.workflow_run(&head, 3, "pending", None);
    gh.suite(&head, 3, "queued", 0);
    waits_for_host_checks(&w, &other, n, "a workflow still to start").await;
    gh.workflow_run(&head, 3, "in_progress", None);
    gh.suite(&head, 3, "in_progress", 1);
    gh.running(&head, 74, "waited");
    waits_for_host_checks(&w, &other, n, "the workflow started").await;
    assert!(hook.received().is_empty());
    gh.check(&head, 74, "waited", "success", "ok\n");
    gh.suite(&head, 3, "completed", 1);
    gh.workflow_run(&head, 3, "completed", Some("success"));
    refresh(&w).await;
    eventually("the notice", || async {
        pull(&w, n).await["landing"]["notified_at"].is_string()
    })
    .await;
    let received = hook.received();
    assert_eq!(received.len(), 1, "{received:?}");
    let text = received[0].1["text"].as_str().unwrap();
    assert!(
        text.contains("passed its landing gate and is ready to merge on GitHub"),
        "{text}"
    );
    let p = pull(&w, n).await;
    assert_eq!(p["landing"]["first"]["next"], "owner_merge_host");
    assert!(!check_names(&w, n).await.contains(&json!("github")));

    // An edit sent to the host runs the host's checks of edits.
    let p = pull(&w, n).await;
    let (st, v) = w
        .server
        .patch(
            &format!("{}/pulls/{n}", w.repo_path()),
            &w.aiko,
            json!({"title": "PR feat, edited", "revision": p["revision"]}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    refresh(&w).await;
    let p = pull(&w, n).await;
    assert_eq!(p["landing"]["first"]["next"], "host_checks", "{p}");
    assert_eq!(
        gh.pull(p["upstream"]["number"].as_u64().unwrap()).title,
        "PR feat, edited"
    );

    // A new head the poller sends waits for its own checks again.
    let next = w.push_change("feat", "f.txt", "two\n");
    waits_for_host_checks(&w, &other, n, "new head").await;
    assert_eq!(pull(&w, n).await["upstream"]["pushed"], next);
    gh.check(&next, 73, "push-fast", "success", "ok\n");
    registration_over(&mut w);
    refresh(&w).await;
    eventually("the new head's notice", || async {
        hook.received().len() == 2
    })
    .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn an_automatic_publish_the_host_refuses_is_left_to_the_owner() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let other = other_repo(&w, OTHER, true).await;
    // GitHub has a branch feat that Branvil did not push.
    let main = gh.tip(GH, "main").unwrap();
    w.push_change("feat", "f.txt", "one\n");
    let theirs = commit_on_github(&gh, "main", "someone's work");
    branch_on_github(&gh, "feat", &theirs);
    branch_on_github(&gh, "main", &main);
    settings(&w, json!({"auto_land": true})).await;
    let n = number(&w.open_pull("feat", json!([])).await);
    eventually("the refusal", || async {
        pull(&w, n).await["upstream"]["stopped"].is_string()
    })
    .await;
    assert_eq!(pull(&w, n).await["upstream"]["auto"], true);
    assert_eq!(gh.tip(GH, "feat").as_deref(), Some(theirs.as_str()));
    assert!(gh.pulls(GH).is_empty());

    // Nothing automatic replaces it, whatever comes later.
    let ours = w.push_change("feat", "f.txt", "two\n");
    refresh(&w).await;
    swept(&w, &other).await;
    assert!(pull(&w, n).await["upstream"]["stopped"].is_string());
    assert_eq!(gh.tip(GH, "feat").as_deref(), Some(theirs.as_str()));
    assert!(gh.pulls(GH).is_empty());

    // The owner first resumes the gate, then explicitly replaces the host branch.
    let p = pull(&w, n).await;
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/landing/resume", w.repo_path()),
            &w.owner,
            json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["upstream"]["auto"], false);
    assert_eq!(gh.tip(GH, "feat").as_deref(), Some(ours.as_str()));
    assert_eq!(gh.pulls(GH).len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failed_notice_is_sent_again_and_a_manual_publish_tells_nobody() {
    let (w, _gh) = followed().await;
    with_aiko_account(&w).await;
    let hook = Hook::start(&w, &[500; 64]).await;
    let other = other_repo(&w, OTHER, true).await;
    settings(&w, json!({"auto_land": true})).await;
    w.push_change("feat", "f.txt", "one\n");
    let n = number(&w.open_pull("feat", json!([])).await);
    eventually("the failure", || async {
        w.server.ok("/admin/webhook", &w.owner).await["problem"].is_string()
    })
    .await;
    let problem = w.server.ok("/admin/webhook", &w.owner).await["problem"].clone();
    assert!(problem.as_str().unwrap().contains("500"), "{problem}");
    assert!(!problem.to_string().contains("hook-9f3c"), "{problem}");
    assert!(pull(&w, n).await["landing"]["notified_at"].is_null());
    hook.answers.lock().unwrap().clear();
    // The next change of the repository (a notice, which awaits no answer)
    // sends it again.
    let (st, _) = w
        .server
        .post(
            &format!("{}/pulls/{n}/comments", w.repo_path()),
            &w.ken,
            json!({"body": "looks good", "meta": {"kind": "notice"}}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED);
    eventually("the notice", || async {
        pull(&w, n).await["landing"]["notified_at"].is_string()
    })
    .await;
    assert!(w.server.ok("/admin/webhook", &w.owner).await["problem"].is_null());
    let received = hook.received();
    assert_eq!(received.last().unwrap().0, 200);
    assert!(received[..received.len() - 1].iter().all(|r| r.0 == 500));
    assert!(received.iter().all(|r| r.1 == received[0].1));
    let sent = received.len();

    // The owner's publish tells nobody. Release the current queue leader first.
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/close", w.repo_path()),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    settings(&w, json!({"auto_land": false})).await;
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("two", "two.txt", "2\n");
    let m = number(&w.open_pull("two", json!([])).await);
    let (st, v) = publish(&w, m).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["upstream"]["auto"], false);
    swept(&w, &other).await;
    assert_eq!(hook.received().len(), sent);
    assert!(pull(&w, m).await["landing"]["notified_at"].is_null());
}

#[tokio::test]
async fn the_webhook_url_is_a_secret_shown_only_by_its_host() {
    let w = World::new().await;
    let s = &w.server;
    let unset = json!({"set": false, "host": null, "problem": null});
    assert_eq!(s.ok("/admin/webhook", &w.owner).await, unset);
    let secret = "hook-2b8d";
    let url = format!("https://hooks.example.test/services/T0/{secret}");
    let (st, _) = s.get("/admin/webhook", &w.aiko).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (st, _) = s.put("/admin/webhook", &w.aiko, json!({"url": url})).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (st, _) = s.delete("/admin/webhook", &w.aiko).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    for bad in ["ftp://hooks.example.test/x", "not a url"] {
        let (st, v) = s.put("/admin/webhook", &w.owner, json!({"url": bad})).await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
        assert!(!v.to_string().contains(bad), "{v}");
    }
    let set = json!({"set": true, "host": "hooks.example.test", "problem": null});
    let (st, v) = s.put("/admin/webhook", &w.owner, json!({"url": url})).await;
    assert_eq!((st, &v), (StatusCode::OK, &set));
    assert_eq!(s.ok("/admin/webhook", &w.owner).await, set);
    let file = s.data.join("secrets/webhook.url");
    assert_eq!(std::fs::read_to_string(&file).unwrap(), url);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&file).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }
    let (st, v) = s.delete("/admin/webhook", &w.owner).await;
    assert_eq!((st, &v), (StatusCode::OK, &unset));
    assert!(!file.exists());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_webhook_set_later_is_told_of_an_automatic_publish_whatever_auto_land() {
    let (w, _gh) = followed().await;
    with_aiko_account(&w).await;
    settings(&w, json!({"auto_land": true})).await;
    w.push_change("feat", "f.txt", "one\n");
    let n = number(&w.open_pull("feat", json!([])).await);
    eventually("the publish", || async {
        pull(&w, n).await["upstream"]["number"].is_u64()
    })
    .await;
    // No webhook then; it is set after the repository stopped landing.
    settings(&w, json!({"auto_land": false})).await;
    let hook = Hook::start(&w, &[]).await;
    let (st, _) = w
        .server
        .post(
            &format!("{}/pulls/{n}/comments", w.repo_path()),
            &w.ken,
            json!({"body": "waiting for GitHub", "meta": {"kind": "notice"}}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED);
    eventually("the notice", || async {
        pull(&w, n).await["landing"]["notified_at"].is_string()
    })
    .await;
    assert_eq!(hook.received().len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_changed_upstream_never_notifies_the_old_publication_or_republishes_automatically() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    gh.repository("octo/other", &w.work);
    let other = other_repo(&w, OTHER, true).await;
    settings(&w, json!({"auto_land": true})).await;
    w.push_change("feat", "f.txt", "one\n");
    let n = number(&w.open_pull("feat", json!([])).await);
    eventually("the automatic publish without a webhook", || async {
        pull(&w, n).await["upstream"]["number"].is_u64()
    })
    .await;
    let original = pull(&w, n).await;
    assert_eq!(original["upstream"]["auto"], true);
    assert!(original["landing"]["notified_at"].is_null());
    let old_url = original["upstream"]["url"].as_str().unwrap();

    let (st, result) = w
        .server
        .put(
            &format!("{}/upstream", w.repo_path()),
            &w.owner,
            json!({"provider":"github","repository":"octo/other"}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{result}");
    let hook = Hook::start(&w, &[]).await;
    let (st, result) = w
        .server
        .post(
            &format!("{}/pulls/{n}/comments", w.repo_path()),
            &w.ken,
            json!({"body":"The owner will publish to the new destination.", "meta": {"kind": "notice"}}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{result}");
    swept(&w, &other).await;
    let retained = pull(&w, n).await;
    assert_eq!(retained["landing"]["state"], "passed");
    assert_eq!(retained["landing"]["first"]["next"], "owner_publish");
    assert_eq!(retained["upstream"]["repository"], GH);
    assert!(hook.received().is_empty(), "{:?}", hook.received());
    assert!(retained["landing"]["notified_at"].is_null());
    assert!(gh.pulls("octo/other").is_empty());
    assert!(gh.tip("octo/other", "feat").is_none());

    // The pending automatic gate notice can describe only the publication
    // that the owner explicitly made at the new destination.
    let (st, published) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{published}");
    assert_eq!(published["upstream"]["repository"], "octo/other");
    assert_eq!(published["upstream"]["auto"], false);
    eventually("the new destination's notice", || async {
        hook.received().len() == 1
    })
    .await;
    let notices = hook.received();
    let text = notices[0].1["text"].as_str().unwrap();
    assert!(text.contains(published["upstream"]["url"].as_str().unwrap()));
    assert!(!text.contains(old_url), "{text}");
    assert_eq!(gh.pulls("octo/other").len(), 1);
}

/// A service started again (now) whose `acme/demo` has a pull request
/// Branvil published by itself while no webhook was set, and whose
/// `acme/other` has a pull request that can land, landing by itself from this
/// start; the webhook holds every answer. The start sweeps `acme/demo`
/// (created first), then `acme/other`. Returns that pull request's number.
async fn notices_before_a_landing() -> (World, Hook, Other, u64) {
    let (mut w, _gh) = followed().await;
    with_aiko_account(&w).await;
    settings(&w, json!({"auto_land": true})).await;
    for branch in ["one"] {
        git_ok(&w.work, &["checkout", "-q", "main"]);
        w.push_change(branch, &format!("{branch}.txt"), "x\n");
        let n = number(&w.open_pull(branch, json!([])).await);
        eventually("the publish", || async {
            pull(&w, n).await["upstream"]["number"].is_u64()
        })
        .await;
    }
    let other = other_repo(&w, OTHER, false).await;
    let fix = number(&open_in(&w, &other, "fix").await);
    let hook = Hook::held(&w, &[]).await;
    w.server.stop();
    auto_land_on(&w, OTHER);
    w.restart(&[]);
    (w, hook, other, fix)
}

#[tokio::test(flavor = "multi_thread")]
async fn a_repository_switched_off_while_a_sweep_walks_lands_nothing() {
    let (w, hook, other, fix) = notices_before_a_landing().await;
    // The sweep waits at acme/demo's first notice; acme/other is switched off.
    eventually("the notice", || async { hook.received().len() == 1 }).await;
    let at = format!("/repos/{}", encode(OTHER));
    let repo = w.server.ok(&at, &w.owner).await;
    let (st, v) = w
        .server
        .patch(
            &at,
            &w.owner,
            json!({"revision": repo["revision"], "auto_land": false}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    hook.release();
    let sentinel = other_repo(&w, "acme/zzz", true).await;
    swept(&w, &sentinel).await;
    assert_eq!(other_pull(&w, &other, fix).await["state"], "open");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_webhook_that_hangs_holds_landing_in_another_repository_up_once_at_most() {
    let (w, hook, other, fix) = notices_before_a_landing().await;
    let started = std::time::Instant::now();
    eventually("the landing", || async {
        other_pull(&w, &other, fix).await["merged"] == true
    })
    .await;
    // One send that timed out (10 s), and no other in that sweep: every
    // sweep tries the first notice only.
    let waited = started.elapsed();
    assert!(waited < std::time::Duration::from_secs(20), "{waited:?}");
    let received = hook.received();
    assert!(
        received
            .iter()
            .all(|(_, b)| b["text"].as_str().unwrap().starts_with("PR one ")),
        "{received:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_automatic_missing_credential_is_persisted_and_clears_after_repair() {
    let (mut w, _gh) = followed().await;
    with_aiko_account(&w).await;
    w.push_change("feature", "f.txt", "feature");
    let n = number(&w.open_pull("feature", json!([])).await);
    eventually("gate passed", || async {
        pull(&w, n).await["landing"]["state"] == "passed"
    })
    .await;
    // Model restoring a backup: account metadata survives, credentials do not.
    w.server.stop();
    std::fs::remove_file(w.server.data.join("secrets/accounts/aiko.github.json")).unwrap();
    auto_land_on(&w, REPO);
    w.restart(&[]);
    eventually("stored automatic failure", || async {
        pull(&w, n).await["landing"]["first"]["next"] == "owner_fix_auto"
    })
    .await;
    let failed = pull(&w, n).await;
    assert!(
        failed["landing"]["first"]["next_detail"]
            .as_str()
            .unwrap()
            .contains("no_account")
    );
    w.server.stop();
    let entry = records(&w.server.data.join("branvil.redb"), "landing")
        .into_iter()
        .find(|e| e["number"] == n)
        .unwrap();
    assert_eq!(entry["error"], failed["landing"]["error"]);
    w.restart(&[]);
    assert_eq!(
        pull(&w, n).await["landing"]["first"]["next"],
        "owner_fix_auto"
    );
    with_aiko_account(&w).await;
    eventually("repaired publication", || async {
        pull(&w, n).await["landing"]["first"]["next"] == "owner_merge_host"
    })
    .await;
    assert!(pull(&w, n).await["landing"]["error"].is_null());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_statement_on_a_published_pull_request_holds_its_notice_until_answered() {
    let (mut w, _gh) = followed().await;
    // The host takes its time to register the checks of the head.
    let config = w.server.data.join("branvil.toml");
    let text = std::fs::read_to_string(&config).unwrap();
    std::fs::write(
        &config,
        text.replace(
            "checks_registration_secs = 0",
            "checks_registration_secs = 3600",
        ),
    )
    .unwrap();
    w.restart(&[]);
    let hook = Hook::start(&w, &[]).await;
    let other = other_repo(&w, OTHER, true).await;
    with_aiko_account(&w).await;
    settings(&w, json!({"auto_land": true})).await;
    w.push_change("feat", "f.txt", "one\n");
    let n = number(&w.open_pull("feat", json!([])).await);
    eventually("the publish", || async {
        pull(&w, n).await["upstream"]["number"].is_u64()
    })
    .await;
    waits_for_host_checks(&w, &other, n, "registering").await;
    let (st, said) = w
        .server
        .post(
            &format!("{}/pulls/{n}/comments", w.repo_path()),
            &w.owner,
            json!({"body": "Please also cover the empty case."}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{said}");
    eventually("the departure", || async {
        pull(&w, n).await["landing"]["reason"]["code"] == "unanswered"
    })
    .await;
    // The host's checks are over: still no notice while the author has not
    // answered.
    registration_over(&mut w);
    refresh(&w).await;
    swept(&w, &other).await;
    let p = pull(&w, n).await;
    assert_eq!(p["landing"]["state"], "left", "{p}");
    assert!(hook.received().is_empty(), "{:?}", hook.received());
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/comments", w.repo_path()),
            &w.aiko,
            json!({"body": "Covered."}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    eventually("the notice", || async {
        pull(&w, n).await["landing"]["notified_at"].is_string()
    })
    .await;
    assert_eq!(hook.received().len(), 1);
}
