use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::{
    Router,
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::get,
};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::oneshot;

use crate::i18n;

const SERVICE: &str = "guildbotics-server";
const WAIT_FOR_CONSENT: Duration = Duration::from_secs(600);

pub(super) fn credential(port: u16) -> Option<String> {
    gb_config::secrets::get_os_credential(SERVICE, &format!("127.0.0.1:{port}"))
        .ok()
        .flatten()
}

pub async fn run(open: impl FnOnce(&str)) -> anyhow::Result<()> {
    let language = super::language().await;
    let port = crate::server::port()?;
    let device = gb_workspace::identity::ensure_device_identity(&gb_base::home::data()?)?;
    let name = client_name(&device.display_name);
    let token = connect_to(port, &name, language, open).await?;
    gb_config::secrets::keep_os_credential(SERVICE, &format!("127.0.0.1:{port}"), &token)?;
    println!("{}", i18n::t(language, "cli.server.connected", &[]));
    Ok(())
}

async fn connect_to(
    port: u16,
    name: &str,
    language: &'static str,
    open: impl FnOnce(&str),
) -> anyhow::Result<String> {
    let text = |key| i18n::t(language, key, &[]);
    let base = format!("http://127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await?;
    let redirect = format!(
        "http://127.0.0.1:{}/callback",
        listener.local_addr()?.port()
    );
    let verifier = loopback_auth::random_secret::<48>();
    let challenge = loopback_auth::b64(&loopback_auth::sha256(verifier.as_bytes()));
    let state = loopback_auth::random_secret::<32>();
    let mut url = url::Url::parse(&format!("{base}/-/authorize"))?;
    url.query_pairs_mut()
        .append_pair("client", name)
        .append_pair("redirect_uri", &redirect)
        .append_pair("state", &state)
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256");

    let (send, received) = oneshot::channel();
    let router = Router::new()
        .route("/callback", get(answer))
        .with_state(Arc::new(Callback {
            state,
            language,
            send: Mutex::new(Some(send)),
        }));
    let (stop, stopping) = oneshot::channel::<()>();
    let server = tokio::spawn(async move {
        axum::serve(listener, router)
            .with_graceful_shutdown(async {
                let _ = stopping.await;
            })
            .await
    });
    println!(
        "{}",
        i18n::t(language, "cli.server.consent", &[("url", url.as_str())])
    );
    open(url.as_str());
    let answer = tokio::time::timeout(WAIT_FOR_CONSENT, received).await;
    let _ = stop.send(());
    let abort = server.abort_handle();
    if tokio::time::timeout(super::PROOF_TIMEOUT, server)
        .await
        .is_err()
    {
        abort.abort();
    }
    let code = answer
        .ok()
        .and_then(Result::ok)
        .ok_or_else(|| anyhow::anyhow!(text("cli.server.consent_timeout")))?
        .ok_or_else(|| anyhow::anyhow!(text("cli.server.denied")))?;
    let call = super::http()?
        .post(format!("{base}/api/apps/token"))
        .timeout(super::TIMEOUT)
        .header("x-guildbotics-language", language)
        .json(&json!({"code":code, "redirect_uri":redirect, "code_verifier":verifier}));
    async {
        let reply: Value = super::checked(call.send().await?).await?.json().await?;
        reply["token"]
            .as_str()
            .filter(|token| crate::auth::parse_token(token).is_some())
            .map(str::to_owned)
            .ok_or_else(|| anyhow::anyhow!(text("cli.server.invalid_token")))
    }
    .await
    .map_err(|error| {
        anyhow::anyhow!(i18n::t(
            language,
            "cli.server.refused",
            &[("reason", &error.to_string())],
        ))
    })
}

fn client_name(machine: &str) -> String {
    let host = machine
        .split('.')
        .next()
        .unwrap_or_default()
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || " ._-".contains(c) {
                c
            } else {
                '-'
            }
        })
        .collect::<String>();
    format!("GuildBotics CLI {host}")
        .chars()
        .take(64)
        .collect::<String>()
        .trim()
        .into()
}

struct Callback {
    state: String,
    language: &'static str,
    send: Mutex<Option<oneshot::Sender<Option<String>>>>,
}

#[derive(Deserialize)]
struct Answer {
    state: String,
    code: Option<String>,
}

async fn answer(State(callback): State<Arc<Callback>>, Query(answer): Query<Answer>) -> Response {
    if !loopback_auth::csrf_matches(&answer.state, &callback.state) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Some(send) = callback.send.lock().unwrap().take() else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let _ = send.send(answer.code);
    (
        [
            ("cache-control", "no-store"),
            ("referrer-policy", "no-referrer"),
        ],
        i18n::t(callback.language, "cli.server.callback_received", &[]),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Json, routing::post};
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn the_machine_name_fits_the_consent_rules() {
        for name in ["pc.local", "作業端末/DEV", &"a".repeat(100)] {
            let name = client_name(name);
            assert!(loopback_auth::valid_client(&name));
            assert!(name.starts_with("GuildBotics CLI"));
        }
    }

    #[tokio::test]
    async fn callback_state_and_consent_gate_the_token_exchange() {
        let exchanged = Arc::new(AtomicUsize::new(0));
        let router = Router::new().route("/api/apps/token", post(
            |State(exchanged): State<Arc<AtomicUsize>>, Json(body): Json<Value>| async move {
                exchanged.fetch_add(1, Ordering::SeqCst);
                assert!(matches!(body["code"].as_str(), Some("server-code" | "refused-code")));
                assert!(body["redirect_uri"].as_str().unwrap().ends_with("/callback"));
                assert_eq!(body["code_verifier"].as_str().unwrap().len(), 64);
                if body["code"] == "refused-code" {
                    (StatusCode::BAD_REQUEST, Json(json!({"message":"fixture refused"})))
                } else {
                    (StatusCode::OK, Json(json!({"token":"gbt_0123456789abcdef_fixture"})))
                }
            },
        )).with_state(exchanged.clone());
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        for code in [Some("server-code"), None, Some("refused-code")] {
            let result = connect_to(port, "Fixture", "en", |url| {
                let url = url::Url::parse(url).unwrap();
                assert_eq!(url.path(), "/-/authorize");
                let params = url
                    .query_pairs()
                    .collect::<std::collections::BTreeMap<_, _>>();
                assert_eq!(params["code_challenge_method"], "S256");
                let callback = url::Url::parse(params["redirect_uri"].as_ref()).unwrap();
                let state = params["state"].to_string();
                tokio::spawn(async move {
                    let http = super::super::http().unwrap();
                    let mut false_callback = callback.clone();
                    false_callback
                        .query_pairs_mut()
                        .append_pair("state", "other");
                    let refused = http.get(false_callback).send().await.unwrap();
                    assert_eq!(refused.status(), StatusCode::NOT_FOUND);
                    let mut returned = callback;
                    returned.query_pairs_mut().append_pair("state", &state);
                    if let Some(code) = code {
                        returned.query_pairs_mut().append_pair("code", code);
                    }
                    let accepted = http.get(returned).send().await.unwrap();
                    assert_eq!(accepted.status(), StatusCode::OK);
                });
            })
            .await;
            match code {
                Some("server-code") => assert_eq!(result.unwrap(), "gbt_0123456789abcdef_fixture"),
                None => assert!(result.unwrap_err().to_string().contains("did not allow")),
                _ => assert_eq!(
                    result.unwrap_err().to_string(),
                    i18n::t("en", "cli.server.refused", &[("reason", "fixture refused")]),
                ),
            }
        }
        assert_eq!(exchanged.load(Ordering::SeqCst), 2);
        server.abort();
    }
}
