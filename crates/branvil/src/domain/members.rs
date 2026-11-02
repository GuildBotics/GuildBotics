//! Members, tokens, GUI sessions, and the single-use codes of entry links
//! and local apps.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::app::App;
use crate::auth::{self, Caller};
use crate::domain::{Page, PageReq, image_type, member_by_name};
use crate::error::{AppError, AppResult};
use crate::model::{Account, Member, MemberKind, Session, Token};
use crate::store::{self, Key, Rd, Wtx};
use crate::util::{self, fold};

#[derive(Serialize, Debug, Clone)]
pub struct MemberView {
    pub name: String,
    pub display_name: String,
    pub kind: MemberKind,
    /// Accounts on upstream hosts (their tokens are never returned).
    pub accounts: Vec<Account>,
    /// The members asked to review every pull request this member opens.
    pub reviewers: Vec<String>,
    /// Where the member's picture is, changing with the picture; `null` for none.
    pub avatar_url: Option<String>,
}

pub fn member_view(m: &Member) -> MemberView {
    MemberView {
        name: m.name.clone(),
        display_name: m.display_name.clone(),
        kind: m.kind,
        accounts: m.accounts.clone(),
        reviewers: m.reviewers.clone(),
        avatar_url: m
            .avatar
            .as_ref()
            .map(|v| format!("/api/v1/members/{}/avatar?v={v}", m.name)),
    }
}

/// The largest picture a member may have.
pub const MAX_AVATAR_BYTES: usize = 4 << 20;

/// Sets (`Some`) or removes a member's picture, by the owner or the member
/// itself. The same picture again changes nothing.
pub async fn set_avatar(
    app: &App,
    caller: &Caller,
    name: String,
    image: Option<Vec<u8>>,
) -> AppResult<MemberView> {
    caller.require_self_or_owner(&name)?;
    if let Some(bytes) = &image {
        if bytes.len() > MAX_AVATAR_BYTES {
            return Err(AppError::too_large(format!(
                "A picture may be at most {} MiB.",
                MAX_AVATAR_BYTES >> 20
            )));
        }
        if image_type(bytes).is_none() {
            return Err(AppError::invalid(
                "A picture must be a PNG, JPEG, GIF, or WebP image.",
            ));
        }
    }
    let m = app
        .store
        .write(move |w| {
            let mut m = member_by_name(w, &name)?;
            let key = Member::key(&name);
            let version = image
                .as_deref()
                .map(|bytes| util::hex(&util::sha256(bytes))[..16].to_string());
            if version == m.avatar {
                return Ok(m);
            }
            match &image {
                Some(bytes) => w.put_raw(store::AVATARS, &key, bytes)?,
                None => {
                    w.del(store::AVATARS, &key)?;
                }
            }
            m.avatar = version;
            w.put(store::MEMBERS, &key, &m)?;
            Ok(m)
        })
        .await?;
    Ok(member_view(&m))
}

/// A member's picture and its media type, for any signed-in member.
pub async fn avatar(app: &App, name: String) -> AppResult<(&'static str, Vec<u8>)> {
    let bytes = app
        .store
        .read(move |r| r.get_raw(store::AVATARS, &Member::key(&name)))
        .await?
        .ok_or_else(|| AppError::not_found("The member has no picture."))?;
    let kind = image_type(&bytes).ok_or_else(|| AppError::internal("unreadable picture"))?;
    Ok((kind, bytes))
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewMember {
    pub name: String,
    pub display_name: Option<String>,
    pub kind: MemberKind,
}

/// Inserts a member in a write transaction (shared by the API, `init`, and
/// the automatic runner).
pub fn insert_member(w: &Wtx, input: &NewMember) -> AppResult<Member> {
    util::validate_member_name(&input.name)?;
    if util::fold(&input.name) == "branvil" {
        return Err(AppError::invalid("The member name branvil is reserved."));
    }
    let key = Member::key(&input.name);
    if w.exists(store::MEMBERS, &key)? {
        return Err(AppError::conflict(format!(
            "A member named {} already exists.",
            input.name
        )));
    }
    let member = Member {
        name: input.name.clone(),
        display_name: input
            .display_name
            .clone()
            .filter(|d| !d.trim().is_empty())
            .unwrap_or_else(|| input.name.clone()),
        kind: input.kind,
        accounts: Vec::new(),
        reviewers: Vec::new(),
        created_at: util::now(),
        avatar: None,
    };
    super::check_text("display_name", &member.display_name, 256)?;
    w.put(store::MEMBERS, &key, &member)?;
    Ok(member)
}

/// A member, read by the owner or by the member itself.
pub async fn get(app: &App, caller: &Caller, name: String) -> AppResult<MemberView> {
    caller.require_self_or_owner(&name)?;
    let m = app.store.read(move |r| member_by_name(r, &name)).await?;
    Ok(member_view(&m))
}

pub async fn list(app: &App, caller: &Caller, page: PageReq) -> AppResult<Page<MemberView>> {
    caller.require_owner()?;
    let (items, next_cursor) = app
        .store
        .read(move |r| {
            page.scan(r, store::MEMBERS, &[], |_, v| {
                Ok(Some(member_view(&store::decode::<Member>(v)?)))
            })
        })
        .await?;
    Ok(Page { items, next_cursor })
}

/// Creates an agent or a runner; the one owner comes from `branvil init`.
pub async fn create(app: &App, caller: &Caller, input: NewMember) -> AppResult<MemberView> {
    caller.require_owner()?;
    if input.kind == MemberKind::Owner {
        return Err(AppError::invalid("kind must be agent or runner."));
    }
    let m = app.store.write(move |w| insert_member(w, &input)).await?;
    Ok(member_view(&m))
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct MemberPatch {
    pub display_name: String,
}

pub async fn update(
    app: &App,
    caller: &Caller,
    name: String,
    patch: MemberPatch,
) -> AppResult<MemberView> {
    caller.require_owner()?;
    super::check_text("display_name", &patch.display_name, 256)?;
    let m = app
        .store
        .write(move |w| {
            let mut m = member_by_name(w, &name)?;
            m.display_name = patch.display_name;
            w.put(store::MEMBERS, &Member::key(&name), &m)?;
            Ok(m)
        })
        .await?;
    Ok(member_view(&m))
}

/// Asks (`add`) or stops asking `reviewer` to review every pull request
/// member `name` opens. A reviewer is an owner or an agent other than the
/// member, which is not a runner (a runner opens no pull requests).
pub async fn set_reviewer(
    app: &App,
    caller: &Caller,
    name: String,
    reviewer: String,
    add: bool,
) -> AppResult<MemberView> {
    caller.require_owner()?;
    let m = app
        .store
        .write(move |w| {
            let mut m = member_by_name(w, &name)?;
            let r = member_by_name(w, &reviewer)?;
            if m.kind == MemberKind::Runner {
                return Err(AppError::invalid(
                    "A runner opens no pull requests and has no reviewers.",
                ));
            }
            if add && r.kind == MemberKind::Runner {
                return Err(AppError::invalid(format!(
                    "{} is a runner, which reviews nothing.",
                    r.name
                )));
            }
            if add && fold(&r.name) == fold(&m.name) {
                return Err(AppError::invalid(
                    "A member cannot review their own pull requests.",
                ));
            }
            let present = m.reviewers.iter().any(|x| fold(x) == fold(&r.name));
            if add && !present {
                m.reviewers.push(r.name);
            } else if !add && present {
                m.reviewers.retain(|x| fold(x) != fold(&r.name));
            } else {
                return Ok(m);
            }
            w.put(store::MEMBERS, &Member::key(&name), &m)?;
            Ok(m)
        })
        .await?;
    Ok(member_view(&m))
}

#[derive(Serialize, Debug)]
pub struct IssuedToken {
    pub id: String,
    pub label: String,
    pub token: String,
    pub created_at: String,
}

impl IssuedToken {
    pub fn new((t, token): (Token, String)) -> Self {
        IssuedToken {
            id: t.id,
            label: t.label,
            token,
            created_at: t.created_at,
        }
    }
}

#[derive(Serialize, Debug)]
pub struct TokenView {
    pub id: String,
    pub label: String,
    pub created_at: String,
    pub last_used_at: Option<String>,
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewToken {
    #[serde(default)]
    pub label: String,
    /// Revokes the member's tokens of this label in the same write.
    #[serde(default)]
    pub replace: bool,
}

/// Issues a token. The owner issues for anyone; a member may issue its own.
/// The labels Branvil issues itself are refused.
pub async fn issue_token(
    app: &App,
    caller: &Caller,
    name: String,
    input: NewToken,
) -> AppResult<IssuedToken> {
    caller.require_self_or_owner(&name)?;
    super::check_text("label", &input.label, 256)?;
    if auth::is_reserved_label(&input.label) {
        return Err(AppError::invalid(format!(
            "The label {} is reserved for tokens Branvil issues itself.",
            input.label
        )));
    }
    let issued = app
        .store
        .write(move |w| {
            member_by_name(w, &name)?;
            if input.replace {
                auth::replace_token(w, &name, &input.label)
            } else {
                auth::issue_token(w, &name, &input.label)
            }
        })
        .await?;
    Ok(IssuedToken::new(issued))
}

pub async fn list_tokens(
    app: &App,
    caller: &Caller,
    name: String,
    page: PageReq,
) -> AppResult<Page<TokenView>> {
    caller.require_self_or_owner(&name)?;
    let (items, next_cursor) = app
        .store
        .read(move |r| {
            member_by_name(r, &name)?;
            let prefix = Key::new().s(&fold(&name)).bytes();
            page.scan(r, store::IX_MEMBER_TOKENS, &prefix, |k, _| {
                let parts = store::decode_key(k, "ss")
                    .ok_or_else(|| AppError::internal("bad token index"))?;
                let t: Option<Token> = r.get(store::TOKENS, &Token::key(parts[1].s()))?;
                Ok(t.map(|t| TokenView {
                    id: t.id,
                    label: t.label,
                    created_at: t.created_at,
                    last_used_at: t.last_used_at,
                }))
            })
        })
        .await?;
    Ok(Page { items, next_cursor })
}

pub async fn revoke_token(app: &App, caller: &Caller, name: String, id: String) -> AppResult<()> {
    caller.require_self_or_owner(&name)?;
    app.store
        .write(move |w| {
            let t: Token = w
                .get(store::TOKENS, &Token::key(&id))?
                .filter(|t: &Token| t.member == fold(&name))
                .ok_or_else(|| {
                    AppError::not_found(format!("Token {id} of {name} was not found."))
                })?;
            auth::revoke(w, &t)
        })
        .await
}

#[derive(Serialize, Debug)]
pub struct SessionView {
    pub me: MemberView,
    pub csrf: String,
}

/// How long a single-use code (an entry link's, a local app's) stays usable.
pub const CODE_SECONDS: u64 = 60;

/// Unused single-use codes, in memory only (a restart invalidates them):
/// SHA-256 hex of the code → (what it stands for, expiry).
pub struct Codes<T>(HashMap<String, (T, Instant)>);

impl<T> Default for Codes<T> {
    fn default() -> Self {
        Codes(HashMap::new())
    }
}

fn code_hash(code: &str) -> String {
    util::hex(&util::sha256(code.as_bytes()))
}

impl<T> Codes<T> {
    /// Makes a code of 32 random bytes for `value`, usable once within
    /// `CODE_SECONDS`, and forgets the expired ones.
    pub fn issue(&mut self, value: T, now: Instant) -> String {
        let code = util::random_secret::<32>();
        self.0.retain(|_, (_, expires)| *expires > now);
        self.0.insert(
            code_hash(&code),
            (value, now + Duration::from_secs(CODE_SECONDS)),
        );
        code
    }

    /// Removes a code (each works once) and returns its value if it has not
    /// expired.
    pub fn take(&mut self, code: &str, now: Instant) -> Option<T> {
        self.0
            .remove(&code_hash(code))
            .filter(|(_, expires)| *expires > now)
            .map(|(value, _)| value)
    }
}

#[derive(Serialize, Debug)]
pub struct EntryLink {
    pub url: String,
    pub expires_at: String,
}

/// Makes a single-use link that opens a GUI session of the owner
/// (`branvil open`). The code travels in the URL fragment, so it is in no
/// request line, log, or Referer; only its hash is kept, in memory.
pub fn entry_link(app: &App, caller: &Caller) -> AppResult<EntryLink> {
    caller.require_owner()?;
    let code = app
        .entry_codes
        .lock()
        .unwrap()
        .issue(caller.name().to_string(), Instant::now());
    Ok(EntryLink {
        url: format!("{}/-/enter#{code}", app.base_url),
        expires_at: util::format_time(
            chrono::Utc::now() + chrono::Duration::seconds(CODE_SECONDS as i64),
        ),
    })
}

/// Exchanges an entry link's code (once) for a session. Returns the view and
/// the session id for the cookie.
pub async fn enter(app: &App, code: String) -> AppResult<(SessionView, String)> {
    let found = app.entry_codes.lock().unwrap().take(&code, Instant::now());
    let name = found.ok_or_else(AppError::unauthorized)?;
    let id = util::random_secret::<32>();
    let csrf = util::random_secret::<32>();
    let hours = app.config.limits.session_hours;
    let session = Session {
        member: fold(&name),
        csrf: csrf.clone(),
        created_at: util::now(),
        expires_at: util::format_time(chrono::Utc::now() + chrono::Duration::hours(hours)),
    };
    let key = auth::session_key(&id);
    let member = app
        .store
        .write(move |w| {
            let member = member_by_name(w, &name)?;
            // Drop expired sessions while here, so the table stays small.
            let now = util::now();
            for (k, v) in w.prefix_raw(store::SESSIONS, &[], None, usize::MAX)? {
                let s: Session = store::decode(&v)?;
                if s.expires_at < now {
                    w.del(store::SESSIONS, &k)?;
                }
            }
            w.put(store::SESSIONS, &key, &session)?;
            Ok(member)
        })
        .await?;
    Ok((
        SessionView {
            me: member_view(&member),
            csrf,
        },
        id,
    ))
}

pub async fn current_session(app: &App, id: String) -> AppResult<SessionView> {
    let found = app.store.read(move |r| auth::check_session(r, &id)).await?;
    let (member, session) = found.ok_or_else(AppError::unauthorized)?;
    Ok(SessionView {
        me: member_view(&member),
        csrf: session.csrf,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_work_once_and_expire() {
        let now = Instant::now();
        let later = now + Duration::from_secs(CODE_SECONDS);
        let mut codes = Codes::default();
        let live = codes.issue("alice", now);
        let old = codes.issue("bob", now);
        assert_ne!(live, old);
        assert_eq!(codes.take(&old, later), None);
        assert_eq!(codes.take("other", now), None);
        assert_eq!(codes.take(&live, now), Some("alice"));
        assert_eq!(codes.take(&live, now), None);
        // Only hashes are kept, and issuing forgets the expired ones.
        codes.issue("carol", now);
        let kept = codes.issue("dave", later);
        assert_eq!(codes.0.len(), 1);
        assert!(!codes.0.contains_key(&kept));
    }
}
