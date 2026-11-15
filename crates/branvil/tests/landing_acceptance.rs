//! The landing contract across host polling, reviews, and CI adoption.

use crate::common::*;
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
        if p["landing"]["next"] == state {
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
    let p = landing(w, number, "owner_merge").await;
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
    let reviewed = landing(&w, 2, "owner_merge").await;
    assert_eq!(reviewed["approvals"], 1);
    merge(&w, 1).await;
    let gated = landing(&w, 2, "owner_merge").await;
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

#[tokio::test(flavor = "multi_thread")]
async fn three_ready_pulls_publish_without_waiting_for_a_host_merge() {
    use crate::upstream::{GH, followed, with_aiko_account};
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    for n in 1..=3 {
        git_ok(&w.work, &["checkout", "-q", "main"]);
        w.push_change(&format!("feature{n}"), &format!("{n}.txt"), "feature");
        w.open_pull(&format!("feature{n}"), json!([])).await;
    }
    settings(&w, json!({"auto_land":true})).await;
    crate::ci::eventually("all three published", || async { gh.pulls(GH).len() == 3 }).await;
    for n in 1..=3 {
        let p = pull(&w, n).await;
        assert_eq!(p["landing"]["blockers"], json!([]));
        assert_eq!(p["upstream"]["auto"], true);
    }
    assert!(
        git_as_ok(
            &w.work,
            "alice",
            &w.owner,
            &[
                "ls-remote",
                "origin",
                "refs/branvil/gate/*",
                "refs/branvil/failed-gate/*"
            ]
        )
        .trim()
        .is_empty()
    );
}
