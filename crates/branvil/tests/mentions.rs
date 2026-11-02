//! Mention parsing, source lifetimes, workspace queries, cursors, and upgrades.
use crate::common::*;
use serde_json::{Value, json};

fn one() -> String {
    format!("/repos/{}/tickets/1", encode(REPO))
}

fn query() -> String {
    format!("/workspaces/{WS}/tickets?mentions=me")
}

#[tokio::test]
async fn mentions_use_ascii_boundaries_existing_members_and_occurrence_order() {
    let w = World::new().await;
    let s = &w.server;
    s.member(&w.owner, "build.bot", "agent").await;
    s.member(&w.owner, "agent_x", "agent").await;
    w.ticket("Mention grammar").await;
    for (body, expected) in [
        (
            "@KEN, @AIKO @ken @missing @build.bot. @agent_x!",
            json!(["ken", "aiko", "build.bot", "agent_x"]),
        ),
        ("x@aiko _@aiko .@aiko @@aiko -@aiko", json!([])),
        ("@aikox @aiko-bot @aiko_bot @aiko.bot @aiko.-bot", json!([])),
        (
            "日本語@AIKO。 (@ken)\n@build.bot",
            json!(["aiko", "ken", "build.bot"]),
        ),
        ("@aiko. @ken.. @agent_x._suffix", json!(["aiko", "ken"])),
        (
            "@build.bot-extra @build.bot.more @build.bot_ @BUILD.BOT.",
            json!(["build.bot"]),
        ),
        ("aiko@example.com https://example.test/@ken", json!(["ken"])),
    ] {
        let (status, updated) = s.patch(&one(), &w.aiko, json!({"body": body})).await;
        assert_eq!(status, 200);
        assert_eq!(updated["ticket"]["mentions"], expected, "{body}");
        let (status, comment) = s
            .post(
                &format!("{}/comments", one()),
                &w.ken,
                json!({"body": body}),
            )
            .await;
        assert_eq!(status, 201);
        assert_eq!(comment["mentions"], expected, "{body}");
    }
    // The title is not a source of mentions.
    s.patch(&one(), &w.aiko, json!({"body": "none", "title": "@aiko"}))
        .await;
    assert_eq!(s.ok(&one(), &w.aiko).await["mentions"], json!([]));
}

#[tokio::test]
async fn query_deduplicates_pages_and_tracks_each_source_independently() {
    let mut w = World::new().await;
    let s = &w.server;
    w.ticket("Unassigned in new").await;
    s.patch(&one(), &w.aiko, json!({"body": "@aiko"})).await;
    let mut comments = Vec::new();
    for _ in 0..3 {
        let (status, c) = s
            .post(
                &format!("{}/comments", one()),
                &w.ken,
                json!({"body": "@AIKO @ken"}),
            )
            .await;
        assert_eq!(status, 201);
        comments.push(c);
    }
    let two = w.ticket("Comment only").await;
    s.post(
        &format!("{}/2/comments", one().trim_end_matches("/1")),
        &w.ken,
        json!({"body": "@aiko"}),
    )
    .await;
    let three = w.ticket("Closed mention").await;
    let three_path = one().trim_end_matches("/1").to_string() + "/3";
    s.patch(
        &three_path,
        &w.owner,
        json!({"body": "@aiko", "state": "closed"}),
    )
    .await;
    let four = w.ticket("No mention").await;

    // Multiple repositories in the requested workspace, and another workspace.
    assert_eq!(
        s.post("/workspaces", &w.owner, json!({"name": "elsewhere"}))
            .await
            .0,
        201
    );
    for repo in ["acme/other", "elsewhere/demo"] {
        assert_eq!(
            s.post("/repos", &w.owner, json!({"path": repo})).await.0,
            201
        );
        assert_eq!(
            s.post(
                &format!("/repos/{}/tickets", encode(repo)),
                &w.aiko,
                json!({"title": repo, "body": "@aiko"})
            )
            .await
            .0,
            201
        );
    }
    // A PR comment mentioning a member never makes its linked ticket a match.
    w.push_change("mention-test", "f.txt", "change\n");
    let p = w
        .open_pull(
            "mention-test",
            json!([{"url": two["url"], "closes": false}, {"url": four["url"], "closes": false}]),
        )
        .await;
    let pull_path = format!("{}/pulls/{}", w.repo_path(), p["number"]);
    let (_, pc) = s
        .post(
            &format!("{pull_path}/comments"),
            &w.ken,
            json!({"body": "@aiko"}),
        )
        .await;
    assert_eq!(pc["mentions"], json!(["aiko"]));

    let mut cursor = String::new();
    let mut urls = Vec::new();
    loop {
        let page = s.ok(&format!("{}&limit=1{cursor}", query()), &w.aiko).await;
        let items = page["items"].as_array().unwrap();
        assert_eq!(items.len(), 1);
        urls.push(items[0]["url"].as_str().unwrap().to_string());
        if let Some(next) = page["next_cursor"].as_str() {
            assert_eq!(
                s.get(&format!("{}&state=all&cursor={next}", query()), &w.aiko)
                    .await
                    .0,
                400
            );
            assert_eq!(
                s.get(&format!("{}&cursor={next}", query()), &w.ken).await.0,
                400
            );
            cursor = format!("&cursor={next}");
        } else {
            break;
        }
    }
    assert_eq!(urls.len(), 3);
    urls.sort();
    urls.dedup();
    assert_eq!(urls.len(), 3);
    assert!(urls.iter().all(|url| url.contains("/acme/")));
    assert!(!urls.contains(&three["url"].as_str().unwrap().to_string()));
    assert!(!urls.contains(&four["url"].as_str().unwrap().to_string()));
    let all = s
        .ok(&format!("{}&state=all&comments=none", query()), &w.aiko)
        .await;
    assert_eq!(all["items"].as_array().unwrap().len(), 4);
    assert!(
        all["items"]
            .as_array()
            .unwrap()
            .iter()
            .all(|t| t["comments"] == json!([]))
    );
    let closed = s.ok(&format!("{}&state=closed", query()), &w.aiko).await;
    assert_eq!(closed["items"][0]["url"], three["url"]);
    let cards = s.ok(&query(), &w.aiko).await;
    let card = cards["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["url"] == two["url"])
        .unwrap();
    assert_eq!(card["comments"][0]["mentions"], json!(["aiko"]));
    assert_eq!(card["pull_requests"][0]["url"], p["url"]);
    for suffix in [
        "?mentions=ken",
        "",
        "?mentions=me&state=bad",
        "?mentions=me&comments=bad",
    ] {
        assert_eq!(
            s.get(&format!("/workspaces/{WS}/tickets{suffix}"), &w.aiko)
                .await
                .0,
            400
        );
    }
    assert_eq!(
        s.get("/workspaces/missing/tickets?mentions=me", &w.aiko)
            .await
            .0,
        404
    );
    let runner = s.member(&w.owner, "mention-runner", "runner").await;
    assert_eq!(s.get(&query(), &runner).await.0, 403);
    assert_eq!(s.get(&query(), "invalid").await.0, 401);

    // Remove the body's mention, then edit and delete comments one at a time.
    s.patch(&one(), &w.aiko, json!({"body": "removed"})).await;
    for (i, c) in comments.iter().enumerate() {
        let path = format!("{}/comments/{}", one(), c["id"].as_str().unwrap());
        if i == 0 {
            let (_, edited) = s.patch(&path, &w.ken, json!({"body": "@ken only"})).await;
            assert_eq!(edited["mentions"], json!(["ken"]));
        } else {
            assert_eq!(s.delete(&path, &w.ken).await.0, 204);
        }
        let page = s.ok(&query(), &w.aiko).await;
        assert_eq!(
            page["items"]
                .as_array()
                .unwrap()
                .iter()
                .any(|t| t["key"] == "1" && t["repository"] == REPO),
            i < 2
        );
    }
    w.server.stop();
    let out = std::process::Command::new(BIN)
        .args(["admin", "check", "--data-dir"])
        .arg(&w.server.data)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[tokio::test]
async fn upgrade_preserves_resolved_mentions_and_their_index() {
    let mut w = World::new().await;
    w.ticket("Old body").await;
    w.ticket("Old comment").await;
    w.server
        .patch(&one(), &w.aiko, json!({"body": "@KEN @AIKO @unknown"}))
        .await;
    let path = format!("{}/2/comments", one().trim_end_matches("/1"));
    w.server
        .post(&path, &w.ken, json!({"body": "@AIKO @ken"}))
        .await;
    let before = w.server.ok(&query(), &w.aiko).await;
    w.server.stop();
    let db = w.server.data.join("branvil.redb");
    as_schema_24(&db);
    let old = tables(&db);
    for (command, expected) in [("upgrade", 3), ("check", 0), ("upgrade", 0)] {
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
    assert_eq!(
        records(&db, "tickets")[0]["mentions"],
        json!(["ken", "aiko"])
    );
    assert_eq!(
        records(&db, "comments")[0]["mentions"],
        json!(["aiko", "ken"])
    );
    assert_eq!(tables(&db)["ix_ticket_mentions"].len(), 4);
    let copy = std::fs::read_dir(&w.server.data)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("branvil-schema-24-")
        })
        .unwrap();
    assert_eq!(tables(&copy), old);
    w.restart(&[]);
    let after = w.server.ok(&query(), &w.aiko).await;
    let shapes = |page: &Value| {
        page["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| {
                (
                    t["key"].clone(),
                    t["mentions"].clone(),
                    t["comments"][0]["mentions"].clone(),
                )
            })
            .collect::<Vec<_>>()
    };
    assert_eq!(shapes(&after), shapes(&before));
}
