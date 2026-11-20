//! The client distribution contract. A version directory is published only after verification.
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path};

#[derive(Clone, Copy, Debug)]
pub struct Target {
    pub triple: &'static str,
    pub artifact: &'static str,
    pub binary: &'static str,
}

pub const WINDOWS: Target = Target {
    triple: "x86_64-pc-windows-gnu",
    artifact: "client-windows",
    binary: "guildbotics.exe",
};
pub const LINUX: Target = Target {
    triple: "x86_64-unknown-linux-gnu",
    artifact: "client-linux",
    binary: "guildbotics",
};
pub const TARGETS: [Target; 2] = [WINDOWS, LINUX];

pub fn target(triple: &str) -> Option<Target> {
    TARGETS.into_iter().find(|target| target.triple == triple)
}

pub fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

pub fn verify(bytes: &[u8], expected: &str) -> anyhow::Result<()> {
    anyhow::ensure!(digest(bytes) == expected, "client sha256 mismatch");
    Ok(())
}

pub fn version(commit: &str) -> anyhow::Result<String> {
    anyhow::ensure!(
        commit.len() == 40 && commit.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid commit"
    );
    Ok(format!("{}+{commit}", env!("CARGO_PKG_VERSION")))
}

/// Read only the exact archive entries in the distribution contract.
fn unpack(target: Target, bytes: &[u8], commit: &str) -> anyhow::Result<Vec<u8>> {
    let mut archive = zip::ZipArchive::new(std::io::Cursor::new(bytes))?;
    let mut built_commit = String::new();
    archive
        .by_name("client-release/commit")?
        .take(41)
        .read_to_string(&mut built_commit)?;
    anyhow::ensure!(
        built_commit.trim() == commit,
        "client artifact belongs to another commit"
    );
    let mut binary = Vec::new();
    archive
        .by_name(&format!("client-release/{}", target.binary))?
        .take(128 * 1024 * 1024)
        .read_to_end(&mut binary)?;
    let mut hash = String::new();
    archive
        .by_name("client-release/sha256")?
        .take(65)
        .read_to_string(&mut hash)?;
    verify(&binary, hash.trim())?;
    Ok(binary)
}

fn directory(data: &Path, version: &str) -> std::path::PathBuf {
    data.join("clients").join(version)
}

/// Validate the entire set before publishing a version. The readiness marker is
/// written last, including when replacing a Windows-only release from an older Hub.
pub async fn prepare<F, Fut>(data: &Path, commit: &str, mut fetch: F) -> anyhow::Result<()>
where
    F: FnMut(&'static str) -> Fut,
    Fut: Future<Output = anyhow::Result<Vec<u8>>>,
{
    let version = version(commit)?;
    if TARGETS
        .iter()
        .all(|target| load(data, &version, *target).is_ok())
    {
        return Ok(());
    }
    let mut releases = Vec::with_capacity(TARGETS.len());
    for target in TARGETS {
        let archive = fetch(target.artifact).await?;
        releases.push((target, unpack(target, &archive, commit)?));
    }
    publish(
        data,
        &version,
        releases.try_into().expect("one release per target"),
    )
}

pub(crate) fn publish(
    data: &Path,
    version: &str,
    releases: [(Target, Vec<u8>); TARGETS.len()],
) -> anyhow::Result<()> {
    let directory = directory(data, version);
    for (target, binary) in releases {
        let path = directory.join(target.triple);
        std::fs::create_dir_all(&path)?;
        gb_base::files::atomic_write(&path.join("sha256"), digest(&binary).as_bytes())?;
        gb_base::files::atomic_write(&path.join(target.binary), &binary)?;
    }
    gb_base::files::atomic_write(&directory.join("ready"), b"")?;
    Ok(())
}

pub fn load(data: &Path, version: &str, target: Target) -> anyhow::Result<Vec<u8>> {
    let directory = directory(data, version);
    anyhow::ensure!(
        directory.join("ready").is_file()
            && TARGETS.iter().all(|target| {
                let path = directory.join(target.triple);
                path.join(target.binary).is_file() && path.join("sha256").is_file()
            }),
        "client release not published"
    );
    let directory = directory.join(target.triple);
    let bytes = std::fs::read(directory.join(target.binary))?;
    verify(&bytes, &std::fs::read_to_string(directory.join("sha256"))?)?;
    Ok(bytes)
}

#[cfg(test)]
pub(crate) fn fixture_archive(target: Target, commit: &str, bytes: &[u8], hash: &str) -> Vec<u8> {
    use std::io::Write;
    let mut zip = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    for (name, bytes) in [
        (target.binary, bytes),
        ("sha256", hash.as_bytes()),
        ("commit", commit.as_bytes()),
    ] {
        zip.start_file(
            format!("client-release/{name}"),
            zip::write::SimpleFileOptions::default(),
        )
        .unwrap();
        zip.write_all(bytes).unwrap();
    }
    zip.finish().unwrap().into_inner()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn archive_verification_rejects_wrong_target_commit_and_checksum() {
        for target in TARGETS {
            let archive = fixture_archive(target, "fixture", b"client", &digest(b"client"));
            assert_eq!(unpack(target, &archive, "fixture").unwrap(), b"client");
            assert!(unpack(target, &archive, "other").is_err());
            let corrupt = fixture_archive(target, "fixture", b"tampered", &digest(b"client"));
            assert!(unpack(target, &corrupt, "fixture").is_err());
            assert!(unpack(target, b"not a zip", "fixture").is_err());
        }
        let linux = fixture_archive(LINUX, "fixture", b"client", &digest(b"client"));
        assert!(unpack(WINDOWS, &linux, "fixture").is_err());
        assert!(target("../other").is_none());
    }

    #[tokio::test]
    async fn a_version_is_published_only_with_both_verified_targets() {
        let root = tempfile::tempdir().unwrap();
        let commit = "a".repeat(40);
        let version = version(&commit).unwrap();
        for missing in TARGETS {
            let result = prepare(root.path(), &commit, |artifact| {
                let target = TARGETS
                    .into_iter()
                    .find(|t| t.artifact == artifact)
                    .unwrap();
                std::future::ready(if artifact == missing.artifact {
                    Err(anyhow::anyhow!("artifact missing"))
                } else {
                    Ok(fixture_archive(
                        target,
                        &commit,
                        b"client",
                        &digest(b"client"),
                    ))
                })
            })
            .await;
            assert!(result.is_err());
        }
        assert!(!directory(root.path(), &version).exists());

        prepare(root.path(), &commit, |artifact| {
            let target = TARGETS
                .into_iter()
                .find(|t| t.artifact == artifact)
                .unwrap();
            let binary = artifact.as_bytes();
            std::future::ready(Ok(fixture_archive(
                target,
                &commit,
                binary,
                &digest(binary),
            )))
        })
        .await
        .unwrap();
        for target in TARGETS {
            assert_eq!(
                load(root.path(), &version, target).unwrap(),
                target.artifact.as_bytes()
            );
        }
        prepare(root.path(), &commit, |_| async {
            panic!("a complete release needs no fetch");
        })
        .await
        .unwrap();
        std::fs::remove_file(
            directory(root.path(), &version)
                .join(LINUX.triple)
                .join(LINUX.binary),
        )
        .unwrap();
        assert!(load(root.path(), &version, WINDOWS).is_err());
    }
}
