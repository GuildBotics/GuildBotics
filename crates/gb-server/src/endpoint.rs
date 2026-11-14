//! Discover the server without starting it.
use crate::i18n;
use gb_base::files::FileLock;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
/// Names the port the server listens on.
pub const PORT_ENV: &str = "GUILDBOTICS_SERVER_PORT";
pub(crate) const LOCK: &str = "run/app-api.lock";
pub(crate) const DEFAULT_PORT: u16 = 7878;
/// The line on stdout that tells a launcher the server listens on the port.
pub const PORT_NOTICE_PREFIX: &str = "GUILDBOTICS_APP_API_PORT=";

/// The port as this machine names it.
pub fn port() -> anyhow::Result<u16> {
    parse_port(&std::env::var(PORT_ENV).unwrap_or_default())
}

pub(crate) fn parse_port(raw: &str) -> anyhow::Result<u16> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(DEFAULT_PORT);
    }
    match raw.parse::<u16>() {
        Ok(port) if port > 0 && raw.bytes().all(|b| b.is_ascii_digit()) => Ok(port),
        _ => anyhow::bail!("{PORT_ENV} must name a TCP port, not {raw:?}."),
    }
}

#[derive(Debug, serde::Deserialize, PartialEq, Eq)]
pub struct RunningServer {
    pub pid: u32,
    pub port: u16,
}

/// Reads the holder of the machine's lock. A stale record on an unlocked
/// file is not a running server.
pub fn running(data: &Path, language: &str) -> anyhow::Result<Option<RunningServer>> {
    let mut file = match std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(data.join(LOCK))
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if FileLock::try_file(file.try_clone()?)?.is_some() {
        return Ok(None);
    }
    file.seek(SeekFrom::Start(1))?;
    let mut record = Vec::new();
    file.take(4096).read_to_end(&mut record)?;
    let owner: RunningServer = serde_json::from_slice(&record).map_err(|_| {
        anyhow::anyhow!(i18n::t(
            language,
            "runtime.service_lock.invalid_metadata",
            &[]
        ))
    })?;
    anyhow::ensure!(
        owner.pid != 0 && owner.port != 0,
        i18n::t(language, "runtime.service_lock.invalid_metadata", &[])
    );
    Ok(Some(owner))
}
