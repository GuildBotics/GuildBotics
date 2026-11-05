use std::path::{Path, PathBuf};

use gb_workspace::selection;

fn workspace_location(path: &Path, language: &str) -> anyhow::Result<PathBuf> {
    let home = std::env::var_os("HOME")
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
    selection::write_active(data, &workspace)?;
    Ok(workspace)
}

pub fn resolve(data: &Path, explicit: Option<PathBuf>, language: &str) -> anyhow::Result<PathBuf> {
    let workspace = read_selection(data, explicit, language)?;
    selection::register(data, &workspace)?;
    Ok(workspace)
}

pub fn read_selection(
    data: &Path,
    explicit: Option<PathBuf>,
    language: &str,
) -> anyhow::Result<PathBuf> {
    let named = match explicit {
        Some(path) => path,
        None => selection::resolve(data, |key| std::env::var_os(key))?.ok_or_else(|| {
            anyhow::anyhow!(crate::i18n::t(
                language,
                "cli.member.client.workspace_required",
                &[]
            ))
        })?,
    };
    workspace_location(&named, language)
}

pub fn active(data: &Path) -> anyhow::Result<Option<PathBuf>> {
    selection::read_active(data)
}

pub fn state_file(data: &Path) -> PathBuf {
    data.join(selection::ACTIVE_FILE)
}
