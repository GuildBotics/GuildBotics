//! Re-run a permission or environment contract without the caller's privileges/settings.
use std::process::Command;

/// Returns true in the parent after the exact test passed in a child process.
/// The child creates its own fixtures; no privileged files are shared with it.
pub fn rerun(test: &str, unprivileged: bool, remove_env: &[&str]) -> bool {
    let mut command = Command::new(std::env::current_exe().unwrap());
    let mut needed = false;
    #[cfg(unix)]
    if unprivileged {
        use std::os::unix::{fs::MetadataExt, process::CommandExt};
        let probe = tempfile::tempdir().unwrap();
        if probe.path().metadata().unwrap().uid() == 0 {
            #[cfg(target_os = "linux")]
            {
                use std::os::unix::fs::PermissionsExt;
                let file = probe.path().join("permission-probe");
                std::fs::write(&file, b"probe").unwrap();
                std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o000)).unwrap();
                // A user namespace may map only UID 0. Drop DAC bypass capabilities
                // on exec instead of switching to an unmapped UID. Probe the actual
                // access so the child runs the test rather than re-executing again.
                needed = std::fs::read(&file).is_ok();
                if needed {
                    // SAFETY: the child only calls prctl before exec; no allocation
                    // or shared process state is touched in the pre-exec hook.
                    unsafe {
                        command.pre_exec(|| {
                            for capability in [1, 2] {
                                // CAP_DAC_OVERRIDE, CAP_DAC_READ_SEARCH
                                if libc::prctl(libc::PR_CAPBSET_DROP, capability, 0, 0, 0) == -1 {
                                    return Err(std::io::Error::last_os_error());
                                }
                            }
                            Ok(())
                        });
                    }
                }
                std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600)).unwrap();
            }
            #[cfg(not(target_os = "linux"))]
            {
                command.uid(65534).gid(65534);
                needed = true;
            }
        }
    }
    #[cfg(not(unix))]
    let _ = unprivileged;
    for name in remove_env {
        needed |= std::env::var_os(name).is_some();
        command.env_remove(name);
    }
    if !needed {
        return false;
    }
    let output = command
        .args(["--exact", test, "--nocapture"])
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success() && stdout.contains("1 passed"),
        "{test}: {}\n{stdout}\n{}",
        output.status,
        String::from_utf8_lossy(&output.stderr)
    );
    true
}
