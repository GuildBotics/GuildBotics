//! What the owner changes from the GUI (Settings → CI): whether the service
//! runs the runner, the limits the runner puts on every job, and the
//! runner's image cache. The first two are `branvil.toml` settings: they are
//! written there (keeping the rest of the file as it is) and applied at once,
//! without restarting the service; the supervised runner reads the limits
//! from the same file before every claim (`supervise.rs`). The image cache is the one of the
//! supervised runner, on this machine (`[ci.microsandbox] home`); a runner
//! started by hand elsewhere keeps its own, managed with `branvil runner pull`.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tokio::sync::watch;

use super::config::{CiConfig, MicrosandboxConfig, RunnerMode};
use crate::error::{AppError, AppResult};
use crate::runner::executor::{self, Microsandbox};
use crate::util;

/// The limits the GUI edits (`[ci.executor]`).
#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    pub max_cpus: u32,
    pub max_memory_mib: u64,
    pub max_timeout_secs: u64,
    pub cache_max_mib: u64,
    pub max_jobs: u32,
}

impl Limits {
    fn of(ci: &CiConfig) -> Self {
        Self {
            max_cpus: ci.executor.max_cpus,
            max_memory_mib: ci.executor.max_memory_mib,
            max_timeout_secs: ci.executor.max_timeout_secs,
            cache_max_mib: ci.executor.cache_max_mib,
            max_jobs: ci.executor.max_jobs,
        }
    }

    /// The time limit has the range of a pipeline's `timeout_secs`, so that
    /// any value a pipeline gives can be the job's own.
    fn check(&self) -> AppResult<()> {
        let out = |what: &str, range: &str| {
            Err(AppError::invalid(format!("{what} must be within {range}.")))
        };
        if !(1..=64).contains(&self.max_cpus) {
            return out("max_cpus", "1–64");
        }
        if !(128..=65536).contains(&self.max_memory_mib) {
            return out("max_memory_mib", "128–65536");
        }
        if !(1..=86400).contains(&self.max_timeout_secs) {
            return out("max_timeout_secs", "1–86400");
        }
        if self.cache_max_mib > 1 << 20 {
            return out("cache_max_mib", "0–1048576");
        }
        if !(1..=64).contains(&self.max_jobs) {
            return out("max_jobs", "1–64");
        }
        Ok(())
    }
}

/// One pull or load of an image into the cache, the latest kept.
#[derive(Serialize, Clone, Debug)]
pub struct Operation {
    pub kind: &'static str,
    pub target: String,
    pub state: &'static str,
    pub detail: Option<String>,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub loaded: Vec<Loaded>,
}

#[derive(Serialize, Clone, Debug)]
pub struct Loaded {
    pub reference: String,
}

pub struct Control {
    file: PathBuf,
    microsandbox: MicrosandboxConfig,
    /// The mode the supervisor follows.
    mode: watch::Sender<RunnerMode>,
    limits: Mutex<Limits>,
    operation: Mutex<Option<Operation>>,
    /// Serializes the writes of the configuration file.
    writing: tokio::sync::Mutex<()>,
}

impl Control {
    pub fn new(file: PathBuf, ci: &CiConfig) -> Self {
        Self {
            file,
            microsandbox: ci.microsandbox.clone(),
            mode: watch::Sender::new(ci.runner),
            limits: Mutex::new(Limits::of(ci)),
            operation: Mutex::new(None),
            writing: tokio::sync::Mutex::new(()),
        }
    }

    pub fn subscribe(&self) -> watch::Receiver<RunnerMode> {
        self.mode.subscribe()
    }

    pub fn mode(&self) -> RunnerMode {
        *self.mode.borrow()
    }

    pub fn limits(&self) -> Limits {
        *self.limits.lock().unwrap()
    }

    /// Saves the mode and starts or stops the supervised runner. The mode
    /// already in effect changes nothing (a running job goes on).
    pub async fn set_mode(&self, mode: RunnerMode) -> AppResult<()> {
        let _w = self.writing.lock().await;
        if self.mode() == mode {
            return Ok(());
        }
        let value = match mode {
            RunnerMode::Auto => "auto",
            RunnerMode::Off => "off",
        };
        self.persist(&["ci"], |t| {
            set(t, "runner", value);
        })
        .await?;
        self.mode.send_replace(mode);
        Ok(())
    }

    /// Saves the limits; the runner applies them from its next claim, and
    /// the jobs it runs keep theirs.
    pub async fn set_limits(&self, limits: Limits) -> AppResult<()> {
        limits.check()?;
        let _w = self.writing.lock().await;
        if self.limits() == limits {
            return Ok(());
        }
        self.persist(&["ci", "executor"], |t| {
            set(t, "max_cpus", i64::from(limits.max_cpus));
            set(t, "max_jobs", i64::from(limits.max_jobs));
            for (key, v) in [
                ("max_memory_mib", limits.max_memory_mib),
                ("max_timeout_secs", limits.max_timeout_secs),
                ("cache_max_mib", limits.cache_max_mib),
            ] {
                set(t, key, v as i64);
            }
        })
        .await?;
        *self.limits.lock().unwrap() = limits;
        Ok(())
    }

    /// Edits the table at `path` of the configuration file, keeping comments
    /// and every other setting, and writes it back when it still reads as a
    /// configuration.
    async fn persist(
        &self,
        path: &[&str],
        edit: impl FnOnce(&mut dyn toml_edit::TableLike),
    ) -> AppResult<()> {
        let refused = |why: String| {
            AppError::conflict(format!("{} cannot be updated: {why}", self.file.display()))
        };
        let mut doc = match tokio::fs::read_to_string(&self.file).await {
            Ok(text) => text
                .parse::<toml_edit::DocumentMut>()
                .map_err(|e| refused(e.to_string()))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Default::default(),
            Err(e) => {
                return Err(AppError::internal(format!(
                    "reading the configuration: {e}"
                )));
            }
        };
        edit(section(&mut doc, path).map_err(refused)?);
        let text = doc.to_string();
        toml::from_str::<crate::config::ConfigFile>(&text).map_err(|e| refused(e.to_string()))?;
        write_config(&self.file, &text)
            .await
            .map_err(|e| AppError::internal(format!("writing {}: {e}", self.file.display())))
    }

    /// The supervised runner's image cache, as the runner `runner` would
    /// use it.
    pub async fn microsandbox(&self, runner: &str) -> Microsandbox {
        let path = self.microsandbox.msb_path.clone();
        let msb = tokio::task::spawn_blocking(move || executor::resolve_msb(path.as_deref()))
            .await
            .unwrap_or_else(|e| executor::Msb::Missing(format!("resolving msb failed: {e}")));
        let home = self
            .microsandbox
            .home
            .clone()
            .unwrap_or_else(|| executor::branvil_home().join("msb-home"));
        Microsandbox::new(msb, home, runner)
    }

    pub fn operation(&self) -> Option<Operation> {
        self.operation.lock().unwrap().clone()
    }

    /// Records a new operation, refused while another one runs.
    pub fn begin(&self, kind: &'static str, target: &str) -> AppResult<()> {
        let mut op = self.operation.lock().unwrap();
        if op.as_ref().is_some_and(|o| o.state == "running") {
            return Err(AppError::conflict(
                "Another image operation is running; wait for it to finish.",
            ));
        }
        *op = Some(Operation {
            kind,
            target: target.to_string(),
            state: "running",
            detail: None,
            started_at: util::now(),
            finished_at: None,
            loaded: Vec::new(),
        });
        Ok(())
    }

    pub fn end(&self, result: Result<Vec<Loaded>, String>) {
        if let Some(op) = self.operation.lock().unwrap().as_mut() {
            op.finished_at = Some(util::now());
            match result {
                Ok(loaded) => {
                    op.state = "succeeded";
                    op.loaded = loaded;
                }
                Err(e) => {
                    op.state = "failed";
                    op.detail = Some(e);
                }
            }
        }
    }
}

/// Sets `key` of `table`, keeping the key's comments when it is there.
fn set(table: &mut dyn toml_edit::TableLike, key: &str, value: impl Into<toml_edit::Value>) {
    match table.get_mut(key) {
        Some(item) => *item = toml_edit::value(value),
        None => {
            table.insert(key, toml_edit::value(value));
        }
    }
}

/// The table at `path`, made when missing; an inline table is one too.
fn section<'a>(
    doc: &'a mut toml_edit::DocumentMut,
    path: &[&str],
) -> Result<&'a mut dyn toml_edit::TableLike, String> {
    let mut table: &mut dyn toml_edit::TableLike = doc.as_table_mut();
    for key in path {
        table = table
            .entry(key)
            .or_insert(toml_edit::table())
            .as_table_like_mut()
            .ok_or_else(|| format!("{} is not a table", path.join(".")))?;
    }
    Ok(table)
}

/// Writes the configuration whole (a reader never sees half a file), where
/// a link to it points and with the permissions it had.
async fn write_config(file: &Path, text: &str) -> std::io::Result<()> {
    use tokio::io::AsyncWriteExt;
    let file = tokio::fs::canonicalize(file)
        .await
        .unwrap_or_else(|_| file.to_path_buf());
    let tmp = file.with_extension(format!("toml.{}", util::random_secret::<6>()));
    let written = async {
        let mut f = tokio::fs::File::create(&tmp).await?;
        f.write_all(text.as_bytes()).await?;
        f.sync_all().await?;
        if let Ok(meta) = tokio::fs::metadata(&file).await {
            tokio::fs::set_permissions(&tmp, meta.permissions()).await?;
        }
        tokio::fs::rename(&tmp, &file).await
    }
    .await;
    if written.is_err() {
        let _ = tokio::fs::remove_file(&tmp).await;
    }
    written
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits() -> Limits {
        Limits {
            max_cpus: 8,
            max_memory_mib: 8192,
            max_timeout_secs: 5400,
            cache_max_mib: 4096,
            max_jobs: 3,
        }
    }

    #[tokio::test]
    async fn settings_are_saved_keeping_the_rest_of_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("branvil.toml");
        std::fs::write(
            &file,
            "# Branvil\nlisten = \"127.0.0.1:7878\"\n\n[ci]\n# who runs jobs\nrunner = \"off\"\nqueue_max = 9\n",
        )
        .unwrap();
        let control = Control::new(file.clone(), &CiConfig::default());
        let mut seen = control.subscribe();
        control.set_mode(RunnerMode::Auto).await.unwrap();
        control.set_limits(limits()).await.unwrap();
        let text = std::fs::read_to_string(&file).unwrap();
        assert!(
            text.contains("# Branvil") && text.contains("# who runs jobs"),
            "{text}"
        );
        let parsed: crate::config::ConfigFile = toml::from_str(&text).unwrap();
        assert_eq!(parsed.ci.runner, RunnerMode::Auto);
        assert_eq!(parsed.ci.queue_max, 9);
        assert_eq!(parsed.listen.as_deref(), Some("127.0.0.1:7878"));
        assert_eq!(parsed.ci.executor.max_cpus, 8);
        assert_eq!(parsed.ci.executor.max_memory_mib, 8192);
        assert_eq!(parsed.ci.executor.max_timeout_secs, 5400);
        assert_eq!(parsed.ci.executor.cache_max_mib, 4096);
        assert_eq!(parsed.ci.executor.max_jobs, 3);
        assert_eq!(control.limits(), limits());
        assert_eq!(*seen.borrow_and_update(), RunnerMode::Auto);
        // New limits do not stop the runner: it reads them before its next
        // claim, and the jobs it runs keep theirs.
        control
            .set_limits(Limits {
                max_jobs: 2,
                ..limits()
            })
            .await
            .unwrap();
        assert!(!seen.has_changed().unwrap(), "a running job would stop");
    }

    #[tokio::test]
    async fn settings_already_in_effect_change_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("branvil.toml");
        let control = Control::new(file.clone(), &CiConfig::default());
        let seen = control.subscribe();
        control.set_mode(RunnerMode::Off).await.unwrap();
        control
            .set_limits(Limits::of(&CiConfig::default()))
            .await
            .unwrap();
        assert!(!seen.has_changed().unwrap(), "a running job would stop");
        assert!(!file.exists());
        control.set_limits(limits()).await.unwrap();
        std::fs::remove_file(&file).unwrap();
        control.set_limits(limits()).await.unwrap();
        assert!(!file.exists(), "the limits in effect are not written again");
    }

    #[tokio::test]
    async fn inline_tables_are_tables_too() {
        let dir = tempfile::tempdir().unwrap();
        for text in [
            "ci = { runner = \"off\" }\n",
            "[ci]\nexecutor = { max_cpus = 2 }\n",
            "ci.runner = \"off\"\n",
        ] {
            let file = dir.path().join("branvil.toml");
            std::fs::write(&file, text).unwrap();
            let control = Control::new(file.clone(), &CiConfig::default());
            control.set_mode(RunnerMode::Auto).await.unwrap();
            control.set_limits(limits()).await.unwrap();
            let parsed: crate::config::ConfigFile =
                toml::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
            assert_eq!(parsed.ci.runner, RunnerMode::Auto, "{text}");
            assert_eq!(parsed.ci.executor.max_cpus, 8, "{text}");
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn the_file_keeps_its_permissions_and_its_link() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let real = dir.path().join("real.toml");
        std::fs::write(&real, "[ci]\n").unwrap();
        std::fs::set_permissions(&real, std::fs::Permissions::from_mode(0o600)).unwrap();
        let link = dir.path().join("branvil.toml");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let control = Control::new(link.clone(), &CiConfig::default());
        control.set_mode(RunnerMode::Auto).await.unwrap();
        assert!(
            std::fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert!(
            std::fs::read_to_string(&real)
                .unwrap()
                .contains("runner = \"auto\"")
        );
        let mode = std::fs::metadata(&real).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[tokio::test]
    async fn a_file_that_is_no_configuration_is_refused_not_overwritten() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("branvil.toml");
        std::fs::write(&file, "unknown_setting = 1\n").unwrap();
        let control = Control::new(file.clone(), &CiConfig::default());
        assert_eq!(
            control.set_mode(RunnerMode::Auto).await.unwrap_err().code,
            "conflict"
        );
        assert_eq!(
            std::fs::read_to_string(&file).unwrap(),
            "unknown_setting = 1\n"
        );
    }

    #[tokio::test]
    async fn a_missing_file_is_created() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("branvil.toml");
        let control = Control::new(file.clone(), &CiConfig::default());
        control.set_mode(RunnerMode::Auto).await.unwrap();
        let parsed: crate::config::ConfigFile =
            toml::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
        assert_eq!(parsed.ci.runner, RunnerMode::Auto);
    }

    #[tokio::test]
    async fn limits_out_of_range_change_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("branvil.toml");
        let control = Control::new(file.clone(), &CiConfig::default());
        for bad in [
            Limits {
                max_cpus: 0,
                ..limits()
            },
            Limits {
                max_cpus: 65,
                ..limits()
            },
            Limits {
                max_memory_mib: 64,
                ..limits()
            },
            Limits {
                max_timeout_secs: 0,
                ..limits()
            },
            Limits {
                cache_max_mib: (1 << 20) + 1,
                ..limits()
            },
        ] {
            let e = control.set_limits(bad).await.unwrap_err();
            assert_eq!(e.code, "invalid", "{bad:?}");
        }
        assert!(!file.exists());
        assert_eq!(control.limits(), Limits::of(&CiConfig::default()));
    }

    #[tokio::test]
    async fn an_unreadable_file_is_not_overwritten() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("branvil.toml");
        std::fs::write(&file, "listen = \n").unwrap();
        let control = Control::new(file.clone(), &CiConfig::default());
        assert!(control.set_mode(RunnerMode::Auto).await.is_err());
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "listen = \n");
        assert_eq!(control.mode(), RunnerMode::Off);
    }

    #[test]
    fn one_image_operation_at_a_time() {
        let control = Control::new(PathBuf::from("/nonexistent"), &CiConfig::default());
        control.begin("load", "/a.tar").unwrap();
        assert_eq!(control.begin("pull", "x").unwrap_err().code, "conflict");
        control.end(Err("broken".into()));
        let op = control.operation().unwrap();
        assert_eq!((op.state, op.detail.as_deref()), ("failed", Some("broken")));
        control.begin("pull", "x").unwrap();
    }
}
