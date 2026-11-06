//! The shipped files a project starts with, copied as bytes so that a
//! template lands with the same newlines on every OS.
use super::*;
use gb_base::shared_writes::Writer;

/// Every regular file under `root`, by its path relative to it, sorted.
fn files_under(root: &Path) -> anyhow::Result<Vec<PathBuf>> {
    fn walk(root: &Path, dir: &Path, found: &mut Vec<PathBuf>) -> anyhow::Result<()> {
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_dir() {
                walk(root, &entry.path(), found)?;
            } else if kind.is_file() {
                found.push(entry.path().strip_prefix(root)?.to_path_buf());
            }
        }
        Ok(())
    }
    let mut found = Vec::new();
    if root.is_dir() {
        walk(root, root, &mut found)?;
    }
    found.sort();
    Ok(found)
}

fn copy(
    view: &ConfigView,
    writer: &mut Writer<'_>,
    source: &Path,
    destination: &str,
) -> anyhow::Result<()> {
    let path = view.path(destination)?;
    writer.write_bytes(&path, &fs::read(source)?)?;
    #[cfg(unix)]
    if path.extension().is_some_and(|e| e == "sh") {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755))?;
    }
    Ok(())
}

fn relative(path: &Path) -> anyhow::Result<String> {
    Ok(path
        .components()
        .map(|c| {
            c.as_os_str()
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("non UTF-8 template path"))
        })
        .collect::<anyhow::Result<Vec<_>>>()?
        .join("/"))
}

impl Setup<'_> {
    /// The CLI agents' definitions, `cli_agents/<tool>/<slot>.yml`, over
    /// the workspace's.
    pub(crate) fn install_cli_agents(
        &self,
        view: &ConfigView,
        writer: &mut Writer<'_>,
    ) -> anyhow::Result<()> {
        let source = self.templates.join("intelligences/cli_agents");
        for file in files_under(&source)? {
            if file.components().count() == 2 && file.extension().is_some_and(|e| e == "yml") {
                let destination = format!("intelligences/cli_agents/{}", relative(&file)?);
                copy(view, writer, &source.join(&file), &destination)?;
            }
        }
        Ok(())
    }

    /// The sample commands of `language` (English when it has none), when
    /// the workspace has no command file yet.
    pub(crate) fn install_samples(
        &self,
        view: &ConfigView,
        writer: &mut Writer<'_>,
        language: &str,
    ) -> anyhow::Result<()> {
        if !files_under(&view.path("commands")?)?.is_empty() {
            return Ok(());
        }
        let samples = self.templates.join("sample_commands");
        let mut source = samples.join(language.trim());
        if !source.is_dir() {
            source = samples.join("en");
        }
        for file in files_under(&source)? {
            copy(
                view,
                writer,
                &source.join(&file),
                &format!("commands/{}", relative(&file)?),
            )?;
        }
        Ok(())
    }
}
