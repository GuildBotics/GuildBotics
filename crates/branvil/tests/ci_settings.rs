//! The owner's CI settings (`/api/v1/admin/ci`, Settings → CI) against a real
//! service: only the owner reads or changes them, the runner mode and the job
//! limits are written to `branvil.toml` and survive a restart, an image the
//! default branches' pipelines name and the cache lacks is listed with its
//! repositories, and
//! image operations refuse what is not an image reference or a regular file.

use crate::common;

use common::*;
use reqwest::StatusCode;
use serde_json::{Value, json};

const IMAGE: &str = "docker.io/library/alpine:3.22";

/// Lands `.branvil/ci.toml` on main through a merged pull request.
async fn land_pipeline(w: &World) {
    std::fs::create_dir_all(w.work.join(".branvil")).unwrap();
    let pipeline =
        format!("[[jobs]]\nname = \"test\"\nimage = \"{IMAGE}\"\ncommands = [\"true\"]\n");
    let head = w.push_change("ci", ".branvil/ci.toml", &pipeline);
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
    std::fs::read_to_string(w.server.data.join("branvil.toml")).unwrap()
}

async fn settings(w: &World) -> Value {
    w.server.ok("/admin/ci", &w.owner).await
}

/// Restarts the service with a home of its own: the supervised runner and
/// the image cache stay out of the user's `~/.branvil`, and no msb is found.
fn isolate(w: &mut World) {
    let home = w.dir.path().join("home");
    std::fs::create_dir_all(&home).unwrap();
    // Stopped as a signal stops it, so its supervised runner stops with it
    // (a killed service would leave the runner holding its work directory).
    #[cfg(unix)]
    w.server.terminate(std::time::Duration::from_secs(10));
    w.restart(&[("HOME", home.to_str().unwrap())]);
}

#[tokio::test]
async fn the_owner_changes_the_runner_and_limits_and_they_survive_a_restart() {
    let mut w = World::new().await;
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
        json!({"max_cpus": 4, "max_memory_mib": 4096, "max_timeout_secs": 3600, "cache_max_mib": 2048, "max_jobs": 1})
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

    let limits = json!({"max_cpus": 8, "max_memory_mib": 8192, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2});
    let (st, v) = w
        .server
        .put("/admin/ci/limits", &w.owner, limits.clone())
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["limits"], limits);
    for bad in [
        json!({"max_cpus": 0, "max_memory_mib": 8192, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8, "max_memory_mib": 64, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "max_timeout_secs": 0, "cache_max_mib": 4096, "max_jobs": 2}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "max_timeout_secs": 5400, "cache_max_mib": 4096, "max_jobs": 0}),
        json!({"max_cpus": 8, "max_memory_mib": 8192, "max_timeout_secs": 5400, "cache_max_mib": 4096}),
        json!({"max_cpus": 8}),
    ] {
        let (st, v) = w
            .server
            .put("/admin/ci/limits", &w.owner, bad.clone())
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{bad}: {v}");
    }

    // The init's settings of the file are kept beside the new ones.
    let text = config_text(&w);
    assert!(
        text.contains("listen = ") && text.contains("[limits]"),
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
    // The stopped runner is forgotten at once, not counted for minutes.
    assert_eq!(v["runner"]["runners"], json!([]), "{v}");
    assert_eq!(v["runner"]["state"], "no_runner");
    let diagnostics = w.server.ok("/admin/diagnostics", &w.owner).await;
    assert_eq!(diagnostics["runner"]["mode"], "off");
}

/// Waits until the supervised runner has contacted the service.
async fn runner_connected(w: &World) {
    for _ in 0..300 {
        let v = settings(w).await;
        if v["runner"]["runners"]
            .as_array()
            .is_some_and(|r| r.iter().any(|r| r["name"] == "branvil-runner"))
        {
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
