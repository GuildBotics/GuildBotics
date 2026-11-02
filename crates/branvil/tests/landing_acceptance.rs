//! The landing contract across host polling, reviews, and CI adoption.

use crate::ci::{Runner, pipeline, sync_main};
use crate::common::*;
use crate::upstream::{GH, commit_on_github, followed, publish, refresh, with_aiko_account};
use serde_json::{Value, json};
use std::time::{Duration, Instant};

async fn pull(w: &World, number: u64) -> Value {
    w.server
        .ok(&format!("{}/pulls/{number}", w.repo_path()), &w.owner)
        .await
}

async fn landing(w: &World, number: u64, state: &str) -> Value {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let p = pull(w, number).await;
        if p["landing"]["state"] == state {
            return p;
        }
        assert!(Instant::now() < deadline, "expected {state}: {p}");
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
}

async fn settings(w: &World, fields: Value) {
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let mut fields = fields.as_object().unwrap().clone();
    fields.insert("revision".into(), repo["revision"].clone());
    let (st, result) = w
        .server
        .patch(&w.repo_path(), &w.owner, Value::Object(fields))
        .await;
    assert_eq!(st, 200, "{result}");
}

async fn merge(w: &World, number: u64) -> Value {
    let p = landing(w, number, "passed").await;
    let (st, result) = w
        .server
        .post(
            &format!("{}/pulls/{number}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head":p["head_sha"],"expected_base":p["base_sha"]}),
        )
        .await;
    assert_eq!(st, 200, "{result}");
    result
}

async fn host_queue() -> (World, crate::common::github::FakeGitHub, Value, Value) {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    w.push_change("first", "first.txt", "first\n");
    w.open_pull("first", json!([])).await;
    let first = landing(&w, 1, "passed").await;
    let (st, published) = publish(&w, 1).await;
    assert_eq!(st, 200, "{published}");
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("second", "second.txt", "second\n");
    w.open_pull("second", json!([])).await;
    let second = landing(&w, 2, "waiting").await;
    (w, gh, first, second)
}

#[tokio::test(flavor = "multi_thread")]
async fn a_host_merge_keeps_the_passed_gate_until_the_host_merge_record_arrives() {
    let (w, gh, first, second) = host_queue().await;
    let published = pull(&w, 1).await;
    let number = published["upstream"]["number"].as_u64().unwrap();
    let pause = gh.pause_pull_read(number);
    let merged = gh.squash_merge(GH, number);
    let refresh = refresh(&w);
    tokio::pin!(refresh);
    tokio::select! {
        _ = pause.reached() => {},
        _ = &mut refresh => panic!("the host pull read was not paused"),
        _ = tokio::time::sleep(Duration::from_secs(15)) => panic!("the host pull read was not reached"),
    }
    // A newly admitted third pull proves reconciliation ran after main moved,
    // while the first pull still has the old, open host record.
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("third", "third.txt", "third\n");
    w.open_pull("third", json!([])).await;
    landing(&w, 3, "waiting").await;
    let waiting_for_host = pull(&w, 1).await;
    assert_eq!(waiting_for_host["base_sha"], merged);
    assert_eq!(waiting_for_host["merged"], false);
    for field in ["state", "gate", "base_sha", "sequence", "queued_at"] {
        assert_eq!(waiting_for_host["landing"][field], first["landing"][field]);
    }
    assert_eq!(
        pull(&w, 2).await["landing"]["sequence"],
        second["landing"]["sequence"]
    );
    drop(pause);
    refresh.await;
    let completed = pull(&w, 1).await;
    assert_eq!(completed["merged"], true);
    assert_eq!(completed["merge_sha"], merged);
    assert_eq!(completed["head_sha"], first["head_sha"]);
    let next = landing(&w, 2, "passed").await;
    assert_eq!(next["landing"]["base_sha"], merged);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_unrelated_host_merge_rebuilds_the_leader_gate_in_its_original_position() {
    let (w, gh, first, second) = host_queue().await;
    let base = commit_on_github(&gh, "main", "another pull landed");
    refresh(&w).await;
    let deadline = Instant::now() + Duration::from_secs(15);
    let rebuilt = loop {
        let p = pull(&w, 1).await;
        if p["landing"]["state"] == "passed" && p["landing"]["base_sha"] == base {
            break p;
        }
        assert!(
            Instant::now() < deadline,
            "leader gate did not rebuild: {p}"
        );
        tokio::time::sleep(Duration::from_millis(30)).await;
    };
    assert_ne!(rebuilt["landing"]["gate"], first["landing"]["gate"]);
    for field in ["position", "sequence", "queued_at"] {
        assert_eq!(rebuilt["landing"][field], first["landing"][field]);
        assert_eq!(
            pull(&w, 2).await["landing"][field],
            second["landing"][field]
        );
    }
    assert_eq!(rebuilt["head_sha"], first["head_sha"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_published_heads_failed_host_check_releases_the_next_gate() {
    let (w, gh, first, _) = host_queue().await;
    gh.check(
        first["head_sha"].as_str().unwrap(),
        441,
        "platform",
        "failure",
        "failed on host\n",
    );
    refresh(&w).await;
    let failed = landing(&w, 1, "left").await;
    assert_eq!(failed["landing"]["reason"]["code"], "host_failed");
    assert_eq!(failed["landing"]["reason"]["jobs"], json!(["platform"]));
    // The author is told on the pull request, with the failed check's link.
    let comments = w
        .server
        .ok(&format!("{}/pulls/1/comments", w.repo_path()), &w.owner)
        .await;
    assert_eq!(comments["items"].as_array().unwrap().len(), 1, "{comments}");
    assert_eq!(comments["items"][0]["author"], "branvil");
    let body = comments["items"][0]["body"].as_str().unwrap();
    for value in [
        "Checks on GitHub failed.",
        first["head_sha"].as_str().unwrap(),
        "- platform: https://github.test/runs/441",
    ] {
        assert!(body.contains(value), "{body}");
    }
    let next = landing(&w, 2, "passed").await;
    assert_eq!(next["landing"]["position"], 1);
    assert_eq!(next["landing"]["first"]["number"], 2);
    refresh(&w).await;
    assert_eq!(pull(&w, 1).await["landing"]["state"], "left");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_host_that_never_opened_the_pull_does_not_hold_an_already_integrated_gate() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let head = w.push_change("feature", "feature.txt", "feature\n");
    w.open_pull("feature", json!([])).await;
    landing(&w, 1, "passed").await;
    gh.set(|st| st.refuse_create = true);
    let (st, result) = publish(&w, 1).await;
    assert_eq!(st, 503, "{result}");
    let unpublished = pull(&w, 1).await;
    assert_eq!(unpublished["upstream"]["pushed"], head);
    assert!(unpublished["upstream"]["number"].is_null());
    assert_eq!(unpublished["landing"]["state"], "passed");
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("following", "following.txt", "following\n");
    w.open_pull("following", json!([])).await;
    landing(&w, 2, "waiting").await;

    // Another integration puts the same tree on main, without merging the
    // original head and without a host pull record that could ever confirm it.
    let dir = gh.dir(GH);
    let base = gh.tip(GH, "main").unwrap();
    let tree = git_ok(&dir, &["rev-parse", &format!("{head}^{{tree}}")]);
    let integrated = git_ok(
        &dir,
        &[
            "commit-tree",
            &tree,
            "-p",
            &base,
            "-m",
            "another integration",
        ],
    );
    git_ok(&dir, &["update-ref", "refs/heads/main", &integrated, &base]);
    refresh(&w).await;
    let left = landing(&w, 1, "left").await;
    assert_eq!(left["landing"]["reason"]["code"], "already_in_base");
    assert_eq!(left["landing"]["base_sha"], integrated);
    assert_eq!(left["merged"], false);
    assert!(gh.pulls(GH).is_empty());
    let next = landing(&w, 2, "passed").await;
    assert_eq!(next["landing"]["position"], 1);
    assert_eq!(next["landing"]["base_sha"], integrated);
}

#[tokio::test]
async fn an_approval_of_the_original_head_still_lands_after_the_base_advances() {
    let w = World::new().await;
    settings(&w, json!({"required_approvals":1})).await;
    let first_head = w.push_change("first", "first.txt", "first\n");
    w.open_pull("first", json!([])).await;
    git_ok(&w.work, &["checkout", "-q", "main"]);
    let second_head = w.push_change("second", "second.txt", "second\n");
    w.open_pull("second", json!([])).await;
    for (number, head) in [(1, &first_head), (2, &second_head)] {
        assert_eq!(pull(&w, number).await["approvals"], 0);
        let (st, result) = w
            .server
            .post(
                &format!("{}/pulls/{number}/reviews", w.repo_path()),
                &w.ken,
                json!({"verdict":"approve","commit":head}),
            )
            .await;
        assert_eq!(st, 201, "{result}");
    }
    let reviewed = landing(&w, 2, "waiting").await;
    assert_eq!(reviewed["approvals"], 1);
    merge(&w, 1).await;
    let gated = landing(&w, 2, "passed").await;
    assert_ne!(gated["landing"]["gate"], second_head);
    assert_eq!(gated["head_sha"], second_head);
    assert_eq!(gated["approvals"], 1);
    assert_eq!(gated["review_states"], reviewed["review_states"]);
    let merged = merge(&w, 2).await;
    assert_eq!(merged["head_sha"], second_head);
    assert_eq!(merged["approvals"], 1);
    let reviews = w
        .server
        .ok(&format!("{}/pulls/2/reviews", w.repo_path()), &w.owner)
        .await;
    assert_eq!(reviews["items"].as_array().unwrap().len(), 1);
    assert_eq!(reviews["items"][0]["commit"], second_head);
}

#[tokio::test]
async fn a_failed_gate_reports_its_jobs_and_main_adopts_its_successful_run_with_cache_rules() {
    let w = World::new().await;
    let runner = Runner::new(&w, "landing-runner").await;
    let jobs = format!(
        "[[jobs]]\nname = \"build\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"echo build\"]\ncache = {{ paths = [\"target\"] }}\n\n{}",
        pipeline(&["lint"])
    );
    std::fs::create_dir_all(w.work.join(".branvil")).unwrap();
    w.push_change("pipeline", ".branvil/ci.toml", &jobs);
    w.open_pull("pipeline", json!([])).await;
    merge(&w, 1).await;
    runner.drain("success").await;
    sync_main(&w);
    let head = w.push_change("feature", "feature.txt", "feature\n");
    sync_main(&w);
    w.push_change("advance", "advance.txt", "advance\n");
    w.open_pull("advance", json!([])).await;
    runner.drain("success").await;
    merge(&w, 2).await;
    runner.drain("success").await;
    settings(&w, json!({"required_checks":["build","lint"]})).await;
    w.open_pull("feature", json!([])).await;
    for _ in 0..2 {
        let job = runner.next_claim().await;
        assert_eq!(job["sha"], head);
        assert_eq!(runner.finish(&job, "success").await.0, 200);
    }
    let gating = landing(&w, 3, "gating").await;
    let gate = gating["landing"]["gate"].as_str().unwrap();
    assert_ne!(gate, head);
    let build = runner.next_claim().await;
    let lint = runner.next_claim().await;
    assert_eq!(build["job"]["name"], "build");
    assert_eq!(lint["job"]["name"], "lint");
    for job in [&build, &lint] {
        assert_eq!(job["sha"], gate);
        assert_eq!(job["cache_scope"], "branch:gate/3");
    }
    assert_eq!(runner.finish(&lint, "success").await.0, 200);
    assert_eq!(runner.finish(&build, "failure").await.0, 200);
    let failed = landing(&w, 3, "left").await;
    assert_eq!(failed["landing"]["reason"]["code"], "gate_failed");
    let comments = w
        .server
        .ok(&format!("{}/pulls/3/comments", w.repo_path()), &w.owner)
        .await;
    assert_eq!(comments["items"].as_array().unwrap().len(), 1);
    assert_eq!(comments["items"][0]["author"], "branvil");
    let body = comments["items"][0]["body"].as_str().unwrap();
    for value in [
        gating["base_sha"].as_str().unwrap(),
        head.as_str(),
        gate,
        "build",
        build["attempt_id"].as_str().unwrap(),
    ] {
        assert!(body.contains(value), "missing {value}: {body}");
    }
    let (st, result) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/retry",
                w.repo_path(),
                build["attempt_id"].as_str().unwrap()
            ),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, 200, "{result}");
    let retry = runner.next_claim().await;
    assert_eq!(retry["attempt_id"], result["id"]);
    assert_eq!(retry["sha"], gate);
    assert_eq!(runner.finish(&retry, "success").await.0, 200);
    w.acknowledge(3).await;
    let passed = landing(&w, 3, "passed").await;
    assert_eq!(passed["landing"]["gate"], gate);
    let merged = merge(&w, 3).await;
    assert_eq!(merged["merge_sha"], gate);
    let bare = w.server.data.join("repos/acme/demo.git");
    assert_eq!(git_ok(&bare, &["rev-parse", "main"]), gate);
    // Only the cache-writing job reruns as protected. The no-cache job's
    // original gate attempt remains the same; deleting the hidden gate ref
    // cannot supersede the run that main now owns.
    let protected = runner.next_claim().await;
    assert_eq!(protected["sha"], gate);
    assert_eq!(protected["job"]["name"], "build");
    assert_eq!(protected["cache_scope"], "protected");
    assert_eq!(protected["attempt"], retry["attempt"].as_u64().unwrap() + 1);
    assert!(runner.claim().await.is_none());
    assert_eq!(runner.finish(&protected, "success").await.0, 200);
    let checks = w
        .server
        .ok(
            &format!("{}/commits/{gate}/checks", w.repo_path()),
            &w.owner,
        )
        .await;
    let lint_check = checks["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["name"] == "lint")
        .unwrap();
    assert_eq!(lint_check["attempt_id"], lint["attempt_id"]);
    assert_eq!(lint_check["conclusion"], "success");
    let build_check = checks["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["name"] == "build")
        .unwrap();
    assert_eq!(build_check["attempt_id"], protected["attempt_id"]);
}
