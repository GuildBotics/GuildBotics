//! Resolve the one command's non-secret guest configuration on the host.
use crate::commands::CommandRoots;
use anyhow::Context;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Component, Path, PathBuf},
};

const MODELS: &[&str] = &[
    "agno.models.openai.OpenAIChat",
    "agno.models.anthropic.Claude",
    "agno.models.google.Gemini",
];

fn read(path: &Path) -> anyhow::Result<Value> {
    crate::yaml::value(&std::fs::read_to_string(path)?)
        .map_err(|_| anyhow::anyhow!("Invalid configuration: {}", path.display()))
}
fn safe(relative: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        !relative.is_empty()
            && Path::new(relative)
                .components()
                .all(|part| matches!(part, Component::Normal(_))),
        "Invalid definition path"
    );
    Ok(())
}
fn selected(roots: &CommandRoots, person: &str, relative: &str) -> anyhow::Result<PathBuf> {
    safe(relative)?;
    let path = [
        roots
            .config
            .join(format!("team/members/{person}/{relative}")),
        roots.config.join(relative),
        roots.templates.join(relative),
    ]
    .into_iter()
    .find(|path| path.is_file())
    .with_context(|| format!("Missing configuration {relative}"))?;
    Ok(path)
}
fn mapping(
    roots: &CommandRoots,
    person: &str,
    name: &str,
) -> anyhow::Result<BTreeMap<String, Value>> {
    let relative = format!("intelligences/{name}_mapping.yml");
    let mut result = BTreeMap::new();
    for path in [
        roots.templates.join(&relative),
        roots.config.join(&relative),
        roots
            .config
            .join(format!("team/members/{person}/{relative}")),
    ] {
        if path.is_file() {
            let value = read(&path)?;
            for (key, value) in value
                .as_object()
                .context("Slot mapping must be an object")?
            {
                result.insert(key.clone(), value.clone());
            }
        }
    }
    Ok(result)
}

/// Output conforms to wire/schema/ResolvedGuest.json. Paths are host paths;
/// the environment maps package paths to its read-only code mount once.
pub fn resolve(roots: &CommandRoots, identity: &Path, person: &str) -> anyhow::Result<Value> {
    let team = crate::team::Team::load(&roots.config, identity)?;
    anyhow::ensure!(
        team.members.iter().any(|m| m.person_id == person),
        "Unknown guest member"
    );
    let mut member = read(&identity.join(format!(
        ".guildbotics/config/team/members/{person}/identity.yml"
    )))?;
    let participation = read(
        &roots
            .config
            .join(format!("team/members/{person}/member.yml")),
    )?;
    let object = member.as_object_mut().context("Member must be an object")?;
    for (key, value) in participation
        .as_object()
        .context("Participation must be an object")?
    {
        if key == "profile" {
            let profile = object
                .entry(key.clone())
                .or_insert(json!({}))
                .as_object_mut()
                .context("Invalid profile")?;
            profile.extend(value.as_object().context("Invalid profile")?.clone());
        } else {
            object.insert(key.clone(), value.clone());
        }
    }
    let mut role_defaults = serde_json::Map::new();
    for root in [&roots.templates, &roots.config] {
        let directory = root.join("roles");
        if directory.is_dir() {
            let mut files = std::fs::read_dir(&directory)?.collect::<Result<Vec<_>, _>>()?;
            files.sort_by_key(|entry| entry.file_name());
            for entry in files {
                if entry.file_type()?.is_file()
                    && entry
                        .file_name()
                        .to_string_lossy()
                        .ends_with(&format!(".{}.yml", team.language))
                    && let Some(roles) = read(&entry.path())?["roles"].as_object()
                {
                    role_defaults.extend(roles.clone());
                }
            }
        }
    }
    let mut roles = serde_json::Map::new();
    if let Some(declared) = member["profile"]["roles"].as_object() {
        for (name, overlay) in declared {
            let mut role = role_defaults.get(name).cloned().unwrap_or(json!({}));
            role["id"] = name.clone().into();
            for field in ["summary", "description"] {
                if let Some(text) = overlay[field].as_str().filter(|s| !s.is_empty()) {
                    role[field] = text.into();
                }
                if role.get(field).is_none() {
                    role[field] = "".into();
                }
            }
            roles.insert(name.clone(), role);
        }
    }
    member["roles"] = Value::Object(roles);
    let models = mapping(roots, person, "model")?;
    let tools = mapping(roots, person, "cli_agent")?;
    let mut brains = BTreeMap::new();
    for (name, config) in mapping(roots, person, "brain")? {
        let kind = config["kind"].as_str().context("Brain requires kind")?;
        let (slot, definition) = match kind {
            "model" => {
                let slot = config["model"]
                    .as_str()
                    .context("Brain requires model slot")?;
                let relative = models
                    .get(slot)
                    .and_then(Value::as_str)
                    .context("Unknown model slot")?;
                let mut definition = read(&selected(
                    roots,
                    person,
                    &format!("intelligences/{relative}"),
                )?)?;
                anyhow::ensure!(
                    MODELS.contains(&definition["model_class"].as_str().unwrap_or("")),
                    "Unsupported inference model_class"
                );
                definition["name"] = relative.into();
                if definition.get("effort").is_none() {
                    let default = Path::new(relative)
                        .parent()
                        .context("Model has no provider")?
                        .join("default.yml");
                    let defaults = read(&selected(
                        roots,
                        person,
                        &format!("intelligences/{}", default.display()),
                    )?)?;
                    definition["effort"] = defaults.get("effort").cloned().unwrap_or(json!({}));
                }
                // Model schemas contain no secret values; key arguments are host-owned.
                for key in ["api_key", "token"] {
                    definition.as_object_mut().unwrap().remove(key);
                }
                if let Some(parameters) = definition
                    .get_mut("parameters")
                    .and_then(Value::as_object_mut)
                {
                    for key in [
                        "api_key",
                        "token",
                        "api_key_env",
                        "base_url",
                        "client_params",
                    ] {
                        parameters.remove(key);
                    }
                }
                (slot.to_owned(), definition)
            }
            "ai_cli" => {
                let slot = config["cli_agent"]
                    .as_str()
                    .context("Brain requires cli_agent slot")?;
                let relative = tools
                    .get(slot)
                    .and_then(Value::as_str)
                    .context("Unknown CLI slot")?;
                let definition = read(&selected(
                    roots,
                    person,
                    &format!("intelligences/{relative}"),
                )?)?;
                let adapter = relative
                    .split('/')
                    .nth(1)
                    .context("Invalid CLI definition")?;
                anyhow::ensure!(
                    ["codex", "claude", "copilot", "grok", "antigravity"].contains(&adapter),
                    "Unsupported CLI adapter"
                );
                (
                    slot.to_owned(),
                    json!({"adapter":adapter,"parameters":definition.get("parameters").cloned().unwrap_or(json!({})),"effort":definition.get("effort").cloned().unwrap_or(json!({}))}),
                )
            }
            "jev" => {
                let settings = read(&selected(roots, person, "intelligences/jev.yml")?)?;
                let base_url = settings["base_url"]
                    .as_str()
                    .context("Jev requires base_url")?;
                anyhow::ensure!(
                    base_url.starts_with("https://") || base_url.starts_with("http://"),
                    "Invalid Jev base_url"
                );
                (
                    config["model"]
                        .as_str()
                        .context("Jev requires model")?
                        .to_owned(),
                    json!({"base_url":base_url,"api_key_env":"TYPESAFE_API_KEY"}),
                )
            }
            _ => anyhow::bail!("Unsupported brain kind"),
        };
        brains.insert(
            name,
            json!({"kind":kind,"slot":slot,"definition":definition}),
        );
    }
    let mut commands = BTreeMap::new();
    let mut names = std::collections::BTreeSet::new();
    for directory in [
        roots.templates.join("commands"),
        roots.config.join("commands"),
        roots.config.join(format!("team/members/{person}/commands")),
    ] {
        inventory(&directory, &directory, &team.language, &mut names)?;
    }
    for name in names {
        if let Ok(path) = roots.resolve(&name, &team.language, Some(person)) {
            commands.insert(name, path.to_string_lossy().to_string());
        }
    }
    let mut clone_paths = BTreeMap::new();
    let workspace = roots
        .config
        .parent()
        .and_then(Path::parent)
        .context("Workspace config root is invalid")?;
    let member_clones = workspace.join(".guildbotics/local/clones").join(person);
    clone_paths.insert(
        person.to_owned(),
        member_clones.to_string_lossy().to_string(),
    );
    if member_clones.is_dir() {
        for entry in std::fs::read_dir(&member_clones)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                clone_paths.insert(
                    format!("{person}/{}", entry.file_name().to_string_lossy()),
                    entry.path().to_string_lossy().to_string(),
                );
            }
        }
    }
    let project = read(&roots.config.join("team/project.yml"))?;
    Ok(
        json!({"language":team.language,"project":project,"member":member,"brains":brains,"commands":commands,"clone_paths":clone_paths,"inference_urls":{},"inference_keys":{}}),
    )
}

fn inventory(
    root: &Path,
    directory: &Path,
    language: &str,
    names: &mut std::collections::BTreeSet<String>,
) -> anyhow::Result<()> {
    if !directory.exists() {
        return Ok(());
    }
    for entry in std::fs::read_dir(directory)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            inventory(root, &entry.path(), language, names)?;
        } else if entry.file_type()?.is_file() {
            let path = entry.path();
            let extension = format!(
                ".{}",
                path.extension().unwrap_or_default().to_string_lossy()
            );
            if !crate::commands::EXTENSIONS.contains(&extension.as_str()) {
                continue;
            }
            let mut relative = path.strip_prefix(root)?.with_extension("");
            let stem = relative
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();
            if stem.contains(".metadata") {
                continue;
            }
            if let Some((base, locale)) = stem.rsplit_once('.') {
                if locale != language && locale != "en" {
                    continue;
                }
                relative.set_file_name(base);
            }
            let name = relative.to_string_lossy().replace('\\', "/");
            names.insert(name.clone());
            names.insert(format!("{name}{extension}"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn put(root: &Path, name: &str, content: &str) {
        let path = root.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
    }
    #[test]
    fn guest_slots_and_commands_are_resolved_on_the_host() {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join("workspace/.guildbotics/config");
        let templates = dir.path().join("templates");
        let identity = dir.path().join("identity");
        put(&config, "team/project.yml", "language: ja\n");
        put(&config, "team/members/aiko/member.yml", "is_active: true\n");
        put(
            &identity,
            ".guildbotics/config/team/members/aiko/identity.yml",
            "person_id: aiko\nname: Aiko\n",
        );
        put(
            &templates,
            "intelligences/brain_mapping.yml",
            "default:\n  kind: model\n  model: default\n",
        );
        put(
            &templates,
            "intelligences/model_mapping.yml",
            "default: models/openai/default.yml\n",
        );
        let model = "model_class: agno.models.openai.OpenAIChat\nparameters:\n  id: fixture\n";
        put(&templates, "intelligences/models/openai/default.yml", model);
        put(
            &templates,
            "commands/probe.en.py",
            "def main(context): return 1\n",
        );
        put(
            &config,
            "commands/probe.ja.py",
            "def main(context): return 2\n",
        );
        let roots = CommandRoots {
            config: config.clone(),
            templates: templates.clone(),
        };
        let data = resolve(&roots, &identity, "aiko").unwrap();
        assert_eq!(data["language"], "ja");
        assert_eq!(data["member"]["person_id"], "aiko");
        assert_eq!(
            data["brains"]["default"]["definition"]["parameters"]["id"],
            "fixture"
        );
        assert_eq!(
            data["commands"]["probe"],
            config
                .join("commands/probe.ja.py")
                .to_string_lossy()
                .to_string()
        );
        put(
            &config,
            "team/members/aiko/intelligences/models/openai/default.yml",
            &model.replace("fixture", "member"),
        );
        assert_eq!(
            resolve(&roots, &identity, "aiko").unwrap()["brains"]["default"]["definition"]["parameters"]
                ["id"],
            "member"
        );
        put(
            &config,
            "team/members/aiko/intelligences/models/openai/default.yml",
            &model.replace("agno.models.openai.OpenAIChat", "other.UnsafeModel"),
        );
        assert!(
            resolve(&roots, &identity, "aiko")
                .unwrap_err()
                .to_string()
                .contains("Unsupported inference model_class")
        );
    }
}
