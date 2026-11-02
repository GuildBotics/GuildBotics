//! Tickets of a repository, ticket comments and reactions, and the board of
//! a workspace with its cards.

use std::collections::HashSet;
use std::ops::Bound;

use axum::http::StatusCode;
use serde::{Deserialize, Serialize};

use crate::app::App;
use crate::auth::Caller;
use crate::domain::{
    self, CommentView, Page, PageReq, comment_view, repo_by_id, repo_by_path, repos,
};
use crate::error::{AppError, AppResult};
use crate::model::{self, Assignee, Comment, LANES, Pull, Repo, Target, Ticket};
use crate::rank;
use crate::store::{self, Key, Rd, Wtx};
use crate::util::{self, fold};

// ---------------------------------------------------------------- tickets

#[derive(Serialize, Debug, Clone)]
pub struct TicketView {
    pub url: String,
    pub repository: String,
    pub key: String,
    pub title: String,
    pub body: String,
    pub mentions: Vec<String>,
    pub state: String,
    pub state_reason: Option<String>,
    pub author: String,
    pub assignee: Option<Assignee>,
    pub labels: Vec<String>,
    /// Whether a person works on the ticket interactively.
    pub interactive: bool,
    /// The nearest ancestor a person works on interactively: the ticket is a
    /// part of that work too.
    pub interactive_ancestor: Option<TicketRef>,
    /// The issue it stands for on its repository's upstream host.
    pub upstream_issue: Option<String>,
    pub lane: String,
    pub rank: String,
    /// The ticket this one is a part of.
    pub parent: Option<TicketRef>,
    /// The tickets that are a part of this one.
    pub children: Vec<TicketRef>,
    /// The tickets that must be closed before this one is started.
    pub blocked_by: Vec<TicketRef>,
    /// The tickets this one must be closed before.
    pub blocks: Vec<TicketRef>,
    pub created_at: String,
    pub updated_at: String,
    pub closed_at: Option<String>,
    pub revision: u64,
}

impl TicketView {
    /// The tickets whose views show this one.
    fn related(&self) -> impl Iterator<Item = &TicketRef> {
        self.parent
            .iter()
            .chain(&self.children)
            .chain(&self.blocked_by)
            .chain(&self.blocks)
    }
}

/// A ticket another ticket is related to.
#[derive(Serialize, Debug, Clone)]
pub struct TicketRef {
    pub url: String,
    pub repository: String,
    pub key: String,
    pub title: String,
    pub state: String,
    pub state_reason: Option<String>,
}

/// A ticket by its (repository id, number).
pub type TicketId = (String, u64);

fn id_of(t: &Ticket) -> TicketId {
    (t.repo.clone(), t.number)
}

fn ticket_by_id(r: &impl Rd, id: &TicketId) -> AppResult<Ticket> {
    r.get(store::TICKETS, &Ticket::key(&id.0, id.1))?
        .ok_or_else(|| AppError::internal("a relation names a missing ticket"))
}

/// The tickets that name `id` as their `kind`: its children (`parent`), or
/// the tickets it blocks (`blocked_by`).
fn referrers(r: &impl Rd, id: &TicketId, kind: &str) -> AppResult<Vec<TicketId>> {
    r.prefix_keys(store::IX_TICKET_REFS, &Ticket::refs_prefix(id, kind))?
        .iter()
        .map(|k| {
            let parts = store::decode_key(k, "sussu")
                .ok_or_else(|| AppError::internal("bad relation index"))?;
            Ok((parts[3].s().to_string(), parts[4].u()))
        })
        .collect()
}

/// The tickets that are a part of `id`, at any depth.
fn descendants(r: &impl Rd, id: &TicketId) -> AppResult<Vec<TicketId>> {
    let mut found = Vec::new();
    let mut next = vec![id.clone()];
    while let Some(id) = next.pop() {
        let children = referrers(r, &id, "parent")?;
        next.extend(children.iter().cloned());
        found.extend(children);
    }
    Ok(found)
}

/// The nearest of `t`'s ancestors a person works on interactively.
pub(crate) fn interactive_ancestor(r: &impl Rd, t: &Ticket) -> AppResult<Option<TicketId>> {
    let mut next = t.parent.clone();
    while let Some(id) = next {
        let ancestor = ticket_by_id(r, &id)?;
        if ancestor.interactive {
            return Ok(Some(id));
        }
        next = ancestor.parent;
    }
    Ok(None)
}

fn ticket_ref(r: &impl Rd, base_url: &str, id: &TicketId) -> AppResult<TicketRef> {
    let t = ticket_by_id(r, id)?;
    let repo = repo_by_id(r, &t.repo)?;
    Ok(TicketRef {
        url: ticket_url(base_url, &repo, t.number),
        repository: repo.path,
        key: t.number.to_string(),
        title: t.title,
        state: t.state,
        state_reason: t.state_reason,
    })
}

/// Whether one of `ids` is an open ticket.
fn any_open(r: &impl Rd, ids: &[TicketId]) -> AppResult<bool> {
    for id in ids {
        if ticket_by_id(r, id)?.state == "open" {
            return Ok(true);
        }
    }
    Ok(false)
}

fn has_open_children(r: &impl Rd, id: &TicketId) -> AppResult<bool> {
    any_open(r, &referrers(r, id, "parent")?)
}

/// Refuses to complete `t` while one of its children is open.
fn check_children_closed(r: &impl Rd, t: &Ticket) -> AppResult<()> {
    if has_open_children(r, &id_of(t))? {
        return Err(AppError::new(
            StatusCode::CONFLICT,
            "children_open",
            format!(
                "Ticket {} has open child tickets; close them before completing it.",
                t.number
            ),
        ));
    }
    Ok(())
}

/// Whether ticket `from` reaches `to` over the edges "must be closed before
/// the other proceeds": from a child to its parent, from a blocker to the
/// tickets it blocks. A relation closing such a path would make tickets wait
/// on each other forever.
fn reaches(r: &impl Rd, from: &TicketId, to: &TicketId) -> AppResult<bool> {
    let mut seen = HashSet::new();
    let mut next = vec![from.clone()];
    while let Some(id) = next.pop() {
        if &id == to {
            return Ok(true);
        }
        if seen.insert(id.clone()) {
            next.extend(ticket_by_id(r, &id)?.parent);
            next.extend(referrers(r, &id, "blocked_by")?);
        }
    }
    Ok(false)
}

/// Tells the GUI that the tickets of `views` changed, and so did the views of
/// the tickets related to them.
fn publish_tickets<'a>(app: &App, views: impl IntoIterator<Item = &'a TicketView>) {
    let mut seen = HashSet::new();
    for v in views {
        let related = v.related().map(|r| (&r.repository, &r.key));
        for (repo, key) in std::iter::once((&v.repository, &v.key)).chain(related) {
            if seen.insert((repo, key)) {
                app.events.publish("ticket", repo, key.clone());
            }
        }
    }
}

/// Tells the GUI that the tickets merged pull request `p` closes changed
/// (`publish_tickets`).
pub async fn publish_closed_by(app: &App, p: &Pull) {
    let ids: Vec<TicketId> = p
        .tickets
        .iter()
        .filter(|l| l.closes)
        .filter_map(|l| l.local.clone())
        .collect();
    let base_url = app.base_url.clone();
    let views = app
        .store
        .read(move |r| {
            let mut views = Vec::new();
            for id in &ids {
                if let Some(t) = r.get::<Ticket>(store::TICKETS, &Ticket::key(&id.0, id.1))? {
                    views.push(ticket_view(r, &base_url, &repo_by_id(r, &t.repo)?, &t)?);
                }
            }
            Ok(views)
        })
        .await;
    match views {
        Ok(views) => publish_tickets(app, &views),
        Err(e) => tracing::warn!("reading the tickets a merge closed: {}", e.message),
    }
}

pub fn ticket_url(base_url: &str, repo: &Repo, number: u64) -> String {
    format!("{base_url}/{}/-/tickets/{number}", repo.path)
}

pub fn ticket_view(r: &impl Rd, base_url: &str, repo: &Repo, t: &Ticket) -> AppResult<TicketView> {
    let refs = |ids: &[TicketId]| -> AppResult<Vec<TicketRef>> {
        ids.iter().map(|id| ticket_ref(r, base_url, id)).collect()
    };
    let id = id_of(t);
    Ok(TicketView {
        url: ticket_url(base_url, repo, t.number),
        repository: repo.path.clone(),
        key: t.number.to_string(),
        title: t.title.clone(),
        body: t.body.clone(),
        mentions: t.mentions.clone(),
        state: t.state.clone(),
        state_reason: t.state_reason.clone(),
        author: t.author.clone(),
        assignee: t.assignee.clone(),
        labels: t.labels.clone(),
        interactive: t.interactive,
        interactive_ancestor: interactive_ancestor(r, t)?
            .map(|a| ticket_ref(r, base_url, &a))
            .transpose()?,
        upstream_issue: t.upstream_issue.clone(),
        lane: t.lane.clone(),
        rank: t.rank.clone(),
        parent: t
            .parent
            .as_ref()
            .map(|p| ticket_ref(r, base_url, p))
            .transpose()?,
        children: refs(&referrers(r, &id, "parent")?)?,
        blocked_by: refs(&t.blocked_by)?,
        blocks: refs(&referrers(r, &id, "blocked_by")?)?,
        created_at: t.created_at.clone(),
        updated_at: t.updated_at.clone(),
        closed_at: t.closed_at.clone(),
        revision: t.revision,
    })
}

/// Parses a ticket number as it appears in paths and URLs: decimal, from 1,
/// without leading zeros.
pub fn parse_number(key: &str) -> Option<u64> {
    if key.starts_with('0') || !key.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    key.parse().ok()
}

pub fn ticket_by_key(r: &impl Rd, repo: &Repo, key: &str) -> AppResult<Ticket> {
    let not_found = || AppError::not_found(format!("Ticket {key} was not found in {}.", repo.path));
    let n = parse_number(key).ok_or_else(not_found)?;
    r.get(store::TICKETS, &Ticket::key(&repo.id, n))?
        .ok_or_else(not_found)
}

/// Resolves a ticket URL of this Branvil. `None` when the URL is not ours.
pub fn resolve_ticket_url(
    r: &impl Rd,
    base_url: &str,
    url: &str,
) -> Option<AppResult<(Repo, Ticket)>> {
    let rest = url.strip_prefix(base_url)?.strip_prefix('/')?;
    let Some((repo_path, key)) = rest.split_once("/-/tickets/") else {
        return Some(Err(AppError::not_found(format!(
            "{url} is not a ticket URL of this Branvil."
        ))));
    };
    Some((|| {
        let repo = repo_by_path(r, repo_path)?;
        let ticket = ticket_by_key(r, &repo, key)?;
        Ok((repo, ticket))
    })())
}

fn write_ticket(w: &Wtx, old: Option<&Ticket>, new: &Ticket) -> AppResult<()> {
    model::put_indexed(
        w,
        store::TICKETS,
        &Ticket::key(&new.repo, new.number),
        old.map(model::ticket_index),
        new,
        model::ticket_index(new),
    )
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewTicket {
    pub title: String,
    #[serde(default)]
    pub body: String,
    #[serde(default)]
    pub labels: Vec<String>,
    /// The lane of its workspace's board the ticket goes to the end of.
    pub lane: Option<String>,
    #[serde(default)]
    pub interactive: bool,
    pub assignee: Option<String>,
    pub parent: Option<String>,
    #[serde(default)]
    pub blocked_by: Vec<String>,
    /// The issue it stands for on the repository's upstream host.
    pub upstream_issue: Option<String>,
}

fn undefined_labels(repo: &Repo, labels: &[String]) -> AppResult<()> {
    let missing: Vec<&String> = labels.iter().filter(|l| !repo.labels.contains(l)).collect();
    if missing.is_empty() {
        return Ok(());
    }
    let names: Vec<&str> = missing.iter().map(|s| s.as_str()).collect();
    Err(AppError::new(
        axum::http::StatusCode::BAD_REQUEST,
        "label_undefined",
        format!(
            "Label {} is not defined by {}; defined labels: {}.",
            names.join(", "),
            repo.path,
            if repo.labels.is_empty() {
                "(none)".to_string()
            } else {
                repo.labels.join(", ")
            }
        ),
    ))
}

pub async fn create_ticket(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    input: NewTicket,
) -> AppResult<TicketView> {
    let repo = repos::visible(app, caller, repo_path).await?;
    domain::check_title(&input.title)?;
    domain::check_text("body", &input.body, domain::MAX_TEXT)?;
    let lane = input.lane.clone().unwrap_or_else(|| {
        if input.parent.is_some() || !input.blocked_by.is_empty() {
            "ready"
        } else {
            "new"
        }
        .into()
    });
    check_lane(&lane)?;
    let upstream_issue = input
        .upstream_issue
        .as_deref()
        .map(|url| app.upstream.check_issue(&repo, url))
        .transpose()?;
    let author = caller.name().to_string();
    let base_url = app.base_url.clone();
    let view = app
        .store
        .write(move |w| {
            let repo = repo_by_id(w, &repo.id)?;
            undefined_labels(&repo, &input.labels)?;
            let mut labels: Vec<String> = Vec::new();
            for l in input.labels {
                if !labels.contains(&l) {
                    labels.push(l);
                }
            }
            let number = w.next(&format!("ticket:{}", repo.id))?;
            let now = util::now();
            let related = |url: &str| related_ticket(w, &base_url, &repo, url);
            let parent = input.parent.as_deref().map(related).transpose()?;
            let mut blocked_by = Vec::new();
            for url in &input.blocked_by {
                let id = related(url)?;
                if !blocked_by.contains(&id) {
                    blocked_by.push(id);
                }
            }
            if lane == "in_progress" && any_open(w, &blocked_by)? {
                return Err(AppError::new(
                    StatusCode::CONFLICT,
                    "blocked",
                    "Close blocking tickets before starting this ticket.",
                ));
            }
            let assignee = input
                .assignee
                .as_deref()
                .map(|name| domain::member_by_name(w, name))
                .transpose()?
                .map(|member| Assignee {
                    name: member.name,
                    assigned_at: now.clone(),
                });
            let t = Ticket {
                repo: repo.id.clone(),
                workspace: repo.workspace.clone(),
                number,
                title: input.title,
                mentions: model::mentions(w, &input.body)?,
                body: input.body,
                state: "open".into(),
                state_reason: None,
                author,
                assignee,
                labels,
                interactive: input.interactive,
                upstream_issue,
                rank: rank_in(w, &repo.workspace, (&repo.id, number), &lane, None, None)?,
                lane,
                parent,
                blocked_by,
                created_at: now.clone(),
                updated_at: now,
                closed_at: None,
                revision: 1,
            };
            write_ticket(w, None, &t)?;
            // Check the combined relation graph, as updates do. A parent
            // that also waits on this ticket would otherwise create a cycle.
            let id = id_of(&t);
            for (from, to) in t
                .parent
                .iter()
                .map(|p| (p, &id))
                .chain(t.blocked_by.iter().map(|b| (&id, b)))
            {
                if reaches(w, from, to)? {
                    return Err(AppError::invalid(
                        "These relations make tickets wait on each other.",
                    ));
                }
            }
            ticket_view(w, &base_url, &repo, &t)
        })
        .await?;
    publish_tickets(app, [&view]);
    app.events
        .publish("board", workspace_of(&view.repository), "");
    Ok(view)
}

/// Resolve a relation within the ticket's workspace before writing it.
fn related_ticket(r: &impl Rd, base_url: &str, repo: &Repo, url: &str) -> AppResult<TicketId> {
    let other = match resolve_ticket_url(r, base_url, url) {
        Some(Ok((_, other))) => other,
        Some(Err(e)) if e.code != "not_found" => return Err(e),
        _ => {
            return Err(AppError::invalid(format!(
                "{url} is not a ticket of this Branvil."
            )));
        }
    };
    if other.workspace != repo.workspace {
        return Err(AppError::invalid(format!(
            "{url} is not a ticket of workspace {}.",
            workspace_of(&repo.path)
        )));
    }
    Ok(id_of(&other))
}

/// The workspace name of a repository path.
pub fn workspace_of(repo_path: &str) -> &str {
    repo_path.split_once('/').map_or(repo_path, |(ws, _)| ws)
}

fn check_lane(lane: &str) -> AppResult<()> {
    match model::lane_index(lane) {
        Some(_) => Ok(()),
        None => Err(AppError::invalid(format!(
            "lane must be one of {}.",
            LANES.join(", ")
        ))),
    }
}

#[derive(Deserialize, Debug, Default)]
pub struct TicketQuery {
    pub q: Option<String>,
    pub state: Option<String>,
    pub lane: Option<String>,
    pub assignee: Option<String>,
    pub label: Option<String>,
}

fn matches(t: &Ticket, q: &TicketQuery, me: &str) -> bool {
    let state_ok = match q.state.as_deref().unwrap_or("open") {
        "all" => true,
        s => t.state == s,
    };
    let lane_ok = q.lane.as_ref().is_none_or(|l| &t.lane == l);
    let assignee_ok = q.assignee.as_ref().is_none_or(|a| {
        let a = if a == "me" { me } else { a.as_str() };
        t.assignee
            .as_ref()
            .is_some_and(|x| fold(&x.name) == fold(a))
    });
    let text_ok = q.q.as_ref().is_none_or(|text| {
        let title = t.title.to_lowercase();
        let body = t.body.to_lowercase();
        text.split_whitespace().all(|word| {
            let word = word.to_lowercase();
            title.contains(&word) || body.contains(&word)
        })
    });
    state_ok && lane_ok && assignee_ok && text_ok
}

pub async fn list_tickets(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    q: TicketQuery,
    page: PageReq,
) -> AppResult<Page<TicketView>> {
    if let Some(s) = &q.state
        && !["open", "closed", "all"].contains(&s.as_str())
    {
        return Err(AppError::invalid("state must be open, closed, or all."));
    }
    if let Some(l) = &q.lane {
        check_lane(l)?;
    }
    let repo = repos::visible(app, caller, repo_path).await?;
    let me = caller.name().to_string();
    let base_url = app.base_url.clone();
    app.store
        .read(move |r| {
            let (items, next_cursor) = match q.label.clone() {
                // The label index gives the tickets carrying a label in number order.
                Some(label) => {
                    let prefix = Key::new().s(&repo.id).s(&label).bytes();
                    page.scan(r, store::IX_TICKET_LABELS, &prefix, |k, _| {
                        let parts = store::decode_key(k, "ssu")
                            .ok_or_else(|| AppError::internal("bad label index"))?;
                        let t: Ticket = r
                            .get(store::TICKETS, &Ticket::key(&repo.id, parts[2].u()))?
                            .ok_or_else(|| {
                                AppError::internal("label index names a missing ticket")
                            })?;
                        Ok(if matches(&t, &q, &me) {
                            Some(ticket_view(r, &base_url, &repo, &t)?)
                        } else {
                            None
                        })
                    })?
                }
                None => {
                    let prefix = Key::new().s(&repo.id).bytes();
                    page.scan(r, store::TICKETS, &prefix, |_, v| {
                        let t: Ticket = store::decode(v)?;
                        Ok(if matches(&t, &q, &me) {
                            Some(ticket_view(r, &base_url, &repo, &t)?)
                        } else {
                            None
                        })
                    })?
                }
            };
            Ok(Page { items, next_cursor })
        })
        .await
}

pub async fn get_ticket(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
) -> AppResult<TicketView> {
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    let base_url = app.base_url.clone();
    app.store
        .read(move |r| {
            let t = ticket_by_key(r, &repo, &key)?;
            ticket_view(r, &base_url, &repo, &t)
        })
        .await
}

/// Deletes only an unreferenced ticket. Checking references and removing all
/// source records and their indexes share the single writer transaction.
pub async fn delete_ticket(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
) -> AppResult<()> {
    caller.require_owner()?;
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    let base_url = app.base_url.clone();
    let view = app
        .store
        .write(move |w| {
            let t = ticket_by_key(w, &repo, &key)?;
            let id = id_of(&t);
            let pull_prefix = Key::new().s(&t.repo).u(t.number).bytes();
            for (present, code, reason) in [
                (
                    !referrers(w, &id, "parent")?.is_empty(),
                    "delete_has_children",
                    "it has child tickets",
                ),
                (
                    !referrers(w, &id, "blocked_by")?.is_empty(),
                    "delete_blocks_tickets",
                    "other tickets are blocked by it",
                ),
                (
                    !w.prefix_keys(store::IX_TICKET_PULLS, &pull_prefix)?
                        .is_empty(),
                    "delete_linked_pull",
                    "it is linked to a pull request",
                ),
                (
                    t.upstream_issue.is_some(),
                    "delete_upstream_issue",
                    "it is linked to an upstream issue",
                ),
            ] {
                if present {
                    return Err(AppError::new(
                        StatusCode::CONFLICT,
                        code,
                        format!("Ticket {key} cannot be deleted because {reason}."),
                    ));
                }
            }
            // Capture related tickets for event invalidation before deleting.
            let view = ticket_view(w, &base_url, &repo, &t)?;
            let target = ticket_target(&t);
            for c in comments_of(w, &target)? {
                let key = target.comment_key(c.seq);
                w.del(store::COMMENTS, &key)?;
                model::reindex(w, model::comment_index(&c, &key), Vec::new())?;
            }
            w.del(store::TICKETS, &Ticket::key(&t.repo, t.number))?;
            model::reindex(w, model::ticket_index(&t), Vec::new())?;
            // Keep the repository's ticket counter: numbers are never reused.
            Ok(view)
        })
        .await?;
    publish_tickets(app, [&view]);
    app.events
        .publish("board", workspace_of(&view.repository), "");
    Ok(())
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct TicketPatch {
    pub title: Option<String>,
    pub body: Option<String>,
    #[serde(default)]
    pub add_labels: Vec<String>,
    #[serde(default)]
    pub remove_labels: Vec<String>,
    pub interactive: Option<bool>,
    pub state: Option<String>,
    #[serde(default, deserialize_with = "domain::double_option")]
    pub state_reason: Option<Option<String>>,
    /// The issue it stands for on the upstream host, or `null` for none.
    #[serde(default, deserialize_with = "domain::double_option")]
    pub upstream_issue: Option<Option<String>>,
    /// The URL of the ticket this one is a part of, or `null` for none.
    #[serde(default, deserialize_with = "domain::double_option")]
    pub parent: Option<Option<String>>,
    /// URLs of tickets that must be closed before this one is started.
    #[serde(default)]
    pub add_blocked_by: Vec<String>,
    #[serde(default)]
    pub remove_blocked_by: Vec<String>,
    pub revision: Option<u64>,
}

#[derive(Serialize, Debug)]
pub struct TicketUpdated {
    pub ticket: TicketView,
    pub state_changed: bool,
}

/// Applies a state change to a ticket record. Returns whether the state moved.
pub fn set_state(t: &mut Ticket, state: &str, reason: Option<String>, at: &str) -> bool {
    let changed = t.state != state;
    t.state = state.to_string();
    if state == "closed" {
        t.state_reason = Some(reason.unwrap_or_else(|| "completed".into()));
        if changed || t.closed_at.is_none() {
            t.closed_at = Some(at.to_string());
        }
    } else {
        t.state_reason = None;
        t.closed_at = None;
    }
    changed
}

pub async fn update_ticket(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    patch: TicketPatch,
) -> AppResult<TicketUpdated> {
    let repo = repos::visible(app, caller, repo_path).await?;
    if let Some(t) = &patch.title {
        domain::check_title(t)?;
    }
    if let Some(b) = &patch.body {
        domain::check_text("body", b, domain::MAX_TEXT)?;
    }
    if let Some(s) = &patch.state
        && s != "open"
        && s != "closed"
    {
        return Err(AppError::invalid("state must be open or closed."));
    }
    if let Some(Some(r)) = &patch.state_reason
        && r != "completed"
        && r != "not_planned"
    {
        return Err(AppError::invalid(
            "state_reason must be completed, not_planned, or null.",
        ));
    }
    let upstream_issue = match &patch.upstream_issue {
        Some(Some(url)) => Some(Some(app.upstream.check_issue(&repo, url)?)),
        Some(None) => Some(None),
        None => None,
    };
    let key = key.to_string();
    let base_url = app.base_url.clone();
    let (result, before, descendants) = app
        .store
        .write(move |w| {
            let repo = repo_by_id(w, &repo.id)?;
            let old = ticket_by_key(w, &repo, &key)?;
            let before = ticket_view(w, &base_url, &repo, &old)?;
            if let Some(rev) = patch.revision
                && rev != old.revision
            {
                return Err(AppError::conflict(format!(
                    "Ticket {key} is at revision {}, not {rev}.",
                    old.revision
                )));
            }
            undefined_labels(&repo, &patch.add_labels)?;
            let mut t = old.clone();
            if let Some(title) = patch.title {
                t.title = title;
            }
            if let Some(body) = patch.body {
                t.mentions = model::mentions(w, &body)?;
                t.body = body;
            }
            if let Some(interactive) = patch.interactive {
                t.interactive = interactive;
            }
            if let Some(issue) = upstream_issue {
                t.upstream_issue = issue;
            }
            // Related tickets are of the same workspace, as its board is.
            let related = |url: &str| related_ticket(w, &base_url, &repo, url);
            if let Some(parent) = &patch.parent {
                t.parent = parent.as_deref().map(related).transpose()?;
            }
            for url in &patch.add_blocked_by {
                let id = related(url)?;
                if !t.blocked_by.contains(&id) {
                    t.blocked_by.push(id);
                }
            }
            for url in &patch.remove_blocked_by {
                let id = related(url)?;
                t.blocked_by.retain(|b| b != &id);
            }
            // Labels change against the stored set, so concurrent changes of
            // other labels are kept.
            for l in patch.add_labels {
                if !t.labels.contains(&l) {
                    t.labels.push(l);
                }
            }
            t.labels.retain(|l| !patch.remove_labels.contains(l));
            let now = util::now();
            let mut state_changed = false;
            match (patch.state, patch.state_reason) {
                (Some(state), reason) => {
                    let reason = reason
                        .flatten()
                        .or_else(|| old.state_reason.clone().filter(|_| state == old.state));
                    state_changed = set_state(&mut t, &state, reason, &now);
                }
                (None, Some(reason)) if t.state == "closed" => {
                    t.state_reason = Some(reason.unwrap_or_else(|| "completed".into()));
                }
                (None, Some(Some(_))) => {
                    return Err(AppError::invalid(
                        "state_reason applies to a closed ticket.",
                    ));
                }
                _ => {}
            }
            let completed = |x: &Ticket| {
                x.state == "closed" && x.state_reason.as_deref() == Some("completed")
            };
            if completed(&t) && !completed(&old) {
                check_children_closed(w, &t)?;
            }
            // A ticket closed on the board leaves its lane for done, as one
            // closed by a merge does.
            if state_changed && t.state == "closed" && t.lane != "done" {
                t = place(w, &t, "done", None, None)?;
            }
            let changed = serde_json::to_value(&t).ok() != serde_json::to_value(&old).ok();
            if changed {
                t.revision += 1;
                t.updated_at = now;
                write_ticket(w, Some(&old), &t)?;
                // Checked on the written relations, so that the new ones
                // are checked together.
                let id = id_of(&t);
                let new_parent = t.parent.as_ref().filter(|_| t.parent != old.parent);
                let new_blockers = t.blocked_by.iter().filter(|b| !old.blocked_by.contains(b));
                for (from, to) in new_parent
                    .map(|p| (p, &id))
                    .into_iter()
                    .chain(new_blockers.map(|b| (&id, b)))
                {
                    if reaches(w, from, to)? {
                        return Err(AppError::invalid(format!(
                            "Ticket {key} cannot have that relation: the tickets would wait on each other."
                        )));
                    }
                }
            }
            // Its descendants show the interactive ancestor they inherit, by
            // its title.
            let descendants = if t.interactive != old.interactive
                || t.parent != old.parent
                || t.title != old.title
            {
                descendants(w, &id_of(&t))?
                    .iter()
                    .map(|d| ticket_ref(w, &base_url, d))
                    .collect::<AppResult<_>>()?
            } else {
                Vec::new()
            };
            Ok((
                TicketUpdated {
                    ticket: ticket_view(w, &base_url, &repo, &t)?,
                    state_changed,
                },
                before,
                descendants,
            ))
        })
        .await?;
    publish_tickets(app, [&before, &result.ticket]);
    for d in &descendants {
        app.events.publish("ticket", &d.repository, d.key.clone());
    }
    Ok(result)
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewAssignee {
    pub name: String,
}

/// Sets the ticket's one assignee (`Some(name)`) or clears it (`None`).
/// Setting the current assignee again keeps its `assigned_at`.
pub async fn set_assignee(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    name: Option<String>,
) -> AppResult<TicketView> {
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    let base_url = app.base_url.clone();
    let (view, changed) = app
        .store
        .write(move |w| {
            let assignee = name
                .map(|n| domain::member_by_name(w, &n))
                .transpose()?
                .map(|m| m.name);
            let old = ticket_by_key(w, &repo, &key)?;
            let mut t = old.clone();
            if t.assignee.as_ref().map(|a| fold(&a.name)) == assignee.as_deref().map(fold) {
                return Ok((ticket_view(w, &base_url, &repo, &t)?, false));
            }
            let now = util::now();
            t.assignee = assignee.map(|name| Assignee {
                name,
                assigned_at: now.clone(),
            });
            t.revision += 1;
            t.updated_at = now;
            write_ticket(w, Some(&old), &t)?;
            Ok((ticket_view(w, &base_url, &repo, &t)?, true))
        })
        .await?;
    if changed {
        app.events
            .publish("ticket", &view.repository, view.key.clone());
    }
    Ok(view)
}

/// Where a ticket goes on its workspace's board.
#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Placement {
    pub lane: String,
    pub before: Option<String>,
    pub after: Option<String>,
}

/// The card key of the ticket named `card` (`<repository path>#<number>`)
/// when it is a card of `lane` on the board of `workspace` other than
/// `exclude`.
fn lane_card(
    r: &impl Rd,
    workspace: &str,
    lane: &str,
    card: &str,
    exclude: (&str, u64),
) -> AppResult<Vec<u8>> {
    let not_found =
        || AppError::not_found(format!("Card {card} is not in lane {lane} of the board."));
    let (repo_path, key) = card.rsplit_once('#').ok_or_else(not_found)?;
    let repo_id = domain::path_id(r, "repo", repo_path)?.ok_or_else(not_found)?;
    let number = parse_number(key).ok_or_else(not_found)?;
    if (repo_id.as_str(), number) == exclude {
        return Err(not_found());
    }
    let ticket: Ticket = r
        .get(store::TICKETS, &Ticket::key(&repo_id, number))?
        .ok_or_else(not_found)?;
    if ticket.workspace != workspace || ticket.lane != lane {
        return Err(not_found());
    }
    Ok(Ticket::card_key(
        workspace,
        lane,
        &ticket.rank,
        &repo_id,
        number,
    ))
}

/// The rank of the card next to `from` (exclusive; the lane's end when
/// `None`) in the given direction, skipping `exclude`. Reads at most two
/// index entries, whatever the lane holds.
fn neighbour(
    r: &impl Rd,
    lane_prefix: &[u8],
    from: Option<&[u8]>,
    forward: bool,
    exclude: (&str, u64),
) -> AppResult<Option<(Vec<u8>, String)>> {
    let lane_end = match store::prefix_end(lane_prefix) {
        Some(e) => Bound::Excluded(e),
        None => Bound::Unbounded,
    };
    let entries = if forward {
        let start = match from {
            Some(k) => Bound::Excluded(k.to_vec()),
            None => Bound::Included(lane_prefix.to_vec()),
        };
        r.range_raw(store::IX_CARDS, start, lane_end, 2)?
    } else {
        let end = match from {
            Some(k) => Bound::Excluded(k.to_vec()),
            None => lane_end,
        };
        r.range_rev_raw(
            store::IX_CARDS,
            Bound::Included(lane_prefix.to_vec()),
            end,
            2,
        )?
    };
    for (k, _) in entries {
        let parts =
            store::decode_key(&k, "sussu").ok_or_else(|| AppError::internal("bad card key"))?;
        if (parts[3].s(), parts[4].u()) != exclude {
            return Ok(Some((k, parts[2].s().to_string())));
        }
    }
    Ok(None)
}

fn card_rank(key: &[u8]) -> AppResult<String> {
    let parts =
        store::decode_key(key, "sussu").ok_or_else(|| AppError::internal("bad card key"))?;
    Ok(parts[2].s().to_string())
}

/// The rank of ticket `own`'s card placed in `lane` of the board of workspace
/// `board`: before the card `before`, after the card `after`, or at the
/// lane's end. Only the neighbours of the new position are read, so the cost
/// does not grow with the lane (the `done` lane keeps every finished ticket).
fn rank_in(
    r: &impl Rd,
    board: &str,
    own: (&str, u64),
    lane: &str,
    before: Option<&str>,
    after: Option<&str>,
) -> AppResult<String> {
    let lane_prefix = Key::new()
        .s(board)
        .u(model::lane_index(lane).unwrap_or(0))
        .bytes();
    Ok(match (before, after) {
        (Some(b), None) => {
            let card = lane_card(r, board, lane, b, own)?;
            let prev = neighbour(r, &lane_prefix, Some(&card), false, own)?;
            rank::between(
                prev.as_ref().map(|p| p.1.as_str()),
                Some(&card_rank(&card)?),
            )
        }
        (None, Some(a)) => {
            let card = lane_card(r, board, lane, a, own)?;
            let next = neighbour(r, &lane_prefix, Some(&card), true, own)?;
            rank::between(
                Some(&card_rank(&card)?),
                next.as_ref().map(|n| n.1.as_str()),
            )
        }
        (Some(b), Some(a)) => {
            let (cb, ca) = (
                lane_card(r, board, lane, b, own)?,
                lane_card(r, board, lane, a, own)?,
            );
            let next = neighbour(r, &lane_prefix, Some(&ca), true, own)?;
            if next.as_ref().map(|n| &n.0) != Some(&cb) {
                return Err(AppError::invalid(
                    "before and after must name adjacent cards.",
                ));
            }
            rank::between(Some(&card_rank(&ca)?), Some(&card_rank(&cb)?))
        }
        (None, None) => {
            let last = neighbour(r, &lane_prefix, None, false, own)?;
            rank::between(last.as_ref().map(|l| l.1.as_str()), None)
        }
    })
}

/// Moves a ticket to a lane of its workspace's board (see `rank_in`).
pub fn place(
    r: &impl Rd,
    ticket: &Ticket,
    lane: &str,
    before: Option<&str>,
    after: Option<&str>,
) -> AppResult<Ticket> {
    let own = (ticket.repo.as_str(), ticket.number);
    Ok(Ticket {
        rank: rank_in(r, &ticket.workspace, own, lane, before, after)?,
        lane: lane.to_string(),
        ..ticket.clone()
    })
}

/// Moves a ticket to a lane of its workspace's board. The owner's move
/// decides the ticket's state too: into `done` closes it as completed, into
/// another lane reopens it. An agent's move leaves the state alone, as closing
/// a ticket is a person's decision.
pub async fn put_on_board(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    p: Placement,
) -> AppResult<TicketView> {
    let repo = repos::visible(app, caller, repo_path).await?;
    check_lane(&p.lane)?;
    let owner = caller.is_owner();
    let key = key.to_string();
    let base_url = app.base_url.clone();
    let view = app
        .store
        .write(move |w| {
            let old = ticket_by_key(w, &repo, &key)?;
            if p.lane == "in_progress" && old.lane != "in_progress" && any_open(w, &old.blocked_by)?
            {
                return Err(AppError::new(
                    StatusCode::CONFLICT,
                    "blocked",
                    format!(
                        "Ticket {key} is blocked by open tickets; close them before starting it."
                    ),
                ));
            }
            let mut t = place(w, &old, &p.lane, p.before.as_deref(), p.after.as_deref())?;
            let now = util::now();
            match p.lane.as_str() {
                "done" if owner && t.state == "open" => {
                    check_children_closed(w, &t)?;
                    set_state(&mut t, "closed", Some("completed".into()), &now);
                }
                lane if owner && lane != "done" && t.state == "closed" => {
                    set_state(&mut t, "open", None, &now);
                }
                _ => {}
            }
            t.revision += 1;
            t.updated_at = now;
            write_ticket(w, Some(&old), &t)?;
            ticket_view(w, &base_url, &repo, &t)
        })
        .await?;
    publish_tickets(app, [&view]);
    app.events
        .publish("board", workspace_of(&view.repository), "");
    Ok(view)
}

// ---------------------------------------------------------------- comments

fn ticket_target(t: &Ticket) -> Target {
    Target::Ticket {
        repo: t.repo.clone(),
        number: t.number,
    }
}

pub fn comments_of(r: &impl Rd, target: &Target) -> AppResult<Vec<Comment>> {
    r.prefix_all(store::COMMENTS, &target.prefix())
}

/// Inserts a comment for a target (ticket or pull request) in a write transaction.
pub fn insert_comment(
    w: &Wtx,
    target: &Target,
    author: &str,
    body: String,
    meta: Option<serde_json::Value>,
) -> AppResult<Comment> {
    let seq = w.next("entry")?;
    let c = Comment {
        id: util::new_id("cmt"),
        seq,
        author: author.to_string(),
        mentions: model::mentions(w, &body)?,
        body,
        meta,
        created_at: util::now(),
        reactions: Vec::new(),
    };
    let key = target.comment_key(seq);
    model::put_indexed(
        w,
        store::COMMENTS,
        &key,
        None,
        &c,
        model::comment_index(&c, &key),
    )?;
    Ok(c)
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewComment {
    pub body: String,
    pub meta: Option<serde_json::Value>,
}

pub fn check_comment(input: &NewComment) -> AppResult<()> {
    check_comment_body(&input.body)?;
    domain::check_meta(&input.meta)
}

fn check_comment_body(body: &str) -> AppResult<()> {
    if body.trim().is_empty() {
        return Err(AppError::invalid("body must not be empty."));
    }
    domain::check_text("body", body, domain::MAX_TEXT)
}

pub async fn list_comments(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    page: PageReq,
) -> AppResult<Page<CommentView>> {
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    let base_url = app.base_url.clone();
    app.store
        .read(move |r| {
            let t = ticket_by_key(r, &repo, &key)?;
            let url = ticket_url(&base_url, &repo, t.number);
            let (items, next_cursor) =
                page.scan(r, store::COMMENTS, &ticket_target(&t).prefix(), |_, v| {
                    Ok(Some(comment_view(&store::decode(v)?, &url)))
                })?;
            Ok(Page { items, next_cursor })
        })
        .await
}

pub async fn add_comment(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    input: NewComment,
) -> AppResult<CommentView> {
    check_comment(&input)?;
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    let author = caller.name().to_string();
    let base_url = app.base_url.clone();
    let (view, path, k) = app
        .store
        .write(move |w| {
            let old = ticket_by_key(w, &repo, &key)?;
            let c = insert_comment(w, &ticket_target(&old), &author, input.body, input.meta)?;
            let mut t = old.clone();
            t.updated_at = c.created_at.clone();
            write_ticket(w, Some(&old), &t)?;
            let tv = ticket_view(w, &base_url, &repo, &t)?;
            Ok((comment_view(&c, &tv.url), tv.repository, tv.key))
        })
        .await?;
    app.events.publish("ticket", &path, k);
    Ok(view)
}

/// Edits the body, or deletes the comment and its entry index. The stored
/// shape, metadata, creation time and order of the remaining comments stay.
pub async fn change_comment(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    id: &str,
    body: Option<String>,
) -> AppResult<Option<CommentView>> {
    if let Some(body) = &body {
        check_comment_body(body)?;
    }
    let repo = repos::visible(app, caller, repo_path).await?;
    let caller = caller.clone();
    let key = key.to_string();
    let id = id.to_string();
    let base_url = app.base_url.clone();
    let (view, path, key) = app
        .store
        .write(move |w| {
            let old = ticket_by_key(w, &repo, &key)?;
            let target = ticket_target(&old);
            let not_found = || AppError::not_found(format!("Comment {id} was not found on {key}."));
            let entry = domain::entry(w, &id, &target.owner())?
                .filter(|e| e.kind == "comment")
                .ok_or_else(not_found)?;
            let ck = domain::entry_key(&entry)?;
            let mut c: Comment = w.get(store::COMMENTS, &ck)?.ok_or_else(not_found)?;
            caller.require_self_or_owner(&c.author)?;
            let old_index = model::comment_index(&c, &ck);
            let view = if let Some(body) = body {
                c.mentions = model::mentions(w, &body)?;
                c.body = body;
                model::put_indexed(
                    w,
                    store::COMMENTS,
                    &ck,
                    Some(old_index),
                    &c,
                    model::comment_index(&c, &ck),
                )?;
                Some(comment_view(&c, &ticket_url(&base_url, &repo, old.number)))
            } else {
                w.del(store::COMMENTS, &ck)?;
                model::reindex(w, old_index, Vec::new())?;
                None
            };
            let mut t = old.clone();
            t.updated_at = util::now();
            write_ticket(w, Some(&old), &t)?;
            Ok((view, repo.path, key))
        })
        .await?;
    app.events.publish("ticket", &path, key);
    Ok(view)
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct CommentPatch {
    pub body: String,
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewReaction {
    pub target_id: String,
    pub reaction: String,
}

#[derive(Serialize, Debug)]
pub struct ReactionAdded {
    pub id: String,
    pub reaction: String,
}

/// Gives (`on`) or takes back the caller's reaction to a comment of the ticket.
pub async fn react(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    input: NewReaction,
    on: bool,
) -> AppResult<Option<ReactionAdded>> {
    domain::check_reaction(&input.reaction)?;
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    let author = caller.name().to_string();
    let (added, path, key) = app
        .store
        .write(move |w| {
            let t = ticket_by_key(w, &repo, &key)?;
            let not_found = || {
                AppError::not_found(format!(
                    "Comment {} was not found on {key}.",
                    input.target_id
                ))
            };
            let entry = domain::entry(w, &input.target_id, &ticket_target(&t).owner())?
                .ok_or_else(not_found)?;
            let ck = domain::entry_key(&entry)?;
            let mut c: Comment = w.get(store::COMMENTS, &ck)?.ok_or_else(not_found)?;
            let r = domain::set_reaction(&mut c.reactions, &author, &input.reaction, on);
            w.put(store::COMMENTS, &ck, &c)?;
            Ok((
                r.map(|r| ReactionAdded {
                    id: r.id,
                    reaction: r.reaction,
                }),
                repo.path.clone(),
                key,
            ))
        })
        .await?;
    app.events.publish("ticket", &path, key);
    Ok(added)
}

// ---------------------------------------------------------------- ticket ↔ pull requests

#[derive(Serialize, Debug, Clone)]
pub struct LinkedPull {
    pub url: String,
    pub label: String,
    pub title: String,
    pub state: String,
    pub merged: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub closes: Option<bool>,
    pub updated_at: String,
}

/// The pull requests of this Branvil that link a ticket, oldest first.
pub fn linked_pulls(
    r: &impl Rd,
    base_url: &str,
    t: &Ticket,
    with_closes: bool,
) -> AppResult<Vec<LinkedPull>> {
    let prefix = Key::new().s(&t.repo).u(t.number).bytes();
    let mut pulls: Vec<(String, LinkedPull)> = Vec::new();
    for key in r.prefix_keys(store::IX_TICKET_PULLS, &prefix)? {
        let parts = store::decode_key(&key, "susu")
            .ok_or_else(|| AppError::internal("bad ticket-pull index"))?;
        let Some(p) = r.get::<Pull>(store::PULLS, &Pull::key(parts[2].s(), parts[3].u()))? else {
            continue;
        };
        let repo = repo_by_id(r, &p.repo)?;
        let closes = p
            .tickets
            .iter()
            .any(|l| l.closes && l.local.as_ref() == Some(&(t.repo.clone(), t.number)));
        pulls.push((
            p.created_at.clone(),
            LinkedPull {
                url: format!("{base_url}/{}/-/pulls/{}", repo.path, p.number),
                label: format!("!{}", p.number),
                title: p.title.clone(),
                state: p.state.clone(),
                merged: p.merged,
                closes: with_closes.then_some(closes),
                updated_at: p.updated_at.clone(),
            },
        ));
    }
    pulls.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(pulls.into_iter().map(|(_, p)| p).collect())
}

pub async fn ticket_pulls(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    page: PageReq,
) -> AppResult<Page<LinkedPull>> {
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    let base_url = app.base_url.clone();
    app.store
        .read(move |r| {
            let t = ticket_by_key(r, &repo, &key)?;
            let all = linked_pulls(r, &base_url, &t, true)?;
            let positioned = all
                .into_iter()
                .enumerate()
                .map(|(i, p)| (Key::new().u(i as u64).bytes(), p))
                .collect();
            let (items, next_cursor) = page.slice(positioned);
            Ok(Page { items, next_cursor })
        })
        .await
}

// ---------------------------------------------------------------- the board of a workspace

#[derive(Serialize, Debug)]
pub struct CardView {
    #[serde(flatten)]
    pub ticket: TicketView,
    pub comments: Vec<CommentView>,
    pub pull_requests: Vec<LinkedPull>,
}

#[derive(Deserialize, Debug, Default)]
pub struct CardQuery {
    pub lane: Option<String>,
    pub assignee: Option<String>,
    pub state: Option<String>,
    pub comments: Option<String>,
}

#[derive(Deserialize, Debug)]
pub struct MentionQuery {
    pub mentions: Option<String>,
    pub state: Option<String>,
    pub comments: Option<String>,
}

/// Mentioned tickets across the workspace, independent of lane or assignee.
pub async fn mentioned(
    app: &App,
    caller: &Caller,
    workspace: &str,
    q: MentionQuery,
    page: PageReq,
) -> AppResult<Page<CardView>> {
    if q.mentions.as_deref() != Some("me") {
        return Err(AppError::invalid("mentions must be me."));
    }
    let state = q.state.unwrap_or_else(|| "open".into());
    if !["open", "closed", "all"].contains(&state.as_str()) {
        return Err(AppError::invalid("state must be open, closed, or all."));
    }
    let with_comments = match q.comments.as_deref() {
        None | Some("all") => true,
        Some("none") => false,
        Some(_) => return Err(AppError::invalid("comments must be all or none.")),
    };
    let workspace = repos::visible_workspace(app, caller, workspace).await?;
    let me = fold(caller.name());
    let base_url = app.base_url.clone();
    app.store
        .read(move |r| {
            let prefixes = r
                .prefix_all::<Repo>(store::REPOS, &[])?
                .into_iter()
                .filter(|repo| repo.workspace == workspace.id)
                .map(|repo| Key::new().s(&me).s(&repo.id).bytes())
                .collect::<Vec<_>>();
            // Skip the same ticket's other sources, including on the next page.
            let ticket_id = |key: &[u8]| -> AppResult<(String, u64)> {
                let parts = store::decode_key(key, "ssuu")
                    .ok_or_else(|| AppError::internal("bad ticket mention index"))?;
                Ok((parts[1].s().to_string(), parts[2].u()))
            };
            let mut previous = page.after.as_deref().map(ticket_id).transpose()?;
            let (items, next_cursor) =
                page.scan_prefixes(r, store::IX_TICKET_MENTIONS, &prefixes, |key, _| {
                    let id = ticket_id(key)?;
                    if previous.as_ref() == Some(&id) {
                        return Ok(None);
                    }
                    previous = Some(id.clone());
                    let t = ticket_by_id(r, &id)?;
                    if state != "all" && t.state != state {
                        return Ok(None);
                    }
                    let repo = repo_by_id(r, &t.repo)?;
                    card_view(r, &base_url, &repo, &t, with_comments).map(Some)
                })?;
            Ok(Page { items, next_cursor })
        })
        .await
}

fn card_view(
    r: &impl Rd,
    base_url: &str,
    repo: &Repo,
    t: &Ticket,
    with_comments: bool,
) -> AppResult<CardView> {
    let ticket = ticket_view(r, base_url, repo, t)?;
    let comments = if with_comments {
        comments_of(r, &ticket_target(t))?
            .iter()
            .map(|c| comment_view(c, &ticket.url))
            .collect()
    } else {
        Vec::new()
    };
    let pull_requests = linked_pulls(r, base_url, t, false)?;
    Ok(CardView {
        ticket,
        comments,
        pull_requests,
    })
}

pub async fn cards(
    app: &App,
    caller: &Caller,
    workspace: &str,
    q: CardQuery,
    page: PageReq,
) -> AppResult<Page<CardView>> {
    let mut lanes: Vec<u64> = match &q.lane {
        Some(text) => text
            .split(',')
            .map(|l| {
                model::lane_index(l.trim())
                    .ok_or_else(|| AppError::invalid(format!("Unknown lane {l}.")))
            })
            .collect::<AppResult<_>>()?,
        None => (0..LANES.len() as u64).collect(),
    };
    lanes.sort_unstable();
    lanes.dedup();
    if let Some(s) = &q.state
        && !["open", "closed", "all"].contains(&s.as_str())
    {
        return Err(AppError::invalid("state must be open, closed, or all."));
    }
    let with_comments = match q.comments.as_deref() {
        None | Some("all") => true,
        Some("none") => false,
        Some(_) => return Err(AppError::invalid("comments must be all or none.")),
    };
    let board = repos::visible_workspace(app, caller, workspace).await?;
    let caller = caller.clone();
    let base_url = app.base_url.clone();
    app.store
        .read(move |r| {
            let filter = TicketQuery {
                q: None,
                state: Some(q.state.clone().unwrap_or_else(|| "open".into())),
                lane: None,
                assignee: q.assignee.clone(),
                label: None,
            };
            // Only the asked lanes are read: `done` keeps every finished
            // ticket, and candidates ask for `ready` and `in_progress`.
            let prefixes: Vec<Vec<u8>> = lanes
                .iter()
                .map(|l| Key::new().s(&board.id).u(*l).bytes())
                .collect();
            let (items, next_cursor) =
                page.scan_prefixes(r, store::IX_CARDS, &prefixes, |k, _| {
                    let parts = store::decode_key(k, "sussu")
                        .ok_or_else(|| AppError::internal("bad card key"))?;
                    let repo = repo_by_id(r, parts[3].s())?;
                    let t: Ticket = r
                        .get(store::TICKETS, &Ticket::key(&repo.id, parts[4].u()))?
                        .ok_or_else(|| AppError::internal("card index names a missing ticket"))?;
                    if !matches(&t, &filter, caller.name()) {
                        return Ok(None);
                    }
                    card_view(r, &base_url, &repo, &t, with_comments).map(Some)
                })?;
            Ok(Page { items, next_cursor })
        })
        .await
}

#[derive(Serialize, Debug)]
pub struct ClosedItem {
    pub kind: &'static str,
    pub url: String,
    pub title: String,
    pub label: String,
    pub closed_at: String,
    pub merged: bool,
}

#[derive(Deserialize, Debug, Default)]
pub struct ClosedQuery {
    pub since: Option<String>,
    pub until: Option<String>,
}

pub async fn closed(
    app: &App,
    caller: &Caller,
    workspace: &str,
    q: ClosedQuery,
    page: PageReq,
) -> AppResult<Page<ClosedItem>> {
    let since = q
        .since
        .as_deref()
        .map(util::parse_time)
        .transpose()?
        .unwrap_or_default();
    let until = q
        .until
        .as_deref()
        .map(util::parse_time)
        .transpose()?
        .unwrap_or_else(|| "9999".into());
    let board = repos::visible_workspace(app, caller, workspace).await?;
    let base_url = app.base_url.clone();
    app.store
        .read(move |r| {
            let mut items: Vec<(Vec<u8>, ClosedItem)> = Vec::new();
            let start = Key::new().s(&board.id).s(&since).bytes();
            let end = Key::new().s(&board.id).s(&until).bytes();
            let end = store::prefix_end(&end).unwrap_or(end);
            for (k, _) in r.range_raw(
                store::IX_CLOSED_TICKETS,
                std::ops::Bound::Included(start),
                std::ops::Bound::Excluded(end),
                usize::MAX,
            )? {
                let parts = store::decode_key(&k, "sssu")
                    .ok_or_else(|| AppError::internal("bad closed index"))?;
                let repo = repo_by_id(r, parts[2].s())?;
                let Some(t) =
                    r.get::<Ticket>(store::TICKETS, &Ticket::key(&repo.id, parts[3].u()))?
                else {
                    continue;
                };
                items.push((
                    Key::new()
                        .s(parts[1].s())
                        .s("t")
                        .s(&repo.id)
                        .u(t.number)
                        .bytes(),
                    ClosedItem {
                        kind: "ticket",
                        url: ticket_url(&base_url, &repo, t.number),
                        title: t.title,
                        label: format!("#{}", t.number),
                        closed_at: parts[1].s().to_string(),
                        merged: false,
                    },
                ));
            }
            // Pull requests of the workspace's repositories.
            let repos = r.prefix_all::<Repo>(store::REPOS, &[])?;
            for repo in repos.into_iter().filter(|x| x.workspace == board.id) {
                let repo_id = repo.id.clone();
                let start = Key::new().s(&repo_id).s(&since).bytes();
                let end = Key::new().s(&repo_id).s(&until).bytes();
                let end = store::prefix_end(&end).unwrap_or(end);
                for (k, _) in r.range_raw(
                    store::IX_CLOSED_PULLS,
                    std::ops::Bound::Included(start),
                    std::ops::Bound::Excluded(end),
                    usize::MAX,
                )? {
                    let parts = store::decode_key(&k, "ssu")
                        .ok_or_else(|| AppError::internal("bad closed index"))?;
                    let Some(p) =
                        r.get::<Pull>(store::PULLS, &Pull::key(&repo_id, parts[2].u()))?
                    else {
                        continue;
                    };
                    items.push((
                        Key::new()
                            .s(parts[1].s())
                            .s("p")
                            .s(&repo_id)
                            .u(p.number)
                            .bytes(),
                        ClosedItem {
                            kind: "pull_request",
                            url: format!("{base_url}/{}/-/pulls/{}", repo.path, p.number),
                            title: p.title,
                            label: format!("!{}", p.number),
                            closed_at: parts[1].s().to_string(),
                            merged: p.merged,
                        },
                    ));
                }
            }
            items.sort_by(|a, b| a.0.cmp(&b.0));
            let (items, next_cursor) = page.slice(items);
            Ok(Page { items, next_cursor })
        })
        .await
}

/// Closes a ticket a merged pull request `closes` and moves it to `done`
/// (inside the merge's write transaction). A ticket with an open child is
/// left as it is (`false`): it completes once its children are closed.
pub fn close_by_merge(w: &Wtx, ticket: &TicketId, at: &str) -> AppResult<bool> {
    let Some(old) = w.get::<Ticket>(store::TICKETS, &Ticket::key(&ticket.0, ticket.1))? else {
        return Ok(true);
    };
    if has_open_children(w, ticket)? {
        return Ok(false);
    }
    let mut t = if old.lane == "done" {
        old.clone()
    } else {
        place(w, &old, "done", None, None)?
    };
    set_state(&mut t, "closed", Some("completed".into()), at);
    t.revision += 1;
    t.updated_at = at.to_string();
    write_ticket(w, Some(&old), &t)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    const BOARD: &str = "ws_1";

    struct Fixture {
        _dir: tempfile::TempDir,
        store: Store,
    }

    fn fixture() -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(&dir.path().join("t.redb"), 1 << 20, 2).unwrap();
        let repo = Repo {
            id: "repo_1".into(),
            path: "acme/t".into(),
            workspace: BOARD.into(),
            default_branch: "main".into(),
            protected_branches: Vec::new(),
            required_checks: Vec::new(),
            required_approvals: 1,
            auto_land: false,
            ci_network: false,
            labels: Vec::new(),
            upstream: None,
            revision: 1,
            created_at: util::now(),
        };
        store
            .write_sync(|w| {
                model::put_indexed(
                    w,
                    store::REPOS,
                    &Repo::key(&repo.id),
                    None,
                    &repo,
                    model::repo_index(&repo),
                )
            })
            .unwrap();
        Fixture { _dir: dir, store }
    }

    fn ticket(n: u64) -> Ticket {
        Ticket {
            repo: "repo_1".into(),
            workspace: BOARD.into(),
            number: n,
            title: format!("t{n}"),
            body: String::new(),
            mentions: Vec::new(),
            state: "open".into(),
            state_reason: None,
            author: "a".into(),
            assignee: None,
            labels: Vec::new(),
            interactive: false,
            upstream_issue: None,
            lane: "new".into(),
            rank: rank::between(None, None),
            parent: None,
            blocked_by: Vec::new(),
            created_at: util::now(),
            updated_at: util::now(),
            closed_at: None,
            revision: 1,
        }
    }

    impl Fixture {
        /// Places ticket `n` (created when missing) and writes it.
        fn put(
            &self,
            n: u64,
            lane: &str,
            before: Option<&str>,
            after: Option<&str>,
        ) -> AppResult<()> {
            let (lane, before, after) = (
                lane.to_string(),
                before.map(str::to_string),
                after.map(str::to_string),
            );
            self.store.write_sync(|w| {
                let old: Option<Ticket> = w.get(store::TICKETS, &Ticket::key("repo_1", n))?;
                let base = old.clone().unwrap_or_else(|| ticket(n));
                let t = place(w, &base, &lane, before.as_deref(), after.as_deref())?;
                write_ticket(w, old.as_ref(), &t)
            })
        }

        fn lane(&self, lane: &str) -> Vec<u64> {
            let prefix = Key::new()
                .s(BOARD)
                .u(model::lane_index(lane).unwrap())
                .bytes();
            self.store
                .read_sync(|r| r.prefix_keys(store::IX_CARDS, &prefix))
                .unwrap()
                .iter()
                .map(|k| store::decode_key(k, "sussu").unwrap()[4].u())
                .collect()
        }
    }

    #[test]
    fn placement_reads_neighbours_only_and_keeps_the_order() {
        let f = fixture();
        for n in 1..=5 {
            f.put(n, "ready", None, None).unwrap();
        }
        assert_eq!(f.lane("ready"), [1, 2, 3, 4, 5]);
        // Without neighbours a card goes last, also when it is already last.
        f.put(5, "ready", None, None).unwrap();
        assert_eq!(f.lane("ready"), [1, 2, 3, 4, 5]);
        f.put(1, "ready", None, None).unwrap();
        assert_eq!(f.lane("ready"), [2, 3, 4, 5, 1]);
        // Before / after a card, skipping the moved card itself.
        f.put(1, "ready", Some("acme/t#2"), None).unwrap();
        assert_eq!(f.lane("ready"), [1, 2, 3, 4, 5]);
        f.put(1, "ready", None, Some("acme/t#2")).unwrap();
        assert_eq!(f.lane("ready"), [2, 1, 3, 4, 5]);
        f.put(3, "ready", Some("acme/t#1"), None).unwrap();
        assert_eq!(f.lane("ready"), [2, 3, 1, 4, 5]);
        f.put(5, "ready", Some("acme/t#4"), Some("acme/t#1"))
            .unwrap();
        assert_eq!(f.lane("ready"), [2, 3, 1, 5, 4]);
        // Between a card and its neighbour once the moved card is skipped.
        f.put(1, "ready", Some("acme/t#5"), Some("acme/t#3"))
            .unwrap();
        assert_eq!(f.lane("ready"), [2, 3, 1, 5, 4]);
        // Into another lane, next to a card there.
        f.put(6, "done", None, None).unwrap();
        f.put(4, "done", Some("acme/t#6"), None).unwrap();
        assert_eq!(f.lane("done"), [4, 6]);
        assert_eq!(f.lane("ready"), [2, 3, 1, 5]);
    }

    #[test]
    fn placement_refuses_cards_that_are_not_neighbours_of_that_lane() {
        let f = fixture();
        for n in 1..=3 {
            f.put(n, "ready", None, None).unwrap();
        }
        f.put(4, "new", None, None).unwrap();
        let code = |r: AppResult<()>| r.unwrap_err().code;
        assert_eq!(
            code(f.put(5, "ready", Some("acme/t#1"), Some("acme/t#3"))),
            "invalid"
        );
        // The moved card itself, a card of another lane, unknown keys.
        assert_eq!(code(f.put(2, "ready", Some("acme/t#2"), None)), "not_found");
        assert_eq!(code(f.put(5, "ready", Some("acme/t#4"), None)), "not_found");
        assert_eq!(code(f.put(5, "ready", None, Some("acme/t#9"))), "not_found");
        assert_eq!(code(f.put(5, "ready", None, Some("acme/x#1"))), "not_found");
        assert_eq!(
            code(f.put(5, "ready", None, Some("acme/t#01"))),
            "not_found"
        );
        assert_eq!(f.lane("ready"), [1, 2, 3]);
    }
}
