//! The consent through which an app on another machine gets a token of the
//! owner: the owner allows the app, the browser goes back to the app's
//! loopback address with a single-use code, and the app exchanges the code
//! and its PKCE verifier for the token.

use subtle::ConstantTimeEq;
use url::Url;

use crate::secret::{b64, sha256, unb64};

/// A display name: `[A-Za-z0-9 ._-]{1,64}`, not blank.
pub fn valid_client(client: &str) -> bool {
    (1..=64).contains(&client.len())
        && !client.trim().is_empty()
        && client
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b" ._-".contains(&b))
}

/// An `http` address on this machine's loopback, without userinfo or
/// fragment, so the code never leaves the browser's machine.
pub fn loopback_redirect(uri: &str) -> Option<Url> {
    let url = Url::parse(uri).ok()?;
    // The parsed host is normalized (`LOCALHOST`, `127.1`, `[0::1]`).
    (url.scheme() == "http"
        && matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"))
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none())
    .then_some(url)
}

/// Whether `challenge` can be an S256 challenge: a base64url (unpadded)
/// SHA-256.
pub fn valid_challenge(challenge: &str) -> bool {
    unb64(challenge).is_some_and(|c| c.len() == 32)
}

/// RFC 7636 S256: the challenge is the base64url SHA-256 of a 43-128
/// character verifier.
pub fn verifier_matches(verifier: &str, challenge: &str) -> bool {
    (43..=128).contains(&verifier.len())
        && bool::from(
            b64(&sha256(verifier.as_bytes()))
                .as_bytes()
                .ct_eq(challenge.as_bytes()),
        )
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
        assert!(valid_challenge(challenge));
        assert!(verifier_matches(verifier, challenge));
        assert!(!verifier_matches(&format!("{verifier}x"), challenge));
        assert!(!verifier_matches("short", &b64(&sha256(b"short"))));
        assert!(!valid_challenge(&b64(&[0; 31])));
        assert!(!valid_challenge("not base64!"));
    }
}
