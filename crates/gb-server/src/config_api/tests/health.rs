use super::*;
#[tokio::test]
async fn environment_tool_authentication_alert_recovers_without_losing_saved_login() {
    let f = fixture();
    f.init().await;
    for name in ["alice", "bob"] {
        f.add(json!({"person_id":name,"person_name":name,"person_type":"agent","is_active":true}))
            .await;
    }
    f.write(
        &f.config().join("intelligences/brain_mapping.yml"),
        "default:\n  kind: ai_cli\n  cli_agent: default\n",
    );
    f.write(
        &f.config().join("intelligences/cli_agent_mapping.yml"),
        "default: cli_agents/claude/default.yml\n",
    );
    f.claude_logged_in();
    let host = gb_environment::Host {
        data: f.data(),
        home: f.root.join("home"),
        package: gb_base::home::python_package(),
        identity_root: f.root.join("identity"),
    };
    let marker = f
        .data()
        .join("agent_environment/claude/authentication-failed");
    for failed in [true, false, true, false] {
        if failed {
            f.write(&marker, "");
        } else {
            std::fs::remove_file(&marker).unwrap();
        }
        let view =
            crate::environment::status_with(&host, &f.workspace(), "en", f.keys.as_ref(), true)
                .await
                .unwrap();
        let tool = view["tools"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["name"] == "claude")
            .unwrap();
        assert_eq!(tool["authentication_failed"], failed);
        assert_eq!(tool["credentials_saved"], true);
        let problems = crate::health::environment_problems(&view);
        let tool_problems: Vec<_> = problems
            .iter()
            .filter(|p| p["setting"] == "tool" && p["slot"] == "claude")
            .collect();
        let a = f.get("/system-alerts").await;
        let tool_alerts: Vec<_> = a["alerts"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|a| a["id"] == "agent-environment:tool:claude")
            .collect();
        assert_eq!(tool_problems.len(), usize::from(failed));
        assert_eq!(tool_alerts.len(), usize::from(failed));
        if failed {
            assert_eq!(tool_problems[0]["reason"], tool["problem"]);
            assert_eq!(tool_alerts[0]["reason"], tool["problem"]);
            assert_eq!(tool_alerts[0]["person_id"], "");
            assert!(
                tool["problem"]
                    .as_str()
                    .unwrap()
                    .contains(tool["login_command"].as_str().unwrap())
            );
        }
    }
}

#[test]
fn environment_alert_projection_preserves_device_tool_slot_and_usage_scope() {
    let mut view = json!({"problem_setting":"runtime","problem":"no hypervisor","warning":"image differs","tools":[{"name":"codex","problem":"login","usage_check":null},{"name":"claude","problem":"unused"},{"name":"grok","problem":"probe","usage_check":{"status":"failed"}}],"members":[{"person_id":"aiko","slots":[{"slot":"default","tool":"codex","problems":[]}]},{"person_id":"kenji","slots":[{"slot":"review","tool":"codex","problems":[{"setting":"grants","reason":"missing local path"}]}]}]});
    let p = crate::health::environment_problems(&view);
    assert_eq!(p,json!([{"person_id":"","slot":"","setting":"runtime","reason":"no hypervisor"},{"person_id":"","slot":"","setting":"image_differs","reason":"image differs"},{"person_id":"","slot":"codex","setting":"tool","reason":"login"},{"person_id":"","slot":"grok","setting":"tool","reason":"probe"},{"person_id":"kenji","slot":"review","setting":"grants","reason":"missing local path"}]).as_array().unwrap().clone());
    view["problem_setting"] = "filesystem".into();
    view["problem"] = "filesystem denied".into();
    assert_eq!(
        crate::health::environment_problems(&view)[0]["reason"],
        "filesystem denied"
    );
    view["problem_setting"] = "".into();
    view["warning"] = "".into();
    view["tools"] = json!([]);
    view["members"] = json!([]);
    assert!(crate::health::environment_problems(&view).is_empty());
}
fn record(f: &Fixture, kind: &str) {
    gb_observability::Diagnostics::open(&f.workspace()).record(json!({"kind":"event","type":kind,"timestamp":"2026-09-29T10:00:00Z","person_id":"alice","command":"workflows/demo","trace_id":"trace-1","attributes":{"agent.kind":"cli_agent","agent.adapter":"codex","credential.provider":"llm","llm.provider":"openai","error.category":"credit","error.status_code":429,"error.response":"credit answer"},"payload":{"model":"actual","model_specified":true,"effort":"xhigh"}}).as_object().unwrap().clone());
}
#[tokio::test]
async fn test_device_record_endpoints_read_the_persisted_records() {
    let f = fixture();
    record(&f, "span.finished");
    record(&f, "span.failed");
    assert_eq!(
        f.get("/intelligences/cli-agents/last-turns").await,
        json!([{"person_id":"alice","agent":"codex","model":"actual","model_specified":true,"effort":"xhigh","timestamp":"2026-09-29T10:00:00Z"}])
    );
    assert_eq!(
        f.get("/intelligences/inference-failures").await,
        json!({"llm":{"openai":{"category":"credit","timestamp":"2026-09-29T10:00:00Z","status_code":429,"response":"credit answer"}},"jev":null})
    );
}
#[tokio::test]
async fn test_system_alerts_endpoint_reads_diagnostics() {
    let f = fixture();
    record(&f, "command.failed");
    let a = f.get("/system-alerts").await;
    assert!(
        a["alerts"]
            .as_array()
            .unwrap()
            .iter()
            .any(|a| a["code"] == "command_failed")
    );
}
#[tokio::test]
async fn test_system_alerts_endpoint_dismisses_alert() {
    let f = fixture();
    let events = f.api.events.snapshot();
    let config = bytes_under(&f.config());
    record(&f, "command.failed");
    let a = f.get("/system-alerts").await;
    let alert = a["alerts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["code"] == "command_failed")
        .unwrap();
    let (status, body) = f
        .call(
            "POST",
            "/system-alerts/dismiss",
            json!({"alert_id":alert["id"]}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        !body["alerts"]
            .as_array()
            .unwrap()
            .iter()
            .any(|a| a["code"] == "command_failed")
    );
    for invalid in [
        json!({}),
        json!({"alert_id":""}),
        json!({"alert_id":17}),
        json!([]),
    ] {
        let (s, b) = f.call("POST", "/system-alerts/dismiss", invalid).await;
        assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(b["code"], "validation_error");
    }
    let (s, _) = f
        .call(
            "POST",
            "/system-alerts/dismiss",
            json!({"alert_id":"unknown"}),
        )
        .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(f.api.events.snapshot(),events);
    assert_eq!(bytes_under(&f.config()),config);
}
