//! The sentences people read, from the locale files the Python version reads
//! too (`guildbotics/templates/locales`): a key `a.b.c` is `c` in the file
//! `a/b.<language>.yml`, under the language's own key, falling back to
//! English and then to the key itself.

use gb_environment::Sentence;

/// The namespaces this executable says something from, by language.
const FILES: &[(&str, &str, &str)] = &[
    (
        "setup",
        "en",
        include_str!("../../../guildbotics/templates/locales/setup.en.yml"),
    ),
    (
        "setup",
        "ja",
        include_str!("../../../guildbotics/templates/locales/setup.ja.yml"),
    ),
    (
        "member_config",
        "en",
        include_str!("../../../guildbotics/templates/locales/member_config.en.yml"),
    ),
    (
        "member_config",
        "ja",
        include_str!("../../../guildbotics/templates/locales/member_config.ja.yml"),
    ),
    (
        "safe_paths",
        "en",
        include_str!("../../../guildbotics/templates/locales/safe_paths.en.yml"),
    ),
    (
        "safe_paths",
        "ja",
        include_str!("../../../guildbotics/templates/locales/safe_paths.ja.yml"),
    ),
    (
        "commands.workflows.chat_conversation_workflow",
        "en",
        include_str!(
            "../../../guildbotics/templates/locales/commands/workflows/chat_conversation_workflow.en.yml"
        ),
    ),
    (
        "commands.workflows.chat_conversation_workflow",
        "ja",
        include_str!(
            "../../../guildbotics/templates/locales/commands/workflows/chat_conversation_workflow.ja.yml"
        ),
    ),
    (
        "drivers.task_scheduler",
        "en",
        include_str!("../../../guildbotics/templates/locales/drivers/task_scheduler.en.yml"),
    ),
    (
        "drivers.task_scheduler",
        "ja",
        include_str!("../../../guildbotics/templates/locales/drivers/task_scheduler.ja.yml"),
    ),
    (
        "commands.workflows.common",
        "en",
        include_str!("../../../guildbotics/templates/locales/commands/workflows/common.en.yml"),
    ),
    (
        "commands.workflows.common",
        "ja",
        include_str!("../../../guildbotics/templates/locales/commands/workflows/common.ja.yml"),
    ),
    (
        "integrations.pull_request_patrol",
        "en",
        include_str!(
            "../../../guildbotics/templates/locales/integrations/pull_request_patrol.en.yml"
        ),
    ),
    (
        "integrations.pull_request_patrol",
        "ja",
        include_str!(
            "../../../guildbotics/templates/locales/integrations/pull_request_patrol.ja.yml"
        ),
    ),
    (
        "capabilities.work_stops",
        "en",
        include_str!("../../../guildbotics/templates/locales/capabilities/work_stops.en.yml"),
    ),
    (
        "capabilities.work_stops",
        "ja",
        include_str!("../../../guildbotics/templates/locales/capabilities/work_stops.ja.yml"),
    ),
    (
        "cli.member",
        "en",
        include_str!("../../../guildbotics/templates/locales/cli/member.en.yml"),
    ),
    (
        "cli.member",
        "ja",
        include_str!("../../../guildbotics/templates/locales/cli/member.ja.yml"),
    ),
    (
        "integrations.resources",
        "en",
        include_str!("../../../guildbotics/templates/locales/integrations/resources.en.yml"),
    ),
    (
        "integrations.resources",
        "ja",
        include_str!("../../../guildbotics/templates/locales/integrations/resources.ja.yml"),
    ),
    (
        "integrations.slack.chat_service",
        "en",
        include_str!(
            "../../../guildbotics/templates/locales/integrations/slack/chat_service.en.yml"
        ),
    ),
    (
        "integrations.slack.chat_service",
        "ja",
        include_str!(
            "../../../guildbotics/templates/locales/integrations/slack/chat_service.ja.yml"
        ),
    ),
    (
        "integrations.branvil.api",
        "en",
        include_str!("../../../guildbotics/templates/locales/integrations/branvil/api.en.yml"),
    ),
    (
        "integrations.branvil.api",
        "ja",
        include_str!("../../../guildbotics/templates/locales/integrations/branvil/api.ja.yml"),
    ),
    (
        "app_api.errors",
        "en",
        include_str!("../../../guildbotics/templates/locales/app_api/errors.en.yml"),
    ),
    (
        "app_api.errors",
        "ja",
        include_str!("../../../guildbotics/templates/locales/app_api/errors.ja.yml"),
    ),
    (
        "app_api.ui",
        "en",
        include_str!("../../../guildbotics/templates/locales/app_api/ui.en.yml"),
    ),
    (
        "app_api.ui",
        "ja",
        include_str!("../../../guildbotics/templates/locales/app_api/ui.ja.yml"),
    ),
    (
        "safe_paths",
        "en",
        include_str!("../../../guildbotics/templates/locales/safe_paths.en.yml"),
    ),
    (
        "safe_paths",
        "ja",
        include_str!("../../../guildbotics/templates/locales/safe_paths.ja.yml"),
    ),
    (
        "cli.install",
        "en",
        include_str!("../../../guildbotics/templates/locales/cli/install.en.yml"),
    ),
    (
        "cli.install",
        "ja",
        include_str!("../../../guildbotics/templates/locales/cli/install.ja.yml"),
    ),
    (
        "capabilities.member_git",
        "en",
        include_str!("../../../guildbotics/templates/locales/capabilities/member_git.en.yml"),
    ),
    (
        "capabilities.member_git",
        "ja",
        include_str!("../../../guildbotics/templates/locales/capabilities/member_git.ja.yml"),
    ),
    (
        "cli.git_credential",
        "en",
        include_str!("../../../guildbotics/templates/locales/cli/git_credential.en.yml"),
    ),
    (
        "cli.git_credential",
        "ja",
        include_str!("../../../guildbotics/templates/locales/cli/git_credential.ja.yml"),
    ),
    (
        "integrations.branvil.owner_git",
        "en",
        include_str!(
            "../../../guildbotics/templates/locales/integrations/branvil/owner_git.en.yml"
        ),
    ),
    (
        "integrations.branvil.owner_git",
        "ja",
        include_str!(
            "../../../guildbotics/templates/locales/integrations/branvil/owner_git.ja.yml"
        ),
    ),
    (
        "cli.secrets",
        "en",
        include_str!("../../../guildbotics/templates/locales/cli/secrets.en.yml"),
    ),
    (
        "cli.secrets",
        "ja",
        include_str!("../../../guildbotics/templates/locales/cli/secrets.ja.yml"),
    ),
    (
        "cli.workspace",
        "en",
        include_str!("../../../guildbotics/templates/locales/cli/workspace.en.yml"),
    ),
    (
        "cli.workspace",
        "ja",
        include_str!("../../../guildbotics/templates/locales/cli/workspace.ja.yml"),
    ),
    (
        "cli.run",
        "en",
        include_str!("../../../guildbotics/templates/locales/cli/run.en.yml"),
    ),
    (
        "cli.run",
        "ja",
        include_str!("../../../guildbotics/templates/locales/cli/run.ja.yml"),
    ),
    (
        "cli.server",
        "en",
        include_str!("../../../guildbotics/templates/locales/cli/server.en.yml"),
    ),
    (
        "cli.server",
        "ja",
        include_str!("../../../guildbotics/templates/locales/cli/server.ja.yml"),
    ),
    (
        "intelligences.agent_environment",
        "en",
        include_str!(
            "../../../guildbotics/templates/locales/intelligences/agent_environment.en.yml"
        ),
    ),
    (
        "intelligences.agent_environment",
        "ja",
        include_str!(
            "../../../guildbotics/templates/locales/intelligences/agent_environment.ja.yml"
        ),
    ),
    (
        "runtime.service_lock",
        "en",
        include_str!("../../../guildbotics/templates/locales/runtime/service_lock.en.yml"),
    ),
    (
        "runtime.service_lock",
        "ja",
        include_str!("../../../guildbotics/templates/locales/runtime/service_lock.ja.yml"),
    ),
];

/// A language a request or a person asked for, as the files name it.
pub fn language(asked: &str) -> &'static str {
    if asked.trim().to_ascii_lowercase().starts_with("ja") {
        "ja"
    } else {
        "en"
    }
}

/// The sentence of `key` in `language`, with each `%{name}` replaced by its
/// value in `params`.
pub fn t(language: &str, key: &str, params: &[(&str, &str)]) -> String {
    let mut text = [language, "en"]
        .iter()
        .find_map(|lang| lookup(lang, key))
        .unwrap_or_else(|| key.to_string());
    for (name, value) in params {
        text = text.replace(&format!("%{{{name}}}"), value);
        text = text.replace(&format!("{{{name}}}"), value);
    }
    text
}

/// Render a sentence emitted by the agent environment.
pub fn say(sentence: Sentence, language: &str) -> String {
    match sentence {
        Sentence::Text(text) => text,
        Sentence::AtPath(path, reason) => format!("{}: {}", path.display(), say(*reason, language)),
        Sentence::Key(key, params) => {
            let params: Vec<(&str, &str)> = params.iter().map(|(k, v)| (*k, v.as_str())).collect();
            t(language, key, &params)
        }
    }
}

fn lookup(language: &str, key: &str) -> Option<String> {
    let (path, source) = FILES
        .iter()
        .filter(|(namespace, lang, _)| {
            *lang == language && key.starts_with(&format!("{namespace}."))
        })
        .map(|(namespace, _, source)| (&key[namespace.len() + 1..], source))
        .next()?;
    let parsed: serde_json::Value = serde_saphyr::from_str(source).ok()?;
    path.split('.')
        .try_fold(parsed.get(language)?, |value, part| value.get(part))?
        .as_str()
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn member_capability_diagnostics_resolve_in_both_languages() {
        let sources = [
            include_str!("../../gb-member/src/lib.rs"),
            include_str!("../../gb-member/src/repository.rs"),
            include_str!("../../gb-member/src/chat.rs"),
            include_str!("../../gb-member/src/context.rs"),
            include_str!("../../gb-member/src/archive.rs"),
            include_str!("../../gb-member/src/audit.rs"),
            include_str!("../../gb-member/src/memory.rs"),
            include_str!("../../gb-member/src/command.rs"),
            include_str!("../../gb-member/src/session.rs"),
            include_str!("../../gb-member/src/git.rs"),
            include_str!("../../gb-member/src/control.rs"),
            include_str!("../../gb-member/src/chat_updates.rs"),
            include_str!("../../gb-member/src/output.rs"),
        ];
        for source in sources {
            for suffix in source.split("\"cli.member.").skip(1) {
                let key = format!("cli.member.{}", suffix.split('"').next().unwrap());
                for lang in ["en", "ja"] {
                    let sentence = t(
                        lang,
                        &key,
                        &[
                            ("action", "ACTION"),
                            ("state", "closed"),
                            ("person", "PERSON"),
                            ("blockers", "BLOCKERS"),
                            ("path", "PATH"),
                            ("name", "CHANNEL"),
                            ("size", "100"),
                            ("limit", "99"),
                            ("option", "OPTION"),
                            ("url", "URL"),
                            ("repository", "REPO"),
                            ("requested", "REPO"),
                            ("run_id", "RUN"),
                            ("status", "blocked"),
                            ("reason", "REASON"),
                            ("head", "HEAD"),
                        ],
                    );
                    assert_ne!(sentence, key, "{lang}: {key}");
                    assert!(!sentence.contains("%{"), "{lang}: {key}");
                }
            }
        }
    }

    #[test]
    fn service_errors_are_translated_with_safe_actionable_parameters() {
        for lang in ["en", "ja"] {
            for key in [
                "out_of_scope",
                "service_failed",
                "not_found",
                "unavailable",
                "operation_unsupported",
                "invalid_result",
            ] {
                let full = format!("integrations.resources.{key}");
                assert_ne!(t(lang, &full, &[]), full);
            }
            let missing = t(
                lang,
                "integrations.resources.missing_credential",
                &[("name", "AIKO_SLACK_BOT_TOKEN")],
            );
            assert!(missing.contains("AIKO_SLACK_BOT_TOKEN"));
            let scope = t(
                lang,
                "integrations.slack.chat_service.missing_scope",
                &[("needed", "channels:history"), ("provided", "identify")],
            );
            assert!(scope.contains("channels:history"));
            assert!(scope.contains("identify"));
        }
        assert_ne!(
            t("en", "integrations.resources.out_of_scope", &[]),
            t("ja", "integrations.resources.out_of_scope", &[])
        );
    }

    #[test]
    fn sentences_come_from_the_locale_files_in_the_language_asked() {
        assert_eq!(
            t("en", "app_api.errors.origin_mismatch", &[]),
            "Requests from another site are refused."
        );
        assert_eq!(
            t("ja", "app_api.errors.origin_mismatch", &[]),
            "別のサイトからのリクエストは受け付けません。"
        );
        assert_eq!(
            t(
                "ja",
                "runtime.service_lock.missing_process",
                &[("pid", "42")]
            ),
            "サーバーのロックは保持されていますが、プロセス 42 は実行されていません。"
        );
        assert_eq!(
            t("en", "app_api.errors.no_such_key", &[]),
            "app_api.errors.no_such_key"
        );
        assert_eq!(
            t("fr", "app_api.errors.invalid", &[]),
            t("en", "app_api.errors.invalid", &[])
        );
    }

    #[test]
    fn languages_are_japanese_or_english() {
        assert_eq!(language("ja-JP"), "ja");
        assert_eq!(language(" JA"), "ja");
        assert_eq!(language("en-US"), "en");
        assert_eq!(language(""), "en");
    }
}
