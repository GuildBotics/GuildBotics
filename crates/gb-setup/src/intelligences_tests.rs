use super::*;

fn fixture() -> (tempfile::TempDir, ConfigRepository, PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let repository = ConfigRepository::new(gb_base::shared_writes::SharedWrites::new(
        tmp.path().join("identity"),
        Some(tmp.path().join("workspace")),
    ))
    .unwrap();
    (
        tmp,
        repository,
        gb_base::home::python_package().join("templates"),
    )
}

#[test]
fn inherited_slots_and_member_differences_are_reconciled() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    let team = service.read(None).unwrap();
    assert_eq!(team["person_id"], Value::Null);
    assert_eq!(team["inherited"], false);
    assert!(
        team["models"]
            .as_array()
            .unwrap()
            .iter()
            .all(|m| m["path"].as_str().unwrap().starts_with("models/"))
    );
    assert!(!team["models"].as_array().unwrap().is_empty());
    assert!(!team["brain_mapping"].as_array().unwrap().is_empty());
    assert_eq!(team["inherited_model_slots"], json!([]));
    let member = service.read(Some("alice")).unwrap();
    assert_eq!(member["inherited"], true);
    assert_eq!(member["model_mapping"], team["model_mapping"]);
    let mut form: Update = serde_json::from_value(json!({"person_id":"alice", "model_mapping":team["model_mapping"], "brain_mapping":team["brain_mapping"]})).unwrap();
    service.update(&form, &Revisions::new()).unwrap();
    assert!(
        !repository
            .view()
            .path("team/members/alice/intelligences/model_mapping.yml")
            .unwrap()
            .exists()
    );
    form.model_mapping =
        Some(serde_json::from_value(json!({"default":"models/openai/custom.yml"})).unwrap());
    service.update(&form, &Revisions::new()).unwrap();
    assert_eq!(
        service.read(Some("alice")).unwrap()["model_mapping"]["default"],
        "models/openai/custom.yml"
    );
    form.inherit_team_defaults = true;
    let receipt = service.update(&form, &Revisions::new()).unwrap();
    assert_eq!(receipt.result[0]["action"], "delete");
    assert_eq!(service.read(Some("alice")).unwrap()["inherited"], true);
    let repeated = service.update(&form, &Revisions::new()).unwrap();
    assert_eq!(repeated.result.len(), 1);
    assert_eq!(repeated.result[0]["action"], "delete");
}

#[test]
fn model_parameters_and_native_empty_effort_survive_save() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    let path = repository
        .view()
        .path("intelligences/models/openai/default.yml")
        .unwrap();
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, json!({"model_class":"old", "parameters":{"id":"old","temperature":0.5,"reasoning_effort":"low"},"rate_limit":{"max_requests_per_minute":3}}).to_string()).unwrap();
    let form: Update = serde_json::from_value(json!({"models":[{"path":"models/openai/default.yml","provider":"openai","model_class":"new","parameters":{"id":"new"}}],"cli_agents":[{"path":"cli_agents/codex/default.yml","name":"codex"}]})).unwrap();
    service.update(&form, &Revisions::new()).unwrap();
    let stored = repository
        .read_config("intelligences/models/openai/default.yml")
        .unwrap()
        .unwrap()
        .value()
        .unwrap();
    assert_eq!(stored["parameters"], json!({"id":"new","temperature":0.5}));
    assert_eq!(stored["rate_limit"], json!({"max_requests_per_minute":3}));
    assert_eq!(stored["model_class"], "new");
    let native = repository
        .read_config("intelligences/cli_agents/codex/default.yml")
        .unwrap()
        .unwrap()
        .value()
        .unwrap();
    assert_eq!(native, json!({"parameters":{},"effort":{}}));
    let read = service.read(None).unwrap();
    let codex = read["cli_agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["name"] == "codex")
        .unwrap();
    assert_eq!(codex["effort"], json!({}));
    assert!(!codex["effort_fields"].as_array().unwrap().is_empty());
}

#[test]
fn directory_revisions_guard_additions_moves_and_inheritance() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    for person in [None, Some("alice")] {
        let read = service.read(person).unwrap();
        let revisions = serde_json::from_value(read["revisions"].clone()).unwrap();
        let scope = scope(person);
        let path = repository
            .view()
            .path(&format!("{scope}/added.yml"))
            .unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "{}").unwrap();
        let form: Update =
            serde_json::from_value(json!({"person_id":person,"inherit_team_defaults":true}))
                .unwrap();
        assert!(
            service
                .update(&form, &revisions)
                .unwrap_err()
                .is::<gb_config::repository::StaleConfigWriteError>()
        );
    }
}

#[test]
fn invalid_brains_and_unknown_tools_never_write_assignments() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    for model in ["", "gpt-5", "claude-sonnet", "jev-1.13.0", "jev-preview"] {
        let form: Update = serde_json::from_value(json!({"brain_mapping":[{"name":"chat_decision","brain_class":JEV,"engine":"jev","target":model}]})).unwrap();
        assert!(service.update(&form, &Revisions::new()).is_err());
        assert!(
            repository
                .read_config("intelligences/brain_mapping.yml")
                .unwrap()
                .is_none()
        );
    }
    for name in ["default", "agent", "translate"] {
        let form: Update = serde_json::from_value(json!({"brain_mapping":[{"name":name,"brain_class":JEV,"engine":"jev","target":"jev-latest"}]})).unwrap();
        assert!(service.update(&form, &Revisions::new()).is_err());
    }
    for tool in ["codex", "claude", "grok", "copilot", "antigravity"] {
        let form: Update = serde_json::from_value(json!({"person_id":"alice","cli_agent_mapping":{"judge":format!("cli_agents/{tool}/default.yml")},"brain_mapping":[{"name":"chat_decision","brain_class":CLI,"engine":"cli","target":"judge"}]})).unwrap();
        assert!(service.update(&form, &Revisions::new()).is_err());
    }
}

fn save(service: &IntelligenceConfig<'_>, form: Value) -> Vec<Value> {
    service
        .update(&serde_json::from_value(form).unwrap(), &Revisions::new())
        .unwrap()
        .result
}
fn stored(repository: &ConfigRepository, name: &str) -> Value {
    repository
        .read_config(name)
        .unwrap()
        .map(|s| s.value().unwrap())
        .unwrap_or(Value::Null)
}

#[test]
fn complete_team_save_and_member_pruning_preserve_hidden_definition_fields() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    let form = json!({"model_mapping":{"default":"models/openai/gpt.yml","openai":"models/openai/gpt.yml"},"models":[{"path":"models/openai/gpt.yml","provider":"openai","model_class":"team.ModelClass","parameters":{"id":"team-model-id"}}],"cli_agent_mapping":{"default":"cli_agents/codex/default.yml"},"cli_agents":[{"path":"cli_agents/codex/default.yml","name":"codex","effort":{"high":{"effort":"high"}}}],"brain_mapping":[{"name":"default","brain_class":AGNO,"engine":"llm","target":"default"},{"name":"agent","brain_class":CLI,"engine":"cli","target":"default"}]});
    let files = save(&service, form.clone());
    assert_eq!(files.len(), 5);
    let prefix = repository.view().path("intelligences").unwrap();
    assert_eq!(
        files
            .iter()
            .map(|f| Path::new(f["path"].as_str().unwrap())
                .strip_prefix(&prefix)
                .unwrap()
                .to_string_lossy()
                .into_owned())
            .collect::<BTreeSet<_>>(),
        [
            "model_mapping.yml",
            "models/openai/gpt.yml",
            "cli_agent_mapping.yml",
            "cli_agents/codex/default.yml",
            "brain_mapping.yml"
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    );
    assert!(files.iter().all(|f| f["action"] == "update"));
    assert_eq!(
        stored(&repository, "intelligences/model_mapping.yml"),
        form["model_mapping"]
    );
    assert_eq!(
        stored(&repository, "intelligences/models/openai/gpt.yml")["parameters"],
        json!({"id":"team-model-id"})
    );
    let team = service.read(None).unwrap();
    assert_eq!(team["inherited_cli_slots"], json!([]));
    assert_eq!(team["inherited_brain_features"], json!([]));
    let member = service.read(Some("alice")).unwrap();
    assert_eq!(
        member["inherited_model_slots"],
        json!(["default", "openai"])
    );
    assert_eq!(member["inherited_cli_slots"], json!(["default"]));
    assert_eq!(
        member["inherited_brain_features"],
        json!(["agent", "default"])
    );
    assert_eq!(member["models"].as_array().unwrap().len(), 1);
    assert_eq!(member["models"][0]["parameters"]["id"], "team-model-id");
    let mut form = form;
    form["person_id"] = json!("alice");
    assert!(save(&service, form.clone()).is_empty());
    form["model_mapping"]["default"] = json!("models/anthropic/claude.yml");
    form["models"].as_array_mut().unwrap().push(json!({"path":"models/anthropic/claude.yml","provider":"anthropic","model_class":"member.ModelClass","parameters":{"id":"member-model-id"}}));
    let files = save(&service, form.clone());
    assert_eq!(files.len(), 2);
    let read = service.read(Some("alice")).unwrap();
    assert_eq!(read["inherited"], false);
    assert_eq!(read["model_mapping"]["openai"], "models/openai/gpt.yml");
    assert_eq!(
        read["model_mapping"]["default"],
        "models/anthropic/claude.yml"
    );
    assert!(
        read["models"]
            .as_array()
            .unwrap()
            .iter()
            .any(|m| m["provider"] == "anthropic" && m["parameters"]["id"] == "member-model-id")
    );
    assert!(
        !repository
            .view()
            .path("team/members/alice/intelligences/cli_agents")
            .unwrap()
            .exists()
    );
    let own = "team/members/alice/intelligences/models/openai/gpt.yml";
    let path = repository.view().path(own).unwrap();
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path,json!({"model_class":"team.ModelClass","parameters":{"id":"team-model-id"},"rate_limit":{"max_requests_per_minute":3}}).to_string()).unwrap();
    form["model_mapping"]["default"] = json!("models/openai/gpt.yml");
    form["models"].as_array_mut().unwrap().truncate(1);
    form["brain_mapping"]
        .as_array_mut()
        .unwrap()
        .push(json!({"name":"translate","brain_class":AGNO,"engine":"llm","target":"openai"}));
    save(&service, form.clone());
    assert_eq!(
        stored(&repository, own)["rate_limit"],
        json!({"max_requests_per_minute":3})
    );
    assert_eq!(
        stored(
            &repository,
            "team/members/alice/intelligences/brain_mapping.yml"
        ),
        json!({"translate":{"kind":"model","model":"openai"}})
    );
    assert!(
        !repository
            .view()
            .path("team/members/alice/intelligences/models/anthropic")
            .unwrap()
            .exists()
    );
    assert!(
        stored(
            &repository,
            "team/members/alice/intelligences/model_mapping.yml"
        )
        .is_null()
    );
    form["cli_agents"][0]["effort"] = json!({"high":{"effort":"medium"}});
    save(&service, form.clone());
    assert_eq!(
        stored(
            &repository,
            "team/members/alice/intelligences/cli_agents/codex/default.yml"
        )["effort"],
        form["cli_agents"][0]["effort"]
    );
    form["cli_agents"][0]["effort"] = json!({"high":{"effort":"high"}});
    save(&service, form);
    assert!(
        stored(
            &repository,
            "team/members/alice/intelligences/cli_agents/codex/default.yml"
        )
        .is_null()
    );
}

#[test]
fn native_catalog_and_shadowed_provider_defaults_keep_typed_fields() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    let mut mapping = Map::new();
    for tool in ["codex", "claude", "grok", "copilot", "antigravity"] {
        mapping.insert(tool.into(), json!(format!("cli_agents/{tool}/default.yml")));
    }
    save(&service, json!({"cli_agent_mapping":mapping}));
    for tool in ["codex", "claude", "grok", "copilot", "antigravity"] {
        let config = service.read(None).unwrap();
        let agent = config["cli_agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["name"] == tool)
            .unwrap();
        let keys: BTreeSet<_> = agent["effort_fields"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f["key"].as_str().unwrap())
            .collect();
        assert_eq!(
            keys,
            BTreeSet::from([
                "model",
                if ["grok", "copilot"].contains(&tool) {
                    "reasoning_effort"
                } else {
                    "effort"
                }
            ])
        );
        assert_eq!(agent["effort_supported"], true);
        assert_eq!(
            agent["inherited_effort"]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["low", "high"])
        );
        if tool == "grok" {
            assert_eq!(
                agent["effort_fields"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|f| f["key"] == "reasoning_effort")
                    .unwrap()["values"],
                json!(["low", "medium", "high"])
            );
        }
    }
    for provider in ["openai", "gemini"] {
        let path = repository
            .view()
            .path(&format!("intelligences/models/{provider}/default.yml"))
            .unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "model_class: old.Class\nparameters: {id: old}\n").unwrap();
        let custom = format!("models/{provider}/translation.yml");
        save(
            &service,
            json!({"model_mapping":{"translation":custom},"models":[{"path":custom,"provider":provider,"model_class":"old.Class","parameters":{"id":"new"}}]}),
        );
        let read = service.read(None).unwrap();
        let model = &read["models"][0];
        let keys: BTreeSet<_> = model["effort_fields"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f["key"].as_str().unwrap())
            .collect();
        assert_eq!(
            keys,
            BTreeSet::from([
                "id",
                if provider == "openai" {
                    "reasoning_effort"
                } else {
                    "thinking_budget"
                }
            ])
        );
        assert_eq!(
            model["inherited_effort"],
            if provider == "openai" {
                json!({"low":{"reasoning_effort":"low"},"high":{"reasoning_effort":"high"}})
            } else {
                json!({"low":{"thinking_budget":0},"high":{"thinking_budget":8000}})
            }
        );
        let rejected: Update = serde_json::from_value(json!({"models":[{"path":custom,"provider":provider,"effort":{"high":{"reasoning_efort":"high"}}}]})).unwrap();
        assert_eq!(
            service
                .update(&rejected, &Revisions::new())
                .unwrap_err()
                .downcast_ref::<SetupError>()
                .unwrap()
                .code,
            "invalid_effort_settings"
        );
    }
}

#[test]
fn effort_round_trips_nested_types_and_rejects_invalid_input_before_writes() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    let effort = json!({"high":{"thinking":{"type":"enabled","budget_tokens":8000}},"low":{"thinking":{"type":"disabled"}}});
    save(
        &service,
        json!({"model_mapping":{"default":"models/anthropic/claude.yml"},"models":[{"path":"models/anthropic/claude.yml","provider":"anthropic","model_class":"anthropic.Class","parameters":{"id":"claude-test"},"effort":effort}]}),
    );
    let model = stored(&repository, "intelligences/models/anthropic/claude.yml");
    assert_eq!(model["effort"], effort);
    assert_eq!(model["parameters"]["id"], "claude-test");
    assert_eq!(service.read(None).unwrap()["models"][0]["effort"], effort);
    for (cli, path, effort) in [
        (
            false,
            "models/openai/default.yml",
            json!({"high":{"reasoning_efort":"high"}}),
        ),
        (
            true,
            "cli_agents/codex/default.yml",
            json!({"high":{"efort":"high"}}),
        ),
        (
            false,
            "models/gemini/default.yml",
            json!({"high":{"thinking_budget":true}}),
        ),
        (
            false,
            "models/gemini/default.yml",
            json!({"high":{"thinking_budget":"8000"}}),
        ),
        (
            false,
            "models/openai/default.yml",
            json!({"high":{"reasoning_effort":"not-a-level"}}),
        ),
    ] {
        let form: Update = serde_json::from_value(json!({if cli {"cli_agents"} else {"models"}:[{"path":path,"provider":"openai","name":"codex","effort":effort}]})).unwrap();
        assert_eq!(
            service
                .update(&form, &Revisions::new())
                .unwrap_err()
                .downcast_ref::<SetupError>()
                .unwrap()
                .code,
            "invalid_effort_settings"
        );
    }
    for effort in [
        json!({"default":{}}),
        json!({"extreme":{"reasoning_effort":"high"}}),
        json!({"high":"not-a-mapping"}),
        json!({"high":[]}),
        json!([]),
    ] {
        for field in ["models", "cli_agents"] {
            assert!(serde_json::from_value::<Update>(json!({field:[{"path":if field == "models" {"models/openai/default.yml"} else {"cli_agents/codex/default.yml"},"provider":"openai","name":"codex","effort":effort}]})).is_err());
        }
    }
    assert!(
        repository
            .read_config("intelligences/models/openai/default.yml")
            .unwrap()
            .is_none()
    );
    save(
        &service,
        json!({"model_mapping":{"default":"models/openai/effort.yml"},"models":[{"path":"models/openai/effort.yml","provider":"openai","effort":{"high":{"reasoning_effort":"high"}}}]}),
    );
    assert_eq!(
        service.read(None).unwrap()["models"][0]["effort"],
        json!({"high":{"reasoning_effort":"high"}})
    );
}

#[test]
fn malformed_definitions_and_unknown_catalog_mappings_are_reported() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    save(
        &service,
        json!({"model_mapping":{"default":"models/openai/gpt.yml","openai":"models/openai/gpt.yml"},"cli_agent_mapping":{"default":"cli_agents/codex/reviewer.yml"}}),
    );
    for (name, text) in [
        ("models/openai/gpt.yml", "just-a-string\n"),
        ("cli_agents/codex/reviewer.yml", "- not-a-mapping\n"),
    ] {
        let path = repository
            .view()
            .path(&format!("intelligences/{name}"))
            .unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    let read = service.read(None).unwrap();
    assert_eq!(read["models"].as_array().unwrap().len(), 1);
    assert_eq!(read["models"][0]["model_class"], "");
    assert_eq!(read["models"][0]["parameters"], json!({}));
    assert_eq!(read["cli_agents"].as_array().unwrap().len(), 1);
    assert_eq!(read["cli_agents"][0]["name"], "codex");
    assert_eq!(read["cli_agents"][0]["parameters"], json!({}));
    assert_eq!(read["cli_agents"][0]["effort"], json!({}));
    let form: Update = serde_json::from_value(
        json!({"cli_agent_mapping":{"default":"cli_agents/mytool/default.yml"}}),
    )
    .unwrap();
    let e = service.update(&form, &Revisions::new()).unwrap_err();
    assert_eq!(
        e.downcast_ref::<SetupError>().unwrap().code,
        "unknown_cli_agent"
    );
    assert!(e.to_string().contains("'default'"));
    assert!(e.to_string().contains("cli_agents/mytool/default.yml"));
    assert!(
        !stored(&repository, "intelligences/cli_agent_mapping.yml")
            .to_string()
            .contains("mytool")
    );
    let path = repository
        .view()
        .path("intelligences/cli_agent_mapping.yml")
        .unwrap();
    std::fs::write(path, "default: cli_agents/mytool/default.yml\n").unwrap();
    assert_eq!(
        service
            .read(None)
            .unwrap_err()
            .downcast_ref::<SetupError>()
            .unwrap()
            .code,
        "unknown_cli_agent"
    );
}

#[test]
fn brain_rows_classify_engines_and_skip_non_mapping_entries() {
    let (_tmp, repository, templates) = fixture();
    let service = IntelligenceConfig {
        repository: &repository,
        templates: &templates,
    };
    let path = repository
        .view()
        .path("intelligences/brain_mapping.yml")
        .unwrap();
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path,json!({"llm_brain":{"kind":"model","model":"openai"},"agent":{"kind":"ai_cli","cli_agent":"codex"},"judge":{"kind":"jev","model":"jev-latest"},"skipped":"not-a-dict"}).to_string()).unwrap();
    let read = service.read(None).unwrap();
    let rows = read["brain_mapping"].as_array().unwrap();
    assert_eq!(rows.len(), 3);
    for (name, engine, class, target) in [
        ("llm_brain", "llm", AGNO, "openai"),
        ("agent", "cli", CLI, "codex"),
        ("judge", "jev", JEV, "jev-latest"),
    ] {
        let row = rows.iter().find(|r| r["name"] == name).unwrap();
        assert_eq!(row["engine"], engine);
        assert_eq!(row["brain_class"], class);
        assert_eq!(row["target"], target);
    }
}
