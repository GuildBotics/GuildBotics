//! Small helpers shared by every module: names, time, hashing, randomness.

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use sha2::{Digest, Sha256};

use crate::error::AppError;

/// Current time as RFC 3339 UTC with millisecond precision and a `Z` suffix.
pub fn now() -> String {
    format_time(chrono::Utc::now())
}

pub fn format_time(t: chrono::DateTime<chrono::Utc>) -> String {
    t.format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

/// Parses a client timestamp and normalizes it to the stored form.
pub fn parse_time(value: &str) -> Result<String, AppError> {
    let parsed = chrono::DateTime::parse_from_rfc3339(value)
        .map_err(|_| AppError::invalid(format!("'{value}' is not an RFC 3339 timestamp.")))?;
    Ok(format_time(parsed.with_timezone(&chrono::Utc)))
}

/// Case folding used for every comparison of names and paths.
pub fn fold(value: &str) -> String {
    value.to_lowercase()
}

/// Validates one name: `[A-Za-z0-9_.-]{1,64}`, not `.`/`..`/`-`, not ending in `.git`.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'.' || b == b'-')
        && name != "."
        && name != ".."
        // `-` separates an item's path from its kind in web URLs (`/-/pulls/3`).
        && name != "-"
        && !fold(name).ends_with(".git")
}

/// Validates a repository path: `<workspace>/<name>`, two names.
pub fn validate_path(path: &str) -> Result<(), AppError> {
    match path.split_once('/') {
        Some((ws, name)) if valid_name(ws) && valid_name(name) => Ok(()),
        _ => Err(AppError::invalid(format!(
            "'{path}' is not a valid repository path (<workspace>/<name>)."
        ))),
    }
}

pub fn validate_member_name(name: &str) -> Result<(), AppError> {
    if !valid_name(name) {
        return Err(AppError::invalid(format!(
            "'{name}' is not a valid member name."
        )));
    }
    Ok(())
}

pub fn sha256(data: &[u8]) -> [u8; 32] {
    Sha256::digest(data).into()
}

pub fn hex(data: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(data.len() * 2);
    for b in data {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 15) as usize] as char);
    }
    out
}

/// HMAC-SHA256 (RFC 2104).
pub fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    const BLOCK: usize = 64;
    let mut block_key = [0u8; BLOCK];
    if key.len() > BLOCK {
        block_key[..32].copy_from_slice(&sha256(key));
    } else {
        block_key[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block_key.map(|b| b ^ 0x36));
    inner.update(message);
    let inner_hash = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(block_key.map(|b| b ^ 0x5c));
    outer.update(inner_hash);
    outer.finalize().into()
}

pub fn random_bytes<const N: usize>() -> [u8; N] {
    let mut buf = [0u8; N];
    rand::fill(&mut buf);
    buf
}

/// A random URL-safe secret of `n` bytes of entropy.
pub fn random_secret<const N: usize>() -> String {
    URL_SAFE_NO_PAD.encode(random_bytes::<N>())
}

pub fn b64(data: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(data)
}

pub fn unb64(data: &str) -> Option<Vec<u8>> {
    URL_SAFE_NO_PAD.decode(data).ok()
}

/// A new ULID: the current time and 80 random bits.
pub fn ulid() -> ulid::Ulid {
    let ms = chrono::Utc::now().timestamp_millis().max(0) as u64;
    let random = u128::from_be_bytes(random_bytes::<16>()) >> 48;
    ulid::Ulid::from_parts(ms, random)
}

/// A new opaque id with a type prefix (`cmt_01J...`).
pub fn new_id(prefix: &str) -> String {
    format!("{prefix}_{}", ulid())
}

/// True when `value` is a full 40-hex object name.
pub fn is_sha(value: &str) -> bool {
    value.len() == 40 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Percent-encodes a path parameter as one segment (`acme/demo` → `acme%2Fdemo`).
pub fn encode_segment(value: &str) -> String {
    percent_encoding::utf8_percent_encode(value, percent_encoding::NON_ALPHANUMERIC).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_follow_the_contract() {
        assert!(valid_name("demo"));
        assert!(valid_name("a.b-c_d"));
        assert!(!valid_name(""));
        assert!(!valid_name("."));
        assert!(!valid_name(".."));
        assert!(!valid_name("demo.git"));
        assert!(!valid_name("-"));
        assert!(!valid_name("demo.GIT"));
        assert!(!valid_name("a b"));
        assert!(!valid_name(&"x".repeat(65)));
        assert!(validate_path("acme/demo").is_ok());
        assert!(validate_path("acme/team/demo").is_err());
        assert!(validate_path("demo").is_err());
        assert!(validate_path("acme//demo").is_err());
        assert!(validate_path("acme/../demo").is_err());
    }

    #[test]
    fn hmac_matches_rfc4231_case_2() {
        let mac = hmac_sha256(b"Jefe", b"what do ya want for nothing?");
        assert_eq!(
            hex(&mac),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    #[test]
    fn times_are_normalized() {
        assert_eq!(
            parse_time("2026-10-14T19:41:20+09:00").unwrap(),
            "2026-10-14T10:41:20.000Z"
        );
        assert!(parse_time("yesterday").is_err());
    }
}
