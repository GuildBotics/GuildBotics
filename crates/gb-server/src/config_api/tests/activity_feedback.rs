use super::*;

const START: &str = "2026-07-01T00:00:00Z";
const END: &str = "2026-07-02T00:00:00Z";
const HISTORY: &str = "/activity/history?start=2026-07-01T00:00:00Z&end=2026-07-02T00:00:00Z";

fn run(f: &Fixture, id: &str, attrs: Value) {
    let writes =
        gb_base::shared_writes::SharedWrites::new(f.root.join("identity"), Some(f.workspace()));
    let store = gb_observability::runs::RunStore::open(&f.workspace());
    let record = serde_json::from_value(json!({"run_id":id,"work_kind":"chat","execution_mode":"autonomous","member_id":"alice","device_id":"device","source":"event_listener","started_at":START,"attributes":attrs,"provider_evidence":[{"type":"chat_reply"}]})).unwrap();
    writes
        .transaction(|w| store.start(w, record, &Default::default()))
        .unwrap();
}
fn diagnostic(f: &Fixture, id: &str, stamp: &str, mut record: Value) {
    record["kind"] = "event".into();
    record["trace_id"] = id.into();
    record["timestamp"] = stamp.into();
    record["person_id"] = "alice".into();
    gb_observability::Diagnostics::open(&f.workspace()).record(record.as_object().unwrap().clone());
}
fn session<'a>(history: &'a Value, id: &str) -> &'a Value {
    history["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["trace_id"] == id)
        .unwrap()
}
fn links(value: &Value) -> Vec<Value> {
    value["links"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| json!([l["kind"], l["label"], l["url"]]))
        .collect()
}

#[tokio::test]
async fn reset_uses_supported_canonical_provider_and_stamps_cutoffs() {
    for (configured, canonical) in [
        ("Slack", Some("slack")),
        ("LOCAL", Some("local")),
        ("branvil", None),
        ("unknown", None),
        ("", None),
    ] {
        let f = fixture();
        f.init_with(json!({"language":"en","services":{"chat_service":{"name":"local"}}}))
            .await;
        let mut member = agent("alice");
        member["chat_channels"] = json!(["C01234567"]);
        f.add(member).await;
        // The frozen fixture constructs its team directly. Persist the provider
        // after setup so unsupported names reach reset rather than setup validation.
        let project_path = f.config().join("team/project.yml");
        let mut project = f.read(&project_path);
        project["services"]["chat_service"]["name"] = configured.into();
        f.write(&project_path, &serde_saphyr::to_string(&project).unwrap());
        assert_eq!(
            f.read(&project_path)["services"]["chat_service"]["name"],
            configured
        );
        let provider = canonical.unwrap_or(configured);
        let root = f
            .workspace()
            .join(format!(".guildbotics/state/chat_state/{provider}/alice"));
        f.write(
            &root.join("pending_events/C01234567.json"),
            r#"{"events":[{"old":"message"}]}"#,
        );
        f.write(
            &root.join("channels/C01234567.json"),
            r#"{"processed_event_ids":["old"],"watermark":"old"}"#,
        );
        let before = bytes_under(&f.workspace().join(".guildbotics/state"));
        let (status, result) = f
            .call("POST", "/chat/receive-state/reset", Value::Null)
            .await;
        assert_eq!(status, StatusCode::OK, "{configured}: {result}");
        if canonical.is_some() {
            assert_eq!(result, json!({"members_reset":1,"channels_reset":1}));
            assert!(!root.join("pending_events/C01234567.json").exists());
            let cutoff = f.read(&root.join("receive_cutoff.json"));
            assert_eq!(
                cutoff["schema_version"],
                gb_base::files::SHARED_RECORD_SCHEMA_VERSION
            );
            assert!(
                chrono::DateTime::parse_from_rfc3339(cutoff["cutoff"].as_str().unwrap()).is_ok()
            );
            assert_eq!(
                f.read(&root.join("channels/C01234567.json")),
                json!({"processed_event_ids":["old"],"watermark":"old"})
            );
        } else {
            assert_eq!(result, json!({"members_reset":0,"channels_reset":0}));
            assert_eq!(
                before,
                bytes_under(&f.workspace().join(".guildbotics/state"))
            );
        }
    }
}

#[tokio::test]
async fn history_real_target_push_chat_and_memory_writers_reach_http_titles_and_exact_links() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    *f.api.activity_runner.lock().unwrap() = Some(Arc::new(|_, _| Ok(())));
    for (id, kind, label, title, url) in [
        (
            "ticket",
            "ticket",
            "T-1",
            "Local ticket",
            "local://tickets/T-1",
        ),
        (
            "pr",
            "pull_request",
            "!3",
            "Local pull request",
            "local://code/pulls/3",
        ),
    ] {
        run(&f, id, json!({}));
        diagnostic(
            &f,
            id,
            START,
            gb_member::events::inspected(
                &json!({"provider":"local","kind":kind,"label":label,"title":title,"url":url}),
                false,
            ),
        );
    }
    let attrs = json!({"chat.provider":"local","chat.channel":"channel-A","chat.thread":"thread-B","event_id":"event-C"});
    for id in ["push", "chat"] {
        run(&f, id, attrs.clone());
        diagnostic(
            &f,
            id,
            START,
            json!({"type":"workflow.completed","attributes":attrs}),
        );
    }
    let writes =
        gb_base::shared_writes::SharedWrites::new(f.root.join("identity"), Some(f.workspace()));
    let events = gb_observability::activity::ActivityEvents::open(&f.workspace());
    let mut push = gb_member::events::pushed(&json!({"pushed":true,"branch":"feature","commits":[{"id":"abc1234","message":"Local commit","url":"local://code/commits/abc1234"}]}), "local").unwrap();
    push["timestamp"] = START.into();
    push["trace_id"] = "push".into();
    push["person_id"] = "alice".into();
    writes
        .transaction(|w| events.record(w, &push, &Default::default()))
        .unwrap();
    diagnostic(&f, "push", START, push);
    run(&f, "memory", json!({}));
    let redactor = gb_observability::redaction::Redactor::default();
    let workspace = f.workspace();
    let memory = gb_member::memory::Memory {
        writes: &writes,
        workspace: &workspace,
        person_id: "alice",
        device_id: "device".into(),
        autonomous: false,
        correlation: json!({"trace_id":"memory"}),
        audit_sink: None,
        redactor: &redactor,
    };
    let document = memory
        .record(
            gb_member::memory::Scope::Personal,
            gb_member::memory::Edit {
                title: Some("Saved lesson".into()),
                body: Some("Reusable lesson".into()),
                ..Default::default()
            },
            false,
        )
        .unwrap();
    // Memory's real writer owns its clock; select its recorded second as a closed window edge.
    let audit = crate::diagnostics::memory_records(&workspace);
    assert_eq!(audit.len(), 1);
    let stamp = audit[0]["timestamp"].as_str().unwrap();
    let history = f.get(HISTORY).await;
    for (id, kind, label, url, title) in [
        (
            "ticket",
            "ticket",
            "Ticket T-1",
            "local://tickets/T-1",
            "Local ticket",
        ),
        (
            "pr",
            "pull_request",
            "PR !3",
            "local://code/pulls/3",
            "Local pull request",
        ),
    ] {
        assert_eq!(session(&history, id)["title"], title);
        assert_eq!(
            links(session(&history, id)),
            vec![json!([kind, label, url])]
        );
    }
    assert_eq!(
        links(session(&history, "push")),
        vec![json!([
            "commit",
            "Local commit",
            "local://code/commits/abc1234"
        ])]
    );
    let chat_title = crate::i18n::t(
        "en",
        "observability.trace_title.chat_trigger",
        &[("provider", "Local")],
    );
    assert_eq!(session(&history, "chat")["title"], chat_title);
    let traces = f.get("/diagnostics/traces").await;
    for id in ["ticket", "pr", "chat"] {
        let trace = traces["traces"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["trace_id"] == id)
            .unwrap();
        assert_eq!(session(&history, id)["title"], trace["title"]);
    }
    let uri = format!("/activity/history?start={stamp}&end={stamp}");
    let memory_history = f.get(&uri).await;
    let memory_session = session(&memory_history, "memory");
    assert_eq!(memory_session["title"], "Saved lesson");
    let memory_links = links(memory_session);
    assert_eq!(memory_links.len(), 1);
    assert_eq!(memory_links[0][0], "doc");
    assert_eq!(memory_links[0][1], "Saved lesson");
    assert!(
        memory_links[0][2]
            .as_str()
            .unwrap()
            .contains(document["doc_id"].as_str().unwrap())
    );
    // Prove the screen reads shared audit files even with no local diagnostics.
    std::fs::remove_file(workspace.join(".guildbotics/local/run/diagnostics.jsonl")).unwrap();
    std::fs::create_dir(workspace.join(".guildbotics/local/run/diagnostics.jsonl")).unwrap();
    let without_diagnostics = f.get(&uri).await;
    assert_eq!(links(session(&without_diagnostics, "memory")), memory_links);
    f.api.work.close();
    f.api.work.wait().await;
}

#[tokio::test]
async fn history_targets_are_start_inclusive_end_exclusive_while_shared_facts_are_inclusive() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    *f.api.activity_runner.lock().unwrap() = Some(Arc::new(|_, _| Ok(())));
    for (id, stamp) in [("start", START), ("end", END)] {
        run(&f, id, json!({}));
        diagnostic(
            &f,
            id,
            stamp,
            gb_member::events::inspected(
                &json!({"kind":"ticket","label":"T-1","title":"Boundary title","url":"local://tickets/T-1"}),
                false,
            ),
        );
    }
    let writes =
        gb_base::shared_writes::SharedWrites::new(f.root.join("identity"), Some(f.workspace()));
    let events = gb_observability::activity::ActivityEvents::open(&f.workspace());
    writes.transaction(|w| {
        for stamp in [START,END] { events.record(w,&json!({"type":"ticket.item","timestamp":stamp,"person_id":"alice","payload":{"action":"opened","ticket":{"label":"T-2","title":"Shared boundary","url":"local://tickets/T-2"}}}),&Default::default())?; }
        Ok(())
    }).unwrap();
    let history = f.get(HISTORY).await;
    assert_eq!(session(&history, "start")["title"], "Boundary title");
    assert_eq!(
        links(session(&history, "start")),
        vec![json!(["ticket", "Ticket T-1", "local://tickets/T-1"])]
    );
    assert_ne!(session(&history, "end")["title"], "Boundary title");
    assert!(links(session(&history, "end")).is_empty());
    assert_eq!(history["events"].as_array().unwrap().len(), 2);
    f.api.work.close();
    f.api.work.wait().await;
}

#[tokio::test]
async fn history_unreadable_display_roots_and_entries_preserve_healthy_sibling_sources() {
    for broken in [
        ".guildbotics/local/run/diagnostics.jsonl",
        ".guildbotics/state/events",
        ".guildbotics/state/task-runs",
        ".guildbotics/state/sessions",
    ] {
        let f = fixture();
        f.init().await;
        f.add(agent("alice")).await;
        *f.api.activity_runner.lock().unwrap() = Some(Arc::new(|_, _| Ok(())));
        if !broken.ends_with("task-runs") {
            run(&f, "healthy", json!({"target.title":"Healthy run"}));
        }
        if broken.ends_with("diagnostics.jsonl") {
            std::fs::create_dir_all(f.workspace().join(broken)).unwrap();
        } else {
            f.write(&f.workspace().join(broken), "not a directory");
        }
        // Healthy interactive sibling remains visible even if the run or event store fails.
        if !broken.ends_with("sessions") {
            f.write(&f.workspace().join(".guildbotics/state/sessions/healthy-session.json"),&json!({"trace_id":"healthy-session","person_id":"alice","command":"member","started_at":START,"last_seen_at":END}).to_string());
            std::fs::create_dir_all(
                f.workspace()
                    .join(".guildbotics/state/sessions/unreadable.json"),
            )
            .unwrap();
            f.write(
                &f.workspace()
                    .join(".guildbotics/state/sessions/broken.json"),
                "bad json",
            );
        }
        if !broken.ends_with("task-runs") {
            std::fs::create_dir_all(
                f.workspace()
                    .join(".guildbotics/state/task-runs/bad/result.json"),
            )
            .unwrap();
        }
        let (status, history) = f.call("GET", HISTORY, Value::Null).await;
        assert_eq!(status, StatusCode::OK, "{broken}: {history}");
        if !broken.ends_with("task-runs") {
            assert_eq!(session(&history, "healthy")["title"], "Healthy run");
        }
        if !broken.ends_with("sessions") {
            assert_eq!(
                session(&history, "healthy-session")["trace_id"],
                "healthy-session"
            );
        }
        if broken.ends_with("task-runs") {
            assert!(
                gb_observability::runs::RunStore::open(&f.workspace())
                    .records()
                    .is_err()
            );
        }
        f.api.work.close();
        f.api.work.wait().await;
    }
}
