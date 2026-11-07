//! The shared command files the command screens change (凍結版
//! `tests/guildbotics/app_api/test_command_files.py`).

use super::*;

fn id(relative: &str) -> String {
    gb_config::command_files::file_id(relative)
}

fn revision_of(path: &Path) -> String {
    gb_config::command_files::revision(&std::fs::read(path).unwrap())
}

/// A workspace with a project in `language` and the member bot.
async fn workspace(language: &str) -> Fixture {
    let f = fixture();
    f.init_with(json!({ "language": language })).await;
    f.add(agent("bot")).await;
    // Without the sample commands the workspace starts with.
    std::fs::remove_dir_all(commands(&f)).unwrap();
    std::fs::create_dir_all(commands(&f)).unwrap();
    f
}

fn commands(f: &Fixture) -> PathBuf {
    f.config().join("commands")
}

fn names_in(directory: &Path) -> Vec<String> {
    let mut names: Vec<_> = std::fs::read_dir(directory)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[tokio::test]
async fn create_writes_each_format_with_its_extension_and_answers_the_detail() {
    let f = workspace("en").await;
    for (command, format, file) in [
        ("md_cmd", "markdown", "md_cmd.md"),
        ("py_cmd", "python", "py_cmd.py"),
        ("yaml_cmd", "yaml", "yaml_cmd.yaml"),
        ("sh_cmd", "shell", "sh_cmd.sh"),
        ("workflows/review", "yaml", "workflows/review.yaml"),
    ] {
        let (status, detail) = f
            .call(
                "POST",
                "/commands/files",
                json!({"command": command, "format": format}),
            )
            .await;
        assert_eq!(status, StatusCode::OK, "{detail}");
        let path = commands(&f).join(file);
        assert!(path.is_file(), "{file}");
        assert_eq!(detail["command"], command);
        assert_eq!(detail["format"], format);
        assert_eq!(detail["relative_path"], file);
        assert_eq!(detail["id"], id(file));
        assert_eq!(detail["revision"], revision_of(&path));
        for field in ["label", "description", "content", "arguments", "inputs"] {
            assert!(detail.get(field).is_some(), "{field}");
        }
    }
}

#[tokio::test]
async fn create_without_content_writes_the_initial_source_of_its_format() {
    let f = workspace("en").await;
    for (format, file, initial) in [
        (
            "markdown",
            "a.md",
            "---\nname: New command\nbrain: none\ninputs:\n  message: hidden\n---\nTODO\n",
        ),
        (
            "python",
            "b.py",
            "COMMAND_METADATA = {\"inputs\": {\"message\": \"hidden\"}}\n\n\ndef main(context) -> str:\n    return \"\"\n",
        ),
        ("shell", "c.sh", "#!/usr/bin/env bash\nset -euo pipefail\n"),
        ("yaml", "d.yaml", "commands: []\n"),
    ] {
        let command = file.split('.').next().unwrap();
        let (status, detail) = f
            .call(
                "POST",
                "/commands/files",
                json!({"command": command, "format": format}),
            )
            .await;
        assert_eq!(status, StatusCode::OK, "{detail}");
        assert_eq!(
            std::fs::read_to_string(commands(&f).join(file)).unwrap(),
            initial
        );
        assert_eq!(detail["content"], initial);
    }
}

#[tokio::test]
async fn create_with_content_writes_it_byte_for_byte() {
    let f = workspace("en").await;
    let draft = "def main(context) -> str:\r\n    return 'generated'\n";
    let (status, detail) = f
        .call(
            "POST",
            "/commands/files",
            json!({"command": "drafted", "format": "python", "content": draft}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{detail}");
    assert_eq!(detail["content"], draft);
    assert_eq!(
        std::fs::read(commands(&f).join("drafted.py")).unwrap(),
        draft.as_bytes()
    );
}

#[tokio::test]
async fn invalid_sources_are_saved_and_answer_no_arguments() {
    let f = workspace("en").await;
    let listed = "---\nargs:\n  - name: text\n---\nPolish the input text.\n";
    let (status, created) = f
        .call(
            "POST",
            "/commands/files",
            json!({"command": "polish", "format": "markdown", "content": listed}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{created}");
    assert_eq!(created["arguments"], json!([]));
    assert_eq!(
        std::fs::read_to_string(commands(&f).join("polish.md")).unwrap(),
        listed
    );
    let both =
        "---\nargs:\n  language:\n    required: true\n    default: English\n---\n${language}\n";
    let (status, updated) = f
        .call(
            "PUT",
            &format!("/commands/files/{}", id("polish.md")),
            json!({"content": both, "expected_revision": created["revision"]}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{updated}");
    assert_eq!(updated["content"], both);
    assert_eq!(updated["arguments"], json!([]));
}

#[cfg(unix)]
#[tokio::test]
async fn created_files_get_0755_for_shell_0644_otherwise_and_an_update_keeps_the_mode() {
    use std::os::unix::fs::PermissionsExt;
    let f = workspace("en").await;
    let mode = |file: &str| {
        std::fs::metadata(commands(&f).join(file))
            .unwrap()
            .permissions()
            .mode()
            & 0o777
    };
    for (command, format) in [("deploy", "shell"), ("note", "markdown"), ("job", "python")] {
        let (status, _) = f
            .call(
                "POST",
                "/commands/files",
                json!({"command": command, "format": format}),
            )
            .await;
        assert_eq!(status, StatusCode::OK);
    }
    assert_eq!(mode("deploy.sh"), 0o755);
    assert_eq!(mode("note.md"), 0o644);
    assert_eq!(mode("job.py"), 0o644);
    std::fs::set_permissions(
        commands(&f).join("note.md"),
        std::fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    for (file, expected) in [("deploy.sh", 0o755), ("note.md", 0o600)] {
        let (status, _) = f
            .call(
                "PUT",
                &format!("/commands/files/{}", id(file)),
                json!({"content": "x\n", "expected_revision": revision_of(&commands(&f).join(file))}),
            )
            .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(mode(file), expected, "{file}");
    }
}

#[tokio::test]
async fn create_refuses_an_invalid_name_with_400() {
    let f = workspace("en").await;
    for name in [
        "/abs",
        "..",
        "a/../b",
        "back\\slash",
        "has.dot",
        "trailing/",
        "with.md",
        "",
        "  ",
        "nul\0",
    ] {
        let (status, answer) = f
            .call(
                "POST",
                "/commands/files",
                json!({"command": name, "format": "markdown"}),
            )
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{name:?}: {answer}");
        assert_eq!(answer["code"], "command_file_invalid_name");
        assert_eq!(answer["context"], json!({ "command": name }));
        assert_eq!(
            answer["message"],
            crate::i18n::t(
                "en",
                "app_api.errors.command_file_invalid_name.value",
                &[("command", name)]
            )
        );
    }
    assert!(names_in(&commands(&f)).is_empty());
}

#[tokio::test]
async fn create_refuses_a_command_that_exists_in_any_extension() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("dup.md"), "kept\n");
    f.write(&commands(&f).join("task.md"), "kept\n");
    for (command, format) in [("dup", "markdown"), ("task", "yaml")] {
        let (status, answer) = f
            .call(
                "POST",
                "/commands/files",
                json!({"command": command, "format": format}),
            )
            .await;
        assert_eq!(status, StatusCode::CONFLICT, "{answer}");
        assert_eq!(answer["code"], "command_file_exists");
        assert_eq!(
            answer["context"],
            json!({"relative_path": format!("{command}.md")})
        );
        assert_eq!(
            answer["message"],
            crate::i18n::t(
                "en",
                "app_api.errors.command_file_exists.exists",
                &[("command", command)]
            )
        );
    }
    assert_eq!(names_in(&commands(&f)), ["dup.md", "task.md"]);
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(f.root.join("nowhere"), commands(&f).join("linked.sh")).unwrap();
        let (status, answer) = f
            .call(
                "POST",
                "/commands/files",
                json!({"command": "linked", "format": "markdown"}),
            )
            .await;
        assert_eq!(status, StatusCode::CONFLICT, "{answer}");
        assert_eq!(answer["context"], json!({"relative_path": "linked.sh"}));
    }
}

/// A template in the project's language runs before the workspace's plain
/// file, while the workspace's plain file runs before a plain template.
#[tokio::test]
async fn create_is_refused_where_a_template_would_run_instead_and_allowed_over_a_plain_one() {
    let f = workspace("ja").await;
    let templates = f.root.join("templates");
    f.write(&templates.join("commands/localized.ja.md"), "template\n");
    f.write(&templates.join("commands/plain.md"), "template\n");
    let roots = gb_config::commands::CommandRoots {
        config: f.config(),
        templates: templates.clone(),
    };
    let mut writes = SharedWrites::new(f.root.join("identity"), Some(f.workspace()));
    writes.timeout = Duration::from_secs(1);
    let refused = writes
        .transaction(|writer| {
            roots.create_file(
                writer,
                "localized",
                gb_config::command_writes::Format::Markdown,
                None,
                "ja",
            )
        })
        .unwrap_err();
    let refused = refused
        .downcast_ref::<gb_config::command_writes::Refused>()
        .unwrap();
    assert_eq!(
        (refused.status, refused.code, refused.key),
        (409, "command_file_shadowed", "command_file_shadowed")
    );
    assert_eq!(refused.context, json!({"shadow_source": "template"}));
    assert!(!commands(&f).join("localized.md").exists());
    let created = writes
        .transaction(|writer| {
            roots.create_file(
                writer,
                "plain",
                gb_config::command_writes::Format::Markdown,
                None,
                "ja",
            )
        })
        .unwrap();
    assert_eq!(created["command"], "plain");
    assert!(commands(&f).join("plain.md").is_file());
}

#[tokio::test]
async fn shadow_source_names_where_the_command_resolves() {
    let f = workspace("en").await;
    let roots = gb_config::commands::CommandRoots {
        config: f.config(),
        templates: f.root.join("templates"),
    };
    for (path, source) in [
        (None, "workspace"),
        (Some(f.config().join("commands/review.md")), "workspace"),
        (
            Some(f.root.join("templates/commands/review.md")),
            "template",
        ),
        (
            Some(f.config().join("team/members/alice/commands/review.md")),
            "member",
        ),
    ] {
        assert_eq!(roots.shadow_source(path.as_deref()), source, "{path:?}");
    }
}

#[tokio::test]
async fn an_update_or_delete_of_a_stale_revision_is_409_and_keeps_the_file() {
    let f = workspace("en").await;
    let greet = commands(&f).join("greet.md");
    f.write(&greet, "hello\n");
    let read = revision_of(&greet);
    f.write(&greet, "newer\n");
    let (status, answer) = f
        .call(
            "PUT",
            &format!("/commands/files/{}", id("greet.md")),
            json!({"content": "changed", "expected_revision": read}),
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{answer}");
    assert_eq!(answer["code"], "command_file_changed");
    assert_eq!(
        answer["message"],
        crate::i18n::t("en", "app_api.errors.command_file_changed.reloaded", &[])
    );
    assert_eq!(
        answer["context"],
        json!({"current_revision": revision_of(&greet)})
    );
    let (status, answer) = f
        .call(
            "DELETE",
            &format!(
                "/commands/files/{}?expected_revision={read}",
                id("greet.md")
            ),
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{answer}");
    assert_eq!(
        answer["context"],
        json!({"current_revision": revision_of(&greet)})
    );
    assert_eq!(std::fs::read_to_string(&greet).unwrap(), "newer\n");
}

#[tokio::test]
async fn an_update_writes_the_content_and_a_delete_answers_the_files_left() {
    let f = workspace("en").await;
    let greet = commands(&f).join("greet.md");
    f.write(&greet, "hello\n");
    f.write(&commands(&f).join("other.md"), "other\n");
    let before = revision_of(&greet);
    let (status, updated) = f
        .call(
            "PUT",
            &format!("/commands/files/{}", id("greet.md")),
            json!({"content": "changed\n", "expected_revision": before}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{updated}");
    assert_eq!(std::fs::read_to_string(&greet).unwrap(), "changed\n");
    assert_ne!(updated["revision"], before);
    assert_eq!(updated["revision"], revision_of(&greet));
    assert_eq!(
        names_in(&commands(&f)),
        ["greet.md", "other.md"],
        "no temporary file"
    );
    let (status, left) = f
        .call(
            "DELETE",
            &format!(
                "/commands/files/{}?expected_revision={}",
                id("greet.md"),
                revision_of(&greet)
            ),
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{left}");
    assert!(!greet.exists());
    assert_eq!(left, f.get("/commands/files").await);
    assert_eq!(left["files"][0]["command"], "other");
    let (status, _) = f
        .call(
            "DELETE",
            &format!("/commands/files/{}", id("other.md")),
            Value::Null,
        )
        .await;
    assert_eq!(
        status,
        StatusCode::UNPROCESSABLE_ENTITY,
        "the revision is required"
    );
}

#[tokio::test]
async fn writes_to_a_file_that_is_not_the_effective_shared_command_are_404() {
    let f = workspace("en").await;
    f.write(&commands(&f).join("greet.md"), "md\n");
    let yaml = commands(&f).join("greet.yaml");
    f.write(&yaml, "commands: []\n");
    #[cfg(unix)]
    {
        f.write(&f.root.join("outside.md"), "outside\n");
        std::os::unix::fs::symlink(f.root.join("outside.md"), commands(&f).join("escape.md"))
            .unwrap();
    }
    for relative in ["greet.yaml", "missing.md", "escape.md", "../outside.md"] {
        let file = id(relative);
        let current = std::fs::read(commands(&f).join(relative))
            .map(|b| gb_config::command_files::revision(&b))
            .unwrap_or_default();
        for (method, uri, body) in [
            ("GET", format!("/commands/files/{file}"), Value::Null),
            (
                "PUT",
                format!("/commands/files/{file}"),
                json!({"content": "commands: []\n", "expected_revision": "any"}),
            ),
            (
                "DELETE",
                format!("/commands/files/{file}?expected_revision={current}"),
                Value::Null,
            ),
            (
                "GET",
                format!("/commands/files/{file}/execution-status?expected_revision={current}"),
                Value::Null,
            ),
        ] {
            let (status, answer) = f.call(method, &uri, body).await;
            assert_eq!(
                status,
                StatusCode::NOT_FOUND,
                "{method} {relative}: {answer}"
            );
            assert_eq!(answer["code"], "command_file_not_found");
            assert_eq!(
                answer["message"],
                crate::i18n::t(
                    "en",
                    "app_api.errors.command_file_not_found.unknown",
                    &[("file_id", &file)]
                )
            );
        }
    }
    assert_eq!(std::fs::read_to_string(&yaml).unwrap(), "commands: []\n");
    let listed = f.get("/commands/files").await;
    let names: Vec<_> = listed["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|file| file["relative_path"].clone())
        .collect();
    assert_eq!(names, [json!("greet.md")]);
}

#[tokio::test]
async fn reserved_metadata_files_are_never_selected() {
    for (language, file) in [
        ("en", "report.metadata.yml"),
        ("en", "report.metadata.en.yml"),
        ("ja", "report.metadata.ja.yaml"),
    ] {
        let f = workspace(language).await;
        f.write(&commands(&f).join(file), "commands: []\n");
        let (status, _) = f
            .call("GET", &format!("/commands/files/{}", id(file)), Value::Null)
            .await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{file}");
        assert_eq!(f.get("/commands/files").await, json!({"files": []}));
    }
}

#[tokio::test]
async fn shared_files_leave_out_member_commands_and_an_empty_root_lists_none() {
    let f = workspace("en").await;
    assert_eq!(f.get("/commands/files").await, json!({"files": []}));
    f.write(&commands(&f).join("shared.md"), "shared\n");
    f.write(
        &f.config().join("team/members/bot/commands/member.md"),
        "member\n",
    );
    let listed = f.get("/commands/files").await;
    assert_eq!(listed["files"].as_array().unwrap().len(), 1);
    assert_eq!(listed["files"][0]["command"], "shared");
}

#[tokio::test]
async fn content_with_nul_or_over_one_mebibyte_is_refused_before_writing() {
    let f = workspace("en").await;
    let greet = commands(&f).join("greet.md");
    f.write(&greet, "hello\n");
    let limit = gb_config::command_writes::MAX_BYTES;
    let current = revision_of(&greet);
    for (content, status, code, context) in [
        (
            "x".repeat(limit + 1),
            StatusCode::PAYLOAD_TOO_LARGE,
            "command_file_too_large",
            json!({"max_bytes": "1048576"}),
        ),
        // Over the request's own limit, before its JSON is read.
        (
            "x".repeat(crate::command_files_api::FILE_REQUEST_LIMIT),
            StatusCode::PAYLOAD_TOO_LARGE,
            "command_file_too_large",
            json!({"max_bytes": "1048576"}),
        ),
        (
            "a\0b".into(),
            StatusCode::BAD_REQUEST,
            "command_file_invalid_source",
            json!({"reason": "nul_byte"}),
        ),
    ] {
        for (method, uri, body) in [
            (
                "POST",
                "/commands/files".to_owned(),
                json!({"command": "big", "format": "markdown", "content": content}),
            ),
            (
                "PUT",
                format!("/commands/files/{}", id("greet.md")),
                json!({"content": content, "expected_revision": "stale"}),
            ),
        ] {
            let (answered, answer) = f.call(method, &uri, body).await;
            assert_eq!(answered, status, "{method}: {}", answer["code"]);
            assert_eq!(answer["code"], code);
            assert_eq!(answer["context"], context);
        }
    }
    assert_eq!(names_in(&commands(&f)), ["greet.md"]);
    assert_eq!(revision_of(&greet), current);
    let (status, _) = f
        .call(
            "PUT",
            &format!("/commands/files/{}", id("greet.md")),
            json!({"content": "x".repeat(limit), "expected_revision": current}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "exactly the limit is written");
}

#[tokio::test]
async fn writes_without_a_selected_workspace_are_refused_as_unconfigured() {
    let f = fixture_with(|_| Selected::default());
    for (method, uri, body) in [
        (
            "POST",
            "/commands/files",
            json!({"command": "a", "format": "markdown"}),
        ),
        (
            "POST",
            "/commands/author/apply",
            json!({"changes": [{"operation": "create", "command": "a", "format": "shell",
                "relative_path": "a.sh", "content": "x"}]}),
        ),
    ] {
        let (status, answer) = f.call(method, uri, body).await;
        assert_eq!(status, StatusCode::CONFLICT, "{uri}: {answer}");
        assert_eq!(answer["code"], "workspace_not_configured");
        assert_eq!(
            answer["message"],
            "No workspace is selected, so there are no shared commands."
        );
    }
}

fn change(operation: &str, command: &str, format: &str, relative: &str, content: &str) -> Value {
    json!({"operation": operation, "command": command, "format": format,
        "relative_path": relative, "content": content})
}

const TRANSLATE: &str = "---\nbrain: default\n---\nTranslate.\n";

#[tokio::test]
async fn authoring_apply_updates_the_current_command_and_creates_a_helper() {
    let f = workspace("en").await;
    let translate = commands(&f).join("translate.md");
    f.write(&translate, "---\nbrain: none\n---\nOld.\n");
    let mut update = change("update", "translate", "markdown", "translate.md", TRANSLATE);
    update["file_id"] = json!(id("translate.md"));
    update["expected_revision"] = json!(revision_of(&translate));
    let helper = "def main(context) -> str:\n    return 'help'\n";
    let (status, applied) = f
        .call(
            "POST",
            "/commands/author/apply",
            json!({"changes": [change("create", "helper", "python", "helper.py", helper), update]}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{applied}");
    let commands_applied: Vec<_> = applied["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|file| file["command"].clone())
        .collect();
    assert_eq!(commands_applied, [json!("helper"), json!("translate")]);
    assert_eq!(
        std::fs::read_to_string(commands(&f).join("helper.py")).unwrap(),
        helper
    );
    assert_eq!(std::fs::read_to_string(&translate).unwrap(), TRANSLATE);
}

#[cfg(unix)]
#[tokio::test]
async fn authoring_apply_refuses_two_changes_that_write_one_file_through_a_link() {
    let f = workspace("en").await;
    std::fs::create_dir_all(commands(&f).join("reports")).unwrap();
    std::os::unix::fs::symlink("reports", commands(&f).join("alias")).unwrap();
    // One file, or one command in two formats.
    for (format, relative) in [
        ("shell", "alias/weekly.sh"),
        ("markdown", "alias/weekly.md"),
    ] {
        let content = if format == "shell" {
            "echo b\n"
        } else {
            TRANSLATE
        };
        let (status, answer) = f
            .call(
                "POST",
                "/commands/author/apply",
                json!({"changes": [
                    change("create", "reports/weekly", "shell", "reports/weekly.sh", "echo a\n"),
                    change("create", "alias/weekly", format, relative, content),
                ]}),
            )
            .await;
        assert_eq!(status, StatusCode::CONFLICT, "{format}: {answer}");
        assert_eq!(answer["code"], "command_file_exists");
        assert_eq!(
            answer["message"],
            crate::i18n::t(
                "en",
                "app_api.errors.command_file_exists.duplicate",
                &[("command", "alias/weekly")]
            )
        );
        assert_eq!(
            names_in(&commands(&f).join("reports")),
            Vec::<String>::new()
        );
    }
}

#[tokio::test]
async fn authoring_apply_checks_every_change_before_writing_any() {
    let f = workspace("en").await;
    let translate = commands(&f).join("translate.md");
    f.write(&translate, "---\nbrain: none\n---\nOld.\n");
    let helper = change(
        "create",
        "helper",
        "python",
        "helper.py",
        "def main(context):\n    return ''\n",
    );
    let mut stale = change("update", "translate", "markdown", "translate.md", TRANSLATE);
    stale["file_id"] = json!(id("translate.md"));
    stale["expected_revision"] = json!("stale");
    let mut retargeted = stale.clone();
    retargeted["file_id"] = json!(id("other.md"));
    f.write(
        &commands(&f).join("other.md"),
        "---\nbrain: none\n---\nOther.\n",
    );
    retargeted["expected_revision"] = json!(revision_of(&commands(&f).join("other.md")));
    for (changes, status, code, key, context) in [
        (
            json!([helper.clone(), stale]),
            StatusCode::CONFLICT,
            "command_file_changed",
            "command_file_changed.proposal",
            json!({"current_revision": revision_of(&translate)}),
        ),
        (
            json!([helper.clone(), retargeted]),
            StatusCode::CONFLICT,
            "command_file_changed",
            "command_file_changed.retargeted",
            json!({}),
        ),
        (
            json!([
                helper.clone(),
                change(
                    "create",
                    "x",
                    "python",
                    "another.py",
                    "def main(c):\n    return ''\n"
                )
            ]),
            StatusCode::BAD_REQUEST,
            "command_file_invalid_name",
            "command_file_invalid_name.path",
            json!({"relative_path": "another.py"}),
        ),
        (
            json!([
                change("create", "helper", "markdown", "helper.md", TRANSLATE),
                change("create", "helper", "shell", "helper.sh", "echo\n")
            ]),
            StatusCode::CONFLICT,
            "command_file_exists",
            "command_file_exists.duplicate",
            json!({}),
        ),
        (
            json!([
                helper.clone(),
                change(
                    "create",
                    "bad",
                    "markdown",
                    "bad.md",
                    "---\ninputs: []\n---\nx\n"
                )
            ]),
            StatusCode::BAD_REQUEST,
            "command_file_invalid_source",
            "",
            json!({}),
        ),
        (
            json!([
                helper.clone(),
                change(
                    "create",
                    "brainless",
                    "markdown",
                    "brainless.md",
                    "No frontmatter.\n"
                )
            ]),
            StatusCode::BAD_REQUEST,
            "command_file_invalid_source",
            "",
            json!({"reason": "generated_brain_not_explicit"}),
        ),
    ] {
        let (answered, answer) = f
            .call(
                "POST",
                "/commands/author/apply",
                json!({ "changes": changes }),
            )
            .await;
        assert_eq!(answered, status, "{answer}");
        assert_eq!(answer["code"], code);
        assert_eq!(answer["context"], context, "{answer}");
        if !key.is_empty() {
            let command = if code == "command_file_exists" {
                "helper"
            } else {
                ""
            };
            assert_eq!(
                answer["message"],
                crate::i18n::t(
                    "en",
                    &format!("app_api.errors.{key}"),
                    &[("command", command)]
                )
            );
        }
        assert!(
            !commands(&f).join("helper.py").exists(),
            "{code}: nothing is written"
        );
        assert!(!commands(&f).join("helper.md").exists());
    }
    assert_eq!(
        std::fs::read_to_string(&translate).unwrap(),
        "---\nbrain: none\n---\nOld.\n"
    );
}

#[tokio::test]
async fn authoring_apply_requests_name_their_files_as_their_operation_does() {
    let f = workspace("en").await;
    let mut create_with_file = change("create", "a", "shell", "a.sh", "x");
    create_with_file["file_id"] = json!("x");
    let update_without = change("update", "a", "shell", "a.sh", "x");
    let mut extra = change("create", "a", "shell", "a.sh", "x");
    extra["unexpected"] = json!(true);
    for changes in [
        json!([]),
        json!([create_with_file]),
        json!([update_without]),
        json!([extra]),
        Value::Array(vec![change("create", "a", "shell", "a.sh", "x"); 11]),
    ] {
        let (status, answer) = f
            .call(
                "POST",
                "/commands/author/apply",
                json!({ "changes": changes }),
            )
            .await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{answer}");
        assert_eq!(answer["code"], "validation_error");
    }
}

#[cfg(unix)]
#[tokio::test]
async fn a_failed_apply_puts_back_what_it_had_written() {
    use std::os::unix::fs::PermissionsExt;
    let f = workspace("en").await;
    let roots = gb_config::commands::CommandRoots {
        config: f.config(),
        templates: gb_base::home::python_package().join("templates"),
    };
    let first = commands(&f).join("first.md");
    f.write(&first, "---\nbrain: none\n---\nFirst.\n");
    // A directory no file can be written in fails the second write only.
    let locked = commands(&f).join("locked");
    std::fs::create_dir_all(&locked).unwrap();
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o555)).unwrap();
    let mut update = change("update", "first", "markdown", "first.md", TRANSLATE);
    update["file_id"] = json!(id("first.md"));
    update["expected_revision"] = json!(revision_of(&first));
    let changes: Vec<gb_config::command_writes::Change> = serde_json::from_value(json!([
        update,
        change("create", "locked/made", "shell", "locked/made.sh", "echo\n"),
    ]))
    .unwrap();
    let mut writes = SharedWrites::new(f.root.join("identity"), Some(f.workspace()));
    writes.timeout = Duration::from_secs(1);
    let failed = writes.transaction(|writer| roots.apply_changes(writer, &changes, "en"));
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(failed.is_err());
    assert_eq!(
        std::fs::read_to_string(&first).unwrap(),
        "---\nbrain: none\n---\nFirst.\n",
        "the written file is put back"
    );
    assert!(!locked.join("made.sh").exists());
}
