//! The diagnostics screen reads the same persisted records as Python.
use gb_observability::{Diagnostics, Failure, Trace};
use serde_json::{Value, json};

#[test]
fn diagnostics_answers_match_python() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../records/golden/screens.json")).unwrap();
    let root = tempfile::tempdir().unwrap();
    let run = root.path().join(".guildbotics/local/run");
    std::fs::create_dir_all(run.join("sessions")).unwrap();
    let expected = &fixture["expected"];
    std::fs::write(
        run.join("diagnostics.jsonl"),
        expected["index"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| format!("{v}\n"))
            .collect::<String>(),
    )
    .unwrap();
    for (name, content) in expected["sessions"].as_object().unwrap() {
        std::fs::write(run.join("sessions").join(name), content.as_str().unwrap()).unwrap();
    }
    let store = Diagnostics::open(root.path());
    assert_eq!(
        store.traces(None, None, None, None, None, 200).unwrap(),
        expected["traces"]
    );
    for detail in expected["trace_details"].as_array().unwrap() {
        assert_eq!(
            store
                .trace_detail(detail["trace_id"].as_str().unwrap())
                .unwrap(),
            *detail
        );
    }
    for (source, person, query, key, value, count) in [
        (Some("scheduled"), None, None, None, None, 0),
        (None, Some("other"), None, None, None, 0),
        (None, None, Some("result 日本語"), None, None, 0),
        (None, None, Some("look"), None, None, 3),
        (None, None, None, Some("test"), Some("success"), 1),
    ] {
        assert_eq!(
            store
                .traces(source, person, query, key, value, 200)
                .unwrap()["traces"]
                .as_array()
                .unwrap()
                .len(),
            count
        );
    }
    assert_eq!(
        store.traces(None, None, None, None, None, 1).unwrap()["traces"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

struct Failed;
impl Failure for Failed {
    fn error_type(&self) -> String {
        "CommandError".into()
    }
}

#[tokio::test]
async fn output_is_io_in_the_transcript_before_the_end_and_not_in_the_index() {
    for fail in [false, true] {
        let root = tempfile::tempdir().unwrap();
        let diagnostics = Diagnostics::open(root.path());
        let trace = Trace::open(&diagnostics, "manual").person("aiko");
        let _ = trace
            .run_command("look", "aiko", async {
                trace.command_output("look", "aiko", "");
                trace.command_output("look", "aiko", "result 日本語\n");
                if fail { Err(Failed) } else { Ok(()) }
            })
            .await;
        let detail = diagnostics.trace_detail(trace.id()).unwrap();
        assert_eq!(detail["records"].as_array().unwrap().len(), 3);
        assert_eq!(detail["records"][1]["kind"], "io");
        assert_eq!(detail["records"][1]["type"], "command.output");
        assert_eq!(
            detail["records"][1]["payload"],
            json!({"output":"result 日本語\n"})
        );
        assert_eq!(detail["summary"]["event_count"], 2);
        assert_eq!(
            detail["summary"]["status"],
            if fail { "failed" } else { "success" }
        );
        let index =
            std::fs::read_to_string(root.path().join(".guildbotics/local/run/diagnostics.jsonl"))
                .unwrap();
        assert!(!index.contains("result 日本語"));
    }
}
