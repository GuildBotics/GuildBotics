//! The actual index/transcript split, compared with Python's decisions.
use gb_observability::Diagnostics;
use serde_json::Value;
use std::path::Path;

fn records(path: &Path) -> Vec<Value> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[test]
fn rust_routes_the_shared_fixture() {
    let cases: Vec<Value> = serde_json::from_str(include_str!(
        "../../../records/golden/diagnostics-routing.json"
    ))
    .unwrap();
    for case in cases {
        let root = tempfile::tempdir().unwrap();
        Diagnostics::open(root.path()).record(case["record"].as_object().unwrap().clone());
        let run = root.path().join(".guildbotics/local/run");
        let indexed = records(&run.join("diagnostics.jsonl"));
        let mut expected = vec!["session.pointer"];
        if case["indexed"] == true {
            expected.push(case["record"]["type"].as_str().unwrap());
        }
        assert_eq!(
            indexed
                .iter()
                .map(|r| r["type"].as_str().unwrap())
                .collect::<Vec<_>>(),
            expected,
            "{case}"
        );
        let saved = records(&run.join("sessions/routing.jsonl"));
        assert_eq!(saved.len(), 1);
        for key in ["kind", "type", "trace_id"] {
            assert_eq!(saved[0][key], case["record"][key], "{case}");
        }
    }
}
