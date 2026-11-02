//! Randomness, hashing, and their encodings.

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

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

/// What is kept of a token or a session id: the hex of its SHA-256.
pub fn digest(secret: &str) -> String {
    hex(&sha256(secret.as_bytes()))
}

/// Whether `secret` has the digest `kept`, compared in constant time.
pub fn digest_matches(secret: &str, kept: &str) -> bool {
    digest(secret).as_bytes().ct_eq(kept.as_bytes()).into()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digests_match_only_their_secret() {
        let kept = digest("secret");
        assert_eq!(
            kept,
            "2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b"
        );
        assert!(digest_matches("secret", &kept));
        assert!(!digest_matches("secret!", &kept));
        assert!(!digest_matches("secret", ""));
    }

    #[test]
    fn secrets_are_url_safe_and_unpadded() {
        let secret = random_secret::<32>();
        assert_eq!(secret.len(), 43);
        assert_eq!(unb64(&secret).map(|b| b.len()), Some(32));
        assert_ne!(secret, random_secret::<32>());
        assert_eq!(b64(&[0xfb, 0xff]), "-_8");
    }
}
