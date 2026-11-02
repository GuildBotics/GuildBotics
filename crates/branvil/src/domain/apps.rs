//! Local apps: the consent exchange through which an app on a machine that
//! reaches Branvil gets an owner token without copying `owner.token`. The
//! owner allows the app on the GUI's `/-/authorize` page, Branvil redirects to
//! the app's loopback address with a single-use code, and the app exchanges
//! the code and its PKCE verifier for a token labelled `app:<client>`.

use std::time::Instant;

use reqwest::Url;
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;

use crate::app::App;
use crate::auth::{self, Caller};
use crate::domain::members::IssuedToken;
use crate::error::{AppError, AppResult};
use crate::util;

/// What an unused code was issued for.
pub struct Grant {
    owner: String,
    client: String,
    redirect_uri: String,
    challenge: String,
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewAuthorization {
    pub client: String,
    pub redirect_uri: String,
    pub state: String,
    pub code_challenge: String,
}

#[derive(Serialize, Debug)]
pub struct Authorization {
    pub redirect: String,
}

/// A display name: `[A-Za-z0-9 ._-]{1,64}`, not blank.
fn valid_client(client: &str) -> bool {
    (1..=64).contains(&client.len())
        && !client.trim().is_empty()
        && client
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b" ._-".contains(&b))
}

/// An `http` address on this machine's loopback, without userinfo or fragment.
fn loopback_redirect(uri: &str) -> Option<Url> {
    let url = Url::parse(uri).ok()?;
    // The parsed host is normalized (`LOCALHOST`, `127.1`, `[0::1]`).
    (url.scheme() == "http"
        && matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"))
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none())
    .then_some(url)
}

/// The owner allows a local app: makes a code bound to the app's name,
/// redirect address, and PKCE challenge, and returns where to send the
/// browser with it.
pub fn authorize(app: &App, caller: &Caller, input: NewAuthorization) -> AppResult<Authorization> {
    caller.require_owner()?;
    if !valid_client(&input.client) {
        return Err(AppError::invalid(
            "client must be 1-64 letters, digits, spaces, '.', '_', or '-'.",
        ));
    }
    let mut redirect = loopback_redirect(&input.redirect_uri).ok_or_else(|| {
        AppError::invalid(
            "redirect_uri must be http://127.0.0.1, http://localhost, or http://[::1] (any port and path), without userinfo or fragment.",
        )
    })?;
    if !(1..=512).contains(&input.state.len()) {
        return Err(AppError::invalid("state must be 1-512 bytes."));
    }
    if util::unb64(&input.code_challenge).is_none_or(|c| c.len() != 32) {
        return Err(AppError::invalid(
            "code_challenge must be the base64url (unpadded) SHA-256 of the verifier.",
        ));
    }
    let code = app.app_codes.lock().unwrap().issue(
        Grant {
            owner: caller.name().to_string(),
            client: input.client,
            redirect_uri: input.redirect_uri,
            challenge: input.code_challenge,
        },
        Instant::now(),
    );
    redirect
        .query_pairs_mut()
        .append_pair("code", &code)
        .append_pair("state", &input.state);
    Ok(Authorization {
        redirect: redirect.into(),
    })
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct TokenRequest {
    pub code: String,
    pub redirect_uri: String,
    pub code_verifier: String,
}

#[derive(Serialize, Debug)]
pub struct AppToken {
    #[serde(flatten)]
    pub issued: IssuedToken,
    pub member: String,
}

/// RFC 7636 S256: the challenge is the base64url SHA-256 of a 43-128
/// character verifier.
fn verifier_matches(verifier: &str, challenge: &str) -> bool {
    (43..=128).contains(&verifier.len())
        && bool::from(
            util::b64(&util::sha256(verifier.as_bytes()))
                .as_bytes()
                .ct_eq(challenge.as_bytes()),
        )
}

/// Exchanges a code (once; it is spent even when the request does not
/// match it) for an owner token labelled `app:<client>`, replacing the
/// previous token of that label.
pub async fn exchange(app: &App, input: TokenRequest) -> AppResult<AppToken> {
    let grant = app
        .app_codes
        .lock()
        .unwrap()
        .take(&input.code, Instant::now())
        .filter(|g| {
            g.redirect_uri == input.redirect_uri
                && verifier_matches(&input.code_verifier, &g.challenge)
        })
        .ok_or_else(|| {
            AppError::invalid(
                "The code is used, expired, or unknown, or the redirect_uri or code_verifier does not match it.",
            )
        })?;
    let owner = grant.owner.clone();
    let label = format!("{}{}", auth::APP_LABEL_PREFIX, grant.client);
    let issued = app
        .store
        .write(move |w| auth::replace_token(w, &owner, &label))
        .await?;
    Ok(AppToken {
        issued: IssuedToken::new(issued),
        member: grant.owner,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clients_are_display_names() {
        for ok in ["GuildBotics Desktop", "app_1.2-x", "a"] {
            assert!(valid_client(ok), "{ok}");
        }
        for bad in ["", "   ", "a/b", "x:y", "日本", &"a".repeat(65)] {
            assert!(!valid_client(bad), "{bad}");
        }
    }

    #[test]
    fn redirects_are_loopback_http_only() {
        for ok in [
            "http://127.0.0.1:53682/callback",
            "http://localhost/cb?x=1",
            "http://[::1]:8080/",
        ] {
            assert!(loopback_redirect(ok).is_some(), "{ok}");
        }
        for bad in [
            "https://127.0.0.1/cb",
            "http://example.com/cb",
            "http://localhost.example.com/cb",
            "http://127.0.0.1.example.com/cb",
            "http://user@127.0.0.1/cb",
            "http://127.0.0.1@example.com/cb",
            "http://127.0.0.2/cb",
            "http://127.0.0.1/cb#frag",
            "file:///tmp/x",
            "/relative",
        ] {
            assert!(loopback_redirect(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn verifiers_match_their_s256_challenge() {
        // RFC 7636, appendix B.
        let verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
        let challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
        assert!(verifier_matches(verifier, challenge));
        assert!(!verifier_matches(&format!("{verifier}x"), challenge));
        assert!(!verifier_matches(
            "short",
            &util::b64(&util::sha256(b"short"))
        ));
    }
}
