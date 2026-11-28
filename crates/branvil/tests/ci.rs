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
pub(crate) const DIGEST: &str =
    "sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8";

#[tokio::test]
async fn records_test_failures_when_a_runner_finishes_its_attempt() {
    use std::io::Write;

    let w = World::new().await;
    let runner = Runner::new(&w, "junit-runner").await;
    let mut ci = pipeline(&["test"]);
    ci.push_str("artifacts=[{name='results', paths=['out/junit.xml'], junit='out/junit.xml'}]\n");
    let head = land(
        &w,
        "test-failure-history",
        &[(".guildbotics-ci.toml", &ci)],
        None,
    )
    .await;
    let attempt = runner.claim_head(&head).await;
    assert_eq!(
        attempt["job"]["artifacts"],
        json!([{"name":"results","paths":["out/junit.xml"]}]),
        "older runners receive the same artifact contract"
    );
    let mut zip = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    zip.start_file("other.xml", zip::write::SimpleFileOptions::default())
        .unwrap();
    zip.write_all(b"<testsuite><testcase name='ignored'><failure/></testcase></testsuite>")
        .unwrap();
    zip.start_file("out/junit.xml", zip::write::SimpleFileOptions::default())
        .unwrap();
    zip.write_all(
        b"<testsuite><testcase name='suite::case[1] &amp; more'><failure/></testcase></testsuite>",
    )
    .unwrap();
    let archive = zip.finish().unwrap().into_inner();
    let (status, _) = runner
        .raw(reqwest::Method::PUT, &attempt, "artifacts/results", archive)
        .await;
    assert_eq!(status, 200);
    let lookup = format!(
        "{}/test-failures?name=suite%3A%3Acase%5B1%5D+%26+more",
        w.repo_path()
    );
    assert_eq!(w.server.ok(&lookup, &w.ken).await, json!([]));
    let (status, finished) = runner.finish(&attempt, "failure").await;
    assert_eq!(status, 200, "{finished}");
    let id = attempt["attempt_id"].as_str().unwrap();
    let expected = json!([{
        "attempt_id": id,
        "job": "test",
        "runner": "junit-runner",
        "sha": head,
        "finished_at": finished["finished_at"],
        "url": format!("{}/workspaces/acme/repos/demo/jobs/{id}", w.server.url),
    }]);
    std::fs::remove_file(
        w.server
            .data
            .join("ci/artifacts")
            .join(id)
            .join("results.zip"),
    )
    .unwrap();
    assert_eq!(w.server.ok(&lookup, &w.ken).await, expected);
    let ignored = format!("{}/test-failures?name=ignored", w.repo_path());
    assert_eq!(w.server.ok(&ignored, &w.ken).await, json!([]));
    assert_eq!(w.server.get(&lookup, &runner.token).await.0, 403);
}

#[tokio::test]
async fn ci_job_lists_live_in_the_pipeline_not_in_the_repository_settings() {
    let w = World::new().await;
    let before = w.server.ok(&w.repo_path(), &w.owner).await;
    for field in ["post_merge_jobs", "required_checks"] {
        assert!(before.get(field).is_none(), "{before}");
        let (code, body) = w
            .server
            .patch(
                &w.repo_path(),
                &w.owner,
                json!({"revision": before["revision"], field: ["slow"]}),
            )
            .await;
        assert_eq!(code, 400, "{body}");
    }
    assert_eq!(w.server.ok(&w.repo_path(), &w.owner).await, before);
}

#[tokio::test]
async fn a_head_cannot_skip_its_base_s_jobs_but_decides_for_the_jobs_it_adds() {
    let w = World::new().await;
    let runner = Runner::new(&w, "post-merge-runner").await;
    land(
        &w,
        "setup",
        &[(
            ".guildbotics-ci.toml",
            &mark(&pipeline(&["lint", "release"]), "post_merge", &["release"]),
        )],
        None,
    )
    .await;
    runner.drain("success").await;
    // The head marks the other way round every job its base has.
    let head = w.push_change(
        "feature",
        ".guildbotics-ci.toml",
        &mark(
            &pipeline(&["lint", "release", "added"]),
            "post_merge",
            &["lint", "added"],
        ),
    );
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let lint = runner.next_claim().await;
    assert_eq!(
        (&lint["job"]["name"], &lint["sha"]),
        (&json!("lint"), &json!(head))
    );
    runner.finish(&lint, "success").await;
    let report = checks(&w, n).await;
    for job in ["release", "added"] {
        let skipped = check(&report, job);
        assert_eq!(skipped["conclusion"], "skipped", "{report}");
        assert_eq!(skipped["path_skip"]["reason"], "post_merge", "{report}");
    }
    assert_eq!(check(&report, "lint")["conclusion"], "success", "{report}");
}

#[tokio::test]
async fn post_merge_jobs_skip_on_heads_but_execute_after_merge() {
    let mut config: toml::Table =
        toml::from_str(include_str!("../../../.guildbotics-ci.toml")).unwrap();
    let jobs = config["jobs"].as_array_mut().unwrap();
    jobs.retain(|job| job["name"].as_str() == Some("client-linux"));
    assert_eq!(jobs.len(), 1);
    assert_eq!(jobs[0]["post_merge"].as_bool(), Some(true));
    assert!(
        jobs[0]["commands"][0]
            .as_str()
            .unwrap()
            .contains("--no-default-features --target x86_64-unknown-linux-gnu --release")
    );
    assert_eq!(
        jobs[0]["artifacts"][0]["name"].as_str(),
        Some("client-linux")
    );
    assert_eq!(
        jobs[0]["artifacts"][0]["paths"].as_array().unwrap(),
        &[
            "client-release/guildbotics",
            "client-release/sha256",
            "client-release/commit"
        ]
        .map(|path| toml::Value::String(path.into()))
    );
    // The scripted runner advertises the fixture image; it does not execute commands.
    jobs[0]["image"] = toml::Value::String(IMAGE.into());
    let pipeline = format!(
        "{}\n{}",
        pipeline(&["fast"]),
        toml::to_string(&config).unwrap()
    );
    let w = World::new().await;
    let runner = Runner::new(&w, "post-merge-runner").await;
    let head = w.push_change("feature", ".guildbotics-ci.toml", &pipeline);
    let commit_checks = w
        .server
        .ok(
            &format!("{}/commits/{head}/checks", w.repo_path()),
            &w.owner,
        )
        .await;
    assert_eq!(commit_checks["run_registered"], false);
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let fast = runner.next_claim().await;
    assert_eq!(fast["job"]["name"], "fast");
    runner.finish(&fast, "success").await;
    let report = checks(&w, n).await;
    let release = check(&report, "client-linux");
    assert_eq!(release["conclusion"], "skipped");
    assert_eq!(release["path_skip"]["reason"], "post_merge");
    assert_eq!(release.get("runner"), Some(&Value::Null));
    let log = w
        .server
        .ok(
            &format!(
                "{}/jobs/{}/log",
                w.repo_path(),
                release["attempt_id"].as_str().unwrap()
            ),
            &w.owner,
        )
        .await;
    assert_eq!(log["log"], "[guildbotics] skipped: post-merge only\n");
    let (status, result) = w
        .server
        .post(
            &format!("{}/pulls/{n}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head":p["head_sha"],"expected_base":p["base_sha"]}),
        )
        .await;
    assert_eq!(status, 200, "{result}");
    let release = runner.next_claim().await;
    assert_eq!(release["sha"], head);
    assert_eq!(release["job"]["name"], "client-linux");
    assert!(!release["reuse"].is_object());
    runner.finish(&release, "success").await;
}

#[tokio::test]
async fn merged_checks_keep_the_decision_when_the_receiving_branch_reselects_jobs() {
    let w = World::new().await;
    let runner = Runner::new(&w, "merge-checks-runner").await;
    let config = format!(
        "{}paths=['src/**']\n{}",
        required_pipeline(&["rust"]),
        mark(&pipeline(&["release"]), "post_merge", &["release"])
    );
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &config)],
        Some(&runner),
    )
    .await;
    runner.drain("success").await;

    std::fs::create_dir_all(w.work.join("src")).unwrap();
    let collecting_tip = w.push_change("ticket/10", "src/code.txt", "parent change");
    let collecting = w.open_pull("ticket/10", json!([])).await;
    let collecting_n = collecting["number"].as_u64().unwrap();
    let child_head = w.push_change("ticket/11", "notes.md", "child change");
    let (status, child) = w
        .server
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head": "ticket/11", "base": "ticket/10", "title": "Child"}),
        )
        .await;
    assert_eq!(status, 201, "{child}");
    let child_n = child["pull"]["number"].as_u64().unwrap();
    let listed = |number| {
        let path = format!(
            "{}/pulls?state=open&post_merge=active&sort=readiness&limit=1",
            w.repo_path()
        );
        let w = &w;
        async move {
            let list = w.server.ok(&path, &w.owner).await;
            assert_eq!(list["items"][0]["number"], number);
            list["items"][0]["post_merge_status"].clone()
        }
    };

    let merge = |number, head: String, base: Value| {
        let path = format!("{}/pulls/{number}/merge", w.repo_path());
        let body = json!({"expected_head": head, "expected_base": base});
        let w = &w;
        async move {
            let (status, result) = w.server.post(&path, &w.owner, body).await;
            assert_eq!(status, 200, "{result}");
        }
    };
    let child_before = checks(&w, child_n).await;
    let skipped = check(&child_before, "rust");
    assert_eq!(child_before["required_checks"], "passed");
    assert_eq!(skipped["path_skip"]["reason"], "paths");
    merge(child_n, child_head.clone(), json!(collecting_tip)).await;

    let new_rust = runner.next_claim().await;
    assert_eq!(new_rust["job"]["name"], "rust");
    assert_eq!(new_rust["sha"], child_head);
    let after = checks(&w, child_n).await;
    assert_eq!(after["checks"], json!([]));
    assert!(after["required_checks"].is_null());
    let decision = &after["merge_checks"];
    assert_eq!(decision["source"], "merge");
    assert_eq!(decision["head_sha"], child_head);
    assert_eq!(decision["base_sha"], collecting_tip);
    let rust = check(decision, "rust");
    assert_eq!(rust["conclusion"], "skipped");
    assert_eq!(rust["attempt"], 1);
    assert_eq!(rust["attempt_id"], skipped["attempt_id"]);
    let confirmation = &after["post_merge_checks"];
    assert_eq!(confirmation["status"], "pending");
    assert_eq!(confirmation["branch"], "ticket/10");
    assert_eq!(confirmation["pulls"], json!([collecting_n]));
    assert_eq!(confirmation["sha"], child_head);
    let rust = check(confirmation, "rust");
    assert_eq!(rust["status"], "in_progress");
    assert_eq!(rust["attempt"], 2);
    assert_eq!(rust["attempt_id"], new_rust["attempt_id"]);

    assert_eq!(listed(child_n).await, "pending");
    let normal_open = w
        .server
        .ok(&format!("{}/pulls?state=open", w.repo_path()), &w.owner)
        .await;
    assert_eq!(normal_open["items"].as_array().unwrap().len(), 1);
    assert_eq!(normal_open["items"][0]["number"], collecting_n);
    assert_eq!(runner.finish(&new_rust, "failure").await.0, 200);
    assert_eq!(listed(child_n).await, "failing");
    assert_eq!(
        checks(&w, child_n).await["post_merge_checks"]["status"],
        "failing"
    );
    let (status, result) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/retry",
                w.repo_path(),
                new_rust["attempt_id"].as_str().unwrap()
            ),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(status, 200, "{result}");
    let new_rust = runner.next_claim().await;
    assert_eq!(runner.finish(&new_rust, "success").await.0, 200);
    assert!(listed(collecting_n).await.is_null());
    assert_eq!(
        checks(&w, child_n).await["post_merge_checks"]["status"],
        "passing"
    );
    merge(
        collecting_n,
        child_head.clone(),
        collecting["base_sha"].clone(),
    )
    .await;
    let release = runner.next_claim().await;
    assert_eq!(release["job"]["name"], "release");
    let after = checks(&w, collecting_n).await;
    let decision = &after["merge_checks"];
    assert_eq!(decision["checks"].as_array().unwrap().len(), 1);
    assert_eq!(check(decision, "rust")["conclusion"], "success");
    assert_eq!(
        check(decision, "rust")["attempt_id"],
        new_rust["attempt_id"]
    );
    let confirmation = &after["post_merge_checks"];
    assert_eq!(confirmation["branch"], "main");
    assert_eq!(confirmation["pulls"], json!([]));
    assert_eq!(confirmation["sha"], child_head);
    let release_check = check(confirmation, "release");
    assert_eq!(release_check["status"], "in_progress");
    assert_eq!(release_check["attempt_id"], release["attempt_id"]);
    assert_eq!(listed(collecting_n).await, "pending");
    assert_eq!(
        checks(&w, child_n).await["merge_checks"]["checks"][0]["attempt_id"],
        skipped["attempt_id"]
    );
}

#[tokio::test]
async fn a_new_collection_merge_retires_the_previous_merge_status() {
    let w = World::new().await;
    let runner = Runner::new(&w, "collection-checks-runner").await;
    let config = format!("{}paths=['src/**']\n", required_pipeline(&["rust"]));
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &config)],
        Some(&runner),
    )
    .await;
    runner.drain("success").await;
    std::fs::create_dir_all(w.work.join("src")).unwrap();
    let base = w.push_change("ticket/10", "src/code.txt", "parent change");
    w.open_pull("ticket/10", json!([])).await;

    let merge_child = |branch: &'static str, base: String| {
        let w = &w;
        async move {
            let head = w.push_change(branch, "notes.md", branch);
            let pulls = format!("{}/pulls", w.repo_path());
            let (status, child) = w
                .server
                .post(
                    &pulls,
                    &w.aiko,
                    json!({"head": branch, "base": "ticket/10", "title": branch}),
                )
                .await;
            assert_eq!(status, 201, "{child}");
            let n = child["pull"]["number"].as_u64().unwrap();
            let (status, result) = w
                .server
                .post(
                    &format!("{pulls}/{n}/merge"),
                    &w.owner,
                    json!({"expected_head": head, "expected_base": base}),
                )
                .await;
            assert_eq!(status, 200, "{result}");
            (n, head)
        }
    };
    let (first, head) = merge_child("ticket/11", base).await;
    let running = runner.next_claim().await;
    assert_eq!(running["sha"], head);
    assert_eq!(
        checks(&w, first).await["post_merge_checks"]["status"],
        "pending"
    );
    let (second, _) = merge_child("ticket/12", head).await;

    let (status, heartbeat) = runner.heartbeat(&running).await;
    assert_eq!(status, 200, "{heartbeat}");
    assert_eq!(heartbeat["cancel"], true);
    let (status, stopped) = runner.finish(&running, "success").await;
    assert_eq!(status, 200, "{stopped}");
    assert_eq!(stopped["conclusion"], "cancelled");
    let old = checks(&w, first).await;
    assert_eq!(
        check(&old["post_merge_checks"], "rust")["conclusion"],
        "cancelled"
    );
    assert!(old["post_merge_checks"]["status"].is_null(), "{old}");
    assert_eq!(
        checks(&w, second).await["post_merge_checks"]["status"],
        "pending"
    );
    let list = w
        .server
        .ok(
            &format!(
                "{}/pulls?state=open&post_merge=active&sort=readiness",
                w.repo_path()
            ),
            &w.owner,
        )
        .await;
    assert_eq!(list["items"][0]["number"], second);
    assert!(
        !list["items"]
            .as_array()
            .unwrap()
            .iter()
            .any(|p| p["number"] == first)
    );
}

#[tokio::test]
async fn job_and_check_responses_identify_the_runner_that_claimed_each_attempt() {
    let w = World::new().await;
    let head = w.push_change(
        "feature",
        ".guildbotics-ci.toml",
        &pipeline(&["test", "lint"]),
    );
    let commit_url = format!("{}/commits/{head}/checks", w.repo_path());
    let unregistered = w.server.ok(&commit_url, &w.aiko).await;
    assert_eq!(unregistered["run_registered"], false);
    assert_eq!(unregistered["checks"], json!([]));

    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let before = checks(&w, n).await;
    let job_url = |check: &Value| {
        format!(
            "{}/jobs/{}",
            w.repo_path(),
            check["attempt_id"].as_str().unwrap()
        )
    };
    for name in ["test", "lint"] {
        let queued = check(&before, name);
        assert_eq!(queued["status"], "queued");
        assert_eq!(queued.get("runner"), Some(&Value::Null));
        let attempt = w.server.ok(&job_url(queued), &w.aiko).await;
        assert_eq!(attempt.get("runner"), Some(&Value::Null));
    }

    let linux = Runner::new(&w, "linux-runner").await;
    let windows = Runner::new(&w, "windows-runner").await;
    let finished = linux.next_claim().await;
    assert_eq!(linux.finish(&finished, "success").await.0, 200);
    let running = windows.next_claim().await;
    let pull_checks = checks(&w, n).await;
    let commit_checks = w.server.ok(&commit_url, &w.aiko).await;
    for (claim, name, status) in [
        (finished, "linux-runner", "completed"),
        (running, "windows-runner", "in_progress"),
    ] {
        let job = claim["job"]["name"].as_str().unwrap();
        let projected = check(&pull_checks, job);
        assert_eq!(projected["attempt_id"], claim["attempt_id"]);
        assert_eq!(projected["status"], status);
        assert_eq!(projected["runner"], name);
        assert_eq!(check(&commit_checks, job)["runner"], name);
        let attempt = w.server.ok(&job_url(projected), &w.aiko).await;
        assert_eq!(attempt["runner"], name);
    }
}

#[tokio::test]
async fn check_responses_follow_claim_order_and_move_a_retry_to_its_new_position() {
    let w = World::new().await;
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let (status, body) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "required_approvals": 1}),
        )
        .await;
    assert_eq!(status, 200, "{body}");
    let runner = Runner::new(&w, "ordering-runner").await;
    let head = w.push_change(
        "feature",
        ".guildbotics-ci.toml",
        &pipeline(&["z", "a", "m"]),
    );
    let p = w.open_pull("feature", json!([])).await;
    let checks_url = format!("{}/pulls/{}/checks", w.repo_path(), p["number"]);
    let names = |v: &Value| {
        v["checks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["name"].as_str().unwrap().to_string())
            .collect::<Vec<_>>()
    };
    let before = w.server.ok(&checks_url, &w.aiko).await;
    assert_eq!(names(&before), ["z", "a", "m"]);
    let first = runner.next_claim().await;
    assert_eq!(before["checks"][0]["attempt_id"], first["attempt_id"]);
    assert_eq!(runner.finish(&first, "success").await.0, 200);
    let (status, retry) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/retry",
                w.repo_path(),
                first["attempt_id"].as_str().unwrap()
            ),
            &w.aiko,
            json!({}),
        )
        .await;
    assert_eq!(status, 200, "{retry}");
    let after = w.server.ok(&checks_url, &w.aiko).await;
    assert_eq!(names(&after), ["a", "m", "z"]);
    let commit = w
        .server
        .ok(&format!("{}/commits/{head}/checks", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(names(&commit), names(&after));
    let next = runner.next_claim().await;
    assert_eq!(after["checks"][0]["attempt_id"], next["attempt_id"]);
}

#[tokio::test]
async fn previous_schema_upgrade_preserves_records() {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let ci = format!(
        "{}{}paths=['src/**']\n",
        required_pipeline(&["test"]),
        pipeline(&["lint"])
    );
    let head = land(
        &w,
        "setup",
        &[("src/a.txt", "a"), (".guildbotics-ci.toml", &ci)],
        None,
    )
    .await;
    let claimed = [runner.claim().await.unwrap(), runner.claim().await.unwrap()];
    for c in &claimed {
        let conclusion = if c["job"]["name"] == "test" {
            "cancelled"
        } else {
            "success"
        };
        assert_eq!(runner.finish(c, conclusion).await.0, 200);
    }
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let (status, result) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision":repo["revision"], "responder":"aiko"}),
        )
        .await;
    assert_eq!(status, 200, "{result}");
    w.push_change("docs-upgrade", "notes.md", "documentation");
    w.open_pull("docs-upgrade", json!([])).await;
    let merge_checks = checks(&w, 1).await["merge_checks"].clone();
    // The upgrade preserves the start time of existing tickets.
    let started = w.ticket("Started").await;
    let board = format!(
        "{}/tickets/{}/board",
        w.repo_path(),
        started["key"].as_str().unwrap()
    );
    let (status, moved) = w
        .server
        .put(&board, &w.aiko, json!({"lane": "in_progress"}))
        .await;
    assert_eq!(status, 200, "{moved}");
    assert!(moved["started_at"].is_string(), "{moved}");
    w.server.stop();
    let data = w.server.data.clone();
    let db = data.join("branvil.redb");
    let admin = |what: &str| {
        let out = branvil_command()
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
    // The successful execution of the job with paths is found by its inputs,
    // and reindexing rebuilds exactly that.
    let indexed = tables(&db)["ix_ci_inputs"].clone();
    assert_eq!(indexed.len(), 1);
    assert_eq!(admin("reindex").0, 0);
    assert_eq!(tables(&db)["ix_ci_inputs"], indexed);
    assert!(
        records(&db, "ci_attempts")
            .iter()
            .any(|a| a["inputs"].is_string())
    );
    as_previous_schema(&db);
    let before = tables(&db);
    assert!(before.contains_key("ticket_moves"));
    assert!(before.contains_key("ci_test_failures"));
    assert!(
        admin("check")
            .1
            .contains("guildbotics server hub-data upgrade")
    );
    let mut attempts = records(&db, "ci_attempts");
    let (code, output) = admin("upgrade");
    assert_eq!(code, 0, "{output}");
    assert!(
        output.contains(&format!(
            "from schema version {PREVIOUS_SCHEMA_VERSION} to {}",
            PREVIOUS_SCHEMA_VERSION + 1
        )),
        "{output}"
    );
    assert!(!attempts.is_empty());
    for attempt in &mut attempts {
        attempt["ci_disabled_runners"] = json!([]);
    }
    assert_eq!(records(&db, "ci_attempts"), attempts);
    let upgraded = tables(&db);
    assert!(!upgraded["ix_ci_pull"].is_empty());
    let unchanged = |mut snapshot: Tables| {
        snapshot.retain(|name, _| !matches!(name.as_str(), "meta" | "ci_attempts"));
        snapshot
    };
    let after = unchanged(upgraded.clone());
    let expected = unchanged(before.clone());
    assert_eq!(
        after.keys().collect::<Vec<_>>(),
        expected.keys().collect::<Vec<_>>()
    );
    for (name, rows) in expected {
        assert!(after[&name] == rows, "upgrade changed table {name}");
    }
    assert_eq!(records(&db, "repos")[0]["responder"], "aiko");
    let runs = records(&db, "ci_runs");
    let run = runs.iter().find(|r| r["sha"] == head).unwrap();
    assert_eq!(run["protected_branches"], json!(["main"]));
    let backup = previous_schema_backup(&data);
    assert!(tables(&backup) == before, "the backup changed");
    let (code, output) = admin("check");
    assert_eq!(code, 0, "{output}");
    assert!(output.contains("indexes: ok"), "{output}");
    // A successful check clears the upgrade marker in meta.
    let checked = tables(&db);
    assert_eq!(admin("upgrade").0, 0);
    assert!(
        tables(&db) == checked,
        "a second upgrade changed the database"
    );
    w.restart(&[]);
    assert_eq!(checks(&w, 1).await["merge_checks"], merge_checks);
}

#[tokio::test]
async fn protected_required_retries_are_visible_as_queued_checks_until_exhausted() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let head = land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &required_pipeline(&["test"]))],
        None,
    )
    .await;
    let alternate = Runner::new(&w, "runner2").await;
    let root = runner.claim().await.unwrap();
    let mut current = root.clone();
    let mut runners = [&runner, &alternate].into_iter().cycle();
    let mut selected = runners.next().unwrap();
    for n in 1..=4 {
        let (st, result) = selected.finish(&current, "failure").await;
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
            assert!(selected.claim().await.is_none());
            selected = runners.next().unwrap();
            current = selected.claim().await.unwrap();
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
    let retry_url = format!(
        "{}/jobs/{}/retry",
        w.repo_path(),
        current["attempt_id"].as_str().unwrap()
    );
    assert_eq!(
        w.server
            .post(&retry_url, &w.owner, json!({"runner":"runner1"}))
            .await
            .0,
        409
    );
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

#[tokio::test]
async fn pr_retry_routes_only_to_selected_runners_and_reports_recovery() {
    let w = World::new().await;
    let first = Runner::new(&w, "runner1").await;
    let second = Runner::new(&w, "runner2").await;
    let third = Runner::new(&w, "runner3").await;
    assert!(second.claim().await.is_none());
    assert!(third.claim().await.is_none());
    let head = w.push_change("feature", ".guildbotics-ci.toml", &pipeline(&["test"]));
    w.open_pull("feature", json!([])).await;
    let a = first.next_claim().await;
    assert_eq!(a["sha"], head);
    assert_eq!(first.finish(&a, "failure").await.0, 200);

    let job_url = |id: &Value| format!("{}/jobs/{}", w.repo_path(), id.as_str().unwrap());
    let url = job_url(&a["attempt_id"]);
    let view = w.server.ok(&url, &w.aiko).await;
    assert_eq!(
        view["retry_runners"],
        json!(["runner1", "runner2", "runner3"])
    );
    for selection in [
        json!({"runner":"unknown"}),
        json!({"runner":"runner2", "exclude_runners":["RUNNER2"]}),
    ] {
        assert_eq!(
            w.server
                .post(&format!("{url}/retry"), &w.aiko, selection)
                .await
                .0,
            400
        );
    }

    let (status, pinned) = w
        .server
        .post(
            &format!("{url}/retry"),
            &w.aiko,
            json!({"runner":"runner2"}),
        )
        .await;
    assert_eq!(status, 200, "{pinned}");
    assert_eq!(pinned["retry_decision"]["requested_by"], "aiko");
    assert_eq!(pinned["retry_decision"]["selection"]["runner"], "runner2");
    assert!(first.claim().await.is_none());
    assert!(third.claim().await.is_none());
    let b = second.next_claim().await;
    assert_eq!(b["attempt_id"], pinned["id"]);
    assert_eq!(second.finish(&b, "failure").await.0, 200);

    let (status, excluded) = w
        .server
        .post(
            &format!("{}/retry", job_url(&b["attempt_id"])),
            &w.aiko,
            json!({"exclude_runners":["runner1", "runner2"]}),
        )
        .await;
    assert_eq!(status, 200, "{excluded}");
    assert_eq!(
        excluded["retry_decision"]["selection"]["exclude_runners"],
        json!(["runner1", "runner2"])
    );
    assert!(first.claim().await.is_none());
    assert!(second.claim().await.is_none());
    let c = third.next_claim().await;
    assert_eq!(c["attempt_id"], excluded["id"]);
    let (status, passed) = third.finish(&c, "success").await;
    assert_eq!(status, 200, "{passed}");
    let recovery = json!({"failed_attempt":b["attempt_id"], "failed_runner":"runner2", "successful_runner":"runner3"});
    assert_eq!(passed["runner_recovery"], recovery);
    let check_view = w
        .server
        .ok(&format!("{}/commits/{head}/checks", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(check(&check_view, "test")["runner_recovery"], recovery);
}

#[tokio::test]
async fn a_jobs_attempts_list_every_try_of_that_job_on_the_commit() {
    let w = World::new().await;
    let first = Runner::new(&w, "runner1").await;
    let second = Runner::new(&w, "runner2").await;
    assert!(second.claim().await.is_none());
    w.push_change(
        "feature",
        ".guildbotics-ci.toml",
        &pipeline(&["test", "lint"]),
    );
    w.open_pull("feature", json!([])).await;
    let mut claims = [first.next_claim().await, first.next_claim().await];
    claims.sort_by_key(|c| c["job"]["name"] != "test");
    let [failed, lint] = claims;
    assert_eq!(first.finish(&failed, "failure").await.0, 200);
    assert_eq!(first.finish(&lint, "success").await.0, 200);
    let attempts_url =
        |id: &Value| format!("{}/jobs/{}/attempts", w.repo_path(), id.as_str().unwrap());
    let (status, retry) = w
        .server
        .post(
            &format!(
                "{}/jobs/{}/retry",
                w.repo_path(),
                failed["attempt_id"].as_str().unwrap()
            ),
            &w.aiko,
            json!({"runner": "runner2"}),
        )
        .await;
    assert_eq!(status, 200, "{retry}");
    let queued = w.server.ok(&attempts_url(&retry["id"]), &w.aiko).await;
    assert_eq!(queued[1]["status"], "queued");
    assert_eq!(queued[1]["runner"], Value::Null);
    assert_eq!(queued[1]["started_at"], Value::Null);

    let rerun = second.next_claim().await;
    assert_eq!(rerun["attempt_id"], retry["id"]);
    assert_eq!(second.finish(&rerun, "success").await.0, 200);
    for id in [&failed["attempt_id"], &retry["id"]] {
        let list = w.server.ok(&attempts_url(id), &w.aiko).await;
        let list = list.as_array().unwrap();
        assert_eq!(list.len(), 2, "{list:?}");
        for (item, (attempt, runner, conclusion)) in list.iter().zip([
            (&failed, "runner1", "failure"),
            (&rerun, "runner2", "success"),
        ]) {
            let id = attempt["attempt_id"].as_str().unwrap();
            assert_eq!(item["id"], id);
            assert_eq!(item["runner"], runner);
            assert_eq!(item["status"], "completed");
            assert_eq!(item["conclusion"], conclusion);
            assert!(item["started_at"].is_string() && item["finished_at"].is_string());
            assert!(
                item["url"]
                    .as_str()
                    .unwrap()
                    .ends_with(&format!("/jobs/{id}"))
            );
        }
        assert_eq!(list[0]["attempt"], 1);
        assert_eq!(list[1]["attempt"], 2);
    }
    let lint_only = w
        .server
        .ok(&attempts_url(&lint["attempt_id"]), &w.aiko)
        .await;
    assert_eq!(lint_only.as_array().unwrap().len(), 1);
    assert_eq!(lint_only[0]["id"], lint["attempt_id"]);
    let missing = format!(
        "{}/jobs/att_01JAMISSINGATTEMPT00000000/attempts",
        w.repo_path()
    );
    assert_eq!(w.server.get(&missing, &w.aiko).await.0, 404);
}

#[tokio::test]
async fn a_pulls_attempts_list_every_head_in_the_order_they_were_queued() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let first_head = w.push_change("feature", ".guildbotics-ci.toml", &pipeline(&["test"]));
    let pull = w.open_pull("feature", json!([])).await;
    let first = runner.next_claim().await;
    assert_eq!(runner.finish(&first, "failure").await.0, 200);
    let second_head = w.push_change("feature", "second.txt", "second\n");
    let second = runner.next_claim().await;
    assert_eq!(runner.finish(&second, "success").await.0, 200);

    let url = format!("{}/pulls/{}/attempts", w.repo_path(), pull["number"]);
    let list = w.server.ok(&url, &w.aiko).await;
    let list = list.as_array().unwrap();
    assert_eq!(list.len(), 2, "{list:?}");
    for (item, (claim, sha, conclusion)) in list.iter().zip([
        (&first, &first_head, "failure"),
        (&second, &second_head, "success"),
    ]) {
        assert_eq!(item["id"], claim["attempt_id"]);
        assert_eq!(item["job"], "test");
        assert_eq!(&item["sha"], sha.as_str());
        assert_eq!(item["runner"], "runner1");
        assert_eq!(item["conclusion"], conclusion);
        let queued = item["created_at"].as_str().unwrap();
        assert!(queued <= item["started_at"].as_str().unwrap(), "{item}");
    }
    assert!(list[0]["created_at"].as_str() < list[1]["created_at"].as_str());
    let missing = format!("{}/pulls/999/attempts", w.repo_path());
    assert_eq!(w.server.get(&missing, &w.aiko).await.0, 404);
}

#[tokio::test]
async fn a_pulls_attempts_keep_a_head_it_replaced() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let replaced = w.push_change("feature", ".guildbotics-ci.toml", &pipeline(&["test"]));
    let pull = w.open_pull("feature", json!([])).await;
    let first = runner.next_claim().await;
    assert_eq!(runner.finish(&first, "failure").await.0, 200);
    // Amended: the first head is no longer one of the pull request's commits.
    git_ok(&w.work, &["reset", "-q", "--hard", "HEAD~1"]);
    let head = w.push_change(
        "feature",
        ".guildbotics-ci.toml",
        &pipeline(&["test", "lint"]),
    );
    let commits = w
        .server
        .ok(
            &format!("{}/pulls/{}/commits", w.repo_path(), pull["number"]),
            &w.aiko,
        )
        .await;
    assert!(
        commits["items"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["sha"] != replaced.as_str()),
        "{commits}"
    );
    let url = format!("{}/pulls/{}/attempts", w.repo_path(), pull["number"]);
    let list = w.server.ok(&url, &w.aiko).await;
    let shas: Vec<_> = list.as_array().unwrap().iter().map(|a| &a["sha"]).collect();
    assert_eq!(shas[0], replaced.as_str(), "{list}");
    assert!(shas[1..].iter().all(|sha| *sha == head.as_str()), "{list}");
    assert_eq!(list[0]["id"], first["attempt_id"]);
    assert_eq!(list[0]["conclusion"], "failure");
}

#[tokio::test]
async fn a_closed_pulls_attempts_leave_out_later_work_on_its_branch() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let own = w.push_change("feature", ".guildbotics-ci.toml", &pipeline(&["test"]));
    let closed = w.open_pull("feature", json!([])).await;
    let first = runner.next_claim().await;
    assert_eq!(runner.finish(&first, "success").await.0, 200);
    let (status, body) = w
        .server
        .post(
            &format!("{}/pulls/{}/close", w.repo_path(), closed["number"]),
            &w.aiko,
            json!({}),
        )
        .await;
    assert_eq!(status, 200, "{body}");
    // The branch goes on, as another pull request's work.
    let later = w.push_change("feature", "later.txt", "later\n");
    w.open_pull("feature", json!([])).await;
    let second = runner.next_claim().await;
    assert_eq!(second["sha"], later.as_str());
    assert_eq!(runner.finish(&second, "success").await.0, 200);
    let url = format!("{}/pulls/{}/attempts", w.repo_path(), closed["number"]);
    let list = w.server.ok(&url, &w.aiko).await;
    let shas: Vec<_> = list.as_array().unwrap().iter().map(|a| &a["sha"]).collect();
    assert_eq!(shas, [own.as_str()], "{list}");
}

pub(crate) fn pipeline(jobs: &[&str]) -> String {
    let mut s = String::new();
    for j in jobs {
        s.push_str(&format!(
            "[[jobs]]\nname = \"{j}\"\nimage = \"{IMAGE}\"\ncommands = [\"echo {j}\"]\n\n"
        ));
    }
    s
}

/// `text` with its jobs `names` marked `required = true`.
pub(crate) fn mark_required(text: &str, names: &[&str]) -> String {
    mark(text, "required", names)
}

/// `text` with its jobs `names` setting `key = true`.
pub(crate) fn mark(text: &str, key: &str, names: &[&str]) -> String {
    names.iter().fold(text.to_string(), |text, name| {
        let line = format!("name = \"{name}\"\n");
        text.replace(&line, &format!("{line}{key} = true\n"))
    })
}

/// A pipeline whose jobs are all required of the pull requests into its branch.
pub(crate) fn required_pipeline(jobs: &[&str]) -> String {
    mark_required(&pipeline(jobs), jobs)
}

// Only tests that upload artifacts opt in; ordinary pipeline fixtures may reuse commits.
fn artifact_pipeline(jobs: &[&str]) -> String {
    pipeline(jobs).replace(
        "\ncommands =",
        "\nartifacts = [{name = \"report\", paths = [\"out/\"]}]\ncommands =",
    )
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
        &[(".guildbotics-ci.toml", &filtered_pipeline())],
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
        &[(
            ".guildbotics-ci.toml",
            &mark_required(&filtered_pipeline(), &["rust", "web", "always"]),
        )],
        None,
    )
    .await;
    runner.drain("success").await;
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
            .await["landing"]["blockers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|b| b["code"] == "already_in_base")
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
        &[(
            ".guildbotics-ci.toml",
            &mark_required(&filtered_pipeline(), &["rust", "web", "always"]),
        )],
        None,
    )
    .await;
    assert_eq!(runner.drain("success").await, 3, "main runs all jobs");
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
            "[guildbotics] skipped: no file matching paths changed since {base}"
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
    assert!(
        current["landing"]["blockers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|b| b["code"] == "head_checks")
    );
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
    assert!(
        runner.claim().await.is_none(),
        "no gate jobs are registered"
    );
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
    let merged = w.server.ok(&pull_path, &w.owner).await;
    assert_eq!(
        merged["merge_sha"], head,
        "path-skipped head still fast-forwards"
    );
    assert_eq!(merged["head_sha"], head);
}

#[tokio::test]
async fn paths_match_git_globs_and_a_pipeline_change_disables_selection() {
    let w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    land(
        &w,
        "setup",
        &[
            (".guildbotics-ci.toml", &filtered_pipeline()),
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
        ".guildbotics-ci.toml",
        &format!("{}\n# changed", filtered_pipeline()),
    );
    let p = w.open_pull("pipeline-change", json!([])).await;
    let report = checks(&w, p["number"].as_u64().unwrap()).await;
    for name in ["rust", "web", "always"] {
        assert_eq!(check(&report, name)["status"], "queued");
    }
}

#[tokio::test]
async fn a_later_heads_job_reuses_the_same_paths_contents_and_the_gate_executes_it() {
    paths_reuse_gate(true, false).await;
}

#[tokio::test]
async fn a_head_without_paths_reuse_is_its_own_gate() {
    paths_reuse_gate(false, false).await;
}

#[tokio::test]
async fn declared_verified_input_prevents_paths_reuse_between_pushed_heads() {
    paths_reuse_gate(false, true).await;
}

/// The second head of a pull request changes only docs: `rust` (paths
/// `src/**`) may reuse the first head's success by the contents of paths.
async fn paths_reuse_gate(reused: bool, verified: bool) {
    let w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    let pipeline = format!(
        "{}paths=['src/**']\nverified_sha={verified}\n{}",
        required_pipeline(&["rust"]),
        required_pipeline(&["always"])
    );
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &pipeline), ("src/a.rs", "a\n")],
        None,
    )
    .await;
    runner.drain("success").await;
    std::fs::create_dir_all(w.work.join("src")).unwrap();
    let first = w.push_change("feature", "src/a.rs", "changed\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let mut source = Value::Null;
    while let Some(c) = runner.claim().await {
        assert_eq!(c["sha"], first);
        if c["job"]["name"] == "rust" {
            assert!(c["reuse"].is_null(), "src/ changed since main");
            assert_eq!(runner.finish(&c, "success").await.0, 200);
            source = c;
        } else {
            // Keeps the first head out of the landing queue.
            assert_eq!(runner.finish(&c, "failure").await.0, 200);
        }
    }
    let head = w.push_change("feature", "notes.md", "docs\n");
    let first_claim = runner.next_claim().await;
    let (rust, claimed_always) = if verified && first_claim["job"]["name"] == "always" {
        (runner.next_claim().await, Some(first_claim))
    } else {
        (first_claim, None)
    };
    assert_eq!(
        (&rust["sha"], &rust["job"]["name"]),
        (&json!(head), &json!("rust"))
    );
    if verified {
        assert!(
            rust["reuse"].is_null(),
            "verified input requires the same tree even with matching paths"
        );
        assert_eq!(rust["verified_sha"], source["verified_sha"]);
        assert!(rust["verified_sha"].is_string());
    } else {
        assert_eq!(rust["reuse"]["attempt_id"], source["attempt_id"]);
        assert_eq!(rust["reuse"]["sha"], first);
        assert_eq!(rust["reuse"]["basis"], "paths");
    }
    let reported = if reused {
        rust["reuse"]["attempt_id"].clone()
    } else {
        Value::Null
    };
    let (st, result) = runner.post(&rust, "finish", json!({"lease": rust["lease"], "conclusion": "success", "image_digest": DIGEST, "reused_from": reported})).await;
    assert_eq!(st, 200, "{result}");
    if reused {
        assert_eq!(result["reused_from"]["sha"], first);
        assert_eq!(result["reused_from"]["basis"], "paths");
        let c = check(&checks(&w, n).await, "rust").clone();
        assert_eq!(c["reused_from"]["basis"], "paths", "{c}");
        assert_eq!(c["reused_from"]["attempt_id"], source["attempt_id"]);
    }
    let always = match claimed_always {
        Some(c) => c,
        None => runner.next_claim().await,
    };
    assert_eq!(
        (&always["sha"], &always["job"]["name"]),
        (&json!(head), &json!("always"))
    );
    assert!(always["reuse"].is_null(), "another tree, and no paths");
    assert_eq!(runner.finish(&always, "success").await.0, 200);
    let pull_path = format!("{}/pulls/{n}", w.repo_path());
    eventually("head conditions satisfied", || async {
        w.server.ok(&pull_path, &w.owner).await["landing"]["blockers"] == json!([])
    })
    .await;
    assert!(
        runner.claim().await.is_none(),
        "no gate jobs are registered"
    );
}

#[tokio::test]
async fn paths_retry_actually_executes_a_skipped_job() {
    let w = World::new().await;
    let runner = Runner::new(&w, "paths-runner").await;
    land(
        &w,
        "setup",
        &[(
            ".guildbotics-ci.toml",
            &mark_required(&filtered_pipeline(), &["always"]),
        )],
        None,
    )
    .await;
    runner.drain("success").await;
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
            (
                ".guildbotics-ci.toml",
                &mark_required(&filtered_pipeline(), &["always"]),
            ),
            ("src/a.rs", "before\n"),
        ],
        None,
    )
    .await;
    runner.drain("success").await;
    land(&w, "advance", &[("src/a.rs", "after\n")], Some(&runner)).await;
    runner.drain("success").await;
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
        &[(".guildbotics-ci.toml", &required_pipeline(&["test"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    sync_main(&w);
    w.push_change("source", "source.txt", "source change");
    w.open_pull("source", json!([])).await;
    let source = runner.claim().await.unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
    assert_eq!(runner.finish(&source, "success").await.0, 200);
    sync_main(&w);
    git_ok(&w.work, &["checkout", "-q", "source"]);
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
        Self::at(
            &w.server.url,
            &w.server.member(&w.owner, name, "runner").await,
        )
    }

    pub(crate) fn at(url: &str, token: &str) -> Self {
        Self {
            url: url.into(),
            token: token.into(),
            http: reqwest::Client::builder()
                .no_proxy()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .unwrap(),
        }
    }

    fn endpoint(&self, path: &str) -> String {
        format!("{}/api/v1/runner{path}", self.url)
    }

    pub(crate) async fn claim(&self) -> Option<Value> {
        let resp = self
            .http
            .post(self.endpoint("/claim"))
            .bearer_auth(&self.token)
            .json(&json!({"executor": "test", "executor_state": "ready", "version":gb_base::BUILD_VERSION,"max_jobs":1,"running_jobs":0,"accept":true, "architecture":"arm64", "images":[{"reference":IMAGE,"architecture":"arm64","digest":DIGEST}]}))
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

    pub(crate) async fn post(&self, c: &Value, what: &str, body: Value) -> (StatusCode, Value) {
        let resp = self
            .http
            .post(self.endpoint(&format!(
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

    pub(crate) async fn raw(
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
                self.endpoint(&format!(
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

    /// Finishes other queued work while waiting for this head.
    pub(crate) async fn claim_head(&self, head: &str) -> Value {
        for _ in 0..100 {
            let attempt = self.next_claim().await;
            if attempt["sha"] == head {
                return attempt;
            }
            assert_eq!(self.finish(&attempt, "success").await.0, StatusCode::OK);
        }
        panic!("fixture head was never claimed");
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
    git_as_ok(&w.work, "alice", &w.owner_git, &["fetch", "-q", "origin"]);
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

/// Polls `f` every 20 ms for up to 30 seconds.
pub(crate) async fn eventually<F, Fut>(what: &str, mut f: F)
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    tokio::time::timeout(std::time::Duration::from_secs(30), async {
        while !f().await {
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {what}"));
}

/// Sets `[ci]` values in the service configuration.
fn set_ci_config(data: &std::path::Path, values: &[(&str, toml::Value)]) {
    let path = data.join("config.toml");
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
        &[(".guildbotics-ci.toml", &pipeline(&["test", "lint"]))],
        None,
    )
    .await;
    // On main's tip, test succeeds after a while and lint fails.
    let main_test = runner.claim().await.unwrap();
    let main_lint = runner.claim().await.unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
    runner.finish(&main_test, "success").await;
    runner.finish(&main_lint, "failure").await;
    let alternate = Runner::new(&w, "runner2").await;
    for runner in [&alternate, &runner, &alternate] {
        let retry = runner.next_claim().await;
        assert_eq!(retry["job"]["name"], "lint");
        assert_eq!(runner.finish(&retry, "failure").await.0, 200);
    }
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
        &[(
            ".guildbotics-ci.toml",
            &mark_required(&artifact_pipeline(&["test"]), &["test"]),
        )],
        None,
    )
    .await;
    // The run of main's new tip (the merge) is queued; take it out of the way.
    assert_eq!(runner.drain("success").await, 1);

    // The head changes its pipeline; its own pipeline runs.
    sync_main(&w);
    let changed = format!(
        "{}\ntmpfs_mib = 2048\nram_workspace = {{ paths = [\"report/data\"], max_mib = 4608 }}\n",
        mark_required(&artifact_pipeline(&["test"]), &["test"])
            .replace("echo test", "echo changed")
    );
    std::fs::write(w.work.join(".guildbotics-ci.toml"), changed).unwrap();
    git_ok(&w.work, &["add", ".guildbotics-ci.toml"]);
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
    assert_eq!(c["job"]["tmpfs_mib"], 2048);
    assert_eq!(
        c["job"]["ram_workspace"],
        json!({"paths": ["report/data"], "max_mib": 4608})
    );
    assert!(c["job"].get("cpus").is_none(), "{c}");
    assert_eq!(c["repo"], REPO);
    assert_eq!(c["network"], false);
    assert_eq!(c["cache_write"], false);
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
        format!("{}/workspaces/acme/repos/demo/jobs/{id}", w.server.url)
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
    let judged = check(&v["merge_checks"], "test");
    assert_eq!(judged["conclusion"], "success", "{v}");
    assert_eq!(judged["attempt_id"], id);
    let confirmed = check(&v["post_merge_checks"], "test");
    assert_eq!(confirmed["attempt_id"], id);
    assert_eq!(confirmed["artifact_names"], json!(["report"]));
    let diag = w.server.ok("/admin/diagnostics", &w.owner).await;
    assert_eq!(diag["runner"]["state"], "ready", "{diag}");
    assert_eq!(diag["runner"]["runners"][0]["name"], "runner1");
}

#[tokio::test]
async fn a_merge_keeps_a_running_head_and_can_publish_its_cache_after_success() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    let jobs = format!(
        "[[jobs]]\nname = \"build\"\nimage = \"{IMAGE}\"\ncommands = [\"echo build\"]\ncache = {{ paths = [\"target\"] }}\n\n{}",
        pipeline(&["lint"])
    );
    land(&w, "setup", &[(".guildbotics-ci.toml", &jobs)], None).await;
    // Merged while its jobs waited, the setup's commit runs them as main's.
    for _ in 0..2 {
        let c = runner.claim().await.unwrap();
        assert_eq!(c["cache_write"], c["job"]["name"] == "build", "{c}");
        assert_eq!(runner.finish(&c, "success").await.0, 200);
    }

    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    // Seed a failed PR build, so its next head puts build before lint
    // independently of the setup run's sub-second durations.
    for _ in 0..2 {
        let c = runner.claim().await.unwrap();
        let result = if c["job"]["name"] == "build" {
            "failure"
        } else {
            "success"
        };
        assert_eq!(runner.finish(&c, result).await.0, 200);
    }
    let head = w.push_change("feature", "f.txt", "two\n");
    // `build` still runs for the pull request when it is merged.
    let running = runner.next_claim().await;
    assert_eq!(running["job"]["name"], "build");
    assert_eq!(running["cache_write"], true, "{running}");
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
    let id = running["attempt_id"].as_str().unwrap();
    assert_eq!(runner.heartbeat(&running).await.1["cancel"], false);
    assert!(runner.claim().await.is_none());
    let poll = || w.server.post("/runner/cache", &runner.token, json!([id]));
    assert_eq!(poll().await, (StatusCode::OK, json!({ (id): "keep" })));
    let (status, _) = runner
        .post(
            &running,
            "finish",
            json!({
                "lease": running["lease"], "conclusion": "success", "image_digest": DIGEST,
                "cache_key": "a".repeat(64),
            }),
        )
        .await;
    assert_eq!(status, 200);
    let (status, decisions) = poll().await;
    assert_eq!(status, 200);
    assert!(decisions[id]["publish"].is_string(), "{decisions}");
    assert_eq!(
        w.server
            .post("/runner/cache", &w.owner, json!([id]))
            .await
            .0,
        403
    );
    let other = Runner::new(&w, "runner2").await;
    assert_eq!(
        w.server
            .post("/runner/cache", &other.token, json!([id]))
            .await,
        (StatusCode::OK, json!({ (id): "discard" }))
    );
    let v = checks(&w, n).await;
    assert_eq!(v["merge_checks"]["checks"], json!([]));
    let confirmation = &v["post_merge_checks"];
    let build = check(confirmation, "build");
    assert_eq!(build["conclusion"], "success", "{v}");
    assert_eq!(build["attempt_id"], id);
    assert_eq!(build["attempt"], 1);
    assert_eq!(check(confirmation, "lint")["attempt"], 1, "{v}");
}

#[tokio::test]
async fn rapid_pushes_supersede_older_commits_and_their_late_results_never_count() {
    let w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(
            ".guildbotics-ci.toml",
            &required_pipeline(&["build", "test"]),
        )],
        None,
    )
    .await;
    runner.drain("success").await;
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
    // A base whose pipeline cannot be read: what it requires is unknown, so a
    // valid head needs the owner's approval to replace it.
    let broken = w.push_change("broken", ".guildbotics-ci.toml", "not toml");
    let head = w.push_change("feature", ".guildbotics-ci.toml", &pipeline(&["test"]));
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head": "feature", "base": "broken", "title": "Onto a broken base"}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    let p = v["pull"].clone();
    let n = p["number"].as_u64().unwrap();
    let base = format!("{}/pulls/{n}", w.repo_path());
    let unknown_base = |v: &Value| {
        let t = check(v, "pipeline");
        assert_eq!(
            (t["status"].as_str(), t["conclusion"].as_str()),
            (Some("completed"), Some("failure")),
            "{v}"
        );
        assert!(
            t["details"]
                .as_str()
                .unwrap()
                .contains("required checks of the base are unknown"),
            "{t}"
        );
    };
    let v = checks(&w, n).await;
    assert_eq!(v["checks_expected"], true);
    assert_eq!(
        blocker(&w, n, "required_removal").await.unwrap()["jobs"],
        json!([format!("pipeline@{}", &broken[..12])])
    );
    let (st, v) = w
        .server
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(
        (st.as_u16(), v["error"]["code"].as_str()),
        (409, Some("not_mergeable")),
        "{v}"
    );
    // A base branch that is gone is just as unknown.
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", "origin", ":refs/heads/broken"],
    );
    unknown_base(&checks(&w, n).await);

    // The pipeline lands (nothing is required of the setup PR: main has none).
    w.server
        .post(&format!("{base}/close"), &w.aiko, json!({}))
        .await;
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &required_pipeline(&["test"]))],
        Some(&runner),
    )
    .await;
    runner.drain("success").await;

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
        w.work.join(".guildbotics-ci.toml"),
        "[[jobs]]\nname = \"test\"\nprivileged = true\n",
    )
    .unwrap();
    git_ok(&w.work, &["add", ".guildbotics-ci.toml"]);
    let head3 = w.push_change("feature3", "h.txt", "1\n");
    let p3 = w.open_pull("feature3", json!([])).await;
    let n3 = p3["number"].as_u64().unwrap();
    let v = checks(&w, n3).await;
    let pl = check(&v, "pipeline");
    assert_eq!(
        (pl["status"].as_str(), pl["conclusion"].as_str()),
        (Some("completed"), Some("failure"))
    );
    assert!(
        pl["details"].as_str().unwrap().contains("privileged"),
        "{pl}"
    );
    // The required job stays required: a placeholder, which no runner holds.
    let t = check(&v, "test");
    assert_eq!(t["conclusion"], "failure");
    assert_eq!(t.get("runner"), Some(&Value::Null));
    assert!(t.get("attempt_id").is_none(), "{t}");
    assert!(
        v["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["conclusion"] != "success")
    );
    let codes = blocker_codes(&w, n3).await;
    assert!(codes.contains(&"head_checks".to_string()), "{codes:?}");
    assert!(
        !codes.contains(&"required_removal".to_string()),
        "{codes:?}"
    );
    // The placeholder is the pull request's answer, not part of the commit.
    let commit = w
        .server
        .ok(
            &format!("{}/commits/{head3}/checks", w.repo_path()),
            &w.aiko,
        )
        .await;
    assert!(
        commit["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["name"] != "test"),
        "{commit}"
    );

    // A head without a pipeline removes nothing either: the check cannot run,
    // says why, and the merge is refused.
    sync_main(&w);
    git_ok(&w.work, &["rm", "-q", ".guildbotics-ci.toml"]);
    let head_none = w.push_change("no-pipeline", "n.txt", "1\n");
    let none = w.open_pull("no-pipeline", json!([])).await;
    let v = checks(&w, none["number"].as_u64().unwrap()).await;
    assert_eq!(v["checks_expected"], true);
    let t = check(&v, "test");
    assert_eq!(
        (t["status"].as_str(), t["conclusion"].as_str()),
        (Some("completed"), Some("failure"))
    );
    assert!(
        t["details"]
            .as_str()
            .unwrap()
            .contains(".guildbotics-ci.toml"),
        "{t}"
    );
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{}/merge", w.repo_path(), none["number"]),
            &w.owner,
            json!({"expected_head": head_none, "expected_base": none["base_sha"]}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("head_checks"),
        "{v}"
    );

    // A head that drops the required check from its own pipeline runs what is
    // left, and lands only once an owner approves exactly that head.
    sync_main(&w);
    std::fs::write(w.work.join(".guildbotics-ci.toml"), pipeline(&["lint"])).unwrap();
    git_ok(&w.work, &["add", ".guildbotics-ci.toml"]);
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
    assert!(
        v["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["name"] != "test"),
        "a job the head removed is not one of its checks: {v}"
    );
    let at4 = format!("{}/pulls/{n4}", w.repo_path());
    let merge4 = |head: String| {
        let (s, url, owner, base) = (
            &w.server,
            format!("{at4}/merge"),
            w.owner.clone(),
            p4["base_sha"].clone(),
        );
        async move {
            s.post(
                &url,
                &owner,
                json!({"expected_head": head, "expected_base": base}),
            )
            .await
        }
    };
    let removal = blocker(&w, n4, "required_removal").await.unwrap();
    assert_eq!(removal["jobs"], json!(["test"]));
    let (st, v) = merge4(head4.clone()).await;
    assert_eq!(
        (st.as_u16(), v["error"]["code"].as_str()),
        (409, Some("not_mergeable")),
        "{v}"
    );
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("required_removal"),
        "{v}"
    );
    // Another member's approval does not lift it; the owner's of this head does.
    assert_eq!(
        crate::pulls::review(&w, &at4, &w.ken, "approve", &head4).await,
        201
    );
    assert!(blocker(&w, n4, "required_removal").await.is_some());
    assert_eq!(
        crate::pulls::review(&w, &at4, &w.owner, "approve", &head4).await,
        201
    );
    assert!(blocker(&w, n4, "required_removal").await.is_none());
    // A new head needs its own approval.
    let moved = w.push_change("feature4", "j.txt", "2\n");
    assert!(blocker(&w, n4, "required_removal").await.is_some());
    let c = runner.next_claim().await;
    assert_eq!(c["sha"], moved.as_str());
    runner.finish(&c, "success").await;
    assert_eq!(
        crate::pulls::review(&w, &at4, &w.owner, "approve", &moved).await,
        201
    );
    let (st, v) = merge4(moved).await;
    assert_eq!(st, 200, "{v}");
}

/// An invalid file cannot reach a base, and one that got there (two clean merges
/// can define a job twice) is repaired by a valid head the owner approves.
#[tokio::test]
async fn an_invalid_base_is_repaired_only_by_a_valid_head_the_owner_approves() {
    let w = World::new().await;
    let runner = Runner::new(&w, "repair-runner").await;
    let invalid = w.push_change("invalid", ".guildbotics-ci.toml", "not toml");
    let opened = w.open_pull("invalid", json!([])).await;
    let n = opened["number"].as_u64().unwrap();
    assert_eq!(checks(&w, n).await["required_names"], json!(["pipeline"]));
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head": invalid, "expected_base": opened["base_sha"]}),
        )
        .await;
    assert_eq!(st, 409, "{v}");

    let head = w.push_change(
        "repair",
        ".guildbotics-ci.toml",
        &required_pipeline(&["test"]),
    );
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head": "repair", "base": "invalid", "title": "Repair", "body": "body"}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    let repair = v["pull"]["number"].as_u64().unwrap();
    let at = format!("{}/pulls/{repair}", w.repo_path());
    assert_eq!(checks(&w, repair).await["required_names"], json!(["test"]));
    runner.drain("success").await;
    assert_eq!(
        crate::pulls::review(&w, &at, &w.ken, "approve", &head).await,
        201
    );
    assert_eq!(
        blocker(&w, repair, "required_removal").await.unwrap()["jobs"],
        json!([format!("pipeline@{}", &invalid[..12])])
    );
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );
    assert!(blocker(&w, repair, "required_removal").await.is_none());
    // The approval covers that invalid file, not the next one.
    let moved = w.push_change("invalid", "moved.txt", "1\n");
    assert_eq!(
        blocker(&w, repair, "required_removal").await.unwrap()["jobs"],
        json!([format!("pipeline@{}", &moved[..12])])
    );
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );
    assert!(blocker(&w, repair, "required_removal").await.is_none());
    let (st, v) = w
        .server
        .post(
            &format!("{at}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": moved}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
}

/// An approval covers what the head itself removed: a required job the base
/// gained afterwards stays required until the head merges the base.
#[tokio::test]
async fn an_approved_head_answers_for_a_required_job_the_base_gains_later() {
    let w = World::new().await;
    let runner = Runner::new(&w, "gained-runner").await;
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &required_pipeline(&["test"]))],
        Some(&runner),
    )
    .await;
    let head = w.push_change("behind", "behind.txt", "1\n");
    let opened = w.open_pull("behind", json!([])).await;
    let n = opened["number"].as_u64().unwrap();
    runner.drain("success").await;
    let at = format!("{}/pulls/{n}", w.repo_path());
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );

    let main = land(
        &w,
        "gain",
        &[(
            ".guildbotics-ci.toml",
            &required_pipeline(&["test", "lint"]),
        )],
        Some(&runner),
    )
    .await;
    assert_eq!(
        checks(&w, n).await["required_names"],
        json!(["test", "lint"])
    );
    let lint = check(&checks(&w, n).await, "lint").clone();
    assert_eq!(lint["conclusion"], "failure", "{lint}");
    assert!(blocker(&w, n, "required_removal").await.is_none());
    assert_eq!(
        blocker(&w, n, "head_checks").await.unwrap()["jobs"],
        json!(["lint"])
    );
    let (st, v) = w
        .server
        .post(
            &format!("{at}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": main}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("head_checks"),
        "{v}"
    );
}

/// A head that leaves an invalid base as it is repairs nothing, whatever was
/// approved before the base broke.
#[tokio::test]
async fn an_approval_does_not_carry_a_head_over_a_base_that_broke_later() {
    let w = World::new().await;
    let runner = Runner::new(&w, "broken-runner").await;
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &required_pipeline(&["test"]))],
        Some(&runner),
    )
    .await;
    w.push_change("target", "target.txt", "1\n");
    let head = w.push_change("behind", "behind.txt", "1\n");
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head": "behind", "base": "target", "title": "Behind"}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    let n = v["pull"]["number"].as_u64().unwrap();
    let at = format!("{}/pulls/{n}", w.repo_path());
    runner.drain("success").await;
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );
    let broken = w.push_change("target", ".guildbotics-ci.toml", "not toml");

    assert_eq!(checks(&w, n).await["required_names"], json!(["pipeline"]));
    assert!(blocker(&w, n, "required_removal").await.is_none());
    let (st, v) = w
        .server
        .post(
            &format!("{at}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": broken}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
}

/// An approval accepts what the head was removing when it was given: a base
/// that later makes the same head remove a required job needs a new one.
#[tokio::test]
async fn an_approval_does_not_cover_a_removal_the_base_creates_later() {
    let w = World::new().await;
    let runner = Runner::new(&w, "removal-runner").await;
    let original = mark_required(&pipeline(&["test", "lint"]), &["test"]);
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &original)],
        Some(&runner),
    )
    .await;
    // The head renames a job nothing requires yet, so nothing needs accepting.
    let head = w.push_change(
        "rename",
        ".guildbotics-ci.toml",
        &original.replace("name = \"lint\"", "name = \"quality\""),
    );
    let opened = w.open_pull("rename", json!([])).await;
    let n = opened["number"].as_u64().unwrap();
    runner.drain("success").await;
    let at = format!("{}/pulls/{n}", w.repo_path());
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );
    assert!(blocker(&w, n, "required_removal").await.is_none());

    // The base starts requiring the old name; the merge would still drop it.
    let tip = land(
        &w,
        "require-lint",
        &[(
            ".guildbotics-ci.toml",
            &original.replace(
                "commands = [\"echo lint\"]\n",
                "commands = [\"echo lint\"]\nrequired = true\n",
            ),
        )],
        Some(&runner),
    )
    .await;
    assert_eq!(
        blocker(&w, n, "required_removal").await.unwrap()["jobs"],
        json!(["lint"])
    );
    let (st, v) = w
        .server
        .post(
            &format!("{at}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": tip}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("required_removal"),
        "{v}"
    );

    // A base that is not a commit is refused.
    for base in ["not-a-sha", "0123456789012345678901234567890123456789"] {
        let (st, v) = w
            .server
            .post(
                &format!("{at}/reviews"),
                &w.owner,
                json!({"verdict": "approve", "commit": head, "base": base}),
            )
            .await;
        assert_eq!(st, 400, "{v}");
    }
    // An approval sent from a screen that showed the old base, or that says
    // nothing about its base, does not accept it.
    for base in [opened["base_sha"].clone(), Value::Null, json!("")] {
        let (st, v) = w
            .server
            .post(
                &format!("{at}/reviews"),
                &w.owner,
                json!({"verdict": "approve", "commit": head, "base": base}),
            )
            .await;
        assert_eq!(st, 201, "{v}");
        assert_eq!(
            blocker(&w, n, "required_removal").await.unwrap()["jobs"],
            json!(["lint"]),
            "{base}"
        );
    }
    // The owner sees it and approves the same head again.
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );
    assert!(blocker(&w, n, "required_removal").await.is_none());
}

/// Landing part of a stacked head's history can turn its rename into the removal
/// of a required job: the approval given before that does not cover it.
#[tokio::test]
async fn an_approval_does_not_cover_a_removal_created_by_landing_part_of_the_head() {
    let w = World::new().await;
    let runner = Runner::new(&w, "stacked-runner").await;
    let original = mark_required(&pipeline(&["test", "lint"]), &["test"]);
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &original)],
        Some(&runner),
    )
    .await;
    let marked = original.replace(
        "commands = [\"echo lint\"]\n",
        "commands = [\"echo lint\"]\nrequired = true\n",
    );
    let lower = w.push_change("mark", ".guildbotics-ci.toml", &marked);
    let head = w.push_change(
        "stacked",
        ".guildbotics-ci.toml",
        &marked.replace("name = \"lint\"", "name = \"quality\""),
    );
    let n = w.open_pull("stacked", json!([])).await["number"]
        .as_u64()
        .unwrap();
    let lower_pull = w.open_pull("mark", json!([])).await;
    runner.drain("success").await;
    let at = format!("{}/pulls/{n}", w.repo_path());
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );
    assert!(blocker(&w, n, "required_removal").await.is_none());

    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{}/merge", w.repo_path(), lower_pull["number"]),
            &w.owner,
            json!({"expected_head": lower, "expected_base": lower_pull["base_sha"]}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(
        blocker(&w, n, "required_removal").await.unwrap()["jobs"],
        json!(["lint"])
    );
    let (st, v) = w
        .server
        .post(
            &format!("{at}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": v["merge_sha"]}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("required_removal"),
        "{v}"
    );
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &head).await,
        201
    );
    assert!(blocker(&w, n, "required_removal").await.is_none());
}

/// A removal stays the head's own after the base moves on, and what the merge
/// would leave decides: a clean merge into an invalid file does not land.
#[tokio::test]
async fn the_merge_result_decides_what_a_head_removes() {
    let w = World::new().await;
    let runner = Runner::new(&w, "merge-runner").await;
    let main = required_pipeline(&["test", "web"]);
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &main)],
        Some(&runner),
    )
    .await;

    // The head drops `web`, then main moves on without touching the pipeline.
    let dropped = w.push_change(
        "drop-web",
        ".guildbotics-ci.toml",
        &required_pipeline(&["test"]),
    );
    let n = w.open_pull("drop-web", json!([])).await["number"]
        .as_u64()
        .unwrap();
    runner.drain("success").await;
    assert_eq!(checks(&w, n).await["required_names"], json!(["test"]));
    assert_eq!(
        blocker(&w, n, "required_removal").await.unwrap()["jobs"],
        json!(["web"])
    );
    let at = format!("{}/pulls/{n}", w.repo_path());
    assert_eq!(
        crate::pulls::review(&w, &at, &w.owner, "approve", &dropped).await,
        201
    );
    assert!(blocker(&w, n, "required_removal").await.is_none());
    // An unrelated change to the base keeps the approval.
    let tip = land(&w, "later", &[("later.txt", "1\n")], Some(&runner)).await;
    runner.drain("success").await;
    assert_eq!(checks(&w, n).await["required_names"], json!(["test"]));
    assert!(blocker(&w, n, "required_removal").await.is_none());
    let (st, v) = w
        .server
        .post(
            &format!("{at}/merge"),
            &w.owner,
            json!({"expected_head": dropped, "expected_base": tip}),
        )
        .await;
    assert_eq!(st, 200, "{v}");

    // Two heads each add job `dup` at opposite ends of the file: each is valid
    // and they merge cleanly, into a file that defines it twice.
    sync_main(&w);
    let job = required_pipeline(&["dup"]);
    let current = std::fs::read_to_string(w.work.join(".guildbotics-ci.toml")).unwrap();
    git_ok(&w.work, &["checkout", "-q", "-b", "prepends"]);
    let prepends = w.push_change(
        "prepends",
        ".guildbotics-ci.toml",
        &format!("{job}{current}"),
    );
    let first = w.open_pull("prepends", json!([])).await;
    sync_main(&w);
    land(
        &w,
        "appends",
        &[(".guildbotics-ci.toml", &format!("{current}{job}"))],
        Some(&runner),
    )
    .await;
    let n = first["number"].as_u64().unwrap();
    let v = checks(&w, n).await;
    let pipeline_check = check(&v, "pipeline");
    assert_eq!(pipeline_check["conclusion"], "failure", "{v}");
    assert!(
        pipeline_check["details"]
            .as_str()
            .unwrap()
            .contains("Merging this head would leave an invalid"),
        "{pipeline_check}"
    );
    let main_tip = w
        .server
        .ok(&format!("{}/branches/main", w.repo_path()), &w.aiko)
        .await["sha"]
        .clone();
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head": prepends, "expected_base": main_tip}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
}

/// A head without a pipeline can still produce an invalid one by merging: here
/// the base renamed the file the head edits.
#[tokio::test]
async fn a_head_without_a_pipeline_cannot_land_an_invalid_merge() {
    let w = World::new().await;
    let draft = required_pipeline(&["test"]);
    land(&w, "draft", &[("ci-draft.toml", &draft)], None).await;
    let head = w.push_change("edit-draft", "ci-draft.toml", &format!("{draft}not toml\n"));
    sync_main(&w);
    git_ok(&w.work, &["checkout", "-q", "-b", "renamed"]);
    git_ok(&w.work, &["mv", "ci-draft.toml", ".guildbotics-ci.toml"]);
    let base = w.push_change("renamed", "after.txt", "1\n");
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head": "edit-draft", "base": "renamed", "title": "Edit the draft"}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    let n = v["pull"]["number"].as_u64().unwrap();
    let v = checks(&w, n).await;
    assert_eq!(v["required_names"], json!(["test", "pipeline"]), "{v}");
    assert!(
        check(&v, "pipeline")["details"]
            .as_str()
            .unwrap()
            .contains("Merging this head would leave an invalid"),
        "{v}"
    );
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head": head, "expected_base": base}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
}

/// Each base decides: a collecting branch that drops a job leaves what main
/// requires alone, and no head changes the list by editing its own file.
#[tokio::test]
async fn each_base_decides_what_its_pull_requests_require() {
    async fn open_into(w: &World, head: &str, base: &str) -> u64 {
        let (st, v) = w
            .server
            .post(
                &format!("{}/pulls", w.repo_path()),
                &w.aiko,
                json!({"head": head, "base": base, "title": head, "body": "body", "tickets": []}),
            )
            .await;
        assert_eq!(st, 201, "{v}");
        v["pull"]["number"].as_u64().unwrap()
    }
    async fn assert_required(w: &World, n: u64, names: &[&str], removed: &[&str]) {
        assert_eq!(checks(w, n).await["required_names"], json!(names), "!{n}");
        let removal = blocker(w, n, "required_removal").await;
        assert_eq!(
            removal.as_ref().map_or(json!([]), |b| b["jobs"].clone()),
            json!(removed),
            "!{n}"
        );
    }

    let w = World::new().await;
    let runner = Runner::new(&w, "base-runner").await;
    let main = required_pipeline(&["test", "web"]);
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &main)],
        Some(&runner),
    )
    .await;

    // A collecting branch drops `web`: its children no longer need it.
    w.push_change(
        "collect",
        ".guildbotics-ci.toml",
        &required_pipeline(&["test"]),
    );
    w.push_change("child", "child.txt", "child\n");
    let child = open_into(&w, "child", "collect").await;
    assert_required(&w, child, &["test"], &[]).await;
    // Its own pull request into main still answers to main.
    let collecting = open_into(&w, "collect", "main").await;
    assert_required(&w, collecting, &["test"], &["web"]).await;

    // A pull request into main is unaffected by the collecting branch.
    sync_main(&w);
    w.push_change("plain", "plain.txt", "plain\n");
    let plain = open_into(&w, "plain", "main").await;
    assert_required(&w, plain, &["test", "web"], &[]).await;

    // Unmarking a job drops it just as deleting it does.
    sync_main(&w);
    w.push_change(
        "unmarked",
        ".guildbotics-ci.toml",
        &mark_required(&pipeline(&["test", "web"]), &["test"]),
    );
    let unmarked = open_into(&w, "unmarked", "main").await;
    assert_required(&w, unmarked, &["test"], &["web"]).await;

    // What a head marks for itself adds nothing until it is in the base.
    sync_main(&w);
    w.push_change(
        "added",
        ".guildbotics-ci.toml",
        &required_pipeline(&["test", "web", "more"]),
    );
    let added = open_into(&w, "added", "main").await;
    assert_required(&w, added, &["test", "web"], &[]).await;
}

async fn blockers(w: &World, n: u64) -> Vec<Value> {
    w.server
        .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
        .await["landing"]["blockers"]
        .as_array()
        .unwrap()
        .clone()
}

async fn blocker(w: &World, n: u64, code: &str) -> Option<Value> {
    blockers(w, n).await.into_iter().find(|b| b["code"] == code)
}

async fn blocker_codes(w: &World, n: u64) -> Vec<String> {
    blockers(w, n)
        .await
        .iter()
        .map(|b| b["code"].as_str().unwrap().to_string())
        .collect()
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
        &[(".guildbotics-ci.toml", &pipeline(&["test"]))],
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
        t["details_url"].as_str().unwrap().contains("/jobs/att_"),
        "{t}"
    );

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
    let w =
        World::with_config(|data| set_ci_config(data, &[("runner_absent_secs", 2.into())])).await;
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &pipeline(&["test"]))],
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
    let mut w =
        World::with_config(|data| set_ci_config(data, &[("runner_absent_secs", 600.into())])).await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    assert!(runner.drain("success").await > 0);

    // Use the default lease for setup, then shorten it for the attempts that expire.
    w.server.stop();
    set_ci_config(&w.server.data, &[("lease_secs", 2.into())]);
    w.restart(&[]);
    let runner = Runner::at(&w.server.url, &runner.token);
    w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let first = runner.claim().await.unwrap();
    assert_eq!(first["sha"], p["head_sha"]);
    assert_eq!(first["lease_secs"], 2);
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

    // Stop with attempt 2 active, then wait until its recorded lease expires.
    let second = runner.claim().await.unwrap();
    assert_eq!(second["attempt"], 2);
    let expires =
        chrono::DateTime::parse_from_rfc3339(second["expires_at"].as_str().unwrap()).unwrap();
    w.server.stop();
    eventually("the stopped service's lease expires", || async {
        chrono::Utc::now() > expires
    })
    .await;
    w.restart(&[]);
    let runner = Runner::at(&w.server.url, &runner.token);
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
    let out = branvil_command()
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
        &[(".guildbotics-ci.toml", &pipeline(&["test", "lint"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    sync_main(w);
    w.push_change("feature", "f.txt", "1\n");
    w.open_pull("feature", json!([])).await;
    for _ in 0..2 {
        let c = runner.claim().await.unwrap();
        let result = if c["job"]["name"] == "test" {
            "failure"
        } else {
            "success"
        };
        assert_eq!(runner.finish(&c, result).await.0, 200);
    }
    w.push_change("feature", "f.txt", "2\n");
    let running = runner.next_claim().await;
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
    let mut w =
        World::with_config(|data| set_ci_config(data, &[("stop_wait_secs", 1.into())])).await;
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
        details.contains("GuildBotics stopped") && details.contains("in its place"),
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
    // Long leases: what follows is not their expiry.
    let mut w = World::with_config(|data| {
        set_ci_config(
            data,
            &[("lease_secs", 600.into()), ("runner", "auto".into())],
        );
    })
    .await;
    // As the automatic runner, which dies with its service.
    let runner = Runner::new(&w, "hub-runner").await;
    let held = running_test_and_queued_lint(&w, &runner).await;
    // A home of its own: without msb the automatic runner leaves work queued
    // for a ready runner instead of taking incompatible attempts.
    let home = w.dir.path().join("home");
    std::fs::create_dir_all(&home).unwrap();
    let root = home.join(".guildbotics");
    w.restart(&[
        ("HOME", home.to_str().unwrap()),
        ("GUILDBOTICS_HOME", root.to_str().unwrap()),
        ("GUILDBOTICS_TEST_SUPERVISE_RUNNER", "1"),
    ]);
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
    let ready = Runner::new(&w, "ready-runner").await;
    let first = ready.next_claim().await;
    assert_eq!(first["job"]["name"], "test");
    assert_eq!(ready.finish(&first, "success").await.0, 200);
    let second = ready.next_claim().await;
    assert_eq!(second["job"]["name"], "lint");
    assert_eq!(ready.finish(&second, "success").await.0, 200);
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
    assert_eq!(test_id, first["attempt_id"].as_str().unwrap());
    assert_eq!(check(&list, "test")["runner"], "ready-runner");
    let rerun = w
        .server
        .ok(&format!("{}/jobs/{test_id}", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(rerun["runner"], "ready-runner");
    assert_eq!(w.server.ok(&url, &w.aiko).await["runner"], "hub-runner");
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
        &[(".guildbotics-ci.toml", &pipeline(&["test"]))],
        None,
    )
    .await;
    assert_eq!(runner.drain("success").await, 1);
    let report = checks(&w, 1).await;
    assert_eq!(report["merge_checks"]["checks"], json!([]));
    let test = check(&report["post_merge_checks"], "test");
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
        &[(".guildbotics-ci.toml", &required_pipeline(&["test"]))],
        None,
    )
    .await;
    runner.drain("success").await;
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
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("head_checks")
    );
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

/// A pull request whose required check logged a line and uploaded an
/// artifact, backed up (`without_ci_files` as asked) after or, unless
/// `finished`, before the check finished, and restored into a new data
/// directory that the server now serves at the same address.
struct RestoredCi {
    w: World,
    runner: Runner,
    claim: Value,
    head: String,
    backup: std::path::PathBuf,
    manifest: Value,
    before: Value,
    after: Value,
    _target: tempfile::TempDir,
}

const CI_LOG: &[u8] = b"kept in the backup\n";

/// The entry count of every table of the database file `db`, read from a
/// copy (opening a redb file writes to it).
fn table_counts(db: &std::path::Path) -> Value {
    let dir = tempfile::tempdir().unwrap();
    let copy = dir.path().join("counted.redb");
    std::fs::copy(db, &copy).unwrap();
    let database = redb::Database::open(&copy).unwrap();
    let txn = redb::ReadableDatabase::begin_read(&database).unwrap();
    let mut counts = serde_json::Map::new();
    for handle in txn.list_tables().unwrap() {
        let name = redb::TableHandle::name(&handle).to_string();
        let def: redb::TableDefinition<&[u8], &[u8]> = redb::TableDefinition::new(&name);
        let n = redb::ReadableTableMetadata::len(&txn.open_table(def).unwrap()).unwrap();
        counts.insert(name, json!(n));
    }
    Value::Object(counts)
}

fn ci_zip() -> Vec<u8> {
    let mut zip = b"PK\x05\x06".to_vec();
    zip.extend_from_slice(&[0; 18]);
    zip
}

async fn restored_ci(without_ci_files: bool, finished: bool) -> RestoredCi {
    let mut w = World::new().await;
    let runner = Runner::new(&w, "runner1").await;
    land(
        &w,
        "setup",
        &[(".guildbotics-ci.toml", &artifact_pipeline(&["test"]))],
        None,
    )
    .await;
    runner.drain("success").await;
    sync_main(&w);
    let head = w.push_change("feature", "f.txt", "1\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let claim = runner.claim().await.unwrap();
    runner
        .raw(reqwest::Method::POST, &claim, "log", CI_LOG.to_vec())
        .await;
    let (st, _) = runner
        .raw(reqwest::Method::PUT, &claim, "artifacts/report", ci_zip())
        .await;
    assert_eq!(st, 200);
    if finished {
        assert_eq!(runner.finish(&claim, "success").await.0, 200);
        eventually("the landing gate before backup", || async {
            checks(&w, n).await["checks"]
                .as_array()
                .unwrap()
                .iter()
                .any(|check| check["name"] == "test" && check["conclusion"] == "success")
        })
        .await;
    }
    let before = checks(&w, n).await;

    let backup = w.dir.path().join("backup");
    let request = if without_ci_files {
        json!({"to": backup, "without_ci_files": true})
    } else {
        json!({"to": backup})
    };
    let (st, manifest) = w.server.post("/admin/backup", &w.owner, request).await;
    assert_eq!(st, 200, "{manifest}");
    assert_eq!(
        manifest["tables"],
        table_counts(&backup.join("branvil.redb"))
    );
    if !finished {
        // The backup leaves the running job of the live service alone.
        let (st, _) = runner
            .raw(
                reqwest::Method::POST,
                &claim,
                "log",
                b"after the backup\n".to_vec(),
            )
            .await;
        assert_eq!(st, 200);
    }
    let target = tempfile::tempdir().unwrap();
    let out = branvil_command()
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
    RestoredCi {
        w,
        runner,
        claim,
        head,
        backup,
        manifest,
        before,
        after,
        _target: target,
    }
}

impl RestoredCi {
    /// The log read of the job that ran before the backup.
    async fn log(&self) -> Value {
        let id = self.claim["attempt_id"].as_str().unwrap();
        self.w
            .server
            .ok(
                &format!("{}/jobs/{id}/log", self.w.repo_path()),
                &self.w.aiko,
            )
            .await
    }

    async fn artifact(&self) -> reqwest::Response {
        self.w
            .server
            .http
            .get(format!(
                "{}/api/v1{}/commits/{}/artifacts/report",
                self.w.server.url,
                self.w.repo_path(),
                self.head
            ))
            .bearer_auth(&self.w.aiko)
            .send()
            .await
            .unwrap()
    }

    fn assert_left_out_ci_files(&self) {
        assert!(!self.backup.join("ci/logs").exists());
        assert!(!self.backup.join("ci/artifacts").exists());
        assert!(
            self.manifest["excluded"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e.as_str().unwrap().starts_with("CI logs and artifacts")),
            "{}",
            self.manifest["excluded"]
        );
    }
}

#[tokio::test]
async fn ci_records_logs_and_artifacts_survive_a_backup_and_restore() {
    let r = restored_ci(false, true).await;
    assert!(r.backup.join("ci/logs").is_dir());
    assert_eq!(r.after["checks"], r.before["checks"]);
    let log = r.log().await;
    assert_eq!(log["log"], "kept in the backup\n");
    assert_eq!(log["pruned"], false);
    let artifact = r.artifact().await;
    assert_eq!(artifact.status(), 200);
    assert_eq!(artifact.bytes().await.unwrap().to_vec(), ci_zip());
}

#[tokio::test]
async fn a_backup_without_ci_files_restores_the_results_with_their_files_pruned() {
    let r = restored_ci(true, true).await;
    r.assert_left_out_ci_files();
    // The checks keep their results, as after retention deleted the files.
    let result = |checks: &Value| {
        checks["checks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| {
                (
                    c["name"].clone(),
                    c["status"].clone(),
                    c["conclusion"].clone(),
                )
            })
            .collect::<Vec<_>>()
    };
    assert_eq!(result(&r.after), result(&r.before));
    assert_eq!(check(&r.after, "test")["artifact_names"], json!([]));
    let log = r.log().await;
    assert_eq!(log["log"], "");
    assert_eq!(log["pruned"], true);
    assert_ne!(r.artifact().await.status(), 200);
}

#[tokio::test]
async fn a_backup_without_ci_files_interrupts_a_running_job_and_queues_it_again() {
    // The job had uploaded its artifact but not finished at backup time.
    let r = restored_ci(true, false).await;
    r.assert_left_out_ci_files();
    assert_eq!(
        check(&r.before, "test")["artifact_names"],
        json!(["report"])
    );
    let again = check(&r.after, "test");
    assert_eq!(again["status"], "queued", "{again}");
    assert_ne!(again["attempt_id"], r.claim["attempt_id"]);
    let log = r.log().await;
    assert_eq!(log["log"], "");
    assert_eq!(log["pruned"], true);
    assert_ne!(r.artifact().await.status(), 200);
    // The runner's lease ended with the backup: nothing it sends lands.
    let (st, _) = r
        .runner
        .raw(reqwest::Method::POST, &r.claim, "log", b"late\n".to_vec())
        .await;
    assert_eq!(st, StatusCode::CONFLICT);
    assert_eq!(
        r.runner.finish(&r.claim, "success").await.0,
        StatusCode::CONFLICT
    );
    assert_eq!(r.log().await["log"], "");
    // The job queued again is not pruned: what it writes is its own.
    let next = r.runner.next_claim().await;
    assert_eq!(next["attempt_id"], again["attempt_id"]);
    let (st, _) = r
        .runner
        .raw(
            reqwest::Method::POST,
            &next,
            "log",
            b"after the restore\n".to_vec(),
        )
        .await;
    assert_eq!(st, 200);
    let id = next["attempt_id"].as_str().unwrap();
    let log =
        r.w.server
            .ok(&format!("{}/jobs/{id}/log", r.w.repo_path()), &r.w.aiko)
            .await;
    assert_eq!(log["log"], "after the restore\n");
    assert_eq!(log["pruned"], false);
}

#[tokio::test]
async fn author_yields_running_checks_deferred_by_review() {
    author_yields_only_deferred_running_checks("review").await;
}

#[tokio::test]
async fn author_yields_running_checks_deferred_by_failure() {
    author_yields_only_deferred_running_checks("failure").await;
}

#[tokio::test]
async fn author_yields_running_checks_deferred_by_draft() {
    author_yields_only_deferred_running_checks("draft").await;
}

async fn author_yields_only_deferred_running_checks(reason: &str) {
    let w = World::new().await;
    let runner = Runner::new(&w, "yield-runner").await;
    land(
        &w,
        "setup",
        &[(
            ".guildbotics-ci.toml",
            &if reason == "failure" {
                required_pipeline(&["test", "lint", "wait"])
            } else {
                pipeline(&["test", "lint", "wait"])
            },
        )],
        None,
    )
    .await;
    runner.drain("success").await;
    let head = w.push_change("feature", "change.txt", "change");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let path = format!("{}/pulls/{n}", w.repo_path());
    let yield_path = format!("{path}/checks/yield");
    let first = runner.claim().await.unwrap();
    let running = runner.claim().await.unwrap();
    assert_eq!(running["sha"], head);
    // A normal head is untouched, even while its checks are running.
    assert_eq!(
        w.server.post(&yield_path, &w.aiko, json!({})).await.1["yielded"],
        json!([])
    );
    for token in [&w.ken, &w.owner] {
        assert_eq!(w.server.post(&yield_path, token, json!({})).await.0, 403);
    }
    if reason == "failure" {
        assert_eq!(runner.finish(&first, "failure").await.0, 200);
    } else {
        assert_eq!(runner.finish(&first, "success").await.0, 200);
        if reason == "draft" {
            let current = w.server.ok(&path, &w.aiko).await;
            assert_eq!(
                w.server
                    .patch(
                        &path,
                        &w.aiko,
                        json!({"revision":current["revision"],"draft":true})
                    )
                    .await
                    .0,
                200
            );
        } else {
            let repo = w.server.ok(&w.repo_path(), &w.owner).await;
            assert_eq!(
                w.server
                    .patch(
                        &w.repo_path(),
                        &w.owner,
                        json!({"revision":repo["revision"],"required_approvals":1})
                    )
                    .await
                    .0,
                200
            );
            assert_eq!(
                w.server
                    .post(
                        &format!("{path}/reviews"),
                        &w.ken,
                        json!({"verdict":"request_changes","body":"Please fix","commit":head})
                    )
                    .await
                    .0,
                201
            );
        }
    }
    let before = checks(&w, n).await;
    let queued = before["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["status"] == "queued")
        .unwrap()
        .clone();
    let (st, result) = w.server.post(&yield_path, &w.aiko, json!({})).await;
    assert_eq!(st, 200, "{result}");
    assert_eq!(result["head"], head);
    assert_eq!(result["yielded"].as_array().unwrap().len(), 1);
    let next = &result["yielded"][0];
    assert_eq!(next["status"], "queued");
    assert_eq!(next["attempt"], 2);
    assert_eq!(next["cancel_requested"], false);
    let job = running["job"]["name"].as_str().unwrap();
    let report = checks(&w, n).await;
    assert_eq!(check(&report, job)["status"], "queued");
    assert!(check(&report, job)["conclusion"].is_null());
    assert!(
        check(&report, job)["details"]
            .as_str()
            .unwrap()
            .contains("Deferred")
    );
    assert_eq!(check(&report, queued["name"].as_str().unwrap()), &queued);
    assert_eq!(
        w.server.post(&yield_path, &w.aiko, json!({})).await.1["yielded"],
        json!([])
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
    assert_eq!(old["cancel_requested"], true);
    assert!(
        old["details"]
            .as_str()
            .unwrap()
            .starts_with("Yielded: aiko")
    );
    let (st, finished) = runner.finish(&running, "failure").await;
    assert_eq!(st, 200, "{finished}");
    assert_eq!(finished["conclusion"], "cancelled");
    assert_eq!(check(&checks(&w, n).await, job)["status"], "queued");
    if reason == "review" {
        assert_eq!(
            w.server
                .post(
                    &format!("{path}/comments"),
                    &w.aiko,
                    json!({"body":"Explained"})
                )
                .await
                .0,
            201
        );
        let replacement = runner.claim().await.unwrap();
        assert_eq!(replacement["attempt_id"], next["id"]);
        assert_eq!(runner.finish(&replacement, "success").await.0, 200);
    } else {
        w.push_change("feature", "change.txt", "next");
        let view = w
            .server
            .ok(
                &format!("{}/jobs/{}", w.repo_path(), next["id"].as_str().unwrap()),
                &w.aiko,
            )
            .await;
        assert_eq!(view["conclusion"], "cancelled");
    }
}

mod yield_noops_for_closed_protected_approved_outdated_and_answered_heads {
    use super::*;

    struct Fixture {
        world: World,
        runner: Runner,
        head: String,
        path: String,
    }

    impl Fixture {
        async fn new(with_pipeline: bool) -> Self {
            let world = World::new().await;
            let runner = Runner::new(&world, "yield-noop").await;
            if with_pipeline {
                land(
                    &world,
                    "setup",
                    &[(".guildbotics-ci.toml", &pipeline(&["test"]))],
                    None,
                )
                .await;
                runner.drain("success").await;
            }
            let head = world.push_change("feature", "change.txt", "change");
            let pull = world.open_pull("feature", json!([])).await;
            let path = format!("{}/pulls/{}", world.repo_path(), pull["number"]);
            let repo = world.server.ok(&world.repo_path(), &world.owner).await;
            let (status, result) = world
                .server
                .patch(
                    &world.repo_path(),
                    &world.owner,
                    json!({"revision": repo["revision"], "required_approvals": 1}),
                )
                .await;
            assert_eq!(status, 200, "{result}");
            let (status, result) = world
                .server
                .post(
                    &format!("{path}/reviews"),
                    &world.ken,
                    json!({"verdict": "request_changes", "body": "Change", "commit": head}),
                )
                .await;
            assert_eq!(status, 201, "{result}");
            Self {
                world,
                runner,
                head,
                path,
            }
        }

        async fn assert_empty_yield(&self) {
            let (status, result) = self
                .world
                .server
                .post(
                    &format!("{}/checks/yield", self.path),
                    &self.world.aiko,
                    json!({}),
                )
                .await;
            assert_eq!(status, 200, "{result}");
            assert_eq!(result["head"], self.head);
            assert_eq!(result["yielded"], json!([]), "{result}");
        }

        async fn assert_running_check_untouched(&self) {
            let running = self.runner.next_claim().await;
            assert_eq!(running["sha"], self.head);
            self.assert_empty_yield().await;
            let world = &self.world;
            let job = world
                .server
                .ok(
                    &format!(
                        "{}/jobs/{}",
                        world.repo_path(),
                        running["attempt_id"].as_str().unwrap()
                    ),
                    &world.aiko,
                )
                .await;
            assert_eq!(job["cancel_requested"], false, "{job}");
            assert_eq!(job["status"], "in_progress");
        }
    }

    #[tokio::test]
    async fn closed() {
        let f = Fixture::new(true).await;
        let (status, result) = f
            .world
            .server
            .post(&format!("{}/close", f.path), &f.world.aiko, json!({}))
            .await;
        assert_eq!(status, 200, "{result}");
        f.assert_running_check_untouched().await;
    }

    #[tokio::test]
    async fn protected() {
        let f = Fixture::new(true).await;
        let w = &f.world;
        let repo = w.server.ok(&w.repo_path(), &w.owner).await;
        let (status, result) = w
            .server
            .patch(
                &w.repo_path(),
                &w.owner,
                json!({"revision": repo["revision"], "protected_branches": ["feature"]}),
            )
            .await;
        assert_eq!(status, 200, "{result}");
        f.assert_running_check_untouched().await;
    }

    #[tokio::test]
    async fn approved() {
        let f = Fixture::new(true).await;
        // An approval can meet the threshold while another review still requests changes.
        let (status, result) = f
            .world
            .server
            .post(
                &format!("{}/reviews", f.path),
                &f.world.owner,
                json!({"verdict": "approve", "commit": f.head}),
            )
            .await;
        assert_eq!(status, 201, "{result}");
        f.assert_running_check_untouched().await;
    }

    #[tokio::test]
    async fn outdated() {
        let mut f = Fixture::new(true).await;
        f.head = f.world.push_change("feature", "change.txt", "next");
        f.assert_running_check_untouched().await;
    }

    #[tokio::test]
    async fn answered() {
        let f = Fixture::new(true).await;
        let (status, result) = f
            .world
            .server
            .post(
                &format!("{}/comments", f.path),
                &f.world.aiko,
                json!({"body": "Explained"}),
            )
            .await;
        assert_eq!(status, 201, "{result}");
        f.assert_running_check_untouched().await;
    }

    #[tokio::test]
    async fn other_pull() {
        let f = Fixture::new(true).await;
        let w = &f.world;
        // Both pulls share a head. The other pull still needs this check.
        git_as_ok(
            &w.work,
            "aiko",
            &w.aiko,
            &["push", "-q", "origin", "HEAD:refs/heads/other"],
        );
        let other = w.open_pull("other", json!([])).await;
        assert_eq!(other["head_sha"], f.head);
        f.assert_running_check_untouched().await;
    }

    #[tokio::test]
    async fn missing_run() {
        let mut f = Fixture::new(false).await;
        // The head has no configured jobs. Deleting it also leaves no run for
        // the pull's current head, which must still be a successful no-op.
        assert!(f.runner.claim().await.is_none());
        f.assert_empty_yield().await;
        let w = &f.world;
        git_as_ok(
            &w.work,
            "aiko",
            &w.aiko,
            &["push", "-q", "origin", ":refs/heads/feature"],
        );
        f.head.clear();
        f.assert_empty_yield().await;
    }
}

#[tokio::test]
async fn verification_api_auth_first_parent_and_cursor_pages() {
    let w = World::new().await;
    let path = "acme/history";
    assert_eq!(
        w.server
            .post(
                "/repos",
                &w.owner,
                json!({"path": path, "default_branch": "main", "labels": []})
            )
            .await
            .0,
        201
    );
    let url = format!("/repos/{}/verification", encode(path));
    let empty = w.server.ok(&url, &w.owner).await;
    assert_eq!(empty["state"], "empty");
    for key in ["tip", "ahead", "latest_verified", "current"] {
        assert!(empty[key].is_null());
    }
    assert_eq!(w.server.get(&url, "").await.0, 401);
    assert_eq!(w.server.get(&format!("{url}/commits"), "").await.0, 401);
    let work = w.dir.path().join("history");
    std::fs::create_dir(&work).unwrap();
    git_ok(&work, &["init", "-q", "-b", "main"]);
    let runner = Runner::new(&w, "verification-reader").await;
    std::fs::write(work.join(".guildbotics-ci.toml"), pipeline(&["verify"])).unwrap();
    git_ok(&work, &["add", ".guildbotics-ci.toml"]);
    for i in 0..32 {
        git_ok(
            &work,
            &["commit", "--allow-empty", "-qm", &format!("main {i}")],
        );
    }
    git_ok(&work, &["checkout", "-qb", "side"]);
    git_ok(&work, &["commit", "--allow-empty", "-qm", "side only"]);
    git_ok(&work, &["checkout", "-q", "main"]);
    git_ok(&work, &["merge", "--no-ff", "-qm", "merge side", "side"]);
    git_as_ok(
        &work,
        "alice",
        &w.owner_git,
        &["push", "-q", &w.server.remote(path), "main"],
    );
    let running = w.server.ok(&url, &w.owner).await;
    assert_eq!(running["state"], "verifying");
    assert_eq!(running["ahead"], 33);
    assert!(running["latest_verified"].is_null());
    assert_eq!(running["current"]["retry"]["number"], 0);
    assert_eq!(
        w.server.ok(&format!("{url}/commits"), &w.aiko).await["items"][0]["mark"],
        "verifying"
    );
    let alternate = Runner::new(&w, "runner2").await;
    let mut last = Value::Null;
    for (n, runner) in [&runner, &alternate]
        .into_iter()
        .cycle()
        .take(4)
        .enumerate()
    {
        let c = runner.next_claim().await;
        assert_eq!(runner.finish(&c, "failure").await.0, 200);
        eventually("retry projection", || async {
            let v = w.server.ok(&url, &w.owner).await;
            if n < 3 {
                v["current"]["retry"]["number"] == json!(n + 1)
            } else {
                v["state"] == "failing"
            }
        })
        .await;
        last = c;
    }
    let repair_url = format!("/repos/{}/tickets/1", encode(path));
    eventually(
        "verification event creates repair without the minute sweep",
        || async { w.server.get(&repair_url, &w.owner).await.0 == 200 },
    )
    .await;
    let repair = w.server.ok(&repair_url, &w.owner).await;
    assert_eq!(repair["repair"], true);
    assert_eq!(repair["author"], "branvil");
    assert_eq!(repair["lane"], "ready");
    assert!(repair["assignee"]["name"].is_string());
    assert!(
        repair["body"]
            .as_str()
            .unwrap()
            .contains(last["attempt_id"].as_str().unwrap())
    );
    let failed = w.server.ok(&url, &w.owner).await;
    assert_eq!(failed["repair"]["url"], {
        let (workspace, name) = path.split_once('/').unwrap();
        format!(
            "{}/workspaces/{workspace}/repos/{name}/tickets/1",
            w.server.url
        )
    });
    assert_eq!(failed["failing_since"], failed["tip"]);
    assert_eq!(
        w.server.ok(&format!("{url}/commits"), &w.aiko).await["items"][0]["mark"],
        "failed"
    );
    let retry_url = format!(
        "/repos/{}/jobs/{}/retry",
        encode(path),
        last["attempt_id"].as_str().unwrap()
    );
    assert_eq!(w.server.post(&retry_url, &w.owner, json!({})).await.0, 200);
    assert_eq!(w.server.ok(&url, &w.owner).await["state"], "verifying");
    let retry = runner.next_claim().await;
    assert_eq!(runner.finish(&retry, "success").await.0, 200);
    eventually("verification finishes", || async {
        w.server.ok(&url, &w.owner).await["state"] == "verified"
    })
    .await;
    let state = w.server.ok(&url, &w.owner).await;
    assert_eq!(
        state["runner_recoveries"],
        json!([{
            "name":"verify", "failed_attempt":last["attempt_id"], "failed_runner":"runner2",
            "successful_runner":"verification-reader"
        }])
    );
    let repaired = w.server.ok(&repair_url, &w.owner).await;
    assert_eq!(repaired["state"], "closed");
    assert_eq!(repaired["state_reason"], "completed");
    assert_eq!(repaired["lane"], "done");
    let state = w.server.ok(&url, &w.aiko).await;
    assert_eq!(state["ahead"], 0);
    assert_eq!(state["tip"], state["latest_verified"]["sha"]);
    assert_eq!(state["current"]["retry"], json!({"number": 3, "max": 3}));
    let first = w.server.ok(&format!("{url}/commits"), &w.owner).await;
    assert_eq!(first["items"].as_array().unwrap().len(), 30);
    assert_eq!(first["items"][0]["title"], "merge side");
    assert_eq!(first["items"][0]["mark"], "verified");
    assert_eq!(first["items"][1]["title"], "main 31");
    assert!(first["items"][1]["mark"].is_null());
    let cursor = first["next_cursor"].as_str().unwrap();
    let second = w
        .server
        .ok(
            &format!("{url}/commits?cursor={}", encode(cursor)),
            &w.owner,
        )
        .await;
    assert_eq!(second["items"].as_array().unwrap().len(), 3);
    assert_eq!(second["items"][0]["title"], "main 2");
    assert_eq!(second["items"][2]["title"], "main 0");
    assert!(second["next_cursor"].is_null());
    assert_eq!(
        w.server
            .get(&format!("{url}/commits?cursor={}", encode(cursor)), &w.aiko)
            .await
            .0,
        400
    );

    // Once a base exists, a later failed main creates diagnostic attempts,
    // visible as jobs without changing the verified commit's checks.
    let base = state["tip"].as_str().unwrap();
    let checks_url = format!("/repos/{}/commits/{base}/checks", encode(path));
    let baseline = w.server.ok(&checks_url, &w.owner).await;
    git_ok(&work, &["checkout", "-qb", "probe-head"]);
    std::fs::write(work.join("changed.txt"), "changed").unwrap();
    git_ok(&work, &["add", "changed.txt"]);
    git_ok(&work, &["commit", "-qm", "failing main"]);
    git_as_ok(
        &work,
        "alice",
        &w.owner_git,
        &["push", "-q", &w.server.remote(path), "probe-head"],
    );
    let pulls = format!("/repos/{}/pulls", encode(path));
    let (status, body) = w
        .server
        .post(
            &pulls,
            &w.owner,
            json!({"title":"probe test", "head":"probe-head", "base":"main"}),
        )
        .await;
    assert_eq!(status, 201, "{body}");
    let pull = w.server.ok(&format!("{pulls}/1"), &w.owner).await;
    let (status, body) = w
        .server
        .post(
            &format!("{pulls}/1/reviews"),
            &w.aiko,
            json!({"verdict":"approve", "commit":pull["head_sha"]}),
        )
        .await;
    assert_eq!(status, 201, "{body}");
    let (status, body) = w
        .server
        .post(
            &format!("{pulls}/1/merge"),
            &w.owner,
            json!({"expected_head":pull["head_sha"],"expected_base":pull["base_sha"]}),
        )
        .await;
    assert_eq!(status, 200, "{body}");
    let alternate = Runner::new(&w, "probe-retry-runner").await;
    for runner in [&runner, &alternate].into_iter().cycle().take(4) {
        let c = runner.next_claim().await;
        assert!(c.get("purpose").is_none());
        assert_eq!(runner.finish(&c, "failure").await.0, 200);
    }
    let probe = runner.next_claim().await;
    assert!(probe.get("purpose").is_none());
    assert_eq!(probe["cache_write"], false);
    assert_eq!(probe["sha"], base);
    assert!(probe["reuse"].is_null());
    let job_url = format!(
        "/repos/{}/jobs/{}",
        encode(path),
        probe["attempt_id"].as_str().unwrap()
    );
    assert_eq!(
        w.server.ok(&job_url, &w.aiko).await["retry_supported"],
        false
    );
    assert_eq!(
        w.server.get(&format!("{job_url}/log"), &w.aiko).await.0,
        200
    );
    assert_eq!(runner.finish(&probe, "failure").await.0, 200);
    assert_eq!(
        w.server
            .post(&format!("{job_url}/retry"), &w.owner, json!({}))
            .await
            .0,
        409
    );
    assert_eq!(w.server.ok(&checks_url, &w.owner).await, baseline);
    assert_eq!(
        w.server.ok(&url, &w.owner).await["latest_verified"]["sha"],
        base
    );
}
