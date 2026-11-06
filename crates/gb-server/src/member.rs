//! Interactive member transport. A worker owns the invocation until it has
//! settled, even when the HTTP caller disconnects or its response times out.
pub mod native;
use crate::api::{ApiError, Shared};
use axum::{
    body::Body,
    extract::{Request, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
pub use gb_member::command::{grammar, usage};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    future::Future,
    path::{Path, PathBuf},
    pin::Pin,
    sync::Arc,
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const MAX_METADATA: usize = 4 << 20;
const MAX_BUNDLE: usize = 1 << 30;
#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Identity,
    Record,
    Known,
    Push,
    Tracking,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MemberRequest {
    pub version: String,
    pub workspace_id: String,
    pub arguments: Vec<String>,
    #[serde(default)]
    pub stdin: String,
    #[serde(default)]
    pub interactive_host: String,
    #[serde(default)]
    pub interactive_thread_key: String,
    pub phase: Option<Phase>,
    #[serde(default)]
    pub data: serde_json::Map<String, Value>,
}
#[derive(Default, Deserialize, Serialize)]
pub struct Result {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    #[serde(flatten)]
    pub metadata: serde_json::Map<String, Value>,
}
pub type Execution<'a> = Pin<Box<dyn Future<Output = Result> + Send + 'a>>;
/// The application supplies the member command grammar and service assembly.
/// Transport metadata can never assign a run, guest, lease, or identity.
pub trait Executor: Send + Sync {
    fn tracker(&self) -> Option<tokio_util::task::TaskTracker> {
        None
    }
    fn broker(
        &self,
        _: gb_environment::broker::Invocation,
        _: gb_environment::guest_process::Channel,
    ) -> Option<gb_environment::broker::Handler> {
        None
    }
    fn execute<'a>(
        &'a self,
        request: MemberRequest,
        describe: bool,
        incoming: Option<&'a Path>,
        outgoing: &'a Path,
    ) -> Execution<'a>;
}
pub struct Backend {
    pub version: String,
    pub workspace_id: String,
    pub executor: Arc<dyn Executor>,
    pub timeout: Duration,
}
impl MemberRequest {
    fn validate(
        &self,
        backend: &Backend,
        exchange: bool,
    ) -> std::result::Result<(), (StatusCode, &'static str)> {
        if self.version != backend.version {
            return Err((StatusCode::CONFLICT, "member_version_mismatch"));
        }
        if self.workspace_id != backend.workspace_id {
            return Err((StatusCode::CONFLICT, "member_workspace_mismatch"));
        }
        if self.phase.is_some() && !exchange {
            return Err((StatusCode::UNPROCESSABLE_ENTITY, "member_invalid_request"));
        }
        if self.arguments.len() > 128
            || self.arguments.iter().map(|v| v.len()).sum::<usize>() > 64 << 10
            || self.stdin.len() > 2 << 20
            || self.interactive_host.len() > 1024
            || self.interactive_thread_key.len() > 4096
        {
            return Err((StatusCode::PAYLOAD_TOO_LARGE, "member_request_too_large"));
        }
        if self.arguments.iter().any(|v| v.contains('\0')) {
            return Err((StatusCode::UNPROCESSABLE_ENTITY, "member_invalid_request"));
        }
        // Only root options are recognized here. Option values after a
        // subcommand belong to that command's grammar, including --workspace.
        let mut index = 0;
        while let Some(argument) = self.arguments.get(index) {
            if argument == "--workspace" || argument.starts_with("--workspace=") {
                return Err((
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "member_workspace_argument",
                ));
            }
            if !argument.starts_with('-') {
                break;
            }
            index += if argument == "--person" { 2 } else { 1 };
        }
        Ok(())
    }
}
pub(crate) async fn execute(
    State(api): State<Shared>,
    headers: HeaderMap,
    crate::api::Body(body): crate::api::Body<MemberRequest>,
) -> Response {
    start(api, headers, body, false, None).await
}
pub(crate) async fn describe(
    State(api): State<Shared>,
    headers: HeaderMap,
    crate::api::Body(body): crate::api::Body<MemberRequest>,
) -> Response {
    start(api, headers, body, true, None).await
}
struct Exchange {
    temporary: tempfile::TempDir,
    incoming: Option<PathBuf>,
}
pub(crate) async fn exchange(State(api): State<Shared>, request: Request) -> Response {
    let headers = request.headers().clone();
    match receive(request.into_body()).await {
        Ok((body, exchange)) => start(api, headers, body, false, Some(exchange)).await,
        Err((status, key)) => ApiError::new(status, key, &headers).into_response(),
    }
}
async fn receive(
    body: Body,
) -> std::result::Result<(MemberRequest, Exchange), (StatusCode, &'static str)> {
    use futures_util::StreamExt;
    let invalid = || (StatusCode::UNPROCESSABLE_ENTITY, "member_invalid_request");
    let too_large = || (StatusCode::PAYLOAD_TOO_LARGE, "member_request_too_large");
    let temporary = tempfile::tempdir().map_err(|_| invalid())?;
    let incoming = temporary.path().join("in.bundle");
    let mut output = tokio::fs::File::create(&incoming)
        .await
        .map_err(|_| invalid())?;
    let mut stream = body.into_data_stream();
    let mut metadata = vec![];
    let mut parsed = None;
    let mut size = 0usize;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| invalid())?;
        let tail = if parsed.is_none() {
            if let Some(newline) = chunk.iter().position(|v| *v == b'\n') {
                if metadata.len() + newline > MAX_METADATA {
                    return Err(too_large());
                }
                metadata.extend_from_slice(&chunk[..newline]);
                parsed = Some(
                    serde_json::from_slice::<MemberRequest>(&metadata).map_err(|_| invalid())?,
                );
                &chunk[newline + 1..]
            } else {
                if metadata.len() + chunk.len() > MAX_METADATA {
                    return Err(too_large());
                }
                metadata.extend_from_slice(&chunk);
                continue;
            }
        } else {
            &chunk[..]
        };
        size = size.checked_add(tail.len()).ok_or_else(too_large)?;
        if size > MAX_BUNDLE {
            return Err(too_large());
        }
        output.write_all(tail).await.map_err(|_| invalid())?;
    }
    output.flush().await.map_err(|_| invalid())?;
    Ok((
        parsed.ok_or_else(invalid)?,
        Exchange {
            temporary,
            incoming: if size > 0 { Some(incoming) } else { None },
        },
    ))
}
async fn start(
    api: Shared,
    headers: HeaderMap,
    body: MemberRequest,
    describe: bool,
    exchange: Option<Exchange>,
) -> Response {
    let Some(backend) = api.member.clone() else {
        return ApiError::new(StatusCode::CONFLICT, "member_workspace_mismatch", &headers)
            .key(
                "member_workspace_mismatch",
                vec![(
                    "workspace",
                    api.workspace
                        .as_ref()
                        .map(|path| path.display().to_string())
                        .unwrap_or_else(|| "—".into()),
                )],
            )
            .into_response();
    };
    if api.cancelled.is_cancelled() || api.work.is_closed() {
        return ApiError::new(StatusCode::CONFLICT, "member_backend_closing", &headers)
            .into_response();
    }
    if let Err((status, key)) = body.validate(&backend, exchange.is_some()) {
        return ApiError::new(status, key, &headers)
            .key(
                key,
                if key == "member_workspace_mismatch" {
                    vec![(
                        "workspace",
                        api.workspace
                            .as_ref()
                            .map(|p| p.display().to_string())
                            .unwrap_or_else(|| "—".into()),
                    )]
                } else {
                    vec![]
                },
            )
            .into_response();
    }
    let exchange = match exchange {
        Some(exchange) => exchange,
        None => match tempfile::tempdir() {
            Ok(temporary) => Exchange {
                temporary,
                incoming: None,
            },
            Err(_) => {
                return ApiError::new(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "member_invalid_request",
                    &headers,
                )
                .into_response();
            }
        },
    };
    let timeout = backend.timeout;
    let (send, receive) = tokio::sync::oneshot::channel();
    api.work.spawn(async move {
        let outgoing = exchange.temporary.path().join("out.bundle");
        let mut result = backend
            .executor
            .execute(body, describe, exchange.incoming.as_deref(), &outgoing)
            .await;
        let stream = tokio::fs::File::open(outgoing).await.ok();
        if let Some(file) = &stream
            && let Ok(metadata) = file.metadata().await
        {
            result.metadata.insert("size".into(), metadata.len().into());
        }
        let _ = send.send((result, stream, exchange.temporary));
    });
    let response = tokio::time::timeout(timeout, receive).await;
    let Ok(Ok((result, file, temporary))) = response else {
        let result = Result {
            exit_code: 124,
            stderr: crate::i18n::t(
                crate::i18n::language(
                    headers
                        .get("x-guildbotics-language")
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or(""),
                ),
                "app_api.errors.member_timed_out",
                &[],
            ),
            ..Default::default()
        };
        return (
            [("content-type", "application/octet-stream")],
            format!("{}\n", serde_json::to_string(&result).unwrap()),
        )
            .into_response();
    };
    let first = Some(format!("{}\n", serde_json::to_string(&result).unwrap()).into_bytes());
    let stream = futures_util::stream::unfold(
        (first, file, temporary),
        |(first, mut file, temporary)| async move {
            if let Some(first) = first {
                return Some((Ok::<_, std::io::Error>(first), (None, file, temporary)));
            }
            let file_reader = file.as_mut()?;
            let mut buffer = vec![0; 1 << 16];
            match file_reader.read(&mut buffer).await {
                Ok(0) => None,
                Ok(size) => {
                    buffer.truncate(size);
                    Some((Ok(buffer), (None, file, temporary)))
                }
                Err(error) => Some((Err(error), (None, None, temporary))),
            }
        },
    );
    (
        [("content-type", "application/octet-stream")],
        Body::from_stream(stream),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tower::ServiceExt;
    struct Echo {
        calls: Arc<AtomicUsize>,
        delay: bool,
    }
    impl Executor for Echo {
        fn execute<'a>(
            &'a self,
            request: MemberRequest,
            describe: bool,
            incoming: Option<&'a Path>,
            outgoing: &'a Path,
        ) -> Execution<'a> {
            Box::pin(async move {
                self.calls.fetch_add(1, Ordering::SeqCst);
                if self.delay {
                    tokio::time::sleep(Duration::from_millis(30)).await;
                }
                if let Some(incoming) = incoming {
                    tokio::fs::copy(incoming, outgoing).await.unwrap();
                }
                Result{stdout:serde_json::to_string(&json!({"stdin":request.stdin,"describe":describe,"arguments":request.arguments})).unwrap(),..Default::default()}
            })
        }
    }
    fn api(root: &Path, delay: bool) -> (Shared, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        (
            Arc::new(crate::api::Api {
                service: Default::default(),
                member: Some(Arc::new(Backend {
                    version: "build-1".into(),
                    workspace_id: "workspace".into(),
                    executor: Arc::new(Echo {
                        calls: calls.clone(),
                        delay,
                    }),
                    timeout: if delay {
                        Duration::from_millis(1)
                    } else {
                        Duration::from_secs(30)
                    },
                })),
                inputs: crate::input_files::InputFileStore::deferred(&root.join("inputs")),
                auth: crate::auth::ServerAuth::open(root, 8765).unwrap(),
                instance_id: "fixture".into(),
                data: root.into(),
                workspace: None,
                work: tokio_util::task::TaskTracker::new(),
                cancelled: tokio_util::sync::CancellationToken::new(),
                shutdown: Default::default(),
            }),
            calls,
        )
    }
    fn body() -> Value {
        json!({"version":"build-1","workspace_id":"workspace","arguments":["help"]})
    }
    async fn request(api: Shared, path: &str, bytes: Vec<u8>, authorized: bool) -> Response {
        let mut builder = axum::http::Request::builder()
            .method("POST")
            .uri(path)
            .header("host", "127.0.0.1:8765")
            .header("content-type", "application/json");
        if authorized {
            let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
            builder = builder.header("authorization", format!("Bearer {}", token.trim()));
        }
        crate::api::router(api)
            .oneshot(builder.body(Body::from(bytes)).unwrap())
            .await
            .unwrap()
    }
    async fn bytes(response: Response) -> Vec<u8> {
        axum::body::to_bytes(response.into_body(), MAX_METADATA)
            .await
            .unwrap()
            .to_vec()
    }
    #[tokio::test]
    async fn authority_version_workspace_and_client_assigned_identity_fail_before_execution() {
        let temp = tempfile::tempdir().unwrap();
        let (api, calls) = api(temp.path(), false);
        assert_eq!(
            request(
                api.clone(),
                "/api/member/run",
                serde_json::to_vec(&body()).unwrap(),
                false
            )
            .await
            .status(),
            StatusCode::UNAUTHORIZED
        );
        for (key, value, status) in [
            ("version", json!("build-2"), StatusCode::CONFLICT),
            ("workspace_id", json!("other"), StatusCode::CONFLICT),
            ("run_id", json!("forged"), StatusCode::UNPROCESSABLE_ENTITY),
            ("phase", json!("push"), StatusCode::UNPROCESSABLE_ENTITY),
            (
                "arguments",
                json!(["--workspace", "/caller"]),
                StatusCode::UNPROCESSABLE_ENTITY,
            ),
        ] {
            let mut input = body();
            input[key] = value;
            assert_eq!(
                request(
                    api.clone(),
                    "/api/member/run",
                    serde_json::to_vec(&input).unwrap(),
                    true
                )
                .await
                .status(),
                status,
                "{key}"
            );
        }
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn plan_and_run_preserve_content_and_option_values_without_opening_caller_paths() {
        let temp = tempfile::tempdir().unwrap();
        let (api, calls) = api(temp.path(), false);
        for (path, describe) in [("/api/member/plan", true), ("/api/member/run", false)] {
            let mut input = body();
            input["arguments"] = json!([
                "ticket",
                "comment",
                "--title",
                "--workspace",
                "--content-stdin"
            ]);
            input["stdin"] = "日本語の本文\n".into();
            let response =
                request(api.clone(), path, serde_json::to_vec(&input).unwrap(), true).await;
            assert_eq!(response.status(), StatusCode::OK);
            let metadata: Value = serde_json::from_slice(&bytes(response).await).unwrap();
            let payload: Value =
                serde_json::from_str(metadata["stdout"].as_str().unwrap()).unwrap();
            assert_eq!(payload["stdin"], input["stdin"]);
            assert_eq!(payload["arguments"], input["arguments"]);
            assert_eq!(payload["describe"], describe);
        }
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }
    #[tokio::test]
    async fn exchange_returns_metadata_line_then_the_exact_raw_bundle() {
        let temp = tempfile::tempdir().unwrap();
        let (api, _) = api(temp.path(), false);
        let mut input = body();
        input["phase"] = "tracking".into();
        let mut wire = serde_json::to_vec(&input).unwrap();
        wire.push(b'\n');
        wire.extend(b"raw\0bundle\xff");
        let response = request(api, "/api/member/git", wire, true).await;
        assert_eq!(response.status(), StatusCode::OK);
        let response = bytes(response).await;
        let newline = response.iter().position(|v| *v == b'\n').unwrap();
        let metadata: Value = serde_json::from_slice(&response[..newline]).unwrap();
        assert_eq!(metadata["size"], 11);
        assert_eq!(&response[newline + 1..], b"raw\0bundle\xff");
    }
    #[tokio::test]
    async fn timeout_keeps_the_worker_tracked_until_it_finishes() {
        let temp = tempfile::tempdir().unwrap();
        let (api, calls) = api(temp.path(), true);
        let response = request(
            api.clone(),
            "/api/member/run",
            serde_json::to_vec(&body()).unwrap(),
            true,
        )
        .await;
        let metadata: Value = serde_json::from_slice(&bytes(response).await).unwrap();
        assert_eq!(metadata["exit_code"], 124);
        assert_eq!(api.work.len(), 1);
        api.work.close();
        api.work.wait().await;
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
    #[tokio::test]
    async fn invalid_exchange_and_oversized_metadata_are_refused_without_a_worker() {
        let temp = tempfile::tempdir().unwrap();
        let (api, calls) = api(temp.path(), false);
        for wire in [
            b"no-newline".to_vec(),
            b"{}\n".to_vec(),
            vec![b'x'; MAX_METADATA + 1],
        ] {
            assert!(
                request(api.clone(), "/api/member/git", wire, true)
                    .await
                    .status()
                    .is_client_error()
            );
        }
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn health_advertises_the_backends_build_version_and_workspace_identity() {
        let temp = tempfile::tempdir().unwrap();
        let (api, _) = api(temp.path(), false);
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let response = crate::api::router(api)
            .oneshot(
                axum::http::Request::builder()
                    .uri("/api/health")
                    .header("host", "127.0.0.1:8765")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let payload: Value = serde_json::from_slice(&bytes(response).await).unwrap();
        assert_eq!(payload["version"], "build-1");
        assert_eq!(payload["workspace_id"], "workspace");
    }
    #[tokio::test]
    async fn mismatched_workspace_and_shutdown_return_localized_restart_guidance() {
        for language in ["en", "ja"] {
            let temp = tempfile::tempdir().unwrap();
            let (mut api, _) = api(temp.path(), false);
            let selected = temp.path().join("selected-workspace");
            Arc::get_mut(&mut api).unwrap().workspace = Some(selected.clone());
            let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
            let body = json!({"version":"build-1","workspace_id":"other","arguments":["help"]});
            let response = crate::api::router(api.clone())
                .oneshot(
                    axum::http::Request::builder()
                        .method("POST")
                        .uri("/api/member/run")
                        .header("host", "127.0.0.1:8765")
                        .header("authorization", format!("Bearer {}", token.trim()))
                        .header("content-type", "application/json")
                        .header("x-guildbotics-language", language)
                        .body(Body::from(body.to_string()))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::CONFLICT);
            let payload: Value = serde_json::from_slice(&bytes(response).await).unwrap();
            assert_eq!(payload["code"], "member_workspace_mismatch");
            let message = payload["message"].as_str().unwrap();
            assert!(message.contains(selected.to_str().unwrap()));
            assert!(!message.contains("%{"));
            assert!(!message.contains("guildbotics server stop"));
            api.cancelled.cancel();
            let response =
                request(api, "/api/member/run", body.to_string().into_bytes(), true).await;
            assert_eq!(response.status(), StatusCode::CONFLICT);
            let payload: Value = serde_json::from_slice(&bytes(response).await).unwrap();
            assert_eq!(payload["code"], "member_backend_closing");
        }
    }
    #[tokio::test]
    async fn missing_member_backend_guides_workspace_recovery_in_both_languages() {
        for language in ["en", "ja"] {
            for selected in [false, true] {
                for path in ["/api/member/plan", "/api/member/run"] {
                    let temp = tempfile::tempdir().unwrap();
                    let (mut api, _) = api(temp.path(), false);
                    Arc::get_mut(&mut api).unwrap().member = None;
                    let expected = if selected {
                        temp.path().display().to_string()
                    } else {
                        "—".into()
                    };
                    if selected {
                        Arc::get_mut(&mut api).unwrap().workspace = Some(temp.path().into());
                    }
                    let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
                    let response = crate::api::router(api)
                        .oneshot(
                            axum::http::Request::builder()
                                .method("POST")
                                .uri(path)
                                .header("host", "127.0.0.1:8765")
                                .header("authorization", format!("Bearer {}", token.trim()))
                                .header("content-type", "application/json")
                                .header("x-guildbotics-language", language)
                                .body(Body::from(body().to_string()))
                                .unwrap(),
                        )
                        .await
                        .unwrap();
                    assert_eq!(response.status(), StatusCode::CONFLICT);
                    let result: Value = serde_json::from_slice(&bytes(response).await).unwrap();
                    assert_eq!(result["code"], "member_workspace_mismatch");
                    let message = result["message"].as_str().unwrap();
                    assert!(message.contains(&expected));
                    assert!(!message.contains("%{"));
                    assert!(!message.contains("guildbotics server stop"));
                    assert!(!message.contains("guildbotics workspace use"));
                }
            }
        }
    }
    #[test]
    fn timeout_guidance_warns_of_late_completion_before_retrying() {
        for (language, late, retry) in [
            ("en", "it may still complete", "Before running it again"),
            ("ja", "このあと完了する可能性", "再実行する前に"),
        ] {
            let message = crate::i18n::t(language, "app_api.errors.member_timed_out", &[]);
            assert!(message.contains(late));
            assert!(message.contains(retry));
        }
    }
}
