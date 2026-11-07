use super::*;

#[tokio::test]
async fn team_failures_keep_json_error_shape_security_headers_and_cross_origin_preflights() {
    let f = fixture();
    f.init().await;
    f.write(&f.config().join("team/project.yml"), "[invalid");
    let response = f
        .respond(f.request("GET", "/team").body(HttpBody::empty()).unwrap())
        .await;
    assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
    for (key, value) in [
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("x-frame-options", "DENY"),
    ] {
        assert_eq!(response.headers()[key], value);
    }
    assert!(response.headers().contains_key("content-security-policy"));
    assert!(
        !response
            .headers()
            .keys()
            .any(|h| h.as_str().starts_with("access-control-"))
    );
    let (_, body) = answer(response).await;
    assert_eq!(
        body,
        json!({"code":"internal_error","message":crate::i18n::t("en","app_api.errors.internal_error",&[]),"context":{"error_type":"RuntimeError"}})
    );
    let error = gb_config::ConfigFile::wrap(
        "project.yml",
        std::io::Error::new(std::io::ErrorKind::NotFound, "missing").into(),
    );
    let (status, body) = answer(crate::screens::answer(Err(error), &HeaderMap::new())).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "config_not_found");
    assert_eq!(body["context"], json!({"path":"project.yml"}));
    for origin in [
        "tauri://localhost",
        "http://tauri.localhost",
        "http://localhost:1420",
        "http://localhost:9999",
    ] {
        for path in ["/api/team", "/api/events"] {
            let response = f
                .respond(
                    Request::builder()
                        .method("OPTIONS")
                        .uri(path)
                        .header("host", "127.0.0.1:8765")
                        .header("origin", origin)
                        .body(HttpBody::empty())
                        .unwrap(),
                )
                .await;
            assert!(
                !response
                    .headers()
                    .keys()
                    .any(|h| h.as_str().starts_with("access-control-"))
            );
            let (status, body) = answer(response).await;
            assert_eq!(status, StatusCode::FORBIDDEN);
            assert_eq!(body["code"], "origin_mismatch");
        }
    }
}

#[tokio::test]
async fn usage_reader_disconnect_and_logout_do_not_orphan_the_probe_on_shutdown() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let f = fixture();
    f.init().await;
    f.claude_logged_in();
    let calls = Arc::new(AtomicUsize::new(0));
    let release = tokio_util::sync::CancellationToken::new();
    let called = calls.clone();
    let released = release.clone();
    *f.api.usage_runner.lock().unwrap() = Some(Arc::new(move |cancelled| {
        called.fetch_add(1, Ordering::SeqCst);
        let released = released.clone();
        Box::pin(async move {
            tokio::select! {_=released.cancelled()=>Ok(json!({"account":"same"})),_=cancelled.cancelled()=>Err("cancelled".into())}
        })
    }));
    let uri = "/intelligences/cli-agents/claude/usage?refresh=true";
    assert!(
        tokio::time::timeout(Duration::from_millis(200), f.get(uri))
            .await
            .is_err()
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(
        tokio::time::timeout(Duration::from_millis(200), f.get(uri))
            .await
            .is_err()
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    release.cancel();
    f.api.work.close();
    f.api.work.wait().await;
    assert_eq!(
        f.get("/intelligences/cli-agents/claude/usage").await["usage"]["account"],
        "same"
    );
    let called = calls.clone();
    *f.api.usage_runner.lock().unwrap() = Some(Arc::new(move |cancelled| {
        called.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move {
            cancelled.cancelled().await;
            Ok(json!({"account":"retired"}))
        })
    }));
    assert!(
        tokio::time::timeout(Duration::from_millis(200), f.get(uri))
            .await
            .is_err()
    );
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    std::fs::remove_file(f.data().join("agent_environment/claude/login.sealed")).unwrap();
    assert!(f.get("/intelligences/cli-agents/claude/usage").await["usage"].is_null());
    f.api.cancelled.cancel();
    tokio::time::timeout(Duration::from_secs(2), f.api.work.wait())
        .await
        .unwrap();
    assert!(f.get("/intelligences/cli-agents/claude/usage").await["usage"].is_null());
}

#[tokio::test]
async fn service_source_combinations_publish_transitions_once_and_stop_keeps_http_alive() {
    for (scheduled, routine, event_queue) in [
        (true, false, false),
        (false, true, false),
        (false, false, true),
        (true, true, true),
    ] {
        let (f, selected) = running();
        let settings = json!({"scheduled":scheduled,"routine":routine,"event_queue":event_queue});
        let (status, _) = f.call("PATCH", "/service/settings", settings).await;
        assert_eq!(status, StatusCode::OK);
        for _ in 0..2 {
            let (status, view) = f.call("POST", "/scheduler/start", Value::Null).await;
            assert_eq!(status, StatusCode::OK, "{view}");
            assert_eq!(view["scheduler"]["running"], true);
            assert_eq!(view["scheduler"]["scheduled_source_enabled"], scheduled);
            assert_eq!(view["scheduler"]["routine_source_enabled"], routine);
            assert_eq!(view["scheduler"]["event_queue_source_enabled"], event_queue);
        }
        assert_eq!(f.events("scheduler.running").len(), 1);
        assert_eq!(f.events("scheduler.starting").len(), 1);
        assert_eq!(f.events("events.starting").len(), usize::from(event_queue));
        for force in [false, true] {
            let (status, view) = f
                .call("POST", "/scheduler/stop", json!({"force":force}))
                .await;
            assert_eq!(status, StatusCode::OK);
            assert_eq!(view["scheduler"]["running"], false);
        }
        assert_eq!(f.events("scheduler.stopping").len(), 1);
        assert_eq!(f.events("scheduler.stopped").len(), 1);
        assert_eq!(f.get("/health").await["status"], "ok");
        assert!(!selected.service.service.status().await.running);
        assert!(f.events("scheduler.running")[0]["attributes"]["service_run_id"].is_string());
    }
}

#[tokio::test]
async fn history_refresh_imports_closed_board_work_once_without_an_agent_session() {
    let (f, selected) = running();
    let runtime = selected
        .service
        .workflow("aiko", tokio_util::task::TaskTracker::new())
        .unwrap();
    let tickets = runtime.tickets.unwrap();
    f.write(
        &f.workspace()
            .join(".guildbotics/local/services/tickets/acme%2Fteam/tracker.json"),
        r#"{"labels":[],"repository":"acme/repo"}"#,
    );
    let ticket = tickets
        .create_ticket(gb_ports::CreateTicket {
            tracker: "acme/team".into(),
            title: "Closed work".into(),
            body: String::new(),
            labels: vec![],
            add_to_board: true,
            interactive: false,
            assignee: None,
            lane: None,
            parent: None,
            blocked_by: vec![],
        })
        .await
        .unwrap();
    tickets
        .update_ticket(
            &ticket.target.url,
            gb_ports::UpdateTicket {
                state: Some("closed".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let start = (chrono::Utc::now() - chrono::Duration::days(1)).to_rfc3339();
    let end = (chrono::Utc::now() + chrono::Duration::days(1)).to_rfc3339();
    let query = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("start", &start)
        .append_pair("end", &end)
        .append_pair("refresh", "true")
        .finish();
    let uri = format!("/activity/history?{query}");
    for _ in 0..2 {
        f.get(&uri).await;
        f.api.work.close();
        f.api.work.wait().await;
    }
    let history = f
        .get(&format!(
            "/activity/history?{}",
            query.replace("refresh=true", "refresh=false")
        ))
        .await;
    let events = history["events"].as_array().unwrap();
    assert_eq!(events.len(), 1, "{history}");
    assert_eq!(events[0]["type"], "issue_resolve");
    assert_eq!(events[0]["url"], ticket.target.url);
    assert_eq!(events[0]["detail"], "Closed work");
    assert!(history["sessions"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn app_consent_requires_owner_or_session_csrf_and_uses_public_pkce_exchange() {
    let f = fixture();
    let form = json!({"client":"Example CLI","redirect_uri":"http://127.0.0.1:12345/callback","state":"state","code_challenge":loopback_auth::b64(&loopback_auth::sha256(b"test-verifier"))});
    let request = Request::builder()
        .method("POST")
        .uri("/api/apps/authorizations")
        .header("host", "127.0.0.1:8765")
        .header("content-type", "application/json")
        .body(HttpBody::from(form.to_string()))
        .unwrap();
    assert_eq!(f.respond(request).await.status(), StatusCode::UNAUTHORIZED);
    let (status, _) = f.call("POST", "/apps/authorizations", form.clone()).await;
    assert_eq!(status, StatusCode::CREATED);
    let entry = f.api.auth.entry_link("/");
    let (cookie, csrf) = f
        .api
        .auth
        .start_session(entry.url.split_once('#').unwrap().1)
        .unwrap()
        .unwrap();
    for (sent, status) in [
        ("wrong", StatusCode::FORBIDDEN),
        (csrf.as_str(), StatusCode::CREATED),
    ] {
        let request = Request::builder()
            .method("POST")
            .uri("/api/apps/authorizations")
            .header("host", "127.0.0.1:8765")
            .header("cookie", format!("{}={cookie}", f.api.auth.cookie))
            .header(crate::auth::CSRF_HEADER, sent)
            .header("content-type", "application/json")
            .body(HttpBody::from(form.to_string()))
            .unwrap();
        assert_eq!(f.respond(request).await.status(), status);
    }
}

#[tokio::test]
async fn refresh_is_forced_on_entry_throttles_failed_reads_and_remembers_completed_windows() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let calls = Arc::new(Mutex::new(Vec::new()));
    let called = calls.clone();
    *f.api.activity_runner.lock().unwrap() = Some(Arc::new(move |start, end| {
        called.lock().unwrap().push((start, end));
        Ok(())
    }));
    let uri = "/activity/history?start=2999-07-10T00:00:00Z&end=2999-07-11T00:00:00Z";
    f.get(uri).await;
    f.api.work.close();
    f.api.work.wait().await;
    f.get(uri).await;
    f.api.work.wait().await;
    f.get(&format!("{uri}&refresh=true")).await;
    f.api.work.wait().await;
    f.get("/activity/history?start=2999-07-10T00:00:00Z&end=2999-07-12T00:00:00Z")
        .await;
    f.api.work.wait().await;
    assert_eq!(calls.lock().unwrap().len(), 3);
    let past = "/activity/history?start=2026-07-06T00:00:00Z&end=2026-07-13T00:00:00Z";
    f.get(past).await;
    f.api.work.wait().await;
    f.api.activity_sync.lock().unwrap().clear();
    f.get(past).await;
    f.api.work.wait().await;
    assert_eq!(calls.lock().unwrap().len(), 4);
    let called = calls.clone();
    *f.api.activity_runner.lock().unwrap() = Some(Arc::new(move |start, end| {
        called.lock().unwrap().push((start, end));
        anyhow::bail!("synthetic provider unavailable")
    }));
    let uri = "/activity/history?start=2999-08-10T00:00:00Z&end=2999-08-11T00:00:00Z";
    f.get(uri).await;
    f.api.work.wait().await;
    f.get(uri).await;
    f.api.work.wait().await;
    assert_eq!(calls.lock().unwrap().len(), 5);
    f.api
        .activity_sync
        .lock()
        .unwrap()
        .values_mut()
        .for_each(|v| *v = std::time::Instant::now() - Duration::from_secs(301));
    f.get(uri).await;
    f.api.work.wait().await;
    assert_eq!(calls.lock().unwrap().len(), 6);
}

#[tokio::test]
async fn history_reads_shared_lifecycles_facts_and_only_local_detail_without_a_record_cap() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let now = chrono::Utc::now();
    let start = (now - chrono::Duration::days(1)).to_rfc3339();
    let end = (now + chrono::Duration::days(1)).to_rfc3339();
    for id in ["local", "remote"] {
        let record = json!({"schema_version":2,"run_id":id,"work_kind":"ticket","execution_mode":"autonomous","member_id":"alice","device_id":"device","source":"routine","started_at":now.to_rfc3339(),"status":"failed","attributes":{"target.title":"Shared work"}});
        f.write(
            &f.workspace()
                .join(format!(".guildbotics/state/task-runs/{id}/result.json")),
            &record.to_string(),
        );
    }
    let diagnostics = gb_observability::Diagnostics::open(&f.workspace());
    diagnostics.record(json!({"kind":"event","type":"command.started","trace_id":"local","person_id":"alice","timestamp":now.to_rfc3339()}).as_object().unwrap().clone());
    let events = gb_observability::activity::ActivityEvents::open(&f.workspace());
    let writes =
        gb_base::shared_writes::SharedWrites::new(f.root.join("identity"), Some(f.workspace()));
    writes.transaction(|writer|{
        for i in 0..1201 {
            events.record(writer,&json!({"type":"repository.push","trace_id":if i%2==0{"local"}else{"remote"},"timestamp":now.to_rfc3339(),"person_id":"alice","payload":{"ref":"refs/heads/main","commits":[{"id":format!("{i:07}"),"message":"Wire it","url":format!("local://code/commits/{i}")}]}}),&gb_observability::redaction::Redactor::default())?;
        }
        Ok(())
    }).unwrap();
    let query = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("start", &start)
        .append_pair("end", &end)
        .finish();
    let history = f.get(&format!("/activity/history?{query}")).await;
    assert_eq!(history["events"].as_array().unwrap().len(), 1201);
    let sessions = history["sessions"].as_array().unwrap();
    assert_eq!(sessions.len(), 2, "{history}");
    for s in sessions {
        assert_eq!(s["title"], "Shared work");
        assert_eq!(s["status"], "failed");
        assert_eq!(s["detail_available"], s["trace_id"] == "local");
        assert!(!s["links"].as_array().unwrap().is_empty());
    }
    f.api.work.close();
    f.api.work.wait().await;
}

#[tokio::test]
async fn invalid_history_and_sync_windows_keep_the_frozen_error_codes() {
    let f = fixture();
    for (query, code) in [
        (
            "start=2026-07-13T00:00:00Z&end=2026-07-06T00:00:00Z",
            "invalid_activity_history_range",
        ),
        (
            "sync_start=2026-07-06T00:00:00Z",
            "invalid_activity_sync_range",
        ),
        (
            "sync_end=2026-07-13T00:00:00Z",
            "invalid_activity_sync_range",
        ),
        (
            "sync_start=bad&sync_end=2026-07-13T00:00:00Z",
            "invalid_activity_sync_range",
        ),
        (
            "sync_start=2026-07-13T00:00:00Z&sync_end=2026-07-06T00:00:00Z",
            "invalid_activity_sync_range",
        ),
    ] {
        let (status, body) = f
            .call("GET", &format!("/activity/history?{query}"), Value::Null)
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
        assert_eq!(body["code"], code);
    }
    f.init().await;
    let query = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("start", "2026-07-10T00:00:00.123+09:00")
        .append_pair("end", "2026-07-11T00:00:00.2345678+09:00")
        .finish();
    let history = f.get(&format!("/activity/history?{query}")).await;
    assert_eq!(history["start"], "2026-07-10T00:00:00.123000+09:00");
    assert_eq!(history["end"], "2026-07-11T00:00:00.234567+09:00");
    f.api.work.close();
    f.api.work.wait().await;
}

#[tokio::test]
async fn reset_drops_only_active_subscribers_backlog_and_floors_name_only_members() {
    let f = fixture();
    f.init().await;
    for (person, active, channel) in [
        ("alice", true, "C1"),
        ("carol", false, "C9"),
        ("dave", true, ""),
    ] {
        f.add(json!({"person_id":person,"person_name":person,"person_type":"agent","is_active":active,"roles":["architect"]})).await;
        let p = f.config().join(format!("team/members/{person}/member.yml"));
        f.write(&p,&format!("is_active: {active}\nmessage_channels:\n- name: general\n  chat:\n    channel_id: '{channel}'\n    channel_name: general\n"));
    }
    let p = f.config().join("team/project.yml");
    let mut project = f.read(&p);
    project["services"] = json!({"chat_service":{"name":"slack"}});
    f.write(&p, &serde_saphyr::to_string(&project).unwrap());
    let root = f
        .workspace()
        .join(".guildbotics/state/chat_state/slack/alice");
    f.write(
        &root.join("pending_events/C1.json"),
        r#"{"events":[{"old":"message"}],"unrelated":"keep"}"#,
    );
    f.write(&root.join("channels/C1.json"), r#"{"watermark":"old"}"#);
    f.write(
        &root.join("threads/C1/thread.json"),
        r#"{"messages":["keep"]}"#,
    );
    let before = bytes_under(&f.config());
    let (status, result) = f
        .call("POST", "/chat/receive-state/reset", Value::Null)
        .await;
    assert_eq!(status, StatusCode::OK, "{result}");
    assert_eq!(result, json!({"members_reset":2,"channels_reset":1}));
    let store = crate::chat_state::Store {
        workspace: f.workspace(),
        writes: Arc::new(gb_base::shared_writes::SharedWrites::new(
            f.root.join("identity"),
            Some(f.workspace()),
        )),
        limit: 500,
    };
    assert!(store.pending("slack", "alice", "C1").unwrap().is_empty());
    assert!(store.cutoff("slack", "alice").unwrap().is_some());
    assert!(store.cutoff("slack", "dave").unwrap().is_some());
    assert!(store.cutoff("slack", "carol").unwrap().is_none());
    assert_eq!(
        f.read(&root.join("channels/C1.json")),
        json!({"watermark":"old"})
    );
    assert!(!root.join("threads/C1/thread.json").exists());
    assert_eq!(before, bytes_under(&f.config()));
    let events = f.api.events.snapshot();
    let event = events
        .iter()
        .find(|r| r["type"] == "chat.receive_state_reset")
        .unwrap();
    assert_eq!(event["payload"], json!({"members":2,"channels":1}));
    f.api.selected().service.service.starting().await;
    let (status, result) = f
        .call("POST", "/chat/receive-state/reset", Value::Null)
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(result["code"], "runtime_running");
}

#[tokio::test]
async fn reset_shared_state_lock_contention_is_503_and_preserves_the_backlog() {
    let f = fixture();
    f.init_with(json!({"language":"en","services":{"chat_service":{"name":"slack"}}}))
        .await;
    let mut member = agent("alice");
    member["chat_channels"] = json!(["C01234567"]);
    f.add(member).await;
    let lock = IdentityLock::acquire(&f.root.join("identity"), Duration::from_secs(1)).unwrap();
    let (status, body) = f
        .call("POST", "/chat/receive-state/reset", Value::Null)
        .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(body["code"], "config_busy");
    drop(lock);
}

#[tokio::test]
async fn reset_without_subscribers_is_a_noop_even_while_another_writer_holds_the_lock() {
    let f = fixture();
    f.init().await;
    let lock = IdentityLock::acquire(&f.root.join("identity"), Duration::from_secs(1)).unwrap();
    let (status, body) = f
        .call("POST", "/chat/receive-state/reset", Value::Null)
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, json!({"members_reset":0,"channels_reset":0}));
    drop(lock);
}

#[tokio::test]
async fn websocket_requires_cookie_and_origin_then_replays_and_delivers_live_in_order() {
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::tungstenite::{Message, client::IntoClientRequest};
    let f = fixture();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let app = crate::api::router(f.api.clone());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let link = f.api.auth.entry_link("/");
    let code = link.url.split_once('#').unwrap().1;
    let (cookie, _) = f.api.auth.start_session(code).unwrap().unwrap();
    let cookie = format!("{}={cookie}", f.api.auth.cookie);
    let request = || {
        let mut r = format!("ws://127.0.0.1:{port}/api/events")
            .into_client_request()
            .unwrap();
        r.headers_mut()
            .insert("host", "127.0.0.1:8765".parse().unwrap());
        r
    };
    f.api.events.publish(
        "first",
        json!({"order":1}).as_object().unwrap().clone(),
        Default::default(),
    );
    for (session, origin, bearer, protocol) in [
        (None, Some("http://127.0.0.1:8765"), false, false),
        (Some("bad"), Some("http://127.0.0.1:8765"), false, false),
        (Some(cookie.as_str()), None, false, false),
        (
            Some(cookie.as_str()),
            Some("http://127.0.0.1:1420"),
            false,
            false,
        ),
        (None, Some("http://127.0.0.1:8765"), true, false),
        (None, Some("http://127.0.0.1:8765"), false, true),
    ] {
        let mut r = request();
        if let Some(s) = session {
            r.headers_mut().insert("cookie", s.parse().unwrap());
        }
        if let Some(o) = origin {
            r.headers_mut().insert("origin", o.parse().unwrap());
        }
        if bearer {
            r.headers_mut().insert(
                "authorization",
                format!("Bearer {}", f.token).parse().unwrap(),
            );
        }
        if protocol {
            r.headers_mut().insert(
                "sec-websocket-protocol",
                format!("guildbotics.session, {}", f.token).parse().unwrap(),
            );
            // Read the refusal frame directly: Tungstenite refuses a server
            // that does not select any offered protocol before reading it.
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut tcp = tokio::net::TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, port))
                .await
                .unwrap();
            let mut wire = String::from("GET /api/events HTTP/1.1\r\n");
            for (key, value) in r.headers() {
                wire.push_str(&format!("{key}: {}\r\n", value.to_str().unwrap()));
            }
            wire.push_str("\r\n");
            tcp.write_all(wire.as_bytes()).await.unwrap();
            let mut response = Vec::new();
            while !response.ends_with(b"\r\n\r\n") {
                response.push(tcp.read_u8().await.unwrap());
            }
            assert!(response.starts_with(b"HTTP/1.1 101"));
            assert!(
                !String::from_utf8_lossy(&response)
                    .to_lowercase()
                    .contains("sec-websocket-protocol")
            );
            let mut frame = [0; 4];
            tcp.read_exact(&mut frame).await.unwrap();
            assert_eq!(frame, [0x88, 2, 3, 240]);
            assert_eq!(f.api.events.subscriber_count(), 0);
            continue;
        }
        let (mut socket, _) = tokio_tungstenite::connect_async(r).await.unwrap();
        let message = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert!(matches!(message,Message::Close(Some(frame)) if u16::from(frame.code)==1008));
        assert_eq!(f.api.events.subscriber_count(), 0);
    }
    let mut r = request();
    r.headers_mut().insert("cookie", cookie.parse().unwrap());
    r.headers_mut()
        .insert("origin", "http://127.0.0.1:8765".parse().unwrap());
    let (mut socket, response) = tokio_tungstenite::connect_async(r).await.unwrap();
    assert!(!response.headers().contains_key("sec-websocket-protocol"));
    let replay: Value =
        serde_json::from_str(socket.next().await.unwrap().unwrap().to_text().unwrap()).unwrap();
    assert_eq!(replay["type"], "first");
    assert_eq!(replay["payload"], json!({"order":1}));
    f.api.events.publish(
        "second",
        json!({"order":2}).as_object().unwrap().clone(),
        Default::default(),
    );
    let live: Value =
        serde_json::from_str(socket.next().await.unwrap().unwrap().to_text().unwrap()).unwrap();
    assert_eq!(live["type"], "second");
    assert_eq!(live["payload"], json!({"order":2}));
    socket.send(Message::Close(None)).await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while f.api.events.subscriber_count() != 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let mut r = request();
    r.headers_mut().insert("cookie", cookie.parse().unwrap());
    r.headers_mut()
        .insert("origin", "http://127.0.0.1:8765".parse().unwrap());
    let (mut socket, _) = tokio_tungstenite::connect_async(r).await.unwrap();
    let _ = socket.next().await;
    let _ = socket.next().await;
    f.api.shutdown.request(false);
    tokio::time::timeout(Duration::from_secs(2), async {
        while f.api.events.subscriber_count() != 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    for bytes in bytes_under(&f.workspace()).values() {
        assert!(!String::from_utf8_lossy(bytes).contains(&f.token));
    }
    server.abort();
}
