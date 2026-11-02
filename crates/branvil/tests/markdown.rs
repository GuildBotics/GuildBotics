//! The display resolver and GUI consume the same examples and exact output.

use crate::common::*;
use serde_json::{Value, json};

#[tokio::test]
async fn resolved_markdown_matches_the_shared_gui_examples_and_tracks_state() {
    let w = World::new().await;
    let s = &w.server;
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/markdown-references.json")).unwrap();
    let targets = fixture["targets"].as_array().unwrap();
    w.ticket(targets[0]["title"].as_str().unwrap()).await;
    w.ticket(targets[1]["title"].as_str().unwrap()).await;
    assert_eq!(
        s.patch(
            &format!("{}/tickets/2", w.repo_path()),
            &w.owner,
            json!({"state": "closed", "state_reason": "completed"})
        )
        .await
        .0,
        200
    );
    for n in 1..=3 {
        let branch = format!("reference-{n}");
        w.push_change(&branch, &format!("{n}.txt"), "change\n");
        w.open_pull(&branch, json!([])).await;
        let path = format!("{}/pulls/{n}", w.repo_path());
        assert_eq!(
            s.patch(&path, &w.aiko, json!({"title": targets[n+1]["title"]}))
                .await
                .0,
            200
        );
        if n <= 2 {
            assert_eq!(
                s.post(&format!("{path}/close"), &w.owner, json!({}))
                    .await
                    .0,
                200
            );
        } else if n == 3 {
            crate::ci::eventually("reference PR's landing gate", || async {
                s.ok(&path, &w.owner).await["landing"]["state"] == "passed"
            })
            .await;
            let p = s.ok(&path, &w.owner).await;
            let (status, result) = s
                .post(
                    &format!("{path}/merge"),
                    &w.owner,
                    json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
                )
                .await;
            assert_eq!(status, 200, "{result}");
        }
    }
    assert_eq!(
        s.post(
            &format!("{}/pulls/1/reopen", w.repo_path()),
            &w.owner,
            json!({})
        )
        .await
        .0,
        200
    );
    let other = "acme/other";
    let other_path = format!("/repos/{}", encode(other));
    assert_eq!(
        s.post(
            "/repos",
            &w.owner,
            json!({"path": other, "default_branch": "main"})
        )
        .await
        .0,
        201
    );
    git_as_ok(
        &w.work,
        "alice",
        &w.owner,
        &["push", "-q", &s.remote(other), "HEAD:refs/heads/main"],
    );
    w.push_change("other-change", "other.txt", "other\n");
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &[
            "push",
            "-q",
            &s.remote(other),
            "HEAD:refs/heads/other-change",
        ],
    );
    assert_eq!(
        s.post(
            &format!("{other_path}/tickets"),
            &w.aiko,
            json!({"title": targets[5]["title"]})
        )
        .await
        .0,
        201
    );
    assert_eq!(
        s.post(
            &format!("{other_path}/pulls"),
            &w.aiko,
            json!({"head": "other-change", "title": targets[6]["title"]})
        )
        .await
        .0,
        201
    );

    let endpoint = format!("{}/markdown", w.repo_path());
    let base = fixture["base_url"].as_str().unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let source = case["source"].as_str().unwrap().replace(base, &s.url);
        let expected = case["markdown"]
            .as_str()
            .unwrap()
            .replace(base, &s.url)
            .replace(
                &format!("&#104;{}", &base[1..]),
                &format!("&#104;{}", &s.url[1..]),
            );
        let (status, result) = s.post(&endpoint, &w.aiko, json!({"source": source})).await;
        assert_eq!(status, 200, "{result}");
        assert_eq!(result, json!({"markdown": expected}), "{}", case["name"]);
    }
    // No cached status: closing a cross-repository target changes the next answer.
    assert_eq!(
        s.patch(
            &format!("{other_path}/tickets/1"),
            &w.owner,
            json!({"state": "closed", "state_reason": "not_planned"})
        )
        .await
        .0,
        200
    );
    let (_, result) = s
        .post(&endpoint, &w.aiko, json!({"source": "acme/other#1"}))
        .await;
    assert!(
        result["markdown"]
            .as_str()
            .unwrap()
            .contains("data-branvil-state=\"closed\"")
    );
    // Input and visibility obey the same boundary as ticket and PR reads.
    assert_eq!(
        s.post(&endpoint, "invalid", json!({"source": "#1"}))
            .await
            .0,
        401
    );
    let runner = s.member(&w.owner, "reference-runner", "runner").await;
    assert_eq!(
        s.post(&endpoint, &runner, json!({"source": "#1"})).await.0,
        403
    );
    assert_eq!(
        s.post(
            "/repos/missing%2Frepo/markdown",
            &w.aiko,
            json!({"source": "#1"})
        )
        .await
        .0,
        404
    );
    assert_eq!(
        s.post(&endpoint, &w.aiko, json!({"source": "", "unknown": true}))
            .await
            .0,
        400
    );
    assert_eq!(
        s.post(
            &endpoint,
            &w.aiko,
            json!({"source": "x".repeat((256 << 10) + 1)})
        )
        .await
        .0,
        413
    );
    assert_eq!(
        s.post(&endpoint, &w.owner, json!({"source": ""})).await.1,
        json!({"markdown": ""})
    );
}
