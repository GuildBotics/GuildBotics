//! Authentication framing shared by the client and server.
use std::path::{Path, PathBuf};
pub(crate) const OWNER_TOKEN: &str = "owner.token";
pub(crate) const TOKEN_PREFIX: &str = "gbt";
/// What the holder of a token's digest answers to a nonce.
pub(crate) fn proof(digest: &str, token_id: &str, nonce: &str) -> String {
    let message = format!("guildbotics-local-api-proof\n{nonce}\n{token_id}");
    loopback_auth::hex(&loopback_auth::hmac_sha256(
        digest.as_bytes(),
        message.as_bytes(),
    ))
}

/// The id and the secret of a token text.
pub(crate) fn parse_token(text: &str) -> Option<(&str, &str)> {
    let rest = text.trim().strip_prefix(TOKEN_PREFIX)?.strip_prefix('_')?;
    let (id, secret) = rest.split_once('_')?;
    (id.len() == 16
        && id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        && !secret.is_empty())
    .then_some((id, secret))
}

/// Where the credentials of the server on `port` are kept, under `data`.
pub(crate) fn directory(data: &Path, port: u16) -> PathBuf {
    data.join("server").join(port.to_string())
}
