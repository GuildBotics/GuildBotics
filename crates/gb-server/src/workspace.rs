use std::path::{Path, PathBuf};

use gb_workspace::selection;

fn workspace_location(path: &Path, language: &str) -> anyhow::Result<PathBuf> {
    let home = std::env::var_os(gb_base::home::USER_HOME_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .ok_or_else(|| {
            anyhow::anyhow!(crate::i18n::t(language, "cli.workspace.home_required", &[]))
        })?;
    gb_environment::contract::workspace_location(path, &home)
        .map_err(|sentence| anyhow::anyhow!(crate::i18n::say(sentence, language)))
}

pub fn select(data: &Path, path: &Path, language: &str) -> anyhow::Result<PathBuf> {
    let workspace = workspace_location(path, language)?;
    selection::write_active(data, &workspace).map_err(|error| registry_error(error, language))?;
    Ok(workspace)
}

pub fn resolve(data: &Path, explicit: Option<PathBuf>, language: &str) -> anyhow::Result<PathBuf> {
    resolve_with_message(
        data,
        explicit,
        language,
        "cli.member.client.workspace_required",
    )
}

pub fn resolve_with_message(
    data: &Path,
    explicit: Option<PathBuf>,
    language: &str,
    missing: &str,
) -> anyhow::Result<PathBuf> {
    let workspace = read_selection_with_message(data, explicit, language, missing)?;
    selection::register(data, &workspace).map_err(|error| registry_error(error, language))?;
    Ok(workspace)
}

pub fn read_selection(
    data: &Path,
    explicit: Option<PathBuf>,
    language: &str,
) -> anyhow::Result<PathBuf> {
    read_selection_with_message(
        data,
        explicit,
        language,
        "cli.member.client.workspace_required",
    )
}

fn read_selection_with_message(
    data: &Path,
    explicit: Option<PathBuf>,
    language: &str,
    missing: &str,
) -> anyhow::Result<PathBuf> {
    let named = match explicit {
        Some(path) => path,
        None => selection::resolve(data, |key| std::env::var_os(key))?
            .ok_or_else(|| anyhow::anyhow!(crate::i18n::t(language, missing, &[])))?,
    };
    workspace_location(&named, language)
}

pub fn active(data: &Path) -> anyhow::Result<Option<PathBuf>> {
    selection::read_active(data)
}

pub fn state_file(data: &Path) -> PathBuf {
    data.join(selection::ACTIVE_FILE)
}

/// Turn a registry failure into the repair instructions at the host boundary.
pub(crate) fn registry_error(error: anyhow::Error, language: &str) -> anyhow::Error {
    let Some(problem) = error.downcast_ref::<selection::RegistryError>() else {
        return error;
    };
    anyhow::anyhow!(crate::i18n::t(
        language,
        "safe_paths.registry",
        &[
            ("path", &problem.path.to_string_lossy()),
            ("reason", &problem.reason)
        ],
    ))
}

#[cfg(feature = "server")]
pub(crate) fn execution_name(
    data: Option<&branvil::api::DataApi>,
    root: &Path,
) -> anyhow::Result<Option<String>> {
    let Some(data) = data else {
        return Ok(None);
    };
    Ok(data.workspace_for_execution(root)?.map(|ws| ws.name))
}

#[cfg(feature = "server")]
pub(crate) async fn register_execution(
    data: Option<&branvil::api::DataApi>,
    root: &Path,
    name: Option<String>,
) -> anyhow::Result<()> {
    if let Some(data) = data {
        let owner = data.owner();
        if let Some(existing) = data.workspace_for_execution(root)? {
            anyhow::ensure!(
                name.is_none_or(|name| name.eq_ignore_ascii_case(&existing.name)),
                "This directory is already registered as {}.",
                existing.name
            );
        } else {
            owner
                .assign_execution_root(name.unwrap_or_else(|| default_name(root)), root.into())
                .await?;
        }
    }
    Ok(())
}

#[cfg(feature = "server")]
pub(crate) const WORKSPACE_HEADER: &str = "x-guildbotics-workspace";

#[cfg(feature = "server")]
fn default_name(root: &Path) -> String {
    let name = root
        .file_name()
        .and_then(std::ffi::OsStr::to_str)
        .unwrap_or("");
    if branvil::agent::port::valid_repository_path(&format!("{name}/repo")) {
        return name.to_owned();
    }
    unscoped_name(root)
}

#[cfg(feature = "server")]
fn unscoped_name(root: &Path) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(root.as_os_str().as_encoded_bytes());
    format!("workspace-{}", loopback_auth::hex(&digest[..8]))
}

#[cfg(feature = "server")]
pub(crate) struct Migration {
    pub(crate) roots: std::collections::BTreeMap<String, PathBuf>,
    projects: Vec<(PathBuf, serde_json::Value)>,
    run_roots: Vec<PathBuf>,
}

#[cfg(feature = "server")]
impl Migration {
    /// Read only the selected data roots. For an archive, names are taken from the manifest
    /// and configuration is opened from the numbered copies.
    pub(crate) fn plan(files: &[PathBuf], locations: &[PathBuf]) -> anyhow::Result<Self> {
        use anyhow::Context;
        let mut roots = std::collections::BTreeMap::<String, PathBuf>::new();
        let mut projects = Vec::new();
        for (root, location) in files.iter().zip(locations).skip(1) {
            let file = root.join(".guildbotics/config/team/project.yml");
            let Some(bytes) = gb_base::files::read_if_present(&file)? else {
                continue;
            };
            let mut project = gb_config::yaml_value(std::str::from_utf8(&bytes)?)
                .with_context(|| format!("reading {}", file.display()))?;
            let Some(services) = project["services"].as_object_mut() else {
                roots.insert(unscoped_name(location), location.clone());
                continue;
            };
            let scope = |kind: &str| -> Option<&str> {
                let service = services.get(kind)?;
                (service["name"]
                    .as_str()
                    .is_some_and(|name| name.eq_ignore_ascii_case("branvil")))
                .then(|| service["workspace"].as_str())
                .flatten()
                .filter(|name| !name.is_empty())
            };
            let (tickets, code) = (scope("ticket_service"), scope("code_hosting_service"));
            if let (Some(tickets), Some(code)) = (tickets, code) {
                anyhow::ensure!(
                    tickets.eq_ignore_ascii_case(code),
                    "{}: tickets use workspace {tickets} and code hosting uses {code}. Set both services to the workspace that should own the execution settings. Runs will use only the repositories of that workspace.",
                    location.display()
                );
            }
            let Some(name) = tickets.or(code) else {
                roots.insert(unscoped_name(location), location.clone());
                continue;
            };
            let name = name.to_ascii_lowercase();
            anyhow::ensure!(
                branvil::agent::port::valid_repository_path(&format!("{name}/repo")),
                "{}: invalid workspace {name}",
                location.display()
            );
            if let Some(existing) = roots.insert(name.clone(), location.clone()) {
                anyhow::bail!(
                    "Workspace {name} is configured in both {} and {}. Leave the workspace selected only in the execution settings that should continue running for its repositories.",
                    existing.display(),
                    location.display()
                );
            }
            for kind in ["ticket_service", "code_hosting_service"] {
                if let Some(service) = services
                    .get_mut(kind)
                    .and_then(serde_json::Value::as_object_mut)
                {
                    service.remove("workspace");
                }
            }
            projects.push((root.clone(), project));
        }
        Ok(Self {
            roots,
            projects,
            run_roots: files.iter().skip(1).cloned().collect(),
        })
    }

    pub(crate) fn apply(self, identity: &Path) -> anyhow::Result<()> {
        for (root, project) in self.projects {
            let writes =
                gb_base::shared_writes::SharedWrites::new(identity.into(), Some(root.clone()));
            writes.transaction(|writer| {
                writer.write_json(&root.join(".guildbotics/config/team/project.yml"), &project)
            })?;
        }
        for root in self.run_roots {
            let directory = root.join(".guildbotics/state/task-runs");
            let entries = match std::fs::read_dir(&directory) {
                Ok(entries) => entries,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            let writes = gb_base::shared_writes::SharedWrites::new(identity.into(), Some(root));
            writes.transaction(|writer| {
                for entry in entries {
                    let entry = entry?;
                    if !entry.file_type()?.is_dir() {
                        continue;
                    }
                    let path = entry.path().join("result.json");
                    if let Some(bytes) = gb_base::files::read_if_present(&path)? {
                        let mut record: serde_json::Value = serde_json::from_slice(&bytes)?;
                        if migrate_run_urls(&mut record) {
                            writer.write_json(&path, &record)?;
                        }
                    }
                }
                Ok(())
            })?;
        }
        Ok(())
    }
}

#[cfg(feature = "server")]
/// A browser may view another workspace without changing what the Hub runs.
pub(crate) async fn for_request(
    api: &crate::api::Api,
    headers: &axum::http::HeaderMap,
) -> Result<Option<PathBuf>, axum::response::Response> {
    use axum::{http::StatusCode, response::IntoResponse};
    let refused = |status, code| crate::api::ApiError::new(status, code, headers).into_response();
    let Some(name) = headers.get(WORKSPACE_HEADER) else {
        return Ok(api.selected().workspace.clone());
    };
    if api.auth.is_consent(headers) {
        return Err(refused(StatusCode::FORBIDDEN, "command_workspace_changed"));
    }
    let name = name
        .to_str()
        .ok()
        .filter(|name| branvil::agent::port::valid_repository_path(&format!("{name}/repo")))
        .ok_or_else(|| refused(StatusCode::BAD_REQUEST, "validation_error"))?;
    let data = api
        .branvil
        .get()
        .ok_or_else(|| refused(StatusCode::SERVICE_UNAVAILABLE, "config_not_found"))?;
    data.owner()
        .repos_visible_workspace(name)
        .await
        .map(|workspace| workspace.execution_root)
        .map_err(|error| crate::config_api::failure(error.into(), headers))
}

#[cfg(feature = "server")]
fn migrate_run_urls(record: &mut serde_json::Value) -> bool {
    let mut changed = false;
    let mut rewrite = |value: Option<&mut serde_json::Value>| {
        if let Some(value) = value
            && let Some(url) = value.as_str().and_then(branvil::legacy_item_url)
        {
            *value = url.into();
            changed = true;
        }
    };
    for (parent, keys) in [
        ("work_identity", &["ticket_url", "pull_request_url"][..]),
        ("attributes", &["target.url", "target.pull_request_url"][..]),
        ("result", &["subject_id", "subject_url"][..]),
    ] {
        if let Some(parent) = record.get_mut(parent) {
            for key in keys {
                rewrite(parent.get_mut(key));
            }
        }
    }
    if let Some(evidence) = record
        .get_mut("provider_evidence")
        .and_then(serde_json::Value::as_array_mut)
    {
        for item in evidence {
            for path in ["/payload/target/url", "/payload/target/id"] {
                rewrite(item.pointer_mut(path));
            }
        }
    }
    changed
}

#[cfg(all(test, feature = "server"))]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn split_scope_requires_a_choice_before_any_settings_move() {
        let temp = tempfile::tempdir().unwrap();
        let identity = temp.path().join("identity");
        let root = temp.path().join("execution");
        let config = root.join(".guildbotics/config/team");
        std::fs::create_dir_all(&config).unwrap();
        let project = config.join("project.yml");
        let project_with = |tickets: &str, code: &str| {
            json!({"services":{
                "ticket_service": {"name":"branvil","workspace":tickets},
                "code_hosting_service": {"name":"branvil","workspace":code},
            }})
        };
        std::fs::write(&project, project_with("alpha", "beta").to_string()).unwrap();
        let roots = [identity.clone(), root.clone()];
        let refused = Migration::plan(&roots, &roots).err().unwrap().to_string();
        assert!(refused.contains("tickets use workspace alpha and code hosting uses beta"));
        assert!(std::fs::read_to_string(&project).unwrap().contains("beta"));

        std::fs::write(&project, project_with("beta", "beta").to_string()).unwrap();
        let migration = Migration::plan(&roots, &roots).unwrap();
        assert_eq!(
            migration.roots.into_iter().collect::<Vec<_>>(),
            [("beta".into(), root)]
        );
    }

    #[tokio::test]
    async fn a_request_for_another_workspace_leaves_the_active_workspace_in_place() {
        use crate::{
            api::{Api, Selected},
            auth::ServerAuth,
            input_files::InputFileStore,
        };
        use axum::http::HeaderMap;
        let root = tempfile::tempdir().unwrap();
        let host = root.path().join("code-host");
        branvil::test_support::initialize(&host, "owner").unwrap();
        let running = branvil::test_support::Running::start(
            &host,
            std::sync::Arc::new(branvil::vault::Memory::default()),
        )
        .await
        .unwrap();
        let active = root.path().join("active");
        let viewed = root.path().join("viewed");
        std::fs::create_dir_all(&active).unwrap();
        std::fs::create_dir_all(&viewed).unwrap();
        register_execution(Some(&running.data), &active, Some("active".into()))
            .await
            .unwrap();
        register_execution(Some(&running.data), &viewed, Some("viewed".into()))
            .await
            .unwrap();
        let api = Api::new(
            Selected {
                workspace: Some(active.clone()),
                ..Selected::default()
            },
            InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 8765).unwrap(),
            "fixture".into(),
            root.path().into(),
        );
        api.branvil.set(running.data.clone()).ok().unwrap();
        let mut headers = HeaderMap::new();
        headers.insert(WORKSPACE_HEADER, "viewed".parse().unwrap());
        assert_eq!(for_request(&api, &headers).await.unwrap(), Some(viewed));
        assert_eq!(api.selected().workspace, Some(active));
        running.stop().await.unwrap();
    }

    #[test]
    fn migration_updates_structured_ticket_and_pr_fields_only() {
        let old = "http://localhost:7878/acme/demo/-/tickets/1#comment";
        let new = "http://localhost:7878/workspaces/acme/repos/demo/tickets/1#comment";
        let mut run = json!({"work_identity":{"ticket_url":old},"result":{"subject_url":old},"safe_summary":old});
        assert!(migrate_run_urls(&mut run));
        assert_eq!(run["work_identity"]["ticket_url"], new);
        assert_eq!(run["result"]["subject_url"], new);
        assert_eq!(run["safe_summary"], old);
        assert!(!migrate_run_urls(&mut run));
    }
}
