//! This machine's command line as a client of the server on its port, as the
//! Python version's (`guildbotics/utils/local_api.py`) is with the owner
//! token file: whatever listens on the port first sees only the token's id
//! and a fresh nonce, and the token is sent only once the answer proves that
//! the listener knows the token's digest.

use std::time::Duration;

use serde_json::{Value, json};

use crate::{auth, i18n};

const PROOF_TIMEOUT: Duration = Duration::from_secs(2);
const TIMEOUT: Duration = Duration::from_secs(10);
/// The command line's sentences: Python's, which has no language set here.
const LANGUAGE: &str = "en";

/// A single-use link into the screens of the server on this machine's port,
/// or the sentence saying why there is none.
pub async fn entry_link() -> Result<String, String> {
    let port = crate::server::port().map_err(|e| e.to_string())?;
    let data = gb_base::home::data().map_err(|e| format!("{e:#}"))?;
    let base = format!("http://127.0.0.1:{port}/api");
    // Answered by the listener on the port itself, never by one it names.
    let http = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(TIMEOUT)
        .build()
        .map_err(|e| e.to_string())?;
    let token = std::fs::read_to_string(auth::directory(&data, port).join(auth::OWNER_TOKEN))
        .unwrap_or_default();
    let unreachable = || i18n::t(LANGUAGE, "cli.server.unreachable", &[]);
    let Some((id, secret)) = auth::parse_token(&token) else {
        return Err(unreachable());
    };
    if !proves(&http, &base, id, &loopback_auth::digest(secret)).await {
        return Err(unreachable());
    }
    let refused = |reason: String| i18n::t(LANGUAGE, "cli.server.refused", &[("reason", &reason)]);
    let answer: Value = http
        .post(format!("{base}/session/links"))
        .bearer_auth(token.trim())
        .header("x-guildbotics-language", LANGUAGE)
        .json(&json!({}))
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
        .map_err(|e| refused(e.to_string()))?
        .json()
        .await
        .map_err(|e| refused(e.to_string()))?;
    answer["url"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| refused(format!("no link in {answer}")))
}

/// Whether the listener on the port answers a fresh nonce as only the holder
/// of the token's digest can.
async fn proves(http: &reqwest::Client, base: &str, id: &str, digest: &str) -> bool {
    let nonce = loopback_auth::hex(&loopback_auth::random_bytes::<32>());
    let answer = http
        .post(format!("{base}/local-api/proof"))
        .timeout(PROOF_TIMEOUT)
        .json(&json!({"token_id": id, "nonce": nonce}))
        .send()
        .await
        .and_then(reqwest::Response::error_for_status);
    let Ok(answer) = answer else {
        return false;
    };
    let proof = answer.json::<Value>().await.unwrap_or_default();
    proof["proof"].as_str() == Some(&auth::proof(digest, id, &nonce))
}
