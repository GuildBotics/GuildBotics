use super::*;
use crate::ci::{api::Machine, config::JobLimits, pipeline};

pub(super) const RETURN_MOUNT: &str = "/.branvil-ram-return";
const PROBED: &str = "ram-workspace.probed";
const COMMAND_HEADROOM_MIB: u64 = 1024;

/// The preparation VM uses the same image and guest kernel as the job.
pub(super) fn probe(script: &mut String) {
    script.push_str(&format!(
        "if command -v mount >/dev/null && command -v umount >/dev/null && \
         python3 -I -S -c 'import ctypes, sys, tarfile; assert sys.version_info >= (3, 8)' >/dev/null 2>&1 && \
         unshare --pid --fork --kill-child --mount-proc setpriv --no-new-privs \
         --pdeathsig=keep --reuid={JOB_USER_ID} --regid={JOB_USER_ID} --clear-groups /bin/true >/dev/null 2>&1; then\n  \
         : >{IO_MOUNT}/{PROBED}\nfi\nsync\n"
    ));
}

fn memory_allows(
    ram: &pipeline::RamWorkspace,
    tmp_mib: Option<u32>,
    vm_mib: u64,
    limits: JobLimits,
    machine: &Machine,
) -> bool {
    let guest = u64::from(ram.max_mib) + u64::from(tmp_mib.unwrap_or_default());
    let reserve = limits.memory_reserve_mib(machine.total_memory_mib);
    guest + COMMAND_HEADROOM_MIB <= vm_mib
        && vm_mib.saturating_mul(u64::from(limits.max_jobs))
            <= machine.total_memory_mib.saturating_sub(reserve)
        && vm_mib <= machine.available_memory_mib.saturating_sub(reserve)
        && machine
            .commit
            .as_ref()
            .is_none_or(|commit| vm_mib <= commit.available_mib.saturating_sub(reserve))
}

pub(super) struct RamWorkspace<'a> {
    config: &'a pipeline::RamWorkspace,
    pub(super) returned: PathBuf,
    pub(super) quota_mib: u64,
}

impl<'a> RamWorkspace<'a> {
    pub(super) fn select(
        config: &'a pipeline::RamWorkspace,
        job: &JobRun<'_>,
        used_exact_cache: bool,
        quota_mib: u64,
        machine: impl FnOnce() -> Option<Machine>,
    ) -> Result<Self, String> {
        config.validate()?;
        if !used_exact_cache {
            return Err("no exact cache was restored".into());
        }
        if std::fs::remove_file(job.io.join(PROBED)).is_err() {
            return Err("the guest image or kernel lacks the RAM workspace tools".into());
        }
        let (Some(limits), Some(machine)) = (job.ram_limits, machine()) else {
            return Err("runner memory is unknown".into());
        };
        if !memory_allows(config, job.job.tmpfs_mib, job.memory_mib, limits, &machine) {
            return Err(format!(
                "insufficient memory (VM {} MiB, available {} MiB, {} slots, {}% reserve)",
                job.memory_mib,
                machine.available_memory_mib,
                limits.max_jobs,
                limits.memory_reserve_percent,
            ));
        }
        let returned = job.io.join("ram-return");
        std::fs::create_dir(&returned)
            .map_err(|e| format!("creating artifact return mount: {e}"))?;
        Ok(Self {
            config,
            returned,
            quota_mib,
        })
    }

    pub(super) fn script(&self, job: &JobDef, log_file: bool) -> String {
        let paths: Vec<_> = self
            .config
            .paths
            .iter()
            .map(|p| p.trim_end_matches('/'))
            .collect();
        let artifacts: Vec<_> = job
            .artifacts
            .iter()
            .flat_map(|a| a.paths.iter())
            .map(|p| p.trim_end_matches('/'))
            .collect();
        let args = serde_json::to_string(&(self.config.max_mib, paths, artifacts, JOB_USER_ID))
            .expect("serializing the RAM workspace configuration");
        let quote = |s: &str| format!("'{}'", s.replace('\'', "'\\''"));
        format!(
            "exec python3 -I -S -c {} {} {}",
            quote(include_str!("ram_workspace.py")),
            quote(&args),
            quote(&job_script(&job.commands, log_file)),
        )
    }

    pub(super) fn used_disk(&self) -> bool {
        self.marked("disk")
    }

    pub(super) fn active(&self) -> bool {
        self.marked("pid")
    }

    fn marked(&self, name: &str) -> bool {
        std::fs::symlink_metadata(self.returned.join(name)).is_ok_and(|m| m.is_file())
    }

    pub(super) fn exit_code(&self) -> Option<i32> {
        let mut file = super::super::workspace::open_returned(&self.returned.join("exit"), 8)
            .ok()
            .flatten()?;
        let mut code = String::new();
        std::io::Read::read_to_string(&mut file, &mut code).ok()?;
        code.parse().ok()
    }

    pub(super) fn flush_script() -> String {
        format!(
            "set -e\ncd {RETURN_MOUNT} || exec sync\n[ -f pid ] || exec sync\n[ -f finished ] && exit 0\n\
             [ -f exit ] || kill -USR1 \"$(cat pid)\" || [ -f finished ]\n\
             while [ ! -f finished ]; do sleep 0.2; done\n"
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ci::{api::CommitMemory, config::ExecutorConfig};

    #[test]
    fn ram_fits_inside_the_vm_and_the_runner_budget() {
        let mut machine = Machine {
            logical_cpus: 8,
            total_memory_mib: 20_000,
            available_memory_mib: 18_000,
            commit: None,
        };
        let ram = pipeline::RamWorkspace {
            paths: vec!["output".into()],
            max_mib: 4608,
        };
        let mut limits = JobLimits::from(&ExecutorConfig::default());
        limits.max_jobs = 2;
        let allows =
            |machine: &Machine, limits, vm| memory_allows(&ram, Some(2048), vm, limits, machine);
        assert!(allows(&machine, limits, 8192));
        assert!(!allows(&machine, limits, 6144));
        limits.max_jobs = 3;
        assert!(!allows(&machine, limits, 8192));
        limits.max_jobs = 2;
        machine.available_memory_mib = 10_191;
        assert!(!allows(&machine, limits, 8192));
        machine.available_memory_mib = 10_192;
        assert!(allows(&machine, limits, 8192));
        machine.commit = Some(CommitMemory {
            available_mib: 10_191,
            started_attempts: vec![],
        });
        assert!(!allows(&machine, limits, 8192));
    }

    #[cfg(unix)]
    #[test]
    fn ram_artifacts_contain_only_current_files_and_do_not_follow_links() {
        use std::process::Command;

        let dir = tempfile::tempdir().unwrap();
        let code = format!(
            "{}\n{}",
            include_str!("ram_workspace.py").replace("if __name__ == \"__main__\":", "if False:"),
            r#"
root = Path(sys.argv[1])
WORK, RETURN = root / "work", root / "returned"
upper = root / "upper"
for directory in [WORK / "out/cache", RETURN, upper]:
    directory.mkdir(parents=True, exist_ok=True)
(WORK / "out/cache/stale").write_text("old")
(WORK / "out/cache/deleted").write_text("old")
(WORK / "out/report").write_text("old")
(WORK / "out/unchanged").write_text("old")
(WORK / "out/link").symlink_to(root)
paths = ["out", "out/cache/current", "out/link/work/out/report"]
layers = {WORK / "out/cache": upper}
before = {path: identity(path) for path in files(paths) if produced(path, layers) is None}
(WORK / "out/cache/deleted").unlink()
(WORK / "out/cache/current").write_text("new")
(upper / "current").write_text("new")
(WORK / "out/report").write_text("new report")
collect(paths, layers, before)
with tarfile.open(RETURN / "artifacts.tar") as tar:
    assert set(tar.getnames()) == {"out/cache/current", "out/report"}, tar.getnames()
    assert tar.extractfile("out/cache/current").read() == b"new"
    assert tar.extractfile("out/report").read() == b"new report"
"#,
        );
        let result = Command::new("python3")
            .args(["-I", "-S", "-c", &code])
            .arg(dir.path())
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
    }

    #[tokio::test]
    #[ignore = "requires an attending owner and a disposable microVM runtime"]
    async fn ram_workspace_in_real_microvms() {
        assert_eq!(
            std::env::var("GUILDBOTICS_REAL_EXECUTOR").as_deref(),
            Ok("1")
        );
        assert!(
            std::env::var_os("CI").is_none(),
            "real executor requires an attending owner"
        );
        let data = PathBuf::from(
            std::env::var_os("GUILDBOTICS_REAL_EXECUTOR_DATA")
                .expect("disposable test data required"),
        );
        assert!(data.is_absolute());
        let home = data.join("msb");
        let name = format!("ram-test-{}", std::process::id());
        let runner = if std::env::var("GUILDBOTICS_RAM_TEST_EXECUTOR").as_deref() == Ok("cli") {
            Microsandbox::new(Msb::Found(home.join("bin").join(msb_file())), home, &name)
        } else {
            #[cfg(feature = "host")]
            {
                Microsandbox::hub(home, &name)
            }
            #[cfg(not(feature = "host"))]
            {
                Microsandbox::new(Msb::Found(home.join("bin").join(msb_file())), home, &name)
            }
        };

        let temp = tempfile::tempdir().unwrap();
        let root = plain_path(std::fs::canonicalize(temp.path()).unwrap(), |path| {
            std::fs::canonicalize(path)
        })
        .unwrap();
        let published = root.join("seed/cache.raw");
        let tree = super::super::super::workspace::Tree::from_index(b"").unwrap();
        let mut cache_data = None;
        let limits = JobLimits::from(&ExecutorConfig::default());
        let verify_and_write = r#"
            set -eu
            test "$(id -u):$(id -g)" = 1000:1000
            test ! -x /.branvil-ram-return
            grep -q ' /work/demo .* - overlay ' /proc/self/mountinfo
            grep -q ' /tmp .* - tmpfs ' /proc/self/mountinfo
            test "$(cat demo/input)" = original
            test "$(cat demo/deleted)" = remove
            test ! -e demo/current
            echo updated > demo/input
            rm demo/deleted
            test ! -e demo/deleted
            echo current > demo/current
            echo 'fixture output is ready'
        "#;
        for (case, command, expected) in [
            (
                "seed",
                "mkdir demo; echo original > demo/input; echo remove > demo/deleted; echo stale > demo/old.xml",
                Outcome::Exited(0),
            ),
            ("success", verify_and_write, Outcome::Exited(0)),
            ("failure", "exit 7", Outcome::Exited(7)),
            ("timeout", "sleep 600 & wait", Outcome::TimedOut),
            ("cancel", "sleep 600 & wait", Outcome::Cancelled),
            (
                "miss",
                "test \"$(cat demo/input)\" = original; echo disk > demo/current",
                Outcome::Exited(0),
            ),
            (
                "unsupported",
                "test \"$(cat demo/input)\" = original; echo disk > demo/current",
                Outcome::Exited(0),
            ),
        ] {
            let seed = case == "seed";
            let active = matches!(case, "success" | "failure" | "timeout" | "cancel");
            let commands = if active && case != "success" {
                vec![verify_and_write.to_string(), command.to_string()]
            } else {
                vec![command.to_string()]
            };
            let attempt = root.join(case);
            let (io, work, logs) = (
                attempt.join("io"),
                attempt.join("work"),
                attempt.join("logs"),
            );
            for dir in [&io, &work, &logs] {
                std::fs::create_dir_all(dir).unwrap();
            }
            tar::Builder::new(std::fs::File::create(io.join(CHECKOUT)).unwrap())
                .finish()
                .unwrap();
            let base = attempt.join("base.raw");
            if !seed {
                std::fs::hard_link(&published, &base).unwrap();
            }
            let job = JobDef {
                verified_sha: false,
                name: "ram-test".into(),
                image: "guildbotics/agent-environment:dev".into(),
                commands,
                paths: vec![],
                timeout_secs: None,
                tmpfs_mib: Some(64),
                ram_workspace: Some(pipeline::RamWorkspace {
                    paths: if case == "unsupported" {
                        vec!["demo".into(), "missing".into()]
                    } else {
                        vec!["demo".into()]
                    },
                    max_mib: 64,
                }),
                artifacts: if seed {
                    vec![]
                } else {
                    vec![pipeline::ArtifactDef {
                        name: "test".into(),
                        paths: vec!["demo/".into()],
                    }]
                },
                cache: Some(pipeline::CacheDef {
                    paths: vec!["demo".into()],
                    key_files: vec![],
                }),
            };
            let (log, mut receiver) = mpsc::channel::<Vec<u8>>(64);
            let (stop, cancel) = watch::channel(false);
            let drain = tokio::spawn(async move {
                let mut all = Vec::new();
                while let Some(bytes) = receiver.recv().await {
                    all.extend(bytes);
                    if case == "cancel"
                        && String::from_utf8_lossy(&all).contains("\nfixture output is ready\n")
                    {
                        let _ = stop.send(true);
                    }
                }
                String::from_utf8(all).unwrap()
            });
            let mut kept = None;
            let cache_disk = attempt.join("cache.raw");
            let outcome = runner
                .run(
                    JobRun {
                        attempt_id: case,
                        peak_memory_mib: &mut None,
                        job: &job,
                        image_lock: ImageLock::default(),
                        cpus: 1,
                        memory_mib: 1536,
                        ram_limits: Some(limits),
                        work_quota_mib: 64,
                        timeout_secs: if case == "timeout" { 15 } else { 30 },
                        io: &io,
                        cache_base: (!seed).then_some(base.as_path()),
                        cache_disk: &cache_disk,
                        tree: &tree,
                        cache_data,
                        exact_cache: !seed && case != "miss",
                        kept_data: &mut kept,
                        save_cache: seed,
                        work: &work,
                        log_dir: &logs,
                        network: false,
                        env: vec![],
                    },
                    log,
                    cancel,
                )
                .await;
            let log = drain.await.unwrap();
            assert_eq!(outcome, expected, "{case}: {log}");
            assert_eq!(
                log.contains("RAM workspace enabled"),
                active,
                "{case}: {log}"
            );
            if seed {
                cache_data = Some(kept.expect(&log));
            } else if active && case != "cancel" {
                assert_eq!(
                    std::fs::read_to_string(work.join("demo/current")).unwrap(),
                    "current\n"
                );
                assert_eq!(
                    std::fs::read_to_string(work.join("demo/input")).unwrap(),
                    "updated\n"
                );
                assert!(!work.join("demo/deleted").exists());
                assert!(!work.join("demo/old.xml").exists());
                assert!(!cache_disk.exists());
            } else if case == "cancel" {
                assert!(!work.join("demo/current").exists());
            } else {
                assert!(log.contains("using disk"), "{case}: {log}");
                assert_eq!(
                    std::fs::read_to_string(work.join("demo/current")).unwrap(),
                    "disk\n"
                );
            }
        }
    }
}
