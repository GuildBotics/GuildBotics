//! The runtime's images go once no snapshot and no microVM uses them. This
//! drives the real runtime store on images and snapshot descriptors written
//! here, without booting a microVM; one test per process, as the runtime is.
use gb_environment::{image::device_architecture, runtime, snapshot::snapshots_dir};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

const TAG: &str = "test/sweep:dev";
const CURRENT: &str = "guildbotics-bbbbbbbbbbbbbbbb";

fn hex(bytes: &[u8]) -> String {
    let digest: String = Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    format!("sha256:{digest}")
}

fn tar(files: &[(&str, &[u8])]) -> Vec<u8> {
    let mut tar = tar::Builder::new(Vec::new());
    for (name, bytes) in files {
        let mut header = tar::Header::new_gnu();
        header.set_size(bytes.len() as u64);
        header.set_mode(0o644);
        header.set_cksum();
        tar.append_data(&mut header, name, *bytes).unwrap();
    }
    tar.into_inner().unwrap()
}

/// A `docker save` archive of a base layer every version shares and one of
/// its own, with the diff ID of its own layer.
fn archive(path: &Path, tag: &str, content: &str) -> String {
    let layers = [
        tar(&[("base.txt", b"shared base\n")]),
        tar(&[("version.txt", content.as_bytes())]),
    ];
    let ids: Vec<String> = layers.iter().map(|l| hex(l)).collect();
    let config = serde_json::to_vec(&serde_json::json!({
        "architecture": device_architecture(), "os": "linux",
        "config": {"Cmd": ["/bin/sh"]},
        "rootfs": {"type": "layers", "diff_ids": ids},
    }))
    .unwrap();
    let config_name = format!("{}.json", &hex(&config)[7..]);
    let names: Vec<String> = ids
        .iter()
        .map(|id| format!("{}/layer.tar", &id[7..]))
        .collect();
    let manifest = serde_json::to_vec(&serde_json::json!([
        {"Config": config_name, "RepoTags": [tag], "Layers": names}
    ]))
    .unwrap();
    let mut files: Vec<(&str, &[u8])> = vec![("manifest.json", &manifest), (&config_name, &config)];
    for (name, layer) in names.iter().zip(&layers) {
        files.push((name, layer));
    }
    std::fs::write(path, tar(&files)).unwrap();
    ids[1].clone()
}

/// Load an archive and return the manifest digest the store holds it under.
async fn load(home: &Path, archive: &Path) -> String {
    let loaded = runtime::load_image(home, archive, None).await.unwrap();
    microsandbox::Image::inspect(&loaded[0].0)
        .await
        .unwrap()
        .handle
        .manifest_digest()
        .unwrap()
        .to_owned()
}

/// A snapshot descriptor as the runtime writes one after a build.
fn snapshot(workspace: &Path, name: &str, manifest: &str) -> PathBuf {
    let path = snapshots_dir(workspace).join(name);
    std::fs::create_dir_all(&path).unwrap();
    std::fs::write(path.join("upper.ext4"), vec![0; 4096]).unwrap();
    let descriptor = serde_json::json!({
        "schema": 1, "artifact": "snapshot", "scope": "disk",
        "created_at": "2026-11-12T00:00:00Z", "parent": null,
        "image": {"ref": TAG, "manifest_digest": manifest},
        "source_sandbox": null,
        "state": {"kind": "file", "format": "raw", "fstype": "ext4",
            "upper": {"file": "upper.ext4", "size_bytes": 4096, "integrity": null}},
        "labels": {}, "extensions": {}, "requires": [],
    });
    std::fs::write(path.join("snapshot.json"), descriptor.to_string()).unwrap();
    path
}

fn layer(home: &Path, diff_id: &str) -> bool {
    home.join("cache/layers")
        .join(format!("{}.erofs", diff_id.replace(':', "_")))
        .exists()
}

async fn held() -> Vec<(String, String)> {
    let mut held: Vec<_> = microsandbox::Image::list()
        .await
        .unwrap()
        .iter()
        .map(|h| {
            (
                h.reference().to_owned(),
                h.manifest_digest().unwrap().to_owned(),
            )
        })
        .collect();
    held.sort();
    held
}

#[tokio::test]
async fn images_of_snapshots_stay_and_replaced_or_unused_images_go() {
    let root = tempfile::Builder::new()
        .prefix("gb")
        .tempdir_in("/tmp")
        .unwrap();
    let home = root.path().join("msb");
    let (built, other, gone) = (
        root.path().join("built"),
        root.path().join("other"),
        root.path().join("gone"),
    );
    let indexed = [snapshots_dir(&built), snapshots_dir(&other)];

    let v1 = archive(&root.path().join("v1.tar"), TAG, "one\n");
    let m1 = load(&home, &root.path().join("v1.tar")).await;
    let stale_layer = archive(&root.path().join("stale.tar"), "test/stale:x", "stale\n");
    let stale = load(&home, &root.path().join("stale.tar")).await;
    let unused_layer = archive(&root.path().join("unused.tar"), "test/unused:x", "unused\n");
    load(&home, &root.path().join("unused.tar")).await;
    // Indexed while it exists, then gone: its row outlives it.
    let gone_path = snapshot(&gone, "guildbotics-cccccccccccccccc", &stale);
    microsandbox::Snapshot::reindex(snapshots_dir(&gone))
        .await
        .unwrap();
    std::fs::remove_dir_all(&gone_path).unwrap();
    // The previous build of this workspace and another workspace's, of v1.
    let previous = snapshot(&built, "guildbotics-aaaaaaaaaaaaaaaa", &m1);
    let shared = snapshot(&other, "guildbotics-dddddddddddddddd", &m1);

    // Reloading the tag leaves v1 with no reference: only its snapshots name it.
    archive(&root.path().join("v2.tar"), TAG, "two\n");
    let m2 = load(&home, &root.path().join("v2.tar")).await;
    let current = snapshot(&built, CURRENT, &m2);
    let prebuilt = snapshot(&built, "guildbotics-eeeeeeeeeeeeeeee", &m2);

    // A load in progress defers the sweep, removing nothing.
    {
        let _loading = gb_base::files::FileLock::try_hold(&home.join("images.lock"))
            .unwrap()
            .unwrap();
        assert_eq!(
            runtime::sweep_images(&home, &indexed).await.unwrap_err(),
            "An image load or a build is starting"
        );
        assert!(layer(&home, &unused_layer));
    }

    runtime::retire(&home, &built, CURRENT, &indexed)
        .await
        .unwrap();
    assert!(!previous.exists(), "the previous image's snapshot goes");
    assert!(current.exists() && prebuilt.exists() && shared.exists());
    assert_eq!(held().await, vec![(TAG.into(), m2.clone())]);
    assert!(layer(&home, &v1), "another workspace's snapshot keeps v1");
    assert!(!layer(&home, &stale_layer) && !layer(&home, &unused_layer));
    let rows: Vec<PathBuf> = microsandbox::Snapshot::list()
        .await
        .unwrap()
        .iter()
        .map(|s| s.path().to_owned())
        .collect();
    assert!(
        !rows.contains(&gone_path),
        "the row of a removed snapshot goes"
    );

    // Once the other workspace drops its snapshot, v1 goes; shared layers stay.
    std::fs::remove_dir_all(&shared).unwrap();
    runtime::sweep_images(&home, &indexed).await.unwrap();
    assert!(!layer(&home, &v1));
    assert_eq!(held().await, vec![(TAG.into(), m2)]);
    let base = hex(&tar(&[("base.txt", b"shared base\n")]));
    assert!(layer(&home, &base));
}
