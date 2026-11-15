//! Client-only removal of Desktop records, never of the user's workspace.
use gb_config::secrets;
use std::{
    fs,
    path::{Path, PathBuf},
};

#[derive(Default)]
pub struct Cleanup {
    language: String,
    pub directories: Vec<PathBuf>,
    pub credentials: Vec<(String, String)>,
}
impl Cleanup {
    pub fn discover(machine: &Path, identity: &Path, language: &str) -> anyhow::Result<Self> {
        let mut plan = Self {
            language: language.into(),
            ..Self::default()
        };
        for workspace in gb_workspace::selection::registered(&machine.join("data"))? {
            plan.directory(&workspace.join(".guildbotics"))?;
        }
        plan.directory(identity)?;
        // This key encrypts only the Desktop's local secret copies.
        if !plan.directories.is_empty() {
            plan.credentials
                .push(("GuildBotics".into(), "agent-environment-credentials".into()));
        }
        let machine = machine.canonicalize()?;
        for directory in &plan.directories {
            for retained in [machine.clone(), machine.join("bin"), machine.join("data")] {
                anyhow::ensure!(
                    !retained.starts_with(directory),
                    "{}",
                    crate::i18n::t(&plan.language, "cli.install.cleanup_invalid", &[])
                );
            }
        }
        Ok(plan)
    }
    fn directory(&mut self, path: &Path) -> anyhow::Result<()> {
        match fs::symlink_metadata(path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.into()),
            Ok(metadata) => anyhow::ensure!(
                metadata.is_dir() && !metadata.is_symlink(),
                "{}",
                crate::i18n::t(&self.language, "cli.install.cleanup_invalid", &[])
            ),
        }
        let path = path.canonicalize()?;
        if self.directories.contains(&path) {
            return Ok(());
        }
        // Legacy Desktop stores names in config and their keychain namespace locally.
        let records = if path.join(".guildbotics").is_dir() {
            path.join(".guildbotics")
        } else {
            path.clone()
        };
        let local = records.join("local/secrets.json");
        if local.is_file() {
            let local: serde_json::Value = serde_json::from_slice(&fs::read(local)?)?;
            let id = local["store_id"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    anyhow::anyhow!(crate::i18n::t(
                        &self.language,
                        "cli.install.cleanup_invalid",
                        &[]
                    ))
                })?;
            anyhow::ensure!(
                id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit()),
                "{}",
                crate::i18n::t(&self.language, "cli.install.cleanup_invalid", &[])
            );
            let index = records.join("config/secrets.yml");
            if index.is_file() {
                let names = secrets::read_secret_index(&records.join("config"))?;
                for name in names {
                    self.credentials.push((format!("GuildBotics/{id}"), name));
                }
            }
        }
        self.directories.push(path);
        Ok(())
    }
    pub fn apply(
        &self,
        confirmed: bool,
        mut delete: impl FnMut(&str, &str) -> anyhow::Result<()>,
    ) -> anyhow::Result<()> {
        if !confirmed {
            return Ok(());
        }
        // Keep indexes until keychain deletion succeeds, so a retry has the same plan.
        for (service, account) in &self.credentials {
            delete(service, account)?;
        }
        let mut directories: Vec<_> = self.directories.iter().collect();
        directories.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
        for directory in directories {
            anyhow::ensure!(
                !directory.is_symlink() && directory.canonicalize()? == *directory,
                "{}",
                crate::i18n::t(&self.language, "cli.install.cleanup_invalid", &[])
            );
            fs::remove_dir_all(directory)?;
        }
        Ok(())
    }
    pub fn remove(&self, confirmed: bool) -> anyhow::Result<()> {
        self.apply(confirmed, secrets::delete_os_credential)
    }
}
