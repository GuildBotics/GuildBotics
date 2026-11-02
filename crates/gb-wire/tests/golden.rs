//! The host speaks the wire as the Python version does: the cases of
//! `wire/golden/entry.json`, which `tests/guildbotics/test_wire_golden.py`
//! runs through the Python version's entry.

use std::path::Path;

use gb_wire::{CommandReply, CommandRequest};
use serde_json::Value;

fn cases() -> Vec<Value> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../wire/golden/entry.json");
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

#[test]
fn requests_are_written_as_the_entry_reads_them() {
    for case in cases() {
        let request: CommandRequest = serde_json::from_value(case["request"].clone()).unwrap();
        let written = serde_json::to_value(&request).unwrap();
        for (key, value) in case["request"].as_object().unwrap() {
            assert_eq!(&written[key], value, "{}: {key}", case["id"]);
        }
        // What the fixture leaves out is written as the entry's defaults.
        assert_eq!(written["pipe"], "", "{}", case["id"]);
        assert_eq!(written["invocation"], Value::Null, "{}", case["id"]);
    }
}

#[test]
fn the_reply_is_the_one_line_the_entry_writes_on_stdout() {
    for case in cases() {
        let stdout = case["stdout"].as_str().unwrap();
        let (line, rest) = stdout.split_once('\n').unwrap();
        assert_eq!(rest, "", "{}: one line, ending in a newline", case["id"]);
        let reply: CommandReply = serde_json::from_str(line).unwrap();
        let expected: CommandReply = serde_json::from_value(case["reply"].clone()).unwrap();
        assert_eq!(reply, expected, "{}", case["id"]);
    }
}

/// The facts as the Python version writes them (`CommandFacts.dump()`).
const FACTS: &str = r#"{"person_id":"aiko","run_id":"r1","work_kind":"manual","work_identity":"r1","trace_id":"t1","access":{"read_only":true,"inspects":["config"]},"inspected":{"config":"/w/.guildbotics/config"},"mounts":{"/tmp":true,"/opt/guildbotics/code/guildbotics":false}}"#;

#[test]
fn the_facts_are_written_as_the_environment_reads_them() {
    let facts: gb_wire::CommandFacts = serde_json::from_str(FACTS).unwrap();
    assert_eq!(facts.access.inspects, [gb_wire::InspectsItem::Config]);
    assert!(!facts.mounts["/opt/guildbotics/code/guildbotics"]);
    let written: Value = serde_json::to_value(&facts).unwrap();
    assert_eq!(written, serde_json::from_str::<Value>(FACTS).unwrap());
}
