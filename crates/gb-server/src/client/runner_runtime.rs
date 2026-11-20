//! The remote runner's pinned runtime, installed before taking any work.
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

pub use gb_runner::runner::executor::MSB_VERSION as VERSION;
const RELEASE: &str = "https://github.com/superradcompany/microsandbox/releases/download/v0.6.17";
const WINDOWS: [(&str, &str, &str); 2] = [
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

const LINUX: [(&str, &str, &str); 2] = [
    (
        "msb-linux-x86_64",
        "bin/msb",
        "7f79c9d0996fac42b4879f4798c6f985f7981b005af0a9b4b8b1ab5e590daee4",
    ),
    (
        "libkrunfw-linux-x86_64.so",
        "lib/libkrunfw.so",
        "d395efaa21984cc6934c900519909a12c8148d9688cfc88f9da3b42132ae32c2",
    ),
];

pub struct Runtime {
    pub binary: PathBuf,
    pub firmware: Option<PathBuf>,
}

fn matches(bytes: &[u8], expected: &str) -> bool {
    crate::client_release::digest(bytes) == expected
}

fn installed(root: &Path, files: &[(&str, &str, &str)]) -> bool {
    files.iter().all(|(_, path, hash)| {
        std::fs::read(root.join(path)).is_ok_and(|bytes| matches(&bytes, hash))
    })
}

/// Return only a fully verified runtime; interrupted installs remain retryable.
pub async fn prepare(root: &Path, language: &str) -> anyhow::Result<Runtime> {
    let files = if cfg!(windows) { WINDOWS } else { LINUX };
    prepare_from(root, language, &files, RELEASE).await
}

async fn prepare_from(
    root: &Path,
    language: &str,
    files: &[(&str, &str, &str); 2],
    release: &str,
) -> anyhow::Result<Runtime> {
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
    if !installed(root, files) {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(600))
            .build()?;
        // Verify both downloads before changing the installation.
        let mut downloads = Vec::new();
        for &(asset, path, hash) in files {
            let bytes = http
                .get(format!("{release}/{asset}"))
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
            let mode = path.starts_with(root.join("bin")).then_some(0o755);
            gb_base::files::atomic_write_mode(&path, &bytes, mode)?;
        }
    }
    let executable = root.join(files[0].1);
    let firmware = (!cfg!(windows)).then(|| root.join(files[1].1));
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
    Ok(Runtime {
        binary: executable,
        firmware,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn linux_runtime_with_tampered_firmware_installs_neither_file() {
        use axum::{Router, routing::get};
        let app = Router::new()
            .route("/msb-linux-x86_64", get(|| async { "verified fixture" }))
            .route("/libkrunfw-linux-x86_64.so", get(|| async { "tampered" }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let release = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let root = tempfile::tempdir().unwrap();
        let hash = crate::client_release::digest(b"verified fixture");
        let files = [(LINUX[0].0, LINUX[0].1, hash.as_str()), LINUX[1]];
        let result = prepare_from(root.path(), "en", &files, &release).await;
        assert_eq!(
            result.err().unwrap().to_string(),
            crate::i18n::t("en", "cli.server.runner_runtime_invalid", &[])
        );
        for (_, path, _) in files {
            assert!(!root.path().join(path).exists());
        }
        server.abort();
    }

    #[test]
    fn each_runtime_requires_both_pinned_files() {
        for files in [WINDOWS, LINUX] {
            let root = tempfile::tempdir().unwrap();
            assert!(!installed(root.path(), &files));
            for (asset, path, hash) in files {
                let path = root.path().join(path);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(path, b"untrusted").unwrap();
                assert!(!installed(root.path(), &files), "{asset}");
                assert_eq!(hash.len(), 64);
                assert!(hash.bytes().all(|b| b.is_ascii_hexdigit()));
            }
        }
        let hash = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        assert!(matches(b"abc", hash));
        assert!(!matches(b"abd", hash));
    }
}
