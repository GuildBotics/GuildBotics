//! The checks every request passes before anything runs for it: the `Host`
//! is the service's (against DNS rebinding) and an `Origin` is its own
//! (against cross-site requests); and the security headers of every response.

use http::header::{
    CONTENT_SECURITY_POLICY, HOST, HeaderName, ORIGIN, REFERRER_POLICY, X_CONTENT_TYPE_OPTIONS,
    X_FRAME_OPTIONS,
};
use http::{HeaderMap, HeaderValue, StatusCode};

/// Why a request was refused before anything ran for it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The `Host` is not the service's.
    Misdirected,
    /// The `Origin` is another site's.
    CrossOrigin,
}

impl Refusal {
    pub fn status(self) -> StatusCode {
        match self {
            Refusal::Misdirected => StatusCode::MISDIRECTED_REQUEST,
            Refusal::CrossOrigin => StatusCode::FORBIDDEN,
        }
    }
}

/// Validates a base URL: `http(s)://host:port` with nothing after it, which
/// is the form the checks below read.
pub fn normalize_base_url(value: &str) -> Result<String, &'static str> {
    let trimmed = value.trim_end_matches('/');
    let rest = trimmed
        .strip_prefix("http://")
        .or_else(|| trimmed.strip_prefix("https://"))
        .ok_or("base_url must start with http:// or https://")?;
    if rest.is_empty()
        || rest.contains('/')
        || rest.contains('?')
        || rest.contains('#')
        || rest.contains('@')
    {
        return Err("base_url must be scheme://host:port with no path, query, or credentials");
    }
    Ok(trimmed.to_string())
}

/// Whether a request's headers are for the service at `base_url` (as
/// `normalize_base_url` returns it) and from its own origin, when they name
/// one.
pub fn check_request(base_url: &str, headers: &HeaderMap) -> Result<(), Refusal> {
    let host = headers
        .get(HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_ascii_lowercase();
    if !allowed_host(base_url, &host) {
        return Err(Refusal::Misdirected);
    }
    if let Some(origin) = headers.get(ORIGIN) {
        let scheme = base_url.split("://").next().unwrap_or("http");
        let expected = format!("{scheme}://{host}");
        if origin.to_str().map(|o| o.to_ascii_lowercase()).ok() != Some(expected) {
            return Err(Refusal::CrossOrigin);
        }
    }
    Ok(())
}

/// The base URL's host and port, or a loopback name with that port.
/// `base_url` is as `normalize_base_url` returns it.
pub fn allowed_host(base_url: &str, host: &str) -> bool {
    let (scheme, authority) = base_url.split_once("://").unwrap_or(("http", base_url));
    let default_port = if scheme == "https" { "443" } else { "80" };
    let (base_host, port) = split_host_port(&authority.to_ascii_lowercase(), default_port);
    let (req_host, req_port) = split_host_port(host, default_port);
    req_port == port
        && [base_host.as_str(), "localhost", "127.0.0.1", "[::1]"].contains(&req_host.as_str())
}

/// `host:port` → (host, port); an IPv6 host keeps its brackets.
fn split_host_port(value: &str, default_port: &str) -> (String, String) {
    match value.rsplit_once(':') {
        Some((h, p)) if !p.contains(']') && (!h.contains(':') || h.ends_with(']')) => {
            (h.to_string(), p.to_string())
        }
        _ => (value.to_string(), default_port.to_string()),
    }
}

/// Gives a response the security headers it does not set itself, with `csp`
/// as its content security policy: a page may narrow the policy for what it
/// does.
pub fn secure(headers: &mut HeaderMap, csp: &'static str) {
    let defaults: [(HeaderName, &'static str); 4] = [
        (X_CONTENT_TYPE_OPTIONS, "nosniff"),
        (REFERRER_POLICY, "no-referrer"),
        (X_FRAME_OPTIONS, "DENY"),
        (CONTENT_SECURITY_POLICY, csp),
    ];
    for (name, value) in defaults {
        headers
            .entry(name)
            .or_insert(HeaderValue::from_static(value));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base_url_is_scheme_host_port_only() {
        assert_eq!(
            normalize_base_url("http://127.0.0.1:7878/").unwrap(),
            "http://127.0.0.1:7878"
        );
        assert!(normalize_base_url("http://127.0.0.1:7878/x").is_err());
        assert!(normalize_base_url("ftp://x").is_err());
        assert!(normalize_base_url("http://u:p@x:1").is_err());
    }

    #[test]
    fn hosts_are_the_base_url_or_loopback_with_its_port() {
        let base = "http://127.0.0.1:7878";
        assert!(allowed_host(base, "127.0.0.1:7878"));
        assert!(allowed_host(base, "localhost:7878"));
        assert!(allowed_host(base, "[::1]:7878"));
        assert!(!allowed_host(base, "127.0.0.1:7879"));
        assert!(!allowed_host(base, "evil.example:7878"));
        assert!(!allowed_host(base, "127.0.0.1"));
        assert!(!allowed_host(base, ""));
        assert!(allowed_host("http://branvil.test:80", "branvil.test"));
        assert!(allowed_host("http://branvil.test", "branvil.test:80"));
        assert!(allowed_host("http://[::1]:7878", "localhost:7878"));
        assert!(!allowed_host("http://[::1]", "[::1]:7878"));
    }

    fn headers(pairs: &[(HeaderName, &str)]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        for (name, value) in pairs {
            headers.insert(name, value.parse().unwrap());
        }
        headers
    }

    #[test]
    fn requests_must_name_the_host_and_may_name_only_its_origin() {
        let base = "http://127.0.0.1:7878";
        let ok = |pairs: &[(HeaderName, &str)]| check_request(base, &headers(pairs));
        assert_eq!(ok(&[(HOST, "LOCALHOST:7878")]), Ok(()));
        assert_eq!(
            ok(&[(HOST, "localhost:7878"), (ORIGIN, "http://LOCALHOST:7878")]),
            Ok(())
        );
        assert_eq!(ok(&[]), Err(Refusal::Misdirected));
        assert_eq!(
            ok(&[(HOST, "evil.example:7878")]),
            Err(Refusal::Misdirected)
        );
        for origin in [
            "http://127.0.0.1:7878",
            "https://localhost:7878",
            "http://evil.example",
            "null",
        ] {
            assert_eq!(
                ok(&[(HOST, "localhost:7878"), (ORIGIN, origin)]),
                Err(Refusal::CrossOrigin),
                "{origin}"
            );
        }
        assert_eq!(
            Refusal::Misdirected.status(),
            StatusCode::MISDIRECTED_REQUEST
        );
        assert_eq!(Refusal::CrossOrigin.status(), StatusCode::FORBIDDEN);
    }

    #[test]
    fn responses_keep_their_own_policy_and_get_the_others() {
        let mut headers = headers(&[(CONTENT_SECURITY_POLICY, "default-src 'none'")]);
        secure(&mut headers, "default-src 'self'");
        assert_eq!(headers[CONTENT_SECURITY_POLICY], "default-src 'none'");
        assert_eq!(headers[X_CONTENT_TYPE_OPTIONS], "nosniff");
        assert_eq!(headers[REFERRER_POLICY], "no-referrer");
        assert_eq!(headers[X_FRAME_OPTIONS], "DENY");
        let mut empty = HeaderMap::new();
        secure(&mut empty, "default-src 'self'");
        assert_eq!(empty[CONTENT_SECURITY_POLICY], "default-src 'self'");
    }
}
