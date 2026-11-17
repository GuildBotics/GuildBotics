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
        "dns: {nameservers: [1.1.1.1]}",
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
    let view = device.rendered(|sentence| match sentence {
        gb_environment::Sentence::Text(text) => text,
        _ => panic!("invalid DNS must report its validation error"),
    });
    assert_eq!(view["problem_setting"], "declaration");
    assert!(view["dns"]["problem"].as_str().unwrap().contains("IPv4"));
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
        "dns: {nameservers: [1.1.1.1]}",
    );
    let preview = device::read(&host, &workspace).await;
    let documents = preview.view["access"]["documents"].as_array().unwrap();
    assert!(documents.is_empty());
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

#[tokio::test]
async fn status_preserves_the_declared_network_and_dns_wire_fields() {
    let (_d, host, workspace) = fixture();
    for (mode, domains) in [
        ("deny", vec![]),
        ("allowlist", vec!["api.example.test"]),
        ("unrestricted", vec![]),
    ] {
        write(&workspace, ".guildbotics/config/intelligences/agent_environment.yml", &serde_json::json!({"network":{"mode":mode,"allowed_domains":domains,"allow_local_network":true},"dns":{"nameservers":["1.1.1.1","8.8.8.8"]}}).to_string());
        let status = device::read(&host, &workspace).await;
        assert_eq!(
            status.view["network"],
            serde_json::json!({"mode":mode,"allowed_domains":domains,"allow_local_network":true})
        );
        assert_eq!(status.view["dns"]["declared"], "1.1.1.1, 8.8.8.8");
        assert_eq!(
            status.view["dns"]["nameservers"],
            serde_json::json!(["1.1.1.1", "8.8.8.8"])
        );
    }
}

#[tokio::test]
async fn failed_snapshot_exposes_its_deadline_and_only_becomes_buildable_when_due() {
    let (_d, host, workspace) = fixture();
    let name = Recipe::load(&host.package.join("templates"))
        .unwrap()
        .name(None);
    assert!(
        snapshot::build(&workspace, &name, "timeout".into(), |_, _| async {
            Err("outage".into())
        })
        .await
        .is_err()
    );
    let waiting = device::read(&host, &workspace).await;
    assert_eq!(waiting.view["snapshot"]["state"], "failed");
    assert!(waiting.view["snapshot"]["next_retry_at"].as_u64().is_some());
    assert_eq!(waiting.view["snapshot"]["build_due"], false);
    let marker = snapshot::snapshots_dir(&workspace).join(format!("{name}.failed"));
    std::fs::File::options()
        .write(true)
        .open(marker)
        .unwrap()
        .set_times(
            std::fs::FileTimes::new()
                .set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(601)),
        )
        .unwrap();
    let due = device::read(&host, &workspace).await;
    assert_eq!(due.view["snapshot"]["build_due"], true);
    assert_eq!(due.view["snapshot"]["detail"], "outage");
}

#[tokio::test]
async fn automatic_build_schedule_requires_build_prerequisites_for_every_buildable_state() {
    for blocker in ["", "runtime", "image", "declaration", "resources", "home"] {
        for state in ["missing", "stale", "failed"] {
            let (_d, mut host, workspace) = fixture();
            let declaration = ".guildbotics/config/intelligences/agent_environment.yml";
            let custom = ["image", "home"].contains(&blocker);
            if custom {
                write(&workspace, declaration, &serde_json::json!({"dns":{"nameservers":["1.1.1.1"]},"image":{"reference":"local/custom:1","digests":{(gb_environment::image::device_architecture()):format!("sha256:{}", "a".repeat(64))}}}).to_string());
            }
            let name = Recipe::load(&host.package.join("templates"))
                .unwrap()
                .name(custom.then_some(""));
            let directory = snapshot::snapshots_dir(&workspace);
            std::fs::create_dir_all(&directory).unwrap();
            if state == "stale" {
                std::fs::create_dir(directory.join("guildbotics-1111111111111111")).unwrap();
            } else if state == "failed" {
                let marker = directory.join(format!("{name}.failed"));
                std::fs::write(&marker, "outage").unwrap();
                std::fs::File::options()
                    .write(true)
                    .open(marker)
                    .unwrap()
                    .set_times(std::fs::FileTimes::new().set_modified(
                        std::time::SystemTime::now() - std::time::Duration::from_secs(601),
                    ))
                    .unwrap();
            }
            match blocker {
                "runtime" => {
                    std::fs::remove_dir_all(host.data.join("msb")).unwrap();
                    std::fs::write(host.data.join("msb"), "not a directory").unwrap();
                }
                "declaration" => write(&workspace, declaration, "dns: {nameservers: [not-ipv4]}"),
                "home" => host.home = PathBuf::from("relative"),
                "resources" => write(&workspace, declaration, "resources: {cpus: false}"),
                _ => {}
            }
            let view = device::read(&host, &workspace).await.view;
            let eligible = blocker.is_empty();
            assert_eq!(
                view["snapshot"]["build_due"], eligible,
                "{blocker}/{state}: {view}"
            );
            assert_eq!(
                view["snapshot"]["next_retry_at"].is_number(),
                eligible && state == "failed",
                "{blocker}/{state}"
            );
            let setting = if blocker == "resources" {
                "declaration"
            } else if blocker == "home" {
                "image"
            } else if eligible {
                "snapshot"
            } else {
                blocker
            };
            assert_eq!(view["problem_setting"], setting);
        }
    }
}
