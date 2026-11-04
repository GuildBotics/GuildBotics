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
    let team = if roots.config.join(&relative).is_file() {
        roots.config.join(&relative)
    } else {
        roots.templates.join(&relative)
    };
    for path in [
        team,
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
    let default = [
        format!("roles/default.{}.yml", team.language),
        "roles/default.en.yml".into(),
        "roles/default.yml".into(),
    ]
    .into_iter()
    .flat_map(|name| [roots.config.join(&name), roots.templates.join(&name)])
    .find(|path| path.is_file());
    if let Some(path) = default {
        add_roles(&path, &mut role_defaults)?;
    }
    // Additional roles come from the selected roles directory, not both roots.
    let directory = if roots.config.join("roles").is_dir() {
        roots.config.join("roles")
    } else {
        roots.templates.join("roles")
    };
    if directory.is_dir() {
        let mut files = std::fs::read_dir(&directory)?.collect::<Result<Vec<_>, _>>()?;
        files.sort_by_key(|entry| entry.file_name());
        for entry in files {
            let name = entry.file_name().to_string_lossy().to_string();
            if entry.file_type()?.is_file()
                && !name.starts_with("default.")
                && name.ends_with(&format!(".{}.yml", team.language))
            {
                add_roles(&entry.path(), &mut role_defaults)?;
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
    let mut brains = BTreeMap::new();
    for (name, config) in mapping(roots, person, "brain")? {
        let resolved = resolve_brain(roots, person, &config).unwrap_or_else(|error| {
            json!({"kind":"invalid", "slot":name, "definition":{"error":error.to_string()}})
        });
        brains.insert(name, resolved);
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

fn add_roles(path: &Path, roles: &mut serde_json::Map<String, Value>) -> anyhow::Result<()> {
    if let Some(values) = read(path)?["roles"].as_object() {
        for (name, value) in values {
            let role = roles.entry(name.clone()).or_insert(json!({}));
            for field in ["summary", "description"] {
                if let Some(text) = value[field].as_str().filter(|text| !text.is_empty()) {
                    role[field] = text.into();
                }
            }
        }
    }
    Ok(())
}

fn resolve_brain(roots: &CommandRoots, person: &str, config: &Value) -> anyhow::Result<Value> {
    let kind = config["kind"].as_str().context("Brain requires kind")?;
    let (slot, definition) = match kind {
        "model" => {
            let slot = config["model"]
                .as_str()
                .context("Brain requires model slot")?;
            let models = mapping(roots, person, "model")?;
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
            if definition.get("effort").is_none_or(Value::is_null) {
                let default = Path::new(relative)
                    .parent()
                    .context("Model has no provider")?
                    .join("default.yml");
                let relative_default = format!("intelligences/{}", default.display());
                let inherited = if Path::new(relative) != default {
                    selected(roots, person, &relative_default)
                        .ok()
                        .map(|path| read(&path))
                        .transpose()?
                        .and_then(|value| value.get("effort").cloned())
                        .filter(|value| value.as_object().is_some_and(|map| !map.is_empty()))
                } else {
                    None
                };
                let packaged = roots.templates.join(relative_default);
                definition["effort"] = match inherited {
                    Some(effort) => effort,
                    None if packaged.is_file() => {
                        read(&packaged)?.get("effort").cloned().unwrap_or(json!({}))
                    }
                    None => json!({}),
                };
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
            let tools = mapping(roots, person, "cli_agent")?;
            let relative = tools
                .get(slot)
                .and_then(Value::as_str)
                .context("Unknown CLI slot")?;
            let mut definition = read(&selected(
                roots,
                person,
                &format!("intelligences/{relative}"),
            )?)?;
            let default = Path::new(relative)
                .parent()
                .context("Invalid CLI definition")?
                .join("default.yml");
            if default != Path::new(relative) {
                let mut inherited = selected(
                    roots,
                    person,
                    &format!("intelligences/{}", default.display()),
                )
                .ok()
                .map(|path| read(&path))
                .transpose()?
                .unwrap_or(json!({}));
                inherited
                    .as_object_mut()
                    .context("CLI default must be an object")?
                    .extend(
                        definition
                            .as_object()
                            .context("CLI definition must be an object")?
                            .clone(),
                    );
                definition = inherited;
            }
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
    Ok(json!({"kind":kind,"slot":slot,"definition":definition}))
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
    fn fixture() -> (tempfile::TempDir, CommandRoots, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let roots = CommandRoots {
            config: dir.path().join("workspace/.guildbotics/config"),
            templates: dir.path().join("templates"),
        };
        let identity = dir.path().join("identity");
        put(&roots.config, "team/project.yml", "language: fr\n");
        put(
            &roots.config,
            "team/members/aiko/member.yml",
            "is_active: true\nprofile:\n  roles:\n    engineer: {}\n",
        );
        put(
            &identity,
            ".guildbotics/config/team/members/aiko/identity.yml",
            "person_id: aiko\nname: Aiko\n",
        );
        (dir, roots, identity)
    }
    #[test]
    fn a_resolved_command_snapshot_does_not_read_changed_settings_between_turns() {
        let (_dir, roots, identity) = fixture();
        put(
            &roots.config,
            "intelligences/brain_mapping.yml",
            "default: {kind: ai_cli, cli_agent: default}\n",
        );
        put(
            &roots.config,
            "intelligences/cli_agent_mapping.yml",
            "default: cli_agents/claude/default.yml\n",
        );
        put(
            &roots.config,
            "intelligences/cli_agents/claude/default.yml",
            "parameters: {model: initial}\n",
        );
        put(
            &roots.config,
            "commands/hello.md",
            "---\nbrain: default\n---\noriginal prompt\n",
        );
        let started = resolve(&roots, &identity, "aiko").unwrap();
        let readonly_payload = serde_json::to_vec(&started).unwrap();
        put(
            &roots.config,
            "intelligences/cli_agents/claude/default.yml",
            "parameters: {model: changed}\n",
        );
        put(&roots.config, "team/project.yml", "language: ja\n");
        for _turn in 0..2 {
            let guest: Value = serde_json::from_slice(&readonly_payload).unwrap();
            assert_eq!(guest["language"], "fr");
            assert_eq!(
                guest["brains"]["default"]["definition"]["parameters"]["model"],
                "initial"
            );
        }
        let next = resolve(&roots, &identity, "aiko").unwrap();
        assert_eq!(next["language"], "ja");
        assert_eq!(
            next["brains"]["default"]["definition"]["parameters"]["model"],
            "changed"
        );
    }

    #[test]
    fn cli_slots_inherit_absent_keys_but_preserve_explicit_empty_overlays() {
        let (_dir, roots, identity) = fixture();
        put(
            &roots.config,
            "intelligences/brain_mapping.yml",
            "fast: {kind: ai_cli, cli_agent: fast}\n",
        );
        put(
            &roots.config,
            "intelligences/cli_agent_mapping.yml",
            "fast: cli_agents/claude/fast.yml\n",
        );
        put(
            &roots.templates,
            "intelligences/cli_agents/claude/default.yml",
            "parameters: {model: default}\neffort: {high: {effort: high}}\n",
        );
        for (content, parameters, effort) in [
            (
                "parameters: {model: fast}\n",
                json!({"model":"fast"}),
                json!({"high":{"effort":"high"}}),
            ),
            ("effort: {}\n", json!({"model":"default"}), json!({})),
        ] {
            put(
                &roots.config,
                "intelligences/cli_agents/claude/fast.yml",
                content,
            );
            let value = resolve(&roots, &identity, "aiko").unwrap();
            assert_eq!(
                value["brains"]["fast"]["definition"]["parameters"],
                parameters
            );
            assert_eq!(value["brains"]["fast"]["definition"]["effort"], effort);
        }
    }
    #[test]
    fn cli_definition_does_not_require_an_optional_tool_default() {
        let (_dir, roots, _) = fixture();
        put(
            &roots.config,
            "intelligences/cli_agent_mapping.yml",
            "fast: cli_agents/claude/fast.yml\n",
        );
        put(
            &roots.config,
            "intelligences/cli_agents/claude/fast.yml",
            "parameters: {model: custom}\n",
        );
        let value =
            resolve_brain(&roots, "aiko", &json!({"kind":"ai_cli","cli_agent":"fast"})).unwrap();
        assert_eq!(value["definition"]["parameters"]["model"], "custom");
        assert_eq!(value["definition"]["effort"], json!({}));
    }
    #[test]
    fn model_effort_reaches_packaged_default_and_respects_explicit_empty() {
        let (_dir, roots, identity) = fixture();
        put(
            &roots.config,
            "intelligences/brain_mapping.yml",
            "default: {kind: model, model: default}\n",
        );
        let model = "model_class: agno.models.openai.OpenAIChat\nparameters: {id: fixture}\n";
        put(
            &roots.templates,
            "intelligences/models/openai/default.yml",
            &format!("{model}effort: {{high: {{reasoning_effort: high}}}}\n"),
        );
        put(
            &roots.config,
            "intelligences/models/openai/default.yml",
            model,
        );
        for filename in ["default", "fast"] {
            put(
                &roots.config,
                "intelligences/model_mapping.yml",
                &format!("default: models/openai/{filename}.yml\n"),
            );
            for explicit_empty in [false, true] {
                put(
                    &roots.config,
                    &format!("intelligences/models/openai/{filename}.yml"),
                    &format!(
                        "{model}{}",
                        if explicit_empty { "effort: {}\n" } else { "" }
                    ),
                );
                let value = resolve(&roots, &identity, "aiko").unwrap();
                assert_eq!(
                    value["brains"]["default"]["definition"]["effort"],
                    if explicit_empty {
                        json!({})
                    } else {
                        json!({"high":{"reasoning_effort":"high"}})
                    }
                );
            }
        }
    }
    #[test]
    fn every_mapping_selects_team_file_before_member_overlay() {
        let (_dir, roots, _) = fixture();
        for name in ["brain", "model", "cli_agent"] {
            let relative = format!("intelligences/{name}_mapping.yml");
            put(
                &roots.templates,
                &relative,
                "template_only: template\ncommon: template\n",
            );
            put(&roots.config, &relative, "team_only: team\ncommon: team\n");
            put(
                &roots.config,
                &format!("team/members/aiko/{relative}"),
                "member_only: member\ncommon: member\n",
            );
            let slots = mapping(&roots, "aiko", name).unwrap();
            assert_eq!(slots.len(), 3);
            assert_eq!(slots["common"], "member");
            assert_eq!(slots["team_only"], "team");
            assert_eq!(slots["member_only"], "member");
            assert!(!slots.contains_key("template_only"));
        }
    }
    #[test]
    fn workspace_mapping_shadows_templates_and_invalid_slots_are_deferred() {
        let (_dir, roots, identity) = fixture();
        put(
            &roots.templates,
            "intelligences/brain_mapping.yml",
            "template_only: {kind: model, model: missing}\n",
        );
        put(
            &roots.config,
            "intelligences/brain_mapping.yml",
            "broken: {kind: unknown}\n",
        );
        put(
            &roots.config,
            "team/members/aiko/intelligences/brain_mapping.yml",
            "member: {kind: model, model: missing}\n",
        );
        put(
            &roots.config,
            "commands/hello.py",
            "def main(context): return 'ok'\n",
        );
        let value = resolve(&roots, &identity, "aiko").unwrap();
        assert!(value["brains"].get("template_only").is_none());
        assert_eq!(value["brains"]["broken"]["kind"], "invalid");
        assert_eq!(
            value["brains"]["broken"]["definition"]["error"],
            "Unsupported brain kind"
        );
        assert_eq!(value["brains"]["member"]["kind"], "invalid");
        assert!(
            value["commands"]["hello"]
                .as_str()
                .unwrap()
                .ends_with("hello.py")
        );
    }
    #[test]
    fn roles_use_localized_default_then_workspace_extras_and_nonempty_overrides() {
        let (_dir, roots, identity) = fixture();
        put(
            &roots.templates,
            "roles/default.en.yml",
            "roles: {engineer: {summary: English, description: Default}}\n",
        );
        put(
            &roots.templates,
            "roles/extra.fr.yml",
            "roles: {engineer: {summary: Ignored template extra}}\n",
        );
        put(
            &roots.config,
            "roles/default.yml",
            "roles: {engineer: {summary: Unlocalized}}\n",
        );
        put(
            &roots.config,
            "roles/extra.fr.yml",
            "roles: {engineer: {summary: '', description: Workspace}}\n",
        );
        let value = resolve(&roots, &identity, "aiko").unwrap();
        assert_eq!(value["member"]["roles"]["engineer"]["summary"], "English");
        assert_eq!(
            value["member"]["roles"]["engineer"]["description"],
            "Workspace"
        );
        put(
            &roots.config,
            "roles/default.fr.yml",
            "roles: {engineer: {summary: French, description: Localized}}\n",
        );
        assert_eq!(
            resolve(&roots, &identity, "aiko").unwrap()["member"]["roles"]["engineer"]["summary"],
            "French"
        );
        std::fs::remove_file(roots.config.join("roles/default.fr.yml")).unwrap();
        std::fs::remove_file(roots.templates.join("roles/default.en.yml")).unwrap();
        assert_eq!(
            resolve(&roots, &identity, "aiko").unwrap()["member"]["roles"]["engineer"]["summary"],
            "Unlocalized"
        );
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
        assert_eq!(
            resolve(&roots, &identity, "aiko").unwrap()["brains"]["default"]["definition"]["error"],
            "Unsupported inference model_class"
        );
    }
}
