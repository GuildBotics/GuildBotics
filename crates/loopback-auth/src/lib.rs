//! The web authentication of a single-owner service on a loopback address,
//! shared by Branvil and GuildBotics, which keep their own records of tokens
//! and sessions: whoever can read the owner's token file on the machine is the
//! owner, a browser gets its session cookie from a single-use entry link, and
//! an app on another machine gets a token through a consent and PKCE.
//!
//! This crate knows neither service: Branvil depends on it, never on
//! GuildBotics.

mod codes;
mod consent;
mod origin;
mod secret;
mod session;

pub use codes::{CODE_SECONDS, Codes};
pub use consent::{loopback_redirect, valid_challenge, valid_client, verifier_matches};
pub use origin::{Refusal, allowed_host, check_request, normalize_base_url, secure};
pub use secret::{
    b64, digest, digest_matches, hex, hmac_sha256, random_bytes, random_secret, sha256, unb64,
};
pub use session::{cookie_value, csrf_matches, session_set_cookie};

/// Shared browser policy for the host and embedded code service.
pub const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";
