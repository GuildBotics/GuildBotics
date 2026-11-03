//! Opt-in reads of #100's production copy. Never copy production into fixtures
//! or print record values on mismatch. The screen endpoints call these readers.
use gb_observability::Diagnostics;
use serde_json::Value;

fn copy_tree(from: &std::path::Path, to: &std::path::Path) {
    std::fs::create_dir_all(to).unwrap();
    for entry in std::fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        if entry.path().is_dir() {
            copy_tree(&entry.path(), &to.join(entry.file_name()));
        } else {
            std::fs::copy(entry.path(), to.join(entry.file_name())).unwrap();
        }
    }
}

#[test]
#[ignore = "GUILDBOTICS_BLACKBOX_GOLDEN must name the offline production copy"]
fn existing_record_endpoints_match_production_golden() {
    let root = std::path::PathBuf::from(std::env::var_os("GUILDBOTICS_BLACKBOX_GOLDEN").unwrap());
    let workspace = tempfile::tempdir().unwrap();
    copy_tree(
        &root.join("workspace/.guildbotics/local/run"),
        &workspace.path().join(".guildbotics/local/run"),
    );
    copy_tree(
        &root.join("workspace/.guildbotics/state/task-runs"),
        &workspace.path().join(".guildbotics/state/task-runs"),
    );
    let diagnostics = Diagnostics::open(workspace.path());
    copy_tree(
        &root.join("workspace/.guildbotics/state/documents/memory_events"),
        &workspace
            .path()
            .join(".guildbotics/state/documents/memory_events"),
    );
    diagnostics.start_system_session("golden-reader").unwrap();
    let read = |name: &str| -> Value {
        serde_json::from_slice(&std::fs::read(root.join("golden").join(name)).unwrap()).unwrap()
    };
    let expected = read("diagnostics__traces.json");
    let actual = diagnostics
        .traces(None, None, None, None, None, 1000)
        .unwrap();
    if actual != expected {
        let mut fields = std::collections::BTreeMap::<String, usize>::new();
        let mut missing = 0;
        for wanted in expected["traces"].as_array().unwrap() {
            if let Some(found) = actual["traces"]
                .as_array()
                .unwrap()
                .iter()
                .find(|v| v["trace_id"] == wanted["trace_id"])
            {
                for (key, value) in wanted.as_object().unwrap() {
                    if found[key] != *value {
                        *fields.entry(key.clone()).or_default() += 1;
                    }
                }
            } else {
                missing += 1;
            }
        }
        panic!(
            "diagnostics/traces differs: actual {}, expected {}, missing {missing}, fields {fields:?}",
            actual["traces"].as_array().unwrap().len(),
            expected["traces"].as_array().unwrap().len()
        );
    }
    let traces = expected["traces"].as_array().unwrap();
    for (count, trace) in traces.iter().enumerate() {
        let id = trace["trace_id"].as_str().unwrap();
        let expected = read(&format!("diagnostics__traces__{id}.json"));
        let actual = diagnostics.trace_detail(id).unwrap();
        assert!(
            actual == expected,
            "diagnostics/traces/<id> differs at entry {count}"
        );
    }
    eprintln!(
        "Matched diagnostics/traces and {} trace-detail responses",
        traces.len()
    );
}
