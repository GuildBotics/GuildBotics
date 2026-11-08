//! What the command screens offer and run (凍結版
//! `tests/guildbotics/app_api/test_runtime_commands.py`、`test_api.py` の
//! コマンドの endpoint、`test_models.py`). A run here never boots a microVM:
//! the device of the fixture refuses one, which still opens, records and ends
//! the run's trace.

use super::*;

fn commands(f: &Fixture) -> PathBuf {
    f.config().join("commands")
}

/// A workspace with a project in `language`, the active agent bot and no
/// sample command.
async fn workspace(language: &str) -> Fixture {
    let f = fixture();
    f.init_with(json!({ "language": language })).await;
    f.add(agent("bot")).await;
    std::fs::remove_dir_all(commands(&f)).unwrap();
    std::fs::create_dir_all(commands(&f)).unwrap();
    f
}

/// The project's services as `services` names them.
fn serve(f: &Fixture, services: Value) {
    let path = f.config().join("team/project.yml");
    let mut project = f.read(&path);
    project["services"] = services;
    f.write(&path, &project.to_string());
}

async fn options(f: &Fixture, uri: &str) -> Vec<Value> {
    f.get(uri).await["options"].as_array().unwrap().clone()
}

fn option<'a>(options: &'a [Value], command: &str) -> &'a Value {
    options
        .iter()
        .find(|o| o["command"] == command)
        .unwrap_or_else(|| panic!("{command} is not listed: {options:?}"))
}

fn names(options: &[Value]) -> Vec<String> {
    options
        .iter()
        .map(|o| o["command"].as_str().unwrap().to_owned())
        .collect()
}

fn kinds(option: &Value) -> Vec<String> {
    option["requirements"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["kind"].as_str().unwrap().to_owned())
        .collect()
}

fn requirement<'a>(option: &'a Value, kind: &str) -> &'a Value {
    option["requirements"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["kind"] == kind)
        .unwrap()
}

fn arguments(option: &Value) -> Vec<(String, String, bool, String)> {
    option["arguments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| {
            (
                a["name"].as_str().unwrap().into(),
                a["kind"].as_str().unwrap().into(),
                a["required"].as_bool().unwrap(),
                a["default"].as_str().unwrap().into(),
            )
        })
        .collect()
}

fn arg(name: &str, kind: &str, required: bool, default: &str) -> (String, String, bool, String) {
    (name.into(), kind.into(), required, default.into())
}

#[tokio::test]
async fn a_broken_member_file_is_the_persons_to_fix() {
    let f = workspace("en").await;
    let member = f.config().join("team/members/bot/member.yml");
    f.write(&member, "[broken\n");
    for (method, uri, body) in [
        ("GET", "/commands/options", Value::Null),
        ("GET", "/commands/routine-options", Value::Null),
        ("POST", "/commands/run", json!({"command": "hello"})),
    ] {
        let (status, answer) = f.call(method, uri, body).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{uri}: {answer}");
        assert_eq!(answer["code"], "member_config_invalid", "{uri}");
        assert_eq!(answer["context"], json!({"path": member}), "{uri}");
    }
}

#[tokio::test]
async fn the_option_endpoints_answer_the_frozen_shape() {
    let f = workspace("en").await;
    f.write(
        &commands(&f).join("hello.md"),
        "---\nbrain: none\n---\nHi.\n",
    );
    let listed = f.get("/commands/options").await;
    let hello = &listed["options"][0];
    let fields: Vec<_> = hello.as_object().unwrap().keys().cloned().collect();
    assert_eq!(
        fields,
        [
            "command",
            "label",
            "description",
            "category",
            "source",
            "path",
            "arguments",
            "inputs",
            "routine_eligible",
            "requirements"
        ]
    );
    assert_eq!(hello["command"], "hello");
    assert_eq!(hello["label"], "Hello");
    assert_eq!(hello["routine_eligible"], true);
    assert_eq!(hello["path"], json!(commands(&f).join("hello.md")));
    f.write(
        &commands(&f).join("hello.md"),
        "---\nbrain: none\nroutine: true\n---\nHi.\n",
    );
    let routine = f.get("/commands/routine-options").await;
    assert_eq!(
        names(routine["options"].as_array().unwrap()),
        ["hello", "workflows/ticket_driven_workflow"]
    );
    assert_eq!(
        routine["default_command"],
        "workflows/ticket_driven_workflow"
    );
}

#[tokio::test]
async fn the_command_options_list_no_bundled_template_by_itself() {
    let f = workspace("en").await;
    assert_eq!(options(&f, "/commands/options").await, Vec::<Value>::new());
    std::fs::remove_dir_all(commands(&f)).unwrap();
    assert_eq!(options(&f, "/commands/options").await, Vec::<Value>::new());
    assert!(!commands(&f).exists(), "listing creates nothing");
}

#[tokio::test]
async fn an_option_is_the_members_own_command_before_the_shared_one_in_the_projects_language() {
    let f = workspace("ja").await;
    f.add(agent("alice")).await;
    f.write(
        &commands(&f).join("greet.md"),
        "---\nname: Shared Greet\n---\n",
    );
    f.write(
        &f.config().join("team/members/bot/commands/greet.md"),
        "---\nname: Member Greet\n---\n",
    );
    f.write(
        &f.config().join("team/members/alice/commands/private.md"),
        "---\nname: Private\n---\n",
    );
    for (file, name) in [
        ("note.md", "Plain Note"),
        ("note.en.md", "English Note"),
        ("note.ja.md", "Japanese Note"),
    ] {
        f.write(
            &commands(&f).join(file),
            &format!("---\nname: {name}\n---\n"),
        );
    }
    f.write(
        &f.root.join("home/.guildbotics/config/commands/greet.md"),
        "---\nname: Home Greet\n---\n",
    );
    let bot = options(&f, "/commands/options?person=bot").await;
    assert_eq!(names(&bot), ["greet", "note"]);
    assert_eq!(option(&bot, "greet")["label"], "Member Greet");
    assert_eq!(option(&bot, "greet")["source"], "workspace");
    assert_eq!(option(&bot, "note")["label"], "Japanese Note");
    let alice = options(&f, "/commands/options?person=alice").await;
    assert_eq!(names(&alice), ["greet", "note", "private"]);
    assert_eq!(option(&alice, "greet")["label"], "Shared Greet");
    let f = workspace("en").await;
    let path = f.config().join("team/project.yml");
    let mut project = f.read(&path);
    project["language"] = json!("de");
    f.write(&path, &project.to_string());
    for (file, name) in [("note.md", "Plain Note"), ("note.en.md", "English Note")] {
        f.write(
            &commands(&f).join(file),
            &format!("---\nname: {name}\n---\n"),
        );
    }
    assert_eq!(
        option(&options(&f, "/commands/options").await, "note")["label"],
        "English Note"
    );
}

#[tokio::test]
async fn an_option_describes_its_label_category_arguments_and_inputs() {
    let f = workspace("en").await;
    f.write(
        &commands(&f).join("workflows/demo.py"),
        "COMMAND_METADATA = {\"requires\": [\"ticket_service\"]}\n\n\nasync def main(context, title, count='3', *, dry_run='False'):\n    \"\"\"Run a demo workflow.\"\"\"\n",
    );
    f.write(
        &commands(&f).join("yaml_task.yml"),
        "description: Convert ${1} using ${format}.\ncommands:\n  - print: done\n",
    );
    f.write(
        &commands(&f).join("declared.md"),
        "---\nargs:\n  file:\n    required: true\n  language:\n    default: English\n---\n${file} ${language} ${style}\n",
    );
    f.write(
        &commands(&f).join("placeholders.md"),
        "---\ndescription: ${1} ${target}\n---\n${1} ${2} {{ extra_note }}\n",
    );
    f.write(
        &commands(&f).join("system.md"),
        "---\nbrain: none\nroutine: true\ncommands:\n  - script: uname -s\n---\n{system__1}\n",
    );
    f.write(
        &commands(&f).join("sub/system.md"),
        "---\nbrain: none\nroutine: true\ncommands:\n  - script: uname -s\n---\n{system__1}\n",
    );
    f.write(
        &commands(&f).join("inputs.md"),
        "---\ninputs:\n  defined_args: hidden\n  extra_args: optional\n  message: required\n---\n",
    );
    f.write(
        &commands(&f).join("sometimes.md"),
        "---\ninputs:\n  message: sometimes\n---\n",
    );
    let listed = options(&f, "/commands/options").await;
    let demo = option(&listed, "workflows/demo");
    assert_eq!(demo["label"], "Demo");
    assert_eq!(demo["description"], "Run a demo workflow.");
    assert_eq!(demo["category"], "workflow");
    assert_eq!(
        arguments(demo),
        [
            arg("title", "positional", true, ""),
            arg("count", "positional", false, "3"),
            arg("dry_run", "keyword", false, "False"),
        ]
    );
    assert_eq!(kinds(demo), ["environment", "ticket_service"]);
    assert_eq!(
        arguments(option(&listed, "yaml_task"))
            .into_iter()
            .map(|a| (a.0, a.1))
            .collect::<Vec<_>>(),
        [
            ("1".into(), "positional".into()),
            ("format".into(), "keyword".into())
        ]
    );
    assert_eq!(
        arguments(option(&listed, "declared")),
        [
            arg("file", "keyword", true, ""),
            arg("language", "keyword", false, "English"),
            arg("style", "keyword", true, ""),
        ]
    );
    assert_eq!(
        arguments(option(&listed, "placeholders"))
            .into_iter()
            .map(|a| (a.0, a.1))
            .collect::<Vec<_>>(),
        [
            ("1".into(), "positional".into()),
            ("2".into(), "positional".into()),
            ("extra_note".into(), "keyword".into()),
            ("target".into(), "keyword".into()),
        ]
    );
    let routines = options(&f, "/commands/routine-options").await;
    for command in ["system", "sub/system"] {
        assert_eq!(
            option(&listed, command)["arguments"],
            json!([]),
            "{command}"
        );
        assert_eq!(
            option(&routines, command)["arguments"],
            json!([]),
            "{command}"
        );
        assert_eq!(option(&routines, command)["routine_eligible"], true);
        assert_eq!(option(&listed, command)["category"], "custom");
    }
    assert_eq!(
        option(&listed, "inputs")["inputs"],
        json!({"defined_args": "hidden", "extra_args": "optional", "message": "required"})
    );
    assert_eq!(
        option(&listed, "sometimes")["inputs"],
        json!({"defined_args": "auto", "extra_args": "hidden", "message": "optional"})
    );
}

#[tokio::test]
async fn an_unreadable_command_is_listed_with_empty_metadata() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("ok.md"), "---\nbrain: none\n---\n");
    f.write(&commands(&f).join("broken.yml"), "commands: [\n");
    f.write(&commands(&f).join("broken.py"), "def main(:\n");
    let listed = options(&f, "/commands/options").await;
    assert_eq!(names(&listed), ["broken", "ok"]);
    let broken = option(&listed, "broken");
    assert!(broken["path"].as_str().unwrap().ends_with("broken.py"));
    assert_eq!(kinds(broken), ["environment"]);
    assert_eq!(broken["arguments"], json!([]));
}

#[tokio::test]
async fn an_option_needs_what_its_brain_its_declaration_and_its_subcommands_run() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("plain.md"), "Explain.\n");
    f.write(&commands(&f).join("agent.md"), "---\nbrain: agent\n---\n");
    f.write(&commands(&f).join("none.md"), "---\nbrain: ' None '\n---\n");
    f.write(
        &commands(&f).join("cli-task.md"),
        "---\nbrain: workspace_actor\n---\n",
    );
    f.write(
        &commands(&f).join("nested.yml"),
        "commands:\n  - command: cli-task file=README.md\n  - prompt: Explain the summary.\n",
    );
    f.write(
        &commands(&f).join("scripted.py"),
        "COMMAND_METADATA = {\"requires\": [\"cli_agent\"]}\n\n\ndef main(context):\n    return ''\n",
    );
    f.write(
        &f.config().join("intelligences/brain_mapping.yml"),
        "default:\n  kind: model\n  model: default\nworkspace_actor:\n  kind: ai_cli\n  cli_agent: default\n",
    );
    f.write(
        &commands(&f).join("edit.md"),
        "---\nbrain: workspace_actor\n---\n",
    );
    f.write(
        &commands(&f).join("inline-edit.yml"),
        "commands:\n  - prompt: Edit.\n    brain: workspace_actor\n",
    );
    f.write(&commands(&f).join("unmapped.md"), "---\nbrain: cli\n---\n");
    f.write(&commands(&f).join("loop.yml"), "commands:\n  - loop\n");
    f.write(&commands(&f).join("unreadable.md"), "---\nbrain: [\n---\n");
    f.write(
        &commands(&f).join("named.yml"),
        "commands:\n  - command: null\n    name: cli-task\n  - path: ''\n    name: plain\n",
    );
    let listed = options(&f, "/commands/options").await;
    for (command, expected) in [
        ("plain", vec!["environment", "llm"]),
        ("agent", vec!["environment", "llm"]),
        ("none", vec!["environment"]),
        ("nested", vec!["cli_agent", "environment", "llm"]),
        ("scripted", vec!["cli_agent", "environment"]),
        ("edit", vec!["cli_agent", "environment"]),
        ("inline-edit", vec!["cli_agent", "environment"]),
        ("unmapped", vec!["environment", "llm"]),
        ("loop", vec!["environment"]),
        ("unreadable", vec!["environment", "llm"]),
        ("named", vec!["cli_agent", "environment", "llm"]),
    ] {
        assert_eq!(kinds(option(&listed, command)), expected, "{command}");
    }
    // A mapping holding a slot it cannot use is read as no mapping at all.
    for (file, text) in [
        (
            "brain_mapping.yml",
            "default:\n  kind: model\n  model: default\nworkspace_actor:\n  kind: ai_cli\n",
        ),
        (
            "brain_mapping.yml",
            "default:\n  kind: bogus\nworkspace_actor:\n  kind: ai_cli\n  cli_agent: default\n",
        ),
        (
            "cli_agent_mapping.yml",
            "default: cli_agents/codex/default.yml\nother: cli_agents/nope/a.yml\n",
        ),
    ] {
        let path = f.config().join("intelligences").join(file);
        let kept = std::fs::read_to_string(&path).ok();
        f.write(&path, text);
        let listed = options(&f, "/commands/options").await;
        assert_eq!(kinds(option(&listed, "edit")), ["environment"], "{text}");
        match kept {
            Some(kept) => f.write(&path, &kept),
            None => std::fs::remove_file(&path).unwrap(),
        }
    }
    // The workspace's mapping has no `agent` slot: it runs an inference model.
    std::fs::remove_file(f.config().join("intelligences/brain_mapping.yml")).unwrap();
    let listed = options(&f, "/commands/options").await;
    assert_eq!(
        kinds(option(&listed, "agent")),
        ["cli_agent", "environment"]
    );
}

#[tokio::test]
async fn a_requirement_is_met_as_the_device_and_the_project_provide_it() {
    let f = workspace("en").await;
    f.write(
        &commands(&f).join("tickets.py"),
        "COMMAND_METADATA = {\"requires\": [\"ticket_service\", \"chat_service\"]}\n\n\ndef main(context):\n    return ''\n",
    );
    f.write(&commands(&f).join("agent.md"), "---\nbrain: agent\n---\n");
    f.write(
        &commands(&f).join("scripted.py"),
        "COMMAND_METADATA = {\"requires\": [\"cli_agent\"]}\n\n\ndef main(context):\n    return ''\n",
    );
    for (services, ticket, chat) in [
        (json!({}), false, false),
        (json!({"ticket_service": {"name": "branvil"}}), true, false),
        (
            json!({"ticket_service": {"name": "local"}, "chat_service": {"name": "slack"}}),
            true,
            true,
        ),
        (
            json!({"ticket_service": {"name": 1}, "chat_service": {"name": ""}}),
            true,
            false,
        ),
    ] {
        serve(&f, services);
        let listed = options(&f, "/commands/options").await;
        let tickets = option(&listed, "tickets");
        for (kind, met) in [("ticket_service", ticket), ("chat_service", chat)] {
            assert_eq!(requirement(tickets, kind)["satisfied"], met, "{kind}");
            assert_eq!(requirement(tickets, kind)["message"], "");
        }
    }
    let listed = options(&f, "/commands/options").await;
    // The fixture's device has no snapshot and no tool login.
    let environment = requirement(option(&listed, "agent"), "environment");
    assert_eq!(environment["satisfied"], false);
    assert_ne!(environment["message"], "");
    let tool = requirement(option(&listed, "agent"), "cli_agent");
    assert_eq!(tool["satisfied"], false);
    let cli = f.root.join("home/.guildbotics/bin/guildbotics");
    assert_eq!(
        tool["message"],
        crate::i18n::t(
            "en",
            "intelligences.agent_environment.tool.credentials_missing",
            &[
                ("tool", "Codex"),
                (
                    "command",
                    &gb_base::shell::managed_cli_command(&cli, &["environment", "login", "codex"])
                ),
            ],
        )
    );
    assert_eq!(
        requirement(option(&listed, "scripted"), "cli_agent"),
        &json!({"kind": "cli_agent", "satisfied": true, "message": ""})
    );
    // A saved login is met, though a past turn failed to authenticate.
    f.write(
        &f.config().join("intelligences/cli_agent_mapping.yml"),
        "default: cli_agents/claude/default.yml\n",
    );
    f.claude_logged_in();
    let listed = options(&f, "/commands/options").await;
    assert_eq!(
        requirement(option(&listed, "agent"), "cli_agent"),
        &json!({"kind": "cli_agent", "satisfied": true, "message": ""})
    );
}

#[tokio::test]
async fn a_llm_requirement_is_met_by_a_providers_key() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("plain.md"), "Explain.\n");
    let met = |listed: &[Value]| requirement(option(listed, "plain"), "llm")["satisfied"].clone();
    assert_eq!(met(&options(&f, "/commands/options").await), false);
    let keys = gb_setup::provider_env_keys(
        &gb_base::home::python_package().join("templates"),
        &f.config().join("intelligences/models"),
    );
    let key = keys.values().next().unwrap();
    f.repository()
        .workspace_secrets(f.device())
        .unwrap()
        .set(key, "value")
        .unwrap();
    assert_eq!(met(&options(&f, "/commands/options").await), true);
}

#[tokio::test]
async fn an_unknown_person_is_refused_with_the_members_available() {
    let f = workspace("en").await;
    for uri in [
        "/commands/options?person=missing",
        "/commands/routine-options?person=missing",
    ] {
        let (status, answer) = f.call("GET", uri, Value::Null).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{uri}");
        assert_eq!(answer["code"], "person_not_found");
        assert_eq!(
            answer["message"],
            crate::i18n::t(
                "en",
                "app_api.errors.person_not_found.plain",
                &[("person", "missing")]
            )
        );
        assert_eq!(
            answer["context"],
            json!({"identifier": "missing", "available": ["bot"]})
        );
    }
}

#[tokio::test]
async fn routine_options_list_the_routines_with_their_eligibility_and_default() {
    let f = workspace("ja").await;
    let routine = |name: &str, front: &str| {
        f.write(
            &commands(&f).join(format!("{name}.md")),
            &format!("---\nroutine: true\nbrain: none\n{front}---\n${{input}}\n"),
        );
    };
    routine("needs_input", "");
    routine("message", "inputs:\n  message: required\n");
    routine("hidden", "inputs:\n  defined_args: hidden\n");
    f.write(
        &commands(&f).join("defaulted.md"),
        "---\nroutine: true\nbrain: none\nargs:\n  language:\n    default: English\n---\n${language}\n",
    );
    f.write(
        &commands(&f).join("my_routine.py"),
        "COMMAND_METADATA = {\"name\": {\"en\": \"My Routine\", \"ja\": \"私の巡回\"}, \"description\": {\"en\": \"Runs.\", \"ja\": \"私の巡回処理を実行します。\"}, \"routine\": True}\n\n\ndef main(context):\n    \"\"\"Docstring.\"\"\"\n",
    );
    f.write(
        &commands(&f).join("summarize.md"),
        "---\nbrain: none\n---\n",
    );
    let listed = options(&f, "/commands/routine-options").await;
    assert_eq!(
        names(&listed),
        [
            "defaulted",
            "hidden",
            "message",
            "my_routine",
            "needs_input",
            "workflows/ticket_driven_workflow"
        ]
    );
    for (command, eligible) in [
        ("needs_input", false),
        ("message", false),
        ("hidden", true),
        ("defaulted", true),
        ("my_routine", true),
    ] {
        assert_eq!(
            option(&listed, command)["routine_eligible"],
            eligible,
            "{command}"
        );
    }
    let mine = option(&listed, "my_routine");
    assert_eq!(mine["label"], "私の巡回");
    assert_eq!(mine["description"], "私の巡回処理を実行します。");
    let ticket = option(&listed, "workflows/ticket_driven_workflow");
    assert_eq!(ticket["source"], "template");
    assert_eq!(ticket["category"], "workflow");
    assert_eq!(ticket["label"], "チケット駆動ワークフロー");
    assert_eq!(
        ticket["description"],
        "対応可能なチケットまたは PR を1件取得し、AI CLIツールへ委譲します。"
    );
    assert!(names(&options(&f, "/commands/options").await).contains(&"summarize".into()));
    let routines = f.get("/commands/routine-options").await;
    assert_eq!(
        routines["default_command"],
        "workflows/ticket_driven_workflow"
    );
    // A workspace's ticket workflow is the one listed and the default.
    f.write(
        &commands(&f).join("workflows/ticket_driven_workflow.md"),
        "---\nname: Local Ticket\nroutine: true\nbrain: none\n---\n",
    );
    let listed = options(&f, "/commands/routine-options").await;
    let ticket: Vec<_> = listed
        .iter()
        .filter(|o| o["command"] == "workflows/ticket_driven_workflow")
        .collect();
    assert_eq!(ticket.len(), 1);
    assert_eq!(ticket[0]["source"], "workspace");
    assert_eq!(ticket[0]["label"], "Local Ticket");
}

#[tokio::test]
async fn the_routine_default_is_the_first_eligible_routine_or_none() {
    let f = workspace("en").await;
    f.write(
        &commands(&f).join("workflows/ticket_driven_workflow.md"),
        "---\nroutine: true\nbrain: none\n---\n${needed}\n",
    );
    f.write(
        &commands(&f).join("zeta.md"),
        "---\nroutine: true\nbrain: none\n---\n",
    );
    let routines = f.get("/commands/routine-options").await;
    assert_eq!(routines["default_command"], "zeta");
    std::fs::remove_file(commands(&f).join("zeta.md")).unwrap();
    let routines = f.get("/commands/routine-options").await;
    assert_eq!(routines["default_command"], "");
}

/// The execution status of the shared file `relative` as `revision`.
async fn status_of(f: &Fixture, relative: &str, revision: &str, person: &str) -> Value {
    let id = gb_config::command_files::file_id(relative);
    let person = if person.is_empty() {
        String::new()
    } else {
        format!("&person={person}")
    };
    f.get(&format!(
        "/commands/files/{id}/execution-status?expected_revision={revision}{person}"
    ))
    .await
}

fn revision_of(path: &Path) -> String {
    gb_config::command_files::revision(&std::fs::read(path).unwrap())
}

#[tokio::test]
async fn execution_status_matches_the_effective_file_with_its_requirements() {
    let f = workspace("en").await;
    let greet = commands(&f).join("greet.md");
    f.write(&greet, "---\nbrain: none\n---\nHi.\n");
    let status = status_of(&f, "greet.md", &revision_of(&greet), "bot").await;
    assert_eq!(status["blocking_code"], "command_requirement_missing");
    assert_eq!(status["matches_selected_file"], false);
    assert_eq!(status["blocking_context"], json!({}));
    assert_eq!(kinds(&status), ["environment"], "the device refuses to run");
    // What blocks only the environment's readiness is a requirement, not the file.
    let status = status_of(&f, "greet.md", "stale", "bot").await;
    assert_eq!(
        status,
        json!({"matches_selected_file": false, "requirements": [],
            "blocking_code": "command_file_changed",
            "blocking_context": {"current_revision": revision_of(&greet)}})
    );
}

#[tokio::test]
async fn execution_status_blocks_a_source_that_does_not_validate() {
    let f = workspace("en").await;
    let broken = commands(&f).join("broken.md");
    for (bytes, context) in [
        (
            b"---\nargs:\n  - name: text\n---\n".to_vec(),
            json!({"message": "Command 'args' must be a mapping."}),
        ),
        (
            b"\xff".to_vec(),
            json!({"message": "Command source must be valid UTF-8.", "reason": "invalid_utf8"}),
        ),
        (
            b"---\nargs: [\n---\n".to_vec(),
            json!({"message": "Command frontmatter is not valid YAML.", "line": "2", "column": "1"}),
        ),
        (
            b"---\nrequires: [network]\n---\n".to_vec(),
            json!({"message": "Command 'requires' must be a list of: llm, cli_agent, code_hosting_service, ticket_service, chat_service."}),
        ),
    ] {
        std::fs::write(&broken, &bytes).unwrap();
        let status = status_of(&f, "broken.md", &revision_of(&broken), "bot").await;
        assert_eq!(status["blocking_code"], "command_file_invalid_source");
        assert_eq!(status["requirements"], json!([]));
        assert_eq!(status["blocking_context"]["message"], context["message"]);
        if context.get("reason").is_some() {
            assert_eq!(status["blocking_context"], context);
        }
        if context.get("line").is_some() {
            assert!(status["blocking_context"]["line"].is_string(), "{status}");
            assert!(status["blocking_context"]["column"].is_string(), "{status}");
        }
    }
}

#[tokio::test]
async fn execution_status_reports_a_member_override_and_evaluates_the_default_member() {
    let f = workspace("en").await;
    let greet = commands(&f).join("greet.md");
    f.write(&greet, "---\nbrain: none\n---\n");
    f.write(
        &f.config().join("team/members/bot/commands/greet.md"),
        "---\nbrain: none\n---\n",
    );
    for person in ["bot", "Test", ""] {
        let status = status_of(&f, "greet.md", &revision_of(&greet), person).await;
        assert_eq!(status["blocking_code"], "command_file_shadowed", "{person}");
        assert_eq!(
            status["blocking_context"],
            json!({"shadow_source": "member"})
        );
        assert_eq!(status["requirements"], json!([]));
    }
    let status = status_of(&f, "greet.md", &revision_of(&greet), "ghost").await;
    assert_eq!(
        status,
        json!({"matches_selected_file": false, "requirements": [],
            "blocking_code": "person_not_found", "blocking_context": {"identifier": "ghost"}})
    );
    let (status, answer) = f
        .call(
            "GET",
            &format!(
                "/commands/files/{}/execution-status",
                gb_config::command_files::file_id("greet.md")
            ),
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{answer}");
}

async fn run(f: &Fixture, body: Value) -> (StatusCode, Value) {
    f.call("POST", "/commands/run", body).await
}

#[tokio::test]
async fn a_run_refused_for_its_member_publishes_a_failed_event_without_a_trace() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("demo.md"), "---\nbrain: none\n---\n");
    let (status, answer) = run(&f, json!({"command": "demo", "person": "ghost"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(answer["code"], "person_not_found");
    let failed = f.events("command.failed");
    assert_eq!(failed.len(), 1);
    assert_eq!(failed[0]["trace_id"], Value::Null);
    let index = std::fs::read_to_string(
        f.workspace()
            .join(".guildbotics/local/run/diagnostics.jsonl"),
    )
    .unwrap();
    let recorded: Vec<Value> = index
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|record| record["type"] == "command.failed")
        .collect();
    assert_eq!(recorded.len(), 1, "the refusal is recorded without a trace");
    assert!(recorded[0]["trace_id"].is_null());
    assert_eq!(
        failed[0]["payload"],
        json!({"command": "demo", "code": "person_not_found", "identifier": "ghost", "available": ["bot (Test)"]})
    );
    let f = workspace("en").await;
    f.write(&commands(&f).join("demo.md"), "---\nbrain: none\n---\n");
    f.add(json!({"person_id": "alice", "person_name": "Alice", "person_type": "agent", "is_active": false})).await;
    let mut bot = agent("bot");
    bot["is_active"] = json!(false);
    f.save("PUT", "/config/members/bot", bot).await;
    let (status, answer) = run(&f, json!({"command": "demo"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{answer}");
    assert_eq!(answer["code"], "person_selection_required");
    assert_eq!(
        answer["message"],
        crate::i18n::t(
            "en",
            "app_api.errors.person_selection_required",
            &[("available", "alice, bot (Test)")]
        )
    );
    assert_eq!(
        answer["context"],
        json!({"available": ["alice", "bot (Test)"]})
    );
    assert_eq!(
        f.events("command.failed")[0]["payload"],
        json!({"command": "demo", "code": "person_selection_required", "available": ["alice", "bot (Test)"]})
    );
    assert!(f.events("command.started").is_empty(), "no run started");
}

#[tokio::test]
async fn a_writing_run_while_another_holds_the_manual_slot_is_a_conflict() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("write.md"), "---\nbrain: none\n---\n");
    f.write(
        &commands(&f).join("look.md"),
        "---\nbrain: none\nread_only: true\n---\n",
    );
    *f.api.manual.lock().unwrap() = Some("inflight-id".into());
    let (status, answer) = run(&f, json!({"command": "write", "person": "bot"})).await;
    assert_eq!(status, StatusCode::CONFLICT, "{answer}");
    assert_eq!(answer["code"], "command_already_running");
    assert_eq!(
        answer["message"],
        crate::i18n::t("en", "app_api.errors.command_already_running", &[])
    );
    assert_eq!(answer["context"], json!({"trace_id": "inflight-id"}));
    assert!(f.events("command.started").is_empty());
    assert!(f.events("command.failed").is_empty());
    // A read-only command does not take the place: it runs, as far as the
    // device lets it.
    let (status, answer) = run(&f, json!({"command": "look", "person": "bot"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{answer}");
    assert_eq!(answer["code"], "command_error");
    assert_eq!(f.events("command.started").len(), 1);
    assert_eq!(*f.api.manual.lock().unwrap(), Some("inflight-id".into()));
}

#[tokio::test]
async fn a_run_records_and_publishes_its_start_and_failure_and_releases_its_place() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("demo.md"), "---\nbrain: none\n---\n");
    // The first names no member: the team's default runs it.
    for body in [
        json!({"command": "demo"}),
        json!({"command": "demo", "person": "bot"}),
    ] {
        let (status, answer) = run(&f, body).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{answer}");
        assert_eq!(answer["code"], "command_error");
        let trace = answer["context"]["trace_id"].as_str().unwrap().to_owned();
        assert_eq!(*f.api.manual.lock().unwrap(), None, "the place is released");
        let started = f.events("command.started").pop().unwrap();
        let failed = f.events("command.failed").pop().unwrap();
        assert_eq!(started["trace_id"], trace);
        assert_eq!(started["source"], "manual");
        assert_eq!(started["attributes"]["run.entry"], "command");
        assert_eq!(
            started["payload"],
            json!({"command": "demo", "person": "bot"})
        );
        assert_eq!(failed["trace_id"], trace);
        assert_eq!(
            failed["payload"],
            json!({"command": "demo", "person": "bot", "error_type": "CommandError",
                "code": "", "message": answer["message"]})
        );
        let listed = f.get("/diagnostics/traces").await;
        let summary = listed["traces"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["trace_id"] == trace)
            .unwrap();
        assert_eq!(
            (
                &summary["source"],
                &summary["person_id"],
                &summary["command"]
            ),
            (&json!("manual"), &json!("bot"), &json!("demo"))
        );
        let detail = f.get(&format!("/diagnostics/traces/{trace}")).await;
        let recorded: Vec<_> = detail["records"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|r| r["type"] == "command.failed")
            .collect();
        assert_eq!(recorded.len(), 1, "recorded once: {detail}");
    }
}

#[tokio::test]
async fn a_guarded_run_must_run_exactly_the_selected_file_as_it_was_read() {
    let f = workspace("en").await;
    let greet = commands(&f).join("greet.md");
    f.write(&greet, "---\nbrain: none\n---\n");
    f.write(&commands(&f).join("other.md"), "---\nbrain: none\n---\n");
    let id = gb_config::command_files::file_id("greet.md");
    let guarded = |command: &str, revision: &str, person: Option<&str>| {
        json!({"command": command, "person": person, "expected_command_file_id": id,
            "expected_command_file_revision": revision})
    };
    let current = revision_of(&greet);
    for (body, code, context) in [
        (
            guarded("greet", "stale", Some("bot")),
            "command_file_changed",
            json!({"current_revision": current}),
        ),
        (
            guarded("other", &current, Some("bot")),
            "command_file_shadowed",
            json!({"shadow_source": "workspace"}),
        ),
    ] {
        let (status, answer) = run(&f, body).await;
        assert_eq!(status, StatusCode::CONFLICT, "{answer}");
        assert_eq!(answer["code"], code);
        assert_eq!(answer["context"], context);
        assert_eq!(
            answer["message"],
            crate::i18n::t("en", "app_api.errors.command_not_runtime_target", &[])
        );
    }
    f.write(
        &f.config().join("team/members/bot/commands/greet.md"),
        "---\nbrain: none\n---\n",
    );
    for person in [Some("bot"), None] {
        let (status, answer) = run(&f, guarded("greet", &current, person)).await;
        assert_eq!(status, StatusCode::CONFLICT, "{answer}");
        assert_eq!(answer["code"], "command_file_shadowed");
        assert_eq!(answer["context"], json!({"shadow_source": "member"}));
    }
    assert!(
        f.events("command.started").is_empty(),
        "no guarded run started"
    );
}

#[tokio::test]
async fn a_guarded_run_of_an_invalid_source_or_with_an_unmet_requirement_is_refused() {
    let f = workspace("en").await;
    let broken = commands(&f).join("broken.md");
    f.write(&broken, "---\nargs:\n  - name: text\n---\n");
    let (status, answer) = run(
        &f,
        json!({"command": "broken", "person": "bot",
            "expected_command_file_id": gb_config::command_files::file_id("broken.md"),
            "expected_command_file_revision": revision_of(&broken)}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{answer}");
    assert_eq!(answer["code"], "command_file_invalid_source");
    let not_target = crate::i18n::t("en", "app_api.errors.command_not_runtime_target", &[]);
    assert_eq!(answer["message"], not_target);
    assert_eq!(
        answer["context"]["message"],
        "Command 'args' must be a mapping."
    );
    let tickets = commands(&f).join("tickets.py");
    f.write(
        &tickets,
        "COMMAND_METADATA = {\"requires\": [\"ticket_service\"]}\n\n\ndef main(context):\n    return ''\n",
    );
    let (status, answer) = run(
        &f,
        json!({"command": "tickets", "person": "bot",
            "expected_command_file_id": gb_config::command_files::file_id("tickets.py"),
            "expected_command_file_revision": revision_of(&tickets)}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{answer}");
    assert_eq!(answer["code"], "command_requirement_missing");
    assert_eq!(answer["message"], not_target);
    assert_eq!(answer["context"], json!({}));
}

fn authoring(mode: &str) -> Value {
    if mode == "edit" {
        json!({"conversation_id": "c-1", "message": "Make it weekly.", "person": "bot",
            "mode": "edit", "command": "reports/weekly", "format": "python",
            "content": "old source", "file_id": "cmVwb3J0cy93ZWVrbHkucHk", "revision": "revision-1"})
    } else {
        json!({"conversation_id": "c-1", "message": "Make a command.", "mode": "create"})
    }
}

#[tokio::test]
async fn an_authoring_request_that_breaks_its_mode_is_a_validation_error() {
    let f = workspace("en").await;
    let mut cases = Vec::new();
    for field in ["command", "format", "file_id", "revision"] {
        let mut edit = authoring("edit");
        edit.as_object_mut().unwrap().remove(field);
        cases.push(edit);
    }
    for (field, value) in [
        ("command", json!("x")),
        ("format", json!("python")),
        ("content", json!("x")),
        ("file_id", json!("x")),
        ("revision", json!("x")),
    ] {
        let mut create = authoring("create");
        create[field] = value;
        cases.push(create);
    }
    let mut long = authoring("create");
    long["conversation_id"] = json!("c".repeat(129));
    let mut empty = authoring("create");
    empty["message"] = json!("");
    let mut extra = authoring("create");
    extra["unexpected"] = json!(true);
    cases.extend([long, empty, extra]);
    for body in cases {
        let (status, answer) = f.call("POST", "/commands/author", body.clone()).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{body}: {answer}");
        assert_eq!(answer["code"], "validation_error");
    }
}

#[tokio::test]
async fn where_a_run_works_is_read_after_its_member() {
    let f = workspace("en").await;
    let mut ghost = authoring("create");
    ghost["person"] = json!("ghost");
    let (_, answer) = f.call("POST", "/commands/author", ghost).await;
    assert_eq!(answer["code"], "person_not_found");
    assert!(!f.workspace().join(".guildbotics/local/work").exists());
    let (_, answer) = f
        .call(
            "POST",
            "/commands/run",
            json!({"command": "hello", "person": "ghost", "cwd": "relative"}),
        )
        .await;
    assert_eq!(answer["code"], "person_not_found");
}

#[tokio::test]
async fn an_authoring_turn_is_a_read_only_run_of_its_conversation_answered_as_a_bad_gateway() {
    let f = workspace("en").await;
    f.write(
        &commands(&f).join("shared.md"),
        "---\nbrain: none\n---\nShared.\n",
    );
    // The manual place is taken and a routine holds the member's lease: a
    // read-only assistant runs anyway.
    *f.api.manual.lock().unwrap() = Some("inflight".into());
    let _routine = gb_base::files::FileLock::try_hold(
        &f.workspace()
            .join(".guildbotics/local/run/person-leases/bot.lock"),
    )
    .unwrap()
    .unwrap();
    let (status, answer) = f.call("POST", "/commands/author", authoring("edit")).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{answer}");
    assert_eq!(answer["code"], "command_authoring_failed");
    let trace = answer["context"]["trace_id"].as_str().unwrap();
    let started = f.events("command.started").pop().unwrap();
    assert_eq!(started["trace_id"], trace);
    assert_eq!(
        started["payload"],
        json!({"command": "author:reports/weekly", "person": "bot"})
    );
    assert_eq!(
        started["attributes"]["command_authoring.conversation_id"],
        "c-1"
    );
    assert!(started["attributes"].get("run.entry").is_none());
    let failed = f.events("command.failed").pop().unwrap();
    assert_eq!(failed["trace_id"], trace);
    assert_eq!(failed["payload"]["command"], "author:reports/weekly");
    let detail = f.get(&format!("/diagnostics/traces/{trace}")).await;
    assert_eq!(detail["summary"]["status"], "failed", "{detail}");
    assert!(
        f.workspace()
            .join(".guildbotics/local/work/command-authoring")
            .is_dir()
    );
    let (_, created) = f
        .call("POST", "/commands/author", authoring("create"))
        .await;
    assert_eq!(created["code"], "command_authoring_failed");
    assert_eq!(
        f.events("command.started").pop().unwrap()["payload"]["command"],
        "author:new-command"
    );
    // An unknown member is refused as it is, not as the assistant's failure.
    let mut ghost = authoring("create");
    ghost["person"] = json!("ghost");
    let (status, answer) = f.call("POST", "/commands/author", ghost).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(answer["code"], "person_not_found");
    let refused = f.events("command.failed").pop().unwrap();
    assert_eq!(refused["trace_id"], Value::Null);
    assert_eq!(refused["payload"]["command"], "author:new-command");
    // An assistant that cannot be read is the assistant's failure.
    f.write(
        &f.config()
            .join("team/members/bot/commands/assistants/author_command.md"),
        "---\n[broken\n---\n",
    );
    let (status, answer) = f
        .call("POST", "/commands/author", authoring("create"))
        .await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{answer}");
    assert_eq!(answer["code"], "command_authoring_failed");
}
