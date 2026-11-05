//! The lifetime of a VM when its owner cannot run destructors.

use super::*;
use std::io::Write;
use tokio::io::{AsyncBufReadExt, BufReader};

const CHILD_HOME: &str = "GUILDBOTICS_REAL_DEVICE_OWNER_HOME";
const TEST: &str = "runtime::real_device::real_device_owner_exit_stops_the_vm";

#[tokio::test]
#[ignore = "real_device: owner approval and disposable snapshot"]
async fn real_device_owner_exit_stops_the_vm() {
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
    let snapshot = crate::snapshot::ready(&root.join("workspace"), &recipe.name(None)).unwrap();
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
    owner.kill().await.unwrap();
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
    assert!(ended.is_ok(), "the VM outlived the killed owner");
}

async fn hold_synthetic_login(home: &Path, snapshot: &Path) {
    let boot = Boot {
        home,
        snapshot,
        memory_mib: 4096,
        cpus: 2,
        mounts: &[Mount {
            guest: "/run/owner-probe".into(),
            host: None,
            readonly: false,
        }],
        network: &Network::default(),
        worktree: None,
    };
    let vm = create_vm(&boot, SCRATCH_MIB).await.unwrap();
    let argv = [
        "sh".into(),
        "-ec".into(),
        "test \"$(stat -f -c %T /run/owner-probe)\" = tmpfs; cat > /run/owner-probe/login; printf 'ready\\n'; sleep 300".into(),
    ];
    let name = vm.name().to_owned();
    let process = Process {
        argv: &argv,
        cwd: "/",
        env: &[],
        stdin: b"synthetic-owner-login",
    };
    let result = execute_private(&vm, &process, |chunk| {
        if chunk.windows(5).any(|part| part == b"ready") {
            println!("owner-holds-login:{name}");
            std::io::stdout().flush().unwrap();
        }
    })
    .await;
    vm.close().await;
    assert!(
        result.is_ok(),
        "the parent should kill the owner while its login is held"
    );
}
