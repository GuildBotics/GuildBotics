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

/// The releases of the repository's default branch after `since`, newest
/// first, read from exactly the local Hub.
pub async fn releases(
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
    anyhow::ensure!(crate::util::is_sha(since), "invalid commit SHA");
    let view = get(base, token, &repo, &format!("/releases?since={since}")).await?;
    let mut releases = Vec::new();
    for item in view["releases"].as_array().context("releases missing")? {
        let sha = item["sha"].as_str().context("commit SHA missing")?;
        let name = item["name"].as_str().context("release name missing")?;
        anyhow::ensure!(crate::util::is_sha(sha), "invalid commit SHA");
        releases.push(json!({"name": name, "sha": sha}));
    }
    Ok(json!({"since_on_main": view["since_on_main"] == true, "releases": releases}))
}

/// A GET of the repository's API on exactly the local Hub, bypassing proxies
/// and redirects.
async fn get(base: &str, token: &str, repo: &str, path: &str) -> anyhow::Result<Value> {
    let repo = crate::util::encode_segment(repo);
    let http = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(10))
        .build()?;
    // Never render a response body or a request (both can contain secrets).
    let response = http
        .get(format!("{base}/api/v1/repos/{repo}{path}"))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| e.without_url())?;
    anyhow::ensure!(
        response.status().is_success(),
        "GuildBotics HTTP {}",
        response.status()
    );
    Ok(response.json().await.map_err(|e| e.without_url())?)
}

/// Client distribution only uses commits that passed main verification.
pub async fn require_verified(
    base: &str,
    token: &str,
    origin: &str,
    sha: &str,
) -> anyhow::Result<()> {
    let repo = repository(origin, base)?;
    let (_, mark) = verification_history(base, token, &repo, sha).await?;
    anyhow::ensure!(
        mark == Some(true),
        "client release commit has not passed main verification"
    );
    Ok(())
}

async fn verification_history(
    base: &str,
    token: &str,
    repo: &str,
    since: &str,
) -> anyhow::Result<(Vec<String>, Option<bool>)> {
    anyhow::ensure!(crate::util::is_sha(since), "invalid commit SHA");
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
        let page = get(base, token, repo, &path).await?;
        for item in page["items"].as_array().context("items missing")? {
            let sha = item["sha"].as_str().context("commit SHA missing")?;
            anyhow::ensure!(
                sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()),
                "invalid commit SHA"
            );
            if sha.eq_ignore_ascii_case(since) {
                return Ok((verified, Some(item["mark"] == "verified")));
            }
            if item["mark"] == "verified" {
                verified.push(sha.to_owned());
            }
        }
        cursor = match &page["next_cursor"] {
            Value::Null => return Ok((verified, None)),
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
        "GuildBotics artifact HTTP {}",
        response.status()
    );
    Ok(response
        .bytes()
        .await
        .map_err(|e| e.without_url())?
        .to_vec())
}

/// Read the same scoped webhook as the server of `data`. Always use the default
/// timeout so startup failures can be reported even with an invalid configuration.
pub async fn notify(
    vault: Arc<dyn vault::Vault>,
    data: &std::path::Path,
    text: &str,
) -> anyhow::Result<bool> {
    let secrets = vault::Secrets::new(vault.clone(), data);
    let Some(url) = vault.get(&secrets.webhook())? else {
        return Ok(false);
    };
    let timeout = crate::config::LimitsConfig::default().webhook_timeout_secs;
    let client = webhook::client(std::time::Duration::from_secs(timeout));
    webhook::send(&client, &url, text)
        .await
        .map_err(anyhow::Error::msg)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn releases_and_verification_are_read_from_the_hub_without_leaking_errors() {
        use axum::{
            extract::{Query, State},
            http::{HeaderMap, StatusCode},
            routing::get,
        };
        use std::collections::HashMap;
        use std::sync::atomic::{AtomicUsize, Ordering};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let releases_at = |repo: Option<&'static str>, origin: Option<String>, since: String| {
            let base = base.clone();
            async move { releases(&base, "fixture-owner", repo, origin.as_deref(), &since).await }
        };
        let mode = Arc::new(AtomicUsize::new(0));
        let app = axum::Router::new()
            .route("/api/v1/repos/{repo}/releases", get(
                |State(mode): State<Arc<AtomicUsize>>, headers: HeaderMap,
                 Query(query): Query<HashMap<String, String>>| async move {
                    assert_eq!(headers["authorization"], "Bearer fixture-owner");
                    match mode.load(Ordering::SeqCst) {
                        2 => return (StatusCode::BAD_REQUEST, axum::Json(json!({"secret":"private response"}))),
                        3 => return (StatusCode::OK, axum::Json(json!({"releases":[{"name":"r","sha":"invalid private response"}],"since_on_main":true}))),
                        _ => {}
                    }
                    let on_main = query["since"] != "f".repeat(40);
                    (StatusCode::OK, axum::Json(json!({
                        "releases": [{"name":"release/2","sha":"a".repeat(40),"title":"x"},{"name":"release/1","sha":"c".repeat(40)}],
                        "since_on_main": on_main,
                    })))
                }
            ))
            .route("/api/v1/repos/{repo}/verification/commits", get(
                |Query(query): Query<HashMap<String, String>>| async move {
                    axum::Json(if query.is_empty() {
                        json!({"items":[{"sha":"a".repeat(40),"mark":"verified"},{"sha":"b".repeat(40),"mark":null}],"next_cursor":"opaque+/="})
                    } else {
                        assert_eq!(query["cursor"], "opaque+/=");
                        json!({"items":[{"sha":"c".repeat(40),"mark":"verified"}],"next_cursor":null})
                    })
                }
            ))
            .with_state(mode.clone());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let expected = json!({"since_on_main":true,"releases":[
            {"name":"release/2","sha":"a".repeat(40)},{"name":"release/1","sha":"c".repeat(40)}]});
        for (repo, origin) in [
            (Some("team/repo"), None),
            (None, Some(format!("{base}/git/team/repo.git"))),
        ] {
            assert_eq!(
                releases_at(repo, origin.clone(), "d".repeat(40))
                    .await
                    .unwrap(),
                expected
            );
        }
        let elsewhere = releases_at(Some("team/repo"), None, "f".repeat(40))
            .await
            .unwrap();
        assert_eq!(elsewhere["since_on_main"], false);
        let origin = format!("{base}/git/team/repo.git");
        for (commit, required) in [('a', true), ('b', false), ('c', true), ('f', false)] {
            let sha = commit.to_string().repeat(40);
            assert_eq!(
                require_verified(&base, "fixture-owner", &origin, &sha)
                    .await
                    .is_ok(),
                required
            );
        }
        for value in [2, 3] {
            mode.store(value, Ordering::SeqCst);
            let error = releases_at(Some("team/repo"), None, "c".repeat(40))
                .await
                .unwrap_err()
                .to_string();
            assert!(!error.contains(&base));
            assert!(!error.contains("private response"));
        }
        assert!(
            releases_at(
                None,
                Some("http://other/git/team/repo.git".into()),
                "c".repeat(40)
            )
            .await
            .is_err()
        );
        assert!(
            releases_at(Some("team/repo"), None, "invalid".into())
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
        for broken_config in ["[limits", "unknown_key = true"] {
            std::fs::write(root.path().join("config.toml"), broken_config).unwrap();
            assert!(crate::config::Config::load(None, root.path()).is_err());
            assert!(notify(vault.clone(), root.path(), "notice").await.unwrap());
        }
        server.abort();
        let _ = server.await;
        let error = notify(vault, root.path(), "notice")
            .await
            .unwrap_err()
            .to_string();
        assert!(!error.contains("secret-hook"));
    }
}
