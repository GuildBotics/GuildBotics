//! The machine's owner token for Git. The member client never sees it.
use std::{path::Path, time::Duration};

use gb_base::{files, home};
use gb_config::secrets;
use gb_ports::GitCredential;
use reqwest::{Method, StatusCode};
use serde_json::{Value, json};

const KEYCHAIN: &str = "GuildBotics/branvil-git";

pub(super) async fn credential(
    origin: &str,
    device: impl FnOnce(&Path) -> anyhow::Result<(String, String)>,
) -> anyhow::Result<Option<GitCredential>> {
    if origin.is_empty() {
        return Ok(None);
    }
    let data = home::data()?;
    let http = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .timeout(Duration::from_secs(15))
        .build()?;
    let read = || secrets::get_os_credential(KEYCHAIN, origin);
    let keep = |token: &str| secrets::keep_os_credential(KEYCHAIN, origin, token);
    acquire(&http, origin, &data, read, keep, async || {
        let language = super::language().await;
        let (owner, _) = super::cli_server(&data, language).await?;
        let (device_id, display_name) = device(&data)?;
        let answer = owner
            .post(
                "/git-credentials",
                Some(&json!({"origin":origin,"device_id":device_id,"display_name":display_name})),
                Some(Duration::from_secs(60)),
            )
            .await?;
        if answer.is_null() {
            return Ok(None);
        }
        Ok(Some(GitCredential {
            username: answer["username"].as_str().ok_or_else(invalid)?.to_owned(),
            password: answer["password"].as_str().ok_or_else(invalid)?.to_owned(),
        }))
    })
    .await
}

async fn acquire(
    http: &reqwest::Client,
    url: &str,
    data: &Path,
    read: impl Fn() -> anyhow::Result<Option<String>>,
    keep: impl Fn(&str) -> anyhow::Result<()>,
    issue: impl AsyncFnOnce() -> anyhow::Result<Option<GitCredential>>,
) -> anyhow::Result<Option<GitCredential>> {
    let cached = || async {
        let Some(token) = read()?.filter(|token| !token.is_empty()) else {
            return Ok(None);
        };
        let api = Api {
            http,
            url,
            token: &token,
        };
        match api.owner().await {
            Ok(Some(username)) => Ok(Some(GitCredential {
                username,
                password: token,
            })),
            Ok(None) => Ok(None),
            Err(error) => Err(error),
        }
    };
    if let Some(credential) = cached().await? {
        return Ok(Some(credential));
    }
    let path = data.join("run/branvil-git.lock");
    let _held =
        tokio::task::spawn_blocking(move || files::FileLock::hold(&path, Duration::from_secs(60)))
            .await??;
    if let Some(credential) = cached().await? {
        return Ok(Some(credential));
    }
    let Some(credential) = issue().await? else {
        return Ok(None);
    };
    anyhow::ensure!(
        !credential.password.is_empty()
            && !credential.password.contains(['\r', '\n'])
            && !credential.username.is_empty()
            && !credential.username.contains(['\r', '\n']),
        invalid()
    );
    keep(&credential.password)?;
    Ok(Some(credential))
}

fn diagnostic(key: &'static str) -> anyhow::Error {
    gb_ports::ServiceError::Diagnostic {
        class: gb_ports::ServiceErrorClass::Failure,
        key,
        args: Default::default(),
    }
    .into()
}

fn invalid() -> anyhow::Error {
    diagnostic("integrations.branvil.api.admin_refused")
}

struct Api<'a> {
    http: &'a reqwest::Client,
    url: &'a str,
    token: &'a str,
}

impl Api<'_> {
    fn request(&self, method: Method, path: &str) -> reqwest::RequestBuilder {
        self.http
            .request(method, format!("{}/api/v1{path}", self.url))
            .bearer_auth(self.token)
    }

    fn transport_error(&self, error: reqwest::Error) -> anyhow::Error {
        let args = [("base_url", self.url.to_owned())];
        if error.is_timeout() {
            gb_ports::ServiceError::Diagnostic {
                class: gb_ports::ServiceErrorClass::Failure,
                key: "integrations.branvil.api.timeout",
                args: args.into_iter().map(|(k, v)| (k.to_owned(), v)).collect(),
            }
            .into()
        } else if error.is_decode() {
            invalid()
        } else {
            gb_ports::ServiceError::Diagnostic {
                class: gb_ports::ServiceErrorClass::Failure,
                key: "integrations.branvil.api.unreachable",
                args: args.into_iter().map(|(k, v)| (k.to_owned(), v)).collect(),
            }
            .into()
        }
    }

    async fn send(&self, request: reqwest::RequestBuilder) -> anyhow::Result<reqwest::Response> {
        request
            .send()
            .await
            .map_err(|error| self.transport_error(error))
    }

    async fn decode(&self, response: reqwest::Response) -> anyhow::Result<Value> {
        anyhow::ensure!(response.status().is_success(), invalid());
        response
            .json()
            .await
            .map_err(|error| self.transport_error(error))
    }

    async fn owner(&self) -> anyhow::Result<Option<String>> {
        let response = self.send(self.request(Method::GET, "/me")).await?;
        if response.status() == StatusCode::UNAUTHORIZED {
            return Ok(None);
        }
        let answer = self.decode(response).await?;
        let name = answer["name"]
            .as_str()
            .filter(|name| !name.is_empty() && !name.contains(['\r', '\n']));
        Ok(name
            .filter(|_| answer["kind"] == "owner")
            .map(str::to_owned))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Router, http::HeaderMap, response::IntoResponse, routing::get};
    use std::sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    };

    #[tokio::test]
    async fn cached_token_validation_does_not_follow_redirects_to_another_origin() {
        let target = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let url = format!("http://{}/api/v1/me", target.local_addr().unwrap());
        let seen = Arc::new(AtomicUsize::new(0));
        let counter = seen.clone();
        let target_server = tokio::spawn(async move {
            axum::serve(
                target,
                Router::new().fallback(move || {
                    let counter = counter.clone();
                    async move {
                        counter.fetch_add(1, Ordering::SeqCst);
                        StatusCode::OK
                    }
                }),
            )
            .await
            .unwrap();
        });
        let source = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let origin = format!("http://{}", source.local_addr().unwrap());
        let source_server = tokio::spawn(async move {
            axum::serve(
                source,
                Router::new().fallback(move || {
                    let url = url.clone();
                    async move { axum::response::Redirect::temporary(&url) }
                }),
            )
            .await
            .unwrap();
        });
        let data = tempfile::tempdir().unwrap();
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap();
        assert!(
            acquire(
                &http,
                &origin,
                data.path(),
                || Ok(Some("fixture-token".into())),
                |_| panic!("store after redirect"),
                async || panic!("issue after redirect")
            )
            .await
            .is_err()
        );
        assert_eq!(seen.load(Ordering::SeqCst), 0);
        source_server.abort();
        target_server.abort();
    }

    #[tokio::test]
    async fn cached_tokens_are_origin_scoped_and_concurrent_misses_issue_once() {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let requests = Arc::new(AtomicUsize::new(0));
        let seen = requests.clone();
        let app = Router::new().route(
            "/api/v1/me",
            get(move |headers: HeaderMap| {
                let seen = seen.clone();
                async move {
                    seen.fetch_add(1, Ordering::SeqCst);
                    if headers
                        .get("authorization")
                        .is_some_and(|h| h == "Bearer fixture-token")
                    {
                        axum::Json(json!({"name":"owner","kind":"owner"})).into_response()
                    } else {
                        StatusCode::UNAUTHORIZED.into_response()
                    }
                }
            }),
        );
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let data = tempfile::tempdir().unwrap();
        let cache = Mutex::new(None::<String>);
        let issued = AtomicUsize::new(0);
        let read = || Ok(cache.lock().unwrap().clone());
        let keep = |token: &str| {
            *cache.lock().unwrap() = Some(token.into());
            Ok(())
        };
        let issue = async || {
            issued.fetch_add(1, Ordering::SeqCst);
            Ok(Some(GitCredential {
                username: "owner".into(),
                password: "fixture-token".into(),
            }))
        };
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap();
        let call = || acquire(&http, &origin, data.path(), read, keep, issue);
        let (a, b) = tokio::join!(call(), call());
        assert!(a.unwrap().is_some() && b.unwrap().is_some());
        assert_eq!(issued.load(Ordering::SeqCst), 1);
        // An available cache needs neither the owner client nor device identity.
        assert!(
            acquire(&http, &origin, data.path(), read, keep, async || panic!(
                "issuance on cache hit"
            ))
            .await
            .unwrap()
            .is_some()
        );
        *cache.lock().unwrap() = Some("revoked".into());
        assert!(call().await.unwrap().is_some());
        assert_eq!(issued.load(Ordering::SeqCst), 2);
        // Another origin has no cache entry; no token may be sent there.
        let before = requests.load(Ordering::SeqCst);
        assert!(
            acquire(
                &http,
                "http://127.0.0.1:1",
                data.path(),
                || Ok(None),
                keep,
                async || Ok(None)
            )
            .await
            .unwrap()
            .is_none()
        );
        assert_eq!(requests.load(Ordering::SeqCst), before);
        let error = acquire(
            &http,
            &origin,
            data.path(),
            || Err(gb_config::secrets::KeychainFailure::Locked.into()),
            keep,
            async || panic!("issue after keychain failure"),
        )
        .await;
        assert!(error.is_err());
        server.abort();
    }
}
