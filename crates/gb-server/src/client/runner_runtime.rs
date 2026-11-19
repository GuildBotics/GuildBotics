//! The Windows runner's pinned runtime, installed before taking any work.
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

pub use gb_runner::runner::executor::MSB_VERSION as VERSION;
const RELEASE: &str = "https://github.com/superradcompany/microsandbox/releases/download/v0.6.17";
const FILES: [(&str, &str, &str); 2] = [
    (
        "msb-windows-x86_64.exe",
        "bin/msb.exe",
        "8ed7fedf00e468efe746934e848723560487525ae00b42ff72dbf176670ce2db",
    ),
    (
        "libkrunfw-windows-x86_64.dll",
        "lib/libkrunfw.dll",
        "c3048ec759c5bc34708442b25ec9931a05fc2109f6428f3b41b133ff45352d7e",
    ),
];

fn matches(bytes: &[u8], expected: &str) -> bool {
    crate::client_release::digest(bytes) == expected
}

fn installed(root: &Path) -> bool {
    FILES.iter().all(|(_, path, hash)| {
        std::fs::read(root.join(path)).is_ok_and(|bytes| matches(&bytes, hash))
    })
}

/// Return only a fully verified runtime; interrupted installs remain retryable.
pub async fn prepare(root: &Path, language: &str) -> anyhow::Result<PathBuf> {
    let mismatch = || {
        anyhow::anyhow!(crate::i18n::t(
            language,
            "cli.server.runner_runtime_invalid",
            &[]
        ))
    };
    std::fs::create_dir_all(root)?;
    let lock_path = root.join("install.lock");
    let _lock = tokio::task::spawn_blocking(move || {
        gb_base::files::FileLock::hold(&lock_path, gb_base::files::SHARED_WRITE_TIMEOUT)
    })
    .await??;
    if !installed(root) {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(600))
            .build()?;
        // Verify both downloads before changing the installation.
        let mut downloads = Vec::new();
        for (asset, path, hash) in FILES {
            let bytes = http
                .get(format!("{RELEASE}/{asset}"))
                .send()
                .await?
                .error_for_status()?
                .bytes()
                .await?;
            anyhow::ensure!(matches(&bytes, hash), mismatch());
            downloads.push((path, bytes));
        }
        for (path, bytes) in downloads {
            let path = root.join(path);
            std::fs::create_dir_all(path.parent().unwrap())?;
            gb_base::files::atomic_write(&path, &bytes)?;
        }
    }
    let executable = root.join("bin/msb.exe");
    let output = tokio::time::timeout(
        Duration::from_secs(10),
        tokio::process::Command::new(&executable)
            .kill_on_drop(true)
            .arg("--version")
            .output(),
    )
    .await??;
    anyhow::ensure!(
        output.status.success()
            && String::from_utf8_lossy(&output.stdout).trim() == format!("msb {VERSION}"),
        mismatch()
    );
    Ok(executable)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_requires_both_pinned_files() {
        let root = tempfile::tempdir().unwrap();
        assert!(!installed(root.path()));
        for (_, path, _) in FILES {
            let path = root.path().join(path);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, b"untrusted").unwrap();
            assert!(!installed(root.path()));
        }
        assert!(matches(
            b"abc",
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        ));
        assert!(!matches(
            b"abd",
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        ));
        for (asset, _, hash) in FILES {
            assert!(asset.contains("windows-x86_64"));
            assert_eq!(hash.len(), 64);
            assert!(hash.bytes().all(|b| b.is_ascii_hexdigit()));
        }
    }
}
