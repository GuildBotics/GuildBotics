//! Disk images moved between the dependency cache and a job's `/work`
//! volume: a clone that shares the blocks where the file system can (APFS),
//! otherwise a copy that writes only the blocks holding data.

use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

/// The bytes `path` takes on its disk: a sparse image's holes take none.
pub fn allocated(path: &Path) -> io::Result<u64> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        Ok(fs::symlink_metadata(path)?.blocks() * 512)
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{GetCompressedFileSizeW, INVALID_FILE_SIZE};
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        let mut high = 0u32;
        // SAFETY: `wide` is a NUL-terminated path and `high` outlives the call.
        let low = unsafe { GetCompressedFileSizeW(wide.as_ptr(), &mut high) };
        if low == INVALID_FILE_SIZE {
            let e = io::Error::last_os_error();
            if e.raw_os_error() != Some(0) {
                return Err(e);
            }
        }
        Ok((u64::from(high) << 32) | u64::from(low))
    }
}

/// Makes `to`, which must not exist, a copy of `from`.
pub fn copy(from: &Path, to: &Path) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    if clone(from, to).is_ok() {
        return Ok(());
    }
    sparse_copy(from, to)
}

/// Moves `from` to `to`, replacing it: a rename on the same file system,
/// otherwise a [`copy`] (and `from` goes when it can). Once it succeeded,
/// `to` has `from`'s content.
pub fn place(from: &Path, to: &Path) -> io::Result<()> {
    if fs::rename(from, to).is_ok() {
        return Ok(());
    }
    let part = to.with_extension("part");
    let _ = fs::remove_file(&part);
    copy(from, &part)?;
    fs::rename(&part, to)?;
    let _ = fs::remove_file(from);
    Ok(())
}

/// The size of the ext4 file system on the image `path`, from its
/// superblock: it can be smaller than the file.
pub fn ext4_bytes(path: &Path) -> io::Result<u64> {
    let mut sb = [0u8; 1024];
    let mut f = fs::File::open(path)?;
    f.seek(SeekFrom::Start(1024))?;
    f.read_exact(&mut sb)?;
    let u32_at = |at: usize| u64::from(u32::from_le_bytes(sb[at..at + 4].try_into().unwrap()));
    let invalid = |what: &str| io::Error::new(io::ErrorKind::InvalidData, what.to_string());
    if u16::from_le_bytes([sb[0x38], sb[0x39]]) != 0xEF53 {
        return Err(invalid("not an ext4 file system"));
    }
    let log = u32_at(0x18);
    if log > 6 {
        return Err(invalid("an unknown block size"));
    }
    // INCOMPAT_64BIT: the block count has a high half.
    let high = if u32_at(0x60) & 0x80 != 0 {
        u32_at(0x150)
    } else {
        0
    };
    Ok(((high << 32) | u32_at(0x04)) << (10 + log))
}

/// Makes `path` at least `len` bytes long; what it gains is a hole.
pub fn grow(path: &Path, len: u64) -> io::Result<()> {
    let file = fs::OpenOptions::new().write(true).open(path)?;
    if file.metadata()?.len() < len {
        file.set_len(len)?;
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn clone(from: &Path, to: &Path) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let c = |p: &Path| CString::new(p.as_os_str().as_bytes()).map_err(io::Error::other);
    // <sys/clonefile.h>: do not follow a link at the source.
    const CLONE_NOFOLLOW: u32 = 0x0001;
    let (src, dst) = (c(from)?, c(to)?);
    // SAFETY: both are NUL-terminated paths that outlive the call.
    if unsafe { libc::clonefile(src.as_ptr(), dst.as_ptr(), CLONE_NOFOLLOW) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// The ranges of `file` (`len` bytes) that may hold data: what `SEEK_DATA`
/// and `SEEK_HOLE` tell, so that the holes of a large image are never read;
/// the whole file where they tell nothing.
fn data_ranges(file: &fs::File, len: u64) -> Vec<(u64, u64)> {
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    {
        use std::os::fd::AsRawFd;
        let fd = file.as_raw_fd();
        let mut ranges = Vec::new();
        let mut at = 0i64;
        while (at as u64) < len {
            // SAFETY: lseek on the open file's descriptor.
            let start = unsafe { libc::lseek(fd, at, libc::SEEK_DATA) };
            if start < 0 {
                // ENXIO: no data after `at`; anything else: no answer.
                let e = io::Error::last_os_error();
                return if e.raw_os_error() == Some(libc::ENXIO) {
                    ranges
                } else {
                    vec![(0, len)]
                };
            }
            // SAFETY: as above.
            let end = unsafe { libc::lseek(fd, start, libc::SEEK_HOLE) };
            if end < 0 {
                return vec![(0, len)];
            }
            ranges.push((start as u64, end as u64));
            at = end;
        }
        ranges
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        let _ = file;
        vec![(0, len)]
    }
}

/// Copies `from` into a new `to`: the ranges that may hold data
/// ([`data_ranges`]) in blocks of 1 MiB, skipping those that are all zero.
/// The rest stays holes of `to`, which is sparse.
fn sparse_copy(from: &Path, to: &Path) -> io::Result<()> {
    const BLOCK: usize = 1 << 20;
    let mut src = fs::File::open(from)?;
    let len = src.metadata()?.len();
    let mut dst = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(to)?;
    let copied = (|| {
        mark_sparse(&dst)?;
        dst.set_len(len)?;
        let mut buf = vec![0u8; BLOCK];
        for (start, end) in data_ranges(&src, len) {
            src.seek(SeekFrom::Start(start))?;
            let mut at = start;
            while at < end {
                let want = BLOCK.min(usize::try_from(end - at).unwrap_or(BLOCK));
                let mut n = 0;
                while n < want {
                    match src.read(&mut buf[n..want])? {
                        0 => break,
                        read => n += read,
                    }
                }
                if n == 0 {
                    break;
                }
                if buf[..n].iter().any(|b| *b != 0) {
                    dst.seek(SeekFrom::Start(at))?;
                    dst.write_all(&buf[..n])?;
                }
                at += n as u64;
            }
        }
        dst.sync_all()
    })();
    if copied.is_err() {
        let _ = fs::remove_file(to);
    }
    copied
}

/// Unwritten ranges of a file are holes on Unix; NTFS fills them with zeros
/// unless the file is marked sparse.
#[cfg(windows)]
fn mark_sparse(file: &fs::File) -> io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::IO::DeviceIoControl;
    use windows_sys::Win32::System::Ioctl::FSCTL_SET_SPARSE;
    let mut returned = 0u32;
    // SAFETY: the handle is the open file's; no buffers are passed.
    let ok = unsafe {
        DeviceIoControl(
            file.as_raw_handle(),
            FSCTL_SET_SPARSE,
            std::ptr::null(),
            0,
            std::ptr::null_mut(),
            0,
            &mut returned,
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn mark_sparse(_: &fs::File) -> io::Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An image of 64 MiB with data in two blocks only (APFS fills a gap
    /// of a few MiB before a write, not one this large).
    fn image(path: &Path) {
        let mut f = fs::File::create(path).unwrap();
        f.set_len(64 << 20).unwrap();
        f.write_all(b"head").unwrap();
        f.seek(SeekFrom::Start(40 << 20)).unwrap();
        f.write_all(b"middle").unwrap();
    }

    /// Whether `a` and `b` hold the same bytes, read a block at a time.
    fn same(a: &Path, b: &Path) -> bool {
        let (mut a, mut b) = (fs::File::open(a).unwrap(), fs::File::open(b).unwrap());
        let (mut x, mut y) = (vec![0u8; 1 << 20], vec![0u8; 1 << 20]);
        loop {
            let (n, m) = (a.read(&mut x).unwrap(), b.read(&mut y).unwrap());
            if n != m || x[..n] != y[..m] {
                return false;
            }
            if n == 0 {
                return true;
            }
        }
    }

    /// The first 4 bytes of `path`.
    fn head(path: &Path) -> [u8; 4] {
        let mut h = [0u8; 4];
        fs::File::open(path).unwrap().read_exact(&mut h).unwrap();
        h
    }

    /// A copy has the image's content and size, and takes no more of the
    /// disk than its blocks with data, whichever way it was made.
    #[test]
    fn a_copy_keeps_the_content_and_the_holes() {
        let dir = tempfile::tempdir().unwrap();
        let from = dir.path().join("from.raw");
        image(&from);
        for name in ["copy", "sparse"] {
            let to = dir.path().join(name);
            if name == "copy" {
                copy(&from, &to).unwrap();
            } else {
                sparse_copy(&from, &to).unwrap();
            }
            assert!(same(&to, &from), "{name}");
            assert!(allocated(&to).unwrap() <= 4 << 20, "{name}");
        }
        // It never replaces a file.
        assert!(copy(&from, &dir.path().join("copy")).is_err());
        assert_eq!(
            fs::metadata(dir.path().join("copy")).unwrap().len(),
            64 << 20
        );
    }

    /// A sparse copy reads only where the image holds data (a file of 64 GiB
    /// with two small blocks of data has a few MiB of ranges to read), and
    /// copies a file with no data, or with data up to its end, whole.
    #[test]
    fn a_sparse_copy_reads_only_the_data() {
        let dir = tempfile::tempdir().unwrap();
        let (from, to) = (dir.path().join("big.raw"), dir.path().join("copy.raw"));
        let mut f = fs::File::create(&from).unwrap();
        f.set_len(64 << 30).unwrap();
        f.write_all(b"head").unwrap();
        f.seek(SeekFrom::Start(40 << 30)).unwrap();
        f.write_all(b"middle").unwrap();
        drop(f);
        let ranges = data_ranges(&fs::File::open(&from).unwrap(), 64 << 30);
        assert!(
            ranges.iter().map(|(a, b)| b - a).sum::<u64>() <= 8 << 20,
            "{ranges:?}"
        );
        sparse_copy(&from, &to).unwrap();
        assert_eq!(fs::metadata(&to).unwrap().len(), 64 << 30);
        let mut middle = [0u8; 6];
        let mut t = fs::File::open(&to).unwrap();
        t.seek(SeekFrom::Start(40 << 30)).unwrap();
        t.read_exact(&mut middle).unwrap();
        assert_eq!(&middle, b"middle");
        assert_eq!(&head(&to), b"head");
        assert!(allocated(&to).unwrap() <= 4 << 20);
        for (name, tail) in [("empty", &b""[..]), ("tail", &b"end"[..])] {
            let (from, to) = (
                dir.path().join(name),
                dir.path().join(format!("{name}.copy")),
            );
            let mut f = fs::File::create(&from).unwrap();
            f.set_len((64 << 20) - tail.len() as u64).unwrap();
            f.seek(SeekFrom::End(0)).unwrap();
            f.write_all(tail).unwrap();
            drop(f);
            sparse_copy(&from, &to).unwrap();
            assert!(same(&from, &to), "{name}");
            assert_eq!(fs::metadata(&to).unwrap().len(), 64 << 20, "{name}");
        }
    }

    /// A copy writes nothing to the original: changing one leaves the other.
    #[test]
    fn a_copy_is_independent_of_its_original() {
        let dir = tempfile::tempdir().unwrap();
        let (from, to) = (dir.path().join("from.raw"), dir.path().join("to.raw"));
        image(&from);
        copy(&from, &to).unwrap();
        let mut f = fs::OpenOptions::new().write(true).open(&to).unwrap();
        f.write_all(b"HEAD").unwrap();
        drop(f);
        assert_eq!(&head(&from), b"head");
        assert_eq!(&head(&to), b"HEAD");
    }

    /// The size of an ext4 file system is read from its superblock, whatever
    /// the file's length; anything else is refused.
    #[test]
    fn the_file_systems_size_comes_from_its_superblock() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fs.raw");
        let mut sb = [0u8; 1024];
        sb[0x04..0x08].copy_from_slice(&5u32.to_le_bytes());
        sb[0x18..0x1c].copy_from_slice(&2u32.to_le_bytes());
        sb[0x38..0x3a].copy_from_slice(&0xEF53u16.to_le_bytes());
        let mut f = fs::File::create(&path).unwrap();
        f.set_len(1 << 20).unwrap();
        f.seek(SeekFrom::Start(1024)).unwrap();
        f.write_all(&sb).unwrap();
        assert_eq!(ext4_bytes(&path).unwrap(), 5 * 4096);
        // 64-bit block counts.
        sb[0x60..0x64].copy_from_slice(&0x80u32.to_le_bytes());
        sb[0x150..0x154].copy_from_slice(&1u32.to_le_bytes());
        f.seek(SeekFrom::Start(1024)).unwrap();
        f.write_all(&sb).unwrap();
        assert_eq!(ext4_bytes(&path).unwrap(), ((1u64 << 32) + 5) * 4096);
        fs::write(&path, vec![0; 4096]).unwrap();
        assert!(ext4_bytes(&path).is_err());
        fs::write(&path, b"short").unwrap();
        assert!(ext4_bytes(&path).is_err());
    }

    /// Placing replaces the target and leaves nothing at the source; growing
    /// adds a hole and never shrinks.
    #[test]
    fn placing_replaces_and_growing_only_grows() {
        let dir = tempfile::tempdir().unwrap();
        let (from, to) = (dir.path().join("from.raw"), dir.path().join("to.raw"));
        image(&from);
        fs::write(&to, "old").unwrap();
        place(&from, &to).unwrap();
        assert!(!from.exists());
        assert_eq!(&head(&to), b"head");
        let before = allocated(&to).unwrap();
        grow(&to, 128 << 20).unwrap();
        assert_eq!(fs::metadata(&to).unwrap().len(), 128 << 20);
        assert!(allocated(&to).unwrap() <= before + (1 << 20));
        grow(&to, 1).unwrap();
        assert_eq!(fs::metadata(&to).unwrap().len(), 128 << 20);
    }
}
