//! Real-device storage and VM lifetime checks.

use super::*;
use std::io::Write;
use std::os::unix::process::ExitStatusExt;
use tokio::io::{AsyncBufReadExt, BufReader};

#[path = "../../tests/support/device_disk.rs"]
mod device_disk;

const CHILD_HOME: &str = "GUILDBOTICS_REAL_DEVICE_OWNER_HOME";
const TEST: &str = "runtime::real_device::real_device_owner_exit_stops_the_vm";
const LOGIN: &str = "synthetic-owner-login";
const CANARY: &str = "synthetic-owner-disk-positive-control";

fn verification_snapshot() -> PathBuf {
    assert!(
        std::env::var_os("CI").is_none(),
        "real_device requires an attending owner"
    );
    let root = std::fs::canonicalize(
        std::env::var_os("GUILDBOTICS_REAL_DEVICE_ROOT").expect("verification root required"),
    )
    .unwrap();
    let recipe =
        crate::recipe::Recipe::load(&gb_base::home::python_package().join("templates")).unwrap();
    crate::snapshot::ready(&root.join("workspace"), &recipe.name(None)).unwrap()
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot; run with #108"]
async fn real_device_large_stdin_reaches_all_guest_process_entries() {
    use sha2::{Digest, Sha256};
    let snapshot = verification_snapshot();
    let dir = tempfile::Builder::new()
        .prefix("gb")
        .rand_bytes(6)
        .tempdir_in("/tmp")
        .unwrap();
    let home = dir.path().join("msb");
    ensure(&home).unwrap();
    let boot = Boot {
        files: None,
        home: &home,
        snapshot: &snapshot,
        memory_mib: 4096,
        cpus: 2,
        mounts: &[],
        links: &[],
        network: &Network::default(),
        worktree: None,
    };
    let vm = create_vm(&boot, SCRATCH_MIB).await.unwrap();
    let bytes: Vec<u8> = (0..5 * 1024 * 1024 + 73)
        .map(|i| ((i * 31 + i / 251) % 256) as u8)
        .collect();
    let expected = (Sha256::digest(&bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>()
        + "\n")
        .into_bytes();
    let argv = vec![
        "python3".into(),
        "-c".into(),
        "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())".into(),
    ];
    let result = tokio::time::timeout(Duration::from_secs(120), async {
        let process = Process {
            argv: &argv,
            cwd: "/",
            env: &[],
            stdin: &bytes,
        };
        let ended = execute(&vm, &process).await?;
        assert_eq!(ended.code, 0);
        assert_eq!(ended.reply, expected);
        assert_eq!(capture(&vm, &argv, &bytes, &[], 1024).await?, expected);
        let ended = execute_private(&vm, &process, |_| {}).await?;
        assert_eq!(ended.code, 0);
        assert_eq!(ended.reply, expected);
        let guest = crate::guest_process::Process {
            argv: argv.clone(),
            cwd: "/".into(),
            env: vec![],
            stdin: bytes,
            limit: 1024,
        };
        let output = execute_guest_process(
            &vm,
            &guest,
            &[],
            std::time::Instant::now() + Duration::from_secs(30),
        )
        .await?;
        assert_eq!(output.code, 0);
        assert_eq!(output.stdout, expected);
        Ok::<_, Failure>(())
    })
    .await;
    vm.close().await;
    result.expect("large stdin probe timed out").unwrap();
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot; run with #227"]
async fn real_device_copy_disk_exceeds_64_mib_and_is_removed_with_the_vm() {
    use crate::worktree::transfer::{Returned, Upload};
    let snapshot = verification_snapshot();
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let home = root.join("msb");
    ensure(&home).unwrap();
    let access = crate::contract::Resolved {
        grants: vec![],
        links: vec![],
        denied: vec![],
        view: serde_json::Value::Null,
    };
    let mut mounts = vec![];
    for (read_only, cwd) in [(false, "/work-copy"), (true, "/work-empty")] {
        let upload = (!read_only).then(|| {
            std::sync::Arc::new(Upload {
                directories: vec![],
                files: vec![],
                nested: vec![],
                git: None,
            })
        });
        mounts.extend(
            crate::contract::received_plan(
                &root.join("workspace"),
                "probe",
                Path::new(cwd),
                &access,
                vec![],
                read_only,
                false,
                upload,
                Returned::default(),
            )
            .unwrap()
            .mounts,
        );
    }
    let boot = Boot {
        files: None,
        home: &home,
        snapshot: &snapshot,
        memory_mib: 4096,
        cpus: 2,
        mounts: &mounts,
        links: &[],
        network: &Network::default(),
        worktree: None,
    };
    let vm = create_vm(&boot, SCRATCH_MIB).await.unwrap();
    let prefix = format!("{}-", vm.name());
    let argv = [
        "python3".into(),
        "-c".into(),
        r#"
import os, subprocess
from pathlib import Path
copy = Path('/work-copy')
assert os.stat(copy).st_dev != os.stat('/').st_dev
assert subprocess.check_output(['stat', '-f', '-c', '%T', str(copy)]).strip() != b'tmpfs'
assert subprocess.check_output(['stat', '-f', '-c', '%T', '/work-empty']).strip() == b'tmpfs'
empty = os.statvfs('/work-empty')
assert empty.f_blocks * empty.f_frsize == 64 * 1024 * 1024
with (copy / 'build-output').open('wb') as output:
    for _ in range(65):
        output.write(b'x' * (1024 * 1024))
    output.flush()
    os.fsync(output.fileno())
assert (copy / 'build-output').stat().st_size == 65 * 1024 * 1024
print('copy disk verified')
"#
        .into(),
    ];
    let result = tokio::time::timeout(
        Duration::from_secs(120),
        execute_private(
            &vm,
            &Process {
                argv: &argv,
                cwd: "/work-copy",
                env: &[],
                stdin: &[],
            },
            |_| {},
        ),
    )
    .await;
    vm.close().await;
    let ended = result.expect("copy disk probe timed out").unwrap();
    assert_eq!(ended.code, 0);
    assert_eq!(ended.reply, b"copy disk verified\n");
    assert!(
        microsandbox::Volume::list()
            .await
            .unwrap()
            .iter()
            .all(|v| !v.name().starts_with(&prefix))
    );
}

#[test]
fn the_disk_scan_detects_a_planted_login() {
    let home = tempfile::tempdir().unwrap();
    let sandbox = home.path().join("sandboxes/guildbotics-probe");
    std::fs::create_dir_all(&sandbox).unwrap();
    std::fs::write(sandbox.join("upper.ext4"), CANARY).unwrap();
    device_disk::assert_private_disk(home.path(), CANARY, LOGIN);
    let log = sandbox.join("probe.log");
    std::fs::write(&log, LOGIN).unwrap();
    assert_eq!(device_disk::disk_matches(home.path(), LOGIN), [log]);
    assert!(
        std::panic::catch_unwind(|| {
            device_disk::assert_private_disk(home.path(), CANARY, LOGIN);
        })
        .is_err()
    );
}

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_owner_exit_stops_the_vm() {
    let snapshot = verification_snapshot();
    if let Some(home) = std::env::var_os(CHILD_HOME) {
        hold_synthetic_login(Path::new(&home), &snapshot).await;
        return;
    }

    let dir = tempfile::Builder::new()
        .prefix("gb")
        .rand_bytes(6)
        .tempdir_in("/tmp")
        .unwrap();
    let home = dir.path().join("msb");
    ensure(&home).unwrap();
    let mut owner = tokio::process::Command::new(std::env::current_exe().unwrap())
        .args([TEST, "--ignored", "--exact", "--nocapture"])
        .env(CHILD_HOME, &home)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let mut lines = BufReader::new(owner.stdout.take().unwrap()).lines();
    let name = tokio::time::timeout(Duration::from_secs(120), async {
        while let Some(line) = lines.next_line().await.unwrap() {
            if let Some(name) = line.strip_prefix("owner-holds-login:") {
                return name.to_owned();
            }
        }
        panic!("the owner exited before the synthetic login was in memory")
    })
    .await
    .expect("the owner did not start a VM");
    assert!(name.starts_with(NAME_PREFIX));
    let scanned =
        std::panic::catch_unwind(|| device_disk::assert_private_disk(&home, CANARY, LOGIN));
    owner.kill().await.unwrap();
    let status = owner.wait().await.unwrap();
    backend(&home).await.unwrap();

    let ended = tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            match Sandbox::get(&name).await {
                Err(microsandbox::MicrosandboxError::SandboxNotFound(_)) => break,
                Err(error) => panic!("cannot inspect the owner probe's VM: {error}"),
                Ok(_) => {}
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await;
    if ended.is_err()
        && let Ok(vm) = Sandbox::get(&name).await
    {
        let status = vm.status_snapshot();
        let _ = vm.kill().await;
        let _ = vm.destroy().await;
        eprintln!("sandbox status before fixture cleanup: {status:?}");
    }
    // The named scratch disk is reclaimed separately from the VM. This test
    // places the synthetic credential on a tmpfs and cleans up its own disk.
    let _ = microsandbox::Volume::remove(&format!("{name}-0")).await;
    assert_eq!(
        status.signal(),
        Some(libc::SIGKILL),
        "the owner exited before it was killed"
    );
    assert!(ended.is_ok(), "the VM outlived the killed owner");
    if let Err(failure) = scanned {
        std::panic::resume_unwind(failure);
    }
}

async fn hold_synthetic_login(home: &Path, snapshot: &Path) {
    let boot = Boot {
        files: None,
        home,
        snapshot,
        memory_mib: 4096,
        cpus: 2,
        mounts: &[Mount {
            guest: "/run/owner-probe".into(),
            host: None,
            readonly: false,
            quota_mib: None,
        }],
        links: &[],
        network: &Network::default(),
        worktree: None,
    };
    let vm = create_vm(&boot, SCRATCH_MIB).await.unwrap();
    let argv = [
        "sh".into(),
        "-ec".into(),
        concat!(
            "test \"$(stat -f -c %T /run/owner-probe)\" = tmpfs; cat > /run/owner-probe/login; ",
            "printf %s \"$GB_DISK_CANARY\" > /var/tmp/owner-positive-control; sync; ",
            "printf 'ready\\n'",
        )
        .into(),
    ];
    let name = vm.name().to_owned();
    let process = Process {
        argv: &argv,
        cwd: "/",
        env: &[("GB_DISK_CANARY".into(), CANARY.into())],
        stdin: LOGIN.as_bytes(),
    };
    let ended = execute_private(&vm, &process, |_| {}).await.unwrap();
    assert_eq!(ended.code, 0);
    assert_eq!(ended.reply, b"ready\n");
    println!("owner-holds-login:{name}");
    std::io::stdout().flush().unwrap();
    // Keep the VM owned by this process while the parent scans its disks.
    std::future::pending::<()>().await;
}

#[test]
fn a_readiness_marker_can_cross_output_frames() {
    let ready = device_disk::OutputMarker::new(b"ready\n");
    assert!(!ready.observed(b"rea"));
    assert!(ready.observed(b"dy\n"));
    assert!(!ready.observed(b"ready\n"));
}
