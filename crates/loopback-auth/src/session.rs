//! A browser's session: its cookie, and the CSRF secret every write sends.

use http::HeaderMap;
use http::header::COOKIE;
use subtle::ConstantTimeEq;

/// The `Set-Cookie` value of a session: never read by scripts, and never sent
/// by a request another site starts.
pub fn session_set_cookie(name: &str, value: &str, max_age_secs: i64) -> String {
    format!("{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age_secs}")
}

/// The value of the cookie `name`, from any `Cookie` header.
pub fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    for value in headers.get_all(COOKIE) {
        let Ok(text) = value.to_str() else { continue };
        for part in text.split(';') {
            if let Some((k, v)) = part.trim().split_once('=')
                && k == name
            {
                return Some(v.to_string());
            }
        }
    }
    None
}

/// Whether the CSRF secret a request sent is its session's, compared in
/// constant time.
pub fn csrf_matches(sent: &str, session: &str) -> bool {
    sent.as_bytes().ct_eq(session.as_bytes()).into()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cookies_are_found_by_exact_name_in_any_header() {
        let mut headers = HeaderMap::new();
        headers.append(COOKIE, "a_session=1; other=2".parse().unwrap());
        headers.append(COOKIE, "session=3".parse().unwrap());
        assert_eq!(cookie_value(&headers, "session").as_deref(), Some("3"));
        assert_eq!(cookie_value(&headers, "other").as_deref(), Some("2"));
        assert_eq!(cookie_value(&headers, "missing"), None);
    }

    #[test]
    fn session_cookies_are_strict_and_http_only() {
        assert_eq!(
            session_set_cookie("s", "v", 60),
            "s=v; Path=/; HttpOnly; SameSite=Strict; Max-Age=60"
        );
        assert!(csrf_matches("x", "x"));
        assert!(!csrf_matches("x", "y"));
        assert!(!csrf_matches("", "y"));
    }
}
