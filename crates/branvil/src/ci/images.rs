//! Architecture-specific CI archives, owned by the Hub and served to runners.
use crate::util;
use std::io::Read;
use std::path::{Path, PathBuf};

pub use gb_runner::ci::images::{Archive, Image, architecture, same_reference};
fn directory(root: &Path) -> PathBuf {
    root.join("ci/images")
}
pub fn path(root: &Path, id: &str) -> anyhow::Result<PathBuf> {
    anyhow::ensure!(
        id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit()),
        "Invalid archive id"
    );
    Ok(directory(root).join(format!("{id}.tar")))
}

pub fn list(root: &Path) -> anyhow::Result<Vec<Archive>> {
    let path = directory(root).join("catalog.json");
    match std::fs::read(path) {
        Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(e) => Err(e.into()),
    }
}

/// A Hub-managed tag is eligible only at the currently published CPU-specific digest.
pub fn current(root: &Path, images: Vec<Image>) -> anyhow::Result<Vec<Image>> {
    let managed = list(root)?;
    Ok(images
        .into_iter()
        .filter(|i| {
            let expected = managed.iter().flat_map(|a| &a.images).find(|m| {
                same_reference(&m.reference, &i.reference) && m.architecture == i.architecture
            });
            expected.is_none_or(|m| m.digest == i.digest)
        })
        .collect())
}

/// Read only bounded metadata entries; layers remain on disk.
fn metadata(path: &Path, wanted: &str) -> anyhow::Result<Vec<u8>> {
    let mut archive = tar::Archive::new(std::fs::File::open(path)?);
    for entry in archive.entries_with_seek()? {
        let mut entry = entry?;
        if entry.path()?.to_string_lossy().trim_start_matches("./") == wanted {
            anyhow::ensure!(entry.size() <= 4 << 20, "Image metadata is too large");
            let mut bytes = Vec::new();
            entry.read_to_end(&mut bytes)?;
            return Ok(bytes);
        }
    }
    anyhow::bail!("Archive is missing {wanted}")
}

pub fn inspect(path: &Path) -> anyhow::Result<Vec<Image>> {
    let docker = metadata(path, "manifest.json");
    let manifests: Vec<serde_json::Value> = if let Ok(bytes) = docker {
        serde_json::from_slice(&bytes)?
    } else {
        let index: serde_json::Value = serde_json::from_slice(&metadata(path, "index.json")?)?;
        let mut manifests = Vec::new();
        for item in index["manifests"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("Missing OCI manifests"))?
        {
            let blob = |digest: &serde_json::Value| -> anyhow::Result<String> {
                let hex = digest
                    .as_str()
                    .and_then(|s| s.strip_prefix("sha256:"))
                    .ok_or_else(|| anyhow::anyhow!("Invalid OCI digest"))?;
                anyhow::ensure!(
                    hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()),
                    "Invalid OCI digest"
                );
                Ok(format!("blobs/sha256/{hex}"))
            };
            let manifest: serde_json::Value =
                serde_json::from_slice(&metadata(path, &blob(&item["digest"])?)?)?;
            manifests.push(serde_json::json!({"Config": blob(&manifest["config"]["digest"])?, "RepoTags": [item["annotations"]["org.opencontainers.image.ref.name"]]}));
        }
        manifests
    };
    anyhow::ensure!(
        manifests.len() == 1,
        "Use one image per CI archive (multiple tags are allowed)"
    );
    let mut images = Vec::new();
    for manifest in manifests {
        let config = manifest["Config"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing image config"))?;
        let bytes = metadata(path, config)?;
        let value: serde_json::Value = serde_json::from_slice(&bytes)?;
        let arch = value["architecture"].as_str().unwrap_or_default();
        anyhow::ensure!(
            value["os"] == "linux" && matches!(arch, "arm64" | "amd64"),
            "CI images must be Linux arm64 or amd64"
        );
        let digest = format!("sha256:{}", util::hex(&util::sha256(&bytes)));
        for tag in manifest["RepoTags"].as_array().into_iter().flatten() {
            let reference = tag
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("Invalid image tag"))?;
            anyhow::ensure!(super::pipeline::valid_image(reference), "Invalid image tag");
            images.push(Image {
                reference: reference.into(),
                architecture: arch.into(),
                digest: digest.clone(),
            });
        }
    }
    anyhow::ensure!(!images.is_empty(), "Archive has no tagged image");
    anyhow::ensure!(
        images
            .iter()
            .all(|i| i.architecture == images[0].architecture),
        "Use a separate archive for each CPU"
    );
    Ok(images)
}

/// Copy before inspecting so the metadata describes exactly the bytes served.
pub fn save(root: &Path, source: &Path) -> anyhow::Result<Archive> {
    use sha2::{Digest, Sha256};
    let dir = directory(root);
    std::fs::create_dir_all(&dir)?;
    let mut tmp = tempfile::NamedTempFile::new_in(&dir)?;
    std::io::copy(&mut std::fs::File::open(source)?, &mut tmp)?;
    let images = inspect(tmp.path())?;
    let mut hash = Sha256::new();
    let mut file = std::fs::File::open(tmp.path())?;
    let mut buf = [0; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
    }
    let id = util::hex(&hash.finalize());
    let target = path(root, &id)?;
    if !target.exists() {
        tmp.persist(&target)?;
    }
    let archive = Archive { images, id };
    let mut catalog = list(root)?;
    for old in &mut catalog {
        old.images.retain(|i| {
            !archive.images.iter().any(|new| {
                same_reference(&new.reference, &i.reference) && new.architecture == i.architecture
            })
        });
    }
    catalog.retain(|a| !a.images.is_empty());
    catalog.push(archive.clone());
    let mut meta = tempfile::NamedTempFile::new_in(&dir)?;
    serde_json::to_writer(&mut meta, &catalog)?;
    meta.persist(dir.join("catalog.json"))?;
    for entry in std::fs::read_dir(&dir)? {
        let path = entry?.path();
        if path.extension().is_some_and(|e| e == "tar")
            && !catalog
                .iter()
                .any(|a| path.file_stem().is_some_and(|s| s == a.id.as_str()))
        {
            // An already-open stream keeps its file; a stale catalog reader retries on its next pass.
            std::fs::remove_file(path)?;
        }
    }
    Ok(archive)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    pub fn docker(architecture: &str, version: &str) -> tempfile::NamedTempFile {
        docker_tagged(architecture, version, "test/image:dev")
    }
    pub fn docker_tagged(architecture: &str, version: &str, tag: &str) -> tempfile::NamedTempFile {
        let file = tempfile::NamedTempFile::new().unwrap();
        let config = serde_json::json!({
            "architecture": architecture, "os": "linux", "version": version,
            "rootfs": {"type": "layers", "diff_ids": []}, "config": {},
        })
        .to_string();
        let manifest =
            serde_json::json!([{"Config": "config.json", "RepoTags": [tag], "Layers": []}])
                .to_string();
        let mut tar = tar::Builder::new(file.reopen().unwrap());
        for (name, value) in [
            ("manifest.json", manifest.as_str()),
            ("config.json", config.as_str()),
        ] {
            let mut h = tar::Header::new_gnu();
            h.set_size(value.len() as u64);
            h.set_mode(0o644);
            h.set_cksum();
            tar.append_data(&mut h, name, value.as_bytes()).unwrap();
        }
        tar.finish().unwrap();
        file
    }
    #[test]
    fn registry_aliases_use_the_executors_reference_parser() {
        let image = Image {
            reference: "docker.io/library/alpine:latest".into(),
            architecture: "arm64".into(),
            digest: "sha256:test".into(),
        };
        assert!(image.supports("alpine"));
        assert!(image.supports("alpine:latest"));
        assert!(!image.supports("other.example/library/alpine:latest"));
    }
    #[test]
    fn architectures_and_replacement_are_independent() {
        let dir = tempfile::tempdir().unwrap();
        let arm = save(dir.path(), docker("arm64", "1").path()).unwrap();
        let amd = save(dir.path(), docker("amd64", "1").path()).unwrap();
        assert_ne!(arm.images[0].digest, amd.images[0].digest);
        let new = save(dir.path(), docker("amd64", "2").path()).unwrap();
        let catalog = list(dir.path()).unwrap();
        assert_eq!(catalog.len(), 2);
        assert!(catalog.iter().any(|a| a.images == arm.images));
        assert!(catalog.iter().any(|a| a.images == new.images));
        assert!(!catalog.iter().any(|a| a.images == amd.images));
        assert!(current(dir.path(), amd.images.clone()).unwrap().is_empty());
        assert_eq!(current(dir.path(), new.images.clone()).unwrap(), new.images);
        assert!(!path(dir.path(), &amd.id).unwrap().exists());
        assert!(path(dir.path(), "../../outside").is_err());
        assert!(save(dir.path(), docker("ppc64", "1").path()).is_err());
    }
}
