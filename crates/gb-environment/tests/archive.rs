//! Archive inspection never starts the runtime or touches its store.
use std::io::Write;
use std::path::Path;

fn archive(path: &Path, files: &[(&str, &str)], gzip: bool) {
    let file = std::fs::File::create(path).unwrap();
    let writer: Box<dyn Write> = if gzip {
        Box::new(flate2::write::GzEncoder::new(
            file,
            flate2::Compression::default(),
        ))
    } else {
        Box::new(file)
    };
    let mut tar = tar::Builder::new(writer);
    for (name, text) in files {
        let mut header = tar::Header::new_gnu();
        header.set_size(text.len() as u64);
        header.set_mode(0o644);
        header.set_cksum();
        tar.append_data(&mut header, name, text.as_bytes()).unwrap();
    }
    tar.into_inner().unwrap().flush().unwrap();
}

#[test]
fn docker_and_oci_metadata_are_checked_before_loading() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("image.tar");
    for gzip in [false, true] {
        for files in [
            vec![
                ("manifest.json", r#"[{"Config":"config.json"}]"#),
                ("config.json", r#"{"architecture":"arm64"}"#),
            ],
            vec![(
                "index.json",
                r#"{"manifests":[{"platform":{"architecture":"arm64"}}]}"#,
            )],
            vec![
                (
                    "index.json",
                    r#"{"manifests":[{"digest":"sha256:manifest"}]}"#,
                ),
                (
                    "blobs/sha256/manifest",
                    r#"{"config":{"digest":"sha256:config"}}"#,
                ),
                ("blobs/sha256/config", r#"{"architecture":"arm64"}"#),
            ],
            vec![
                ("index.json", r#"{"manifests":[{"digest":"sha256:index"}]}"#),
                (
                    "blobs/sha256/index",
                    r#"{"manifests":[{"digest":"sha256:manifest"}]}"#,
                ),
                (
                    "blobs/sha256/manifest",
                    r#"{"config":{"digest":"sha256:config"}}"#,
                ),
                ("blobs/sha256/config", r#"{"architecture":"arm64"}"#),
            ],
        ] {
            archive(&path, &files, gzip);
            assert_eq!(
                gb_environment::image_archive::architecture(&path).unwrap(),
                "arm64"
            );
            gb_environment::image_archive::validate(&path, "arm64").unwrap();
            let error = gb_environment::image_archive::validate(&path, "amd64")
                .unwrap_err()
                .to_string();
            assert!(error.contains("arm64") && error.contains("amd64"));
        }
    }
}

#[test]
fn absent_cpu_matches_frozen_policy_but_unreadable_metadata_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("image.tar");
    archive(
        &path,
        &[
            ("manifest.json", r#"[{"Config":"config.json"}]"#),
            ("config.json", "{}"),
        ],
        false,
    );
    gb_environment::image_archive::validate(&path, "amd64").unwrap();
    for files in [
        vec![],
        vec![("manifest.json", "not json")],
        vec![("manifest.json", r#"[{"Config":"missing.json"}]"#)],
        vec![("index.json", r#"{"manifests":[{"digest":null}]}"#)],
    ] {
        archive(&path, &files, false);
        assert!(gb_environment::image_archive::architecture(&path).is_err());
    }
    std::fs::write(&path, "not tar").unwrap();
    assert!(gb_environment::image_archive::architecture(&path).is_err());
}
