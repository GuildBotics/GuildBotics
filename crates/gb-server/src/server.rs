//! The server's process: one server per machine, on the port every program
//! of the machine names, telling whoever started it that it listens.

use std::future::Future;
use std::io::{Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::Arc;

use gb_base::files::FileLock;

use crate::api::{self, Api};
use crate::auth::ServerAuth;
use crate::i18n;

/// Names the port the server listens on.
pub const PORT_ENV: &str = "GUILDBOTICS_SERVER_PORT";
const DEFAULT_PORT: u16 = 8765;
/// The line on stdout that tells a launcher the server listens on the port.
pub const PORT_NOTICE_PREFIX: &str = "GUILDBOTICS_APP_API_PORT=";

/// The port as this machine names it.
pub fn port() -> anyhow::Result<u16> {
    let raw = std::env::var(PORT_ENV).unwrap_or_default();
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(DEFAULT_PORT);
    }
    match raw.parse::<u16>() {
        Ok(port) if port > 0 && raw.bytes().all(|b| b.is_ascii_digit()) => Ok(port),
        _ => anyhow::bail!("{PORT_ENV} must name a TCP port, not {raw:?}."),
    }
}

/// The machine-wide ownership of the server: `<data>/run/app-api.lock`,
/// held for the server's whole life, naming its process and port so that
/// whoever stops it reaches it. Its record is what the Python version writes
/// after the byte it reserves for locking on Windows.
pub struct ServerLock(#[allow(dead_code)] FileLock);

impl ServerLock {
    pub fn hold(data: &Path, port: u16) -> anyhow::Result<ServerLock> {
        let Some(lock) = FileLock::try_hold(&data.join("run/app-api.lock"))? else {
            anyhow::bail!(i18n::t("en", "runtime.service_lock.already_running", &[]));
        };
        let record = serde_json::json!({"pid": std::process::id(), "port": port});
        let mut file = lock.file();
        file.set_len(0)?;
        file.seek(SeekFrom::Start(0))?;
        file.write_all(format!(" {record}\n").as_bytes())?;
        file.sync_all()?;
        Ok(ServerLock(lock))
    }
}

/// Tells the launcher, as the only line stdout ever carries, that this
/// process holds and listens on the port; afterwards stdout is stderr, so
/// nothing, not even a child process inheriting it, can write a second one.
fn announce(port: u16) -> std::io::Result<()> {
    let mut out = std::io::stdout().lock();
    // Starting on a line of its own, whatever was written before it.
    write!(out, "\n{PORT_NOTICE_PREFIX}{port}\n")?;
    out.flush()?;
    #[cfg(unix)]
    // SAFETY: both are the process's standard descriptors.
    if unsafe { libc::dup2(libc::STDERR_FILENO, libc::STDOUT_FILENO) } < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

/// The server, holding the machine's lock and listening on its port.
pub struct Server {
    lock: ServerLock,
    listener: tokio::net::TcpListener,
    api: Arc<Api>,
}

/// The machine's lock, which its holder keeps until everything the process
/// runs has stopped: whoever stops the server waits for it.
pub struct Held(#[allow(dead_code)] ServerLock);

impl Server {
    /// Holds the machine's lock before binding or touching any records, binds
    /// the loopback port, keeps the owner token, and says that it listens.
    pub async fn start() -> anyhow::Result<Server> {
        let port = port()?;
        let data = gb_base::home::data()?;
        let lock = ServerLock::hold(&data, port)?;
        // Only the process that holds the port touches the credentials of
        // the server on it, so a failed bind leaves a running server's
        // sessions alone.
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
        let auth = ServerAuth::open(&data, port)?;
        // Before the notice: the launcher reads the owner token once told.
        announce(port)?;
        // A workspace that cannot be opened leaves the server running for
        // the screens to choose another.
        let workspace = gb_workspace::selection::restore(&data, |name| std::env::var_os(name))
            .unwrap_or_else(|e| {
                tracing::error!("cannot open the selected workspace: {e:#}");
                None
            });
        let api = Arc::new(Api {
            inputs: crate::input_files::InputFileStore::new(&data.join("command_inputs"))?,
            auth,
            instance_id: loopback_auth::hex(&loopback_auth::random_bytes::<16>()),
            data,
            workspace,
            work: tokio_util::task::TaskTracker::new(),
            cancelled: tokio_util::sync::CancellationToken::new(),
        });
        if let Some(workspace) = &api.workspace {
            let diagnostics = gb_observability::Diagnostics::open(workspace);
            let instance = api.instance_id.clone();
            if let Err(e) =
                tokio::task::spawn_blocking(move || diagnostics.start_system_session(&instance))
                    .await?
            {
                tracing::warn!("cannot start system transcript: {e}");
            }
        }
        Ok(Server {
            lock,
            listener,
            api,
        })
    }

    /// Serves until `stop`, then waits for the work it started, and hands
    /// back the machine's lock with how serving ended. `cancel` cancels that
    /// work instead of waiting for it.
    pub async fn serve(
        self,
        stop: impl Future<Output = ()> + Send + 'static,
        cancel: impl Future<Output = ()> + Send + 'static,
    ) -> (Held, anyhow::Result<()>) {
        let (work, cancelled) = (self.api.work.clone(), self.api.cancelled.clone());
        let cancelling = tokio::spawn(async move {
            cancel.await;
            cancelled.cancel();
        });
        let diagnostics = self
            .api
            .workspace
            .as_ref()
            .map(|w| gb_observability::Diagnostics::open(w));
        let upkeep = tokio::spawn(crate::environment::upkeep(self.api.clone()));
        let served = axum::serve(self.listener, api::router(self.api))
            .with_graceful_shutdown(stop)
            .await;
        upkeep.abort();
        let _ = upkeep.await;
        work.close();
        work.wait().await;
        crate::tool_login::drain_private().await;
        cancelling.abort();
        if let Some(diagnostics) = diagnostics {
            match tokio::task::spawn_blocking(move || diagnostics.finish_system_session()).await {
                Ok(Ok(())) => (),
                result => tracing::warn!("cannot finish system transcript: {result:?}"),
            }
        }
        (Held(self.lock), served.map_err(Into::into))
    }
}

#[cfg(test)]
mod tests {
    use std::fs::File;
    use std::io::Read;

    use super::*;

    /// The record of the lock at `path`: its holder's PID and port.
    fn read_lock(path: &Path) -> Option<(u32, u16)> {
        let mut text = String::new();
        File::open(path).ok()?.read_to_string(&mut text).ok()?;
        let record: serde_json::Value = serde_json::from_str(text.get(1..)?).ok()?;
        Some((
            u32::try_from(record["pid"].as_u64()?).ok()?,
            u16::try_from(record["port"].as_u64()?).ok()?,
        ))
    }

    #[test]
    fn the_lock_names_its_holder_and_refuses_a_second_one() {
        let data = tempfile::tempdir().unwrap();
        let held = ServerLock::hold(data.path(), 8765).unwrap();
        let path = data.path().join("run/app-api.lock");
        assert_eq!(read_lock(&path), Some((std::process::id(), 8765)));
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.starts_with(' ') && text.ends_with('\n'), "{text:?}");
        let refused = ServerLock::hold(data.path(), 8766).err().unwrap();
        assert_eq!(
            refused.to_string(),
            "Another GuildBotics server is already running."
        );
        drop(held);
        ServerLock::hold(data.path(), 8766).unwrap();
        assert_eq!(read_lock(&path), Some((std::process::id(), 8766)));
    }
}
