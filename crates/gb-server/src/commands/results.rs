//! Bounded, process-local receipts. Expired IDs can never start new work.
use super::RunRequest;
use crate::api::{ApiError, Shared};
use axum::{
    Json,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Instant, SystemTime, UNIX_EPOCH},
};

const TTL: u64 = 24 * 60 * 60;
const CAPACITY: usize = 256;
const MAX_RESULT: usize = 1 << 20;

#[derive(Default)]
pub(crate) struct Results(Mutex<HashMap<String, Entry>>);
struct Entry {
    issued: u64,
    result: Option<(u16, Value)>,
}

impl Results {
    pub(crate) fn now() -> u64 {
        // Keep expiration monotonic even if the system clock is adjusted.
        static CLOCK: std::sync::OnceLock<(u64, Instant)> = std::sync::OnceLock::new();
        let (epoch, started) = CLOCK.get_or_init(|| {
            (
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs(),
                Instant::now(),
            )
        });
        epoch.saturating_add(started.elapsed().as_secs())
    }

    fn timestamp(id: &str, instance: &str, now: u64) -> Option<u64> {
        if id.len() > 128 {
            return None;
        }
        let tail = id.strip_prefix(instance)?.strip_prefix('.')?;
        let (issued, nonce) = tail.split_once('.')?;
        uuid::Uuid::parse_str(nonce).ok()?;
        let issued = issued.parse::<u64>().ok()?;
        (issued <= now && now - issued < TTL).then_some(issued)
    }

    fn prune(entries: &mut HashMap<String, Entry>, now: u64) {
        entries.retain(|_, entry| entry.result.is_none() || now.saturating_sub(entry.issued) < TTL);
    }
}

pub(super) async fn submit(api: Shared, headers: HeaderMap, request: RunRequest) -> Response {
    let id = request.request_id.clone().unwrap();
    {
        let mut entries = api.command_results.0.lock().unwrap();
        let now = Results::now();
        Results::prune(&mut entries, now);
        if entries.contains_key(&id) {
            drop(entries);
            return get(&api, &headers, &id);
        }
        let Some(issued) = Results::timestamp(&id, &api.instance_id, now) else {
            return ApiError::new(StatusCode::NOT_FOUND, "command_result_missing", &headers)
                .into_response();
        };
        if entries.len() >= CAPACITY {
            return ApiError::new(
                StatusCode::TOO_MANY_REQUESTS,
                "command_results_full",
                &headers,
            )
            .into_response();
        }
        entries.insert(
            id.clone(),
            Entry {
                issued,
                result: None,
            },
        );
        // Insert and spawn without an await: dropping the HTTP request cannot
        // leave an accepted ID without a worker.
        let weak = std::sync::Arc::downgrade(&api);
        // Expire idle receipts too; this timer must not delay server shutdown.
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_secs(TTL - (now - issued))).await;
            if let Some(api) = weak.upgrade() {
                Results::prune(&mut api.command_results.0.lock().unwrap(), Results::now());
            }
        });
        let worker_api = api.clone();
        let worker_id = id.clone();
        api.work.spawn(async move {
            // Also turn a panicking worker into a terminal receipt.
            let task = tokio::spawn(super::run_once(
                worker_api.clone(),
                headers.clone(),
                request,
            ));
            let response = task
                .await
                .unwrap_or_else(|_| super::internal("command worker stopped", "RuntimeError"));
            let answer = retain(response, &headers).await;
            if let Some(entry) = worker_api
                .command_results
                .0
                .lock()
                .unwrap()
                .get_mut(&worker_id)
            {
                entry.result = Some(answer);
            }
            Results::prune(
                &mut worker_api.command_results.0.lock().unwrap(),
                Results::now(),
            );
        });
    }
    get(&api, &HeaderMap::new(), &id)
}

async fn retain(response: Response, headers: &HeaderMap) -> (u16, Value) {
    let status = response.status();
    match axum::body::to_bytes(response.into_body(), MAX_RESULT).await {
        Ok(bytes) => (
            status.as_u16(),
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        ),
        Err(_) => {
            let response = ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "command_result_too_large",
                headers,
            )
            .into_response();
            let bytes = axum::body::to_bytes(response.into_body(), MAX_RESULT)
                .await
                .unwrap();
            (
                StatusCode::INTERNAL_SERVER_ERROR.as_u16(),
                serde_json::from_slice(&bytes).unwrap(),
            )
        }
    }
}

pub(super) fn get(api: &Shared, headers: &HeaderMap, id: &str) -> Response {
    let mut entries = api.command_results.0.lock().unwrap();
    Results::prune(&mut entries, Results::now());
    match entries.get(id) {
        Some(entry) => Json(match &entry.result {
            None => json!({"request_id": id, "state": "running"}),
            Some((status, answer)) => {
                json!({"request_id": id, "state": "finished", "status": status, "result": answer})
            }
        })
        .into_response(),
        None => {
            ApiError::new(StatusCode::NOT_FOUND, "command_result_missing", headers).into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use tower::ServiceExt;

    #[tokio::test]
    async fn receipts_preserve_success_and_failures_and_bound_large_results() {
        for status in [StatusCode::OK, StatusCode::BAD_REQUEST] {
            let result = json!({"output":"result"});
            assert_eq!(
                retain(
                    (status, Json(result.clone())).into_response(),
                    &HeaderMap::new()
                )
                .await,
                (status.as_u16(), result)
            );
        }
        for language in ["en", "ja"] {
            let mut headers = HeaderMap::new();
            headers.insert("x-guildbotics-language", language.parse().unwrap());
            let response = Json(json!({"output":"x".repeat(MAX_RESULT)})).into_response();
            let (status, result) = retain(response, &headers).await;
            assert_eq!(status, 500);
            assert_eq!(result["code"], "command_result_too_large");
            assert_eq!(
                result["message"],
                crate::i18n::t(language, "app_api.errors.command_result_too_large", &[])
            );
        }
    }

    #[test]
    fn ids_expire_and_belong_to_one_server_process() {
        let id = format!("server.100000.{}", uuid::Uuid::new_v4());
        assert_eq!(Results::timestamp(&id, "server", 100000), Some(100000));
        assert_eq!(Results::timestamp(&id, "server", 99999), None);
        assert_eq!(Results::timestamp(&id, "server", 100000 + TTL), None);
        assert_eq!(Results::timestamp(&id, "restarted", 100000), None);
        assert_eq!(
            Results::timestamp("server.100000.invalid", "server", 100000),
            None
        );
        let mut entries = HashMap::from([
            (
                "running".into(),
                Entry {
                    issued: 0,
                    result: None,
                },
            ),
            (
                "finished".into(),
                Entry {
                    issued: 0,
                    result: Some((200, json!({"output":"done"}))),
                },
            ),
        ]);
        Results::prune(&mut entries, TTL);
        assert!(entries.contains_key("running"));
        assert!(!entries.contains_key("finished"));
    }

    async fn body(response: Response) -> Value {
        serde_json::from_slice(
            &axum::body::to_bytes(response.into_body(), MAX_RESULT * 2)
                .await
                .unwrap(),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn http_receipts_survive_a_dropped_response_and_never_repeat_work() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let workspace = native.workspace.clone();
        let identity = gb_workspace::identity::ensure_workspace_identity(&workspace).unwrap();
        let path = workspace.join(".guildbotics/config/commands/functions/probe.py");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "async def main(context):\n    return 'done'\n").unwrap();
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let count = Arc::new(AtomicUsize::new(0));
        let factory = crate::keep_awake::test_count(&workspace, count);
        let api = Arc::new(crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(workspace),
                service: crate::service_runtime::Controller::new(native.clone(), host),
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(&native.data, 8765).unwrap(),
            "fixture".into(),
            native.data,
        ));
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let app = crate::api::router(api.clone());
        let request = |method: &str, path: &str, data: Value| {
            axum::http::Request::builder()
                .method(method)
                .uri(format!("/-/guildbotics/api{path}"))
                .header("host", "127.0.0.1:8765")
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {}", token.trim()))
                .body(axum::body::Body::from(data.to_string()))
                .unwrap()
        };
        let id = format!("fixture.{}.{}", Results::now(), uuid::Uuid::new_v4());
        let data = json!({"command":"functions/probe", "person":"aiko", "request_id":id, "expected_workspace":identity.workspace_id});
        let first = app
            .clone()
            .oneshot(request("POST", "/commands/run", data.clone()))
            .await
            .unwrap();
        assert_eq!(first.status(), StatusCode::OK);
        drop(first);
        let repeated = app
            .clone()
            .oneshot(request("POST", "/commands/run", data.clone()))
            .await
            .unwrap();
        assert_eq!(repeated.status(), StatusCode::OK);
        api.work.close();
        api.work.wait().await;
        let path = format!("/commands/runs/{id}");
        let result = body(
            app.clone()
                .oneshot(request("GET", &path, Value::Null))
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(result["state"], "finished");
        assert_eq!(result["status"], 400);
        let again = body(
            app.clone()
                .oneshot(request("POST", "/commands/run", data))
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(again, result);
        assert_eq!(factory.1.load(Ordering::SeqCst), 1);
        let unknown = app
            .clone()
            .oneshot(request("GET", "/commands/runs/unknown", Value::Null))
            .await
            .unwrap();
        assert_eq!(unknown.status(), StatusCode::NOT_FOUND);
        for id in [
            format!("restarted.{}.{}", Results::now(), uuid::Uuid::new_v4()),
            format!("fixture.1.{}", uuid::Uuid::new_v4()),
        ] {
            let refused = app
                .clone()
                .oneshot(request(
                    "POST",
                    "/commands/run",
                    json!({"command":"functions/probe", "request_id":id}),
                ))
                .await
                .unwrap();
            assert_eq!(refused.status(), StatusCode::NOT_FOUND);
        }
        let wrong = format!("fixture.{}.{}", Results::now(), uuid::Uuid::new_v4());
        let response = app.clone().oneshot(request("POST", "/commands/run", json!({"command":"functions/probe", "request_id":wrong,"expected_workspace":"different-id"}))).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        api.work.wait().await;
        let refused = body(
            app.clone()
                .oneshot(request(
                    "GET",
                    &format!("/commands/runs/{wrong}"),
                    Value::Null,
                ))
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(refused["status"], 409);
        assert_eq!(refused["result"]["code"], "command_workspace_changed");
        assert_eq!(factory.1.load(Ordering::SeqCst), 1);
        // A full store rejects new IDs without discarding old receipts.
        {
            let mut entries = api.command_results.0.lock().unwrap();
            for n in entries.len()..CAPACITY {
                entries.insert(
                    format!("held-{n}"),
                    Entry {
                        issued: Results::now(),
                        result: None,
                    },
                );
            }
        }
        let refused = app.clone().oneshot(request("POST", "/commands/run", json!({"command":"functions/probe", "request_id":format!("fixture.{}.{}", Results::now(), uuid::Uuid::new_v4())}))).await.unwrap();
        assert_eq!(refused.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(
            body(
                app.oneshot(request("GET", &path, Value::Null))
                    .await
                    .unwrap()
            )
            .await,
            result
        );
    }
}
