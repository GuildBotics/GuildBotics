//! The workspace's files are written and read as the Python version writes
//! and reads them: `records/golden/files.json`, which
//! `tests/guildbotics/test_records_golden.py` checks against the Python
//! version.

use std::path::{Path, PathBuf};

use gb_base::host_path;
use gb_workspace::{identity, selection};
use serde_json::Value;

fn fixture() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../records/golden/files.json");
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

/// `text` with each value spelled as the fixture names it.
fn spelled(mut text: String, values: &[(&str, &str)]) -> String {
    for (name, value) in values {
        let quoted = serde_json::to_string(value).unwrap();
        text = text.replace(&quoted[1..quoted.len() - 1], &format!("<{name}>"));
    }
    text
}

struct Machine {
    _dir: tempfile::TempDir,
    root: PathBuf,
    data: PathBuf,
}

fn machine() -> Machine {
    let dir = tempfile::tempdir().unwrap();
    let base = host_path::normalize(dir.path()).unwrap();
    Machine {
        _dir: dir,
        root: base.join("machine"),
        data: base.join("home/.guildbotics/data"),
    }
}

#[test]
fn the_workspace_files_are_written_as_the_fixture_has_them() {
    let files = fixture();
    let m = machine();
    let root = m.root.to_string_lossy().into_owned();
    for name in files["registry"]["register"].as_array().unwrap() {
        selection::register(&m.data, &m.root.join(name.as_str().unwrap())).unwrap();
    }
    let registry = std::fs::read_to_string(m.data.join("workspaces.json")).unwrap();
    assert_eq!(
        spelled(registry, &[("root", &root)]),
        files["registry"]["text"]
    );

    let selected = m.root.join("selected");
    std::fs::create_dir_all(&selected).unwrap();
    let created = identity::ensure_workspace_identity(&selected).unwrap();
    let text = std::fs::read_to_string(identity::workspace_identity_path(&selected)).unwrap();
    let values = [
        ("workspace_id", created.workspace_id.as_str()),
        ("created_at", &created.created_at),
    ];
    assert_eq!(spelled(text, &values), files["workspace_identity"]);

    let device = identity::ensure_device_identity(&m.data).unwrap();
    let text = std::fs::read_to_string(m.data.join("device.json")).unwrap();
    let os = serde_json::to_value(device.os).unwrap();
    let values = [
        ("device_id", device.device_id.as_str()),
        ("display_name", &device.display_name),
        ("os", os.as_str().unwrap()),
    ];
    assert_eq!(spelled(text, &values), files["device_identity"]);
}

#[test]
fn the_fixture_files_are_read() {
    let files = fixture();
    let m = machine();
    let selected = m.root.join("selected");
    std::fs::create_dir_all(&selected).unwrap();
    std::fs::create_dir_all(&m.data).unwrap();
    let root = m.root.to_string_lossy();
    let active = files["active_workspace"]
        .as_str()
        .unwrap()
        .replace("<root>", &root);
    std::fs::write(m.data.join("active-workspace.json"), active).unwrap();
    assert_eq!(
        selection::restore(&m.data, |_| None).unwrap(),
        Some(selected.clone())
    );

    let id = "01a0951c-4e6e-7747-a845-11a5ec9ff682";
    let text = files["workspace_identity"]
        .as_str()
        .unwrap()
        .replace("<workspace_id>", id)
        .replace("<created_at>", "2026-11-02T00:00:00.000000Z");
    let path = identity::workspace_identity_path(&selected);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, text).unwrap();
    assert_eq!(
        identity::ensure_workspace_identity(&selected)
            .unwrap()
            .workspace_id,
        id
    );
}
