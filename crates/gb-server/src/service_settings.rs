//! Device-local service settings, independent of shared configuration.
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug)]
pub struct InvalidSettings {
    pub(crate) path: PathBuf,
    pub(crate) reason: String,
}
impl std::fmt::Display for InvalidSettings {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.path.display(), self.reason)
    }
}
impl std::error::Error for InvalidSettings {}

#[derive(Debug)]
struct InvalidValues;
impl std::fmt::Display for InvalidValues {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("invalid service settings values")
    }
}
impl std::error::Error for InvalidValues {}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Settings {
    pub start_with_server: bool,
    pub keep_awake: bool,
    pub scheduled: bool,
    pub routine: bool,
    pub event_queue: bool,
    pub routine_interval_minutes: u16,
    pub max_consecutive_errors: u8,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            start_with_server: false,
            keep_awake: false,
            scheduled: true,
            routine: true,
            event_queue: true,
            routine_interval_minutes: 10,
            max_consecutive_errors: 3,
        }
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Change {
    pub start_with_server: Option<bool>,
    pub keep_awake: Option<bool>,
    pub scheduled: Option<bool>,
    pub routine: Option<bool>,
    pub event_queue: Option<bool>,
    pub routine_interval_minutes: Option<u16>,
    pub max_consecutive_errors: Option<u8>,
}

impl Settings {
    pub fn validate(&self) -> anyhow::Result<()> {
        if !(self.scheduled || self.routine || self.event_queue)
            || !(1..=1440).contains(&self.routine_interval_minutes)
            || !(1..=20).contains(&self.max_consecutive_errors)
        {
            return Err(InvalidValues.into());
        }
        Ok(())
    }

    pub fn path(workspace: &Path) -> PathBuf {
        workspace.join(".guildbotics/local/service.yml")
    }

    pub fn read(workspace: &Path) -> anyhow::Result<Self> {
        let path = Self::path(workspace);
        let result: anyhow::Result<Self> = (|| {
            let Some(bytes) = gb_base::files::read_if_present(&path)? else {
                return Ok(Self::default());
            };
            let text = std::str::from_utf8(&bytes)?;
            anyhow::ensure!(!text.trim().is_empty(), "empty settings");
            let settings: Self = serde_saphyr::from_str(text)?;
            settings.validate()?;
            Ok(settings)
        })();
        result.map_err(|error| {
            InvalidSettings {
                path,
                reason: error.to_string(),
            }
            .into()
        })
    }

    pub fn write(&self, workspace: &Path) -> anyhow::Result<()> {
        self.validate()?;
        gb_base::files::atomic_write(
            &Self::path(workspace),
            serde_saphyr::to_string(self)?.as_bytes(),
        )?;
        Ok(())
    }

    /// Serialize read/merge/write so concurrent screens retain each other's changes.
    pub fn update(workspace: &Path, change: Change) -> anyhow::Result<Self> {
        let _lock = gb_base::files::FileLock::hold(
            &workspace.join(".guildbotics/local/run/service-settings.lock"),
            std::time::Duration::from_secs(30),
        )?;
        let mut settings = Self::read(workspace)?;
        macro_rules! merge { ($($field:ident),*) => { $(if let Some(value) = change.$field { settings.$field = value; })* }; }
        merge!(
            start_with_server,
            keep_awake,
            scheduled,
            routine,
            event_queue,
            routine_interval_minutes,
            max_consecutive_errors
        );
        settings.write(workspace)?;
        Ok(settings)
    }
}

pub(crate) async fn get(
    axum::extract::State(api): axum::extract::State<crate::api::Shared>,
    headers: axum::http::HeaderMap,
) -> axum::response::Response {
    route(api, headers, None).await
}

pub(crate) async fn patch(
    axum::extract::State(api): axum::extract::State<crate::api::Shared>,
    headers: axum::http::HeaderMap,
    crate::api::Body(change): crate::api::Body<Change>,
) -> axum::response::Response {
    route(api, headers, Some(change)).await
}

async fn route(
    api: crate::api::Shared,
    headers: axum::http::HeaderMap,
    change: Option<Change>,
) -> axum::response::Response {
    use crate::api::ApiError;
    use axum::{Json, http::StatusCode, response::IntoResponse};
    let Some(workspace) = api.selected().workspace.clone() else {
        return ApiError::new(StatusCode::BAD_REQUEST, "config_not_found", &headers)
            .into_response();
    };
    let result = tokio::task::spawn_blocking(move || match change {
        None => Settings::read(&workspace),
        Some(change) => Settings::update(&workspace, change),
    })
    .await;
    match result {
        Ok(Ok(settings)) => Json(settings).into_response(),
        Ok(Err(error)) => {
            if let Some(invalid) = error.downcast_ref::<InvalidSettings>() {
                let path = invalid.path.display().to_string();
                return ApiError::new(
                    StatusCode::BAD_REQUEST,
                    "service_settings_invalid",
                    &headers,
                )
                .key(
                    "service_settings_invalid",
                    vec![("path", path.clone()), ("reason", invalid.reason.clone())],
                )
                .context(serde_json::json!({"path":path}))
                .into_response();
            }
            if let Some(invalid) = error.downcast_ref::<InvalidValues>() {
                return ApiError::new(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "validation_error",
                    &headers,
                )
                .context(serde_json::json!({"errors": [{"msg": invalid.to_string()}]}))
                .into_response();
            }
            tracing::error!("cannot save service settings: {error:#}");
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                &headers,
            )
            .into_response()
        }
        Err(error) => {
            tracing::error!("service settings task failed: {error}");
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                &headers,
            )
            .into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn http_settings_validate_preserve_partial_changes_and_classify_saved_file_errors() {
        use axum::{
            body::{Body, to_bytes},
            http::{Request, StatusCode},
        };
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let data = root.path().join("data");
        let workspace = root.path().join("workspace");
        let auth = crate::auth::ServerAuth::open(&data, 38761).unwrap();
        let token = std::fs::read_to_string(auth.owner_token_path()).unwrap();
        let api = std::sync::Arc::new(crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(workspace.clone()),
                ..Default::default()
            },
            crate::input_files::InputFileStore::new(&data.join("inputs")).unwrap(),
            auth,
            "fixture".into(),
            data,
        ));
        let app = crate::api::router(api);
        for (method, body, authorized, expected) in [
            ("GET", json!({}), false, StatusCode::UNAUTHORIZED),
            ("GET", json!({}), true, StatusCode::OK),
            (
                "PATCH",
                json!({"keep_awake":true,"scheduled":false}),
                true,
                StatusCode::OK,
            ),
            ("PATCH", json!({"event_queue":false}), true, StatusCode::OK),
            (
                "PATCH",
                json!({"routine":false}),
                true,
                StatusCode::UNPROCESSABLE_ENTITY,
            ),
            (
                "PATCH",
                json!({"unknown":true}),
                true,
                StatusCode::UNPROCESSABLE_ENTITY,
            ),
            (
                "PATCH",
                json!({"routine_interval_minutes":1441}),
                true,
                StatusCode::UNPROCESSABLE_ENTITY,
            ),
        ] {
            let mut request = Request::builder()
                .method(method)
                .uri("/-/guildbotics/api/service/settings")
                .header("host", "127.0.0.1:38761")
                .header("content-type", "application/json");
            if authorized {
                request = request.header("authorization", format!("Bearer {}", token.trim()));
            }
            let response = app
                .clone()
                .oneshot(request.body(Body::from(body.to_string())).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), expected, "{method}: {body}");
            if expected == StatusCode::UNPROCESSABLE_ENTITY {
                // The stable shape of a refused request.
                let answer: serde_json::Value =
                    serde_json::from_slice(&to_bytes(response.into_body(), 1 << 20).await.unwrap())
                        .unwrap();
                assert_eq!(answer["code"], "validation_error", "{body}");
                assert_eq!(answer["message"], "Request validation failed.", "{body}");
                assert!(
                    answer["context"]["errors"][0]["msg"].is_string(),
                    "{body}: {answer}"
                );
            }
        }
        let settings = Settings::read(&workspace).unwrap();
        assert!(settings.keep_awake && settings.routine);
        assert!(!settings.scheduled && !settings.event_queue);
        std::fs::write(Settings::path(&workspace), "unknown: true\n").unwrap();
        for method in ["GET", "PATCH"] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri("/-/guildbotics/api/service/settings")
                        .header("host", "127.0.0.1:38761")
                        .header("content-type", "application/json")
                        .header("authorization", format!("Bearer {}", token.trim()))
                        .header("x-guildbotics-language", "ja")
                        .body(Body::from("{}"))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::BAD_REQUEST);
            let value: serde_json::Value =
                serde_json::from_slice(&to_bytes(response.into_body(), 65536).await.unwrap())
                    .unwrap();
            assert_eq!(value["code"], "service_settings_invalid");
            assert_eq!(
                value["context"]["path"],
                Settings::path(&workspace).to_str().unwrap()
            );
            assert!(
                !value["message"]
                    .as_str()
                    .unwrap()
                    .starts_with("app_api.errors.")
            );
        }
        assert_eq!(
            std::fs::read_to_string(Settings::path(&workspace)).unwrap(),
            "unknown: true\n"
        );
    }

    #[test]
    fn defaults_run_every_source_every_ten_minutes_without_creating_a_file() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(
            serde_json::to_value(Settings::read(dir.path()).unwrap()).unwrap(),
            json!({
                "start_with_server":false,"keep_awake":false,"scheduled":true,
                "routine":true,"event_queue":true,"routine_interval_minutes":10,"max_consecutive_errors":3
            })
        );
        assert!(!Settings::path(dir.path()).exists());
    }

    #[test]
    fn settings_roundtrip_at_the_device_path_and_old_fields_keep_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let settings = Settings {
            keep_awake: true,
            scheduled: false,
            event_queue: false,
            routine_interval_minutes: 3,
            max_consecutive_errors: 7,
            ..Settings::default()
        };
        settings.write(dir.path()).unwrap();
        assert_eq!(
            Settings::path(dir.path()),
            dir.path().join(".guildbotics/local/service.yml")
        );
        assert_eq!(Settings::read(dir.path()).unwrap(), settings);
        std::fs::write(Settings::path(dir.path()), "keep_awake: true\n").unwrap();
        assert_eq!(
            Settings::read(dir.path()).unwrap(),
            Settings {
                keep_awake: true,
                ..Settings::default()
            }
        );
    }

    #[test]
    fn source_and_numeric_boundaries_are_validated_before_any_write() {
        let dir = tempfile::tempdir().unwrap();
        for interval in [1, 1440] {
            Settings {
                routine_interval_minutes: interval,
                ..Settings::default()
            }
            .write(dir.path())
            .unwrap();
        }
        for limit in [1, 20] {
            Settings {
                max_consecutive_errors: limit,
                ..Settings::default()
            }
            .write(dir.path())
            .unwrap();
        }
        for settings in [
            Settings {
                scheduled: false,
                routine: false,
                event_queue: false,
                ..Settings::default()
            },
            Settings {
                routine_interval_minutes: 0,
                ..Settings::default()
            },
            Settings {
                routine_interval_minutes: 1441,
                ..Settings::default()
            },
            Settings {
                max_consecutive_errors: 0,
                ..Settings::default()
            },
            Settings {
                max_consecutive_errors: 21,
                ..Settings::default()
            },
        ] {
            let before = std::fs::read(Settings::path(dir.path())).unwrap();
            assert!(settings.write(dir.path()).is_err());
            assert_eq!(std::fs::read(Settings::path(dir.path())).unwrap(), before);
        }
    }

    #[test]
    fn invalid_files_name_the_path_and_are_preserved() {
        let dir = tempfile::tempdir().unwrap();
        let path = Settings::path(dir.path());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        for content in [
            b"keep_awake: [".as_slice(),
            b"",
            b"- a list\n",
            b"routine_interval_minutes: 0\n",
            b"scheduled: false\nroutine: false\nevent_queue: false\n",
            b"unknown: 1\n",
            b"\xff\xfe",
        ] {
            std::fs::write(&path, content).unwrap();
            assert!(
                format!("{:#}", Settings::read(dir.path()).unwrap_err())
                    .contains(path.to_str().unwrap())
            );
            assert_eq!(std::fs::read(&path).unwrap(), content);
        }
    }

    #[test]
    fn partial_updates_preserve_unmentioned_fields_and_reject_empty_sources() {
        let dir = tempfile::tempdir().unwrap();
        Settings::update(
            dir.path(),
            Change {
                keep_awake: Some(true),
                scheduled: Some(false),
                ..Change::default()
            },
        )
        .unwrap();
        let settings = Settings::update(
            dir.path(),
            Change {
                event_queue: Some(false),
                ..Change::default()
            },
        )
        .unwrap();
        assert!(settings.keep_awake && settings.routine);
        assert!(!settings.scheduled && !settings.event_queue);
        assert!(
            Settings::update(
                dir.path(),
                Change {
                    routine: Some(false),
                    ..Change::default()
                }
            )
            .is_err()
        );
        assert_eq!(Settings::read(dir.path()).unwrap(), settings);
    }
}
