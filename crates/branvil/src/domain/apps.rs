//! Local apps: the consent exchange through which an app on a machine that
//! reaches Branvil gets an owner token without copying `owner.token`. The
//! owner allows the app on the GUI's `/-/authorize` page, Branvil redirects to
//! the app's loopback address with a single-use code, and the app exchanges
//! the code and its PKCE verifier for a token labelled `app:<client>`.

use std::time::Instant;

use loopback_auth::{loopback_redirect, valid_challenge, valid_client, verifier_matches};
use serde::{Deserialize, Serialize};

use crate::app::App;
use crate::auth::{self, Caller};
use crate::domain::members::IssuedToken;
use crate::error::{AppError, AppResult};

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
    if !valid_challenge(&input.code_challenge) {
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
