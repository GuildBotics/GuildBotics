//! Diagnostics HTTP contracts, using only temporary workspaces.
use super::*;

fn by_code<'a>(body: &'a Value, code: &str) -> &'a Value {
    body["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["code"] == code)
        .unwrap_or_else(|| panic!("missing {code}: {body}"))
}
async fn scenario(f: &Fixture, query: &str) -> Value {
    let (status, result) = f
        .call(
            "POST",
            &format!("/diagnostics/scenario{query}"),
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    result
}
async fn team() -> Fixture {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    f.add(agent("bob")).await;
    f.write(
        &f.config().join("intelligences/model_mapping.yml"),
        "default: models/openai/default.yml\n",
    );
    f.write(
        &f.config().join("intelligences/brain_mapping.yml"),
        "agent: {kind: ai_cli, cli_agent: default}\ndefault: {kind: model, model: default}\n",
    );
    f.write(
        &f.config().join("intelligences/cli_agent_mapping.yml"),
        "default: cli_agents/codex/default.yml\n",
    );
    *f.api.diagnostics_runner.lock().unwrap() = Some(Arc::new(|_, _, cwd, message| {
        assert!(message.contains("read-only diagnostics check"));
        assert!(cwd.is_dir());
        Ok("OK".into())
    }));
    f
}
#[tokio::test]
async fn targets_active_agents_explicit_inactive_and_human_members() {
    let f = team().await;
    let mut participation = f.read(&f.config().join("team/members/alice/member.yml"));
    participation["is_active"] = false.into();
    f.write(
        &f.config().join("team/members/alice/member.yml"),
        &participation.to_string(),
    );
    let result = scenario(&f, "").await;
    assert_eq!(result["active_members"], json!(["bob"]));
    assert_eq!(
        by_code(&result, "active_members")["context"],
        json!({"members":["bob"]})
    );
    let result = scenario(&f, "?person_id=alice").await;
    assert_eq!(result["active_members"], json!(["alice"]));
    assert_eq!(by_code(&result, "member_inactive")["status"], "warning");
    assert_eq!(
        by_code(&result, "member_inactive")["context"],
        json!({"inactive_members":["alice"]})
    );
    let mut person = agent("hana");
    person["person_type"] = "human".into();
    person["is_active"] = false.into();
    f.add(person).await;
    let result = scenario(&f, "?person_id=hana").await;
    assert_eq!(result["active_members"], json!(["hana"]));
    assert_eq!(by_code(&result, "human_member_reference")["status"], "ok");
    assert_eq!(by_code(&result, "human_member_roles")["status"], "ok");
    assert!(
        !result["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["section"] == "llm" || c["section"] == "cli_agent")
    );
    let path = f.config().join("team/members/bob/member.yml");
    let mut v = f.read(&path);
    v["is_active"] = false.into();
    f.write(&path, &v.to_string());
    let human_path = f.config().join("team/members/hana/member.yml");
    let mut human = f.read(&human_path);
    human["is_active"] = true.into();
    f.write(&human_path, &human.to_string());
    let result = scenario(&f, "").await;
    assert_eq!(result["active_members"], json!([]));
    assert_eq!(by_code(&result, "active_members")["status"], "error");
}
#[tokio::test]
async fn missing_llm_key_short_circuits_only_llm_and_checks_each_effective_cli() {
    let f = team().await;
    let mut carol = agent("carol");
    carol["is_active"] = false.into();
    f.add(carol).await;
    f.write(
        &f.config()
            .join("team/members/bob/intelligences/cli_agent_mapping.yml"),
        "default: cli_agents/grok/default.yml\n",
    );
    let called = Arc::new(Mutex::new(Vec::new()));
    let calls = called.clone();
    *f.api.diagnostics_runner.lock().unwrap() = Some(Arc::new(move |command, person, cwd, _| {
        calls
            .lock()
            .unwrap()
            .push((command.to_string(), person.to_string(), cwd.to_owned()));
        Ok("OK".into())
    }));
    let result = scenario(&f, "").await;
    assert_eq!(result["active_members"], json!(["alice", "bob"]));
    let missing = by_code(&result, "llm_api_key");
    assert_eq!(missing["credential"], true);
    assert_eq!(missing["person_id"], "alice");
    assert_eq!(
        missing["context"],
        json!({"provider":"openai","env_key":"OPENAI_API_KEY"})
    );
    assert!(
        missing["message"]
            .as_str()
            .unwrap()
            .contains("OPENAI_API_KEY is not configured")
    );
    let checks = result["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["code"] == "cli_agent_brain")
        .collect::<Vec<_>>();
    assert_eq!(checks[0]["target"], "codex");
    assert_eq!(checks[1]["target"], "grok");
    let calls = called.lock().unwrap();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].0, "diagnostics/cli_agent");
    assert_eq!(calls[0].1, "alice");
    assert_eq!(calls[1].1, "bob");
    assert!(calls.iter().all(|c| !c.2.exists()));
}
#[tokio::test]
async fn llm_command_preserves_safe_provider_failure_and_targets_one_member() {
    let f = team().await;
    f.seed_identity_secret("OPENAI_API_KEY", "synthetic-llm-key");
    for failing in [false, true] {
        *f.api.diagnostics_runner.lock().unwrap() = Some(Arc::new(move |command, person, _, _| {
            assert_eq!(person, "bob");
            if command == "diagnostics/llm" && failing {
                let failure=serde_json::from_value(json!({"command":true,"type":"CommandError","message":"provider refused","inference_error_type":"HTTPStatusError","inference_category":"credit"})).unwrap();
                Err(("provider refused".into(), Some(failure)))
            } else {
                Ok("OK".into())
            }
        }));
        let result = scenario(&f, "?person_id=bob").await;
        assert_eq!(result["active_members"], json!(["bob"]));
        let live = by_code(&result, "llm_live_call");
        assert_eq!(live["person_id"], "bob");
        assert_eq!(live["status"], if failing { "error" } else { "ok" });
        assert_eq!(
            live["context"],
            if failing {
                json!({"provider":"openai","error_type":"HTTPStatusError","category":"credit"})
            } else {
                json!({"provider":"openai"})
            }
        );
        assert_eq!(result["ok"], !failing);
    }
}
#[tokio::test]
async fn cli_failures_empty_output_and_non_cli_brain_are_errors() {
    let f = team().await;
    for case in ["empty", "exit", "refusal"] {
        *f.api.diagnostics_runner.lock().unwrap() = Some(Arc::new(move |_, _, _, _| {
            match case{
            "empty"=>Ok("   ".into()),
            "exit"=>Err(("login required".into(),Some(serde_json::from_value(json!({"command":true,"type":"CommandError","message":"login required","cli_agent_result":{"returncode":2,"stdout":"","stderr":"login required"}})).unwrap()))),
            _=>Err(("environment has no snapshot".into(),None)),
        }
        }));
        let result = scenario(&f, "?person_id=bob").await;
        let cli = by_code(&result, "cli_agent_brain");
        assert_eq!(cli["status"], "error");
        assert_eq!(cli["target"], "codex");
        match case {
            "empty" => assert_eq!(cli["context"]["empty_stdout"], true),
            "exit" => {
                assert_eq!(cli["context"]["returncode"], 2);
                assert!(cli["message"].as_str().unwrap().contains("login required"));
            }
            _ => assert!(
                cli["message"]
                    .as_str()
                    .unwrap()
                    .contains("environment has no snapshot")
            ),
        }
    }
    f.write(
        &f.config()
            .join("team/members/bob/intelligences/cli_agent_mapping.yml"),
        "default: cli_agents/grok/default.yml\n",
    );
    *f.api.diagnostics_runner.lock().unwrap() = Some(Arc::new(|_, _, _, _| {
        Err((
            "member broker failed".into(),
            Some(
                serde_json::from_value(
                    json!({"command":false,"type":"RuntimeError","message":"member broker failed"}),
                )
                .unwrap(),
            ),
        ))
    }));
    let body = scenario(&f, "?person_id=bob").await;
    let check = by_code(&body, "cli_agent_brain");
    assert_eq!(check["target"], "grok");
    assert_eq!(check["person_id"], "bob");
    assert_eq!(check["context"]["error_type"], "RuntimeError");
    assert!(
        check["message"]
            .as_str()
            .unwrap()
            .contains("member broker failed")
    );
    f.write(
        &f.config()
            .join("team/members/bob/intelligences/brain_mapping.yml"),
        "agent: {kind: model, model: default}\n",
    );
    let result = scenario(&f, "?person_id=bob").await;
    assert_eq!(
        by_code(&result, "cli_agent_brain")["context"],
        json!({"brain":"agent"})
    );
}

#[tokio::test]
async fn unknown_provider_falls_through_and_generic_inference_failure_keeps_its_type() {
    let f = team().await;
    f.write(
        &f.config().join("intelligences/model_mapping.yml"),
        "default: unclassified\n",
    );
    let result = scenario(&f, "?person_id=alice").await;
    assert_eq!(by_code(&result, "llm_live_call")["status"], "ok");
    assert_eq!(
        by_code(&result, "llm_live_call")["context"],
        json!({"provider":""})
    );
    assert!(
        !result["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "llm_api_key")
    );
    f.write(
        &f.config().join("intelligences/model_mapping.yml"),
        "default: models/openai/default.yml\n",
    );
    f.seed_identity_secret("OPENAI_API_KEY", "synthetic-llm-key");
    *f.api.diagnostics_runner.lock().unwrap() = Some(Arc::new(|command, _, _, _| {
        if command == "diagnostics/llm" {
            Err((
                "invalid api key".into(),
                Some(
                    serde_json::from_value(
                        json!({"command":false,"type":"RuntimeError","message":"invalid api key"}),
                    )
                    .unwrap(),
                ),
            ))
        } else {
            Ok("OK".into())
        }
    }));
    let result = scenario(&f, "?person_id=alice").await;
    let live = by_code(&result, "llm_live_call");
    assert_eq!(
        live["context"],
        json!({"provider":"openai","error_type":"RuntimeError"})
    );
    assert!(
        live["message"]
            .as_str()
            .unwrap()
            .contains("invalid api key")
    );
    assert_eq!(result["ok"], false);
    assert!(result["checks"].as_array().unwrap().iter().all(|c| {
        ["config", "members", "llm", "cli_agent"].contains(&c["section"].as_str().unwrap())
    }));
}

#[tokio::test]
async fn diagnostics_commands_are_read_only_and_diagnosis_uses_the_environment_refusal() {
    let f = team().await;
    let roots = gb_config::commands::CommandRoots {
        config: f.config(),
        templates: gb_base::home::python_package().join("templates"),
    };
    for (command, scopes) in [
        ("diagnostics/llm", vec![]),
        ("diagnostics/cli_agent", vec![]),
        ("assistants/troubleshoot", vec!["config", "diagnostics"]),
    ] {
        let access = gb_config::commands::command_access(
            &roots.resolve(command, "en", Some("alice")).unwrap(),
        )
        .unwrap();
        assert!(access.read_only);
        let mut actual = access
            .inspects
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>();
        actual.sort();
        assert_eq!(actual, scopes);
    }
    *f.api.diagnostics_runner.lock().unwrap() = None;
    let result = scenario(&f, "?person_id=alice").await;
    assert_eq!(by_code(&result, "cli_agent_brain")["status"], "error");
    assert_eq!(by_code(&result, "cli_agent_brain")["target"], "codex");
    let traces = f
        .get("/diagnostics/traces?source=diagnostics&person_id=alice")
        .await;
    assert_eq!(traces["traces"].as_array().unwrap().len(), 1);
    let detail = f
        .get(&format!(
            "/diagnostics/traces/{}",
            traces["traces"][0]["trace_id"].as_str().unwrap()
        ))
        .await;
    assert!(
        detail["records"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["type"] == "command.failed")
    );
    assert!(
        detail["records"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["type"] == "diagnostics.completed")
    );
}

#[tokio::test]
async fn debug_settings_read_and_update_both_flags() {
    let f = fixture();
    f.write(
        &f.workspace().join(".guildbotics/local/debug.env"),
        "LOG_LEVEL=DEBUG\nAGNO_DEBUG=false\n",
    );
    assert_eq!(
        f.get("/runtime/debug").await,
        json!({"enabled":true,"log_level":"DEBUG","agno_debug":false})
    );
    for enabled in [true, false] {
        let (status, body) = f
            .call("PUT", "/runtime/debug", json!({"enabled":enabled}))
            .await;
        assert_eq!(status, StatusCode::OK);
        let level = if enabled { "DEBUG" } else { "INFO" };
        assert_eq!(
            body,
            json!({"enabled":enabled,"log_level":level,"agno_debug":enabled})
        );
        let saved =
            std::fs::read_to_string(f.workspace().join(".guildbotics/local/debug.env")).unwrap();
        assert!(saved.contains(&format!("LOG_LEVEL={level}")));
        assert!(saved.contains(&format!("AGNO_DEBUG={enabled}")));
    }
}

#[tokio::test]
async fn scenario_missing_config_and_unknown_person_are_checks() {
    let f = fixture();
    let (status, body) = f.call("POST", "/diagnostics/scenario", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["ok"], false);
    assert_eq!(body["checks"][0]["code"], "config_load");
    assert_eq!(body["checks"][0]["status"], "error");
    assert!(
        body["checks"][0]["message"]
            .as_str()
            .unwrap()
            .starts_with("Configuration could not be loaded:")
    );
    assert_eq!(body["checks"].as_array().unwrap().len(), 1);
    assert_eq!(body["checks"], body["errors"]);
    assert_eq!(body["warnings"], json!([]));
    assert_eq!(body["checks"][0]["context"], json!({}));
    assert_eq!(body["checks"][0]["credential"], false);
    assert_eq!(body["active_members"], json!([]));
    f.init().await;
    let (status, body) = f
        .call("POST", "/diagnostics/scenario?person_id=ghost", Value::Null)
        .await;
    assert_eq!(status, StatusCode::OK);
    let checks = body["checks"].as_array().unwrap();
    let missing = checks
        .iter()
        .find(|c| c["code"] == "member_not_found")
        .unwrap();
    assert_eq!(missing["person_id"], "ghost");
    assert_eq!(missing["status"], "error");
    let events = f.api.events.snapshot();
    assert_eq!(events.last().unwrap()["type"], "diagnostics.completed");
    assert_eq!(
        events.last().unwrap()["payload"]["scope_person_id"],
        "ghost"
    );
    assert_eq!(
        f.get("/diagnostics/traces?person_id=ghost").await["traces"][0]["trace_id"],
        events.last().unwrap()["trace_id"]
    );
}

#[tokio::test]
async fn troubleshooting_validates_before_execution_and_uses_the_named_conversation() {
    let f = team().await;
    let initial = f.api.events.snapshot();
    for body in [
        json!({"conversation_id":"c","message":""}),
        json!({"conversation_id":"","message":"Why?"}),
        json!({"message":"Why?"}),
        json!({"conversation_id":"c","message":"Why?","focus":{"view":"nope"}}),
        json!({"conversation_id":"c","message":"Why?","unexpected":1}),
        json!({"conversation_id":"c","message":"Why?","focus":{"extra":1}}),
        json!({"conversation_id":"x".repeat(129),"message":"Why?"}),
    ] {
        let (status, body) = f.call("POST", "/diagnostics/troubleshoot", body).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(body["code"], "validation_error");
    }
    assert_eq!(f.api.events.snapshot(), initial);
    for (focus, label) in [
        (Value::Null, "trace"),
        (json!({"view":"trace","trace_id":"abc123"}), "abc123"),
    ] {
        let (status, body) = f
            .call(
                "POST",
                "/diagnostics/troubleshoot",
                json!({"conversation_id":"conv-1","message":"Why?","person":"alice","focus":focus}),
            )
            .await;
        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert_eq!(body["code"], "troubleshooting_failed");
        let id = body["context"]["trace_id"].as_str().unwrap();
        let detail = f.get(&format!("/diagnostics/traces/{id}")).await;
        assert_eq!(detail["summary"]["person_id"], "alice");
        assert_eq!(
            detail["summary"]["command"],
            format!("troubleshoot:{label}")
        );
        assert_eq!(
            detail["summary"]["attributes"]["troubleshooting.conversation_id"],
            "conv-1"
        );
    }
}

#[tokio::test]
async fn debug_settings_reload_for_updates_and_workspace_selection() {
    let f = fixture();
    let applied = Arc::new(Mutex::new(Vec::new()));
    let captured = applied.clone();
    crate::diagnostic_logging::install_reload(Arc::new(move |level| {
        captured.lock().unwrap().push(level.map(str::to_string))
    }));
    for enabled in [true, false] {
        assert_eq!(
            f.call("PUT", "/runtime/debug", json!({"enabled":enabled}))
                .await
                .0,
            StatusCode::OK
        );
        assert_eq!(
            applied.lock().unwrap().last().unwrap().as_deref(),
            Some(if enabled { "DEBUG" } else { "INFO" })
        );
    }
    f.write(
        &f.workspace().join(".guildbotics/local/debug.env"),
        "LOG_LEVEL=DEBUG\nAGNO_DEBUG=true\n",
    );
    f.api.select(Selected {
        workspace: Some(f.workspace()),
        ..Default::default()
    });
    assert_eq!(
        applied.lock().unwrap().last().unwrap().as_deref(),
        Some("DEBUG")
    );
    f.api.select(Selected::default());
    assert_eq!(applied.lock().unwrap().last().unwrap(), &None);
    let (status, body) = f
        .call("PUT", "/runtime/debug", json!({"enabled":true}))
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "workspace_not_configured");
}

#[tokio::test]
async fn retention_prunes_on_selection_and_maintenance_without_deleting_active_or_index() {
    let f = fixture();
    let workspace = f.workspace();
    let sessions = workspace.join(".guildbotics/local/run/sessions");
    f.write(
        &workspace.join(".guildbotics/config/transcripts.yml"),
        "retention_days: 1\n",
    );
    let old = sessions.join("old.jsonl");
    f.write(&old, "old");
    f.write(&sessions.join("keep.txt"), "keep");
    std::fs::File::options()
        .write(true)
        .open(&old)
        .unwrap()
        .set_modified(std::time::UNIX_EPOCH)
        .unwrap();
    let store = gb_observability::Diagnostics::open(&workspace);
    store.start_system_session("active").unwrap();
    let active = store
        .system_trace_id()
        .unwrap()
        .trim_start_matches("system:")
        .to_owned();
    let active = sessions.join(format!("{active}.jsonl"));
    std::fs::File::options()
        .write(true)
        .open(&active)
        .unwrap()
        .set_modified(std::time::UNIX_EPOCH)
        .unwrap();
    f.api.select(Selected {
        workspace: Some(workspace.clone()),
        ..Default::default()
    });
    assert!(!old.exists());
    assert!(active.exists());
    assert!(sessions.join("keep.txt").exists());
    assert!(
        workspace
            .join(".guildbotics/local/run/diagnostics.jsonl")
            .exists()
    );
    f.write(&old, "old");
    std::fs::File::options()
        .write(true)
        .open(&old)
        .unwrap()
        .set_modified(std::time::UNIX_EPOCH)
        .unwrap();
    let task = tokio::spawn(crate::diagnostics::maintenance(f.api.clone()));
    tokio::time::timeout(Duration::from_secs(2), async {
        while old.exists() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    f.api.cancelled.cancel();
    task.await.unwrap();
    assert!(active.exists());
    store.finish_system_session().unwrap();
}

#[tokio::test]
async fn global_records_exclude_scoped_logs() {
    let f = fixture();
    let store = gb_observability::Diagnostics::open(&f.workspace());
    store.start_system_session("test").unwrap();
    f.api.events.publish_log("INFO", "global line", None);
    let trace = gb_observability::Trace::open(&store, "manual");
    f.api
        .events
        .publish_log("INFO", "scoped line", Some(&trace));
    let body = f.get("/diagnostics/global").await;
    assert!(
        body["records"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["message"] == "global line")
    );
    assert!(!body.to_string().contains("scoped line"));
    assert_eq!(body["transcript_available"], true);
    store.finish_system_session().unwrap();
    assert_eq!(
        f.call("GET", "/diagnostics/global?limit=0", Value::Null)
            .await
            .0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
}

#[tokio::test]
async fn memory_events_filter_audits_and_preview_only_document_bodies() {
    let f = fixture();
    let documents = f.workspace().join(".guildbotics/state/documents");
    f.write(
        &documents.join("personal/alice/note/body.md"),
        "Retry after refreshing the token.",
    );
    let recorded = json!({"kind":"memory","type":"memory.record","timestamp":"2026-11-01T12:00:00Z","trace_id":"memory-trace","person_id":"alice","attributes":{"memory.action":"record","memory.doc_id":"note","memory.scope":"personal","memory.path":"documents/personal/alice/note"},"payload":{"title":"Retry note","summary":"Refresh before retry.","source":[{"type":"ticket","url":"https://example.test/issues/42"}]}});
    let recalled = json!({"kind":"memory","type":"memory.recall","timestamp":"2026-11-01T12:00:01Z","trace_id":"memory-trace","person_id":"alice","attributes":{"memory.action":"recall"},"payload":{"query_keywords":["Retry"],"result_count":1,"duration_ms":1.25}});
    let other = json!({"kind":"memory","type":"memory.touch","timestamp":"2026-11-01T12:00:02Z","trace_id":"other","person_id":"bob","attributes":{"memory.action":"touch","memory.path":"documents/../../canary"},"payload":{}});
    f.write(
        &documents.join("memory_events/device.jsonl"),
        &format!("{recorded}\n{recalled}\n{other}\n"),
    );
    f.write(&f.workspace().join("canary/body.md"), "must not read");
    let result = f
        .get("/diagnostics/memory-events?person_id=alice&action=record&source=issues/42&q=Retry")
        .await;
    assert_eq!(result["event_count"], 1);
    let event = &result["events"][0];
    assert_eq!(event["action"], "record");
    assert_eq!(event["person_id"], "alice");
    assert_eq!(event["doc_id"], "note");
    assert_eq!(event["trace_id"], "memory-trace");
    assert_eq!(event["body_preview"], "Retry after refreshing the token.");
    assert_eq!(event["source"], recorded["payload"]["source"]);
    let result = f
        .get("/diagnostics/memory-events?person_id=alice&action=recall&q=Retry")
        .await;
    assert_eq!(result["event_count"], 1);
    assert_eq!(result["events"][0]["query_keywords"], json!(["Retry"]));
    assert_eq!(result["events"][0]["result_count"], 1);
    assert_eq!(result["events"][0]["duration_ms"], 1.25);
    assert_eq!(result["events"][0]["body_preview"], "");
    assert_eq!(
        f.get("/diagnostics/memory-events?trace_id=memory-trace")
            .await["event_count"],
        2
    );
    assert_eq!(f.get("/diagnostics/memory-events?doc_id=note&since=2026-11-01T12:00:00Z&until=2026-11-01T12:00:00Z").await["event_count"],1);
    assert_eq!(
        f.get("/diagnostics/memory-events?person_id=bob").await["events"][0]["body_preview"],
        ""
    );
    assert_eq!(
        f.get("/diagnostics/memory-events?limit=1").await["event_count"],
        1
    );
    let detail = f.get("/diagnostics/traces/memory-trace").await;
    assert_eq!(detail["records"].as_array().unwrap().len(), 2);
    assert_eq!(detail["records"][0]["type"], "memory.record");
}
#[tokio::test]
async fn traces_and_detail_localize_chat_titles_and_use_completion_summaries() {
    let f = fixture();
    let store = gb_observability::Diagnostics::open(&f.workspace());
    for event in ["command.started", "command.finished"] {
        store.record(json!({"kind":"event","type":event,"trace_id":"chat","timestamp":"2026-11-01T12:00:00Z","source":"chat","person_id":"alice","command":"workflows/chat","attributes":{"chat.provider":"slack","chat.channel":"C","chat.thread":"T","event_id":"E"},"payload":{}}).as_object().unwrap().clone());
    }
    for (lang, title) in [("en", "Slack thread"), ("ja", "Slack スレッドの対応")] {
        for path in ["/diagnostics/traces", "/diagnostics/traces/chat"] {
            let (status, body) = f.call_in(lang, "GET", path, Value::Null).await;
            assert_eq!(status, StatusCode::OK);
            assert_eq!(
                if path.ends_with("/chat") {
                    &body["summary"]["title"]
                } else {
                    &body["traces"][0]["title"]
                },
                title
            );
        }
    }
    let run = json!({"schema_version":gb_base::files::SHARED_RECORD_SCHEMA_VERSION,"run_id":"run","work_kind":"chat","execution_mode":"autonomous","device_id":"test","member_id":"alice","status":"succeeded","started_at":"2026-11-01T12:00:00Z","finished_at":"2026-11-01T12:01:00Z","safe_summary":"Handled the thread.\nSecond line.","result":{"subject_type":"chat","subject_id":"slack:C:T:E","subject_url":"","status":"done"}});
    f.write(
        &f.workspace()
            .join(".guildbotics/state/task-runs/run/result.json"),
        &run.to_string(),
    );
    for path in ["/diagnostics/traces", "/diagnostics/traces/chat"] {
        let body = f.get(path).await;
        let title = if path.ends_with("/chat") {
            &body["summary"]["title"]
        } else {
            &body["traces"][0]["title"]
        };
        assert_eq!(title, "Handled the thread.");
    }
}
#[tokio::test]
async fn trace_reads_filter_order_collapse_streams_and_keep_review_limits_informational() {
    let f = fixture();
    let store = gb_observability::Diagnostics::open(&f.workspace());
    for (stamp, kind, event, payload) in [
        ("03", "event", "command.finished", json!({"command":"demo"})),
        ("01", "event", "command.started", json!({"command":"demo"})),
        ("02", "log", "", json!({})),
    ] {
        store.record(json!({"kind":kind,"type":event,"trace_id":"t1","timestamp":format!("2026-11-01T12:00:{stamp}Z"),"source":"manual","person_id":"alice","command":"demo","level":"INFO","message":"working","attributes":{"target.label":"#42","target.title":"Work title"},"payload":payload}).as_object().unwrap().clone());
    }
    let detail = f.get("/diagnostics/traces/t1").await;
    assert_eq!(detail["summary"]["status"], "success");
    assert_eq!(detail["summary"]["title"], "Work title");
    let records = detail["records"].as_array().unwrap();
    assert_eq!(
        records
            .iter()
            .map(|r| r["kind"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["event", "log", "event"]
    );
    assert_eq!(records[1]["presentation"]["message"], "working");
    assert_eq!(
        records[0]["presentation"],
        json!({"label_key":"diagnostics.executions.eventTypes.command_started","label_fallback":"command.started","message_key":"","message":"demo","message_params":{},"tone":"success","effort":""})
    );
    for (query, count) in [
        ("source=manual", 1),
        ("source=scheduled", 0),
        ("person_id=alice", 1),
        ("attr_key=target.label&attr_value=%2342", 1),
        ("q=demo", 1),
    ] {
        assert_eq!(
            f.get(&format!("/diagnostics/traces?{query}")).await["traces"]
                .as_array()
                .unwrap()
                .len(),
            count
        );
    }
    for (index, name, message) in [
        (0, "started", ""),
        (1, "delta", "Hel"),
        (2, "delta", "lo"),
        (3, "completed", "Hello"),
    ] {
        store.record(json!({"kind":"event","type":"agent_runtime.assistant","trace_id":"stream","span_id":"s","span":"cli_agent","timestamp":format!("2026-11-01T12:01:0{index}Z"),"attributes":{},"payload":{"name":name,"message":message}}).as_object().unwrap().clone());
    }
    let detail = f.get("/diagnostics/traces/stream").await;
    assert_eq!(detail["records"].as_array().unwrap().len(), 1);
    assert_eq!(detail["records"][0]["payload"]["message"], "Hello");
    for event in [
        "review_limit.started",
        "review_limit.finished",
        "review_limit.failed",
        "review_limit.deferred",
        "pull_request.review_limit",
    ] {
        let trace = gb_observability::Trace::open(&store, "manual");
        trace.event(event, serde_json::Map::new());
        assert_eq!(
            store.trace_detail(trace.id()).unwrap()["summary"]["status"],
            "info"
        );
    }
}
