//! Sending to the owner's webhook.

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(concat!("branvil/", env!("CARGO_PKG_VERSION")))
        .build()
        .expect("an HTTP client")
}

/// Neither errors nor response bodies may expose the credential in the URL.
pub async fn send(client: &reqwest::Client, url: &str, text: &str) -> Result<(), String> {
    let response = client
        .post(url.trim())
        .json(&serde_json::json!({ "text": text }))
        .send()
        .await
        .map_err(|e| {
            if e.is_timeout() {
                "the webhook request timed out"
            } else if e.is_connect() {
                "could not connect to the webhook"
            } else {
                "the webhook request failed"
            }
            .to_string()
        })?;
    if !response.status().is_success() {
        return Err(format!("the webhook answered {}", response.status()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn delivery_errors_identify_timeout_connection_and_status_without_secrets() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let router = axum::Router::new()
            .route(
                "/secret-timeout",
                axum::routing::post(|| async {
                    std::future::pending::<()>().await;
                    "never"
                }),
            )
            .route(
                "/secret-status",
                axum::routing::post(|| async {
                    (
                        axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                        "secret-response-body",
                    )
                }),
            );
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let short = reqwest::Client::builder()
            .timeout(std::time::Duration::from_millis(100))
            .build()
            .unwrap();
        let timeout = format!("http://{address}/secret-timeout");
        assert_eq!(
            send(&short, &timeout, "notice").await.unwrap_err(),
            "the webhook request timed out"
        );
        let status = format!("http://{address}/secret-status");
        let error = send(&client(), &status, "notice").await.unwrap_err();
        assert!(error.contains("500"), "{error}");
        assert!(!error.contains("secret"), "{error}");
        server.abort();
        let _ = server.await;
        assert_eq!(
            send(&client(), &status, "notice").await.unwrap_err(),
            "could not connect to the webhook"
        );
    }
}
