//! Keep a service awake, following only successfully read device settings.
use crate::service_settings::Settings;
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

static WARNED: AtomicBool = AtomicBool::new(false);
fn warn() {
    if !WARNED.swap(true, Ordering::SeqCst) {
        tracing::warn!("This machine cannot be kept awake; it may sleep while GuildBotics works.");
    }
}

trait Held {
    fn alive(&mut self) -> bool {
        true
    }
}
type Factory = Arc<dyn Fn() -> anyhow::Result<Box<dyn Held>> + Send + Sync>;
type Signature = (std::time::SystemTime, u64, u64);
fn signature(workspace: &std::path::Path) -> std::io::Result<Option<Signature>> {
    let meta = match std::fs::metadata(Settings::path(workspace)) {
        Ok(meta) => meta,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    #[cfg(unix)]
    let identity = {
        use std::os::unix::fs::MetadataExt;
        meta.ino()
    };
    #[cfg(windows)]
    let identity = {
        use std::os::windows::fs::MetadataExt;
        meta.creation_time()
    };
    Ok(Some((meta.modified()?, meta.len(), identity)))
}
struct State {
    workspace: PathBuf,
    signature: Option<Signature>,
    observed: bool,
    unreadable: bool,
    held: Option<Box<dyn Held>>,
    enabled: bool,
    factory: Factory,
}
impl State {
    fn set(&mut self, enabled: bool) {
        if enabled == self.enabled {
            return;
        }
        self.enabled = enabled;
        self.held = if enabled {
            match (self.factory)() {
                Ok(held) => Some(held),
                Err(_) => {
                    warn();
                    None
                }
            }
        } else {
            None
        };
    }
    fn refresh(&mut self) {
        if self.held.as_mut().is_some_and(|held| !held.alive()) {
            self.held = None;
            warn();
        }
        let signature = match signature(&self.workspace) {
            Ok(value) => value,
            Err(_) => return,
        };
        if self.observed && signature == self.signature {
            return;
        }
        match Settings::read(&self.workspace) {
            Ok(settings) => {
                self.signature = signature;
                self.observed = true;
                self.unreadable = false;
                self.set(settings.keep_awake);
            }
            Err(_) => {
                if !self.unreadable {
                    tracing::warn!(
                        "The service settings cannot be read; keeping the machine as it is until they can."
                    );
                }
                self.unreadable = true;
            }
        }
    }
}

pub struct Follow {
    stop: std::sync::mpsc::Sender<()>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Follow {
    pub fn start(workspace: PathBuf, enabled: bool) -> anyhow::Result<Self> {
        Self::with_factory(workspace, enabled, Arc::new(platform_hold))
    }
    fn with_factory(workspace: PathBuf, enabled: bool, factory: Factory) -> anyhow::Result<Self> {
        Self::watch(Some(workspace), enabled, factory)
    }
    pub fn command() -> anyhow::Result<Self> {
        Self::watch(None, true, Arc::new(platform_hold))
    }
    fn watch(workspace: Option<PathBuf>, enabled: bool, factory: Factory) -> anyhow::Result<Self> {
        let (stop, received) = std::sync::mpsc::channel();
        let (ready, entered) = std::sync::mpsc::sync_channel(0);
        let thread = std::thread::Builder::new()
            .name("guildbotics-keep-awake".into())
            .spawn(move || {
                let follows_settings = workspace.is_some();
                let workspace = workspace.unwrap_or_default();
                let signature = if follows_settings {
                    signature(&workspace).unwrap_or_default()
                } else {
                    None
                };
                let mut state = State {
                    workspace,
                    signature,
                    observed: false,
                    unreadable: false,
                    held: None,
                    enabled: false,
                    factory,
                };
                state.set(enabled);
                let _ = ready.send(());
                while matches!(
                    received.recv_timeout(std::time::Duration::from_secs(1)),
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout)
                ) {
                    if follows_settings {
                        state.refresh();
                    }
                }
            })?;
        entered.recv()?;
        Ok(Self {
            stop,
            thread: Some(thread),
        })
    }
}
impl Drop for Follow {
    fn drop(&mut self) {
        let _ = self.stop.send(());
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(unix)]
struct Process(std::process::Child);
#[cfg(unix)]
impl Held for Process {
    fn alive(&mut self) -> bool {
        matches!(self.0.try_wait(), Ok(None))
    }
}
#[cfg(unix)]
impl Drop for Process {
    fn drop(&mut self) {
        #[cfg(target_os = "macos")]
        let _ = self.0.kill();
        // On Linux the inhibitor's cat exits on EOF, releasing its parent
        // too. Killing only the inhibitor could leave its child behind.
        drop(self.0.stdin.take());
        let _ = self.0.wait();
    }
}
#[cfg(unix)]
fn platform_hold() -> anyhow::Result<Box<dyn Held>> {
    use std::process::{Command, Stdio};
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut c = Command::new("caffeinate");
        c.arg("-i");
        c
    };
    #[cfg(not(target_os = "macos"))]
    let mut command = {
        let mut c = Command::new("systemd-inhibit");
        c.args([
            "--what=idle",
            "--mode=block",
            "--who=GuildBotics",
            "--why=GuildBotics service",
            "cat",
        ]);
        c
    };
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    anyhow::ensure!(child.try_wait()?.is_none(), "cannot inhibit idle sleep");
    Ok(Box::new(Process(child)))
}
#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn SetThreadExecutionState(flags: u32) -> u32;
}
#[cfg(windows)]
struct WindowsHold;
#[cfg(windows)]
impl Held for WindowsHold {}
#[cfg(windows)]
impl Drop for WindowsHold {
    fn drop(&mut self) {
        unsafe {
            SetThreadExecutionState(0x80000000);
        }
    }
}
#[cfg(windows)]
fn platform_hold() -> anyhow::Result<Box<dyn Held>> {
    // The watcher acquires and releases on this same dedicated thread.
    anyhow::ensure!(
        unsafe { SetThreadExecutionState(0x80000001) } != 0,
        "cannot inhibit idle sleep"
    );
    Ok(Box::new(WindowsHold))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    struct Hold(Arc<AtomicUsize>);
    impl Held for Hold {}
    impl Drop for Hold {
        fn drop(&mut self) {
            self.0.fetch_sub(1, Ordering::SeqCst);
        }
    }
    fn factory(count: Arc<AtomicUsize>) -> Factory {
        Arc::new(move || {
            count.fetch_add(1, Ordering::SeqCst);
            Ok(Box::new(Hold(count.clone())))
        })
    }
    #[tokio::test]
    async fn commands_hold_independently_through_success_failure_and_cancellation() {
        let count = Arc::new(AtomicUsize::new(0));
        for fails in [false, true] {
            let held = count.clone();
            let result: Result<(), ()> = async {
                let _hold = Follow::watch(None, true, factory(held.clone())).unwrap();
                assert_eq!(held.load(Ordering::SeqCst), 1);
                if fails { Err(()) } else { Ok(()) }
            }
            .await;
            assert_eq!(result.is_err(), fails);
            assert_eq!(count.load(Ordering::SeqCst), 0);
        }
        let first = Follow::watch(None, true, factory(count.clone())).unwrap();
        let held = count.clone();
        let entered = tokio_util::sync::CancellationToken::new();
        let ready = entered.clone();
        let running = tokio::spawn(async move {
            let _hold = Follow::watch(None, true, factory(held)).unwrap();
            ready.cancel();
            std::future::pending::<()>().await;
        });
        entered.cancelled().await;
        assert_eq!(count.load(Ordering::SeqCst), 2);
        running.abort();
        assert!(running.await.unwrap_err().is_cancelled());
        assert_eq!(count.load(Ordering::SeqCst), 1);
        drop(first);
        assert_eq!(count.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn independent_holds_start_synchronously_and_release_from_another_thread() {
        let dir = tempfile::tempdir().unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let first = Follow::with_factory(dir.path().into(), true, factory(count.clone())).unwrap();
        let second = Follow::with_factory(dir.path().into(), true, factory(count.clone())).unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 2);
        std::thread::spawn(move || drop(first)).join().unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 1);
        drop(second);
        assert_eq!(count.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn only_changed_readable_settings_change_a_hold_and_repeated_settings_keep_one_hold() {
        let dir = tempfile::tempdir().unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let mut state = State {
            workspace: dir.path().into(),
            signature: None,
            observed: false,
            unreadable: false,
            held: None,
            enabled: false,
            factory: factory(count.clone()),
        };
        let mut settings = Settings {
            keep_awake: true,
            ..Default::default()
        };
        state.refresh();
        assert_eq!(count.load(Ordering::SeqCst), 0);
        settings.write(dir.path()).unwrap();
        state.refresh();
        state.refresh();
        assert_eq!(count.load(Ordering::SeqCst), 1);
        let signature = state.signature;
        let path = Settings::path(dir.path());
        let contents = std::fs::read(&path).unwrap();
        // A canary with the same signature must not be read on every poll.
        std::fs::write(&path, vec![b'['; contents.len()]).unwrap();
        std::fs::File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(signature.unwrap().0))
            .unwrap();
        state.refresh();
        assert!(!state.unreadable);
        assert_eq!(count.load(Ordering::SeqCst), 1);
        std::fs::write(Settings::path(dir.path()), "keep_awake: [").unwrap();
        let (_, warnings) = crate::test_logs::capture(|| {
            state.refresh();
            state.refresh();
            state.refresh();
        });
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("service settings cannot be read"));
        assert_eq!(count.load(Ordering::SeqCst), 1);
        assert_eq!(state.signature, signature);
        assert!(state.unreadable);
        settings.keep_awake = false;
        settings.write(dir.path()).unwrap();
        state.refresh();
        assert_eq!(count.load(Ordering::SeqCst), 0);
        assert!(!state.unreadable);
        settings.keep_awake = true;
        settings.write(dir.path()).unwrap();
        state.refresh();
        std::fs::remove_file(Settings::path(dir.path())).unwrap();
        state.refresh();
        assert_eq!(count.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn unsupported_keep_awake_warns_once_and_unwinding_always_releases_a_hold() {
        let dir = tempfile::tempdir().unwrap();
        WARNED.store(false, Ordering::SeqCst);
        let (_, warnings) = crate::test_logs::capture(|| {
            let mut state = State {
                workspace: dir.path().into(),
                signature: None,
                observed: false,
                unreadable: false,
                held: None,
                enabled: false,
                factory: Arc::new(|| anyhow::bail!("unsupported device")),
            };
            state.set(true);
            assert!(state.enabled && state.held.is_none());
            state.set(false);
            state.set(true);
            assert!(state.enabled && state.held.is_none());
        });
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].starts_with("This machine cannot be kept awake"));
        let count = Arc::new(AtomicUsize::new(0));
        let held = Follow::with_factory(dir.path().into(), true, factory(count.clone())).unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 1);
        let ended = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
            let _held = held;
            panic!("fixture");
        }));
        assert!(ended.is_err());
        assert_eq!(count.load(Ordering::SeqCst), 0);
    }
}
