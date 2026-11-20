use crate::ci::api::Machine;

pub(super) fn sample() -> Option<Machine> {
    let (total, available) = memory_mib()?;
    let logical_cpus = u32::try_from(std::thread::available_parallelism().ok()?.get()).ok()?;
    (total > 0 && available <= total).then_some(Machine {
        logical_cpus,
        total_memory_mib: total,
        available_memory_mib: available,
    })
}

#[cfg(target_os = "linux")]
fn memory_mib() -> Option<(u64, u64)> {
    parse_meminfo(&std::fs::read_to_string("/proc/meminfo").ok()?)
}

#[cfg(any(target_os = "linux", test))]
fn parse_meminfo(text: &str) -> Option<(u64, u64)> {
    let read = |name: &str| {
        let value = text.lines().find_map(|line| line.strip_prefix(name))?;
        let mut fields = value.split_whitespace();
        let kib = fields.next()?.parse::<u64>().ok()?;
        (fields.next()? == "kB" && fields.next().is_none()).then_some(kib / 1024)
    };
    Some((read("MemTotal:")?, read("MemAvailable:")?))
}

#[cfg(target_os = "macos")]
fn memory_mib() -> Option<(u64, u64)> {
    use std::ffi::CStr;

    fn read<T: Copy + Default>(key: &CStr) -> Option<T> {
        let mut value = T::default();
        let mut size = std::mem::size_of::<T>();
        // The sysctl writes at most `size` bytes into the typed buffer.
        let result = unsafe {
            libc::sysctlbyname(
                key.as_ptr(),
                std::ptr::from_mut(&mut value).cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        };
        (result == 0 && size == std::mem::size_of::<T>()).then_some(value)
    }

    let total = read::<u64>(c"hw.memsize")? >> 20;
    let percent = u64::from(read::<u32>(c"kern.memorystatus_level")?);
    Some((total, total.checked_mul(percent)? / 100))
}

#[cfg(windows)]
fn memory_mib() -> Option<(u64, u64)> {
    #[repr(C)]
    struct MemoryStatus {
        length: u32,
        load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page_file: u64,
        avail_page_file: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_extended_virtual: u64,
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GlobalMemoryStatusEx(status: *mut MemoryStatus) -> i32;
    }

    // All fields are integers. The API requires an initialized length.
    let mut status: MemoryStatus = unsafe { std::mem::zeroed() };
    status.length = std::mem::size_of::<MemoryStatus>() as u32;
    (unsafe { GlobalMemoryStatusEx(&mut status) } != 0)
        .then_some((status.total_phys >> 20, status.avail_phys >> 20))
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn memory_mib() -> Option<(u64, u64)> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linux_uses_available_including_reclaimable_memory() {
        assert_eq!(
            parse_meminfo("MemTotal: 4096 kB\nMemFree: 512 kB\nMemAvailable: 2048 kB"),
            Some((4, 2))
        );
        assert_eq!(parse_meminfo("MemTotal: 4096 kB\nMemFree: 512 kB"), None);
        assert_eq!(parse_meminfo("MemTotal: 4096 kB\nMemAvailable: 1 MB"), None);
        assert_eq!(
            parse_meminfo("MemTotal: 4096 kB\nMemAvailable: 0 kB"),
            Some((4, 0))
        );
    }
}
