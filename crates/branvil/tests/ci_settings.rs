//! The owner's CI settings (`/api/v1/admin/ci`, Settings → CI) against a real
//! service: only the owner reads or changes them, the runner mode and the job
//! limits are written to `config.toml` and survive a restart, an image the
//! default branches' pipelines name and the cache lacks is listed with its
//! repositories, and
//! image operations refuse what is not an image reference or a regular file.

use crate::{ci::eventually, common};

use common::*;
use reqwest::StatusCode;
use serde_json::{Value, json};

const IMAGE: &str = "docker.io/library/alpine:3.22";

/// Lands `.guildbotics-ci.toml` on main through a merged pull request.
async fn land_pipeline(w: &World) {
    let pipeline =
        format!("[[jobs]]\nname = \"test\"\nimage = \"{IMAGE}\"\ncommands = [\"true\"]\n");
    let head = w.push_change("ci", ".guildbotics-ci.toml", &pipeline);
    let p = w.open_pull("ci", json!([])).await;
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{}/merge", w.repo_path(), p["number"]),
            &w.owner,
            json!({"expected_head": head, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
}

fn config_text(w: &World) -> String {
    std::fs::read_to_string(w.server.data.join("config.toml")).unwrap()
}

async fn settings(w: &World) -> Value {
    w.server.ok("/admin/ci", &w.owner).await
}

/// Restarts the service with a home of its own: the supervised runner and
/// the image cache stay out of the user's machine root, and no msb is found.
fn isolate(w: &mut World) {
    let home = w.dir.path().join("home");
    std::fs::create_dir_all(&home).unwrap();
    // Stopped as a signal stops it, so its supervised runner stops with it
    // (a killed service would leave the runner holding its work directory).
    #[cfg(unix)]
    w.server.terminate(std::time::Duration::from_secs(10));
    let root = home.join(".guildbotics");
    w.restart(&[
        ("HOME", home.to_str().unwrap()),
        ("GUILDBOTICS_HOME", root.to_str().unwrap()),
    ]);
}

#[tokio::test]
async fn the_owner_changes_the_runner_and_limits_and_they_survive_a_restart() {
    let mut w = World::new().await;
    // A setting of the file before the owner changes any.
    std::fs::write(
        w.server.data.join("config.toml"),
        "# kept\n[limits]\ngit_concurrency = 8\n",
    )
    .unwrap();
    isolate(&mut w);
    land_pipeline(&w).await;

    for (method, path, body) in [
        ("GET", "/admin/ci", json!(null)),
        ("PUT", "/admin/ci/runner", json!({"mode": "auto"})),
        ("POST", "/admin/ci/images/pull", json!({"reference": IMAGE})),
    ] {
        let (st, v) = match method {
            "GET" => w.server.get(path, &w.aiko).await,
            "PUT" => w.server.put(path, &w.aiko, body).await,
            _ => w.server.post(path, &w.aiko, body).await,
        };
        assert_eq!(st, StatusCode::FORBIDDEN, "{method} {path}: {v}");
    }

    let v = settings(&w).await;
    assert_eq!(v["runner"]["mode"], "off");
    assert_eq!(
        v["limits"],
        json!({"max_cpus": 4, "max_memory_mib": 4096, "work_quota_mib": 16384, "max_timeout_secs": 3600, "cache_max_mib": 16384, "max_jobs": 1, "memory_reserve_percent": 10, "cpu_multiplier_percent": 0})
    );
    assert_eq!(
        v["repositories"],
        json!([{"path": REPO, "ci_network": false}])
    );
    // No msb in the isolated home, so the image the pipeline names is missing.
    assert_eq!(
        v["images"]["missing"],
        json!([{"image": IMAGE, "repositories": [{"repository": REPO, "jobs": ["test"]}]}])
    );
    assert_eq!(v["images"]["cached"], json!([]));
    assert!(v["operation"].is_null());

    let (st, v) = w
        .server
        .put("/admin/ci/runner", &w.owner, json!({"mode": "auto"}))
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["runner"]["mode"], "auto");
    assert!(
        config_text(&w).contains("runner = \"auto\""),
        "{}",
        config_text(&w)
    );

    let limits = json!({"max_cpus": 8, "max_memory_mib": 8192, "work_quota_mib": 32768, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2, "memory_reserve_percent": 15, "cpu_multiplier_percent": 125});
    let (st, v) = w
        .server
        .put("/admin/ci/limits", &w.owner, limits.clone())
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["limits"], limits);
    for bad in [
        json!({"max_cpus": 0, "max_memory_mib": 8192, "work_quota_mib": 32768, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8, "max_memory_mib": 64, "work_quota_mib": 32768, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "work_quota_mib": 512, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "work_quota_mib": 32768, "max_timeout_secs": 0, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "work_quota_mib": 32768, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 0}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "work_quota_mib": 32768, "max_timeout_secs": 5400, "cache_max_mib": 4096}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8}),
    ] {
        let (st, v) = w
            .server
            .put("/admin/ci/limits", &w.owner, bad.clone())
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{bad}: {v}");
    }

    // The file's own settings are kept beside the new ones.
    let text = config_text(&w);
    assert!(
        text.contains("# kept") && text.contains("[limits]"),
        "{text}"
    );
    isolate(&mut w);
    let v = settings(&w).await;
    assert_eq!(v["runner"]["mode"], "auto");
    assert_eq!(v["limits"], limits);
    // The saved mode started the runner with the service.
    runner_connected(&w).await;

    let (st, v) = w
        .server
        .put("/admin/ci/runner", &w.owner, json!({"mode": "off"}))
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["runner"]["mode"], "off");
    // The supervisor stops it asynchronously. Its last report stays in the list.
    eventually("runner stopped", || {
        let w = &w;
        async move {
            let v = settings(w).await;
            v["runner"]["state"] == "no_runner" && row(&v, "hub-runner")["connection"] == "offline"
        }
    })
    .await;
    let diagnostics = w.server.ok("/admin/diagnostics", &w.owner).await;
    assert_eq!(diagnostics["runner"]["mode"], "off");
}

fn row<'a>(view: &'a Value, name: &str) -> &'a Value {
    view["runner"]["runners"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["name"] == name)
        .unwrap()
}

#[tokio::test]
async fn the_runner_list_combines_members_reports_attempts_and_saved_limits() {
    let mut w = World::new().await;
    std::fs::write(
        w.server.data.join("config.toml"),
        "[ci.executor]\nmax_cpus = 6\n",
    )
    .unwrap();
    isolate(&mut w);
    let remote = w.server.member(&w.owner, "remote-runner", "runner").await;
    let idle = w.server.member(&w.owner, "idle-runner", "runner").await;
    land_pipeline(&w).await;

    let v = settings(&w).await;
    assert_eq!(v["runner"]["runners"].as_array().unwrap().len(), 2);
    let idle_row = row(&v, "idle-runner");
    assert_eq!(idle_row["connection"], "never_seen");
    assert!(idle_row["last_seen_at"].is_null());
    assert_eq!(idle_row["limits"]["max_cpus"], 6);

    let path = "/admin/ci/runners/idle-runner/limits";
    let limits = json!({"max_cpus": 8, "max_memory_mib": 8192, "work_quota_mib": 32768, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 3, "memory_reserve_percent": 15, "cpu_multiplier_percent": 125});
    for token in [&w.aiko, &remote] {
        assert_eq!(
            w.server.put(path, token, limits.clone()).await.0,
            StatusCode::FORBIDDEN
        );
    }
    assert_eq!(
        w.server
            .put("/admin/ci/runners/aiko/limits", &w.owner, limits.clone())
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        w.server.get("/runner/health", &w.owner).await.0,
        StatusCode::FORBIDDEN
    );
    // Only an image for the reporting runner's CPU counts.
    let mut report = json!({
        "executor": "fixture", "executor_state": "ready", "version": "older-build",
        "max_jobs": 1, "running_jobs": 0, "accept": true, "architecture": "amd64",
        "images": [{"reference": IMAGE, "architecture": "arm64", "digest": format!("sha256:{}", "a".repeat(64))}]
    });
    assert_eq!(
        w.server
            .post("/runner/claim", &remote, report.clone())
            .await
            .0,
        StatusCode::NO_CONTENT
    );
    let v = settings(&w).await;
    let remote_row = row(&v, "remote-runner");
    assert_eq!(remote_row["connection"], "online");
    assert!(remote_row["last_seen_at"].is_string());
    assert_eq!(remote_row["executor_state"], "ready");
    assert_eq!(remote_row["version_matches"], false);
    assert_eq!(remote_row["missing_images"], json!([IMAGE]));
    assert_eq!(remote_row["running_jobs"], 0);

    for info in [
        v["runner"].clone(),
        w.server.ok("/admin/diagnostics", &w.owner).await["runner"].clone(),
    ] {
        assert_eq!(info["state"], "executor_unavailable");
        assert_eq!(
            info["version_mismatch"],
            json!({"name": "remote-runner", "version": "older-build"})
        );
        let detail = info["detail"].as_str().unwrap();
        assert!(
            detail.contains("remote-runner") && detail.contains("older-build"),
            "{detail}"
        );
    }

    // Explicit settings remain an override even when they equal the defaults.
    let defaults = v["limits"].clone();
    let (status, v) = w.server.put(path, &w.owner, defaults).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(row(&v, "idle-runner")["limits_source"], "runner");

    let (status, v) = w.server.put(path, &w.owner, limits.clone()).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(row(&v, "idle-runner")["limits"], limits);
    assert_eq!(row(&v, "idle-runner")["limits_source"], "runner");
    assert_eq!(w.server.ok("/runner/health", &idle).await["max_jobs"], 3);
    assert_eq!(
        row(&settings(&w).await, "idle-runner")["connection"],
        "never_seen"
    );

    // A usable runner takes precedence over a simultaneously online mismatch.
    let mut peer = report.clone();
    peer["accept"] = json!(false);
    assert_eq!(
        w.server.post("/runner/claim", &idle, peer).await.0,
        StatusCode::NO_CONTENT
    );

    report["version"] = json!(gb_base::BUILD_VERSION);
    report["images"][0]["architecture"] = json!("amd64");
    report["running_jobs"] = json!(1);
    assert_eq!(
        w.server
            .post("/runner/claim", &remote, report.clone())
            .await
            .0,
        StatusCode::NO_CONTENT
    );
    report["running_jobs"] = json!(0);
    report["machine"] =
        json!({"logical_cpus": 4, "total_memory_mib": 10000, "available_memory_mib": 3000});
    assert_eq!(
        w.server
            .post("/runner/claim", &remote, report.clone())
            .await
            .0,
        StatusCode::NO_CONTENT
    );
    let blocked = settings(&w).await;
    let waiting = &row(&blocked, "remote-runner")["resource_wait"];
    assert_eq!(waiting["job"], "test");
    assert_eq!(waiting["memory_shortfall_mib"], 2096);
    report["machine"] = Value::Null;
    let (status, claim) = w.server.post("/runner/claim", &remote, report).await;
    assert_eq!(status, StatusCode::OK, "{claim}");
    assert_eq!(claim["limits"]["max_cpus"], 6);
    assert_eq!(claim["limits"]["max_jobs"], 1);
    let v = settings(&w).await;
    let remote_row = row(&v, "remote-runner");
    assert_eq!(remote_row["version_matches"], true);
    assert_eq!(remote_row["missing_images"], json!([]));
    assert_eq!(remote_row["running_jobs"], 1);
    assert!(remote_row["machine"].is_null());
    assert!(remote_row["resource_wait"].is_null());
    assert_eq!(v["runner"]["state"], "ready");
    assert!(v["runner"]["version_mismatch"].is_null());
    assert_eq!(row(&v, "idle-runner")["connection"], "online");
    assert_eq!(row(&v, "idle-runner")["version_matches"], false);

    let finish = format!(
        "/runner/attempts/{}/finish",
        claim["attempt_id"].as_str().unwrap()
    );
    let (status, result) = w
        .server
        .post(
            &finish,
            &remote,
            json!({
                "lease": claim["lease"], "conclusion": "success", "details": "finished",
                "image_digest": format!("sha256:{}", "a".repeat(64)),
                "peak_memory_mib": 5000,
            }),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{result}");
    assert_eq!(
        result["allocation"],
        json!({"cpus": 6, "vm_memory_mib": 4096, "reserved_memory_mib": 4096})
    );
    assert_eq!(result["peak_memory_mib"], 5000);
    assert_eq!(row(&settings(&w).await, "remote-runner")["running_jobs"], 0);

    isolate(&mut w);
    let v = settings(&w).await;
    assert_eq!(row(&v, "remote-runner")["connection"], "never_seen");
    assert_eq!(row(&v, "remote-runner")["limits_source"], "default");
    assert!(row(&v, "remote-runner")["version"].is_null());
    assert_eq!(row(&v, "idle-runner")["limits"], limits);
    assert_eq!(row(&v, "idle-runner")["limits_source"], "runner");
    assert_eq!(w.server.ok("/runner/health", &idle).await["max_jobs"], 3);
}

#[tokio::test]
async fn job_details_keep_the_claim_and_the_other_runners_latest_shortfall() {
    let w = World::new().await;
    let small = w.server.member(&w.owner, "small", "runner").await;
    let large = w.server.member(&w.owner, "large", "runner").await;
    land_pipeline(&w).await;
    let mut report = json!({
        "executor": "fixture", "executor_state": "ready", "version": gb_base::BUILD_VERSION,
        "max_jobs": 1, "running_jobs": 0, "accept": true, "architecture": "amd64",
        "images": [{"reference": IMAGE, "architecture": "amd64", "digest": format!("sha256:{}", "a".repeat(64))}],
        "machine": {"logical_cpus": 8, "total_memory_mib": 10000, "available_memory_mib": 3000}
    });
    for available in [3000, 4000] {
        report["machine"]["available_memory_mib"] = json!(available);
        assert_eq!(
            w.server
                .post("/runner/claim", &small, report.clone())
                .await
                .0,
            StatusCode::NO_CONTENT
        );
    }
    report["machine"]["available_memory_mib"] = json!(8000);
    let (status, claim) = w.server.post("/runner/claim", &large, report).await;
    assert_eq!(status, StatusCode::OK, "{claim}");
    let id = claim["attempt_id"].as_str().unwrap();
    let job = w
        .server
        .ok(&format!("{}/jobs/{id}", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(job["runner"], "large");
    assert_eq!(
        job["runner_claim"],
        json!({
            "machine": {"logical_cpus": 8, "total_memory_mib": 10000, "available_memory_mib": 8000,
                "commit_available_mib": null},
            "memory_samples": 0,
            "budget": {"memory_reserve_mib": 1000, "remaining_cpus": 4, "remaining_memory_mib": 2904,
                "remaining_commit_mib": null}
        })
    );
    let deferrals = job["runner_deferrals"].as_object().unwrap();
    assert_eq!(deferrals.len(), 1);
    let wait = &deferrals["small"];
    assert_eq!(wait["attempt_id"], id);
    assert_eq!(wait["cpu_shortfall"], 0);
    assert_eq!(wait["memory_shortfall_mib"], 1096);
    assert_eq!(wait["machine"]["available_memory_mib"], 4000);
    assert!(wait["at"].is_string());
}

/// Waits until the supervised runner has contacted the service.
async fn runner_connected(w: &World) {
    for _ in 0..300 {
        let v = settings(w).await;
        if v["runner"]["runners"].as_array().is_some_and(|r| {
            r.iter()
                .any(|r| r["name"] == "hub-runner" && r["connection"] == "online")
        }) {
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    panic!("the supervised runner never contacted the service");
}

#[tokio::test]
async fn image_operations_take_only_image_references_and_regular_files() {
    let mut w = World::new().await;
    isolate(&mut w);
    let dir = w.dir.path();
    std::fs::create_dir_all(dir.join("folder")).unwrap();
    std::fs::write(dir.join("image.tar"), "not really").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(dir.join("image.tar"), dir.join("link.tar")).unwrap();

    let digest = "alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8";
    for reference in ["", "-x", "alpine@sha256:short", digest, "a b"] {
        let (st, v) = w
            .server
            .post(
                "/admin/ci/images/pull",
                &w.owner,
                json!({"reference": reference}),
            )
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{reference:?}: {v}");
    }
    let mut paths = vec![
        "image.tar".to_string(),
        dir.join("missing.tar").display().to_string(),
        dir.join("folder").display().to_string(),
    ];
    if cfg!(unix) {
        paths.push(dir.join("link.tar").display().to_string());
    }
    for path in paths {
        let (st, v) = w
            .server
            .post("/admin/ci/images/load", &w.owner, json!({"path": path}))
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{path}: {v}");
    }
    assert!(settings(&w).await["operation"].is_null());

    // A regular file is accepted and the operation reported; what msb makes
    // of it (here: nothing, or no msb at all) ends it as failed.
    let (st, v) = w
        .server
        .post(
            "/admin/ci/images/load",
            &w.owner,
            json!({"path": dir.join("image.tar").display().to_string()}),
        )
        .await;
    assert_eq!(st, StatusCode::ACCEPTED, "{v}");
    assert_eq!(v["operation"]["kind"], "load");
    let mut op = v["operation"].clone();
    for _ in 0..600 {
        if op["state"] != "running" {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        op = settings(&w).await["operation"].clone();
    }
    assert_eq!(op["state"], "failed", "{op}");
    assert!(op["detail"].as_str().is_some_and(|d| !d.is_empty()), "{op}");
}

/// A runner that is no longer used is deleted from Settings → CI once it
/// has stopped: its token stops working, it leaves the list and
/// `config.toml`, the jobs it ran keep its name, and it can join again.
#[tokio::test]
async fn the_owner_deletes_a_stopped_runner_and_it_can_join_again() {
    let mut w = World::new().await;
    std::fs::write(
        w.server.data.join("config.toml"),
        "# kept\n[limits]\ngit_concurrency = 8\n\n[ci]\nrunner_absent_secs = 2\nlease_secs = 600\n",
    )
    .unwrap();
    isolate(&mut w);
    let token = w.server.member(&w.owner, "old-runner", "runner").await;
    let other = w.server.member(&w.owner, "kept-runner", "runner").await;
    let runner = crate::ci::Runner::at(&w.server.url, &token);
    let limits = json!({"max_cpus": 8, "max_memory_mib": 8192, "work_quota_mib": 32768, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 3, "memory_reserve_percent": 15, "cpu_multiplier_percent": 125});
    for name in ["old-runner", "kept-runner"] {
        let path = format!("/admin/ci/runners/{name}/limits");
        assert_eq!(
            w.server.put(&path, &w.owner, limits.clone()).await.0,
            StatusCode::OK
        );
    }
    land_pipeline(&w).await;
    let claim = runner.next_claim().await;
    let path = "/admin/ci/runners/old-runner";

    // Running: it reported a moment ago.
    let (status, v) = w.server.delete(path, &w.owner).await;
    assert_eq!(status, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "runner_online");
    // Stopped, but still holding the attempt it claimed.
    eventually("the runner to go offline", || {
        let w = &w;
        async move { row(&settings(w).await, "old-runner")["connection"] == "offline" }
    })
    .await;
    let (status, v) = w.server.delete(path, &w.owner).await;
    assert_eq!(status, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "runner_holds_attempts");
    assert_eq!(
        w.server.get("/runner/health", &token).await.0,
        StatusCode::OK
    );
    assert!(config_text(&w).contains("old-runner"));
    assert_eq!(runner.finish(&claim, "success").await.0, StatusCode::OK);

    // Only the owner deletes, and only runners.
    for caller in [&w.aiko, &token, &other] {
        assert_eq!(w.server.delete(path, caller).await.0, StatusCode::FORBIDDEN);
    }
    for name in ["aiko", "missing"] {
        let (status, _) = w
            .server
            .delete(&format!("/admin/ci/runners/{name}"), &w.owner)
            .await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{name}");
    }

    let (status, v) = w.server.delete(path, &w.owner).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    let names: Vec<_> = v["runner"]["runners"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["kept-runner"]);
    assert_eq!(
        w.server.get("/runner/health", &token).await.0,
        StatusCode::UNAUTHORIZED
    );
    let text = config_text(&w);
    assert!(!text.contains("old-runner"), "{text}");
    assert!(
        text.contains("# kept") && text.contains("git_concurrency = 8"),
        "{text}"
    );
    assert!(text.contains("[ci.runner_limits.kept-runner]"), "{text}");
    assert_eq!(
        w.server.delete(path, &w.owner).await.0,
        StatusCode::NOT_FOUND
    );

    // The job it ran keeps its name, in the job and in the commit's checks.
    let job = w
        .server
        .ok(
            &format!(
                "{}/jobs/{}",
                w.repo_path(),
                claim["attempt_id"].as_str().unwrap()
            ),
            &w.aiko,
        )
        .await;
    assert_eq!(job["runner"], "old-runner", "{job}");
    let checks = w
        .server
        .ok(
            &format!(
                "{}/commits/{}/checks",
                w.repo_path(),
                claim["sha"].as_str().unwrap()
            ),
            &w.aiko,
        )
        .await;
    assert!(
        checks["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["runner"] == "old-runner"),
        "{checks}"
    );

    // The same name joins again, as a new runner with the Hub defaults; the
    // old token stays revoked.
    let joined = w.server.member(&w.owner, "old-runner", "runner").await;
    assert_eq!(
        w.server.get("/runner/health", &joined).await.0,
        StatusCode::OK
    );
    assert_eq!(
        w.server.get("/runner/health", &token).await.0,
        StatusCode::UNAUTHORIZED
    );
    let again = row(&settings(&w).await, "old-runner").clone();
    assert_eq!(again["connection"], "never_seen");
    assert_eq!(again["limits_source"], "default");
}
