use std::path::{Path, PathBuf};

use gb_workspace::selection;

fn workspace_location(path: &Path, language: &str) -> anyhow::Result<PathBuf> {
    let home = std::env::var_os(gb_base::home::USER_HOME_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .ok_or_else(|| {
            anyhow::anyhow!(crate::i18n::t(language, "cli.workspace.home_required", &[]))
        })?;
    gb_environment::contract::workspace_location(path, &home)
        .map_err(|sentence| anyhow::anyhow!(crate::i18n::say(sentence, language)))
}

pub fn select(data: &Path, path: &Path, language: &str) -> anyhow::Result<PathBuf> {
    let workspace = workspace_location(path, language)?;
    selection::write_active(data, &workspace).map_err(|error| registry_error(error, language))?;
    Ok(workspace)
}

pub fn resolve(data: &Path, explicit: Option<PathBuf>, language: &str) -> anyhow::Result<PathBuf> {
    resolve_with_message(
        data,
        explicit,
        language,
        "cli.member.client.workspace_required",
    )
}

pub fn resolve_with_message(
    data: &Path,
    explicit: Option<PathBuf>,
    language: &str,
    missing: &str,
) -> anyhow::Result<PathBuf> {
    let workspace = read_selection_with_message(data, explicit, language, missing)?;
    selection::register(data, &workspace).map_err(|error| registry_error(error, language))?;
    Ok(workspace)
}

pub fn read_selection(
    data: &Path,
    explicit: Option<PathBuf>,
    language: &str,
) -> anyhow::Result<PathBuf> {
    read_selection_with_message(
        data,
        explicit,
        language,
        "cli.member.client.workspace_required",
    )
}

fn read_selection_with_message(
    data: &Path,
    explicit: Option<PathBuf>,
    language: &str,
    missing: &str,
) -> anyhow::Result<PathBuf> {
    let named = match explicit {
        Some(path) => path,
        None => selection::resolve(data, |key| std::env::var_os(key))?
            .ok_or_else(|| anyhow::anyhow!(crate::i18n::t(language, missing, &[])))?,
    };
    workspace_location(&named, language)
}

pub fn active(data: &Path) -> anyhow::Result<Option<PathBuf>> {
    selection::read_active(data)
}

pub fn state_file(data: &Path) -> PathBuf {
    data.join(selection::ACTIVE_FILE)
}

/// Turn a registry failure into the repair instructions at the host boundary.
pub(crate) fn registry_error(error: anyhow::Error, language: &str) -> anyhow::Error {
    let Some(problem) = error.downcast_ref::<selection::RegistryError>() else {
        return error;
    };
    anyhow::anyhow!(crate::i18n::t(
        language,
        "safe_paths.registry",
        &[
            ("path", &problem.path.to_string_lossy()),
            ("reason", &problem.reason)
        ],
    ))
}
