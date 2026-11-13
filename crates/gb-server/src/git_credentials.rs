//! Owner or consent-authorized issuance; credentials go straight to the response, never to events.
use crate::api::{ApiError, Body, Shared};
use axum::{
    Json,
    extract::State,
    http::{HeaderMap, StatusCode},
};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    origin: String,
    device_id: String,
    display_name: String,
}
impl Request {
    fn valid(&self) -> bool {
        uuid::Uuid::parse_str(&self.device_id).is_ok_and(|id| id.to_string() == self.device_id)
            && !self.display_name.trim().is_empty()
            && self.display_name.len() <= 241
            && !self.display_name.chars().any(char::is_control)
    }
}
pub(crate) async fn issue(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<Request>,
) -> Result<Json<Value>, ApiError> {
    if !request.valid() {
        return Err(crate::api::invalid(&headers, "device"));
    }
    let Some(data) = api.branvil.get() else {
        return Ok(Json(Value::Null));
    };
    if !request
        .origin
        .eq_ignore_ascii_case(data.base_url().trim_end_matches('/'))
    {
        return Ok(Json(Value::Null));
    }
    // Match the legacy label's suffix, so even suffix collisions cannot rotate concurrently.
    let path = api.data.join("run/git-credentials").join(format!(
        "{}.lock",
        &request.device_id[request.device_id.len() - 8..]
    ));
    let held = tokio::task::spawn_blocking(move || {
        gb_base::files::FileLock::hold(&path, std::time::Duration::from_secs(60))
    })
    .await;
    let _held = held
        .map_err(|_| unavailable(&headers))?
        .map_err(|_| unavailable(&headers))?;
    let registry = crate::setup_providers::Registry(Some(api));
    let Some((data, admin)) = registry.admin().await.map_err(|e| {
        let message = crate::setup_providers::accounts::render(
            e,
            crate::i18n::language(
                headers
                    .get("x-guildbotics-language")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or(""),
            ),
        );
        ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "git_credentials", &headers).reason(message)
    })?
    else {
        return Ok(Json(Value::Null));
    };
    let credential = gb_integrations::factory::human_git_credential(
        &request.origin,
        &data,
        &admin,
        &request.device_id,
        &request.display_name,
    )
    .await
    .map_err(|error| {
        if let Some(error) = error.downcast_ref::<gb_ports::ServiceError>() {
            ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "git_credentials", &headers).locale(
                error.translation_key(),
                error.translation_args().into_iter().collect(),
            )
        } else {
            unavailable(&headers)
        }
    })?;
    Ok(Json(match credential {
        Some(c) => json!({"username":c.username,"password":c.password}),
        None => Value::Null,
    }))
}
fn unavailable(headers: &HeaderMap) -> ApiError {
    ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "git_credentials", headers)
        .locale("integrations.branvil.api.admin_refused", vec![])
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn owner_api_issues_for_requesting_device_and_rejects_other_origins() {
        use gb_config::secrets::{DeviceKey, Keychain, Scope, SecretStore};
        use std::sync::{Arc, Mutex};
        use tower::ServiceExt;
        #[derive(Default)]
        struct Keys(Mutex<Option<String>>);
        impl Keychain for Keys {
            fn read(&self) -> anyhow::Result<Option<String>> {
                Ok(self.0.lock().unwrap().clone())
            }
            fn create(&self, key: &str) -> anyhow::Result<()> {
                *self.0.lock().unwrap() = Some(key.into());
                Ok(())
            }
        }
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path().join("data");
        let identity = dir.path().join("identity");
        let root = dir.path().join("branvil");
        let token = branvil::api::initialize(&root, "owner").unwrap();
        let running =
            branvil::api::Running::start(&root, Arc::new(branvil::vault::Memory::default()))
                .await
                .unwrap();
        let origin = running.data.base_url().to_owned();
        let mut api = crate::api::Api::new(
            crate::api::Selected::default(),
            crate::input_files::InputFileStore::deferred(&dir.path().join("inputs")),
            crate::auth::ServerAuth::open(&data, 8765).unwrap(),
            "fixture".into(),
            data,
        );
        api.identity_root = identity.clone();
        api.keychain = Arc::new(Keys::default());
        let writes = gb_base::shared_writes::SharedWrites::new(identity.clone(), None);
        let store = SecretStore::new(
            &writes,
            Scope::Identity,
            DeviceKey {
                machine_root: dir.path().to_path_buf(),
                keychain: &*api.keychain,
            },
        )
        .unwrap();
        store.set("BRANVIL_ADMIN_TOKEN", &token).unwrap();
        let config = identity.join(".guildbotics/config/connections/branvil.yml");
        std::fs::create_dir_all(config.parent().unwrap()).unwrap();
        std::fs::write(config, format!("base_url: {origin}\n")).unwrap();
        assert!(api.branvil.set(running.data.clone()).is_ok());
        let link = api.auth.entry_link("/");
        let (cookie, csrf) = api
            .auth
            .start_session(link.url.split_once('#').unwrap().1)
            .unwrap()
            .unwrap();
        let cookie = format!("{}={cookie}", api.auth.cookie);
        let consent = api.auth.test_consent();
        let app = crate::api::router(Arc::new(api));
        for use_consent in [false, true] {
            for (origin, device, name, status) in [
                (
                    origin.as_str(),
                    "019691c2-8dc0-7654-8000-0000abc01234",
                    "Client",
                    StatusCode::OK,
                ),
                (
                    "http://127.0.0.1:1",
                    "019691c2-8dc0-7654-8000-0000abc01234",
                    "Client",
                    StatusCode::OK,
                ),
                (
                    origin.as_str(),
                    "bad",
                    "Client",
                    StatusCode::UNPROCESSABLE_ENTITY,
                ),
            ] {
                let mut builder = axum::http::Request::builder();
                if use_consent {
                    builder = builder.header("authorization", format!("Bearer {consent}"));
                }
                let response = app
                    .clone()
                    .oneshot(
                        builder
                            .method("POST")
                            .uri("/-/guildbotics/api/git-credentials")
                            .header("host", "127.0.0.1:8765")
                            .header("cookie", if use_consent { "" } else { &cookie })
                            .header(crate::auth::CSRF_HEADER, &csrf)
                            .header("content-type", "application/json")
                            .body(axum::body::Body::from(
                                json!({"origin":origin,"device_id":device,"display_name":name})
                                    .to_string(),
                            ))
                            .unwrap(),
                    )
                    .await
                    .unwrap();
                assert_eq!(response.status(), status);
                if status == StatusCode::OK {
                    let body: Value = serde_json::from_slice(
                        &axum::body::to_bytes(response.into_body(), usize::MAX)
                            .await
                            .unwrap(),
                    )
                    .unwrap();
                    if origin == "http://127.0.0.1:1" {
                        assert!(body.is_null());
                    } else {
                        assert!(
                            running
                                .data
                                .authenticate(body["password"].as_str().unwrap().into())
                                .await
                                .is_ok()
                        );
                    }
                }
            }
        }
        let admin = running.data.authenticate(token).await.unwrap();
        let tokens = admin
            .members_list_tokens(
                "owner".into(),
                admin.page("test".into(), Some(100), None).unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            tokens
                .items
                .iter()
                .filter(|t| t.label == "git:Client (abc01234)")
                .count(),
            1
        );
    }

    #[test]
    fn validates_device_before_using_its_suffix_or_label() {
        let mut r = Request {
            origin: "http://127.0.0.1:7878".into(),
            device_id: "019691c2-8dc0-7654-8000-0000abc01234".into(),
            display_name: "Fixture machine".into(),
        };
        assert!(r.valid());
        r.display_name = "x".repeat(241);
        assert!(r.valid());
        for name in ["", " ", "a\nb", "a\0b", &"x".repeat(242)] {
            r.display_name = name.into();
            assert!(!r.valid());
        }
        r.display_name = "Fixture".into();
        for id in [
            "",
            "x",
            "../../../../abc01234",
            "019691C2-8DC0-7654-8000-0000ABC01234",
        ] {
            r.device_id = id.into();
            assert!(!r.valid());
        }
    }
}
