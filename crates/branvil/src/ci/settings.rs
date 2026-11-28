//! Owner settings for CI runner admission, job resources, and the Hub's
//! image cache. Config changes are persisted before applying them to claims.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::Serialize;

use super::config::{
    CiConfig, JobLimits as Limits, MicrosandboxConfig, RunnerMode, RunnerSettings,
};
use super::supervise::AUTO_RUNNER;
use crate::error::{AppError, AppResult};
use crate::runner::executor::{self, Microsandbox};
use crate::util;

/// The time limit has the range of a pipeline's `timeout_secs`, so that
/// any value a pipeline gives can be the job's own.
fn check(limits: Limits) -> AppResult<()> {
    let out =
        |what: &str, range: &str| Err(AppError::invalid(format!("{what} must be within {range}.")));
    if !(1..=64).contains(&limits.max_cpus) {
        return out("max_cpus", "1–64");
    }
    if !(128..=65536).contains(&limits.max_memory_mib) {
        return out("max_memory_mib", "128–65536");
    }
    if !(1024..=1 << 20).contains(&limits.work_quota_mib) {
        return out("work_quota_mib", "1024–1048576");
    }
    if !(1..=86400).contains(&limits.max_timeout_secs) {
        return out("max_timeout_secs", "1–86400");
    }
    if limits.cache_max_mib > 1 << 20 {
        return out("cache_max_mib", "0–1048576");
    }
    if !(1..=64).contains(&limits.max_jobs) {
        return out("max_jobs", "1–64");
    }
    if limits.memory_reserve_percent > 100 {
        return out("memory_reserve_percent", "0–100");
    }
    if limits.cpu_multiplier_percent > 6400 {
        return out("cpu_multiplier_percent", "0–6400");
    }
    Ok(())
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

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LimitsSource {
    Default,
    Runner,
}

pub struct Control {
    file: PathBuf,
    microsandbox: MicrosandboxConfig,
    #[cfg(feature = "host")]
    pub(crate) vm_home: std::sync::OnceLock<PathBuf>,
    limits: Mutex<(Limits, BTreeMap<String, RunnerSettings>)>,
    operation: Mutex<Option<Operation>>,
    /// Serializes the writes of the configuration file.
    writing: tokio::sync::Mutex<()>,
}

impl Control {
    pub fn new(file: PathBuf, ci: &CiConfig) -> Self {
        let mut runners = ci.runner_limits.clone();
        if let Some(mode) = ci.runner {
            runners
                .entry(AUTO_RUNNER.into())
                .or_default()
                .ci_enabled
                .get_or_insert(mode == RunnerMode::Auto);
        }
        Self {
            file,
            microsandbox: ci.microsandbox.clone(),
            #[cfg(feature = "host")]
            vm_home: Default::default(),
            limits: Mutex::new((Limits::from(&ci.executor), runners)),
            operation: Mutex::new(None),
            writing: tokio::sync::Mutex::new(()),
        }
    }

    pub fn limits(&self) -> Limits {
        self.limits.lock().unwrap().0
    }

    pub fn ci_enabled(&self, name: &str) -> bool {
        self.limits
            .lock()
            .unwrap()
            .1
            .get(&util::fold(name))
            .and_then(|settings| settings.ci_enabled)
            .unwrap_or(true)
    }

    pub fn has_disabled_runner(&self) -> bool {
        self.limits
            .lock()
            .unwrap()
            .1
            .values()
            .any(|settings| settings.ci_enabled == Some(false))
    }

    pub async fn set_ci_enabled(&self, name: &str, enabled: bool) -> AppResult<()> {
        let _w = self.writing.lock().await;
        let name = util::fold(name);
        if self.ci_enabled(&name) == enabled {
            return Ok(());
        }
        self.persist(&["ci", "runner_limits", &name], |t| {
            set(t, "ci_enabled", enabled)
        })
        .await?;
        self.limits
            .lock()
            .unwrap()
            .1
            .entry(name)
            .or_default()
            .ci_enabled = Some(enabled);
        Ok(())
    }

    pub fn runner_limits(&self, name: &str) -> (Limits, LimitsSource) {
        let (defaults, overrides) = &*self.limits.lock().unwrap();
        match overrides.get(&util::fold(name)).and_then(|r| r.limits) {
            Some(limits) => (limits, LimitsSource::Runner),
            None => (*defaults, LimitsSource::Default),
        }
    }

    /// Saves job resources without changing the snapshot of any held attempt.
    pub async fn set_limits(&self, runner: Option<&str>, limits: Limits) -> AppResult<()> {
        check(limits)?;
        let _w = self.writing.lock().await;
        let name = runner.map(util::fold);
        let path = {
            let state = self.limits.lock().unwrap();
            match &name {
                Some(name) if state.1.get(name).and_then(|r| r.limits) == Some(limits) => {
                    return Ok(());
                }
                Some(name) => vec!["ci", "runner_limits", name],
                None if state.0 == limits => return Ok(()),
                None => vec!["ci", "executor"],
            }
        };
        self.persist(&path, |t| write_limits(t, limits)).await?;
        let mut state = self.limits.lock().unwrap();
        if let Some(name) = name {
            state.1.entry(name).or_default().limits = Some(limits);
        } else {
            state.0 = limits;
        }
        Ok(())
    }

    /// Removes the runner's own settings (`[ci.runner_limits.<name>]`)
    /// with the runner, which `delete` deletes, keeping every other setting.
    /// The file goes first, so that a file that cannot be written deletes
    /// nothing; claims and health reads keep the resources until `delete` has
    /// deleted the runner, so that a runner that comes back meanwhile, which
    /// `delete` then refuses to delete, runs its attempts with them. A
    /// refused `delete` writes them back to the file; when that fails too,
    /// the runner uses the defaults, as the file says.
    pub async fn remove_limits(
        &self,
        runner: &str,
        delete: impl std::future::Future<Output = AppResult<()>>,
    ) -> AppResult<()> {
        let _w = self.writing.lock().await;
        let name = util::fold(runner);
        let Some(settings) = self.limits.lock().unwrap().1.get(&name).copied() else {
            return delete.await;
        };
        self.persist(&["ci"], |ci| {
            let emptied = ci
                .get_mut("runner_limits")
                .and_then(|t| t.as_table_like_mut())
                .is_some_and(|limits| {
                    limits.remove(&name);
                    limits.is_empty()
                });
            if emptied {
                ci.remove("runner_limits");
            }
        })
        .await?;
        if let Err(e) = delete.await {
            let path = ["ci", "runner_limits", name.as_str()];
            if let Err(restore) = self
                .persist(&path, |t| {
                    if let Some(limits) = settings.limits {
                        write_limits(t, limits);
                    }
                    if let Some(enabled) = settings.ci_enabled {
                        set(t, "ci_enabled", enabled);
                    }
                })
                .await
            {
                // What the file says from now on, so that saving them again
                // writes them.
                self.limits.lock().unwrap().1.remove(&name);
                tracing::warn!("restoring the settings of {runner} failed: {restore}");
            }
            return Err(e);
        }
        self.limits.lock().unwrap().1.remove(&name);
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
        let table = section(&mut doc, path).map_err(refused)?;
        edit(table);
        if path == ["ci", "runner_limits", AUTO_RUNNER] && table.contains_key("ci_enabled") {
            section(&mut doc, &["ci"])
                .map_err(refused)?
                .remove("runner");
        }
        let text = doc.to_string();
        toml::from_str::<crate::config::ConfigFile>(&text).map_err(|e| refused(e.to_string()))?;
        write_config(&self.file, &text)
            .await
            .map_err(|e| AppError::internal(format!("writing {}: {e}", self.file.display())))
    }

    /// The supervised runner's image cache, as the runner `runner` would
    /// use it.
    pub async fn microsandbox(&self, runner: &str) -> Microsandbox {
        #[cfg(feature = "host")]
        if let Some(home) = self.vm_home.get() {
            return Microsandbox::hub(home.clone(), runner);
        }
        let path = self.microsandbox.msb_path.clone();
        let msb = tokio::task::spawn_blocking(move || executor::resolve_msb(path.as_deref()))
            .await
            .unwrap_or_else(|e| executor::Msb::Missing(format!("resolving msb failed: {e}")));
        let home = self
            .microsandbox
            .home
            .clone()
            .unwrap_or_else(|| executor::runner_home().join("msb-home"));
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

fn write_limits(t: &mut dyn toml_edit::TableLike, limits: Limits) {
    set(t, "max_cpus", i64::from(limits.max_cpus));
    set(t, "max_jobs", i64::from(limits.max_jobs));
    set(
        t,
        "memory_reserve_percent",
        i64::from(limits.memory_reserve_percent),
    );
    set(
        t,
        "cpu_multiplier_percent",
        i64::from(limits.cpu_multiplier_percent),
    );
    for (key, v) in [
        ("max_memory_mib", limits.max_memory_mib),
        ("work_quota_mib", limits.work_quota_mib),
        ("max_timeout_secs", limits.max_timeout_secs),
        ("cache_max_mib", limits.cache_max_mib),
    ] {
        set(t, key, v as i64);
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
            work_quota_mib: 32768,
            max_timeout_secs: 5400,
            cache_max_mib: 4096,
            max_jobs: 3,
            memory_reserve_percent: 15,
            cpu_multiplier_percent: 125,
        }
    }

    #[test]
    fn hub_switch_uses_the_legacy_value_and_runners_default_to_on() {
        for (text, enabled) in [
            ("", true),
            ("[ci]\nrunner = \"auto\"", true),
            ("[ci]\nrunner = \"off\"", false),
            (
                "[ci]\nrunner = \"off\"\n[ci.runner_limits.hub-runner]\nci_enabled = true",
                true,
            ),
        ] {
            let parsed: crate::config::ConfigFile = toml::from_str(text).unwrap();
            let control = Control::new("config.toml".into(), &parsed.ci);
            assert_eq!(control.ci_enabled(AUTO_RUNNER), enabled, "{text}");
            assert!(control.ci_enabled("other"), "{text}");
        }
    }

    #[tokio::test]
    async fn settings_are_saved_keeping_the_rest_of_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("config.toml");
        let before = "# Branvil\n[storage]\ncache_size_mib = 16\n\n[ci]\nrunner = \"off\"\n# queue size\nqueue_max = 9\n";
        std::fs::write(&file, before).unwrap();
        let parsed: crate::config::ConfigFile = toml::from_str(before).unwrap();
        let control = Control::new(file.clone(), &parsed.ci);
        control.set_ci_enabled(AUTO_RUNNER, true).await.unwrap();
        control.set_ci_enabled("remote", false).await.unwrap();
        control.set_limits(None, limits()).await.unwrap();
        assert!(matches!(
            control.runner_limits("remote"),
            (_, LimitsSource::Default)
        ));
        control
            .set_limits(
                Some("remote"),
                Limits {
                    max_jobs: 2,
                    ..limits()
                },
            )
            .await
            .unwrap();

        let text = std::fs::read_to_string(&file).unwrap();
        assert!(
            text.contains("# Branvil") && text.contains("# queue size"),
            "{text}"
        );
        let parsed: crate::config::ConfigFile = toml::from_str(&text).unwrap();
        assert!(parsed.ci.runner.is_none());
        assert_eq!(parsed.ci.queue_max, 9);
        assert_eq!(parsed.storage.cache_size_mib, 16);
        let restored = Control::new(file, &parsed.ci);
        assert!(restored.ci_enabled(AUTO_RUNNER));
        assert!(!restored.ci_enabled("remote"));
        assert_eq!(restored.limits(), limits());
        assert_eq!(restored.runner_limits("remote").0.max_jobs, 2);
    }

    /// A deleted runner's own job resources leave the file, and nothing else
    /// does: the other runners' resources, the defaults, and the comments.
    /// Until the runner is deleted, claims keep reading them: a runner that
    /// comes back meanwhile, whose deletion is then refused, runs with its
    /// own resources, and the refusal writes them back to the file.
    #[tokio::test]
    async fn removing_a_runners_limits_keeps_the_rest_of_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("config.toml");
        std::fs::write(
            &file,
            "# Branvil\n[ci]\n# who runs jobs\nrunner = \"off\"\n",
        )
        .unwrap();
        let control = Control::new(file.clone(), &CiConfig::default());
        control
            .set_limits(Some("Old-Runner"), limits())
            .await
            .unwrap();
        control.set_ci_enabled("old-runner", false).await.unwrap();
        control.set_limits(Some("kept"), limits()).await.unwrap();
        let before = std::fs::read_to_string(&file).unwrap();
        let own = |name: &str| match control.runner_limits(name) {
            (l, LimitsSource::Runner) => Some(l),
            (_, LimitsSource::Default) => None,
        };
        let parsed = |text: &str| -> crate::config::ConfigFile { toml::from_str(text).unwrap() };

        // Refused: the runner reported while it was being deleted.
        let refused = control
            .remove_limits("old-runner", async {
                assert_eq!(own("old-runner"), Some(limits()), "a claim meanwhile");
                assert!(
                    !std::fs::read_to_string(&file)
                        .unwrap()
                        .contains("old-runner")
                );
                Err(AppError::conflict_code("runner_online", "back"))
            })
            .await;
        assert_eq!(refused.unwrap_err().code, "runner_online");
        assert_eq!(own("old-runner"), Some(limits()));
        assert_eq!(
            parsed(&std::fs::read_to_string(&file).unwrap())
                .ci
                .runner_limits,
            parsed(&before).ci.runner_limits
        );

        control.set_limits(None, limits()).await.unwrap();
        control
            .remove_limits("old-runner", async {
                assert_eq!(own("old-runner"), Some(limits()), "a claim meanwhile");
                Ok(())
            })
            .await
            .unwrap();
        let text = std::fs::read_to_string(&file).unwrap();
        assert!(
            text.contains("# Branvil") && text.contains("# who runs jobs"),
            "{text}"
        );
        assert_eq!(
            parsed(&text).ci.runner_limits.keys().collect::<Vec<_>>(),
            ["kept"]
        );
        assert_eq!(parsed(&text).ci.executor.max_cpus, 8);
        assert_eq!(own("old-runner"), None);
        assert_eq!(own("kept"), Some(limits()));

        // A runner without resources of its own leaves the file as it is.
        control
            .remove_limits("old-runner", async { Ok(()) })
            .await
            .unwrap();
        assert_eq!(std::fs::read_to_string(&file).unwrap(), text);

        // The last one takes the emptied table with it.
        control
            .remove_limits("kept", async { Ok(()) })
            .await
            .unwrap();
        let text = std::fs::read_to_string(&file).unwrap();
        assert!(!text.contains("runner_limits"), "{text}");
        assert!(text.contains("[ci.executor]"), "{text}");

        // A refusal whose write-back fails too leaves the runner with the
        // defaults, as the file says, so that saving them again writes them.
        control.set_limits(Some("back"), limits()).await.unwrap();
        let refused = control
            .remove_limits("back", async {
                std::fs::write(&file, "listen = \n").unwrap();
                Err(AppError::conflict_code("runner_online", "back"))
            })
            .await;
        assert_eq!(refused.unwrap_err().code, "runner_online");
        assert_eq!(own("back"), None);
    }

    #[tokio::test]
    async fn settings_already_in_effect_change_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("config.toml");
        let control = Control::new(file.clone(), &CiConfig::default());
        control.set_ci_enabled(AUTO_RUNNER, true).await.unwrap();
        control
            .set_limits(None, Limits::from(&CiConfig::default().executor))
            .await
            .unwrap();
        assert!(!file.exists());
        control.set_limits(None, limits()).await.unwrap();
        std::fs::remove_file(&file).unwrap();
        control.set_limits(None, limits()).await.unwrap();
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
            let file = dir.path().join("config.toml");
            std::fs::write(&file, text).unwrap();
            let control = Control::new(file.clone(), &CiConfig::default());
            control.set_ci_enabled(AUTO_RUNNER, false).await.unwrap();
            control.set_limits(None, limits()).await.unwrap();
            let parsed: crate::config::ConfigFile =
                toml::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
            assert_eq!(
                parsed.ci.runner_limits[AUTO_RUNNER].ci_enabled,
                Some(false),
                "{text}"
            );
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
        let link = dir.path().join("config.toml");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let control = Control::new(link.clone(), &CiConfig::default());
        control.set_ci_enabled(AUTO_RUNNER, false).await.unwrap();
        assert!(
            std::fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert!(
            std::fs::read_to_string(&real)
                .unwrap()
                .contains("ci_enabled = false")
        );
        let mode = std::fs::metadata(&real).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[tokio::test]
    async fn a_file_that_is_no_configuration_is_refused_not_overwritten() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("config.toml");
        std::fs::write(&file, "unknown_setting = 1\n").unwrap();
        let control = Control::new(file.clone(), &CiConfig::default());
        assert_eq!(
            control
                .set_ci_enabled(AUTO_RUNNER, false)
                .await
                .unwrap_err()
                .code,
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
        let file = dir.path().join("config.toml");
        let control = Control::new(file.clone(), &CiConfig::default());
        control.set_ci_enabled(AUTO_RUNNER, false).await.unwrap();
        let parsed: crate::config::ConfigFile =
            toml::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
        assert_eq!(parsed.ci.runner_limits[AUTO_RUNNER].ci_enabled, Some(false));
    }

    /// A job may add 16 GiB to its work directory unless the configuration
    /// says otherwise, and the bounds of the range are accepted.
    #[tokio::test]
    async fn the_work_quota_is_16_gib_by_default() {
        let parsed: crate::config::ConfigFile = toml::from_str("[ci.executor]\n").unwrap();
        assert_eq!(parsed.ci.executor.work_quota_mib, 16384);
        assert_eq!(
            Limits::from(&CiConfig::default().executor).work_quota_mib,
            16384
        );
        let dir = tempfile::tempdir().unwrap();
        let control = Control::new(dir.path().join("config.toml"), &CiConfig::default());
        for quota in [1024, 1 << 20] {
            let limits = Limits {
                work_quota_mib: quota,
                ..limits()
            };
            control.set_limits(None, limits).await.unwrap();
            assert_eq!(control.limits(), limits);
        }
    }

    #[tokio::test]
    async fn limits_out_of_range_change_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("config.toml");
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
                work_quota_mib: 1023,
                ..limits()
            },
            Limits {
                work_quota_mib: (1 << 20) + 1,
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
            let e = control.set_limits(None, bad).await.unwrap_err();
            assert_eq!(e.code, "invalid", "{bad:?}");
        }
        assert!(!file.exists());
        assert_eq!(
            control.limits(),
            Limits::from(&CiConfig::default().executor)
        );
    }

    #[tokio::test]
    async fn an_unreadable_file_is_not_overwritten() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("config.toml");
        std::fs::write(&file, "listen = \n").unwrap();
        let control = Control::new(file.clone(), &CiConfig::default());
        assert!(control.set_ci_enabled(AUTO_RUNNER, false).await.is_err());
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "listen = \n");
        assert!(control.ci_enabled(AUTO_RUNNER));
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
