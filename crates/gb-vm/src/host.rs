use std::path::{Path, PathBuf};
use std::time::Duration;

use microsandbox::{LocalBackend, Sandbox};
use tokio::sync::OnceCell;

use crate::{RUNTIME_VERSION, digest_reference};

pub const STOP_TIMEOUT: Duration = Duration::from_secs(5);
static BACKEND: OnceCell<PathBuf> = OnceCell::const_new();

pub fn placed_files(home: &Path) -> [PathBuf; 2] {
    let os = std::env::consts::OS;
    [
        home.join(microsandbox_utils::BIN_SUBDIR)
            .join(microsandbox_utils::msb_binary_filename(os)),
        home.join(microsandbox_utils::LIB_SUBDIR)
            .join(microsandbox_utils::libkrunfw_filename(os)),
    ]
}

pub enum Readiness {
    Missing,
    Version(String),
}

pub fn ready(home: &Path) -> Result<(), Readiness> {
    if !placed_files(home).iter().all(|file| file.is_file()) {
        return Err(Readiness::Missing);
    }
    let placed = std::fs::read_to_string(home.join("version")).unwrap_or_default();
    if placed.trim() != RUNTIME_VERSION {
        return Err(Readiness::Version(placed.trim().to_owned()));
    }
    Ok(())
}

/// Place the exact runtime carried by this executable, once per version.
pub fn ensure(home: &Path) -> std::io::Result<()> {
    place(
        home,
        [
            include_bytes!(env!("GB_MSB")),
            include_bytes!(env!("GB_KRUN")),
        ],
    )
}

pub fn install_from(home: &Path, bundled: &Path) -> std::io::Result<()> {
    let [msb, lib] = placed_files(bundled);
    place(home, [&std::fs::read(msb)?, &std::fs::read(lib)?])
}

fn place(home: &Path, bytes: [&[u8]; 2]) -> std::io::Result<()> {
    let _held =
        gb_base::files::FileLock::hold(&home.join("install.lock"), Duration::from_secs(30))?;
    if ready(home).is_ok() {
        return Ok(());
    }
    for (i, path) in placed_files(home).iter().enumerate() {
        gb_base::files::atomic_write(path, bytes[i])?;
        #[cfg(unix)]
        if i == 0 {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))?;
        }
    }
    gb_base::files::atomic_write(&home.join("version"), RUNTIME_VERSION.as_bytes())
}

/// Bind this process to one runtime home for SDK operations.
pub async fn backend(home: &Path) -> Result<(), String> {
    let installed = BACKEND
        .get_or_try_init(|| async {
            let backend = LocalBackend::builder()
                .home(home)
                .build()
                .await
                .map_err(|e| format!("The microVM runtime cannot start: {e}"))?;
            microsandbox::set_default_backend(backend);
            Ok::<_, String>(home.to_path_buf())
        })
        .await?;
    if installed != home {
        return Err(format!(
            "The microVM runtime is at {}, not {}.",
            installed.display(),
            home.display()
        ));
    }
    Ok(())
}

/// Import an archive into the already selected local store. The caller holds
/// the image lock while changing references.
pub async fn load_image(
    path: &Path,
    tags: Vec<String>,
) -> Result<Vec<microsandbox::ImageHandle>, String> {
    use microsandbox::Image;
    use microsandbox_image::{GlobalCache, ImageLoadOptions, Reference, load_archive};

    let backend = microsandbox::default_backend();
    let local = backend
        .as_local()
        .ok_or("The runtime has no local image store")?;
    let cache_dir = local.cache_dir();
    let cache = GlobalCache::new(&cache_dir).map_err(|e| e.to_string())?;
    let images = load_archive(
        &cache_dir,
        path,
        ImageLoadOptions {
            tags,
            progress: None,
        },
    )
    .await
    .map_err(|e| e.to_string())?;
    let mut handles = Vec::with_capacity(images.len());
    for image in images {
        let reference: Reference = image.reference.parse().map_err(|e| format!("{e}"))?;
        let pin = Reference::with_digest(
            reference.registry().to_owned(),
            reference.repository().to_owned(),
            image.metadata.manifest_digest.to_string(),
        );
        // Keep a removable reference when the tag moves. The manifest could
        // still be in use by a VM or snapshot at that point.
        cache
            .write_image_metadata_async(&pin, &image.metadata)
            .await
            .map_err(|e| e.to_string())?;
        Image::persist(local, &pin.whole(), image.metadata.clone())
            .await
            .map_err(|e| e.to_string())?;
        Image::persist(local, &image.reference, image.metadata)
            .await
            .map_err(|e| e.to_string())?;
        handles.push(
            Image::get(&image.reference)
                .await
                .map_err(|e| e.to_string())?,
        );
    }
    Ok(handles)
}

/// Drop old digest-only references without deleting tags or the base image
/// of a snapshot. The SDK's bulk prune also removes unused tagged images.
/// The caller holds the exclusive image lock.
pub async fn collect_images() -> Result<(), String> {
    use microsandbox::{Image, MicrosandboxError, Snapshot};
    use std::collections::HashSet;

    let images = Image::list().await.map_err(|e| e.to_string())?;
    let mut keep: HashSet<_> = images
        .iter()
        .filter(|i| !digest_reference(i.reference()))
        .filter_map(|i| i.manifest_digest().map(str::to_owned))
        .collect();
    for snapshot in Snapshot::list().await.map_err(|e| e.to_string())? {
        let Ok(opened) = Snapshot::open(snapshot.path().to_string_lossy()).await else {
            // The build holding this directory may be replacing its snapshot.
            return Ok(());
        };
        keep.insert(opened.manifest().image.manifest_digest.clone());
    }
    for image in images {
        if digest_reference(image.reference())
            && image
                .manifest_digest()
                .is_some_and(|digest| !keep.contains(digest))
        {
            match Image::remove(image.reference(), false).await {
                Ok(()) | Err(MicrosandboxError::ImageInUse(_)) => {}
                Err(error) => return Err(error.to_string()),
            }
        }
    }
    Ok(())
}

static VM_TASKS: std::sync::LazyLock<tokio_util::task::TaskTracker> =
    std::sync::LazyLock::new(tokio_util::task::TaskTracker::new);

/// Drain late boots and shutdown tasks after callers stop accepting work.
pub async fn drain_vms() {
    drain_tasks(&VM_TASKS, STOP_TIMEOUT).await;
}
async fn drain_tasks(tasks: &tokio_util::task::TaskTracker, deadline: Duration) {
    tasks.close();
    let _ = tokio::time::timeout(deadline, tasks.wait()).await;
}

/// An owned cleanup task survives cancellation of the operation awaiting it.
pub struct Cleanup {
    start: Option<tokio::sync::oneshot::Sender<()>>,
    task: tokio::task::JoinHandle<()>,
}
impl Cleanup {
    pub fn new(work: impl Future<Output = ()> + Send + 'static) -> Self {
        Self::new_in(&VM_TASKS, work)
    }
    fn new_in(
        tasks: &tokio_util::task::TaskTracker,
        work: impl Future<Output = ()> + Send + 'static,
    ) -> Self {
        let (start, ready) = tokio::sync::oneshot::channel();
        let task = tasks.spawn(async move {
            let _ = ready.await;
            work.await;
        });
        Self {
            start: Some(start),
            task,
        }
    }
    pub async fn finish(mut self) {
        if let Some(start) = self.start.take() {
            let _ = start.send(());
        }
        let _ = (&mut self.task).await;
    }
}

pub struct OwnedVm {
    sandbox: Sandbox,
    cleanup: Cleanup,
}
impl OwnedVm {
    pub async fn start(
        builder: microsandbox::sandbox::SandboxBuilder,
        retain: impl Send + 'static,
    ) -> microsandbox::MicrosandboxResult<Self> {
        owned_boot(async move {
            let sandbox = builder.create().await?;
            let owned = sandbox.clone();
            let cleanup = Cleanup::new(async move {
                stop_vm(owned).await;
                drop(retain);
            });
            Ok(Self { sandbox, cleanup })
        })
        .await
    }

    pub fn into_parts(self) -> (Sandbox, Cleanup) {
        (self.sandbox, self.cleanup)
    }

    pub async fn close(self) {
        drop(self.sandbox);
        self.cleanup.finish().await;
    }
}
impl std::ops::Deref for OwnedVm {
    type Target = Sandbox;
    fn deref(&self) -> &Sandbox {
        &self.sandbox
    }
}
pub async fn owned_boot<T: Send + 'static>(boot: impl Future<Output = T> + Send + 'static) -> T {
    owned_boot_in(&VM_TASKS, boot).await
}
async fn owned_boot_in<T: Send + 'static>(
    tasks: &tokio_util::task::TaskTracker,
    boot: impl Future<Output = T> + Send + 'static,
) -> T {
    // Dropping the waiter detaches creation; its owned result still drops and cleans up.
    tasks.spawn(boot).await.expect("microVM boot task failed")
}
/// Only named volumes owned by this VM are removed after its destruction.
pub fn temporary_disk_names<'a>(
    vm: &str,
    mounts: &'a [microsandbox::sandbox::VolumeMount],
) -> impl Iterator<Item = &'a str> {
    let prefix = format!("{vm}-");
    mounts.iter().filter_map(move |mount| match mount {
        microsandbox::sandbox::VolumeMount::Named { name, .. } if name.starts_with(&prefix) => {
            Some(name.as_str())
        }
        _ => None,
    })
}

async fn stop_vm(sandbox: Sandbox) {
    let name = sandbox.name();
    let disks: Vec<_> = temporary_disk_names(name, &sandbox.config().spec.mounts)
        .map(str::to_owned)
        .collect();
    if let Err(e) = sandbox.stop_with_timeout(STOP_TIMEOUT).await {
        tracing::warn!("The microVM {name} did not stop, and is killed: {e}");
        if let Err(e) = sandbox.kill().await {
            tracing::warn!("The microVM {name} was not killed: {e}");
        }
    }
    let _ = sandbox.destroy().await;
    // The stopped process handle still owns its disk locks until the last Sandbox drops.
    drop(sandbox);
    for disk in disks {
        let _ = microsandbox::Volume::remove(&disk).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn shutdown_drain_has_a_deadline_for_hanging_vm_tasks() {
        let tasks = tokio_util::task::TaskTracker::new();
        let task = tasks.spawn(std::future::pending::<()>());
        tokio::time::timeout(
            Duration::from_millis(100),
            drain_tasks(&tasks, Duration::from_millis(10)),
        )
        .await
        .unwrap();
        assert!(!tasks.is_empty());
        task.abort();
        let _ = task.await;
    }
    #[tokio::test]
    async fn shutdown_drain_waits_for_a_cancelled_late_boot_and_its_cleanup() {
        let tasks = tokio_util::task::TaskTracker::new();
        let boot_tasks = tasks.clone();
        let cleanup_tasks = tasks.clone();
        let (started, boot_started) = tokio::sync::oneshot::channel();
        let (boot_release, boot_released) = tokio::sync::oneshot::channel();
        let (cleanup_start, cleanup_started) = tokio::sync::oneshot::channel();
        let (cleanup_release, cleanup_released) = tokio::sync::oneshot::channel();
        let waiter = tokio::spawn(async move {
            owned_boot_in(&boot_tasks, async move {
                let _ = started.send(());
                let _ = boot_released.await;
                Cleanup::new_in(&cleanup_tasks, async move {
                    let _ = cleanup_start.send(());
                    let _ = cleanup_released.await;
                })
            })
            .await
        });
        boot_started.await.unwrap();
        waiter.abort();
        let _ = waiter.await;
        let mut drain = Box::pin(drain_tasks(&tasks, STOP_TIMEOUT));
        assert!(
            tokio::time::timeout(Duration::from_millis(10), &mut drain)
                .await
                .is_err()
        );
        boot_release.send(()).unwrap();
        cleanup_started.await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(10), &mut drain)
                .await
                .is_err()
        );
        cleanup_release.send(()).unwrap();
        drain.await;
        assert!(tasks.is_empty());
    }
}
