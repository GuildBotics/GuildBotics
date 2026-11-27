use super::*;
use crate::ci::{api::Machine, pipeline};

pub(super) const RETURN_MOUNT: &str = "/.branvil-ram-return";
const PROBED: &str = "ram-workspace.probed";
const CACHE_PROBED: &str = "ram-cache.probed";
pub(super) const CACHE_IMAGE: &str = "cache.raw";

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

pub(super) fn vm_timeout_secs(commands: u64, ram: Option<&RamWorkspace<'_>>) -> u64 {
    let collection = ram.filter(|ram| ram.cache).map_or(0, |_| HELPER_SECS);
    commands + collection + 60
}

/// A completed command has the same cache collection budget as a disk job.
/// A command still running at the deadline only gets the usual flush window.
pub(super) fn timeout_flush(
    ram: Option<&RamWorkspace<'_>>,
    has_artifacts: bool,
) -> Option<Duration> {
    if let Some(ram) = ram.filter(|ram| ram.cache) {
        return Some(if ram.exit_code() == Some(0) {
            Duration::from_secs(HELPER_SECS)
        } else {
            TIMEOUT_FLUSH
        });
    }
    has_artifacts.then_some(TIMEOUT_FLUSH)
}

/// Stopping cache-only collection preserves a successful command outcome.
pub(super) fn cancelled_outcome(ram: Option<&RamWorkspace<'_>>, has_artifacts: bool) -> Outcome {
    if !has_artifacts && ram.is_some_and(|ram| ram.cache && ram.exit_code() == Some(0)) {
        Outcome::Exited(0)
    } else {
        Outcome::Cancelled
    }
}

/// The preparation VM uses the same image and guest kernel as the job.
pub(super) fn probe(script: &mut String, save_cache: bool) {
    script.push_str(&format!(
        "if command -v mount >/dev/null && command -v umount >/dev/null && \
         python3 -I -S -c 'import ctypes, sys, tarfile; assert sys.version_info >= (3, 8)' >/dev/null 2>&1 && \
         unshare --pid --fork --kill-child --mount-proc setpriv --no-new-privs \
         --pdeathsig=keep --reuid={JOB_USER_ID} --regid={JOB_USER_ID} --clear-groups /bin/true >/dev/null 2>&1; then\n  \
         : >{IO_MOUNT}/{PROBED}\n"
    ));
    if save_cache {
        let supervisor = shell_quote(include_str!("ram_workspace.py"));
        script.push_str(&format!(
            "  if python3 -I -S -c {supervisor} --probe-cache >/dev/null 2>&1; then\n    \
             : >{IO_MOUNT}/{CACHE_PROBED}\n  fi\n"
        ));
    }
    script.push_str("fi\nsync\n");
}

pub(super) struct RamWorkspace<'a> {
    config: &'a pipeline::RamWorkspace,
    pub(super) returned: PathBuf,
    pub(super) quota_mib: u64,
    cache: bool,
}

pub(super) fn take_probe(job: &JobRun<'_>) -> bool {
    std::fs::remove_file(job.io.join(PROBED)).is_ok()
}

impl<'a> RamWorkspace<'a> {
    pub(super) fn select(
        config: &'a pipeline::RamWorkspace,
        job: &JobRun<'_>,
        quota_mib: u64,
        cache: bool,
        tools: bool,
        machine: impl FnOnce() -> Option<Machine>,
    ) -> Result<Self, String> {
        config.validate()?;
        if !tools {
            return Err("the guest image or kernel lacks the RAM workspace tools".into());
        }
        let loop_supported = std::fs::remove_file(job.io.join(CACHE_PROBED)).is_ok();
        if cache && !loop_supported {
            return Err(
                "the guest ext4 loop-mount probe failed (requires mkfs.ext4 and mount -o loop)"
                    .into(),
            );
        }
        let (Some(limits), Some(machine)) = (job.ram_limits, machine()) else {
            return Err("runner memory is unknown".into());
        };
        if !config.memory_allows(job.job.tmpfs_mib, job.memory_mib, limits, &machine) {
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
            // The late raw image is sparse but the return mount can account
            // for its full length. Keep room for artifacts and control files.
            quota_mib: if cache { quota_mib * 2 } else { quota_mib },
            cache,
        })
    }

    pub(super) fn saving_cache(&self) -> bool {
        self.cache && !self.used_disk()
    }

    /// Once provisioning starts, let it finish even if the VM stops. An
    /// interrupted volume create may leave an image the runtime cannot list.
    pub(super) async fn run_with_cache<T>(
        &self,
        run: impl Future<Output = T>,
        create: impl Future<Output = Option<JobDisk>>,
    ) -> (T, std::io::Result<()>) {
        tokio::pin!(run);
        tokio::select! {
            biased;
            outcome = &mut run => (outcome, Ok(())),
            () = self.wait_for_cache_request() => {
                let (prepared, outcome) = tokio::join!(self.provide_cache(create), run);
                (outcome, prepared)
            }
        }
    }

    async fn wait_for_cache_request(&self) {
        let mut poll = tokio::time::interval(Duration::from_millis(50));
        while !self.marked("cache.requested") || self.exit_code() != Some(0) {
            poll.tick().await;
        }
    }

    async fn provide_cache(
        &self,
        create: impl Future<Output = Option<JobDisk>>,
    ) -> std::io::Result<()> {
        let ready = self.returned.join(CACHE_IMAGE);
        let result = match create.await {
            Some(disk) => {
                super::super::blocking(move || std::fs::hard_link(disk.path, ready)).await
            }
            None => Err(std::io::Error::other("creating the cache volume failed")),
        };
        if result.is_err() {
            let _ = std::fs::write(self.returned.join("cache.unavailable"), []);
        }
        result
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
        let keep = job.cache.as_ref().filter(|_| self.cache).map(|cache| {
            let cached: Vec<_> = cache.paths.iter().map(String::as_str).collect();
            keep_script(
                CACHE_MOUNT,
                &format!("{RETURN_MOUNT}/cache-result"),
                &cached,
            )
        });
        let args =
            serde_json::to_string(&(self.config.max_mib, paths, artifacts, JOB_USER_ID, keep))
                .expect("serializing the RAM workspace configuration");
        format!(
            "exec python3 -I -S -c {} {} {}",
            shell_quote(include_str!("ram_workspace.py")),
            shell_quote(&args),
            shell_quote(&job_script(&job.commands, log_file)),
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
    use crate::ci::{
        api::CommitMemory,
        config::{ExecutorConfig, JobLimits},
    };

    #[tokio::test]
    async fn cache_volume_is_requested_only_after_the_guest_asks_for_it() {
        let dir = tempfile::tempdir().unwrap();
        let config = pipeline::RamWorkspace {
            paths: vec!["target".into()],
            max_mib: 64,
        };
        let ram = RamWorkspace {
            config: &config,
            returned: dir.path().to_path_buf(),
            quota_mib: 128,
            cache: true,
        };
        let path = dir.path().join("volume.raw");
        let create = || async {
            std::fs::write(&path, b"cache image").unwrap();
            Some(JobDisk {
                name: "cache".into(),
                mib: 64,
                path: path.clone(),
            })
        };
        let (outcome, result) = ram
            .run_with_cache(async { Outcome::Exited(7) }, create())
            .await;
        assert_eq!(outcome, Outcome::Exited(7));
        result.unwrap();
        assert!(!path.exists());
        assert!(
            tokio::time::timeout(Duration::from_millis(10), ram.wait_for_cache_request())
                .await
                .is_err()
        );
        assert!(!path.exists());
        assert!(!dir.path().join(CACHE_IMAGE).exists());
        std::fs::write(dir.path().join("cache.requested"), []).unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(10), ram.wait_for_cache_request())
                .await
                .is_err()
        );
        assert!(!path.exists());
        std::fs::write(dir.path().join("exit"), "0").unwrap();
        let (starting, started) = tokio::sync::oneshot::channel();
        let job = async { started.await.unwrap() };
        let prepare = async {
            starting.send(()).unwrap();
            tokio::task::yield_now().await;
            create().await
        };
        let (_, result) = ram.run_with_cache(job, prepare).await;
        result.unwrap();
        assert_eq!(
            std::fs::read(dir.path().join(CACHE_IMAGE)).unwrap(),
            b"cache image"
        );
    }

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
            |machine: &Machine, limits, vm| ram.memory_allows(Some(2048), vm, limits, machine);
        assert!(allows(&machine, limits, 8192));
        assert!(allows(&machine, limits, 7680));
        assert!(!allows(&machine, limits, 7679));
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
    fn cache_probe_and_save_use_mount_without_losetup() {
        let dir = tempfile::tempdir().unwrap();
        let mut preparation = String::from("set -e\n");
        probe(&mut preparation, true);
        let preparation =
            preparation.replace(IO_MOUNT, &shell_quote(&dir.path().to_string_lossy()));
        let code = r##"
import os
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
tools = root / "bin"
tools.mkdir()
(tools / "python3").symlink_to(sys.executable)

def tool(name, body):
    path = tools / name
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)

for name in ["unshare", "umount", "sync"]:
    tool(name, "exit 0\n")
tool("mkfs.ext4", ': >"$3"\n')
env = {"PATH": str(tools), "TMPDIR": str(root)}
supported = 'test "$1 $2 $3 $4" = "-t ext4 -o loop" && test -f "$5" && test -d "$6"\n'
for case in ["standard", "without-losetup", "without-loop-mount"]:
    tool("mount", supported if case != "without-loop-mount" else "exit 1\n")
    if case == "standard":
        tool("losetup", "exit 99\n")
    else:
        (tools / "losetup").unlink(missing_ok=True)
    subprocess.run(["/bin/sh", "-c", sys.argv[2]], env=env, check=True)
    assert (root / "ram-workspace.probed").is_file(), case
    marker = root / "ram-cache.probed"
    assert marker.is_file() == (case != "without-loop-mount"), case
    if marker.is_file():
        marker.unlink()
        work, returned, cache = (root / (case + suffix) for suffix in [".work", ".return", ".cache"])
        work.mkdir()
        returned.mkdir()
        (work / "output").write_text(case)
        (returned / "cache.raw").touch()
        os.environ.update(env)
        guest = {"__name__": "fixture"}
        exec(sys.argv[3], guest)
        guest.update(WORK=work, RETURN=returned, CACHE=cache)
        guest["save_cache"](
            f'IFS= read -r data <output || :; printf "%s" "$data" >"{cache}/output"; '
            f'printf "1\\n" >"{returned}/cache-result/cache.kept"'
        )
        assert (cache / "output").read_text() == case
        assert (returned / "cache.kept").read_text() == "1\n"
"##;
        let result = std::process::Command::new("python3")
            .args(["-I", "-S", "-c", code])
            .arg(dir.path())
            .arg(preparation)
            .arg(include_str!("ram_workspace.py"))
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
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
from unittest.mock import patch
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
with patch("time.monotonic", side_effect=[0, 60]), patch("__main__.run") as mount:
    try:
        save_cache("true")
    except TimeoutError:
        pass
    else:
        raise AssertionError("a missing cache provisioning response must stop the wait")
    assert (RETURN / "cache.requested").is_file()
    assert not (RETURN / "cache.kept").exists()
    mount.assert_not_called()
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
        let mut disk_identity = None;
        let mut ram_identity = None;
        let limits = JobLimits::from(&ExecutorConfig::default());
        let no_losetup = std::env::var("GUILDBOTICS_RAM_TEST_IMAGE_NO_LOSETUP").ok();
        let no_loop = std::env::var("GUILDBOTICS_RAM_TEST_IMAGE_NO_LOOP").ok();
        let verify_and_write = r#"
            set -eu
            test "$(id -u):$(id -g)" = 1000:1000
            test ! -x /.branvil-ram-return
            test ! -x /.branvil-cache
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
            ("miss", verify_and_write, Outcome::Exited(0)),
            (
                "miss-restore",
                "set -e; test \"$(cat demo/input)\" = updated; test ! -e demo/deleted; test \"$(cat demo/current)\" = current",
                Outcome::Exited(0),
            ),
            (
                "cold",
                "set -e; test ! -e demo; mkdir demo; echo current > demo/current",
                Outcome::Exited(0),
            ),
            (
                "cold-restore",
                "set -e; test \"$(cat demo/current)\" = current; test ! -e demo/input",
                Outcome::Exited(0),
            ),
            (
                "unsupported",
                "set -e; test \"$(cat demo/input)\" = original; echo disk > demo/current",
                Outcome::Exited(0),
            ),
            ("no-losetup", verify_and_write, Outcome::Exited(0)),
            (
                "no-loop",
                "set -e; test \"$(cat demo/input)\" = original; echo disk > demo/current",
                Outcome::Exited(0),
            ),
        ] {
            let image = match case {
                "no-losetup" => no_losetup.as_deref(),
                "no-loop" => no_loop.as_deref(),
                _ => Some("guildbotics/agent-environment:dev"),
            };
            let Some(image) = image else { continue };
            let seed = case == "seed";
            let active = !matches!(case, "seed" | "unsupported" | "cold" | "no-loop");
            let returns_changes = matches!(
                case,
                "success" | "failure" | "timeout" | "miss" | "no-losetup"
            );
            let save_cache = matches!(
                case,
                "seed"
                    | "failure"
                    | "timeout"
                    | "cancel"
                    | "miss"
                    | "cold"
                    | "miss-restore"
                    | "cold-restore"
                    | "no-losetup"
                    | "no-loop"
            );
            let mut commands = vec![
                r#"printf 'fixture identity HOME=%s USER=%s LOGNAME=%s\n' "${HOME-<unset>}" "${USER-<unset>}" "${LOGNAME-<unset>}""#
                    .to_string(),
            ];
            if matches!(case, "failure" | "timeout" | "cancel") {
                commands.push(verify_and_write.to_string());
            }
            if case == "no-losetup" {
                commands.push("! command -v losetup".into());
            }
            commands.push(command.to_string());
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
            if !matches!(case, "seed" | "cold") {
                let source = match case {
                    "miss-restore" => root.join("miss/cache.raw"),
                    "cold-restore" => root.join("cold/cache.raw"),
                    _ => published.clone(),
                };
                std::fs::hard_link(source, &base).unwrap();
            }
            let job = JobDef {
                verified_sha: false,
                name: "ram-test".into(),
                image: image.into(),
                commands,
                paths: vec![],
                timeout_secs: None,
                tmpfs_mib: Some(64),
                ram_workspace: (!seed).then(|| pipeline::RamWorkspace {
                    paths: if case == "unsupported" {
                        vec!["demo/input".into()]
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
                        junit: None,
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
                        cache_base: base.is_file().then_some(base.as_path()),
                        cache_disk: &cache_disk,
                        tree: &tree,
                        cache_data,
                        exact_cache: matches!(
                            case,
                            "success" | "unsupported" | "miss-restore" | "cold-restore"
                        ),
                        kept_data: &mut kept,
                        save_cache,
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
            if case == "no-loop" {
                assert!(
                    log.contains("ext4 loop-mount probe failed"),
                    "{case}: {log}"
                );
            }
            if matches!(case, "seed" | "success") {
                let identity = log
                    .lines()
                    .find_map(|line| line.strip_prefix("fixture identity "))
                    .unwrap_or_else(|| panic!("missing job identity in {case}: {log}"))
                    .to_owned();
                if seed {
                    disk_identity = Some(identity);
                } else {
                    ram_identity = Some(identity);
                }
            }
            if seed {
                cache_data = Some(kept.expect(&log));
            } else if returns_changes {
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
            } else if case == "cold" {
                assert!(log.contains("no cache was restored"), "{log}");
                assert_eq!(
                    std::fs::read_to_string(work.join("demo/current")).unwrap(),
                    "current\n"
                );
            } else if case == "cancel" {
                assert!(!work.join("demo/current").exists());
            } else if matches!(case, "unsupported" | "no-loop") {
                assert!(log.contains("using disk"), "{case}: {log}");
                assert_eq!(
                    std::fs::read_to_string(work.join("demo/current")).unwrap(),
                    "disk\n"
                );
            }
            assert_eq!(
                kept.is_some(),
                matches!(case, "seed" | "miss" | "cold" | "no-losetup" | "no-loop"),
                "{case}: {log}"
            );
            if !seed {
                assert_eq!(cache_disk.is_file(), kept.is_some(), "{case}: {log}");
            }
        }
        assert_eq!(
            disk_identity, ram_identity,
            "job identity changed with RAM workspace"
        );
    }

    #[test]
    fn successful_ram_cache_collection_can_finish_after_the_command_deadline() {
        let returned = tempfile::tempdir().unwrap();
        let config = pipeline::RamWorkspace {
            paths: vec!["target".into()],
            max_mib: 64,
        };
        let ram = RamWorkspace {
            config: &config,
            returned: returned.path().to_path_buf(),
            quota_mib: 128,
            cache: true,
        };
        assert_eq!(timeout_flush(None, false), None);
        assert_eq!(timeout_flush(Some(&ram), false), Some(TIMEOUT_FLUSH));
        assert_eq!(cancelled_outcome(Some(&ram), false), Outcome::Cancelled);
        std::fs::write(returned.path().join("exit"), "7").unwrap();
        assert_eq!(timeout_flush(Some(&ram), false), Some(TIMEOUT_FLUSH));
        assert_eq!(cancelled_outcome(Some(&ram), false), Outcome::Cancelled);
        std::fs::write(returned.path().join("exit"), "0").unwrap();
        assert_eq!(
            timeout_flush(Some(&ram), false),
            Some(Duration::from_secs(HELPER_SECS)),
        );
        assert_eq!(vm_timeout_secs(30, Some(&ram)), 30 + HELPER_SECS + 60);
        assert_eq!(vm_timeout_secs(30, None), 90);
        assert_eq!(cancelled_outcome(Some(&ram), false), Outcome::Exited(0));
        assert_eq!(cancelled_outcome(Some(&ram), true), Outcome::Cancelled);
    }
}
