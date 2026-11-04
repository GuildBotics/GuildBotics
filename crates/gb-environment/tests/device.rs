//! Preflight fixtures contain no runnable VM and never initialize an image store.
use gb_environment::{Host, device, recipe::Recipe, runtime, snapshot};
use std::path::{Path, PathBuf};
fn write(root: &Path, path: &str, text: &str) {
    let file = root.join(path);
    std::fs::create_dir_all(file.parent().unwrap()).unwrap();
    std::fs::write(file, text).unwrap();
}
fn fixture() -> (tempfile::TempDir, Host, PathBuf) {
    let d = tempfile::tempdir().unwrap();
    let package = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("guildbotics");
    let host = Host {
        data: d.path().join("data"),
        home: d.path().join("home"),
        package,
        identity_root: d.path().join("identity"),
    };
    std::fs::create_dir_all(&host.home).unwrap();
    for file in runtime::placed_files(&host.data.join("msb")) {
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        std::fs::write(file, "not a runtime executable").unwrap();
    }
    write(&host.data, "msb/version", runtime::RUNTIME_VERSION);
    let workspace = d.path().join("workspace");
    write(
        &workspace,
        ".guildbotics/config/intelligences/agent_environment.yml",
        "{}",
    );
    (d, host, workspace)
}
#[tokio::test]
async fn snapshot_refusal_precedes_filesystem_and_ready_snapshot_exposes_filesystem_problem() {
    let (_d, host, workspace) = fixture();
    write(
        &workspace,
        ".guildbotics/config/intelligences/cli_agent_filesystem_grants.yml",
        "documents:\n  - path: .ssh\n    access: read\n",
    );
    let missing = device::read(&host, &workspace).await;
    assert_eq!(missing.view["problem_setting"], "snapshot");
    assert!(missing.refusal.is_some());
    let recipe = Recipe::load(&host.package.join("templates")).unwrap();
    std::fs::create_dir_all(snapshot::snapshots_dir(&workspace).join(recipe.name(None))).unwrap();
    let ready = device::read(&host, &workspace).await;
    assert_eq!(ready.view["problem_setting"], "filesystem");
    assert!(
        ready.view["access"]["problem"]
            .as_str()
            .unwrap()
            .contains("protected")
    );
}
#[tokio::test]
async fn dns_refusal_precedes_missing_image_and_snapshot() {
    let (_d, host, workspace) = fixture();
    write(
        &workspace,
        ".guildbotics/config/intelligences/agent_environment.yml",
        &format!(
            "dns:\n  nameservers: [not-ipv4]\nimage:\n  reference: local/custom:1\n  digests:\n    arm64: sha256:{}\n",
            "a".repeat(64)
        ),
    );
    let device = device::read(&host, &workspace).await;
    assert_eq!(device.view["problem_setting"], "declaration");
    assert!(
        device.view["dns"]["problem"]
            .as_str()
            .unwrap()
            .contains("IPv4")
    );
}
#[tokio::test]
async fn declaration_precedes_snapshot_and_access_preview_keeps_document_rows() {
    let (_d, host, workspace) = fixture();
    write(
        &workspace,
        ".guildbotics/config/intelligences/agent_environment.yml",
        "resources: {cpus: false}",
    );
    let device = device::read(&host, &workspace).await;
    assert_eq!(device.view["problem_setting"], "declaration");
    write(
        &workspace,
        ".guildbotics/config/intelligences/agent_environment.yml",
        "{}",
    );
    let preview = device::read(&host, &workspace).await;
    let documents = preview.view["access"]["documents"].as_array().unwrap();
    assert_eq!(documents.len(), 1);
    assert_eq!(documents[0]["grant"], "Documents/GuildBotics");
    assert_eq!(documents[0]["builtin"], true);
    assert_eq!(documents[0]["present"], false);
    assert_eq!(documents[0]["access"], "read_write");
    assert_eq!(preview.view["access"]["paths"], serde_json::json!([]));
    assert!(!host.home.join("Documents").exists());
}
#[tokio::test]
async fn rendered_warning_is_shared_with_image_and_runtime_refusal_stays_first() {
    let (_d, host, workspace) = fixture();
    std::fs::remove_dir_all(host.data.join("msb")).unwrap();
    std::fs::write(host.data.join("msb"), "not a directory").unwrap();
    write(
        &workspace,
        ".guildbotics/config/intelligences/agent_environment.yml",
        "resources: {cpus: false}",
    );
    let mut device = device::read(&host, &workspace).await;
    assert_eq!(device.view["problem_setting"], "runtime");
    device.warning = Some(gb_environment::Sentence::Text("different image".into()));
    let view = device.rendered(|s| match s {
        gb_environment::Sentence::Text(text) => text,
        _ => "runtime unavailable".into(),
    });
    assert_eq!(view["runtime"]["reason"], view["problem"]);
    assert_eq!(view["image"]["warning"], view["warning"]);
    assert_eq!(view["warning"], "different image");
}
