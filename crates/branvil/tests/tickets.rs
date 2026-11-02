//! Workspaces, tickets, labels, the board and its cards, comments, cursors,
//! and permissions against a real service.

use crate::common;

use common::*;
use serde_json::{Value, json};

fn tickets_path() -> String {
    format!("/repos/{}/tickets", encode(REPO))
}

#[tokio::test]
async fn text_search_combines_words_filters_and_pagination() {
    let w = World::new().await;
    let s = &w.server;
    for (title, body, label, assignee, lane) in [
        ("unrelated", "", "bug", "aiko", "ready"),
        ("検索 Alpha ÄPFEL", "本文 BETA ÜBER", "bug", "aiko", "ready"),
        ("検索 alpha", "本文 beta", "docs", "aiko", "ready"),
        ("検索 alpha", "本文 beta", "bug", "ken", "ready"),
        ("検索 alpha", "本文 beta", "bug", "aiko", "new"),
        ("検索 ALPHA", "本文 beta", "bug", "aiko", "ready"),
        ("検索 alpha", "本文 beta", "bug", "aiko", "ready"),
        ("検索 alpha", "本文 only", "bug", "aiko", "ready"),
    ] {
        let (status, _) = s
            .post(
                &tickets_path(),
                &w.aiko,
                json!({
                    "title": title, "body": body, "labels": [label],
                    "assignee": assignee, "lane": lane
                }),
            )
            .await;
        assert_eq!(status, 201);
    }
    assert_eq!(
        s.patch(
            &format!("{}/7", tickets_path()),
            &w.aiko,
            json!({"state": "closed", "state_reason": "completed"})
        )
        .await
        .0,
        200
    );
    for (query, expected) in [
        (
            "q=検索%20bEtA&state=all",
            vec!["2", "3", "4", "5", "6", "7"],
        ),
        ("q=検索%20bEtA", vec!["2", "3", "4", "5", "6"]),
        ("q=検索%20bEtA&state=closed", vec!["7"]),
        ("q=ALPHA%20BETA&label=docs", vec!["3"]),
        ("q=äpfel%20über", vec!["2"]),
        ("q=missing", vec![]),
        ("q=\"", vec![]),
        ("q=", vec!["1", "2", "3", "4", "5", "6", "8"]),
        ("q=%20%09%E3%80%80", vec!["1", "2", "3", "4", "5", "6", "8"]),
    ] {
        let page = s.ok(&format!("{}?{query}", tickets_path()), &w.aiko).await;
        let keys: Vec<_> = page["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|ticket| ticket["key"].as_str().unwrap())
            .collect();
        assert_eq!(keys, expected, "{query}");
    }
    // Both the primary scan and label-index scan must filter before paging.
    for label in ["", "&label=bug"] {
        let query = format!("q=検索%20ALPHA%20本文%20beta&lane=ready&assignee=me{label}&limit=1");
        let first = s.ok(&format!("{}?{query}", tickets_path()), &w.aiko).await;
        assert_eq!(first["items"][0]["key"], "2");
        let cursor = first["next_cursor"].as_str().unwrap();
        let second = s
            .ok(
                &format!("{}?{query}&cursor={cursor}", tickets_path()),
                &w.aiko,
            )
            .await;
        assert_eq!(
            second["items"][0]["key"],
            if label.is_empty() { "3" } else { "6" }
        );
        let changed = query.replace("beta", "only");
        let (status, answer) = s
            .get(
                &format!("{}?{changed}&cursor={cursor}", tickets_path()),
                &w.aiko,
            )
            .await;
        assert_eq!(status, 400);
        assert_eq!(answer["error"]["code"], "invalid_cursor");
    }
}

#[tokio::test]
async fn creation_commits_assignment_mode_relations_and_lane_together() {
    let w = World::new().await;
    let parent = w.ticket("Parent").await;
    let blocker = w.ticket("Blocker").await;
    for interactive in [false, true] {
        for relation in ["none", "parent", "blocker", "both"] {
            let mut input = json!({"title": "Created complete", "interactive": interactive,
                                   "assignee": "aiko"});
            if ["parent", "both"].contains(&relation) {
                input["parent"] = parent["url"].clone();
            }
            if ["blocker", "both"].contains(&relation) {
                input["blocked_by"] = json!([blocker["url"], blocker["url"]]);
            }
            let (status, ticket) = w.server.post(&tickets_path(), &w.aiko, input).await;
            assert_eq!(status, 201, "{ticket}");
            assert_eq!(
                ticket["lane"],
                if relation == "none" { "new" } else { "ready" }
            );
            assert_eq!(ticket["interactive"], interactive);
            assert_eq!(ticket["assignee"]["name"], "aiko");
            assert_eq!(ticket["assignee"]["assigned_at"], ticket["created_at"]);
            assert_eq!(
                ticket["parent"].is_null(),
                !["parent", "both"].contains(&relation)
            );
            assert_eq!(
                ticket["blocked_by"].as_array().unwrap().len(),
                usize::from(["blocker", "both"].contains(&relation))
            );
            let path = format!("{}/{}", tickets_path(), ticket["key"].as_str().unwrap());
            assert_eq!(w.server.get(&path, &w.aiko).await.1, ticket);
            // The references' indexes were written in the same transaction.
            if ["parent", "both"].contains(&relation) {
                let (_, reread) = w
                    .server
                    .get(&format!("{}/1", tickets_path()), &w.aiko)
                    .await;
                assert!(
                    reread["children"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|c| c["url"] == ticket["url"])
                );
            }
        }
    }
    for lane in ["new", "ready", "in_progress"] {
        let (status, ticket) = w.server.post(&tickets_path(), &w.aiko,
            json!({"title": "Explicit", "lane": lane, "interactive": true, "assignee": "alice"})).await;
        assert_eq!(status, 201, "{ticket}");
        assert_eq!(ticket["lane"], lane);
        assert_eq!(ticket["assignee"]["name"], "alice");
    }
}

#[tokio::test]
async fn creation_rejects_invalid_initial_state_without_leaving_a_ticket() {
    let w = World::new().await;
    let parent = w.ticket("Parent").await;
    let bad = [
        json!({"assignee": "missing"}),
        json!({"parent": "https://elsewhere/tickets/1"}),
        json!({"blocked_by": ["https://elsewhere/tickets/1"]}),
        json!({"lane": "in_progress", "blocked_by": [parent["url"]]}),
        json!({"parent": parent["url"], "blocked_by": [parent["url"]]}),
    ];
    for mut input in bad {
        input["title"] = json!("Invalid");
        let (status, answer) = w.server.post(&tickets_path(), &w.aiko, input).await;
        assert!(
            status == 400 || status == 404 || status == 409,
            "{status} {answer}"
        );
        assert_eq!(
            w.server
                .get(&format!("{}/2", tickets_path()), &w.aiko)
                .await
                .0,
            404
        );
    }
    assert_eq!(w.ticket("Next").await["key"], "2");
}

#[tokio::test]
async fn deleting_a_ticket_removes_its_sources_and_indexes_but_keeps_numbers() {
    let mut w = World::new().await;
    let s = &w.server;
    let parent = w.ticket("Parent").await;
    let blocker = w.ticket("Blocker").await;
    w.ticket("To delete").await;
    let path = format!("{}/3", tickets_path());
    // The same attachment and mention have other sources that must survive.
    let response = s
        .http
        .post(format!("{}/api/v1{}/attachments", s.url, w.repo_path()))
        .bearer_auth(&w.aiko)
        .header("content-type", "image/png")
        .body(b"\x89PNG\r\n\x1a\nrest".to_vec())
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 201);
    let image: Value = response.json().await.unwrap();
    let body = format!("@aiko ![image]({})", image["url"].as_str().unwrap());
    let (status, _) = s
        .patch(
            &path,
            &w.owner,
            json!({
                "body": body, "add_labels": ["bug", "docs"], "parent": parent["url"],
                "add_blocked_by": [blocker["url"]], "state": "closed", "state_reason": "not_planned"
            }),
        )
        .await;
    assert_eq!(status, 200);
    let (_, comment) = s
        .post(&format!("{path}/comments"), &w.aiko, json!({"body": body}))
        .await;
    assert_eq!(
        s.post(
            &format!("{path}/reactions"),
            &w.ken,
            json!({"target_id": comment["id"], "reaction": "ack"})
        )
        .await
        .0,
        200
    );
    let (_, kept) = s
        .post(
            &format!("{}/1/comments", tickets_path()),
            &w.aiko,
            json!({"body": body}),
        )
        .await;
    let mut events = s
        .http
        .get(format!("{}/api/v1/events", s.url))
        .bearer_auth(&w.owner)
        .send()
        .await
        .unwrap();
    assert_eq!(events.status(), 200);
    assert_eq!(s.delete(&path, &w.owner).await.0, 204);
    let notices = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        let mut text = String::new();
        while !text.contains("\"kind\":\"board\"") {
            text.push_str(std::str::from_utf8(&events.chunk().await.unwrap().unwrap()).unwrap());
        }
        text
    })
    .await
    .expect("deletion events");
    for key in ["1", "2", "3"] {
        assert!(notices.contains(&format!("\"id\":\"{key}\"")), "{notices}");
    }
    assert!(notices.contains(&format!("\"path\":\"{WS}\"")));
    drop(events);
    for suffix in ["", "/comments", "/pulls", "/attachments"] {
        assert_eq!(s.get(&format!("{path}{suffix}"), &w.aiko).await.0, 404);
    }
    assert_eq!(s.delete(&path, &w.owner).await.0, 404);
    assert_eq!(
        s.post(
            &format!("{}/1/reactions", tickets_path()),
            &w.owner,
            json!({"target_id": comment["id"], "reaction": "ack"})
        )
        .await
        .0,
        404
    );
    assert_eq!(
        s.ok(&format!("{}/1", tickets_path()), &w.aiko).await["children"],
        json!([])
    );
    assert_eq!(
        s.ok(&format!("{}/2", tickets_path()), &w.aiko).await["blocks"],
        json!([])
    );
    for list in [
        format!("{}?state=all&label=bug", tickets_path()),
        format!("/workspaces/{WS}/board/closed"),
    ] {
        assert_eq!(s.ok(&list, &w.aiko).await["items"], json!([]));
    }
    let mentions = s
        .ok(&format!("/workspaces/{WS}/tickets?mentions=me"), &w.aiko)
        .await;
    assert_eq!(mentions["items"].as_array().unwrap().len(), 1);
    assert_eq!(mentions["items"][0]["key"], "1");
    let attachments = s
        .ok(&format!("{}/1/attachments", tickets_path()), &w.aiko)
        .await;
    assert_eq!(
        attachments["items"][0]["referenced_by"],
        json!([kept["id"]])
    );
    let cards = s
        .ok(&format!("/workspaces/{WS}/board/cards?state=all"), &w.aiko)
        .await;
    assert_eq!(cards["items"].as_array().unwrap().len(), 2);
    assert_eq!(w.ticket("Next").await["key"], "4");
    // Open tickets are deletable as well.
    assert_eq!(
        s.delete(&format!("{}/4", tickets_path()), &w.owner).await.0,
        204
    );
    assert_eq!(w.ticket("Next again").await["key"], "5");
    w.server.stop();
    let checked = w.server.cli(&w.owner, &["admin", "check"]);
    assert!(
        checked.status.success(),
        "{}",
        String::from_utf8_lossy(&checked.stderr)
    );
}

#[tokio::test]
async fn ticket_deletion_requires_owner_and_refuses_incoming_references() {
    let mut w = World::new().await;
    let s = &w.server;
    let one = w.ticket("Target").await;
    let two = w.ticket("Referrer").await;
    let path = format!("{}/1", tickets_path());
    let other = format!("{}/2", tickets_path());
    let (_, comment) = s
        .post(
            &format!("{path}/comments"),
            &w.aiko,
            json!({"body": "@ken keep me"}),
        )
        .await;
    let runner = s.member(&w.owner, "delete-runner", "runner").await;
    let before = s.ok(&path, &w.owner).await;
    for (token, expected) in [
        (&w.aiko, 403),
        (&w.ken, 403),
        (&runner, 403),
        (&String::from("invalid"), 401),
    ] {
        assert_eq!(s.delete(&path, token).await.0, expected);
        assert_eq!(s.ok(&path, &w.owner).await, before);
    }
    for (patch, code, clear) in [
        (
            json!({"parent": one["url"]}),
            "delete_has_children",
            json!({"parent": null}),
        ),
        (
            json!({"add_blocked_by": [one["url"]]}),
            "delete_blocks_tickets",
            json!({"remove_blocked_by": [one["url"]]}),
        ),
    ] {
        assert_eq!(s.patch(&other, &w.owner, patch).await.0, 200);
        // Closed referrers still protect their target.
        for state in ["open", "closed"] {
            assert_eq!(
                s.patch(&other, &w.owner, json!({"state": state})).await.0,
                200
            );
            let before = s.ok(&path, &w.owner).await;
            let referring = s.ok(&other, &w.owner).await;
            let (status, refusal) = s.delete(&path, &w.owner).await;
            assert_eq!(status, 409, "{refusal}");
            assert_eq!(refusal["error"]["code"], code);
            assert_eq!(s.ok(&path, &w.owner).await, before);
            assert_eq!(s.ok(&other, &w.owner).await, referring);
            assert_eq!(
                s.ok(&format!("{path}/comments"), &w.owner).await["items"],
                json!([comment])
            );
        }
        assert_eq!(s.patch(&other, &w.owner, clear).await.0, 200);
    }
    w.push_change("delete-linked", "change.txt", "change\n");
    let pull = w
        .open_pull(
            "delete-linked",
            json!([{"url": one["url"], "closes": false}]),
        )
        .await;
    let pull_path = format!("{}/pulls/{}", w.repo_path(), pull["number"]);
    for closed in [false, true] {
        if closed {
            assert_eq!(
                s.post(&format!("{pull_path}/close"), &w.owner, json!({}))
                    .await
                    .0,
                200
            );
        }
        let before = s.ok(&path, &w.owner).await;
        let (status, refusal) = s.delete(&path, &w.owner).await;
        assert_eq!(status, 409, "{refusal}");
        assert_eq!(refusal["error"]["code"], "delete_linked_pull");
        assert_eq!(s.ok(&path, &w.owner).await, before);
    }
    assert_eq!(
        s.patch(&pull_path, &w.owner, json!({"tickets": []}))
            .await
            .0,
        200
    );
    assert_eq!(s.delete(&path, &w.owner).await.0, 204);
    assert_eq!(s.ok(&other, &w.owner).await["url"], two["url"]);
    w.server.stop();
    assert!(w.server.cli(&w.owner, &["admin", "check"]).status.success());
}

#[tokio::test]
async fn ticket_deletion_refuses_an_upstream_issue_without_changing_it() {
    let (mut w, _gh) = crate::upstream::followed().await;
    let s = &w.server;
    let (status, ticket) = s
        .post(
            &tickets_path(),
            &w.owner,
            json!({
                "title": "Upstream", "upstream_issue": "https://github.test/octo/demo/issues/7"
            }),
        )
        .await;
    assert_eq!(status, 201, "{ticket}");
    let path = format!("{}/1", tickets_path());
    let (status, refusal) = s.delete(&path, &w.owner).await;
    assert_eq!(status, 409, "{refusal}");
    assert_eq!(refusal["error"]["code"], "delete_upstream_issue");
    assert_eq!(s.ok(&path, &w.owner).await, ticket);
    assert_eq!(
        s.patch(&path, &w.owner, json!({"upstream_issue": null}))
            .await
            .0,
        200
    );
    assert_eq!(s.delete(&path, &w.owner).await.0, 204);
    w.server.stop();
    assert!(w.server.cli(&w.owner, &["admin", "check"]).status.success());
}

#[tokio::test]
async fn comment_edit_and_delete_preserve_identity_and_enforce_permissions() {
    let mut w = World::new().await;
    let s = &w.server;
    w.ticket("Comment changes").await;
    w.ticket("Another ticket").await;
    let one = format!("{}/1", tickets_path());
    let two = format!("{}/2", tickets_path());
    let (status, original) = s
        .post(
            &format!("{one}/comments"),
            &w.aiko,
            json!({"body": "Original", "meta": {"routing": "suppress", "kind": "test"}}),
        )
        .await;
    assert_eq!(status, 201);
    let id = original["id"].as_str().unwrap();
    let path = format!("{one}/comments/{id}");
    let (_, next) = s
        .post(&format!("{one}/comments"), &w.ken, json!({"body": "Next"}))
        .await;
    s.post(
        &format!("{one}/reactions"),
        &w.ken,
        json!({"target_id": id, "reaction": "ack"}),
    )
    .await;
    let runner = s.member(&w.owner, "comment-runner", "runner").await;
    for (token, expected) in [
        (&w.ken, 403),
        (&runner, 403),
        (&String::from("invalid"), 401),
    ] {
        let (status, _) = s.patch(&path, token, json!({"body": "Forbidden"})).await;
        assert_eq!(status, expected);
        let (status, _) = s.delete(&path, token).await;
        assert_eq!(status, expected);
    }
    for bad in [
        format!("{two}/comments/{id}"),
        format!("{one}/comments/missing"),
    ] {
        assert_eq!(
            s.patch(&bad, &w.owner, json!({"body": "Wrong target"}))
                .await
                .0,
            404
        );
        assert_eq!(s.delete(&bad, &w.owner).await.0, 404);
    }
    for body in [
        json!({"body": "  "}),
        json!({"body": ""}),
        json!({"body": "ok", "meta": {}}),
        json!({}),
    ] {
        assert_eq!(s.patch(&path, &w.aiko, body).await.0, 400);
    }
    for (token, body) in [(&w.aiko, "By author"), (&w.owner, "By owner")] {
        let (status, edited) = s.patch(&path, token, json!({"body": body})).await;
        assert_eq!(status, 200, "{edited}");
        assert_eq!(edited["body"], body);
        for field in ["id", "url", "author", "created_at", "meta"] {
            assert_eq!(edited[field], original[field]);
        }
        assert_eq!(
            edited["reactions"],
            json!([{"author": "ken", "reaction": "ack"}])
        );
        let comments = s.ok(&format!("{one}/comments"), &w.aiko).await;
        assert_eq!(comments["items"], json!([edited, next]));
        let cards = s
            .ok(&format!("/workspaces/{WS}/board/cards"), &w.aiko)
            .await;
        let card = cards["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["key"] == "1")
            .unwrap();
        assert_eq!(card["comments"][0]["body"], body);
    }
    // A closed ticket's comments can still be corrected or removed.
    assert_eq!(
        s.patch(
            &one,
            &w.owner,
            json!({"state": "closed", "state_reason": "completed"})
        )
        .await
        .0,
        200
    );
    assert_eq!(
        s.patch(&path, &w.aiko, json!({"body": "Closed"})).await.0,
        200
    );
    assert_eq!(s.delete(&path, &w.owner).await.0, 204);
    assert_eq!(
        s.patch(&path, &w.aiko, json!({"body": "Gone"})).await.0,
        404
    );
    assert_eq!(s.delete(&path, &w.owner).await.0, 404);
    assert_eq!(
        s.post(
            &format!("{one}/reactions"),
            &w.ken,
            json!({"target_id": id, "reaction": "ack"})
        )
        .await
        .0,
        404
    );
    let comments = s.ok(&format!("{one}/comments"), &w.aiko).await;
    assert_eq!(comments["items"], json!([next.clone()]));
    // The author can delete too; all entry indexes must remain consistent.
    assert_eq!(
        s.delete(
            &format!("{one}/comments/{}", next["id"].as_str().unwrap()),
            &w.ken
        )
        .await
        .0,
        204
    );
    w.server.stop();
    let checked = w.server.cli(&w.owner, &["admin", "check"]);
    assert!(
        checked.status.success(),
        "{}",
        String::from_utf8_lossy(&checked.stderr)
    );
}

#[tokio::test]
async fn ticket_lifecycle_and_board() {
    let w = World::new().await;
    let s = &w.server;
    let t = w.ticket("Fix login").await;
    assert_eq!(t["key"], "1");
    assert_eq!(t["url"], format!("{}/{REPO}/-/tickets/1", s.url));
    assert_eq!(t["repository"], REPO);
    // Created at the end of new unless another lane is given.
    assert_eq!(t["lane"], "new");
    assert_eq!(t["state"], "open");

    // Undefined labels are refused with the defined ones listed.
    let (st, v) = s
        .post(
            &tickets_path(),
            &w.aiko,
            json!({"title": "x", "labels": ["nope"]}),
        )
        .await;
    assert_eq!(st, 400);
    assert_eq!(v["error"]["code"], "label_undefined");
    assert!(v["error"]["message"].as_str().unwrap().contains("bug"));

    // Created at the end of another lane of the workspace's board.
    let (st, t2) = s
        .post(
            &tickets_path(),
            &w.aiko,
            json!({"title": "Second", "lane": "ready"}),
        )
        .await;
    assert_eq!(st, 201, "{t2}");
    assert_eq!(t2["lane"], "ready");

    // Assign, move to ready (placed after #2), comment, react.
    let one = format!("{}/1", tickets_path());
    let (st, v) = s
        .put(&format!("{one}/assignee"), &w.ken, json!({"name": "aiko"}))
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["assignee"]["name"], "aiko");
    let (st, v) = s
        .put(
            &format!("{one}/board"),
            &w.ken,
            json!({"lane": "ready", "after": format!("{REPO}#2")}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    let (st, c) = s
        .post(
            &format!("{one}/comments"),
            &w.aiko,
            json!({"body": "Looking", "meta": {"kind": "workflow_error", "routing": "suppress"}}),
        )
        .await;
    assert_eq!(st, 201, "{c}");
    assert!(
        c["url"]
            .as_str()
            .unwrap()
            .ends_with(&format!("/-/tickets/1#{}", c["id"].as_str().unwrap()))
    );
    let (st, r) = s
        .post(
            &format!("{one}/reactions"),
            &w.ken,
            json!({"target_id": c["id"], "reaction": "ack"}),
        )
        .await;
    assert_eq!(st, 200, "{r}");
    let (st, _) = s
        .post(
            &format!("{one}/reactions"),
            &w.ken,
            json!({"target_id": c["id"], "reaction": "heart"}),
        )
        .await;
    assert_eq!(st, 400);
    // A reaction is taken back by its author only, and taking back twice is no error.
    let (st, r) = s
        .post(
            &format!("{one}/reactions"),
            &w.aiko,
            json!({"target_id": c["id"], "reaction": "agree"}),
        )
        .await;
    assert_eq!(st, 200, "{r}");
    for _ in 0..2 {
        let (st, v) = s
            .delete_with(
                &format!("{one}/reactions"),
                &w.aiko,
                json!({"target_id": c["id"], "reaction": "agree"}),
            )
            .await;
        assert_eq!(st, 204, "{v}");
    }
    let (st, v) = s
        .delete_with(
            &format!("{one}/reactions"),
            &w.aiko,
            json!({"target_id": c["id"], "reaction": "ack"}),
        )
        .await;
    assert_eq!(st, 204, "{v}");
    let comments = s.ok(&format!("{one}/comments"), &w.aiko).await;
    assert_eq!(
        comments["items"][0]["reactions"],
        json!([{"author": "ken", "reaction": "ack"}])
    );

    // Cards: ready lane, ordered by rank, with comments; assignee=me filters.
    let cards = s
        .ok(&format!("/workspaces/{WS}/board/cards?lane=ready"), &w.aiko)
        .await;
    let keys: Vec<&str> = cards["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["key"].as_str().unwrap())
        .collect();
    assert_eq!(keys, ["2", "1"]);
    assert_eq!(
        cards["items"][1]["comments"][0]["meta"]["routing"],
        "suppress"
    );
    assert_eq!(
        cards["items"][1]["comments"][0]["reactions"][0]["reaction"],
        "ack"
    );
    let mine = s
        .ok(
            &format!("/workspaces/{WS}/board/cards?assignee=me&comments=none"),
            &w.aiko,
        )
        .await;
    assert_eq!(mine["items"].as_array().unwrap().len(), 1);
    assert_eq!(mine["items"][0]["comments"], json!([]));
    // Move #1 before #2.
    s.put(
        &format!("{one}/board"),
        &w.ken,
        json!({"lane": "ready", "before": format!("{REPO}#2")}),
    )
    .await;
    let cards = s
        .ok(&format!("/workspaces/{WS}/board/cards?lane=ready"), &w.aiko)
        .await;
    assert_eq!(cards["items"][0]["key"], "1");

    // Close: state_changed, closed window lists it; reopen clears closed_at.
    let since = s.ok(&one, &w.aiko).await["updated_at"]
        .as_str()
        .unwrap()
        .to_string();
    let (st, v) = s
        .patch(
            &one,
            &w.aiko,
            json!({"state": "closed", "state_reason": "not_planned"}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["state_changed"], true);
    assert_eq!(v["ticket"]["state_reason"], "not_planned");
    let closed = s
        .ok(
            &format!("/workspaces/{WS}/board/closed?since={since}"),
            &w.aiko,
        )
        .await;
    assert_eq!(closed["items"][0]["label"], "#1");
    assert_eq!(closed["items"][0]["kind"], "ticket");
    let (_, v) = s.patch(&one, &w.aiko, json!({"state": "closed"})).await;
    assert_eq!(v["state_changed"], false);
    let (_, v) = s.patch(&one, &w.aiko, json!({"state": "open"})).await;
    assert_eq!(v["ticket"]["closed_at"], Value::Null);
    assert_eq!(v["ticket"]["state_reason"], Value::Null);

    // Conditional update with a stale revision.
    let (st, v) = s
        .patch(&one, &w.aiko, json!({"title": "New", "revision": 1}))
        .await;
    assert_eq!(st, 409, "{v}");
    assert_eq!(v["error"]["code"], "conflict");

    // Lists filter by state and label.
    s.patch(&one, &w.aiko, json!({"add_labels": ["bug"]})).await;
    let bugs = s
        .ok(&format!("{}?label=bug", tickets_path()), &w.aiko)
        .await;
    assert_eq!(bugs["items"].as_array().unwrap().len(), 1);
    let all = s
        .ok(&format!("{}?state=all", tickets_path()), &w.aiko)
        .await;
    assert_eq!(all["items"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn tickets_are_numbered_and_labelled_per_repository() {
    let w = World::new().await;
    let s = &w.server;
    let (st, v) = s
        .post(
            "/repos",
            &w.owner,
            json!({"path": "acme/other", "labels": ["infra"]}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    assert_eq!(v["labels"], json!(["infra"]));
    let other = format!("/repos/{}/tickets", encode("acme/other"));
    // Pull requests are numbered on their own: the first ticket is still #1.
    w.push_change("feature", "f.txt", "one\n");
    assert_eq!(w.open_pull("feature", json!([])).await["number"], 1);
    assert_eq!(w.ticket("First here").await["key"], "1");
    let (st, t) = s
        .post(
            &other,
            &w.aiko,
            json!({"title": "First there", "labels": ["infra"]}),
        )
        .await;
    assert_eq!(st, 201, "{t}");
    assert_eq!(t["key"], "1");
    assert_eq!(t["repository"], "acme/other");
    assert_eq!(t["url"], format!("{}/acme/other/-/tickets/1", s.url));
    assert_eq!(w.ticket("Second here").await["key"], "2");
    for key in ["01", "0", "x", "1x"] {
        assert_eq!(
            s.get(&format!("{other}/{key}"), &w.aiko).await.0,
            404,
            "{key}"
        );
    }

    // A ticket carries only labels of its own repository.
    let (st, v) = s
        .post(&other, &w.aiko, json!({"title": "x", "labels": ["bug"]}))
        .await;
    assert_eq!(st, 400, "{v}");
    assert_eq!(v["error"]["code"], "label_undefined");
    assert!(
        v["error"]["message"].as_str().unwrap().contains("infra"),
        "{v}"
    );
    let (st, v) = s
        .patch(
            &format!("{other}/1"),
            &w.aiko,
            json!({"add_labels": ["bug"]}),
        )
        .await;
    assert_eq!(st, 400, "{v}");
    assert_eq!(v["error"]["code"], "label_undefined");
    // The repository's labels are the owner's setting.
    let repo = s.ok("/repos/acme%2Fother", &w.aiko).await;
    let change = json!({"revision": repo["revision"], "labels": ["infra", "bug"]});
    let (st, _) = s
        .patch("/repos/acme%2Fother", &w.aiko, change.clone())
        .await;
    assert_eq!(st, 403);
    let (st, v) = s.patch("/repos/acme%2Fother", &w.owner, change).await;
    assert_eq!(st, 200, "{v}");
    let (st, v) = s
        .patch(
            &format!("{other}/1"),
            &w.aiko,
            json!({"add_labels": ["bug"]}),
        )
        .await;
    assert_eq!(st, 200, "{v}");

    // Cards of both repositories of the workspace share its board; a card is
    // named by its repository and number.
    for path in [format!("{}/1", tickets_path()), format!("{other}/1")] {
        let (st, v) = s
            .put(&format!("{path}/board"), &w.aiko, json!({"lane": "ready"}))
            .await;
        assert_eq!(st, 200, "{v}");
    }
    let (st, v) = s
        .put(
            &format!("{}/2/board", tickets_path()),
            &w.aiko,
            json!({"lane": "ready", "before": "acme/other#1"}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    let cards = s
        .ok(&format!("/workspaces/{WS}/board/cards?lane=ready"), &w.aiko)
        .await;
    let refs: Vec<String> = cards["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            format!(
                "{}#{}",
                c["repository"].as_str().unwrap(),
                c["key"].as_str().unwrap()
            )
        })
        .collect();
    assert_eq!(refs, ["acme/demo#1", "acme/demo#2", "acme/other#1"]);
}

#[tokio::test]
async fn workspaces_hold_repositories_and_one_board_each() {
    let w = World::new().await;
    let s = &w.server;
    // Only the owner creates workspaces and repositories.
    let (st, _) = s
        .post("/workspaces", &w.aiko, json!({"name": "beta"}))
        .await;
    assert_eq!(st, 403);
    let (st, v) = s
        .post("/workspaces", &w.owner, json!({"name": "beta"}))
        .await;
    assert_eq!(st, 201, "{v}");
    assert_eq!(v["lanes"], json!(["new", "ready", "in_progress", "done"]));
    let (st, _) = s
        .post("/workspaces", &w.owner, json!({"name": "BETA"}))
        .await;
    assert_eq!(st, 409);
    let (st, _) = s
        .post("/workspaces", &w.owner, json!({"name": "a/b"}))
        .await;
    assert_eq!(st, 400);
    let names: Vec<Value> = s.ok("/workspaces", &w.aiko).await["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x["name"].clone())
        .collect();
    assert_eq!(names, ["acme", "beta"]);
    assert_eq!(s.ok("/workspaces/beta", &w.aiko).await["name"], "beta");
    assert_eq!(s.get("/workspaces/nope", &w.aiko).await.0, 404);
    // A repository is `<workspace>/<name>` of an existing workspace, spelled
    // as the workspace is.
    let (st, _) = s.post("/repos", &w.aiko, json!({"path": "beta/x"})).await;
    assert_eq!(st, 403);
    let (st, v) = s.post("/repos", &w.owner, json!({"path": "nope/x"})).await;
    assert_eq!(st, 404, "{v}");
    let (st, v) = s
        .post("/repos", &w.owner, json!({"path": "beta/team/x"}))
        .await;
    assert_eq!(st, 400, "{v}");
    let (st, v) = s.post("/repos", &w.owner, json!({"path": "BETA/x"})).await;
    assert_eq!(st, 201, "{v}");
    assert_eq!(v["path"], "beta/x");

    // A ticket goes on its own workspace's board only.
    let beta = format!("/repos/{}/tickets", encode("beta/x"));
    let (st, v) = s
        .post(&beta, &w.aiko, json!({"title": "There", "lane": "ready"}))
        .await;
    assert_eq!(st, 201, "{v}");
    w.ticket("Here").await;
    let one = format!("{}/1", tickets_path());
    let (st, v) = s
        .put(
            &format!("{one}/board"),
            &w.aiko,
            json!({"lane": "ready", "before": "beta/x#1"}),
        )
        .await;
    assert_eq!(st, 404, "{v}");
    let (st, v) = s
        .put(&format!("{one}/board"), &w.aiko, json!({"lane": "ready"}))
        .await;
    assert_eq!(st, 200, "{v}");
    let aiko = w.aiko.as_str();
    let cards = |ws: &'static str| async move {
        let v = s.ok(&format!("/workspaces/{ws}/board/cards"), aiko).await;
        v["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| {
                format!(
                    "{}#{}",
                    c["repository"].as_str().unwrap(),
                    c["key"].as_str().unwrap()
                )
            })
            .collect::<Vec<_>>()
    };
    assert_eq!(cards("acme").await, ["acme/demo#1"]);
    assert_eq!(cards("beta").await, ["beta/x#1"]);
    // A ticket does not leave the board.
    for body in [json!({"lane": null}), json!({})] {
        let (st, v) = s.put(&format!("{one}/board"), &w.aiko, body).await;
        assert_eq!(st, 400, "{v}");
        assert_eq!(v["error"]["code"], "invalid");
    }
    assert_eq!(cards("acme").await, ["acme/demo#1"]);
}

#[tokio::test]
async fn the_board_lists_the_closed_items_of_its_workspace() {
    let w = World::new().await;
    let s = &w.server;
    // A closed pull request and a closed ticket.
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let (st, v) = s
        .post(
            &format!("{}/pulls/{}/close", w.repo_path(), p["number"]),
            &w.aiko,
            json!({}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    w.ticket("Closed").await;
    let (st, v) = s
        .patch(
            &format!("{}/1", tickets_path()),
            &w.aiko,
            json!({"state": "closed"}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    // The same in a repository of another workspace.
    s.post("/workspaces", &w.owner, json!({"name": "beta"}))
        .await;
    s.post("/repos", &w.owner, json!({"path": "beta/x"})).await;
    let remote = s.remote("beta/x");
    git_as_ok(&w.work, "alice", &w.owner, &["push", "-q", &remote, "main"]);
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", &remote, "feature"],
    );
    let (st, v) = s
        .post(
            &format!("/repos/{}/pulls", encode("beta/x")),
            &w.aiko,
            json!({"head": "feature", "title": "There"}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    s.post(
        &format!("/repos/{}/pulls/1/close", encode("beta/x")),
        &w.aiko,
        json!({}),
    )
    .await;
    let beta = format!("/repos/{}/tickets", encode("beta/x"));
    s.post(&beta, &w.aiko, json!({"title": "There"})).await;
    s.patch(&format!("{beta}/1"), &w.aiko, json!({"state": "closed"}))
        .await;
    let aiko = w.aiko.as_str();
    let closed = |ws: &'static str| async move {
        let v = s.ok(&format!("/workspaces/{ws}/board/closed"), aiko).await;
        v["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|i| {
                i["url"]
                    .as_str()
                    .unwrap()
                    .replace(&format!("{}/", s.url), "")
            })
            .collect::<Vec<_>>()
    };
    let mut acme = closed("acme").await;
    acme.sort();
    assert_eq!(acme, ["acme/demo/-/pulls/1", "acme/demo/-/tickets/1"]);
    let mut beta = closed("beta").await;
    beta.sort();
    assert_eq!(beta, ["beta/x/-/pulls/1", "beta/x/-/tickets/1"]);
}

/// How many open tickets `assignee=<name>` lists.
#[tokio::test]
async fn the_owner_closes_by_moving_to_done_and_closing_moves_to_done() {
    let w = World::new().await;
    let s = &w.server;
    let one = format!(
        "{}/{}",
        tickets_path(),
        w.ticket("Owner's moves").await["key"].as_str().unwrap()
    );
    let two = format!(
        "{}/{}",
        tickets_path(),
        w.ticket("Agent's moves").await["key"].as_str().unwrap()
    );
    let put = |path: String, token: String, lane: &'static str| async move {
        let (st, v) = s
            .put(&format!("{path}/board"), &token, json!({"lane": lane}))
            .await;
        assert_eq!(st, 200, "{v}");
        v
    };
    let state = |v: &Value| {
        (
            v["lane"].clone(),
            v["state"].clone(),
            v["state_reason"].clone(),
        )
    };

    // The owner's move into done closes as completed; into another lane reopens.
    put(one.clone(), w.owner.clone(), "ready").await;
    let v = put(one.clone(), w.owner.clone(), "done").await;
    assert_eq!(
        state(&v),
        (json!("done"), json!("closed"), json!("completed"))
    );
    let v = put(one.clone(), w.owner.clone(), "in_progress").await;
    assert_eq!(
        state(&v),
        (json!("in_progress"), json!("open"), Value::Null)
    );

    // An agent's moves leave the state alone, both ways.
    let v = put(two.clone(), w.aiko.clone(), "done").await;
    assert_eq!(state(&v), (json!("done"), json!("open"), Value::Null));
    put(two.clone(), w.owner.clone(), "done").await;
    let v = put(two.clone(), w.aiko.clone(), "ready").await;
    assert_eq!(
        state(&v),
        (json!("ready"), json!("closed"), json!("completed"))
    );

    // Closing from any lane moves the ticket to done, keeping its reason;
    // reopening leaves it there, and the owner's move within done keeps the reason.
    let (st, v) = s
        .patch(
            &one,
            &w.aiko,
            json!({"state": "closed", "state_reason": "not_planned"}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(
        state(&v["ticket"]),
        (json!("done"), json!("closed"), json!("not_planned"))
    );
    let v = put(one.clone(), w.owner.clone(), "done").await;
    assert_eq!(
        state(&v),
        (json!("done"), json!("closed"), json!("not_planned"))
    );
    let (st, v) = s.patch(&one, &w.owner, json!({"state": "open"})).await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(
        state(&v["ticket"]),
        (json!("done"), json!("open"), Value::Null)
    );
}

async fn assigned(s: &Server, token: &str, name: &str) -> usize {
    let found = s
        .ok(&format!("{}?assignee={name}", tickets_path()), token)
        .await;
    found["items"].as_array().unwrap().len()
}

#[tokio::test]
async fn a_ticket_has_at_most_one_assignee() {
    let w = World::new().await;
    let s = &w.server;
    let t = w.ticket("One agent").await;
    assert_eq!(t["assignee"], Value::Null);
    let path = format!("{}/{}/assignee", tickets_path(), t["key"].as_str().unwrap());
    // Set.
    let (st, v) = s.put(&path, &w.ken, json!({"name": "aiko"})).await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["assignee"]["name"], "aiko");
    let first = v["assignee"]["assigned_at"].as_str().unwrap().to_string();
    assert!(first.ends_with('Z'), "{first}");
    assert_eq!(assigned(s, &w.aiko, "aiko").await, 1);
    assert_eq!(assigned(s, &w.aiko, "me").await, 1);

    // The same assignee again keeps assigned_at and the revision.
    let (st, again) = s.put(&path, &w.ken, json!({"name": "AIKO"})).await;
    assert_eq!(st, 200, "{again}");
    assert_eq!(again["assignee"]["assigned_at"], first.as_str());
    assert_eq!(again["revision"], v["revision"]);

    // Another name replaces it with a new assigned_at.
    tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    let (st, v) = s.put(&path, &w.ken, json!({"name": "ken"})).await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["assignee"]["name"], "ken");
    assert!(v["assignee"]["assigned_at"].as_str().unwrap() > first.as_str());
    assert!(v["revision"].as_u64() > again["revision"].as_u64(), "{v}");
    assert_eq!(assigned(s, &w.aiko, "aiko").await, 0);
    assert_eq!(assigned(s, &w.aiko, "ken").await, 1);

    // Unknown members, unknown fields, and runners are refused.
    let (st, _) = s.put(&path, &w.ken, json!({"name": "nobody"})).await;
    assert_eq!(st, 404);
    let (st, _) = s.put(&path, &w.ken, json!({"names": ["aiko"]})).await;
    assert_eq!(st, 400);
    let runner = s.member(&w.owner, "runner1", "runner").await;
    let (st, _) = s.put(&path, &runner, json!({"name": "aiko"})).await;
    assert_eq!(st, 403);

    // Clear; clearing again is still 200 with the ticket.
    for _ in 0..2 {
        let (st, v) = s.delete(&path, &w.ken).await;
        assert_eq!(st, 200, "{v}");
        assert_eq!(v["assignee"], Value::Null);
    }
    assert_eq!(assigned(s, &w.aiko, "ken").await, 0);

    // Concurrent assignments end with exactly one assignee, and the
    // filters agree with the record.
    let names = ["aiko", "ken", "alice"];
    let mut handles = Vec::new();
    for _ in 0..5 {
        for name in names {
            let (http, url, token, path) =
                (s.http.clone(), s.url.clone(), w.ken.clone(), path.clone());
            handles.push(tokio::spawn(async move {
                http.put(format!("{url}/api/v1{path}"))
                    .bearer_auth(token)
                    .json(&json!({"name": name}))
                    .send()
                    .await
                    .unwrap()
                    .status()
            }));
        }
    }
    for h in handles {
        assert_eq!(h.await.unwrap(), 200);
    }
    let ticket = s.ok(&path.replace("/assignee", ""), &w.aiko).await;
    let winner = ticket["assignee"]["name"].as_str().unwrap();
    for name in names {
        assert_eq!(
            assigned(s, &w.aiko, name).await,
            usize::from(name == winner),
            "{name}"
        );
    }
}

#[tokio::test]
async fn concurrent_label_changes_are_all_kept_and_indexes_match() {
    let w = World::new().await;
    let t = w.ticket("Labels").await;
    let path = format!("{}/{}", tickets_path(), t["key"].as_str().unwrap());
    let s = &w.server;
    // Start with docs and ui; four writers race: two add, two remove.
    s.patch(&path, &w.aiko, json!({"add_labels": ["docs", "ui"]}))
        .await;
    let ops = [
        json!({"add_labels": ["bug"]}),
        json!({"add_labels": ["feature"]}),
        json!({"remove_labels": ["docs"]}),
        json!({"remove_labels": ["ui"]}),
    ];
    let mut handles = Vec::new();
    for _ in 0..5 {
        for op in &ops {
            let (http, url, token, path, op) = (
                s.http.clone(),
                s.url.clone(),
                w.aiko.clone(),
                path.clone(),
                op.clone(),
            );
            handles.push(tokio::spawn(async move {
                http.patch(format!("{url}/api/v1{path}"))
                    .bearer_auth(token)
                    .json(&op)
                    .send()
                    .await
                    .unwrap()
                    .status()
            }));
        }
    }
    for h in handles {
        assert_eq!(h.await.unwrap(), 200);
    }
    let v = s.ok(&path, &w.aiko).await;
    let mut labels: Vec<String> = v["labels"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l.as_str().unwrap().to_string())
        .collect();
    labels.sort();
    assert_eq!(labels, ["bug", "feature"]);
    // The label index agrees with the record.
    for (label, n) in [("bug", 1), ("feature", 1), ("docs", 0), ("ui", 0)] {
        let found = s
            .ok(&format!("{}?label={label}", tickets_path()), &w.aiko)
            .await;
        assert_eq!(found["items"].as_array().unwrap().len(), n, "label {label}");
    }

    // And the offline consistency check finds nothing to repair.
    let mut server = w.server;
    server.stop();
    let out = std::process::Command::new(BIN)
        .args(["admin", "check", "--data-dir"])
        .arg(&server.data)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success(),
        "{text}{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(text.contains("indexes: ok"), "{text}");
}

#[tokio::test]
async fn cursors_ids_and_member_kinds_are_checked() {
    let w = World::new().await;
    let s = &w.server;
    for i in 0..5 {
        w.ticket(&format!("T{i}")).await;
    }
    let first = s.ok(&format!("{}?limit=2", tickets_path()), &w.aiko).await;
    assert_eq!(first["items"].as_array().unwrap().len(), 2);
    let cursor = first["next_cursor"].as_str().unwrap().to_string();
    let second = s
        .ok(
            &format!("{}?limit=2&cursor={cursor}", tickets_path()),
            &w.aiko,
        )
        .await;
    assert_eq!(second["items"][0]["key"], "3");
    // Follow to the end.
    let mut keys = Vec::new();
    let mut next: Option<String> = None;
    loop {
        let q = next
            .as_ref()
            .map(|c| format!("&cursor={c}"))
            .unwrap_or_default();
        let page = s
            .ok(&format!("{}?limit=2{q}", tickets_path()), &w.aiko)
            .await;
        keys.extend(
            page["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(|t| t["key"].as_str().unwrap().to_string()),
        );
        match page["next_cursor"].as_str() {
            Some(c) => next = Some(c.to_string()),
            None => break,
        }
    }
    assert_eq!(keys, ["1", "2", "3", "4", "5"]);

    // A tampered cursor, a cursor of other conditions, and another member's
    // cursor are refused.
    let mut tampered = cursor.clone();
    tampered.replace_range(0..1, if cursor.starts_with('A') { "B" } else { "A" });
    for (q, token) in [
        (format!("limit=2&cursor={tampered}"), &w.aiko),
        (format!("limit=2&state=all&cursor={cursor}"), &w.aiko),
        (format!("limit=2&cursor={cursor}"), &w.ken),
        ("limit=2&cursor=garbage".to_string(), &w.aiko),
    ] {
        let (st, v) = s.get(&format!("{}?{q}", tickets_path()), token).await;
        assert_eq!(st, 400, "{q}: {v}");
        assert_eq!(v["error"]["code"], "invalid_cursor");
    }
    let (st, _) = s.get(&format!("{}?limit=0", tickets_path()), &w.aiko).await;
    assert_eq!(st, 400);

    // A comment id of another ticket is not this ticket's.
    let one = format!("{}/1", tickets_path());
    let two = format!("{}/2", tickets_path());
    let (_, c) = s
        .post(&format!("{one}/comments"), &w.aiko, json!({"body": "hi"}))
        .await;
    let (st, v) = s
        .post(
            &format!("{two}/reactions"),
            &w.aiko,
            json!({"target_id": c["id"], "reaction": "ack"}),
        )
        .await;
    assert_eq!(st, 404, "{v}");

    // A runner neither reads nor writes tickets; a bad token is 401.
    let runner = s.member(&w.owner, "runner1", "runner").await;
    let (st, _) = s
        .post(&tickets_path(), &runner, json!({"title": "x"}))
        .await;
    assert_eq!(st, 403);
    let (st, _) = s.get(&one, &runner).await;
    assert_eq!(st, 403);
    let (st, v) = s.get(&one, "bvt_bad_token").await;
    assert_eq!(st, 401);
    assert_eq!(v["error"]["code"], "unauthorized");
    // Members list: the owner's, with every member's kind.
    assert_eq!(s.get("/members", &w.aiko).await.0, 403);
    let members = s.ok("/members", &w.owner).await;
    assert_eq!(
        members["items"],
        json!([
            {"name": "aiko", "display_name": "aiko", "kind": "agent", "accounts": [], "reviewers": [], "avatar_url": null},
            {"name": "alice", "display_name": "alice", "kind": "owner", "accounts": [], "reviewers": [], "avatar_url": null},
            {"name": "ken", "display_name": "ken", "kind": "agent", "accounts": [], "reviewers": [], "avatar_url": null},
            {"name": "runner1", "display_name": "runner1", "kind": "runner", "accounts": [], "reviewers": [], "avatar_url": null},
        ])
    );
}

#[tokio::test]
async fn idempotency_keys_replay_and_conflict() {
    let w = World::new().await;
    let s = &w.server;
    let send = |body: Value| {
        s.http
            .post(format!("{}/api/v1{}", s.url, tickets_path()))
            .bearer_auth(&w.aiko)
            .header("Idempotency-Key", "create-1")
            .json(&body)
            .send()
    };
    let a = send(json!({"title": "Once"})).await.unwrap();
    assert_eq!(a.status(), 201);
    let a: Value = a.json().await.unwrap();
    let b = send(json!({"title": "Once"})).await.unwrap();
    assert_eq!(b.status(), 201);
    let b: Value = b.json().await.unwrap();
    assert_eq!(a["key"], b["key"]);
    let c = send(json!({"title": "Other"})).await.unwrap();
    assert_eq!(c.status(), 409);
    let all = s
        .ok(&format!("{}?state=all", tickets_path()), &w.aiko)
        .await;
    assert_eq!(all["items"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn sessions_need_csrf_and_hosts_are_checked() {
    let w = World::new().await;
    let s = &w.server;
    let http = reqwest::Client::new();
    let resp = s.enter(&s.entry_code(&w.owner).await).await;
    assert_eq!(resp.status(), 200);
    let cookie = resp.headers()["set-cookie"].to_str().unwrap().to_string();
    assert!(cookie.contains("HttpOnly") && cookie.contains("SameSite=Strict"));
    let cookie = cookie.split(';').next().unwrap().to_string();
    let body: Value = resp.json().await.unwrap();
    let csrf = body["csrf"].as_str().unwrap();
    // GET with the cookie works; a write without the CSRF header does not.
    let me = http
        .get(format!("{}/api/v1/me", s.url))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(me.status(), 200);
    let url = format!("{}/api/v1{}", s.url, tickets_path());
    let no_csrf = http
        .post(&url)
        .header("cookie", &cookie)
        .json(&json!({"title": "x"}))
        .send()
        .await
        .unwrap();
    assert_eq!(no_csrf.status(), 403);
    let with_csrf = http
        .post(&url)
        .header("cookie", &cookie)
        .header("x-branvil-csrf", csrf)
        .json(&json!({"title": "x"}))
        .send()
        .await
        .unwrap();
    assert_eq!(with_csrf.status(), 201);
    // DNS rebinding: another Host is misdirected; another Origin is refused.
    let wrong_host = http
        .get(format!("{}/api/v1/health", s.url))
        .header("host", "evil.example")
        .send()
        .await
        .unwrap();
    assert_eq!(wrong_host.status(), 421);
    let wrong_origin = http
        .get(format!("{}/api/v1/me", s.url))
        .bearer_auth(&w.ken)
        .header("origin", "http://evil.example")
        .send()
        .await
        .unwrap();
    assert_eq!(wrong_origin.status(), 403);
    // Responses carry the security headers; the SPA serves web URLs.
    let page = http
        .get(format!("{}/{REPO}/-/tickets/1", s.url))
        .send()
        .await
        .unwrap();
    // Scripts only from the GUI's own files; inline styles are allowed for its components.
    let csp = page.headers()["content-security-policy"].to_str().unwrap();
    assert!(csp.contains("script-src 'self';"), "{csp}");
    assert!(csp.contains("style-src 'self' 'unsafe-inline';"), "{csp}");
    assert_eq!(page.headers()["x-content-type-options"], "nosniff");
}

/// The URL path of ticket `t` (a ticket of `REPO`).
fn path_of(t: &Value) -> String {
    format!("{}/{}", tickets_path(), t["key"].as_str().unwrap())
}

/// The keys of the tickets in relation list `list` of a ticket.
fn keys(list: &Value) -> Vec<String> {
    list.as_array()
        .unwrap()
        .iter()
        .map(|r| r["key"].as_str().unwrap().to_string())
        .collect()
}

#[tokio::test]
async fn relations_are_shown_from_both_sides_and_never_make_tickets_wait_on_each_other() {
    let w = World::new().await;
    let s = &w.server;
    let parent = w.ticket("Parent").await;
    let a = w.ticket("Child A").await;
    let b = w.ticket("Child B").await;
    let blocker = w.ticket("Blocker").await;
    let aiko = &w.aiko;
    let patch = |t: &Value, body: Value| {
        let path = path_of(t);
        async move { s.patch(&path, aiko, body).await }
    };
    for child in [&a, &b] {
        let (st, v) = patch(child, json!({"parent": parent["url"]})).await;
        assert_eq!(st, 200, "{v}");
        assert_eq!(v["ticket"]["parent"]["key"], "1", "{v}");
    }
    let (st, v) = patch(&parent, json!({"add_blocked_by": [blocker["url"]]})).await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(keys(&v["ticket"]["blocked_by"]), ["4"]);
    // Each side shows the other with its title and state.
    let p = s.ok(&path_of(&parent), &w.aiko).await;
    assert_eq!(keys(&p["children"]), ["2", "3"]);
    assert_eq!(p["children"][0]["title"], "Child A");
    assert_eq!(p["children"][0]["state"], "open");
    assert_eq!(p["children"][0]["url"], a["url"]);
    assert_eq!(
        keys(&s.ok(&path_of(&blocker), &w.aiko).await["blocks"]),
        ["1"]
    );

    // Refused, changing nothing: a cycle of parents, a ticket that would wait
    // on itself (its own parent or blocker, a child blocked by its parent, a
    // blocker blocked by what it blocks, a parent made a child of its
    // blocker), a ticket of another workspace, a URL that is not a ticket of
    // this Branvil (another host, a ticket that is not there, a pull request).
    s.post("/workspaces", &w.owner, json!({"name": "beta"}))
        .await;
    s.post("/repos", &w.owner, json!({"path": "beta/x"})).await;
    let (_, elsewhere) = s
        .post(
            &format!("/repos/{}/tickets", encode("beta/x")),
            &w.aiko,
            json!({"title": "Elsewhere"}),
        )
        .await;
    for (t, body) in [
        (&parent, json!({"parent": a["url"]})),
        (&parent, json!({"parent": parent["url"]})),
        (&parent, json!({"add_blocked_by": [parent["url"]]})),
        (&a, json!({"add_blocked_by": [parent["url"]]})),
        (&blocker, json!({"add_blocked_by": [parent["url"]]})),
        (&parent, json!({"parent": blocker["url"]})),
        (&parent, json!({"parent": elsewhere["url"]})),
        (
            &parent,
            json!({"add_blocked_by": ["https://example.com/acme/demo/-/tickets/2"]}),
        ),
        (
            &parent,
            json!({"parent": format!("{}/acme/demo/-/tickets/99", s.url)}),
        ),
        (
            &parent,
            json!({"parent": format!("{}/acme/demo/-/pulls/1", s.url)}),
        ),
    ] {
        let before = s.ok(&path_of(t), &w.aiko).await;
        let (st, v) = patch(t, body.clone()).await;
        assert_eq!(
            (st.as_u16(), v["error"]["code"].as_str()),
            (400, Some("invalid")),
            "{body} {v}"
        );
        assert_eq!(s.ok(&path_of(t), &w.aiko).await, before, "{body}");
    }
    // A parent and a blocker set together are checked together.
    let c = w.ticket("Child C").await;
    let (st, v) = patch(
        &c,
        json!({"parent": blocker["url"], "add_blocked_by": [parent["url"]]}),
    )
    .await;
    assert_eq!(st, 400, "{v}");

    // Blocked: not started (into in_progress) while a blocker is open, by
    // anyone; once in progress it may still move within the lane.
    for token in [&w.aiko, &w.owner] {
        let (st, v) = s
            .put(
                &format!("{}/board", path_of(&parent)),
                token,
                json!({"lane": "in_progress"}),
            )
            .await;
        assert_eq!(
            (st.as_u16(), v["error"]["code"].as_str()),
            (409, Some("blocked")),
            "{v}"
        );
    }
    patch(
        &blocker,
        json!({"state": "closed", "state_reason": "not_planned"}),
    )
    .await;
    s.put(
        &format!("{}/board", path_of(&a)),
        &w.aiko,
        json!({"lane": "in_progress"}),
    )
    .await;
    let (st, v) = s
        .put(
            &format!("{}/board", path_of(&parent)),
            &w.aiko,
            json!({"lane": "in_progress"}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    patch(&blocker, json!({"state": "open"})).await;
    let (st, v) = s
        .put(
            &format!("{}/board", path_of(&parent)),
            &w.aiko,
            json!({"lane": "in_progress", "before": format!("{REPO}#2")}),
        )
        .await;
    assert_eq!(st, 200, "{v}");

    // A parent completes only once its children are closed: closing it as
    // completed, changing a not-planned close to completed, and the owner's
    // move into done are refused; closing as not planned and an agent's move
    // into done (which leaves the state) are not.
    let refused = |(st, v): (reqwest::StatusCode, Value)| {
        assert_eq!(
            (st.as_u16(), v["error"]["code"].as_str()),
            (409, Some("children_open")),
            "{v}"
        );
    };
    refused(patch(&parent, json!({"state": "closed"})).await);
    let (st, v) = patch(
        &parent,
        json!({"state": "closed", "state_reason": "not_planned"}),
    )
    .await;
    assert_eq!(st, 200, "{v}");
    refused(patch(&parent, json!({"state_reason": "completed"})).await);
    patch(&parent, json!({"state": "open"})).await;
    refused(
        s.put(
            &format!("{}/board", path_of(&parent)),
            &w.owner,
            json!({"lane": "done"}),
        )
        .await,
    );
    let (st, v) = s
        .put(
            &format!("{}/board", path_of(&parent)),
            &w.aiko,
            json!({"lane": "done"}),
        )
        .await;
    assert_eq!(
        (st.as_u16(), v["state"].as_str()),
        (200, Some("open")),
        "{v}"
    );
    // A child closed as not planned is closed too.
    patch(&a, json!({"state": "closed"})).await;
    patch(
        &b,
        json!({"state": "closed", "state_reason": "not_planned"}),
    )
    .await;
    let (st, v) = patch(&parent, json!({"state": "closed"})).await;
    assert_eq!(
        (st.as_u16(), v["ticket"]["state_reason"].as_str()),
        (200, Some("completed")),
        "{v}"
    );
    let p = s.ok(&path_of(&parent), &w.aiko).await;
    assert_eq!(p["children"][1]["state_reason"], "not_planned");

    // Relations are taken back as they were given.
    patch(&b, json!({"parent": null})).await;
    patch(&parent, json!({"remove_blocked_by": [blocker["url"]]})).await;
    let p = s.ok(&path_of(&parent), &w.aiko).await;
    assert_eq!(keys(&p["children"]), ["2"]);
    assert_eq!(keys(&p["blocked_by"]), Vec::<String>::new());
    assert_eq!(
        keys(&s.ok(&path_of(&blocker), &w.aiko).await["blocks"]),
        Vec::<String>::new()
    );
    // Cards carry the relations too.
    let cards = s
        .ok(
            &format!("/workspaces/{WS}/board/cards?state=all&comments=none"),
            &w.aiko,
        )
        .await;
    let card = cards["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["key"] == "1")
        .unwrap();
    assert_eq!(keys(&card["children"]), ["2"]);

    // The offline check finds the relation index as the records imply.
    let mut server = w.server;
    server.stop();
    let out = std::process::Command::new(BIN)
        .args(["admin", "check", "--data-dir"])
        .arg(&server.data)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success() && text.contains("indexes: ok"),
        "{text}"
    );
}

#[tokio::test]
async fn a_merge_closes_a_parent_only_with_its_children() {
    let w = &World::new().await;
    let s = &w.server;
    let parent = w.ticket("Parent").await;
    let child = w.ticket("Child").await;
    s.patch(&path_of(&child), &w.aiko, json!({"parent": parent["url"]}))
        .await;
    s.put(
        &format!("{}/board", path_of(&parent)),
        &w.aiko,
        json!({"lane": "in_progress"}),
    )
    .await;
    let state = |t: Value| (t["state"].clone(), t["lane"].clone());
    let merge = |branch: &'static str, tickets: Value| async move {
        let head = w.push_change(branch, &format!("{branch}.txt"), "one\n");
        let p = w.open_pull(branch, tickets).await;
        let (st, v) = s
            .post(
                &format!("{}/pulls/{}/merge", w.repo_path(), p["number"]),
                &w.owner,
                json!({"expected_head": head, "expected_base": p["base_sha"]}),
            )
            .await;
        assert_eq!(st, 200, "{v}");
    };
    // A merge that closes the parent while its child is open leaves it as
    // it is.
    merge("first", json!([{"url": parent["url"], "closes": true}])).await;
    assert_eq!(
        state(s.ok(&path_of(&parent), &w.aiko).await),
        (json!("open"), json!("in_progress"))
    );
    // One that closes both closes both, whatever the order of its links.
    merge(
        "second",
        json!([{"url": parent["url"], "closes": true}, {"url": child["url"], "closes": true}]),
    )
    .await;
    assert_eq!(
        state(s.ok(&path_of(&parent), &w.aiko).await),
        (json!("closed"), json!("done"))
    );
    assert_eq!(s.ok(&path_of(&child), &w.aiko).await["state"], "closed");
}

/// Whether ticket 1 is interactive, as its read, the board's cards, and ken's
/// mentions show it.
async fn interactive_as_read(w: &World) -> Vec<Value> {
    let s = &w.server;
    vec![
        s.ok(&format!("{}/1", tickets_path()), &w.aiko).await["interactive"].clone(),
        s.ok(&format!("/workspaces/{WS}/board/cards"), &w.aiko)
            .await["items"][0]["interactive"]
            .clone(),
        s.ok(&format!("/workspaces/{WS}/tickets?mentions=me"), &w.ken)
            .await["items"][0]["interactive"]
            .clone(),
    ]
}

#[tokio::test]
async fn a_ticket_is_interactive_only_while_a_member_says_so() {
    let w = World::new().await;
    let s = &w.server;
    let created = w.ticket("Pair on it").await;
    assert_eq!(created["interactive"], false);
    let one = format!("{}/1", tickets_path());
    s.patch(&one, &w.aiko, json!({"body": "@ken"})).await;
    assert_eq!(interactive_as_read(&w).await, vec![json!(false); 3]);
    for on in [true, false] {
        let revision = s.ok(&one, &w.aiko).await["revision"].as_u64().unwrap();
        let (st, v) = s.patch(&one, &w.ken, json!({"interactive": on})).await;
        assert_eq!(st, 200, "{v}");
        assert_eq!(v["ticket"]["interactive"], on);
        assert_eq!(v["ticket"]["revision"], revision + 1);
        assert_eq!(interactive_as_read(&w).await, vec![json!(on); 3]);
    }
    // Setting it as it is changes nothing.
    let revision = s.ok(&one, &w.aiko).await["revision"].clone();
    let (_, v) = s.patch(&one, &w.ken, json!({"interactive": false})).await;
    assert_eq!(v["ticket"]["revision"], revision);
    let (st, _) = s.patch(&one, &w.ken, json!({"interactive": "yes"})).await;
    assert_eq!(st, 400);
}

/// The key of the interactive ancestor of ticket `t` (`null` for none), as
/// its read and its card show it.
async fn interactive_ancestor_as_read(w: &World, t: &Value) -> Vec<Value> {
    let s = &w.server;
    let cards = s
        .ok(&format!("/workspaces/{WS}/board/cards"), &w.aiko)
        .await;
    let card = cards["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["key"] == t["key"])
        .unwrap()
        .clone();
    [s.ok(&path_of(t), &w.aiko).await, card]
        .iter()
        .map(|v| v["interactive_ancestor"]["key"].clone())
        .collect()
}

#[tokio::test]
async fn a_ticket_is_a_part_of_the_interactive_work_of_its_nearest_interactive_ancestor() {
    let w = World::new().await;
    let s = &w.server;
    let top = w.ticket("Top").await;
    let middle = w.ticket("Middle").await;
    let bottom = w.ticket("Bottom").await;
    for (child, parent) in [(&middle, &top), (&bottom, &middle)] {
        let (st, v) = s
            .patch(&path_of(child), &w.aiko, json!({"parent": parent["url"]}))
            .await;
        assert_eq!(st, 200, "{v}");
    }
    let ken = &w.ken;
    let set = |t: &Value, on: bool| {
        let path = path_of(t);
        async move {
            let (st, v) = s.patch(&path, ken, json!({"interactive": on})).await;
            assert_eq!(st, 200, "{v}");
        }
    };
    assert_eq!(
        interactive_ancestor_as_read(&w, &bottom).await,
        [Value::Null, Value::Null]
    );
    set(&top, true).await;
    assert_eq!(interactive_ancestor_as_read(&w, &middle).await, ["1", "1"]);
    assert_eq!(interactive_ancestor_as_read(&w, &bottom).await, ["1", "1"]);
    // The nearest one is named; the ticket's own switch stays its own.
    set(&middle, true).await;
    assert_eq!(interactive_ancestor_as_read(&w, &bottom).await, ["2", "2"]);
    assert_eq!(s.ok(&path_of(&bottom), &w.aiko).await["interactive"], false);
    assert_eq!(
        interactive_ancestor_as_read(&w, &top).await,
        [Value::Null, Value::Null]
    );
    set(&middle, false).await;
    assert_eq!(interactive_ancestor_as_read(&w, &bottom).await, ["1", "1"]);
    // Leaving the tree leaves its interactive work.
    s.patch(&path_of(&middle), &w.aiko, json!({"parent": null}))
        .await;
    assert_eq!(
        interactive_ancestor_as_read(&w, &bottom).await,
        [Value::Null, Value::Null]
    );
}

#[tokio::test]
async fn a_change_of_a_ticket_reaches_the_views_of_all_its_descendants() {
    let w = World::new().await;
    let s = &w.server;
    let top = w.ticket("Top").await;
    let middle = w.ticket("Middle").await;
    let bottom = w.ticket("Bottom").await;
    for (child, parent) in [(&middle, &top), (&bottom, &middle)] {
        let (st, v) = s
            .patch(&path_of(child), &w.aiko, json!({"parent": parent["url"]}))
            .await;
        assert_eq!(st, 200, "{v}");
    }
    let mut events = s
        .http
        .get(format!("{}/api/v1/events", s.url))
        .bearer_auth(&w.aiko)
        .send()
        .await
        .unwrap();
    assert_eq!(events.status(), 200);
    s.patch(&path_of(&top), &w.ken, json!({"interactive": true}))
        .await;
    let bottom_changed = format!(r#"data: {{"kind":"ticket","path":"{REPO}","id":"3"}}"#);
    let mut seen = String::new();
    let found = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        while let Ok(Some(chunk)) = events.chunk().await {
            seen.push_str(&String::from_utf8_lossy(&chunk));
            if seen.contains(&bottom_changed) {
                return true;
            }
        }
        false
    })
    .await;
    assert_eq!(found, Ok(true), "{seen}");
}
