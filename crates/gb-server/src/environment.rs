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

fn project_tool(row: &mut Value, catalog: &Value, language: &str) {
    let name = row["name"].as_str().unwrap_or_default().to_owned();
    let label = catalog
        .as_array()
        .into_iter()
        .flatten()
        .find(|tool| tool["name"] == name)
        .and_then(|tool| tool["label"].as_str())
        .unwrap_or(&name)
        .to_owned();
    row["label"] = json!(label);
    row["config_reference"] = json!(format!("cli_agents/{name}/default.yml"));
    row["credentials_saved"] = row["saved"].clone();
    row["usage_supported"] = json!(true);
    let reason = if row["provisioned"] != true {
        Some("not_provisioned")
    } else {
        match row["credentials"].as_str().unwrap_or("missing") {
            "locked" => Some("credentials_locked"),
            "unavailable" => Some("credentials_unavailable"),
            "corrupt" => Some("credentials_corrupt"),
            _ if row["saved"] != true => Some("credentials_missing"),
            _ if row["authentication_failed"] == true => Some("authentication_failed"),
            _ => None,
        }
    };
    row["problem"] = json!(
        reason
            .map(|key| crate::i18n::t(
                language,
                &format!("intelligences.agent_environment.tool.{key}"),
                &[
                    ("tool", label.as_str()),
                    ("command", row["login_command"].as_str().unwrap_or_default()),
                ]
            ))
            .unwrap_or_default()
    );
    if row["problem"] == "" && row["usage_check"]["status"] == "failed" {
        row["problem"] = json!(crate::i18n::t(
            language,
            "app_api.errors.cli_agent_usage_failed",
            &[("label", row["label"].as_str().unwrap_or_default())]
        ));
    }
}

#[derive(Default)]
struct UsageReading {
    generation: u64,
    usage: Value,
    check: Value,
    checked: Option<std::time::Instant>,
}
#[derive(Default)]
struct ToolUsage {
    reading: Mutex<UsageReading>,
    probe: tokio::sync::Mutex<()>,
}
type UsageSlot = std::sync::Arc<ToolUsage>;
static PROBE_SLOTS: tokio::sync::Semaphore = tokio::sync::Semaphore::const_new(2);
static USAGE: LazyLock<Mutex<std::collections::BTreeMap<(PathBuf, String), UsageSlot>>> =
    LazyLock::new(Default::default);
fn usage_slot(data: &std::path::Path, tool: &str) -> UsageSlot {
    USAGE
        .lock()
        .unwrap()
        .entry((output_key(data), tool.into()))
        .or_default()
        .clone()
}
fn usage_reading(data: &std::path::Path, tool: &str) -> Value {
    usage_slot(data, tool).snapshot(tool, false)
}

pub(crate) fn forget_usage(data: &std::path::Path, tool: &str) {
    usage_slot(data, tool).forget();
}

impl ToolUsage {
    fn snapshot(&self, tool: &str, refreshing: bool) -> Value {
        let reading = self.reading.lock().unwrap();
        json!({"agent":tool,"usage":reading.usage,"check":reading.check,"refreshing":refreshing})
    }

    fn forget(&self) {
        let mut reading = self.reading.lock().unwrap();
        let generation = reading.generation.wrapping_add(1);
        *reading = UsageReading {
            generation,
            ..Default::default()
        };
    }

    async fn read<F, Fut>(
        &self,
        tool: &str,
        trace_id: &str,
        refresh: bool,
        requested: std::time::Instant,
        credentials_saved: impl Fn() -> bool,
        probe: F,
    ) -> Value
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<Value, String>>,
    {
        // Check before returning an old reading, including during a probe.
        if !credentials_saved() {
            self.forget();
            return self.snapshot(tool, false);
        }
        let _probe = match self.probe.try_lock() {
            Ok(guard) => guard,
            Err(_) => {
                if !refresh && {
                    let reading = self.reading.lock().unwrap();
                    !reading.usage.is_null() || reading.generation > 0
                } {
                    return self.snapshot(tool, true);
                }
                self.probe.lock().await
            }
        };
        // Credentials may have disappeared while another reader held the lock.
        if !credentials_saved() {
            self.forget();
            return self.snapshot(tool, false);
        }
        let due = {
            let reading = self.reading.lock().unwrap();
            let ttl = if reading.check["status"] == "succeeded" {
                300
            } else {
                30
            };
            reading
                .checked
                .is_none_or(|at| at < requested && (refresh || at.elapsed().as_secs() >= ttl))
        };
        if due {
            let generation = self.reading.lock().unwrap().generation;
            let result = probe().await;
            let mut reading = self.reading.lock().unwrap();
            if reading.generation == generation {
                reading.complete(result);
                reading.check["trace_id"] = json!(trace_id);
            }
        }
        self.snapshot(tool, false)
    }
}

#[derive(serde::Deserialize, Default)]
pub(crate) struct UsageInput {
    #[serde(default)]
    refresh: bool,
}
pub(crate) async fn usage(
    State(api): State<Shared>,
    axum::extract::Path(tool): axum::extract::Path<String>,
    axum::extract::Query(input): axum::extract::Query<UsageInput>,
    headers: HeaderMap,
) -> Response {
    let Some(workspace) = api.workspace.clone() else {
        return ApiError::new(
            axum::http::StatusCode::BAD_REQUEST,
            "config_not_found",
            &headers,
        )
        .into_response();
    };
    let catalog: Value = match std::fs::read(
        gb_base::home::python_package().join("templates/environment/cli_provision.json"),
    )
    .ok()
    .and_then(|b| serde_json::from_slice(&b).ok())
    {
        Some(value) => value,
        None => {
            return ApiError::new(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                &headers,
            )
            .into_response();
        }
    };
    if !catalog
        .as_array()
        .into_iter()
        .flatten()
        .any(|entry| entry["name"] == tool)
    {
        return ApiError::new(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response();
    }
    let data = api.data.clone();
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    if !crate::host_window::tool_status(&data, &home, &catalog)
        .as_array()
        .into_iter()
        .flatten()
        .any(|row| row["name"] == tool && row["saved"] == true)
    {
        forget_usage(&data, &tool);
    }
    let slot = usage_slot(&data, &tool);
    let cached = slot.snapshot(&tool, false);
    let return_cached = !input.refresh && !cached["usage"].is_null();
    let refreshing = {
        let reading = slot.reading.lock().unwrap();
        let ttl = if reading.check["status"] == "succeeded" {
            300
        } else {
            30
        };
        reading
            .checked
            .is_none_or(|at| at.elapsed().as_secs() >= ttl)
            || slot.probe.try_lock().is_err()
    };
    let requested = std::time::Instant::now();
    let cancelled = api.cancelled.clone();
    let task = api.work.spawn(async move {
        let slot = usage_slot(&data, &tool);
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default();
        let trace_id = gb_observability::Diagnostics::open(&workspace)
            .system_trace_id()
            .unwrap_or_default();
        slot.read(
            &tool,
            &trace_id,
            input.refresh,
            requested,
            || {
                crate::host_window::tool_status(&data, &home, &catalog)
                    .as_array()
                    .into_iter()
                    .flatten()
                    .any(|row| row["name"] == tool && row["saved"] == true)
            },
            || async {
                let host = gb_environment::Host {
                    data: data.clone(),
                    home: home.clone(),
                    package: gb_base::home::python_package(),
                    identity_root: gb_base::home::identity_root().unwrap_or_default(),
                };
                limited_probe(|| {
                    crate::tool_login::login_usage(&host, &workspace, &tool, cancelled)
                })
                .await
            },
        )
        .await
    });
    if return_cached {
        let mut value = cached;
        value["refreshing"] = refreshing.into();
        return Json(value).into_response();
    }
    match task.await {
        Ok(value) => Json(value).into_response(),
        Err(_) => ApiError::new(
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            &headers,
        )
        .into_response(),
    }
}
async fn limited_probe<F, Fut>(probe: F) -> Result<Value, String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<Value, String>>,
{
    let _capacity = PROBE_SLOTS.acquire().await.unwrap();
    probe().await
}

impl UsageReading {
    fn complete(&mut self, result: Result<Value, String>) {
        let succeeded = result.is_ok();
        if let Ok(value) = result {
            self.usage = value;
        }
        self.check = json!({"status":if succeeded {"succeeded"} else {"failed"},"checked_at":chrono::Utc::now().to_rfc3339(),"trace_id":""});
        self.checked = Some(std::time::Instant::now());
    }
}

fn member_slots(resolved: &Value, access: &Value, language: &str) -> Vec<Value> {
    resolved["brains"].as_object().into_iter().flatten().map(|(name, brain)| {
        let tool = brain["definition"]["adapter"].as_str().unwrap_or_default();
        let mut problems = Vec::new();
        if let Some(error) = brain["definition"]["error"].as_str() {
            problems.push(json!({"setting":"declaration","reason":error}));
        }
        for row in access["paths"].as_array().into_iter().flatten().filter(|row| row["present"] == false) {
            problems.push(json!({"setting":"grants","reason":crate::i18n::t(language,"intelligences.agent_environment.grants.local_path_missing",&[("path",row["path"].as_str().unwrap_or_default())])}));
        }
        json!({"slot":name,"tool":tool,"problems":problems})
    }).collect()
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
            if row["saved"] != true {
                forget_usage(&host.data, &name);
            }
            row["usage_check"] = usage_reading(&host.data, &name)["check"].clone();
            project_tool(row, &catalog, language);
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
            let slots = match gb_config::resolved_guest::resolve(
                &roots,
                &host.identity_root,
                &member.person_id,
            ) {
                Ok(resolved) => member_slots(&resolved, &view["access"], language),
                Err(error) => vec![
                    json!({"slot":"default","tool":"","problems":[{"setting":"declaration","reason":error.to_string()}]}),
                ],
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
    Json(
        match list_images(&api.data, api.workspace.as_deref()).await {
            Ok(view) => view,
            Err(error) => json!({
                "architecture":gb_environment::image::device_architecture(),
                "images":[],
                "problem":error.to_string(),
            }),
        },
    )
}

pub async fn list_images(
    data: &std::path::Path,
    workspace: Option<&std::path::Path>,
) -> anyhow::Result<Value> {
    let templates = gb_base::home::python_package().join("templates");
    let default = gb_environment::recipe::Recipe::load(&templates)
        .map_err(|e| anyhow::anyhow!("{e}"))?
        .image;
    let declared = match workspace {
        Some(workspace) => {
            gb_config::agent_environment::image(&workspace.join(".guildbotics/config"), &templates)
                .map_err(|e| anyhow::anyhow!("{e}"))?
        }
        None => None,
    };
    let images = gb_environment::runtime::images(&data.join("msb"), &default)
        .await
        .map_err(|e| anyhow::anyhow!("{e:?}"))?;
    Ok(image_projection(
        images,
        declared.as_ref(),
        gb_environment::image::device_architecture(),
    ))
}

fn build_timeout(language: &str) -> String {
    crate::commands::say(
        gb_environment::Sentence::Key(
            "intelligences.agent_environment.snapshot.build_timeout",
            vec![("minutes", "30".into())],
        ),
        language,
    )
}

/// Build from one declaration reading; only a completed replacement prunes old snapshots.
pub async fn build(
    workspace: &std::path::Path,
    data: &std::path::Path,
    home: &std::path::Path,
    force: bool,
    language: &str,
    on_line: impl Fn(&str) + Send + Sync + 'static,
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
    let network = contract::network(&config, &templates, false)
        .map_err(|reason| anyhow::anyhow!(crate::commands::say(reason, language)))?;
    let base = declared
        .as_ref()
        .map(|i| i.reference.as_str())
        .unwrap_or(&recipe.image)
        .to_owned();
    let pull = declared.is_none();
    let guest_home = gb_environment::guest_path(home).map_err(|reason| match reason {
        gb_environment::Failure::Command(sentence) | gb_environment::Failure::Runtime(sentence) => {
            anyhow::anyhow!(crate::commands::say(sentence, language))
        }
    })?;
    let result = snapshot::build(
        workspace,
        &name,
        build_timeout(language),
        move |directory, cancelled| async move {
            runtime::build_snapshot(
                runtime::Build {
                    home: &runtime_home,
                    directory: &directory,
                    recipe: &recipe,
                    image: &base,
                    pull,
                    guest_home: &guest_home,
                    resources,
                    nameservers: &network.nameservers,
                },
                on_line,
                cancelled,
            )
            .await
        },
        runtime::remove_snapshot,
    )
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
    let cancelled = api.cancelled.clone();
    api.work.spawn(async move {
        let output_workspace = workspace.clone();
        build_task(
            &workspace,
            cancelled,
            build(
                &workspace,
                &data,
                &home,
                input.force,
                &language,
                move |line| {
                    tracing::info!("{line}");
                    output(&output_workspace, line);
                },
            ),
        )
        .await;
    });
    Json(json!({"state":"building","output":[]})).into_response()
}

async fn build_task(
    workspace: &std::path::Path,
    cancelled: tokio_util::sync::CancellationToken,
    build: impl Future<Output = anyhow::Result<PathBuf>>,
) {
    tokio::select! {
        biased;
        _ = cancelled.cancelled() => {},
        result = build => {
            if let Err(error) = result {
                output(workspace, &format!("Build failed: {error}"));
            }
        }
    }
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

fn upkeep_decision(view: &Value, last_problem: &mut String) -> (bool, Option<String>) {
    let state = view["snapshot"]["state"].as_str().unwrap_or_default();
    let setting = view["problem_setting"].as_str().unwrap_or_default();
    let build = ["missing", "stale"].contains(&state) && ["snapshot", ""].contains(&setting);
    let problem = if build || ["ready", "building"].contains(&state) && setting.is_empty() {
        String::new()
    } else {
        view["problem"].as_str().unwrap_or_default().to_owned()
    };
    let notice = if problem != *last_problem && !problem.is_empty() {
        Some(problem.clone())
    } else {
        None
    };
    *last_problem = problem;
    (build, notice)
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
    let last_problem = Mutex::new(String::new());
    gb_environment::snapshot::upkeep(|| async {
        let host = gb_environment::Host {
            data: api.data.clone(),
            home: home.clone(),
            package: gb_base::home::python_package(),
            identity_root: gb_base::home::identity_root().unwrap_or_default(),
        };
        let status = gb_environment::device::read(&host, &workspace).await;
        let view = status.rendered(|sentence| crate::commands::say(sentence, "en"));
        let (should_build, notice) = upkeep_decision(&view, &mut last_problem.lock().unwrap());
        if let Some(notice) = notice {
            tracing::warn!("Environment upkeep: {notice}");
            output(&workspace, &notice);
        }
        let output_workspace = workspace.clone();
        if should_build
            && let Err(e) = build(&workspace, &api.data, &home, false, "en", move |line| {
                tracing::info!("{line}");
                output(&output_workspace, line);
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
    #[tokio::test]
    async fn diagnostics_preserves_guest_config_file_and_parse_reason() {
        const CHILD: &str = "GUILDBOTICS_CONFIG_DIAGNOSTICS_CHILD";
        if std::env::var_os(CHILD).is_none() {
            let root = tempfile::tempdir().unwrap();
            let result = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "environment::tests::diagnostics_preserves_guest_config_file_and_parse_reason",
                    "--nocapture",
                ])
                .env(CHILD, root.path())
                .env("HOME", root.path().join("home"))
                .env(gb_base::home::HOME_ENV, root.path().join("machine"))
                .env(
                    gb_base::home::IDENTITY_ROOT_ENV,
                    root.path().join("identity"),
                )
                .status()
                .unwrap();
            assert!(result.success());
            return;
        }
        let root = std::path::PathBuf::from(std::env::var_os(CHILD).unwrap());
        let workspace = root.join("workspace");
        let config = workspace.join(".guildbotics/config");
        let identity = root.join("identity/.guildbotics/config/team/members/aiko/identity.yml");
        let mapping = config.join("intelligences/brain_mapping.yml");
        for (path, content) in [
            (config.join("team/project.yml"), "language: en"),
            (
                config.join("team/members/aiko/member.yml"),
                "is_active: true",
            ),
            (identity, "person_id: aiko\nname: Aiko"),
            (mapping.clone(), "["),
        ] {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, content).unwrap();
        }
        let view = super::status(workspace, root.join("data"), root.join("home"), "en")
            .await
            .unwrap();
        assert_eq!(view["members"][0]["person_id"], "aiko");
        assert_eq!(
            view["members"][0]["slots"][0]["problems"][0]["setting"],
            "declaration"
        );
        assert_eq!(
            view["members"][0]["slots"][0]["problems"][0]["reason"],
            format!("{}: Invalid configuration YAML", mapping.display())
        );
    }

    #[tokio::test]
    async fn server_shutdown_cancels_build_work_without_publishing_a_failure() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().to_owned();
        super::begin_output(&workspace);
        let work = tokio_util::task::TaskTracker::new();
        let cancelled = tokio_util::sync::CancellationToken::new();
        let cancellation = cancelled.clone();
        let dropped = tokio_util::sync::CancellationToken::new();
        let dropped_guard = dropped.clone().drop_guard();
        let (started, running) = tokio::sync::oneshot::channel();
        let task_workspace = workspace.clone();
        work.spawn(async move {
            super::build_task(&task_workspace, cancellation, async move {
                let _held = dropped_guard;
                let _ = started.send(());
                std::future::pending().await
            })
            .await;
        });
        running.await.unwrap();
        cancelled.cancel();
        work.close();
        tokio::time::timeout(std::time::Duration::from_millis(100), work.wait())
            .await
            .unwrap();
        assert!(dropped.is_cancelled());
        let mut view = serde_json::json!({"snapshot":{}});
        super::attach_output(&workspace, &mut view);
        assert_eq!(view["snapshot"]["output"], serde_json::json!([]));
    }

    #[test]
    fn build_timeout_uses_the_locale_key_in_both_languages() {
        assert_eq!(
            super::build_timeout("en"),
            "The build did not finish within 30 minutes."
        );
        assert_eq!(
            super::build_timeout("ja"),
            "ビルドが 30 分以内に終わりませんでした。"
        );
    }

    #[tokio::test]
    async fn usage_route_is_owner_only_validates_tools_and_answers_without_a_login() {
        use axum::{
            body::{Body, to_bytes},
            http::{Request, StatusCode},
        };
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let data = root.path().join("data");
        let auth = crate::auth::ServerAuth::open(&data, 38761).unwrap();
        let token = std::fs::read_to_string(auth.owner_token_path()).unwrap();
        let api = std::sync::Arc::new(crate::api::Api {
            service: Default::default(),
            member: None,
            inputs: crate::input_files::InputFileStore::new(&data.join("inputs")).unwrap(),
            auth,
            instance_id: "fixture".into(),
            data,
            workspace: Some(root.path().join("workspace")),
            work: tokio_util::task::TaskTracker::new(),
            cancelled: tokio_util::sync::CancellationToken::new(),
            shutdown: crate::server::Shutdown::default(),
        });
        let app = crate::api::router(api);
        for (agent, authorized, expected) in [
            ("claude", false, StatusCode::UNAUTHORIZED),
            ("unknown", true, StatusCode::UNPROCESSABLE_ENTITY),
            ("claude", true, StatusCode::OK),
        ] {
            let mut request = Request::builder()
                .uri(format!(
                    "/api/intelligences/cli-agents/{agent}/usage?refresh=true"
                ))
                .header("host", "127.0.0.1:38761");
            if authorized {
                request = request.header("authorization", format!("Bearer {}", token.trim()));
            }
            let response = app
                .clone()
                .oneshot(request.body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), expected);
            if expected == StatusCode::OK {
                let value: serde_json::Value =
                    serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap())
                        .unwrap();
                assert_eq!(
                    value,
                    serde_json::json!({"agent":"claude","usage":null,"check":null,"refreshing":false})
                );
            }
        }
    }

    #[test]
    fn tool_guidance_uses_labels_language_and_each_credential_state() {
        let catalog = serde_json::json!([{"name":"claude","label":"Claude Code"}]);
        for (state, saved, failed, key) in [
            ("missing", false, false, "credentials_missing"),
            ("locked", false, false, "credentials_locked"),
            ("unavailable", false, false, "credentials_unavailable"),
            ("corrupt", false, false, "credentials_corrupt"),
            ("saved", true, true, "authentication_failed"),
        ] {
            for lang in ["ja", "en"] {
                let mut row = serde_json::json!({"name":"claude","provisioned":true,"credentials":state,"saved":saved,"authentication_failed":failed,"login_command":"/managed/guildbotics environment login claude"});
                super::project_tool(&mut row, &catalog, lang);
                let text = row["problem"].as_str().unwrap();
                assert!(text.contains("Claude Code"));
                assert!(!text.contains("%{"));
                assert_eq!(
                    text,
                    crate::i18n::t(
                        lang,
                        &format!("intelligences.agent_environment.tool.{key}"),
                        &[
                            ("tool", "Claude Code"),
                            ("command", "/managed/guildbotics environment login claude")
                        ]
                    )
                );
                if state == "locked" {
                    assert!(!text.contains("/managed/"));
                }
                assert_eq!(row["credentials_saved"], saved);
            }
        }
        let mut row = serde_json::json!({"name":"claude","provisioned":false,"saved":true});
        super::project_tool(&mut row, &catalog, "en");
        assert_eq!(
            row["problem"],
            "Claude Code is not provisioned in the agent environment yet."
        );
        row["provisioned"] = true.into();
        row["credentials"] = "saved".into();
        super::project_tool(&mut row, &catalog, "ja");
        assert_eq!(row["problem"], "");
        assert_eq!(row["label"], "Claude Code");
    }

    async fn poll_pending<F: std::future::Future>(mut future: std::pin::Pin<&mut F>) {
        std::future::poll_fn(|cx| {
            assert!(future.as_mut().poll(cx).is_pending());
            std::task::Poll::Ready(())
        })
        .await;
    }

    #[tokio::test]
    async fn usage_readers_share_refresh_and_cached_readers_do_not_wait() {
        let slot = super::ToolUsage::default();
        slot.reading
            .lock()
            .unwrap()
            .complete(Ok(serde_json::json!({"account":"old"})));
        let (release, released) = tokio::sync::oneshot::channel();
        let first = slot.read(
            "claude",
            "trace-1",
            true,
            std::time::Instant::now(),
            || true,
            || async {
                released.await.unwrap();
                Ok(serde_json::json!({"account":"new"}))
            },
        );
        tokio::pin!(first);
        poll_pending(first.as_mut()).await;
        let second = slot.read(
            "claude",
            "trace-1",
            true,
            std::time::Instant::now(),
            || true,
            || async { panic!("concurrent refresh must reuse the running probe") },
        );
        tokio::pin!(second);
        poll_pending(second.as_mut()).await;
        let cached = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            slot.read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || true,
                || async { panic!("cached read must not probe") },
            ),
        )
        .await
        .expect("cached read must not wait behind a refresh");
        assert_eq!(cached["usage"]["account"], "old");
        assert_eq!(cached["refreshing"], true);
        release.send(()).unwrap();
        let (first, second) = tokio::join!(first, second);
        assert_eq!(first, second);
        assert_eq!(first["usage"]["account"], "new");
        assert_eq!(first["check"]["status"], "succeeded");
        assert_eq!(first["refreshing"], false);
        let cached = slot
            .read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || true,
                || async { panic!("successful reading must remain cached") },
            )
            .await;
        assert_eq!(cached, first);
    }

    #[tokio::test]
    async fn lost_credentials_clear_reading_and_retire_the_running_probe() {
        let slot = super::ToolUsage::default();
        slot.reading
            .lock()
            .unwrap()
            .complete(Ok(serde_json::json!({"account":"old"})));
        let (release, released) = tokio::sync::oneshot::channel();
        let probe = slot.read(
            "claude",
            "trace-1",
            true,
            std::time::Instant::now(),
            || true,
            || async {
                released.await.unwrap();
                Ok(serde_json::json!({"account":"retired"}))
            },
        );
        tokio::pin!(probe);
        poll_pending(probe.as_mut()).await;
        let missing = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            slot.read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || false,
                || async { panic!("missing credentials must not probe") },
            ),
        )
        .await
        .expect("lost credentials must clear cached usage immediately");
        assert!(missing["usage"].is_null());
        assert!(missing["check"].is_null());
        release.send(()).unwrap();
        let retired = probe.await;
        assert_eq!(retired, missing);
        let current = slot
            .read(
                "claude",
                "trace-1",
                true,
                std::time::Instant::now(),
                || true,
                || async { Ok(serde_json::json!({"account":"current"})) },
            )
            .await;
        assert_eq!(current["usage"]["account"], "current");
    }

    #[test]
    fn forgetting_usage_updates_status_projection_and_keeps_other_tools() {
        let root = tempfile::tempdir().unwrap();
        for tool in ["claude", "codex"] {
            super::usage_slot(root.path(), tool)
                .reading
                .lock()
                .unwrap()
                .complete(Ok(serde_json::json!({"agent":tool})));
        }
        assert_eq!(
            super::usage_reading(root.path(), "claude")["check"]["status"],
            "succeeded"
        );
        super::forget_usage(root.path(), "claude");
        assert!(super::usage_reading(root.path(), "claude")["check"].is_null());
        assert_eq!(
            super::usage_reading(root.path(), "codex")["check"]["status"],
            "succeeded"
        );
    }

    #[test]
    fn usage_failure_preserves_last_success_and_updates_latest_check() {
        let mut reading = super::UsageReading::default();
        let usage = serde_json::json!({"agent":"claude","windows":[],"limit_reached":false,"checked_at":"2026-11-04T00:00:00Z"});
        reading.complete(Ok(usage.clone()));
        assert_eq!(reading.usage, usage);
        assert_eq!(reading.check["status"], "succeeded");
        assert!(!reading.check["checked_at"].as_str().unwrap().is_empty());
        reading.complete(Err("probe failed".into()));
        assert_eq!(reading.usage, usage);
        assert_eq!(reading.check["status"], "failed");
        assert!(reading.checked.is_some());
    }

    #[tokio::test]
    async fn usage_first_failure_retries_after_thirty_seconds_and_success_after_five_minutes() {
        let slot = super::ToolUsage::default();
        let failed = slot
            .read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || true,
                || async { Err("provider detail".into()) },
            )
            .await;
        assert_eq!(failed["check"]["trace_id"], "trace-1");
        assert!(failed["usage"].is_null());
        assert_eq!(failed["check"]["status"], "failed");
        slot.reading.lock().unwrap().checked =
            Some(std::time::Instant::now() - std::time::Duration::from_secs(31));
        let succeeded = slot
            .read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || true,
                || async { Ok(serde_json::json!({"windows":[{"used_percent":12}]})) },
            )
            .await;
        assert_eq!(succeeded["usage"]["windows"][0]["used_percent"], 12);
        slot.reading.lock().unwrap().checked =
            Some(std::time::Instant::now() - std::time::Duration::from_secs(31));
        slot.read(
            "claude",
            "trace-1",
            false,
            std::time::Instant::now(),
            || true,
            || async { panic!("successful reading still fresh") },
        )
        .await;
        slot.reading.lock().unwrap().checked =
            Some(std::time::Instant::now() - std::time::Duration::from_secs(301));
        let refreshed = slot
            .read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || true,
                || async { Ok(serde_json::json!({"windows":[]})) },
            )
            .await;
        assert_eq!(refreshed["usage"]["windows"], serde_json::json!([]));
    }

    #[tokio::test]
    async fn a_slow_usage_tool_never_holds_another_tool_and_logout_keeps_one_probe() {
        let slow = super::ToolUsage::default();
        let fast = super::ToolUsage::default();
        let (release, released) = tokio::sync::oneshot::channel();
        let pending = slow.read(
            "claude",
            "trace-1",
            false,
            std::time::Instant::now(),
            || true,
            || async {
                released.await.unwrap();
                Ok(serde_json::json!({"account":"retired"}))
            },
        );
        tokio::pin!(pending);
        poll_pending(pending.as_mut()).await;
        let other = fast
            .read(
                "codex",
                "trace-1",
                false,
                std::time::Instant::now(),
                || true,
                || async { Ok(serde_json::json!({"account":"other"})) },
            )
            .await;
        assert_eq!(other["usage"]["account"], "other");
        let logged_out = slow
            .read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || false,
                || async { panic!("logged out") },
            )
            .await;
        assert!(logged_out["usage"].is_null());
        let back = slow
            .read(
                "claude",
                "trace-1",
                false,
                std::time::Instant::now(),
                || true,
                || async { panic!("must not start second probe") },
            )
            .await;
        assert_eq!(back["refreshing"], true);
        assert!(back["usage"].is_null());
        release.send(()).unwrap();
        assert!(pending.await["usage"].is_null());
        let fresh = slow
            .read(
                "claude",
                "trace-1",
                true,
                std::time::Instant::now(),
                || true,
                || async { Ok(serde_json::json!({"account":"new"})) },
            )
            .await;
        assert_eq!(fresh["usage"]["account"], "new");
    }

    #[tokio::test]
    async fn usage_probes_share_two_slots_and_release_them_on_completion() {
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        let running = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let gate = Arc::new(tokio::sync::Semaphore::new(0));
        let (entered, mut entries) = tokio::sync::mpsc::unbounded_channel();
        let mut tasks = Vec::new();
        for _ in 0..3 {
            let (running, peak, gate, entered) =
                (running.clone(), peak.clone(), gate.clone(), entered.clone());
            tasks.push(tokio::spawn(async move {
                super::limited_probe(|| async move {
                    let count = running.fetch_add(1, Ordering::SeqCst) + 1;
                    peak.fetch_max(count, Ordering::SeqCst);
                    entered.send(()).unwrap();
                    gate.acquire().await.unwrap().forget();
                    running.fetch_sub(1, Ordering::SeqCst);
                    Ok(serde_json::json!({}))
                })
                .await
            }));
        }
        entries.recv().await.unwrap();
        entries.recv().await.unwrap();
        assert_eq!(running.load(Ordering::SeqCst), 2);
        assert!(entries.try_recv().is_err());
        gate.add_permits(3);
        for task in tasks {
            task.await.unwrap().unwrap();
        }
        assert_eq!(peak.load(Ordering::SeqCst), 2);
        assert_eq!(running.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn member_slots_only_repeat_local_grant_problems_and_keep_tool_guidance_on_device() {
        let resolved = serde_json::json!({"brains":{"default":{"definition":{"adapter":"claude"}},"broken":{"definition":{"error":"invalid mapping"}}}});
        let access = serde_json::json!({"documents":[{"path":"/absent/document","present":false}],"paths":[{"path":"/absent/local","present":false}]});
        let slots = super::member_slots(&resolved, &access, "en");
        let valid = slots.iter().find(|slot| slot["slot"] == "default").unwrap();
        assert_eq!(valid["tool"], "claude");
        assert_eq!(valid["problems"].as_array().unwrap().len(), 1);
        assert_eq!(valid["problems"][0]["setting"], "grants");
        assert!(
            valid["problems"][0]["reason"]
                .as_str()
                .unwrap()
                .contains("/absent/local")
        );
        let invalid = slots.iter().find(|slot| slot["slot"] == "broken").unwrap();
        assert_eq!(invalid["problems"][0]["setting"], "declaration");
        assert_eq!(
            super::member_slots(&resolved, &serde_json::json!({"paths":[]}), "ja")
                .iter()
                .find(|slot| slot["slot"] == "default")
                .unwrap()["problems"],
            serde_json::json!([])
        );
    }

    #[test]
    fn usage_failure_guidance_uses_label_and_language_after_authentication_guidance() {
        let catalog = serde_json::json!([{"name":"claude","label":"Claude Code"}]);
        for lang in ["en", "ja"] {
            let mut row = serde_json::json!({"name":"claude","provisioned":true,"saved":true,"credentials":"saved","usage_check":{"status":"failed"},"login_command":"managed login"});
            super::project_tool(&mut row, &catalog, lang);
            assert_eq!(
                row["problem"],
                crate::i18n::t(
                    lang,
                    "app_api.errors.cli_agent_usage_failed",
                    &[("label", "Claude Code")]
                )
            );
            row["authentication_failed"] = true.into();
            super::project_tool(&mut row, &catalog, lang);
            assert!(row["problem"].as_str().unwrap().contains("managed login"));
        }
    }

    #[test]
    fn upkeep_builds_only_missing_or_stale_snapshots_and_reports_a_blocker_once() {
        let mut last = String::new();
        for state in ["missing", "stale"] {
            assert_eq!(
                super::upkeep_decision(
                    &serde_json::json!({"snapshot":{"state":state},"problem_setting":"snapshot","problem":"build it"}),
                    &mut last
                ),
                (true, None)
            );
        }
        for state in ["ready", "building", "failed"] {
            assert!(!super::upkeep_decision(&serde_json::json!({"snapshot":{"state":state},"problem_setting":"","problem":""}),&mut last).0);
        }
        for (setting, problem) in [
            ("runtime", "no hypervisor"),
            ("image", "load custom image"),
            ("declaration", "invalid DNS"),
        ] {
            let view = serde_json::json!({"snapshot":{"state":"missing"},"problem_setting":setting,"problem":problem});
            assert_eq!(
                super::upkeep_decision(&view, &mut last),
                (false, Some(problem.into()))
            );
            assert_eq!(super::upkeep_decision(&view, &mut last), (false, None));
        }
        let ready =
            serde_json::json!({"snapshot":{"state":"ready"},"problem_setting":"","problem":""});
        assert_eq!(super::upkeep_decision(&ready, &mut last), (false, None));
        assert_eq!(last, "");
        let missing = serde_json::json!({"snapshot":{"state":"missing"},"problem_setting":"snapshot","problem":"build"});
        assert!(super::upkeep_decision(&missing, &mut last).0);
    }

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
