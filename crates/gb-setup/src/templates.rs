//! Shipped templates are passed as bytes, keeping LF stable on every OS.
use super::*;

impl Setup<'_> {
    /// Install CLI definitions and sample commands. Existing command trees are
    /// left intact; every destination is checked before the first write.
    pub fn install_templates(&self, files: &BTreeMap<String, Vec<u8>>) -> anyhow::Result<()> {
        self.repository.write(
            &Revisions::new(),
            |view, writer| {
                let commands_empty = view.tree_revisions("commands")?.len() == 1;
                let mut destinations = Vec::new();
                for (name, bytes) in files {
                    let sample = name.starts_with("commands/");
                    ensure!(
                        sample || name.starts_with("intelligences/cli_agents/"),
                        "not a setup template"
                    );
                    let path = view.path(name)?;
                    if !sample || commands_empty {
                        destinations.push((path, bytes));
                    }
                }
                for (path, bytes) in destinations {
                    writer.write_bytes(&path, bytes)?;
                    #[cfg(unix)]
                    if path.extension().is_some_and(|e| e == "sh") {
                        use std::os::unix::fs::PermissionsExt;
                        fs::set_permissions(path, fs::Permissions::from_mode(0o755))?;
                    }
                }
                Ok(())
            },
            |_| Ok(Revisions::new()),
        )?;
        Ok(())
    }
}
