//! The machine's owner token for Git. The member client never sees it.
use std::{path::Path, time::Duration};

use gb_base::{files, home, shared_writes::SharedWrites};
use gb_config::secrets::{self, DeviceKey, Scope, SecretStore, SystemKeychain};
use gb_ports::GitCredential;
use reqwest::{Method, StatusCode};
use serde_json::{Value, json};

const KEYCHAIN: &str = "GuildBotics/branvil-git";

pub(in crate::factory) async fn credential(origin: &str) -> anyhow::Result<Option<GitCredential>> {
    if origin.is_empty() {
        return Ok(None);
    }
    let identity = home::identity_root()?;
    let Some(connection) =
        files::read_if_present(&identity.join(".guildbotics/config/connections/branvil.yml"))?
    else {
        return Ok(None);
    };
    let connection = gb_config::yaml_value(std::str::from_utf8(&connection)?)?;
    let url = connection["base_url"]
        .as_str()
        .unwrap_or_default()
        .trim_end_matches('/');
    if url.is_empty() || !origin.eq_ignore_ascii_case(url) {
        return Ok(None);
    }
    let data = home::data()?;
    let machine = home::machine_root()?;
    let writes = SharedWrites::new(identity, None);
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: machine,
            keychain: &SystemKeychain,
        },
    )?;
    let http = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .timeout(Duration::from_secs(15))
        .build()?;
    let read = || secrets::get_os_credential(KEYCHAIN, url);
    let keep = |token: &str| secrets::keep_os_credential(KEYCHAIN, url, token);
    let admin = || secrets.get("BRANVIL_ADMIN_TOKEN");
    acquire(&http, url, &data, read, keep, admin)
        .await
        .map(Some)
}

async fn acquire(
    http: &reqwest::Client,
    url: &str,
    data: &Path,
    read: impl Fn() -> anyhow::Result<Option<String>>,
    keep: impl Fn(&str) -> anyhow::Result<()>,
    admin: impl FnOnce() -> anyhow::Result<Option<String>>,
) -> anyhow::Result<GitCredential> {
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
        return Ok(credential);
    }
    let path = data.join("run/branvil-git.lock");
    let _held =
        tokio::task::spawn_blocking(move || files::FileLock::hold(&path, Duration::from_secs(60)))
            .await??;
    if let Some(credential) = cached().await? {
        return Ok(credential);
    }
    let token = admin()?
        .filter(|token| !token.is_empty())
        .ok_or_else(|| diagnostic("integrations.branvil.api.not_connected"))?;
    let api = Api {
        http,
        url,
        token: &token,
    };
    let owner = api
        .owner()
        .await?
        .ok_or_else(|| diagnostic("integrations.branvil.api.admin_refused"))?;
    let device = gb_workspace::identity::ensure_device_identity(data)?;
    let id = &device.device_id[device.device_id.len() - 8..];
    let suffix = format!("({id})");
    let collection = format!("/members/{}/tokens", segment(&owner));
    let mut cursor = String::new();
    let mut expired = Vec::new();
    loop {
        let path = if cursor.is_empty() {
            collection.clone()
        } else {
            format!("{collection}?cursor={}", segment(&cursor))
        };
        let page = api.json(api.request(Method::GET, &path)).await?;
        let items = page["items"].as_array().ok_or_else(invalid)?;
        for item in items {
            let label = item["label"].as_str().unwrap_or_default();
            if label.starts_with("git:") && label.ends_with(&suffix) {
                expired.push(item["id"].as_str().ok_or_else(invalid)?.to_owned());
            }
        }
        let Some(next) = page["next_cursor"].as_str().filter(|v| !v.is_empty()) else {
            break;
        };
        anyhow::ensure!(next != cursor, invalid());
        cursor = next.to_owned();
    }
    for id in expired {
        let response = api
            .request(Method::DELETE, &format!("{collection}/{}", segment(&id)))
            .send()
            .await?;
        let status = response.status();
        anyhow::ensure!(
            status.is_success() || status == StatusCode::NOT_FOUND,
            diagnostic("integrations.branvil.api.admin_refused")
        );
    }
    let issued = api
        .json(
            api.request(Method::POST, &collection)
                .json(&json!({"label":format!("git:{} {suffix}", device.display_name)})),
        )
        .await?;
    let password = issued["token"]
        .as_str()
        .filter(|token| !token.is_empty() && !token.contains(['\r', '\n']))
        .ok_or_else(invalid)?;
    keep(password)?;
    Ok(GitCredential {
        username: owner,
        password: password.to_owned(),
    })
}

fn segment(value: &str) -> String {
    percent_encoding::utf8_percent_encode(value, percent_encoding::NON_ALPHANUMERIC).to_string()
}

fn diagnostic(key: &'static str) -> anyhow::Error {
    super::diagnostic(key, []).into()
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

    async fn owner(&self) -> anyhow::Result<Option<String>> {
        let response = self.request(Method::GET, "/me").send().await?;
        if response.status() == StatusCode::UNAUTHORIZED {
            return Ok(None);
        }
        anyhow::ensure!(response.status().is_success(), invalid());
        let answer: Value = response.json().await?;
        let name = answer["name"]
            .as_str()
            .filter(|name| !name.is_empty() && !name.contains(['\r', '\n']));
        Ok(name
            .filter(|_| answer["kind"] == "owner")
            .map(str::to_owned))
    }

    async fn json(&self, request: reqwest::RequestBuilder) -> anyhow::Result<Value> {
        let response = request.send().await?;
        anyhow::ensure!(response.status().is_success(), invalid());
        Ok(response.json().await?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        Router,
        body::Bytes,
        extract::State,
        http::{HeaderMap, Uri},
        response::{IntoResponse, Response},
    };
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct Requests {
        list: Vec<Value>,
        revoked: Vec<String>,
        issued: Vec<String>,
    }

    async fn double(
        State(requests): State<Arc<Mutex<Requests>>>,
        method: Method,
        uri: Uri,
        headers: HeaderMap,
        body: Bytes,
    ) -> Response {
        let bearer = headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default();
        let mut requests = requests.lock().unwrap();
        let path = uri.path();
        if path == "/api/v1/me" {
            let accepted = match bearer {
                "Bearer admin-fixture" => Some("owner"),
                "Bearer another-member" => Some("agent"),
                _ if bearer.strip_prefix("Bearer ").is_some_and(|token| {
                    requests
                        .issued
                        .iter()
                        .enumerate()
                        .any(|(i, _)| token == format!("issued-{i}"))
                }) =>
                {
                    Some("owner")
                }
                _ => None,
            };
            return match accepted {
                Some(kind) => axum::Json(json!({"kind":kind,"name":kind})).into_response(),
                None => StatusCode::UNAUTHORIZED.into_response(),
            };
        }
        assert_eq!(bearer, "Bearer admin-fixture");
        if path == "/api/v1/members/owner/tokens" && method == Method::GET {
            return axum::Json(json!({"items":requests.list,"next_cursor":null})).into_response();
        }
        if path == "/api/v1/members/owner/tokens" && method == Method::POST {
            let request: Value = serde_json::from_slice(&body).unwrap();
            let token = format!("issued-{}", requests.issued.len());
            requests
                .issued
                .push(request["label"].as_str().unwrap().into());
            return (StatusCode::CREATED, axum::Json(json!({"token":token}))).into_response();
        }
        if let Some(id) = path.strip_prefix("/api/v1/members/owner/tokens/")
            && method == Method::DELETE
        {
            requests.revoked.push(id.into());
            return StatusCode::NO_CONTENT.into_response();
        }
        StatusCode::NOT_FOUND.into_response()
    }

    #[tokio::test]
    async fn owner_git_reuses_only_owner_tokens_and_rotates_this_devices_labels() {
        let dir = tempfile::tempdir().unwrap();
        let device = gb_workspace::identity::ensure_device_identity(dir.path()).unwrap();
        let suffix = &device.device_id[device.device_id.len() - 8..];
        let requests = Arc::new(Mutex::new(Requests::default()));
        let router = Router::new().fallback(double).with_state(requests.clone());
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let http = reqwest::Client::builder().no_proxy().build().unwrap();
        let cache = Mutex::new(None::<String>);
        let read = || Ok(cache.lock().unwrap().clone());
        let keep = |token: &str| {
            *cache.lock().unwrap() = Some(token.into());
            Ok(())
        };
        let admin = || Ok(Some("admin-fixture".to_owned()));
        let acquire = || acquire(&http, &url, dir.path(), read, keep, admin);
        let (first, raced) = tokio::join!(acquire(), acquire());
        let first = first.unwrap();
        assert_eq!(first.username, "owner");
        assert_eq!(raced.unwrap().password, first.password);
        assert_eq!(requests.lock().unwrap().issued.len(), 1);
        assert_eq!(acquire().await.unwrap().password, first.password);
        {
            let mut observed = requests.lock().unwrap();
            let first_label = observed.issued[0].clone();
            observed.list = vec![
                json!({"id":"first", "label":first_label}),
                json!({"id":"old", "label":format!("git:Old machine ({suffix})")}),
                json!({"id":"other", "label":"git:Other machine (deadbeef)"}),
                json!({"id":"keep", "label":format!("app:Other app ({suffix})")}),
            ];
        }
        *cache.lock().unwrap() = None;
        let second = acquire().await.unwrap();
        assert_ne!(second.password, first.password);
        assert_eq!(requests.lock().unwrap().revoked, ["first", "old"]);
        {
            let mut observed = requests.lock().unwrap();
            assert!(observed.issued[1].ends_with(&format!("({suffix})")));
            observed
                .list
                .retain(|token| matches!(token["id"].as_str(), Some("other" | "keep")));
        }
        *cache.lock().unwrap() = Some("revoked-token".into());
        assert_ne!(acquire().await.unwrap().password, second.password);
        *cache.lock().unwrap() = Some("another-member".into());
        assert_eq!(acquire().await.unwrap().username, "owner");
        assert_eq!(requests.lock().unwrap().issued.len(), 4);
        server.abort();
    }

    #[tokio::test]
    async fn owner_git_fails_closed_without_admin_or_keychain() {
        let dir = tempfile::tempdir().unwrap();
        let http = reqwest::Client::builder().no_proxy().build().unwrap();
        let unused = "http://127.0.0.1:1";
        let no_admin = acquire(
            &http,
            unused,
            dir.path(),
            || Ok(None),
            |_| unreachable!(),
            || Ok(None),
        )
        .await;
        assert!(matches!(
            no_admin
                .err()
                .unwrap()
                .downcast_ref::<gb_ports::ServiceError>(),
            Some(gb_ports::ServiceError::Diagnostic {
                key: "integrations.branvil.api.not_connected",
                ..
            }),
        ));
        let locked = acquire(
            &http,
            unused,
            dir.path(),
            || Err(gb_config::secrets::KeychainFailure::Locked.into()),
            |_| unreachable!(),
            || unreachable!(),
        )
        .await;
        assert!(
            locked
                .err()
                .unwrap()
                .is::<gb_config::secrets::KeychainFailure>()
        );
    }
}
