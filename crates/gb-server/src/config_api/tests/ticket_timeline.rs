//! The ticket timeline joins the shared runs and conversations to the code
//! host's pull requests, reviews and CI attempts.
use super::*;
use branvil::test_support::input;
use gb_observability::{
    interactive::{InteractiveSessions, SessionCommand},
    redaction::Redactor,
    runs::{Completion, RunCompletion, RunFinish, RunRecord, RunResult, RunState, RunStore},
};

fn run(id: &str, member: &str, target: &str, kind: &str, started: &str) -> RunRecord {
    serde_json::from_value(
        json!({"run_id":id,"work_kind":"ticket","execution_mode":"autonomous",
        "member_id":member,"device_id":"device","source":"routine","started_at":started,
        "attributes":{"target.url":target,"target.kind":kind,"target.label":id}}),
    )
    .unwrap()
}

#[tokio::test]
async fn a_tickets_runs_reviews_and_attempts_are_bars_and_its_waits_are_left_open() {
    let f = fixture();
    f.init().await;
    for id in ["alice", "bob"] {
        f.add(agent(id)).await;
    }
    let hub = branvil::test_support::Fixture::new("owner").await;
    hub.workspace("acme").await;
    hub.repository("acme/demo").await;
    f.api.branvil.set(hub.data()).ok().unwrap();
    let ticket = hub
        .ticket(
            "acme/demo",
            json!({"title":"Timeline"}),
            None,
            "in_progress",
        )
        .await;
    let other = hub
        .ticket("acme/demo", json!({"title":"Talked about"}), None, "ready")
        .await;
    let touched = hub
        .ticket("acme/demo", json!({"title":"Only touched"}), None, "ready")
        .await;
    let main = hub.commit("acme/demo", "main", None, "a.txt", "main");
    let first = hub.commit_check_pipeline("acme/demo", "ticket/1", Some(&main));
    let alice = hub.member("alice").await;
    let pull = hub
        .pull(
            &alice,
            "acme/demo",
            json!({"head":"ticket/1","title":"Timeline","tickets":[{"url":ticket,"closes":true}]}),
        )
        .await;
    hub.check("acme/demo", "ticket/1", &first, Some("failure"))
        .await;
    let second = hub.commit("acme/demo", "ticket/1", Some(&first), "b.txt", "fix");
    hub.git("acme/demo", &["update-ref", "refs/heads/ticket/1", &second]);
    let second = hub.commit_check_pipeline("acme/demo", "ticket/1", Some(&second));
    hub.check("acme/demo", "ticket/1", &second, None).await;
    hub.member("bob")
        .await
        .pulls_review(
            "acme/demo",
            1,
            input(json!({"verdict":"approve","body":"ok","commit":second})),
        )
        .await
        .unwrap();
    // A ticket whose only movement is its CI: no session works on it.
    let ci_only = hub
        .ticket("acme/demo", json!({"title":"CI only"}), None, "ready")
        .await;
    let ci_head = hub.commit_check_pipeline("acme/demo", "ticket/4", Some(&main));
    hub.pull(
        &alice,
        "acme/demo",
        json!({"head":"ticket/4","title":"CI only","tickets":[{"url":ci_only,"closes":true}]}),
    )
    .await;
    hub.check("acme/demo", "ticket/4", &ci_head, Some("success"))
        .await;
    // A ticket whose CI only waited in the queue: no runner ran anything.
    let queued = hub
        .ticket("acme/demo", json!({"title":"Queued only"}), None, "ready")
        .await;
    let queued_head = hub.commit_check_pipeline("acme/demo", "ticket/5", Some(&main));
    hub.pull(
        &alice,
        "acme/demo",
        json!({"head":"ticket/5","title":"Queued only","tickets":[{"url":queued,"closes":true}]}),
    )
    .await;
    hub.check("acme/demo", "ticket/5", &queued_head, None).await;
    let owner = hub.data().owner();
    owner
        .tickets_add_comment("acme/demo", "1", input(json!({"body":"Go ahead"})))
        .await
        .unwrap();
    // The owner closes the ticket only touched: no bar of it, but closed today.
    owner
        .tickets_put_on_board("acme/demo", "3", input(json!({"lane":"done"})))
        .await
        .unwrap();
    owner
        .assign_execution_root("acme".into(), f.workspace())
        .await
        .unwrap();

    // Whole seconds: the shared records keep microseconds.
    let now = chrono::DateTime::from_timestamp(chrono::Utc::now().timestamp(), 0).unwrap();
    // After the pull request opened: the ticket's run worked on it.
    let at = |minutes: i64| (now + chrono::Duration::minutes(minutes)).to_rfc3339();
    let (stopped, done) = (at(20), at(55));
    // Two days before, before its work started: planning, not work waited on.
    let (before, before_end) = (at(-2 * 24 * 60), at(-2 * 24 * 60 + 15));
    let workspace = f.workspace();
    let writes =
        gb_base::shared_writes::SharedWrites::new(f.root.join("identity"), Some(workspace.clone()));
    let (runs, redactor) = (RunStore::open(&workspace), Redactor::default());
    writes
        .transaction(|w| {
            let finish = |state, now| RunFinish {
                state,
                summary: "",
                attributes: BTreeMap::new(),
                now,
            };
            runs.start(
                w,
                run("earlier", "alice", &ticket, "ticket", &before),
                &redactor,
            )?;
            runs.finish(
                w,
                "earlier",
                finish(RunState::Succeeded, &before_end),
                &redactor,
            )?;
            runs.start(
                w,
                run("work", "alice", &ticket, "ticket", &at(10)),
                &redactor,
            )?;
            runs.finish(w, "work", finish(RunState::Deferred, &stopped), &redactor)?;
            // Resumed under the same record: the 20 minutes between are a wait.
            runs.start(
                w,
                run("work", "alice", &ticket, "ticket", &at(40)),
                &redactor,
            )?;
            let result = |subject: &str, status| RunResult {
                subject_type: "ticket".into(),
                subject_id: subject.into(),
                subject_url: String::new(),
                status,
                waiting_for: vec![],
            };
            runs.record_completion(
                w,
                "work",
                RunCompletion {
                    result: result("1", Completion::Asking),
                    member_id: "alice",
                    summary: "asked",
                    now: &at(50),
                },
                &redactor,
            )?;
            runs.start(
                w,
                run("review", "bob", &pull, "pull_request", &at(52)),
                &redactor,
            )?;
            runs.finish(w, "review", finish(RunState::Succeeded, &done), &redactor)?;
            // The ticket's run commented on another ticket on the way.
            gb_observability::activity::ActivityEvents::open(&workspace).record(
                w,
                &json!({"type":"ticket.comment","trace_id":"work","person_id":"alice",
                    "timestamp":at(15),"attributes":{"target.url":touched,"target.kind":"ticket"}}),
                &redactor,
            )?;
            // Still going on since two days ago, with nothing recorded since:
            // its last event is before the window, yet it runs in it.
            runs.start(
                w,
                run("running", "alice", &other, "ticket", &at(-2 * 24 * 60 - 5)),
                &redactor,
            )?;
            // One conversation that worked on both tickets.
            let session = InteractiveSessions::open(&workspace).start_or_touch(
                w,
                "bob",
                "host",
                "thread",
                now + chrono::Duration::minutes(56),
                chrono::Duration::minutes(30),
            )?;
            // From the ticket on to the other one.
            for (target, minute) in [(&ticket, 57), (&other, 58), (&other, 59)] {
                InteractiveSessions::open(&workspace).record(
                    w,
                    &session,
                    SessionCommand {
                        command: "member ticket comment",
                        status: "succeeded",
                        attributes: [
                            ("target.url".into(), target.clone()),
                            ("target.kind".into(), "ticket".into()),
                        ]
                        .into(),
                        now: &at(minute),
                    },
                    &redactor,
                )?;
            }
            Ok(())
        })
        .unwrap();

    let query = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("start", &(now - chrono::Duration::days(1)).to_rfc3339())
        .append_pair("end", &(now + chrono::Duration::days(1)).to_rfc3339())
        .finish();
    let timeline = f.get(&format!("/ticket-timeline?{query}")).await;
    let tickets = timeline["tickets"].as_array().unwrap();
    assert_eq!(
        timeline["summary"],
        json!({"closed_tickets":1,"merged_pulls":0}),
        "{timeline}"
    );
    assert_eq!(
        tickets.iter().map(|t| &t["url"]).collect::<Vec<_>>(),
        // In the order their work began: the running run began first. The
        // ticket whose CI only queued did not move.
        [&json!(other), &json!(ticket), &json!(ci_only)],
        "{timeline}"
    );
    let by_url = |url: &str| tickets.iter().find(|t| t["url"] == url).unwrap();
    let ci_ticket = by_url(&ci_only);
    let ci_pull = &ci_ticket["pulls"][0];
    assert_eq!(ci_pull["jobs"][0]["attempts"][0]["sha"], ci_head);
    assert!(ci_ticket["work"].as_array().unwrap().is_empty());
    let first_ticket = by_url(&ticket);
    assert_eq!(first_ticket["label"], "#1");
    // Its work started when it moved into progress; what ran on it before
    // that is not what its first wait follows.
    assert!(first_ticket["started_at"].is_string(), "{first_ticket}");
    assert!(first_ticket["previous"].is_null(), "{first_ticket}");
    // The other ticket was never started.
    assert!(by_url(&other)["started_at"].is_null(), "{}", by_url(&other));
    let pr = &first_ticket["pulls"][0];
    assert_eq!(pr["url"], pull);
    assert_eq!(pr["person_id"], "alice");
    // The resumed run is two bars: the wait between them is open.
    let work = pr["work"].as_array().unwrap();
    assert_eq!(work.len(), 2, "{pr}");
    assert_eq!(
        (
            parse(&work[0]["ended_at"]),
            parse(&work[1]["started_at"]),
            &work[0]["outcome"],
            &work[1]["outcome"]
        ),
        (
            parse(&json!(at(20))),
            parse(&json!(at(40))),
            &json!("deferred"),
            &json!("asking")
        )
    );
    let reviews = pr["reviews"].as_array().unwrap();
    assert_eq!(reviews.len(), 1);
    assert_eq!(reviews[0]["person_id"], "bob");
    // Every head's attempt, each with the time it began to wait.
    let jobs = pr["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 1, "{pr}");
    let attempts = jobs[0]["attempts"].as_array().unwrap();
    assert_eq!(
        attempts.iter().map(|a| &a["sha"]).collect::<Vec<_>>(),
        [&json!(first), &json!(second)]
    );
    assert!(attempts.iter().all(|a| a["created_at"].is_string()));
    assert!(attempts[1]["started_at"].is_null());
    let kinds: Vec<_> = first_ticket["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| {
            (
                e["kind"].as_str().unwrap(),
                e["person_id"].as_str().unwrap(),
            )
        })
        .collect();
    for expected in [
        ("comment", "owner"),
        ("review", "bob"),
        ("pull_created", "alice"),
    ] {
        assert!(kinds.contains(&expected), "{kinds:?}");
    }
    // The reviewer's conversation is on both tickets, outside the pull request,
    // each for its part: the ticket from the start, the other one after it.
    for (t, (first, last)) in [(by_url(&ticket), (56, 57)), (by_url(&other), (57, 59))] {
        let work = t["work"].as_array().unwrap();
        let talk: Vec<_> = work.iter().filter(|b| b["mode"] == "interactive").collect();
        assert_eq!(talk.len(), 1, "{t}");
        assert_eq!(
            (parse(&talk[0]["started_at"]), parse(&talk[0]["ended_at"])),
            (parse(&json!(at(first))), parse(&json!(at(last))))
        );
    }
    // A run still going on runs until now, not until the last thing it recorded.
    let running: Vec<_> = by_url(&other)["work"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|b| b["mode"] == "workflow")
        .collect();
    assert_eq!(running.len(), 1, "{}", by_url(&other));
    assert_eq!(running[0]["running"], true);
    assert!(
        parse(&running[0]["ended_at"]) >= parse(&json!(at(-1))),
        "{}",
        running[0]
    );
    // Later, a window with only the merge of the CI-only ticket's pull request.
    let late = chrono::Utc::now();
    hub.merge("acme/demo", 2, &ci_head, &main).await;
    let query = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("start", &late.to_rfc3339())
        .append_pair("end", &(late + chrono::Duration::days(1)).to_rfc3339())
        .finish();
    let later = f.get(&format!("/ticket-timeline?{query}")).await;
    assert_eq!(later["summary"]["merged_pulls"], 1, "{later}");
    assert!(
        later["tickets"]
            .as_array()
            .unwrap()
            .iter()
            .any(|t| t["url"] == ci_only && t["pulls"][0]["merged_at"].is_string()),
        "{later}"
    );
    hub.stop().await;
}

fn parse(value: &Value) -> chrono::DateTime<chrono::FixedOffset> {
    chrono::DateTime::parse_from_rfc3339(value.as_str().unwrap()).unwrap()
}
