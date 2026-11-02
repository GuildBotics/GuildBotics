//! Restart and crash recovery: redb reopened from disk, journaled merges
//! interrupted between Git and the database, and pushes whose post-receive
//! never reached the database. Crashes are simulated with
//! `BRANVIL_FAILPOINT`, which aborts the process at a named point. This is
//! process-kill testing, not power-loss testing. Every other stop is a normal
//! one (SIGINT), which waits for the repository operations already started.

use crate::common;

use common::*;
use serde_json::{Value, json};

async fn ready_pull(w: &World) -> (String, String, String) {
    let s = &w.server;
    let t = w.ticket("Recover").await;
    let one = format!("/repos/{}/tickets/1", encode(REPO));
    s.put(
        &format!("{one}/board"),
        &w.aiko,
        json!({"lane": "in_progress"}),
    )
    .await;
    let head = w.push_change("feature", "f.txt", "one\n");
    let p = w
        .open_pull("feature", json!([{"url": t["url"], "closes": true}]))
        .await;
    let base = format!("{}/pulls/{}", w.repo_path(), p["number"]);
    (base, head, p["base_sha"].as_str().unwrap().to_string())
}

fn bare_main(w: &World) -> String {
    bare_ref(w, "refs/heads/main")
}

fn bare_ref(w: &World, refname: &str) -> String {
    let bare = w.server.data.join("repos/acme/demo.git");
    git_ok(
        &bare,
        &["--git-dir", bare.to_str().unwrap(), "rev-parse", refname],
    )
}

async fn pending_ops(w: &World) -> u64 {
    w.server.ok("/admin/diagnostics", &w.owner).await["storage"]["pending_ref_operations"]
        .as_u64()
        .unwrap()
}

#[tokio::test]
async fn records_survive_a_restart() {
    let mut w = World::new().await;
    let t = w.ticket("Persist").await;
    w.push_change("feature", "f.txt", "x");
    let p = w
        .open_pull("feature", json!([{"url": t["url"], "closes": true}]))
        .await;
    w.server
        .post(
            &format!("{}/pulls/{}/comments", w.repo_path(), p["number"]),
            &w.ken,
            json!({"body": "kept"}),
        )
        .await;
    w.restart(&[]);
    let s = &w.server;
    let ticket = s
        .ok(&format!("/repos/{}/tickets/1", encode(REPO)), &w.aiko)
        .await;
    assert_eq!(ticket["title"], "Persist");
    let pull = s.ok(&format!("{}/pulls/1", w.repo_path()), &w.aiko).await;
    assert_eq!(pull["tickets"][0]["closes"], true);
    let comments = s
        .ok(&format!("{}/pulls/1/comments", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(comments["items"][0]["body"], "kept");
    // Tokens still work, numbering continues.
    assert_eq!(w.ticket("Next").await["key"], "2");
}

#[tokio::test]
async fn a_merge_interrupted_after_the_ref_update_completes_at_startup() {
    let mut w = World::new().await;
    let (base, head, main) = ready_pull(&w).await;
    w.restart(&[("BRANVIL_FAILPOINT", "merge_after_update_ref")]);
    let url = format!("{}/api/v1{base}/merge", w.server.url);
    let result = w
        .server
        .http
        .post(url)
        .bearer_auth(&w.owner)
        .json(&json!({"expected_head": head, "expected_base": main}))
        .send()
        .await;
    assert!(result.is_err() || !result.unwrap().status().is_success());
    assert!(
        !w.server.wait_exit().success(),
        "the failpoint aborts the service"
    );
    // Git moved and keeps the merged head; the database did not record it yet.
    assert_eq!(bare_main(&w), head);
    assert_eq!(bare_ref(&w, "refs/branvil/merged/1"), head);

    w.restart(&[]);
    let s = &w.server;
    let pull = s.ok(&base, &w.aiko).await;
    assert_eq!(pull["merged"], true, "{pull}");
    assert_eq!(pull["merge_sha"], head.as_str());
    let ticket = s
        .ok(&format!("/repos/{}/tickets/1", encode(REPO)), &w.aiko)
        .await;
    assert_eq!(ticket["state"], "closed");
    assert_eq!(ticket["lane"], "done");
    assert_eq!(pending_ops(&w).await, 0);
}

#[tokio::test]
async fn a_merge_interrupted_before_the_ref_update_is_dropped_at_startup() {
    let mut w = World::new().await;
    let (base, head, main) = ready_pull(&w).await;
    w.restart(&[("BRANVIL_FAILPOINT", "merge_before_update_ref")]);
    let _ = w
        .server
        .http
        .post(format!("{}/api/v1{base}/merge", w.server.url))
        .bearer_auth(&w.owner)
        .json(&json!({"expected_head": head, "expected_base": main}))
        .send()
        .await;
    assert!(!w.server.wait_exit().success());
    assert_eq!(bare_main(&w), main);

    w.restart(&[]);
    let s = &w.server;
    let pull = s.ok(&base, &w.aiko).await;
    assert_eq!(pull["merged"], false);
    assert_eq!(pull["state"], "open");
    assert_eq!(pending_ops(&w).await, 0);
    // The merge can be retried and succeeds.
    let (st, v) = s
        .post(
            &format!("{base}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": main}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
}

/// Waits until pull request `base` is merged, at most 15 s.
async fn merged(w: &World, base: &str) -> Value {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
    loop {
        let pull = w.server.ok(base, &w.aiko).await;
        if pull["merged"] == true {
            return pull;
        }
        assert!(std::time::Instant::now() < deadline, "never merged: {pull}");
        tokio::time::sleep(std::time::Duration::from_millis(30)).await;
    }
}

fn merge_request(w: &World, base: &str, head: &str, main: &str) -> impl std::future::Future {
    w.server
        .http
        .post(format!("{}/api/v1{base}/merge", w.server.url))
        .bearer_auth(&w.owner)
        .json(&json!({"expected_head": head, "expected_base": main}))
        .send()
}

/// The client gives up on a merge while Git holds the base's lock: the merge
/// still runs to its end, and Git and the database agree without a restart.
#[cfg(unix)]
#[tokio::test]
async fn a_merge_whose_client_gives_up_runs_to_its_end() {
    let w = World::new().await;
    let (base, head, main) = ready_pull(&w).await;
    let hold = RefHold::new(&w, "refs/heads/main");
    abandon_at(&hold, merge_request(&w, &base, &head, &main)).await;
    hold.release();
    let pull = merged(&w, &base).await;
    assert_eq!(pull["merge_sha"], head.as_str());
    assert_eq!(bare_main(&w), head);
    let bare = w.server.data.join("repos/acme/demo.git");
    assert!(!bare.join("refs/heads/main.lock").exists());
    assert_eq!(pending_ops(&w).await, 0);
}

/// Ctrl-C in the terminal (SIGINT to the service's process group) while Git
/// holds the base's lock in a merge: Git, in a group of its own, is not
/// interrupted, and the service says it waits for the merge, waits, and
/// stops cleanly; the base moved and no lock is left.
#[cfg(unix)]
#[tokio::test]
async fn ctrl_c_during_a_merge_waits_for_it() {
    let mut w = World::new().await;
    let (base, head, main) = ready_pull(&w).await;
    let hold = RefHold::new(&w, "refs/heads/main");
    abandon_at(&hold, merge_request(&w, &base, &head, &main)).await;
    w.server.interrupt_group();
    let line = w
        .server
        .line_within(std::time::Duration::from_secs(10))
        .expect("a stop that waits says so");
    assert!(
        line.starts_with("branvil stopping: waiting for the started repository operations"),
        "{line}"
    );
    assert!(w.server.running(), "the stop waits for the merge");
    hold.release();
    let status = w.server.exit_within(std::time::Duration::from_secs(10));
    assert!(status.is_some_and(|s| s.success()), "{status:?}");
    let bare = w.server.data.join("repos/acme/demo.git");
    assert!(!bare.join("refs/heads/main.lock").exists());
    assert_eq!(bare_main(&w), head);

    w.restart(&[]);
    let pull = w.server.ok(&base, &w.aiko).await;
    assert_eq!(pull["merged"], true, "{pull}");
    assert_eq!(pending_ops(&w).await, 0);
}

/// The service exits while its Git, in a group of its own, still holds a
/// merge's base in the ref update (it was killed), and a supervisor that waited only for the service's own exit
/// starts the next one at once. The next start waits for that Git before it
/// touches any repository: the merge is then recorded as Git did it, on
/// another base than the default branch (whose recovery would otherwise drop
/// the merge Git then did) and on the default branch (whose `HEAD.lock` would
/// otherwise fail the start).
#[cfg(unix)]
#[tokio::test]
async fn the_next_start_waits_for_the_git_a_stopped_service_left_running() {
    for branch in ["release", "main"] {
        let mut w = World::new().await;
        let s = &w.server;
        let t = w.ticket("Recover").await;
        if branch != "main" {
            w.push_change(branch, "r.txt", "r\n");
        }
        let head = w.push_change("feature", "f.txt", "one\n");
        let (st, p) = s
            .post(
                &format!("{}/pulls", w.repo_path()),
                &w.aiko,
                json!({"head": "feature", "base": branch, "title": "Recover",
                    "tickets": [{"url": t["url"], "closes": true}]}),
            )
            .await;
        assert_eq!(st, 201, "{p}");
        let pull = &p["pull"];
        let base = format!("{}/pulls/{}", w.repo_path(), pull["number"]);
        let old = pull["base_sha"].as_str().unwrap().to_string();
        let refname = format!("refs/heads/{branch}");
        let hold = RefHold::new(&w, &refname);
        abandon_at(&hold, merge_request(&w, &base, &head, &old)).await;

        // Only the service ends; its session is not drained.
        w.server.signal("KILL");
        while w.server.running() {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        // A start that waits still stops at Ctrl-C.
        let waiting_log = w.dir.path().join("waiting.log");
        let mut waiting = ServiceProcess::spawn(
            std::process::Command::new(BIN)
                .args(["serve", "--listen", "127.0.0.1:0", "--data-dir"])
                .arg(&w.server.data)
                .env_remove("BRANVIL_FAILPOINT")
                .stdout(std::process::Stdio::null())
                .stderr(std::fs::File::create(&waiting_log).unwrap()),
        )
        .unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        while !std::fs::read_to_string(&waiting_log)
            .unwrap()
            .contains("waiting for the Git a stopped service started to end")
        {
            assert!(
                std::time::Instant::now() < deadline,
                "the start did not wait"
            );
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let pid = waiting.id().to_string();
        assert!(
            std::process::Command::new("kill")
                .args(["-INT", &pid])
                .status()
                .unwrap()
                .success()
        );
        let status = waiting
            .exit_within(std::time::Duration::from_secs(5))
            .unwrap()
            .expect("Ctrl-C stops it");
        assert!(status.success(), "{status:?}");

        let log = w.dir.path().join("serve.log");
        let releasing = {
            let (hold_reached, log) = (hold.reached(), log.clone());
            let released = hold.released_path();
            let bare = w.server.data.join("repos/acme/demo.git");
            let (refname, old) = (refname.clone(), old.clone());
            std::thread::spawn(move || {
                assert!(hold_reached);
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
                while !std::fs::read_to_string(&log)
                    .unwrap()
                    .contains("waiting for the Git a stopped service started to end")
                {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "the next start did not wait"
                    );
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                let at = git_ok(
                    &bare,
                    &["--git-dir", bare.to_str().unwrap(), "rev-parse", &refname],
                );
                assert_eq!(at, old, "Git is still held");
                std::fs::write(released, "").unwrap();
            })
        };
        let next = Server::start_with(w.dir.path(), &[]);
        let stopped = std::mem::replace(&mut w.server, next);
        releasing.join().unwrap();
        drop(stopped);

        let pull = w.server.ok(&base, &w.aiko).await;
        assert_eq!(pull["merged"], true, "{branch}: {pull}");
        assert_eq!(bare_ref(&w, &refname), head);
        let ticket = w
            .server
            .ok(&format!("/repos/{}/tickets/1", encode(REPO)), &w.aiko)
            .await;
        assert_eq!(ticket["state"], "closed", "{branch}");
        assert_eq!(pending_ops(&w).await, 0);
        let bare = w.server.data.join("repos/acme/demo.git");
        assert!(!bare.join(format!("{refname}.lock")).exists());
        assert!(!bare.join("HEAD.lock").exists());
    }
}

/// The service is killed while a push's Git holds the branch's lock: the next
/// service waits for it (it never takes the lock for a stale one), the push
/// lands, and the next service's startup rescan records it: the open pull
/// request of that branch shows the activity.
#[cfg(unix)]
#[tokio::test]
async fn a_push_outlives_its_killed_service() {
    let mut w = World::new().await;
    w.push_change("crossing", "c.txt", "one\n");
    let p = w.open_pull("crossing", json!([])).await;
    let pull = format!("{}/pulls/{}", w.repo_path(), p["number"]);
    let before = w.server.ok(&pull, &w.aiko).await["updated_at"].clone();
    std::fs::write(w.work.join("c.txt"), "two\n").unwrap();
    git_ok(&w.work, &["commit", "-qam", "two"]);
    let sha = git_ok(&w.work, &["rev-parse", "HEAD"]);
    let hold = RefHold::new(&w, "refs/heads/crossing");
    let (work, aiko) = (w.work.clone(), w.aiko.clone());
    let push = std::thread::spawn(move || {
        git_as(
            &work,
            "aiko",
            &aiko,
            &["push", "-q", "origin", "HEAD:refs/heads/crossing"],
        )
    });
    hold.wait().await;
    // Only the service ends; its session is not drained.
    w.server.signal("KILL");
    while w.server.running() {
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    let releasing = {
        let log = w.dir.path().join("serve.log");
        let released = hold.released_path();
        std::thread::spawn(move || {
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
            while !std::fs::read_to_string(&log)
                .unwrap()
                .contains("waiting for the Git a stopped service started to end")
            {
                assert!(
                    std::time::Instant::now() < deadline,
                    "the next start did not wait"
                );
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            std::fs::write(released, "").unwrap();
        })
    };
    let next = Server::start_with(w.dir.path(), &[]);
    let stopped = std::mem::replace(&mut w.server, next);
    releasing.join().unwrap();
    drop(stopped);
    let _ = push.join();
    assert_eq!(bare_ref(&w, "refs/heads/crossing"), sha);
    let after = w.server.ok(&pull, &w.aiko).await;
    assert_ne!(
        after["updated_at"], before,
        "the rescan recorded the push: {after}"
    );
}

/// Lock files a killed Git left (no Git runs any more) are removed at start:
/// the service starts, and the base can be merged.
#[cfg(unix)]
#[tokio::test]
async fn lock_files_a_killed_git_left_are_removed_at_start() {
    let mut w = World::new().await;
    let (pull, head, main) = ready_pull(&w).await;
    w.server.stop();
    let bare = w.server.data.join("repos/acme/demo.git");
    for lock in ["HEAD.lock", "refs/heads/main.lock"] {
        std::fs::write(bare.join(lock), "").unwrap();
    }
    w.restart(&[]);
    let (st, v) = w
        .server
        .post(
            &format!("{pull}/merge"),
            &w.owner,
            json!({"expected_head": head, "expected_base": main}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
}

#[tokio::test]
async fn a_push_the_service_did_not_record_is_found_at_startup() {
    let mut w = World::new().await;
    w.push_change("feature", "f.txt", "one\n");
    let p = w.open_pull("feature", json!([])).await;
    let before: Value = w
        .server
        .ok(&format!("{}/pulls/1", w.repo_path()), &w.aiko)
        .await;
    w.restart(&[("BRANVIL_FAILPOINT", "post_receive_before_record")]);
    // The push lands in Git; the service aborts before recording it.
    let w2 = &w;
    let file = w2.work.join("f.txt");
    std::fs::write(&file, "one\ntwo\n").unwrap();
    git_ok(&w.work, &["commit", "-qam", "unrecorded"]);
    let _ = git_as(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", "origin", "HEAD:refs/heads/feature"],
    );
    assert!(!w.server.wait_exit().success());
    let new_tip = git_ok(&w.work, &["rev-parse", "HEAD"]);

    w.restart(&[]);
    let after = w
        .server
        .ok(&format!("{}/pulls/1", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(after["head_sha"], new_tip.as_str());
    assert_ne!(after["head_sha"], p["head_sha"]);
    assert!(
        after["updated_at"].as_str().unwrap() > before["updated_at"].as_str().unwrap(),
        "the startup rescan recorded the new head as activity"
    );
}
