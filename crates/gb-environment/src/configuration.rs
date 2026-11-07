//! The workspace's configuration as one run's command reads it: a copy the
//! host takes once, as it prepares the run. The host decides the command from
//! the copy (which file it is, what it declares, what the screen checked, what
//! the guest is told in `resolved.json`), and the microVM is given the copy at
//! the configuration's own path, so a save made while the run starts or goes
//! on reaches neither the command nor what it reads of the configuration. The
//! isolated environment's own declaration (resources, network, grants) and
//! the host's services, such as the member broker, read the workspace's.

use std::io;
use std::path::{Component, Path, PathBuf};

/// A run's copy of `<workspace>/.guildbotics/config`, removed with it.
pub struct Configuration {
    _copy: tempfile::TempDir,
    root: PathBuf,
    source: PathBuf,
}

impl Configuration {
    /// Copies the configuration of `workspace`, as the host spells it, into
    /// the device's own data, where no grant reaches. It is copied under the
    /// workspace's shared write lock, so no save is half made in it, and is
    /// reached without following a link. Directories and files are copied,
    /// and links as links: one that names a place in the configuration by its
    /// absolute path is made relative, so that it names the copy's. A
    /// workspace without a configuration gives an empty copy.
    /// It may wait for the lock as long as a save holds it: an async caller
    /// takes it with [`Configuration::copied`]. A held lock is refused as the
    /// `SharedWriteBusyError` it is.
    pub fn copy(host: &crate::Host, workspace: &Path) -> anyhow::Result<Configuration> {
        let source = normalized(&workspace.join(".guildbotics/config"))?;
        let place = host.data.join("run/configurations");
        std::fs::create_dir_all(&place)?;
        let copy = tempfile::tempdir_in(&place)?;
        let root = normalized(copy.path())?.join("config");
        let configuration = Configuration {
            _copy: copy,
            root,
            source,
        };
        gb_base::shared_writes::SharedWrites::new(
            host.identity_root.clone(),
            Some(workspace.to_owned()),
        )
        .transaction(|_| Ok(configuration.copy_source()?))?;
        Ok(configuration)
    }

    /// [`Configuration::copy`] on a blocking worker.
    pub async fn copied(host: &crate::Host, workspace: &Path) -> anyhow::Result<Configuration> {
        let (host, workspace) = (host.clone(), workspace.to_owned());
        tokio::task::spawn_blocking(move || Configuration::copy(&host, &workspace)).await?
    }

    /// The copy, read in place of the configuration, as the host spells it.
    pub fn path(&self) -> PathBuf {
        self.root.clone()
    }

    /// The configuration it was copied from, as the host spells it.
    pub fn source(&self) -> &Path {
        &self.source
    }

    /// Where `path`, a file of the copy, is in the configuration.
    pub fn original(&self, path: &Path) -> Option<PathBuf> {
        let relative = path.strip_prefix(&self.root).ok()?;
        Some(match relative.as_os_str().is_empty() {
            true => self.source.clone(),
            false => self.source.join(relative),
        })
    }

    /// `text`, said of the copy, as said of the configuration: a file of the
    /// copy is named as the configuration's, which is what a person edits.
    pub fn spoken(&self, text: &str) -> String {
        text.replace(
            &*self.root.to_string_lossy(),
            &self.source.to_string_lossy(),
        )
    }

    fn copy_source(&self) -> io::Result<()> {
        match gb_base::host_path::directory(&self.source, false) {
            Ok(_) => {}
            Err(gb_base::host_path::PathProblem::Missing(_)) => return Ok(()),
            Err(problem) => return Err(io::Error::other(problem.to_string())),
        }
        std::fs::create_dir(&self.root)?;
        self.copy_directory(&self.source, &self.root)
    }

    /// Each entry of `from` into `to`; what cannot be read is named.
    fn copy_directory(&self, from: &Path, to: &Path) -> io::Result<()> {
        for entry in std::fs::read_dir(from).map_err(named(from))? {
            let entry = entry.map_err(named(from))?;
            let (from, to) = (entry.path(), to.join(entry.file_name()));
            let kind = entry.file_type().map_err(named(&from))?;
            if kind.is_dir() {
                std::fs::create_dir(&to)?;
                self.copy_directory(&from, &to)?;
            } else if kind.is_file() {
                std::fs::copy(&from, &to).map_err(named(&from))?;
            } else if kind.is_symlink() {
                self.copy_link(&from, &to).map_err(named(&from))?;
            }
        }
        Ok(())
    }

    fn copy_link(&self, from: &Path, to: &Path) -> io::Result<()> {
        let mut target = std::fs::read_link(from)?;
        let spelled = target
            .is_absolute()
            .then(|| lexical(&target))
            .and_then(|target| normalized(&target).ok());
        if let Some(inside) = spelled
            .as_deref()
            .and_then(|t| t.strip_prefix(&self.source).ok())
        {
            let depth = from
                .parent()
                .and_then(|parent| parent.strip_prefix(&self.source).ok())
                .map_or(0, |parent| parent.components().count());
            target = std::iter::repeat_n(Component::ParentDir.as_os_str(), depth)
                .collect::<PathBuf>()
                .join(inside);
            if target.as_os_str().is_empty() {
                target = Component::CurDir.as_os_str().into();
            }
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(&target, to)?;
        #[cfg(not(unix))]
        let _ = (target, to);
        Ok(())
    }
}

/// `path` with its `.` and `..` taken by their spelling, as a link's target
/// names a place.
fn lexical(path: &Path) -> PathBuf {
    let mut plain = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                plain.pop();
            }
            other => plain.push(other),
        }
    }
    plain
}

/// An error of reading `path`, which it names.
fn named(path: &Path) -> impl FnOnce(io::Error) -> io::Error + '_ {
    move |e| io::Error::new(e.kind(), format!("{}: {e}", path.display()))
}

fn normalized(path: &Path) -> io::Result<PathBuf> {
    gb_base::host_path::normalize(path).map_err(|e| io::Error::other(e.to_string()))
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    /// A device whose data and identities are beside `workspace`.
    fn host(workspace: &tempfile::TempDir) -> crate::Host {
        let beside = workspace.path().join("device");
        crate::Host {
            data: beside.join("data"),
            home: beside.join("home"),
            package: beside.join("package"),
            identity_root: beside.join("identity"),
        }
    }

    #[test]
    fn a_copy_keeps_what_the_configuration_held_when_it_was_taken() {
        let workspace = tempfile::tempdir().unwrap();
        let config = workspace.path().join(".guildbotics/config");
        std::fs::create_dir_all(config.join("commands/reports")).unwrap();
        let script = config.join("commands/reports/weekly.sh");
        std::fs::write(&script, "echo old\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::os::unix::fs::symlink("reports", config.join("commands/alias")).unwrap();
        let source = normalized(&config).unwrap();
        std::os::unix::fs::symlink(
            source.join("commands/reports"),
            config.join("commands/full"),
        )
        .unwrap();
        std::os::unix::fs::symlink("/elsewhere", config.join("commands/out")).unwrap();
        // As the OS hands the name out, not as the host spells it.
        std::os::unix::fs::symlink(
            config.join("commands/reports"),
            config.join("commands/spelled"),
        )
        .unwrap();
        std::os::unix::fs::symlink(&source, config.join("whole")).unwrap();
        std::os::unix::fs::symlink(
            source.join("commands/../commands/reports"),
            config.join("commands/climbing"),
        )
        .unwrap();

        let copy = Configuration::copy(&host(&workspace), workspace.path()).unwrap();
        std::fs::write(&script, "echo new\n").unwrap();

        let copied = copy.path().join("commands/reports/weekly.sh");
        assert_eq!(std::fs::read_to_string(&copied).unwrap(), "echo old\n");
        assert_eq!(
            std::fs::metadata(&copied).unwrap().permissions().mode() & 0o777,
            0o755
        );
        for link in [
            "alias",
            "full",
            "spelled",
            "climbing",
            "../whole/commands/reports",
        ] {
            let through = copy.path().join(format!("commands/{link}/weekly.sh"));
            assert_eq!(
                std::fs::read_to_string(through).unwrap(),
                "echo old\n",
                "{link}"
            );
        }
        assert_eq!(
            std::fs::read_link(copy.path().join("commands/full")).unwrap(),
            Path::new("../commands/reports")
        );
        assert_eq!(
            std::fs::read_link(copy.path().join("commands/out")).unwrap(),
            Path::new("/elsewhere")
        );
        assert_eq!(
            std::fs::read_link(copy.path().join("whole")).unwrap(),
            Path::new(".")
        );
        assert_eq!(copy.source(), source);
        assert_eq!(
            copy.original(&copied),
            Some(source.join("commands/reports/weekly.sh"))
        );
        assert_eq!(copy.original(&script), None);
        assert_eq!(copy.original(&copy.path()), Some(source.clone()));
        assert_eq!(
            copy.spoken(&format!("{}: not a mapping", copied.display())),
            format!(
                "{}: not a mapping",
                source.join("commands/reports/weekly.sh").display()
            )
        );
        let kept = copy.path();
        drop(copy);
        assert!(!kept.exists(), "the copy goes with its run");
    }

    #[test]
    fn a_configuration_reached_through_a_link_or_unreadable_is_refused_by_name() {
        let workspace = tempfile::tempdir().unwrap();
        let elsewhere = workspace.path().join("elsewhere");
        std::fs::create_dir_all(elsewhere.join("config")).unwrap();
        std::os::unix::fs::symlink(&elsewhere, workspace.path().join(".guildbotics")).unwrap();
        assert!(Configuration::copy(&host(&workspace), workspace.path()).is_err());

        let workspace = tempfile::tempdir().unwrap();
        let locked = workspace.path().join(".guildbotics/config/team/locked.yml");
        std::fs::create_dir_all(locked.parent().unwrap()).unwrap();
        std::fs::write(&locked, "x: 1\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
        // A process that reads it anyway (root) has nothing to refuse.
        if std::fs::read(&locked).is_err() {
            let refused = Configuration::copy(&host(&workspace), workspace.path())
                .err()
                .unwrap();
            assert!(refused.to_string().contains("locked.yml"), "{refused}");
        }
    }

    #[test]
    fn a_workspace_without_a_configuration_gives_an_empty_copy() {
        let workspace = tempfile::tempdir().unwrap();
        let copy = Configuration::copy(&host(&workspace), workspace.path()).unwrap();
        assert!(!copy.path().exists());
    }
}
