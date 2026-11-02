//! Images attached to tickets: upload, delivery, the tickets' references, and
//! the deletion of attachments no ticket refers to.

use crate::common::*;
use reqwest::StatusCode;
use serde_json::{Value, json};

/// A PNG's signature followed by `len` bytes in all.
fn png(len: usize) -> Vec<u8> {
    let mut bytes = b"\x89PNG\r\n\x1a\n".to_vec();
    bytes.resize(len, 7);
    bytes
}

async fn upload(
    s: &Server,
    token: &str,
    name: &str,
    content_type: &str,
    bytes: Vec<u8>,
) -> (StatusCode, Value) {
    let resp = s
        .http
        .post(format!(
            "{}/api/v1/repos/{}/attachments?name={name}",
            s.url,
            encode(REPO)
        ))
        .bearer_auth(token)
        .header("content-type", content_type)
        .body(bytes)
        .send()
        .await
        .unwrap();
    let status = resp.status();
    (status, resp.json().await.unwrap())
}

async fn fetch(s: &Server, token: &str, url: &str) -> reqwest::Response {
    s.http
        .get(format!("{}{url}", s.url))
        .bearer_auth(token)
        .send()
        .await
        .unwrap()
}

#[tokio::test]
async fn images_are_kept_by_their_bytes_and_served_as_they_were_read() {
    let w = World::new().await;
    let s = &w.server;
    // Larger than a JSON body may be, and said to be something else.
    let image = png(3 << 20);
    let (st, a) = upload(s, &w.aiko, "screen.png", "image/svg+xml", image.clone()).await;
    assert_eq!(st, StatusCode::CREATED, "{a}");
    let id = a["id"].as_str().unwrap();
    assert!(id.starts_with("atc_"), "{a}");
    assert_eq!(a["name"], "screen.png");
    assert_eq!(a["media_type"], "image/png");
    assert_eq!(a["size"], 3 << 20);
    assert_eq!(
        a["url"],
        format!("/api/v1/repos/{}/attachments/{id}", encode(REPO))
    );
    let resp = fetch(s, &w.ken, a["url"].as_str().unwrap()).await;
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(resp.headers()["content-type"], "image/png");
    assert_eq!(resp.headers()["x-content-type-options"], "nosniff");
    assert!(
        resp.headers()["cache-control"]
            .to_str()
            .unwrap()
            .contains("immutable")
    );
    assert_eq!(resp.bytes().await.unwrap().to_vec(), image);

    // Each kind by its own signature; nothing else, SVG included.
    for (bytes, kind) in [
        (b"\xff\xd8\xff\xe0rest".to_vec(), "image/jpeg"),
        (b"GIF89a....".to_vec(), "image/gif"),
        (b"RIFF\0\0\0\0WEBPVP8 ".to_vec(), "image/webp"),
    ] {
        let (st, a) = upload(s, &w.aiko, "x", "application/octet-stream", bytes).await;
        assert_eq!(
            (st, a["media_type"].as_str()),
            (StatusCode::CREATED, Some(kind))
        );
    }
    for bytes in [
        b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>".to_vec(),
        b"plain text".to_vec(),
        Vec::new(),
    ] {
        let (st, e) = upload(s, &w.aiko, "x.png", "image/png", bytes).await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{e}");
        assert_eq!(e["error"]["code"], "invalid");
    }
    let (st, e) = upload(s, &w.aiko, "big.png", "image/png", png((10 << 20) + 1)).await;
    assert_eq!(st, StatusCode::PAYLOAD_TOO_LARGE, "{e}");
    assert_eq!(e["error"]["code"], "too_large");
    let (st, _) = upload(s, &w.aiko, "max.png", "image/png", png(10 << 20)).await;
    assert_eq!(st, StatusCode::CREATED);
    // A name with a control character is refused; none is `image`.
    let (st, _) = upload(s, &w.aiko, "a%0Ab.png", "image/png", png(16)).await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    let (st, a) = upload(s, &w.aiko, "", "image/png", png(16)).await;
    assert_eq!(
        (st, a["name"].as_str()),
        (StatusCode::CREATED, Some("image"))
    );
    // An upload is never replayed: its body is larger than a JSON body.
    let resp = s
        .http
        .post(format!(
            "{}/api/v1/repos/{}/attachments",
            s.url,
            encode(REPO)
        ))
        .bearer_auth(&w.aiko)
        .header("Idempotency-Key", "same")
        .body(png(2 << 20))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
}

#[tokio::test]
async fn only_members_who_read_the_repository_get_its_attachments() {
    let w = World::new().await;
    let s = &w.server;
    let runner = s.member(&w.owner, "runner1", "runner").await;
    let (st, _) = upload(s, &runner, "x.png", "image/png", png(16)).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (_, a) = upload(s, &w.aiko, "x.png", "image/png", png(16)).await;
    let url = a["url"].as_str().unwrap();
    assert_eq!(fetch(s, &runner, url).await.status(), StatusCode::FORBIDDEN);
    assert_eq!(fetch(s, &w.owner, url).await.status(), StatusCode::OK);
    let anonymous = s.http.get(format!("{}{url}", s.url)).send().await.unwrap();
    assert_eq!(anonymous.status(), StatusCode::UNAUTHORIZED);
    // The GUI's session reads it as an `<img>` does: a cookie, no CSRF.
    let (cookie, _) = s.session(&w.owner).await;
    let resp = s
        .http
        .get(format!("{}{url}", s.url))
        .header("cookie", cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    // An attachment belongs to its repository only.
    assert_eq!(
        s.post("/repos", &w.owner, json!({"path": "acme/other"}))
            .await
            .0,
        StatusCode::CREATED
    );
    let elsewhere = url.replace(&encode(REPO), &encode("acme/other"));
    assert_eq!(
        fetch(s, &w.aiko, &elsewhere).await.status(),
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn a_ticket_lists_the_attachments_its_body_and_comments_refer_to() {
    let w = World::new().await;
    let s = &w.server;
    let mut ids = Vec::new();
    let mut urls = Vec::new();
    for name in ["one.png", "two.png", "three.png"] {
        let (_, a) = upload(s, &w.aiko, name, "image/png", png(16)).await;
        ids.push(a["id"].as_str().unwrap().to_string());
        urls.push(a["url"].as_str().unwrap().to_string());
    }
    let tickets = format!("{}/tickets", w.repo_path());
    let (_, t) = s
        .post(
            &tickets,
            &w.aiko,
            json!({"title": "Broken", "body": format!("![one]({})\nand {}{}", urls[0], s.url, urls[1])}),
        )
        .await;
    let ticket = format!("{tickets}/{}", t["key"].as_str().unwrap());
    let (_, c) = s
        .post(
            &format!("{ticket}/comments"),
            &w.ken,
            json!({"body": format!("again ![one]({}) and ![three]({}) and a missing atc_01JAAAAAAAAAAAAAAAAAAAAAAA", urls[0], urls[2])}),
        )
        .await;
    let comment = c["id"].as_str().unwrap();
    let list = s.ok(&format!("{ticket}/attachments"), &w.aiko).await;
    let got: Vec<(String, Value)> = list["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| {
            (
                i["id"].as_str().unwrap().to_string(),
                i["referenced_by"].clone(),
            )
        })
        .collect();
    assert_eq!(
        got,
        vec![
            (ids[0].clone(), json!(["body", comment])),
            (ids[1].clone(), json!(["body"])),
            (ids[2].clone(), json!([comment])),
        ]
    );
    let first = &list["items"][0];
    assert_eq!(first["name"], "one.png");
    assert_eq!(first["media_type"], "image/png");
    assert_eq!(first["size"], 16);
    assert_eq!(first["url"], urls[0].as_str());
    assert_eq!(list["next_cursor"], Value::Null);
    // Pages of one.
    let page = s
        .ok(&format!("{ticket}/attachments?limit=1"), &w.aiko)
        .await;
    assert_eq!(page["items"][0]["id"], ids[0].as_str());
    let cursor = page["next_cursor"].as_str().unwrap();
    let next = s
        .ok(
            &format!("{ticket}/attachments?limit=1&cursor={cursor}"),
            &w.aiko,
        )
        .await;
    assert_eq!(next["items"][0]["id"], ids[1].as_str());
    // Editing the comment changes what it refers to.
    s.patch(
        &format!("{ticket}/comments/{comment}"),
        &w.ken,
        json!({"body": "no image"}),
    )
    .await;
    let list = s.ok(&format!("{ticket}/attachments"), &w.aiko).await;
    assert_eq!(list["items"].as_array().unwrap().len(), 2);
    assert_eq!(list["items"][0]["referenced_by"], json!(["body"]));
}

#[tokio::test]
async fn attachments_no_ticket_refers_to_are_deleted_after_their_grace() {
    let mut w = World::new().await;
    let s = &w.server;
    let mut urls = Vec::new();
    for name in [
        "body.png",
        "comment.png",
        "deleted.png",
        "loose.png",
        "fresh.png",
    ] {
        let (_, a) = upload(s, &w.aiko, name, "image/png", png(16)).await;
        urls.push(a["url"].as_str().unwrap().to_string());
    }
    let tickets = format!("{}/tickets", w.repo_path());
    let (_, t) = s
        .post(
            &tickets,
            &w.aiko,
            json!({"title": "T", "body": format!("![a]({})", urls[0])}),
        )
        .await;
    let ticket = format!("{tickets}/{}", t["key"].as_str().unwrap());
    s.post(
        &format!("{ticket}/comments"),
        &w.aiko,
        json!({"body": format!("![b]({})", urls[1])}),
    )
    .await;
    let (_, gone) = s
        .post(
            &format!("{ticket}/comments"),
            &w.aiko,
            json!({"body": format!("![c]({})", urls[2])}),
        )
        .await;
    let (st, _) = s
        .delete(
            &format!("{ticket}/comments/{}", gone["id"].as_str().unwrap()),
            &w.aiko,
        )
        .await;
    assert_eq!(st, StatusCode::NO_CONTENT);
    w.server.stop();
    // Age every upload but the last past the grace, and leave a file that
    // has had no record for as long, and a new one.
    let data = w.server.data.clone();
    let db = data.join("branvil.redb");
    let fresh = urls[4].rsplit('/').next().unwrap().to_string();
    edit_records(&db, "attachments", |a| {
        if a["id"] != fresh.as_str() {
            a["created_at"] = json!("2026-01-01T00:00:00.000Z");
        }
    });
    let dir = data.join("attachments");
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(48 * 3600);
    for (name, modified) in [("atc_orphan", Some(old)), ("atc_young", None)] {
        std::fs::write(dir.join(name), b"x").unwrap();
        if let Some(t) = modified {
            std::fs::File::options()
                .write(true)
                .open(dir.join(name))
                .unwrap()
                .set_modified(t)
                .unwrap();
        }
    }
    w.restart(&[]);
    let s = &w.server;
    // The sweep at start.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
    while dir.join("atc_orphan").exists() {
        assert!(
            std::time::Instant::now() < deadline,
            "the start's sweep did not run"
        );
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    let status = |i: usize| fetch(s, &w.aiko, &urls[i]);
    assert_eq!(
        status(0).await.status(),
        StatusCode::OK,
        "the body refers to it"
    );
    assert_eq!(
        status(1).await.status(),
        StatusCode::OK,
        "a comment refers to it"
    );
    assert_eq!(
        status(2).await.status(),
        StatusCode::NOT_FOUND,
        "its comment is gone"
    );
    assert_eq!(
        status(3).await.status(),
        StatusCode::NOT_FOUND,
        "nothing refers to it"
    );
    assert_eq!(status(4).await.status(), StatusCode::OK, "within its grace");
    assert!(dir.join("atc_young").exists());
    w.server.stop();
    let mut kept: Vec<String> = records(&db, "attachments")
        .iter()
        .map(|a| a["name"].as_str().unwrap().to_string())
        .collect();
    kept.sort();
    assert_eq!(kept, ["body.png", "comment.png", "fresh.png"]);
    for gone in &urls[2..4] {
        assert!(!dir.join(gone.rsplit('/').next().unwrap()).exists());
    }
}

#[tokio::test]
async fn a_restored_backup_serves_the_attachments() {
    let w = World::new().await;
    let s = &w.server;
    let (_, a) = upload(s, &w.aiko, "x.png", "image/png", png(64)).await;
    let url = a["url"].as_str().unwrap().to_string();
    s.post(
        &format!("{}/tickets", w.repo_path()),
        &w.aiko,
        json!({"title": "T", "body": format!("![x]({url})")}),
    )
    .await;
    let into = tempfile::tempdir().unwrap();
    let restored = w.restored(into.path()).await;
    let resp = fetch(&restored, &w.aiko, &url).await;
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(resp.bytes().await.unwrap().to_vec(), png(64));
    let list = restored
        .ok(&format!("{}/tickets/1/attachments", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(list["items"][0]["id"], a["id"]);
}
