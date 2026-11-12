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
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Instant, SystemTime, UNIX_EPOCH},
};

const TTL: u64 = 24 * 60 * 60 * 1_000_000_000;
const CAPACITY: usize = 256;
const MAX_RESULT: usize = 32 * 1024 * 1024;

#[derive(Default)]
pub(crate) struct Results(Mutex<Store>);
#[derive(Default)]
struct Store {
    entries: HashMap<String, Entry>,
    retired_through: u64,
}
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
                    .as_nanos() as u64,
                Instant::now(),
            )
        });
        static LAST: AtomicU64 = AtomicU64::new(0);
        let now = epoch.saturating_add(started.elapsed().as_nanos() as u64);
        let previous = LAST
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |last| {
                Some(now.max(last + 1))
            })
            .unwrap();
        now.max(previous + 1)
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

impl Store {
    fn admit(&mut self, id: &str, instance: &str, now: u64) -> Result<(), &'static str> {
        let issued = Results::timestamp(id, instance, now)
            .filter(|issued| *issued > self.retired_through)
            .ok_or("command_result_missing")?;
        if self.entries.len() >= CAPACITY {
            let oldest = self
                .entries
                .iter()
                .filter(|(_, entry)| entry.result.is_some())
                .min_by_key(|(_, entry)| entry.issued)
                .map(|(id, entry)| (id.clone(), entry.issued))
                .ok_or("command_results_full")?;
            // A delayed, previously unseen request must not pass the new floor.
            if issued <= oldest.1 {
                return Err("command_result_missing");
            }
            self.retired_through = self.retired_through.max(oldest.1);
            self.entries.remove(&oldest.0);
        }
        self.entries.insert(
            id.to_owned(),
            Entry {
                issued,
                result: None,
            },
        );
        Ok(())
    }
}

pub(super) async fn submit(api: Shared, headers: HeaderMap, mut request: RunRequest) -> Response {
    let id = request.request_id.clone().unwrap();
    // Missing prerequisites are refused before admitting the receipt or starting work.
    // A repeated accepted ID never imports or runs anything again.
    if api
        .command_results
        .0
        .lock()
        .unwrap()
        .entries
        .contains_key(&id)
    {
        return get(&api, &headers, &id);
    }
    if super::history::complete(&api, &mut request).await.is_err() {
        return ApiError::new(StatusCode::CONFLICT, "command_history_required", &headers)
            .into_response();
    }
    {
        let mut store = api.command_results.0.lock().unwrap();
        let now = Results::now();
        Results::prune(&mut store.entries, now);
        if store.entries.contains_key(&id) {
            drop(store);
            return get(&api, &headers, &id);
        }
        if let Err(code) = store.admit(&id, &api.instance_id, now) {
            let status = if code == "command_results_full" {
                StatusCode::TOO_MANY_REQUESTS
            } else {
                StatusCode::NOT_FOUND
            };
            return ApiError::new(status, code, &headers).into_response();
        }
        // Insert and spawn without an await: dropping the HTTP request cannot
        // leave an accepted ID without a worker.
        // Prune on access and completion; per-ID timers would outlive eviction
        // and make memory grow with throughput rather than CAPACITY.
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
                .entries
                .get_mut(&worker_id)
            {
                entry.result = Some(answer);
            }
            Results::prune(
                &mut worker_api.command_results.0.lock().unwrap().entries,
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
    let mut store = api.command_results.0.lock().unwrap();
    Results::prune(&mut store.entries, Results::now());
    match store.entries.get(id) {
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

    #[test]
    fn completed_receipts_make_room_without_replaying_retired_ids() {
        let mut store = Store::default();
        let id = |time| format!("server.{time}.{}", uuid::Uuid::new_v4());
        let first_time = Results::now();
        let running = id(first_time);
        store.admit(&running, "server", Results::now()).unwrap();
        let mut retired = Vec::new();
        for _ in 0..CAPACITY * 2 {
            let time = Results::now();
            let next = id(time);
            store.admit(&next, "server", Results::now()).unwrap();
            store.entries.get_mut(&next).unwrap().result = Some((200, json!({})));
            retired.push((next, time));
            assert!(store.entries.contains_key(&running));
            assert!(store.entries.len() <= CAPACITY);
        }
        let (old, time) = &retired[0];
        assert!(!store.entries.contains_key(old));
        for old in [old.clone(), id(*time), id(first_time)] {
            assert_eq!(
                store.admit(&old, "server", Results::now()),
                Err("command_result_missing")
            );
        }
        assert!(Results::now() > store.retired_through);
        // A delayed ID newer than the current floor but older than the next
        // eviction must fail without losing another retained result.
        let floor = store.retired_through;
        let next_evicted = store
            .entries
            .values()
            .filter(|entry| entry.result.is_some())
            .map(|entry| entry.issued)
            .min()
            .unwrap();
        assert_eq!(
            store.admit(&id(next_evicted), "server", Results::now()),
            Err("command_result_missing")
        );
        assert_eq!(store.retired_through, floor);
        assert_eq!(store.entries.len(), CAPACITY);
        store.entries.remove(&retired.last().unwrap().0);
        assert_eq!(
            store.admit(old, "server", Results::now()),
            Err("command_result_missing")
        );
    }

    #[test]
    fn eviction_floor_never_moves_backwards() {
        let mut store = Store {
            retired_through: 20,
            ..Default::default()
        };
        for n in 0..CAPACITY {
            store.entries.insert(
                n.to_string(),
                Entry {
                    issued: n as u64 + 1,
                    result: Some((200, json!({}))),
                },
            );
        }
        let id = format!("server.1000.{}", uuid::Uuid::new_v4());
        store.admit(&id, "server", 1000).unwrap();
        assert_eq!(store.retired_through, 20);
        assert!(store.entries.contains_key("1"));
    }

    #[test]
    fn health_times_remain_unique_across_concurrent_callers() {
        let times = std::thread::scope(|scope| {
            let workers: Vec<_> = (0..4)
                .map(|_| scope.spawn(|| (0..256).map(|_| Results::now()).collect::<Vec<_>>()))
                .collect();
            workers
                .into_iter()
                .flat_map(|worker| {
                    let times = worker.join().unwrap();
                    assert!(times.windows(2).all(|pair| pair[0] < pair[1]));
                    times
                })
                .collect::<Vec<_>>()
        });
        let unique: std::collections::HashSet<_> = times.iter().collect();
        assert_eq!(unique.len(), times.len());
    }

    #[tokio::test]
    async fn http_receipts_survive_a_dropped_response_and_never_repeat_work() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let (seed, bare, _) = crate::member::native::tests::seed_code(&native, root.path());
        let remote = url::Url::from_file_path(bare).unwrap().to_string();
        let workspace = native.workspace.clone();
        let identity = gb_workspace::identity::ensure_workspace_identity(&workspace).unwrap();
        let path = workspace.join(".guildbotics/config/commands/functions/probe.py");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "async def main(context):\n    return 'done'\n").unwrap();
        std::fs::create_dir_all(root.path().join("home")).unwrap();
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let count = Arc::new(AtomicUsize::new(0));
        let factory = crate::keep_awake::test_count(&workspace, count);
        let mut api = crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(workspace),
                service: crate::service_runtime::Controller::new(native.clone(), host),
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(&native.data, 8765).unwrap(),
            "fixture".into(),
            native.data.clone(),
        );
        api.identity_root = native.identity_root.clone();
        api.keychain = native.keychain.clone();
        let api = Arc::new(api);
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
        let mixed = app
            .clone()
            .oneshot(request(
                "POST",
                "/commands/run",
                json!({
                    "command":"functions/probe", "person":"aiko", "preflight":true,
                    "cwd":root.path(), "client_cwd":root.path().join("client-only/absent")
                }),
            ))
            .await
            .unwrap();
        assert_eq!(mixed.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let preflight = app.clone().oneshot(request("POST", "/commands/run", json!({"command":"functions/probe", "person":"aiko", "preflight":true, "client_cwd":root.path().join("client-only/absent"), "expected_workspace":identity.workspace_id, "remotes":{"origin":remote, "external":"https://unavailable.invalid/repo.git"}}))).await.unwrap();
        let status = preflight.status();
        let bytes = axum::body::to_bytes(preflight.into_body(), MAX_RESULT)
            .await
            .unwrap();
        assert_eq!(
            status,
            StatusCode::OK,
            "{}",
            String::from_utf8_lossy(&bytes)
        );
        let answer: Value = serde_json::from_slice(&bytes).unwrap();
        let declaration: Value = serde_json::from_str(answer["output"].as_str().unwrap()).unwrap();
        assert_eq!(declaration["read_only"], false);
        assert_eq!(declaration["remotes"], json!(["origin"]));
        assert!(api.command_results.0.lock().unwrap().entries.is_empty());
        assert!(!root.path().join("client-only").exists());
        let id = format!("fixture.{}.{}", Results::now(), uuid::Uuid::new_v4());
        let copy = gb_environment::worktree::Copy::new(
            seed,
            "/work".into(),
            vec![],
            root.path().join("write.lock"),
        )
        .unwrap();
        let full = gb_environment::worktree::transfer::Upload::capture(&copy).unwrap();
        let data = json!({"command":"functions/probe", "person":"aiko", "request_id":id, "expected_workspace":identity.workspace_id,
            "client_cwd":root.path().join("client-only/absent"), "upload": full});
        let mut incremental = data.clone();
        incremental["client_cwd"] = json!(root.path().join("client-only/absent"));
        incremental["upload"] = json!({"directories":[], "files":[], "nested":[], "git": {
            "bundle":"", "bases":["refs/remotes/origin/main"], "object_format":"sha1", "head":"refs/heads/main",
            "refs":{}, "index":null, "exclude":null, "remotes":{"origin":"https://unavailable.invalid/repo.git"}, "objects":{}, "shared_indexes":{}
        }});
        let refused = app
            .clone()
            .oneshot(request("POST", "/commands/run", incremental))
            .await
            .unwrap();
        assert_eq!(refused.status(), StatusCode::CONFLICT);
        assert_eq!(body(refused).await["code"], "command_history_required");
        assert!(api.command_results.0.lock().unwrap().entries.is_empty());
        assert_eq!(factory.1.load(Ordering::SeqCst), 0);
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
        // Force eviction of the second receipt while an older running receipt
        // remains accessible. Reposting an evicted ID must not spawn work.
        let held = format!("fixture.{}.{}", Results::now(), uuid::Uuid::new_v4());
        {
            let mut store = api.command_results.0.lock().unwrap();
            for n in store.entries.len()..CAPACITY {
                store.entries.insert(
                    format!("finished-{n}"),
                    Entry {
                        issued: Results::now(),
                        result: Some((200, json!({}))),
                    },
                );
            }
            let running = store.entries.get_mut(&id).unwrap();
            running.result = None;
        }
        let accepted = app
            .clone()
            .oneshot(request(
                "POST",
                "/commands/run",
                json!({"command":"functions/probe", "request_id":held}),
            ))
            .await
            .unwrap();
        assert_eq!(accepted.status(), StatusCode::OK);
        api.work.wait().await;
        let before = factory.1.load(Ordering::SeqCst);
        let replayed = app
            .clone()
            .oneshot(request(
                "POST",
                "/commands/run",
                json!({"command":"functions/probe", "request_id":wrong}),
            ))
            .await
            .unwrap();
        assert_eq!(replayed.status(), StatusCode::NOT_FOUND);
        assert_eq!(factory.1.load(Ordering::SeqCst), before);
        let running = body(
            app.clone()
                .oneshot(request(
                    "POST",
                    "/commands/run",
                    json!({"command":"functions/probe", "request_id":id}),
                ))
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(running["state"], "running");
        {
            api.command_results
                .0
                .lock()
                .unwrap()
                .entries
                .get_mut(&id)
                .unwrap()
                .result = Some((200, json!({})));
        }
        let finished = app
            .clone()
            .oneshot(request(
                "POST",
                "/commands/run",
                json!({"command":"functions/probe", "request_id":id}),
            ))
            .await
            .unwrap();
        assert_eq!(finished.status(), StatusCode::OK);
        assert_eq!(body(finished).await["state"], "finished");
        assert_eq!(factory.1.load(Ordering::SeqCst), before);
        // A store filled entirely with running work rejects new IDs.
        {
            let mut store = api.command_results.0.lock().unwrap();
            store.entries.clear();
            for n in 0..CAPACITY {
                store.entries.insert(
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
            api.command_results.0.lock().unwrap().entries.len(),
            CAPACITY
        );
    }
}
