//! The sentences people read, from the locale files the Python version reads
//! too (`guildbotics/templates/locales`): a key `a.b.c` is `c` in the file
//! `a/b.<language>.yml`, under the language's own key, falling back to
//! English and then to the key itself.

/// The namespaces this executable says something from, by language.
const FILES: &[(&str, &str, &str)] = &[
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
    }
    text
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
