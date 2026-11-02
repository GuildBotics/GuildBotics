//! Members' credentials and permissions: tokens (stored as SHA-256 only and
//! compared in constant time), the owner's token file, GUI sessions with a
//! CSRF secret, and the checks of what a member's kind may do.

use axum::http::{HeaderMap, Method};
use subtle::ConstantTimeEq;

use crate::app::App;
use crate::config::{Layout, restrict};
use crate::error::{AppError, AppResult};
use crate::model::{self, Member, MemberKind, Session, Token};
use crate::store::{self, Key, Rd, Wtx};
use crate::util::{self, fold};

pub const TOKEN_PREFIX: &str = "bvt_";
pub const SESSION_COOKIE: &str = "branvil_session";
pub const CSRF_HEADER: &str = "x-branvil-csrf";

/// The authenticated member of a request.
#[derive(Clone, Debug)]
pub struct Caller {
    pub member: Member,
}

impl Caller {
    pub fn name(&self) -> &str {
        &self.member.name
    }

    pub fn is_owner(&self) -> bool {
        self.member.kind == MemberKind::Owner
    }

    /// The owner and agents read and work on every repository, its tickets,
    /// and its pull requests; a runner uses the runner API only.
    pub fn require_agent_or_owner(&self) -> AppResult<()> {
        if self.member.kind == MemberKind::Runner {
            Err(AppError::forbidden("A runner uses only the runner API."))
        } else {
            Ok(())
        }
    }

    pub fn require_owner(&self) -> AppResult<()> {
        if self.is_owner() {
            Ok(())
        } else {
            Err(AppError::forbidden("Only the owner may do this."))
        }
    }

    /// A member reads and manages its own record and tokens; only the owner
    /// those of others.
    pub fn require_self_or_owner(&self, name: &str) -> AppResult<()> {
        if fold(name) == fold(self.name()) {
            Ok(())
        } else {
            self.require_owner()
        }
    }
}

fn hash_secret(secret: &str) -> String {
    util::hex(&util::sha256(secret.as_bytes()))
}

/// Creates a token for a member inside a write transaction and returns the
/// stored record and the token text (shown once).
pub fn issue_token(w: &Wtx, member: &str, label: &str) -> AppResult<(Token, String)> {
    let id = util::ulid().to_string().to_lowercase();
    let secret = util::random_secret::<32>();
    let token = Token {
        id: format!("tok_{id}"),
        member: fold(member),
        label: label.to_string(),
        hash: hash_secret(&secret),
        created_at: util::now(),
        last_used_at: None,
    };
    model::put_indexed(
        w,
        store::TOKENS,
        &Token::key(&token.id),
        None,
        &token,
        model::token_index(&token),
    )?;
    Ok((
        token.clone(),
        format!("{TOKEN_PREFIX}{}_{secret}", &token.id[4..]),
    ))
}

/// Deletes a token record and its index entry.
pub fn revoke(w: &Wtx, token: &Token) -> AppResult<()> {
    w.del(store::TOKENS, &Token::key(&token.id))?;
    model::reindex(w, model::token_index(token), Vec::new())
}

/// Revokes every token of `member` with `label` and issues one in its place,
/// in the same write, so a retry leaves exactly one token of that label.
pub fn replace_token(w: &Wtx, member: &str, label: &str) -> AppResult<(Token, String)> {
    let prefix = Key::new().s(&fold(member)).bytes();
    for k in w.prefix_keys(store::IX_MEMBER_TOKENS, &prefix)? {
        let Some(parts) = store::decode_key(&k, "ss") else {
            continue;
        };
        if let Some(t) = w.get::<Token>(store::TOKENS, &Token::key(parts[1].s()))?
            && t.label == label
        {
            revoke(w, &t)?;
        }
    }
    issue_token(w, member, label)
}

/// Label of the owner token whose text is kept in `secrets/owner.token`.
const OWNER_FILE_LABEL: &str = "owner-file";
/// Label of the automatic runner's token.
pub const AUTO_LABEL: &str = "auto";
/// Prefix of the labels of tokens a local app got through the consent screen.
pub const APP_LABEL_PREFIX: &str = "app:";

/// Labels Branvil issues itself, never through the tokens endpoint.
pub fn is_reserved_label(label: &str) -> bool {
    label == OWNER_FILE_LABEL || label == AUTO_LABEL || label.starts_with(APP_LABEL_PREFIX)
}

/// Keeps `secrets/owner.token` holding a token of the owner. When the file is
/// missing or its token no longer authenticates the owner (a restored backup
/// has no `secrets/`), the previous file tokens are revoked, every GUI
/// session ends, and a new token is written to the file.
pub fn ensure_owner_token(store: &store::Store, layout: &Layout) -> anyhow::Result<()> {
    let path = layout.owner_token();
    let current = std::fs::read_to_string(&path).unwrap_or_default();
    let current = current.trim();
    let valid = store.read_sync(|r| {
        Ok(check_token(r, current)?.is_some_and(|(m, _)| m.kind == MemberKind::Owner))
    })?;
    if !valid {
        let text = store.write_sync(|w| {
            let owner = w
                .prefix_all::<Member>(store::MEMBERS, &[])?
                .into_iter()
                .find(|m| m.kind == MemberKind::Owner)
                .ok_or_else(|| AppError::internal("there is no owner; run `branvil init`"))?;
            w.clear(store::SESSIONS)?;
            Ok(replace_token(w, &owner.name, OWNER_FILE_LABEL)?.1)
        })?;
        std::fs::write(&path, text)?;
    }
    // Also a kept file: one copied back with looser permissions is tightened.
    restrict(&path, false)
}

/// Splits `bvt_<id>_<secret>` into the stored token id and the secret.
fn parse_token(text: &str) -> Option<(String, &str)> {
    let rest = text.strip_prefix(TOKEN_PREFIX)?;
    let (id, secret) = rest.split_once('_')?;
    if id.is_empty() || secret.is_empty() {
        return None;
    }
    Some((format!("tok_{id}"), secret))
}

/// Looks up the member a token belongs to.
pub fn check_token(r: &impl Rd, text: &str) -> AppResult<Option<(Member, Token)>> {
    let Some((id, secret)) = parse_token(text) else {
        return Ok(None);
    };
    let Some(token) = r.get::<Token>(store::TOKENS, &Token::key(&id))? else {
        return Ok(None);
    };
    let computed = hash_secret(secret);
    if !bool::from(computed.as_bytes().ct_eq(token.hash.as_bytes())) {
        return Ok(None);
    }
    let Some(member) = r.get::<Member>(store::MEMBERS, &Member::key(&token.member))? else {
        return Ok(None);
    };
    Ok(Some((member, token)))
}

/// Authenticates a token and records its use (at most once a minute).
pub async fn authenticate_token(app: &App, text: &str) -> AppResult<Member> {
    let text = text.to_string();
    let found = app.store.read(move |r| check_token(r, &text)).await?;
    let Some((member, token)) = found else {
        return Err(AppError::unauthorized());
    };
    let due = {
        let mut touched = app.token_touched.lock().unwrap();
        let now = std::time::Instant::now();
        match touched.get(&token.id) {
            Some(t) if now.duration_since(*t).as_secs() < 60 => false,
            _ => {
                touched.insert(token.id.clone(), now);
                true
            }
        }
    };
    if due {
        // Bookkeeping only: the request does not wait behind other writers
        // for it (a read would otherwise queue for the single writer).
        let id = token.id.clone();
        let store = app.store.clone();
        tokio::spawn(async move {
            let at = util::now();
            let result = store
                .write(move |w| {
                    if let Some(mut t) = w.get::<Token>(store::TOKENS, &Token::key(&id))? {
                        t.last_used_at = Some(at);
                        w.put(store::TOKENS, &Token::key(&id), &t)?;
                    }
                    Ok(())
                })
                .await;
            if let Err(e) = result {
                tracing::warn!("token use not recorded: {}", e.message);
            }
        });
    }
    Ok(member)
}

pub fn session_key(id: &str) -> Vec<u8> {
    Key::new()
        .s(&util::hex(&util::sha256(id.as_bytes())))
        .bytes()
}

fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    for value in headers.get_all(axum::http::header::COOKIE) {
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

pub fn session_cookie(headers: &HeaderMap) -> Option<String> {
    cookie_value(headers, SESSION_COOKIE)
}

/// Loads a valid session and its member.
pub fn check_session(r: &impl Rd, id: &str) -> AppResult<Option<(Member, Session)>> {
    let Some(session) = r.get::<Session>(store::SESSIONS, &session_key(id))? else {
        return Ok(None);
    };
    if session.expires_at < util::now() {
        return Ok(None);
    }
    let Some(member) = r.get::<Member>(store::MEMBERS, &Member::key(&session.member))? else {
        return Ok(None);
    };
    Ok(Some((member, session)))
}

/// Authenticates an API request: a Bearer token, or the session cookie (with
/// the CSRF header on anything but GET/HEAD).
pub async fn authenticate(app: &App, method: &Method, headers: &HeaderMap) -> AppResult<Caller> {
    if let Some(value) = headers.get(axum::http::header::AUTHORIZATION) {
        let text = value.to_str().map_err(|_| AppError::unauthorized())?;
        let token = text
            .strip_prefix("Bearer ")
            .ok_or_else(AppError::unauthorized)?
            .trim();
        let member = authenticate_token(app, token).await?;
        return Ok(Caller { member });
    }
    let Some(id) = session_cookie(headers) else {
        return Err(AppError::unauthorized());
    };
    let found = app.store.read(move |r| check_session(r, &id)).await?;
    let Some((member, session)) = found else {
        return Err(AppError::unauthorized());
    };
    if method != Method::GET && method != Method::HEAD {
        let sent = headers
            .get(CSRF_HEADER)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if !bool::from(sent.as_bytes().ct_eq(session.csrf.as_bytes())) {
            return Err(AppError::new(
                axum::http::StatusCode::FORBIDDEN,
                "csrf_mismatch",
                "The X-Branvil-CSRF header does not match the current session.",
            ));
        }
    }
    Ok(Caller { member })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn csrf_refusal_is_distinct_from_permissions_and_expired_sessions() {
        let dir = tempfile::tempdir().unwrap();
        let config = crate::config::Config::load(None, Some(dir.path()), None, None).unwrap();
        config.layout().create().unwrap();
        let store = store::Store::open(&config.layout().db(), 1 << 20, 1).unwrap();
        store
            .write_sync(|w| {
                w.put(
                    store::MEMBERS,
                    &Member::key("test-agent"),
                    &Member {
                        name: "test-agent".into(),
                        display_name: "Test Agent".into(),
                        kind: MemberKind::Agent,
                        accounts: Vec::new(),
                        reviewers: Vec::new(),
                        created_at: util::now(),
                        avatar: None,
                    },
                )?;
                w.put(
                    store::SESSIONS,
                    &session_key("test-session"),
                    &Session {
                        member: "test-agent".into(),
                        csrf: "test-csrf".into(),
                        created_at: util::now(),
                        expires_at: util::format_time(
                            chrono::Utc::now() + chrono::Duration::minutes(1),
                        ),
                    },
                )
            })
            .unwrap();
        let app = App::new(
            config,
            "http://127.0.0.1:7878".into(),
            store,
            vec![],
            "http://127.0.0.1:7878".into(),
        );
        let mut headers = HeaderMap::new();
        headers.insert("cookie", "branvil_session=test-session".parse().unwrap());
        for method in [Method::POST, Method::PATCH, Method::PUT, Method::DELETE] {
            for sent in [None, Some("wrong"), Some("test-csrf")] {
                headers.remove(CSRF_HEADER);
                if let Some(value) = sent {
                    headers.insert(CSRF_HEADER, value.parse().unwrap());
                }
                let result = authenticate(&app, &method, &headers).await;
                if sent == Some("test-csrf") {
                    assert_eq!(
                        result.unwrap().require_owner().unwrap_err().code,
                        "forbidden"
                    );
                } else {
                    let error = result.unwrap_err();
                    assert_eq!(error.status, axum::http::StatusCode::FORBIDDEN);
                    assert_eq!(error.code, "csrf_mismatch");
                }
            }
        }
        headers.remove(CSRF_HEADER);
        for method in [Method::GET, Method::HEAD] {
            assert!(authenticate(&app, &method, &headers).await.is_ok());
        }
        headers.insert("cookie", "branvil_session=expired".parse().unwrap());
        assert_eq!(
            authenticate(&app, &Method::POST, &headers)
                .await
                .unwrap_err()
                .code,
            "unauthorized"
        );
    }

    #[test]
    fn tokens_parse_and_verify() {
        let dir = tempfile::tempdir().unwrap();
        let store = store::Store::open(&dir.path().join("t.redb"), 1 << 20, 1).unwrap();
        let text = store
            .write_sync(|w| {
                w.put(
                    store::MEMBERS,
                    &Member::key("aiko"),
                    &Member {
                        name: "aiko".into(),
                        display_name: "Aiko".into(),
                        kind: MemberKind::Agent,
                        accounts: Vec::new(),
                        reviewers: Vec::new(),
                        created_at: util::now(),
                        avatar: None,
                    },
                )?;
                Ok(issue_token(w, "aiko", "test")?.1)
            })
            .unwrap();
        assert!(text.starts_with("bvt_"));
        let found = store.read_sync(|r| check_token(r, &text)).unwrap();
        assert_eq!(found.unwrap().0.name, "aiko");
        let mut tampered = text.clone();
        tampered.push('x');
        assert!(
            store
                .read_sync(|r| check_token(r, &tampered))
                .unwrap()
                .is_none()
        );
        assert!(
            store
                .read_sync(|r| check_token(r, "bvt_nope"))
                .unwrap()
                .is_none()
        );
        // Only the hash is stored.
        let stored: Vec<Token> = store
            .read_sync(|r| r.prefix_all(store::TOKENS, &[]))
            .unwrap();
        // `bvt_<id>_<secret>`: the secret itself may contain `_`.
        let secret = text.splitn(3, '_').nth(2).unwrap();
        assert!(secret.len() >= 40);
        assert!(!stored[0].hash.contains(secret));
    }
}
