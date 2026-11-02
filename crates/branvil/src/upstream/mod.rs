//! The upstream code host above Branvil (`docs/architecture.md`, "Upstream"):
//! following its default branch, publishing pull requests to it, and carrying
//! changes both ways. Everything host-specific is behind `host::Host`.

mod git;
mod github;
pub mod host;
pub mod http;
pub mod ops;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tokio::sync::Notify;

use crate::app::{App, KeyedGuard, KeyedLocks, Shared};
use crate::config::UpstreamConfig;
use crate::domain::markdown::{self, Item};
use crate::domain::pulls::{self, load_pull};
use crate::error::{AppError, AppResult};
use crate::git::ZERO_SHA;
use crate::model::{Member, MemberKind, Published, Pull, Repo, Ticket, Upstream as Setting};
use crate::references;
use crate::refs::RefChange;
use crate::store::{self, Key, Rd};
use crate::util::{self, fold};

pub use git::askpass_main;
use host::{As, Credential, Host, Hosts, PullChange, PullState};

/// What the last poll found about a repository's default branch.
#[derive(Serialize, Clone, Debug, Default, PartialEq)]
pub struct Following {
    /// The host's tip of the default branch.
    pub tip: Option<String>,
    pub checked_at: Option<String>,
    /// When Branvil's branch last moved to the host's.
    pub followed_at: Option<String>,
    /// The last failure (the next poll retries).
    pub error: Option<String>,
    /// Why following stopped (Branvil's branch has commits the host lacks).
    pub stopped: Option<String>,
}

/// A repository with an upstream: the setting and the host it names.
#[derive(Clone, Copy)]
pub struct Place<'a> {
    pub app: &'a App,
    pub repo: &'a Repo,
    pub host: &'a Host,
    pub setting: &'a Setting,
}

pub struct Upstream {
    config: UpstreamConfig,
    pub hosts: Hosts,
    wake: Notify,
    following: Mutex<HashMap<String, Following>>,
    /// One poll or publish of a pull request at a time (`pull_lock`).
    pulls: KeyedLocks,
    /// One poll of a repository at a time (the poller and "Check now").
    repos: KeyedLocks,
}

impl Upstream {
    pub fn new(config: &UpstreamConfig) -> Self {
        Self {
            config: config.clone(),
            hosts: Hosts::new(config),
            wake: Notify::new(),
            following: Mutex::new(HashMap::new()),
            pulls: KeyedLocks::default(),
            repos: KeyedLocks::default(),
        }
    }

    pub fn wake(&self) {
        self.wake.notify_one();
    }

    /// Held by whatever reads and rewrites a pull request's `Published`
    /// record (a poll, a publish, the webhook's notice), one at a time.
    pub async fn pull_lock(&self, repo_id: &str, number: u64) -> KeyedGuard<'_> {
        self.pulls
            .lock(&Key::new().s(repo_id).u(number).bytes())
            .await
    }

    /// The display name of a provider, or the provider itself when unknown.
    pub fn host_name(&self, provider: &str) -> String {
        self.hosts
            .get(provider)
            .map(|h| h.display_name().to_string())
            .unwrap_or_else(|_| provider.to_string())
    }

    pub fn following(&self, repo_id: &str) -> Following {
        self.following
            .lock()
            .unwrap()
            .get(repo_id)
            .cloned()
            .unwrap_or_default()
    }

    /// After every recorded batch of ref changes: a branch other than the
    /// default one moved in a repository with an upstream (perhaps a
    /// published head), so poll now. Writes nothing.
    pub fn on_ref_changes(&self, repo: &Repo, changes: &[RefChange]) {
        if repo.upstream.is_some()
            && changes.iter().any(|c| {
                c.refname.starts_with("refs/heads/")
                    && c.refname != format!("refs/heads/{}", repo.default_branch)
            })
        {
            self.wake();
        }
    }

    /// The canonical form of an issue URL of the repository's host.
    pub fn check_issue(&self, repo: &Repo, url: &str) -> AppResult<String> {
        let Some(up) = &repo.upstream else {
            return Err(AppError::invalid(format!(
                "{} has no upstream, so its tickets name no upstream issue.",
                repo.path
            )));
        };
        let host = self.hosts.get(&up.provider)?;
        host.issue_url(url).ok_or_else(|| {
            AppError::invalid(format!(
                "{url} is not an issue URL of {}.",
                host.display_name()
            ))
        })
    }

    pub async fn diagnostics(&self, app: &App) -> serde_json::Value {
        let repos = match app.store.read(upstream_repos).await {
            Ok(r) => r,
            Err(e) => return serde_json::json!({"state": "unknown", "detail": e.message}),
        };
        if repos.is_empty() {
            return serde_json::json!({"state": "not_configured"});
        }
        let problems: Vec<serde_json::Value> = repos
            .iter()
            .filter_map(|r| {
                let f = self.following(&r.id);
                let why = f.stopped.or(f.error)?;
                Some(serde_json::json!({"repository": r.path, "problem": why}))
            })
            .collect();
        serde_json::json!({
            "state": if problems.is_empty() { "following" } else { "problems" },
            "repositories": repos.len(),
            "problems": problems,
        })
    }
}

// ---------------------------------------------------------------- accounts

fn credential_path(app: &App, member: &str, provider: &str) -> PathBuf {
    app.layout
        .accounts()
        .join(format!("{}.{provider}.json", fold(member)))
}

/// Writes a member's credential to its `0600` file.
fn write_credential(
    app: &App,
    member: &str,
    provider: &str,
    credential: &Credential,
) -> AppResult<()> {
    let dir = app.layout.accounts();
    std::fs::create_dir_all(&dir).map_err(AppError::internal)?;
    crate::config::restrict(&dir, true).map_err(AppError::internal)?;
    let text = serde_json::to_vec(credential).map_err(AppError::internal)?;
    crate::config::write_secret(&credential_path(app, member, provider), &text)
        .map_err(AppError::internal)
}

fn remove_credential(app: &App, member: &str, provider: &str) {
    let _ = std::fs::remove_file(credential_path(app, member, provider));
}

fn require_account(member: &Member, host: &Host) -> Result<(), String> {
    if !member.accounts.iter().any(|a| a.provider == host.name()) {
        return Err(format!(
            "{} has no {} account in Branvil.",
            member.name,
            host.display_name()
        ));
    }
    Ok(())
}

/// A token of a member's account on a host, or why there is none. Every use
/// of an account (reads, pushes, API calls) gets its token here.
async fn token_of(app: &App, member: &Member, host: &Host) -> Result<String, String> {
    require_account(member, host)?;
    let credential: Credential =
        std::fs::read(credential_path(app, &member.name, host.name()))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .ok_or_else(|| {
                format!(
                    "The {} credential of {} is not on this machine (backups do not carry it); set it again.",
                    host.display_name(),
                    member.name
                )
            })?;
    host.token(&credential).await.map_err(|e| e.message)
}

// ---------------------------------------------------------------- reads

fn upstream_repos(r: &redb::ReadTransaction) -> AppResult<Vec<Repo>> {
    Ok(r.prefix_all::<Repo>(store::REPOS, &[])?
        .into_iter()
        .filter(|x| x.upstream.is_some())
        .collect())
}

/// The member whose account reads an upstream when nobody was named: an
/// agent's (the first by name) rather than the owner's, so the owner's own
/// token is used only when no agent has an account there.
pub fn pick_reader(r: &impl Rd, provider: &str) -> AppResult<Option<String>> {
    let mut members: Vec<Member> = r
        .prefix_all::<Member>(store::MEMBERS, &[])?
        .into_iter()
        .filter(|m| m.accounts.iter().any(|a| a.provider == provider))
        .collect();
    members.sort_by_key(|m| (m.kind == MemberKind::Owner, fold(&m.name)));
    Ok(members.into_iter().next().map(|m| m.name))
}

/// The member named to read a repository's upstream (read again in the
/// transaction, so a just-changed reader is used).
fn setting_reader(r: &impl Rd, repo_id: &str) -> AppResult<String> {
    crate::domain::repo_by_id(r, repo_id)?
        .upstream
        .map(|u| u.reader.member)
        .ok_or_else(|| AppError::conflict("The repository has no upstream any more."))
}

/// Pull requests the poller still has work on: published, not merged, and
/// open here or open there.
fn live_pulls(r: &impl Rd, repo_id: &str) -> AppResult<Vec<Pull>> {
    Ok(
        r.prefix_all::<Pull>(store::PULLS, &Key::new().s(repo_id).bytes())?
            .into_iter()
            .filter(|p| {
                !p.merged
                    && p.upstream.as_ref().is_some_and(|x| {
                        p.state == "open" || (x.number.is_some() && x.state == "open")
                    })
            })
            .collect(),
    )
}

/// The issues a pull request's ticket links name: `(URL, closes)`, from the
/// linked Branvil tickets' `upstream_issue` and links that are URLs
/// themselves. A link closes there only when it closes the Branvil ticket.
pub fn linked_issues(r: &impl Rd, p: &Pull) -> AppResult<Vec<(String, bool)>> {
    let mut out = Vec::new();
    for link in &p.tickets {
        let issue = match &link.local {
            Some((repo, n)) => r
                .get::<Ticket>(store::TICKETS, &Ticket::key(repo, *n))?
                .and_then(|t| t.upstream_issue)
                .map(|url| (url, link.closes)),
            None => Some((link.url.clone(), false)),
        };
        out.extend(issue);
    }
    Ok(out)
}

/// What one of Branvil's references stands for on the host.
#[derive(Debug, Clone)]
enum Target {
    /// A ticket that names an issue there (its URL).
    Issue(String),
    /// A pull request published there.
    Pull {
        provider: String,
        repository: String,
        number: u64,
        url: Option<String>,
    },
    /// Something of Branvil's only, by Branvil's name for it.
    Branvil(String),
}

/// What each reference in `texts` stands for; references to repositories
/// that are not Branvil's are left out (they name something on the host).
fn targets(
    r: &impl Rd,
    p: &Pull,
    texts: &[&str],
    base_url: &str,
) -> AppResult<HashMap<references::Reference, Target>> {
    let own = crate::domain::repo_by_id(r, &p.repo)?;
    let mut out = HashMap::new();
    for text in texts {
        for (_, reference) in references::find(text, base_url) {
            if out.contains_key(&reference) {
                continue;
            }
            let Some((repo, item)) = markdown::resolve(r, &own, &reference)? else {
                continue;
            };
            let n = reference.number;
            let target = match item {
                Item::Ticket(t) => t
                    .upstream_issue
                    .map(Target::Issue)
                    .unwrap_or_else(|| Target::Branvil(format!("{}#{n}", repo.path))),
                Item::Pull(q) => q
                    .upstream
                    .and_then(|x| {
                        Some(Target::Pull {
                            number: x.number?,
                            provider: x.provider,
                            repository: x.repository,
                            url: x.url,
                        })
                    })
                    .unwrap_or_else(|| Target::Branvil(format!("{}!{n}", repo.path))),
            };
            out.insert(reference, target);
        }
    }
    Ok(out)
}

/// The title and body a pull request has on the host: its own, with
/// Branvil's references rewritten for the host, plus a line for every linked
/// issue of the host.
pub async fn compose(at: Place<'_>, p: &Pull) -> AppResult<(String, String)> {
    let (p2, base_url) = (p.clone(), at.app.base_url.clone());
    let (issues, targets) = at
        .app
        .store
        .read(move |r| {
            let texts = [p2.title.as_str(), p2.body.as_str()];
            Ok((linked_issues(r, &p2)?, targets(r, &p2, &texts, &base_url)?))
        })
        .await?;
    let (host, here) = (at.host, at.setting.repository.as_str());
    let replace = |reference: &references::Reference| {
        let literal = |name: &str| host.literal(name);
        Some(match targets.get(reference)? {
            Target::Issue(url) if reference.url => url.clone(),
            Target::Issue(url) => host
                .issue_reference(here, url)
                .unwrap_or_else(|| literal(url)),
            Target::Pull {
                provider,
                repository,
                number,
                url,
            } if provider == host.name() => match (reference.url, url) {
                (true, Some(url)) => url.clone(),
                _ => host.pull_reference(here, repository, *number),
            },
            Target::Pull { number, .. } => literal(&format!("!{number}")),
            Target::Branvil(name) => literal(name),
        })
    };
    let title = references::rewrite(&p.title, &at.app.base_url, replace);
    let mut body = references::rewrite(p.body.trim_end(), &at.app.base_url, replace);
    let mut lines: Vec<String> = Vec::new();
    for (url, closes) in issues {
        if let Some(line) = at.host.issue_line(here, &url, closes)
            && !lines.contains(&line)
        {
            lines.push(line);
        }
    }
    if !lines.is_empty() {
        if !body.is_empty() {
            body.push_str("\n\n");
        }
        body.push_str(&lines.join("\n"));
    }
    Ok((title, body))
}

// ---------------------------------------------------------------- the poller

/// Starts the poller. Call once, after startup recovery.
pub fn start(app: Shared) {
    tokio::spawn(async move {
        loop {
            let started = util::now();
            let active = poll_all(&app).await;
            let secs = if active {
                app.upstream.config.poll_active_secs
            } else {
                app.upstream.config.poll_idle_secs
            };
            let mut wait = Duration::from_secs(secs.max(1));
            match app.store.read(move |r| registration_due(r, &started)).await {
                Ok(Some(by)) => {
                    if let Ok(by) = chrono::DateTime::parse_from_rfc3339(&by) {
                        let by = by.with_timezone(&chrono::Utc);
                        wait = wait.min((by - chrono::Utc::now()).to_std().unwrap_or_default());
                    }
                }
                Ok(None) => {}
                Err(e) => tracing::warn!("upstream: {e}"),
            }
            tokio::select! {
                _ = app.upstream.wake.notified() => {}
                _ = tokio::time::sleep(wait) => {}
            }
        }
    });
}

/// The next registration deadline of a live pull request. An expired
/// deadline has already had its poll: if reading failed, retry at the usual
/// interval rather than spinning. One that expired during this poll still
/// needs a read after it, so compare with the poll's start, not its end.
fn registration_due(r: &redb::ReadTransaction, started: &str) -> AppResult<Option<String>> {
    let mut next = None;
    for repo in upstream_repos(r)? {
        for p in live_pulls(r, &repo.id)? {
            if let Some(by) = p.upstream.and_then(|x| x.checks_registered_by)
                && by.as_str() > started
                && next.as_ref().is_none_or(|n| &by < n)
            {
                next = Some(by);
            }
        }
    }
    Ok(next)
}

/// Polls every repository with an upstream; true while any published pull
/// request is still live.
async fn poll_all(app: &App) -> bool {
    let repos = match app.store.read(upstream_repos).await {
        Ok(r) => r,
        Err(e) => {
            tracing::error!("upstream: {e}");
            return false;
        }
    };
    let mut active = false;
    for repo in repos {
        active |= poll_repo(app, &repo).await;
    }
    active
}

/// One poll of one repository: its default branch, then its live published
/// pull requests, each an operation of its own (`App::finish`): what it sends
/// to the host and what it records of it are one write.
pub async fn poll_repo(app: &App, repo: &Repo) -> bool {
    let Some(setting) = repo.upstream.clone() else {
        return false;
    };
    let _polling = app.upstream.repos.lock(repo.id.as_bytes()).await;
    let host = match app.upstream.hosts.get(&setting.provider) {
        Ok(h) => h,
        Err(e) => {
            note(app, repo, |f| f.error = Some(e.message.clone()));
            return false;
        }
    };
    let id = repo.id.clone();
    let loaded = app
        .store
        .read(move |r| {
            let name = setting_reader(r, &id)?;
            let reader = match name.as_str() {
                "" => None,
                name => Some(crate::domain::member_by_name(r, name)?),
            };
            Ok((reader, live_pulls(r, &id)?))
        })
        .await;
    let (reader_member, live) = match loaded {
        Ok(x) => x,
        Err(e) => {
            tracing::error!("upstream: {}: {e}", repo.path);
            return false;
        }
    };
    let Some(reader_member) = reader_member else {
        let why = format!(
            "No member has a {} account in Branvil to read the upstream with; set one (an agent's bot account is enough).",
            host.display_name()
        );
        note(app, repo, |f| f.error = Some(why));
        return !live.is_empty();
    };
    let token = match token_of(app, &reader_member, host).await {
        Ok(t) => t,
        Err(why) => {
            note(app, repo, |f| f.error = Some(why.clone()));
            return !live.is_empty();
        }
    };
    let polling = Polling {
        repo: repo.clone(),
        setting,
        reader: reader_member,
        token,
    };
    let p = polling.clone();
    app.finish(move |app| async move {
        if let Ok(at) = p.place(&app) {
            follow(at, p.as_reader()).await;
        }
    })
    .await;
    for number in live.iter().map(|p| p.number) {
        let p = polling.clone();
        let synced = app
            .finish(move |app| async move {
                let _guard = app.upstream.pull_lock(&p.repo.id, number).await;
                sync_pull(p.place(&app)?, p.as_reader(), &p.reader, number).await
            })
            .await;
        if let Err(e) = synced {
            tracing::warn!("upstream: {} !{number}: {e}", repo.path);
        }
    }
    !live.is_empty()
}

/// What a poll's operations need, owned for their tasks.
#[derive(Clone)]
struct Polling {
    repo: Repo,
    setting: Setting,
    reader: Member,
    token: String,
}

impl Polling {
    fn place<'a>(&'a self, app: &'a App) -> AppResult<Place<'a>> {
        Ok(Place {
            app,
            repo: &self.repo,
            host: app.upstream.hosts.get(&self.setting.provider)?,
            setting: &self.setting,
        })
    }

    fn as_reader(&self) -> As<'_> {
        As {
            member: &self.reader.name,
            token: &self.token,
        }
    }
}

fn note(app: &App, repo: &Repo, change: impl FnOnce(&mut Following)) {
    let changed = {
        let mut map = app.upstream.following.lock().unwrap();
        let entry = map.entry(repo.id.clone()).or_default();
        let before = entry.clone();
        change(entry);
        entry.checked_at = Some(util::now());
        before.error != entry.error || before.stopped != entry.stopped || before.tip != entry.tip
    };
    if changed {
        app.events.publish("upstream", &repo.path, "");
    }
}

/// Brings Branvil's default branch to the host's when it only moved ahead.
/// Never waits for the host while holding the repository lock.
pub async fn follow(at: Place<'_>, reader: As<'_>) {
    let (app, repo) = (at.app, at.repo);
    match follow_once(at, reader).await {
        Ok((tip, moved)) => note(app, repo, |f| {
            f.tip = Some(tip);
            f.error = None;
            f.stopped = None;
            if moved {
                f.followed_at = Some(util::now());
            }
        }),
        Err(Stop::Failed(why)) => note(app, repo, |f| f.error = Some(why)),
        Err(Stop::Stopped(tip, why)) => note(app, repo, |f| {
            f.tip = Some(tip);
            f.error = None;
            f.stopped = Some(why);
        }),
    }
}

enum Stop {
    Failed(String),
    Stopped(String, String),
}

impl From<AppError> for Stop {
    fn from(e: AppError) -> Self {
        Stop::Failed(e.message)
    }
}

pub fn hidden_ref(branch: &str) -> String {
    format!("refs/branvil/upstream/{branch}")
}

/// `(the host's tip, whether Branvil's branch moved)`.
async fn follow_once(at: Place<'_>, reader: As<'_>) -> Result<(String, bool), Stop> {
    let Place {
        app,
        repo,
        host,
        setting,
    } = at;
    let branch = &repo.default_branch;
    let tip = host
        .branch_tip(reader, &setting.repository, branch)
        .await
        .map_err(|e| Stop::Failed(e.message))?
        .ok_or_else(|| {
            Stop::Failed(format!(
                "{} {} has no branch {branch}.",
                host.display_name(),
                setting.repository
            ))
        })?;
    let dir = app.git.repo_dir(&repo.path);
    let refname = format!("refs/heads/{branch}");
    if app.git.all_refs(&dir).await?.get(&refname) == Some(&tip) {
        return Ok((tip, false));
    }
    let login = git::Login {
        username: host.git_username(),
        token: reader.token,
    };
    git::fetch(
        &app.git,
        &dir,
        &host.git_url(&setting.repository),
        &login,
        branch,
        &hidden_ref(branch),
    )
    .await
    .map_err(Stop::Failed)?;
    let _guard = app.lock_repo(&repo.id).await;
    let refs = app.git.all_refs(&dir).await?;
    let theirs = refs
        .get(&hidden_ref(branch))
        .cloned()
        .ok_or_else(|| Stop::Failed(format!("The fetch of {branch} left nothing.")))?;
    let ours = refs.get(&refname).cloned();
    let moved = match ours {
        Some(ours) if ours == theirs => false,
        Some(ours) => {
            if !app.git.is_ancestor(&dir, &ours, &theirs).await? {
                return Err(Stop::Stopped(
                    theirs,
                    format!(
                        "Branvil's {branch} has commits {}'s lacks; following stopped and nothing was overwritten.",
                        host.display_name()
                    ),
                ));
            }
            app.git
                .update_ref(&dir, &refname, &theirs, &ours, "branvil: follow upstream")
                .await?
        }
        None => {
            app.git
                .update_ref(
                    &dir,
                    &refname,
                    &theirs,
                    ZERO_SHA,
                    "branvil: follow upstream",
                )
                .await?
        }
    };
    crate::refs::record_refs_locked(app, repo).await?;
    Ok((theirs, moved))
}

/// Pushes `sha` to the host's branch, replacing only `lease`.
pub async fn push_branch(
    at: Place<'_>,
    writer: As<'_>,
    branch: &str,
    sha: &str,
    lease: &str,
) -> git::Pushed {
    let login = git::Login {
        username: at.host.git_username(),
        token: writer.token,
    };
    git::push(
        &at.app.git,
        &at.app.git.repo_dir(&at.repo.path),
        &at.host.git_url(&at.setting.repository),
        &login,
        sha,
        branch,
        lease,
    )
    .await
}

pub use git::Pushed;

/// Opens the host's pull request of a published one whose branch is there,
/// or adopts the open one with that head that the same account opened (a
/// publish that stopped between the two). Records the outcome on `x`.
pub async fn open_on_host(at: Place<'_>, writer: As<'_>, login: &str, p: &Pull, x: &mut Published) {
    let Place { host, setting, .. } = at;
    let found = match host
        .open_pull_with_head(writer, &setting.repository, &p.head)
        .await
    {
        Ok(found) => found.filter(|hp| hp.author.eq_ignore_ascii_case(login)),
        Err(e) => {
            x.error = Some(e.message);
            return;
        }
    };
    let (title, body) = match compose(at, p).await {
        Ok(x) => x,
        Err(e) => {
            x.error = Some(e.message);
            return;
        }
    };
    let created = match found {
        // Adopted: what it says is unknown, so the next poll sends ours.
        Some(hp) => Ok((hp, String::new(), String::new())),
        None => host
            .create_pull(writer, &setting.repository, &p.head, &p.base, &title, &body)
            .await
            .map(|hp| (hp, title, body)),
    };
    match created {
        Ok((hp, title, body)) => {
            x.number = Some(hp.number);
            x.url = Some(hp.url);
            x.state = hp.state.as_str().into();
            x.sent_state = "open".into();
            x.sent_title = title;
            x.sent_body = body;
            x.error = None;
            // The host runs the checks of pull requests only once they are
            // there; so does every event of one (an edit, a reopening).
            x.await_checks(at.app.upstream.config.checks_registration_secs);
        }
        Err(e) => x.error = Some(e.message),
    }
}

/// The login of a member's account on a host.
pub fn login_of<'a>(member: &'a Member, host: &Host) -> &'a str {
    member
        .accounts
        .iter()
        .find(|a| a.provider == host.name())
        .map(|a| a.login.as_str())
        .unwrap_or_default()
}

/// One poll of one published pull request, against the host repository it
/// was published to. `following` is the repository's current upstream, whose
/// default branch a merge lands on.
async fn sync_pull(
    following: Place<'_>,
    reader: As<'_>,
    reader_member: &Member,
    number: u64,
) -> AppResult<()> {
    let (app, repo) = (following.app, following.repo);
    let r2 = repo.clone();
    let p = app.store.read(move |r| load_pull(r, &r2, number)).await?;
    let Some(mut x) = p.upstream.clone() else {
        return Ok(());
    };
    let host = app.upstream.hosts.get(&x.provider)?;
    let setting = Setting {
        provider: x.provider.clone(),
        repository: x.repository.clone(),
        reader: following.setting.reader.clone(),
    };
    let at = Place {
        app,
        repo,
        host,
        setting: &setting,
    };
    let same_place = following.setting.same_place(&x.provider, &x.repository);
    let reader_token = if same_place {
        Ok(reader.token.to_string())
    } else {
        token_of(app, reader_member, host).await
    };
    let account = {
        let name = x.account.clone();
        app.store
            .read(move |r| crate::domain::member_by_name(r, &name))
            .await?
    };
    let token = token_of(app, &account, host).await;
    let writer = token.as_ref().ok().map(|t| As {
        member: &account.name,
        token: t,
    });
    let login = login_of(&account, host);
    let mut open = None;
    let mut observed_open = false;
    match (x.number, &reader_token) {
        (_, Err(why)) => x.error = Some(why.clone()),
        (None, Ok(_)) => {
            // Opened only once the branch is there and sending did not stop.
            if p.state == "open" && x.pushed.is_some() && x.stopped.is_none() {
                match writer {
                    Some(w) => open_on_host(at, w, login, &p, &mut x).await,
                    None => x.error = token.clone().err(),
                }
            }
        }
        (Some(n), Ok(reader_token)) => {
            let reader = As {
                member: &reader_member.name,
                token: reader_token,
            };
            match host.read_pull(reader, &setting.repository, n).await {
                Err(e) => x.error = Some(e.message),
                Ok(hp) if hp.state == PullState::Merged => {
                    x.state = "merged".into();
                    x.error = None;
                    let landed = match &hp.merge_sha {
                        Some(sha) if same_place => merged_here(following, reader, sha).await?,
                        _ => false,
                    };
                    if landed {
                        // Under the lock, so a backup has the merge together
                        // with the ref that keeps its head.
                        let _guard = app.lock_repo(&repo.id).await;
                        let merge_sha = hp.merge_sha.clone().unwrap_or_default();
                        let dir = app.git.repo_dir(&repo.path);
                        // What the pull request brought: from where its pushed
                        // head left the base, whichever way the host merged it.
                        let merged_base = match &x.pushed {
                            Some(pushed) => {
                                pulls::keep_merged(app, repo, number, pushed).await?;
                                app.git.merge_base(&dir, pushed, &merge_sha).await?
                            }
                            None => None,
                        };
                        let (r2, x2) = (repo.clone(), x.clone());
                        let merged = app
                            .store
                            .write(move |w| {
                                pulls::store_published(w, &r2, number, x2, None)?;
                                pulls::mark_merged(w, &r2, number, &merge_sha, merged_base)
                            })
                            .await?;
                        app.events.publish("pull", &repo.path, number.to_string());
                        crate::domain::tickets::publish_closed_by(app, &merged).await;
                        tracing::info!(
                            "upstream: {} !{number} merged on {}",
                            repo.path,
                            host.display_name()
                        );
                        pulls::retire_merged_head(app, repo, &merged).await?;
                        crate::landing::remove_merged_ref(app, repo, number).await?;
                        crate::refs::record_refs_locked(app, repo).await?;
                        return Ok(());
                    }
                }
                Ok(hp) => {
                    observed_open = hp.state == PullState::Open;
                    x.error = None;
                    x.state = hp.state.as_str().into();
                    let here = p.state.as_str();
                    if x.state != x.sent_state {
                        // The host closed or reopened it: so does Branvil.
                        x.sent_state = x.state.clone();
                        open = Some(x.state == "open");
                        if x.state == "open" {
                            x.await_checks(app.upstream.config.checks_registration_secs);
                        }
                    } else if here != x.sent_state {
                        // Branvil closed or reopened it: so does the host.
                        match writer {
                            Some(w) => {
                                let change = PullChange {
                                    open: Some(here == "open"),
                                    ..PullChange::default()
                                };
                                match host.change_pull(w, &setting.repository, n, &change).await {
                                    Ok(hp) => {
                                        x.state = hp.state.as_str().into();
                                        x.sent_state = here.into();
                                        if here == "open" {
                                            x.await_checks(
                                                app.upstream.config.checks_registration_secs,
                                            );
                                        }
                                    }
                                    Err(e) => x.error = Some(e.message),
                                }
                            }
                            None => x.error = token.clone().err(),
                        }
                    }
                    if here == "open" && x.state == "open" && open != Some(false) {
                        send_changes(at, reader, writer, &p, &mut x).await;
                        if writer.is_none() {
                            x.error = token.clone().err();
                        }
                    }
                }
            }
        }
    }
    let changed = {
        let _guard = app.lock_repo(&repo.id).await;
        let r2 = repo.clone();
        app.store
            .write(move |w| pulls::store_published(w, &r2, number, x, open))
            .await?
    };
    if changed.is_some() {
        app.events.publish("pull", &repo.path, number.to_string());
    }
    if observed_open && same_place {
        crate::landing::host_observed_open(app, repo, number).await?;
    }
    Ok(())
}

/// Whether the host's merge commit is on Branvil's default branch (following
/// the host first when it is not): a commit only fetched into the hidden ref,
/// while following is stopped, is not merged here.
async fn merged_here(at: Place<'_>, reader: As<'_>, sha: &str) -> AppResult<bool> {
    let git = &at.app.git;
    let dir = git.repo_dir(&at.repo.path);
    let branch = format!("refs/heads/{}", at.repo.default_branch);
    for attempt in 0..2 {
        if attempt == 1 {
            follow(at, reader).await;
        }
        if git.commit_exists(&dir, sha).await? && git.is_ancestor(&dir, sha, &branch).await? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Sends a new head, title, or body of an open published pull request, and
/// reads the host's checks of the pushed head.
async fn send_changes(
    at: Place<'_>,
    reader: As<'_>,
    writer: Option<As<'_>>,
    p: &Pull,
    x: &mut Published,
) {
    let Place { host, setting, .. } = at;
    if let Some(w) = writer {
        let head = match pulls::tips(at.app, at.repo).await {
            Ok(t) => t.get(&p.head).cloned(),
            Err(e) => {
                x.error = Some(e.message);
                return;
            }
        };
        if let Some(head) = head
            && x.stopped.is_none()
            && x.pushed.as_deref() != Some(head.as_str())
        {
            let lease = x.pushed.clone().unwrap_or_default();
            match push_branch(at, w, &p.head, &head, &lease).await {
                Pushed::Done => {
                    x.record_push(head, at.app.upstream.config.checks_registration_secs)
                }
                Pushed::Refused(why) => x.stopped = Some(why),
                Pushed::Failed(why) => x.error = Some(why),
            }
        }
        match compose(at, p).await {
            Ok((title, body)) if title != x.sent_title || body != x.sent_body => {
                let change = PullChange {
                    title: (title != x.sent_title).then(|| title.clone()),
                    body: (body != x.sent_body).then(|| body.clone()),
                    open: None,
                };
                let n = x.number.unwrap_or_default();
                match host.change_pull(w, &setting.repository, n, &change).await {
                    Ok(_) => {
                        x.sent_title = title;
                        x.sent_body = body;
                        // An edit runs the host's checks of edits.
                        x.await_checks(at.app.upstream.config.checks_registration_secs);
                    }
                    Err(e) => x.error = Some(e.message),
                }
            }
            Ok(_) => {}
            Err(e) => x.error = Some(e.message),
        }
    }
    if let Some(sha) = x.pushed.clone() {
        match host.checks(reader, &setting.repository, &sha).await {
            Ok(checks) => x.record_checks(checks),
            Err(e) => x.error = Some(e.message),
        }
    }
}

// ---------------------------------------------------------------- views

#[derive(Serialize, Debug, Clone)]
pub struct UpstreamView {
    pub provider: String,
    /// The provider's name for people ("GitHub").
    pub host: String,
    pub repository: String,
    pub url: String,
    /// Whose account reads it: `{member, chosen}` (`member` is `""` when no
    /// member has an account there).
    pub reader: crate::model::Reader,
    pub following: Following,
}

pub fn upstream_view(app: &App, repo: &Repo) -> Option<UpstreamView> {
    let setting = repo.upstream.as_ref()?;
    let host = app.upstream.hosts.get(&setting.provider).ok();
    Some(UpstreamView {
        provider: setting.provider.clone(),
        host: app.upstream.host_name(&setting.provider),
        repository: setting.repository.clone(),
        url: host
            .map(|h| h.web_url(&setting.repository))
            .unwrap_or_default(),
        reader: setting.reader.clone(),
        following: app.upstream.following(&repo.id),
    })
}
