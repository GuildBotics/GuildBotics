//! Runs at the destination boundary, never on a host with guest-owned paths.
use anyhow::{Result, ensure};
use std::{
    collections::BTreeSet,
    fs,
    io::{Cursor, Read, Write},
    path::{Path, PathBuf},
};
fn diagnostic(
    key: &'static str,
    args: impl IntoIterator<Item = (&'static str, String)>,
) -> anyhow::Error {
    crate::MemberError::CapabilityArgs {
        key,
        args: args.into_iter().map(|(k, v)| (k.into(), v)).collect(),
    }
    .into()
}
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
        .open(&target)
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::AlreadyExists {
                diagnostic(
                    "cli.member.attachment_exists",
                    [("path", target.display().to_string())],
                )
            } else {
                e.into()
            }
        })?;
    file.write_all(content)?;
    Ok(target)
}
pub fn extract(content: &[u8], destination: &Path) -> Result<Vec<PathBuf>> {
    let destination = gb_base::shared_writes::resolved(destination)?;
    let mut archive = zip::ZipArchive::new(Cursor::new(content))
        .map_err(|_| diagnostic("cli.member.artifact_invalid", []))?;
    // ZipArchive indexes by name and silently replaces duplicate entries.
    // Count the original central headers before relying on that index. All
    // metadata and decompression still go through the ZIP library.
    let mut cursor = usize::try_from(archive.central_directory_start())?;
    let mut entries = 0;
    let mut raw_names = BTreeSet::new();
    while content.get(cursor..cursor.saturating_add(4)) == Some(b"PK\x01\x02") {
        let header = content
            .get(cursor..cursor.saturating_add(46))
            .ok_or_else(|| diagnostic("cli.member.artifact_invalid", []))?;
        let length = u16::from_le_bytes([header[28], header[29]]) as usize;
        let raw = content
            .get(cursor + 46..cursor + 46 + length)
            .ok_or_else(|| diagnostic("cli.member.artifact_invalid", []))?;
        ensure!(
            raw_names.insert(raw.to_vec()),
            diagnostic(
                "cli.member.artifact_duplicate",
                [("path", String::from_utf8_lossy(raw).into_owned())]
            )
        );
        let extra: usize = [28, 30, 32]
            .into_iter()
            .map(|i| u16::from_le_bytes([header[i], header[i + 1]]) as usize)
            .sum();
        cursor = cursor
            .checked_add(46 + extra)
            .filter(|n| *n <= content.len())
            .ok_or_else(|| diagnostic("cli.member.artifact_invalid", []))?;
        entries += 1;
    }
    ensure!(
        entries == archive.len(),
        diagnostic(
            "cli.member.artifact_duplicate",
            [(
                "path",
                raw_names
                    .iter()
                    .map(|n| String::from_utf8_lossy(n))
                    .collect::<Vec<_>>()
                    .join(", ")
            )]
        )
    );
    let mut targets = Vec::new();
    let mut seen = BTreeSet::new();
    let mut size = 0u64;
    for i in 0..archive.len() {
        let item = archive
            .by_index(i)
            .map_err(|_| diagnostic("cli.member.artifact_invalid", []))?;
        size = size
            .checked_add(item.size())
            .ok_or_else(|| anyhow::anyhow!("cli.member.artifact_too_large"))?;
        ensure!(
            size <= MAX_ARTIFACT_BYTES,
            diagnostic(
                "cli.member.artifact_too_large",
                [
                    ("size", size.to_string()),
                    ("limit", MAX_ARTIFACT_BYTES.to_string())
                ]
            )
        );
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
                && !(cfg!(windows) && name.contains(':'))
                && !name.contains('\0'),
            diagnostic("cli.member.artifact_path", [("path", item.name().into())])
        );
        ensure!(
            item.unix_mode().is_none_or(|m| m & 0o170000 != 0o120000),
            diagnostic(
                "cli.member.artifact_symlink",
                [("path", item.name().into())]
            )
        );
        let target = destination.join(path);
        ensure!(
            gb_base::shared_writes::resolved(&target)?.starts_with(&destination),
            diagnostic("cli.member.artifact_path", [("path", item.name().into())])
        );
        ensure!(
            seen.insert(target.clone()),
            diagnostic(
                "cli.member.artifact_duplicate",
                [("path", target.display().to_string())]
            )
        );
        ensure!(
            item.is_dir() || !target.exists(),
            diagnostic(
                "cli.member.artifact_exists",
                [("path", target.display().to_string())]
            )
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
        let mut item = archive
            .by_index(i)
            .map_err(|_| diagnostic("cli.member.artifact_invalid", []))?
            .take(MAX_ARTIFACT_BYTES + 1 - actual);
        let mut data = Vec::new();
        item.read_to_end(&mut data)
            .map_err(|_| diagnostic("cli.member.artifact_invalid", []))?;
        actual += data.len() as u64;
        ensure!(
            actual <= MAX_ARTIFACT_BYTES,
            diagnostic(
                "cli.member.artifact_too_large",
                [
                    ("size", actual.to_string()),
                    ("limit", MAX_ARTIFACT_BYTES.to_string())
                ]
            )
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
