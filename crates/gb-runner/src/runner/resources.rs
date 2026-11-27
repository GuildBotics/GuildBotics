//! Best-effort host counters. A missing sample is kept distinct from zero.
use crate::ci::queue::{AttemptResources, PhaseResources};
use std::{future::Future, time::Duration};

pub const INTERVAL: Duration = Duration::from_millis(500);

#[derive(Default, Clone, Copy)]
pub(super) struct Process {
    pub write_bytes: Option<u64>,
    pub cpu_ms: Option<u64>,
    pub rss_kib: Option<u64>,
}

impl Process {
    pub fn add(&mut self, sample: Self) {
        for (total, value) in [
            (&mut self.write_bytes, sample.write_bytes),
            (&mut self.cpu_ms, sample.cpu_ms),
            (&mut self.rss_kib, sample.rss_kib),
        ] {
            if let Some(value) = value {
                *total = Some(total.unwrap_or_default().saturating_add(value));
            }
        }
    }
}

#[derive(Default)]
pub(super) struct Meter {
    previous: Option<(u64, u64)>,
    busy: u64,
    total: u64,
    peak: Option<u32>,
    process: Process,
}

impl Meter {
    pub fn start() -> Self {
        Self {
            previous: host_cpu(),
            ..Self::default()
        }
    }

    pub fn sample(&mut self, process: Process) {
        self.process.write_bytes = self.process.write_bytes.max(process.write_bytes);
        self.process.cpu_ms = self.process.cpu_ms.max(process.cpu_ms);
        self.process.rss_kib = self.process.rss_kib.max(process.rss_kib);
        let Some(current) = host_cpu() else { return };
        if let Some(previous) = self.previous.replace(current)
            && let (Some(busy), Some(total)) = (
                current.0.checked_sub(previous.0),
                current.1.checked_sub(previous.1),
            )
            && total > 0
            && busy <= total
        {
            self.busy += busy;
            self.total += total;
            self.peak = self.peak.max(Some(basis_points(busy, total)));
        }
    }

    pub fn finish(self, result: &mut PhaseResources) -> Option<u64> {
        result.add_usage(self.process.write_bytes, self.process.cpu_ms);
        if self.total > 0 {
            result.host_cpu_busy_ticks += self.busy;
            result.host_cpu_total_ticks += self.total;
            result.host_cpu_average_bps = Some(basis_points(
                result.host_cpu_busy_ticks,
                result.host_cpu_total_ticks,
            ));
            result.host_cpu_peak_bps = result.host_cpu_peak_bps.max(self.peak);
        }
        self.process.rss_kib
    }
}

/// The RAM workspace collects results in the command VM after it marks
/// command exit. Split cumulative counters at the first observed boundary.
pub(super) struct CommandMeter {
    meter: Meter,
    commands: Option<Meter>,
    baseline: Process,
}

impl CommandMeter {
    pub fn start() -> Self {
        Self {
            meter: Meter::start(),
            commands: None,
            baseline: Process::default(),
        }
    }

    pub fn sample(&mut self, process: Process, collecting: bool) {
        let delta = |current: Option<u64>, baseline: Option<u64>| {
            current
                .zip(baseline)
                .and_then(|(current, baseline)| current.checked_sub(baseline))
        };
        if self.commands.is_some() {
            self.meter.sample(Process {
                write_bytes: delta(process.write_bytes, self.baseline.write_bytes),
                cpu_ms: delta(process.cpu_ms, self.baseline.cpu_ms),
                rss_kib: process.rss_kib,
            });
        } else {
            self.meter.sample(process);
            if collecting {
                self.baseline = process;
                let save = Meter {
                    previous: self.meter.previous,
                    ..Meter::default()
                };
                self.commands = Some(std::mem::replace(&mut self.meter, save));
            }
        }
    }

    pub fn finish(mut self, result: &mut AttemptResources) -> Option<u64> {
        self.meter.sample(Process::default());
        match self.commands {
            Some(commands) => commands
                .finish(&mut result.commands)
                .max(self.meter.finish(&mut result.save)),
            None => self.meter.finish(&mut result.commands),
        }
    }
}

fn basis_points(busy: u64, total: u64) -> u32 {
    (u128::from(busy) * 10_000 / u128::from(total)) as u32
}

/// Sample an operation without extending its timeout or changing cancellation.
pub(super) async fn observe<T, F, S>(
    operation: impl Future<Output = T>,
    mut sample: impl FnMut() -> F,
    mut record: impl FnMut(S),
) -> T
where
    F: Future<Output = S>,
{
    let mut interval = tokio::time::interval(INTERVAL);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    tokio::pin!(operation);
    loop {
        tokio::select! {
            biased;
            value = &mut operation => break value,
            _ = interval.tick() => {
                tokio::select! {
                    biased;
                    value = &mut operation => break value,
                    sample = sample() => record(sample),
                }
            }
        }
    }
}

pub(super) async fn observe_phase<T, F>(
    operation: impl Future<Output = T>,
    sample: impl FnMut() -> F,
    resources: &mut PhaseResources,
) -> T
where
    F: Future<Output = Process>,
{
    let mut meter = Meter::start();
    let value = observe(operation, sample, |process| meter.sample(process)).await;
    meter.sample(Process::default());
    meter.finish(resources);
    value
}

pub(super) async fn observe_host<T>(
    operation: impl Future<Output = T>,
    phase: &mut PhaseResources,
) -> T {
    observe_phase(operation, || std::future::ready(Process::default()), phase).await
}

pub(super) async fn measure_filesystem<T: Send + 'static>(
    operation: impl FnOnce() -> std::io::Result<(T, u64)> + Send + 'static,
    phase: &mut PhaseResources,
) -> std::io::Result<T> {
    let (result, usage) = observe_host(
        super::blocking(move || Ok(cache_operation(operation))),
        phase,
    )
    .await?;
    let logical_bytes = result.as_ref().ok().map(|(_, bytes)| *bytes);
    phase.add_usage(usage.write_bytes.or(logical_bytes), usage.cpu_ms);
    result.map(|(result, _)| result)
}

/// Cache filesystem work runs on a dedicated blocking worker for the duration
/// of each call. Linux exposes its I/O counter, independent of other attempts.
pub(super) fn cache_operation<T>(operation: impl FnOnce() -> T) -> (T, Process) {
    let before = thread_writes();
    let cpu_start = thread_cpu_ms();
    let result = operation();
    let written = thread_writes()
        .zip(before)
        .and_then(|(end, start)| end.checked_sub(start));
    let cpu_ms = thread_cpu_ms()
        .zip(cpu_start)
        .and_then(|(end, start)| end.checked_sub(start));
    (
        result,
        Process {
            write_bytes: written,
            cpu_ms,
            rss_kib: None,
        },
    )
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn thread_cpu_ms() -> Option<u64> {
    let mut time = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // The calling blocking worker is the only thread executing this operation.
    (unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut time) } == 0)
        .then(|| time.tv_sec as u64 * 1000 + time.tv_nsec as u64 / 1_000_000)
}

#[cfg(windows)]
fn thread_cpu_ms() -> Option<u64> {
    use windows_sys::Win32::System::Threading::{GetCurrentThread, GetThreadTimes};
    let [mut creation, mut exit, mut kernel, mut user] = Default::default();
    // The current thread is a pseudo-handle without ownership.
    (unsafe {
        GetThreadTimes(
            GetCurrentThread(),
            &mut creation,
            &mut exit,
            &mut kernel,
            &mut user,
        )
    } != 0)
        .then(|| (filetime(kernel) + filetime(user)) / 10_000)
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn thread_cpu_ms() -> Option<u64> {
    None
}

#[cfg(target_os = "linux")]
fn thread_writes() -> Option<u64> {
    linux_writes("/proc/thread-self/io")
}

#[cfg(not(target_os = "linux"))]
fn thread_writes() -> Option<u64> {
    None
}

#[cfg(target_os = "linux")]
fn linux_writes(path: impl AsRef<std::path::Path>) -> Option<u64> {
    std::fs::read_to_string(path)
        .ok()?
        .lines()
        .find_map(|line| line.strip_prefix("write_bytes: "))?
        .parse()
        .ok()
}

#[cfg(target_os = "linux")]
pub(super) fn process(pid: u32) -> Process {
    let cpu_ms = (|| {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        // The executable name is parenthesized and can contain spaces or ')'.
        let mut fields = stat.rsplit_once(')')?.1.split_whitespace().skip(11);
        // Include CPU time of children already reaped by this process, which
        // no longer appear in the sampled tree.
        let ticks = fields.by_ref().take(4).try_fold(0_u64, |sum, item| {
            sum.checked_add(item.parse::<u64>().ok()?)
        })?;
        // sysconf returns a positive clock frequency on success.
        let hz = u64::try_from(unsafe { libc::sysconf(libc::_SC_CLK_TCK) }).ok()?;
        ticks.saturating_mul(1000).checked_div(hz)
    })();
    let rss_kib = std::fs::read_to_string(format!("/proc/{pid}/status"))
        .ok()
        .and_then(|status| {
            let value = status
                .lines()
                .find_map(|line| line.strip_prefix("VmRSS:"))?;
            value.split_whitespace().next()?.parse().ok()
        });
    Process {
        write_bytes: linux_writes(format!("/proc/{pid}/io")),
        cpu_ms,
        rss_kib,
    }
}

#[cfg(target_os = "linux")]
fn host_cpu() -> Option<(u64, u64)> {
    let stat = std::fs::read_to_string("/proc/stat").ok()?;
    let fields = stat
        .lines()
        .next()?
        .strip_prefix("cpu ")?
        .split_whitespace()
        .take(8)
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let [user, nice, system, idle, wait, irq, softirq, steal] = *fields.as_slice() else {
        return None;
    };
    let busy = user + nice + system + irq + softirq + steal;
    Some((busy, busy + idle + wait))
}

#[cfg(target_os = "macos")]
pub(super) fn process(pid: u32) -> Process {
    // libproc fills this fixed-layout output buffer for RUSAGE_INFO_V2.
    let mut info: libc::rusage_info_v2 = unsafe { std::mem::zeroed() };
    if unsafe {
        libc::proc_pid_rusage(
            pid as i32,
            libc::RUSAGE_INFO_V2,
            std::ptr::from_mut(&mut info).cast(),
        )
    } != 0
    {
        return Process::default();
    }
    Process {
        write_bytes: Some(info.ri_diskio_byteswritten),
        cpu_ms: Some(info.ri_user_time.saturating_add(info.ri_system_time) / 1_000_000),
        rss_kib: Some(info.ri_resident_size.div_ceil(1024)),
    }
}

#[cfg(target_os = "macos")]
#[allow(
    deprecated,
    reason = "libc exposes the macOS host port ABI used for CPU counters"
)]
fn host_cpu() -> Option<(u64, u64)> {
    static HOST: std::sync::OnceLock<libc::mach_port_t> = std::sync::OnceLock::new();
    let mut ticks = [0_u32; 4];
    let mut count = libc::HOST_CPU_LOAD_INFO_COUNT;
    // Keep one host send right for the process lifetime. Mach fills four CPU-state counters.
    unsafe {
        let host = *HOST.get_or_init(|| libc::mach_host_self());
        let status = libc::host_statistics(
            host,
            libc::HOST_CPU_LOAD_INFO,
            ticks.as_mut_ptr().cast(),
            &mut count,
        );
        if status != libc::KERN_SUCCESS || count != libc::HOST_CPU_LOAD_INFO_COUNT {
            return None;
        }
    }
    let [user, system, idle, nice] = ticks.map(u64::from);
    let busy = user + system + nice;
    Some((busy, busy + idle))
}

#[cfg(windows)]
fn filetime(ticks: windows_sys::Win32::Foundation::FILETIME) -> u64 {
    (u64::from(ticks.dwHighDateTime) << 32) | u64::from(ticks.dwLowDateTime)
}

#[cfg(windows)]
pub(super) fn process(pid: u32) -> Process {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::System::{
        ProcessStatus::{K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
        Threading::{
            GetProcessIoCounters, GetProcessTimes, IO_COUNTERS, OpenProcess,
            PROCESS_QUERY_LIMITED_INFORMATION,
        },
    };
    // The handle is closed even if an individual counter is unavailable.
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return Process::default();
    }
    let handle = unsafe { OwnedHandle::from_raw_handle(handle) };
    let handle = handle.as_raw_handle();
    let mut io = IO_COUNTERS::default();
    let [mut creation, mut exit, mut kernel, mut user] = Default::default();
    let size = std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
    let mut memory = PROCESS_MEMORY_COUNTERS {
        cb: size,
        ..Default::default()
    };
    Process {
        write_bytes: (unsafe { GetProcessIoCounters(handle, &mut io) } != 0)
            .then_some(io.WriteTransferCount),
        cpu_ms: (unsafe {
            GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user)
        } != 0)
            .then(|| (filetime(kernel) + filetime(user)) / 10_000),
        rss_kib: (unsafe { K32GetProcessMemoryInfo(handle, &mut memory, size) } != 0)
            .then(|| (memory.WorkingSetSize as u64).div_ceil(1024)),
    }
}

#[cfg(windows)]
fn host_cpu() -> Option<(u64, u64)> {
    use windows_sys::Win32::System::Threading::GetSystemTimes;
    let [mut idle, mut kernel, mut user] = Default::default();
    // Kernel time includes idle time on Windows.
    if unsafe { GetSystemTimes(&mut idle, &mut kernel, &mut user) } == 0 {
        return None;
    }
    let total = filetime(kernel) + filetime(user);
    Some((total.checked_sub(filetime(idle))?, total))
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
pub(super) fn process(_pid: u32) -> Process {
    Process::default()
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn host_cpu() -> Option<(u64, u64)> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_ram_cache_writes_from_cumulative_command_counters() {
        let mut meter = CommandMeter::start();
        let process = |writes, cpu, rss| Process {
            write_bytes: Some(writes),
            cpu_ms: Some(cpu),
            rss_kib: Some(rss),
        };
        meter.sample(process(1024, 70, 3000), false);
        meter.sample(process(4096, 100, 4000), true);
        meter.sample(process(8192, 120, 3900), true);
        meter.sample(Process::default(), true);

        let mut resources = AttemptResources::default();
        resources.save.add_usage(Some(512), Some(5));
        assert_eq!(meter.finish(&mut resources), Some(4000));
        assert_eq!(resources.commands.disk_write_bytes, Some(4096));
        assert_eq!(resources.commands.cpu_time_ms, Some(100));
        assert_eq!(resources.save.disk_write_bytes, Some(4608));
        assert_eq!(resources.save.cpu_time_ms, Some(25));
    }
}
