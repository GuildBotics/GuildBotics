//! Runs are recorded as the Python version records them: the cases of
//! `records/golden/diagnostics.json`, which `tests/guildbotics/test_records_golden.py`
//! runs through the Python version's writer and reader.

use std::path::Path;
use std::time::Duration;

use gb_observability::{Diagnostics, Failure, Trace};
use serde_json::Value;

fn cases() -> Vec<Value> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../records/golden/diagnostics.json");
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

#[derive(Debug)]
struct Failed(String);

impl Failure for Failed {
    fn error_type(&self) -> String {
        self.0.clone()
    }
}

/// Values that differ between runs, as the fixture spells them; each
/// timestamp is checked to be the Python version's local ISO 8601 first.
fn normalized(value: Value, trace_id: &str) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(key, item)| {
                    if key == "timestamp" {
                        let stamp = item.as_str().unwrap();
                        chrono::DateTime::parse_from_str(stamp, "%Y-%m-%dT%H:%M:%S%.6f%:z")
                            .unwrap_or_else(|e| panic!("{stamp}: {e}"));
                        (key, "<timestamp>".into())
                    } else {
                        (key, normalized(item, trace_id))
                    }
                })
                .collect(),
        ),
        Value::Array(items) => {
            Value::Array(items.into_iter().map(|i| normalized(i, trace_id)).collect())
        }
        Value::String(s) => Value::String(s.replace(trace_id, "<trace>")),
        other => other,
    }
}

fn jsonl(path: &Path, trace_id: &str) -> Value {
    let text = std::fs::read_to_string(path).unwrap();
    let records = text
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    normalized(Value::Array(records), trace_id)
}

#[tokio::test]
async fn a_run_is_recorded_as_the_fixture_has_it() {
    for case in cases() {
        let workspace = tempfile::tempdir().unwrap();
        let diagnostics = Diagnostics::open(workspace.path());
        let fields = &case["trace"];
        let field = |name: &str| fields[name].as_str().unwrap();
        let mut trace = Trace::open(&diagnostics, field("source"))
            .person(field("person_id"))
            .workflow(field("workflow"));
        for (key, value) in fields["attributes"].as_object().unwrap() {
            trace = trace.attribute(key, value.clone());
        }
        let run = &case["run"];
        let (command, person) = (
            run["command"].as_str().unwrap(),
            run["person"].as_str().unwrap(),
        );
        match &run["ends"] {
            Value::String(ends) if ends == "finished" => {
                let ran = trace.run_command(command, person, async { Ok::<_, Failed>(()) });
                ran.await.unwrap_or_else(|_| panic!("finished"));
            }
            Value::String(ends) if ends == "cancelled" => {
                let ran = trace.run_command(
                    command,
                    person,
                    std::future::pending::<Result<(), Failed>>(),
                );
                tokio::time::timeout(Duration::from_millis(10), ran)
                    .await
                    .unwrap_err();
            }
            ends => {
                let error_type = ends["failed"]["error_type"].as_str().unwrap().to_string();
                let ran =
                    trace.run_command(command, person, async { Err::<(), _>(Failed(error_type)) });
                assert!(ran.await.is_err());
            }
        }

        let run_dir = workspace.path().join(".guildbotics/local/run");
        let id = trace.id();
        assert_eq!(
            jsonl(&run_dir.join("diagnostics.jsonl"), id),
            case["index"],
            "{}",
            case["id"]
        );
        let transcript = run_dir.join(format!("sessions/{id}.jsonl"));
        assert_eq!(jsonl(&transcript, id), case["transcript"], "{}", case["id"]);
    }
}

#[test]
fn each_trace_is_named_by_a_fresh_id() {
    let workspace = tempfile::tempdir().unwrap();
    let diagnostics = Diagnostics::open(workspace.path());
    let (a, b) = (
        Trace::open(&diagnostics, "manual"),
        Trace::open(&diagnostics, "manual"),
    );
    assert_ne!(a.id(), b.id());
    assert!(a.id().len() == 32 && a.id().bytes().all(|c| c.is_ascii_hexdigit()));
}

#[tokio::test]
async fn a_run_that_panics_ends_as_failed() {
    let workspace = tempfile::tempdir().unwrap();
    let diagnostics = Diagnostics::open(workspace.path());
    let trace = Trace::open(&diagnostics, "manual");
    let id = trace.id().to_string();
    let ran = tokio::spawn(async move {
        trace
            .run_command("c", "p", async { panic!("a defect") as Result<(), Failed> })
            .await
    });
    assert!(ran.await.unwrap_err().is_panic());
    let index = jsonl(
        &workspace
            .path()
            .join(".guildbotics/local/run/diagnostics.jsonl"),
        &id,
    );
    let ended = index.as_array().unwrap().last().unwrap();
    assert_eq!(ended["type"], "command.failed");
    assert_eq!(ended["payload"]["error_type"], "panic");
}
