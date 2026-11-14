//! The client distribution contract. A version directory is published only after verification.
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path};

pub const TARGET: &str = "x86_64-pc-windows-gnu";
pub const ARTIFACT: &str = "client-windows";
pub const BINARY: &str = "guildbotics.exe";

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

/// Do not extract archive paths: only two exact entries belong to this contract.
pub fn unpack(bytes: &[u8], commit: &str) -> anyhow::Result<Vec<u8>> {
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
        .by_name("client-release/guildbotics.exe")?
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

pub fn store(data: &Path, version: &str, binary: &[u8]) -> anyhow::Result<()> {
    let directory = data.join("clients").join(version).join(TARGET);
    std::fs::create_dir_all(&directory)?;
    gb_base::files::atomic_write(&directory.join("sha256"), digest(binary).as_bytes())?;
    gb_base::files::atomic_write(&directory.join(BINARY), binary)?;
    Ok(())
}

pub fn load(data: &Path, version: &str, target: &str) -> anyhow::Result<Vec<u8>> {
    anyhow::ensure!(target == TARGET, "unsupported client target");
    let directory = data.join("clients").join(version).join(target);
    let bytes = std::fs::read(directory.join(BINARY))?;
    verify(&bytes, &std::fs::read_to_string(directory.join("sha256"))?)?;
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn archive_verification_rejects_tampering_before_storage() {
        use std::io::Write;
        let archive = |bytes: &[u8], checksum: &str| {
            let mut zip = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
            let options = zip::write::SimpleFileOptions::default();
            zip.start_file("client-release/guildbotics.exe", options)
                .unwrap();
            zip.write_all(bytes).unwrap();
            zip.start_file("client-release/sha256", options).unwrap();
            zip.write_all(checksum.as_bytes()).unwrap();
            zip.start_file("client-release/commit", options).unwrap();
            zip.write_all(b"fixture").unwrap();
            zip.finish().unwrap().into_inner()
        };
        assert_eq!(
            unpack(&archive(b"client", &digest(b"client")), "fixture").unwrap(),
            b"client"
        );
        assert!(unpack(&archive(b"tampered", &digest(b"client")), "fixture").is_err());
        assert!(unpack(b"not a zip", "fixture").is_err());
        assert!(unpack(&archive(b"client", &digest(b"client")), "other").is_err());
    }

    #[test]
    fn distribution_is_scoped_to_the_commit_and_target() {
        let root = tempfile::tempdir().unwrap();
        let v = version(&"a".repeat(40)).unwrap();
        assert!(load(root.path(), &v, TARGET).is_err());
        store(root.path(), &v, b"client").unwrap();
        assert_eq!(load(root.path(), &v, TARGET).unwrap(), b"client");
        assert!(load(root.path(), &version(&"b".repeat(40)).unwrap(), TARGET).is_err());
        assert!(load(root.path(), &v, "../other").is_err());
        assert!(verify(b"wrong", &digest(b"client")).is_err());
    }
}
