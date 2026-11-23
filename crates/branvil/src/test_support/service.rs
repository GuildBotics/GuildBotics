//! The service as the server runs it, with a host of its own, for tests
//! without the GuildBotics server.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::Context;

use crate::api::DataApi;
use crate::config::Layout;
use crate::domain::members::{self, NewMember};
use crate::model::MemberKind;
use crate::service::{open_service, recover};
use crate::store::Store;

/// The owner's Bearer token the test host accepts.
pub const OWNER_TOKEN: &str = "test-owner-token";
/// The browser session the test host accepts: its cookie and CSRF value.
pub const SESSION_COOKIE: &str = "test_session=fixture";
pub const SESSION_CSRF: &str = "test-csrf";

/// Authenticates the owner as the GuildBotics server does, by a token and a
/// browser session of its own.
pub struct TestHost;

impl crate::host::Host for TestHost {
    fn check(&self, cookie: &str, csrf: Option<&str>) -> Result<String, crate::host::Denied> {
        if !cookie.split(';').any(|part| part.trim() == SESSION_COOKIE) {
            return Err(crate::host::Denied::Unauthorized);
        }
        if csrf.is_some_and(|sent| sent != SESSION_CSRF) {
            return Err(crate::host::Denied::CsrfMismatch);
        }
        Ok(SESSION_CSRF.into())
    }
    fn owner_token(&self, token: &str) -> bool {
        token == OWNER_TOKEN
    }
    fn entry_path(&self) -> &str {
        "/enter?next=%2Fworkspaces"
    }
    fn navigation(&self) -> crate::host::Navigation {
        crate::host::Navigation {
            label: "GuildBotics".into(),
            url: "/".into(),
        }
    }
}

/// Creates the data directory `data_dir` with `owner` as its owner, as the
/// server's first start does with its own name, and an empty configuration
/// for the tests to extend.
pub fn initialize(data_dir: &Path, owner: &str) -> anyhow::Result<()> {
    let layout = Layout::new(data_dir);
    layout.create()?;
    std::fs::write(layout.config_file(), "")?;
    let store = Store::open(&layout.db(), 1 << 20, 1)?;
    store.write_sync(|w| {
        members::insert_member(
            w,
            &NewMember {
                name: owner.into(),
                display_name: None,
                kind: MemberKind::Owner,
            },
        )
    })?;
    Ok(())
}

/// Keeps values in plain files, one per name, so that they outlive a test
/// server's process.
pub struct FileVault(pub PathBuf);

impl crate::vault::Vault for FileVault {
    fn get(&self, name: &str) -> anyhow::Result<Option<String>> {
        match std::fs::read_to_string(self.0.join(name)) {
            Ok(value) => Ok(Some(value)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }
    fn set(&self, name: &str, value: &str) -> anyhow::Result<()> {
        std::fs::create_dir_all(&self.0)?;
        Ok(std::fs::write(self.0.join(name), value)?)
    }
    fn delete(&self, name: &str) -> anyhow::Result<()> {
        match std::fs::remove_file(self.0.join(name)) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
            _ => Ok(()),
        }
    }
}

/// The service on its own listener in this process. Data operations use
/// `data` directly; its URL serves Smart HTTP for Git and hook callbacks.
pub struct Running {
    pub data: DataApi,
    stop: Option<tokio::sync::oneshot::Sender<()>>,
    task: Option<tokio::task::JoinHandle<anyhow::Result<u8>>>,
}

impl Running {
    pub async fn start(
        data_dir: &Path,
        vault: Arc<dyn crate::vault::Vault>,
    ) -> anyhow::Result<Self> {
        let (stop, stopped) = tokio::sync::oneshot::channel();
        let (ready, started) = tokio::sync::oneshot::channel();
        let data_dir = data_dir.to_path_buf();
        let mut task = tokio::spawn(async move {
            serve(
                &data_dir,
                "127.0.0.1:0",
                vault,
                async {
                    let _ = stopped.await;
                },
                std::future::pending(),
                false,
                Some(ready),
            )
            .await
        });
        let data = tokio::select! {
            result = started => result.map_err(|_| anyhow::anyhow!("integrations.branvil.api.start_failed"))?,
            result = &mut task => { result??; anyhow::bail!("integrations.branvil.api.start_failed"); }
        };
        Ok(Self {
            data,
            stop: Some(stop),
            task: Some(task),
        })
    }

    pub async fn stop(mut self) -> anyhow::Result<u8> {
        self.stop.take();
        self.task.take().expect("running service task").await?
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        self.stop.take();
    }
}

/// Serves the data directory `data_dir` on `listen` with the test host, as
/// the server does, until `interrupt` or `terminate` stops it; the exit
/// status is 3 when the stop interrupted running CI jobs (`Ci::stop`). With
/// `announce`, it says on stdout when it listens and when it waits for
/// repository operations; otherwise only its log does.
pub async fn serve(
    data_dir: &Path,
    listen: &str,
    vault: Arc<dyn crate::vault::Vault>,
    interrupt: impl Future<Output = ()> + Send + 'static,
    terminate: impl Future<Output = ()> + Send + 'static,
    announce: bool,
    ready: Option<tokio::sync::oneshot::Sender<DataApi>>,
) -> anyhow::Result<u8> {
    let config = crate::service::load_config(data_dir)?;
    let (mut interrupt, mut terminate) = (Box::pin(interrupt), Box::pin(terminate));
    let store = tokio::select! {
        store = open_service(&config) => store?,
        _ = &mut interrupt => return Ok(0),
        _ = &mut terminate => return Ok(0),
    };
    let listener = tokio::net::TcpListener::bind(listen)
        .await
        .with_context(|| format!("cannot listen on {listen}"))?;
    let bound = listener.local_addr()?;
    let base_url = format!("http://{bound}");
    let prepared = recover(
        config,
        vault,
        base_url.clone(),
        base_url.clone(),
        store,
        None,
    )
    .await?;
    let _ = prepared.app.host.set(Arc::new(TestHost));
    let app = prepared.app.clone();
    let router = crate::http::router_with(app.clone(), super::routes::routes());
    if let Some(ready) = ready {
        let _ = ready.send(prepared.data_api());
    }
    if announce {
        println!("branvil listening on {base_url}");
        std::io::stdout().flush()?;
    } else {
        tracing::info!("listening on http://{bound} (base URL {base_url})");
    }
    // SIGTERM stops CI first (`Ci::stop`: no new jobs, the running ones
    // finish) while the server keeps serving the runners and everyone else;
    // SIGINT (Ctrl-C), also during that wait, skips it. Then the event streams
    // end and the server stops accepting; requests in flight get
    // `SHUTDOWN_GRACE` to finish, then the stop goes on without waiting for
    // them (their tasks live until the process exits). The repository
    // operations already started (`App::finish`), a dropped request's or a
    // background task's included, end first, each within its own limits; none
    // starts meanwhile. It exits with 3 when the stop interrupted jobs.
    let (stopping, mut stopped) = tokio::sync::watch::channel(None);
    let events = app.events.clone();
    let ci = app.ci.clone();
    let server = axum::serve(listener, router).with_graceful_shutdown(async move {
        tokio::pin!(interrupt);
        let mut cut = 0;
        tokio::select! {
            _ = &mut interrupt => {},
            _ = terminate => {
                tracing::info!("stopping: no new CI jobs; waiting for the running ones (SIGINT skips the wait)");
                tokio::select! {
                    _ = &mut interrupt => {},
                    stopped = ci.stop(announce) => match stopped {
                        Ok(n) => cut = n,
                        Err(e) => tracing::warn!("stopping: cannot wait for the running CI jobs: {e}"),
                    },
                }
            },
        }
        tracing::info!("shutting down");
        events.close();
        stopping.send_replace(Some(cut));
    });
    let served = tokio::select! {
        served = server => served,
        _ = async {
            let _ = stopped.wait_for(Option::is_some).await;
            tokio::time::sleep(SHUTDOWN_GRACE).await;
        } => {
            tracing::warn!(
                "requests still open {} s after the shutdown signal; exiting",
                SHUTDOWN_GRACE.as_secs()
            );
            Ok(())
        }
    };
    let cut = stopped.borrow().unwrap_or(0);
    let code = if cut > 0 { 3 } else { 0 };
    if cut > 0 {
        tracing::warn!(
            "stopped with {cut} CI job(s) still running after ci.stop_wait_secs: they were interrupted, and queued again in their place unless interrupted before"
        );
    }
    let close = app.close();
    tokio::pin!(close);
    if futures_util::poll!(close.as_mut()).is_pending() {
        if announce {
            let mut out = std::io::stdout().lock();
            let _ = writeln!(
                out,
                "branvil stopping: waiting for the started repository operations to finish"
            )
            .and_then(|_| out.flush());
        }
        tracing::info!("stopping: waiting for the started repository operations");
        close.await;
    }
    served?;
    Ok(code)
}

/// How long requests in flight may take after a shutdown signal.
const SHUTDOWN_GRACE: std::time::Duration = std::time::Duration::from_secs(5);
