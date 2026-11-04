//! The environment's shared preflight, presented to the screens and CLI.
use crate::api::{ApiError, Shared};
use axum::{
    Json,
    extract::State,
    http::HeaderMap,
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use std::path::PathBuf;
use std::sync::{LazyLock, Mutex};
static BUILD_OUTPUT: LazyLock<Mutex<std::collections::BTreeMap<PathBuf, Vec<String>>>> =
    LazyLock::new(|| Mutex::new(Default::default()));
fn output_key(workspace: &std::path::Path) -> PathBuf {
    gb_base::host_path::normalize(workspace).unwrap_or_else(|_| workspace.to_owned())
}
fn begin_output(workspace: &std::path::Path) {
    BUILD_OUTPUT
        .lock()
        .unwrap()
        .insert(output_key(workspace), Vec::new());
}
fn output(workspace: &std::path::Path, chunk: &str) {
    let mut all = BUILD_OUTPUT.lock().unwrap();
    let lines = all.entry(output_key(workspace)).or_default();
    for line in chunk.lines() {
        lines.push(line.chars().take(4096).collect());
    }
    if lines.len() > 200 {
        lines.drain(..lines.len() - 200);
    }
}
fn attach_output(workspace: &std::path::Path, view: &mut Value) {
    view["snapshot"]["output"] = json!(
        BUILD_OUTPUT
            .lock()
            .unwrap()
            .get(&output_key(workspace))
            .cloned()
            .unwrap_or_default()
    );
}

fn image_projection(
    images: Vec<(String, String, Option<u64>)>,
    declared: Option<&gb_config::agent_environment::BaseImage>,
    architecture: &str,
) -> Value {
    json!({"architecture":architecture,"images":images.into_iter().map(|(reference,digest,size)| {
        let selected = declared.is_some_and(|image| image.reference == reference && image.digests.get(architecture) == Some(&digest));
        json!({"reference":reference,"digest":digest,"size_bytes":size,"declared":selected})
    }).collect::<Vec<_>>(),"problem":""})
}

pub fn workspace(explicit: Option<PathBuf>) -> anyhow::Result<PathBuf> {
    if let Some(path) = explicit {
        return Ok(path);
    }
    gb_workspace::selection::restore(&gb_base::home::data()?, |key| std::env::var_os(key))?
        .ok_or_else(|| anyhow::anyhow!("No workspace is selected"))
}

/// Compare and update the shared declaration within the configuration writer.
pub fn declare(
    workspace: &std::path::Path,
    reference: Option<String>,
    digests: std::collections::BTreeMap<String, String>,
) -> anyhow::Result<()> {
    use gb_config::{agent_environment::PATH, repository::ConfigRepository};
    let repository = ConfigRepository::new(gb_base::shared_writes::SharedWrites::new(
        gb_base::home::identity_root()?,
        Some(workspace.into()),
    ))?;
    let current = repository.read_config(PATH)?;
    let expected = current
        .as_ref()
        .map(|s| (PATH.into(), s.blob_id.clone()))
        .into_iter()
        .collect();
    repository.write(
        &expected,
        |view, writer| {
            let mut value = match view.read_config(PATH)? {
                Some(snapshot) => snapshot.value()?,
                None => gb_config::yaml_value(&std::fs::read_to_string(
                    gb_base::home::python_package().join("templates").join(PATH),
                )?)?,
            };
            update_image(&mut value, reference, digests)?;
            writer.write_json(&view.path(PATH)?, &value)
        },
        |view| view.revisions(&[PATH]),
    )?;
    Ok(())
}

fn update_image(
    value: &mut Value,
    reference: Option<String>,
    digests: std::collections::BTreeMap<String, String>,
) -> anyhow::Result<()> {
    match reference {
        None => {
            value
                .as_object_mut()
                .ok_or_else(|| anyhow::anyhow!("Invalid declaration"))?
                .remove("image");
        }
        Some(reference) => {
            let mut kept = if value["image"]["reference"].as_str() == Some(&reference) {
                value["image"]["digests"]
                    .as_object()
                    .cloned()
                    .unwrap_or_default()
            } else {
                Default::default()
            };
            for (architecture, digest) in digests {
                kept.insert(architecture, json!(digest));
            }
            anyhow::ensure!(
                !reference.is_empty()
                    && !reference.starts_with('-')
                    && !reference.chars().any(char::is_whitespace),
                "Invalid image reference"
            );
            anyhow::ensure!(!kept.is_empty(), "Name at least one architecture digest");
            for (arch, digest) in &kept {
                anyhow::ensure!(
                    ["amd64", "arm64"].contains(&arch.as_str()),
                    "Invalid image architecture {arch}"
                );
                let text = digest.as_str().unwrap_or_default();
                anyhow::ensure!(
                    text.strip_prefix("sha256:").is_some_and(|v| v.len() == 64
                        && v.bytes()
                            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())),
                    "Invalid image digest"
                );
            }
            value["image"] = json!({"reference":reference,"digests":kept});
        }
    }
    Ok(())
}

pub async fn status(
    workspace: PathBuf,
    data: PathBuf,
    home: PathBuf,
    language: &str,
) -> anyhow::Result<Value> {
    let host = gb_environment::Host {
        data,
        home,
        package: gb_base::home::python_package(),
        identity_root: gb_base::home::identity_root()?,
    };
    let device = gb_environment::device::read(&host, &workspace).await;
    let mut view = device.rendered(|s| crate::commands::say(s, language));
    attach_output(&workspace, &mut view);
    let catalog: Value = serde_json::from_slice(&std::fs::read(
        host.package
            .join("templates/environment/cli_provision.json"),
    )?)?;
    let mut tools = crate::host_window::tool_status(&host.data, &host.home, &catalog);
    if let Some(rows) = tools.as_array_mut() {
        for row in rows {
            let name = row["name"].as_str().unwrap_or_default().to_owned();
            row["label"] = json!(name);
            row["config_reference"] = json!(format!("cli_agents/{name}/default.yml"));
            row["credentials_saved"] = row["saved"].clone();
            row["usage_check"] = Value::Null;
            row["usage_supported"] = json!(true);
            row["problem"] = json!(if row["provisioned"] != true {
                format!("{name} is not provisioned")
            } else if row["saved"] != true {
                format!(
                    "Log in with {}",
                    row["login_command"].as_str().unwrap_or_default()
                )
            } else {
                String::new()
            });
        }
    }
    view["tools"] = tools;
    let config = workspace.join(".guildbotics/config");
    let roots = gb_config::commands::CommandRoots {
        config: config.clone(),
        templates: host.package.join("templates"),
    };
    let mut members = vec![];
    if let Ok(team) = gb_config::team::Team::load(&config, &host.identity_root) {
        for member in team.members.into_iter().filter(|m| m.is_active) {
            let slots=match gb_config::resolved_guest::resolve(&roots,&host.identity_root,&member.person_id){
            Ok(resolved)=>resolved["brains"].as_object().into_iter().flatten().map(|(name,brain)|{
                let tool=brain["definition"]["adapter"].as_str().unwrap_or_default();
                let reason=view["tools"].as_array().into_iter().flatten().find(|r|r["name"]==tool).and_then(|r|r["problem"].as_str()).unwrap_or_default();
                json!({"slot":name,"tool":tool,"problems":if reason.is_empty(){vec![]}else{vec![json!({"setting":"tool","reason":reason})]}})
            }).collect::<Vec<_>>(),
            Err(error)=>vec![json!({"slot":"default","tool":"","problems":[{"setting":"declaration","reason":error.to_string()}]})],
        };
            members.push(json!({"person_id":member.person_id,"slots":slots}));
        }
    }
    view["members"] = json!(members);
    Ok(view)
}

pub(crate) async fn get(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let Some(workspace) = api.workspace.clone() else {
        return ApiError::new(
            axum::http::StatusCode::BAD_REQUEST,
            "config_not_found",
            &headers,
        )
        .into_response();
    };
    let language = headers
        .get("x-guildbotics-language")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("en");
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    match status(workspace, api.data.clone(), home, language).await {
        Ok(view) => Json(view).into_response(),
        Err(error) => ApiError::new(
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            &headers,
        )
        .reason(error.to_string())
        .into_response(),
    }
}

pub(crate) async fn images(State(api): State<Shared>) -> Json<Value> {
    let templates = gb_base::home::python_package().join("templates");
    let architecture = gb_environment::image::device_architecture();
    let result = async {
        let default = gb_environment::recipe::Recipe::load(&templates)
            .map_err(|e| anyhow::anyhow!("{e}"))?
            .image;
        let declared = match &api.workspace {
            Some(workspace) => gb_config::agent_environment::image(
                &workspace.join(".guildbotics/config"),
                &templates,
            )
            .map_err(|e| anyhow::anyhow!("{e}"))?,
            None => None,
        };
        let images = gb_environment::runtime::images(&api.data.join("msb"), &default)
            .await
            .map_err(|e| anyhow::anyhow!("{e:?}"))?;
        Ok::<_, anyhow::Error>(image_projection(images, declared.as_ref(), architecture))
    }
    .await;
    Json(match result {
        Ok(view) => view,
        Err(error) => json!({"architecture":architecture,"images":[],"problem":error.to_string()}),
    })
}

/// Build from one declaration reading; only a completed replacement prunes old snapshots.
pub async fn build(
    workspace: &std::path::Path,
    data: &std::path::Path,
    home: &std::path::Path,
    force: bool,
    language: &str,
    on_line: impl Fn(&str),
) -> anyhow::Result<PathBuf> {
    use gb_environment::{contract, image, recipe::Recipe, runtime, snapshot};
    let runtime_home = data.join("msb");
    runtime::ensure(&runtime_home).map_err(|e| anyhow::anyhow!("{e:?}"))?;
    let config = workspace.join(".guildbotics/config");
    let templates = gb_base::home::python_package().join("templates");
    let resources = gb_config::agent_environment::resources(&config, &templates)?;
    let recipe = Recipe::load(&templates).map_err(|e| anyhow::anyhow!("{e}"))?;
    let declared = gb_config::agent_environment::image(&config, &templates)?;
    let mut held = None;
    if let Some(image) = &declared {
        held = runtime::held_image(&runtime_home, &image.reference, &recipe.image)
            .await
            .map_err(|e| anyhow::anyhow!("{e:?}"))?;
        let command = image::load_command(home).map_err(anyhow::Error::msg)?;
        let status = image::status(image, image::device_architecture(), held.clone(), &command);
        if let Some(reason) = status.refusal {
            return Err(anyhow::anyhow!(crate::commands::say(reason, language)));
        }
        if let Some(warning) = status.warning {
            on_line(&format!(
                "[image] {}",
                crate::commands::say(warning, language)
            ));
        }
    }
    let name = recipe.name(held.as_deref());
    let current = snapshot::status(workspace, &name)?;
    if !force && current.state == snapshot::State::Ready {
        return Ok(current.path);
    }
    let network = contract::network(&config, &templates, false).map_err(anyhow::Error::msg)?;
    let base = declared
        .as_ref()
        .map(|i| i.reference.as_str())
        .unwrap_or(&recipe.image)
        .to_owned();
    let pull = declared.is_none();
    let result = snapshot::build(workspace, &name, |directory| async move {
        runtime::build_snapshot(
            runtime::Build {
                home: &runtime_home,
                directory: &directory,
                recipe: &recipe,
                image: &base,
                pull,
                guest_home: &home.to_string_lossy(),
                resources,
                nameservers: &network.nameservers,
            },
            on_line,
        )
        .await
    })
    .await
    .map_err(anyhow::Error::msg)?;
    Ok(result.path)
}

#[derive(serde::Deserialize, Default)]
pub(crate) struct BuildInput {
    #[serde(default)]
    force: bool,
}
pub(crate) async fn build_route(
    State(api): State<Shared>,
    headers: HeaderMap,
    crate::api::Body(input): crate::api::Body<BuildInput>,
) -> Response {
    let Some(workspace) = api.workspace.clone() else {
        return ApiError::new(
            axum::http::StatusCode::BAD_REQUEST,
            "config_not_found",
            &headers,
        )
        .into_response();
    };
    let language = headers
        .get("x-guildbotics-language")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("en");
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    begin_output(&workspace);
    let data = api.data.clone();
    let language = language.to_owned();
    api.work.spawn(async move {
        if let Err(error) = build(&workspace, &data, &home, input.force, &language, |line| {
            tracing::info!("{line}");
            output(&workspace, line);
        })
        .await
        {
            output(&workspace, &format!("Build failed: {error}"));
        }
    });
    Json(json!({"state":"building","output":[]})).into_response()
}

/// A CI archive belongs to the code host's runner store, not this device's store.
pub async fn load_ci(
    workspace: &std::path::Path,
    archive: &std::path::Path,
) -> anyhow::Result<Vec<String>> {
    use gb_config::secrets::{DeviceKey, Scope, SecretStore, SystemKeychain};
    let project = gb_config::yaml_value(&std::fs::read_to_string(
        workspace.join(".guildbotics/config/team/project.yml"),
    )?)?;
    if project
        .pointer("/services/code_hosting_service/name")
        .and_then(Value::as_str)
        != Some("branvil")
    {
        return Ok(vec![]);
    }
    let identity = gb_base::home::identity_root()?;
    let config = gb_config::yaml_value(&std::fs::read_to_string(
        identity.join(".guildbotics/config/connections/branvil.yml"),
    )?)?;
    let base = config["base_url"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("Branvil is not connected"))?
        .trim_end_matches('/');
    let writes = gb_base::shared_writes::SharedWrites::new(identity, Some(workspace.into()));
    let device = DeviceKey {
        machine_root: gb_base::home::machine_root()?,
        keychain: &SystemKeychain,
    };
    let token = SecretStore::new(&writes, Scope::Identity, device)?
        .get("BRANVIL_ADMIN_TOKEN")?
        .ok_or_else(|| anyhow::anyhow!("Branvil admin credential is unavailable"))?;
    let path = std::fs::canonicalize(archive)?;
    anyhow::ensure!(path.is_file(), "The archive is not a regular file");
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(10))
        .build()?;
    let response = client
        .post(format!("{base}/api/v1/admin/ci/images/load"))
        .bearer_auth(&token)
        .json(&json!({"path":path}))
        .send()
        .await?
        .error_for_status()?;
    let mut state: Value = response.json().await?;
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(30 * 60);
    loop {
        if let Some(loaded) = ci_result(&state["operation"], &path)? {
            return Ok(loaded);
        }
        anyhow::ensure!(
            tokio::time::Instant::now() < deadline,
            "CI image load exceeded 30 minutes"
        );
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        state = client
            .get(format!("{base}/api/v1/admin/ci"))
            .bearer_auth(&token)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
    }
}

/// The service owns this task and cancels it before releasing its lock.
pub(crate) async fn upkeep(api: Shared) {
    let Some(workspace) = api.workspace.clone() else {
        return;
    };
    // Linux guests used by CI have no nested KVM. They still report state,
    // but cannot start the runtime's background build microVM here.
    #[cfg(target_os = "linux")]
    if !std::path::Path::new("/dev/kvm").exists() {
        return;
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    gb_environment::snapshot::upkeep(|| async {
        let host = gb_environment::Host {
            data: api.data.clone(),
            home: home.clone(),
            package: gb_base::home::python_package(),
            identity_root: gb_base::home::identity_root().unwrap_or_default(),
        };
        let status = gb_environment::device::read(&host, &workspace).await;
        let state = status.view["snapshot"]["state"]
            .as_str()
            .unwrap_or_default();
        if ["missing", "stale"].contains(&state)
            && ["snapshot", ""]
                .contains(&status.view["problem_setting"].as_str().unwrap_or_default())
            && let Err(e) = build(&workspace, &api.data, &home, false, "en", |line| {
                tracing::info!("{line}");
                output(&workspace, line);
            })
            .await
        {
            tracing::error!("Environment upkeep failed: {e}");
            output(&workspace, &format!("Build failed: {e}"));
        }
    })
    .await;
}

fn ci_result(operation: &Value, path: &std::path::Path) -> anyhow::Result<Option<Vec<String>>> {
    anyhow::ensure!(!operation.is_null(), "CI image load outcome was lost");
    anyhow::ensure!(
        operation["kind"] == "load" && operation["target"].as_str() == path.to_str(),
        "CI image load was superseded by another operation"
    );
    match operation["state"].as_str() {
        Some("running") => Ok(None),
        Some("succeeded") => {
            let images = operation["loaded"]
                .as_array()
                .ok_or_else(|| anyhow::anyhow!("CI image result is malformed"))?;
            let references = images
                .iter()
                .map(|i| {
                    i["reference"]
                        .as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| anyhow::anyhow!("CI image reference is malformed"))
                })
                .collect::<anyhow::Result<Vec<_>>>()?;
            Ok(Some(references))
        }
        _ => anyhow::bail!("CI image load failed"),
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn build_output_is_bounded_scoped_and_projected_without_losing_warnings() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("workspace");
        super::begin_output(&workspace);
        super::output(&workspace, "[image] digest mismatch\n[step] provisioning");
        let mut view = serde_json::json!({"warning":"digest mismatch","image":{"warning":"digest mismatch"},"snapshot":{"state":"building","output":[]}});
        super::attach_output(&workspace.join("."), &mut view);
        assert_eq!(
            view["snapshot"]["output"],
            serde_json::json!(["[image] digest mismatch", "[step] provisioning"])
        );
        assert_eq!(view["snapshot"]["state"], "building");
        assert_eq!(view["warning"], view["image"]["warning"]);
        let mut other = serde_json::json!({"snapshot":{}});
        super::attach_output(&root.path().join("other"), &mut other);
        assert_eq!(other["snapshot"]["output"], serde_json::json!([]));
        for index in 0..205 {
            super::output(&workspace, &format!("step {index}"));
        }
        super::output(&workspace, &"é".repeat(5000));
        super::attach_output(&workspace, &mut view);
        let lines = view["snapshot"]["output"].as_array().unwrap();
        assert_eq!(lines.len(), 200);
        assert_eq!(lines[0], "step 6");
        assert_eq!(lines[199].as_str().unwrap().chars().count(), 4096);
        super::begin_output(&workspace);
        super::attach_output(&workspace, &mut view);
        assert_eq!(view["snapshot"]["output"], serde_json::json!([]));
    }

    #[test]
    fn declaration_and_picker_share_reference_digest_and_architecture_identity() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let old_digest = format!("sha256:{}", "b".repeat(64));
        let mut declaration = serde_json::json!({"memory_mib":2048,"network":{"mode":"deny"}});
        super::update_image(
            &mut declaration,
            Some("local/base:1".into()),
            [("arm64".into(), old_digest.clone())].into(),
        )
        .unwrap();
        super::update_image(
            &mut declaration,
            Some("local/base:1".into()),
            [("amd64".into(), digest.clone())].into(),
        )
        .unwrap();
        assert_eq!(declaration["image"]["digests"]["arm64"], old_digest);
        let image: gb_config::agent_environment::BaseImage =
            serde_json::from_value(declaration["image"].clone()).unwrap();
        let candidates = vec![
            ("local/base:1".into(), digest.clone(), Some(123)),
            ("local/other:1".into(), digest.clone(), None),
            ("local/base:1".into(), old_digest.clone(), None),
        ];
        let view = super::image_projection(candidates.clone(), Some(&image), "amd64");
        assert_eq!(view["images"][0]["declared"], true);
        assert_eq!(view["images"][0]["size_bytes"], 123);
        assert_eq!(view["images"][1]["declared"], false);
        assert_eq!(view["images"][2]["declared"], false);
        assert_eq!(
            super::image_projection(candidates, None, "amd64")["images"][0]["declared"],
            false
        );
        super::update_image(
            &mut declaration,
            Some("local/new:1".into()),
            [("amd64".into(), digest)].into(),
        )
        .unwrap();
        assert!(declaration["image"]["digests"].get("arm64").is_none());
        super::update_image(&mut declaration, None, Default::default()).unwrap();
        assert!(declaration.get("image").is_none());
        assert_eq!(declaration["memory_mib"], 2048);
        assert_eq!(declaration["network"]["mode"], "deny");
        for (reference, arch, digest) in [
            ("-invalid", "amd64", old_digest.as_str()),
            ("local/base:1", "riscv64", old_digest.as_str()),
            ("local/base:1", "amd64", "invalid"),
        ] {
            assert!(
                super::update_image(
                    &mut declaration,
                    Some(reference.into()),
                    [(arch.into(), digest.into())].into()
                )
                .is_err()
            );
        }
        assert!(
            !serde_json::from_str::<super::BuildInput>("{}")
                .unwrap()
                .force
        );
        assert!(
            serde_json::from_str::<super::BuildInput>("{\"force\":true}")
                .unwrap()
                .force
        );
    }

    #[test]
    fn ci_operation_identity_is_the_kind_and_exact_archive_path() {
        let path = std::path::Path::new("/tmp/archive.tar");
        let correct = serde_json::json!({"kind":"load","target":"/tmp/archive.tar","state":"succeeded","loaded":[{"reference":"local/agent:1"}]});
        assert_eq!(
            super::ci_result(&correct, path).unwrap(),
            Some(vec!["local/agent:1".into()])
        );
        for operation in [
            serde_json::Value::Null,
            serde_json::json!({"kind":"pull","target":"/tmp/archive.tar","state":"succeeded"}),
            serde_json::json!({"kind":"load","target":"/tmp/other.tar","state":"succeeded"}),
            serde_json::json!({"kind":"load","target":"/tmp/archive.tar","state":"failed"}),
        ] {
            assert!(super::ci_result(&operation, path).is_err());
        }
        assert_eq!(
            super::ci_result(
                &serde_json::json!({"kind":"load","target":"/tmp/archive.tar","state":"running"}),
                path
            )
            .unwrap(),
            None
        );
    }
}
