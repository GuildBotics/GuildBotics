use super::*;
use std::{
    fs,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};
#[path = "../../../tests/support/contracts.rs"]
mod contracts;
fn fixture() -> (tempfile::TempDir, LocalService) {
    let d = tempfile::tempdir().unwrap();
    let s = LocalService::new(
        d.path().into(),
        "acme",
        "local-aiko",
        "aiko",
        "Aiko",
        "success",
    );
    let folder = s.store.dir(WorkKind::Ticket, "acme/team");
    fs::create_dir_all(&folder).unwrap();
    fs::write(
        folder.join("tracker.json"),
        br#"{"labels":["bug"],"repository":"acme/repo"}"#,
    )
    .unwrap();
    (d, s)
}
fn request(tracker: &str) -> CreateTicket {
    CreateTicket {
        tracker: tracker.into(),
        title: "ticket".into(),
        body: "".into(),
        labels: vec![],
        add_to_board: true,
        interactive: false,
        assignee: Some(ActorName("local-aiko".into())),
        lane: Some(CreateLane::Ready),
        parent: None,
        blocked_by: vec![],
    }
}
#[tokio::test]
async fn ticket_shared_contract() {
    let (_d, s) = fixture();
    contracts::ticket_contract(&s, "acme/team", "bug").await;
}
#[tokio::test]
async fn chat_shared_contract() {
    let (d, _s) = fixture();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    contracts::chat_contract(&c, "general").await;
    let lines = fs::read_to_string(d.path().join("chat/general.jsonl")).unwrap();
    assert_eq!(lines.lines().count(), 3);
}
#[tokio::test]
async fn relations_validate_before_writes_and_working_ignores_open_blockers() {
    let (_d, s) = fixture();
    let first = s.create_ticket(request("acme/team")).await.unwrap();
    let mut q = request("acme/team");
    q.blocked_by = vec![first.target.url.clone()];
    q.lane = Some(CreateLane::InProgress);
    let second = s.create_ticket(q).await.unwrap();
    assert!(
        TicketService::candidates(&s)
            .await
            .unwrap()
            .iter()
            .any(|t| t.url.as_deref() == Some(second.target.url.as_str()))
    );
    let r = Ref::locate(&second.target.url, WorkKind::Ticket).unwrap();
    let before = fs::read(s.store.path(&r)).unwrap();
    {
        let blocked_by = vec!["local://tickets/acme/team/T-999".into()];
        let mut q = request("acme/team");
        q.blocked_by = blocked_by.clone();
        assert_eq!(
            s.create_ticket(q).await.unwrap_err(),
            ServiceError::NotFound
        );
        assert_eq!(
            s.update_ticket(
                &second.target.url,
                UpdateTicket {
                    add_blocked_by: blocked_by,
                    ..Default::default()
                }
            )
            .await
            .unwrap_err(),
            ServiceError::NotFound
        );
        assert_eq!(before, fs::read(s.store.path(&r)).unwrap());
    }
    assert!(
        s.update_ticket(
            &second.target.url,
            UpdateTicket {
                body: Some("should not persist".into()),
                parent: Some("local://tickets/acme/team/T-999".into()),
                ..Default::default()
            }
        )
        .await
        .is_err()
    );
    assert_eq!(before, fs::read(s.store.path(&r)).unwrap());
    let mut v = s.store.load(&r).unwrap();
    v["parent"] = json!("local://tickets/other/team/T-1");
    let outside = Ref::locate("local://tickets/other/team/T-1", WorkKind::Ticket).unwrap();
    let p = s.store.path(&outside);
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(
        p,
        serde_json::to_vec(&json!({"number":1,"title":"old","state":"open"})).unwrap(),
    )
    .unwrap();
    s.store.save(&r, &mut v).unwrap();
    let before = fs::read(s.store.path(&r)).unwrap();
    for parent in [String::new(), first.target.url.clone()] {
        assert_eq!(
            s.update_ticket(
                &r.url(),
                UpdateTicket {
                    body: Some("should not persist".into()),
                    parent: Some(parent),
                    ..Default::default()
                }
            )
            .await
            .unwrap_err(),
            ServiceError::OutOfScope
        );
        assert_eq!(before, fs::read(s.store.path(&r)).unwrap());
    }
}
#[tokio::test]
async fn draft_lock_times_out_without_blocking_other_local_writes() {
    let (_d, s) = fixture();
    let missing = "local://code/acme/fresh/pulls/1";
    assert_eq!(
        s.convert_pull_request_to_draft(missing).await.unwrap_err(),
        ServiceError::NotFound
    );
    assert!(
        s.store
            .dir(WorkKind::PullRequest, "acme/fresh")
            .join("pulls/1.lock")
            .is_file()
    );
    let url = shared_local_pr(&s, "feature", "main").await;
    let r = Ref::locate(&url, WorkKind::PullRequest).unwrap();
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(s.store.path(&r).with_extension("lock"))
        .unwrap();
    file.lock().unwrap();
    let s = Arc::new(s);
    let other = s.clone();
    let target = url.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    let worker = std::thread::spawn(move || {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(async {
                tx.send(()).unwrap();
                other.convert_pull_request_to_draft(&target).await
            })
    });
    rx.recv().unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    let chat = LocalChat::new(s.store.root.clone(), "local-aiko", "Aiko");
    let (tx, rx) = std::sync::mpsc::channel();
    let posting = std::thread::spawn(move || {
        let result = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(chat.post_message("other", "unrelated", None, None));
        tx.send(result.is_ok()).unwrap();
    });
    let independent = rx.recv_timeout(std::time::Duration::from_millis(500));
    let started = std::time::Instant::now();
    let result = worker.join().unwrap();
    assert!(started.elapsed() < std::time::Duration::from_secs(6));
    assert_eq!(result.unwrap_err(), ServiceError::Failed);
    file.unlock().unwrap();
    posting.join().unwrap();
    assert!(independent.unwrap());
    assert!(s.convert_pull_request_to_draft(&url).await.unwrap());
    assert!(!s.convert_pull_request_to_draft(&url).await.unwrap());
}
#[tokio::test]
async fn chat_malformed_records_cursor_status_and_listener_partial_lines() {
    let (d, _s) = fixture();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    assert_eq!(c.resolve_channel_id("missing").await.unwrap(), None);
    assert_eq!(
        c.list_thread_events("missing", "root", None, 30)
            .await
            .unwrap_err(),
        ServiceError::NotFound
    );
    assert!(
        c.list_channel_events("missing", ChannelEventsQuery::default())
            .await
            .unwrap()
            .events
            .is_empty()
    );
    fs::create_dir_all(d.path().join("chat/unreadable.jsonl")).unwrap();
    assert_eq!(
        c.resolve_channel_id("unreadable").await.unwrap(),
        Some("unreadable".into())
    );
    assert_eq!(
        c.list_thread_events("unreadable", "root", None, 30)
            .await
            .unwrap_err(),
        ServiceError::Failed
    );
    assert_eq!(
        c.list_channel_events("unreadable", ChannelEventsQuery::default())
            .await
            .unwrap_err(),
        ServiceError::Failed
    );
    let calls = Arc::new(AtomicUsize::new(0));
    let copy = calls.clone();
    let diagnostics = Arc::new(std::sync::Mutex::new(Vec::new()));
    let received_diagnostics = diagnostics.clone();
    let listener = LocalListener::new(
        d.path().into(),
        Arc::new(move || {
            copy.fetch_add(1, Ordering::SeqCst);
        }),
        Arc::new(move |event| {
            if let crate::factory::IntegrationEvent::ReceiveRecordSkipped { code, .. } = event {
                received_diagnostics.lock().unwrap().push(code);
            }
        }),
    );
    listener.start().await.unwrap();
    listener.start().await.unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    fs::write(d.path().join("chat/events.jsonl"),b"invalid\n[]\n{\"message_id\":\"bad\",\"occurred_at\":\"2026-01-01\"}\n{\"message_id\":\"good\",\"occurred_at\":\"2026-01-01T00:00:00Z\",\"status\":[]}").unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(180)).await;
    assert!(listener.drain_events().is_empty());
    use std::io::Write;
    fs::OpenOptions::new()
        .append(true)
        .open(d.path().join("chat/events.jsonl"))
        .unwrap()
        .write_all(b"\n")
        .unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(180)).await;
    let events = listener.drain_events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].message.message_id, "good");
    assert!(events[0].status.is_none());
    assert_eq!(
        c.resolve_channel_id("events").await.unwrap(),
        Some("events".into())
    );
    let channel = c
        .list_channel_events(
            "events",
            ChannelEventsQuery {
                cursor: Some(String::new()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(channel.events.len(), 1);
    assert_eq!(channel.events[0].message.message_id, "good");
    assert_eq!(
        c.list_thread_events("events", "good", Some(String::new()), 30)
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    c.add_reaction("events", "good", SemanticReaction::Ack)
        .await
        .unwrap();
    assert_eq!(
        c.list_thread_events("events", "good", None, 30)
            .await
            .unwrap()
            .events
            .len(),
        1
    );
    let records = fs::read_to_string(d.path().join("chat/events.jsonl")).unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(records.lines().last().unwrap()).unwrap(),
        json!({"message_id":"good","reaction":"ack","author":"local-aiko"})
    );
    for cursor in ["bad", "²"] {
        assert_eq!(
            c.list_channel_events(
                "events",
                ChannelEventsQuery {
                    cursor: Some(cursor.into()),
                    ..Default::default()
                }
            )
            .await
            .unwrap_err(),
            ServiceError::InvalidContinuation
        );
        assert_eq!(
            c.list_thread_events("events", "good", Some(cursor.into()), 30)
                .await
                .unwrap_err(),
            ServiceError::InvalidContinuation
        );
    }
    listener.stop().await.unwrap();
    assert!(!listener.connected());
    assert!(!listener.auth_failed());
    {
        let diagnostics = diagnostics.lock().unwrap();
        assert!(diagnostics.contains(&"malformed_line"));
        assert!(diagnostics.contains(&"unreadable_channel"));
    }
    let q = ChannelEventsQuery {
        cursor: Some("bad".into()),
        ..Default::default()
    };
    assert!(c.list_channel_events("events", q).await.is_err());
}
#[test]
fn local_urls_and_scope_reject_all_escape_shapes() {
    for u in [
        "local://tickets/acme/team/T-0",
        "local://tickets/acme/team/T-01",
        "local://tickets/acme/team/T-+1",
        "local://tickets/acme/team/T-10000000000",
        "local://tickets/acme/../T-1",
        "local://tickets/acme/team/T-1?x=1",
        "local://tickets/acme/team/T-1#x",
        "local://code/acme/repo/pulls/-1",
        "local://code/acme/repo/pulls/+1",
        "local://code/acme/repo/pulls/01",
        "local://code/acme/repo/pulls/0",
        "local://code/acme/repo/pulls/10000000000",
    ] {
        let k = if u.contains("code/") {
            WorkKind::PullRequest
        } else {
            WorkKind::Ticket
        };
        assert!(Ref::locate(u, k).is_err(), "{u}");
    }
}
fn git(cwd: &std::path::Path, args: &[&str]) -> String {
    let o = std::process::Command::new("git")
        .current_dir(cwd)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .args(args)
        .output()
        .unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    String::from_utf8(o.stdout).unwrap().trim().into()
}
#[tokio::test]
async fn pull_request_git_unicode_files_reviews_threads_checks_and_artifacts() {
    let (d, s) = fixture();
    let seed = d.path().join("seed");
    fs::create_dir(&seed).unwrap();
    git(&seed, &["init", "-b", "main"]);
    fs::write(seed.join("README.md"), "seed\n").unwrap();
    git(&seed, &["add", "README.md"]);
    git(&seed, &["commit", "-m", "seed"]);
    git(&seed, &["checkout", "-b", "改善"]);
    fs::write(seed.join("設計\n.md"), b"\x93\xfa\x96\x7b\n").unwrap();
    git(&seed, &["add", "."]);
    git(&seed, &["commit", "-m", "feature"]);
    let bare = s.store.bare("acme/repo");
    fs::create_dir_all(bare.parent().unwrap()).unwrap();
    git(&seed, &["clone", "--bare", ".", bare.to_str().unwrap()]);
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/main"]);
    let req = || CreatePullRequest {
        repository: "acme/repo".into(),
        head: "改善".into(),
        base: "".into(),
        title: "PR".into(),
        body: "body".into(),
        draft: false,
        tickets: vec![],
    };
    let pr = s.create_pull_request(req()).await.unwrap();
    assert!(pr.created);
    assert!(!s.create_pull_request(req()).await.unwrap().created);
    assert_eq!(pr.base, "main");
    let url = &pr.target.url;
    let detail = CodeHostingService::read(&s, Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    let head = detail.items()[0]["head_sha"].as_str().unwrap();
    let check_path = s
        .store
        .dir(WorkKind::PullRequest, "acme/repo")
        .join("checks")
        .join(head);
    fs::create_dir_all(check_path.parent().unwrap()).unwrap();
    for directory in [false, true] {
        if directory {
            fs::create_dir(&check_path).unwrap();
        } else {
            fs::write(&check_path, "").unwrap();
        }
        assert_eq!(
            s.readiness(url, false, MAX_LOG_TAIL_BYTES)
                .await
                .unwrap()
                .rollup,
            ReadinessRollup::Success
        );
        if directory {
            fs::remove_dir(&check_path).unwrap();
        } else {
            fs::remove_file(&check_path).unwrap();
        }
    }
    let linked = s.create_ticket(request("acme/team")).await.unwrap();
    s.update_pull_request(
        url,
        UpdatePullRequest {
            tickets: Some(vec![TicketLink {
                url: format!("{}/", linked.target.url),
                closes: true,
            }]),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let links = CodeHostingService::read(
        &s,
        Resource::TicketPullRequests,
        &linked.target.url,
        None,
        "",
    )
    .await
    .unwrap();
    assert_eq!(links.items()[0]["url"], *url);
    let r = Ref::locate(url, WorkKind::PullRequest).unwrap();
    assert_eq!(
        s.store.load(&r).unwrap()["tickets"][0]["url"],
        linked.target.url
    );
    let page = CodeHostingService::read(&s, Resource::PullRequestFiles, url, None, "")
        .await
        .unwrap();
    assert_eq!(page.items()[0]["path"], "設計\n.md");
    assert_eq!(page.items()[0]["additions"], 1);
    assert!(
        page.items()[0]["patch"]
            .as_str()
            .unwrap()
            .contains('\u{fffd}')
    );
    assert_eq!(s.default_branch("acme/repo").await.unwrap(), "main");
    let clone = s.clone_url("acme/repo").await.unwrap();
    assert_eq!(s.repository_from_remote(&clone), Some("acme/repo".into()));
    assert_eq!(s.pull_request_head(url).await.unwrap().branch, "改善");
    assert!(
        s.review(url, "stale", ReviewVerdict::Approve, "bad")
            .await
            .is_err()
    );
    let comment = CodeHostingService::comment(&s, url, "comment", None)
        .await
        .unwrap();
    let inline = s
        .review_comment(
            url,
            ReviewComment {
                body: "inline".into(),
                head: head.into(),
                path: "設計\n.md".into(),
                line: 1,
                side: "RIGHT".into(),
                start_line: None,
                start_side: None,
            },
        )
        .await
        .unwrap();
    assert_eq!(comment.id, "comment-1");
    assert_eq!(inline.id, "inline-2");
    let reply = s.reply(url, &inline.id, "reply").await.unwrap();
    assert_eq!(reply.id, "inline-3");
    assert!(s.reply(url, &reply.id, "bad").await.is_err());
    s.review(url, "approve", ReviewVerdict::Approve, head)
        .await
        .unwrap();
    let reviews = CodeHostingService::read(&s, Resource::PullRequestReviews, url, None, "")
        .await
        .unwrap();
    assert_eq!(reviews.items()[0]["id"], "review-4");
    let threads = CodeHostingService::read(&s, Resource::PullRequestThreads, url, None, "")
        .await
        .unwrap();
    assert_eq!(threads.items()[0]["comments"].as_array().unwrap().len(), 2);
    let thread = CodeHostingService::read(
        &s,
        Resource::ReviewThreadComments,
        url,
        Some(&json!({"node":inline.id,"page_size":1})),
        "",
    )
    .await
    .unwrap();
    assert_eq!(thread.continuation(), Some("1"));
    assert_eq!(thread.items()[0]["reply_to_id"], Value::Null);
    let ready = s.readiness(url, false, MAX_LOG_TAIL_BYTES).await.unwrap();
    assert_eq!(ready.rollup, ReadinessRollup::Success);
    let checks = s
        .store
        .dir(WorkKind::PullRequest, "acme/repo")
        .join("checks");
    fs::create_dir_all(&checks).unwrap();
    fs::write(checks.join(head), "failure").unwrap();
    let artifacts = s
        .store
        .dir(WorkKind::PullRequest, "acme/repo")
        .join("artifacts")
        .join(head);
    fs::create_dir_all(&artifacts).unwrap();
    fs::write(artifacts.join("report.zip"), b"bytes").unwrap();
    let failure = s.readiness(url, true, 10).await.unwrap();
    assert_eq!(failure.rollup, ReadinessRollup::Failure);
    assert_eq!(
        failure.failed_logs.unwrap()[0].artifact_names,
        vec!["report"]
    );
    assert!(s.artifact(url, "../report", 20).await.is_err());
    assert!(s.artifact(url, "report", 2).await.is_err());
    assert_eq!(
        s.artifact(url, "report", 20).await.unwrap().content,
        b"bytes"
    );
    assert!(s.convert_pull_request_to_draft(url).await.unwrap());
    assert!(!s.convert_pull_request_to_draft(url).await.unwrap());
    assert!(s.pull_request_candidates().await.unwrap().is_empty());
    git(&seed, &["checkout", "-b", "contract-feature"]);
    fs::write(seed.join("README.md"), "contract change\n").unwrap();
    git(&seed, &["add", "README.md"]);
    git(&seed, &["commit", "-m", "contract"]);
    git(
        &bare,
        &[
            "fetch",
            seed.to_str().unwrap(),
            "contract-feature:contract-feature",
        ],
    );
}
#[tokio::test]
async fn ticket_search_relations_interactive_and_referenced_attachments() {
    let (_d, s) = fixture();
    let parent = s.create_ticket(request("acme/team")).await.unwrap();
    s.update_ticket(
        &parent.target.url,
        UpdateTicket {
            interactive: Some(true),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let mut child = request("acme/team");
    child.parent = Some(parent.target.url.clone());
    child.body = "local://tickets/acme/team/attachments/report-more".into();
    child.title = "Straße 検索".into();
    child.labels = vec!["bug".into()];
    let child = s.create_ticket(child).await.unwrap();
    let detail = TicketService::read(&s, Resource::Tickets, &child.target.url, None, "")
        .await
        .unwrap();
    assert_eq!(detail.items()[0]["interactive_ancestor"], parent.target.url);
    assert!(TicketService::candidates(&s).await.unwrap().is_empty());
    let p = TicketService::read(&s, Resource::Tickets, &parent.target.url, None, "")
        .await
        .unwrap();
    assert_eq!(p.items()[0]["children"][0]["url"], child.target.url);
    let found = TicketService::read(
        &s,
        Resource::TrackerTickets,
        "acme/team",
        Some(&json!({"text":"STRASSE 検索","label":"bug","page_size":1})),
        "",
    )
    .await
    .unwrap();
    assert_eq!(found.items().len(), 1);
    assert_eq!(found.items()[0]["url"], child.target.url);
    let folder = s
        .store
        .dir(WorkKind::Ticket, "acme/team")
        .join("attachments");
    fs::create_dir_all(&folder).unwrap();
    for id in ["report", "report-more"] {
        fs::write(folder.join(id), b"attachment").unwrap();
        fs::write(
            folder.join(format!("{id}.json")),
            br#"{"name":"name.txt","media_type":"text/plain"}"#,
        )
        .unwrap();
    }
    let attachments =
        TicketService::read(&s, Resource::TicketAttachments, &child.target.url, None, "")
            .await
            .unwrap();
    assert_eq!(attachments.items().len(), 1);
    assert_eq!(attachments.items()[0]["id"], "report-more");
    assert_eq!(attachments.items()[0]["referenced_by"], json!(["body"]));
    assert!(
        s.attachment(&child.target.url, "report", 100)
            .await
            .is_err()
    );
    assert!(
        s.attachment(&child.target.url, "report-more", 2)
            .await
            .is_err()
    );
    assert_eq!(
        s.attachment(&child.target.url, "report-more", 100)
            .await
            .unwrap()
            .content,
        b"attachment"
    );
}
#[tokio::test]
async fn chat_status_inclusive_window_and_participant_text() {
    let (d, _s) = fixture();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    let status = WorkflowStatus::from_payload(
        &json!({"kind":"workflow_error","reason":"failed","person_id":"aiko"}),
    )
    .unwrap();
    let post = c
        .post_message(
            "window",
            "@local-aiko @local-bob @local-aiko",
            None,
            Some(&status),
        )
        .await
        .unwrap();
    let q = ChannelEventsQuery {
        since: Some(post.occurred_at.to_utc()),
        until: Some(post.occurred_at.to_utc()),
        ..Default::default()
    };
    let page = c.list_channel_events("window", q).await.unwrap();
    assert_eq!(page.events.len(), 1);
    assert_eq!(page.events[0].status, Some(status));
    assert_eq!(
        page.events[0].message.mentions,
        vec!["local-aiko", "local-bob"]
    );
    let labels = std::collections::BTreeMap::from([("local-aiko".into(), "Member1".into())]);
    assert_eq!(
        c.normalize_participant_text("@local-aiko @stranger", &labels),
        "@Member1 @participant"
    );
    assert_eq!(
        c.render_participant_text("@Member1 @other", &labels),
        "@local-aiko @other"
    );
}
#[tokio::test]
async fn posts_after_a_future_clock_tick_still_follow_in_order() {
    let (d, _s) = fixture();
    fs::create_dir_all(d.path().join("chat")).unwrap();
    fs::write(
        d.path().join("chat/clock.jsonl"),
        b"{\"message_id\":\"first\",\"occurred_at\":\"2099-01-01T00:00:00Z\",\"text\":\"first\"}\n",
    )
    .unwrap();
    let c = LocalChat::new(d.path().into(), "local-aiko", "Aiko");
    let first = c.post_message("clock", "second", None, None).await.unwrap();
    let second = c.post_message("clock", "third", None, None).await.unwrap();
    assert_eq!(
        second.occurred_at - first.occurred_at,
        chrono::Duration::microseconds(1)
    );
    let all = c
        .list_channel_events("clock", ChannelEventsQuery::default())
        .await
        .unwrap();
    assert_eq!(
        all.events
            .iter()
            .map(|e| e.message.text.as_str())
            .collect::<Vec<_>>(),
        vec!["first", "second", "third"]
    );
}
#[tokio::test]
async fn every_local_write_refuses_other_scope_without_changing_files() {
    let (d, s) = fixture();
    let other = LocalService::new(
        d.path().into(),
        "other",
        "local-bob",
        "bob",
        "Bob",
        "success",
    );
    let folder = other.store.dir(WorkKind::Ticket, "other/team");
    fs::create_dir_all(&folder).unwrap();
    fs::write(
        folder.join("tracker.json"),
        br#"{"labels":[],"repository":"other/repo"}"#,
    )
    .unwrap();
    let t = other.create_ticket(request("other/team")).await.unwrap();
    let url = &t.target.url;
    let c = TicketService::comment(&other, url, "existing", None)
        .await
        .unwrap();
    let r = Ref::locate(url, WorkKind::Ticket).unwrap();
    let before = fs::read(s.store.path(&r)).unwrap();
    assert_eq!(
        s.create_ticket(request("other/team")).await.unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        s.update_ticket(
            url,
            UpdateTicket {
                body: Some("changed".into()),
                ..Default::default()
            }
        )
        .await
        .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        TicketService::comment(&s, url, "changed", None)
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        TicketService::add_reaction(&s, url, &c.id, SemanticReaction::Ack)
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    let task = Task {
        url: Some(url.clone()),
        ..Default::default()
    };
    assert_eq!(
        s.move_ticket(&task, MoveLane::Done).await.unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(before, fs::read(s.store.path(&r)).unwrap());
    let req = |head: &str| CreatePullRequest {
        repository: "other/repo".into(),
        head: head.into(),
        base: "main".into(),
        title: "PR".into(),
        body: "body".into(),
        draft: false,
        tickets: vec![],
    };
    let pr = other.create_pull_request(req("feature")).await.unwrap();
    let url = &pr.target.url;
    let comment = CodeHostingService::comment(&other, url, "existing", None)
        .await
        .unwrap();
    let inline = other
        .review_comment(
            url,
            ReviewComment {
                body: "existing".into(),
                head: "".into(),
                path: "a".into(),
                line: 1,
                side: "RIGHT".into(),
                start_line: None,
                start_side: None,
            },
        )
        .await
        .unwrap();
    let r = Ref::locate(url, WorkKind::PullRequest).unwrap();
    let before = fs::read(s.store.path(&r)).unwrap();
    assert_eq!(
        s.create_pull_request(req("new-feature")).await.unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        s.update_pull_request(
            url,
            UpdatePullRequest {
                body: Some("changed".into()),
                ..Default::default()
            }
        )
        .await
        .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        CodeHostingService::comment(&s, url, "changed", None)
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        CodeHostingService::add_reaction(&s, url, &comment.id, SemanticReaction::Ack)
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        s.review(url, "changed", ReviewVerdict::Approve, "")
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        s.reply(url, &inline.id, "changed").await.unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        s.convert_pull_request_to_draft(url).await.unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(before, fs::read(s.store.path(&r)).unwrap());
    assert!(
        TicketService::read(&s, Resource::Tickets, &t.target.url, None, "")
            .await
            .is_ok()
    );
}
#[test]
fn git_strictly_decodes_machine_output_and_sanitizes_failures() {
    let (_d, s) = fixture();
    let bare = s.store.bare("acme/repo");
    fs::create_dir_all(&bare).unwrap();
    git(&bare, &["init", "--bare"]);
    use std::io::Write;
    fs::OpenOptions::new()
        .append(true)
        .open(bare.join("config"))
        .unwrap()
        .write_all(b"\n[test]\nvalue = \xff\n")
        .unwrap();
    assert_eq!(
        s.store
            .git("acme/repo", &["config", "--local", "--get", "test.value"])
            .unwrap_err(),
        ServiceError::Failed
    );
    let error = s
        .store
        .git("acme/repo", &["show", "does-not-exist"])
        .unwrap_err();
    assert_eq!(error, ServiceError::Failed);
    assert_eq!(error.to_string(), "integrations.resources.service_failed");
}
struct LocalTicketContractDriver<'a> {
    member: &'a LocalService,
    human: LocalService,
}
impl contracts::TicketContractDriver for LocalTicketContractDriver<'_> {
    fn human_comment<'a>(&'a self, url: &'a str, body: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            TicketService::comment(&self.human, url, body, None).await?;
            Ok(())
        })
    }
    fn assign<'a>(&'a self, url: &'a str, actor: &'a ActorName) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let r = Ref::locate(url, WorkKind::Ticket)?;
            let mut v = self.member.store.load(&r)?;
            v["assignees"] = json!([actor]);
            v["lane"] = json!("ready");
            self.member.store.save(&r, &mut v)
        })
    }
    fn merge_reference<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let p = self
                .member
                .create_pull_request(CreatePullRequest {
                    repository: "acme/repo".into(),
                    head: format!(
                        "merged-reference-{}",
                        Ref::locate(url, WorkKind::Ticket)?.name
                    ),
                    base: "main".into(),
                    title: "Merged nonclosing reference".into(),
                    body: "".into(),
                    draft: false,
                    tickets: vec![TicketLink {
                        url: url.into(),
                        closes: false,
                    }],
                })
                .await?;
            let r = Ref::locate(&p.target.url, WorkKind::PullRequest)?;
            let mut v = self.member.store.load(&r)?;
            v["state"] = json!("closed");
            v["merged"] = json!(true);
            v["closed_at"] = json!(now());
            self.member.store.save(&r, &mut v)
        })
    }
}
#[tokio::test]
async fn full_shared_ticket_patrol_contract() {
    let (d, s) = fixture();
    let driver = LocalTicketContractDriver {
        member: &s,
        human: LocalService::new(
            d.path().into(),
            "acme",
            "local-human",
            "human",
            "Human",
            "success",
        ),
    };
    contracts::ticket_patrol_contract(
        &s,
        &driver,
        "acme/team",
        "bug",
        ActorName("local-aiko".into()),
        ActorName("local-human".into()),
    )
    .await;
}
#[tokio::test]
async fn full_shared_ticket_scope_resource_and_attachment_contract() {
    let (d, s) = fixture();
    let outside = LocalService::new(
        d.path().into(),
        "other",
        "local-human",
        "human",
        "Human",
        "success",
    );
    let folder = outside.store.dir(WorkKind::Ticket, "other/team");
    fs::create_dir_all(&folder).unwrap();
    fs::write(
        folder.join("tracker.json"),
        br#"{"labels":[],"repository":"other/repo"}"#,
    )
    .unwrap();
    let theirs = outside
        .create_ticket(contracts::ticket_request("other/team", "Theirs"))
        .await
        .unwrap();
    let comment = TicketService::comment(&outside, &theirs.target.url, "existing", None)
        .await
        .unwrap();
    contracts::ticket_scope_contract(
        &s,
        "acme/team",
        "other/team",
        &theirs.target.url,
        &comment.id,
    )
    .await;
    let p = s
        .create_pull_request(CreatePullRequest {
            repository: "acme/repo".into(),
            head: "wrong-ticket-subject".into(),
            base: "main".into(),
            title: "PR".into(),
            body: "".into(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap();
    let c = CodeHostingService::comment(&s, &p.target.url, "PR comment", None)
        .await
        .unwrap();
    contracts::ticket_wrong_subject_contract(&s, &p.target.url, &c.id).await;
    let folder = s
        .store
        .dir(WorkKind::Ticket, "acme/team")
        .join("attachments");
    fs::create_dir_all(&folder).unwrap();
    for (id, name, media, bytes) in [
        (
            "shared-screen",
            "screen.png",
            "image/png",
            b"\x89PNG\r\n\x1a\nscreen".as_slice(),
        ),
        (
            "shared-log",
            "log.gif",
            "image/gif",
            b"GIF89alog".as_slice(),
        ),
        (
            "shared-unused",
            "unused.png",
            "image/png",
            b"\x89PNG\r\n\x1a\nscreen".as_slice(),
        ),
    ] {
        fs::write(folder.join(id), bytes).unwrap();
        fs::write(
            folder.join(format!("{id}.json")),
            serde_json::to_vec(&json!({"name":name,"media_type":media})).unwrap(),
        )
        .unwrap();
    }
    contracts::ticket_attachment_contract(
        &s,
        "acme/team",
        contracts::TicketAttachmentFixture {
            screen_url: "local://tickets/acme/team/attachments/shared-screen".into(),
            screen_id: "shared-screen".into(),
            log_url: "local://tickets/acme/team/attachments/shared-log".into(),
            log_id: "shared-log".into(),
            unused_id: "shared-unused".into(),
        },
    )
    .await;
}
fn merge_local_fixture(service: &LocalService, url: &str) {
    let r = Ref::locate(url, WorkKind::PullRequest).unwrap();
    let mut p = service.store.load(&r).unwrap();
    p["state"] = json!("closed");
    p["merged"] = json!(true);
    p["closed_at"] = json!(now());
    for link in array(&p, "tickets") {
        if link["closes"] == true {
            let r = Ref::locate(text(&link, "url"), WorkKind::Ticket).unwrap();
            let mut t = service.store.load(&r).unwrap();
            t["state"] = json!("closed");
            t["closed_at"] = json!(now());
            service.store.save(&r, &mut t).unwrap();
        }
    }
    service.store.save(&r, &mut p).unwrap();
}
async fn shared_local_pr(s: &LocalService, head: &str, base: &str) -> String {
    s.create_pull_request(CreatePullRequest {
        repository: "acme/repo".into(),
        head: head.into(),
        base: base.into(),
        title: "Shared states".into(),
        body: "body".into(),
        draft: false,
        tickets: vec![],
    })
    .await
    .unwrap()
    .target
    .url
}
#[tokio::test]
async fn full_shared_code_state_and_link_contracts() {
    let (d, s) = fixture();
    let seed = d.path().join("shared-seed");
    fs::create_dir(&seed).unwrap();
    git(&seed, &["init", "-b", "main"]);
    fs::write(seed.join("README.md"), "seed\n").unwrap();
    git(&seed, &["add", "."]);
    git(&seed, &["commit", "-m", "seed"]);
    let main = git(&seed, &["rev-parse", "HEAD"]);
    git(&seed, &["checkout", "-b", "feature"]);
    fs::write(seed.join("README.md"), "feature\n").unwrap();
    fs::write(seed.join("設計.md"), "日本語の変更\n").unwrap();
    git(&seed, &["add", "."]);
    git(&seed, &["commit", "-m", "feature"]);
    let head = git(&seed, &["rev-parse", "HEAD"]);
    let bare = s.store.bare("acme/repo");
    fs::create_dir_all(bare.parent().unwrap()).unwrap();
    git(&seed, &["clone", "--bare", ".", bare.to_str().unwrap()]);
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/main"]);
    for branch in [
        "feature-other",
        "states",
        "states-wrong-kind-true",
        "states-wrong-kind-false",
        "disposable-base",
    ] {
        git(
            &bare,
            &["update-ref", &format!("refs/heads/{branch}"), &head],
        );
    }
    for i in 0..8 {
        git(
            &bare,
            &[
                "update-ref",
                &format!("refs/heads/feature-links-{i}"),
                &head,
            ],
        );
    }
    git(
        &bare,
        &["update-ref", "refs/heads/ticket-search-decoy", &head],
    );
    contracts::ticket_search_decoy_contract(
        &s,
        &s,
        "acme/team",
        "acme/repo",
        "ticket-search-decoy",
        "main",
    )
    .await;
    git(&bare, &["update-ref", "refs/heads/order-head", &head]);
    let bases = (0..101)
        .map(|i| format!("order-base-{i}"))
        .collect::<Vec<_>>();
    for base in &bases {
        git(&bare, &["update-ref", &format!("refs/heads/{base}"), &main]);
    }
    contracts::code_open_order_contract(&s, "acme/repo", "order-head", &bases, false).await;
    contracts::code_contract(&s, "acme/repo", "feature", "main").await;
    let states = shared_local_pr(&s, "states", "main").await;
    contracts::code_kind_boundary_contract(&s, &s, "acme/team", &states).await;
    let other = LocalService::new(
        d.path().into(),
        "acme",
        "local-bob",
        "bob",
        "Bob",
        "success",
    );
    contracts::code_other_actor_contract(&other, &states).await;
    contracts::code_utf8_files_contract(&s, &states, "設計.md", "日本語の変更").await;
    let artifact = s
        .store
        .dir(WorkKind::PullRequest, "acme/repo")
        .join("artifacts")
        .join(&head);
    fs::create_dir_all(&artifact).unwrap();
    fs::write(artifact.join("report.zip"), b"zip fixture").unwrap();
    contracts::code_artifact_contract(&s, &states, &head, b"zip fixture").await;
    git(&bare, &["update-ref", "refs/heads/states", &main]);
    contracts::code_stale_head_contract(&s, &states, &head).await;
    contracts::code_old_artifact_contract(&s, &states).await;
    let missing = shared_local_pr(&s, "states", "disposable-base").await;
    git(&bare, &["update-ref", "-d", "refs/heads/disposable-base"]);
    git(
        &bare,
        &["update-ref", "refs/heads/disposable-base/next", &head],
    );
    contracts::code_missing_base_contract(&s, &missing).await;
    merge_local_fixture(&s, &states);
    contracts::code_closed_contract(&s, &states).await;
    let cases =
        contracts::code_link_population(&s, &s, "acme/repo", "acme/team", "feature", "main").await;
    for case in cases {
        merge_local_fixture(&s, &case.url);
        contracts::code_link_case_finished(&s, &case).await;
    }
    let folder = s.store.dir(WorkKind::Ticket, "acme/different");
    fs::create_dir_all(&folder).unwrap();
    fs::write(
        folder.join("tracker.json"),
        br#"{"labels":[],"repository":"acme/different-repo"}"#,
    )
    .unwrap();
    let foreign = s
        .create_ticket(contracts::ticket_request(
            "acme/different",
            "Other repository",
        ))
        .await
        .unwrap();
    contracts::code_cross_repo_link_contract(&s, &missing, &foreign.target.url).await;
}
