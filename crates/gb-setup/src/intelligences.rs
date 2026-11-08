//! Intelligence editor inheritance and directory reconciliation.
use crate::{InvalidInput, SetupError};
use gb_base::shared_writes::Writer;
use gb_config::repository::{ConfigRepository, ConfigView, ConfigWriteReceipt, Revisions};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};

const AGNO: &str = "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain";
const CLI: &str = "guildbotics.guest.cli_agent.CliAgentBrain";
const JEV: &str = "guildbotics.intelligences.brains.jev.JevBrain";

#[derive(Default, Deserialize)]
pub struct Update {
    #[serde(default, deserialize_with = "person_id")]
    pub person_id: Option<String>,
    #[serde(default)]
    pub inherit_team_defaults: bool,
    pub model_mapping: Option<BTreeMap<String, String>>,
    pub cli_agent_mapping: Option<BTreeMap<String, String>>,
    #[serde(default, deserialize_with = "model_definitions")]
    pub models: Option<Vec<Definition>>,
    #[serde(default, deserialize_with = "cli_definitions")]
    pub cli_agents: Option<Vec<Definition>>,
    pub brain_mapping: Option<Vec<Brain>>,
    #[serde(skip)]
    pub filesystem_grants: Option<Value>,
    #[serde(skip)]
    pub local_grants: Option<Value>,
    #[serde(skip)]
    pub agent_environment: Option<Value>,
}
pub fn person_id<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<String>, D::Error> {
    let value = Option::<String>::deserialize(deserializer)?;
    member(value.as_deref()).map_err(serde::de::Error::custom)?;
    Ok(value)
}

fn definitions_input<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
    required: &str,
) -> Result<Option<Vec<Definition>>, D::Error> {
    let values = Option::<Vec<Value>>::deserialize(deserializer)?;
    values
        .map(|values| {
            values
                .into_iter()
                .map(|mut v| {
                    if !v.get(required).is_some_and(Value::is_string) {
                        return Err(serde::de::Error::custom(format!(
                            "{required} is required and must be text"
                        )));
                    }
                    let where_ = if required == "provider" {
                        "model definition"
                    } else {
                        "AI CLI tool definition"
                    };
                    v["effort"] =
                        normalize_overlay(v.get("effort").unwrap_or(&Value::Null), where_)
                            .map_err(serde::de::Error::custom)?;
                    serde_json::from_value(v).map_err(serde::de::Error::custom)
                })
                .collect()
        })
        .transpose()
}
fn model_definitions<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Vec<Definition>>, D::Error> {
    definitions_input(deserializer, "provider")
}
fn cli_definitions<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Vec<Definition>>, D::Error> {
    definitions_input(deserializer, "name")
}

#[derive(Deserialize)]
pub struct Definition {
    pub path: String,
    #[serde(default)]
    pub model_class: String,
    #[serde(default)]
    pub parameters: Map<String, Value>,
    #[serde(default = "object")]
    pub effort: Value,
}
#[derive(Deserialize)]
pub struct Brain {
    pub name: String,
    pub brain_class: String,
    #[serde(deserialize_with = "brain_engine")]
    pub engine: String,
    pub target: String,
}
fn brain_engine<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<String, D::Error> {
    let engine = String::deserialize(deserializer)?;
    if !["llm", "cli", "jev"].contains(&engine.as_str()) {
        return Err(serde::de::Error::custom("invalid brain engine"));
    }
    Ok(engine)
}

fn object() -> Value {
    json!({})
}
pub fn scope(person: Option<&str>) -> String {
    person.map_or_else(
        || "intelligences".into(),
        |p| format!("team/members/{p}/intelligences"),
    )
}
fn member(person: Option<&str>) -> anyhow::Result<()> {
    if let Some(p) = person
        && !gb_config::team::valid_person_id(p)
    {
        return Err(InvalidInput("invalid person_id".into()).into());
    }
    Ok(())
}
fn read_yaml(path: &Path) -> anyhow::Result<Value> {
    match std::fs::read_to_string(path) {
        Ok(s) => Ok(gb_config::yaml_value(&s)?
            .as_object()
            .cloned()
            .map(Value::Object)
            .unwrap_or_else(object)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(object()),
        Err(e) => Err(e.into()),
    }
}
fn nonempty(v: &Value) -> bool {
    !v.is_null() && v != &json!({}) && v != &json!([]) && v != ""
}

pub struct IntelligenceConfig<'a> {
    pub repository: &'a ConfigRepository,
    pub templates: &'a Path,
}
impl IntelligenceConfig<'_> {
    fn scoped(&self, view: &ConfigView, person: Option<&str>, path: &str) -> anyhow::Result<Value> {
        for prefix in person
            .map(|p| scope(Some(p)))
            .into_iter()
            .chain(std::iter::once(scope(None)))
        {
            let data = view
                .read_config(&format!("{prefix}/{path}"))?
                .map(|s| s.value())
                .transpose()?
                .unwrap_or_else(object);
            if data.as_object().is_some_and(|m| !m.is_empty()) {
                return Ok(data);
            }
        }
        read_yaml(&self.templates.join("intelligences").join(path))
    }
    fn mapping(
        &self,
        view: &ConfigView,
        person: Option<&str>,
        name: &str,
    ) -> anyhow::Result<Value> {
        let mut team = self.scoped(view, None, name)?;
        if let Some(person) = person {
            let own = view
                .read_config(&format!("{}/{name}", scope(Some(person))))?
                .map(|s| s.value())
                .transpose()?
                .unwrap_or_else(object);
            if let Some(m) = own.as_object() {
                team.as_object_mut().unwrap().extend(m.clone());
            }
        }
        if name != "brain_mapping.yml" {
            for value in team.as_object_mut().unwrap().values_mut() {
                *value = json!(python_string(value));
            }
        }
        if name == "cli_agent_mapping.yml" {
            validate_cli(&team)?;
        }
        Ok(team)
    }
    fn descriptors(
        &self,
        view: &ConfigView,
        person: Option<&str>,
        path: &str,
        cli: bool,
    ) -> anyhow::Result<Value> {
        let provider = path.split('/').nth(1).unwrap_or_default();
        let default = format!(
            "{}/{provider}/default.yml",
            if cli { "cli_agents" } else { "models" }
        );
        let mut candidates = Vec::new();
        if cli {
            candidates.push(self.scoped(view, person, path)?);
        }
        candidates.push(self.scoped(view, person, &default)?);
        candidates.push(read_yaml(
            &self.templates.join("intelligences").join(default),
        )?);
        for data in candidates {
            if data.get("effort_fields").is_some_and(nonempty) {
                return fields(&data["effort_fields"]);
            }
        }
        Ok(json!([]))
    }
    fn definitions(
        &self,
        view: &ConfigView,
        person: Option<&str>,
        mapping: &Value,
        cli: bool,
    ) -> anyhow::Result<Vec<Value>> {
        let paths = mapping
            .as_object()
            .unwrap()
            .values()
            .filter_map(Value::as_str);
        let mut seen = BTreeSet::new();
        let mut result = Vec::new();
        for path in paths {
            if !seen.insert(path) {
                continue;
            }
            if !cli && !path.starts_with("models/") {
                continue;
            }
            let data = self.scoped(view, person, path)?;
            let provider = path.split('/').nth(1).unwrap_or_default();
            let default = format!(
                "{}/{provider}/default.yml",
                if cli { "cli_agents" } else { "models" }
            );
            let own_default = self.scoped(view, person, &default)?;
            let packaged = read_yaml(&self.templates.join("intelligences").join(default))?;
            let inherited = own_default
                .get("effort")
                .filter(|v| nonempty(v))
                .or_else(|| packaged.get("effort"))
                .cloned()
                .unwrap_or_else(object);
            let mut row = json!({"path":path,"parameters":data.get("parameters").filter(|v|v.is_object()).cloned().unwrap_or_else(object),"effort":normalize_overlay(data.get("effort").unwrap_or(&Value::Null),if cli {"AI CLI tool definition"} else {"model definition"})?,"inherited_effort":inherited,"effort_fields":self.descriptors(view,person,path,cli)?});
            if cli {
                row["name"] = json!(provider);
                row["effort_supported"] = json!(python_truth(
                    &data
                        .get("effort_supported")
                        .or_else(|| own_default.get("effort_supported"))
                        .cloned()
                        .unwrap_or(json!(true))
                ));
            } else {
                row["provider"] = json!(provider);
                row["model_class"] =
                    json!(python_string(data.get("model_class").unwrap_or(&json!(""))));
            }
            result.push(row);
        }
        Ok(result)
    }
    pub fn read(&self, person: Option<&str>) -> anyhow::Result<Value> {
        member(person)?;
        let scope = scope(person);
        let revisions = self.repository.tree_revisions(&scope)?;
        self.read_view(self.repository.view(), person, revisions)
    }
    fn read_view(
        &self,
        view: &ConfigView,
        person: Option<&str>,
        revisions: Revisions,
    ) -> anyhow::Result<Value> {
        let scope = scope(person);
        let models = self.mapping(view, person, "model_mapping.yml")?;
        let cli = self.mapping(view, person, "cli_agent_mapping.yml")?;
        let brains = self.mapping(view, person, "brain_mapping.yml")?;
        let assignments: Vec<_> = brains
            .as_object()
            .unwrap()
            .iter()
            .filter(|(_, v)| v.is_object())
            .map(|(name, data)| {
                let engine = match data["kind"].as_str() {
                    Some("ai_cli") => "cli",
                    Some("jev") => "jev",
                    Some("model") => "llm",
                    _ => anyhow::bail!("Unsupported brain kind for {name}"),
                };
                let class = match engine {
                    "cli" => CLI,
                    "jev" => JEV,
                    _ => AGNO,
                };
                let target = python_string(
                    data.get(if engine == "cli" {
                        "cli_agent"
                    } else {
                        "model"
                    })
                    .unwrap_or(&json!("default")),
                );
                Ok(json!({"name":name,"brain_class":class,"engine":engine,"target":target}))
            })
            .collect::<anyhow::Result<_>>()?;
        let inherited = |name| -> anyhow::Result<Value> {
            Ok(if person.is_some() {
                json!(
                    self.mapping(view, None, name)?
                        .as_object()
                        .unwrap()
                        .keys()
                        .collect::<Vec<_>>()
                )
            } else {
                json!([])
            })
        };
        Ok(
            json!({"config_dir":view.path("intelligences")?.parent(),"revisions":revisions,"person_id":person,"inherited":person.is_some() && !view.path(&scope)?.exists(),"models":self.definitions(view,person,&models,false)?,"model_mapping":models,"cli_agents":self.definitions(view,person,&cli,true)?,"cli_agent_mapping":cli,"brain_mapping":assignments,"inherited_model_slots":inherited("model_mapping.yml")?,"inherited_cli_slots":inherited("cli_agent_mapping.yml")?,"inherited_brain_features":inherited("brain_mapping.yml")?}),
        )
    }
    pub fn update(
        &self,
        request: &Update,
        expected: &Revisions,
    ) -> anyhow::Result<ConfigWriteReceipt<Vec<Value>>> {
        member(request.person_id.as_deref())?;
        let scope = scope(request.person_id.as_deref());
        self.repository.write(
            expected,
            |view, writer| self.apply(view, writer, request),
            |view| view.tree_revisions(&scope),
        )
    }
    pub fn update_validated(
        &self,
        request: &mut Update,
        expected: &Revisions,
        check: impl FnOnce(&mut Update) -> anyhow::Result<()>,
    ) -> anyhow::Result<ConfigWriteReceipt<Vec<Value>>> {
        member(request.person_id.as_deref())?;
        let scope = scope(request.person_id.as_deref());
        self.repository.write(
            expected,
            |view, writer| {
                check(request)?;
                self.apply(view, writer, request)
            },
            |view| view.tree_revisions(&scope),
        )
    }
    fn apply(
        &self,
        view: &ConfigView,
        writer: &mut Writer<'_>,
        r: &Update,
    ) -> anyhow::Result<Vec<Value>> {
        let person = r.person_id.as_deref();
        let prefix = scope(person);
        let mut files = Vec::new();
        let mut mappings = Vec::new();
        if let Some(m) = &r.model_mapping {
            mappings.push(("model_mapping.yml", json!(m)));
        }
        if let Some(m) = &r.cli_agent_mapping {
            mappings.push(("cli_agent_mapping.yml", json!(m)));
        }
        if let Some(brains) = &r.brain_mapping {
            let mut mapping = Map::new();
            for b in brains {
                let reason = match (b.engine.as_str(), b.name.as_str(), b.target.as_str()) {
                    ("cli", "chat_decision", _) => {
                        Some("AI CLI is not supported for chat_decision")
                    }
                    ("jev", name, _) if name != "chat_decision" => {
                        Some("Jev is only supported for chat_decision")
                    }
                    ("jev", _, model) if model != "jev-latest" => Some("Unsupported Jev model"),
                    ("llm" | "cli" | "jev", _, _) => None,
                    _ => return Err(InvalidInput("invalid brain engine".into()).into()),
                };
                if let Some(reason) = reason {
                    return Err(SetupError::text("invalid_decision", reason).into());
                }
                let kind = match b.engine.as_str() {
                    "cli" => "ai_cli",
                    "jev" => "jev",
                    _ => "model",
                };
                mapping.insert(
                    b.name.clone(),
                    json!({"kind":kind,if b.engine == "cli" {"cli_agent"} else {"model"}:b.target}),
                );
            }
            mappings.push(("brain_mapping.yml", Value::Object(mapping)));
        }
        if person.is_some() && r.inherit_team_defaults {
            let path = view.path(&prefix)?;
            if path.exists() {
                writer.remove_tree(&path)?;
            }
            return Ok(vec![json!({"path":path,"action":"delete"})]);
        }
        if person.is_some() {
            // Load the complete team scope before a reconciliation can write anything.
            self.read_view(view, None, view.tree_revisions("intelligences")?)?;
        }
        if let Some(mapping) = &r.cli_agent_mapping {
            validate_cli(&json!(mapping))?;
        }
        for (defs, cli) in [(&r.models, false), (&r.cli_agents, true)] {
            if let Some(defs) = defs {
                for def in defs {
                    if !cli && !def.path.starts_with("models/") {
                        continue;
                    }
                    view.path(&format!("{prefix}/{}", def.path))?;
                    if person.is_some() {
                        continue;
                    }
                    let descriptors = self.descriptors(view, person, &def.path, cli)?;
                    normalize_overlay(
                        &def.effort,
                        if cli {
                            "AI CLI tool definition"
                        } else {
                            "model definition"
                        },
                    )?;
                    let where_ = if cli {
                        format!("AI CLI tool '{}'", def.path)
                    } else {
                        format!(
                            "provider '{}'",
                            def.path.split('/').nth(1).unwrap_or_default()
                        )
                    };
                    check_settings(&def.effort, &descriptors, &where_)?;
                    check_settings(&json!({"parameters":def.parameters}), &descriptors, &where_)?;
                }
            }
        }
        for (name, mut data) in mappings {
            if person.is_some() {
                let team = self.mapping(view, None, name)?;
                data.as_object_mut()
                    .unwrap()
                    .retain(|k, v| team.get(k) != Some(v));
            }
            write(
                view,
                writer,
                &format!("{prefix}/{name}"),
                &data,
                person.is_some(),
                &mut files,
            )?;
        }
        for (defs, cli) in [(&r.models, false), (&r.cli_agents, true)] {
            let Some(defs) = defs else {
                continue;
            };
            let mut kept = BTreeSet::new();
            for def in defs {
                if !cli && !def.path.starts_with("models/") {
                    continue;
                }
                let descriptors = if cli {
                    json!([])
                } else {
                    self.descriptors(view, person, &def.path, false)?
                };
                let base = self.scoped(view, person, &def.path)?;
                let data = if cli {
                    json!({"parameters":def.parameters,"effort":def.effort})
                } else {
                    let mut data = base.as_object().cloned().unwrap_or_default();
                    let described: BTreeSet<_> = descriptors
                        .as_array()
                        .unwrap()
                        .iter()
                        .filter_map(|f| f["key"].as_str())
                        .map(|k| k.split('.').next().unwrap())
                        .collect();
                    let mut parameters =
                        base["parameters"].as_object().cloned().unwrap_or_default();
                    parameters.retain(|k, _| !described.contains(k.as_str()));
                    parameters.extend(def.parameters.clone());
                    data.insert("model_class".into(), json!(def.model_class));
                    data.insert("parameters".into(), json!(parameters));
                    data.insert("effort".into(), def.effort.clone());
                    Value::Object(data)
                };
                let name = format!("{prefix}/{}", def.path);
                if person.is_some()
                    && comparable(&data) == comparable(&self.scoped(view, None, &def.path)?)
                {
                    continue;
                }
                write(view, writer, &name, &data, false, &mut files)?;
                kept.insert(view.path(&name)?);
            }
            if person.is_some() {
                prune(
                    &view.path(&format!(
                        "{prefix}/{}",
                        if cli { "cli_agents" } else { "models" }
                    ))?,
                    writer,
                    &kept,
                    &mut files,
                )?;
            }
        }
        let root = view.path(&prefix)?;
        if person.is_none() {
            if let Some(shared) = &r.filesystem_grants {
                write(
                    view,
                    writer,
                    "intelligences/cli_agent_filesystem_grants.yml",
                    shared,
                    false,
                    &mut files,
                )?;
            }
            if let Some(declaration) = &r.agent_environment {
                write(
                    view,
                    writer,
                    "intelligences/agent_environment.yml",
                    declaration,
                    false,
                    &mut files,
                )?;
            }
            if let Some(local) = &r.local_grants {
                let path = root
                    .parent()
                    .unwrap()
                    .parent()
                    .unwrap()
                    .join("local/cli_agent_filesystem_grants.yml");
                writer.write_json(&path, local)?;
                files.push(json!({"path":path,"action":"update"}));
            }
        }
        if person.is_some() && root.is_dir() && std::fs::read_dir(&root)?.next().is_none() {
            std::fs::remove_dir(root)?;
        }
        Ok(files)
    }
}
/// Providers are discovered from their own defaults, in scope order.
pub fn providers(roots: &[PathBuf]) -> anyhow::Result<Vec<Value>> {
    let mut names = BTreeSet::new();
    for root in roots {
        if !root.is_dir() {
            continue;
        }
        for entry in std::fs::read_dir(root)? {
            let entry = entry?;
            if entry.path().join("default.yml").is_file() {
                names.insert(entry.file_name().to_string_lossy().into_owned());
            }
        }
    }
    let mut result = Vec::new();
    for name in names {
        let mut data = object();
        for root in roots {
            let path = root.join(&name).join("default.yml");
            data = match std::fs::read_to_string(&path) {
                Ok(text) => gb_config::yaml_value(&text)
                    .ok()
                    .filter(Value::is_object)
                    .unwrap_or_else(object),
                Err(_) => object(),
            };
            if nonempty(&data) {
                break;
            }
        }
        let effort = data.get("effort").cloned().unwrap_or_else(object);
        let effort = normalize_overlay(&effort, &format!("provider '{name}'"))?;
        let text = |key| python_string(data.get(key).unwrap_or(&json!("")));
        let label = data
            .get("label")
            .filter(|v| python_truth(v))
            .map(python_string)
            .unwrap_or_else(|| name.clone());
        let order = match data.get("order") {
            Some(Value::String(s)) => s.trim().parse::<i64>().ok(),
            Some(Value::Bool(b)) => Some(i64::from(*b)),
            Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|n| n as i64)),
            _ => None,
        }
        .unwrap_or(1000);
        result.push(json!({"provider":name,"label":label,"order":order,"api_key_env":text("api_key_env"),"model_class":text("model_class"),"model_id":python_string(data["parameters"].get("id").unwrap_or(&json!(""))),"effort":effort,"effort_fields":fields(data.get("effort_fields").unwrap_or(&json!([])))?}));
    }
    result.sort_by_key(|p| {
        (
            p["order"].as_i64().unwrap(),
            p["provider"].as_str().unwrap().to_owned(),
        )
    });
    Ok(result)
}
fn python_truth(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}
fn python_string(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        _ => value.to_string(),
    }
}

fn write(
    view: &ConfigView,
    writer: &mut Writer<'_>,
    name: &str,
    value: &Value,
    remove_empty: bool,
    files: &mut Vec<Value>,
) -> anyhow::Result<()> {
    let path = view.path(name)?;
    if remove_empty && !nonempty(value) {
        if path.exists() {
            writer.delete(&path)?;
            files.push(json!({"path":path,"action":"delete"}));
        }
    } else {
        writer.write_json(&path, &crate::clean(value.clone()))?;
        files.push(json!({"path":path,"action":"update"}));
    }
    Ok(())
}
fn comparable(v: &Value) -> Value {
    match v {
        Value::Object(m) => Value::Object(
            m.iter()
                .filter(|(k, _)| k.as_str() != "effort_fields")
                .map(|(k, v)| (k.clone(), comparable(v)))
                .filter(|(_, v)| nonempty(v))
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.iter().map(comparable).collect()),
        _ => v.clone(),
    }
}
fn prune(
    dir: &Path,
    writer: &mut Writer<'_>,
    kept: &BTreeSet<PathBuf>,
    files: &mut Vec<Value>,
) -> anyhow::Result<()> {
    if !dir.is_dir() {
        return Ok(());
    }
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            prune(&entry.path(), writer, kept, files)?;
        } else if entry.path().extension().is_some_and(|e| e == "yml")
            && !kept.contains(&entry.path())
        {
            writer.delete(&entry.path())?;
            files.push(json!({"path":entry.path(),"action":"delete"}));
        }
    }
    if std::fs::read_dir(dir)?.next().is_none() {
        std::fs::remove_dir(dir)?;
    }
    Ok(())
}
fn validate_cli(mapping: &Value) -> anyhow::Result<()> {
    for (slot, path) in mapping
        .as_object()
        .ok_or_else(|| InvalidInput("invalid CLI mapping".into()))?
    {
        let path = path.as_str().unwrap_or_default();
        let parts: Vec<_> = path.split('/').collect();
        if parts.len() < 3
            || parts[0] != "cli_agents"
            || !["codex", "claude", "grok", "copilot", "antigravity"].contains(&parts[1])
        {
            return Err(SetupError::text(
                "unknown_cli_agent",
                format!("AI CLI tool slot '{slot}': '{path}' does not name a supported AI CLI tool (supported: codex, claude, grok, copilot, antigravity)"),
            )
            .into());
        }
    }
    Ok(())
}
fn fields(value: &Value) -> anyhow::Result<Value> {
    if value.is_null() || value == "" {
        return Ok(json!([]));
    }
    let list = value
        .as_array()
        .ok_or_else(|| InvalidInput("effort_fields must be a list of descriptors".into()))?;
    let mut result = Vec::new();
    for field in list {
        let key = field["key"].as_str().unwrap_or_default().trim();
        let kind = field["type"].as_str().unwrap_or_default().trim();
        if key.is_empty() || !["enum", "integer", "boolean", "string", "model_id"].contains(&kind) {
            return Err(InvalidInput("invalid effort field descriptor".into()).into());
        }
        let values = field
            .get("values")
            .filter(|v| !v.is_null())
            .cloned()
            .unwrap_or(json!([]));
        let values = values
            .as_array()
            .ok_or_else(|| InvalidInput("effort field values must be a list".into()))?;
        if kind == "enum" && values.is_empty() {
            return Err(InvalidInput("enum effort field needs values".into()).into());
        }
        let bound = |key| -> anyhow::Result<Option<i64>> {
            let v = &field[key];
            if v.is_null() || v == "" {
                return Ok(None);
            }
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                .map(Some)
                .ok_or_else(|| InvalidInput(format!("'{v}' is not a whole number.")).into())
        };
        result.push(json!({"key":key,"type":kind,"values":values.iter().map(|v|v.as_str().map(str::to_owned).unwrap_or_else(||v.to_string())).collect::<Vec<_>>(),"minimum":bound("minimum")?,"maximum":bound("maximum")?}));
    }
    Ok(json!(result))
}
fn normalize_overlay(value: &Value, where_: &str) -> anyhow::Result<Value> {
    if value.is_null() || value == "" {
        return Ok(object());
    }
    let mapping = value.as_object().ok_or_else(|| {
        InvalidInput(format!(
            "{where_}: 'effort' must be a mapping of level to settings."
        ))
    })?;
    let mut result = Map::new();
    for (level, settings) in mapping {
        let normalized = level.trim().to_lowercase();
        let reason = if normalized == "default" {
            format!(
                "{where_}: 'effort.default' can never be applied, because default means 'do not intervene'. Put settings that should always apply in 'parameters' instead."
            )
        } else if normalized.is_empty() {
            format!("{where_}: 'effort' keys must be one of: low, high.")
        } else if !["low", "high"].contains(&normalized.as_str()) {
            format!("Unknown effort '{level}'. Expected one of: low, default, high.")
        } else if !settings.is_object() {
            format!("{where_}: 'effort.{normalized}' must be a mapping of settings.")
        } else {
            String::new()
        };
        if !reason.is_empty() {
            return Err(InvalidInput(reason).into());
        }
        result.insert(normalized, settings.clone());
    }
    Ok(Value::Object(result))
}

fn check_settings(settings: &Value, fields: &Value, where_: &str) -> anyhow::Result<()> {
    fn flatten(value: &Value, prefix: String, pairs: &mut Vec<(String, Value)>) {
        if let Some(m) = value.as_object() {
            for (k, v) in m {
                flatten(
                    v,
                    if prefix.is_empty() {
                        k.clone()
                    } else {
                        format!("{prefix}.{k}")
                    },
                    pairs,
                );
            }
        } else {
            pairs.push((prefix, value.clone()));
        }
    }
    let fields = fields.as_array().unwrap();
    if fields.is_empty() {
        return Ok(());
    }
    let mut problems = Vec::new();
    for (level, settings) in settings.as_object().unwrap() {
        let mut pairs = Vec::new();
        flatten(settings, String::new(), &mut pairs);
        for (key, value) in pairs {
            let field = fields.iter().find(|f| f["key"] == key);
            let problem = match field {
                None => format!(
                    "not a setting this provider accepts (expected one of: {})",
                    fields
                        .iter()
                        .map(|f| f["key"].as_str().unwrap())
                        .collect::<BTreeSet<_>>()
                        .into_iter()
                        .collect::<Vec<_>>()
                        .join(", ")
                ),
                Some(f) => match f["type"].as_str().unwrap_or_default() {
                    "enum"
                        if !f["values"].as_array().unwrap().contains(&json!(
                            value
                                .as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| value.to_string())
                        )) =>
                    {
                        format!(
                            "must be one of: {}",
                            f["values"]
                                .as_array()
                                .unwrap()
                                .iter()
                                .map(|v| v.as_str().unwrap())
                                .collect::<Vec<_>>()
                                .join(", ")
                        )
                    }
                    "boolean" if !value.is_boolean() => "must be true or false".into(),
                    "integer" => match value.as_i64() {
                        None => "must be a whole number".into(),
                        Some(n) if f["minimum"].as_i64().is_some_and(|min| n < min) => {
                            format!("must be at least {}", f["minimum"])
                        }
                        Some(n) if f["maximum"].as_i64().is_some_and(|max| n > max) => {
                            format!("must be at most {}", f["maximum"])
                        }
                        _ => String::new(),
                    },
                    "string" | "model_id" if !value.is_string() => "must be text".into(),
                    _ => String::new(),
                },
            };
            if !problem.is_empty() {
                problems.push(format!("{level}.{key}: {problem}"));
            }
        }
    }
    if !problems.is_empty() {
        return Err(SetupError::text(
            "invalid_effort_settings",
            format!("{where_}: {}", problems.join("; ")),
        )
        .into());
    }
    Ok(())
}

#[cfg(test)]
#[path = "intelligences_tests.rs"]
mod tests;
