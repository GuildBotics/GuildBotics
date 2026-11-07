use super::*;

#[tokio::test]
async fn intelligence_http_reads_saves_and_resets_member_differences() {
    let f = fixture();
    f.init().await;
    let events = f.api.events.snapshot();
    let mut read = f.get("/config/intelligences").await;
    assert!(
        read["model_mapping"]["default"]
            .as_str()
            .unwrap()
            .starts_with("models/")
    );
    assert!(!read["brain_mapping"].as_array().unwrap().is_empty());
    assert!(
        read["revisions"]
            .as_object()
            .unwrap()
            .contains_key("intelligences/model_mapping.yml")
    );
    assert!(
        read["revisions"]
            .as_object()
            .unwrap()
            .keys()
            .all(|k| k.starts_with("intelligences/"))
    );
    assert_eq!(read["filesystem_grants"], json!({"documents":[]}));
    assert_eq!(
        read["agent_environment"]["resources"],
        json!({"memory_mib":4096,"cpus":2})
    );
    read["model_mapping"]["default"] = json!("models/openai/gpt-5-mini.yml");
    *read["brain_mapping"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|b| b["name"] == "default")
        .unwrap() = json!({"name":"default","brain_class":"guildbotics.guest.cli_agent.CliAgentBrain","engine":"cli","target":"codex"});
    read["expected_revisions"] = read["revisions"].clone();
    let (status, saved) = f.save("PUT", "/config/intelligences", read.clone()).await;
    assert_eq!(status, StatusCode::OK, "{saved}");
    assert_eq!(
        saved["revisions"],
        f.get("/config/intelligences").await["revisions"]
    );
    read["expected_revisions"] = saved["revisions"].clone();
    assert_eq!(
        f.save("PUT", "/config/intelligences", read).await.0,
        StatusCode::OK
    );
    let mut member = f.get("/config/intelligences?person_id=alice").await;
    assert_eq!(member["inherited"], true);
    *member["brain_mapping"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|b| b["name"] == "default")
        .unwrap() = json!({"name":"default","brain_class":"guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain","engine":"llm","target":"anthropic"});
    assert_eq!(
        f.save("PUT", "/config/intelligences", member).await.0,
        StatusCode::OK
    );
    let dir = f.config().join("team/members/alice/intelligences");
    let files = bytes_under(&dir);
    assert_eq!(files.len(), 1, "{files:?}");
    assert!(dir.join("brain_mapping.yml").is_file());
    let result = f
        .save(
            "PUT",
            "/config/intelligences",
            json!({"person_id":"alice","inherit_team_defaults":true}),
        )
        .await;
    assert_eq!(result.0, StatusCode::OK, "{}", result.1);
    assert!(!dir.exists());
    assert_eq!(f.api.events.snapshot(), events);
}

#[tokio::test]
async fn intelligence_http_stale_content_additions_and_inherited_tree_refuse_save() {
    for mode in ["content", "added", "inherited"] {
        let f = fixture();
        f.init().await;
        let person = if mode == "inherited" {
            Some("alice")
        } else {
            None
        };
        let url = if person.is_some() {
            "/config/intelligences?person_id=alice"
        } else {
            "/config/intelligences"
        };
        let read = f.get(url).await;
        let dir = f.config().join(gb_setup::intelligences::scope(person));
        let changed = dir.join(if mode == "content" {
            "model_mapping.yml"
        } else {
            "models/openai/o9.yml"
        });
        f.write(&changed, "model_class: newer\n");
        let (status,body) = f.save("PUT","/config/intelligences",json!({"person_id":person,"expected_revisions":read["revisions"],"inherit_team_defaults":true})).await;
        assert_eq!(status, StatusCode::CONFLICT, "{mode}: {body}");
        assert_eq!(body["code"], "config_changed");
        assert_eq!(
            std::fs::read_to_string(changed).unwrap(),
            "model_class: newer\n"
        );
    }
}

#[tokio::test]
async fn decision_credentials_stay_in_workspace_store_and_never_echo() {
    let f = fixture();
    f.init().await;
    assert_eq!(
        f.get("/intelligences/decisions/options").await["credential_present"],
        false
    );
    let (status, body) = f
        .call(
            "POST",
            "/intelligences/decisions/credential",
            json!({"value":"  synthetic-decision-value  "}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, json!({"state":"unverified"}));
    assert_eq!(
        f.workspace_secrets()["TYPESAFE_API_KEY"],
        "synthetic-decision-value"
    );
    assert!(!f.identity_secrets().contains_key("TYPESAFE_API_KEY"));
    assert_eq!(
        f.get("/intelligences/decisions/options").await["credential_present"],
        true
    );
    f.repository()
        .workspace_secrets(f.device())
        .unwrap()
        .delete("TYPESAFE_API_KEY")
        .unwrap();
    assert_eq!(
        f.get("/intelligences/decisions/options").await["credential_present"],
        false
    );
    for value in [json!(["synthetic-private"]), json!("")] {
        let (status, body) = f
            .call(
                "POST",
                "/intelligences/decisions/credential",
                json!({"value":value}),
            )
            .await;
        assert!(status.is_client_error());
        assert!(!body.to_string().contains("synthetic-private"));
    }
    f.keys(Keys::Locked);
    let (status, body) = f
        .call(
            "POST",
            "/intelligences/decisions/credential",
            json!({"value":"synthetic-private"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_decision");
    assert!(!body.to_string().contains("synthetic-private"));
}

#[tokio::test]
async fn provider_catalog_is_available_without_project_and_settings_require_project() {
    let f = fixture();
    let providers = f.get("/intelligences/model-providers").await;
    assert!(
        providers["providers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|p| p["provider"] == "openai")
    );
    let (status, body) = f.call("GET", "/config/intelligences", Value::Null).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "project_not_found");
}

#[tokio::test]
async fn grant_preview_validates_without_creating_directories_and_composes_existing() {
    let f = fixture();
    f.init().await;
    for name in ["Documents", ".ssh", ".cache/uv", ".codex", "shared/private"] {
        std::fs::create_dir_all(f.root.join("home").join(name)).unwrap();
    }
    for (scope, path, access, valid, present) in [
        ("document", "Documents", "read", true, true),
        ("document", "Projects/new", "read_write", true, false),
        ("document", "/opt/x", "read", false, false),
        ("document", "..", "read", false, false),
        ("document", ".ssh", "read", false, false),
        ("local", "/opt/nowhere", "read", false, false),
        ("local", ".cache/uv", "read_write", true, true),
        ("local", ".codex", "read", false, false),
        ("deny", "/opt/homebrew/etc", "read", true, true),
        ("deny", ".local/share/some-app", "read", true, true),
        ("deny", "..", "read", false, false),
    ] {
        let v = f
            .get(&format!(
                "/intelligences/grant-evaluation?scope={scope}&path={path}&access={access}"
            ))
            .await;
        assert_eq!(v["valid"], valid, "{v}");
        assert_eq!(v["present"], present, "{v}");
        if valid {
            assert_eq!(v["reason"], "");
        } else {
            let key = match (scope, path) {
                ("document", "/opt/x") => "path_not_relative",
                (_, "..") => "path_not_a_directory_name",
                ("local", "/opt/nowhere") => "local_path_missing",
                _ => "protected",
            };
            let expected = if key == "protected" {
                let path = f.root.join("home").join(path).display().to_string();
                crate::i18n::t(
                    "en",
                    "safe_paths.protected_credentials",
                    &[("path", &path), ("protected", &path)],
                )
            } else {
                crate::i18n::t(
                    "en",
                    &format!("intelligences.agent_environment.grants.{key}"),
                    &[("path", path)],
                )
            };
            assert_eq!(v["reason"], expected);
        }
        assert!(!f.root.join("home/Projects/new").exists());
    }
    let home = f.root.join("home");
    assert_eq!(
        f.get(&format!(
            "/intelligences/grant-evaluation?scope=deny&path={}",
            home.display()
        ))
        .await["valid"],
        false
    );
    f.write(
        &f.config()
            .join("intelligences/cli_agent_filesystem_grants.yml"),
        "documents: [{path: shared, access: read}]\n",
    );
    for scope in ["document", "local", "deny"] {
        let path = if scope == "local" {
            home.join("shared/private").display().to_string()
        } else {
            "shared/private".into()
        };
        f.write(
            &f.workspace()
                .join(".guildbotics/local/cli_agent_filesystem_grants.yml"),
            if scope == "deny" {
                "{}"
            } else {
                "deny: [shared/private]"
            },
        );
        assert_eq!(
            f.get(&format!(
                "/intelligences/grant-evaluation?scope={scope}&path={path}"
            ))
            .await["valid"],
            false
        );
    }
}

#[tokio::test]
async fn invalid_member_ids_are_rejected_before_read_save_or_delete() {
    let f = fixture();
    f.init().await;
    f.write(&f.config().join("intelligences/keep"), "keep");
    let before = bytes_under(&f.config());
    for id in [
        "/private/keys",
        "../../keys",
        "a/b",
        "a%5Cb",
        "",
        "con",
        "aux",
        "nul",
        "com1",
        "lpt1",
    ] {
        let (status, _) = f
            .call(
                "GET",
                &format!("/config/intelligences?person_id={id}"),
                Value::Null,
            )
            .await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{id}");
        let (status,_) = f.save("PUT","/config/intelligences",json!({"person_id":if id == "a%5Cb" {"a\\b"} else {id},"inherit_team_defaults":true})).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{id}");
        assert_eq!(bytes_under(&f.config()), before);
    }
}

#[tokio::test]
async fn saved_brains_resolve_member_models_and_reject_cli_decision_override() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let form = json!({"model_mapping":{"judge":"models/openai/judge.yml"},"models":[{"path":"models/openai/judge.yml","provider":"openai","model_class":"agno.models.openai.OpenAIChat","parameters":{"id":"team-model"}}],"brain_mapping":[{"name":"chat_decision","brain_class":"guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain","engine":"llm","target":"judge"}]});
    let (status, body) = f.save("PUT", "/config/intelligences", form.clone()).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let mut member = form;
    member["person_id"] = json!("alice");
    member["models"][0]["parameters"]["id"] = json!("member-model");
    assert_eq!(
        f.save("PUT", "/config/intelligences", member).await.0,
        StatusCode::OK
    );
    let roots = gb_config::commands::CommandRoots {
        config: f.config(),
        templates: gb_base::home::python_package().join("templates"),
    };
    let resolved = gb_config::resolved_guest::resolve(
        &roots,
        &f.workspace(),
        &f.root.join("identity"),
        "alice",
    )
    .unwrap();
    assert_eq!(
        resolved["brains"]["chat_decision"]["definition"]["parameters"]["id"],
        "member-model"
    );
    assert!(!f.config().join("intelligences/decision.yml").exists());
    let (status,body) = f.save("PUT","/config/intelligences",json!({"brain_mapping":[{"name":"chat_decision","brain_class":"guildbotics.intelligences.brains.jev.JevBrain","engine":"jev","target":"jev-latest"}]})).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status,body) = f.save("PUT","/config/intelligences",json!({"person_id":"alice","brain_mapping":[{"name":"chat_decision","brain_class":"guildbotics.guest.cli_agent.CliAgentBrain","engine":"cli","target":"default"}]})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_decision");
    let resolved = gb_config::resolved_guest::resolve(
        &roots,
        &f.workspace(),
        &f.root.join("identity"),
        "alice",
    )
    .unwrap();
    assert_eq!(resolved["brains"]["chat_decision"]["kind"], "jev");
    assert_eq!(resolved["brains"]["chat_decision"]["slot"], "jev-latest");
}

#[tokio::test]
async fn team_environment_save_preserves_omitted_settings_and_member_cannot_write_it() {
    let f = fixture();
    f.init().await;
    std::fs::create_dir_all(f.root.join("home/.cache/uv")).unwrap();
    let before = bytes_under(&f.config().join("intelligences"));
    let form = json!({"filesystem_grants":{"documents":[{"path":"Projects/out","access":"read_write"}]},"local_grants":{"paths":[{"path":".cache/uv","access":"read_write"}],"deny":[f.root.join("private")]},"agent_environment":{"dns":{"nameservers":"host"}}});
    let (status, body) = f.save("PUT", "/config/intelligences", form.clone()).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    for (path, bytes) in before {
        assert_eq!(std::fs::read(path).unwrap(), bytes);
    }
    assert_eq!(body["intelligence"]["files"].as_array().unwrap().len(), 3);
    let read = f.get("/config/intelligences").await;
    assert_eq!(read["agent_environment"]["dns"]["nameservers"], "host");
    assert_eq!(
        read["local_grants"],
        json!({"paths":[{"path":".cache/uv","access":"read_write"}],"deny":[f.root.join("private")]})
    );
    let mut member = form;
    member["person_id"] = json!("alice");
    member["agent_environment"]["dns"]["nameservers"] = json!(["10.0.0.53"]);
    assert_eq!(
        f.save("PUT", "/config/intelligences", member).await.0,
        StatusCode::OK
    );
    assert_eq!(
        f.get("/config/intelligences?person_id=alice").await["agent_environment"],
        read["agent_environment"]
    );
    assert_eq!(
        f.save("PUT", "/config/intelligences", json!({})).await.0,
        StatusCode::OK
    );
    assert_eq!(
        f.get("/config/intelligences").await["filesystem_grants"],
        read["filesystem_grants"]
    );
    assert_eq!(
        f.get("/config/intelligences").await["agent_environment"],
        read["agent_environment"]
    );
}

#[tokio::test]
async fn protected_parent_and_missing_local_grants_refuse_the_whole_save() {
    let f = fixture();
    f.init().await;
    let inactive = f.root.join("data/workspace");
    gb_workspace::selection::register(&f.data(), &inactive).unwrap();
    for path in [
        inactive.parent().unwrap().display().to_string(),
        f.root.join("missing").display().to_string(),
    ] {
        let before = bytes_under(&f.config());
        let (status,body) = f.save("PUT","/config/intelligences",json!({"filesystem_grants":{"documents":[{"path":"safe-new","access":"read"}]},"local_grants":{"paths":[{"path":path,"access":"read"}]},"model_mapping":{"default":"must-not-save"}})).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
        assert_eq!(body["code"], "invalid_filesystem_grants");
        assert_eq!(bytes_under(&f.config()), before);
        assert!(!f.root.join("home/safe-new").exists());
        assert!(
            !f.workspace()
                .join(".guildbotics/local/cli_agent_filesystem_grants.yml")
                .exists()
        );
    }
}

#[tokio::test]
async fn malformed_environment_files_are_reported_at_the_editor() {
    let f = fixture();
    f.init().await;
    let declaration = f.config().join("intelligences/agent_environment.yml");
    f.write(&declaration, "packages: {}\ndns: {nameservers: host}\n");
    let (status, body) = f.call("GET", "/config/intelligences", Value::Null).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_agent_environment");
    std::fs::remove_file(declaration).unwrap();
    f.write(
        &f.config()
            .join("intelligences/cli_agent_filesystem_grants.yml"),
        "documents: [{path: /etc, access: read_write}]\n",
    );
    let (status, body) = f.call("GET", "/config/intelligences", Value::Null).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_filesystem_grants");
}

#[tokio::test]
async fn scope_environment_reads_include_shared_device_files_and_declaration() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/intelligences").await;
    assert_eq!(
        read["agent_environment"]["dns"]["nameservers"],
        json!(["1.1.1.1", "8.8.8.8"])
    );
    assert_eq!(
        read["agent_environment"]["resources"],
        json!({"memory_mib":4096,"cpus":2})
    );
    assert_eq!(read["agent_environment"]["network"]["mode"], "allowlist");
    assert!(!read["platform"].as_str().unwrap().is_empty());
    f.write(
        &f.config()
            .join("intelligences/cli_agent_filesystem_grants.yml"),
        "documents: [{path: Documents/shared, access: read}]\n",
    );
    f.write(
        &f.workspace()
            .join(".guildbotics/local/cli_agent_filesystem_grants.yml"),
        "paths: [{path: /opt/homebrew, access: read}]\ndeny: [/opt/homebrew/etc]\n",
    );
    f.write(&f.config().join("intelligences/agent_environment.yml"),"network: {mode: allowlist, allowed_domains: [registry.npmjs.org], allow_local_network: false}\ndns: {nameservers: [1.1.1.1]}\n");
    let read = f.get("/config/intelligences").await;
    assert_eq!(
        read["filesystem_grants"],
        json!({"documents":[{"path":"Documents/shared","access":"read"}]})
    );
    assert_eq!(
        read["local_grants"],
        json!({"paths":[{"path":"/opt/homebrew","access":"read"}],"deny":["/opt/homebrew/etc"]})
    );
    assert_eq!(
        read["agent_environment"]["network"]["allowed_domains"],
        json!(["registry.npmjs.org"])
    );
    assert_eq!(
        read["agent_environment"]["dns"]["nameservers"],
        json!(["1.1.1.1"])
    );
    let member = f.get("/config/intelligences?person_id=alice").await;
    for field in ["filesystem_grants", "local_grants", "agent_environment"] {
        assert_eq!(member[field], read[field]);
    }
}

#[tokio::test]
async fn setup_errors_keep_their_code_and_sentence() {
    let (status, body) = answer(failure(
        SetupError::text("intelligence_invalid", "Intelligence config invalid.").into(),
        &HeaderMap::new(),
    ))
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body,
        json!({"code":"intelligence_invalid","message":"Intelligence config invalid.","context":{}})
    );
}

#[tokio::test]
async fn native_definition_fields_and_empty_effort_are_not_refilled() {
    let f = fixture();
    f.init().await;
    f.add(agent("alice")).await;
    let path = f
        .config()
        .join("intelligences/cli_agents/codex/default.yml");
    f.write(
        &path,
        "effort: {high: {effort: high}}\nnetwork: {mode: unrestricted}\n",
    );
    let read = f.get("/config/intelligences").await;
    let native = read["cli_agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["name"] == "codex")
        .unwrap();
    assert_eq!(native["effort"], json!({"high":{"effort":"high"}}));
    assert!(native.get("network").is_none());
    let mut form = json!({"cli_agent_mapping":{"default":"cli_agents/codex/default.yml","writer":"cli_agents/codex/writer.yml"},"cli_agents":[{"path":"cli_agents/codex/default.yml","name":"codex","effort":{"high":{"model":"gpt-strong","effort":"high"}}},{"path":"cli_agents/codex/writer.yml","name":"codex"}]});
    assert_eq!(
        f.save("PUT", "/config/intelligences", form.clone()).await.0,
        StatusCode::OK
    );
    let stored = || gb_config::yaml_value(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert_eq!(
        stored(),
        json!({"parameters":{},"effort":{"high":{"model":"gpt-strong","effort":"high"}}})
    );
    form["cli_agents"][0]["effort"] = json!({});
    assert_eq!(
        f.save("PUT", "/config/intelligences", form.clone()).await.0,
        StatusCode::OK
    );
    assert_eq!(stored(), json!({"parameters":{},"effort":{}}));
    let read = f.get("/config/intelligences").await;
    assert!(
        read["cli_agents"]
            .as_array()
            .unwrap()
            .iter()
            .all(|a| a.get("network").is_none())
    );
    let codex = read["cli_agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["path"] == "cli_agents/codex/default.yml")
        .unwrap();
    assert_eq!(
        codex["effort_fields"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f["key"].as_str().unwrap())
            .collect::<std::collections::BTreeSet<_>>(),
        ["model", "effort"].into_iter().collect()
    );
    let roots = gb_config::commands::CommandRoots {
        config: f.config(),
        templates: gb_base::home::python_package().join("templates"),
    };
    let resolved = gb_config::resolved_guest::resolve(
        &roots,
        &f.workspace(),
        &f.root.join("identity"),
        "alice",
    )
    .unwrap();
    assert_eq!(
        resolved["brains"]["agent"]["definition"]["effort"],
        json!({})
    );
    form["cli_agents"][0]["effort"] = json!({"high":{"efort":"high"}});
    let (status, body) = f.save("PUT", "/config/intelligences", form).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_effort_settings");
    assert_eq!(stored(), json!({"parameters":{},"effort":{}}));
}
