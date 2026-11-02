//! The owner's webhook, usable without opening the database or the service.

use crate::config::Layout;

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(concat!("branvil/", env!("CARGO_PKG_VERSION")))
        .build()
        .expect("an HTTP client")
}

/// True only after delivery; an unset webhook is a successful no-op.
/// Neither errors nor response bodies may expose the credential in the URL.
pub async fn send(client: &reqwest::Client, layout: &Layout, text: &str) -> Result<bool, String> {
    let url = match std::fs::read_to_string(layout.webhook()) {
        Ok(url) => url,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(_) => return Err("cannot read the webhook credential".into()),
    };
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
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn delivery_errors_identify_timeout_connection_and_status_without_secrets() {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        layout.create().unwrap();
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
        std::fs::write(layout.webhook(), format!("http://{address}/secret-timeout")).unwrap();
        assert_eq!(
            send(&short, &layout, "notice").await.unwrap_err(),
            "the webhook request timed out"
        );
        std::fs::write(layout.webhook(), format!("http://{address}/secret-status")).unwrap();
        let error = send(&client(), &layout, "notice").await.unwrap_err();
        assert!(error.contains("500"), "{error}");
        assert!(!error.contains("secret"), "{error}");
        server.abort();
        let _ = server.await;
        assert_eq!(
            send(&client(), &layout, "notice").await.unwrap_err(),
            "could not connect to the webhook"
        );
    }
}
