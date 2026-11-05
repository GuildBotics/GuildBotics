//! Runs at the destination boundary, never on a host with guest-owned paths.
use anyhow::{Result, ensure};
use std::{
    collections::BTreeSet,
    fs,
    io::{Cursor, Read, Write},
    path::{Path, PathBuf},
};
pub const MAX_ARTIFACT_BYTES: u64 = 100 * 1024 * 1024;
pub fn attachment(content: &[u8], destination: &Path, name: &str, id: &str) -> Result<PathBuf> {
    let name = [name, id]
        .into_iter()
        .find(|s| {
            !s.trim_matches([' ', '.']).is_empty()
                && !s
                    .chars()
                    .any(|c| c.is_control() || "/\\:*?\"<>|".contains(c))
        })
        .ok_or_else(|| anyhow::anyhow!("cli.member.attachment_name"))?;
    fs::create_dir_all(destination)?;
    let destination = fs::canonicalize(destination)?;
    let target = destination.join(name);
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&target)?;
    file.write_all(content)?;
    Ok(target)
}
pub fn extract(content: &[u8], destination: &Path) -> Result<Vec<PathBuf>> {
    let destination = gb_base::shared_writes::resolved(destination)?;
    let mut archive = zip::ZipArchive::new(Cursor::new(content))?;
    // ZipArchive indexes by name and silently replaces duplicate entries.
    // Count the original central headers before relying on that index. All
    // metadata and decompression still go through the ZIP library.
    let mut cursor = usize::try_from(archive.central_directory_start())?;
    let mut entries = 0;
    while content.get(cursor..cursor.saturating_add(4)) == Some(b"PK\x01\x02") {
        let header = content
            .get(cursor..cursor.saturating_add(46))
            .ok_or_else(|| anyhow::anyhow!("cli.member.artifact_path"))?;
        let extra: usize = [28, 30, 32]
            .into_iter()
            .map(|i| u16::from_le_bytes([header[i], header[i + 1]]) as usize)
            .sum();
        cursor = cursor
            .checked_add(46 + extra)
            .filter(|n| *n <= content.len())
            .ok_or_else(|| anyhow::anyhow!("cli.member.artifact_path"))?;
        entries += 1;
    }
    ensure!(entries == archive.len(), "cli.member.artifact_duplicate");
    let mut targets = Vec::new();
    let mut seen = BTreeSet::new();
    let mut size = 0u64;
    for i in 0..archive.len() {
        let item = archive.by_index(i)?;
        size = size
            .checked_add(item.size())
            .ok_or_else(|| anyhow::anyhow!("cli.member.artifact_too_large"))?;
        ensure!(size <= MAX_ARTIFACT_BYTES, "cli.member.artifact_too_large");
        let name = item.name().replace('\\', "/");
        let path = Path::new(&name);
        ensure!(
            !path.is_absolute()
                && path
                    .components()
                    .any(|c| matches!(c, std::path::Component::Normal(_)))
                && path.components().all(|c| matches!(
                    c,
                    std::path::Component::Normal(_) | std::path::Component::CurDir
                ))
                && !name.contains(':')
                && !name.contains('\0'),
            "cli.member.artifact_path"
        );
        ensure!(
            item.unix_mode().is_none_or(|m| m & 0o170000 != 0o120000),
            "cli.member.artifact_symlink"
        );
        let target = destination.join(path);
        ensure!(
            gb_base::shared_writes::resolved(&target)?.starts_with(&destination),
            "cli.member.artifact_path"
        );
        ensure!(seen.insert(target.clone()), "cli.member.artifact_duplicate");
        ensure!(
            item.is_dir() || !target.exists(),
            "cli.member.artifact_exists"
        );
        targets.push((i, target, item.is_dir()));
    }
    fs::create_dir_all(&destination)?;
    let mut files = Vec::new();
    let mut actual = 0u64;
    for (i, target, directory) in targets {
        if directory {
            fs::create_dir_all(target)?;
            continue;
        }
        fs::create_dir_all(target.parent().unwrap())?;
        let mut item = archive.by_index(i)?.take(MAX_ARTIFACT_BYTES + 1 - actual);
        let mut data = Vec::new();
        item.read_to_end(&mut data)?;
        actual += data.len() as u64;
        ensure!(
            actual <= MAX_ARTIFACT_BYTES,
            "cli.member.artifact_too_large"
        );
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)?;
        output.write_all(&data)?;
        files.push(target);
    }
    Ok(files)
}
