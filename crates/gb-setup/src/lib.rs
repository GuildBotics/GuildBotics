//! Provider-independent setup. Provider models validate forms before any write;
//! network preparation/settlement belongs outside the shared-write transaction.
//!
//! This foundation is consumed by the Rust provider and server migrations;
//! the Python server remains in service until their cutover. No Python class
//! names are accepted by the brain mapping API. JSON output is also YAML and
//! quotes strings so a subsequent YAML 1.1 read preserves their types.
use anyhow::{Context, ensure};
use gb_config::{
    repository::{ConfigRepository, ConfigView, ConfigWriteReceipt, Revisions},
    team::valid_person_id,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs};

pub mod members;
mod templates;
pub use members::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceKind {
    ChatService,
    CodeHostingService,
    TicketService,
}

/// Implemented by the provider registry (#88). Unknown providers and kinds
/// must be rejected; a service result contains its canonical `name`.
pub trait ProviderModels {
    fn member_secret_names(&self) -> Vec<String>;
    fn service(&self, kind: ServiceKind, form: &Value) -> anyhow::Result<Value>;
    fn account(
        &self,
        provider: &str,
        form: &Value,
        person_id: &str,
        human: bool,
    ) -> anyhow::Result<StoredAccount>;
    fn channel(&self, services: &Value, reference: &str) -> anyhow::Result<Value>;
}

/// Deliberately not Debug/Serialize: secret values never belong in responses.
#[derive(Default)]
pub struct StoredAccount {
    pub account_info: BTreeMap<String, String>,
    pub secrets: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Brain {
    AiCli { cli_agent: String },
    Model { model: String },
    Jev { model: String },
}
pub type BrainMapping = BTreeMap<String, Brain>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectInput {
    pub language: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub services: BTreeMap<ServiceKind, Value>,
    #[serde(default)]
    pub llm_api_type: String,
    #[serde(default)]
    pub cli_agent: String,
}

pub struct Setup<'a> {
    pub repository: &'a ConfigRepository,
    pub providers: &'a dyn ProviderModels,
}

pub(crate) fn read(view: &ConfigView, path: &str) -> anyhow::Result<Value> {
    let value = view
        .read_config(path)?
        .map(|s| s.value())
        .transpose()?
        .unwrap_or(json!({}));
    ensure!(value.is_object(), "config must be a mapping: {path}");
    Ok(value)
}
pub(crate) fn member_path(id: &str, file: &str) -> anyhow::Result<String> {
    ensure!(valid_person_id(id), "invalid person_id");
    Ok(format!("team/members/{id}/{file}.yml"))
}

impl Setup<'_> {
    /// Workspace-level model API keys use environment names resolved by the
    /// model configuration, separately from provider account forms.
    pub fn save_model_keys(
        &self,
        keys: &BTreeMap<String, String>,
        device: gb_config::secrets::DeviceKey<'_>,
    ) -> anyhow::Result<()> {
        let store = self.repository.workspace_secrets(device)?;
        store.set_many(keys)
    }
    pub fn read_project(&self) -> anyhow::Result<Value> {
        // Use the same transaction for a consistent multi-file snapshot.
        self.repository
            .write(
                &Revisions::new(),
                |view, _| read(view, "team/project.yml"),
                |_| Ok(Revisions::new()),
            )
            .map(|r| r.result)
    }

    pub fn save_project(
        &self,
        input: &ProjectInput,
        expected: &Revisions,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        ensure!(
            ["en", "ja"].contains(&input.language.as_str()),
            "invalid language"
        );
        for choice in [&input.llm_api_type, &input.cli_agent] {
            ensure!(
                choice
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
                "invalid selection"
            );
        }
        let mut services = serde_json::Map::new();
        for (kind, form) in &input.services {
            let service = self.providers.service(*kind, form)?;
            ensure!(
                service.get("name").and_then(Value::as_str).is_some(),
                "provider model omitted name"
            );
            services.insert(
                serde_json::to_value(kind)?.as_str().unwrap().into(),
                service,
            );
        }
        let paths = [
            "team/project.yml",
            "intelligences/model_mapping.yml",
            "intelligences/cli_agent_mapping.yml",
        ];
        self.repository.write_checked(
            expected,
            &[],
            &paths,
            |view, writer| {
                let mut project = read(view, paths[0])?;
                project["language"] = json!(input.language);
                for (key, value) in [
                    ("description", json!(input.description)),
                    ("services", json!(services)),
                ] {
                    if value == "" || value == json!({}) {
                        project.as_object_mut().unwrap().remove(key);
                    } else {
                        project[key] = value;
                    }
                }
                let mut model = read(view, paths[1])?;
                if !input.llm_api_type.is_empty() {
                    model["default"] = model
                        .get(&input.llm_api_type)
                        .cloned()
                        .unwrap_or(json!(format!("models/{}/default.yml", input.llm_api_type)));
                }
                let mut cli = read(view, paths[2])?;
                if !input.cli_agent.is_empty() {
                    cli["default"] = json!(format!("cli_agents/{}/default.yml", input.cli_agent));
                }
                for (path, value) in paths.iter().zip([project, model, cli]) {
                    writer.write_json(&view.path(path)?, &value)?;
                }
                Ok(())
            },
            |view| view.revisions(&paths),
        )
    }

    pub fn read_brains(&self) -> anyhow::Result<BrainMapping> {
        self.repository
            .write(
                &Revisions::new(),
                |view, _| {
                    Ok(serde_json::from_value(read(
                        view,
                        "intelligences/brain_mapping.yml",
                    )?)?)
                },
                |_| Ok(Revisions::new()),
            )
            .map(|r| r.result)
    }
    pub fn save_brains(
        &self,
        mapping: &BrainMapping,
        expected: &Revisions,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        for brain in mapping.values() {
            let slot = match brain {
                Brain::AiCli { cli_agent } => cli_agent,
                Brain::Model { model } | Brain::Jev { model } => model,
            };
            ensure!(!slot.trim().is_empty(), "brain selection is required");
        }
        let path = "intelligences/brain_mapping.yml";
        self.repository.write_checked(
            expected,
            &[],
            &[path],
            |view, writer| writer.write_json(&view.path(path)?, mapping),
            |view| view.revisions(&[path]),
        )
    }

    pub fn set_default_person(
        &self,
        id: &str,
        expected: &Revisions,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        self.repository.write_checked(
            expected,
            &[],
            &["team/project.yml"],
            |view, writer| {
                if !id.is_empty() {
                    let member = read(view, &member_path(id, "member")?)?;
                    let identity = read(
                        &self.repository.identity_view(),
                        &member_path(id, "identity")?,
                    )?;
                    ensure!(
                        member["is_active"] == true
                            && identity["person_type"] != "human"
                            && identity["person_id"] == id,
                        "person is not executable"
                    );
                }
                let mut project = read(view, "team/project.yml")?;
                if id.is_empty() {
                    project.as_object_mut().unwrap().remove("default_person_id");
                } else {
                    project["default_person_id"] = json!(id);
                }
                writer.write_json(&view.path("team/project.yml")?, &project)
            },
            |view| view.revisions(&["team/project.yml"]),
        )
    }
}
