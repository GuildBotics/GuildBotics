//! Local Hub maintenance, without accepting credentials from the caller.

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
    let prefix = format!("{}/", base.path().trim_end_matches('/'));
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

/// Read exactly the locally configured Hub, bypassing proxies and redirects.
pub async fn checks(repo: Option<&str>, origin: Option<&str>, sha: &str) -> anyhow::Result<Value> {
    let config = crate::serve_config()?;
    checks_at(&config, repo, origin, sha).await
}

async fn checks_at(
    config: &crate::config::Config,
    repo: Option<&str>,
    origin: Option<&str>,
    sha: &str,
) -> anyhow::Result<Value> {
    let base = config
        .base_url
        .clone()
        .unwrap_or_else(|| format!("http://{}", config.listen));
    let repo = match repo {
        Some(repo) => {
            validate_repository(repo)?;
            repo.to_owned()
        }
        None => repository(origin.context("checkout origin is missing")?, &base)?,
    };
    anyhow::ensure!(
        sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid commit SHA"
    );
    let token = std::fs::read_to_string(config.layout().owner_token())?;
    let http = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(10))
        .build()?;
    let get = async |path: String| -> anyhow::Result<Value> {
        // Never render a response body or a request (both can contain secrets).
        let response = http
            .get(format!("{base}/api/v1/repos/{repo}{path}"))
            .bearer_auth(token.trim())
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
    let settings = get(String::new()).await?;
    let checks = get(format!("/commits/{sha}/checks")).await?;
    summarize(&settings, &checks)
}

fn summarize(repo: &Value, response: &Value) -> anyhow::Result<Value> {
    let registered = response["run_registered"]
        .as_bool()
        .context("run_registered missing")?;
    let required = repo["required_checks"]
        .as_array()
        .context("required_checks missing")?;
    let checks = response["checks"].as_array().context("checks missing")?;
    let mut state = "success";
    if !registered {
        state = "unregistered";
    } else {
        for name in required {
            let name = name.as_str().context("invalid required check")?;
            let check = checks.iter().find(|c| c["name"] == name);
            match check {
                Some(c) if c["status"] == "completed" => {
                    if c["conclusion"] != "success"
                        && !(c["conclusion"] == "skipped" && c["path_skip"].is_object())
                    {
                        state = "failure";
                        break;
                    }
                }
                _ => state = "pending",
            }
        }
        // Invalid pipelines must block even with no required job names.
        if checks
            .iter()
            .any(|c| c["name"] == "pipeline" && c["conclusion"] == "failure")
        {
            state = "failure";
        }
    }
    Ok(json!({"run_registered": registered, "state": state}))
}

/// Read the same scoped webhook as the server, including while it is stopped.
pub async fn notify(vault: Arc<dyn vault::Vault>, text: &str) -> anyhow::Result<bool> {
    notify_at(vault, &crate::serve_config()?.data_dir, text).await
}

async fn notify_at(
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
    async fn commit_checks_read_the_local_owner_file_and_public_http_contract() {
        let root = tempfile::tempdir().unwrap();
        let mut config = crate::config::Config::load(None, Some(root.path()), None, None).unwrap();
        std::fs::create_dir_all(root.path().join("secrets")).unwrap();
        std::fs::write(config.layout().owner_token(), "fixture-owner\n").unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        config.base_url = Some(base.clone());
        let sha = "a".repeat(40);
        let app = axum::Router::new()
            .route("/api/v1/repos/team/repo", axum::routing::get(|headers: axum::http::HeaderMap| async move {
                assert_eq!(headers["authorization"], "Bearer fixture-owner");
                axum::Json(json!({"required_checks":["test"]}))
            }))
            .route("/api/v1/repos/team/repo/commits/{sha}/checks", axum::routing::get(|axum::extract::Path(sha): axum::extract::Path<String>| async move {
                assert_eq!(sha, "a".repeat(40));
                axum::Json(json!({"run_registered":true,"checks":[{"name":"test","status":"completed","conclusion":"success"}]}))
            }));
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        for (repo, origin) in [
            (Some("team/repo"), None),
            (None, Some(format!("{base}/team/repo.git"))),
        ] {
            assert_eq!(
                checks_at(&config, repo, origin.as_deref(), &sha)
                    .await
                    .unwrap(),
                json!({"run_registered":true,"state":"success"})
            );
        }
        std::fs::remove_file(config.layout().owner_token()).unwrap();
        assert!(
            checks_at(&config, Some("team/repo"), None, &sha)
                .await
                .is_err()
        );
        server.abort();
        let _ = server.await;
    }

    #[test]
    fn origin_is_bound_to_the_configured_hub() {
        assert_eq!(
            repository(
                "http://127.0.0.1:7878/team/repo.git",
                "http://127.0.0.1:7878"
            )
            .unwrap(),
            "team/repo"
        );
        for origin in [
            "https://other/team/repo.git",
            "http://user:secret@127.0.0.1:7878/team/repo",
            "http://127.0.0.1:7878/team/repo?token=x",
            "http://127.0.0.1:7878/team/repo/extra",
        ] {
            assert!(repository(origin, "http://127.0.0.1:7878").is_err());
        }
    }

    #[test]
    fn required_checks_distinguish_registration_and_terminal_results() {
        let repo = json!({"required_checks":["test"]});
        for (registered, checks, expected) in [
            (false, json!([]), "unregistered"),
            (true, json!([]), "pending"),
            (
                true,
                json!([{"name":"test","status":"in_progress"}]),
                "pending",
            ),
            (
                true,
                json!([{"name":"test","status":"completed","conclusion":"success"}]),
                "success",
            ),
            (
                true,
                json!([{"name":"test","status":"completed","conclusion":"failure"}]),
                "failure",
            ),
            (
                true,
                json!([{"name":"test","status":"completed","conclusion":"cancelled"}]),
                "failure",
            ),
            (
                true,
                json!([{"name":"test","status":"completed","conclusion":"skipped"}]),
                "failure",
            ),
            (
                true,
                json!([{"name":"test","status":"completed","conclusion":"skipped","path_skip":{}}]),
                "success",
            ),
        ] {
            assert_eq!(
                summarize(&repo, &json!({"run_registered":registered,"checks":checks})).unwrap()["state"],
                expected
            );
        }
        assert!(summarize(&repo, &json!({})).is_err());
    }

    #[tokio::test]
    async fn notification_without_a_server_uses_the_vault_and_sanitizes_failure() {
        let root = tempfile::tempdir().unwrap();
        let vault: Arc<dyn vault::Vault> = Arc::new(vault::Memory::default());
        assert!(
            !notify_at(vault.clone(), root.path(), "notice")
                .await
                .unwrap()
        );
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
        assert!(
            notify_at(vault.clone(), root.path(), "notice")
                .await
                .unwrap()
        );
        server.abort();
        let _ = server.await;
        let error = notify_at(vault, root.path(), "notice")
            .await
            .unwrap_err()
            .to_string();
        assert!(!error.contains("secret-hook"));
    }
}
