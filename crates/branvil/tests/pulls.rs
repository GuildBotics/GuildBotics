//! Pull requests against a real service and real Git: concurrent creation,
//! head checks for reviews and inline comments, entry ids, patrol, diffs,
//! checks, and the fast-forward merge.

use crate::common;

use common::*;
use serde_json::{Value, json};
use std::collections::BTreeMap;

fn pulls(w: &World) -> String {
    format!("{}/pulls", w.repo_path())
}

#[tokio::test]
async fn repository_pages_put_recent_updates_first_and_visit_every_matching_pull() {
    let w = World::new().await;
    w.push_change("feature", "f.txt", "one");
    let s = &w.server;
    let mut closed = Vec::new();
    for i in 0..32 {
        let p = w.open_pull("feature", json!([])).await;
        let (action, token, body) = if i == 31 {
            (
                "merge",
                &w.owner,
                json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
            )
        } else {
            ("close", &w.aiko, json!({}))
        };
        let (st, p) = s
            .post(
                &format!("{}/{}/{action}", pulls(&w), p["number"]),
                token,
                body,
            )
            .await;
        assert_eq!(st, 200, "{p}");
        closed.push(p);
    }
    w.push_change("feature", "f.txt", "two");
    let open = w.open_pull("feature", json!([])).await;
    // An older PR updated recently belongs ahead of recently created PRs.
    let (st, first) = s
        .patch(
            &format!("{}/1", pulls(&w)),
            &w.aiko,
            json!({"title": "Recently updated"}),
        )
        .await;
    assert_eq!(st, 200, "{first}");
    closed[0] = first;
    for state in ["closed", "all", "open"] {
        let mut expected = if state == "open" {
            Vec::new()
        } else {
            closed.clone()
        };
        if state != "closed" {
            expected.push(open.clone());
        }
        expected.sort_by(|a, b| {
            b["updated_at"]
                .as_str()
                .cmp(&a["updated_at"].as_str())
                .then_with(|| b["number"].as_u64().cmp(&a["number"].as_u64()))
        });
        let mut walked = Vec::new();
        let mut cursor = String::new();
        loop {
            let page = s
                .ok(
                    &format!("{}?state={state}&head=feature&base=main{cursor}", pulls(&w)),
                    &w.aiko,
                )
                .await;
            let rows = page["items"].as_array().unwrap();
            if walked.is_empty() && state != "open" {
                assert_eq!(rows.len(), 30);
                assert_eq!(rows[0]["number"], 1);
                assert!(
                    rows.iter()
                        .any(|p| p["number"] == 32 && p["merged"] == true)
                );
            }
            walked.extend(rows.iter().map(|p| p["number"].as_u64().unwrap()));
            match page["next_cursor"].as_str() {
                Some(next) => {
                    // Cursors cannot silently change their state or branch filters.
                    for condition in [
                        "state=all&head=other&base=main",
                        "state=closed&head=feature&base=other",
                    ] {
                        let (st, _) = s
                            .get(&format!("{}?{condition}&cursor={next}", pulls(&w)), &w.aiko)
                            .await;
                        assert_eq!(st, 400);
                    }
                    cursor = format!("&cursor={next}");
                }
                None => break,
            }
        }
        assert_eq!(
            walked,
            expected
                .iter()
                .map(|p| p["number"].as_u64().unwrap())
                .collect::<Vec<_>>()
        );
    }
    let empty = s
        .ok(&format!("{}?state=all&head=missing", pulls(&w)), &w.aiko)
        .await;
    assert_eq!(empty["items"], json!([]));
    assert!(empty["next_cursor"].is_null());
}

#[tokio::test]
async fn concurrent_creates_of_one_head_and_base_make_one_pull_request() {
    let w = World::new().await;
    w.push_change("feature", "f.txt", "one");
    let s = &w.server;
    let mut handles = Vec::new();
    for i in 0..8 {
        let (http, url, token) = (
            s.http.clone(),
            format!("{}/api/v1{}", s.url, pulls(&w)),
            w.aiko.clone(),
        );
        handles.push(tokio::spawn(async move {
            let resp = http
                .post(url)
                .bearer_auth(token)
                .json(&json!({"head": "feature", "title": format!("try {i}")}))
                .send()
                .await
                .unwrap();
            let status = resp.status().as_u16();
            (status, resp.json::<Value>().await.unwrap())
        }));
    }
    let mut created = 0;
    let mut numbers = Vec::new();
    for h in handles {
        let (status, v) = h.await.unwrap();
        match status {
            201 => {
                created += 1;
                assert_eq!(v["created"], true);
            }
            200 => assert_eq!(v["created"], false),
            other => panic!("unexpected {other}: {v}"),
        }
        numbers.push(v["pull"]["number"].as_u64().unwrap());
    }
    assert_eq!(created, 1);
    assert!(numbers.iter().all(|n| *n == numbers[0]));
    let open = s
        .ok(&format!("{}?state=open&head=feature", pulls(&w)), &w.aiko)
        .await;
    assert_eq!(open["items"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn reviews_and_inline_comments_on_a_moved_head_write_nothing() {
    let w = World::new().await;
    let old = w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    assert_eq!(p["head_sha"], old.as_str());
    let n = p["number"].as_u64().unwrap();
    let new = w.push_change("feature", "f.txt", "one\ntwo\n");
    assert_ne!(old, new);
    let s = &w.server;
    let base = format!("{}/{n}", pulls(&w));

    let (st, v) = s
        .post(
            &format!("{base}/reviews"),
            &w.ken,
            json!({"verdict": "approve", "body": "ok", "commit": old}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
    assert_eq!(v["error"]["code"], "head_moved");
    let (st, v) = s
        .post(
            &format!("{base}/inline-comments"),
            &w.ken,
            json!({"body": "here", "commit": old, "path": "f.txt", "line": 1, "side": "RIGHT"}),
        )
        .await;
    assert_eq!(st, 409, "{v}");
    assert_eq!(v["error"]["code"], "head_moved");
    let activity = s.ok(&format!("{base}/activity"), &w.ken).await;
    assert_eq!(activity["reviews"], json!([]));
    assert_eq!(activity["threads"], json!([]));

    // On the current head both are recorded.
    let (st, v) = s
        .post(
            &format!("{base}/reviews"),
            &w.ken,
            json!({"verdict": "approve", "body": "ok", "commit": new}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    let (st, root) = s
        .post(
            &format!("{base}/inline-comments"),
            &w.ken,
            json!({"body": "here", "commit": new, "path": "f.txt", "line": 2, "side": "RIGHT"}),
        )
        .await;
    assert_eq!(st, 201, "{root}");
    assert_eq!(root["reply_to_id"], Value::Null);
    // The author's own comment is recorded.
    let (st, own) = s
        .post(
            &format!("{base}/reviews"),
            &w.aiko,
            json!({"verdict": "comment", "commit": new}),
        )
        .await;
    assert_eq!(st, 201, "{own}");
    // A path the pull request does not change is refused.
    let (st, _) = s
        .post(
            &format!("{base}/inline-comments"),
            &w.ken,
            json!({"body": "x", "commit": new, "path": "README.md", "line": 1, "side": "RIGHT"}),
        )
        .await;
    assert_eq!(st, 400);

    // Replies and resolution; a later push makes the thread outdated.
    let root_id = root["id"].as_str().unwrap();
    let (st, reply) = s
        .post(
            &format!("{base}/threads/{root_id}/replies"),
            &w.aiko,
            json!({"body": "fixed"}),
        )
        .await;
    assert_eq!(st, 201, "{reply}");
    assert_eq!(reply["reply_to_id"], root_id);
    // A reply id does not root a thread.
    let (st, _) = s
        .post(
            &format!("{base}/threads/{}/replies", reply["id"].as_str().unwrap()),
            &w.aiko,
            json!({"body": "x"}),
        )
        .await;
    assert_eq!(st, 404);
    let (st, t) = s
        .put(
            &format!("{base}/threads/{root_id}/resolved"),
            &w.ken,
            json!({"resolved": true}),
        )
        .await;
    assert_eq!(st, 200, "{t}");
    assert_eq!(t["resolved"], true);
    w.push_change("feature", "f.txt", "one\ntwo\nthree\n");
    let threads = s.ok(&format!("{base}/threads"), &w.ken).await;
    assert_eq!(threads["items"][0]["outdated"], true);
    assert_eq!(threads["items"][0]["comments"].as_array().unwrap().len(), 2);
    let tc = s
        .ok(&format!("{base}/threads/{root_id}/comments"), &w.ken)
        .await;
    assert_eq!(tc["items"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn entry_ids_belong_to_their_pull_request() {
    let w = World::new().await;
    w.push_change("one", "a.txt", "a");
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("two", "b.txt", "b");
    let p1 = w.open_pull("one", json!([])).await;
    let p2 = w.open_pull("two", json!([])).await;
    let s = &w.server;
    let b1 = format!("{}/{}", pulls(&w), p1["number"]);
    let b2 = format!("{}/{}", pulls(&w), p2["number"]);
    let (_, c) = s
        .post(&format!("{b1}/comments"), &w.ken, json!({"body": "c"}))
        .await;
    let (_, r) = s
        .post(
            &format!("{b1}/reviews"),
            &w.ken,
            json!({"verdict": "comment", "body": "r", "commit": p1["head_sha"]}),
        )
        .await;
    let (_, i) = s
        .post(
            &format!("{b1}/inline-comments"),
            &w.ken,
            json!({"body": "i", "commit": p1["head_sha"], "path": "a.txt", "line": 1, "side": "RIGHT"}),
        )
        .await;
    for id in [&c["id"], &r["id"], &i["id"]] {
        let (st, v) = s
            .post(
                &format!("{b2}/reactions"),
                &w.aiko,
                json!({"target_id": id, "reaction": "ack"}),
            )
            .await;
        assert_eq!(st, 404, "{v}");
        let (st, v) = s
            .post(
                &format!("{b1}/reactions"),
                &w.aiko,
                json!({"target_id": id, "reaction": "agree"}),
            )
            .await;
        assert_eq!(st, 200, "{v}");
    }
    let (st, _) = s
        .post(
            &format!("{b2}/threads/{}/replies", i["id"].as_str().unwrap()),
            &w.aiko,
            json!({"body": "x"}),
        )
        .await;
    assert_eq!(st, 404);
    // Taking a reaction back works on every kind of entry, and only on this pull request's.
    for id in [&c["id"], &i["id"]] {
        let (st, v) = s
            .delete_with(
                &format!("{b2}/reactions"),
                &w.aiko,
                json!({"target_id": id, "reaction": "agree"}),
            )
            .await;
        assert_eq!(st, 404, "{v}");
    }
    let (st, v) = s
        .delete_with(
            &format!("{b1}/reactions"),
            &w.aiko,
            json!({"target_id": r["id"], "reaction": "agree"}),
        )
        .await;
    assert_eq!(st, 204, "{v}");
    let activity = s.ok(&format!("{b1}/activity"), &w.aiko).await;
    assert_eq!(activity["reviews"][0]["reactions"], json!([]));
    for id in [&c["id"], &i["id"]] {
        let (st, v) = s
            .delete_with(
                &format!("{b1}/reactions"),
                &w.aiko,
                json!({"target_id": id, "reaction": "agree"}),
            )
            .await;
        assert_eq!(st, 204, "{v}");
    }
    let activity = s.ok(&format!("{b1}/activity"), &w.aiko).await;
    assert_eq!(activity["comments"][0]["reactions"], json!([]));
    assert_eq!(
        activity["threads"][0]["comments"][0]["reactions"],
        json!([])
    );
    for id in [&c["id"], &r["id"], &i["id"]] {
        let (st, v) = s
            .post(
                &format!("{b1}/reactions"),
                &w.aiko,
                json!({"target_id": id, "reaction": "agree"}),
            )
            .await;
        assert_eq!(st, 200, "{v}");
    }
    let activity = s.ok(&format!("{b1}/activity"), &w.aiko).await;
    assert_eq!(activity["comments"][0]["reactions"][0]["reaction"], "agree");
    assert_eq!(activity["reviews"][0]["reactions"][0]["reaction"], "agree");
    assert_eq!(
        activity["threads"][0]["comments"][0]["reactions"][0]["reaction"],
        "agree"
    );
    // A runner sees no pull request.
    let runner = s.member(&w.owner, "runner1", "runner").await;
    let (st, _) = s.get(&b1, &runner).await;
    assert_eq!(st, 403);
}

#[tokio::test]
async fn patrol_files_links_and_draft_rules() {
    let w = World::new().await;
    let mia = w.server.member(&w.owner, "mia", "agent").await;
    let t = w.ticket("Link me").await;
    w.push_change("feature", "f.txt", "one\n");
    std::fs::write(w.work.join("bin.dat"), [0u8, 159, 146, 150, 0, 1]).unwrap();
    git_ok(&w.work, &["add", "bin.dat"]);
    git_ok(&w.work, &["commit", "-qm", "binary"]);
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", "origin", "HEAD:refs/heads/feature"],
    );
    let s = &w.server;
    // A foreign ticket cannot be closed; a missing ticket of ours is 404.
    let (st, _) = s
        .post(&pulls(&w), &w.aiko, json!({"head": "feature", "title": "x", "tickets": [{"url": "https://example.com/t/1", "closes": true}]}))
        .await;
    assert_eq!(st, 422);
    for key in ["99", "01"] {
        let (st, _) = s
            .post(&pulls(&w), &w.aiko, json!({"head": "feature", "title": "x", "tickets": [{"url": format!("{}/{REPO}/-/tickets/{key}", s.url), "closes": false}]}))
            .await;
        assert_eq!(st, 404, "{key}");
    }
    // A ticket of another repository is a reference: it cannot be closed.
    let (st, _) = s
        .post("/repos", &w.owner, json!({"path": "acme/other"}))
        .await;
    assert_eq!(st, 201);
    let (st, other) = s
        .post(
            &format!("/repos/{}/tickets", encode("acme/other")),
            &w.aiko,
            json!({"title": "There"}),
        )
        .await;
    assert_eq!(st, 201, "{other}");
    let (st, v) = s
        .post(&pulls(&w), &w.aiko, json!({"head": "feature", "title": "x", "tickets": [{"url": other["url"], "closes": true}]}))
        .await;
    assert_eq!(st, 422, "{v}");
    assert_eq!(v["error"]["code"], "unsupported");
    let p = w
        .open_pull("feature", json!([{"url": t["url"], "closes": true}, {"url": "https://example.com/t/1", "closes": false}]))
        .await;
    let n = p["number"].as_u64().unwrap();
    assert_eq!(p["label"], format!("!{n}"));
    assert_eq!(p["changed_files"], 2);
    assert!(p["ready_at"].is_string());
    let base = format!("{}/{n}", pulls(&w));

    // A body change keeps the links; the ticket lists the pull request.
    let (st, v) = s.patch(&base, &w.aiko, json!({"body": "new body"})).await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["tickets"].as_array().unwrap().len(), 2);
    let tp = s
        .ok(&format!("/repos/{}/tickets/1/pulls", encode(REPO)), &w.aiko)
        .await;
    assert_eq!(tp["items"][0]["closes"], true);
    assert_eq!(tp["items"][0]["label"], format!("!{n}"));

    // Draft: an agent may set draft, only the owner may mark ready.
    let (st, _) = s.patch(&base, &w.aiko, json!({"draft": true})).await;
    assert_eq!(st, 200);
    let (st, _) = s.patch(&base, &w.ken, json!({"draft": false})).await;
    assert_eq!(st, 403);
    let (st, v) = s.patch(&base, &w.owner, json!({"draft": false})).await;
    assert_eq!(st, 200);
    assert_ne!(v["ready_at"], p["ready_at"]);

    // Files: text has a patch from the first @@, binary has null.
    let files = s.ok(&format!("{base}/files"), &w.aiko).await;
    let items = files["items"].as_array().unwrap();
    let text = items.iter().find(|f| f["path"] == "f.txt").unwrap();
    assert_eq!(text["status"], "added");
    assert_eq!(text["additions"], 1);
    assert!(text["patch"].as_str().unwrap().starts_with("@@"));
    let bin = items.iter().find(|f| f["path"] == "bin.dat").unwrap();
    assert_eq!(bin["patch"], Value::Null);

    // Commits: what the head has and the base does not, oldest first, at UTC times.
    let commits = s.ok(&format!("{base}/commits"), &w.aiko).await;
    let subjects: Vec<&str> = commits["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["subject"].as_str().unwrap())
        .collect();
    assert_eq!(subjects, ["change f.txt", "binary"], "{commits}");
    assert_eq!(commits["items"][1]["sha"], p["head_sha"]);
    assert!(
        commits["items"][0]["committed_at"]
            .as_str()
            .unwrap()
            .ends_with('Z')
    );

    // Patrol: author and requested reviewer see it; a review answers the request.
    let (st, v) = s
        .put(&format!("{base}/reviewers/ken"), &w.aiko, json!({}))
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["reviewers"], json!(["ken"]));
    for token in [&w.aiko, &w.ken] {
        let patrol = s
            .ok("/pulls?involves=me&state=open&draft=false", token)
            .await;
        assert_eq!(patrol["items"].as_array().unwrap().len(), 1);
        assert!(patrol["items"][0]["activity"]["comments"].is_array());
    }
    let patrol = s.ok("/pulls?involves=me", &mia).await;
    assert_eq!(patrol["items"], json!([]));
    let (st, _) = s
        .post(
            &format!("{base}/reviews"),
            &w.ken,
            json!({"verdict": "request_changes", "body": "no", "commit": v["head_sha"]}),
        )
        .await;
    assert_eq!(st, 201);
    let after = s.ok(&base, &w.ken).await;
    assert_eq!(after["reviewers"], json!([]));
    let patrol = s.ok("/pulls?involves=me", &w.ken).await;
    assert_eq!(
        patrol["items"].as_array().unwrap().len(),
        1,
        "a reviewer stays involved"
    );

    // A thread reply involves its author without answering a review request.
    let (st, root) = s
        .post(
            &format!("{base}/inline-comments"),
            &w.ken,
            json!({"body": "here", "commit": after["head_sha"], "path": "f.txt", "line": 1, "side": "RIGHT"}),
        )
        .await;
    assert_eq!(st, 201, "{root}");
    let (st, _) = s
        .put(&format!("{base}/reviewers/mia"), &w.aiko, json!({}))
        .await;
    assert_eq!(st, 200);
    let (st, _) = s
        .post(
            &format!("{base}/threads/{}/replies", root["id"].as_str().unwrap()),
            &mia,
            json!({"body": "agreed"}),
        )
        .await;
    assert_eq!(st, 201);
    assert_eq!(s.ok(&base, &mia).await["reviewers"], json!(["mia"]));
    let (st, _) = s.delete(&format!("{base}/reviewers/mia"), &w.aiko).await;
    assert_eq!(st, 200);
    let patrol = s.ok("/pulls?involves=me", &mia).await;
    assert_eq!(patrol["items"].as_array().unwrap().len(), 1, "{patrol}");

    // Commit details.
    let c = s
        .ok(
            &format!(
                "{}/commits/{}",
                w.repo_path(),
                after["head_sha"].as_str().unwrap()
            ),
            &w.aiko,
        )
        .await;
    assert_eq!(c["message"], "binary");
    assert_eq!(c["parents"].as_array().unwrap().len(), 1);

    // Close and reopen.
    let (st, v) = s.post(&format!("{base}/close"), &w.aiko, json!({})).await;
    assert_eq!(st, 200);
    assert_eq!(v["state"], "closed");
    let (st, v) = s.post(&format!("{base}/reopen"), &w.aiko, json!({})).await;
    assert_eq!(st, 200);
    assert_eq!(v["state"], "open");
}

#[tokio::test]
async fn the_owner_is_involved_in_every_open_pull_request() {
    let w = World::new().await;
    let s = &w.server;
    let mia = s.member(&w.owner, "mia", "agent").await;
    // A second repository with an agent's pull request in it.
    let (st, v) = s
        .post("/repos", &w.owner, json!({"path": "acme/other"}))
        .await;
    assert_eq!(st, 201, "{v}");
    let other = s.remote("acme/other");
    git_as_ok(&w.work, "alice", &w.owner, &["push", "-q", &other, "main"]);
    w.push_change("elsewhere", "e.txt", "e\n");
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", &other, "+HEAD:refs/heads/elsewhere"],
    );
    let (st, v) = s
        .post(
            "/repos/acme%2Fother/pulls",
            &w.aiko,
            json!({"head": "elsewhere", "title": "Elsewhere", "body": "", "tickets": []}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    // In acme/demo: one open pull request with ken as reviewer, one draft,
    // and one closed.
    w.push_change("one", "1.txt", "1\n");
    let one = w.open_pull("one", json!([])).await;
    s.put(
        &format!("{}/{}/reviewers/ken", pulls(&w), one["number"]),
        &w.aiko,
        json!({}),
    )
    .await;
    w.push_change("two", "2.txt", "2\n");
    let two = w.open_pull("two", json!([])).await;
    s.patch(
        &format!("{}/{}", pulls(&w), two["number"]),
        &w.aiko,
        json!({"draft": true}),
    )
    .await;
    w.push_change("three", "3.txt", "3\n");
    let three = w.open_pull("three", json!([])).await;
    s.post(
        &format!("{}/{}/close", pulls(&w), three["number"]),
        &w.aiko,
        json!({}),
    )
    .await;

    let labels = |v: Value| -> Vec<String> {
        let mut all: Vec<String> = v["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| {
                format!(
                    "{} {}",
                    p["repository"].as_str().unwrap(),
                    p["label"].as_str().unwrap()
                )
            })
            .collect();
        all.sort();
        all
    };
    // The owner wrote, reviewed, and was asked about none of them, and sees
    // every open one, with the same entries an agent's patrol gets.
    let owner = s.ok("/pulls?involves=me&state=open", &w.owner).await;
    assert!(
        owner["items"][0]["activity"]["comments"].is_array(),
        "{owner}"
    );
    assert_eq!(
        labels(owner),
        ["acme/demo !1", "acme/demo !2", "acme/other !1"]
    );
    let ready = s.ok("/pulls?involves=me&draft=false", &w.owner).await;
    assert_eq!(labels(ready), ["acme/demo !1", "acme/other !1"]);
    // Page by page across both repositories: each once, none lost.
    let mut walked = Vec::new();
    let mut cursor = String::new();
    loop {
        let page = s
            .ok(
                &format!("/pulls?involves=me&state=open&limit=1{cursor}"),
                &w.owner,
            )
            .await;
        assert!(page["items"].as_array().unwrap().len() <= 1, "{page}");
        let next = page["next_cursor"].as_str().map(str::to_string);
        walked.extend(labels(page));
        match next {
            Some(c) => cursor = format!("&cursor={c}"),
            None => break,
        }
    }
    walked.sort();
    assert_eq!(walked, ["acme/demo !1", "acme/demo !2", "acme/other !1"]);
    // Agents still see only what involves them.
    assert_eq!(
        labels(s.ok("/pulls?involves=me", &w.ken).await),
        ["acme/demo !1"]
    );
    assert_eq!(
        labels(s.ok("/pulls?involves=me", &mia).await),
        Vec::<String>::new()
    );
    assert_eq!(
        labels(s.ok("/pulls?involves=me", &w.aiko).await),
        ["acme/demo !1", "acme/demo !2", "acme/other !1"]
    );
}

#[tokio::test]
async fn the_owner_merges_the_current_head_and_closes_tickets() {
    let w = World::new().await;
    let s = &w.server;
    let t = w.ticket("Ship it").await;
    let one = format!("/repos/{}/tickets/1", encode(REPO));
    s.put(
        &format!("{one}/board"),
        &w.aiko,
        json!({"lane": "in_progress"}),
    )
    .await;
    // A ticket of another repository is linked as a reference.
    s.post("/repos", &w.owner, json!({"path": "acme/other"}))
        .await;
    let (_, other) = s
        .post(
            &format!("/repos/{}/tickets", encode("acme/other")),
            &w.aiko,
            json!({"title": "There"}),
        )
        .await;
    let head = w.push_change("feature", "f.txt", "one\n");
    let p = w
        .open_pull(
            "feature",
            json!([{"url": t["url"], "closes": true}, {"url": other["url"], "closes": false}]),
        )
        .await;
    let n = p["number"].as_u64().unwrap();
    let base = format!("{}/{n}", pulls(&w));
    let main = p["base_sha"].as_str().unwrap().to_string();
    let merge = json!({"expected_head": head, "expected_base": main});

    // Only the owner merges.
    for agent in [&w.aiko, &w.ken] {
        let (st, _) = s.post(&format!("{base}/merge"), agent, merge.clone()).await;
        assert_eq!(st, 403);
    }

    // A head that moved is refused.
    let head2 = w.push_change("feature", "f.txt", "one\ntwo\n");
    let (st, v) = s
        .post(&format!("{base}/merge"), &w.owner, merge.clone())
        .await;
    assert_eq!(st, 409);
    assert_eq!(v["error"]["code"], "head_moved");
    let merge2 = json!({"expected_head": head2, "expected_base": main});
    // A stale base is refused.
    let (st, v) = s
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": head2, "expected_base": head}),
        )
        .await;
    assert_eq!(v["error"]["code"], "base_moved", "{st} {v}");

    // Required checks without a pipeline on the base: a failing check that
    // says why, never success; merge refused.
    let (_, repo) = s.get(&w.repo_path(), &w.owner).await;
    s.patch(
        &w.repo_path(),
        &w.owner,
        json!({"revision": repo["revision"], "required_checks": ["test"]}),
    )
    .await;
    let checks = s.ok(&format!("{base}/checks"), &w.aiko).await;
    assert_eq!(checks["checks_expected"], true);
    assert_eq!(checks["checks"][0]["name"], "test");
    assert_eq!(checks["checks"][0]["status"], "completed");
    assert_eq!(checks["checks"][0]["conclusion"], "failure");
    assert!(
        checks["checks"][0]["details"]
            .as_str()
            .unwrap()
            .contains(".branvil/ci.toml"),
        "{checks}"
    );
    assert!(checks["checks"][0].get("attempt_id").is_none());
    let (st, v) = s
        .post(&format!("{base}/merge"), &w.owner, merge2.clone())
        .await;
    assert_eq!(st, 409);
    assert!(
        v["error"]["message"].as_str().unwrap().contains("test"),
        "{v}"
    );
    let (_, repo) = s.get(&w.repo_path(), &w.owner).await;
    s.patch(
        &w.repo_path(),
        &w.owner,
        json!({"revision": repo["revision"], "required_checks": []}),
    )
    .await;
    let checks = s.ok(&format!("{base}/checks"), &w.aiko).await;
    assert_eq!(checks["checks"], json!([]));
    assert_eq!(checks["checks_expected"], false);

    // Now it merges: main moves to the head, the ticket is closed and done.
    let (st, v) = s.post(&format!("{base}/merge"), &w.owner, merge2).await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["merged"], true);
    assert_eq!(v["state"], "closed");
    assert_eq!(v["merge_sha"], head2.as_str());
    let (_, main_now) = s
        .get(&format!("{}/branches/main", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(main_now["sha"], head2.as_str());
    // Its branch is deleted; it keeps the head it merged.
    let (st, _) = s
        .get(&format!("{}/branches/feature", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(st, 404);
    assert_eq!(s.ok(&base, &w.aiko).await["head_sha"], head2.as_str());
    let checks = s.ok(&format!("{base}/checks"), &w.aiko).await;
    assert_eq!(checks["head_sha"], head2.as_str());
    let ticket = s.ok(&one, &w.aiko).await;
    assert_eq!(ticket["state"], "closed");
    assert_eq!(ticket["state_reason"], "completed");
    assert_eq!(ticket["lane"], "done");
    let other = s
        .ok(
            &format!("/repos/{}/tickets/1", encode("acme/other")),
            &w.aiko,
        )
        .await;
    assert_eq!(other["state"], "open");
    let closed = s
        .ok(&format!("/workspaces/{WS}/board/closed"), &w.aiko)
        .await;
    let kinds: Vec<&str> = closed["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["kind"].as_str().unwrap())
        .collect();
    assert!(
        kinds.contains(&"ticket") && kinds.contains(&"pull_request"),
        "{closed}"
    );
    let pr = closed["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["kind"] == "pull_request")
        .unwrap();
    assert_eq!(pr["merged"], true);
    // A merged pull request keeps its diff.
    assert_eq!(s.ok(&base, &w.aiko).await["changed_files"], 1);
}

#[tokio::test]
async fn a_merge_deletes_its_head_unless_protected_or_used_by_an_open_pull_request() {
    let world = World::new().await;
    let w = &world;
    let s = &w.server;
    let merge = |p: Value| async move {
        let url = format!("{}/pulls/{}", w.repo_path(), p["number"]);
        let p = s.ok(&url, &w.owner).await;
        let (st, v) = s
            .post(
                &format!("{url}/merge"),
                &w.owner,
                json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
            )
            .await;
        assert_eq!(st, 200, "{v}");
    };
    let tip = |branch: &'static str| async move {
        let (st, v) = s
            .get(&format!("{}/branches/{branch}", w.repo_path()), &w.aiko)
            .await;
        (st == 200).then(|| v["sha"].as_str().unwrap().to_string())
    };

    // A protected head stays.
    let release = w.push_change("release", "r.txt", "r\n");
    let repo = s.ok(&w.repo_path(), &w.owner).await;
    s.patch(
        &w.repo_path(),
        &w.owner,
        json!({"revision": repo["revision"], "protected_branches": ["release"]}),
    )
    .await;
    merge(w.open_pull("release", json!([])).await).await;
    assert_eq!(tip("release").await, Some(release));

    // A head that an open pull request has as its base stays; merging that
    // one into it deletes its own head, whatever the base.
    let feature = w.push_change("feature", "f.txt", "f\n");
    let stacked = w.push_change("stacked", "s.txt", "s\n");
    let p = w.open_pull("feature", json!([])).await;
    let (st, on_feature) = s
        .post(
            &pulls(w),
            &w.aiko,
            json!({"head": "stacked", "base": "feature", "title": "On feature"}),
        )
        .await;
    assert_eq!(st, 201, "{on_feature}");
    merge(p).await;
    assert_eq!(tip("feature").await, Some(feature));
    merge(on_feature["pull"].clone()).await;
    assert_eq!(tip("stacked").await, None);
    assert_eq!(tip("feature").await, Some(stacked.clone()));

    // What it merged outlives the branch it merged into, in a backup too.
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", "origin", ":refs/heads/feature"],
    );
    assert_eq!(tip("feature").await, None);
    let into = tempfile::tempdir().unwrap();
    let r = w.restored(into.path()).await;
    let url = format!("{}/pulls/{}", w.repo_path(), on_feature["pull"]["number"]);
    assert_eq!(r.ok(&url, &w.aiko).await["head_sha"], stacked.as_str());
    let files = r.ok(&format!("{url}/files"), &w.aiko).await;
    assert_eq!(files["items"][0]["path"], "s.txt", "{files}");
    let commits = r.ok(&format!("{url}/commits"), &w.aiko).await;
    assert_eq!(commits["items"].as_array().unwrap().len(), 1, "{commits}");
    assert_eq!(commits["items"][0]["subject"], "change s.txt");
}

async fn needs_approvals(w: &World, n: u64) {
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let (st, v) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "required_approvals": n}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["required_approvals"], n);
}

/// The review state of every member of the pull request `pull`, as
/// `[member, requested, verdict, outdated]`, and its approvals.
async fn review_states(w: &World, pull: &str) -> (Vec<Value>, u64) {
    let p = w.server.ok(pull, &w.aiko).await;
    let states = p["review_states"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| json!([x["member"], x["requested"], x["verdict"], x["outdated"]]))
        .collect();
    (states, p["approvals"].as_u64().unwrap())
}

pub(crate) async fn review(w: &World, pull: &str, token: &str, verdict: &str, commit: &str) -> u16 {
    let (st, v) = w
        .server
        .post(
            &format!("{pull}/reviews"),
            token,
            json!({"verdict": verdict, "commit": commit}),
        )
        .await;
    assert!(st == 201 || st == 400, "{verdict}: {st} {v}");
    st.as_u16()
}

/// The owner merges the pull request `pull` at `head` onto its current base.
async fn merge_at(w: &World, pull: &str, head: &str) -> (u16, Value) {
    let p = w.server.ok(pull, &w.owner).await;
    let (st, v) = w
        .server
        .post(
            &format!("{pull}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": p["base_sha"]}),
        )
        .await;
    (st.as_u16(), v)
}

#[tokio::test]
async fn reviewer_states_decide_the_approvals_a_merge_needs() {
    let w = World::new().await;
    let s = &w.server;
    // A new repository needs one approval; a count is never negative.
    let (st, v) = s
        .post("/repos", &w.owner, json!({"path": "acme/other"}))
        .await;
    assert_eq!(st, 201, "{v}");
    assert_eq!(v["required_approvals"], 1);
    let repo = s.ok(&w.repo_path(), &w.owner).await;
    let (st, _) = s
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "required_approvals": -1}),
        )
        .await;
    assert!(st.is_client_error(), "{st}");
    needs_approvals(&w, 2).await;

    let head = w.push_change("feature", "f.txt", "one\n");
    let n = w.open_pull("feature", json!([])).await["number"]
        .as_u64()
        .unwrap();
    let pull = format!("{}/{n}", pulls(&w));
    for name in ["ken", "alice"] {
        let (st, v) = s
            .put(&format!("{pull}/reviewers/{name}"), &w.owner, json!({}))
            .await;
        assert_eq!(st, 200, "{v}");
    }
    assert_eq!(
        review_states(&w, &pull).await,
        (
            vec![
                json!(["ken", true, null, false]),
                json!(["alice", true, null, false])
            ],
            0
        )
    );

    // The author only comments, and is no reviewer.
    assert_eq!(review(&w, &pull, &w.aiko, "approve", &head).await, 400);
    assert_eq!(
        review(&w, &pull, &w.aiko, "request_changes", &head).await,
        400
    );
    assert_eq!(review(&w, &pull, &w.aiko, "comment", &head).await, 201);
    // The last approve or request_changes decides; a comment keeps it, and
    // stands only for those who only commented.
    for (token, verdict) in [(&w.ken, "request_changes"), (&w.ken, "comment")] {
        assert_eq!(review(&w, &pull, token, verdict, &head).await, 201);
    }
    assert_eq!(
        review_states(&w, &pull).await.0[0],
        json!(["ken", false, "request_changes", false])
    );
    for (token, verdict) in [
        (&w.ken, "approve"),
        (&w.ken, "comment"),
        (&w.owner, "comment"),
    ] {
        assert_eq!(review(&w, &pull, token, verdict, &head).await, 201);
    }
    assert_eq!(
        review_states(&w, &pull).await,
        (
            vec![
                json!(["ken", false, "approve", false]),
                json!(["alice", false, "comment", false])
            ],
            1
        )
    );
    // A reply in a thread is no review.
    let (st, thread) = s
        .post(
            &format!("{pull}/inline-comments"),
            &w.aiko,
            json!({"body": "why?", "commit": head, "path": "f.txt", "line": 1, "side": "RIGHT"}),
        )
        .await;
    assert_eq!(st, 201, "{thread}");
    let (st, v) = s
        .post(
            &format!("{pull}/threads/{}/replies", thread["id"].as_str().unwrap()),
            &w.owner,
            json!({"body": "because"}),
        )
        .await;
    assert_eq!(st, 201, "{v}");
    assert_eq!(review_states(&w, &pull).await.1, 1);
    // The author answers the reply, which the last merge below needs.
    let (st, _) = s
        .post(
            &format!("{pull}/reactions"),
            &w.aiko,
            json!({"target_id": v["id"], "reaction": "ack"}),
        )
        .await;
    assert_eq!(st, 200);
    let (st, v) = merge_at(&w, &pull, &head).await;
    assert_eq!(st, 409, "{v}");
    assert_eq!(v["error"]["code"], "not_mergeable");
    let refused = format!("Approvals of {head}: 1 / 2 required.");
    assert_eq!(v["error"]["message"], refused.as_str(), "{v}");
    assert_eq!(review(&w, &pull, &w.owner, "approve", &head).await, 201);
    assert_eq!(review_states(&w, &pull).await.1, 2);

    // A new head leaves the approvals on the old one; a new request asks again.
    let head2 = w.push_change("feature", "f.txt", "one\ntwo\n");
    let (st, v) = s
        .put(&format!("{pull}/reviewers/ken"), &w.owner, json!({}))
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(
        review_states(&w, &pull).await,
        (
            vec![
                json!(["ken", true, "approve", true]),
                json!(["alice", false, "approve", true])
            ],
            0
        )
    );
    // A comment on the new head keeps the approval on the old one.
    assert_eq!(review(&w, &pull, &w.owner, "comment", &head2).await, 201);
    assert_eq!(
        review_states(&w, &pull).await,
        (
            vec![
                json!(["ken", true, "approve", true]),
                json!(["alice", false, "approve", true])
            ],
            0
        )
    );
    let (st, v) = merge_at(&w, &pull, &head2).await;
    assert_eq!(st, 409, "{v}");
    let refused = format!("Approvals of {head2}: 0 / 2 required.");
    assert_eq!(v["error"]["message"], refused.as_str(), "{v}");
    for token in [&w.ken, &w.owner] {
        assert_eq!(review(&w, &pull, token, "approve", &head2).await, 201);
    }
    assert_eq!(review_states(&w, &pull).await.1, 2);
    let (st, v) = merge_at(&w, &pull, &head2).await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["merged"], true);

    // 0 asks for none.
    needs_approvals(&w, 1).await;
    let head3 = w.push_change("second", "g.txt", "two\n");
    let n = w.open_pull("second", json!([])).await["number"]
        .as_u64()
        .unwrap();
    let pull = format!("{}/{n}", pulls(&w));
    assert_eq!(merge_at(&w, &pull, &head3).await.0, 409);
    needs_approvals(&w, 0).await;
    let (st, v) = merge_at(&w, &pull, &head3).await;
    assert_eq!(st, 200, "{v}");
}

#[tokio::test]
async fn a_pull_request_asks_the_reviewers_its_author_names() {
    let w = World::new().await;
    let s = &w.server;
    s.member(&w.owner, "box", "runner").await;
    let aiko = "/members/aiko";
    let reviewer = |member: &str, name: &str| format!("/members/{member}/reviewers/{name}");
    // The owner names them, one by one: owners and agents other than the
    // member, each once, spelled as they are named.
    for name in ["KEN", "alice", "ken"] {
        let (st, v) = s.put(&reviewer("aiko", name), &w.owner, json!({})).await;
        assert_eq!(st, 200, "{name}: {v}");
    }
    let (st, v) = s.put(&reviewer("aiko", "box"), &w.owner, json!({})).await;
    assert_eq!(st, 400, "a runner reviews nothing: {v}");
    for (member, name, status) in [
        ("aiko", "aiko", 400),
        ("aiko", "nobody", 404),
        ("box", "ken", 400),
    ] {
        let (st, v) = s.put(&reviewer(member, name), &w.owner, json!({})).await;
        assert_eq!(st, status, "{member} {name}: {v}");
    }
    let (st, _) = s.put(&reviewer("aiko", "ken"), &w.aiko, json!({})).await;
    assert_eq!(st, 403);
    let (st, _) = s.delete(&reviewer("aiko", "alice"), &w.aiko).await;
    assert_eq!(st, 403);
    // A display name change keeps them.
    let (st, v) = s
        .patch(aiko, &w.owner, json!({"display_name": "Aiko"}))
        .await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["reviewers"], json!(["ken", "alice"]));
    assert_eq!(
        s.ok(aiko, &w.aiko).await["reviewers"],
        json!(["ken", "alice"])
    );

    // A pull request aiko opens asks them, and ken's patrol sees it.
    w.push_change("feature", "f.txt", "one\n");
    let path = pulls(&w);
    let open = |head: &str| s.post(&path, &w.aiko, json!({"head": head, "title": head}));
    let (st, v) = open("feature").await;
    assert_eq!(st, 201, "{v}");
    assert_eq!(v["pull"]["reviewers"], json!(["ken", "alice"]));
    let patrol = s.ok("/pulls?involves=me", &w.ken).await;
    assert_eq!(patrol["items"].as_array().unwrap().len(), 1, "{patrol}");
    // Changing them leaves the pull requests already open as they are; the
    // next one asks the new ones.
    for name in ["Ken", "alice"] {
        let (st, v) = s.delete(&reviewer("aiko", name), &w.owner).await;
        assert_eq!(st, 200, "{v}");
        let (st, v) = s.delete(&reviewer("aiko", name), &w.owner).await;
        assert_eq!(st, 200, "withdrawing again changes nothing: {v}");
    }
    let (st, v) = open("feature").await;
    assert_eq!(st, 200, "{v}");
    assert_eq!(v["pull"]["reviewers"], json!(["ken", "alice"]));
    w.push_change("second", "g.txt", "two\n");
    let (st, v) = open("second").await;
    assert_eq!(st, 201, "{v}");
    assert_eq!(v["pull"]["reviewers"], json!([]));
}

/// The labels of pull requests `ns`, least recently updated first.
async fn by_update(w: &World, ns: [u64; 2]) -> [String; 2] {
    let mut found = Vec::new();
    for n in ns {
        let p = w
            .server
            .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
            .await;
        found.push((
            p["updated_at"].as_str().unwrap().to_string(),
            format!("!{n}"),
        ));
    }
    found.sort();
    [found[0].1.clone(), found[1].1.clone()]
}

/// The labels of the patrol's items for `token`, page by page `limit` at a
/// time.
pub(crate) async fn patrol_walk(w: &World, token: &str, limit: usize) -> Vec<String> {
    let mut walked = Vec::new();
    let mut cursor = String::new();
    loop {
        let page = w
            .server
            .ok(
                &format!("/pulls?involves=me&state=open&draft=false&limit={limit}{cursor}"),
                token,
            )
            .await;
        walked.extend(
            page["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| p["label"].as_str().unwrap().to_string()),
        );
        match page["next_cursor"].as_str() {
            Some(c) => cursor = format!("&cursor={c}"),
            None => return walked,
        }
    }
}

#[tokio::test]
async fn the_patrol_puts_first_what_waits_for_least_once_the_work_is_done() {
    use crate::ci::{Runner, eventually, pipeline, sync_main};
    let w = World::new().await;
    let s = &w.server;
    let runner = Runner::new(&w, "patrol-runner").await;
    let land = |n: u64| {
        let w = &w;
        async move {
            let at = format!("{}/pulls/{n}", w.repo_path());
            eventually(&format!("!{n} passed"), || async {
                s.ok(&at, &w.owner).await["landing"]["state"] == "passed"
            })
            .await;
            let p = s.ok(&at, &w.owner).await;
            let (st, v) = merge_at(w, &at, p["head_sha"].as_str().unwrap()).await;
            assert_eq!(st, 200, "{v}");
        }
    };
    std::fs::create_dir_all(w.work.join(".branvil")).unwrap();
    w.push_change("pipeline", ".branvil/ci.toml", &pipeline(&["test"]));
    w.open_pull("pipeline", json!([])).await;
    runner.drain("success").await;
    land(1).await;
    // !3 changes the README the base changes after it (!2).
    sync_main(&w);
    w.push_change("conflict", "README.md", "mine\n");
    sync_main(&w);
    w.push_change("advance", "README.md", "theirs\n");
    w.open_pull("advance", json!([])).await;
    runner.drain("success").await;
    land(2).await;
    let repo = s.ok(&w.repo_path(), &w.owner).await;
    let (st, v) = s
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "required_checks": ["test"], "required_approvals": 1}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    w.open_pull("conflict", json!([])).await;
    sync_main(&w);
    let mut heads = BTreeMap::new();
    for (n, branch) in [(4, "approved"), (5, "ready"), (6, "running"), (7, "failed")] {
        git_ok(&w.work, &["checkout", "-q", "main"]);
        heads.insert(
            w.push_change(branch, &format!("{branch}.txt"), "x\n"),
            branch,
        );
        assert_eq!(w.open_pull(branch, json!([])).await["number"], n);
    }
    let mut running = Vec::new();
    while let Some(c) = runner.claim().await {
        match heads.get(c["sha"].as_str().unwrap()).copied() {
            Some("running") => running.push(c),
            Some("failed") => assert_eq!(runner.finish(&c, "failure").await.0, 200),
            _ => assert_eq!(runner.finish(&c, "success").await.0, 200),
        }
    }
    assert_eq!(running.len(), 1);
    for n in 3..=7 {
        let at = format!("{}/pulls/{n}", w.repo_path());
        let (st, v) = s
            .put(&format!("{at}/reviewers/ken"), &w.owner, json!({}))
            .await;
        assert_eq!(st, 200, "{v}");
    }
    // ken approves !3, which leaves the queue in conflict, then !4, which
    // passes its gate.
    for n in [3, 4] {
        let at = format!("{}/pulls/{n}", w.repo_path());
        let head = s.ok(&at, &w.owner).await["head_sha"].clone();
        assert_eq!(
            review(&w, &at, &w.ken, "approve", head.as_str().unwrap()).await,
            201
        );
        let state = if n == 3 { "left" } else { "passed" };
        eventually(&format!("!{n} {state}"), || async {
            s.ok(&at, &w.owner).await["landing"]["state"] == state
        })
        .await;
    }
    assert_eq!(
        s.ok(&format!("{}/pulls/3", w.repo_path()), &w.owner).await["landing"]["reason"]["code"],
        "conflict"
    );
    let fives = by_update(&w, [5, 6]).await;
    let eights = by_update(&w, [3, 4]).await;
    // aiko wrote them all: approved and passed (2), approved but in conflict
    // (4), unapproved with checks passed or running (5), failed (6).
    let author = ["!4", "!3", &fives[0], &fives[1], "!7"];
    // ken reviews them: passed (1), running (3), failed (7), approved (8).
    let reviewer = ["!5", "!6", "!7", &eights[0], &eights[1]];
    for (token, expected) in [(&w.aiko, author), (&w.ken, reviewer)] {
        assert_eq!(patrol_walk(&w, token, 100).await, expected);
        assert_eq!(patrol_walk(&w, token, 1).await, expected, "page by page");
    }
    drop(running);
}
