//! On-disk formats used by the runner's dependency cache.

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

/// Makes what was written to `path` reach the file before it is read
/// elsewhere. Windows flushes only through a handle that may write.
pub fn flush(path: &Path) -> io::Result<()> {
    fs::OpenOptions::new().write(true).open(path)?.sync_all()
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

const CLUSTER: usize = 1 << 16;
pub const CACHE_BASE: &str = "cache-base.raw";

/// Creates an empty qcow2 v3 overlay on the raw image beside it. All guest
/// writes go to the overlay. The backing format is explicit so a raw image
/// whose contents resemble a qcow2 header is still opened as raw.
pub fn create_work_overlay(path: &Path, size: u64) -> io::Result<()> {
    const HEADER_LEN: usize = 104;
    // L2 entries are 8 bytes; each maps one data cluster.
    const L1_COVERAGE: u64 = (CLUSTER as u64 / 8) * CLUSTER as u64;
    const L1_OFFSET: usize = 3 * CLUSTER;
    const BACKING_OFFSET: usize = HEADER_LEN + 16 + 8;
    let l1_entries = size.div_ceil(L1_COVERAGE);
    let l1_clusters = (l1_entries * 8).div_ceil(CLUSTER as u64);
    let metadata_clusters = 3 + l1_clusters;
    // The single refcount block must cover all preallocated clusters.
    if size == 0 || metadata_clusters > (CLUSTER / 2) as u64 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "unsupported /work size for a qcow2 overlay",
        ));
    }

    let mut metadata = vec![0; L1_OFFSET];
    let header = &mut metadata[..CLUSTER];
    header[..4].copy_from_slice(b"QFI\xfb");
    header[4..8].copy_from_slice(&3u32.to_be_bytes());
    header[8..16].copy_from_slice(&(BACKING_OFFSET as u64).to_be_bytes());
    header[16..20].copy_from_slice(&(CACHE_BASE.len() as u32).to_be_bytes());
    header[20..24].copy_from_slice(&16u32.to_be_bytes());
    header[24..32].copy_from_slice(&size.to_be_bytes());
    header[36..40].copy_from_slice(&(l1_entries as u32).to_be_bytes());
    header[40..48].copy_from_slice(&(L1_OFFSET as u64).to_be_bytes());
    header[48..56].copy_from_slice(&(CLUSTER as u64).to_be_bytes());
    header[56..60].copy_from_slice(&1u32.to_be_bytes());
    header[96..100].copy_from_slice(&4u32.to_be_bytes());
    header[100..104].copy_from_slice(&(HEADER_LEN as u32).to_be_bytes());
    header[104..108].copy_from_slice(&0xE279_2ACAu32.to_be_bytes());
    header[108..112].copy_from_slice(&3u32.to_be_bytes());
    header[112..115].copy_from_slice(b"raw");
    header[BACKING_OFFSET..BACKING_OFFSET + CACHE_BASE.len()]
        .copy_from_slice(CACHE_BASE.as_bytes());

    // The refcount table points at cluster 2, which counts itself as well
    // as the header, the table and every L1 cluster. L1 starts all zero.
    metadata[CLUSTER..CLUSTER + 8].copy_from_slice(&(2 * CLUSTER as u64).to_be_bytes());
    for count in metadata[2 * CLUSTER..][..metadata_clusters as usize * 2].chunks_exact_mut(2) {
        count.copy_from_slice(&1u16.to_be_bytes());
    }
    let mut file = fs::File::create_new(path)?;
    let result = file
        .write_all(&metadata)
        .and_then(|()| file.set_len(metadata_clusters * CLUSTER as u64));
    drop(file);
    if result.is_err() {
        let _ = fs::remove_file(path);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_closed_image_can_be_flushed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("disk.raw");
        fs::write(&path, b"head").unwrap();
        flush(&path).unwrap();
        assert_eq!(fs::read(path).unwrap(), b"head");
    }

    #[tokio::test]
    async fn the_overlay_reads_raw_backing_and_keeps_writes_after_reopening() {
        use imago::file::File;
        use imago::qcow2::Qcow2;
        use imago::{FormatAccess, FormatDriverBuilder, PermissiveImplicitOpenGate};

        let dir = tempfile::tempdir().unwrap();
        let published = dir.path().join("disk.raw");
        let base = dir.path().join(CACHE_BASE);
        let overlay = dir.path().join("work.qcow2");
        // A qcow2 signature at the start of the raw image must be read as data.
        let mut original = vec![0x12u8; CLUSTER];
        original[..4].copy_from_slice(b"QFI\xfb");
        fs::write(&published, &original).unwrap();
        fs::hard_link(&published, &base).unwrap();
        let size = 512 << 20;
        create_work_overlay(&overlay, size).unwrap();

        let qcow = Qcow2::<File>::builder_path(&overlay)
            .write(true)
            .open(PermissiveImplicitOpenGate::default())
            .await
            .unwrap();
        assert_eq!(
            qcow.implicit_backing_file().map(String::as_str),
            Some(CACHE_BASE)
        );
        assert_eq!(
            qcow.implicit_backing_format().map(String::as_str),
            Some("raw")
        );
        let image = FormatAccess::new(qcow);
        assert_eq!(image.size(), size);
        let mut data = vec![0; 2 * CLUSTER];
        image.read(data.as_mut_slice(), 0).await.unwrap();
        assert_eq!(&data[..CLUSTER], &original);
        assert!(data[CLUSTER..].iter().all(|&byte| byte == 0));

        image.write(&b"updated"[..], 0).await.unwrap();
        image
            .write(&b"new block"[..], CLUSTER as u64)
            .await
            .unwrap();
        image.flush().await.unwrap();
        drop(image);
        assert_eq!(fs::read(&published).unwrap(), original);
        fs::remove_file(published).unwrap();

        let qcow = Qcow2::<File>::builder_path(&overlay)
            .open(PermissiveImplicitOpenGate::default())
            .await
            .unwrap();
        FormatAccess::new(qcow)
            .read(data.as_mut_slice(), 0)
            .await
            .unwrap();
        assert_eq!(&data[..7], b"updated");
        assert_eq!(&data[7..CLUSTER], &original[7..]);
        assert_eq!(&data[CLUSTER..CLUSTER + 9], b"new block");
        assert_eq!(fs::read(base).unwrap(), original);
    }

    #[test]
    fn overlay_metadata_covers_every_l1_cluster() {
        let dir = tempfile::tempdir().unwrap();
        for (size, entries, clusters) in [
            (512 << 20, 1, 4),
            ((512 << 20) + 1, 2, 4),
            (16_794 << 20, 33, 4),
            ((4 << 40) + 1, 8193, 5),
        ] {
            let path = dir.path().join(format!("{entries}.qcow2"));
            create_work_overlay(&path, size).unwrap();
            let bytes = fs::read(path).unwrap();
            assert_eq!(u64::from_be_bytes(bytes[24..32].try_into().unwrap()), size);
            assert_eq!(
                u32::from_be_bytes(bytes[36..40].try_into().unwrap()),
                entries
            );
            assert_eq!(
                u64::from_be_bytes(bytes[40..48].try_into().unwrap()),
                3 * CLUSTER as u64
            );
            assert_eq!(
                u64::from_be_bytes(bytes[CLUSTER..CLUSTER + 8].try_into().unwrap()),
                2 * CLUSTER as u64
            );
            assert_eq!(bytes.len(), clusters * CLUSTER);
            for (i, count) in bytes[2 * CLUSTER..3 * CLUSTER].chunks_exact(2).enumerate() {
                assert_eq!(count, if i < clusters { &[0, 1] } else { &[0, 0] });
            }
            assert!(bytes[3 * CLUSTER..].iter().all(|&b| b == 0));
        }
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
}
