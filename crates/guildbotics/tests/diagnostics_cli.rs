use std::{
    path::Path,
    process::{Command, Output},
};

use serde_json::{Value, json};

const BIN: &str = env!("CARGO_BIN_EXE_guildbotics");

fn run(root: &Path, workspace: &Path, args: &[&str]) -> Output {
    Command::new(BIN)
        .env("HOME", root)
        .env("GUILDBOTICS_HOME", root.join("home"))
        .env("GUILDBOTICS_WORKSPACE_ROOT", root.join("elsewhere"))
        .args(["diagnostics", "--workspace"])
        .arg(workspace)
        .args(args)
        .output()
        .unwrap()
}

fn read(root: &Path, workspace: &Path, args: &[&str]) -> Value {
    let result = run(root, workspace, args);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    serde_json::from_slice(&result.stdout).unwrap()
}

#[test]
fn diagnostics_read_summary_transcript_filters_and_system_without_writes() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = dir.path().join("workspace");
    let storage = workspace.join(".guildbotics/local/run");
    let sessions = storage.join("sessions");
    std::fs::create_dir_all(&sessions).unwrap();
    let index = storage.join("diagnostics.jsonl");
    let transcript = sessions.join("trace-bad.jsonl");
    let record = |id: &str, command: &str, event: &str, source: &str, day: u8| {
        json!({
            "kind": "event", "type": event, "trace_id": id, "span_id":"span-1",
            "source":source, "person_id":"aiko", "command":command, "workflow":"",
            "attributes":{}, "payload":{}, "timestamp":format!("2026-07-{day}T10:00:00+09:00"),
        })
    };
    let index_contents = [
        record(
            "trace-ok",
            "reports/weekly",
            "command.finished",
            "routine",
            20,
        ),
        record("trace-bad", "git/push", "command.failed", "routine", 21),
        record(
            "trace-assist",
            "troubleshoot:trace-bad",
            "command.finished",
            "manual",
            22,
        ),
    ]
    .iter()
    .map(|record| format!("{record}\n"))
    .collect::<String>();
    let transcript_contents = [
        record("trace-bad", "git/push", "command.started", "routine", 21),
        json!({"trace_id":"trace-bad","kind":"log","level":"error","message":"gh: authentication required for \"alice\"","timestamp":"2026-07-21T10:02:00+09:00"}),
        json!({"trace_id":"trace-bad","kind":"io","type":"cli_agent.response","payload":{"stderr":"fatal: could not read Username"}}),
    ].iter().map(|record| format!("{record}\n")).collect::<String>();
    std::fs::write(&index, &index_contents).unwrap();
    std::fs::write(&transcript, &transcript_contents).unwrap();
    let before = tree(&workspace);
    let read = |args: &[&str]| read(dir.path(), &workspace, args);
    let list = read(&["traces"]);
    let traces = list["traces"].as_array().unwrap();
    assert_eq!(
        traces
            .iter()
            .map(|v| v["trace_id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["trace-bad", "trace-ok"]
    );
    assert_eq!(traces[0]["status"], "failed");
    assert_eq!(traces[0]["command"], "git/push");
    assert_eq!(
        read(&["traces", "--limit", "1"])["traces"][0]["trace_id"],
        "trace-bad"
    );
    assert_eq!(
        read(&["traces", "--include-assistant"])["traces"][0]["trace_id"],
        "trace-assist"
    );
    assert_eq!(
        read(&["traces", "--source", "nothing"])["traces"],
        json!([])
    );
    assert!(
        !run(dir.path(), &workspace, &["traces", "--limit", "0"])
            .status
            .success()
    );
    for (query, expected) in [
        ("authentication required", "trace-bad"),
        ("\"alice\"", "trace-bad"),
        ("could not read Username", "trace-bad"),
        ("reports/weekly", "trace-ok"),
    ] {
        let result = read(&["traces", "--query", query]);
        assert_eq!(result["traces"].as_array().unwrap().len(), 1);
        assert_eq!(result["traces"][0]["trace_id"], expected);
    }
    assert_eq!(
        read(&["traces", "--query", "no such text anywhere"])["traces"],
        json!([])
    );
    assert_eq!(
        read(&["traces", "--query", "10:02:00"])["traces"],
        json!([])
    );
    let detail = read(&["trace", "trace-bad"]);
    assert_eq!(detail["summary"]["status"], "failed");
    assert_eq!(detail["record_count"], 3);
    assert_eq!(
        detail["records"]
            .as_array()
            .unwrap()
            .iter()
            .map(|record| record["kind"].as_str().unwrap())
            .collect::<std::collections::BTreeSet<_>>(),
        ["event", "io", "log"].into()
    );
    assert_eq!(
        read(&["trace", "trace-bad", "--kind", "log", "--level", "error"])["records"][0]["message"],
        "gh: authentication required for \"alice\""
    );
    assert_eq!(
        read(&["trace", "trace-bad", "--kind", "io"])["records"][0]["payload"]["stderr"],
        "fatal: could not read Username"
    );
    let missing = read(&["trace", "trace-missing"]);
    assert_eq!(missing["summary"], Value::Null);
    assert_eq!(missing["records"], json!([]));
    assert_eq!(read(&["system"])["records"], json!([]));
    assert_eq!(tree(&workspace), before);
    assert!(!dir.path().join("home").exists());
}

fn tree(root: &Path) -> std::collections::BTreeMap<std::path::PathBuf, Option<Vec<u8>>> {
    let mut paths = std::collections::BTreeMap::new();
    for entry in std::fs::read_dir(root).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            paths.insert(path.clone(), None);
            paths.extend(tree(&path));
        } else {
            paths.insert(path.clone(), Some(std::fs::read(path).unwrap()));
        }
    }
    paths
}

#[test]
fn diagnostics_lists_interrupted_system_sessions_and_orders_by_instant() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = dir.path().join("workspace");
    let storage = workspace.join(".guildbotics/local/run");
    let sessions = storage.join("sessions");
    std::fs::create_dir_all(&sessions).unwrap();
    let start = |id, timestamp| {
        json!({
            "kind":"event", "type":"system.started", "trace_id":null, "timestamp":timestamp,
            "attributes":{"system_session_id":id}
        })
    };
    let old = start("old", "2026-07-21T09:00:00");
    let latest = start("latest", "2026-07-22T09:00:00Z");
    let command = json!({
        "kind":"event", "type":"command.finished", "trace_id":"command", "source":"manual",
        "person_id":"aiko", "timestamp":"2026-07-21T10:00:00+09:00", "attributes":{"ticket":"T-1"}
    });
    std::fs::write(
        storage.join("diagnostics.jsonl"),
        format!("{old}\n{latest}\n{command}\n"),
    )
    .unwrap();
    let newest = json!({"kind":"log","timestamp":"2026-07-22T12:00:00","message":"newest"});
    let earlier = json!({"kind":"log","timestamp":"2026-07-22T13:00:00+02:00","message":"earlier"});
    std::fs::write(
        sessions.join("latest.jsonl"),
        format!("{latest}\n{newest}\n{earlier}\n"),
    )
    .unwrap();

    let read = |args: &[&str]| read(dir.path(), &workspace, args);
    let result = read(&["traces"]);
    let rows = result["traces"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["trace_id"], "system:old");
    assert_eq!(rows[0]["status"], "interrupted");
    assert_eq!(rows[1]["trace_id"], "command");
    for filter in [
        ["--source", "manual"],
        ["--person", "aiko"],
        ["--attr-key", "ticket"],
    ] {
        let rows = read(&["traces", filter[0], filter[1]]);
        assert_eq!(rows["traces"].as_array().unwrap().len(), 1);
        assert_eq!(rows["traces"][0]["trace_id"], "command");
    }
    assert_eq!(
        read(&["system", "--limit", "1"])["records"][0]["message"],
        "newest"
    );
}

#[test]
fn diagnostics_aggregates_events_and_logs_and_filters_exact_attributes() {
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("workspace");
    let storage = workspace.join(".guildbotics/local/run");
    let sessions = storage.join("sessions");
    std::fs::create_dir_all(&sessions).unwrap();
    let event = |id, ty, timestamp, person, attributes: Value| {
        json!({
            "kind":"event","type":ty,"trace_id":id,"source":"manual","person_id":person,
            "command":"demo","timestamp":timestamp,"attributes":attributes,
        })
    };
    let start = event(
        "one",
        "command.started",
        "2026-07-01T09:00:00+09:00",
        "aiko",
        json!({"ticket":"T-1"}),
    );
    let log = json!({"kind":"log","level":"INFO","trace_id":"one","timestamp":"2026-07-01T00:02:00Z","message":"working"});
    let end = event(
        "one",
        "command.finished",
        "2026-07-01T00:30:00Z",
        "aiko",
        json!({}),
    );
    let other = event(
        "two",
        "command.failed",
        "2026-07-01T00:15:00Z",
        "yuki",
        json!({"ticket":"T-10"}),
    );
    let failure = json!({"kind":"log","level":"ERROR","trace_id":"two","timestamp":"2026-07-01T00:10:00Z","message":"boom"});
    std::fs::write(
        storage.join("diagnostics.jsonl"),
        format!("{end}\n{start}\n{log}\n{other}\n{failure}\n"),
    )
    .unwrap();
    std::fs::write(
        sessions.join("one.jsonl"),
        format!("{end}\n{start}\n{log}\n"),
    )
    .unwrap();
    let read = |args: &[&str]| read(root.path(), &workspace, args);
    let list = read(&["traces"]);
    let rows = list["traces"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["trace_id"], "two");
    assert_eq!(rows[0]["status"], "failed");
    assert_eq!(rows[0]["error_count"], 2);
    for (key, value) in [
        ("trace_id", json!("one")),
        ("command", json!("demo")),
        ("person_id", json!("aiko")),
        ("source", json!("manual")),
        ("status", json!("success")),
        ("started_at", json!("2026-07-01T09:00:00+09:00")),
        ("updated_at", json!("2026-07-01T00:30:00Z")),
        ("event_count", json!(2)),
        ("log_count", json!(1)),
    ] {
        assert_eq!(rows[1][key], value, "{key}");
    }
    assert_eq!(rows[1]["attributes"]["ticket"], "T-1");
    let filtered = read(&["traces", "--attr-key", "ticket", "--attr-value", "T-1"]);
    assert_eq!(filtered["traces"], json!([rows[1]]));
    let detail = read(&["trace", "one"]);
    assert_eq!(detail["records"][0]["type"], "command.started");
    assert_eq!(detail["records"][1]["message"], "working");
    assert_eq!(detail["records"][2]["type"], "command.finished");
}
