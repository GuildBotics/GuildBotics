//! Check the forwarded Hub before any command consumes stdin or submits work.
use super::*;
use gb_base::files::{FileLock, SHARED_WRITE_TIMEOUT};

pub async fn prepare() -> anyhow::Result<Option<PathBuf>> {
    if gb_base::BUILD_VERSION.ends_with(".dev") {
        return Ok(None);
    }
    let language = language().await;
    let data = gb_base::home::data()?;
    // The Hub uses its checkout build and must not perform another authentication.
    if crate::endpoint::running(&data, language)?.is_some() {
        return Ok(None);
    }
    let (owner, local) = cli_server(&data, language).await?;
    if local {
        return Ok(None);
    }
    prepare_at(
        &owner,
        std::env::current_exe()?,
        gb_base::BUILD_VERSION,
        target(),
    )
    .await
}

fn target() -> &'static str {
    if cfg!(all(windows, target_arch = "x86_64", target_env = "gnu")) {
        crate::client_release::TARGET
    } else {
        "unsupported"
    }
}

async fn prepare_at(
    owner: &OwnerClient,
    executable: PathBuf,
    own_version: &str,
    target: &str,
) -> anyhow::Result<Option<PathBuf>> {
    if own_version.ends_with(".dev") {
        return Ok(None);
    }
    let language = owner.language;
    let unreachable = || anyhow::anyhow!(i18n::t(language, "cli.server.unreachable", &[]));
    let health = owner
        .request(reqwest::Method::GET, "/health")
        .timeout(TIMEOUT)
        .send()
        .await
        .map_err(|_| unreachable())?;
    let health: Value = response(health)
        .await
        .map_err(|error| {
            if error.is::<ServerAdvice>() {
                error
            } else {
                unreachable()
            }
        })?
        .json()
        .await
        .map_err(|_| unreachable())?;
    let version = health["version"].as_str().ok_or_else(|| {
        anyhow::anyhow!(i18n::t(language, "cli.server.update_invalid_version", &[]))
    })?;
    if version == own_version {
        return Ok(None);
    }
    if target == "unsupported" {
        anyhow::bail!(i18n::t(language, "cli.server.update_unsupported", &[]));
    }
    let result = async {
        let lock_path = executable.with_extension("update.lock");
        let _lock =
            tokio::task::spawn_blocking(move || FileLock::hold(&lock_path, SHARED_WRITE_TIMEOUT))
                .await??;
        // Another caller may already have installed this version while we waited.
        if std::process::Command::new(&executable)
            .arg("version")
            .output()
            .is_ok_and(|out| {
                out.status.success() && String::from_utf8_lossy(&out.stdout).trim() == version
            })
        {
            return Ok(Some(executable));
        }
        let path = format!("/clients/{target}");
        let manifest: Value = response(
            owner
                .request(reqwest::Method::GET, &path)
                .timeout(TIMEOUT)
                .send()
                .await?,
        )
        .await?
        .json()
        .await?;
        anyhow::ensure!(
            manifest["version"] == version && manifest["target"] == target,
            i18n::t(language, "cli.server.update_changed", &[])
        );
        let hash = manifest["sha256"].as_str().ok_or_else(|| {
            anyhow::anyhow!(i18n::t(language, "cli.server.update_missing_hash", &[]))
        })?;
        let bytes = response(
            owner
                .request(reqwest::Method::GET, &format!("{path}/binary"))
                .timeout(Duration::from_secs(120))
                .send()
                .await?,
        )
        .await?
        .bytes()
        .await?;
        crate::client_release::verify(&bytes, hash).map_err(|_| {
            anyhow::anyhow!(i18n::t(language, "cli.server.update_hash_mismatch", &[]))
        })?;
        replace(&executable, &bytes)?;
        eprintln!(
            "{}",
            i18n::t(
                language,
                "cli.server.update_installed",
                &[("version", version)]
            )
        );
        Ok(Some(executable))
    }
    .await;
    result.map_err(|error: anyhow::Error| {
        if error.is::<ServerAdvice>() {
            return error;
        }
        anyhow::anyhow!(i18n::t(
            language,
            "cli.server.update_failed",
            &[("reason", &error.to_string())]
        ))
    })
}

#[derive(Debug)]
struct ServerAdvice(String);
impl std::fmt::Display for ServerAdvice {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for ServerAdvice {}

async fn response(reply: reqwest::Response) -> anyhow::Result<reqwest::Response> {
    if reply.status().is_success() {
        return Ok(reply);
    }
    let status = reply.status();
    let value: Value = reply.json().await.unwrap_or_default();
    if let Some(message) = value["message"].as_str().filter(|s| !s.trim().is_empty()) {
        return Err(ServerAdvice(message.into()).into());
    }
    anyhow::bail!("HTTP {status}")
}

#[cfg(any(windows, test))]
fn cleanup_old(path: &Path) -> anyhow::Result<()> {
    let prefix = format!("{}.old-", path.file_name().unwrap().to_string_lossy());
    for entry in std::fs::read_dir(path.parent().unwrap())? {
        let entry = entry?;
        if entry.file_name().to_string_lossy().starts_with(&prefix) && entry.file_type()?.is_file()
        {
            let _ = std::fs::remove_file(entry.path());
        }
    }
    Ok(())
}

fn replace(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    #[cfg(windows)]
    {
        // Windows cannot overwrite a running image. Reserve a unique old name,
        // keep it until all processes using it exit, and restore it on failure.
        let prefix = format!("{}.old-", path.file_name().unwrap().to_string_lossy());
        cleanup_old(path)?;
        let old = tempfile::Builder::new()
            .prefix(&prefix)
            .tempfile_in(path.parent().unwrap())?
            .into_temp_path();
        std::fs::remove_file(&old)?;
        std::fs::rename(path, &old)?;
        if let Err(error) = gb_base::files::atomic_write(path, bytes) {
            std::fs::rename(&old, path)?;
            return Err(error.into());
        }
        let _ = old.keep();
    }
    #[cfg(not(windows))]
    gb_base::files::atomic_write_mode(path, bytes, Some(0o755))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cleanup_removes_only_this_executables_old_images() {
        let root = tempfile::tempdir().unwrap();
        let exe = root.path().join("client.exe");
        for name in [
            "client.exe",
            "client.exe.old-first",
            "client.exe.old-second",
            "other.exe.old-one",
            "user.tmp",
        ] {
            std::fs::write(root.path().join(name), "fixture").unwrap();
        }
        std::fs::create_dir(root.path().join("client.exe.old-directory")).unwrap();
        cleanup_old(&exe).unwrap();
        assert!(!root.path().join("client.exe.old-first").exists());
        assert!(!root.path().join("client.exe.old-second").exists());
        for name in [
            "client.exe",
            "other.exe.old-one",
            "user.tmp",
            "client.exe.old-directory",
        ] {
            assert!(root.path().join(name).exists());
        }
    }

    #[tokio::test]
    async fn connection_health_and_server_guidance_keep_one_remedy_in_both_languages() {
        use axum::{Json, Router, routing::get};
        for language in ["en", "ja"] {
            let advice = i18n::t(language, "cli.server.update_unavailable", &[]);
            let expected = advice.clone();
            let app = Router::new()
                .route("/health", get(|| async { Json(json!({"version":"new"})) }))
                .route(
                    "/clients/unavailable",
                    get(move || {
                        let text = advice.clone();
                        async move {
                            (
                                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                                Json(json!({"message":text})),
                            )
                        }
                    }),
                );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let base = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
            let mut owner = OwnerClient {
                http: http().unwrap(),
                base,
                token: "fixture".into(),
                language,
            };
            let dir = tempfile::tempdir().unwrap();
            let exe = dir.path().join("missing-client");
            let error = prepare_at(&owner, exe.clone(), "old", "unavailable")
                .await
                .unwrap_err();
            assert_eq!(error.to_string(), expected);
            let error = prepare_at(&owner, exe.clone(), "old", "unsupported")
                .await
                .unwrap_err();
            assert_eq!(
                error.to_string(),
                i18n::t(language, "cli.server.update_unsupported", &[])
            );
            owner.base.push_str("/missing");
            let error = prepare_at(&owner, exe, "old", "unavailable")
                .await
                .unwrap_err();
            assert_eq!(
                error.to_string(),
                i18n::t(language, "cli.server.unreachable", &[])
            );
            server.abort();
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn concurrent_calls_download_once_then_resume_and_failures_do_not_replace() {
        use axum::{Json, Router, routing::get};
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        let root = tempfile::tempdir().unwrap();
        let exe = root.path().join("guildbotics");
        let version = "0.1.0+aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let binary = format!("#!/bin/sh\nif [ \"$1\" = version ]; then echo {version}; else printf '%s\\n' \"$*\"; fi\n").into_bytes();
        let count = Arc::new(AtomicUsize::new(0));
        let calls = count.clone();
        let content = binary.clone();
        let hash = crate::client_release::digest(&binary);
        let wrong_content = binary.clone();
        let router = Router::new()
            .route(
                "/health",
                get(move || async move { Json(json!({"version":version})) }),
            )
            .route(
                "/clients/test",
                get(move || {
                    let hash = hash.clone();
                    async move { Json(json!({"version":version,"target":"test","sha256":hash})) }
                }),
            )
            .route(
                "/clients/test/binary",
                get(move || {
                    let calls = calls.clone();
                    let content = content.clone();
                    async move {
                        calls.fetch_add(1, Ordering::SeqCst);
                        content
                    }
                }),
            )
            .route(
                "/clients/bad",
                get(move || async move {
                    Json(json!({"version":version,"target":"bad","sha256":"incorrect"}))
                }),
            )
            .route(
                "/clients/bad/binary",
                get(move || {
                    let content = wrong_content.clone();
                    async move { content }
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let owner = OwnerClient {
            http: http().unwrap(),
            base,
            token: "fixture".into(),
            language: "en",
        };
        replace(&exe, b"#!/bin/sh\necho old\n").unwrap();
        let (a, b) = tokio::join!(
            prepare_at(&owner, exe.clone(), "old", "test"),
            prepare_at(&owner, exe.clone(), "old", "test")
        );
        assert_eq!(a.unwrap(), Some(exe.clone()));
        assert_eq!(b.unwrap(), Some(exe.clone()));
        assert_eq!(count.load(Ordering::SeqCst), 1);
        let output = std::process::Command::new(&exe)
            .args(["run", "original", "argument"])
            .output()
            .unwrap();
        assert_eq!(
            String::from_utf8(output.stdout).unwrap().trim(),
            "run original argument"
        );
        assert_eq!(
            prepare_at(&owner, exe.clone(), "old.dev", "test")
                .await
                .unwrap(),
            None
        );
        replace(&exe, b"#!/bin/sh\necho old\n").unwrap();
        assert!(
            prepare_at(&owner, exe.clone(), "old", "bad")
                .await
                .unwrap_err()
                .to_string()
                .contains("sha256")
        );
        assert!(
            prepare_at(&owner, exe.clone(), "old", "missing")
                .await
                .is_err()
        );
        assert_eq!(std::fs::read(&exe).unwrap(), b"#!/bin/sh\necho old\n");
        server.abort();
    }

    #[cfg(windows)]
    #[test]
    fn windows_replaces_a_running_image() {
        const PROBE: &str = "GB_TEST_RUNNING_CLIENT";
        if let Some(marker) = std::env::var_os(PROBE) {
            std::fs::write(&marker, "running").unwrap();
            let deadline = std::time::Instant::now() + Duration::from_secs(20);
            while Path::new(&marker).exists() && std::time::Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            return;
        }
        let directory = tempfile::tempdir().unwrap();
        let exe = directory.path().join("client.exe");
        let bytes = std::fs::read(std::env::current_exe().unwrap()).unwrap();
        std::fs::write(&exe, &bytes).unwrap();
        let marker = directory.path().join("running");
        let mut child = std::process::Command::new(&exe)
            .args([
                "--exact",
                "client::update::tests::windows_replaces_a_running_image",
            ])
            .env(PROBE, &marker)
            .spawn()
            .unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while !marker.exists() && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        if !marker.exists() {
            let _ = child.kill();
            let _ = child.wait();
            panic!("client did not start");
        }
        let result = replace(&exe, &bytes);
        std::fs::remove_file(marker).unwrap();
        assert!(child.wait().unwrap().success());
        result.unwrap();
        assert_eq!(std::fs::read(&exe).unwrap(), bytes);
        let backups = || {
            std::fs::read_dir(directory.path())
                .unwrap()
                .filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| {
                    p.file_name()
                        .unwrap()
                        .to_string_lossy()
                        .starts_with("client.exe.old-")
                })
                .collect::<Vec<_>>()
        };
        let previous = backups();
        assert_eq!(previous.len(), 1);
        replace(&exe, &bytes).unwrap();
        assert!(!previous[0].exists());
        assert_eq!(backups().len(), 1);
    }

    #[test]
    fn replacement_is_complete_and_hash_failure_preserves_the_executable() {
        let directory = tempfile::tempdir().unwrap();
        let executable = directory.path().join("client");
        std::fs::write(&executable, b"old").unwrap();
        assert!(
            crate::client_release::verify(b"tampered", &crate::client_release::digest(b"new"))
                .is_err()
        );
        assert_eq!(std::fs::read(&executable).unwrap(), b"old");
        replace(&executable, b"new").unwrap();
        assert_eq!(std::fs::read(executable).unwrap(), b"new");
    }
}
