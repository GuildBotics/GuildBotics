//! Hermetic gateway and MCP contracts migrated from the frozen Python host.
use super::*;
use axum::http::{Method, Request};
use std::sync::Mutex;
use tower::ServiceExt;

type Seen = Arc<Mutex<Vec<(String, HeaderMap, Vec<u8>)>>>;
struct Upstream {
    state: GatewayState,
    seen: Seen,
    asked: Arc<Mutex<Vec<Option<String>>>>,
    lease: Lease,
    task: JoinHandle<()>,
}
impl Drop for Upstream {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl Upstream {
    async fn start() -> Self {
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen: Seen = Arc::default();
        let received = seen.clone();
        let router = Router::new().fallback(move |request: Request<Body>| {
            let received = received.clone();
            async move {
                let (parts, body) = request.into_parts();
                let bytes = axum::body::to_bytes(body, 1024).await.unwrap();
                let case = parts
                    .headers
                    .get("x-case")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("");
                let mut response = match case {
                    "retry" => Response::builder()
                        .status(401)
                        .body(Body::from("denied"))
                        .unwrap(),
                    "redirect" => Response::builder()
                        .status(307)
                        .header("location", format!("http://127.0.0.1:{port}/redirect"))
                        .body(Body::empty())
                        .unwrap(),
                    "encoded" => Response::builder()
                        .header("content-encoding", "gzip")
                        .body(Body::from("synthetic-private"))
                        .unwrap(),
                    "stream" => Response::builder()
                        .header("content-type", "text/event-stream")
                        .body(Body::from("data: synthetic-private\n\ndata: final\n\n"))
                        .unwrap(),
                    _ => Response::new(Body::from("echo synthetic-private")),
                };
                response
                    .headers_mut()
                    .insert("x-echo", "synthetic-private".parse().unwrap());
                response
                    .headers_mut()
                    .insert("connection", "x-hop".parse().unwrap());
                response
                    .headers_mut()
                    .insert("x-hop", "never forward".parse().unwrap());
                received.lock().unwrap().push((
                    parts.uri.to_string(),
                    parts.headers,
                    bytes.to_vec(),
                ));
                response
            }
        });
        let task = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let asked = Arc::new(Mutex::new(Vec::new()));
        let calls = asked.clone();
        let source: TokenSource = Arc::new(move |refused| {
            calls.lock().unwrap().push(refused);
            Box::pin(async { Ok("synthetic-private".into()) })
        });
        let leases = Leases::default();
        let lease = leases.lend_token("synthetic-stand-in".into());
        Self {
            state: GatewayState {
                policy: Policy {
                    provider: gb_environment::gateway::Provider::OpenAi,
                    origin: format!("http://127.0.0.1:{port}"),
                    routes: vec![("POST".into(), "/v1/messages".into())],
                },
                source,
                leases,
                refused: Arc::default(),
                client: reqwest::Client::builder()
                    .no_proxy()
                    .redirect(reqwest::redirect::Policy::none())
                    .build()
                    .unwrap(),
            },
            seen,
            asked,
            lease,
            task,
        }
    }
    async fn send(&self, method: Method, path: &str, auth: &str, case: &str) -> Response {
        forward(
            State(self.state.clone()),
            Request::builder()
                .method(method)
                .uri(path)
                .header("authorization", auth)
                .header("x-case", case)
                .header("cookie", "private-cookie")
                .header("x-api-key", "guest-key")
                .body(Body::from("literal body"))
                .unwrap(),
        )
        .await
    }
}
#[tokio::test]
async fn gateway_authentication_routes_and_turn_generations_fail_before_token_lookup() {
    let upstream = Upstream::start().await;
    for auth in ["", "Basic x", "Bearer wrong", "Bearer synthetic-private"] {
        assert_eq!(
            upstream
                .send(Method::POST, "/v1/messages", auth, "")
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
    }
    for (method, path) in [
        (Method::GET, "/v1/messages"),
        (Method::POST, "/v1/messages/sub"),
        (Method::POST, "/v1/oauth/token"),
        (Method::POST, "/v1/messages%2fadmin"),
    ] {
        assert_eq!(
            upstream
                .send(method, path, "Bearer synthetic-stand-in", "")
                .await
                .status(),
            StatusCode::FORBIDDEN
        );
    }
    upstream.state.leases.revoke();
    assert_eq!(
        upstream
            .send(
                Method::POST,
                "/v1/messages",
                "Bearer synthetic-stand-in",
                ""
            )
            .await
            .status(),
        StatusCode::UNAUTHORIZED
    );
    let next = upstream.state.leases.lend_token("next-stand-in".into());
    assert!(!upstream.lease.active());
    assert_eq!(
        upstream
            .send(
                Method::POST,
                "/v1/messages",
                "Bearer synthetic-stand-in",
                ""
            )
            .await
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert!(upstream.asked.lock().unwrap().is_empty());
    assert!(upstream.seen.lock().unwrap().is_empty());
    assert_eq!(
        upstream
            .send(
                Method::POST,
                "/v1/messages",
                &format!("Bearer {}", next.stand_in),
                ""
            )
            .await
            .status(),
        StatusCode::OK
    );
}
#[tokio::test]
async fn gateway_retries_once_preserves_body_and_never_follows_redirects_or_encoded_answers() {
    for (case, status, calls) in [
        ("retry", 401, 2),
        ("redirect", 307, 1),
        ("encoded", 502, 1),
        ("stream", 200, 1),
        ("plain", 200, 1),
    ] {
        let upstream = Upstream::start().await;
        let response = upstream
            .send(
                Method::POST,
                "/v1/messages?beta=true",
                "Bearer synthetic-stand-in",
                case,
            )
            .await;
        assert_eq!(response.status().as_u16(), status, "{case}");
        assert!(!response.headers().contains_key("x-hop"));
        if case == "redirect" {
            assert!(
                response.headers()["location"]
                    .to_str()
                    .unwrap()
                    .ends_with("/redirect")
            );
        }
        if case == "stream" {
            assert_eq!(response.headers()["content-type"], "text/event-stream");
        }
        let bytes = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("synthetic-private"));
        let seen = upstream.seen.lock().unwrap();
        assert_eq!(seen.len(), calls);
        for (uri, headers, body) in seen.iter() {
            assert_eq!(uri, "/v1/messages?beta=true");
            assert_eq!(body, b"literal body");
            assert_eq!(headers["authorization"], "Bearer synthetic-private");
            assert_eq!(headers["accept-encoding"], "identity");
            assert!(!headers.contains_key("cookie"));
            assert!(!headers.contains_key("x-api-key"));
        }
        assert_eq!(
            *upstream.asked.lock().unwrap(),
            if calls == 2 {
                vec![
                    None,
                    Some("synthetic-private".into()),
                    Some("synthetic-private".into()),
                ]
            } else {
                vec![None]
            }
        );
    }
}
#[tokio::test]
async fn login_refusals_reach_the_401_body_in_both_languages_after_retry_too() {
    for language in ["ja", "en"] {
        for after_retry in [false, true] {
            let mut upstream = Upstream::start().await;
            let reason = crate::i18n::t(
                language,
                "intelligences.agent_environment.tool.login_refused",
                &[
                    ("tool", "Claude Code"),
                    ("command", "guildbotics environment login claude"),
                ],
            );
            let expected = reason.clone();
            let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
            upstream.state.source = Arc::new(move |_| {
                let call = calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let reason = reason.clone();
                Box::pin(async move {
                    if after_retry && call < 2 {
                        Ok("synthetic-private".into())
                    } else {
                        Err(reason)
                    }
                })
            });
            let failed = upstream
                .send(
                    Method::POST,
                    "/v1/messages",
                    "Bearer synthetic-stand-in",
                    "retry",
                )
                .await;
            assert_eq!(failed.status(), StatusCode::UNAUTHORIZED);
            assert_eq!(
                axum::body::to_bytes(failed.into_body(), 4096)
                    .await
                    .unwrap(),
                expected
            );
            assert_eq!(
                upstream.seen.lock().unwrap().len(),
                if after_retry { 2 } else { 0 }
            );
        }
    }
}
#[tokio::test]
async fn unavailable_tokens_disconnected_bodies_and_ended_turns_never_reach_the_provider() {
    let mut upstream = Upstream::start().await;
    upstream.state.source = Arc::new(|_| Box::pin(async { Err("synthetic login refusal".into()) }));
    let failed = upstream
        .send(
            Method::POST,
            "/v1/messages",
            "Bearer synthetic-stand-in",
            "",
        )
        .await;
    assert_eq!(failed.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        axum::body::to_bytes(failed.into_body(), 4096)
            .await
            .unwrap(),
        "synthetic login refusal"
    );
    assert!(upstream.seen.lock().unwrap().is_empty());
    let body = Body::from_stream(futures_util::stream::once(async {
        Err::<Vec<u8>, _>(std::io::Error::other("disconnected"))
    }));
    let request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("authorization", "Bearer synthetic-stand-in")
        .body(body)
        .unwrap();
    assert_eq!(
        forward(State(upstream.state.clone()), request)
            .await
            .status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );
    let started = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let (begun, released) = (started.clone(), release.clone());
    upstream.state.source = Arc::new(move |_| {
        let (begun, released) = (begun.clone(), released.clone());
        Box::pin(async move {
            begun.notify_one();
            released.notified().await;
            Ok("synthetic-private".into())
        })
    });
    let state = upstream.state.clone();
    let request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("authorization", "Bearer synthetic-stand-in")
        .body(Body::empty())
        .unwrap();
    let waiting = tokio::spawn(forward(State(state), request));
    started.notified().await;
    upstream.state.leases.revoke();
    release.notify_one();
    assert_eq!(waiting.await.unwrap().status(), StatusCode::UNAUTHORIZED);
    assert!(upstream.seen.lock().unwrap().is_empty());
}
#[tokio::test]
async fn tls_gateways_trust_only_their_own_certificate_names_and_lease() {
    let upstream = Upstream::start().await;
    let first = Gateway::start_tls(
        upstream.state.policy.clone(),
        upstream.state.source.clone(),
        vec!["localhost".into()],
    )
    .await
    .unwrap();
    let second = Gateway::start_tls(
        upstream.state.policy.clone(),
        upstream.state.source.clone(),
        vec!["localhost".into()],
    )
    .await
    .unwrap();
    assert_ne!(first.ca_pem, second.ca_pem);
    let trusted = reqwest::Client::builder()
        .no_proxy()
        .add_root_certificate(reqwest::Certificate::from_pem(&first.ca_pem).unwrap())
        .build()
        .unwrap();
    let url = format!("https://localhost:{}/v1/messages", first.port);
    assert_eq!(
        trusted
            .post(&url)
            .bearer_auth(&first.lease.stand_in)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        trusted
            .post(&url)
            .bearer_auth(&second.lease.stand_in)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert!(
        trusted
            .post(format!("https://localhost:{}/v1/messages", second.port))
            .send()
            .await
            .is_err()
    );
    assert!(
        trusted
            .post(format!("https://127.0.0.1:{}/v1/messages", first.port))
            .send()
            .await
            .is_err()
    );
    first.revoke();
    assert_eq!(
        trusted
            .post(url)
            .bearer_auth(&first.lease.stand_in)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
}
#[tokio::test]
async fn mcp_requires_exact_bearer_and_local_host_and_dispatches_scoped_member_calls() {
    let dir = tempfile::tempdir().unwrap();
    let broker = Broker::new(
        dir.path().join("unused"),
        gb_environment::broker::Invocation {
            person_id: "aiko".into(),
            run_id: "run".into(),
            trace_id: "trace".into(),
            work_kind: "ticket".into(),
            work_identity: "87".into(),
            cwd: dir.path().into(),
            access: gb_wire::CommandAccess {
                read_only: true,
                inspects: vec![],
            },
        },
    );
    let state = HostState {
        token: "synthetic-owner-token".into(),
        calls: Arc::new(|_, _| Box::pin(async { Ok(json!({})) })),
        broker,
        alive: Arc::new(std::sync::atomic::AtomicBool::new(true)),
    };
    let router = Router::new().route("/mcp", post(mcp)).with_state(state);
    for (auth, host, status) in [
        ("", "localhost", 401),
        ("Bearer synthetic-owner-token-more", "localhost", 401),
        ("Basic synthetic-owner-token", "localhost", 401),
        ("Bearer synthetic-owner-token", "evil.test", 401),
        (
            "Bearer synthetic-owner-token",
            "host.microsandbox.internal:123",
            200,
        ),
    ] {
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp")
                    .header("authorization", auth)
                    .header("host", host)
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status().as_u16(), status);
    }
    let response = router.oneshot(Request::builder().method("POST").uri("/mcp").header("authorization","Bearer synthetic-owner-token").header("host","localhost").header("content-type","application/json").body(Body::from(r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"guildbotics_member","arguments":{"arguments":["help"],"turn_grant":"expired"}}}"#)).unwrap()).await.unwrap();
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let reply: Value = serde_json::from_slice(&bytes).unwrap();
    let result: Value =
        serde_json::from_str(reply["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(result["exit_code"], 2);
    assert!(result["stderr"].as_str().unwrap().contains("expired"));
    assert!(!String::from_utf8_lossy(&bytes).contains("synthetic-owner-token"));
}

#[tokio::test]
async fn revocation_stops_streams_even_when_waiting_for_the_next_upstream_chunk() {
    use futures_util::StreamExt;
    for pending in [false, true] {
        let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let (send, receiver) = tokio::sync::mpsc::channel::<Vec<u8>>(4);
        let receiver = Arc::new(tokio::sync::Mutex::new(Some(receiver)));
        let router = Router::new().fallback(move || {
            let receiver = receiver.clone();
            async move {
                let stream = futures_util::stream::unfold(
                    receiver.lock().await.take().unwrap(),
                    |mut receiver| async move {
                        receiver
                            .recv()
                            .await
                            .map(|bytes| (Ok::<_, std::io::Error>(bytes), receiver))
                    },
                );
                Response::new(Body::from_stream(stream))
            }
        });
        let task = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let mut upstream = Upstream::start().await;
        upstream.state.policy.origin = format!("http://127.0.0.1:{port}");
        send.send(vec![b'a'; 128]).await.unwrap();
        let response = upstream
            .send(
                Method::POST,
                "/v1/messages",
                "Bearer synthetic-stand-in",
                "",
            )
            .await;
        let mut stream = response.into_body().into_data_stream();
        assert!(!stream.next().await.unwrap().unwrap().is_empty());
        if pending {
            let next = tokio::spawn(async move { stream.next().await });
            tokio::task::yield_now().await;
            upstream.state.leases.revoke();
            send.send(b"never hand on".to_vec()).await.unwrap();
            assert!(next.await.unwrap().is_none());
        } else {
            upstream.state.leases.revoke();
            assert!(stream.next().await.is_none());
        }
        task.abort();
    }
}
#[tokio::test]
async fn unreachable_upstreams_and_upgrade_headers_never_expose_credentials_or_open_a_tunnel() {
    let mut upstream = Upstream::start().await;
    let request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("authorization", "Bearer synthetic-stand-in")
        .header("connection", "upgrade")
        .header("upgrade", "websocket")
        .body(Body::empty())
        .unwrap();
    let response = forward(State(upstream.state.clone()), request).await;
    assert_ne!(response.status(), StatusCode::SWITCHING_PROTOCOLS);
    {
        let seen = upstream.seen.lock().unwrap();
        assert!(!seen[0].1.contains_key("upgrade"));
    }
    let unavailable = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
        .await
        .unwrap();
    let port = unavailable.local_addr().unwrap().port();
    drop(unavailable);
    upstream.state.policy.origin = format!("http://127.0.0.1:{port}");
    let failed = upstream
        .send(
            Method::POST,
            "/v1/messages",
            "Bearer synthetic-stand-in",
            "",
        )
        .await;
    assert_eq!(failed.status(), StatusCode::BAD_GATEWAY);
    assert!(
        !String::from_utf8_lossy(
            &axum::body::to_bytes(failed.into_body(), 4096)
                .await
                .unwrap()
        )
        .contains("synthetic-private")
    );
}

#[tokio::test]
async fn refused_routes_are_logged_once_per_turn_without_query_headers_or_body() {
    #[derive(Clone)]
    struct Recorder(Arc<Mutex<Vec<String>>>);
    impl tracing::Subscriber for Recorder {
        fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
        fn enter(&self, _: &tracing::span::Id) {}
        fn exit(&self, _: &tracing::span::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            struct Fields(String);
            impl tracing::field::Visit for Fields {
                fn record_debug(&mut self, _: &tracing::field::Field, value: &dyn std::fmt::Debug) {
                    self.0.push_str(&format!("{value:?}"));
                }
            }
            let mut fields = Fields(String::new());
            event.record(&mut fields);
            self.0.lock().unwrap().push(fields.0);
        }
    }
    let upstream = Upstream::start().await;
    let records = Arc::new(Mutex::new(Vec::new()));
    let _guard = tracing::subscriber::set_default(Recorder(records.clone()));
    for _ in 0..3 {
        assert_eq!(
            upstream
                .send(
                    Method::POST,
                    "/v1/oauth/token?secret=synthetic-carried",
                    "Bearer synthetic-stand-in",
                    ""
                )
                .await
                .status(),
            StatusCode::FORBIDDEN
        );
    }
    assert_eq!(
        records
            .lock()
            .unwrap()
            .iter()
            .filter(|s| s.contains("Gateway refused POST /v1/oauth/token"))
            .count(),
        1
    );
    let next = upstream.state.leases.lend_token("next".into());
    assert_eq!(
        upstream
            .send(
                Method::POST,
                "/v1/oauth/token?secret=synthetic-carried",
                &format!("Bearer {}", next.stand_in),
                ""
            )
            .await
            .status(),
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        records
            .lock()
            .unwrap()
            .iter()
            .filter(|s| s.contains("Gateway refused POST /v1/oauth/token"))
            .count(),
        2
    );
    assert!(
        !records
            .lock()
            .unwrap()
            .iter()
            .any(|s| s.contains("synthetic-carried")
                || s.contains("literal body")
                || s.contains("private-cookie"))
    );
    assert_eq!(
        upstream
            .send(Method::POST, "/v1/messages", "Bearer next", "encoded")
            .await
            .status(),
        StatusCode::BAD_GATEWAY
    );
    assert!(
        records
            .lock()
            .unwrap()
            .iter()
            .any(|s| s.contains("Gateway refused an encoded answer to POST /v1/messages"))
    );
}
