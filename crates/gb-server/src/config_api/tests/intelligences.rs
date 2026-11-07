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
    assert_eq!(
        member["model_mapping"]["default"],
        "models/openai/gpt-5-mini.yml"
    );
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
    assert_eq!(
        f.read(&dir.join("brain_mapping.yml")),
        json!({"default":{"kind":"model","model":"anthropic"}})
    );
    let reread = f.get("/config/intelligences?person_id=alice").await;
    assert_eq!(reread["inherited"], false);
    assert_eq!(
        reread["brain_mapping"]
            .as_array()
            .unwrap()
            .iter()
            .find(|b| b["name"] == "default")
            .unwrap()["target"],
        "anthropic"
    );
    let result = f
        .save(
            "PUT",
            "/config/intelligences",
            json!({"person_id":"alice","inherit_team_defaults":true}),
        )
        .await;
    assert_eq!(result.0, StatusCode::OK, "{}", result.1);
    assert!(!dir.exists());
    let reset = f.get("/config/intelligences?person_id=alice").await;
    assert_eq!(reset["inherited"], true);
    assert_eq!(
        reset["model_mapping"]["default"],
        "models/openai/gpt-5-mini.yml"
    );
    assert_eq!(
        reset["brain_mapping"]
            .as_array()
            .unwrap()
            .iter()
            .find(|b| b["name"] == "default")
            .unwrap()["target"],
        "codex"
    );
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
    for (value, expected) in [
        (
            json!(["synthetic-private"]),
            StatusCode::UNPROCESSABLE_ENTITY,
        ),
        (json!(""), StatusCode::BAD_REQUEST),
        (json!("  "), StatusCode::BAD_REQUEST),
    ] {
        let (status, body) = f
            .call(
                "POST",
                "/intelligences/decisions/credential",
                json!({"value":value}),
            )
            .await;
        assert_eq!(status, expected, "{body}");
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
    assert_eq!(
        providers["providers"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["provider"] == "openai")
            .unwrap()["api_key_env"],
        "OPENAI_API_KEY"
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
        assert_eq!(v["scope"], scope);
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
    let denied = f
        .get(&format!(
            "/intelligences/grant-evaluation?scope=deny&path={}",
            home.display()
        ))
        .await;
    assert_eq!(denied["valid"], false);
    assert_eq!(denied["scope"], "deny");
    assert_eq!(
        denied["reason"],
        crate::i18n::t(
            "en",
            "intelligences.agent_environment.grants.deny_too_broad",
            &[("path", &home.display().to_string())]
        )
    );
    let (status, body) = f
        .call(
            "GET",
            "/intelligences/grant-evaluation?scope=other&path=Docs",
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{body}");
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
        let result = f
            .get(&format!(
                "/intelligences/grant-evaluation?scope={scope}&path={path}"
            ))
            .await;
        assert_eq!(result["valid"], false);
        assert_eq!(result["scope"], scope);
        let source = home.join("shared").display().to_string();
        let protected = home.join("shared/private").display().to_string();
        assert_eq!(
            result["reason"],
            crate::i18n::t(
                "en",
                "safe_paths.protected_local",
                &[("path", &source), ("protected", &protected)]
            )
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
    let shared_path = f
        .config()
        .join("intelligences/cli_agent_filesystem_grants.yml");
    let local_path = f
        .workspace()
        .join(".guildbotics/local/cli_agent_filesystem_grants.yml");
    let declaration_path = f.config().join("intelligences/agent_environment.yml");
    assert_eq!(
        body["intelligence"]["files"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["path"].as_str().unwrap().to_owned())
            .collect::<std::collections::BTreeSet<_>>(),
        [
            shared_path.display().to_string(),
            local_path.display().to_string(),
            declaration_path.display().to_string()
        ]
        .into_iter()
        .collect()
    );
    assert_eq!(f.read(&shared_path), form["filesystem_grants"]);
    assert_eq!(f.read(&local_path), form["local_grants"]);
    assert_eq!(
        f.read(&declaration_path),
        json!({"dns":{"nameservers":"host"},"resources":{"memory_mib":4096,"cpus":2},"network":{"mode":"deny","allowed_domains":[],"allow_local_network":false}})
    );
    assert!(!f.root.join("home/Projects/out").exists());
    let read = f.get("/config/intelligences").await;
    assert_eq!(read["agent_environment"]["dns"]["nameservers"], "host");
    assert_eq!(
        read["filesystem_grants"],
        json!({"documents":[{"path":"Projects/out","access":"read_write"}]})
    );
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
    let f = fixture();
    f.init().await;
    let before = bytes_under(&f.config());
    let (status, body) = f.save("PUT", "/config/intelligences", json!({"brain_mapping":[{"name":"chat_decision","brain_class":"guildbotics.guest.cli_agent.CliAgentBrain","engine":"cli","target":"default"}]})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_decision");
    assert!(
        body["message"]
            .as_str()
            .unwrap()
            .contains("not supported for chat_decision"),
        "{body}"
    );
    assert_eq!(bytes_under(&f.config()), before);
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
    assert!(
        body["message"].as_str().unwrap().contains("efort"),
        "{body}"
    );
    assert_eq!(stored(), json!({"parameters":{},"effort":{}}));
}

#[tokio::test]
async fn omitted_environment_values_do_not_create_files() {
    let f = fixture();
    f.init().await;
    let shared = f
        .config()
        .join("intelligences/cli_agent_filesystem_grants.yml");
    let local = f
        .workspace()
        .join(".guildbotics/local/cli_agent_filesystem_grants.yml");
    let declaration = f.config().join("intelligences/agent_environment.yml");
    for path in [&shared, &local, &declaration] {
        if path.exists() {
            std::fs::remove_file(path).unwrap();
        }
    }
    let (status, body) = f.save("PUT", "/config/intelligences", json!({})).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    for path in [&shared, &local, &declaration] {
        assert!(!path.exists(), "{}", path.display());
    }
}

#[tokio::test]
async fn grant_replacements_can_repair_each_broken_disk_file_and_both() {
    for (replace_shared, replace_local) in [(true, false), (false, true), (true, true)] {
        let f = fixture();
        f.init().await;
        let shared = f
            .config()
            .join("intelligences/cli_agent_filesystem_grants.yml");
        let local = f
            .workspace()
            .join(".guildbotics/local/cli_agent_filesystem_grants.yml");
        if replace_shared {
            f.write(&shared, "documents: [{path: '..', access: read}]\n");
        }
        if replace_local {
            f.write(&local, "paths: [{path: '..', access: read}]\n");
        }
        let mut form = json!({});
        if replace_shared {
            form["filesystem_grants"] = json!({"documents":[]});
        }
        if replace_local {
            form["local_grants"] = json!({"paths":[],"deny":[]});
        }
        let (status, body) = f.save("PUT", "/config/intelligences", form).await;
        assert_eq!(
            status,
            StatusCode::OK,
            "shared={replace_shared}, local={replace_local}: {body}"
        );
        if replace_shared {
            assert_eq!(f.read(&shared), json!({"documents":[]}));
        }
        if replace_local {
            assert_eq!(f.read(&local), json!({"paths":[],"deny":[]}));
        }
    }
}

#[tokio::test]
async fn malformed_grants_and_declarations_are_rejected_before_revision_comparison() {
    let f = fixture();
    f.init().await;
    let read = f.get("/config/intelligences").await;
    f.write(
        &f.config().join("intelligences/model_mapping.yml"),
        "default: models/openai/newer.yml\n",
    );
    let before = bytes_under(&f.workspace().join(".guildbotics"));
    for (field, value) in [
        ("filesystem_grants", json!([])),
        ("filesystem_grants", json!({"documents":[["Docs","read"]]})),
        ("local_grants", json!({"paths":[["Docs","read"]]})),
        (
            "agent_environment",
            json!({"dns":{"nameservers":"host"}, "network":[]}),
        ),
        (
            "agent_environment",
            json!({"dns":{"nameservers":"host"}, "image":["image",{}]}),
        ),
        ("local_grants", json!([])),
        (
            "local_grants",
            json!({"paths":[{"path":"/","access":"read"}]}),
        ),
        ("local_grants", json!({"deny":["/"]})),
        (
            "local_grants",
            json!({"paths":[{"path":r"a\..\b","access":"read"}]}),
        ),
        ("local_grants", json!({"deny":[r"a\..\b"]})),
        (
            "filesystem_grants",
            json!({"documents":[{"path":"Docs","access":"write"}]}),
        ),
        (
            "local_grants",
            json!({"paths":[{"path":"Docs","access":"write"}]}),
        ),
        ("filesystem_grants", json!({"unknown":true})),
        ("local_grants", json!({"unknown":true})),
        (
            "filesystem_grants",
            json!({"documents":[{"path":"/absolute","access":"read"}]}),
        ),
        (
            "agent_environment",
            json!({"dns":{"nameservers":["8.8.8.8x"]}}),
        ),
        ("agent_environment", json!({"dns":{"nameservers":[]}})),
        (
            "agent_environment",
            json!({"dns":{"nameservers":"host"},"unknown":true}),
        ),
        ("agent_environment", json!([])),
    ] {
        let mut form = json!({"expected_revisions":read["revisions"]});
        form[field] = value;
        let (status, body) = f.save("PUT", "/config/intelligences", form).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{field}: {body}");
        assert_eq!(body["code"], "validation_error");
        assert_eq!(bytes_under(&f.workspace().join(".guildbotics")), before);
    }
}

#[tokio::test]
async fn grant_preview_requires_workspace_and_preserves_absolute_local_spelling() {
    let empty = fixture_with(|_| Selected::default());
    let (status, body) = empty
        .call(
            "GET",
            "/intelligences/grant-evaluation?scope=document&path=Docs",
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["code"], "workspace_not_configured");
    let (status, body) = empty
        .call(
            "GET",
            "/intelligences/grant-evaluation?scope=document&path=/absolute",
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["valid"], false);
    assert_eq!(
        body["reason"],
        crate::i18n::t(
            "en",
            "intelligences.agent_environment.grants.path_not_relative",
            &[("path", "/absolute")]
        )
    );
    let f = fixture();
    f.init().await;
    let absolute = f.root.join("home/.cache/uv");
    std::fs::create_dir_all(&absolute).unwrap();
    let read = f
        .get(&format!(
            "/intelligences/grant-evaluation?scope=local&path={}",
            absolute.display()
        ))
        .await;
    assert_eq!(read["valid"], true);
    assert_eq!(read["present"], true);
    assert_eq!(read["path"], absolute.display().to_string());
}

#[tokio::test]
async fn member_save_reads_all_team_configuration_before_any_write() {
    for (path, content) in [
        ("brain_mapping.yml", "default: [oops"),
        (
            "agent_environment.yml",
            "dns: {nameservers: ['8.8.8.8x']}\n",
        ),
    ] {
        let f = fixture();
        f.init().await;
        f.write(&f.config().join("intelligences").join(path), content);
        let before = bytes_under(&f.config());
        let (status, body) = f.save("PUT", "/config/intelligences", json!({"person_id":"alice", "model_mapping":{"default":"models/openai/member.yml"}, "brain_mapping":[{"name":"default", "brain_class":"guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain", "engine":"llm", "target":"default"}]})).await;
        assert!(
            status.is_client_error() || status.is_server_error(),
            "{path}: {body}"
        );
        if path == "agent_environment.yml" {
            assert_eq!(body["code"], "invalid_agent_environment");
        }
        assert!(
            bytes_under(&f.config()) == before,
            "{path}: unexpected configuration write: {body}"
        );
        assert!(!f.config().join("team/members/alice/intelligences").exists());
    }
}

#[tokio::test]
async fn invalid_stored_declaration_and_grants_use_localized_editor_context() {
    let f = fixture();
    f.init().await;
    let declaration = f.config().join("intelligences/agent_environment.yml");
    let grants = f
        .config()
        .join("intelligences/cli_agent_filesystem_grants.yml");
    f.write(&declaration, "dns: {nameservers: ['8.8.8.8x']}\n");
    for language in ["en", "ja"] {
        let (status, body) = f
            .call_in(language, "GET", "/config/intelligences", Value::Null)
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["code"], "invalid_agent_environment");
        assert_eq!(
            body["message"],
            format!(
                "intelligences/agent_environment.yml: {}",
                crate::i18n::t(
                    language,
                    "intelligences.agent_environment.declaration.not_ipv4",
                    &[("nameserver", "8.8.8.8x")]
                )
            )
        );
    }
    std::fs::remove_file(declaration).unwrap();
    f.write(&grants, "unknown: true\n");
    for language in ["en", "ja"] {
        let (status, body) = f
            .call_in(language, "GET", "/config/intelligences", Value::Null)
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["code"], "invalid_filesystem_grants");
        let message = body["message"].as_str().unwrap();
        let where_ = "intelligences/cli_agent_filesystem_grants.yml";
        let prefix = crate::i18n::t(
            language,
            "intelligences.agent_environment.grants.invalid",
            &[("where", where_), ("error", "")],
        );
        assert!(message.starts_with(&prefix), "{body}");
        assert!(message.contains("unknown"), "{body}");
    }
}

#[tokio::test]
async fn omitted_invalid_local_grants_are_localized_on_put() {
    let f = fixture();
    f.init().await;
    f.write(
        &f.workspace()
            .join(".guildbotics/local/cli_agent_filesystem_grants.yml"),
        "unknown: true\n",
    );
    let before = bytes_under(&f.config());
    let (status, body) = f
        .save_in(
            "ja",
            "PUT",
            "/config/intelligences",
            json!({"filesystem_grants":{"documents":[]}}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_filesystem_grants");
    let prefix = crate::i18n::t(
        "ja",
        "intelligences.agent_environment.grants.invalid",
        &[
            ("where", "local/cli_agent_filesystem_grants.yml"),
            ("error", ""),
        ],
    );
    assert!(
        body["message"].as_str().unwrap().starts_with(&prefix),
        "{body}"
    );
    assert_eq!(bytes_under(&f.config()), before);
}

#[tokio::test]
async fn invalid_preview_syntax_is_reported_before_reading_workspace_registry() {
    let f = fixture();
    f.write(&f.data().join("workspaces.json"), "{broken registry");
    for language in ["en", "ja"] {
        let (status, body) = f
            .call_in(
                language,
                "GET",
                "/intelligences/grant-evaluation?scope=document&path=/absolute",
                Value::Null,
            )
            .await;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(body["valid"], false);
        assert_eq!(
            body["reason"],
            crate::i18n::t(
                language,
                "intelligences.agent_environment.grants.path_not_relative",
                &[("path", "/absolute")]
            )
        );
    }
    assert_eq!(
        std::fs::read_to_string(f.data().join("workspaces.json")).unwrap(),
        "{broken registry"
    );
}
