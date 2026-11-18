//! Local Hub maintenance on the server of this machine: `base` is its base
//! URL and `token` its owner's token, which the caller reads from disk.

use std::sync::Arc;

use anyhow::Context;
use serde_json::{Value, json};

use crate::{vault, webhook};

/// The checkout's origin must name this Hub, never an arbitrary token recipient.
pub fn repository(origin: &str, base: &str) -> anyhow::Result<String> {
    let base = reqwest::Url::parse(base)?;
    let url = reqwest::Url::parse(origin.trim())?;
    anyhow::ensure!(
        url.origin() == base.origin()
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none(),
        "origin does not name this Hub"
    );
    let prefix = format!("{}/git/", base.path().trim_end_matches('/'));
    let repo = url.path().strip_prefix(&prefix).context("origin path")?;
    let repo = repo.strip_suffix(".git").unwrap_or(repo);
    validate_repository(repo)?;
    Ok(repo.into())
}

fn validate_repository(repo: &str) -> anyhow::Result<()> {
    let parts: Vec<_> = repo.split('/').collect();
    anyhow::ensure!(
        parts.len() == 2
            && parts.iter().all(|part| {
                !part.is_empty()
                    && *part != "."
                    && *part != ".."
                    && part
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            }),
        "invalid repository"
    );
    Ok(())
}

/// Read exactly the local Hub, bypassing proxies and redirects.
pub async fn verified(
    base: &str,
    token: &str,
    repo: Option<&str>,
    origin: Option<&str>,
    since: &str,
) -> anyhow::Result<Value> {
    let repo = match repo {
        Some(repo) => {
            validate_repository(repo)?;
            repo.to_owned()
        }
        None => repository(origin.context("checkout origin is missing")?, base)?,
    };
    anyhow::ensure!(
        since.len() == 40 && since.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid commit SHA"
    );
    let repo = crate::util::encode_segment(&repo);
    let http = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(10))
        .build()?;
    let get = async |path: String| -> anyhow::Result<Value> {
        // Never render a response body or a request (both can contain secrets).
        let response = http
            .get(format!("{base}/api/v1/repos/{repo}{path}"))
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| e.without_url())?;
        anyhow::ensure!(
            response.status().is_success(),
            "Branvil HTTP {}",
            response.status()
        );
        Ok(response.json().await.map_err(|e| e.without_url())?)
    };
    let mut verified = Vec::new();
    let mut cursor = None::<String>;
    let mut seen = std::collections::HashSet::new();
    loop {
        let path = match &cursor {
            Some(cursor) => format!(
                "/verification/commits?cursor={}",
                crate::util::encode_segment(cursor)
            ),
            None => "/verification/commits".into(),
        };
        let page = get(path).await?;
        for item in page["items"].as_array().context("items missing")? {
            let sha = item["sha"].as_str().context("commit SHA missing")?;
            anyhow::ensure!(
                sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()),
                "invalid commit SHA"
            );
            if sha.eq_ignore_ascii_case(since) {
                return Ok(json!({"since_on_main":true,"verified":verified}));
            }
            if item["mark"] == "verified" {
                verified.push(sha.to_owned());
            }
        }
        cursor = match &page["next_cursor"] {
            Value::Null => return Ok(json!({"since_on_main":false,"verified":verified})),
            Value::String(value) => Some(value.clone()),
            _ => anyhow::bail!("invalid verification cursor"),
        };
        anyhow::ensure!(seen.insert(cursor.clone()), "repeated verification cursor");
    }
}

/// Download a named artifact from the local Hub only.
pub async fn artifact(
    base: &str,
    token: &str,
    origin: &str,
    sha: &str,
    name: &str,
) -> anyhow::Result<Vec<u8>> {
    let repo = repository(origin, base)?;
    anyhow::ensure!(
        sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid commit SHA"
    );
    anyhow::ensure!(
        name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-'),
        "invalid artifact name"
    );
    let response = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(120))
        .build()?
        .get(format!(
            "{base}/api/v1/repos/{}/commits/{sha}/artifacts/{name}",
            crate::util::encode_segment(&repo)
        ))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| e.without_url())?;
    anyhow::ensure!(
        response.status().is_success(),
        "Branvil artifact HTTP {}",
        response.status()
    );
    Ok(response
        .bytes()
        .await
        .map_err(|e| e.without_url())?
        .to_vec())
}

/// Read the same scoped webhook as the server of `data`, including while it is
/// stopped.
pub async fn notify(
    vault: Arc<dyn vault::Vault>,
    data: &std::path::Path,
    text: &str,
) -> anyhow::Result<bool> {
    let secrets = vault::Secrets::new(vault.clone(), data);
    let Some(url) = vault.get(&secrets.webhook())? else {
        return Ok(false);
    };
    webhook::send(&webhook::client(), &url, text)
        .await
        .map_err(anyhow::Error::msg)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn verified_reads_owner_file_pages_until_since_and_sanitizes_errors() {
        use axum::{
            extract::{Query, State},
            http::{HeaderMap, StatusCode},
            routing::get,
        };
        use std::sync::atomic::{AtomicUsize, Ordering};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let verified_at = |repo: Option<&'static str>, origin: Option<String>, since: String| {
            let base = base.clone();
            async move { verified(&base, "fixture-owner", repo, origin.as_deref(), &since).await }
        };
        let mode = Arc::new(AtomicUsize::new(0));
        let app = axum::Router::new().route("/api/v1/repos/{repo}/verification/commits", get(
            |State(mode): State<Arc<AtomicUsize>>, headers: HeaderMap,
             Query(query): Query<std::collections::HashMap<String, String>>| async move {
                assert_eq!(headers["authorization"], "Bearer fixture-owner");
                let mode = mode.load(Ordering::SeqCst);
                if mode == 2 { return (StatusCode::BAD_REQUEST, axum::Json(json!({"secret":"private response"}))); }
                if mode == 3 { return (StatusCode::OK, axum::Json(json!({"items":[{"sha":"invalid private response","mark":"verified"}],"next_cursor":null}))); }
                let page = if query.is_empty() {
                    json!({"items":[{"sha":"a".repeat(40),"mark":"verified"},{"sha":"b".repeat(40),"mark":null}],"next_cursor":"opaque+/="})
                } else {
                    assert_eq!(query["cursor"], "opaque+/=");
                    json!({"items":[{"sha":"c".repeat(40),"mark":"verified"},{"sha":"d".repeat(40),"mark":"verified"}],"next_cursor":null})
                };
                (StatusCode::OK, axum::Json(page))
            }
        )).with_state(mode.clone());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        for (repo, origin) in [
            (Some("team/repo"), None),
            (None, Some(format!("{base}/git/team/repo.git"))),
        ] {
            assert_eq!(
                verified_at(repo, origin.clone(), "c".repeat(40))
                    .await
                    .unwrap(),
                json!({"since_on_main":true,"verified":["a".repeat(40)]})
            );
        }
        assert_eq!(
            verified_at(Some("team/repo"), None, "f".repeat(40))
                .await
                .unwrap(),
            json!({"since_on_main":false,"verified":["a".repeat(40),"c".repeat(40),"d".repeat(40)]})
        );
        assert_eq!(
            verified_at(Some("team/repo"), None, "a".repeat(40))
                .await
                .unwrap(),
            json!({"since_on_main":true,"verified":[]})
        );
        for value in [2, 3] {
            mode.store(value, Ordering::SeqCst);
            let error = verified_at(Some("team/repo"), None, "c".repeat(40))
                .await
                .unwrap_err()
                .to_string();
            assert!(!error.contains(&base));
            assert!(!error.contains("private response"));
        }
        assert!(
            verified_at(
                None,
                Some("http://other/git/team/repo.git".into()),
                "c".repeat(40)
            )
            .await
            .is_err()
        );
        assert!(
            verified_at(Some("team/repo"), None, "invalid".into())
                .await
                .is_err()
        );
        server.abort();
        let _ = server.await;
    }

    #[test]
    fn origin_is_bound_to_the_configured_hub() {
        // Use the same clone URL shape as App::clone_url and the Git HTTP route.
        for (origin, base) in [
            (
                "http://127.0.0.1:7878/git/team/repo.git",
                "http://127.0.0.1:7878",
            ),
            (
                "http://127.0.0.1:7878/git/team/repo",
                "http://127.0.0.1:7878/",
            ),
            (
                "https://localhost:7878/hub/git/team/repo.git",
                "https://localhost:7878/hub",
            ),
        ] {
            assert_eq!(repository(origin, base).unwrap(), "team/repo");
        }
        for origin in [
            "https://other/git/team/repo.git",
            "http://127.0.0.1:7879/git/team/repo.git",
            "http://user:secret@127.0.0.1:7878/git/team/repo.git",
            "http://127.0.0.1:7878/git/team/repo.git?token=x",
            "http://127.0.0.1:7878/git/team/repo.git#fragment",
            "http://127.0.0.1:7878/git/team/repo/extra",
            "http://127.0.0.1:7878/team/repo.git",
        ] {
            assert!(repository(origin, "http://127.0.0.1:7878").is_err());
        }
    }

    #[tokio::test]
    async fn notification_without_a_server_uses_the_vault_and_sanitizes_failure() {
        let root = tempfile::tempdir().unwrap();
        let vault: Arc<dyn vault::Vault> = Arc::new(vault::Memory::default());
        assert!(!notify(vault.clone(), root.path(), "notice").await.unwrap());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/secret-hook", listener.local_addr().unwrap());
        vault
            .set(
                &vault::Secrets::new(vault.clone(), root.path()).webhook(),
                &url,
            )
            .unwrap();
        let app = axum::Router::new().route(
            "/secret-hook",
            axum::routing::post(|axum::Json(body): axum::Json<Value>| async move {
                assert_eq!(body, json!({"text":"notice"}));
                axum::http::StatusCode::NO_CONTENT
            }),
        );
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        assert!(notify(vault.clone(), root.path(), "notice").await.unwrap());
        server.abort();
        let _ = server.await;
        let error = notify(vault, root.path(), "notice")
            .await
            .unwrap_err()
            .to_string();
        assert!(!error.contains("secret-hook"));
    }
}
