//! Device-local Hub maintenance state. No shared record is written.
use crate::api::{Body, Shared};
use axum::{Json, extract::State};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

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

fn build_metadata(source: &std::path::Path) -> Option<Value> {
    std::fs::read(source.join("hub-build.json"))
        .ok()
        .and_then(|s| serde_json::from_slice(&s).ok())
}

pub(crate) async fn snapshot(api: &Shared) -> Value {
    let source = gb_base::home::python_package();
    snapshot_with_build(api, source.parent().and_then(build_metadata)).await
}

async fn snapshot_with_build(api: &Shared, metadata: Option<Value>) -> Value {
    let phase = api.hub_phase.lock().unwrap().clone();
    let mut current = metadata
        .as_ref()
        .map(|v| json!({"sha":v["sha"],"subject":v["subject"],"url":null}));
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
        .zip(metadata.as_ref().and_then(|v| v["remote"].as_str()))
        .and_then(|(_, remote)| {
            let data = api.branvil.get()?;
            Some((
                data,
                branvil::agent::CodeAgent::repository_for_remote(data, remote)?,
            ))
        });
    let mut releases = Value::Null;
    if let Some((data, repository)) = &hosted {
        for version in [&mut current, &mut target].into_iter().flatten() {
            let sha = version["sha"].as_str().unwrap_or_default().to_owned();
            version["url"] = json!(branvil::agent::CodeAgent::commit_url_for(
                data, repository, &sha
            ));
        }
        if let Some(sha) = current.as_ref().and_then(|v| v["sha"].as_str()) {
            releases = release_status(data, repository, sha).await;
        }
    }
    let operation = phase.map(|p|json!({"kind":p.kind,"phase":p.phase,"since":p.since,"commit":p.commit,"waiting":{"runs":api.selected().service.execution.generation().snapshot().len(),"ci_jobs":0}}));
    let mut result =
        json!({"current":current,"target":target,"operation":operation,"releases":releases});
    if result["operation"].is_object()
        && let Some(host) = api.branvil.get()
    {
        result["operation"]["waiting"]["ci_jobs"] = json!(host.running_ci_jobs().await);
    }
    result
}

/// The release the Hub runs and the releases it has not applied yet; null
/// when the code host cannot tell.
async fn release_status(data: &branvil::api::DataApi, repository: &str, sha: &str) -> Value {
    let owner = data.owner();
    let (Ok(all), Ok(newer)) = (
        owner.releases(repository, None).await,
        owner.releases(repository, Some(sha.to_owned())).await,
    ) else {
        return Value::Null;
    };
    let running = all.releases.iter().find(|r| r.sha == sha).map(|r| &r.name);
    // A build off the first-parent history has no releases after it.
    let pending: Vec<_> = newer
        .releases
        .iter()
        .filter(|_| newer.since_on_main == Some(true))
        .map(|r| json!({"name":r.name,"sha":r.sha,"subject":r.title}))
        .collect();
    json!({
        "url": branvil::agent::CodeAgent::releases_url_for(data, repository),
        "running": running,
        "pending": pending,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn metadata_is_available_only_when_the_build_wrote_it() {
        let root = tempfile::tempdir().unwrap();
        assert!(build_metadata(root.path()).is_none());
        std::fs::write(root.path().join("hub-build.json"), br#"{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","subject":"Built source","remote":"https://example.test/repo.git"}"#).unwrap();
        let value = build_metadata(root.path()).unwrap();
        assert_eq!(value["sha"], "a".repeat(40));
        assert_eq!(value["subject"], "Built source");
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
            Some(json!({"sha":"a".repeat(40),"subject":"Current title","remote":remote}))
        };
        let value = snapshot_with_build(&api, metadata(format!("{base}/git/acme/demo.git"))).await;
        assert_eq!(value["current"]["subject"], "Current title");
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
        assert!(unknown["target"]["url"].is_null());
        assert!(snapshot_with_build(&api, None).await["current"].is_null());
        running.stop().await.unwrap();
    }

    #[tokio::test]
    async fn the_hub_names_its_release_and_the_newer_ones_it_has_not_applied() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("workspace");
        let config = workspace.join(".guildbotics/config/team");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::write(
            config.join("project.yml"),
            r#"{"services":{"code_hosting_service":{"name":"branvil"}}}"#,
        )
        .unwrap();
        let hub = branvil::test_support::Fixture::new("owner").await;
        hub.workspace("acme").await;
        hub.repository("acme/demo").await;
        let running = hub.commit("acme/demo", "main", None, "a.txt", "running");
        let between = hub.commit("acme/demo", "main", Some(&running), "a.txt", "between");
        let newer = hub.commit("acme/demo", "main", Some(&between), "a.txt", "newer");
        for (tag, sha) in [("release/1", &running), ("release/2", &newer)] {
            hub.git(
                "acme/demo",
                &["update-ref", &format!("refs/tags/{tag}"), sha],
            );
            let verified = format!("refs/branvil/verified/{sha}");
            hub.git("acme/demo", &["update-ref", &verified, sha]);
        }
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
        assert!(api.branvil.set(hub.data()).is_ok());
        let api = std::sync::Arc::new(api);
        let base = hub.data().base_url().to_owned();
        let remote = format!("{base}/git/acme/demo.git");
        let value = snapshot_with_build(
            &api,
            Some(json!({"sha":running,"subject":"running","remote":remote})),
        )
        .await;
        assert_eq!(
            value["releases"],
            json!({
                "url": format!("{base}/workspaces/acme/repos/demo/releases"),
                "running": "release/1",
                "pending": [{"name": "release/2", "sha": newer, "subject": "main"}],
            })
        );
        let unreleased = snapshot_with_build(
            &api,
            Some(json!({"sha":between,"subject":"between","remote":remote})),
        )
        .await;
        assert!(unreleased["releases"]["running"].is_null());
        assert_eq!(unreleased["releases"]["pending"][0]["name"], "release/2");
        // A build off main's first-parent history has nothing pending.
        let off = hub.commit("acme/demo", "side", Some(&running), "a.txt", "side");
        let off_main = snapshot_with_build(
            &api,
            Some(json!({"sha":off,"subject":"side","remote":remote})),
        )
        .await;
        assert_eq!(off_main["releases"]["pending"], json!([]));
        hub.stop().await;
    }
}
