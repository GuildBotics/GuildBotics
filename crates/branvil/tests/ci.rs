//! CI against a real service and real Git. The test acts as the runner over
//! the runner API (a scripted runner: it decides each attempt's conclusion),
//! so the service's queue, leases, supersession, checks, and merge gate are
//! exercised exactly as a real runner would see them.

use crate::common;

use common::*;
use reqwest::StatusCode;
use serde_json::{Value, json};

const IMAGE: &str = "docker.io/library/alpine:3.22";
/// The configuration digest the scripted runner reports for the image.
const DIGEST: &str = "sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8";

#[tokio::test]
async fn schema_24_upgrade_preserves_attempts_and_indexes_pending_failure_notices() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let head = land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    let attempt = runner.claim().await.unwrap();
    set_required(&w, &["test"]).await;
    assert_eq!(runner.finish(&attempt, "startup_failure").await.0, 200);
    w.server.stop();
    let data = w.server.data.clone();
    let db = data.join("branvil.redb");
    let attempts = records(&db, "ci_attempts");
    let pulls = records(&db, "pulls");
    as_schema_24(&db);
    let before = tables(&db);
    let admin = |what: &str| {
        let out = std::process::Command::new(BIN)
            .args(["admin", what, "--data-dir"])
            .arg(&data)
            .output()
            .unwrap();
        (
            out.status.code().unwrap(),
            format!(
                "{}{}",
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            ),
        )
    };
    assert!(admin("check").1.contains("branvil admin upgrade"));
    let (code, output) = admin("upgrade");
    assert_eq!(code, 3, "{output}");
    assert!(output.contains("from schema version 24 to 25"), "{output}");
    assert_eq!(records(&db, "ci_attempts"), attempts);
    assert_eq!(records(&db, "pulls"), pulls);
    let runs = records(&db, "ci_runs");
    let run = runs.iter().find(|r| r["sha"] == head).unwrap();
    assert_eq!(run["protected_branches"], json!(["main"]));
    assert!(run["failure_notified_at"].is_null());
    assert!(!tables(&db)["ix_ci_unnotified"].is_empty());
    let backup = std::fs::read_dir(&data)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("branvil-schema-24-")
        })
        .unwrap();
    assert_eq!(tables(&backup), before);
    let (code, output) = admin("check");
    assert_eq!(code, 0, "{output}");
    assert!(output.contains("indexes: ok"), "{output}");
    let upgraded = tables(&db);
    assert_eq!(admin("upgrade").0, 0);
    assert_eq!(tables(&db), upgraded);
}

#[tokio::test]
async fn protected_required_retries_are_visible_as_queued_checks_until_exhausted() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let head = land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    set_required(&w, &["test"]).await;
    let root = runner.claim().await.unwrap();
    let mut current = root.clone();
    for n in 1..=4 {
        let (st, result) = runner.finish(&current, "failure").await;
        assert_eq!(st, 200, "{result}");
        let report = w
            .server
            .ok(
                &format!("{}/commits/{head}/checks", w.repo_path()),
                &w.owner,
            )
            .await;
        let check = check(&report, "test");
        if n <= 3 {
            assert_eq!(check["status"], "queued", "{report}");
            assert!(check["conclusion"].is_null(), "{report}");
            let label = format!(
                "Automatic retry {n}/3 of {}",
                root["attempt_id"].as_str().unwrap()
            );
            assert!(check["details"].as_str().unwrap().contains(&label));
            current = runner.claim().await.unwrap();
            assert!(current["reuse"].is_null());
            let view = w
                .server
                .ok(
                    &format!(
                        "{}/jobs/{}",
                        w.repo_path(),
                        current["attempt_id"].as_str().unwrap()
                    ),
                    &w.owner,
                )
                .await;
            assert!(view["details"].as_str().unwrap().contains(&label));
        } else {
            assert_eq!(check["conclusion"], "failure", "{report}");
            assert!(runner.claim().await.is_none());
        }
    }
    let (st, manual) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/retry",
                w.repo_path(),
                current["attempt_id"].as_str().unwrap()
            ),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, 200, "{manual}");
    assert!(
        manual["details"].is_null(),
        "manual retry has its own budget"
    );
}

pub(crate) fn pipeline(jobs: &[&str]) -> String {
    let mut s = String::new();
    for j in jobs {
        s.push_str(&format!(
            "[[jobs]]\nname = \"{j}\"\nimage = \"{IMAGE}\"\ncommands = [\"echo {j}\"]\nartifacts = [{{name = \"report\", paths = [\"out/\"]}}]\n\n"
        ));
    }
    s
}

fn filtered_pipeline() -> String {
    format!(
        "{}paths=['src/**']\n{}paths=['web/**']\n{}",
        pipeline(&["rust"]),
        pipeline(&["web"]),
        pipeline(&["always"])
    )
}

#[tokio::test]
async fn paths_protected_non_default_tip_needs_a_pull_but_then_runs_all_jobs() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &filtered_pipeline())],
        None,
    )
    .await;
    runner.drain("success").await;
    let head = w.push_change("release", "notes.md", "notes\n");
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let (st, v) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "protected_branches": ["release"]}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    w.restart(&[]);
    let runner = Runner::new(&w, "paths-runner-after-restart").await;
    let report = w
        .server
        .ok(
            &format!("{}/commits/{head}/checks", w.repo_path()),
            &w.owner,
        )
        .await;
    assert_eq!(report["run_registered"], false, "{report}");
    assert!(runner.claim().await.is_none());

    let p = w.open_pull("release", json!([])).await;
    let report = checks(&w, p["number"].as_u64().unwrap()).await;
    for name in ["rust", "web", "always"] {
        let c = check(&report, name);
        assert_eq!(c["status"], "queued", "{report}");
        assert!(c["path_skip"].is_null(), "{report}");
    }
    assert_eq!(runner.drain("success").await, 3);
}

#[tokio::test]
async fn paths_empty_tree_changes_leave_as_already_in_base() {
    let w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &filtered_pipeline())],
        None,
    )
    .await;
    runner.drain("success").await;
    set_required(&w, &["rust", "web", "always"]).await;
    git_ok(&w.work, &["checkout", "-qb", "empty"]);
    git_ok(&w.work, &["commit", "--allow-empty", "-qm", "same tree"]);
    git_as_ok(&w.work, "aiko", &w.aiko, &["push", "-q", "origin", "empty"]);
    let p = w.open_pull("empty", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    assert_eq!(check(&checks(&w, n).await, "rust")["conclusion"], "skipped");
    let c = runner.claim().await.unwrap();
    assert_eq!(c["job"]["name"], "always");
    runner.finish(&c, "success").await;
    eventually("a no-op gate leaves the queue", || async {
        w.server
            .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
            .await["landing"]["reason"]["code"]
            == "already_in_base"
    })
    .await;
    assert!(runner.claim().await.is_none());
}

#[tokio::test]
async fn paths_head_skips_have_logs_and_gates_execute_every_job_before_owner_merge() {
    paths_land(false).await;
}

#[tokio::test]
async fn paths_head_skips_allow_auto_merge_after_the_full_gate() {
    paths_land(true).await;
}

async fn paths_land(auto: bool) {
    let w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    let base = land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &filtered_pipeline())],
        None,
    )
    .await;
    assert_eq!(runner.drain("success").await, 3, "main runs all jobs");
    set_required(&w, &["rust", "web", "always"]).await;
    let head = w.push_change("docs", "notes.md", "documentation\n");
    let p = w.open_pull("docs", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let report = checks(&w, n).await;
    for name in ["rust", "web"] {
        let c = check(&report, name);
        assert_eq!(c["conclusion"], "skipped");
        assert_eq!(c["path_skip"]["bases"][0]["merge_base"], base);
        assert_eq!(c["path_skip"]["bases"][0]["base_sha"], base);
        let id = c["attempt_id"].as_str().unwrap();
        let job = w
            .server
            .ok(&format!("{}/jobs/{id}", w.repo_path()), &w.owner)
            .await;
        assert_eq!(job["path_skip"], c["path_skip"]);
        let log = w
            .server
            .ok(&format!("{}/jobs/{id}/log", w.repo_path()), &w.owner)
            .await;
        assert!(log["log"].as_str().unwrap().starts_with(&format!(
            "[branvil] skipped: no file matching paths changed since {base}"
        )));
        assert!(log["log"].as_str().unwrap().contains(&base));
    }
    let always = runner.claim().await.unwrap();
    assert_eq!(always["job"]["name"], "always");
    assert_eq!(always["sha"], head);
    assert!(
        runner.claim().await.is_none(),
        "filtered jobs never reach a runner"
    );
    runner.finish(&always, "skipped").await;
    // A runner's skipped is not a path decision, so the PR cannot enter the gate.
    let pull_path = format!("{}/pulls/{n}", w.repo_path());
    let current = w.server.ok(&pull_path, &w.owner).await;
    assert!(current["landing"].is_null());
    let id = always["attempt_id"].as_str().unwrap();
    assert_eq!(
        w.server
            .post(
                &format!("{}/jobs/{id}/retry", w.repo_path()),
                &w.owner,
                json!({})
            )
            .await
            .0,
        200
    );
    let actual = runner.claim().await.unwrap();
    assert_eq!(runner.finish(&actual, "success").await.0, 200);
    let mut seen = Vec::new();
    let mut next = Some(runner.next_claim().await);
    while let Some(c) = next {
        assert_ne!(c["sha"], head, "head remains satisfied throughout gating");
        let name = c["job"]["name"].as_str().unwrap().to_string();
        if name == "always" {
            assert_eq!(c["reuse"]["attempt_id"], actual["attempt_id"]);
            let source = c["reuse"]["attempt_id"].clone();
            assert_eq!(runner.post(&c, "finish", json!({"lease":c["lease"],"conclusion":"success","image_digest":DIGEST,"reused_from":source})).await.0, 200);
        } else {
            assert!(c["reuse"].is_null(), "service skips are not reuse sources");
            assert_eq!(runner.finish(&c, "success").await.0, 200);
        }
        seen.push(name);
        assert_eq!(check(&checks(&w, n).await, "rust")["conclusion"], "skipped");
        next = runner.claim().await;
    }
    seen.sort();
    assert_eq!(seen, ["always", "rust", "web"]);
    eventually("gate passed", || async {
        w.server.ok(&pull_path, &w.owner).await["landing"]["state"] == "passed"
    })
    .await;
    if auto {
        let repo = w.server.ok(&w.repo_path(), &w.owner).await;
        assert_eq!(
            w.server
                .patch(
                    &w.repo_path(),
                    &w.owner,
                    json!({"revision":repo["revision"],"auto_land":true})
                )
                .await
                .0,
            200
        );
        eventually("automatic merge after the full gate", || async {
            w.server.ok(&pull_path, &w.owner).await["merged"] == true
        })
        .await;
    } else {
        let (status, result) = w
            .server
            .post(
                &format!("{pull_path}/merge"),
                &w.owner,
                json!({"expected_head":head,"expected_base":p["base_sha"]}),
            )
            .await;
        assert_eq!(status, 200, "{result}");
    }
}

#[tokio::test]
async fn paths_match_git_globs_and_a_pipeline_change_disables_selection() {
    let w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    land(
        &w,
        "setup",
        &[
            (".branvil/ci.toml", &filtered_pipeline()),
            ("src/nested/a.rs", "before\n"),
        ],
        None,
    )
    .await;
    runner.drain("success").await;
    std::fs::create_dir_all(w.work.join("src/nested")).unwrap();
    w.push_change("rust", "src/nested/a.rs", "changed\n");
    let p = w.open_pull("rust", json!([])).await;
    let report = checks(&w, p["number"].as_u64().unwrap()).await;
    assert_eq!(check(&report, "rust")["status"], "queued");
    assert_eq!(check(&report, "web")["conclusion"], "skipped");
    git_ok(&w.work, &["mv", "src/nested/a.rs", "renamed.txt"]);
    git_ok(&w.work, &["commit", "-qm", "rename source out of filter"]);
    git_as_ok(&w.work, "aiko", &w.aiko, &["push", "-q", "origin", "rust"]);
    let report = checks(&w, p["number"].as_u64().unwrap()).await;
    assert_eq!(
        check(&report, "rust")["status"],
        "queued",
        "the deleted side of a rename still matches"
    );
    assert_eq!(check(&report, "web")["conclusion"], "skipped");
    sync_main(&w);
    w.push_change(
        "pipeline-change",
        ".branvil/ci.toml",
        &format!("{}\n# changed", filtered_pipeline()),
    );
    let p = w.open_pull("pipeline-change", json!([])).await;
    let report = checks(&w, p["number"].as_u64().unwrap()).await;
    for name in ["rust", "web", "always"] {
        assert_eq!(check(&report, name)["status"], "queued");
    }
}

#[tokio::test]
async fn paths_retry_actually_executes_a_skipped_job() {
    let w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &filtered_pipeline())],
        None,
    )
    .await;
    runner.drain("success").await;
    set_required(&w, &["always"]).await;
    let head = w.push_change("docs", "notes.md", "notes\n");
    let p = w.open_pull("docs", json!([])).await;
    let report = checks(&w, p["number"].as_u64().unwrap()).await;
    let id = check(&report, "rust")["attempt_id"].as_str().unwrap();
    let (st, retry) = w
        .server
        .post(
            &format!("{}/jobs/{id}/retry", w.repo_path()),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, 200, "{retry}");
    assert!(retry["path_skip"].is_null());
    let mut seen = Vec::new();
    while let Some(c) = runner.claim().await {
        if c["sha"] == head {
            if c["job"]["name"] == "rust" {
                assert!(c["reuse"].is_null());
            }
            seen.push(c["job"]["name"].as_str().unwrap().to_string());
        }
        assert_eq!(runner.finish(&c, "success").await.0, 200);
    }
    seen.sort();
    assert_eq!(seen, ["always", "rust"]);
}

#[tokio::test]
async fn paths_reselect_for_another_pr_and_ref_of_the_same_sha_and_remain_stable_on_restart() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    let older = land(
        &w,
        "setup",
        &[
            (".branvil/ci.toml", &filtered_pipeline()),
            ("src/a.rs", "before\n"),
        ],
        None,
    )
    .await;
    runner.drain("success").await;
    land(&w, "advance", &[("src/a.rs", "after\n")], None).await;
    runner.drain("success").await;
    set_required(&w, &["always"]).await;
    let head = w.push_change("docs", "notes.md", "notes\n");
    let p = w.open_pull("docs", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    assert_eq!(check(&checks(&w, n).await, "rust")["conclusion"], "skipped");
    git_ok(&w.work, &["branch", "older", &older]);
    git_ok(&w.work, &["branch", "alias", &head]);
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", "origin", "older", "alias"],
    );
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head":"alias", "base":"older", "title":"Same head, older base"}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    let mut before = checks(&w, n).await;
    assert_eq!(check(&before, "rust")["status"], "queued");
    assert_eq!(check(&before, "rust")["attempt"], 2);
    assert_eq!(
        check(&before, "web")["path_skip"]["bases"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    w.restart(&[]);
    let mut after = checks(&w, n).await;
    for report in [&mut before, &mut after] {
        for check in report["checks"].as_array_mut().unwrap() {
            check.as_object_mut().unwrap().remove("details_url");
        }
    }
    assert_eq!(
        after["checks"], before["checks"],
        "reconcile does not recreate skipped attempts"
    );
    let report = w
        .server
        .ok(
            &format!("{}/commits/{head}/checks", w.repo_path()),
            &w.owner,
        )
        .await;
    assert_eq!(check(&report, "rust")["status"], "queued");
    git_ok(
        &w.work,
        &[
            "update-ref",
            "refs/heads/older",
            p["base_sha"].as_str().unwrap(),
        ],
    );
    git_as_ok(&w.work, "aiko", &w.aiko, &["push", "-q", "origin", "older"]);
    w.restart(&[]);
    eventually("reconcile observes the changed base tip", || async {
        check(&checks(&w, n).await, "web")["path_skip"]["bases"][1]["base_sha"] == p["base_sha"]
    })
    .await;
    let report = checks(&w, n).await;
    let bases = check(&report, "web")["path_skip"]["bases"]
        .as_array()
        .unwrap();
    assert_eq!(
        bases[1]["base_sha"], p["base_sha"],
        "unchanged PR ids still reselect when a base tip moves"
    );
    assert_eq!(check(&report, "web")["attempt"], 3);
}

#[tokio::test]
async fn a_reused_success_counts_for_required_checks_and_not_for_duration_estimates() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    let source = runner.claim().await.unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
    assert_eq!(runner.finish(&source, "success").await.0, 200);
    set_required(&w, &["test"]).await;
    sync_main(&w);
    git_ok(&w.work, &["checkout", "-qb", "same"]);
    git_ok(&w.work, &["commit", "--allow-empty", "-qm", "same tree"]);
    git_as_ok(&w.work, "aiko", &w.aiko, &["push", "-q", "origin", "same"]);
    let p = w.open_pull("same", json!([])).await;
    let number = p["number"].as_u64().unwrap();
    let c = runner.claim().await.unwrap();
    assert_eq!(c["reuse"]["attempt_id"], source["attempt_id"]);
    let (status, result) = runner.post(&c, "finish", json!({"lease": c["lease"], "conclusion": "success", "image_digest": DIGEST, "reused_from": source["attempt_id"]})).await;
    assert_eq!(status, 200, "{result}");
    assert_eq!(result["reused_from"]["sha"], source["sha"]);
    assert_eq!(
        check(&checks(&w, number).await, "test")["conclusion"],
        "success"
    );
    let (status, result) = w
        .server
        .post(
            &format!("{}/pulls/{number}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(status, 200, "{result}");
    sync_main(&w);
    w.push_change("next", "next.txt", "next\n");
    let next = w.open_pull("next", json!([])).await;
    runner.claim().await.unwrap();
    let checks = checks(&w, next["number"].as_u64().unwrap()).await;
    let took = check(&checks, "test")["expected_secs"].as_u64().unwrap();
    assert!(
        took >= 1,
        "reuse's sub-second duration must not replace the actual duration: {checks}"
    );
}

#[tokio::test]
async fn cache_refresh_never_changes_main_checks_or_required_readiness() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let cached_pipeline =
        pipeline(&["test"]).replace("commands =", "cache = { paths = [\"target\"] }\ncommands =");
    land(&w, "setup", &[(".branvil/ci.toml", &cached_pipeline)], None).await;
    let source = runner.claim().await.unwrap();
    let key = "a".repeat(64);
    let (st, result) = runner.post(&source, "finish", json!({"lease": source["lease"], "conclusion": "success", "image_digest": DIGEST, "cache_key": key})).await;
    assert_eq!(st, 200, "{result}");
    set_required(&w, &["test"]).await;
    sync_main(&w);
    git_ok(&w.work, &["checkout", "-qb", "same"]);
    git_ok(&w.work, &["commit", "--allow-empty", "-qm", "same tree"]);
    git_as_ok(&w.work, "aiko", &w.aiko, &["push", "-q", "origin", "same"]);
    let p = w.open_pull("same", json!([])).await;
    let c = runner.claim().await.unwrap();
    assert_eq!(c["reuse"]["attempt_id"], source["attempt_id"]);
    assert_eq!(runner.post(&c, "finish", json!({"lease": c["lease"], "conclusion": "success", "image_digest": DIGEST, "reused_from": source["attempt_id"]})).await.0, 200);
    let n = p["number"].as_u64().unwrap();
    let (st, result) = w
        .server
        .post(
            &format!("{}/pulls/{n}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, 200, "{result}");
    let c = runner.claim().await.unwrap();
    assert_eq!(c["purpose"], "check");
    let (st, result) = runner.post(&c, "finish", json!({"lease": c["lease"], "conclusion": "success", "image_digest": DIGEST, "reused_from": source["attempt_id"], "refresh_cache": true})).await;
    assert_eq!(st, 200, "{result}");
    let refresh_id = result["cache_refresh"].as_str().unwrap().to_owned();
    let sha = c["sha"].as_str().unwrap();
    let checks_path = format!("{}/commits/{sha}/checks", w.repo_path());
    let before = w.server.ok(&checks_path, &w.aiko).await;
    assert_eq!(before["checks"].as_array().unwrap().len(), 1);
    assert_eq!(before["checks"][0]["status"], "completed");
    assert_eq!(before["checks"][0]["conclusion"], "success");
    assert_eq!(before["checks"][0]["cache_refresh"], refresh_id);
    assert!(
        before["checks"][0]["details"]
            .as_str()
            .unwrap()
            .contains(&format!("/-/jobs/{refresh_id}"))
    );
    let refresh = runner.claim().await.unwrap();
    assert_eq!(refresh["attempt_id"], refresh_id);
    assert_eq!(refresh["purpose"], "cache_refresh");
    assert_eq!(refresh["cache_scope"], "protected");
    assert!(refresh["reuse"].is_null());
    assert_eq!(w.server.ok(&checks_path, &w.aiko).await, before);
    assert_eq!(runner.finish(&refresh, "failure").await.0, 200);
    assert_eq!(w.server.ok(&checks_path, &w.aiko).await, before);
    let (st, refused) = w
        .server
        .post(
            &format!("{}/jobs/{refresh_id}/retry", w.repo_path()),
            &w.aiko,
            json!({}),
        )
        .await;
    assert_eq!(st, 409, "refresh retry cannot become a check");
    assert_eq!(
        refused["error"]["message"],
        "Cache refresh attempts cannot be manually retried."
    );
    // Preserve both purposes and their association across the previous-schema upgrade.
    w.server.stop();
    let db = w.server.data.join("branvil.redb");
    let before_attempts = records(&db, "ci_attempts");
    assert!(
        before_attempts
            .iter()
            .any(|a| a["purpose"] == "cache_refresh")
    );
    assert!(
        before_attempts
            .iter()
            .any(|a| a["cache_refresh"] == refresh_id)
    );
    as_schema_24(&db);
    for (command, expected) in [("upgrade", 3), ("check", 0)] {
        let out = std::process::Command::new(BIN)
            .args(["admin", command, "--data-dir"])
            .arg(&w.server.data)
            .output()
            .unwrap();
        assert_eq!(
            out.status.code(),
            Some(expected),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    assert_eq!(records(&db, "ci_attempts"), before_attempts);
}

#[tokio::test]
async fn next_claim_retries_empty_responses_until_a_job_is_registered() {
    use axum::{Json, Router, response::IntoResponse, routing::post};
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    let requests = Arc::new(AtomicUsize::new(0));
    let count = requests.clone();
    let app = Router::new().route(
        "/api/v1/runner/claim",
        post(move || {
            let count = count.clone();
            async move {
                if count.fetch_add(1, Ordering::SeqCst) < 2 {
                    StatusCode::NO_CONTENT.into_response()
                } else {
                    Json(json!({"attempt_id":"registered"})).into_response()
                }
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let runner = Runner {
        url: format!("http://{}", listener.local_addr().unwrap()),
        token: "scripted-runner".into(),
        http: reqwest::Client::new(),
    };
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let claim = runner.next_claim().await;
    server.abort();
    assert_eq!(claim["attempt_id"], "registered");
    assert_eq!(requests.load(Ordering::SeqCst), 3);
}

/// A scripted runner talking to the runner API.
pub(crate) struct Runner {
    url: String,
    token: String,
    http: reqwest::Client,
}

impl Runner {
    pub(crate) async fn new(w: &World, name: &str) -> Runner {
        Runner {
            url: w.server.url.clone(),
            token: w.server.member(&w.owner, name, "runner").await,
            http: reqwest::Client::new(),
        }
    }

    fn at(&self, path: &str) -> String {
        format!("{}/api/v1/runner{path}", self.url)
    }

    pub(crate) async fn claim(&self) -> Option<Value> {
        let resp = self
            .http
            .post(self.at("/claim"))
            .bearer_auth(&self.token)
            .json(&json!({"executor": "test", "executor_state": "ready"}))
            .send()
            .await
            .unwrap();
        match resp.status() {
            StatusCode::NO_CONTENT => None,
            StatusCode::OK => Some(resp.json().await.unwrap()),
            other => panic!("claim: {other} {}", resp.text().await.unwrap()),
        }
    }

    /// Waits for asynchronously registered work; an empty queue is not EOF.
    pub(crate) async fn next_claim(&self) -> Value {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        loop {
            if let Some(claim) = self.claim().await {
                return claim;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "no queued job appeared"
            );
            tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        }
    }

    async fn post(&self, c: &Value, what: &str, body: Value) -> (StatusCode, Value) {
        let resp = self
            .http
            .post(self.at(&format!(
                "/attempts/{}/{what}",
                c["attempt_id"].as_str().unwrap()
            )))
            .bearer_auth(&self.token)
            .json(&body)
            .send()
            .await
            .unwrap();
        let st = resp.status();
        (st, resp.json().await.unwrap_or(Value::Null))
    }

    async fn heartbeat(&self, c: &Value) -> (StatusCode, Value) {
        self.post(c, "heartbeat", json!({"lease": c["lease"]}))
            .await
    }

    pub(crate) async fn finish(&self, c: &Value, conclusion: &str) -> (StatusCode, Value) {
        self.post(
            c,
            "finish",
            json!({"lease": c["lease"], "conclusion": conclusion, "details": format!("scripted {conclusion}"), "image_digest": DIGEST}),
        )
        .await
    }

    async fn raw(
        &self,
        method: reqwest::Method,
        c: &Value,
        path: &str,
        body: Vec<u8>,
    ) -> (StatusCode, Vec<u8>) {
        let resp = self
            .http
            .request(
                method,
                self.at(&format!(
                    "/attempts/{}/{path}",
                    c["attempt_id"].as_str().unwrap()
                )),
            )
            .bearer_auth(&self.token)
            .header("x-branvil-lease", c["lease"].as_str().unwrap())
            .body(body)
            .send()
            .await
            .unwrap();
        (resp.status(), resp.bytes().await.unwrap().to_vec())
    }

    /// Claims every queued attempt and finishes it with `conclusion`.
    pub(crate) async fn drain(&self, conclusion: &str) -> usize {
        let mut n = 0;
        while let Some(c) = self.claim().await {
            assert_eq!(self.finish(&c, conclusion).await.0, 200);
            n += 1;
        }
        n
    }
}

/// Brings the work clone's main to the service's main.
pub(crate) fn sync_main(w: &World) {
    git_as_ok(&w.work, "alice", &w.owner, &["fetch", "-q", "origin"]);
    git_ok(&w.work, &["checkout", "-q", "-B", "main", "origin/main"]);
}

/// Lands files on the protected main through a reviewed, merged pull
/// request (running CI with `runner` when there is any).
pub(crate) async fn land(
    w: &World,
    branch: &str,
    files: &[(&str, &str)],
    runner: Option<&Runner>,
) -> String {
    sync_main(w);
    for (path, content) in files {
        if let Some(parent) = std::path::Path::new(path).parent() {
            std::fs::create_dir_all(w.work.join(parent)).unwrap();
        }
        std::fs::write(w.work.join(path), content).unwrap();
        git_ok(&w.work, &["add", path]);
    }
    let (last, content) = files.last().unwrap();
    let head = w.push_change(branch, last, content);
    let p = w.open_pull(branch, json!([])).await;
    let n = p["number"].as_u64().unwrap();
    if let Some(r) = runner {
        r.drain("success").await;
    }
    let base = format!("{}/pulls/{n}", w.repo_path());
    let (st, v) = w
        .server
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    sync_main(w);
    head
}

async fn set_required(w: &World, checks: &[&str]) {
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let (st, v) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "required_checks": checks}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
}

async fn checks(w: &World, n: u64) -> Value {
    w.server
        .ok(&format!("{}/pulls/{n}/checks", w.repo_path()), &w.aiko)
        .await
}

fn check<'a>(v: &'a Value, name: &str) -> &'a Value {
    v["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == name)
        .unwrap_or_else(|| panic!("no check {name} in {v}"))
}

/// Polls `f` until it returns true (the service sweeps every 5 s).
pub(crate) async fn eventually<F, Fut>(what: &str, mut f: F)
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    for _ in 0..60 {
        if f().await {
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    panic!("timed out waiting for {what}");
}

/// Sets `[ci]` values in the service's configuration (restart to apply).
fn set_ci_config(w: &World, values: &[(&str, toml::Value)]) {
    let path = w.server.data.join("branvil.toml");
    let mut doc: toml::Table = std::fs::read_to_string(&path).unwrap().parse().unwrap();
    let ci = doc
        .entry("ci")
        .or_insert_with(|| toml::Value::Table(toml::Table::new()))
        .as_table_mut()
        .unwrap();
    for (k, v) in values {
        ci.insert(k.to_string(), v.clone());
    }
    std::fs::write(path, toml::to_string(&doc).unwrap()).unwrap();
}

#[tokio::test]
async fn checks_say_when_they_ran_and_how_long_the_job_took_where_it_last_succeeded() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test", "lint"]))],
        None,
    )
    .await;
    // On main's tip, test succeeds after a while and lint fails.
    let main_test = runner.claim().await.unwrap();
    let main_lint = runner.claim().await.unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
    runner.finish(&main_test, "success").await;
    runner.finish(&main_lint, "failure").await;
    let tip = main_test["sha"].as_str().unwrap();
    let on_main = w
        .server
        .ok(&format!("{}/commits/{tip}/checks", w.repo_path()), &w.aiko)
        .await;
    let ran = check(&on_main, "test");
    let secs = |c: &Value| {
        let at = |k: &str| chrono::DateTime::parse_from_rfc3339(c[k].as_str().unwrap()).unwrap();
        (at("finished_at") - at("started_at")).num_seconds()
    };
    let took = secs(ran);
    assert!(took >= 1, "{on_main}");
    assert!(ran.get("expected_secs").is_none(), "{on_main}");

    // A head on top of it. Queued, nothing has a time yet; running, its test
    // expects test's time on main, and lint, which never succeeded, nothing.
    sync_main(&w);
    w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let v = checks(&w, n).await;
    assert_eq!(check(&v, "test")["status"], "queued");
    assert!(check(&v, "test").get("started_at").is_none(), "{v}");
    assert!(check(&v, "test").get("expected_secs").is_none(), "{v}");
    let running = runner.claim().await.unwrap();
    let lint = runner.claim().await.unwrap();
    let v = checks(&w, n).await;
    let t = check(&v, "test");
    assert_eq!(t["status"], "in_progress");
    assert!(
        t["started_at"].is_string() && t.get("finished_at").is_none(),
        "{v}"
    );
    assert_eq!(t["expected_secs"], took, "{v}");
    assert!(check(&v, "lint").get("expected_secs").is_none(), "{v}");
    // The commit's page says the same.
    let head = running["sha"].as_str().unwrap();
    let on_head = w
        .server
        .ok(&format!("{}/commits/{head}/checks", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(check(&on_head, "test")["expected_secs"], took, "{on_head}");

    // Finished, it says how long it took, and expects nothing.
    runner.finish(&running, "success").await;
    runner.finish(&lint, "success").await;
    let v = checks(&w, n).await;
    let t = check(&v, "test").clone();
    assert!(t.get("expected_secs").is_none(), "{v}");
    let own = secs(&t);
    assert!(own < took, "{v}");
    // Run again, it expects the time it took on this very commit.
    let (st, again) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/retry",
                w.repo_path(),
                t["attempt_id"].as_str().unwrap()
            ),
            &w.aiko,
            json!({}),
        )
        .await;
    assert!(st.is_success(), "{again}");
    runner.claim().await.unwrap();
    assert_eq!(check(&checks(&w, n).await, "test")["expected_secs"], own);
}

#[tokio::test]
async fn a_head_runs_its_own_pipeline_and_reports_logs_and_artifacts() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    // The run of main's new tip (the merge) is queued; take it out of the way.
    assert_eq!(runner.drain("success").await, 1);
    set_required(&w, &["test"]).await;

    // The head changes its pipeline; its own pipeline runs.
    sync_main(&w);
    let changed = pipeline(&["test"]).replace("echo test", "echo changed");
    std::fs::write(w.work.join(".branvil/ci.toml"), changed).unwrap();
    git_ok(&w.work, &["add", ".branvil/ci.toml"]);
    let head = w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let v = checks(&w, n).await;
    assert_eq!(v["checks_expected"], true);
    assert_eq!(v["checks"].as_array().unwrap().len(), 1, "{v}");
    assert_eq!(check(&v, "test")["status"], "queued");
    assert!(check(&v, "test")["attempt_id"].is_string());

    let c = runner.claim().await.unwrap();
    assert_eq!(c["sha"], head.as_str());
    assert_eq!(c["job"]["name"], "test");
    assert_eq!(c["job"]["commands"], json!(["echo changed"]));
    assert_eq!(c["job"]["image"], IMAGE);
    // The runner's limits apply: the pipeline gives no time limit.
    assert!(c["job"]["timeout_secs"].is_null(), "{c}");
    assert!(c["job"].get("cpus").is_none(), "{c}");
    assert_eq!(c["repo"], REPO);
    assert_eq!(c["network"], false);
    assert_eq!(c["cache_scope"], "branch:feature");
    assert!(c["queued_secs"].is_u64(), "{c}");
    assert_eq!(check(&checks(&w, n).await, "test")["status"], "in_progress");

    // The source is a Git bundle made by the service: the head as HEAD,
    // with its history.
    let (st, bundle) = runner.raw(reqwest::Method::GET, &c, "source", vec![]).await;
    assert_eq!(st, 200);
    let bundle_path = w.dir.path().join("src.bundle");
    std::fs::write(&bundle_path, &bundle).unwrap();
    git_ok(
        w.dir.path(),
        &["clone", "-q", bundle_path.to_str().unwrap(), "src"],
    );
    let src = w.dir.path().join("src");
    assert_eq!(git_ok(&src, &["rev-parse", "HEAD"]), head);
    let files = git_ok(&src, &["ls-files"]);
    assert!(files.lines().any(|l| l == "f.txt"), "{files}");
    assert!(files.lines().any(|l| l == "README.md"), "{files}");
    assert!(
        git_ok(&src, &["rev-list", "--count", "HEAD"])
            .parse::<u32>()
            .unwrap()
            > 1,
        "the history comes along"
    );

    let (st, beat) = runner.heartbeat(&c).await;
    assert_eq!(st, 200, "{beat}");
    assert_eq!(beat["cancel"], false);
    let (st, _) = runner
        .raw(
            reqwest::Method::POST,
            &c,
            "log",
            b"line one\nline two\n".to_vec(),
        )
        .await;
    assert_eq!(st, 200);
    // Artifacts: zip only, declared names only, no path tricks.
    let zip = b"PK\x05\x06\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0".to_vec();
    let (st, _) = runner
        .raw(reqwest::Method::PUT, &c, "artifacts/report", zip.clone())
        .await;
    assert_eq!(st, 200);
    let (st, _) = runner
        .raw(reqwest::Method::PUT, &c, "artifacts/other", zip.clone())
        .await;
    assert_eq!(st, 400, "undeclared artifact");
    let (st, _) = runner
        .raw(
            reqwest::Method::PUT,
            &c,
            "artifacts/report",
            b"not a zip".to_vec(),
        )
        .await;
    assert_eq!(st, 400);
    let (st, _) = runner
        .raw(
            reqwest::Method::PUT,
            &c,
            "artifacts/..%2F..%2Fx",
            zip.clone(),
        )
        .await;
    assert!(st == 400 || st == 404, "{st}");
    // Another runner's or a made-up lease changes nothing.
    let mut forged = c.clone();
    forged["lease"] = json!("bvl_forged");
    assert_eq!(runner.finish(&forged, "success").await.0, 409);
    assert_eq!(
        runner
            .raw(reqwest::Method::POST, &forged, "log", b"x".to_vec())
            .await
            .0,
        409
    );

    let (st, fin) = runner
        .post(
            &c,
            "finish",
            json!({"lease": c["lease"], "conclusion": "success", "image_digest": "alpine:3.22"}),
        )
        .await;
    assert_eq!(st, 400, "not a digest: {fin}");
    let (st, fin) = runner.finish(&c, "success").await;
    assert_eq!(st, 200, "{fin}");
    assert_eq!(fin["image_digest"], DIGEST);
    let v = checks(&w, n).await;
    let t = check(&v, "test");
    assert_eq!(
        (t["status"].as_str(), t["conclusion"].as_str()),
        (Some("completed"), Some("success"))
    );
    assert_eq!(t["artifact_names"], json!(["report"]));
    let id = t["attempt_id"].as_str().unwrap();
    assert_eq!(
        t["details_url"].as_str().unwrap(),
        format!("{}/{REPO}/-/jobs/{id}", w.server.url)
    );
    let job = w
        .server
        .ok(&format!("{}/jobs/{id}", w.repo_path()), &w.ken)
        .await;
    assert_eq!(job["job"], "test");
    assert_eq!(job["attempt"], 1);
    assert_eq!(job["sha"], head.as_str());
    assert_eq!(job["image_digest"], DIGEST);
    let log = w
        .server
        .ok(
            &format!("{}/jobs/{id}/log?tail_bytes=9", w.repo_path()),
            &w.ken,
        )
        .await;
    assert_eq!(log["log"], "line two\n");
    assert_eq!(log["log_bytes"], 18);
    assert_eq!(log["truncated"], true);
    let resp = w
        .server
        .http
        .get(format!(
            "{}/api/v1{}/commits/{head}/artifacts/report",
            w.server.url,
            w.repo_path()
        ))
        .bearer_auth(&w.ken)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    assert_eq!(resp.headers()["content-type"], "application/zip");
    assert_eq!(resp.bytes().await.unwrap().to_vec(), zip);
    // A runner sees no job outside the runner API; another commit has no artifact.
    assert_eq!(
        w.server
            .get(&format!("{}/jobs/{id}", w.repo_path()), &runner.token)
            .await
            .0,
        403
    );
    let base_sha = p["base_sha"].as_str().unwrap();
    assert_eq!(
        w.server
            .get(
                &format!("{}/commits/{base_sha}/artifacts/nothing", w.repo_path()),
                &w.ken
            )
            .await
            .0,
        404
    );

    // The runner API is for runner members only.
    let resp = w
        .server
        .http
        .post(format!("{}/api/v1/runner/claim", w.server.url))
        .bearer_auth(&w.aiko)
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 403);

    // With the check green, the merge goes through.
    let base = format!("{}/pulls/{n}", w.repo_path());
    let (st, v) = w
        .server
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    // The fast-forwarded main keeps the head's run: nothing runs again (no
    // job has a dependency cache), and
    // the merged pull request still shows its check once its branch is gone.
    assert!(runner.claim().await.is_none());
    let v = checks(&w, n).await;
    assert_eq!(v["head_sha"], head.as_str());
    assert_eq!(check(&v, "test")["conclusion"], "success", "{v}");
    let diag = w.server.ok("/admin/diagnostics", &w.owner).await;
    assert_eq!(diag["runner"]["state"], "ready", "{diag}");
    assert_eq!(diag["runner"]["runners"][0]["name"], "runner1");
}

/// A fast-forward merge runs no job of the merged head again, but those
/// with a dependency cache: only a protected branch's run saves the cache, and
/// a merged head's jobs ran for its pull request.
#[tokio::test]
async fn a_merge_runs_the_jobs_with_a_cache_again_as_the_default_branchs() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let jobs = format!(
        "[[jobs]]\nname = \"build\"\nimage = \"{IMAGE}\"\ncommands = [\"echo build\"]\ncache = {{ paths = [\"target\"] }}\n\n{}",
        pipeline(&["lint"])
    );
    land(&w, "setup", &[(".branvil/ci.toml", &jobs)], None).await;
    // Merged while its jobs waited, the setup's commit runs them as main's.
    for _ in 0..2 {
        let c = runner.claim().await.unwrap();
        assert_eq!(c["cache_scope"], "protected", "{c}");
        assert_eq!(runner.finish(&c, "success").await.0, 200);
    }

    let head = w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    // `build` still runs for the pull request when it is merged.
    let running = runner.claim().await.unwrap();
    assert_eq!(running["job"]["name"], "build");
    assert_eq!(running["cache_scope"], "branch:feature", "{running}");
    let lint = runner.claim().await.unwrap();
    assert_eq!(runner.finish(&lint, "success").await.0, 200);
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head": head, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    // The pull request's run of it is asked to stop; main's runs instead.
    assert_eq!(runner.heartbeat(&running).await.1["cancel"], true);
    let c = runner.claim().await.expect("build runs again as main's");
    assert_eq!(
        (
            &c["job"]["name"],
            &c["sha"],
            &c["attempt"],
            &c["cache_scope"]
        ),
        (
            &json!("build"),
            &json!(head),
            &json!(2),
            &json!("protected")
        )
    );
    assert!(runner.claim().await.is_none(), "lint has no cache");
    assert_eq!(runner.finish(&c, "success").await.0, 200);
    let v = checks(&w, n).await;
    assert_eq!(check(&v, "build")["conclusion"], "success", "{v}");
    assert_eq!(check(&v, "build")["attempt"], 2, "{v}");
    assert_eq!(check(&v, "lint")["attempt"], 1, "{v}");
}

#[tokio::test]
async fn rapid_pushes_supersede_older_commits_and_their_late_results_never_count() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["build", "test"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    set_required(&w, &["build", "test"]).await;
    sync_main(&w);
    let s1 = w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    // build of s1 starts; test of s1 is still queued.
    let running = runner.claim().await.unwrap();
    assert_eq!(running["sha"], s1.as_str());
    let s2 = w.push_change("feature", "f.txt", "2\n");
    let s3 = w.push_change("feature", "f.txt", "3\n");
    // The running attempt of s1 is asked to stop and its result is not used.
    let (st, beat) = runner.heartbeat(&running).await;
    assert_eq!(st, 200);
    assert_eq!(beat["cancel"], true);
    let (st, fin) = runner.finish(&running, "success").await;
    assert_eq!(st, 200);
    assert_eq!(fin["conclusion"], "cancelled");
    // s2 never got to run: its attempts were replaced before they started.
    let mut claimed = Vec::new();
    while let Some(c) = runner.claim().await {
        claimed.push(c["sha"].as_str().unwrap().to_string());
        assert_eq!(runner.finish(&c, "success").await.0, 200);
    }
    assert_eq!(
        claimed,
        vec![s3.clone(), s3.clone()],
        "only the newest commit runs"
    );
    assert!(!claimed.contains(&s2), "s2 was replaced before it started");
    let v = checks(&w, n).await;
    assert_eq!(v["head_sha"], s3.as_str());
    assert_eq!(check(&v, "build")["conclusion"], "success");
    assert_eq!(check(&v, "test")["conclusion"], "success");
    // No check of s1 carries over.
    let base = format!("{}/pulls/{n}", w.repo_path());
    let (st, v) = w
        .server
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": s1, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(
        (st.as_u16(), v["error"]["code"].as_str()),
        (409, Some("head_moved"))
    );
}

#[tokio::test]
async fn required_ci_is_never_shown_as_success_or_no_checks() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    set_required(&w, &["test"]).await;
    // No pipeline on the base: the required check cannot run, and says so.
    let head = w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let v = checks(&w, n).await;
    assert_eq!(v["checks_expected"], true);
    let t = check(&v, "test");
    assert_eq!(
        (t["status"].as_str(), t["conclusion"].as_str()),
        (Some("completed"), Some("failure"))
    );
    assert!(
        t["details"].as_str().unwrap().contains(".branvil/ci.toml"),
        "{t}"
    );
    let base = format!("{}/pulls/{n}", w.repo_path());
    let merge = |head: String, base_sha: Value| {
        let (s, url, owner) = (&w.server, format!("{base}/merge"), w.owner.clone());
        async move {
            s.post(
                &url,
                &owner,
                json!({"expected_head": head, "expected_base": base_sha}),
            )
            .await
        }
    };
    let (st, v) = merge(head.clone(), p["base_sha"].clone()).await;
    assert_eq!(
        (st.as_u16(), v["error"]["code"].as_str()),
        (409, Some("not_mergeable")),
        "{v}"
    );

    // The pipeline lands (the setup PR is merged while no check is required).
    set_required(&w, &[]).await;
    w.server
        .post(&format!("{base}/close"), &w.aiko, json!({}))
        .await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        Some(&runner),
    )
    .await;
    runner.drain("success").await;
    set_required(&w, &["test"]).await;

    // A pending, failed, or cancelled required check is never success.
    sync_main(&w);
    let head = w.push_change("feature2", "g.txt", "1\n");
    let p2 = w.open_pull("feature2", json!([])).await;
    let n2 = p2["number"].as_u64().unwrap();
    let base2 = format!("{}/pulls/{n2}", w.repo_path());
    let merge2 = |c: Value| {
        let (s, url, owner, h) = (
            &w.server,
            format!("{base2}/merge"),
            w.owner.clone(),
            head.clone(),
        );
        async move {
            let (st, v) = s
                .post(
                    &url,
                    &owner,
                    json!({"expected_head": h, "expected_base": c}),
                )
                .await;
            (st.as_u16(), v)
        }
    };
    // pending (queued, then in progress)
    assert_eq!(check(&checks(&w, n2).await, "test")["status"], "queued");
    assert_eq!(merge2(p2["base_sha"].clone()).await.0, 409);
    let c = runner.claim().await.unwrap();
    assert_eq!(merge2(p2["base_sha"].clone()).await.0, 409);
    // failure
    runner.finish(&c, "failure").await;
    let t = check(&checks(&w, n2).await, "test").clone();
    assert_eq!(t["conclusion"], "failure");
    assert_eq!(merge2(p2["base_sha"].clone()).await.0, 409);
    // retry → queued attempt 2, then cancelled
    let id = t["attempt_id"].as_str().unwrap();
    let (st, a2) = w
        .server
        .post(
            &format!("{}/jobs/{id}/retry", w.repo_path()),
            &w.aiko,
            json!({}),
        )
        .await;
    assert_eq!(st, 200, "{a2}");
    assert_eq!(a2["attempt"], 2);
    assert_eq!(a2["status"], "queued");
    let (st, _) = w
        .server
        .post(
            &format!("{}/jobs/{id}/retry", w.repo_path()),
            &w.aiko,
            json!({}),
        )
        .await;
    assert_eq!(st, 409, "only the latest completed attempt can be retried");
    let (st, v) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/cancel",
                w.repo_path(),
                a2["id"].as_str().unwrap()
            ),
            &runner.token,
            json!({}),
        )
        .await;
    assert_eq!(st, 403, "{v}");
    let (st, cancelled) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/cancel",
                w.repo_path(),
                a2["id"].as_str().unwrap()
            ),
            &w.aiko,
            json!({}),
        )
        .await;
    assert_eq!(st, 200);
    assert_eq!(cancelled["conclusion"], "cancelled");
    let t = check(&checks(&w, n2).await, "test").clone();
    assert_eq!(
        (t["attempt"].as_u64(), t["conclusion"].as_str()),
        (Some(2), Some("cancelled"))
    );
    assert_eq!(merge2(p2["base_sha"].clone()).await.0, 409);
    assert!(
        runner.claim().await.is_none(),
        "a cancelled attempt is not handed out"
    );

    // A head that breaks its own pipeline gets a failing pipeline check.
    sync_main(&w);
    std::fs::write(
        w.work.join(".branvil/ci.toml"),
        "[[jobs]]\nname = \"test\"\nprivileged = true\n",
    )
    .unwrap();
    git_ok(&w.work, &["add", ".branvil/ci.toml"]);
    w.push_change("feature3", "h.txt", "1\n");
    let p3 = w.open_pull("feature3", json!([])).await;
    let v = checks(&w, p3["number"].as_u64().unwrap()).await;
    let pl = check(&v, "pipeline");
    assert_eq!(
        (pl["status"].as_str(), pl["conclusion"].as_str()),
        (Some("completed"), Some("failure"))
    );
    assert!(
        pl["details"].as_str().unwrap().contains("privileged"),
        "{pl}"
    );
    assert_eq!(check(&v, "test")["conclusion"], "failure");
    assert!(
        v["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["conclusion"] != "success")
    );

    // A head that drops the required check from its own pipeline runs what is
    // left, and still cannot be merged: the required check fails.
    sync_main(&w);
    std::fs::write(w.work.join(".branvil/ci.toml"), pipeline(&["lint"])).unwrap();
    git_ok(&w.work, &["add", ".branvil/ci.toml"]);
    let head4 = w.push_change("feature4", "i.txt", "1\n");
    let p4 = w.open_pull("feature4", json!([])).await;
    let n4 = p4["number"].as_u64().unwrap();
    let c = runner.claim().await.unwrap();
    assert_eq!(
        (c["sha"].as_str(), c["job"]["name"].as_str()),
        (Some(head4.as_str()), Some("lint"))
    );
    runner.finish(&c, "success").await;
    let v = checks(&w, n4).await;
    assert_eq!(check(&v, "lint")["conclusion"], "success");
    let t = check(&v, "test");
    assert_eq!(
        (t["status"].as_str(), t["conclusion"].as_str()),
        (Some("completed"), Some("failure"))
    );
    assert!(
        t["details"]
            .as_str()
            .unwrap()
            .contains("defines no job named test"),
        "{t}"
    );
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n4}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head": head4, "expected_base": p4["base_sha"]}),
        )
        .await;
    assert_eq!(
        (st.as_u16(), v["error"]["code"].as_str()),
        (409, Some("not_mergeable")),
        "{v}"
    );
}

#[tokio::test]
async fn a_commit_shows_its_checks_with_or_without_a_pull_request() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let commit_checks = |sha: &str| {
        let (s, path, token) = (
            &w.server,
            format!("{}/commits/{sha}/checks", w.repo_path()),
            w.aiko.clone(),
        );
        async move { s.ok(&path, &token).await }
    };
    // A commit without a pipeline has no checks, and none are expected.
    let initial = git_ok(&w.work, &["rev-parse", "HEAD"]);
    assert_eq!(
        commit_checks(&initial).await,
        json!({"sha": initial, "checks": [], "checks_expected": false, "run_registered": true})
    );

    // The default branch's new tip is followed from queued to its conclusion.
    let tip = land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    let v = commit_checks(&tip).await;
    assert_eq!(v["sha"], tip.as_str());
    assert_eq!(v["checks_expected"], true);
    assert_eq!(check(&v, "test")["status"], "queued");
    let c = runner.claim().await.unwrap();
    assert_eq!(c["sha"], tip.as_str());
    let v = commit_checks(&tip).await;
    assert_eq!(check(&v, "test")["status"], "in_progress");
    assert_eq!(check(&v, "test")["attempt_id"], c["attempt_id"]);
    assert_eq!(runner.finish(&c, "success").await.0, 200);
    let t = check(&commit_checks(&tip).await, "test").clone();
    assert_eq!(
        (t["status"].as_str(), t["conclusion"].as_str()),
        (Some("completed"), Some("success"))
    );
    assert!(
        t["details_url"].as_str().unwrap().contains("/-/jobs/att_"),
        "{t}"
    );

    // Required checks the commit's run lacks are listed as for a pull request.
    set_required(&w, &["test", "lint"]).await;
    let v = commit_checks(&tip).await;
    assert_eq!(check(&v, "lint")["conclusion"], "failure");

    // Only a full sha of a commit of the repository is found.
    for sha in [&tip[..12], &"0".repeat(40)] {
        let (st, v) = w
            .server
            .get(&format!("{}/commits/{sha}/checks", w.repo_path()), &w.aiko)
            .await;
        assert_eq!(st, 404, "{sha}: {v}");
    }
}

#[tokio::test]
async fn no_runner_means_startup_failure_not_waiting_forever() {
    let mut w = World::new().await;
    set_ci_config(&w, &[("runner_absent_secs", 2.into())]);
    w.restart(&[]);
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    sync_main(&w);
    w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    eventually("startup_failure", || {
        let w = &w;
        async move { check(&checks(w, n).await, "test")["conclusion"] == "startup_failure" }
    })
    .await;
    let t = check(&checks(&w, n).await, "test").clone();
    assert!(
        t["details"].as_str().unwrap().contains("No CI runner"),
        "{t}"
    );
    let diag = w.server.ok("/admin/diagnostics", &w.owner).await;
    assert_eq!(diag["runner"]["state"], "no_runner", "{diag}");
}

#[tokio::test]
async fn an_expired_lease_requeues_once_and_a_restart_handles_orphans() {
    let mut w = World::new().await;
    set_ci_config(
        &w,
        &[("lease_secs", 2.into()), ("runner_absent_secs", 600.into())],
    );
    w.restart(&[]);
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    sync_main(&w);
    w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let first = runner.claim().await.unwrap();
    // The runner goes silent; its lease expires and the job is queued again.
    eventually("re-queue", || {
        let w = &w;
        async move { check(&checks(w, n).await, "test")["attempt"] == 2 }
    })
    .await;
    let t = check(&checks(&w, n).await, "test").clone();
    assert_eq!(t["status"], "queued");
    let old = w
        .server
        .ok(
            &format!(
                "{}/jobs/{}",
                w.repo_path(),
                first["attempt_id"].as_str().unwrap()
            ),
            &w.aiko,
        )
        .await;
    assert_eq!(old["conclusion"], "cancelled");
    assert!(
        old["details"].as_str().unwrap().contains("Interrupted"),
        "{old}"
    );
    // The silent runner comes back: its result is refused.
    assert_eq!(runner.finish(&first, "success").await.0, 409);
    assert_eq!(runner.heartbeat(&first).await.0, 409);

    // Attempt 2 is running when the service stops; its lease runs out while
    // it is down. The restarted service interrupts it for good.
    let second = runner.claim().await.unwrap();
    assert_eq!(second["attempt"], 2);
    w.server.stop();
    tokio::time::sleep(std::time::Duration::from_secs(3)).await;
    w.restart(&[]);
    let runner = Runner {
        url: w.server.url.clone(),
        token: runner.token.clone(),
        http: reqwest::Client::new(),
    };
    eventually("interrupted twice", || {
        let w = &w;
        async move { check(&checks(w, n).await, "test")["conclusion"] == "cancelled" }
    })
    .await;
    let t = check(&checks(&w, n).await, "test").clone();
    assert_eq!(t["attempt"], 2);
    assert!(t["details"].as_str().unwrap().contains("twice"), "{t}");
    assert_eq!(runner.finish(&second, "success").await.0, 409);
    assert!(runner.claim().await.is_none());
    // The indexes still match the records after all of it.
    w.server.stop();
    let out = std::process::Command::new(BIN)
        .args(["admin", "check", "--data-dir"])
        .arg(&w.server.data)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
}

/// A pull request whose `test` is claimed and whose `lint` is queued after
/// it; returns the claimed attempt.
async fn running_test_and_queued_lint(w: &World, runner: &Runner) -> Value {
    land(
        w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test", "lint"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    sync_main(w);
    w.push_change("feature", "f.txt", "1\n");
    w.open_pull("feature", json!([])).await;
    let running = runner.claim().await.unwrap();
    assert_eq!(running["job"]["name"], "test");
    running
}

#[cfg(unix)]
#[tokio::test]
async fn a_stopping_service_gives_out_no_job_and_waits_for_the_running_ones() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let running = running_test_and_queued_lint(&w, &runner).await;
    w.server.signal("TERM");
    eventually("stopping", || {
        let w = &w;
        async move {
            w.server.ok("/admin/diagnostics", &w.owner).await["runner"]["state"] == "stopping"
        }
    })
    .await;
    // The queued job waits; the running one reports and finishes as usual.
    assert!(runner.claim().await.is_none());
    assert_eq!(runner.heartbeat(&running).await.0, 200);
    assert_eq!(runner.finish(&running, "success").await.0, 200);
    let status = w
        .server
        .exit_within(std::time::Duration::from_secs(10))
        .expect("still running after its last job finished");
    assert!(status.success(), "{status}");
    // The queued job runs after the next start.
    w.restart(&[]);
    let runner = Runner {
        url: w.server.url.clone(),
        token: runner.token.clone(),
        http: reqwest::Client::new(),
    };
    assert_eq!(runner.claim().await.unwrap()["job"]["name"], "lint");
}

#[cfg(unix)]
#[tokio::test]
async fn a_job_still_running_when_the_stop_wait_ends_is_queued_again_in_its_place() {
    let mut w = World::new().await;
    set_ci_config(&w, &[("stop_wait_secs", 1.into())]);
    w.restart(&[]);
    let runner = Runner::new(&w, "runner1").await;
    let running = running_test_and_queued_lint(&w, &runner).await;
    w.server.signal("TERM");
    let status = w
        .server
        .exit_within(std::time::Duration::from_secs(10))
        .expect("still running after the stop wait");
    // It says that it cut a job off.
    assert_eq!(status.code(), Some(3), "{status}");
    w.restart(&[]);
    let runner = Runner {
        url: w.server.url.clone(),
        token: runner.token.clone(),
        http: reqwest::Client::new(),
    };
    let again = runner.claim().await.unwrap();
    assert_eq!(
        (again["job"]["name"].as_str(), again["attempt"].as_u64()),
        (Some("test"), Some(2))
    );
    let old = w
        .server
        .ok(
            &format!(
                "{}/jobs/{}",
                w.repo_path(),
                running["attempt_id"].as_str().unwrap()
            ),
            &w.aiko,
        )
        .await;
    assert_eq!(old["conclusion"], "cancelled");
    let details = old["details"].as_str().unwrap();
    assert!(
        details.contains("Branvil stopped") && details.contains("in its place"),
        "{old}"
    );
    assert_eq!(runner.finish(&running, "success").await.0, 409);
}

#[cfg(unix)]
#[tokio::test]
async fn sigint_stops_a_service_that_waits_for_its_running_jobs_at_once() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    running_test_and_queued_lint(&w, &runner).await;
    w.server.signal("TERM");
    eventually("stopping", || {
        let w = &w;
        async move {
            w.server.ok("/admin/diagnostics", &w.owner).await["runner"]["state"] == "stopping"
        }
    })
    .await;
    w.server.signal("INT");
    let status = w
        .server
        .exit_within(std::time::Duration::from_secs(10))
        .expect("still waiting after SIGINT");
    assert!(status.success(), "{status}");
}

#[tokio::test]
async fn the_automatic_runner_starting_again_queues_the_jobs_its_predecessor_held_in_their_place() {
    let mut w = World::new().await;
    // Long leases: what follows is not their expiry.
    set_ci_config(&w, &[("lease_secs", 600.into())]);
    w.restart(&[]);
    // As the automatic runner, which dies with its service.
    let runner = Runner::new(&w, "branvil-runner").await;
    let held = running_test_and_queued_lint(&w, &runner).await;
    set_ci_config(&w, &[("runner", "auto".into())]);
    // A home of its own: no msb, so the new automatic runner concludes what
    // it claims as startup failures.
    let home = w.dir.path().join("home");
    std::fs::create_dir_all(&home).unwrap();
    w.restart(&[("HOME", home.to_str().unwrap())]);
    let url = format!(
        "{}/jobs/{}",
        w.repo_path(),
        held["attempt_id"].as_str().unwrap()
    );
    eventually("interrupted at the start", || {
        let (w, url) = (&w, &url);
        async move { w.server.ok(url, &w.aiko).await["conclusion"] == "cancelled" }
    })
    .await;
    let old = w.server.ok(&url, &w.aiko).await;
    let details = old["details"].as_str().unwrap();
    assert!(
        details.contains("automatic runner stopped") && details.contains("in its place"),
        "{old}"
    );
    // Its next attempt was taken before lint, queued after it.
    let sha = held["sha"].as_str().unwrap().to_string();
    let checks_url = format!("{}/commits/{sha}/checks", w.repo_path());
    eventually("both taken", || {
        let (w, url) = (&w, &checks_url);
        async move {
            w.server.ok(url, &w.aiko).await["checks"]
                .as_array()
                .is_some_and(|c| c.iter().all(|c| c["status"] == "completed"))
        }
    })
    .await;
    let list = w.server.ok(&checks_url, &w.aiko).await;
    let started = |name: &str| {
        let c = list["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == name)
            .unwrap()
            .clone();
        (
            c["attempt"].clone(),
            c["attempt_id"].as_str().unwrap().to_string(),
        )
    };
    let ((test_attempt, test_id), (_, lint_id)) = (started("test"), started("lint"));
    assert_eq!(test_attempt, 2);
    let at = |id: String| {
        let w = &w;
        async move {
            w.server
                .ok(&format!("{}/jobs/{id}", w.repo_path()), &w.aiko)
                .await["started_at"]
                .as_str()
                .unwrap()
                .to_string()
        }
    };
    let (test_at, lint_at) = (at(test_id).await, at(lint_id).await);
    assert!(test_at <= lint_at, "test {test_at}, lint {lint_at}");
    #[cfg(unix)]
    w.server.terminate(std::time::Duration::from_secs(10));
}

#[tokio::test]
async fn deleting_a_merged_head_keeps_the_jobs_of_the_commit_it_merged() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    // The head's job is still queued when it merges; the deletion of the head
    // (recorded with main's move) cancels nothing.
    land(
        &w,
        "add-ci",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    assert_eq!(runner.drain("success").await, 1);
    let test = check(&checks(&w, 1).await, "test").clone();
    assert_eq!(
        (test["attempt"].clone(), test["conclusion"].clone()),
        (json!(1), json!("success")),
        "{test}"
    );
}

#[tokio::test]
async fn a_merge_needs_success_on_the_exact_head_and_refuses_a_moved_base() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    set_required(&w, &["test"]).await;
    sync_main(&w);
    let s1 = w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    runner.drain("success").await;
    let base = format!("{}/pulls/{n}", w.repo_path());
    // A new head: the old success does not carry over.
    let s2 = w.push_change("feature", "f.txt", "2\n");
    let (st, v) = w
        .server
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": s2, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(
        (st.as_u16(), v["error"]["code"].as_str()),
        (409, Some("not_mergeable")),
        "{v}"
    );
    assert!(v["error"]["message"].as_str().unwrap().contains("test"));
    assert_ne!(s1, s2);
    assert_eq!(
        w.server
            .patch(&base, &w.owner, json!({"draft": true}))
            .await
            .0,
        200
    );
    runner.drain("success").await;
    // While this pull is a draft, another pull can take the queue's front
    // and move the base after the caller read it.
    let old_base = p["base_sha"].clone();
    land(&w, "other", &[("o.txt", "o\n")], Some(&runner)).await;
    assert_eq!(
        w.server
            .patch(&base, &w.owner, json!({"draft": false}))
            .await
            .0,
        200
    );
    runner.drain("success").await;
    let (st, v) = w
        .server
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": s2, "expected_base": old_base}),
        )
        .await;
    assert_eq!(
        (st.as_u16(), v["error"]["code"].as_str()),
        (409, Some("base_moved")),
        "{v}"
    );
    let pull = w.server.ok(&base, &w.aiko).await;
    assert_eq!(pull["merged"], false);
}

#[tokio::test]
async fn the_owner_alone_may_open_the_network_of_jobs() {
    let w = World::new().await;
    let repo = w.server.ok(&w.repo_path(), &w.aiko).await;
    let (st, v) = w
        .server
        .patch(
            &w.repo_path(),
            &w.aiko,
            json!({"revision": repo["revision"], "ci_network": true}),
        )
        .await;
    assert_eq!(st, 403, "{v}");
    let (st, v) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "ci_network": true}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["ci_network"], true);
}

#[tokio::test]
async fn ci_records_logs_and_artifacts_survive_a_backup_and_restore() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".branvil/ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    sync_main(&w);
    let head = w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let c = runner.claim().await.unwrap();
    runner
        .raw(
            reqwest::Method::POST,
            &c,
            "log",
            b"kept in the backup\n".to_vec(),
        )
        .await;
    let mut zip = b"PK\x05\x06".to_vec();
    zip.extend_from_slice(&[0; 18]);
    let (st, _) = runner
        .raw(reqwest::Method::PUT, &c, "artifacts/report", zip.clone())
        .await;
    assert_eq!(st, 200);
    assert_eq!(runner.finish(&c, "success").await.0, 200);
    eventually("the landing gate before backup", || async {
        checks(&w, n).await["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|check| check["name"] == "gate/test" && check["conclusion"] == "success")
    })
    .await;
    let before = checks(&w, n).await;

    let backup = w.dir.path().join("backup");
    let (st, v) = w
        .server
        .post("/admin/backup", &w.owner, json!({"to": backup}))
        .await;
    assert_eq!(st, 200, "{v}");
    let target = tempfile::tempdir().unwrap();
    let out = std::process::Command::new(BIN)
        .args(["restore", "--from"])
        .arg(&backup)
        .arg("--data-dir")
        .arg(target.path().join("data"))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let listen = w.server.url.trim_start_matches("http://").to_string();
    w.server.stop();
    w.server = Server::start_on(target.path(), &listen, &[]);
    let after = checks(&w, n).await;
    assert_eq!(after["checks"], before["checks"]);
    let id = check(&after, "test")["attempt_id"]
        .as_str()
        .unwrap()
        .to_string();
    let log = w
        .server
        .ok(&format!("{}/jobs/{id}/log", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(log["log"], "kept in the backup\n");
    let resp = w
        .server
        .http
        .get(format!(
            "{}/api/v1{}/commits/{head}/artifacts/report",
            w.server.url,
            w.repo_path()
        ))
        .bearer_auth(&w.aiko)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    assert_eq!(resp.bytes().await.unwrap().to_vec(), zip);
}
