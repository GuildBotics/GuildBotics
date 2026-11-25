//! Members' credentials and permissions: tokens (stored as SHA-256 only and
//! compared in constant time), the host's owner credentials, and the checks
//! of what a member's kind may do.

use axum::http::{HeaderMap, Method};
use loopback_auth::CSRF_HEADER;

use crate::app::App;
use crate::error::{AppError, AppResult};
use crate::model::{self, Member, MemberKind, Token};
use crate::store::{self, Key, Rd, Wtx};
use crate::util::{self, fold};

pub const TOKEN_PREFIX: &str = "bvt_";

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

/// Creates a token for a member inside a write transaction and returns the
/// stored record and the token text (shown once).
pub fn issue_token(w: &Wtx, member: &str, label: &str) -> AppResult<(Token, String)> {
    let id = util::ulid().to_string().to_lowercase();
    let secret = util::random_secret::<32>();
    let token = Token {
        id: format!("tok_{id}"),
        member: fold(member),
        label: label.to_string(),
        hash: loopback_auth::digest(&secret),
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

/// Label of the automatic runner's token.
pub const AUTO_LABEL: &str = "auto";

/// Labels Branvil issues itself, never through the tokens endpoint.
pub fn is_reserved_label(label: &str) -> bool {
    label == AUTO_LABEL
}

/// The owner, whom the service creates when it first opens its data.
pub fn owner(r: &impl Rd) -> AppResult<Member> {
    r.prefix_all::<Member>(store::MEMBERS, &[])?
        .into_iter()
        .find(|m| m.kind == MemberKind::Owner)
        .ok_or_else(|| AppError::internal("there is no owner"))
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
    if !loopback_auth::digest_matches(secret, &token.hash) {
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

/// Authenticates an API request: a member's Bearer token, the host owner's
/// Bearer token, or the host's browser session (with the CSRF header on
/// anything but GET/HEAD).
pub async fn authenticate(app: &App, method: &Method, headers: &HeaderMap) -> AppResult<Caller> {
    if let Some(value) = headers.get(axum::http::header::AUTHORIZATION) {
        let text = value.to_str().map_err(|_| AppError::unauthorized())?;
        let token = text
            .strip_prefix("Bearer ")
            .ok_or_else(AppError::unauthorized)?
            .trim();
        if !token.starts_with(TOKEN_PREFIX)
            && let Some(host) = app.host.get()
            && host.owner_token(token)
        {
            return Ok(Caller {
                member: app.store.read(owner).await?,
            });
        }
        let member = authenticate_token(app, token).await?;
        return Ok(Caller { member });
    }
    Ok(Caller {
        member: host_session(app, method, headers).await?.0,
    })
}

/// Resolve a host browser session without accepting a Bearer credential.
pub(crate) async fn host_session(
    app: &App,
    method: &Method,
    headers: &HeaderMap,
) -> AppResult<(Member, String)> {
    let host = app.host.get().ok_or_else(AppError::unauthorized)?;
    let cookie = headers
        .get("cookie")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let csrf = if method == Method::GET || method == Method::HEAD {
        None
    } else {
        Some(
            headers
                .get(CSRF_HEADER)
                .and_then(|v| v.to_str().ok())
                .unwrap_or(""),
        )
    };
    let csrf = host.check(cookie, csrf).map_err(|e| match e {
        crate::host::Denied::Unauthorized => AppError::unauthorized(),
        crate::host::Denied::CsrfMismatch => AppError::new(
            axum::http::StatusCode::FORBIDDEN,
            "csrf_mismatch",
            "The X-GuildBotics-CSRF header does not match the current session.",
        ),
    })?;
    let member = app.store.read(owner).await?;
    Ok((member, csrf))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Host;
    impl crate::host::Host for Host {
        fn check(&self, cookie: &str, csrf: Option<&str>) -> Result<String, crate::host::Denied> {
            if cookie != "host_session_7878=fixture" {
                return Err(crate::host::Denied::Unauthorized);
            }
            if csrf.is_some_and(|v| v != "host-csrf") {
                return Err(crate::host::Denied::CsrfMismatch);
            }
            Ok("host-csrf".into())
        }
        fn owner_token(&self, token: &str) -> bool {
            token == "host-owner-token"
        }
    }

    #[tokio::test]
    async fn the_host_identifies_the_owner_and_member_tokens_their_members() {
        use std::sync::Arc;
        let dir = tempfile::tempdir().unwrap();
        let config = crate::config::Config::load(None, dir.path()).unwrap();
        config.layout().create().unwrap();
        let store = store::Store::open(&config.layout().db(), 1 << 20, 1).unwrap();
        let agent = store
            .write_sync(|w| {
                for (name, kind) in [
                    ("fixture-owner", MemberKind::Owner),
                    ("fixture-agent", MemberKind::Agent),
                ] {
                    crate::domain::members::insert_member(
                        w,
                        &crate::domain::members::NewMember {
                            name: name.into(),
                            display_name: None,
                            kind,
                        },
                    )?;
                }
                Ok(issue_token(w, "fixture-agent", "fixture")?.1)
            })
            .unwrap();
        let app = App::new(
            config,
            "http://127.0.0.1:7878".into(),
            store,
            Arc::new(crate::vault::Memory::default()),
            "http://127.0.0.1:7878".into(),
        );
        let bearer = |token: &str| {
            let mut headers = HeaderMap::new();
            headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
            headers
        };
        let mut session = HeaderMap::new();
        session.insert("cookie", "host_session_7878=fixture".parse().unwrap());
        // Without a host, only member tokens authenticate.
        assert_eq!(
            authenticate(&app, &Method::GET, &session)
                .await
                .unwrap_err()
                .code,
            "unauthorized"
        );
        let _ = app.host.set(Arc::new(Host));
        assert!(
            authenticate(&app, &Method::GET, &session)
                .await
                .unwrap()
                .is_owner()
        );
        for method in [Method::POST, Method::PUT, Method::PATCH, Method::DELETE] {
            session.remove(CSRF_HEADER);
            assert_eq!(
                authenticate(&app, &method, &session)
                    .await
                    .unwrap_err()
                    .code,
                "csrf_mismatch"
            );
            session.insert(CSRF_HEADER, "host-csrf".parse().unwrap());
            assert!(
                authenticate(&app, &method, &session)
                    .await
                    .unwrap()
                    .is_owner()
            );
        }
        assert!(
            authenticate(&app, &Method::POST, &bearer("host-owner-token"))
                .await
                .unwrap()
                .is_owner()
        );
        let member = authenticate(&app, &Method::POST, &bearer(&agent))
            .await
            .unwrap();
        assert_eq!(member.name(), "fixture-agent");
        for refused in ["other-token", "bvt_host-owner-token", ""] {
            assert_eq!(
                authenticate(&app, &Method::GET, &bearer(refused))
                    .await
                    .unwrap_err()
                    .code,
                "unauthorized",
                "{refused}"
            );
        }
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
