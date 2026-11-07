//! Who may use the server, and how a browser comes to be one of them, as the
//! Python version (`guildbotics/app_api/auth.py`) does and in the same files:
//! whoever can read the owner token file is the owner and can ask for an
//! entry link, a single-use code that a browser exchanges for its session
//! cookie. Tokens and sessions are kept in `<data>/server/<port>/auth.json`,
//! only as digests; the single-use codes only in memory, so a restart spends
//! them.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use axum::http::{HeaderMap, Method, header::AUTHORIZATION};
use loopback_auth::{CODE_SECONDS, Codes};
use serde::{Deserialize, Serialize};

/// Followed by the port: a browser sends a cookie to every port of a host, so
/// one name would let the servers of two ports end each other's sessions,
/// as Branvil's does too.
const COOKIE_PREFIX: &str = "guildbotics_session";
pub const CSRF_HEADER: &str = "x-guildbotics-csrf";
pub const SESSION_SECONDS: i64 = 30 * 24 * 3600;
const OWNER_FILE: &str = "owner-file";
/// The owner token file, beside the records.
pub(crate) const OWNER_TOKEN: &str = "owner.token";
const TOKEN_PREFIX: &str = "gbt";

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
struct Token {
    label: String,
    digest: String,
    created_at: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
struct Session {
    csrf: String,
    expires_at: f64,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
#[serde(deny_unknown_fields)]
struct Records {
    #[serde(default)]
    tokens: BTreeMap<String, Token>,
    /// By the SHA-256 of the cookie value.
    #[serde(default)]
    sessions: BTreeMap<String, Session>,
}

/// Why a request was not let through.
#[derive(Debug, PartialEq, Eq)]
pub enum Denied {
    Unauthorized,
    CsrfMismatch,
}

#[derive(Serialize, Debug)]
pub struct EntryLink {
    pub url: String,
    pub expires_at: String,
}

struct State {
    records: Records,
    entries: Codes<()>,
    consents: Codes<Consent>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Authorization {
    client: String,
    redirect_uri: String,
    state: String,
    code_challenge: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Exchange {
    code: String,
    redirect_uri: String,
    code_verifier: String,
}

struct Consent {
    client: String,
    redirect_uri: String,
    challenge: String,
}

/// The server's credentials.
pub struct ServerAuth {
    base_url: String,
    /// The session cookie's name.
    pub cookie: String,
    directory: PathBuf,
    state: Mutex<State>,
}

impl ServerAuth {
    /// The credentials of the server on `port`, under `data`; the owner token
    /// file is made to hold a token that authenticates.
    pub fn open(data: &Path, port: u16) -> anyhow::Result<ServerAuth> {
        let directory = directory(data, port);
        create_private_dir(&directory)?;
        let records = std::fs::read(directory.join("auth.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default();
        let auth = ServerAuth {
            base_url: format!("http://127.0.0.1:{port}"),
            cookie: format!("{COOKIE_PREFIX}_{port}"),
            directory,
            state: Mutex::new(State {
                records,
                entries: Codes::default(),
                consents: Codes::default(),
            }),
        };
        auth.keep_owner_token()?;
        Ok(auth)
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn owner_token_path(&self) -> PathBuf {
        self.directory.join(OWNER_TOKEN)
    }

    /// Lets the owner through: a Bearer token, or the session cookie with that
    /// session's CSRF value on every write.
    pub fn authenticate(&self, method: &Method, headers: &HeaderMap) -> Result<(), Denied> {
        self.authenticate_for(method, headers, false)
    }

    pub(crate) fn authenticate_for(
        &self,
        method: &Method,
        headers: &HeaderMap,
        allow_apps: bool,
    ) -> Result<(), Denied> {
        if let Some(value) = headers.get(AUTHORIZATION) {
            let text = value.to_str().map_err(|_| Denied::Unauthorized)?;
            let (scheme, token) = text.split_once(' ').unwrap_or((text, ""));
            if !scheme.eq_ignore_ascii_case("bearer") || !self.known_token(token, allow_apps) {
                return Err(Denied::Unauthorized);
            }
            return Ok(());
        }
        let csrf = self.session_csrf(headers).ok_or(Denied::Unauthorized)?;
        let safe = [Method::GET, Method::HEAD, Method::OPTIONS].contains(method);
        let sent = headers
            .get(CSRF_HEADER)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if !safe && !loopback_auth::csrf_matches(sent, &csrf) {
            return Err(Denied::CsrfMismatch);
        }
        Ok(())
    }

    /// What only the holder of the token's digest can answer: a client asks
    /// it before it trusts the port with its token.
    pub fn proof(&self, token_id: &str, nonce: &str) -> Option<String> {
        let state = self.state.lock().unwrap();
        let token = state.records.tokens.get(token_id)?;
        Some(proof(&token.digest, token_id, nonce))
    }

    /// A single-use link into the screens, landing on `next`, a path of this
    /// server. The code travels in the URL fragment.
    pub fn entry_link(&self, next: &str) -> EntryLink {
        let code = self.state.lock().unwrap().entries.issue((), Instant::now());
        let query = if next == "/" {
            String::new()
        } else {
            let mut url = url::Url::parse("http://x/").expect("a URL");
            url.query_pairs_mut().append_pair("next", next);
            format!("?{}", url.query().unwrap_or_default())
        };
        EntryLink {
            url: format!("{}/-/enter{query}#{code}", self.base_url),
            expires_at: iso(now() + CODE_SECONDS as f64),
        }
    }

    /// Spends an entry code: the new session's cookie value and CSRF value.
    pub fn start_session(&self, code: &str) -> anyhow::Result<Option<(String, String)>> {
        let mut state = self.state.lock().unwrap();
        if state.entries.take(code, Instant::now()).is_none() {
            return Ok(None);
        }
        let cookie = loopback_auth::random_secret::<32>();
        let csrf = loopback_auth::random_secret::<32>();
        let mut records = state.records.clone();
        let now = now();
        records
            .sessions
            .retain(|_, session| session.expires_at > now);
        records.sessions.insert(
            loopback_auth::digest(&cookie),
            Session {
                csrf: csrf.clone(),
                expires_at: now + SESSION_SECONDS as f64,
            },
        );
        self.save(&mut state, records)?;
        Ok(Some((cookie, csrf)))
    }

    /// The CSRF value of the request's live session. A session lasts its
    /// `SESSION_SECONDS` however much it is used; the screen then says how to
    /// enter again.
    pub fn session_csrf(&self, headers: &HeaderMap) -> Option<String> {
        let cookie = loopback_auth::cookie_value(headers, &self.cookie)?;
        let state = self.state.lock().unwrap();
        let session = state
            .records
            .sessions
            .get(&loopback_auth::digest(&cookie))?;
        (session.expires_at > now()).then(|| session.csrf.clone())
    }

    fn known_token(&self, text: &str, allow_apps: bool) -> bool {
        let Some((id, secret)) = parse_token(text) else {
            return false;
        };
        let state = self.state.lock().unwrap();
        state.records.tokens.get(id).is_some_and(|token| {
            (token.label == OWNER_FILE || allow_apps && token.label.starts_with("app:"))
                && loopback_auth::digest_matches(secret, &token.digest)
        })
    }

    pub(crate) fn authorize(&self, request: Authorization) -> Option<String> {
        let mut redirect = loopback_auth::loopback_redirect(&request.redirect_uri)?;
        if !loopback_auth::valid_client(&request.client)
            || !loopback_auth::valid_challenge(&request.code_challenge)
            || !(1..=512).contains(&request.state.len())
            || redirect
                .query_pairs()
                .any(|(key, _)| matches!(key.as_ref(), "code" | "state"))
        {
            return None;
        }
        let code = self.state.lock().unwrap().consents.issue(
            Consent {
                client: request.client,
                redirect_uri: request.redirect_uri,
                challenge: request.code_challenge,
            },
            Instant::now(),
        );
        redirect
            .query_pairs_mut()
            .append_pair("code", &code)
            .append_pair("state", &request.state);
        Some(redirect.into())
    }

    pub(crate) fn app_token(&self, request: Exchange) -> anyhow::Result<Option<serde_json::Value>> {
        let mut state = self.state.lock().unwrap();
        let Some(consent) = state.consents.take(&request.code, Instant::now()) else {
            return Ok(None);
        };
        if consent.redirect_uri != request.redirect_uri
            || !request
                .code_verifier
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-._~".contains(&b))
            || !loopback_auth::verifier_matches(&request.code_verifier, &consent.challenge)
        {
            return Ok(None);
        }
        let label = format!("app:{}", consent.client);
        let (id, digest, token) = new_token();
        let created_at = iso(now());
        let mut records = state.records.clone();
        records.tokens.retain(|_, entry| entry.label != label);
        records.tokens.insert(
            id.clone(),
            Token {
                label: label.clone(),
                digest,
                created_at: created_at.clone(),
            },
        );
        self.save(&mut state, records)?;
        Ok(Some(
            serde_json::json!({"id": id, "label": label, "token": token, "created_at": created_at}),
        ))
    }

    /// Keeps a token in the owner file that authenticates. When the file is
    /// missing or does not (replaced, restored without it), its previous
    /// token is revoked and every session ends with it.
    fn keep_owner_token(&self) -> anyhow::Result<()> {
        let path = self.owner_token_path();
        if std::fs::read_to_string(&path).is_ok_and(|text| self.known_token(&text, false)) {
            return Ok(());
        }
        let mut state = self.state.lock().unwrap();
        let mut tokens = state.records.tokens.clone();
        tokens.retain(|_, token| token.label != OWNER_FILE);
        let (id, digest, text) = new_token();
        tokens.insert(
            id,
            Token {
                label: OWNER_FILE.into(),
                digest,
                created_at: iso(now()),
            },
        );
        // The records first: a token file the records do not know would end
        // every session again at the next start.
        self.save(
            &mut state,
            Records {
                tokens,
                sessions: BTreeMap::new(),
            },
        )?;
        Ok(gb_base::files::atomic_write(
            &path,
            format!("{text}\n").as_bytes(),
        )?)
    }

    fn save(&self, state: &mut State, records: Records) -> anyhow::Result<()> {
        gb_base::files::atomic_write(
            &self.directory.join("auth.json"),
            &serde_json::to_vec(&records)?,
        )?;
        state.records = records;
        Ok(())
    }
}

/// A fresh token: its public id, its secret's digest, and its text.
fn new_token() -> (String, String, String) {
    let id = loopback_auth::hex(&loopback_auth::random_bytes::<8>());
    let secret = loopback_auth::random_secret::<32>();
    let text = format!("{TOKEN_PREFIX}_{id}_{secret}");
    (id, loopback_auth::digest(&secret), text)
}

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

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or_default()
}

fn iso(timestamp: f64) -> String {
    chrono::DateTime::from_timestamp_micros((timestamp * 1e6) as i64)
        .unwrap_or_default()
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

/// Where the credentials of the server on `port` are kept, under `data`.
pub(crate) fn directory(data: &Path, port: u16) -> PathBuf {
    data.join("server").join(port.to_string())
}

fn create_private_dir(path: &Path) -> std::io::Result<()> {
    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder.create(path)
}

#[cfg(test)]
mod tests {
    #[test]
    fn app_consent_pkce_is_single_use_bound_to_redirect_and_revokes_the_previous_app_token() {
        use super::*;
        use base64::Engine;
        use sha2::Digest;
        let dir = tempfile::tempdir().unwrap();
        let auth = ServerAuth::open(dir.path(), 8765).unwrap();
        let verifier = "a".repeat(43);
        let challenge = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(sha2::Sha256::digest(verifier.as_bytes()));
        let redirect = "http://127.0.0.1:54321/callback?own=value";
        let consent = || {
            let redirect = auth
                .authorize(Authorization {
                    client: "test-app".into(),
                    redirect_uri: redirect.into(),
                    state: "state-1".into(),
                    code_challenge: challenge.clone(),
                })
                .unwrap();
            let url = url::Url::parse(&redirect).unwrap();
            assert!(url.query_pairs().any(|(k, v)| k == "own" && v == "value"));
            assert!(
                url.query_pairs()
                    .any(|(k, v)| k == "state" && v == "state-1")
            );
            url.query_pairs()
                .find(|(k, _)| k == "code")
                .unwrap()
                .1
                .to_string()
        };
        let exchange = |code: String, uri: &str, verifier: &str| {
            auth.app_token(Exchange {
                code,
                redirect_uri: uri.into(),
                code_verifier: verifier.into(),
            })
            .unwrap()
        };
        let code = consent();
        let token = exchange(code.clone(), redirect, &verifier).unwrap();
        assert!(exchange(code, redirect, &verifier).is_none());
        let token = token["token"].as_str().unwrap();
        assert!(auth.known_token(token, true));
        assert!(!auth.known_token(token, false));
        assert!(
            !std::fs::read_to_string(auth.directory.join("auth.json"))
                .unwrap()
                .contains(token)
        );
        let next = exchange(consent(), redirect, &verifier).unwrap();
        assert!(auth.known_token(next["token"].as_str().unwrap(), true));
        assert!(!auth.known_token(token, true));
        for (uri, v) in [
            ("http://127.0.0.1:54321/other", verifier.as_str()),
            (redirect, "wrong"),
        ] {
            let code = consent();
            assert!(exchange(code.clone(), uri, v).is_none());
            assert!(exchange(code, redirect, &verifier).is_none());
        }
        let code = auth.state.lock().unwrap().consents.issue(
            Consent {
                client: "test-app".into(),
                redirect_uri: redirect.into(),
                challenge,
            },
            Instant::now() - std::time::Duration::from_secs(CODE_SECONDS + 1),
        );
        assert!(exchange(code, redirect, &verifier).is_none());
        for uri in [
            "https://example.com/callback",
            "http://0.0.0.0:123/callback",
            "http://127.0.0.1:123/callback?code=owned",
            "http://127.0.0.1:123/callback?state=owned",
        ] {
            assert!(
                auth.authorize(Authorization {
                    client: "test-app".into(),
                    redirect_uri: uri.into(),
                    state: "state".into(),
                    code_challenge: "a".repeat(43)
                })
                .is_none()
            );
        }
    }
    use super::*;

    fn headers(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        for (name, value) in pairs {
            headers.insert(
                axum::http::HeaderName::from_bytes(name.as_bytes()).unwrap(),
                value.parse().unwrap(),
            );
        }
        headers
    }

    #[test]
    fn tokens_are_gbt_a_sixteen_hex_id_and_a_secret() {
        let (id, digest, text) = new_token();
        assert_eq!(parse_token(&text), Some((id.as_str(), &text[21..])));
        assert!(loopback_auth::digest_matches(&text[21..], &digest));
        for bad in [
            "",
            "gbt_",
            "gbt_0123456789abcdef_",
            "bvt_0123456789abcdef_x",
            "gbt_0123456789ABCDEF_x",
            "gbt_0123_x",
        ] {
            assert_eq!(parse_token(bad), None, "{bad}");
        }
        assert_eq!(
            parse_token(" gbt_0123456789abcdef_s_e_c \n"),
            Some(("0123456789abcdef", "s_e_c"))
        );
    }

    #[test]
    fn the_owner_file_authenticates_and_survives_a_restart() {
        let data = tempfile::tempdir().unwrap();
        let auth = ServerAuth::open(data.path(), 8765).unwrap();
        let token = std::fs::read_to_string(auth.owner_token_path()).unwrap();
        let bearer = headers(&[("authorization", &format!("Bearer {}", token.trim()))]);
        assert_eq!(auth.authenticate(&Method::POST, &bearer), Ok(()));
        let lower = headers(&[("authorization", &format!("bearer {}", token.trim()))]);
        assert_eq!(auth.authenticate(&Method::GET, &lower), Ok(()));
        let wrong = headers(&[("authorization", "Bearer gbt_0123456789abcdef_nope")]);
        assert_eq!(
            auth.authenticate(&Method::GET, &wrong),
            Err(Denied::Unauthorized)
        );
        assert_eq!(
            auth.authenticate(&Method::GET, &HeaderMap::new()),
            Err(Denied::Unauthorized)
        );

        let again = ServerAuth::open(data.path(), 8765).unwrap();
        assert_eq!(
            std::fs::read_to_string(again.owner_token_path()).unwrap(),
            token
        );
        assert_eq!(again.authenticate(&Method::GET, &bearer), Ok(()));
    }

    #[test]
    fn a_replaced_owner_file_revokes_its_token_and_ends_every_session() {
        let data = tempfile::tempdir().unwrap();
        let auth = ServerAuth::open(data.path(), 8765).unwrap();
        let old = std::fs::read_to_string(auth.owner_token_path()).unwrap();
        let link = auth.entry_link("/");
        let code = link.url.split_once('#').unwrap().1;
        let (cookie, _) = auth.start_session(code).unwrap().unwrap();
        std::fs::write(auth.owner_token_path(), "something else").unwrap();

        let again = ServerAuth::open(data.path(), 8765).unwrap();
        let new = std::fs::read_to_string(again.owner_token_path()).unwrap();
        assert_ne!(new, old);
        let bearer = headers(&[("authorization", &format!("Bearer {}", old.trim()))]);
        assert_eq!(
            again.authenticate(&Method::GET, &bearer),
            Err(Denied::Unauthorized)
        );
        let session = headers(&[("cookie", &format!("{}={cookie}", again.cookie))]);
        assert_eq!(again.session_csrf(&session), None);
    }

    #[test]
    fn an_entry_code_opens_one_session_whose_writes_carry_its_csrf() {
        let data = tempfile::tempdir().unwrap();
        let auth = ServerAuth::open(data.path(), 8765).unwrap();
        let link = auth.entry_link("/commands?x=1");
        assert!(
            link.url
                .starts_with("http://127.0.0.1:8765/-/enter?next=%2Fcommands%3Fx%3D1#"),
            "{}",
            link.url
        );
        let code = link.url.split_once('#').unwrap().1;
        assert_eq!(auth.start_session("other").unwrap(), None);
        let (cookie, csrf) = auth.start_session(code).unwrap().unwrap();
        assert_eq!(auth.start_session(code).unwrap(), None);

        let session = format!("{}={cookie}", auth.cookie);
        assert_eq!(
            auth.session_csrf(&headers(&[("cookie", &session)])),
            Some(csrf.clone())
        );
        assert_eq!(
            auth.authenticate(&Method::GET, &headers(&[("cookie", &session)])),
            Ok(())
        );
        assert_eq!(
            auth.authenticate(&Method::POST, &headers(&[("cookie", &session)])),
            Err(Denied::CsrfMismatch)
        );
        assert_eq!(
            auth.authenticate(
                &Method::POST,
                &headers(&[("cookie", &session), (CSRF_HEADER, &csrf)])
            ),
            Ok(())
        );
        // Another port's cookie is not this server's.
        let other = format!("guildbotics_session_8766={cookie}");
        assert_eq!(
            auth.authenticate(&Method::GET, &headers(&[("cookie", &other)])),
            Err(Denied::Unauthorized)
        );
        assert_eq!(
            auth.entry_link("/").url.split_once('#').unwrap().0,
            "http://127.0.0.1:8765/-/enter"
        );
    }

    #[test]
    fn credential_carrier_spellings_are_owned_by_authentication() {
        fn offenders(dir: &Path, carriers: &[&str], found: &mut Vec<PathBuf>) {
            for entry in std::fs::read_dir(dir).unwrap() {
                let entry = entry.unwrap();
                let path = entry.path();
                let kind = entry.file_type().unwrap();
                if kind.is_dir() {
                    offenders(&path, carriers, found);
                } else if kind.is_file()
                    && path.extension().is_some_and(|ext| ext == "rs")
                    && !path.ends_with("gb-server/src/auth.rs")
                {
                    let source = std::fs::read_to_string(&path).unwrap().to_ascii_lowercase();
                    if carriers.iter().any(|carrier| source.contains(carrier)) {
                        found.push(path);
                    }
                }
            }
        }

        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let carriers = [OWNER_TOKEN, COOKIE_PREFIX, CSRF_HEADER];
        let mut found = Vec::new();
        for source in ["gb-server/src", "guildbotics/src"] {
            offenders(&root.join(source), &carriers, &mut found);
        }
        assert!(
            found.is_empty(),
            "unexpected credential carrier spellings in {found:?}"
        );

        let canary = tempfile::tempdir().unwrap();
        let misplaced = canary.path().join("transport.rs");
        std::fs::write(
            &misplaced,
            format!("const COOKIE: &str = {COOKIE_PREFIX:?};"),
        )
        .unwrap();
        offenders(canary.path(), &carriers, &mut found);
        assert_eq!(found, [misplaced]);
    }

    #[test]
    fn the_proof_is_the_hmac_of_the_tokens_digest() {
        let data = tempfile::tempdir().unwrap();
        let auth = ServerAuth::open(data.path(), 8765).unwrap();
        let token = std::fs::read_to_string(auth.owner_token_path()).unwrap();
        let (id, secret) = parse_token(&token).unwrap();
        let nonce = "0".repeat(64);
        let expected = loopback_auth::hex(&loopback_auth::hmac_sha256(
            loopback_auth::digest(secret).as_bytes(),
            format!("guildbotics-local-api-proof\n{nonce}\n{id}").as_bytes(),
        ));
        assert_eq!(auth.proof(id, &nonce), Some(expected));
        assert_eq!(auth.proof("0123456789abcdef", &nonce), None);
    }

    #[test]
    fn records_the_python_version_wrote_are_read_and_kept() {
        let data = tempfile::tempdir().unwrap();
        let directory = data.path().join("server/8765");
        std::fs::create_dir_all(&directory).unwrap();
        // As pydantic's model_dump_json writes them.
        let secret = "s3cr3t-of-the-app";
        let app_digest = loopback_auth::digest(secret);
        let written = format!(
            r#"{{"tokens":{{"00112233445566ff":{{"label":"app:Laptop","digest":"{app_digest}","created_at":"2026-11-01T00:00:00.123456Z"}}}},"sessions":{{"{}":{{"csrf":"c","expires_at":4102444800.5}}}}}}"#,
            loopback_auth::digest("cookie")
        );
        std::fs::write(directory.join("auth.json"), &written).unwrap();

        let auth = ServerAuth::open(data.path(), 8765).unwrap();
        let app = headers(&[(
            "authorization",
            &format!("Bearer gbt_00112233445566ff_{secret}"),
        )]);
        assert_eq!(
            auth.authenticate(&Method::GET, &app),
            Err(Denied::Unauthorized)
        );
        assert_eq!(auth.authenticate_for(&Method::POST, &app, true), Ok(()));
        // A new owner token ends the sessions, and keeps the app's token.
        let session = headers(&[("cookie", &format!("{}=cookie", auth.cookie))]);
        assert_eq!(auth.session_csrf(&session), None);
        let kept: serde_json::Value =
            serde_json::from_slice(&std::fs::read(directory.join("auth.json")).unwrap()).unwrap();
        assert_eq!(kept["tokens"]["00112233445566ff"]["label"], "app:Laptop");
        assert_eq!(kept["tokens"].as_object().unwrap().len(), 2);
    }

    #[cfg(unix)]
    #[test]
    fn credentials_are_readable_by_their_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let data = tempfile::tempdir().unwrap();
        let auth = ServerAuth::open(data.path(), 8765).unwrap();
        let mode = |path: &Path| std::fs::metadata(path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(&auth.directory), 0o700);
        assert_eq!(mode(&auth.owner_token_path()), 0o600);
        assert_eq!(mode(&auth.directory.join("auth.json")), 0o600);
    }
}
