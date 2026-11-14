//! Device-local receipts survive an interrupted CLI, without storing file contents.
use gb_environment::worktree::Copy;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Saved {
    host: PathBuf,
    guest: String,
    excluded: Vec<String>,
    pub copied: String,
    #[cfg(unix)]
    root_identity: (u64, u64),
}

fn path(data: &Path, id: &str) -> anyhow::Result<PathBuf> {
    anyhow::ensure!(
        !id.is_empty()
            && id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
            && id != "."
            && id != "..",
        "Invalid receipt ID"
    );
    Ok(data
        .join("run/worktree-receipts")
        .join(format!("{id}.json")))
}

pub(super) fn save(data: &Path, id: &str, copy: &Copy, copied: &[u8]) -> anyhow::Result<()> {
    let saved = Saved {
        host: copy.host.clone(),
        guest: copy.guest.clone(),
        excluded: copy.excluded.clone(),
        copied: String::from_utf8(copied.to_vec())?,
        #[cfg(unix)]
        root_identity: root_identity(&copy.host)?,
    };
    gb_base::files::atomic_write_mode(&path(data, id)?, &serde_json::to_vec(&saved)?, Some(0o600))?;
    Ok(())
}

pub(super) fn load(data: &Path, id: &str) -> anyhow::Result<Option<Saved>> {
    match std::fs::read(path(data, id)?) {
        Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

impl Saved {
    pub fn copy(&self, data: &Path) -> anyhow::Result<Copy> {
        #[cfg(unix)]
        anyhow::ensure!(
            root_identity(&self.host)? == self.root_identity,
            "Working directory was replaced"
        );
        Copy::new(
            self.host.clone(),
            self.guest.clone(),
            self.excluded.clone(),
            data.join("run/worktree-write-back.lock"),
        )
        .map_err(anyhow::Error::msg)
    }
}

#[cfg(unix)]
fn root_identity(path: &Path) -> std::io::Result<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    let metadata = std::fs::symlink_metadata(path)?;
    Ok((metadata.dev(), metadata.ino()))
}

pub(super) fn remove(data: &Path, id: &str) -> anyhow::Result<()> {
    match std::fs::remove_file(path(data, id)?) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.into()),
    }
}

// A transport failure or server error can hide accepted work. Only a
// definitive client rejection makes the export baseline disposable here.
pub(super) async fn submission(
    response: reqwest::Response,
    data: &Path,
    id: &str,
) -> anyhow::Result<()> {
    if response.status().is_client_error()
        && response.status() != reqwest::StatusCode::REQUEST_TIMEOUT
    {
        remove(data, id)?;
    }
    super::checked(response).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::super::{OwnerClient, collect_command};
    use super::*;
    use gb_environment::worktree::transfer::Upload;

    async fn serve(
        status: reqwest::StatusCode,
        answer: serde_json::Value,
    ) -> (OwnerClient, tokio::task::JoinHandle<()>) {
        let router = axum::Router::new().route(
            "/commands/runs/{id}",
            axum::routing::get(move || {
                let answer = answer.clone();
                async move { (status, axum::Json(answer)) }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let owner = OwnerClient {
            http: super::super::http().unwrap(),
            base: format!("http://{}", listener.local_addr().unwrap()),
            token: "test".into(),
            language: "en",
        };
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        (owner, server)
    }

    #[test]
    fn absent_receipt_message_explains_completed_writeback_in_both_languages() {
        for (language, expected) in [("en", "already written back"), ("ja", "書き戻し済み")] {
            assert!(
                crate::i18n::t(language, "cli.run.resume_no_writeback", &[]).contains(expected)
            );
        }
    }

    #[tokio::test]
    async fn terminal_receipts_are_removed_but_uncertain_results_remain() {
        for (status, answer, remains) in [
            (
                200,
                serde_json::json!({"state":"finished","status":500}),
                false,
            ),
            (
                200,
                serde_json::json!({"state":"finished","status":200,"result":{"output":"done"}}),
                false,
            ),
            (
                404,
                serde_json::json!({"code":"command_result_missing"}),
                true,
            ),
            (500, serde_json::json!({}), true),
            (401, serde_json::json!({}), true),
            (200, serde_json::json!({"state":"broken"}), true),
            (
                200,
                serde_json::json!({"state":"finished","status":200,"result":null}),
                true,
            ),
            (
                200,
                serde_json::json!({"state":"finished","status":200,"result":{"output":"done","worktree_changes":42}}),
                true,
            ),
        ] {
            let data = tempfile::tempdir().unwrap();
            let root = tempfile::tempdir().unwrap();
            let copy = Copy::new(
                root.path().into(),
                "/work".into(),
                vec![],
                data.path().join("lock"),
            )
            .unwrap();
            save(data.path(), "run.2", &copy, b"").unwrap();
            let succeeds = status == 200 && !remains && answer["status"] == 200;
            let (owner, server) =
                serve(reqwest::StatusCode::from_u16(status).unwrap(), answer).await;
            for _ in 0..2 {
                let result = collect_command(&owner, "run.2", "en", data.path()).await;
                assert_eq!(result.is_ok(), succeeds);
                assert_eq!(
                    load(data.path(), "run.2").unwrap().is_some(),
                    remains,
                    "HTTP {status}"
                );
            }
            server.abort();
        }
    }

    #[tokio::test]
    async fn submission_rejections_discard_only_definitive_failures() {
        for (status, remains) in [
            (200, true),
            (409, false),
            (422, false),
            (404, false),
            (429, false),
            (408, true),
            (500, true),
        ] {
            let data = tempfile::tempdir().unwrap();
            let root = tempfile::tempdir().unwrap();
            let copy = Copy::new(
                root.path().into(),
                "/work".into(),
                vec![],
                data.path().join("lock"),
            )
            .unwrap();
            save(data.path(), "run.3", &copy, b"").unwrap();
            let (owner, server) = serve(
                reqwest::StatusCode::from_u16(status).unwrap(),
                serde_json::json!({}),
            )
            .await;
            let response = owner
                .request(reqwest::Method::GET, "/commands/runs/run.3")
                .send()
                .await
                .unwrap();
            assert_eq!(
                submission(response, data.path(), "run.3").await.is_ok(),
                status == 200
            );
            assert_eq!(
                load(data.path(), "run.3").unwrap().is_some(),
                remains,
                "HTTP {status}"
            );
            server.abort();
        }
    }

    #[cfg(unix)]
    #[test]
    fn reloaded_receipt_refuses_a_replaced_root() {
        let data = tempfile::tempdir().unwrap();
        let root = data.path().join("root");
        std::fs::create_dir(&root).unwrap();
        let copy = Copy::new(
            root.clone(),
            "/work".into(),
            vec![],
            data.path().join("lock"),
        )
        .unwrap();
        save(data.path(), "run.1", &copy, b"").unwrap();
        drop(copy);
        std::fs::rename(&root, data.path().join("moved")).unwrap();
        std::fs::create_dir(&root).unwrap();
        assert!(
            load(data.path(), "run.1")
                .unwrap()
                .unwrap()
                .copy(data.path())
                .is_err()
        );
        for id in ["", "../escape", "..", "."] {
            assert!(load(data.path(), id).is_err());
        }
    }

    #[tokio::test]
    async fn receipt_reload_keeps_conflicts_atomic_and_is_removed_after_success() {
        let data = tempfile::tempdir().unwrap();
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("a"), "old\r\n").unwrap();
        std::fs::write(root.path().join("b"), "old\r\n").unwrap();
        let copy = Copy::new(
            root.path().into(),
            "/work".into(),
            vec![],
            data.path().join("lock"),
        )
        .unwrap();
        let baseline = Upload::capture(&copy).unwrap().copied();
        save(data.path(), "run.1", &copy, &baseline).unwrap();
        drop(copy);

        let changes =
            b"{\"path\":\"a\",\"content\":\"bmV3DQo=\"}\n{\"path\":\"b\",\"content\":\"bmV3DQo=\"}";
        let answer = serde_json::json!({"state":"finished","status":200,
            "result":{"output":"done", "worktree_changes":std::str::from_utf8(changes).unwrap()}});
        let (owner, server) = serve(reqwest::StatusCode::OK, answer).await;
        std::fs::write(root.path().join("b"), "edited").unwrap();
        let error = collect_command(&owner, "run.1", "en", data.path())
            .await
            .unwrap_err();
        assert!(error.to_string().contains("b"));
        assert_eq!(
            std::fs::read_to_string(root.path().join("a")).unwrap(),
            "old\r\n"
        );
        assert!(load(data.path(), "run.1").unwrap().is_some());
        std::fs::write(root.path().join("b"), "old\r\n").unwrap();
        assert_eq!(
            collect_command(&owner, "run.1", "en", data.path())
                .await
                .unwrap(),
            "done"
        );
        assert_eq!(
            collect_command(&owner, "run.1", "en", data.path())
                .await
                .unwrap(),
            "done"
        );
        server.abort();
        assert!(load(data.path(), "run.1").unwrap().is_none());
        assert_eq!(
            std::fs::read_to_string(root.path().join("a")).unwrap(),
            "new\r\n"
        );
        assert_eq!(
            std::fs::read_to_string(root.path().join("b")).unwrap(),
            "new\r\n"
        );
    }
}
