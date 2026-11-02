//! The upstream host against a real service and a GitHub stand-in
//! (`common::github`): accounts, following the default branch, publishing a
//! pull request as its author, carrying changes both ways, the host's checks
//! and logs, the merge on the host, and conditional requests.

use crate::ci::eventually;
use crate::common;

use common::github::{AppKey, FakeGitHub, Refusal};
use common::*;
use reqwest::StatusCode;
use serde_json::{Value, json};

pub(crate) const GH: &str = "octo/demo";
const OWNER_TOKEN: &str = "gh-owner-4c1d";
const AIKO_TOKEN: &str = "gh-aiko-9e2b";

/// A world whose `acme/demo` follows `octo/demo` on the stand-in, with the
/// owner's account set.
pub(crate) async fn followed() -> (World, FakeGitHub) {
    let mut w = World::new().await;
    let gh = FakeGitHub::start(&w.dir.path().join("github")).await;
    gh.repository(GH, &w.work);
    gh.account(OWNER_TOKEN, "alice-gh");
    gh.account(AIKO_TOKEN, "aiko-bot");
    gh.configure(&w.server.data);
    w.restart(&[]);
    let s = &w.server;
    let (st, v) = s
        .put(
            "/members/alice/accounts/github",
            &w.owner,
            json!({"token": OWNER_TOKEN}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let (st, v) = s
        .put(
            &format!("{}/upstream", w.repo_path()),
            &w.owner,
            json!({"provider": "github", "repository": GH}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    (w, gh)
}

pub(crate) async fn refresh(w: &World) -> Value {
    let (st, v) = w
        .server
        .post(
            &format!("{}/upstream/refresh", w.repo_path()),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    v
}

pub(crate) async fn publish(w: &World, number: u64) -> (StatusCode, Value) {
    let pull = format!("{}/pulls/{number}", w.repo_path());
    let p = w.server.ok(&pull, &w.owner).await;
    w.server
        .post(
            &format!("{pull}/publish"),
            &w.owner,
            json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
        )
        .await
}

async fn resume_publish(w: &World, number: u64) -> Value {
    let pull = format!("{}/pulls/{number}", w.repo_path());
    let p = w.server.ok(&pull, &w.owner).await;
    let (st, v) = w
        .server
        .post(
            &format!("{pull}/landing/resume"),
            &w.owner,
            json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    v
}

fn branvil_tip(w: &World, branch: &str) -> String {
    let dir = w.server.data.join("repos/acme/demo.git");
    git_ok(
        &dir,
        &[
            "--git-dir",
            dir.to_str().unwrap(),
            "rev-parse",
            &format!("refs/heads/{branch}"),
        ],
    )
}

/// Adds a commit to `branch` of the stand-in's repository (someone working on
/// GitHub directly).
pub(crate) fn commit_on_github(gh: &FakeGitHub, branch: &str, message: &str) -> String {
    let dir = gh.dir(GH);
    let d = dir.to_str().unwrap();
    let parent = gh.tip(GH, branch).unwrap();
    let tree = git_ok(
        &dir,
        &["--git-dir", d, "rev-parse", &format!("{parent}^{{tree}}")],
    );
    let sha = git_ok(
        &dir,
        &[
            "--git-dir",
            d,
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@x",
            "commit-tree",
            &tree,
            "-p",
            &parent,
            "-m",
            message,
        ],
    );
    git_ok(
        &dir,
        &[
            "--git-dir",
            d,
            "update-ref",
            &format!("refs/heads/{branch}"),
            &sha,
        ],
    );
    sha
}

#[tokio::test(flavor = "multi_thread")]
async fn accounts_are_checked_with_the_host_and_their_tokens_never_returned() {
    let (w, _gh) = followed().await;
    let s = &w.server;
    let account = "/members/aiko/accounts/github";
    // The host refuses the token: nothing is kept.
    let (st, v) = s.put(account, &w.owner, json!({"token": "wrong"})).await;
    assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
    // Only the owner sets accounts.
    let (st, _) = s.put(account, &w.aiko, json!({"token": AIKO_TOKEN})).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (st, v) = s.put(account, &w.owner, json!({"token": AIKO_TOKEN})).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["accounts"][0]["provider"], "github");
    assert_eq!(v["accounts"][0]["login"], "aiko-bot");
    assert_eq!(v["accounts"][0]["kind"], "token");
    let members = s.ok("/members?limit=100", &w.owner).await.to_string();
    assert!(!members.contains(AIKO_TOKEN) && !members.contains(OWNER_TOKEN));
    let file = s.data.join("secrets/accounts/aiko.github.json");
    assert_eq!(
        serde_json::from_slice::<Value>(&std::fs::read(&file).unwrap()).unwrap(),
        json!({"token": AIKO_TOKEN})
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    let (st, _) = s.delete("/members/aiko/accounts/../x", &w.owner).await;
    assert_ne!(st, StatusCode::OK);
    let (st, v) = s.delete("/members/aiko/accounts/gitlab", &w.owner).await;
    assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
    let (st, v) = s.delete(account, &w.owner).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert!(v["accounts"].as_array().unwrap().is_empty());
    assert!(!file.exists());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_repository_follows_its_upstream_and_its_default_branch_changes_only_there() {
    let mut w = World::new().await;
    let gh = FakeGitHub::start(&w.dir.path().join("github")).await;
    gh.repository(GH, &w.work);
    gh.account(OWNER_TOKEN, "alice-gh");
    gh.configure(&w.server.data);
    w.restart(&[]);
    let s = &w.server;
    let upstream = format!("{}/upstream", w.repo_path());
    let setting = json!({"provider": "github", "repository": GH});
    // The owner's account reads the upstream, so it comes first.
    let (st, v) = s.put(&upstream, &w.owner, setting.clone()).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "no_account");
    let (st, _) = s
        .put(
            "/members/alice/accounts/github",
            &w.owner,
            json!({"token": OWNER_TOKEN}),
        )
        .await;
    assert_eq!(st, StatusCode::OK);
    let (st, v) = s
        .put(
            &upstream,
            &w.owner,
            json!({"provider": "github", "repository": "octo/x/y"}),
        )
        .await;
    assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
    let (st, v) = s.put(&upstream, &w.owner, setting).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["upstream"]["host"], "GitHub");
    assert_eq!(v["upstream"]["url"], "https://github.test/octo/demo");
    assert_eq!(
        v["upstream"]["following"]["tip"],
        branvil_tip(&w, "main").as_str()
    );

    // Nobody pushes main any more, not even the owner; merges go upstream.
    std::fs::write(w.work.join("README.md"), "direct\n").unwrap();
    git_ok(&w.work, &["commit", "-qam", "direct"]);
    let out = git_as(&w.work, "alice", &w.owner, &["push", "origin", "main"]);
    assert!(!out.status.success());
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("follows the upstream"),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    git_ok(&w.work, &["reset", "-q", "--hard", "HEAD~1"]);
    let repo = s.ok(&w.repo_path(), &w.owner).await;
    let (st, v) = s
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "default_branch": "trunk"}),
        )
        .await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    w.push_change("feature", "f.txt", "x\n");
    let p = w.open_pull("feature", json!([])).await;
    let pull = format!("{}/pulls/{}", w.repo_path(), p["number"]);
    let (st, v) = s
        .post(
            &format!("{pull}/merge"),
            &w.owner,
            json!({"expected_head": p["head_sha"], "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert!(
        v["error"]["message"].as_str().unwrap().contains("publish"),
        "{v}"
    );

    // GitHub's main moves (another person's merge): Branvil follows.
    let theirs = commit_on_github(&gh, "main", "merged elsewhere");
    // Polls of one repository take turns ("Check now" beside the poller).
    let polls: Vec<_> = (0..4)
        .map(|_| {
            let (http, url, token) = (
                s.http.clone(),
                format!("{}/api/v1{}/upstream/refresh", s.url, w.repo_path()),
                w.owner.clone(),
            );
            tokio::spawn(async move {
                let r = http.post(url).bearer_auth(token).send().await.unwrap();
                (r.status(), r.json::<Value>().await.unwrap())
            })
        })
        .collect();
    for poll in polls {
        let (st, v) = poll.await.unwrap();
        assert_eq!(st, StatusCode::OK, "{v}");
        assert_eq!(v["upstream"]["following"]["error"], Value::Null, "{v}");
    }
    let v = refresh(&w).await;
    assert_eq!(v["upstream"]["following"]["tip"], theirs.as_str());
    assert_eq!(branvil_tip(&w, "main"), theirs);
    assert!(v["upstream"]["following"]["followed_at"].is_string());

    // GitHub's main is rewritten: following stops, nothing is overwritten.
    let dir = gh.dir(GH);
    let d = dir.to_str().unwrap();
    let tree = git_ok(&dir, &["--git-dir", d, "rev-parse", "main^{tree}"]);
    let unrelated = git_ok(
        &dir,
        &[
            "--git-dir",
            d,
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@x",
            "commit-tree",
            &tree,
            "-m",
            "rewritten",
        ],
    );
    git_ok(
        &dir,
        &["--git-dir", d, "update-ref", "refs/heads/main", &unrelated],
    );
    let v = refresh(&w).await;
    assert!(
        v["upstream"]["following"]["stopped"]
            .as_str()
            .unwrap_or_default()
            .contains("following stopped"),
        "{v}"
    );
    assert_eq!(branvil_tip(&w, "main"), theirs);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pull_request_is_published_as_its_author_and_follows_the_host_to_the_merge() {
    let (w, gh) = followed().await;
    let s = &w.server;
    // A ticket that stands for a GitHub issue, closed by the pull request.
    let (st, t) = s
        .post(
            &format!("{}/tickets", w.repo_path()),
            &w.aiko,
            json!({"title": "From GitHub", "upstream_issue": "https://github.test/octo/demo/issues/7"}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{t}");
    assert_eq!(
        t["upstream_issue"],
        "https://github.test/octo/demo/issues/7"
    );
    let (st, v) = s
        .post(
            &format!("{}/tickets", w.repo_path()),
            &w.aiko,
            json!({"title": "x", "upstream_issue": "https://example.test/octo/demo/issues/7"}),
        )
        .await;
    assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
    let local = w.ticket("Only here").await;
    let head = w.push_change("ticket/1", "f.txt", "one\n");
    let p = w
        .open_pull(
            "ticket/1",
            json!([{"url": t["url"], "closes": true}, {"url": local["url"], "closes": false}]),
        )
        .await;
    let n = p["number"].as_u64().unwrap();
    let pull = format!("{}/pulls/{n}", w.repo_path());

    // The preview shows exactly what will be posted, and as whom.
    let preview = s.ok(&format!("{pull}/publish"), &w.owner).await;
    assert_eq!(preview["host"], "GitHub");
    assert_eq!(preview["title"], "PR ticket/1");
    assert_eq!(preview["body"], "body\n\nCloses #7");
    assert_eq!(preview["account"], "aiko");
    assert_eq!(preview["login"], Value::Null);
    // The author has no GitHub account yet.
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "no_account");
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("has no GitHub account"),
        "{v}"
    );
    assert_eq!(gh.tip(GH, "ticket/1"), None);
    let (st, _) = s
        .put(
            "/members/aiko/accounts/github",
            &w.owner,
            json!({"token": AIKO_TOKEN}),
        )
        .await;
    assert_eq!(st, StatusCode::OK);
    // Only the owner publishes.
    let (st, _) = s
        .post(
            &format!("{pull}/publish"),
            &w.aiko,
            json!({"expected_head": head, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    // An approval the repository asks for is missing, then given.
    let repo = s.ok(&w.repo_path(), &w.owner).await;
    let (st, v) = s
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "required_approvals": 1}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "not_mergeable");
    assert!(
        v["error"]["message"].as_str().unwrap().contains("0 / 1"),
        "{v}"
    );
    assert_eq!(gh.tip(GH, "ticket/1"), None);
    let (st, v) = s
        .post(
            &format!("{pull}/reviews"),
            &w.ken,
            json!({"verdict": "approve", "commit": head}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let number = v["upstream"]["number"].as_u64().unwrap();
    assert_eq!(v["upstream"]["account"], "aiko");
    assert_eq!(v["upstream"]["state"], "open");
    assert_eq!(gh.tip(GH, "ticket/1").as_deref(), Some(head.as_str()));
    let hp = gh.pull(number);
    assert_eq!((hp.author.as_str(), hp.base.as_str()), ("aiko-bot", "main"));
    assert_eq!(
        (hp.title.as_str(), hp.body.as_str()),
        ("PR ticket/1", "body\n\nCloses #7")
    );
    // Publishing again changes nothing.
    let (st, _) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(gh.requests("POST", "/pulls").len(), 1);

    // GitHub's checks of the head appear among Branvil's, with their logs.
    gh.check(
        &head,
        41,
        "test-windows (remainder)",
        "failure",
        "line 1\nboom on Windows\n",
    );
    refresh(&w).await;
    let checks = s.ok(&format!("{pull}/checks"), &w.aiko).await;
    let windows = checks["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "github/test-windows (remainder)")
        .cloned()
        .unwrap_or_else(|| panic!("{checks}"));
    assert_eq!(windows["conclusion"], "failure");
    assert_eq!(windows["attempt_id"], "github-41");
    assert_eq!(windows["details_url"], "https://github.test/runs/41");
    let log = s
        .ok(
            &format!("{}/jobs/github-41/log?tail_bytes=16", w.repo_path()),
            &w.aiko,
        )
        .await;
    assert_eq!(log["log"], "boom on Windows\n");
    assert_eq!(log["truncated"], true);
    assert_eq!(log["log_bytes"], "line 1\nboom on Windows\n".len());
    // Read as the reader only; a refusal or a missing credential is said.
    let log_path = format!("{}/jobs/github-41/log", w.repo_path());
    gh.set(|st| {
        st.refusal = Some(Refusal {
            status: 403,
            headers: vec![],
            message: "Resource not accessible by integration",
        })
    });
    let (st, v) = s.get(&log_path, &w.aiko).await;
    assert_eq!(
        (st, &v["error"]["code"]),
        (StatusCode::CONFLICT, &json!("upstream_refused"))
    );
    gh.set(|st| st.refusal = None);
    let file = s.data.join("secrets/accounts/alice.github.json");
    let kept = std::fs::read(&file).unwrap();
    std::fs::remove_file(&file).unwrap();
    let (st, v) = s.get(&log_path, &w.aiko).await;
    assert_eq!(
        (st, &v["error"]["code"]),
        (StatusCode::CONFLICT, &json!("no_account"))
    );
    std::fs::write(&file, kept).unwrap();
    assert!(
        gh.requests("GET", "/logs")
            .iter()
            .all(|(_, l)| l == "alice-gh")
    );

    // A fix pushed in Branvil and an edited body reach GitHub as aiko.
    let fixed = w.push_change("ticket/1", "f.txt", "fixed\n");
    let (st, _) = s
        .patch(&pull, &w.aiko, json!({"body": "body, fixed"}))
        .await;
    assert_eq!(st, StatusCode::OK);
    refresh(&w).await;
    assert_eq!(gh.tip(GH, "ticket/1").as_deref(), Some(fixed.as_str()));
    assert_eq!(gh.pull(number).body, "body, fixed\n\nCloses #7");
    assert!(
        gh.requests("PATCH", &format!("/pulls/{number}"))
            .iter()
            .all(|(_, login)| login == "aiko-bot")
    );
    let checks = s.ok(&format!("{pull}/checks"), &w.aiko).await;
    assert!(
        !checks.to_string().contains("github/test-windows"),
        "the old head's checks are not the new head's: {checks}"
    );

    // Closed on GitHub → closed here; reopened here → reopened on GitHub
    // (closed on both sides, it is no longer polled: Branvil reopens it).
    gh.set_open(number, false);
    refresh(&w).await;
    assert_eq!(s.ok(&pull, &w.aiko).await["state"], "closed");
    let (st, _) = s.post(&format!("{pull}/reopen"), &w.owner, json!({})).await;
    assert_eq!(st, StatusCode::OK);
    refresh(&w).await;
    assert!(gh.pull(number).open);
    // Closed here → closed on GitHub, and back.
    let (st, _) = s.post(&format!("{pull}/close"), &w.owner, json!({})).await;
    assert_eq!(st, StatusCode::OK);
    refresh(&w).await;
    assert!(!gh.pull(number).open);
    let (st, _) = s.post(&format!("{pull}/reopen"), &w.owner, json!({})).await;
    assert_eq!(st, StatusCode::OK);
    refresh(&w).await;
    assert!(gh.pull(number).open);

    // Someone else's change lands on GitHub's main first.
    git_ok(&w.work, &["checkout", "-q", "main"]);
    std::fs::write(w.work.join("other.txt"), "other\n").unwrap();
    git_ok(&w.work, &["add", "other.txt"]);
    git_ok(&w.work, &["commit", "-qm", "other"]);
    git_ok(
        &w.work,
        &["push", "-q", gh.dir(GH).to_str().unwrap(), "main"],
    );

    // A person merges on GitHub: Branvil's main follows, the pull request is
    // merged with GitHub's commit, and the ticket it closes is closed.
    let merged = gh.squash_merge(GH, number);
    refresh(&w).await;
    assert_eq!(branvil_tip(&w, "main"), merged);
    let got = s.ok(&pull, &w.aiko).await;
    assert_eq!(got["merged"], true, "{got}");
    assert_eq!(got["merge_sha"], merged.as_str());
    assert_eq!(got["upstream"]["state"], "merged");
    // Its branch is deleted here, not on GitHub; it keeps the head it pushed.
    let (st, _) = s
        .get(
            &format!("{}/branches/{}", w.repo_path(), encode("ticket/1")),
            &w.aiko,
        )
        .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    assert!(gh.tip(GH, "ticket/1").is_some());
    assert_eq!(got["head_sha"], got["upstream"]["pushed"]);
    // Its diff is its own change, not the other one merged meanwhile.
    let files = s.ok(&format!("{pull}/files"), &w.aiko).await;
    let paths: Vec<&str> = files["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["path"].as_str().unwrap())
        .collect();
    assert_eq!(paths, ["f.txt"]);
    // The squash left that head on no branch; a backup still has it.
    let into = tempfile::tempdir().unwrap();
    let r = w.restored(into.path()).await;
    assert_eq!(r.ok(&pull, &w.aiko).await["head_sha"], got["head_sha"]);
    assert_eq!(r.ok(&format!("{pull}/files"), &w.aiko).await, files);
    let ticket = s.ok(&format!("{}/tickets/1", w.repo_path()), &w.aiko).await;
    assert_eq!(ticket["state"], "closed");
    let other = s.ok(&format!("{}/tickets/2", w.repo_path()), &w.aiko).await;
    assert_eq!(other["state"], "open");

    // The tokens are only in their own files: not in the database, not in the
    // service's log (which the scan reads: the login kept beside them is there).
    let db = std::fs::read(s.data.join("branvil.redb")).unwrap();
    assert!(db.windows(8).any(|x| x == b"aiko-bot"));
    for file in [s.data.join("branvil.redb"), w.dir.path().join("serve.log")] {
        let bytes = std::fs::read(&file).unwrap();
        for token in [OWNER_TOKEN, AIKO_TOKEN] {
            assert!(
                !bytes.windows(token.len()).any(|x| x == token.as_bytes()),
                "{} contains a token",
                file.display()
            );
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_commit_added_on_the_host_stops_sending_until_published_again() {
    let (w, gh) = followed().await;
    let s = &w.server;
    s.put(
        "/members/aiko/accounts/github",
        &w.owner,
        json!({"token": AIKO_TOKEN}),
    )
    .await;
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let pull = format!("{}/pulls/{n}", w.repo_path());
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let theirs = commit_on_github(&gh, "feature", "a suggestion applied on GitHub");
    let ours = w.push_change("feature", "f.txt", "two\n");
    refresh(&w).await;
    let got = s.ok(&pull, &w.owner).await;
    assert!(
        got["upstream"]["stopped"]
            .as_str()
            .unwrap_or_default()
            .contains("refused"),
        "{got}"
    );
    assert_eq!(gh.tip(GH, "feature").as_deref(), Some(theirs.as_str()));
    eventually("the stopped publication leaves the queue", || async {
        s.ok(&pull, &w.owner).await["landing"]["state"] == "left"
    })
    .await;
    let stopped = s.ok(&pull, &w.owner).await;
    let resume = format!("{pull}/landing/resume");
    let input = json!({"expected_head": ours, "expected_base": stopped["base_sha"]});
    let (st, v) = s.post(&resume, &w.aiko, input.clone()).await;
    assert_eq!(st, StatusCode::FORBIDDEN, "{v}");
    for (field, code) in [
        ("expected_head", "head_moved"),
        ("expected_base", "base_moved"),
    ] {
        let mut stale = input.clone();
        stale[field] = json!("0".repeat(40));
        let (st, v) = s.post(&resume, &w.owner, stale).await;
        assert_eq!(st, StatusCode::CONFLICT, "{v}");
        assert_eq!(v["error"]["code"], code);
    }
    let unchanged = s.ok(&pull, &w.owner).await;
    assert_eq!(unchanged["landing"], stopped["landing"]);
    assert_eq!(
        unchanged["upstream"]["stopped"],
        stopped["upstream"]["stopped"]
    );

    // Another pull request now owns the base. Resuming rejoins behind it,
    // and cannot replace the host branch until its turn and gate pass.
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("next", "n.txt", "next\n");
    let next = w.open_pull("next", json!([])).await;
    let next_path = format!("{}/pulls/{}", w.repo_path(), next["number"]);
    eventually("the next pull request passes its gate", || async {
        s.ok(&next_path, &w.owner).await["landing"]["state"] == "passed"
    })
    .await;
    let resumed = resume_publish(&w, n).await;
    assert_eq!(resumed["landing"]["state"], "waiting", "{resumed}");
    assert_eq!(resumed["landing"]["position"], 2, "{resumed}");
    assert_eq!(
        resumed["upstream"]["stopped"],
        stopped["upstream"]["stopped"]
    );
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "not_mergeable");
    assert_eq!(gh.tip(GH, "feature").as_deref(), Some(theirs.as_str()));
    let (st, v) = s
        .post(&format!("{next_path}/close"), &w.owner, json!({}))
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    // The owner's explicit replacement now passes the same gate as any publish.
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["upstream"]["stopped"], Value::Null);
    assert_eq!(gh.tip(GH, "feature").as_deref(), Some(ours.as_str()));
    assert_eq!(gh.requests("POST", "/pulls").len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn unchanged_resources_are_read_with_conditional_requests() {
    let (w, gh) = followed().await;
    w.server
        .put(
            "/members/aiko/accounts/github",
            &w.owner,
            json!({"token": AIKO_TOKEN}),
        )
        .await;
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let (st, v) = publish(&w, p["number"].as_u64().unwrap()).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    refresh(&w).await;
    let before = gh.not_modified();
    let reads = gh.requests("GET", "/repos/").len();
    refresh(&w).await;
    let polled = gh.requests("GET", "/repos/").len() - reads;
    assert!(polled >= 3, "main, the pull request, its checks: {polled}");
    assert_eq!(
        gh.not_modified() - before,
        polled,
        "every read of an unchanged resource was a 304"
    );
    // The poll reads with the owner's account.
    assert!(
        gh.requests("GET", "/repos/")[reads..]
            .iter()
            .all(|(_, login)| login == "alice-gh")
    );
}

pub(crate) async fn with_aiko_account(w: &World) {
    let (st, v) = w
        .server
        .put(
            "/members/aiko/accounts/github",
            &w.owner,
            json!({"token": AIKO_TOKEN}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
}

/// Sets the stand-in's `branch` of `octo/demo` to `sha` (a branch made there
/// directly).
pub(crate) fn branch_on_github(gh: &FakeGitHub, branch: &str, sha: &str) {
    let dir = gh.dir(GH);
    let d = dir.to_str().unwrap();
    git_ok(
        &dir,
        &[
            "--git-dir",
            d,
            "update-ref",
            &format!("refs/heads/{branch}"),
            sha,
        ],
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_first_publish_replaces_only_what_branvil_pushed_and_survives_a_lost_answer() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let s = &w.server;
    let main = gh.tip(GH, "main").unwrap();

    // GitHub already has a branch of that name that Branvil did not push (a
    // person's, or an older pull request's): nothing is overwritten.
    let ours = w.push_change("feat", "f.txt", "one\n");
    let p = w.open_pull("feat", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let theirs = commit_on_github(&gh, "main", "someone's work");
    branch_on_github(&gh, "feat", &theirs);
    branch_on_github(&gh, "main", &main);
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "upstream_refused");
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("already has a branch feat"),
        "{v}"
    );
    assert_eq!(gh.tip(GH, "feat").as_deref(), Some(theirs.as_str()));
    let got = s
        .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
        .await;
    assert!(got["upstream"]["stopped"].is_string(), "{got}");
    refresh(&w).await;
    assert!(gh.pulls(GH).is_empty(), "nothing is opened while stopped");
    // Someone's own open pull request of that branch is never taken as ours.
    gh.set(|st| {
        st.pulls.push(common::github::FakePull {
            repo: GH.into(),
            number: 7,
            head: "feat".into(),
            base: "main".into(),
            title: "Someone's".into(),
            body: String::new(),
            open: true,
            merge_sha: None,
            author: "someone".into(),
        })
    });
    // Publishing again is the owner's decision to replace the branch.
    resume_publish(&w, n).await;
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(gh.tip(GH, "feat").as_deref(), Some(ours.as_str()));
    assert_ne!(v["upstream"]["number"], 7);
    assert_eq!(gh.pulls(GH).len(), 2);
    assert_eq!(gh.pull(7).title, "Someone's");
    let (st, v) = s
        .post(
            &format!("{}/pulls/{n}/close", w.repo_path()),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");

    // The push landed but its answer was lost: GitHub has exactly the head,
    // so publishing goes on.
    let head = w.push_change("landed", "l.txt", "l\n");
    let p = w.open_pull("landed", json!([])).await;
    let dir = gh.dir(GH);
    let branvil = s.data.join("repos/acme/demo.git");
    git_ok(
        &dir,
        &[
            "--git-dir",
            dir.to_str().unwrap(),
            "fetch",
            "-q",
            branvil.to_str().unwrap(),
            "refs/heads/landed:refs/heads/landed",
        ],
    );
    assert_eq!(gh.tip(GH, "landed").as_deref(), Some(head.as_str()));
    let (st, v) = publish(&w, p["number"].as_u64().unwrap()).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert!(v["upstream"]["number"].is_u64(), "{v}");
    let (st, v) = s
        .post(
            &format!("{}/pulls/{}/close", w.repo_path(), p["number"]),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");

    // The pull request was opened but the answer was lost; a branch name that
    // needs encoding in URLs. The next poll adopts it instead of opening a
    // second one.
    let branch = "fix/c++#2";
    w.push_change(branch, "c.txt", "c\n");
    let p = w.open_pull(branch, json!([])).await;
    let n = p["number"].as_u64().unwrap();
    gh.set(|st| st.lose_next_create = true);
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE, "{v}");
    refresh(&w).await;
    let opened: Vec<_> = gh
        .pulls(GH)
        .into_iter()
        .filter(|p| p.head == branch)
        .collect();
    assert_eq!(opened.len(), 1);
    let got = s
        .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
        .await;
    assert_eq!(got["upstream"]["number"], opened[0].number, "{got}");
    assert_eq!(got["upstream"]["error"], Value::Null, "{got}");
    // Ours: the next poll sent our title and body.
    refresh(&w).await;
    assert_eq!(gh.pull(opened[0].number).title, format!("PR {branch}"));
}

/// The client gives up on a publish while the host's answer to opening the
/// pull request is late: the publish runs to its end and records the pull
/// request the host opened.
#[tokio::test(flavor = "multi_thread")]
async fn a_publish_whose_client_gives_up_records_what_the_host_opened() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    w.push_change("feat", "f.txt", "one\n");
    let n = w.open_pull("feat", json!([])).await["number"]
        .as_u64()
        .unwrap();
    let pause = gh.pause_answer("POST", &format!("/repos/{GH}/pulls"));
    tokio::select! {
        _ = publish(&w, n) => panic!("the publish ended before the host answered"),
        _ = pause.reached() => {}
    }
    drop(pause);
    let opened = gh.pulls(GH);
    assert_eq!(opened.len(), 1);
    let pull = format!("{}/pulls/{n}", w.repo_path());
    eventually("the publish to record the opened pull request", || async {
        w.server.ok(&pull, &w.owner).await["upstream"]["number"] == opened[0].number
    })
    .await;
}

/// Ctrl-C while the host's answer to a poll's close of a published pull
/// request is late: the stop waits for the poll, which records what it sent,
/// so no later poll sends it again.
#[cfg(unix)]
#[tokio::test(flavor = "multi_thread")]
async fn ctrl_c_during_a_poll_waits_for_it_to_record_what_it_sent() {
    let (mut w, gh) = followed().await;
    with_aiko_account(&w).await;
    w.push_change("feat", "f.txt", "one\n");
    let n = w.open_pull("feat", json!([])).await["number"]
        .as_u64()
        .unwrap();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let number = v["upstream"]["number"].as_u64().unwrap();
    let host_pull = format!("/repos/{GH}/pulls/{number}");
    let pause = gh.pause_answer("PATCH", &host_pull);
    let (st, v) = w
        .server
        .post(
            &format!("{}/pulls/{n}/close", w.repo_path()),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    tokio::time::timeout(std::time::Duration::from_secs(15), pause.reached())
        .await
        .expect("the poll sends the close");
    w.server.interrupt_group();
    let line = w
        .server
        .line_within(std::time::Duration::from_secs(10))
        .expect("a stop that waits says so");
    assert!(line.starts_with("branvil stopping: "), "{line}");
    // Other operations end at once; the poll waits for the host's answer.
    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    assert!(w.server.running(), "the stop waits for the poll");
    drop(pause);
    let status = w.server.exit_within(std::time::Duration::from_secs(10));
    assert!(status.is_some_and(|s| s.success()), "{status:?}");
    assert!(!gh.pull(number).open);

    w.restart(&[]);
    refresh(&w).await;
    assert_eq!(
        gh.requests("PATCH", &host_pull).len(),
        1,
        "no later poll sends the close again"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pull_request_stays_with_the_repository_it_was_published_to() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let s = &w.server;
    gh.repository("octo/other", &w.work);
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let pull = format!("{}/pulls/{n}", w.repo_path());
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let number = v["upstream"]["number"].as_u64().unwrap();

    // The upstream changes while it is published: allowed, and the pull
    // request keeps talking to octo/demo.
    let (st, v) = s
        .put(
            &format!("{}/upstream", w.repo_path()),
            &w.owner,
            json!({"provider": "github", "repository": "octo/other"}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let (st, _) = s.patch(&pull, &w.aiko, json!({"body": "edited"})).await;
    assert_eq!(st, StatusCode::OK);
    refresh(&w).await;
    assert_eq!(gh.pull_of(GH, number).body, "edited");
    assert!(gh.pulls("octo/other").is_empty());
    assert!(gh.requests("PATCH", "/repos/octo/other/").is_empty());
    let got = s.ok(&pull, &w.owner).await;
    assert_eq!(got["upstream"]["repository"], GH);

    // Publishing it to the new upstream makes a new record there.
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["upstream"]["repository"], "octo/other");
    assert_eq!(gh.pulls("octo/other").len(), 1);

    // Removing the upstream is allowed too.
    let (st, v) = s
        .delete(&format!("{}/upstream", w.repo_path()), &w.owner)
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["upstream"], Value::Null);
}

#[tokio::test(flavor = "multi_thread")]
async fn retained_publication_problems_apply_only_to_the_current_upstream() {
    for condition in ["healthy", "stopped", "failed"] {
        for destination in ["current", "different", "absent"] {
            let (w, gh) = followed().await;
            with_aiko_account(&w).await;
            gh.repository("octo/other", &w.work);
            let mut head = w.push_change("feature", "f.txt", "one\n");
            let p = w.open_pull("feature", json!([])).await;
            let n = p["number"].as_u64().unwrap();
            let pull = format!("{}/pulls/{n}", w.repo_path());
            let (st, v) = publish(&w, n).await;
            assert_eq!(st, StatusCode::OK, "{condition}/{destination}: {v}");
            match condition {
                "stopped" => {
                    commit_on_github(&gh, "feature", "Host-side suggestion");
                    head = w.push_change("feature", "f.txt", "two\n");
                }
                "failed" => gh.check(&head, 51, "host-test", "failure", "failed\n"),
                _ => {}
            }
            refresh(&w).await;
            let expected = match condition {
                "stopped" => Some("publish_stopped"),
                "failed" => Some("host_failed"),
                _ => None,
            };
            eventually(
                &format!("{condition}/{destination}: the current publication controls admission"),
                || async {
                    let got = w.server.ok(&pull, &w.owner).await;
                    match expected {
                        Some(code) => {
                            got["landing"]["state"] == "left"
                                && got["landing"]["reason"]["code"] == code
                        }
                        None => got["landing"]["state"] == "passed",
                    }
                },
            )
            .await;
            // The author answers what Branvil said of the failure.
            w.acknowledge(n).await;
            match destination {
                "current" => continue,
                "different" => {
                    let (st, v) = w
                        .server
                        .put(
                            &format!("{}/upstream", w.repo_path()),
                            &w.owner,
                            json!({"provider":"github", "repository":"octo/other"}),
                        )
                        .await;
                    assert_eq!(st, StatusCode::OK, "{v}");
                }
                _ => {
                    let (st, v) = w
                        .server
                        .delete(&format!("{}/upstream", w.repo_path()), &w.owner)
                        .await;
                    assert_eq!(st, StatusCode::OK, "{v}");
                }
            }
            eventually(
                &format!("{condition}/{destination}: an old publication cannot exclude the new destination"),
                || async { w.server.ok(&pull, &w.owner).await["landing"]["state"] == "passed" },
            )
            .await;
            let got = w.server.ok(&pull, &w.owner).await;
            assert_eq!(got["upstream"]["repository"], GH);
            assert_eq!(got["head_sha"], head);
            if destination == "different" {
                let (st, v) = publish(&w, n).await;
                assert_eq!(st, StatusCode::OK, "{condition}/{destination}: {v}");
                assert_eq!(v["upstream"]["repository"], "octo/other");
                assert_eq!(gh.pulls("octo/other").len(), 1);
            } else {
                let (st, v) = w
                    .server
                    .post(
                        &format!("{pull}/merge"),
                        &w.owner,
                        json!({"expected_head":head, "expected_base":got["base_sha"]}),
                    )
                    .await;
                assert_eq!(st, StatusCode::OK, "{condition}/{destination}: {v}");
                assert_eq!(v["head_sha"], head);
                assert_eq!(v["merged"], true);
            }
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pull_request_the_host_never_opened_is_left_alone_once_closed() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let s = &w.server;
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    gh.set(|st| st.refuse_create = true);
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE, "{v}");
    refresh(&w).await;
    let open = gh.requests("POST", "/pulls").len();
    assert!(open >= 2, "retried while open: {open}");
    let (st, _) = s
        .post(
            &format!("{}/pulls/{n}/close", w.repo_path()),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, StatusCode::OK);
    // A poll the refused publish woke may still try once around the close
    // (it read the pull request before): count from the first poll after it.
    refresh(&w).await;
    let tries = gh.requests("POST", "/pulls").len();
    refresh(&w).await;
    refresh(&w).await;
    assert_eq!(gh.requests("POST", "/pulls").len(), tries);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_merge_is_taken_only_once_it_is_on_branvils_default_branch() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let s = &w.server;
    let t = w.ticket("t").await;
    w.push_change("feature", "f.txt", "one\n");
    let p = w
        .open_pull("feature", json!([{"url": t["url"], "closes": true}]))
        .await;
    let n = p["number"].as_u64().unwrap();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let number = v["upstream"]["number"].as_u64().unwrap();
    // GitHub's main is rewritten (following stops), then the merge lands on it.
    let dir = gh.dir(GH);
    let d = dir.to_str().unwrap();
    let tree = git_ok(&dir, &["--git-dir", d, "rev-parse", "main^{tree}"]);
    let unrelated = git_ok(
        &dir,
        &[
            "--git-dir",
            d,
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@x",
            "commit-tree",
            &tree,
            "-m",
            "rewritten",
        ],
    );
    git_ok(
        &dir,
        &["--git-dir", d, "update-ref", "refs/heads/main", &unrelated],
    );
    gh.squash_merge(GH, number);
    let v = refresh(&w).await;
    assert!(v["upstream"]["following"]["stopped"].is_string(), "{v}");
    let got = s
        .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
        .await;
    assert_eq!(got["merged"], false, "{got}");
    assert_eq!(got["upstream"]["state"], "merged");
    let ticket = s.ok(&format!("{}/tickets/1", w.repo_path()), &w.aiko).await;
    assert_eq!(ticket["state"], "open");
    assert_eq!(got["head_sha"], branvil_tip(&w, "feature").as_str());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_head_that_moved_past_what_the_host_merged_stays() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    gh.squash_merge(GH, v["upstream"]["number"].as_u64().unwrap());
    let later = w.push_change("feature", "g.txt", "two\n");
    refresh(&w).await;
    let got = w
        .server
        .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
        .await;
    assert_eq!(got["merged"], true, "{got}");
    assert_eq!(branvil_tip(&w, "feature"), later);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_published_pull_request_merged_here_once_the_upstream_is_removed_merged_its_head() {
    let (w, _gh) = followed().await;
    with_aiko_account(&w).await;
    let s = &w.server;
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let (st, v) = s
        .delete(&format!("{}/upstream", w.repo_path()), &w.owner)
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    // A commit Branvil never pushed to the host is what merges here.
    let later = w.push_change("feature", "g.txt", "two\n");
    let pull = format!("{}/pulls/{n}", w.repo_path());
    let p = s.ok(&pull, &w.owner).await;
    let (st, v) = s
        .post(
            &format!("{pull}/merge"),
            &w.owner,
            json!({"expected_head": later, "expected_base": p["base_sha"]}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["head_sha"], later.as_str());
    let (st, _) = s
        .get(&format!("{}/branches/feature", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_rebase_merge_on_the_host_keeps_the_whole_change() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let s = &w.server;
    w.push_change("feature", "f.txt", "one\n");
    w.push_change("feature", "g.txt", "two\n");
    let p = w.open_pull("feature", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let merged = gh.rebase_merge(GH, v["upstream"]["number"].as_u64().unwrap());
    refresh(&w).await;
    let pull = format!("{}/pulls/{n}", w.repo_path());
    let got = s.ok(&pull, &w.owner).await;
    assert_eq!(
        (got["merged"].clone(), got["merge_sha"].clone()),
        (json!(true), json!(merged))
    );
    let files = s.ok(&format!("{pull}/files"), &w.owner).await;
    let paths: Vec<&str> = files["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["path"].as_str().unwrap())
        .collect();
    assert_eq!(paths, ["f.txt", "g.txt"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pull_request_is_published_only_once_its_required_checks_succeed() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    w.push_change("feat", "f.txt", "one\n");
    let p = w.open_pull("feat", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    // `test` never ran (no pipeline), so it did not succeed: nothing goes out.
    set_required_checks(&w, json!(["test"])).await;
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "not_mergeable");
    assert!(
        v["error"]["message"].as_str().unwrap().contains("test"),
        "{v}"
    );
    assert_eq!(gh.tip(GH, "feat"), None);
    assert!(gh.pulls(GH).is_empty());

    // Without the requirement the same head is published.
    set_required_checks(&w, json!([])).await;
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert!(gh.tip(GH, "feat").is_some());
    assert_eq!(gh.pulls(GH).len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_upgrade_puts_publications_past_the_registration_of_their_host_checks() {
    let (mut w, _gh) = followed().await;
    with_aiko_account(&w).await;
    w.push_change("feat", "f.txt", "one\n");
    let n = w.open_pull("feat", json!([])).await["number"]
        .as_u64()
        .unwrap();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    w.server.stop();
    let db = w.server.data.join("branvil.redb");
    let mut pulls = records(&db, "pulls");
    as_schema_24(&db);
    let out = std::process::Command::new(BIN)
        .args(["admin", "upgrade", "--data-dir"])
        .arg(&w.server.data)
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(3),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    pulls[0]["upstream"]["checks_registered_by"] = Value::Null;
    pulls[0]["upstream"]["checks_settled"] = json!(false);
    assert_eq!(records(&db, "pulls"), pulls);
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

#[tokio::test(flavor = "multi_thread")]
async fn a_workflow_run_that_failed_without_a_job_is_a_failed_check() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let head = w.push_change("feat", "f.txt", "one\n");
    let n = w.open_pull("feat", json!([])).await["number"]
        .as_u64()
        .unwrap();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    // An invalid workflow file: GitHub ends its run before any job.
    gh.workflow_run(&head, 6, "completed", Some("startup_failure"));
    gh.workflow_run(&head, 7, "completed", Some("success"));
    refresh(&w).await;
    let checks = w
        .server
        .ok(&format!("{}/pulls/{n}/checks", w.repo_path()), &w.aiko)
        .await;
    let failed: Vec<_> = checks["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["name"].as_str().unwrap().starts_with("github/"))
        .collect();
    assert_eq!(failed.len(), 1, "{checks}");
    assert_eq!(failed[0]["name"], "github/ci-6");
    assert_eq!(failed[0]["conclusion"], "startup_failure");
    assert_eq!(
        failed[0]["details_url"],
        "https://github.test/actions/runs/6"
    );
}

async fn set_required_checks(w: &World, checks: Value) {
    let (_, repo) = w.server.get(&w.repo_path(), &w.owner).await;
    let (st, v) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "required_checks": checks}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
}

async fn set_account(w: &World, name: &str, token: &str) {
    let (st, v) = w
        .server
        .put(
            &format!("/members/{name}/accounts/github"),
            &w.owner,
            json!({"token": token}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
}

/// Sets the upstream and returns its reader.
async fn put_upstream(w: &World, body: Value) -> Value {
    let (st, v) = w
        .server
        .put(&format!("{}/upstream", w.repo_path()), &w.owner, body)
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    v["upstream"]["reader"].clone()
}

async fn reader_now(w: &World) -> Value {
    w.server.ok(&w.repo_path(), &w.owner).await["upstream"]["reader"].clone()
}

/// The logins of the GitHub reads since the `since`-th one.
fn reads_since(gh: &FakeGitHub, since: usize) -> Vec<String> {
    gh.requests("GET", "/repos/")[since..]
        .iter()
        .map(|(_, login)| login.clone())
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn the_reader_is_picked_once_and_changes_only_when_the_owner_or_a_removal_says_so() {
    let mut w = World::new().await;
    let gh = FakeGitHub::start(&w.dir.path().join("github")).await;
    gh.repository(GH, &w.work);
    for (token, login) in [
        (OWNER_TOKEN, "alice-gh"),
        (AIKO_TOKEN, "aiko-bot"),
        ("gh-ken", "ken-bot"),
        ("gh-abe", "abe-bot"),
    ] {
        gh.account(token, login);
    }
    gh.configure(&w.server.data);
    w.restart(&[]);
    let s = &w.server;
    let same = json!({"provider": "github", "repository": GH});

    // The owner's token is used only when no agent has an account, even
    // when the owner comes first by name (alice before ken).
    set_account(&w, "alice", OWNER_TOKEN).await;
    set_account(&w, "ken", "gh-ken").await;
    assert_eq!(
        put_upstream(&w, same.clone()).await,
        json!({"member": "ken", "chosen": false})
    );

    // A new account, even one that comes first by name, changes nothing.
    let (st, _) = s
        .post(
            "/members",
            &w.owner,
            json!({"name": "abe", "kind": "agent"}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED);
    set_account(&w, "abe", "gh-abe").await;
    let since = gh.requests("GET", "/repos/").len();
    let v = refresh(&w).await;
    assert_eq!(
        v["upstream"]["reader"],
        json!({"member": "ken", "chosen": false})
    );
    assert!(reads_since(&gh, since).iter().all(|l| l == "ken-bot"));
    // Saving the upstream again keeps it too.
    assert_eq!(
        put_upstream(&w, same.clone()).await,
        json!({"member": "ken", "chosen": false})
    );

    // The owner names one; one without an account is refused.
    let mut named = same.clone();
    named["reader"] = json!("aiko");
    let (st, v) = s
        .put(
            &format!("{}/upstream", w.repo_path()),
            &w.owner,
            named.clone(),
        )
        .await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "no_account");
    set_account(&w, "aiko", AIKO_TOKEN).await;
    assert_eq!(
        put_upstream(&w, named).await,
        json!({"member": "aiko", "chosen": true})
    );
    let since = gh.requests("GET", "/repos/").len();
    refresh(&w).await;
    assert!(reads_since(&gh, since).iter().all(|l| l == "aiko-bot"));
    // `null` picks again.
    let mut again = same.clone();
    again["reader"] = Value::Null;
    assert_eq!(
        put_upstream(&w, again).await,
        json!({"member": "abe", "chosen": false})
    );

    // Removing the reader's account picks another one at once.
    s.delete("/members/abe/accounts/github", &w.owner).await;
    assert_eq!(
        reader_now(&w).await,
        json!({"member": "aiko", "chosen": false})
    );
    for name in ["aiko", "ken"] {
        s.delete(&format!("/members/{name}/accounts/github"), &w.owner)
            .await;
    }
    assert_eq!(
        reader_now(&w).await,
        json!({"member": "alice", "chosen": false})
    );
    s.delete("/members/alice/accounts/github", &w.owner).await;
    let v = refresh(&w).await;
    assert_eq!(v["upstream"]["reader"]["member"], "");
    assert!(
        v["upstream"]["following"]["error"]
            .as_str()
            .unwrap_or_default()
            .contains("No member has a GitHub account"),
        "{v}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn branvils_references_are_written_for_the_host_when_published() {
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let s = &w.server;
    let tickets = format!("{}/tickets", w.repo_path());
    let issue = "https://github.test/octo/demo/issues/7";
    let (st, _) = s
        .post(
            &tickets,
            &w.aiko,
            json!({"title": "on GitHub", "upstream_issue": issue}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED);
    w.ticket("only here").await; // #2
    // !9 does not exist.
    // A pull request published first: !1 is #100 there.
    w.push_change("first", "a.txt", "a\n");
    let first = w.open_pull("first", json!([])).await;
    let (st, v) = publish(&w, first["number"].as_u64().unwrap()).await;
    assert_eq!(st, StatusCode::OK, "{v}");

    let base = &s.url;
    let body = format!(
        "Fixes #1 and #2, after !1 (not !9), like acme/demo#1 and octo/other#5.\n\n\
         `#1` in code, [#2](https://example.test) as a link.\n\n\
         ```\n#1 in a block\n```\n\n\
         See {base}/acme/demo/-/tickets/1, {base}/acme/demo/-/tickets/2 and {base}/acme/demo/-/pulls/1.\n\n\
         Use Vec<String> here.\n\nAfter #1 and !1."
    );
    w.push_change("second", "b.txt", "b\n");
    let (st, v) = s
        .post(
            &format!("{}/pulls", w.repo_path()),
            &w.aiko,
            json!({"head": "second", "title": "Follow-up to #1", "body": body}),
        )
        .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let n = v["pull"]["number"].as_u64().unwrap();
    let expected = format!(
        "Fixes #7 and `acme/demo#2`, after #100 (not !9), like #7 and octo/other#5.\n\n\
         `#1` in code, [#2](https://example.test) as a link.\n\n\
         ```\n#1 in a block\n```\n\n\
         See {issue}, `acme/demo#2` and {pr}.\n\n\
         Use Vec<String> here.\n\nAfter #7 and #100.",
        pr = "https://github.test/octo/demo/pull/100",
    );
    // The preview shows exactly that, and it is what GitHub gets.
    let preview = s
        .ok(&format!("{}/pulls/{n}/publish", w.repo_path()), &w.owner)
        .await;
    assert_eq!(preview["title"], "Follow-up to #7");
    assert_eq!(preview["body"], expected.as_str());
    // The first published pull request owns this base's gate until it leaves.
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"]["code"], "not_mergeable");
    let (st, v) = s
        .post(
            &format!("{}/pulls/{}/close", w.repo_path(), first["number"]),
            &w.owner,
            json!({}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let hp = gh.pull(v["upstream"]["number"].as_u64().unwrap());
    assert_eq!(
        (hp.title.as_str(), hp.body.as_str()),
        ("Follow-up to #7", expected.as_str())
    );
}

const APP_ID: &str = "4242";
const INSTALLATION: &str = "777";

/// A GitHub App (`octo-app`, installed as `INSTALLATION`) on the stand-in.
fn with_app(gh: &FakeGitHub) -> AppKey {
    let key = AppKey::generate();
    gh.app(APP_ID, INSTALLATION, "octo-app", &key);
    key
}

fn app_body(key: &str, installation: &str) -> Value {
    json!({"app": {"app_id": APP_ID, "installation_id": installation, "private_key": key}})
}

/// A line from the middle of a PEM: what must never show anywhere.
fn key_line(key: &AppKey) -> String {
    key.pem.lines().nth(5).unwrap().to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn an_app_account_is_checked_with_an_installation_token_and_replaces_a_token_account() {
    let (w, gh) = followed().await;
    let key = with_app(&gh);
    let s = &w.server;
    let account = "/members/aiko/accounts/github";
    let file = s.data.join("secrets/accounts/aiko.github.json");
    let (st, v) = s.put(account, &w.owner, json!({"token": AIKO_TOKEN})).await;
    assert_eq!(st, StatusCode::OK, "{v}");

    // Refused by the host or malformed: 400, and the token account stays.
    let other = AppKey::generate();
    for body in [
        app_body(&other.pem, INSTALLATION),
        app_body(&key.pem, "778"),
        app_body(
            "-----BEGIN RSA PRIVATE KEY-----\nnot a key\n-----END RSA PRIVATE KEY-----",
            INSTALLATION,
        ),
        json!({"app": {"app_id": "octo", "installation_id": INSTALLATION, "private_key": key.pem}}),
        json!({"app": key.pem}),
        json!({"token": AIKO_TOKEN, "app": {"app_id": APP_ID, "installation_id": INSTALLATION, "private_key": key.pem}}),
    ] {
        let (st, v) = s.put(account, &w.owner, body.clone()).await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{body}: {v}");
        assert_eq!(v["error"]["code"], "invalid");
        assert!(!v.to_string().contains(&key_line(&key)), "{v}");
    }
    assert_eq!(
        serde_json::from_slice::<Value>(&std::fs::read(&file).unwrap()).unwrap(),
        json!({"token": AIKO_TOKEN})
    );

    // Accepted: JWT → GET /app → an installation token; the login is the app's bot.
    let (st, v) = s
        .put(account, &w.owner, app_body(&key.pem, INSTALLATION))
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let a = &v["accounts"][0];
    assert_eq!(
        (&a["provider"], &a["login"], &a["kind"]),
        (&json!("github"), &json!("octo-app[bot]"), &json!("app"))
    );
    // The wrong key and the unknown installation were refused by GitHub
    // (the stand-in verifies the JWT); the malformed bodies never left.
    assert_eq!(gh.requests("GET", "/app").len(), 3);
    assert_eq!(gh.installation_tokens().len(), 1);
    // The file now holds the app (the token is gone), owner-only.
    let kept: Value = serde_json::from_slice(&std::fs::read(&file).unwrap()).unwrap();
    assert_eq!(kept, app_body(&key.pem, INSTALLATION));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    // Never in an answer: neither the key nor an installation token.
    let me = s.ok("/me", &w.aiko).await.to_string();
    let members = s.ok("/members?limit=100", &w.owner).await.to_string();
    for text in [me, members, v.to_string()] {
        assert!(!text.contains(&key_line(&key)), "{text}");
        for token in gh.installation_tokens() {
            assert!(!text.contains(&token), "{text}");
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn an_app_account_reads_and_publishes_with_installation_tokens_renewed_before_they_expire() {
    let (w, gh) = followed().await;
    let key = with_app(&gh);
    let s = &w.server;
    let (st, v) = s
        .put(
            "/members/aiko/accounts/github",
            &w.owner,
            app_body(&key.pem, INSTALLATION),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let mut named = json!({"provider": "github", "repository": GH});
    named["reader"] = json!("aiko");
    assert_eq!(
        put_upstream(&w, named).await,
        json!({"member": "aiko", "chosen": true})
    );

    // aiko's pull request is pushed and opened with the installation token,
    // and the app reads the upstream.
    let head = w.push_change("ticket/1", "f.txt", "one\n");
    let p = w.open_pull("ticket/1", json!([])).await;
    let n = p["number"].as_u64().unwrap();
    let reads = gh.requests("GET", "/repos/").len();
    let (st, v) = publish(&w, n).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(gh.tip(GH, "ticket/1").as_deref(), Some(head.as_str()));
    let number = v["upstream"]["number"].as_u64().unwrap();
    assert_eq!(gh.pull(number).author, "octo-app[bot]");
    let v = refresh(&w).await;
    assert_eq!(v["upstream"]["following"]["error"], Value::Null, "{v}");
    assert!(reads_since(&gh, reads).iter().all(|l| l == "octo-app[bot]"));
    // The installation token made when the account was set is still used.
    assert_eq!(gh.requests("POST", "/access_tokens").len(), 1);

    // Tokens that expire soon: setting the account again makes one (valid
    // 301 s; Branvil makes a new one when less than 300 s are left). Once it
    // and every earlier one stopped working, reads, the push of a fix, and
    // API calls still succeed with a token made in time.
    gh.set(|st| st.token_lifetime = 301);
    let (st, v) = s
        .put(
            "/members/aiko/accounts/github",
            &w.owner,
            app_body(&key.pem, INSTALLATION),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(gh.requests("POST", "/access_tokens").len(), 2);
    tokio::time::sleep(std::time::Duration::from_millis(1500)).await;
    gh.expire_installation_tokens();
    let fixed = w.push_change("ticket/1", "f.txt", "fixed\n");
    let (st, _) = s
        .patch(
            &format!("{}/pulls/{n}", w.repo_path()),
            &w.aiko,
            json!({"body": "renewed"}),
        )
        .await;
    assert_eq!(st, StatusCode::OK);
    let since = gh.requests("GET", "/repos/").len();
    let v = refresh(&w).await;
    assert_eq!(v["upstream"]["following"]["error"], Value::Null, "{v}");
    let got = s.ok(&format!("{}/pulls/{n}", w.repo_path()), &w.aiko).await;
    assert_eq!(got["upstream"]["error"], Value::Null, "{got}");
    assert_eq!(gh.tip(GH, "ticket/1").as_deref(), Some(fixed.as_str()));
    assert_eq!(gh.pull(number).body, "renewed");
    assert!(gh.requests("POST", "/access_tokens").len() >= 3);
    assert!(reads_since(&gh, since).iter().all(|l| l == "octo-app[bot]"));

    // The key and the tokens are only in the account's file.
    let mut secrets = gh.installation_tokens();
    secrets.push(key_line(&key));
    for file in [s.data.join("branvil.redb"), w.dir.path().join("serve.log")] {
        let bytes = std::fs::read(&file).unwrap();
        for secret in &secrets {
            assert!(
                !bytes.windows(secret.len()).any(|x| x == secret.as_bytes()),
                "{} contains a secret",
                file.display()
            );
        }
    }
}

fn alerts_path(w: &World) -> String {
    format!("{}/upstream/dependency-alerts", w.repo_path())
}

/// Every alert of `query`, following `next_cursor` page by page.
async fn all_alerts(w: &World, query: &str) -> (Vec<Value>, usize) {
    let (mut found, mut pages, mut cursor) = (Vec::new(), 0, String::new());
    loop {
        let mut path = format!("{}?{query}", alerts_path(w));
        if !cursor.is_empty() {
            path = format!("{path}&cursor={cursor}");
        }
        let v = w.server.ok(&path, &w.aiko).await;
        found.extend(v["items"].as_array().unwrap().iter().cloned());
        pages += 1;
        match v["next_cursor"].as_str() {
            Some(c) => cursor = c.to_string(),
            None => return (found, pages),
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn dependency_alerts_are_read_from_the_host_page_by_page_with_the_readers_account() {
    let (w, gh) = followed().await;
    let s = &w.server;
    // aiko has an account too, but the owner's is the upstream's reader.
    set_account(&w, "aiko", AIKO_TOKEN).await;
    assert_eq!(reader_now(&w).await["member"], "alice");

    // Nothing on the host yet: an empty list, read from the host.
    let v = s.ok(&alerts_path(&w), &w.aiko).await;
    assert_eq!(v, json!({"items": [], "next_cursor": null}));

    gh.alert(1, "open", Some("2.0.0"));
    gh.alert(2, "fixed", Some("2.0.0"));
    gh.alert(3, "open", None);
    gh.alert(4, "auto_dismissed", Some("2.0.0"));
    gh.alert(5, "open", Some("2.1.0"));
    gh.alert(6, "dismissed", None);

    // Any member who sees the repository reads them, in the host's order,
    // across pages; the host's cursor is carried in Branvil's.
    let (open, pages) = all_alerts(&w, "limit=2").await;
    let ids: Vec<&str> = open.iter().map(|a| a["id"].as_str().unwrap()).collect();
    assert_eq!((ids, pages), (vec!["1", "3", "5"], 2));
    assert_eq!(
        open[0],
        json!({
            "id": "1", "state": "open",
            "url": "https://github.test/octo/demo/security/dependabot/1",
            "package": "pkg-1", "ecosystem": "npm", "manifest_path": "package-lock.json",
            "severity": "high",
            "identifiers": [
                {"type": "GHSA", "value": "GHSA-0000-0000-0001"},
                {"type": "CVE", "value": "CVE-2026-0001"},
            ],
            "summary": "Advisory 1", "description": "What goes wrong.",
            "affected_versions": "< 2.0.0", "patched_version": "2.0.0",
            "created_at": "2026-10-20T08:00:00.000Z", "updated_at": "2026-10-21T09:30:00.000Z",
        })
    );
    // Without a version that fixes it.
    assert_eq!(open[1]["patched_version"], Value::Null);
    let states = |list: &[Value]| -> Vec<(String, String)> {
        list.iter()
            .map(|a| {
                (
                    a["id"].as_str().unwrap().into(),
                    a["state"].as_str().unwrap().into(),
                )
            })
            .collect()
    };
    let (resolved, _) = all_alerts(&w, "state=resolved").await;
    assert_eq!(states(&resolved), [("2".into(), "resolved".into())]);
    let (dismissed, _) = all_alerts(&w, "state=dismissed&limit=1").await;
    assert_eq!(
        states(&dismissed),
        [
            ("4".into(), "dismissed".into()),
            ("6".into(), "dismissed".into())
        ]
    );

    // One alert by its id.
    let v = s.ok(&format!("{}/2", alerts_path(&w)), &w.aiko).await;
    assert_eq!((&v["id"], &v["state"]), (&json!("2"), &json!("resolved")));
    for missing in ["99", "0", "02", "x"] {
        let (st, v) = s
            .get(&format!("{}/{missing}", alerts_path(&w)), &w.aiko)
            .await;
        assert_eq!(st, StatusCode::NOT_FOUND, "{missing}: {v}");
    }

    // A cursor is bound to the conditions (`limit` aside, as for every
    // collection) and to the upstream.
    let first = s.ok(&format!("{}?limit=1", alerts_path(&w)), &w.aiko).await;
    let cursor = first["next_cursor"].as_str().unwrap().to_string();
    let rest = s
        .ok(
            &format!("{}?limit=5&cursor={cursor}", alerts_path(&w)),
            &w.aiko,
        )
        .await;
    assert_eq!(rest["items"].as_array().unwrap().len(), 2, "{rest}");
    for other in [
        format!("state=resolved&limit=1&cursor={cursor}"),
        "limit=1&cursor=garbage".to_string(),
    ] {
        let (st, v) = s
            .get(&format!("{}?{other}", alerts_path(&w)), &w.aiko)
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{other}: {v}");
        assert_eq!(v["error"]["code"], "invalid_cursor");
    }
    let (st, v) = s
        .get(&format!("{}?state=fixed", alerts_path(&w)), &w.aiko)
        .await;
    assert_eq!(
        (st, &v["error"]["code"]),
        (StatusCode::BAD_REQUEST, &json!("invalid"))
    );

    // Every read went to GitHub as the reader, never as aiko's account.
    let reads = gh.requests("GET", "/dependabot/alerts");
    assert!(!reads.is_empty());
    assert!(
        reads.iter().all(|(_, login)| login == "alice-gh"),
        "{reads:?}"
    );
    let answer = s
        .ok(&format!("{}?limit=100", alerts_path(&w)), &w.aiko)
        .await;
    assert!(!answer.to_string().contains(OWNER_TOKEN));

    // A GitHub App reads them with its installation token.
    let key = with_app(&gh);
    let (st, v) = s
        .put(
            "/members/aiko/accounts/github",
            &w.owner,
            app_body(&key.pem, INSTALLATION),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let mut named = json!({"provider": "github", "repository": GH});
    named["reader"] = json!("aiko");
    put_upstream(&w, named).await;
    let since = gh.requests("GET", "/dependabot/alerts").len();
    let (open, _) = all_alerts(&w, "limit=100").await;
    assert_eq!(open.len(), 3);
    let reads = gh.requests("GET", "/dependabot/alerts");
    assert!(
        reads[since..].iter().all(|(_, l)| l == "octo-app[bot]"),
        "{reads:?}"
    );

    // The cursor of an earlier upstream does not continue on another one.
    let first = s.ok(&format!("{}?limit=1", alerts_path(&w)), &w.aiko).await;
    let cursor = first["next_cursor"].as_str().unwrap().to_string();
    gh.repository("octo/other", &w.work);
    put_upstream(
        &w,
        json!({"provider": "github", "repository": "octo/other"}),
    )
    .await;
    let (st, v) = s
        .get(
            &format!("{}?limit=1&cursor={cursor}", alerts_path(&w)),
            &w.aiko,
        )
        .await;
    assert_eq!(
        (st, &v["error"]["code"]),
        (StatusCode::BAD_REQUEST, &json!("invalid_cursor"))
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failed_read_of_dependency_alerts_is_never_an_empty_list() {
    let (w, gh) = followed().await;
    let s = &w.server;
    set_account(&w, "aiko", AIKO_TOKEN).await;
    gh.alert(1, "open", None);
    let list = alerts_path(&w);
    let one = format!("{list}/1");
    let failed = |st: StatusCode, v: &Value, code: &str| {
        assert_eq!((st, v["error"]["code"].as_str()), (st, Some(code)), "{v}");
        assert!(v.get("items").is_none(), "{v}");
    };

    // The host refuses: what it said and what to check, as the reader only.
    let refused = |status, message| Refusal {
        status,
        headers: vec![],
        message,
    };
    let access = "Dependabot alerts: Read-only";
    let rate_limited = Refusal {
        headers: vec![
            ("x-ratelimit-remaining", "0".into()),
            ("x-ratelimit-reset", "1792483200".into()),
        ],
        ..refused(403, "API rate limit exceeded")
    };
    let cases = [
        (
            refused(403, "Resource not accessible by integration"),
            StatusCode::CONFLICT,
            "upstream_refused",
            vec!["Resource not accessible by integration", access],
        ),
        (
            refused(401, "Bad credentials"),
            StatusCode::CONFLICT,
            "upstream_refused",
            vec!["Bad credentials", access],
        ),
        (
            refused(404, "Not Found"),
            StatusCode::NOT_FOUND,
            "not_found",
            vec!["Not Found", access],
        ),
        (
            rate_limited,
            StatusCode::SERVICE_UNAVAILABLE,
            "unavailable",
            vec!["rate limit", "until 2026-10-20T08:00:00.000Z"],
        ),
        (
            refused(502, "Server Error"),
            StatusCode::SERVICE_UNAVAILABLE,
            "unavailable",
            vec!["502"],
        ),
    ];
    for (refusal, expected, code, words) in cases {
        gh.set(|st| st.refusal = Some(refusal.clone()));
        for path in [&list, &one] {
            let (st, v) = s.get(path, &w.aiko).await;
            assert_eq!(st, expected, "{} {path}: {v}", refusal.status);
            failed(st, &v, code);
            let said = v["error"]["message"].as_str().unwrap();
            assert!(words.iter().all(|w| said.contains(w)), "{said}");
        }
    }
    let reads = gh.requests("GET", "/dependabot/alerts");
    assert!(
        reads.iter().all(|(_, login)| login == "alice-gh"),
        "{reads:?}"
    );
    gh.set(|st| st.refusal = None);
    assert_eq!(s.ok(&list, &w.aiko).await["items"][0]["id"], "1");

    // The reader's credential is not on this machine: nobody else's is used.
    let file = s.data.join("secrets/accounts/alice.github.json");
    let kept = std::fs::read(&file).unwrap();
    std::fs::remove_file(&file).unwrap();
    let before = gh.requests("GET", "/dependabot/alerts").len();
    let (st, v) = s.get(&list, &w.aiko).await;
    failed(st, &v, "no_account");
    assert_eq!(st, StatusCode::CONFLICT);
    assert_eq!(gh.requests("GET", "/dependabot/alerts").len(), before);
    std::fs::write(&file, kept).unwrap();

    // No upstream: nothing to read alerts of.
    let (st, _) = s
        .delete(&format!("{}/upstream", w.repo_path()), &w.owner)
        .await;
    assert_eq!(st, StatusCode::OK);
    for path in [&list, &one] {
        let (st, v) = s.get(path, &w.aiko).await;
        assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY, "{v}");
        failed(st, &v, "unsupported");
    }
}
