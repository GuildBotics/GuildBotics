//! Device-local Hub maintenance state. No shared record is written.
use crate::api::{ApiError, Body, Shared};
use axum::http::{HeaderMap, StatusCode};
use axum::{Json, extract::State};
use gb_base::hub_update::{self, Status};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Kind {
    Update,
    Restart,
    Stop,
}
#[derive(Clone, Deserialize, Serialize)]
pub(crate) struct Phase {
    kind: Kind,
    phase: String,
    commit: Option<String>,
    subject: Option<String>,
    since: String,
}
#[derive(Deserialize)]
pub(crate) struct Request {
    #[serde(default)]
    clear: bool,
    phase: Option<String>,
    commit: Option<String>,
    subject: Option<String>,
}
fn announce(api: &Shared) {
    if let Some(host) = api.branvil.get() {
        host.hub_status_changed();
    }
}
pub(crate) fn drain(api: &Shared, reason: Option<Kind>) {
    let mut state = api.hub_phase.lock().unwrap();
    let previous = state.as_ref();
    let kind = reason.unwrap_or(Kind::Stop);
    *state = Some(Phase {
        kind,
        phase: "draining".into(),
        commit: if matches!(kind, Kind::Update) {
            previous.and_then(|p| p.commit.clone())
        } else {
            None
        },
        subject: if matches!(kind, Kind::Update) {
            previous.and_then(|p| p.subject.clone())
        } else {
            None
        },
        since: chrono::Utc::now().to_rfc3339(),
    });
    drop(state);
    announce(api);
}
pub(crate) async fn set(
    State(api): State<Shared>,
    headers: axum::http::HeaderMap,
    Body(body): Body<Request>,
) -> Result<Json<Value>, crate::api::ApiError> {
    // A late clear must never erase a stop that has already been accepted.
    let mut state = api.hub_phase.lock().unwrap();
    if !api.shutdown.stopping() {
        let phase = if body.clear {
            None
        } else {
            if body.phase.as_deref() != Some("preparing")
                || body
                    .commit
                    .as_ref()
                    .is_none_or(|s| s.len() != 40 || !s.bytes().all(|b| b.is_ascii_hexdigit()))
                || body.subject.is_none()
            {
                return Err(crate::api::ApiError::new(
                    axum::http::StatusCode::BAD_REQUEST,
                    "validation_error",
                    &headers,
                ));
            }
            Some(Phase {
                kind: Kind::Update,
                phase: "preparing".into(),
                commit: body.commit,
                subject: body.subject,
                since: chrono::Utc::now().to_rfc3339(),
            })
        };
        *state = phase;
        drop(state);
        announce(&api);
    }
    Ok(Json(json!({})))
}
pub(crate) async fn read(State(api): State<Shared>) -> Json<Value> {
    Json(snapshot(&api).await)
}

/// The server and updater run from this fixed path even when builds are stored elsewhere.
pub(crate) fn update_directory() -> Option<PathBuf> {
    let executable = std::env::current_exe().ok()?;
    let builds = executable.parent()?;
    let directory = builds.parent()?;
    let name = directory.file_name()?.to_str()?;
    (executable.file_name()? == "current"
        && builds.file_name()? == "builds"
        && (name == "Hub" || name.starts_with("Hub-"))
        && directory
            .parent()?
            .ends_with("Library/Application Support/GuildBotics"))
    .then(|| directory.to_owned())
}

fn update_status(directory: &Path) -> Option<Value> {
    let status = hub_update::status(directory).ok()??;
    let mut value = serde_json::to_value(&status).expect("Hub update status serializes");
    if let Status::Manual(manual) = status {
        value["confirmed"] = json!(manual.confirmed(directory).unwrap_or(false));
    }
    Some(value)
}

#[derive(Deserialize)]
pub(crate) struct Confirmation {
    sha: String,
}

pub(crate) async fn confirm(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(body): Body<Confirmation>,
) -> Result<Json<Value>, ApiError> {
    if api.shutdown.stopping() {
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            "server_stopping",
            &headers,
        ));
    }
    let stale = || ApiError::new(StatusCode::CONFLICT, "hub_update_changed", &headers);
    let failed = |error| {
        tracing::warn!(%error, "Hub update confirmation");
        ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "hub_update_confirmation_failed",
            &headers,
        )
    };
    let directory = api.hub_update_dir.as_deref().ok_or_else(stale)?;
    let Some(Status::Manual(manual)) = hub_update::status(directory).map_err(failed)? else {
        return Err(stale());
    };
    if manual.sha != body.sha {
        return Err(stale());
    }
    manual.confirm(directory).map_err(failed)?;
    announce(&api);
    Ok(Json(json!({})))
}

#[derive(Deserialize)]
struct BuildMetadata {
    sha: String,
    subject: String,
    tag: Option<String>,
    remote: Option<String>,
}

fn build_metadata(source: &Path) -> Option<BuildMetadata> {
    std::fs::read(source.join("hub-build.json"))
        .ok()
        .and_then(|s| serde_json::from_slice(&s).ok())
}

pub(crate) async fn snapshot(api: &Shared) -> Value {
    let source = gb_base::home::python_package();
    snapshot_with_build(api, source.parent().and_then(build_metadata)).await
}

async fn snapshot_with_build(api: &Shared, metadata: Option<BuildMetadata>) -> Value {
    let phase = api.hub_phase.lock().unwrap().clone();
    let mut current = metadata
        .as_ref()
        .map(|v| json!({"sha":v.sha,"subject":v.subject,"tag":v.tag,"url":null}));
    let mut target = phase
        .as_ref()
        .filter(|p| matches!(p.kind, Kind::Update))
        .and_then(|p| {
            p.commit
                .as_ref()
                .map(|sha| json!({"sha":sha,"subject":p.subject,"url":null}))
        });
    let provider = api.selected().workspace.as_ref().and_then(|workspace| {
        let project =
            std::fs::read_to_string(workspace.join(".guildbotics/config/team/project.yml")).ok()?;
        let project = gb_config::yaml_value(&project).ok()?;
        project["services"]["code_hosting_service"]["name"]
            .as_str()
            .map(str::to_owned)
    });
    // The Hub's own repository on the embedded code host, when it is one.
    let hosted = provider
        .filter(|provider| provider.eq_ignore_ascii_case("branvil"))
        .zip(metadata.as_ref().and_then(|v| v.remote.as_deref()))
        .and_then(|(_, remote)| {
            let data = api.branvil.get()?;
            Some((
                data,
                branvil::agent::CodeAgent::repository_for_remote(data, remote)?,
            ))
        });
    if let Some((data, repository)) = &hosted {
        for version in [&mut current, &mut target].into_iter().flatten() {
            let sha = version["sha"].as_str().unwrap_or_default().to_owned();
            version["url"] = json!(branvil::agent::CodeAgent::commit_url_for(
                data, repository, &sha
            ));
        }
    }
    let operation = phase.map(|p|json!({"kind":p.kind,"phase":p.phase,"since":p.since,"commit":p.commit,"waiting":{"runs":api.selected().service.execution.generation().snapshot().len(),"ci_jobs":0}}));
    let update = api.hub_update_dir.as_deref().and_then(update_status);
    let mut result =
        json!({"current":current,"target":target,"operation":operation,"update":update});
    if result["operation"].is_object()
        && let Some(host) = api.branvil.get()
    {
        result["operation"]["waiting"]["ci_jobs"] = json!(host.running_ci_jobs().await);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn current_version_comes_from_the_build_even_without_a_code_host() {
        let root = tempfile::tempdir().unwrap();
        let api = std::sync::Arc::new(crate::api::Api::for_test(root.path(), Default::default()));
        let path = root.path().join("hub-build.json");
        let sha = "a".repeat(40);
        assert!(build_metadata(root.path()).is_none());
        std::fs::write(&path, b"{}").unwrap();
        assert!(build_metadata(root.path()).is_none());

        for tag in [None, Some("release/2026-11-27-1")] {
            let mut metadata = json!({"sha":sha,"subject":"Built source","remote":"https://example.test/repo.git"});
            if let Some(tag) = tag {
                metadata["tag"] = json!(tag);
            }
            std::fs::write(&path, metadata.to_string()).unwrap();
            let value = snapshot_with_build(&api, build_metadata(root.path())).await;
            assert_eq!(
                value["current"],
                json!({"sha":sha,"subject":"Built source","tag":tag,"url":null})
            );
            assert!(value.get("releases").is_none());
        }
    }
    #[tokio::test]
    async fn preparing_clear_and_drain_keep_only_device_local_state() {
        let root = tempfile::tempdir().unwrap();
        let api = std::sync::Arc::new(crate::api::Api::new(
            crate::api::Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(root.path(), 7878).unwrap(),
            "fixture".into(),
            root.path().into(),
        ));
        assert!(snapshot(&api).await["operation"].is_null());
        assert!(snapshot(&api).await["current"].is_null());
        for (phase, sha, subject) in [
            ("draining", "a".repeat(40), Some("Title")),
            ("preparing", "a".repeat(39), Some("Title")),
            ("preparing", "z".repeat(40), Some("Title")),
            ("preparing", "a".repeat(40), None),
        ] {
            assert!(
                set(
                    State(api.clone()),
                    Default::default(),
                    Body(Request {
                        clear: false,
                        phase: Some(phase.into()),
                        commit: Some(sha),
                        subject: subject.map(str::to_owned),
                    })
                )
                .await
                .is_err()
            );
        }
        let prepare = || Request {
            clear: false,
            phase: Some("preparing".into()),
            commit: Some("a".repeat(40)),
            subject: Some("A change".into()),
        };
        assert!(
            set(State(api.clone()), Default::default(), Body(prepare()))
                .await
                .is_ok()
        );
        let value = snapshot(&api).await;
        assert_eq!(value["operation"]["phase"], "preparing");
        assert_eq!(value["target"]["sha"], "a".repeat(40));
        assert_eq!(value["target"]["subject"], "A change");
        assert!(value["target"]["url"].is_null());
        assert_eq!(value["operation"]["waiting"]["runs"], 0);
        let _ = set(
            State(api.clone()),
            Default::default(),
            Body(Request {
                clear: true,
                phase: None,
                commit: None,
                subject: None,
            }),
        )
        .await
        .ok()
        .unwrap();
        assert!(snapshot(&api).await["operation"].is_null());
        assert!(
            set(State(api.clone()), Default::default(), Body(prepare()))
                .await
                .is_ok()
        );
        drain(&api, Some(Kind::Update));
        assert_eq!(snapshot(&api).await["operation"]["phase"], "draining");
        assert_eq!(snapshot(&api).await["target"]["sha"], "a".repeat(40));
        drain(&api, None);
        assert_eq!(snapshot(&api).await["operation"]["kind"], "stop");
        assert!(snapshot(&api).await["target"].is_null());
    }

    #[tokio::test]
    async fn owner_browser_confirms_only_the_displayed_update() {
        use crate::auth::CSRF_HEADER;
        use axum::http::{Request, header};
        use gb_base::hub_update::{ManualStep, ManualUpdate};
        use tower::ServiceExt;

        let root = tempfile::tempdir().unwrap();
        let mut api = crate::api::Api::for_test(root.path(), Default::default());
        api.hub_update_dir = Some(root.path().into());
        let entry = api.auth.entry_link("/");
        let (cookie, csrf) = api
            .auth
            .start_session(entry.url.split_once('#').unwrap().1)
            .unwrap()
            .unwrap();
        let cookie = format!("{}={cookie}", api.auth.cookie);
        let bearer = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let app = crate::api::router(std::sync::Arc::new(api));
        let manual = ManualUpdate {
            sha: "a".repeat(40),
            release: "release/2".into(),
            steps: [1, 2]
                .map(|version| ManualStep {
                    version,
                    ticket: format!("https://hub.example/tickets/{version}"),
                })
                .into(),
        };
        let state_path = root.path().join(hub_update::STATE_FILE);
        std::fs::write(
            &state_path,
            json!({"status": Status::Manual(manual.clone())}).to_string(),
        )
        .unwrap();

        let call = async |path: &str, body: Value, headers: &[(&str, &str)]| {
            let read = body.is_null();
            let mut request = Request::builder()
                .method(if read { "GET" } else { "POST" })
                .uri(format!("/-/guildbotics/api/hub-status{path}"))
                .header("host", "127.0.0.1:8765")
                .header("content-type", "application/json");
            for (name, value) in headers {
                request = request.header(*name, *value);
            }
            let response = app
                .clone()
                .oneshot(
                    request
                        .body(axum::body::Body::from(body.to_string()))
                        .unwrap(),
                )
                .await
                .unwrap();
            let status = response.status();
            let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap();
            (status, serde_json::from_slice::<Value>(&bytes).unwrap())
        };
        let confirmation = json!({"sha":manual.sha});
        let headers = [("cookie", cookie.as_str()), (CSRF_HEADER, csrf.as_str())];
        let (_, before) = call("", Value::Null, &headers).await;
        assert_eq!(before["update"]["steps"], json!(manual.steps));
        assert_eq!(before["update"]["confirmed"], false);
        assert_eq!(
            call(
                "/confirm",
                confirmation.clone(),
                &[(
                    header::AUTHORIZATION.as_str(),
                    &format!("Bearer {}", bearer.trim())
                )]
            )
            .await
            .0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            call("/confirm", confirmation.clone(), &headers[..1])
                .await
                .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            call("/confirm", json!({"sha":"b".repeat(40)}), &headers)
                .await
                .0,
            StatusCode::CONFLICT
        );
        assert!(!manual.confirmed(root.path()).unwrap());
        assert_eq!(
            call("/confirm", confirmation, &headers).await.0,
            StatusCode::OK
        );
        assert!(manual.confirmed(root.path()).unwrap());
        let (_, after) = call("", Value::Null, &headers).await;
        assert_eq!(after["update"]["confirmed"], true);
        assert!(after["operation"].is_null());

        let failure = Status::Failed(hub_update::Failure {
            sha: manual.sha,
            stage: "update".into(),
            reason: "could not activate the new build".into(),
            next: "run recover".into(),
        });
        std::fs::write(state_path, json!({"status": failure}).to_string()).unwrap();
        let (_, failed) = call("", Value::Null, &headers).await;
        assert_eq!(failed["update"], json!(failure));
    }

    #[tokio::test]
    async fn build_versions_use_provider_links_without_member_credentials() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("workspace");
        let config = workspace.join(".guildbotics/config/team");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::write(
            config.join("project.yml"),
            r#"{"services":{"code_hosting_service":{"name":"branvil"}}}"#,
        )
        .unwrap();
        let data = root.path().join("branvil");
        branvil::test_support::initialize(&data, "owner").unwrap();
        let running = branvil::test_support::Running::start(
            &data,
            std::sync::Arc::new(branvil::vault::Memory::default()),
        )
        .await
        .unwrap();
        let base = running.data.base_url().to_owned();
        let api = crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(workspace),
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(root.path(), 7878).unwrap(),
            "fixture".into(),
            root.path().join("data"),
        );
        assert!(api.branvil.set(running.data.clone()).is_ok());
        let api = std::sync::Arc::new(api);
        assert!(
            set(
                State(api.clone()),
                Default::default(),
                Body(Request {
                    clear: false,
                    phase: Some("preparing".into()),
                    commit: Some("b".repeat(40)),
                    subject: Some("Next title".into())
                })
            )
            .await
            .is_ok()
        );
        let metadata = |remote: String| {
            Some(BuildMetadata {
                sha: "a".repeat(40),
                subject: "Current title".into(),
                tag: Some("release/2026-11-27-1".into()),
                remote: Some(remote),
            })
        };
        let value = snapshot_with_build(&api, metadata(format!("{base}/git/acme/demo.git"))).await;
        assert_eq!(value["current"]["subject"], "Current title");
        assert_eq!(value["current"]["tag"], "release/2026-11-27-1");
        assert_eq!(
            value["current"]["url"],
            format!(
                "{base}/workspaces/acme/repos/demo/commits/{}",
                "a".repeat(40)
            )
        );
        assert_eq!(
            value["target"]["url"],
            format!(
                "{base}/workspaces/acme/repos/demo/commits/{}",
                "b".repeat(40)
            )
        );
        let unknown =
            snapshot_with_build(&api, metadata("https://unrecognized.test/demo.git".into())).await;
        assert!(unknown["current"]["url"].is_null());
        for field in ["sha", "subject", "tag"] {
            assert_eq!(unknown["current"][field], value["current"][field]);
        }
        assert!(unknown["target"]["url"].is_null());
        assert!(snapshot_with_build(&api, None).await["current"].is_null());
        running.stop().await.unwrap();
    }
}
