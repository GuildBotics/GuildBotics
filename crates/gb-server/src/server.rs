//! The server's process: one server per machine, on the port every program
//! of the machine names, telling whoever started it that it listens.

use std::future::Future;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use gb_base::files::FileLock;

use crate::api::{self, Api, Selected};
use crate::auth::ServerAuth;
use crate::i18n;

/// Names the port the server listens on.
pub const PORT_ENV: &str = "GUILDBOTICS_SERVER_PORT";
const LOCK: &str = "run/app-api.lock";
const DEFAULT_PORT: u16 = 7878;
/// The line on stdout that tells a launcher the server listens on the port.
pub const PORT_NOTICE_PREFIX: &str = "GUILDBOTICS_APP_API_PORT=";

/// The port as this machine names it.
pub fn port() -> anyhow::Result<u16> {
    parse_port(&std::env::var(PORT_ENV).unwrap_or_default())
}

fn parse_port(raw: &str) -> anyhow::Result<u16> {
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
        let Some(lock) = FileLock::try_hold(&data.join(LOCK))? else {
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

/// Remote stop requests take the same drain and cancellation paths as signals.
#[derive(Clone, Default)]
pub struct Shutdown {
    stop: tokio_util::sync::CancellationToken,
    cancel: tokio_util::sync::CancellationToken,
}

impl Shutdown {
    pub fn request(&self, force: bool) {
        if force {
            self.cancel.cancel();
        }
        self.stop.cancel();
    }

    pub async fn wait(&self) {
        self.stop.cancelled().await;
    }

    pub async fn wait_for_cancel(&self) {
        self.cancel.cancelled().await;
    }
}

#[derive(serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
#[derive(Default)]
pub(crate) struct ShutdownRequest {
    force: bool,
}

pub(crate) async fn shutdown(
    axum::extract::State(api): axum::extract::State<api::Shared>,
    api::Body(body): api::Body<ShutdownRequest>,
) -> (axum::http::StatusCode, axum::Json<serde_json::Value>) {
    api.shutdown.request(body.force);
    (
        axum::http::StatusCode::ACCEPTED,
        axum::Json(serde_json::json!({})),
    )
}

/// The server, holding the machine's lock and listening on its port.
pub struct Server {
    lock: ServerLock,
    listener: tokio::net::TcpListener,
    api: Arc<Api>,
    start_services: bool,
}

/// The machine's lock, which its holder keeps until everything the process
/// runs has stopped: whoever stops the server waits for it.
pub struct Held(#[allow(dead_code)] ServerLock);

impl Server {
    /// A server of `api` on a free loopback port, holding its data's lock.
    #[cfg(test)]
    pub(crate) async fn for_test(api: Arc<Api>) -> (Server, u16) {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let lock = ServerLock::hold(&api.data, port).unwrap();
        (
            Server {
                lock,
                listener,
                api,
                start_services: false,
            },
            port,
        )
    }

    /// Holds the machine's lock before binding or touching any records, binds
    /// the loopback port, keeps the owner token, and says that it listens.
    pub async fn start() -> anyhow::Result<Server> {
        Self::start_with_keychain(Arc::new(gb_config::secrets::SystemKeychain)).await
    }

    /// Starts the same HTTP server with a caller-owned credential vault.
    /// Browser integration harnesses use an ephemeral vault, never the device's.
    pub async fn start_with_keychain(
        keychain: Arc<dyn gb_config::secrets::Keychain>,
    ) -> anyhow::Result<Server> {
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
        let language = crate::client::language().await;
        let workspace = gb_workspace::selection::restore(&data, |name| std::env::var_os(name))
            .unwrap_or_else(|e| {
                let error = crate::workspace::registry_error(e, language);
                tracing::error!("{error:#}");
                None
            });
        let mut api = Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&data.join("command_inputs")),
            auth,
            loopback_auth::hex(&loopback_auth::random_bytes::<16>()),
            data,
        );
        api.keychain = keychain;
        let api = Arc::new(api);
        api.select(Selected::open(workspace, &api, None));
        if let Some(workspace) = &api.selected().workspace {
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
            start_services: false,
        })
    }
    pub fn start_services(&mut self, start: bool) {
        self.start_services = start;
    }

    pub fn shutdown_requests(&self) -> Shutdown {
        self.api.shutdown.clone()
    }

    pub fn attach_branvil(&mut self, branvil: branvil::api::DataApi) -> anyhow::Result<()> {
        let workspace = self.api.selected().workspace.clone();
        self.api
            .branvil
            .set(branvil.clone())
            .map_err(|_| anyhow::anyhow!("Branvil already attached"))?;
        self.api
            .select(Selected::open(workspace, &self.api, Some(branvil)));
        Ok(())
    }

    /// Serves until `stop`, then waits for the work it started, and hands
    /// back the machine's lock with how serving ended. `cancel` cancels that
    /// work instead of waiting for it.
    pub async fn serve(
        self,
        stop: impl Future<Output = ()> + Send + 'static,
        cancel: impl Future<Output = ()> + Send + 'static,
    ) -> (Held, anyhow::Result<()>) {
        self.serve_inner(stop, cancel, None).await
    }

    pub fn base_url(&self) -> String {
        self.api.auth.base_url().to_string()
    }

    /// Keep the shared listener alive through work and CI drain.
    pub async fn serve_embedded(
        self,
        stop: impl Future<Output = ()> + Send + 'static,
        cancel: impl Future<Output = ()> + Send + 'static,
        prepare: impl Future<Output = anyhow::Result<branvil::Embedded>> + Send + 'static,
    ) -> (Held, anyhow::Result<()>) {
        self.serve_inner(stop, cancel, Some(Box::pin(prepare)))
            .await
    }

    async fn serve_inner(
        self,
        stop: impl Future<Output = ()> + Send + 'static,
        cancel: impl Future<Output = ()> + Send + 'static,
        prepare: Option<
            std::pin::Pin<Box<dyn Future<Output = anyhow::Result<branvil::Embedded>> + Send>>,
        >,
    ) -> (Held, anyhow::Result<()>) {
        use axum::{Router, http::StatusCode, middleware};
        use tower::ServiceExt;
        let accepting = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let (ready_tx, ready_rx) = tokio::sync::watch::channel(prepare.is_none());
        let host_router = Arc::new(std::sync::RwLock::new(None::<Router>));
        let host = prepare.as_ref().map(|_| {
            let host_router = host_router.clone();
            Router::new().fallback(move |request: axum::extract::Request| {
                let router = host_router.read().unwrap().clone();
                async move {
                    use axum::response::IntoResponse;
                    match router {
                        Some(router) => router.oneshot(request).await.unwrap(),
                        None => StatusCode::SERVICE_UNAVAILABLE.into_response(),
                    }
                }
            })
        });
        // Publish the embedded provider as one workspace transition. HTTP stays
        // available, but no operation may capture the old selection meanwhile.
        let preparing = if prepare.is_some() {
            Some(self.api.switching.clone().write_owned().await)
        } else {
            None
        };
        let startup_api = self.api.clone();
        let mut startup = tokio::spawn(async move {
            let _preparing = preparing;
            let Some(prepare) = prepare else {
                return Ok(None);
            };
            let embedded = prepare.await?;
            let data_api = embedded.data_api();
            startup_api
                .branvil
                .set(data_api.clone())
                .map_err(|_| anyhow::anyhow!("Branvil already attached"))?;
            let workspace = startup_api.selected().workspace.clone();
            startup_api.select(Selected::open(workspace, &startup_api, Some(data_api)));
            *host_router.write().unwrap() = Some(embedded.router());
            ready_tx.send_replace(true);
            tracing::info!("Branvil ready on the shared listener");
            Ok::<_, anyhow::Error>(Some(embedded))
        });
        let (work, cancelled) = (self.api.work.clone(), self.api.cancelled.clone());
        let autostart_api = self.api.clone();
        let start_services = self.start_services;
        let mut ready_rx = ready_rx;
        let autostart = tokio::spawn(async move {
            if ready_rx.wait_for(|ready| *ready).await.is_err() {
                return;
            }
            // Waits out a switch, then starts the service of what is selected.
            let _admitted = autostart_api.switching.clone().read_owned().await;
            let selected = autostart_api.selected();
            if let Some(workspace) = &selected.workspace {
                match crate::service_settings::Settings::read(workspace) {
                    Ok(settings) if start_services || settings.start_with_server => {
                        if let Err(error) = selected.service.start().await {
                            tracing::error!("cannot automatically start service: {error:#}");
                        }
                    }
                    Err(error) => tracing::error!("cannot read service settings: {error:#}"),
                    _ => (),
                }
            }
        });
        let stop_autostart = autostart.abort_handle();
        let cancel_autostart = autostart.abort_handle();
        let service = self.api.clone();
        let cancelling_service = service.clone();
        let cancel_request = service.shutdown.clone();
        let stop_request = service.shutdown.clone();
        let cancelling = tokio::spawn(async move {
            tokio::select! {
                () = cancel => {},
                () = cancel_request.wait_for_cancel() => {},
            }
            cancel_autostart.abort();
            cancelled.cancel();
            cancelling_service.selected().service.stop(true).await;
        });
        let maintenance = tokio::spawn(crate::diagnostics::maintenance(self.api.clone()));
        let upkeep = tokio::spawn(crate::environment::upkeep(self.api.clone()));
        let service_api = self.api.clone();
        let integrated = host.is_some();
        let gate = accepting.clone();
        let router = api::router_with_host(self.api, host).layer(middleware::from_fn(
            move |request: axum::extract::Request, next: middleware::Next| {
                let guildbotics =
                    !integrated || request.uri().path().starts_with("/-/guildbotics/");
                let refuse = guildbotics
                    && request.uri().path() != "/-/guildbotics/api/shutdown"
                    && !gate.load(std::sync::atomic::Ordering::Acquire);
                async move {
                    use axum::response::IntoResponse;
                    if refuse {
                        StatusCode::SERVICE_UNAVAILABLE.into_response()
                    } else {
                        next.run(request).await
                    }
                }
            },
        ));
        let (network_stop, network_stopped) = tokio::sync::oneshot::channel();
        let mut network = tokio::spawn(async move {
            axum::serve(self.listener, router)
                .with_graceful_shutdown(async {
                    let _ = network_stopped.await;
                })
                .await
        });
        tokio::pin!(stop);
        let mut startup_consumed = false;
        let mut embedded = None;
        let mut failure = None;
        tokio::select! {
            () = &mut stop => {},
            () = stop_request.wait() => {},
            result = &mut startup => {
                startup_consumed = true;
                match result {
                    Ok(Ok(service)) => embedded = service,
                    Ok(Err(error)) => failure = Some(error),
                    Err(error) => failure = Some(error.into()),
                }
                if failure.is_none() {
                    tokio::select! {
                        () = stop_request.wait() => {},
                        () = &mut stop => {},
                        result = &mut network => { failure = Some(anyhow::anyhow!("HTTP listener ended: {result:?}")); },
                    }
                }
            },
        }
        accepting.store(false, std::sync::atomic::Ordering::Release);
        stop_autostart.abort();
        service.selected().service.stop(false).await;
        if !startup_consumed {
            if !startup.is_finished() {
                startup.abort();
            }
            if let Ok(Ok(service)) = startup.await {
                embedded = service;
            }
        }
        autostart.abort();
        let _ = autostart.await;
        maintenance.abort();
        let _ = maintenance.await;
        upkeep.abort();
        let _ = upkeep.await;
        work.close();
        work.wait().await;
        // A start the server's work finished after the stop above began after
        // the stop was asked for, so what it runs is cancelled rather than
        // waited for; only then is the last selection's transcript finished.
        service_api.selected().service.stop(true).await;
        crate::tool_login::drain_private().await;
        gb_environment::worktree::drain_writebacks().await;
        gb_environment::snapshot::drain_builds().await;
        gb_environment::runtime::drain_vms().await;
        if let Some(embedded) = embedded {
            tokio::select! {
                result = embedded.drain() => { if let Err(error) = result { failure = Some(error); } },
                () = service_api.cancelled.cancelled() => {},
            }
            embedded.close_events();
            let _ = network_stop.send(());
            if !network.is_finished()
                && tokio::time::timeout(std::time::Duration::from_secs(5), &mut network)
                    .await
                    .is_err()
            {
                network.abort();
                let _ = network.await;
            }
            embedded.close().await;
        } else {
            let _ = network_stop.send(());
            if !network.is_finished() {
                let _ = network.await;
            }
        }
        cancelling.abort();
        let diagnostics = service_api
            .selected()
            .workspace
            .as_deref()
            .map(gb_observability::Diagnostics::open);
        if let Some(diagnostics) = diagnostics {
            match tokio::task::spawn_blocking(move || diagnostics.finish_system_session()).await {
                Ok(Ok(())) => (),
                result => tracing::warn!("cannot finish system transcript: {result:?}"),
            }
        }
        (Held(self.lock), failure.map_or(Ok(()), Err))
    }
}

impl Selected {
    /// What the server builds from `workspace`: its member backend and its
    /// service. Either that cannot be built is left out, and the screens
    /// stay to repair or replace the workspace.
    pub(crate) fn open(
        workspace: Option<PathBuf>,
        api: &Api,
        branvil: Option<branvil::api::DataApi>,
    ) -> Selected {
        let Some(workspace) = workspace else {
            return Selected::default();
        };
        let mut service = service_controller(Some(&workspace), &api.data, api.keychain.clone());
        if let Some(branvil) = &branvil {
            service.attach_branvil(branvil.clone());
        }
        Selected {
            member: member_backend(
                &workspace,
                &api.data,
                &api.work,
                branvil,
                api.keychain.clone(),
            ),
            service,
            workspace: Some(workspace),
        }
    }
}

/// Keep workspace recovery screens available when member assembly fails.
pub(crate) fn service_controller(
    workspace: Option<&Path>,
    data: &Path,
    keychain: Arc<dyn gb_config::secrets::Keychain>,
) -> crate::service_runtime::Controller {
    let built = (|| -> anyhow::Result<_> {
        let workspace = workspace.ok_or_else(|| anyhow::anyhow!("workspace not selected"))?;
        let mut native = crate::member::native::Native::new(workspace.into(), data.into())?;
        native.keychain = keychain;
        let host = Arc::new(gb_environment::Host {
            data: data.into(),
            home: std::env::var_os("HOME")
                .map(std::path::PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?,
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        Ok(crate::service_runtime::Controller::new(native, host))
    })();
    built.unwrap_or_default()
}

pub(crate) fn member_backend(
    workspace: &Path,
    data: &Path,
    work: &tokio_util::task::TaskTracker,
    branvil: Option<branvil::api::DataApi>,
    keychain: Arc<dyn gb_config::secrets::Keychain>,
) -> Option<Arc<crate::member::Backend>> {
    let built = (|| -> anyhow::Result<_> {
        let mut native = crate::member::native::Native::new(workspace.into(), data.into())?;
        native.keychain = keychain;
        native.runtime_work = Some(work.clone());
        native.branvil = branvil;
        native.backend().map(Some)
    })();
    built.unwrap_or_else(|_| {
        tracing::error!("cannot initialize the member backend for the selected workspace");
        None
    })
}

#[cfg(test)]
mod tests {
    use std::fs::File;
    use std::io::Read;

    use super::*;

    /// The record of the lock at `path`: its holder's PID and port.
    fn read_lock(path: &Path) -> Option<(u32, u16)> {
        let mut text = String::new();
        let mut file = File::open(path).ok()?;
        file.seek(SeekFrom::Start(1)).ok()?;
        file.read_to_string(&mut text).ok()?;
        let record: serde_json::Value = serde_json::from_str(&text).ok()?;
        Some((
            u32::try_from(record["pid"].as_u64()?).ok()?,
            u16::try_from(record["port"].as_u64()?).ok()?,
        ))
    }

    #[test]
    fn port_settings_allow_only_decimal_tcp_ports() {
        assert_eq!(parse_port("").unwrap(), 7878);
        for (raw, expected) in [
            ("", DEFAULT_PORT),
            ("  ", DEFAULT_PORT),
            (" 008765 ", 8765),
            ("65535", 65535),
        ] {
            assert_eq!(parse_port(raw).unwrap(), expected);
        }
        for raw in ["0", "-1", "+42", "65536", "localhost", "42.5", "１２"] {
            assert!(
                parse_port(raw).unwrap_err().to_string().contains(PORT_ENV),
                "{raw}"
            );
        }
    }

    #[test]
    fn the_lock_names_its_holder_and_refuses_a_second_one() {
        let data = tempfile::tempdir().unwrap();
        let held = ServerLock::hold(data.path(), 8765).unwrap();
        let path = data.path().join("run/app-api.lock");
        assert_eq!(read_lock(&path), Some((std::process::id(), 8765)));
        let mut owner = String::new();
        let mut file = File::open(&path).unwrap();
        file.seek(SeekFrom::Start(1)).unwrap();
        file.read_to_string(&mut owner).unwrap();
        assert!(owner.ends_with('\n'), "{owner:?}");
        let refused = ServerLock::hold(data.path(), 8766).err().unwrap();
        assert_eq!(
            refused.to_string(),
            "Another GuildBotics server is already running."
        );
        drop(held);
        ServerLock::hold(data.path(), 8766).unwrap();
        assert_eq!(read_lock(&path), Some((std::process::id(), 8766)));
    }
    #[tokio::test]
    async fn embedded_routes_are_unavailable_until_preparation_finishes() {
        let root = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let api = Arc::new(Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), port).unwrap(),
            "fixture".into(),
            root.path().into(),
        ));
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let server = Server {
            lock: ServerLock::hold(root.path(), port).unwrap(),
            listener,
            api: api.clone(),
            start_services: false,
        };
        let (finish, prepared) = tokio::sync::oneshot::channel();
        let serving = tokio::spawn(server.serve_embedded(
            std::future::pending(),
            std::future::pending(),
            async {
                prepared.await.unwrap();
                anyhow::bail!("fixture preparation failed")
            },
        ));
        let http = reqwest::Client::builder()
            .no_proxy()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .unwrap();
        for path in [
            "/",
            "/api/v1/health",
            "/git/acme/demo.git/info/refs",
            "/api/v1/runner/claim",
        ] {
            let response = http
                .get(format!("http://127.0.0.1:{port}{path}"))
                .send()
                .await
                .unwrap();
            assert_eq!(
                response.status(),
                reqwest::StatusCode::SERVICE_UNAVAILABLE,
                "{path}"
            );
        }
        assert!(
            api.admit().is_none(),
            "preparation must exclude every admitted operation"
        );
        assert!(
            api.switching.clone().try_write_owned().is_err(),
            "preparation must exclude workspace replacement"
        );
        let start = http
            .post(format!(
                "http://127.0.0.1:{port}/-/guildbotics/api/scheduler/start"
            ))
            .bearer_auth(token.trim())
            .send()
            .await
            .unwrap();
        assert_eq!(start.status(), reqwest::StatusCode::CONFLICT);
        let body: serde_json::Value = start.json().await.unwrap();
        assert_eq!(body["code"], "command_workspace_changing");
        let selection = http
            .post(format!(
                "http://127.0.0.1:{port}/-/guildbotics/api/workspace"
            ))
            .bearer_auth(token.trim())
            .json(&serde_json::json!({"workspace_dir": root.path()}))
            .send()
            .await
            .unwrap();
        assert_eq!(selection.status(), reqwest::StatusCode::CONFLICT);
        let body: serde_json::Value = selection.json().await.unwrap();
        assert_eq!(body["code"], "workspace_switch_blocked_by_active_work");
        let response = http
            .get(format!("http://127.0.0.1:{port}/-/guildbotics/unknown"))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), reqwest::StatusCode::NOT_FOUND);
        finish.send(()).unwrap();
        let (_, result) = serving.await.unwrap();
        assert!(
            api.admit().is_some(),
            "failed preparation must release admission"
        );
        assert_eq!(
            result.unwrap_err().to_string(),
            "fixture preparation failed"
        );
    }

    #[tokio::test]
    async fn autostart_waits_for_service_without_blocking_http_readiness() {
        use tokio_util::sync::CancellationToken;
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        crate::service_settings::Settings::write(
            &crate::service_settings::Settings {
                start_with_server: true,
                ..Default::default()
            },
            &native.workspace,
        )
        .unwrap();
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let api = Arc::new(Api::new(
            Selected {
                workspace: Some(native.workspace.clone()),
                member: None,
                service: crate::service_runtime::Controller::new(native.clone(), host),
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(&native.data, port).unwrap(),
            "fixture".into(),
            native.data.clone(),
        ));
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let selected = api.selected();
        let assembly = selected.service.hold_start_for_test().await;
        let server = Server {
            lock: ServerLock::hold(&native.data, port).unwrap(),
            listener,
            api: api.clone(),
            start_services: false,
        };
        let stop = CancellationToken::new();
        let serving = tokio::spawn({
            let stop = stop.clone();
            async move {
                server
                    .serve(
                        async move { stop.cancelled().await },
                        std::future::pending(),
                    )
                    .await
            }
        });
        let response = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            reqwest::Client::new()
                .get(format!("http://127.0.0.1:{port}/-/guildbotics/api/health"))
                .bearer_auth(token.trim())
                .send(),
        )
        .await;
        stop.cancel();
        drop(assembly);
        let (_held, result) = serving.await.unwrap();
        result.unwrap();
        assert!(response.unwrap().unwrap().status().is_success());
        assert!(!selected.service.service.status().await.running);
    }

    #[tokio::test]
    async fn server_keeps_its_lock_through_drain_and_releases_it_after_forced_work_cancels() {
        use std::time::Duration;
        use tokio_util::sync::CancellationToken;

        for method in ["api-force", "interrupt-then-cancel", "immediate-cancel"] {
            let root = tempfile::tempdir().unwrap();
            let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
                .await
                .unwrap();
            let port = listener.local_addr().unwrap().port();
            let api = Arc::new(Api::new(
                Selected::default(),
                crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
                ServerAuth::open(root.path(), port).unwrap(),
                "fixture".into(),
                root.path().into(),
            ));
            let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
            let (started, entered) = tokio::sync::oneshot::channel();
            let (settled, cancelled) = tokio::sync::oneshot::channel();
            api.work.spawn({
                let signal = api.cancelled.clone();
                async move {
                    started.send(()).unwrap();
                    signal.cancelled().await;
                    settled.send(()).unwrap();
                }
            });
            let server = Server {
                lock: ServerLock::hold(root.path(), port).unwrap(),
                listener,
                api: api.clone(),
                start_services: false,
            };
            let stop = CancellationToken::new();
            let force = CancellationToken::new();
            let mut serving = tokio::spawn(server.serve(
                stop.clone().cancelled_owned(),
                force.clone().cancelled_owned(),
            ));
            let endpoint = format!("http://127.0.0.1:{port}/-/guildbotics/api");
            let http = reqwest::Client::builder()
                .no_proxy()
                .timeout(Duration::from_secs(5))
                .build()
                .unwrap();
            assert!(
                http.get(format!("{endpoint}/health"))
                    .bearer_auth(token.trim())
                    .send()
                    .await
                    .unwrap()
                    .status()
                    .is_success()
            );
            entered.await.unwrap();
            if method == "api-force" {
                let response = http
                    .post(format!("{endpoint}/shutdown"))
                    .bearer_auth(token.trim())
                    .json(&serde_json::json!({"force":true}))
                    .send()
                    .await
                    .unwrap();
                assert_eq!(response.status(), reqwest::StatusCode::ACCEPTED);
            } else {
                if method == "interrupt-then-cancel" {
                    stop.cancel();
                    assert!(
                        tokio::time::timeout(Duration::from_millis(100), &mut serving)
                            .await
                            .is_err()
                    );
                    assert!(!api.cancelled.is_cancelled());
                    assert!(running(root.path(), "en").unwrap().is_some());
                }
                force.cancel();
                stop.cancel();
            }
            tokio::time::timeout(Duration::from_secs(5), cancelled)
                .await
                .unwrap()
                .unwrap();
            let (held, result) = tokio::time::timeout(Duration::from_secs(5), serving)
                .await
                .unwrap()
                .unwrap();
            result.unwrap();
            assert!(running(root.path(), "en").unwrap().is_some());
            drop(held);
            // A parallel test may be forking a child that briefly inherits
            // the descriptor before exec closes it.
            let reopened = FileLock::hold(&root.path().join(LOCK), Duration::from_secs(1))
                .unwrap_or_else(|error| panic!("{method} did not release its lock: {error}"));
            drop(reopened);
        }
    }

    #[test]
    fn damaged_workspace_identity_disables_member_without_disabling_recovery() {
        let workspace = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let path = gb_workspace::identity::workspace_identity_path(workspace.path());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "not json").unwrap();
        let api = Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&data.path().join("inputs")),
            ServerAuth::open(data.path(), 8765).unwrap(),
            "fixture".into(),
            data.path().into(),
        );
        let selected = Selected::open(Some(workspace.path().into()), &api, None);
        assert!(selected.member.is_none());
        assert_eq!(selected.workspace.as_deref(), Some(workspace.path()));
        assert_eq!(std::fs::read_to_string(path).unwrap(), "not json");
    }
}
