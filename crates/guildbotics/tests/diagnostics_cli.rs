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
        json!({"trace_id":"trace-bad","kind":"log","level":"error","message":"gh: authentication required"}),
        json!({"trace_id":"trace-bad","kind":"io","type":"cli_agent.response","payload":{"stderr":"fatal: could not read Username"}}),
    ].iter().map(|record| format!("{record}\n")).collect::<String>();
    std::fs::write(&index, &index_contents).unwrap();
    std::fs::write(&transcript, &transcript_contents).unwrap();
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
    let detail = read(&["trace", "trace-bad"]);
    assert_eq!(detail["summary"]["status"], "failed");
    assert_eq!(detail["record_count"], 3);
    assert_eq!(
        read(&["trace", "trace-bad", "--kind", "log", "--level", "error"])["records"][0]["message"],
        "gh: authentication required"
    );
    assert_eq!(
        read(&["trace", "trace-bad", "--kind", "io"])["records"][0]["payload"]["stderr"],
        "fatal: could not read Username"
    );
    let missing = read(&["trace", "trace-missing"]);
    assert_eq!(missing["summary"], Value::Null);
    assert_eq!(missing["records"], json!([]));
    assert_eq!(read(&["system"])["records"], json!([]));
    assert_eq!(std::fs::read_to_string(index).unwrap(), index_contents);
    assert_eq!(
        std::fs::read_to_string(transcript).unwrap(),
        transcript_contents
    );
    assert!(!dir.path().join("home").exists());
}
