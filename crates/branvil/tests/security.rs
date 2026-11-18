//! Security properties claimed in `docs/security.md` that other tests do not
//! already cover: token revocation (API and Git), the host's owner session
//! and token, CSRF on cookie writes, what each member kind may do, owner-only
//! endpoints, replacing a label's tokens and the reserved label, and hook
//! callbacks.

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

/// The host authenticates the owner: its browser session (with its CSRF
/// value on writes) and its owner token. Branvil keeps no session of its own.
#[tokio::test]
async fn the_hosts_session_and_owner_token_identify_the_owner() {
    let w = World::new().await;
    let s = &w.server;
    let (cookie, csrf) = s.session();
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
    assert_eq!(
        (
            &session["me"]["name"],
            &session["me"]["kind"],
            &session["csrf"]
        ),
        (&json!("alice"), &json!("owner"), &json!(csrf))
    );
    assert_eq!(session["host_navigation"]["url"], "/-/guildbotics/");
    assert_eq!(
        s.get("/session", "unknown").await.0,
        StatusCode::UNAUTHORIZED
    );
    // There is no entry of Branvil's own.
    for path in ["/session", "/session/links"] {
        let (st, _) = s.post(path, &w.owner, json!({"code": "x"})).await;
        assert!(
            matches!(st, StatusCode::NOT_FOUND | StatusCode::METHOD_NOT_ALLOWED),
            "{path}: {st}"
        );
    }
    // Only the owner's token is the host's; an agent's token stays its own.
    assert_eq!(s.ok("/me", &w.owner).await["name"], "alice");
    assert_eq!(s.ok("/me", &w.aiko).await["name"], "aiko");
    let url = format!("{}/api/v1/repos/{}/tickets", s.url, encode(REPO));
    for sent in ["", "wrong"] {
        let mut req = s
            .http
            .post(&url)
            .header("cookie", &cookie)
            .json(&json!({"title": "x"}));
        if !sent.is_empty() {
            req = req.header("x-guildbotics-csrf", sent);
        }
        let response = req.send().await.unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response.json::<Value>().await.unwrap()["error"]["code"],
            "csrf_mismatch"
        );
    }
    let created = s
        .http
        .post(&url)
        .header("cookie", &cookie)
        .header("x-guildbotics-csrf", &csrf)
        .json(&json!({"title": "from the browser"}))
        .send()
        .await
        .unwrap();
    assert_eq!(created.status(), StatusCode::CREATED);
    let ticket: Value = created.json().await.unwrap();
    assert_eq!(ticket["author"], "alice");
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
    let (cookie, _) = s.session();
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
    let mode = std::fs::metadata(&w.server.data)
        .unwrap()
        .permissions()
        .mode()
        & 0o777;
    assert_eq!(mode, 0o700);
    // The cursor key is made per process, and the owner has no token file.
    assert!(!w.server.data.join("secrets").exists());
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
        ("post-receive", json!({"repo": "x", "member": "aiko"})),
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
    // The label Branvil issues itself, by anyone, with or without replace.
    for label in ["auto"] {
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
    assert_eq!(labels(s, &w.owner, "alice").await, ["git"]);
}

/// The classified catalogs are the only way in: a router built with
/// `.route()` would escape the classification tested below.
#[test]
fn routers_register_routes_only_through_the_classified_catalogs() {
    for (file, source, direct_routes) in [
        // Git's body limit; the path comes from GIT_ROUTE.
        ("http.rs", include_str!("../src/http.rs"), 1),
        ("ci/api.rs", include_str!("../src/ci/api.rs"), 0),
        ("ci/admin.rs", include_str!("../src/ci/admin.rs"), 0),
        (
            "upstream/http.rs",
            include_str!("../src/upstream/http.rs"),
            0,
        ),
        ("autoland.rs", include_str!("../src/autoland.rs"), 0),
        ("landing.rs", include_str!("../src/landing.rs"), 0),
    ] {
        let production = source.split("#[cfg(test)]\nmod tests").next().unwrap();
        assert_eq!(
            production.matches(".route(").count(),
            direct_routes,
            "{file}: register the route in the classified catalog"
        );
    }
}

/// All registered methods, not a hand-picked sample of the API. The host
/// checks Host and Origin (gb-server's classification tests).
#[tokio::test]
async fn classified_routes_enforce_credentials() {
    use loopback_auth::RouteAuth;
    let w = World::new().await;
    let s = &w.server;
    let (cookie, _) = s.session();
    let mut seen = std::collections::BTreeSet::new();
    for (method, path) in [
        (
            reqwest::Method::GET,
            "/git/acme/demo.git/info/refs?service=git-upload-pack",
        ),
        (reqwest::Method::POST, "/git/acme/demo.git/git-upload-pack"),
    ] {
        for credential in [None, Some("Basic invalid-test-value")] {
            let mut request = s.http.request(method.clone(), format!("{}{path}", s.url));
            if let Some(value) = credential {
                request = request.header("authorization", value);
            }
            let response = request.send().await.unwrap();
            assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
            assert!(
                response.headers()["www-authenticate"]
                    .to_str()
                    .unwrap()
                    .starts_with("Basic ")
            );
        }
    }
    for (path, route) in branvil::route_inventory() {
        assert!(
            seen.insert((route.method, path.clone())),
            "duplicate {route:?}"
        );
        let path = if route.auth == RouteAuth::Basic {
            "/git/acme/demo.git/info/refs?service=git-upload-pack".to_owned()
        } else {
            path.split('/')
                .map(|part| if part.starts_with('{') { "1" } else { part })
                .collect::<Vec<_>>()
                .join("/")
        };
        let methods: &[&str] = match route.method {
            "any" => &[
                "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT",
            ],
            "get" => &["GET", "HEAD"],
            method => &[method],
        };
        for method in methods {
            let method = reqwest::Method::from_bytes(method.to_uppercase().as_bytes()).unwrap();
            let request = || {
                s.http
                    .request(method.clone(), format!("{}{path}", s.url))
                    .json(&json!({"repo":"missing", "member":"missing", "updates":[]}))
            };
            if route.auth == RouteAuth::HookSecret {
                let response = request().send().await.unwrap();
                assert_eq!(response.status(), StatusCode::FORBIDDEN, "{route:?}");
                if method != reqwest::Method::HEAD {
                    assert_eq!(
                        response.json::<Value>().await.unwrap()["error"]["message"],
                        "Bad hook secret."
                    );
                }
                continue;
            }
            if matches!(
                route.auth,
                RouteAuth::OwnerSession | RouteAuth::MemberSession
            ) && method != reqwest::Method::GET
                && method != reqwest::Method::HEAD
            {
                let response = request().header("cookie", &cookie).send().await.unwrap();
                assert_eq!(response.status(), StatusCode::FORBIDDEN, "{method} {path}");
                assert_eq!(
                    response.json::<Value>().await.unwrap()["error"]["code"],
                    "csrf_mismatch"
                );
            }
            if matches!(
                route.auth,
                RouteAuth::OwnerSession | RouteAuth::MemberSession | RouteAuth::Session
            ) {
                for unknown in [false, true] {
                    let mut req = request();
                    if unknown {
                        req = req.bearer_auth("unknown-test-token");
                    }
                    let response = req.send().await.unwrap();
                    assert_eq!(
                        response.status(),
                        StatusCode::UNAUTHORIZED,
                        "{method} {path}"
                    );
                }
            }
        }
    }
}
