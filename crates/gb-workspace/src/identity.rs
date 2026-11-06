//! The identities of a workspace and of this machine, in the files the Python
//! version keeps them in (`guildbotics/workspace/identity.py`): each is
//! generated once, on first use, and the one already on disk always wins.
//! Neither authenticates anything.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::Context as _;
use gb_base::files::{self, FileLock, SHARED_RECORD_SCHEMA_VERSION};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

const DEVICE_LOCK_TIMEOUT: Duration = Duration::from_secs(5);

fn current_schema() -> u64 {
    SHARED_RECORD_SCHEMA_VERSION
}

/// What names one workspace wherever it is (`<workspace>/.guildbotics/state/workspace.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceIdentity {
    #[serde(default = "current_schema")]
    pub schema_version: u64,
    pub workspace_id: String,
    pub created_at: String,
}

/// The operating systems a machine's identity names.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DeviceOs {
    Macos,
    Windows,
    Linux,
}

/// What names this machine (`<machine root>/data/device.json`, never shared).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceIdentity {
    #[serde(default = "current_schema")]
    pub schema_version: u64,
    pub device_id: String,
    pub display_name: String,
    pub os: DeviceOs,
}

/// A record checked as the Python version's model checks it.
trait Record: Serialize + DeserializeOwned {
    fn check(&self) -> anyhow::Result<()>;
}

fn check_schema(version: u64) -> anyhow::Result<()> {
    anyhow::ensure!(
        (1..=SHARED_RECORD_SCHEMA_VERSION).contains(&version),
        "schema_version must be between 1 and {SHARED_RECORD_SCHEMA_VERSION}, got {version}"
    );
    Ok(())
}

fn check_uuid(value: &str) -> anyhow::Result<()> {
    uuid::Uuid::parse_str(value).with_context(|| format!("{value:?} is not a UUID"))?;
    Ok(())
}

/// An ISO 8601 date, with or without a time and an offset, in the forms
/// Python's `datetime.fromisoformat` reads.
fn check_timestamp(value: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        iso_datetime(value.trim()),
        "{value:?} is not an ISO 8601 timestamp"
    );
    Ok(())
}

fn iso_datetime(value: &str) -> bool {
    // The extended date is 10 characters, the basic one 8, and any one
    // character separates the time.
    let split = [10, 8]
        .into_iter()
        .find(|&i| i < value.len() && value.is_char_boundary(i) && iso_date(&value[..i]));
    let (date, time) = match split {
        Some(i) => {
            let separator = value[i..].chars().next().expect("a character").len_utf8();
            (&value[..i], Some(&value[i + separator..]))
        }
        None => (value, None),
    };
    if !iso_date(date) {
        return false;
    }
    let Some(time) = time else {
        return true;
    };
    let (clock, offset) = match time.find(['+', '-', 'Z']) {
        Some(i) => time.split_at(i),
        None => (time, ""),
    };
    let clock_ok = ["%H:%M:%S%.f", "%H:%M", "%H%M%S%.f", "%H%M", "%H"]
        .iter()
        .any(|format| chrono::NaiveTime::parse_from_str(clock, format).is_ok());
    let offset_ok = match offset.as_bytes() {
        [] | [b'Z'] => true,
        [b'+' | b'-', rest @ ..] => {
            let digits: Vec<u8> = rest.iter().copied().filter(|b| *b != b':').collect();
            matches!(digits.len(), 2 | 4 | 6)
                && digits.iter().all(u8::is_ascii_digit)
                && (rest.len() == digits.len()
                    || rest.iter().skip(2).step_by(3).all(|b| *b == b':'))
        }
        _ => false,
    };
    clock_ok && offset_ok
}

fn iso_date(value: &str) -> bool {
    ["%Y-%m-%d", "%Y%m%d"]
        .iter()
        .any(|format| chrono::NaiveDate::parse_from_str(value, format).is_ok())
}

impl Record for WorkspaceIdentity {
    fn check(&self) -> anyhow::Result<()> {
        check_schema(self.schema_version)?;
        check_uuid(&self.workspace_id)?;
        check_timestamp(&self.created_at)
    }
}

impl Record for DeviceIdentity {
    fn check(&self) -> anyhow::Result<()> {
        check_schema(self.schema_version)?;
        check_uuid(&self.device_id)?;
        anyhow::ensure!(!self.display_name.is_empty(), "display_name is empty");
        Ok(())
    }
}

/// The record at `path`, or `None` before it is created; a record in another
/// shape is refused rather than read as this one.
fn read<R: Record>(path: &Path) -> anyhow::Result<Option<R>> {
    let Some(bytes) = files::read_if_present(path)? else {
        return Ok(None);
    };
    let record: R = serde_json::from_slice(&bytes)
        .with_context(|| format!("{} is not a valid record", path.display()))?;
    record
        .check()
        .with_context(|| format!("{} is not a valid record", path.display()))?;
    Ok(Some(record))
}

/// The record at `path`, created by `new` under `lock` if there is none. The
/// decision is made inside the span that writes, so two processes starting
/// together never return different identities.
fn ensure<R: Record>(
    path: &Path,
    lock: impl FnOnce() -> std::io::Result<FileLock>,
    new: impl FnOnce() -> R,
) -> anyhow::Result<R> {
    if let Some(existing) = read(path)? {
        return Ok(existing);
    }
    let _lock = lock()?;
    if let Some(existing) = read(path)? {
        return Ok(existing);
    }
    let record = new();
    files::atomic_write(path, files::dump_shared_json(&record).as_bytes())?;
    Ok(record)
}

fn now() -> String {
    chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S%.6fZ")
        .to_string()
}

/// A UUID version 7, so listings of records stay roughly chronological.
fn new_uuid7() -> String {
    uuid::Uuid::now_v7().to_string()
}

pub fn workspace_identity_path(workspace: &Path) -> PathBuf {
    workspace.join(".guildbotics/state/workspace.json")
}

/// Read an existing identity without modifying the workspace.
pub fn read_workspace_identity(workspace: &Path) -> anyhow::Result<Option<WorkspaceIdentity>> {
    read(&workspace_identity_path(workspace))
}

/// The workspace's identity, created on first use.
pub fn ensure_workspace_identity(workspace: &Path) -> anyhow::Result<WorkspaceIdentity> {
    let path = workspace_identity_path(workspace);
    let writes = gb_base::shared_writes::SharedWrites::new(
        gb_base::home::identity_root()?,
        Some(workspace.to_path_buf()),
    );
    writes.at(&path, |writer| {
        if let Some(existing) = read(&path)? {
            return Ok(existing);
        }
        let record = WorkspaceIdentity {
            schema_version: SHARED_RECORD_SCHEMA_VERSION,
            workspace_id: new_uuid7(),
            created_at: now(),
        };
        writer.write_json(&path, &record)?;
        Ok(record)
    })
}

/// This machine's identity (`data` is `<machine root>/data`), created on first
/// use and named after the host, so setup never has to ask for a name.
pub fn ensure_device_identity(data: &Path) -> anyhow::Result<DeviceIdentity> {
    ensure(
        &data.join("device.json"),
        || FileLock::hold(&data.join("run/device-identity.lock"), DEVICE_LOCK_TIMEOUT),
        || DeviceIdentity {
            schema_version: SHARED_RECORD_SCHEMA_VERSION,
            device_id: new_uuid7(),
            display_name: default_display_name(),
            os: current_os(),
        },
    )
}

fn current_os() -> DeviceOs {
    if cfg!(target_os = "macos") {
        DeviceOs::Macos
    } else if cfg!(windows) {
        DeviceOs::Windows
    } else {
        DeviceOs::Linux
    }
}

fn default_display_name() -> String {
    let host = host_name();
    match host.split('.').next() {
        Some(short) if !short.is_empty() => short.into(),
        _ => "GuildBotics device".into(),
    }
}

#[cfg(unix)]
fn host_name() -> String {
    let mut buffer = [0u8; 256];
    // SAFETY: the buffer is valid for its length, and gethostname writes at
    // most that many bytes.
    let written = unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) };
    if written != 0 {
        return String::new();
    }
    let end = buffer.iter().position(|b| *b == 0).unwrap_or(buffer.len());
    String::from_utf8_lossy(&buffer[..end]).into_owned()
}

#[cfg(not(unix))]
fn host_name() -> String {
    std::env::var("COMPUTERNAME").unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_workspace_identity_is_created_once_and_then_read() {
        let workspace = tempfile::tempdir().unwrap();
        let created = ensure_workspace_identity(workspace.path()).unwrap();
        assert_eq!(created.schema_version, SHARED_RECORD_SCHEMA_VERSION);
        assert_eq!(
            uuid::Uuid::parse_str(&created.workspace_id)
                .unwrap()
                .get_version_num(),
            7
        );
        check_timestamp(&created.created_at).unwrap();
        assert_eq!(
            ensure_workspace_identity(workspace.path()).unwrap(),
            created
        );
        let text = std::fs::read_to_string(workspace_identity_path(workspace.path())).unwrap();
        assert_eq!(
            text,
            format!(
                "{{\n  \"created_at\": \"{}\",\n  \"schema_version\": 4,\n  \"workspace_id\": \"{}\"\n}}\n",
                created.created_at, created.workspace_id
            )
        );
    }

    #[test]
    fn timestamps_are_read_in_the_forms_python_reads() {
        for read in [
            "2026-11-02T01:02:03.123456Z",
            "2026-11-02T01:02:03+09:00",
            "2026-11-02 01:02:03",
            "2026-11-02T01:02",
            "2026-11-02",
            "20261102T010203",
            "2026-11-02T01:02:03+0900",
            "2026-11-02T01:02:03-09",
            "2026-11-02X01:02",
            "2026-11-02T01:02:03+09:00:00",
        ] {
            assert!(iso_datetime(read), "{read}");
        }
        for refused in [
            "",
            "yesterday",
            "2026-13-02",
            "2026-11-02T25:00",
            "2026-11-02T01:02+9",
            "2026-11-02T01:02+09:0",
        ] {
            assert!(!iso_datetime(refused), "{refused}");
        }
    }

    #[test]
    fn processes_starting_together_get_the_same_identity() {
        let workspace = tempfile::tempdir().unwrap();
        let ids: Vec<_> = std::thread::scope(|scope| {
            let starts: Vec<_> = (0..8)
                .map(|_| scope.spawn(|| ensure_workspace_identity(workspace.path()).unwrap()))
                .collect();
            starts.into_iter().map(|s| s.join().unwrap()).collect()
        });
        assert!(ids.iter().all(|id| *id == ids[0]), "{ids:?}");
        assert_eq!(
            read::<WorkspaceIdentity>(&workspace_identity_path(workspace.path()))
                .unwrap()
                .as_ref(),
            Some(&ids[0])
        );
    }

    #[test]
    fn a_record_in_another_shape_is_refused_rather_than_replaced() {
        let workspace = tempfile::tempdir().unwrap();
        let path = workspace_identity_path(workspace.path());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let id = "01a0951c-4e6e-7747-a845-11a5ec9ff682";
        for refused in [
            format!(
                r#"{{"workspace_id": "{id}", "created_at": "2026-01-01T00:00:00Z", "extra": 1}}"#
            ),
            r#"{"workspace_id": "x", "created_at": "2026-01-01T00:00:00Z"}"#.to_string(),
            format!(r#"{{"workspace_id": "{id}", "created_at": "yesterday"}}"#),
            format!(
                r#"{{"schema_version": 5, "workspace_id": "{id}", "created_at": "2026-01-01T00:00:00Z"}}"#
            ),
            "not json".to_string(),
        ] {
            std::fs::write(&path, &refused).unwrap();
            assert!(
                ensure_workspace_identity(workspace.path()).is_err(),
                "{refused}"
            );
            assert_eq!(std::fs::read_to_string(&path).unwrap(), refused);
        }
        let older = format!(r#"{{"workspace_id": "{id}", "created_at": "2026-01-01T00:00:00"}}"#);
        std::fs::write(&path, older).unwrap();
        let read = ensure_workspace_identity(workspace.path()).unwrap();
        assert_eq!((read.schema_version, read.workspace_id.as_str()), (4, id));
    }

    #[test]
    fn the_machine_is_named_after_its_host_once() {
        let data = tempfile::tempdir().unwrap();
        let created = ensure_device_identity(data.path()).unwrap();
        let name = default_display_name();
        assert!(!name.is_empty() && !name.contains('.'), "{name}");
        assert_eq!(created.display_name, name);
        assert_eq!(created.os, current_os());
        let renamed = DeviceIdentity {
            display_name: "hub".into(),
            ..created
        };
        std::fs::write(
            data.path().join("device.json"),
            files::dump_shared_json(&renamed),
        )
        .unwrap();
        assert_eq!(ensure_device_identity(data.path()).unwrap(), renamed);
    }
}
