use super::*;
use gb_vm::sdk::sandbox::{PullPolicy, SandboxBuilder};
use gb_vm::sdk::{
    ExecEvent, Image, MicrosandboxError, NetworkPolicy, NetworkProfile, RegistryAuth, Sandbox,
};
use gb_vm::{OwnedVm, owned_boot};

impl DiskMount<'_> {
    fn attach_to_sdk(self, builder: SandboxBuilder, at: &str) -> SandboxBuilder {
        match self {
            Self::Named { name, .. } => builder.volume(at, |m| m.named(name)),
            Self::Overlay(path) => builder.volume(at, |m| m.disk(path).fstype("ext4")),
        }
    }
}

impl HelperDisks<'_> {
    fn attach_to_sdk(&self, builder: SandboxBuilder) -> SandboxBuilder {
        self.mounts().fold(builder, |builder, (at, disk)| {
            disk.attach_to_sdk(builder, at)
        })
    }
}

impl Microsandbox {
    fn mount_job_tmp_sdk(
        &self,
        builder: SandboxBuilder,
        attempt_id: &str,
        tmpfs_mib: Option<u32>,
    ) -> SandboxBuilder {
        builder.volume("/tmp", |m| match tmpfs_mib {
            // Agentd defaults to mode 1777 for tmpfs mounts at /tmp.
            Some(mib) => m.tmpfs().size(mib),
            None => m.named_with(self.tmp_volume(attempt_id), |v| {
                v.create().disk().size(TMP_DISK_MIB)
            }),
        })
    }

    pub(super) async fn sdk_inspect(&self, reference: &str) -> Result<Option<ImageInfo>, String> {
        gb_vm::backend(&self.home).await?;
        let detail = match Image::inspect(reference).await {
            Err(MicrosandboxError::ImageNotFound(_)) => {
                let Some(alias) = Image::list()
                    .await
                    .map_err(|error| error.to_string())?
                    .into_iter()
                    .find(|image| crate::ci::images::same_reference(reference, image.reference()))
                else {
                    return Ok(None);
                };
                Image::inspect(alias.reference()).await
            }
            result => result,
        };
        let detail = match detail {
            Ok(detail) => detail,
            Err(MicrosandboxError::ImageNotFound(_)) => return Ok(None),
            Err(error) => return Err(error.to_string()),
        };
        let handle = detail.handle;
        gb_vm::verify_cached_image(&handle).await?;
        let config = detail
            .config
            .ok_or_else(|| format!("Missing image configuration for {reference}"))?;
        Ok(Some(ImageInfo {
            reference: handle.reference().to_owned(),
            digest: handle.manifest_digest().unwrap_or_default().to_owned(),
            config_digest: config.digest,
            size_bytes: handle
                .size_bytes()
                .and_then(|size| u64::try_from(size).ok())
                .unwrap_or_default(),
            created_at: handle
                .created_at()
                .map(|time| time.to_rfc3339())
                .unwrap_or_default(),
        }))
    }

    pub(super) async fn sdk_listed(&self) -> Result<Vec<Listed>, String> {
        gb_vm::backend(&self.home).await?;
        let images = Image::list().await.map_err(|e| e.to_string())?;
        Ok(images
            .into_iter()
            .map(|h| Listed {
                reference: h.reference().to_owned(),
                digest: h.manifest_digest().unwrap_or_default().to_owned(),
            })
            .collect())
    }

    pub(super) async fn sdk_load(&self, archive: &Path, tags: Vec<String>) -> Result<(), String> {
        gb_vm::backend(&self.home).await?;
        gb_vm::load_image(archive, tags).await?;
        Ok(())
    }

    pub(super) async fn sdk_register_pulled_image(&self, name: &str) -> Result<(), String> {
        gb_vm::backend(&self.home).await?;
        gb_vm::register_pulled_image(name).await
    }

    async fn builder(&self, job: &JobRun<'_>, name: &str) -> Result<SandboxBuilder, String> {
        gb_vm::backend(&self.home).await?;
        Ok(Sandbox::builder(name)
            .image(job.job.image.as_str())
            .pull_policy(PullPolicy::Never)
            .registry(|r| r.auth(RegistryAuth::Anonymous))
            .labels([
                ("branvil.runner", self.runner.as_str()),
                ("branvil.job", job.attempt_id),
            ])
            .ephemeral(true))
    }

    pub(super) async fn sdk_create_disk(&self, name: &str, mib: u64) -> Result<PathBuf, String> {
        gb_vm::backend(&self.home).await?;
        let volume = gb_vm::sdk::Volume::builder(name)
            .disk()
            .size(mib as u32)
            .create()
            .await
            .map_err(|error| error.to_string())?;
        volume
            .disk_path()
            .ok_or_else(|| format!("{name} is not a disk volume"))
    }

    pub(super) async fn sdk_helper(
        &self,
        job: &JobRun<'_>,
        what: &str,
        quota_mib: u64,
        disks: HelperDisks<'_>,
        script: &str,
        cancel: &mut watch::Receiver<bool>,
    ) -> Result<(), Option<String>> {
        let name = format!("bv-{what}-{}", job.attempt_id.to_lowercase());
        let builder = self
            .builder(job, &name)
            .await
            .map_err(Some)?
            .user("0:0")
            .max_duration(HELPER_SECS)
            .disable_network()
            .volume(IO_MOUNT, |m| m.bind(job.io).quota(quota_mib as u32));
        let builder = disks.attach_to_sdk(builder);
        let vm = OwnedVm::start(builder, ())
            .await
            .map_err(|error| Some(error.to_string()))?;
        let result = tokio::select! {
            biased;
            _ = cancel.wait_for(|stop| *stop) => Err(None),
            result = vm.exec("/bin/sh", ["-c", script]) => {
                match result {
                    Ok(output) if output.status().success => Ok(()),
                    Ok(output) => Err(Some(format!("{}: {}", output.status().code,
                        output.stderr().unwrap_or_default()))),
                    Err(error) => Err(Some(error.to_string())),
                }
            }
        };
        vm.close().await;
        result
    }

    pub(super) async fn sdk_job(
        &self,
        job: &mut JobRun<'_>,
        (disk, ram): (DiskMount<'_>, Option<&ram_workspace::RamWorkspace<'_>>),
        image_lock: ImageLock,
        log: &mpsc::Sender<Vec<u8>>,
        cancel: &mut watch::Receiver<bool>,
    ) -> Outcome {
        let started = Instant::now();
        let name = Self::sandbox_name(job.attempt_id);
        let builder = match self.builder(job, &name).await {
            Ok(builder) => builder,
            Err(error) => return Outcome::StartupFailure(error),
        };
        let builder = disk
            .attach_to_sdk(builder, "/work")
            .cpus(job.cpus as u8)
            .memory(job.memory_mib as u32)
            .workdir("/work")
            .max_duration(ram_workspace::vm_timeout_secs(job.timeout_secs, ram));
        let builder = if let Some(ram) = ram {
            builder
                .user("0:0")
                .volume(ram_workspace::RETURN_MOUNT, |m| {
                    m.bind(&ram.returned)
                        .owner(0, 0)
                        .quota(ram.quota_mib as u32)
                })
        } else {
            builder.user(format!("{JOB_USER_ID}:{JOB_USER_ID}"))
        };
        let builder = self.mount_job_tmp_sdk(builder, job.attempt_id, job.job.tmpfs_mib);
        let builder = if job.network {
            // Allow public destinations only; the host and private networks stay blocked.
            builder.network(|network| {
                network.policy(NetworkPolicy::from_profiles([NetworkProfile::Public]))
            })
        } else {
            builder.disable_network()
        };
        let vm = match owned_boot(async move {
            let vm = OwnedVm::start(builder, ()).await;
            drop(image_lock);
            vm
        })
        .await
        {
            Ok(vm) => vm,
            Err(error) => return Outcome::StartupFailure(error.to_string()),
        };
        let deadline = tokio::time::Instant::now()
            + Duration::from_secs(job.timeout_secs).saturating_sub(started.elapsed());
        let outcome = {
            let has_artifacts = !job.job.artifacts.is_empty();
            let stream = stream_job(&vm, job, ram, log, started);
            tokio::pin!(stream);
            let mut outcome = tokio::select! {
                biased;
                _ = async { let _ = cancel.wait_for(|stop| *stop).await; } => ram_workspace::cancelled_outcome(ram, has_artifacts),
                result = &mut stream => result,
                _ = tokio::time::sleep_until(deadline) => Outcome::TimedOut,
            };
            if outcome == Outcome::TimedOut
                && let Some(timeout) = ram_workspace::timeout_flush(ram, has_artifacts)
            {
                let flush = async {
                    if ram.is_some() {
                        vm.exec(
                            "/bin/sh",
                            ["-c", &ram_workspace::RamWorkspace::flush_script()],
                        )
                        .await
                    } else {
                        vm.exec("sync", [] as [&str; 0]).await
                    }
                };
                outcome = tokio::select! {
                    biased;
                    _ = async { let _ = cancel.wait_for(|stop| *stop).await; } => ram_workspace::cancelled_outcome(ram, has_artifacts),
                    flushed = tokio::time::timeout(timeout, flush) => {
                        if !flushed.is_ok_and(|result| result.is_ok_and(|out| out.status().success)) {
                            let _ = log.send(b"[branvil] flushing the job at its timeout failed: returned files may be incomplete\n".to_vec()).await;
                        }
                        Outcome::TimedOut
                    }
                };
            }
            if outcome == Outcome::TimedOut
                && let Some(code) = ram.and_then(|r| r.exit_code())
            {
                outcome = Outcome::Exited(code);
            }
            outcome
        };
        vm.close().await;
        outcome
    }
}

async fn stream_job(
    vm: &Sandbox,
    job: &mut JobRun<'_>,
    ram: Option<&ram_workspace::RamWorkspace<'_>>,
    log: &mpsc::Sender<Vec<u8>>,
    boot_started: Instant,
) -> Outcome {
    let script = ram.map_or_else(
        || job_script(&job.job.commands, false),
        |ram| ram.script(job.job, false),
    );
    let mut exec = match vm
        .exec_stream_with("/bin/sh", |exec| {
            exec.args(["-c", &script]).envs(job.env.iter().cloned())
        })
        .await
    {
        Ok(exec) => exec,
        Err(error) => return Outcome::StartupFailure(error.to_string()),
    };
    let mut memory = tokio::time::interval(Duration::from_secs(1));
    let mut started = None;
    let mut peak_kib = 0;
    let outcome = loop {
        tokio::select! {
            _ = memory.tick() => {
                if let Ok(sample) = vm.metrics().await {
                    peak_kib = peak_kib.max(sample.memory_host_resident_bytes.unwrap_or(sample.memory_bytes) / 1024);
                }
                if peak_kib > 0 {
                    *job.peak_memory_mib = Some(peak_kib.div_ceil(1024));
                }
            }
            event = exec.recv() => match event {
                Some(ExecEvent::Started {..}) => {
                    started = Some(Instant::now());
                    let _ = log.send(format!("[branvil] sandbox started in {} ms\n", boot_started.elapsed().as_millis()).into_bytes()).await;
                }
                Some(ExecEvent::Stdout(bytes) | ExecEvent::Stderr(bytes)) => {
                    let _ = log.send(bytes.to_vec()).await;
                }
                Some(ExecEvent::Exited { code }) => break Outcome::Exited(code),
                Some(ExecEvent::Failed(error)) => break Outcome::StartupFailure(error.message),
                Some(ExecEvent::StdinError(_)) => {},
                None => break Outcome::StartupFailure("The microVM disconnected before reporting an exit status".into()),
            }
        }
    };
    if let Some(at) = started {
        let _ = log
            .send(
                format!(
                    "[branvil] commands ran for {} ms\n",
                    at.elapsed().as_millis()
                )
                .into_bytes(),
            )
            .await;
    }
    *job.peak_memory_mib = log_peak_memory(log, peak_kib).await;
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    use gb_vm::sdk::sandbox::{SandboxConfig, VolumeMount};

    #[tokio::test]
    async fn sdk_job_tmp_mount_is_executable_and_matches_the_requested_storage() {
        let runner = Microsandbox::new(Msb::Missing(String::new()), PathBuf::new(), "fixture");
        for size in [None, Some(2048)] {
            let builder = Sandbox::builder("job").image("img:dev");
            let config = runner
                .mount_job_tmp_sdk(builder, "attempt", size)
                .build()
                .await
                .unwrap();
            match (size, config.spec.mounts.as_slice()) {
                (
                    Some(mib),
                    [
                        VolumeMount::Tmpfs {
                            guest,
                            size_mib,
                            options,
                        },
                    ],
                ) => {
                    assert_eq!(guest, "/tmp");
                    assert_eq!(*size_mib, Some(mib));
                    assert!(!options.noexec && !options.readonly);
                }
                (
                    None,
                    [
                        VolumeMount::Named {
                            guest,
                            name,
                            create: Some(create),
                            ..
                        },
                    ],
                ) => {
                    assert_eq!(guest, "/tmp");
                    assert_eq!(name, "bv-tmp-fixture-attempt");
                    assert_eq!(create.capacity_mib, Some(4096));
                }
                (_, mounts) => panic!("unexpected job mounts: {mounts:?}"),
            }
        }
    }

    #[tokio::test]
    async fn sdk_mounts_use_the_same_work_disk_for_preparation_job_and_collection() {
        use gb_vm::sdk::sandbox::DiskImageFormat;

        let dir = tempfile::tempdir().unwrap();
        let overlay = dir.path().join("work.qcow2");
        crate::runner::disk::create_work_overlay(&overlay, 128 << 20).unwrap();
        let cache = JobDisk {
            name: "cache".into(),
            mib: 128,
            path: PathBuf::new(),
        };
        for work in [
            DiskMount::Named {
                name: "work",
                mib: 128,
            },
            DiskMount::Overlay(&overlay),
        ] {
            for phase in ["prep", "job", "collect"] {
                let mut config = SandboxConfig::default();
                config.spec.name = phase.into();
                let builder = SandboxBuilder::from(config).image("img:dev");
                let builder = if phase == "job" {
                    work.attach_to_sdk(builder, "/work")
                } else {
                    HelperDisks {
                        work,
                        cache: (phase == "collect").then_some(&cache),
                    }
                    .attach_to_sdk(builder)
                };
                let config = builder.build().await.unwrap();
                let mounts = &config.spec.mounts;
                let collecting = phase == "collect";
                assert_eq!(mounts.len(), 1 + usize::from(collecting));
                let work_mount = mounts
                    .iter()
                    .find(|mount| mount.guest() == "/work")
                    .unwrap();
                match (work, work_mount) {
                    (DiskMount::Named { .. }, VolumeMount::Named { name, .. }) => {
                        assert_eq!(name, "work");
                    }
                    (
                        DiskMount::Overlay(_),
                        VolumeMount::DiskImage {
                            host,
                            format,
                            fstype,
                            ..
                        },
                    ) => {
                        assert_eq!(host, &overlay);
                        assert_eq!(*format, DiskImageFormat::Qcow2);
                        assert_eq!(fstype.as_deref(), Some("ext4"));
                    }
                    (_, other) => panic!("unexpected /work mount: {other:?}"),
                }
                if collecting {
                    let mount = mounts.iter().find(|mount| mount.guest() == CACHE_MOUNT);
                    assert!(
                        matches!(mount, Some(VolumeMount::Named { name, .. }) if name == "cache")
                    );
                }
            }
        }
    }
}
