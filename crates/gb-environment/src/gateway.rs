//! Per-command credential gateway policy. Real tokens stay in the host.
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Provider {
    OpenAi,
    Anthropic,
    Gemini,
    Jev,
}
impl Provider {
    pub fn credential_header(self) -> &'static str {
        match self {
            Self::Anthropic => "x-api-key",
            Self::Gemini => "x-goog-api-key",
            _ => "authorization",
        }
    }
}

/// Origins and permitted route prefixes come from the resolved catalog,
/// never from the guest request. Prefixes end at path-component boundaries.
#[derive(Clone)]
pub struct Policy {
    pub provider: Provider,
    pub origin: String,
    pub routes: Vec<(String, String)>,
}
impl Policy {
    pub fn target(&self, method: &str, path_and_query: &str) -> Result<String, &'static str> {
        let path = path_and_query.split('?').next().unwrap_or("");
        if !path.starts_with('/')
            || path.starts_with("//")
            || path.contains("..")
            || path.contains('%')
            || path.contains('#')
            || path.contains('\\')
        {
            return Err("Not a forwarded route.");
        }
        for (verb, route) in &self.routes {
            if verb != method {
                continue;
            }
            let (origin, configured) =
                if route.starts_with("https://") || route.starts_with("http://") {
                    let url = url::Url::parse(route).map_err(|_| "Invalid provider route.")?;
                    (url.origin().ascii_serialization(), url.path().to_owned())
                } else {
                    (self.origin.clone(), route.clone())
                };
            let covers = if let Some(prefix) = configured.strip_suffix("/*") {
                path.strip_prefix(&format!("{prefix}/"))
                    .is_some_and(|suffix| {
                        suffix.split('/').all(|segment| {
                            !segment.is_empty()
                                && segment
                                    .bytes()
                                    .all(|b| b.is_ascii_alphanumeric() || b"._~@:+-".contains(&b))
                        })
                    })
            } else {
                path == configured
            };
            if !covers {
                continue;
            }
            let parsed = url::Url::parse(&origin).map_err(|_| "Invalid provider origin.")?;
            if !matches!(parsed.scheme(), "http" | "https")
                || parsed.host_str().is_none()
                || parsed.query().is_some()
                || parsed.fragment().is_some()
                || !parsed.username().is_empty()
                || parsed.password().is_some()
                || parsed.path() != "/"
            {
                return Err("Invalid provider origin.");
            }
            return Ok(format!("{}{path_and_query}", origin.trim_end_matches('/')));
        }
        Err("Not a forwarded route.")
    }
    pub fn headers(
        &self,
        supplied: &BTreeMap<String, String>,
        token: &str,
    ) -> BTreeMap<String, String> {
        let mut headers = supplied
            .iter()
            .filter(|(name, _)| !dropped(name))
            .map(|(k, v)| (k.to_ascii_lowercase(), v.clone()))
            .collect::<BTreeMap<_, _>>();
        // Connection may name additional hop-by-hop headers.
        for name in supplied
            .iter()
            .filter(|(name, _)| name.eq_ignore_ascii_case("connection"))
            .flat_map(|(_, v)| v.split(','))
        {
            headers.remove(&name.trim().to_ascii_lowercase());
        }
        headers.insert("accept-encoding".into(), "identity".into());
        headers.insert(
            self.provider.credential_header().into(),
            match self.provider {
                Provider::OpenAi | Provider::Jev => format!("Bearer {token}"),
                _ => token.into(),
            },
        );
        headers
    }
}
fn dropped(name: &str) -> bool {
    matches!(
        name.to_ascii_lowercase().as_str(),
        "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "proxy-connection"
            | "te"
            | "trailer"
            | "transfer-encoding"
            | "upgrade"
            | "host"
            | "authorization"
            | "x-api-key"
            | "x-goog-api-key"
            | "cookie"
            | "content-length"
            | "accept-encoding"
    )
}

/// An accepted request keeps its generation; replacing the lease cannot
/// lend a late body/token/answer to the next turn.
#[derive(Clone, Default)]
pub struct Leases {
    state: Arc<Mutex<Option<(u64, String)>>>,
    generation: Arc<AtomicU64>,
}
impl Leases {
    pub fn lend(&self) -> Lease {
        self.lend_token(uuid::Uuid::new_v4().simple().to_string())
    }
    pub fn lend_token(&self, stand_in: String) -> Lease {
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        *self.state.lock().expect("lease mutex poisoned") = Some((generation, stand_in.clone()));
        Lease {
            generation,
            stand_in,
            owner: self.clone(),
        }
    }
    pub fn authorize(&self, presented: &str) -> Option<u64> {
        self.state
            .lock()
            .expect("lease mutex poisoned")
            .as_ref()
            .and_then(|(id, token)| equal(token, presented).then_some(*id))
    }
    pub fn revoke(&self) {
        *self.state.lock().expect("lease mutex poisoned") = None;
    }
    pub fn accepts(&self, generation: u64, presented: &str) -> bool {
        self.state
            .lock()
            .expect("lease mutex poisoned")
            .as_ref()
            .is_some_and(|(id, token)| *id == generation && equal(token, presented))
    }
}
pub struct Lease {
    pub generation: u64,
    pub stand_in: String,
    owner: Leases,
}
impl Lease {
    pub fn active(&self) -> bool {
        self.owner.accepts(self.generation, &self.stand_in)
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        let mut state = self.owner.state.lock().expect("lease mutex poisoned");
        if state.as_ref().is_some_and(|(id, _)| *id == self.generation) {
            *state = None;
        }
    }
}
fn equal(a: &str, b: &str) -> bool {
    let mut difference = a.len() ^ b.len();
    for (a, b) in a.bytes().zip(b.bytes()) {
        difference |= usize::from(a ^ b);
    }
    difference == 0
}

/// Mask a streaming answer, retaining a possible token prefix across chunks.
/// Tokens are supplied by the trusted caller; no token values are logged.
pub struct Mask {
    secret: Vec<u8>,
    pending: Vec<u8>,
}
impl Mask {
    pub fn new(secret: &str) -> Self {
        Self {
            secret: secret.as_bytes().to_vec(),
            pending: vec![],
        }
    }
    pub fn feed(&mut self, chunk: &[u8], finish: bool) -> Vec<u8> {
        self.pending.extend_from_slice(chunk);
        let mut output = vec![];
        let mut offset = 0;
        while offset < self.pending.len() {
            let rest = &self.pending[offset..];
            if !self.secret.is_empty() && rest.starts_with(&self.secret) {
                output.extend(std::iter::repeat_n(b'*', self.secret.len()));
                offset += self.secret.len();
            } else if !finish && self.secret.starts_with(rest) {
                break;
            } else {
                output.push(rest[0]);
                offset += 1;
            }
        }
        self.pending.drain(..offset);
        output
    }
}
impl Drop for Mask {
    fn drop(&mut self) {
        self.secret.fill(0);
        self.pending.fill(0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn providers_strip_all_credentials_but_keep_anthropic_query_and_beta() {
        for provider in [
            Provider::OpenAi,
            Provider::Anthropic,
            Provider::Gemini,
            Provider::Jev,
        ] {
            let p = Policy {
                provider,
                origin: "https://example.test".into(),
                routes: vec![("POST".into(), "/v1/messages".into())],
            };
            assert_eq!(
                p.target("POST", "/v1/messages?beta=true").unwrap(),
                "https://example.test/v1/messages?beta=true"
            );
            for path in [
                "/v1/messages-evil",
                "/v1/messages/subroute",
                "//evil/v1/messages",
                "/v1/messages/../admin",
                "/v1/messages%2fadmin",
            ] {
                assert!(p.target("POST", path).is_err());
            }
            assert!(p.target("GET", "/v1/messages").is_err());
            let supplied = BTreeMap::from([
                ("Authorization".into(), "guest".into()),
                ("x-api-key".into(), "guest".into()),
                ("x-goog-api-key".into(), "guest".into()),
                ("anthropic-beta".into(), "beta".into()),
                ("Connection".into(), "x-extra".into()),
                ("x-extra".into(), "hop".into()),
            ]);
            let headers = p.headers(&supplied, "synthetic");
            assert_eq!(headers["anthropic-beta"], "beta");
            assert!(!headers.contains_key("x-extra"));
            assert!(!headers.values().any(|v| v == "guest"));
            assert!(headers.contains_key(provider.credential_header()));
        }
    }
    #[test]
    fn replacement_and_revoke_end_old_requests() {
        let leases = Leases::default();
        let first = leases.lend();
        let second = leases.lend();
        assert!(!first.active());
        drop(first);
        assert!(second.active());
        leases.revoke();
        assert!(!second.active());
    }
    #[test]
    fn echoes_are_masked_at_every_chunk_boundary() {
        let text = b"a synthetic-token z";
        for split in 0..=text.len() {
            let mut mask = Mask::new("synthetic-token");
            let mut actual = mask.feed(&text[..split], false);
            actual.extend(mask.feed(&text[split..], true));
            assert_eq!(actual, b"a *************** z");
        }
    }
    #[test]
    fn wildcard_routes_stay_under_their_prefix_and_explicit_origins_are_pinned() {
        let policy = Policy {
            provider: Provider::OpenAi,
            origin: "https://primary.example".into(),
            routes: vec![
                ("POST".into(), "/agents/*".into()),
                ("GET".into(), "https://profile.example/me".into()),
            ],
        };
        assert_eq!(
            policy.target("POST", "/agents/owner/repo?x=1").unwrap(),
            "https://primary.example/agents/owner/repo?x=1"
        );
        assert_eq!(
            policy.target("GET", "/me").unwrap(),
            "https://profile.example/me"
        );
        for path in [
            "/agents",
            "/agents/",
            "/agents//repo",
            "/agents/../admin",
            "/agents/%2e%2e/admin",
            "/agents/owner/../../admin",
        ] {
            assert!(policy.target("POST", path).is_err(), "{path}");
        }
    }
}
