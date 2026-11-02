//! The operations of the upstream (`docs/api.md`, "Upstream"): a
//! repository's upstream, members' accounts, publishing a pull request, and
//! what Branvil reads of the host for clients: check logs and dependency
//! alerts.

use serde::{Deserialize, Serialize};

use super::host::{AlertState, As, Credential, DependencyAlert, Host, HostError};
use super::{Place, Pushed, follow_once, open_on_host, push_branch, token_of};
use crate::app::App;
use crate::auth::Caller;
use crate::domain::members::{MemberView, member_view};
use crate::domain::pulls::{self, MergeInput, PullView, check_landable, load_pull};
use crate::domain::repos::{self, RepoView, repo_view};
use crate::domain::{Page, PageReq, member_by_name, repo_by_id};
use crate::error::{AppError, AppResult};
use crate::model::{
    Account, Member, MemberKind, Published, Pull, Reader, Repo, Upstream as Setting,
};
use crate::store::{self, Rd};
use crate::util;

/// The member who reads a repository's upstream.
async fn reader_of(app: &App, setting: &Setting) -> AppResult<Member> {
    let name = setting.reader.member.clone();
    app.store
        .read(move |r| member_by_name(r, &name))
        .await
        .map_err(|_| no_reader())
}

fn no_reader() -> AppError {
    AppError::conflict_code(
        "no_account",
        "No member has an account on that host in Branvil to read it with; set one first (an agent's bot account is enough).",
    )
}

// ---------------------------------------------------------------- the setting

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct UpstreamInput {
    pub provider: String,
    pub repository: String,
    /// The member whose account reads it; `null` picks one; absent keeps the
    /// current reader (or picks one for a new upstream).
    #[serde(default, deserialize_with = "crate::domain::double_option")]
    pub reader: Option<Option<String>>,
}

/// Sets the repository's upstream after checking that Branvil can follow it:
/// its reader's account reads it, and Branvil's default branch is the host's
/// or behind it (then it moves to the host's). The reader is named, picked
/// when `null` (or for a new upstream), and otherwise kept.
pub async fn set(
    app: &App,
    caller: &Caller,
    path: &str,
    input: UpstreamInput,
) -> AppResult<RepoView> {
    caller.require_owner()?;
    let repo = repos::visible(app, caller, path).await?;
    let host = app.upstream.hosts.get(&input.provider)?;
    host.validate_repository(&input.repository)?;
    let provider = host.name().to_string();
    let kept = repo
        .upstream
        .as_ref()
        .filter(|u| u.provider == provider)
        .map(|u| u.reader.clone());
    let p2 = provider.clone();
    let reader = match input.reader {
        Some(Some(name)) => {
            let m = app.store.read(move |r| member_by_name(r, &name)).await?;
            if !m.accounts.iter().any(|a| a.provider == p2) {
                return Err(AppError::conflict_code(
                    "no_account",
                    format!(
                        "{} has no {} account in Branvil.",
                        m.name,
                        host.display_name()
                    ),
                ));
            }
            Reader {
                member: m.name,
                chosen: true,
            }
        }
        Some(None) => picked(app, &p2).await?,
        None => match kept {
            Some(r) => r,
            None => picked(app, &p2).await?,
        },
    };
    let setting = Setting {
        provider,
        repository: input.repository,
        reader,
    };
    let reader_member = reader_of(app, &setting).await?;
    let token = token_of(app, &reader_member, host)
        .await
        .map_err(|why| AppError::conflict_code("no_account", why))?;
    // Following the host's branch, recording the setting, and following
    // again are one write (`App::finish`).
    let updated = app
        .finish(move |app| async move {
            let host = app.upstream.hosts.get(&setting.provider)?;
            let reader = As {
                member: &reader_member.name,
                token: &token,
            };
            let at = Place {
                app: &app,
                repo: &repo,
                host,
                setting: &setting,
            };
            match follow_once(at, reader).await {
                Ok(_) => {}
                Err(super::Stop::Failed(why)) => return Err(AppError::unavailable(why)),
                Err(super::Stop::Stopped(_, why)) => return Err(AppError::conflict(why)),
            }
            let (id, s) = (repo.id.clone(), setting.clone());
            let updated = app
                .store
                .write(move |w| {
                    let mut r = repo_by_id(w, &id)?;
                    if r.upstream.as_ref() != Some(&s) {
                        r.upstream = Some(s);
                        r.revision += 1;
                        w.put(store::REPOS, &Repo::key(&r.id), &r)?;
                    }
                    Ok(r)
                })
                .await?;
            super::follow(
                Place {
                    repo: &updated,
                    ..at
                },
                reader,
            )
            .await;
            Ok(updated)
        })
        .await?;
    app.events.publish("repo", &updated.path, "");
    app.upstream.wake();
    Ok(repo_view(app, &updated))
}

pub async fn delete(app: &App, caller: &Caller, path: &str) -> AppResult<RepoView> {
    caller.require_owner()?;
    let repo = repos::visible(app, caller, path).await?;
    let id = repo.id.clone();
    let updated = app
        .store
        .write(move |w| {
            let mut r = repo_by_id(w, &id)?;
            if r.upstream.take().is_some() {
                r.revision += 1;
                w.put(store::REPOS, &Repo::key(&r.id), &r)?;
            }
            Ok(r)
        })
        .await?;
    app.upstream.following.lock().unwrap().remove(&repo.id);
    app.events.publish("repo", &updated.path, "");
    Ok(repo_view(app, &updated))
}

/// Polls the repository now ("Check now") and answers with what it found.
pub async fn refresh(app: &App, caller: &Caller, path: &str) -> AppResult<RepoView> {
    let repo = repos::visible(app, caller, path).await?;
    if repo.upstream.is_none() {
        return Err(AppError::conflict(format!(
            "{} has no upstream.",
            repo.path
        )));
    }
    super::poll_repo(app, &repo).await;
    Ok(repo_view(app, &repo))
}

/// A reader picked as when nobody is named (`pick_reader`).
async fn picked(app: &App, provider: &str) -> AppResult<Reader> {
    let p = provider.to_string();
    let member = app
        .store
        .read(move |r| super::pick_reader(r, &p))
        .await?
        .ok_or_else(no_reader)?;
    Ok(Reader {
        member,
        chosen: false,
    })
}

// ---------------------------------------------------------------- accounts

/// Sets a member's account on a host after the host accepted the
/// credential (`{"token"}` or `{"app"}`).
pub async fn set_account(
    app: &App,
    caller: &Caller,
    name: &str,
    provider: &str,
    credential: Credential,
) -> AppResult<MemberView> {
    caller.require_owner()?;
    let host = app.upstream.hosts.get(provider)?;
    credential.validate()?;
    let n = name.to_string();
    let member = app.store.read(move |r| member_by_name(r, &n)).await?;
    if member.kind == MemberKind::Runner {
        return Err(AppError::invalid("A runner has no upstream account."));
    }
    let login = host
        .check(&member.name, &credential)
        .await
        .map_err(|e| match e.status {
            Some(400..500) => AppError::invalid(e.message),
            _ => AppError::unavailable(e.message),
        })?;
    super::write_credential(app, &member.name, host.name(), &credential)?;
    let (n, p, kind) = (
        member.name.clone(),
        host.name().to_string(),
        credential.kind(),
    );
    let (m, repos) = app
        .store
        .write(move |w| {
            let mut m = member_by_name(w, &n)?;
            m.accounts.retain(|a| a.provider != p);
            m.accounts.push(Account {
                provider: p.clone(),
                login,
                kind,
                set_at: util::now(),
            });
            w.put(store::MEMBERS, &Member::key(&m.name), &m)?;
            // The repositories on that host, whose pull requests by the member
            // can now be published.
            let repos: Vec<String> = w
                .prefix_all::<Repo>(store::REPOS, &[])?
                .into_iter()
                .filter(|r| r.upstream.as_ref().is_some_and(|u| u.provider == p))
                .map(|r| r.path)
                .collect();
            Ok((m, repos))
        })
        .await?;
    for path in repos {
        app.events.publish("repo", &path, "");
    }
    app.upstream.wake();
    Ok(member_view(&m))
}

pub async fn delete_account(
    app: &App,
    caller: &Caller,
    name: &str,
    provider: &str,
) -> AppResult<MemberView> {
    caller.require_owner()?;
    let host = app.upstream.hosts.get(provider)?;
    let (n, p) = (name.to_string(), host.name().to_string());
    let (m, repos) = app
        .store
        .write(move |w| {
            let mut m = member_by_name(w, &n)?;
            m.accounts.retain(|a| a.provider != p);
            w.put(store::MEMBERS, &Member::key(&m.name), &m)?;
            // The upstreams this account read get another reader, picked.
            let mut changed: Vec<String> = Vec::new();
            for mut r in w.prefix_all::<Repo>(store::REPOS, &[])? {
                let Some(u) = &mut r.upstream else { continue };
                if u.provider != p || u.reader.member != m.name {
                    continue;
                }
                u.reader = Reader {
                    member: super::pick_reader(w, &p)?.unwrap_or_default(),
                    chosen: false,
                };
                r.revision += 1;
                w.put(store::REPOS, &Repo::key(&r.id), &r)?;
                changed.push(r.path.clone());
            }
            Ok((m, changed))
        })
        .await?;
    for path in repos {
        app.events.publish("repo", &path, "");
    }
    super::remove_credential(app, &m.name, host.name());
    Ok(member_view(&m))
}

// ---------------------------------------------------------------- publishing

/// What publishing would post, for the owner to read first.
#[derive(Serialize, Debug)]
pub struct Preview {
    /// The provider's name for people ("GitHub").
    pub host: String,
    pub repository: String,
    pub head: String,
    pub base: String,
    pub title: String,
    pub body: String,
    /// The member whose account posts it (the author).
    pub account: String,
    /// That account's login, or `None` when the author has none.
    pub login: Option<String>,
}

fn setting_of(repo: &Repo) -> AppResult<Setting> {
    repo.upstream
        .clone()
        .ok_or_else(|| AppError::conflict(format!("{} has no upstream; merge instead.", repo.path)))
}

pub async fn preview(app: &App, caller: &Caller, path: &str, number: u64) -> AppResult<Preview> {
    let repo = repos::visible(app, caller, path).await?;
    let setting = setting_of(&repo)?;
    let host = app.upstream.hosts.get(&setting.provider)?;
    let r2 = repo.clone();
    let (p, author) = app
        .store
        .read(move |r| {
            let p = load_pull(r, &r2, number)?;
            let author = member_by_name(r, &p.author)?;
            Ok((p, author))
        })
        .await?;
    let at = Place {
        app,
        repo: &repo,
        host,
        setting: &setting,
    };
    let (title, body) = super::compose(at, &p).await?;
    Ok(Preview {
        host: host.display_name().to_string(),
        repository: setting.repository.clone(),
        head: p.head.clone(),
        base: p.base.clone(),
        title,
        body,
        account: author.name.clone(),
        login: author
            .accounts
            .iter()
            .find(|a| a.provider == setting.provider)
            .map(|a| a.login.clone()),
    })
}

/// Publishes a pull request to the repository's upstream host as its
/// author: pushes the head branch there and opens the host's pull request.
/// Publishing a pull request whose sending stopped replaces what the host has.
pub async fn publish(
    app: &App,
    caller: &Caller,
    path: &str,
    number: u64,
    input: MergeInput,
) -> AppResult<PullView> {
    caller.require_owner()?;
    let repo = repos::visible(app, caller, path).await?;
    publish_pull(app, &repo, number, input, false).await
}

/// Resumes a stopped publish's landing checks. The host branch is replaced
/// only by the owner's subsequent publish after the gate passes.
pub async fn resume_landing(
    app: &App,
    caller: &Caller,
    path: &str,
    number: u64,
    input: MergeInput,
) -> AppResult<PullView> {
    caller.require_owner()?;
    let repo = repos::visible(app, caller, path).await?;
    crate::landing::resume_publish(app, &repo, number, input).await?;
    let r2 = repo.clone();
    let p = app.store.read(move |r| load_pull(r, &r2, number)).await?;
    let tips = pulls::tips(app, &repo).await?;
    pulls::pull_view(app, &repo, &p, &tips).await
}

/// `publish` of a pull request of `repo`, by the owner or (`auto`) by the
/// repository landing automatically (`autoland`).
pub async fn publish_pull(
    app: &App,
    repo: &Repo,
    number: u64,
    input: MergeInput,
    auto: bool,
) -> AppResult<PullView> {
    let id = repo.id.clone();
    let (repo, p) = app
        .finish(move |app| async move { publish_op(&app, &id, number, input, auto).await })
        .await?;
    let tips = pulls::tips(app, &repo).await?;
    pulls::pull_view(app, &repo, &p, &tips).await
}

/// `publish_pull` as one operation (`App::finish`): pushing the branch,
/// opening the host's pull request, and recording what the host has are one
/// write. Returns the repository and the pull request.
async fn publish_op(
    app: &App,
    repo_id: &str,
    number: u64,
    input: MergeInput,
    auto: bool,
) -> AppResult<(Repo, Pull)> {
    let _guard = app.upstream.pull_lock(repo_id, number).await;
    let (repo, setting, p, author, head) = {
        let _guard = app.lock_repo(repo_id).await;
        let id = repo_id.to_string();
        let (repo, p, author) = app
            .store
            .read(move |r| {
                let repo = repo_by_id(r, &id)?;
                let p = load_pull(r, &repo, number)?;
                let author = member_by_name(r, &p.author)?;
                Ok((repo, p, author))
            })
            .await?;
        let setting = setting_of(&repo)?;
        if p.base != repo.default_branch {
            return Err(AppError::conflict_code(
                "not_mergeable",
                format!(
                    "!{number} goes into {}; only pull requests into {} are published.",
                    p.base, repo.default_branch
                ),
            ));
        }
        // Branvil publishes a pull request by itself once: what became of
        // that (a refusal by the host included) is the owner's to settle.
        if auto && p.upstream.is_some() {
            return Err(AppError::conflict_code(
                "not_mergeable",
                format!("!{number} was published before; only the owner publishes it again."),
            ));
        }
        let host = app.upstream.hosts.get(&setting.provider)?;
        super::require_account(&author, host)
            .map_err(|why| AppError::conflict_code("no_account", why))?;
        crate::landing::reconcile_locked(app, &repo).await?;
        let (head, _, _) = check_landable(app, &repo, &p, &input, auto).await?;
        (repo, setting, p, author, head)
    };
    let host = app.upstream.hosts.get(&setting.provider)?;
    // A record of another place (the upstream changed since) is replaced.
    let earlier = p
        .upstream
        .clone()
        .filter(|x| x.provider == setting.provider && x.repository == setting.repository);
    if earlier
        .as_ref()
        .is_some_and(|x| x.stopped.is_none() && x.number.is_some())
    {
        // Already there; the poller carries every later change.
        app.upstream.wake();
        return Ok((repo, p));
    }
    let token = token_of(app, &author, host)
        .await
        .map_err(|why| AppError::conflict_code("no_account", why))?;
    let writer = As {
        member: &author.name,
        token: &token,
    };
    let mut x = earlier.unwrap_or_else(|| Published {
        provider: setting.provider.clone(),
        repository: setting.repository.clone(),
        account: author.name.clone(),
        number: None,
        url: None,
        state: "none".into(),
        pushed: None,
        sent_title: String::new(),
        sent_body: String::new(),
        sent_state: "open".into(),
        checks: Vec::new(),
        checks_registered_by: None,
        checks_settled: false,
        published_at: util::now(),
        error: None,
        stopped: None,
        auto,
    });
    x.account = author.name.clone();
    x.auto = auto;
    x.error = None;
    // Publishing after sending stopped is the owner's decision to replace
    // what the host has on the branch.
    let replace = x.stopped.take().is_some();
    let theirs = host
        .branch_tip(writer, &setting.repository, &p.head)
        .await
        .map_err(|e| AppError::unavailable(e.message))?;
    let at = Place {
        app,
        repo: &repo,
        host,
        setting: &setting,
    };
    let refused = if theirs.as_deref() == Some(head.as_str()) {
        // Already there (an earlier publish pushed it, then stopped).
        None
    } else if theirs.is_none() || theirs == x.pushed || replace {
        // Nothing there, Branvil's own push, or the owner replaces it.
        let lease = theirs.clone().unwrap_or_default();
        match push_branch(at, writer, &p.head, &head, &lease).await {
            Pushed::Done => None,
            Pushed::Refused(why) => Some(why),
            Pushed::Failed(why) => return Err(AppError::unavailable(why)),
        }
    } else {
        let t = theirs.clone().unwrap_or_default();
        Some(format!(
            "{} already has a branch {} (at {}) that Branvil did not push for this pull request; publishing again replaces it.",
            host.display_name(),
            p.head,
            &t[..t.len().min(12)]
        ))
    };
    if refused.is_none() {
        x.record_push(head, app.upstream.config.checks_registration_secs);
        if x.number.is_none() {
            open_on_host(at, writer, super::login_of(&author, host), &p, &mut x).await;
        }
    }
    x.stopped = refused.clone();
    let failed = x.error.clone();
    let r2 = repo.clone();
    let stored = app
        .store
        .write(move |w| {
            pulls::store_published(w, &r2, number, x, None)?;
            crate::landing::clear_resume_publish(w, &r2.id, number)?;
            load_pull(w, &r2, number)
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    app.upstream.wake();
    if let Some(why) = refused {
        return Err(AppError::conflict_code("upstream_refused", why));
    }
    if let Some(why) = failed {
        return Err(AppError::unavailable(format!(
            "The branch is on {}, but opening the pull request failed: {why} Branvil tries again.",
            host.display_name()
        )));
    }
    Ok((repo, stored))
}

// ---------------------------------------------------------------- reads for clients

/// The upstream's reader, as whom Branvil reads the host for a client: its
/// account only, never another one's.
async fn reader_token(app: &App, setting: &Setting, host: &Host) -> AppResult<(String, String)> {
    let member = reader_of(app, setting).await?;
    let token = token_of(app, &member, host)
        .await
        .map_err(|why| AppError::conflict_code("no_account", why))?;
    Ok((member.name, token))
}

/// A failure reading the host, in the API's words.
fn host_failure(e: HostError) -> AppError {
    match e.status {
        Some(404) => AppError::not_found(e.message),
        Some(400..=428 | 430..=499) => AppError::conflict_code("upstream_refused", e.message),
        _ => AppError::unavailable(e.message),
    }
}

/// The log of one of the host's check runs (`<provider>-<id>`, as the checks
/// name it), or `None` when `id` does not name one.
pub async fn check_log(
    app: &App,
    caller: &Caller,
    path: &str,
    id: &str,
    tail: u64,
) -> AppResult<Option<serde_json::Value>> {
    let repo = repos::visible(app, caller, path).await?;
    let Some(setting) = &repo.upstream else {
        return Ok(None);
    };
    let Some(check) = id.strip_prefix(&format!("{}-", setting.provider)) else {
        return Ok(None);
    };
    let host = app.upstream.hosts.get(&setting.provider)?;
    let (member, token) = reader_token(app, setting, host).await?;
    let (log, size) = host
        .check_log(
            As {
                member: &member,
                token: &token,
            },
            &setting.repository,
            check,
            tail as usize,
        )
        .await
        .map_err(host_failure)?;
    Ok(Some(serde_json::json!({
        "log": String::from_utf8_lossy(&log),
        "log_bytes": size,
        "truncated": size > tail,
        "pruned": false,
    })))
}

/// The upstream of a repository the caller sees, its host, and its reader's
/// account, to read dependency alerts with.
async fn alerts_upstream<'a>(
    app: &'a App,
    caller: &Caller,
    path: &str,
) -> AppResult<(Setting, &'a Host, String, String)> {
    let repo = repos::visible(app, caller, path).await?;
    let setting = repo.upstream.ok_or_else(|| {
        AppError::unsupported(format!(
            "{} has no upstream, so Branvil has no dependency alerts of it.",
            repo.path
        ))
    })?;
    let host = app.upstream.hosts.get(&setting.provider)?;
    let (member, token) = reader_token(app, &setting, host).await?;
    Ok((setting, host, member, token))
}

/// A page of the dependency alerts the upstream host reports, in `state`.
/// The cursor carries the host's own, bound to the upstream it came from.
pub async fn dependency_alerts(
    app: &App,
    caller: &Caller,
    path: &str,
    state: AlertState,
    page: PageReq,
) -> AppResult<Page<DependencyAlert>> {
    let (setting, host, member, token) = alerts_upstream(app, caller, path).await?;
    let place = format!("{}\n{}\n", setting.provider, setting.repository);
    let after = match &page.after {
        Some(position) => Some(
            std::str::from_utf8(position)
                .ok()
                .and_then(|p| p.strip_prefix(place.as_str()))
                .ok_or_else(AppError::invalid_cursor)?,
        ),
        None => None,
    };
    let who = As {
        member: &member,
        token: &token,
    };
    let (items, next) = host
        .dependency_alerts(who, &setting.repository, state, page.limit, after)
        .await
        .map_err(host_failure)?;
    Ok(Page {
        items,
        next_cursor: next.map(|n| page.cursor(format!("{place}{n}").as_bytes())),
    })
}

pub async fn dependency_alert(
    app: &App,
    caller: &Caller,
    path: &str,
    id: &str,
) -> AppResult<DependencyAlert> {
    let (setting, host, member, token) = alerts_upstream(app, caller, path).await?;
    let who = As {
        member: &member,
        token: &token,
    };
    host.dependency_alert(who, &setting.repository, id)
        .await
        .map_err(host_failure)
}
