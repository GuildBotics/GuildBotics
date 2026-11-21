use super::*;
use gb_vm::sdk::sandbox::{PullPolicy, SandboxBuilder};
use gb_vm::sdk::{
    ExecEvent, Image, MicrosandboxError, NetworkPolicy, NetworkProfile, RegistryAuth, Sandbox,
};
use gb_vm::{OwnedVm, owned_boot};

impl HelperDisks<'_> {
    fn attach_to_sdk(&self, builder: SandboxBuilder) -> SandboxBuilder {
        self.mounts().fold(builder, |builder, (at, disk)| {
            builder.volume(at, |mount| mount.named(&disk.name))
        })
    }
}

impl Microsandbox {
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
        disk: &JobDisk,
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
        let builder = builder
            .cpus(job.cpus as u8)
            .memory(job.memory_mib as u32)
            .user(format!("{JOB_USER_ID}:{JOB_USER_ID}"))
            .workdir("/work")
            .max_duration(job.timeout_secs + 60)
            .volume("/work", |m| m.named(&disk.name))
            .volume("/tmp", |m| {
                m.named_with(self.tmp_volume(job.attempt_id), |v| {
                    v.create().disk().size(4096)
                })
            });
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
        let outcome = tokio::select! {
            biased;
            _ = cancel.wait_for(|stop| *stop) => Outcome::Cancelled,
            result = tokio::time::timeout_at(deadline, stream_job(&vm, job, log, started)) => {
                result.unwrap_or(Outcome::TimedOut)
            }
        };
        let outcome = if outcome == Outcome::TimedOut && !job.job.artifacts.is_empty() {
            tokio::select! {
                biased;
                _ = async { let _ = cancel.wait_for(|stop| *stop).await; } => Outcome::Cancelled,
                flushed = tokio::time::timeout(TIMEOUT_FLUSH, vm.exec("sync", [] as [&str; 0])) => {
                    if !flushed.is_ok_and(|result| result.is_ok_and(|out| out.status().success)) {
                        let _ = log.send(b"[branvil] flushing the job at its timeout failed: its artifacts may miss what it wrote last\n".to_vec()).await;
                    }
                    outcome
                }
            }
        } else {
            outcome
        };
        vm.close().await;
        outcome
    }
}

async fn stream_job(
    vm: &Sandbox,
    job: &mut JobRun<'_>,
    log: &mpsc::Sender<Vec<u8>>,
    boot_started: Instant,
) -> Outcome {
    let script = job_script(&job.job.commands, false);
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
    async fn the_sdk_mounts_the_cache_disk_only_when_collecting_a_save() {
        let disk = |name: &str| JobDisk {
            name: name.into(),
            mib: 128,
            path: PathBuf::new(),
        };
        let (work, cache) = (disk("work"), disk("cache"));
        for saving in [false, true] {
            let mut config = SandboxConfig::default();
            config.spec.name = "helper".into();
            let builder = SandboxBuilder::from(config).image("img:dev");
            let config = HelperDisks {
                work: &work,
                cache: saving.then_some(&cache),
            }
            .attach_to_sdk(builder)
            .build()
            .await
            .unwrap();
            let mut expected = vec![("/work", "work")];
            if saving {
                expected.push(("/.branvil-cache", "cache"));
            }
            let mut mounts: Vec<_> = config
                .spec
                .mounts
                .iter()
                .map(|mount| match mount {
                    VolumeMount::Named { guest, name, .. } => (guest.as_str(), name.as_str()),
                    other => panic!("unexpected mount: {other:?}"),
                })
                .collect();
            expected.sort_unstable();
            mounts.sort_unstable();
            assert_eq!(mounts, expected);
        }
    }
}
