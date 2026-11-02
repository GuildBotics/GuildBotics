//! Security properties claimed in `docs/security.md` that other tests do not
//! already cover: token revocation (API and Git), Host checks on every route,
//! entry links (owner only, single use, gone after a restart), Origin and CSRF
//! on cookie writes, ending every session through the owner's token file,
//! what each member kind may do, owner-only endpoints, replacing a label's
//! tokens and the reserved labels, local apps' consent exchange (PKCE,
//! loopback redirects, single-use codes), hook callbacks, and the absence of
//! CORS.

use crate::common;

use common::*;
use reqwest::StatusCode;
use serde_json::{Value, json};

#[tokio::test]
async fn a_revoked_token_no_longer_opens_the_api_or_git() {
    let w = World::new().await;
    let s = &w.server;
    let (st, issued) = s
        .post("/members/aiko/tokens", &w.aiko, json!({"label": "laptop"}))
        .await;
    assert_eq!(st, StatusCode::CREATED);
    let token = issued["token"].as_str().unwrap().to_string();
    let id = issued["id"].as_str().unwrap().to_string();
    assert_eq!(s.get("/me", &token).await.0, StatusCode::OK);
    let clones = w.dir.path().join("clones");
    std::fs::create_dir_all(&clones).unwrap();
    git_as_ok(&clones, "aiko", &token, &["ls-remote", &s.remote(REPO)]);
    // Another member cannot revoke it; aiko can.
    let url = format!("{}/api/v1/members/aiko/tokens/{id}", s.url);
    let resp = s
        .http
        .delete(&url)
        .bearer_auth(&w.ken)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let resp = s
        .http
        .delete(&url)
        .bearer_auth(&w.aiko)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    let (st, v) = s.get("/me", &token).await;
    assert_eq!(st, StatusCode::UNAUTHORIZED);
    assert_eq!(v["error"]["code"], "unauthorized");
    assert!(
        !git_as(&clones, "aiko", &token, &["ls-remote", &s.remote(REPO)])
            .status
            .success()
    );
    // Only the hash is listed; the token text never comes back.
    let list = s.ok("/members/aiko/tokens", &w.aiko).await;
    assert!(!list.to_string().contains(&token));
    // `bvt_<id>_<secret>`: the secret is base64url and may itself contain `_`.
    let secret = token.splitn(3, '_').nth(2).unwrap();
    assert!(!list.to_string().contains(secret));
    // The other tokens of aiko still work.
    assert_eq!(s.get("/me", &w.aiko).await.0, StatusCode::OK);

    // An Idempotency-Key does not make the service keep a token's text: a
    // repeated request issues another token, and the database never holds one.
    let issue = || {
        s.http
            .post(format!("{}/api/v1/members/aiko/tokens", s.url))
            .bearer_auth(&w.aiko)
            .header("Idempotency-Key", "token-1")
            .json(&json!({"label": "again"}))
            .send()
    };
    let a: Value = issue().await.unwrap().json().await.unwrap();
    let b: Value = issue().await.unwrap().json().await.unwrap();
    assert_ne!(a["token"], b["token"]);
    let db = std::fs::read(s.data.join("branvil.redb")).unwrap();
    for t in [&a["token"], &b["token"], &issued["token"]] {
        // `bvt_<id>_<secret>`: the secret itself may contain `_`.
        let secret = t.as_str().unwrap().splitn(3, '_').nth(2).unwrap();
        assert!(
            !db.windows(secret.len()).any(|w| w == secret.as_bytes()),
            "a token secret is in the database"
        );
    }
}

/// A token's use is recorded (`last_used_at`) without the request waiting
/// for it; the record appears shortly after.
#[tokio::test]
async fn token_use_is_recorded() {
    let w = World::new().await;
    let s = &w.server;
    let (st, issued) = s
        .post("/members/aiko/tokens", &w.aiko, json!({"label": "probe"}))
        .await;
    assert_eq!(st, StatusCode::CREATED);
    let id = issued["id"].as_str().unwrap().to_string();
    let token = issued["token"].as_str().unwrap().to_string();
    assert_eq!(s.get("/me", &token).await.0, StatusCode::OK);
    let mut last_used = Value::Null;
    for _ in 0..100 {
        let list = s.ok("/members/aiko/tokens", &w.aiko).await;
        let item = list["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["id"] == id)
            .cloned()
            .unwrap();
        last_used = item["last_used_at"].clone();
        if !last_used.is_null() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    assert!(last_used.is_string(), "last_used_at was never recorded");
}

#[tokio::test]
async fn hosts_and_origins_are_checked_on_every_kind_of_route() {
    let w = World::new().await;
    let s = &w.server;
    let http = reqwest::Client::new();
    let port = s.url.rsplit(':').next().unwrap();
    // A rebinding name with the right port, the right name with another
    // port, on the API, Git, and GUI routes.
    for host in [
        format!("attacker.example:{port}"),
        "127.0.0.1:1".to_string(),
    ] {
        for path in [
            "/api/v1/health".to_string(),
            format!("/git/{REPO}.git/info/refs?service=git-upload-pack"),
            "/".to_string(),
        ] {
            let resp = http
                .get(format!("{}{path}", s.url))
                .header("host", &host)
                .send()
                .await
                .unwrap();
            assert_eq!(
                resp.status(),
                StatusCode::MISDIRECTED_REQUEST,
                "{host} {path}"
            );
        }
    }
    // localhost with the right port is the same service.
    let resp = http
        .get(format!("{}/api/v1/health", s.url))
        .header("host", format!("localhost:{port}"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    // A same-origin request gets no CORS headers; a cross-origin one is refused.
    let resp = http
        .get(format!("{}/api/v1/me", s.url))
        .bearer_auth(&w.ken)
        .header("origin", &s.url)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert!(resp.headers().get("access-control-allow-origin").is_none());
    for origin in [
        "null",
        "http://127.0.0.1:1",
        "http://localhost.attacker.example",
    ] {
        let resp = http
            .post(format!("{}/api/v1/workspaces", s.url))
            .bearer_auth(&w.owner)
            .header("origin", origin)
            .json(&json!({"name": "evil"}))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN, "{origin}");
    }
    let workspaces = s.ok("/workspaces", &w.owner).await;
    assert!(!workspaces.to_string().contains("evil"));
    // API responses carry the security headers too.
    let resp = http
        .get(format!("{}/api/v1/health", s.url))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.headers()["x-frame-options"], "DENY");
    assert_eq!(resp.headers()["referrer-policy"], "no-referrer");
}

#[tokio::test]
async fn entry_links_are_the_owners_work_once_and_end_with_the_process() {
    let mut w = World::new().await;
    let s = &w.server;
    // Agents get no link.
    for agent in [&w.aiko, &w.ken] {
        let (st, _) = s.post("/session/links", agent, json!({})).await;
        assert_eq!(st, StatusCode::FORBIDDEN);
    }
    let code = s.entry_code(&w.owner).await;
    // A cross-origin or misdirected exchange is refused before the code is used.
    let resp = s
        .http
        .post(format!("{}/api/v1/session", s.url))
        .header("origin", "http://attacker.example")
        .json(&json!({"code": code}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let resp = s
        .http
        .post(format!("{}/api/v1/session", s.url))
        .header("host", "attacker.example")
        .json(&json!({"code": code}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::MISDIRECTED_REQUEST);
    // The code works once.
    let resp = s.enter(&code).await;
    assert_eq!(resp.status(), StatusCode::OK);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(
        (&body["me"]["name"], &body["me"]["kind"]),
        (&json!("alice"), &json!("owner"))
    );
    for code in [code.as_str(), "guess"] {
        let resp = s.enter(code).await;
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED, "{code}");
    }
    // The code is written nowhere: not in the service log (debug level here)
    // nor in the database.
    let log = std::fs::read_to_string(w.dir.path().join("serve.log")).unwrap();
    assert!(!log.contains(&code), "code in the service log");
    let db = std::fs::read(s.data.join("branvil.redb")).unwrap();
    assert!(!db.windows(code.len()).any(|win| win == code.as_bytes()));
    // An Idempotency-Key does not make the service keep a link.
    let link = || {
        s.http
            .post(format!("{}/api/v1/session/links", s.url))
            .bearer_auth(&w.owner)
            .header("Idempotency-Key", "link-1")
            .send()
    };
    let a: Value = link().await.unwrap().json().await.unwrap();
    let b: Value = link().await.unwrap().json().await.unwrap();
    assert_ne!(a["url"], b["url"]);
    // Codes live in memory only: a restart invalidates the unused ones.
    let code = w.server.entry_code(&w.owner).await;
    w.restart(&[]);
    assert_eq!(
        w.server.enter(&code).await.status(),
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn cookie_writes_need_the_sessions_own_csrf() {
    let w = World::new().await;
    let s = &w.server;
    let (cookie, csrf) = s.session(&w.owner).await;
    let (_, other_csrf) = s.session(&w.owner).await;
    let url = format!("{}/api/v1/repos/{}/tickets", s.url, encode(REPO));
    for sent in ["", "wrong", other_csrf.as_str()] {
        let mut req = s
            .http
            .post(&url)
            .header("cookie", &cookie)
            .json(&json!({"title": "x"}));
        if !sent.is_empty() {
            req = req.header("x-branvil-csrf", sent);
        }
        let response = req.send().await.unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response.json::<Value>().await.unwrap()["error"]["code"],
            "csrf_mismatch"
        );
    }
    // The right CSRF value from another origin is still refused.
    let resp = s
        .http
        .post(&url)
        .header("cookie", &cookie)
        .header("x-branvil-csrf", &csrf)
        .header("origin", "http://attacker.example")
        .json(&json!({"title": "x"}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let tickets = s
        .ok(&format!("/repos/{}/tickets", encode(REPO)), &w.ken)
        .await;
    assert!(tickets["items"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn an_old_tab_can_refresh_csrf_after_another_entry_replaces_the_cookie() {
    let w = World::new().await;
    let s = &w.server;
    let (_, old_csrf) = s.session(&w.owner).await;
    let (cookie, _) = s.session(&w.owner).await;
    let url = format!("{}/api/v1/repos/{}/tickets", s.url, encode(REPO));
    let refused = s
        .http
        .post(&url)
        .header("cookie", &cookie)
        .header("x-branvil-csrf", old_csrf)
        .json(&json!({"title": "from the old tab"}))
        .send()
        .await
        .unwrap();
    assert_eq!(refused.status(), StatusCode::FORBIDDEN);
    assert_eq!(
        refused.json::<Value>().await.unwrap()["error"]["code"],
        "csrf_mismatch"
    );
    let session: Value = s
        .http
        .get(format!("{}/api/v1/session", s.url))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let saved = s
        .http
        .post(&url)
        .header("cookie", &cookie)
        .header("x-branvil-csrf", session["csrf"].as_str().unwrap())
        .json(&json!({"title": "from the old tab"}))
        .send()
        .await
        .unwrap();
    assert_eq!(saved.status(), StatusCode::CREATED);
    let tickets = s
        .ok(&format!("/repos/{}/tickets", encode(REPO)), &w.ken)
        .await;
    assert_eq!(tickets["items"].as_array().unwrap().len(), 1);
}

/// Without a sign-out, the owner's token file is the way to end every GUI
/// session: `serve` replaces a missing file with a new token and drops the
/// sessions and the previous file token.
#[tokio::test]
async fn a_new_owner_token_ends_every_session() {
    let mut w = World::new().await;
    let (cookie, _) = w.server.session(&w.owner).await;
    let me = |s: &Server, cookie: &str| {
        s.http
            .get(format!("{}/api/v1/me", s.url))
            .header("cookie", cookie)
            .send()
    };
    // A restart keeps a valid file, its token, and the sessions.
    w.restart(&[]);
    assert_eq!(owner_token(&w.server.data), w.owner);
    assert_eq!(
        me(&w.server, &cookie).await.unwrap().status(),
        StatusCode::OK
    );
    std::fs::remove_file(w.server.data.join("secrets/owner.token")).unwrap();
    w.restart(&[]);
    let s = &w.server;
    assert_eq!(
        me(s, &cookie).await.unwrap().status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(s.get("/me", &w.owner).await.0, StatusCode::UNAUTHORIZED);
    let fresh = owner_token(&s.data);
    assert_ne!(fresh, w.owner);
    assert_eq!(s.ok("/me", &fresh).await["kind"], "owner");
    // Agents' tokens are untouched.
    assert_eq!(s.get("/me", &w.aiko).await.0, StatusCode::OK);
}

#[tokio::test]
async fn member_kinds_decide_what_a_member_may_do() {
    let w = World::new().await;
    let s = &w.server;
    // An agent reads and works in every workspace's repositories.
    s.post("/workspaces", &w.owner, json!({"name": "beta"}))
        .await;
    let (st, v) = s.post("/repos", &w.owner, json!({"path": "beta/x"})).await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let beta = format!("/repos/{}", encode("beta/x"));
    assert_eq!(s.get(&beta, &w.aiko).await.0, StatusCode::OK);
    let (st, v) = s
        .post(&format!("{beta}/tickets"), &w.aiko, json!({"title": "x"}))
        .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let repos = s.ok("/repos", &w.aiko).await;
    assert_eq!(repos["items"].as_array().unwrap().len(), 2, "{repos}");
    let clones = w.dir.path().join("clones");
    std::fs::create_dir_all(&clones).unwrap();
    let ls = |member: &str, token: &str| {
        git_as(&clones, member, token, &["ls-remote", &s.remote("beta/x")])
            .status
            .success()
    };
    assert!(ls("aiko", &w.aiko));
    // A runner uses the runner API only: no repository, ticket, event, or Git.
    let runner = s.member(&w.owner, "runner1", "runner").await;
    for path in ["/repos", "/workspaces", "/pulls?involves=me", "/events"] {
        let (st, _) = s.get(path, &runner).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{path}");
    }
    assert!(!ls("runner1", &runner));
    // Owner-only endpoints refuse an agent.
    for path in ["/members", "/admin/diagnostics"] {
        let (st, _) = s.get(path, &w.aiko).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{path}");
    }
    for (path, body) in [
        (
            format!("/repos/{}/upstream", encode(REPO)),
            json!({"provider": "github", "repository": "octo/demo"}),
        ),
        (
            "/members/aiko/accounts/github".to_string(),
            json!({"token": "t"}),
        ),
    ] {
        let (st, _) = s.put(&path, &w.aiko, body).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{path}");
    }
    let repo = s.ok(&beta, &w.aiko).await;
    for (path, body) in [
        ("/admin/backup", json!({"to": "/nonexistent-backup"})),
        ("/workspaces", json!({"name": "gamma"})),
        ("/repos", json!({"path": "beta/y"})),
    ] {
        let (st, _) = s.post(path, &w.aiko, body).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{path}");
    }
    assert!(!std::path::Path::new("/nonexistent-backup").exists());
    let (st, _) = s
        .patch(
            &beta,
            &w.aiko,
            json!({"revision": repo["revision"], "labels": ["x"]}),
        )
        .await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    // There is no second owner.
    for kind in ["owner", "human"] {
        let (st, v) = s
            .post(
                "/members",
                &w.owner,
                json!({"name": "second", "kind": kind}),
            )
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{kind}: {v}");
    }
    // The owner's GUI session lists members; an agent's token does not.
    let (cookie, _) = s.session(&w.owner).await;
    let resp = s
        .http
        .get(format!("{}/api/v1/members", s.url))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[cfg(unix)]
#[tokio::test]
async fn the_data_directory_is_owner_only() {
    use std::os::unix::fs::PermissionsExt;
    let w = World::new().await;
    for dir in [w.server.data.clone(), w.server.data.join("secrets")] {
        let mode = std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o700, "{}", dir.display());
    }
    for file in ["cursor.key", "owner.token"] {
        let mode = std::fs::metadata(w.server.data.join("secrets").join(file))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "{file}");
    }
}

#[tokio::test]
async fn hook_callbacks_need_the_per_process_secret() {
    let w = World::new().await;
    let s = &w.server;
    for (hook, body) in [
        (
            "pre-receive",
            json!({"repo": "x", "member": "aiko", "updates": []}),
        ),
        ("post-receive", json!({"repo": "x"})),
    ] {
        for secret in [None, Some("guess")] {
            let mut req = s.http.post(format!("{}/-/hooks/{hook}", s.url)).json(&body);
            if let Some(v) = secret {
                req = req.header("x-branvil-hook", v);
            }
            assert_eq!(req.send().await.unwrap().status(), StatusCode::FORBIDDEN);
        }
    }
}

#[tokio::test]
async fn a_member_is_read_by_the_owner_or_itself() {
    let w = World::new().await;
    let s = &w.server;
    let runner = s.member(&w.owner, "runner1", "runner").await;
    for (token, name) in [
        (&w.owner, "aiko"),
        (&w.owner, "alice"),
        (&w.aiko, "aiko"),
        (&w.aiko, "AIKO"),
        (&runner, "runner1"),
    ] {
        let v = s.ok(&format!("/members/{name}"), token).await;
        assert_eq!(
            v["name"].as_str().unwrap().to_lowercase(),
            name.to_lowercase()
        );
        assert!(v["accounts"].is_array(), "{v}");
    }
    assert_eq!(s.ok("/members/alice", &w.owner).await["kind"], "owner");
    // Another member, known or not, is the owner's to read.
    for name in ["ken", "alice", "nobody"] {
        let (st, _) = s.get(&format!("/members/{name}"), &w.aiko).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{name}");
    }
    let (st, v) = s.get("/members/nobody", &w.owner).await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    assert_eq!(v["error"]["code"], "not_found");
}

/// Puts `body` as `name`'s picture with `token`.
async fn put_avatar(s: &Server, token: &str, name: &str, body: Vec<u8>) -> (StatusCode, Value) {
    let resp = s
        .http
        .put(format!("{}/api/v1/members/{name}/avatar", s.url))
        .bearer_auth(token)
        .header("content-type", "application/octet-stream")
        .body(body)
        .send()
        .await
        .unwrap();
    let status = resp.status();
    (status, resp.json().await.unwrap_or(Value::Null))
}

#[tokio::test]
async fn a_member_has_one_picture_set_by_the_owner_or_itself() {
    let w = World::new().await;
    let s = &w.server;
    let png = b"\x89PNG\r\n\x1a\nrest of a png".to_vec();
    let gif = b"GIF89a and so on".to_vec();
    assert_eq!(
        s.ok("/members/aiko", &w.owner).await["avatar_url"],
        Value::Null
    );

    // The owner sets an agent's; its address names the picture's version.
    let (st, v) = put_avatar(s, &w.owner, "aiko", png.clone()).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let url = v["avatar_url"].as_str().unwrap().to_string();
    assert!(url.starts_with("/api/v1/members/aiko/avatar?v="), "{url}");
    let resp = s
        .http
        .get(format!("{}{url}", s.url))
        .bearer_auth(&w.ken)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(resp.headers()["content-type"], "image/png");
    assert_eq!(resp.bytes().await.unwrap().to_vec(), png);
    // The same picture again keeps the address; another changes it.
    let (_, again) = put_avatar(s, &w.owner, "aiko", png.clone()).await;
    assert_eq!(again["avatar_url"], url.as_str());
    let (st, v) = put_avatar(s, &w.aiko, "aiko", gif.clone()).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_ne!(v["avatar_url"], url.as_str());
    let resp = s
        .http
        .get(format!("{}/api/v1/members/aiko/avatar", s.url))
        .bearer_auth(&w.owner)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.headers()["content-type"], "image/gif");

    // Another member's is not an agent's to set; what is no picture, or too
    // large, is refused.
    let (st, _) = put_avatar(s, &w.aiko, "ken", png.clone()).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (st, v) = put_avatar(s, &w.owner, "aiko", b"<svg onload=alert(1)>".to_vec()).await;
    assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
    let mut big = png.clone();
    big.resize((4 << 20) + 1, 0);
    let (st, v) = put_avatar(s, &w.owner, "aiko", big).await;
    assert_eq!(st, StatusCode::PAYLOAD_TOO_LARGE, "{v}");
    assert_eq!(v["error"]["code"], "too_large");

    // Removed, it is gone.
    let (st, v) = s.delete("/members/aiko/avatar", &w.aiko).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["avatar_url"], Value::Null);
    let (st, _) = s.get("/members/aiko/avatar", &w.owner).await;
    assert_eq!(st, StatusCode::NOT_FOUND);
}

/// The labels of a member's tokens as listed.
async fn labels(s: &Server, token: &str, name: &str) -> Vec<String> {
    let list = s.ok(&format!("/members/{name}/tokens"), token).await;
    list["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["label"].as_str().unwrap().to_string())
        .collect()
}

#[tokio::test]
async fn replacing_a_label_leaves_one_token_of_it_and_reserved_labels_are_refused() {
    let w = World::new().await;
    let s = &w.server;
    let issue = |replace: bool| {
        s.post(
            "/members/ken/tokens",
            &w.owner,
            json!({"label": "desk", "replace": replace}),
        )
    };
    let (st, first) = issue(false).await;
    assert_eq!(st, StatusCode::CREATED);
    let (_, second) = issue(false).await;
    let (st, replaced) = issue(true).await;
    assert_eq!(st, StatusCode::CREATED, "{replaced}");
    assert_eq!(replaced["label"], "desk");
    let (_, again) = issue(true).await;
    let mut listed = labels(s, &w.owner, "ken").await;
    listed.sort();
    assert_eq!(listed, ["desk", "test"]);
    for old in [&first, &second, &replaced] {
        let (st, _) = s.get("/me", old["token"].as_str().unwrap()).await;
        assert_eq!(st, StatusCode::UNAUTHORIZED);
    }
    assert_eq!(
        s.get("/me", again["token"].as_str().unwrap()).await.0,
        StatusCode::OK
    );
    // Other labels are untouched.
    assert_eq!(s.get("/me", &w.ken).await.0, StatusCode::OK);
    // The labels Branvil issues itself, by anyone, with or without replace.
    for label in ["owner-file", "auto", "app:", "app:Some App"] {
        for (token, name) in [(&w.owner, "alice"), (&w.owner, "ken"), (&w.ken, "ken")] {
            for replace in [false, true] {
                let (st, v) = s
                    .post(
                        &format!("/members/{name}/tokens"),
                        token,
                        json!({"label": label, "replace": replace}),
                    )
                    .await;
                assert_eq!(st, StatusCode::BAD_REQUEST, "{label} {name}: {v}");
                assert_eq!(v["error"]["code"], "invalid");
            }
        }
    }
    assert_eq!(s.get("/me", &w.owner).await.0, StatusCode::OK);
    assert_eq!(labels(s, &w.owner, "alice").await, ["owner-file"]);
}

/// A PKCE verifier and its S256 challenge.
fn pkce(seed: &str) -> (String, String) {
    use base64::Engine;
    use sha2::Digest;
    let verifier = format!("{seed}-{}", "v".repeat(48));
    let challenge = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .encode(sha2::Sha256::digest(verifier.as_bytes()));
    (verifier, challenge)
}

const REDIRECT: &str = "http://127.0.0.1:53682/callback";

/// Allows `client` as the caller (`POST /apps/authorizations`); returns the
/// code from the redirect, after checking the redirect carries the state.
async fn allow(s: &Server, token: &str, client: &str, challenge: &str) -> String {
    let (st, v) = s
        .post(
            "/apps/authorizations",
            token,
            json!({"client": client, "redirect_uri": REDIRECT, "state": "st 1", "code_challenge": challenge}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let redirect = v["redirect"].as_str().unwrap();
    let query = redirect
        .strip_prefix(&format!("{REDIRECT}?code="))
        .unwrap_or_else(|| panic!("unexpected redirect {redirect}"));
    let (code, state) = query.split_once('&').unwrap();
    assert_eq!(state, "state=st+1");
    code.to_string()
}

/// `POST /apps/token` without authentication.
async fn exchange(s: &Server, code: &str, redirect: &str, verifier: &str) -> (StatusCode, Value) {
    let resp = s
        .http
        .post(format!("{}/api/v1/apps/token", s.url))
        .json(&json!({"code": code, "redirect_uri": redirect, "code_verifier": verifier}))
        .send()
        .await
        .unwrap();
    (resp.status(), resp.json().await.unwrap())
}

#[tokio::test]
async fn a_local_app_gets_an_owner_token_through_the_consent_exchange() {
    let w = World::new().await;
    let s = &w.server;
    let (verifier, challenge) = pkce("one");
    // The owner allows it, by Bearer or with the GUI session and its CSRF.
    let code = allow(s, &w.owner, "Desk App", &challenge).await;
    let (st, v) = exchange(s, &code, REDIRECT, &verifier).await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    assert_eq!(
        (&v["label"], &v["member"]),
        (&json!("app:Desk App"), &json!("alice"))
    );
    let first = v["token"].as_str().unwrap().to_string();
    // The token is the owner's and listed with its label.
    assert_eq!(s.ok("/me", &first).await["kind"], "owner");
    assert_eq!(s.get("/members", &first).await.0, StatusCode::OK);
    let mut listed = labels(s, &w.owner, "alice").await;
    listed.sort();
    assert_eq!(listed, ["app:Desk App", "owner-file"]);
    // The code works once.
    let (st, v) = exchange(s, &code, REDIRECT, &verifier).await;
    assert_eq!(
        (st, &v["error"]["code"]),
        (StatusCode::BAD_REQUEST, &json!("invalid"))
    );

    // A second consent replaces the app's token; the session path works too.
    let (cookie, csrf) = s.session(&w.owner).await;
    let resp = s
        .http
        .post(format!("{}/api/v1/apps/authorizations", s.url))
        .header("cookie", &cookie)
        .header("x-branvil-csrf", &csrf)
        .json(&json!({"client": "Desk App", "redirect_uri": REDIRECT, "state": "s", "code_challenge": challenge}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let body: Value = resp.json().await.unwrap();
    let code = body["redirect"].as_str().unwrap()[REDIRECT.len() + 6..]
        .split('&')
        .next()
        .unwrap()
        .to_string();
    let (st, v) = exchange(s, &code, REDIRECT, &verifier).await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let second = v["token"].as_str().unwrap().to_string();
    assert_eq!(s.get("/me", &first).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(s.ok("/me", &second).await["name"], "alice");
    let mut listed = labels(s, &w.owner, "alice").await;
    listed.sort();
    assert_eq!(listed, ["app:Desk App", "owner-file"]);
    // Revoked like any token.
    let tokens = s.ok("/members/alice/tokens", &w.owner).await;
    let id = tokens["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["label"] == "app:Desk App")
        .unwrap()["id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(
        s.delete(&format!("/members/alice/tokens/{id}"), &w.owner)
            .await
            .0,
        StatusCode::NO_CONTENT
    );
    assert_eq!(s.get("/me", &second).await.0, StatusCode::UNAUTHORIZED);

    // Neither the codes nor the tokens are written anywhere: not in the
    // service log (debug level here) nor in the database.
    let log = std::fs::read_to_string(w.dir.path().join("serve.log")).unwrap();
    let db = std::fs::read(s.data.join("branvil.redb")).unwrap();
    // `bvt_<id>_<secret>`: the secret itself may contain `_`.
    for secret in [
        code.as_str(),
        first.splitn(3, '_').nth(2).unwrap(),
        second.splitn(3, '_').nth(2).unwrap(),
    ] {
        assert!(!log.contains(secret), "a secret in the service log");
        assert!(!db.windows(secret.len()).any(|win| win == secret.as_bytes()));
    }
}

#[tokio::test]
async fn the_consent_exchange_refuses_what_does_not_match_the_code() {
    let w = World::new().await;
    let s = &w.server;
    let (verifier, challenge) = pkce("two");
    // Only the owner allows an app; a runner or agent cannot.
    let runner = s.member(&w.owner, "runner1", "runner").await;
    for token in [&w.aiko, &runner] {
        let (st, _) = s
            .post(
                "/apps/authorizations",
                token,
                json!({"client": "x", "redirect_uri": REDIRECT, "state": "s", "code_challenge": challenge}),
            )
            .await;
        assert_eq!(st, StatusCode::FORBIDDEN);
    }
    // Requests that are not a loopback app's are refused before a code exists.
    for (field, value) in [
        ("redirect_uri", "https://127.0.0.1/cb"),
        ("redirect_uri", "http://attacker.example/cb"),
        ("redirect_uri", "http://localhost.attacker.example/cb"),
        ("redirect_uri", "http://me@127.0.0.1/cb"),
        ("redirect_uri", "http://127.0.0.1/cb#x"),
        ("client", ""),
        ("client", "a/b"),
        ("state", ""),
        ("code_challenge", "plain-text"),
    ] {
        let mut body = json!({"client": "x", "redirect_uri": REDIRECT, "state": "s", "code_challenge": challenge});
        body[field] = json!(value);
        let (st, v) = s.post("/apps/authorizations", &w.owner, body).await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{field}={value}: {v}");
    }
    // A wrong redirect_uri or verifier spends the code.
    let other_redirect = "http://127.0.0.1:53683/callback";
    let (other_verifier, _) = pkce("three");
    for (redirect, verifier_sent) in [(other_redirect, &verifier), (REDIRECT, &other_verifier)] {
        let code = allow(s, &w.owner, "x", &challenge).await;
        let (st, v) = exchange(s, &code, redirect, verifier_sent).await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
        assert!(!v.to_string().contains(&code));
        let (st, _) = exchange(s, &code, REDIRECT, &verifier).await;
        assert_eq!(st, StatusCode::BAD_REQUEST);
    }
    let (st, _) = exchange(s, "guess", REDIRECT, &verifier).await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    // A cross-origin exchange is refused before the code is used.
    let code = allow(s, &w.owner, "x", &challenge).await;
    let resp = s
        .http
        .post(format!("{}/api/v1/apps/token", s.url))
        .header("origin", "http://attacker.example")
        .json(&json!({"code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    // An Idempotency-Key does not make the service keep a code or a token.
    let send = |path: &str, body: Value| {
        s.http
            .post(format!("{}/api/v1{path}", s.url))
            .bearer_auth(&w.owner)
            .header("Idempotency-Key", "app-1")
            .json(&body)
            .send()
    };
    let authorization =
        json!({"client": "x", "redirect_uri": REDIRECT, "state": "s", "code_challenge": challenge});
    let a: Value = send("/apps/authorizations", authorization.clone())
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let b: Value = send("/apps/authorizations", authorization)
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_ne!(a["redirect"], b["redirect"]);
    let resp = send(
        "/apps/token",
        json!({"code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}),
    )
    .await
    .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    // Only the last exchange issued a token of the label.
    assert_eq!(
        labels(s, &w.owner, "alice")
            .await
            .iter()
            .filter(|l| *l == "app:x")
            .count(),
        1
    );
}
