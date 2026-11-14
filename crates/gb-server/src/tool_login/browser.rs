//! Ephemeral owner login dialogue. Neither codes nor provider output enter run logs.
use crate::api::{ApiError, Shared};
use axum::{
    extract::{
        Path, State,
        ws::{Message, WebSocketUpgrade},
    },
    http::HeaderMap,
    response::{IntoResponse, Response},
};
use serde::Deserialize;

#[derive(Deserialize)]
struct Start {
    csrf: String,
}

pub(crate) async fn connect(
    State(api): State<Shared>,
    Path(tool): Path<String>,
    headers: HeaderMap,
    upgrade: WebSocketUpgrade,
) -> Response {
    let Some(workspace) = api.selected().workspace.clone() else {
        return ApiError::new(
            axum::http::StatusCode::BAD_REQUEST,
            "config_not_found",
            &headers,
        )
        .into_response();
    };
    let csrf = api.auth.session_csrf(&headers);
    if !headers.contains_key(axum::http::header::ORIGIN)
        || loopback_auth::check_request(api.auth.base_url(), &headers).is_err()
        || csrf.is_none()
        || !supported(&tool)
    {
        return ApiError::new(axum::http::StatusCode::FORBIDDEN, "forbidden", &headers)
            .into_response();
    }
    upgrade.max_message_size(8192).on_upgrade(move |mut socket| async move {
        // A GET/handshake alone must not start a VM or mutate a saved login.
        let start = tokio::time::timeout(std::time::Duration::from_secs(10), socket.recv()).await;
        let authenticated = match start {
            Ok(Some(Ok(Message::Text(text)))) => serde_json::from_str::<Start>(&text)
                .is_ok_and(|start| Some(start.csrf) == csrf),
            _ => false,
        };
        if !authenticated || api.auth.session_csrf(&headers) != csrf || api.shutdown.stopping() { return; }
        let _work = api.work.token();
        let cancelled = api.cancelled.child_token();
        let _cancel = cancelled.clone().drop_guard();
        let (input, receiver) = tokio::sync::mpsc::channel(8);
        let (output, mut chunks) = tokio::sync::mpsc::channel(64);
        let host = gb_environment::Host {
            data: api.data.clone(), home: api.home.clone(),
            package: gb_base::home::python_package(), identity_root: api.identity_root.clone(),
        };
        let io = super::LoginIo { input: receiver, output };
        #[cfg(test)]
        let runner = api.login_runner.lock().unwrap().clone();
        let login = async {
            #[cfg(test)]
            if let Some(runner) = runner { return runner(io, cancelled.clone()).await; }
            super::login_with_io(&host, &workspace, &tool, false, cancelled.clone(), Some(io), api.keychain.clone()).await
        };
        tokio::pin!(login);
        loop {
            tokio::select! {
                result = &mut login => {
                    if result.is_ok() { crate::environment::forget_usage(&api.data, &tool); }
                    // Drain the final prompt/output before announcing completion.
                    while let Ok(chunk) = chunks.try_recv() {
                        if socket.send(Message::Binary(chunk.into())).await.is_err() { break; }
                    }
                    let _ = socket.send(Message::Text(serde_json::json!({"saved":result.is_ok(), "error":result.err()}).to_string().into())).await;
                    return;
                },
                _ = cancelled.cancelled() => break,
                _ = api.shutdown.closing() => break,
                Some(chunk) = chunks.recv() => if socket.send(Message::Binary(chunk.into())).await.is_err() { break; },
                incoming = socket.recv() => match incoming {
                    Some(Ok(Message::Text(text))) if api.auth.session_csrf(&headers).is_some() => {
                        if input.try_send(text.as_bytes().to_vec()).is_err() { break; }
                    },
                    Some(Ok(Message::Ping(_)|Message::Pong(_))) => (),
                    _ => break,
                },
            }
        }
        cancelled.cancel();
        let _ = login.await;
    })
}

#[cfg(test)]
pub(crate) type LoginRunner = std::sync::Arc<
    dyn Fn(
            super::LoginIo,
            tokio_util::sync::CancellationToken,
        ) -> std::pin::Pin<
            Box<dyn std::future::Future<Output = Result<serde_json::Value, String>> + Send>,
        > + Send
        + Sync,
>;

pub(crate) fn supported(tool: &str) -> bool {
    ["codex", "claude", "grok", "copilot"].contains(&tool)
}

#[cfg(test)]
mod tests {
    use super::super::{
        tests::{FakeKeychain, catalog, credentials},
        *,
    };
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::{
        connect_async,
        tungstenite::{Message as ClientMessage, client::IntoClientRequest},
    };

    #[tokio::test]
    async fn browser_auth_dialogue_seals_login_and_updates_status() {
        let root = tempfile::tempdir().unwrap();
        let data = root.path().join("data");
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let auth = crate::auth::ServerAuth::open(&data, port).unwrap();
        let link = auth.entry_link("/");
        let (cookie, csrf) = auth
            .start_session(link.url.split_once('#').unwrap().1)
            .unwrap()
            .unwrap();
        let cookie = format!("{}={cookie}", auth.cookie);
        let api = std::sync::Arc::new(crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(root.path().join("workspace")),
                ..Default::default()
            },
            crate::input_files::InputFileStore::new(&data.join("inputs")).unwrap(),
            auth,
            "fixture".into(),
            data.clone(),
        ));
        let called = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let calls = called.clone();
        let saved_data = data.clone();
        *api.login_runner.lock().unwrap() = Some(std::sync::Arc::new(move |mut io, cancelled| {
            let data = saved_data.clone();
            let calls = calls.clone();
            Box::pin(async move {
                calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let device = DeviceKey {
                    machine_root: data.parent().unwrap().into(),
                    keychain: &FakeKeychain,
                };
                login_operation(
                    &device,
                    &data,
                    &catalog(),
                    "codex",
                    false,
                    "en",
                    |_| async move {
                        io.output
                            .send(b"https://example.test/login device-code".to_vec())
                            .await
                            .unwrap();
                        let answer = tokio::select! {
                        _ = cancelled.cancelled() => return Err(PrivateError::Start("cancelled".into())),
                        answer = io.input.recv() => answer.unwrap(),
                    };
                    if answer == b"synthetic-failure\n" { return Err(PrivateError::Start("Build the agent environment first.".into())); }
                    assert_eq!(answer, b"synthetic-answer\n");
                        Ok((0, credentials("synthetic-browser-login"), vec![]))
                    },
                )
                .await
            })
        }));
        let app = crate::api::router(api.clone());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let endpoint =
            format!("ws://127.0.0.1:{port}/-/guildbotics/api/intelligences/cli-agents/codex/login");
        for (session, origin, first) in [
            (false, true, "bad"),
            (true, false, "bad"),
            (true, true, "bad"),
        ] {
            let mut request = endpoint.clone().into_client_request().unwrap();
            if session {
                request
                    .headers_mut()
                    .insert("cookie", cookie.parse().unwrap());
            }
            if origin {
                request.headers_mut().insert(
                    "origin",
                    format!("http://127.0.0.1:{port}").parse().unwrap(),
                );
            }
            if let Ok((mut socket, _)) = connect_async(request).await {
                socket
                    .send(ClientMessage::Text(
                        format!("{{\"csrf\":\"{first}\"}}").into(),
                    ))
                    .await
                    .unwrap();
                let _ = socket.next().await;
            }
            assert_eq!(called.load(std::sync::atomic::Ordering::SeqCst), 0);
        }
        // Closing a live dialogue must cancel the provider without saving.
        let mut request = endpoint.clone().into_client_request().unwrap();
        request
            .headers_mut()
            .insert("cookie", cookie.parse().unwrap());
        request.headers_mut().insert(
            "origin",
            format!("http://127.0.0.1:{port}").parse().unwrap(),
        );
        let (mut abandoned, _) = connect_async(request).await.unwrap();
        abandoned
            .send(ClientMessage::Text(
                serde_json::json!({"csrf":csrf}).to_string().into(),
            ))
            .await
            .unwrap();
        assert!(abandoned.next().await.is_some());
        abandoned.close(None).await.unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            while !api.work.is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(!path(&data, "codex").exists());
        let mut request = endpoint.into_client_request().unwrap();
        request
            .headers_mut()
            .insert("cookie", cookie.parse().unwrap());
        request.headers_mut().insert(
            "origin",
            format!("http://127.0.0.1:{port}").parse().unwrap(),
        );
        let (mut socket, _) = connect_async(request).await.unwrap();
        socket
            .send(ClientMessage::Text(
                serde_json::json!({"csrf":csrf}).to_string().into(),
            ))
            .await
            .unwrap();
        assert!(
            matches!(socket.next().await.unwrap().unwrap(), ClientMessage::Binary(bytes) if bytes == b"https://example.test/login device-code"[..])
        );
        socket
            .send(ClientMessage::Text("synthetic-answer\n".into()))
            .await
            .unwrap();
        assert!(
            matches!(socket.next().await.unwrap().unwrap(), ClientMessage::Text(text) if text.contains("\"saved\":true"))
        );
        let device = DeviceKey {
            machine_root: data.parent().unwrap().into(),
            keychain: &FakeKeychain,
        };
        let files = credential_vault::unseal(&device, &path(&data, "codex"), "codex:default:test")
            .unwrap()
            .unwrap();
        assert_eq!(
            access(&files, &catalog()).unwrap(),
            "synthetic-browser-login"
        );
        assert!(
            crate::host_window::tool_status(
                &data,
                root.path(),
                &serde_json::json!([{"name":"codex","provision":catalog()["provision"]}]),
                &FakeKeychain
            )[0]["saved"]
                == true
        );
        let mut request =
            format!("ws://127.0.0.1:{port}/-/guildbotics/api/intelligences/cli-agents/codex/login")
                .into_client_request()
                .unwrap();
        request
            .headers_mut()
            .insert("cookie", cookie.parse().unwrap());
        request.headers_mut().insert(
            "origin",
            format!("http://127.0.0.1:{port}").parse().unwrap(),
        );
        let (mut failure, _) = connect_async(request).await.unwrap();
        failure
            .send(ClientMessage::Text(
                serde_json::json!({"csrf":csrf}).to_string().into(),
            ))
            .await
            .unwrap();
        let _ = failure.next().await;
        failure
            .send(ClientMessage::Text("synthetic-failure\n".into()))
            .await
            .unwrap();
        let result = failure.next().await.unwrap().unwrap().into_text().unwrap();
        let result: Value = serde_json::from_str(&result).unwrap();
        assert_eq!(result["saved"], false);
        assert_eq!(result["error"], "Build the agent environment first.");
        server.abort();
    }
}
