//! Provider-independent setup, as the Python version's
//! (`guildbotics/setup/setup_service.py`): a project's config files and a
//! member's identity and participation. What each provider stores is named
//! by [`ProviderModels`], every write goes through the [`ConfigRepository`],
//! and what a save needs of a provider itself is done outside its locks.
//!
//! Files are written as JSON, which is YAML too, without the keys whose value
//! is null or an empty string, as the Python version writes them.
use anyhow::Context as _;
use gb_config::{
    repository::{ConfigRepository, ConfigSnapshot, ConfigView, ConfigWriteReceipt, Revisions},
    secrets::DeviceKey,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
    fmt, fs,
    future::Future,
    path::{Path, PathBuf},
    pin::Pin,
};

pub mod members;
mod templates;
pub use members::*;

/// The files the project screen reads and saves together, so a save is
/// compared against all of them.
pub const PROJECT_CONFIG_PATHS: [&str; 3] = [
    "team/project.yml",
    "intelligences/model_mapping.yml",
    "intelligences/cli_agent_mapping.yml",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceKind {
    ChatService,
    CodeHostingService,
    TicketService,
}

/// What a setup refuses, as the screen reports it: a code it can name, and
/// a sentence named by a locale key, rendered in the language the screen
/// asks for, or one a provider wrote.
#[derive(Debug, Clone, PartialEq)]
pub struct SetupError {
    pub code: String,
    pub status: u16,
    pub sentence: Sentence,
}
#[derive(Debug, Clone, PartialEq)]
pub enum Sentence {
    Key(String, Vec<(String, String)>),
    Text(String),
}
impl SetupError {
    pub fn new(code: &str, key: &str) -> SetupError {
        SetupError {
            code: code.into(),
            status: 400,
            sentence: Sentence::Key(key.into(), Vec::new()),
        }
    }
    pub fn text(code: &str, text: impl Into<String>) -> SetupError {
        SetupError {
            code: code.into(),
            status: 400,
            sentence: Sentence::Text(text.into()),
        }
    }
    /// Gives the sentence's `name` parameter.
    pub fn with(mut self, name: &str, value: impl Into<String>) -> SetupError {
        if let Sentence::Key(_, params) = &mut self.sentence {
            params.push((name.into(), value.into()));
        }
        self
    }
    pub(crate) fn not_found() -> SetupError {
        SetupError::new("person_not_found", "setup.person_not_found")
    }
}
impl fmt::Display for SetupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.sentence {
            Sentence::Key(key, _) => write!(f, "{}: {key}", self.code),
            Sentence::Text(text) => write!(f, "{}: {text}", self.code),
        }
    }
}
impl std::error::Error for SetupError {}

/// A form does not have the shape its model requires: the request is
/// wrong, as a validation error says.
#[derive(Debug, Clone, PartialEq)]
pub struct InvalidInput(pub String);
impl fmt::Display for InvalidInput {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for InvalidInput {}

/// A secret store could not be used: its reason, which holds no value.
#[derive(Debug)]
pub struct SecretsUnavailable(pub String);
impl fmt::Display for SecretsUnavailable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for SecretsUnavailable {}

pub(crate) fn secrets<T>(result: anyhow::Result<T>) -> anyhow::Result<T> {
    result.map_err(|e| {
        if e.is::<gb_base::shared_writes::SharedWriteBusyError>() {
            e
        } else {
            SecretsUnavailable(format!("{e:#}")).into()
        }
    })
}

/// Something a provider does while a member is saved, deleted or given a
/// new picture.
pub type Hook<'a> = Pin<Box<dyn Future<Output = Result<(), SetupError>> + Send + 'a>>;

/// The providers' part of a setup, as the provider registry knows it.
pub trait ProviderModels: Send + Sync {
    /// Every secret a member may hold with any provider, by its
    /// member-relative name.
    fn member_secret_names(&self) -> Vec<String>;
    /// `project.services[kind]` as the provider `form` names stores it: its
    /// canonical `name` and its items. A provider that is unknown or does
    /// not serve `kind`, or a form its model refuses, is an [`InvalidInput`].
    fn service(&self, kind: ServiceKind, form: &Value) -> anyhow::Result<Value>;
    /// The providers with a member account, in their order.
    fn member_providers(&self) -> Vec<String>;
    /// The `account_info` keys the member's account with `provider` stores,
    /// which a save of that account replaces.
    fn account_info_keys(&self, provider: &str) -> Vec<String>;
    /// The member's account with `provider` as the form names it, or an
    /// [`InvalidInput`].
    fn account(&self, provider: &str, form: &Value) -> anyhow::Result<Box<dyn MemberAccount>>;
    /// The stored account with `provider` as the member screen shows it.
    fn view(
        &self,
        provider: &str,
        account_info: &BTreeMap<String, String>,
        has_secret: &dyn Fn(&str) -> bool,
    ) -> Value;
    /// Undo with `provider` what saving `member` obtained, before it is
    /// deleted; a refusal keeps the member.
    fn removed<'a>(&'a self, provider: &'a str, member: &'a SavedMember) -> Hook<'a>;
    /// Hand `provider` the member's picture, which has just changed; a
    /// refusal leaves the picture changed here.
    fn avatar_changed<'a>(&'a self, provider: &'a str, member: &'a SavedMember) -> Hook<'a>;
    /// How the chat of `services` names the channel the member screen names.
    fn channel(&self, services: &Value, reference: &str) -> anyhow::Result<Map<String, Value>>;
}

/// A member's account with a provider, as a save handles it: what the save
/// needs of the provider is obtained by `prepare` before any lock, stored by
/// `stored`, and `settled` once the save is over.
pub trait MemberAccount: Send {
    fn prepare<'a>(&'a mut self, member: &'a SavedMember) -> Hook<'a>;
    fn stored(&self, person_id: &str, human: bool) -> Result<StoredAccount, SetupError>;
    /// The save is over: `saved` with what `stored` gave, or refused before
    /// it wrote anything. It never fails the save.
    fn settled(self: Box<Self>, saved: bool) -> Pin<Box<dyn Future<Output = ()> + Send>>;
}

/// Deliberately not Debug/Serialize: secret values never belong in responses.
#[derive(Default)]
pub struct StoredAccount {
    pub account_info: BTreeMap<String, String>,
    pub secrets: BTreeMap<String, String>,
}

/// A member as its accounts see it while it is saved or deleted.
#[derive(Clone)]
pub struct SavedMember {
    pub person_id: String,
    pub name: String,
    pub human: bool,
    /// The `account_info` entries of all its accounts.
    pub account_info: BTreeMap<String, String>,
    /// Its secrets by member-relative name, as the save leaves them.
    pub secrets: BTreeMap<String, String>,
    /// Its picture as GuildBotics keeps it.
    pub avatar: Option<PathBuf>,
}
impl SavedMember {
    /// A secret's value by its member-relative name ("" for none).
    pub fn secret(&self, name: &str) -> &str {
        self.secrets.get(name).map(String::as_str).unwrap_or("")
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Brain {
    AiCli { cli_agent: String },
    Model { model: String },
    Jev { model: String },
}
pub type BrainMapping = BTreeMap<String, Brain>;

/// The project screen's form.
#[derive(Deserialize)]
pub struct ProjectSetupInput {
    pub language: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub llm_api_type: String,
    #[serde(default)]
    pub cli_agent: String,
    /// API keys by the LLM provider they are for; an empty one is ignored.
    #[serde(default)]
    pub provider_api_keys: BTreeMap<String, String>,
    #[serde(default)]
    pub services: BTreeMap<String, Value>,
}

/// The project as its screen shows it.
#[derive(Debug, Serialize)]
pub struct ProjectSnapshot {
    pub language: String,
    pub description: String,
    pub llm_api_type: String,
    pub cli_agent: String,
    pub services: Map<String, Value>,
    /// Whether the workspace's secret store holds each provider's key.
    pub provider_api_keys: BTreeMap<String, bool>,
    pub revisions: Revisions,
}

pub struct Setup<'a> {
    pub repository: &'a ConfigRepository,
    pub providers: &'a dyn ProviderModels,
    /// The shipped templates (`guildbotics/templates`).
    pub templates: &'a Path,
    pub device: DeviceKey<'a>,
}

/// A stored mapping, or nothing for an absent or invalid file: invalid
/// stored data is shown as an editable form, never refused.
pub(crate) fn mapping(view: &ConfigView, path: &str) -> anyhow::Result<Map<String, Value>> {
    Ok(match view.read_config(path)? {
        Some(snapshot) => match snapshot.value() {
            Ok(Value::Object(map)) => map,
            _ => Map::new(),
        },
        None => Map::new(),
    })
}

/// A project or mapping file as stored: absent is empty, and one that is not
/// a mapping is refused rather than written over.
pub(crate) fn strict(snapshot: Option<&ConfigSnapshot>) -> anyhow::Result<Map<String, Value>> {
    let Some(snapshot) = snapshot else {
        return Ok(Map::new());
    };
    match snapshot.value() {
        Ok(Value::Object(map)) => Ok(map),
        Ok(Value::Null) => Ok(Map::new()),
        Ok(_) => Err(gb_config::ConfigFile::wrap(
            &snapshot.path,
            anyhow::anyhow!("not a YAML mapping"),
        )),
        Err(error) => Err(gb_config::ConfigFile::wrap(&snapshot.path, error)),
    }
}

fn template_mapping(templates: &Path, path: &str) -> anyhow::Result<Map<String, Value>> {
    let text = fs::read_to_string(templates.join(path))
        .with_context(|| format!("cannot read the template {path}"))?;
    match gb_config::yaml_value(&text)? {
        Value::Object(map) => Ok(map),
        _ => anyhow::bail!("the template {path} is not a mapping"),
    }
}

/// Drops the keys whose value is null or an empty string, at every depth.
pub(crate) fn clean(value: Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.into_iter()
                .filter(|(_, v)| !v.is_null() && v != "")
                .map(|(k, v)| (k, clean(v)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.into_iter().map(clean).collect()),
        other => other,
    }
}

pub(crate) fn text(map: &Map<String, Value>, key: &str) -> String {
    match map.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

/// The provider of a model definition path `models/<provider>/<file>.yml`.
fn provider_of(path: &str) -> String {
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() >= 3 {
        parts[1].into()
    } else {
        String::new()
    }
}

/// The tool of a CLI agent definition path `cli_agents/<tool>/<slot>.yml`.
fn cli_agent_of(path: &str) -> String {
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() >= 3 && parts[0] == "cli_agents" {
        parts[1].into()
    } else {
        String::new()
    }
}

fn selection(choice: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        choice
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
        InvalidInput(format!("invalid selection: {choice}"))
    );
    Ok(())
}

/// A form's strings without the whitespace around them, which a pasted
/// value often carries and a stored one would keep.
fn trimmed(value: &Value) -> Value {
    match value {
        Value::String(s) => Value::String(s.trim().into()),
        Value::Object(map) => {
            Value::Object(map.iter().map(|(k, v)| (k.clone(), trimmed(v))).collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(trimmed).collect()),
        other => other.clone(),
    }
}

impl Setup<'_> {
    /// The environment variable holding each LLM provider's API key: the
    /// `api_key_env` of its `models/<provider>/default.yml`, the workspace's
    /// over the template's.
    pub fn provider_env_keys(&self) -> anyhow::Result<BTreeMap<String, String>> {
        self.repository
            .write(
                &Revisions::new(),
                |view, _| {
                    Ok(provider_env_keys(
                        self.templates,
                        &view.path("intelligences/models")?,
                    ))
                },
                |_| Ok(Revisions::new()),
            )
            .map(|r| r.result)
    }

    /// The project, its mappings and its API keys, with the revisions of the
    /// very bytes read.
    pub fn read_project(&self) -> anyhow::Result<ProjectSnapshot> {
        let view = self.repository.view();
        let snapshots = PROJECT_CONFIG_PATHS
            .iter()
            .map(|path| view.read_config(path))
            .collect::<anyhow::Result<Vec<_>>>()?;
        if snapshots[0].is_none() {
            return Err(
                SetupError::new("project_not_found", "app_api.errors.project_not_found").into(),
            );
        }
        let project = strict(snapshots[0].as_ref())?;
        let stored = |index: usize| match &snapshots[index] {
            Some(snapshot) => strict(Some(snapshot)),
            None => template_mapping(self.templates, PROJECT_CONFIG_PATHS[index]),
        };
        let (model, cli) = (stored(1)?, stored(2)?);
        let store = secrets(self.repository.workspace_secrets(self.device.clone()))?;
        let held = secrets(store.values())?;
        let mut provider_api_keys = BTreeMap::new();
        for (provider, env) in
            provider_env_keys(self.templates, &view.path("intelligences/models")?)
        {
            let present = held.get(&env).is_some_and(|v| !v.is_empty());
            provider_api_keys.insert(provider, present);
        }
        let services = match project.get("services") {
            Some(Value::Object(services)) => services
                .iter()
                .filter_map(|(kind, item)| {
                    let mut item = item.as_object()?.clone();
                    let name = text(&item, "name").to_lowercase();
                    item.insert("name".into(), name.into());
                    Some((kind.clone(), Value::Object(item)))
                })
                .collect(),
            _ => Map::new(),
        };
        let language = text(&project, "language");
        Ok(ProjectSnapshot {
            language: if language.is_empty() {
                "en".into()
            } else {
                language
            },
            description: text(&project, "description"),
            llm_api_type: provider_of(&text(&model, "default")),
            cli_agent: cli_agent_of(&text(&cli, "default")),
            services,
            provider_api_keys,
            revisions: PROJECT_CONFIG_PATHS
                .iter()
                .zip(&snapshots)
                .map(|(path, snapshot)| {
                    let revision = snapshot.as_ref().map(|s| s.blob_id.clone());
                    (path.to_string(), revision.unwrap_or_default())
                })
                .collect(),
        })
    }

    /// Creates the project: the CLI agents' definitions, the sample commands
    /// when the workspace has no command yet, the project and its mappings
    /// started from the templates, and the workspace's secret store with
    /// the keys the form gives.
    pub fn init_project(
        &self,
        input: &ProjectSetupInput,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        self.repository.write(
            &Revisions::new(),
            |view, writer| {
                let plan = self.project_files(view, input, false)?;
                self.install_cli_agents(view, writer)?;
                self.install_samples(view, writer, &input.language)?;
                plan.write(self, view, writer)
            },
            |view| view.revisions(&PROJECT_CONFIG_PATHS),
        )
    }

    /// Saves the project screen at the revisions it was read at, starting
    /// from the files already there; the store is touched only for a key.
    pub fn update_project(
        &self,
        input: &ProjectSetupInput,
        expected: &Revisions,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        self.repository.write(
            expected,
            |view, writer| {
                self.project_files(view, input, true)?
                    .write(self, view, writer)?;
                self.install_samples(view, writer, &input.language)
            },
            |view| view.revisions(&PROJECT_CONFIG_PATHS),
        )
    }

    /// The project form as its model reads it: the services it names as
    /// each provider stores them, or an [`InvalidInput`].
    pub fn check_project(&self, input: &ProjectSetupInput) -> anyhow::Result<Map<String, Value>> {
        if !["en", "ja"].contains(&input.language.trim()) {
            return Err(InvalidInput("language must be one of: en, ja".into()).into());
        }
        selection(input.llm_api_type.trim())?;
        selection(input.cli_agent.trim())?;
        let mut services = Map::new();
        for (kind, form) in &input.services {
            let kind_name: ServiceKind = serde_json::from_value(json!(kind))
                .map_err(|_| InvalidInput(format!("unknown service kind: {kind}")))?;
            services.insert(
                kind.clone(),
                self.providers.service(kind_name, &trimmed(form))?,
            );
        }
        Ok(services)
    }

    /// What saving the project form writes, checked before anything is.
    fn project_files(
        &self,
        view: &ConfigView,
        input: &ProjectSetupInput,
        update: bool,
    ) -> anyhow::Result<ProjectFiles> {
        let services = self.check_project(input)?;
        let (language, llm, cli) = (
            input.language.trim(),
            input.llm_api_type.trim(),
            input.cli_agent.trim(),
        );
        let env_keys = provider_env_keys(self.templates, &view.path("intelligences/models")?);
        let api_keys = input
            .provider_api_keys
            .iter()
            .filter_map(|(provider, value)| {
                let value = value.trim();
                let env = env_keys.get(provider)?;
                (!value.is_empty()).then(|| (env.clone(), value.to_owned()))
            })
            .collect();
        let mut project = if update {
            strict(view.read_config(PROJECT_CONFIG_PATHS[0])?.as_ref())?
        } else {
            Map::new()
        };
        project.insert("language".into(), language.into());
        for (key, value) in [
            ("description", json!(input.description.trim())),
            ("services", Value::Object(services)),
        ] {
            if value == "" || value == json!({}) {
                project.remove(key);
            } else {
                project.insert(key.into(), value);
            }
        }
        let mut files = vec![(PROJECT_CONFIG_PATHS[0], project)];
        for (path, choice) in [
            (PROJECT_CONFIG_PATHS[1], llm),
            (PROJECT_CONFIG_PATHS[2], cli),
        ] {
            let mut map = match view.read_config(path)? {
                Some(stored) if update => strict(Some(&stored))?,
                _ => template_mapping(self.templates, path)?,
            };
            if !choice.is_empty() {
                let default = if path == PROJECT_CONFIG_PATHS[1] {
                    match map.get(choice) {
                        Some(Value::String(slot)) if !slot.is_empty() => slot.clone(),
                        _ => format!("models/{choice}/default.yml"),
                    }
                } else {
                    format!("cli_agents/{choice}/default.yml")
                };
                map.insert("default".into(), default.into());
            }
            files.push((path, map));
        }
        Ok(ProjectFiles {
            files,
            api_keys,
            open_store: !update,
        })
    }

    /// Stores the member a command runs as when it names none; an empty ID
    /// clears it. The member must be in the workspace and not a human.
    pub fn set_default_person(&self, id: &str) -> anyhow::Result<ConfigWriteReceipt<()>> {
        if !id.is_empty() && !gb_config::team::valid_person_id(id) {
            return Err(
                SetupError::new("invalid_person_id", "member_config.id_requirement").into(),
            );
        }
        self.repository.write(
            &Revisions::new(),
            |view, writer| {
                if !id.is_empty() {
                    if view.read_config(&member_path(id, "member"))?.is_none() {
                        return Err(SetupError::not_found().into());
                    }
                    let identity = mapping(
                        &self.repository.identity_view(),
                        &member_path(id, "identity"),
                    )?;
                    if text(&identity, "person_type") == "human" {
                        return Err(SetupError::new(
                            "person_not_executable",
                            "setup.person_not_executable",
                        )
                        .into());
                    }
                }
                let mut project = strict(view.read_config(PROJECT_CONFIG_PATHS[0])?.as_ref())?;
                if text(&project, "default_person_id") == id {
                    return Ok(());
                }
                if id.is_empty() {
                    project.remove("default_person_id");
                } else {
                    project.insert("default_person_id".into(), id.into());
                }
                writer.write_json(
                    &view.path(PROJECT_CONFIG_PATHS[0])?,
                    &clean(Value::Object(project)),
                )
            },
            |view| view.revisions(&PROJECT_CONFIG_PATHS),
        )
    }

    pub fn read_brains(&self) -> anyhow::Result<BrainMapping> {
        self.repository
            .write(
                &Revisions::new(),
                |view, _| {
                    Ok(serde_json::from_value(Value::Object(mapping(
                        view,
                        "intelligences/brain_mapping.yml",
                    )?))?)
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
            anyhow::ensure!(!slot.trim().is_empty(), "brain selection is required");
        }
        let path = "intelligences/brain_mapping.yml";
        self.repository.write(
            expected,
            |view, writer| writer.write_json(&view.path(path)?, mapping),
            |view| view.revisions(&[path]),
        )
    }
}

/// The project's files and keys a save writes.
struct ProjectFiles {
    files: Vec<(&'static str, Map<String, Value>)>,
    api_keys: BTreeMap<String, String>,
    /// A new project opens its secret store even without a key.
    open_store: bool,
}
impl ProjectFiles {
    fn write(
        self,
        setup: &Setup<'_>,
        view: &ConfigView,
        writer: &mut gb_base::shared_writes::Writer<'_>,
    ) -> anyhow::Result<()> {
        // The keys first: a store that cannot be used leaves no project
        // claiming them.
        if self.open_store || !self.api_keys.is_empty() {
            let store = secrets(setup.repository.workspace_secrets(setup.device.clone()))?;
            let keys = self.api_keys;
            secrets(store.update_with(writer, |current| current.extend(keys)))?;
        }
        for (path, map) in self.files {
            writer.write_json(&view.path(path)?, &clean(Value::Object(map)))?;
        }
        Ok(())
    }
}

/// The environment variable holding each LLM provider's API key: the
/// `api_key_env` of its `<provider>/default.yml` in the workspace's `models`,
/// over the template's. Read without a lock: a reader, not a writer.
pub fn provider_env_keys(templates: &Path, models: &Path) -> BTreeMap<String, String> {
    let mut keys = BTreeMap::new();
    for root in [templates.join("intelligences/models"), models.to_owned()] {
        let Ok(entries) = fs::read_dir(&root) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(content) = fs::read_to_string(entry.path().join("default.yml")) else {
                continue;
            };
            // A definition that holds nothing leaves the template's.
            let Ok(Value::Object(map)) = gb_config::yaml_value(&content) else {
                continue;
            };
            if map.is_empty() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().into_owned();
            let env = text(&map, "api_key_env");
            keys.remove(&name);
            if !env.is_empty() {
                keys.insert(name, env);
            }
        }
    }
    keys
}
