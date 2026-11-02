//! Pull requests: creation, patrol, conversation, reviews, inline threads,
//! diffs, checks, and the fast-forward merge.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::app::App;
use crate::auth::Caller;
use crate::ci::Check;
use crate::domain::tickets::{self, NewComment, NewReaction, ReactionAdded};
use crate::domain::{
    self, CommentView, Page, PageReq, ReactionView, comment_view, reaction_views, repo_by_id,
};
use crate::error::{AppError, AppResult};
use crate::git::{CommitSummary, FileChange, ZERO_SHA};
use crate::model::{
    self, InlineComment, JournalEntry, JournalOp, Published, Pull, Repo, Review, Target, Thread,
    TicketLink,
};
use crate::refs;
use crate::store::{self, Key, Rd, Wtx};
use crate::util::{self, fold};

/// Patches are cut at this size (`patch_complete: false`).
const PATCH_LIMIT: usize = 256 << 10;

#[derive(Serialize, Debug, Clone)]
pub struct LinkView {
    pub url: String,
    pub closes: bool,
}

#[derive(Serialize, Debug, Clone)]
pub struct PullView {
    pub url: String,
    pub repository: String,
    pub number: u64,
    pub label: String,
    pub title: String,
    pub body: String,
    pub state: String,
    pub merged: bool,
    pub draft: bool,
    pub author: String,
    pub head: String,
    pub head_repository: String,
    pub head_sha: String,
    pub base: String,
    pub base_sha: String,
    pub reviewers: Vec<String>,
    pub review_states: Vec<ReviewState>,
    /// The members whose review state approves (`ReviewState::approves`).
    pub approvals: u64,
    /// The statements that wait on the author's answer (`unanswered`).
    pub unanswered: Vec<Unanswered>,
    pub tickets: Vec<LinkView>,
    pub labels: Vec<String>,
    pub changed_files: u64,
    pub ready_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    pub closed_at: Option<String>,
    pub merged_at: Option<String>,
    pub merge_sha: Option<String>,
    /// Set once published to the repository's upstream host.
    pub upstream: Option<PublishedView>,
    pub revision: u64,
    pub landing: Option<crate::landing::LandingView>,
}

/// A published pull request as the API shows it (`Published` without what
/// only the poller needs).
#[derive(Serialize, Debug, Clone)]
pub struct PublishedView {
    /// Where it was published (it stays there if the upstream changes).
    pub provider: String,
    pub repository: String,
    pub account: String,
    pub number: Option<u64>,
    pub url: Option<String>,
    pub state: String,
    pub pushed: Option<String>,
    pub published_at: String,
    pub error: Option<String>,
    pub stopped: Option<String>,
    /// Published by Branvil itself, the repository landing automatically.
    pub auto: bool,
}

fn published_view(x: &Published) -> PublishedView {
    PublishedView {
        provider: x.provider.clone(),
        repository: x.repository.clone(),
        account: x.account.clone(),
        number: x.number,
        url: x.url.clone(),
        state: x.state.clone(),
        pushed: x.pushed.clone(),
        published_at: x.published_at.clone(),
        error: x.error.clone(),
        stopped: x.stopped.clone(),
        auto: x.auto,
    }
}

/// Where a member stands in a pull request's review: what its reviewer badge
/// shows, and whether it counts as an approval.
#[derive(Serialize, Debug, Clone)]
pub struct ReviewState {
    pub member: String,
    /// Asked to review and not answered since.
    pub requested: bool,
    /// The verdict of the member's last approve or request_changes, or
    /// `comment` while they only commented; `None` before any review.
    pub verdict: Option<String>,
    /// That review is on a commit other than the current head.
    pub outdated: bool,
}

impl ReviewState {
    /// An approval of the current head, not asked again since.
    fn approves(&self) -> bool {
        !self.requested && !self.outdated && self.verdict.as_deref() == Some("approve")
    }
}

/// Every member's review state, the one place it is decided: the members
/// who reviewed, in the order of their first review, then those asked who
/// have not reviewed. `reviews` are in the order they were submitted
/// (a thread reply is not a review). A comment says neither yes nor no, so it
/// keeps an earlier verdict. The author is not a reviewer: their own reviews
/// (only comments can be submitted) count for nothing.
fn review_states(p: &Pull, reviews: &[Review], head: &str) -> Vec<ReviewState> {
    fn entry<'a>(states: &'a mut Vec<ReviewState>, name: &str) -> &'a mut ReviewState {
        let i = states
            .iter()
            .position(|s| fold(&s.member) == fold(name))
            .unwrap_or_else(|| {
                states.push(ReviewState {
                    member: name.to_string(),
                    requested: false,
                    verdict: None,
                    outdated: false,
                });
                states.len() - 1
            });
        &mut states[i]
    }
    let mut states = Vec::new();
    for x in reviews
        .iter()
        .filter(|x| fold(&x.author) != fold(&p.author))
    {
        let s = entry(&mut states, &x.author);
        if x.verdict == "comment" && s.verdict.as_deref().is_some_and(|v| v != "comment") {
            continue;
        }
        s.verdict = Some(x.verdict.clone());
        s.outdated = x.commit != head;
    }
    for name in &p.reviewers {
        entry(&mut states, name).requested = true;
    }
    states
}

/// The review states of `p` with `head` as its current head.
pub(crate) async fn read_review_states(
    app: &App,
    p: &Pull,
    head: &str,
) -> AppResult<Vec<ReviewState>> {
    let (repo, number) = (p.repo.clone(), p.number);
    let reviews = app
        .store
        .read(move |r| reviews_of(r, &repo, number))
        .await?;
    Ok(review_states(p, &reviews, head))
}

pub(crate) fn approvals(states: &[ReviewState]) -> u64 {
    states.iter().filter(|s| s.approves()).count() as u64
}

pub(crate) type Tips = BTreeMap<String, String>;

pub async fn tips(app: &App, repo: &Repo) -> AppResult<Tips> {
    app.git.branch_tips(&app.git.repo_dir(&repo.path)).await
}

/// The commit a pull request's head is at: its branch's tip until it is
/// merged, then what was merged (its branch may be gone). Merged on the
/// upstream host, that is what Branvil pushed there, whichever way the host
/// merged it (a squash or a rebase makes commits of its own).
fn head_of(p: &Pull, tips: &Tips) -> Option<String> {
    if !p.merged {
        return tips.get(&p.head).cloned();
    }
    match p.upstream.as_ref().filter(|x| x.state == "merged") {
        Some(x) => x.pushed.clone(),
        None => p.merged_head.clone(),
    }
}

/// Where a pull request's changes start: its base branch's tip until it is
/// merged, then the base the merge was made on.
fn base_of(p: &Pull, tips: &Tips) -> Option<String> {
    if p.merged {
        p.merged_base.clone()
    } else {
        tips.get(&p.base).cloned()
    }
}

/// The diff range a pull request shows: from the merge base to the head, or
/// what the merge brought in once merged.
fn diff_range(p: &Pull, tips: &Tips) -> Option<String> {
    let (base, head) = (base_of(p, tips)?, head_of(p, tips)?);
    Some(if p.merged {
        format!("{base}..{head}")
    } else {
        format!("{base}...{head}")
    })
}

async fn changed_files(app: &App, repo: &Repo, p: &Pull, tips: &Tips) -> AppResult<u64> {
    let Some(range) = diff_range(p, tips) else {
        return Ok(0);
    };
    let cache_key = (repo.id.clone(), range.clone(), String::new());
    if let Some(n) = app.changed_files.lock().unwrap().get(&cache_key) {
        return Ok(*n);
    }
    let n = app
        .git
        .changed_paths(&app.git.repo_dir(&repo.path), &range)
        .await?
        .len() as u64;
    let mut cache = app.changed_files.lock().unwrap();
    if cache.len() > 4096 {
        cache.clear();
    }
    cache.insert(cache_key, n);
    Ok(n)
}

pub async fn pull_view(app: &App, repo: &Repo, p: &Pull, tips: &Tips) -> AppResult<PullView> {
    let changed = changed_files(app, repo, p, tips).await?;
    let head_sha = head_of(p, tips).unwrap_or_default();
    let review_states = read_review_states(app, p, &head_sha).await?;
    let url = app.pull_url(&repo.path, p.number);
    let (p2, url2) = (p.clone(), url.clone());
    // Only an open pull request can still wait on its author.
    let unanswered = match p.state.as_str() {
        "open" => app.store.read(move |r| unanswered(r, &p2, &url2)).await?,
        _ => Vec::new(),
    };
    Ok(PullView {
        url,
        unanswered,
        repository: repo.path.clone(),
        number: p.number,
        label: format!("!{}", p.number),
        title: p.title.clone(),
        body: p.body.clone(),
        state: p.state.clone(),
        merged: p.merged,
        draft: p.draft,
        author: p.author.clone(),
        head: p.head.clone(),
        head_repository: repo.path.clone(),
        head_sha,
        base: p.base.clone(),
        base_sha: tips.get(&p.base).cloned().unwrap_or_default(),
        reviewers: p.reviewers.clone(),
        approvals: approvals(&review_states),
        review_states,
        tickets: p
            .tickets
            .iter()
            .map(|l| LinkView {
                url: l.url.clone(),
                closes: l.closes,
            })
            .collect(),
        labels: p.labels.clone(),
        changed_files: changed,
        ready_at: p.ready_at.clone(),
        created_at: p.created_at.clone(),
        updated_at: p.updated_at.clone(),
        closed_at: p.closed_at.clone(),
        merged_at: p.merged_at.clone(),
        merge_sha: p.merge_sha.clone(),
        upstream: p.upstream.as_ref().map(published_view),
        revision: p.revision,
        landing: crate::landing::view(app, repo, p.number, tips).await?,
    })
}

pub fn load_pull(r: &impl Rd, repo: &Repo, number: u64) -> AppResult<Pull> {
    r.get(store::PULLS, &Pull::key(&repo.id, number))?
        .ok_or_else(|| {
            AppError::not_found(format!(
                "Pull request !{number} was not found in {}.",
                repo.path
            ))
        })
}

/// Open pull requests from the repository's maintained index.
pub fn open_pulls(r: &impl Rd, repo: &Repo) -> AppResult<Vec<Pull>> {
    let prefix = Key::new().s(&repo.id).bytes();
    let mut out = Vec::new();
    for (_, n) in r.prefix_raw(store::IX_OPEN_PULLS, &prefix, None, usize::MAX)? {
        let n = String::from_utf8_lossy(&n)
            .parse()
            .map_err(AppError::internal)?;
        out.push(load_pull(r, repo, n)?);
    }
    Ok(out)
}

fn write_pull(w: &Wtx, old: Option<&Pull>, new: &Pull) -> AppResult<()> {
    model::put_indexed(
        w,
        store::PULLS,
        &Pull::key(&new.repo, new.number),
        old.map(model::pull_index),
        new,
        model::pull_index(new),
    )
}

fn target(p: &Pull) -> Target {
    Target::Pull {
        repo: p.repo.clone(),
        number: p.number,
    }
}

/// A repository the caller can read and one of its pull requests.
async fn visible(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
) -> AppResult<(Repo, Pull)> {
    let repo = domain::repos::visible(app, caller, repo_path).await?;
    let r2 = repo.clone();
    let p = app.store.read(move |r| load_pull(r, &r2, number)).await?;
    Ok((repo, p))
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct LinkInput {
    pub url: String,
    #[serde(default)]
    pub closes: bool,
}

/// Validates ticket links for a pull request of `repo` (in a transaction).
fn resolve_links(
    r: &impl Rd,
    base_url: &str,
    repo: &Repo,
    links: &[LinkInput],
) -> AppResult<Vec<TicketLink>> {
    let mut out: Vec<TicketLink> = Vec::new();
    for l in links {
        if l.url.len() > 2048 {
            return Err(AppError::invalid("A ticket URL is longer than 2048 bytes."));
        }
        let link = match tickets::resolve_ticket_url(r, base_url, &l.url) {
            None => {
                if l.closes {
                    return Err(AppError::unsupported(format!(
                        "{} is not a ticket of this Branvil; a merge cannot close it.",
                        l.url
                    )));
                }
                TicketLink {
                    url: l.url.clone(),
                    closes: false,
                    local: None,
                }
            }
            Some(found) => {
                let (ticket_repo, ticket) = found?;
                if l.closes && ticket_repo.id != repo.id {
                    return Err(AppError::unsupported(format!(
                        "{} is a ticket of {}, not of {}; a merge cannot close it.",
                        l.url, ticket_repo.path, repo.path
                    )));
                }
                TicketLink {
                    url: tickets::ticket_url(base_url, &ticket_repo, ticket.number),
                    closes: l.closes,
                    local: Some((ticket_repo.id, ticket.number)),
                }
            }
        };
        if !out.iter().any(|o| o.url == link.url) {
            out.push(link);
        }
    }
    Ok(out)
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewPull {
    pub head: String,
    #[serde(default)]
    pub base: String,
    pub title: String,
    #[serde(default)]
    pub body: String,
    #[serde(default)]
    pub draft: bool,
    #[serde(default)]
    pub tickets: Vec<LinkInput>,
}

#[derive(Serialize, Debug)]
pub struct PullCreated {
    pub created: bool,
    pub pull: PullView,
}

pub async fn create(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    input: NewPull,
) -> AppResult<PullCreated> {
    let repo = domain::repos::visible(app, caller, repo_path).await?;
    domain::check_title(&input.title)?;
    domain::check_text("body", &input.body, domain::MAX_TEXT)?;
    let base = if input.base.is_empty() {
        repo.default_branch.clone()
    } else {
        input.base.clone()
    };
    for b in [&input.head, &base] {
        if !crate::git::valid_branch(b) {
            return Err(AppError::invalid(format!(
                "{b} is not a valid branch name."
            )));
        }
    }
    if input.head == base {
        return Err(AppError::invalid(
            "head and base must be different branches.",
        ));
    }
    let tips = tips(app, &repo).await?;
    for b in [&input.head, &base] {
        if !tips.contains_key(b.as_str()) {
            return Err(AppError::not_found(format!(
                "Branch {b} was not found in {}.",
                repo.path
            )));
        }
    }
    let author = caller.name().to_string();
    let base_url = app.base_url.clone();
    let r2 = repo.clone();
    let (created, p) = app
        .store
        .write(move |w| {
            // The open (head, base) lookup and the insert share one write
            // transaction, and redb has one writer: concurrent creates of the
            // same head and base produce one pull request.
            let open = Pull::open_key(&r2.id, &input.head, &base);
            if let Some(n) = w.get_raw(store::IX_OPEN_PULLS, &open)? {
                let n: u64 = String::from_utf8_lossy(&n)
                    .parse()
                    .map_err(AppError::internal)?;
                return Ok((false, load_pull(w, &r2, n)?));
            }
            let links = resolve_links(w, &base_url, &r2, &input.tickets)?;
            let number = w.next(&format!("pull:{}", r2.id))?;
            let now = util::now();
            // Those the author asks to review every pull request it opens.
            let reviewers = domain::member_by_name(w, &author)?.reviewers;
            let p = Pull {
                repo: r2.id.clone(),
                number,
                title: input.title,
                body: input.body,
                state: "open".into(),
                merged: false,
                draft: input.draft,
                author,
                head: input.head,
                base,
                reviewers,
                tickets: links,
                labels: Vec::new(),
                reviewed_by: Vec::new(),
                ready_at: (!input.draft).then(|| now.clone()),
                created_at: now.clone(),
                updated_at: now,
                closed_at: None,
                merged_at: None,
                merge_sha: None,
                merged_head: None,
                merged_base: None,
                upstream: None,
                revision: 1,
            };
            write_pull(w, None, &p)?;
            Ok((true, p))
        })
        .await?;
    if created {
        app.events.publish("pull", &repo.path, p.number.to_string());
        app.ci.on_pull_opened(app, &repo, &p.head).await;
    }
    Ok(PullCreated {
        created,
        pull: pull_view(app, &repo, &p, &tips).await?,
    })
}

#[derive(Deserialize, Debug, Default)]
pub struct PullQuery {
    pub state: Option<String>,
    pub head: Option<String>,
    pub base: Option<String>,
}

pub async fn list(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    q: PullQuery,
    page: PageReq,
) -> AppResult<Page<PullView>> {
    let state = q.state.clone().unwrap_or_else(|| "open".into());
    if !["open", "closed", "all"].contains(&state.as_str()) {
        return Err(AppError::invalid("state must be open, closed, or all."));
    }
    let repo = domain::repos::visible(app, caller, repo_path).await?;
    let r2 = repo.clone();
    let (pulls, next_cursor) = app
        .store
        .read(move |r| {
            let all: Vec<Pull> = r.prefix_all(store::PULLS, &Key::new().s(&r2.id).bytes())?;
            let mut positioned: Vec<_> = all
                .into_iter()
                .filter(|p| {
                    (state == "all" || p.state == state)
                        && q.head.as_ref().is_none_or(|h| &p.head == h)
                        && q.base.as_ref().is_none_or(|b| &p.base == b)
                })
                .map(|p| {
                    // Complement the key so PageReq's ascending positions
                    // mean newest update first, then largest number first.
                    let position: Vec<u8> = Key::new()
                        .s(&p.updated_at)
                        .u(p.number)
                        .bytes()
                        .into_iter()
                        .map(|b| !b)
                        .collect();
                    (position, p)
                })
                .collect();
            positioned.sort_by(|a, b| a.0.cmp(&b.0));
            Ok(page.slice(positioned))
        })
        .await?;
    let tips = tips(app, &repo).await?;
    let mut items = Vec::new();
    for p in &pulls {
        items.push(pull_view(app, &repo, p, &tips).await?);
    }
    Ok(Page { items, next_cursor })
}

pub async fn get(app: &App, caller: &Caller, repo_path: &str, number: u64) -> AppResult<PullView> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let tips = tips(app, &repo).await?;
    pull_view(app, &repo, &p, &tips).await
}

// ---------------------------------------------------------------- activity

#[derive(Serialize, Debug, Clone)]
pub struct ReviewView {
    pub id: String,
    pub author: String,
    pub verdict: String,
    pub body: String,
    pub commit: String,
    pub submitted_at: String,
    pub reply_only: bool,
    pub reactions: Vec<ReactionView>,
    pub url: String,
}

#[derive(Serialize, Debug, Clone)]
pub struct ThreadCommentView {
    #[serde(flatten)]
    pub comment: CommentView,
    pub reply_to_id: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
pub struct ThreadView {
    pub id: String,
    pub resolved: bool,
    pub outdated: bool,
    pub path: String,
    pub line: u64,
    pub side: String,
    pub start_line: Option<u64>,
    pub start_side: Option<String>,
    pub commit: String,
    pub comments: Vec<ThreadCommentView>,
}

#[derive(Serialize, Debug, Clone)]
pub struct Activity {
    pub comments: Vec<CommentView>,
    pub reviews: Vec<ReviewView>,
    pub threads: Vec<ThreadView>,
}

fn review_view(x: &Review, pull_url: &str) -> ReviewView {
    ReviewView {
        id: x.id.clone(),
        author: x.author.clone(),
        verdict: x.verdict.clone(),
        body: x.body.clone(),
        commit: x.commit.clone(),
        submitted_at: x.submitted_at.clone(),
        reply_only: false,
        reactions: reaction_views(&x.reactions),
        url: format!("{pull_url}#{}", x.id),
    }
}

fn inline_view(c: &InlineComment, pull_url: &str) -> ThreadCommentView {
    ThreadCommentView {
        comment: CommentView {
            mentions: None,
            id: c.id.clone(),
            author: c.author.clone(),
            body: c.body.clone(),
            meta: None,
            created_at: c.created_at.clone(),
            reactions: reaction_views(&c.reactions),
            url: format!("{pull_url}#{}", c.id),
        },
        reply_to_id: c.reply_to_id.clone(),
    }
}

fn reviews_of(r: &impl Rd, repo: &str, number: u64) -> AppResult<Vec<Review>> {
    r.prefix_all(store::REVIEWS, &Key::new().s(repo).u(number).bytes())
}

fn threads_of(r: &impl Rd, p: &Pull, head_sha: &str, pull_url: &str) -> AppResult<Vec<ThreadView>> {
    let threads: Vec<Thread> =
        r.prefix_all(store::THREADS, &Key::new().s(&p.repo).u(p.number).bytes())?;
    let mut out = Vec::new();
    for t in threads {
        let comments: Vec<InlineComment> = r.prefix_all(
            store::INLINE,
            &Key::new().s(&p.repo).u(p.number).u(t.root_seq).bytes(),
        )?;
        out.push(ThreadView {
            id: t.id.clone(),
            resolved: t.resolved,
            outdated: t.commit != head_sha,
            path: t.path.clone(),
            line: t.line,
            side: t.side.clone(),
            start_line: t.start_line,
            start_side: t.start_side.clone(),
            commit: t.commit.clone(),
            comments: comments.iter().map(|c| inline_view(c, pull_url)).collect(),
        });
    }
    Ok(out)
}

fn activity_of(r: &impl Rd, p: &Pull, head_sha: &str, pull_url: &str) -> AppResult<Activity> {
    Ok(Activity {
        comments: tickets::comments_of(r, &target(p))?
            .iter()
            .map(|c| comment_view(c, pull_url))
            .collect(),
        reviews: reviews_of(r, &p.repo, p.number)?
            .iter()
            .map(|x| review_view(x, pull_url))
            .collect(),
        threads: threads_of(r, p, head_sha, pull_url)?,
    })
}

/// A statement on a pull request that waits on its author's answer.
#[derive(Serialize, Debug, Clone)]
pub struct Unanswered {
    /// `comment`, `review`, or `thread`.
    pub kind: &'static str,
    /// The entry the author answers: the comment, the review, or the
    /// thread's last comment.
    pub id: String,
    pub author: String,
    pub url: String,
}

/// The statements on `p` its author has not answered, the one place it is
/// decided; none while `p` awaits no answer from its author: the owner wrote
/// it, or it links an interactive ticket of this Branvil (a person works on
/// it with an agent at hand).
pub(crate) fn unanswered(r: &impl Rd, p: &Pull, pull_url: &str) -> AppResult<Vec<Unanswered>> {
    if r.get::<model::Member>(store::MEMBERS, &model::Member::key(&p.author))?
        .is_some_and(|m| m.kind == model::MemberKind::Owner)
    {
        return Ok(Vec::new());
    }
    for id in p.tickets.iter().filter_map(|l| l.local.as_ref()) {
        if let Some(t) = r.get::<model::Ticket>(store::TICKETS, &model::Ticket::key(&id.0, id.1))?
            && (t.interactive || tickets::interactive_ancestor(r, &t)?.is_some())
        {
            return Ok(Vec::new());
        }
    }
    Ok(unanswered_in(&p.author, &activity_of(r, p, "", pull_url)?))
}

/// The statements of `a` that wait on `author`: an unresolved thread whose
/// last comment is someone else's, a review with a body (an approval may
/// carry a suggestion), or a conversation comment without `meta` (one with
/// it is a machine's notice). The author answers a thread by replying in it
/// or reacting to its last comment, and a review or comment by reacting to
/// it or by speaking after it: a comment without `meta`, a review, or an
/// inline comment.
fn unanswered_in(author: &str, a: &Activity) -> Vec<Unanswered> {
    let mine = |name: &str| fold(name) == fold(author);
    let reacted = |reactions: &[ReactionView]| reactions.iter().any(|x| mine(&x.author));
    let inline = || {
        a.threads
            .iter()
            .flat_map(|t| &t.comments)
            .map(|c| &c.comment)
    };
    let spoken = a
        .comments
        .iter()
        .filter(|c| c.meta.is_none())
        .map(|c| (&c.author, &c.created_at))
        .chain(a.reviews.iter().map(|x| (&x.author, &x.submitted_at)))
        .chain(inline().map(|c| (&c.author, &c.created_at)))
        .filter(|(by, _)| mine(by))
        .map(|(_, at)| at.as_str())
        .max()
        .unwrap_or_default();
    let threads = a
        .threads
        .iter()
        .filter(|t| !t.resolved)
        .filter_map(|t| t.comments.last())
        .map(|c| ("thread", &c.comment))
        .filter(|(_, c)| !mine(&c.author) && !reacted(&c.reactions))
        .map(|(kind, c)| (kind, &c.id, &c.author, &c.url));
    let comments = a
        .comments
        .iter()
        .filter(|c| c.meta.is_none() && !mine(&c.author))
        .filter(|c| c.created_at.as_str() > spoken && !reacted(&c.reactions))
        .map(|c| ("comment", &c.id, &c.author, &c.url));
    let reviews = a
        .reviews
        .iter()
        .filter(|x| !x.body.trim().is_empty() && !mine(&x.author))
        .filter(|x| x.submitted_at.as_str() > spoken && !reacted(&x.reactions))
        .map(|x| ("review", &x.id, &x.author, &x.url));
    threads
        .chain(comments)
        .chain(reviews)
        .map(|(kind, id, author, url)| Unanswered {
            kind,
            id: id.clone(),
            author: author.clone(),
            url: url.clone(),
        })
        .collect()
}

pub async fn activity(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
) -> AppResult<Activity> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let head_sha = head_of(&p, &tips(app, &repo).await?).unwrap_or_default();
    let url = app.pull_url(&repo.path, number);
    app.store
        .read(move |r| activity_of(r, &p, &head_sha, &url))
        .await
}

#[derive(Serialize, Debug)]
pub struct PatrolEntry {
    #[serde(flatten)]
    pub pull: PullView,
    pub activity: Activity,
}

#[derive(Deserialize, Debug, Default)]
pub struct PatrolQuery {
    pub involves: Option<String>,
    pub state: Option<String>,
    pub draft: Option<String>,
}

pub async fn patrol(
    app: &App,
    caller: &Caller,
    q: PatrolQuery,
    page: PageReq,
) -> AppResult<Page<PatrolEntry>> {
    if q.involves.as_deref() != Some("me") {
        return Err(AppError::invalid("involves=me is required."));
    }
    if q.state.as_deref().is_some_and(|s| s != "open") {
        return Err(AppError::invalid(
            "Only state=open is offered for involves=me.",
        ));
    }
    let draft = match q.draft.as_deref() {
        None => None,
        Some("true") => Some(true),
        Some("false") => Some(false),
        Some(_) => return Err(AppError::invalid("draft must be true or false.")),
    };
    caller.require_agent_or_owner()?;
    let me = fold(caller.name());
    let owner = caller.is_owner();
    let involved_me = me.clone();
    let found: Vec<(Repo, Pull)> = app
        .store
        .read(move |r| {
            // The owner reviews and merges every pull request, so every open
            // one involves the owner; anyone else is involved as the index says.
            let mut involved = Vec::new();
            if owner {
                for (key, n) in r.prefix_raw(store::IX_OPEN_PULLS, &[], None, usize::MAX)? {
                    let parts = store::decode_key(&key, "sss")
                        .ok_or_else(|| AppError::internal("bad open pull index"))?;
                    let n = String::from_utf8_lossy(&n)
                        .parse()
                        .map_err(AppError::internal)?;
                    involved.push((parts[0].s().to_string(), n));
                }
            } else {
                for key in r.prefix_keys(store::IX_INVOLVED, &Key::new().s(&involved_me).bytes())? {
                    let parts = store::decode_key(&key, "ssu")
                        .ok_or_else(|| AppError::internal("bad involved index"))?;
                    involved.push((parts[1].s().to_string(), parts[2].u()));
                }
            }
            let mut out = Vec::new();
            for (repo_id, number) in involved {
                let repo = repo_by_id(r, &repo_id)?;
                if let Some(p) = r.get::<Pull>(store::PULLS, &Pull::key(&repo.id, number))?
                    && p.state == "open"
                    && draft.is_none_or(|d| p.draft == d)
                {
                    out.push((repo, p));
                }
            }
            Ok(out)
        })
        .await?;
    let mut tips_cache: BTreeMap<String, Tips> = BTreeMap::new();
    let mut positioned: Vec<(Vec<u8>, (Repo, Pull))> = Vec::new();
    for (repo, p) in found {
        if !tips_cache.contains_key(&repo.id) {
            tips_cache.insert(repo.id.clone(), tips(app, &repo).await?);
        }
        let rank = patrol_rank(app, &repo, &p, &me, &tips_cache[&repo.id]).await?;
        positioned.push((
            Key::new()
                .u(rank)
                .s(&p.updated_at)
                .s(&repo.id)
                .u(p.number)
                .bytes(),
            (repo, p),
        ));
    }
    positioned.sort_by(|a, b| a.0.cmp(&b.0));
    let (selected, next_cursor) = page.slice(positioned);
    let mut items = Vec::new();
    for (repo, p) in selected {
        let tips = &tips_cache[&repo.id];
        let view = pull_view(app, &repo, &p, tips).await?;
        let head_sha = view.head_sha.clone();
        let url = view.url.clone();
        let activity = app
            .store
            .read(move |r| activity_of(r, &p, &head_sha, &url))
            .await?;
        items.push(PatrolEntry {
            pull: view,
            activity,
        });
    }
    Ok(Page { items, next_cursor })
}

/// Where `p` stands in `me`'s patrol (`patrol_order`): its author's role or
/// a reviewer's, whether its head has the approvals it needs, and how its
/// required checks stand, a failure the queue keeps it out for counting as
/// failed.
async fn patrol_rank(app: &App, repo: &Repo, p: &Pull, me: &str, tips: &Tips) -> AppResult<u64> {
    let head = tips.get(&p.head).cloned().unwrap_or_default();
    let base = tips.get(&p.base).cloned().unwrap_or_default();
    let approved = approvals(&read_review_states(app, p, &head).await?) >= repo.required_approvals;
    let mut checks = crate::landing::head_checks(
        repo,
        &if head.is_empty() {
            Vec::new()
        } else {
            app.ci.head_checks(repo, &head).await?.checks
        },
    );
    let (id, number) = (repo.id.clone(), p.number);
    if let Some(e) = app
        .store
        .read(move |r| crate::landing::current(r, &id, number))
        .await?
        && crate::landing::failure_stands(app, repo, p, &e, &head, &base).await?
    {
        checks = crate::landing::HeadChecks::Failed;
    }
    Ok(patrol_order(fold(&p.author) == me, approved, checks))
}

/// The patrol's order: first what has the least left to wait for once the
/// member's work is done, and, between the same, a reviewer's work, which
/// takes less time than an author's that may need a fix.
fn patrol_order(author: bool, approved: bool, checks: crate::landing::HeadChecks) -> u64 {
    use crate::landing::HeadChecks::*;
    match (author, approved, checks) {
        // Nothing: the approval puts it in the queue.
        (false, false, Passed) => 1,
        // An answer leaves the checks; a fix, a review and the checks.
        (true, true, Passed | Running) => 2,
        (false, false, Running) => 3,
        // The rest leave a review and the checks after the author's work.
        (true, true, Failed) => 4,
        (true, false, Passed | Running) => 5,
        (true, false, Failed) => 6,
        // The author's fix first.
        (false, false, Failed) => 7,
        // The pull request does not wait on this reviewer.
        (false, true, _) => 8,
    }
}

// ---------------------------------------------------------------- update

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct PullPatch {
    pub title: Option<String>,
    pub body: Option<String>,
    pub tickets: Option<Vec<LinkInput>>,
    #[serde(default)]
    pub drop_ticket_links: bool,
    pub draft: Option<bool>,
    pub revision: Option<u64>,
}

pub async fn update(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    patch: PullPatch,
) -> AppResult<PullView> {
    let (repo, _) = visible(app, caller, repo_path, number).await?;
    if let Some(t) = &patch.title {
        domain::check_title(t)?;
    }
    if let Some(b) = &patch.body {
        domain::check_text("body", b, domain::MAX_TEXT)?;
    }
    if patch.draft == Some(false) && !caller.is_owner() {
        return Err(AppError::forbidden(
            "Only the owner marks a pull request ready for review.",
        ));
    }
    let base_url = app.base_url.clone();
    let r2 = repo.clone();
    let p = app
        .store
        .write(move |w| {
            let old = load_pull(w, &r2, number)?;
            if let Some(rev) = patch.revision
                && rev != old.revision
            {
                return Err(AppError::conflict(format!(
                    "Pull request !{number} is at revision {}, not {rev}.",
                    old.revision
                )));
            }
            let mut p = old.clone();
            let now = util::now();
            if let Some(t) = patch.title {
                p.title = t;
            }
            if let Some(b) = patch.body {
                p.body = b;
            }
            // Links are replaced only when `tickets` is given and dropped only
            // on request; a body change keeps them.
            if let Some(links) = patch.tickets {
                p.tickets = resolve_links(w, &base_url, &r2, &links)?;
            } else if patch.drop_ticket_links {
                p.tickets.clear();
            }
            if let Some(d) = patch.draft {
                if p.draft && !d {
                    p.ready_at = Some(now.clone());
                }
                p.draft = d;
            }
            if serde_json::to_value(&p).ok() != serde_json::to_value(&old).ok() {
                p.revision += 1;
                p.updated_at = now;
                write_pull(w, Some(&old), &p)?;
            }
            Ok(p)
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    if p.upstream.is_some() {
        app.upstream.wake();
    }
    let tips = tips(app, &repo).await?;
    pull_view(app, &repo, &p, &tips).await
}

pub async fn set_reviewer(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    name: &str,
    add: bool,
) -> AppResult<PullView> {
    let (repo, _) = visible(app, caller, repo_path, number).await?;
    let name = name.to_string();
    let r2 = repo.clone();
    let p = app
        .store
        .write(move |w| {
            let member = domain::member_by_name(w, &name)?;
            let old = load_pull(w, &r2, number)?;
            let mut p = old.clone();
            let present = p.reviewers.iter().any(|x| fold(x) == fold(&member.name));
            if add && fold(&member.name) == fold(&p.author) {
                return Err(AppError::invalid(
                    "The author cannot be a reviewer of their own pull request.",
                ));
            }
            if add && !present {
                p.reviewers.push(member.name.clone());
            } else if !add && present {
                p.reviewers.retain(|x| fold(x) != fold(&member.name));
            } else {
                return Ok(p);
            }
            p.revision += 1;
            p.updated_at = util::now();
            write_pull(w, Some(&old), &p)?;
            Ok(p)
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    let tips = tips(app, &repo).await?;
    pull_view(app, &repo, &p, &tips).await
}

/// Closes or reopens a pull request.
pub async fn set_open(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    open: bool,
) -> AppResult<PullView> {
    let (repo, _) = visible(app, caller, repo_path, number).await?;
    let r2 = repo.clone();
    let p = app
        .locked(&repo.id, move |app| async move {
            app.store
                .write(move |w| set_open_in(w, &r2, number, open))
                .await
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    if open && p.state == "open" {
        app.ci.on_pull_opened(app, &repo, &p.head).await;
    }
    if p.upstream.is_some() {
        app.upstream.wake();
    }
    let tips = tips(app, &repo).await?;
    pull_view(app, &repo, &p, &tips).await
}

/// Closes or reopens a pull request in a write transaction; nothing changes
/// when it already is so.
pub fn set_open_in(w: &Wtx, repo: &Repo, number: u64, open: bool) -> AppResult<Pull> {
    let old = load_pull(w, repo, number)?;
    if old.merged {
        return Err(AppError::conflict(format!(
            "Pull request !{number} is merged."
        )));
    }
    let mut p = old.clone();
    let now = util::now();
    match (open, old.state.as_str()) {
        (true, "closed") => {
            if w.exists(
                store::IX_OPEN_PULLS,
                &Pull::open_key(&p.repo, &p.head, &p.base),
            )? {
                return Err(AppError::conflict(format!(
                    "Another open pull request has head {} and base {}.",
                    p.head, p.base
                )));
            }
            p.state = "open".into();
            p.closed_at = None;
        }
        (false, "open") => {
            p.state = "closed".into();
            p.closed_at = Some(now.clone());
            crate::landing::mark_closed(w, &repo.id, number)?;
        }
        _ => return Ok(p),
    }
    p.revision += 1;
    p.updated_at = now;
    write_pull(w, Some(&old), &p)?;
    Ok(p)
}

/// Replaces what is recorded about the pull request's upstream copy and,
/// when the host closed or reopened it, applies that (`open`). Returns the
/// pull request when anything changed.
pub fn store_published(
    w: &Wtx,
    repo: &Repo,
    number: u64,
    published: Published,
    open: Option<bool>,
) -> AppResult<Option<Pull>> {
    let old = load_pull(w, repo, number)?;
    let mut changed = old.upstream.as_ref() != Some(&published);
    if changed {
        // Keep the wait clock's publication/error semantics without storing
        // a second copy of the host's publication flag in Landing.
        let stage = |x: Option<&Published>| {
            (
                x.is_some_and(|x| x.number.is_some() && x.stopped.is_none()),
                x.and_then(|x| x.error.clone()),
            )
        };
        if stage(old.upstream.as_ref()) != stage(Some(&published)) {
            crate::landing::publication_changed(w, &repo.id, number)?;
        }
        let mut p = old.clone();
        p.upstream = Some(published);
        write_pull(w, Some(&old), &p)?;
    }
    if let Some(open) = open
        && (old.state == "open") != open
        && !old.merged
    {
        set_open_in(w, repo, number, open)?;
        changed = true;
    }
    changed.then(|| load_pull(w, repo, number)).transpose()
}

// ---------------------------------------------------------------- comments, reviews, threads

/// Who an activity on a pull request is by, as far as its patrol cares.
enum By<'a> {
    /// No one the pull request then involves (a conversation comment, a resolution).
    Nobody,
    /// A reviewer joining the review without a verdict (a thread reply).
    Participant(&'a str),
    /// A reviewer giving the review (a verdict or a new inline comment), which
    /// answers the member's review request; a later request asks again.
    Reviewer(&'a str),
}

/// Marks activity on a pull request inside a write transaction.
fn touch(w: &Wtx, repo: &Repo, number: u64, by: By, at: &str) -> AppResult<Pull> {
    let old = load_pull(w, repo, number)?;
    let mut p = old.clone();
    p.updated_at = at.to_string();
    if let By::Participant(m) | By::Reviewer(m) = by
        && !p.reviewed_by.iter().any(|x| fold(x) == fold(m))
    {
        p.reviewed_by.push(m.to_string());
    }
    if let By::Reviewer(m) = by {
        p.reviewers.retain(|x| fold(x) != fold(m));
    }
    write_pull(w, Some(&old), &p)?;
    Ok(p)
}

pub async fn list_comments(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    page: PageReq,
) -> AppResult<Page<CommentView>> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let url = app.pull_url(&repo.path, number);
    app.store
        .read(move |r| {
            let (items, next_cursor) =
                page.scan(r, store::COMMENTS, &target(&p).prefix(), |_, v| {
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
    number: u64,
    input: NewComment,
) -> AppResult<CommentView> {
    tickets::check_comment(&input)?;
    let (repo, _) = visible(app, caller, repo_path, number).await?;
    let author = caller.name().to_string();
    let url = app.pull_url(&repo.path, number);
    let r2 = repo.clone();
    let c = app
        .store
        .write(move |w| {
            let p = load_pull(w, &r2, number)?;
            let c = tickets::insert_comment(w, &target(&p), &author, input.body, input.meta)?;
            touch(w, &r2, number, By::Nobody, &c.created_at)?;
            Ok(c)
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    Ok(comment_view(&c, &url))
}

/// A service-authored conversation entry follows the same activity ordering
/// as a user's comment, without requiring a member account for Branvil.
pub(crate) fn system_comment(w: &Wtx, repo: &Repo, number: u64, body: String) -> AppResult<()> {
    let p = load_pull(w, repo, number)?;
    let c = tickets::insert_comment(w, &target(&p), "branvil", body, None)?;
    touch(w, repo, number, By::Nobody, &c.created_at)?;
    Ok(())
}

/// Refuses an entry on a commit that is not the pull request's current head.
async fn require_head(app: &App, repo: &Repo, p: &Pull, commit: &str) -> AppResult<()> {
    if p.state != "open" {
        return Err(AppError::conflict(format!(
            "Pull request !{} is closed.",
            p.number
        )));
    }
    let head = tips(app, repo)
        .await?
        .get(&p.head)
        .cloned()
        .unwrap_or_default();
    if head.is_empty() || head != commit {
        return Err(AppError::conflict_code(
            "head_moved",
            format!(
                "The head of !{} is {}, not {commit}. Nothing was written.",
                p.number,
                if head.is_empty() {
                    "gone"
                } else {
                    head.as_str()
                }
            ),
        ));
    }
    Ok(())
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewReview {
    pub verdict: String,
    #[serde(default)]
    pub body: String,
    pub commit: String,
}

#[derive(Serialize, Debug)]
pub struct ReviewSubmitted {
    pub id: String,
    pub verdict: String,
    pub commit: String,
    pub submitted_at: String,
    pub url: String,
}

pub async fn review(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    input: NewReview,
) -> AppResult<ReviewSubmitted> {
    if !["approve", "request_changes", "comment"].contains(&input.verdict.as_str()) {
        return Err(AppError::invalid(
            "verdict must be approve, request_changes, or comment.",
        ));
    }
    domain::check_text("body", &input.body, domain::MAX_TEXT)?;
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    if input.verdict != "comment" && fold(caller.name()) == fold(&p.author) {
        return Err(AppError::invalid(
            "The author cannot approve or request changes on their own pull request; only comment.",
        ));
    }
    // The head is read from Git just before the write: a review is recorded
    // only on the head the caller read; a later push does not change it.
    require_head(app, &repo, &p, &input.commit).await?;
    let author = caller.name().to_string();
    let url = app.pull_url(&repo.path, number);
    let r2 = repo.clone();
    let x = app
        .store
        .write(move |w| {
            let p = load_pull(w, &r2, number)?;
            let seq = w.next("entry")?;
            let x = Review {
                id: util::new_id("rev"),
                seq,
                author: author.clone(),
                verdict: input.verdict,
                body: input.body,
                commit: input.commit,
                submitted_at: util::now(),
                reactions: Vec::new(),
            };
            let key = model::review_key(&p.repo, p.number, seq);
            model::put_indexed(
                w,
                store::REVIEWS,
                &key,
                None,
                &x,
                model::entry_index(&x.id, "review", &target(&p).owner(), &key),
            )?;
            touch(w, &r2, number, By::Reviewer(&author), &x.submitted_at)?;
            Ok(x)
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    Ok(ReviewSubmitted {
        url: format!("{url}#{}", x.id),
        id: x.id,
        verdict: x.verdict,
        commit: x.commit,
        submitted_at: x.submitted_at,
    })
}

pub async fn list_reviews(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    page: PageReq,
) -> AppResult<Page<ReviewView>> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let url = app.pull_url(&repo.path, number);
    app.store
        .read(move |r| {
            let (items, next_cursor) = page.scan(
                r,
                store::REVIEWS,
                &Key::new().s(&p.repo).u(p.number).bytes(),
                |_, v| Ok(Some(review_view(&store::decode(v)?, &url))),
            )?;
            Ok(Page { items, next_cursor })
        })
        .await
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewInline {
    pub body: String,
    pub commit: String,
    pub path: String,
    pub line: u64,
    pub side: String,
    pub start_line: Option<u64>,
    pub start_side: Option<String>,
}

pub async fn inline_comment(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    input: NewInline,
) -> AppResult<ThreadCommentView> {
    if input.body.trim().is_empty() {
        return Err(AppError::invalid("body must not be empty."));
    }
    domain::check_text("body", &input.body, domain::MAX_TEXT)?;
    let sides = ["LEFT", "RIGHT"];
    if !sides.contains(&input.side.as_str())
        || input
            .start_side
            .as_ref()
            .is_some_and(|s| !sides.contains(&s.as_str()))
    {
        return Err(AppError::invalid("side must be LEFT or RIGHT."));
    }
    if input.line == 0 || input.start_line.is_some_and(|s| s == 0 || s > input.line) {
        return Err(AppError::invalid(
            "line must be at least 1 and start_line at most line.",
        ));
    }
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    require_head(app, &repo, &p, &input.commit).await?;
    let tips_now = tips(app, &repo).await?;
    if let Some(range) = diff_range(&p, &tips_now) {
        let paths = app
            .git
            .changed_paths(&app.git.repo_dir(&repo.path), &range)
            .await?;
        if !paths.contains(&input.path) {
            return Err(AppError::invalid(format!(
                "{} is not changed by !{number}.",
                input.path
            )));
        }
    }
    let author = caller.name().to_string();
    let url = app.pull_url(&repo.path, number);
    let r2 = repo.clone();
    let c = app
        .store
        .write(move |w| {
            let p = load_pull(w, &r2, number)?;
            let seq = w.next("entry")?;
            let now = util::now();
            let c = InlineComment {
                id: util::new_id("ilc"),
                seq,
                root_seq: seq,
                author: author.clone(),
                body: input.body,
                created_at: now.clone(),
                reactions: Vec::new(),
                reply_to_id: None,
            };
            let t = Thread {
                id: c.id.clone(),
                root_seq: seq,
                path: input.path,
                line: input.line,
                side: input.side,
                start_line: input.start_line,
                start_side: input.start_side,
                commit: input.commit,
                resolved: false,
            };
            w.put(
                store::THREADS,
                &model::thread_key(&p.repo, p.number, seq),
                &t,
            )?;
            let key = model::inline_key(&p.repo, p.number, seq, seq);
            model::put_indexed(
                w,
                store::INLINE,
                &key,
                None,
                &c,
                model::entry_index(&c.id, "inline", &target(&p).owner(), &key),
            )?;
            touch(w, &r2, number, By::Reviewer(&author), &now)?;
            Ok(c)
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    Ok(inline_view(&c, &url))
}

pub async fn list_threads(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    page: PageReq,
) -> AppResult<Page<ThreadView>> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let head_sha = head_of(&p, &tips(app, &repo).await?).unwrap_or_default();
    let url = app.pull_url(&repo.path, number);
    app.store
        .read(move |r| {
            let all = threads_of(r, &p, &head_sha, &url)?;
            let positioned = all
                .into_iter()
                .enumerate()
                .map(|(i, t)| (Key::new().u(i as u64).bytes(), t))
                .collect();
            let (items, next_cursor) = page.slice(positioned);
            Ok(Page { items, next_cursor })
        })
        .await
}

/// The thread a root comment id names, only within this pull request.
fn thread_of(r: &impl Rd, p: &Pull, id: &str) -> AppResult<Thread> {
    let not_found = || AppError::not_found(format!("Thread {id} was not found on !{}.", p.number));
    let entry = domain::entry(r, id, &target(p).owner())?
        .filter(|e| e.kind == "inline")
        .ok_or_else(not_found)?;
    let key = domain::entry_key(&entry)?;
    let parts = store::decode_key(&key, "suuu").ok_or_else(not_found)?;
    let (root_seq, seq) = (parts[2].u(), parts[3].u());
    if root_seq != seq {
        return Err(not_found());
    }
    r.get(
        store::THREADS,
        &model::thread_key(&p.repo, p.number, root_seq),
    )?
    .ok_or_else(not_found)
}

pub async fn thread_comments(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    id: &str,
    page: PageReq,
) -> AppResult<Page<ThreadCommentView>> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let url = app.pull_url(&repo.path, number);
    let id = id.to_string();
    app.store
        .read(move |r| {
            let t = thread_of(r, &p, &id)?;
            let prefix = Key::new().s(&p.repo).u(p.number).u(t.root_seq).bytes();
            let (items, next_cursor) = page.scan(r, store::INLINE, &prefix, |_, v| {
                Ok(Some(inline_view(&store::decode(v)?, &url)))
            })?;
            Ok(Page { items, next_cursor })
        })
        .await
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewReply {
    pub body: String,
}

pub async fn reply(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    id: &str,
    input: NewReply,
) -> AppResult<ThreadCommentView> {
    if input.body.trim().is_empty() {
        return Err(AppError::invalid("body must not be empty."));
    }
    domain::check_text("body", &input.body, domain::MAX_TEXT)?;
    let (repo, _) = visible(app, caller, repo_path, number).await?;
    let author = caller.name().to_string();
    let url = app.pull_url(&repo.path, number);
    let id = id.to_string();
    let r2 = repo.clone();
    let c = app
        .store
        .write(move |w| {
            let p = load_pull(w, &r2, number)?;
            let t = thread_of(w, &p, &id)?;
            let seq = w.next("entry")?;
            let now = util::now();
            let c = InlineComment {
                id: util::new_id("ilc"),
                seq,
                root_seq: t.root_seq,
                author: author.clone(),
                body: input.body,
                created_at: now.clone(),
                reactions: Vec::new(),
                reply_to_id: Some(t.id.clone()),
            };
            let key = model::inline_key(&p.repo, p.number, t.root_seq, seq);
            model::put_indexed(
                w,
                store::INLINE,
                &key,
                None,
                &c,
                model::entry_index(&c.id, "inline", &target(&p).owner(), &key),
            )?;
            touch(w, &r2, number, By::Participant(&author), &now)?;
            Ok(c)
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    Ok(inline_view(&c, &url))
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Resolution {
    pub resolved: bool,
}

pub async fn resolve(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    id: &str,
    input: Resolution,
) -> AppResult<ThreadView> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let head_sha = head_of(&p, &tips(app, &repo).await?).unwrap_or_default();
    let url = app.pull_url(&repo.path, number);
    let id = id.to_string();
    let r2 = repo.clone();
    let view = app
        .store
        .write(move |w| {
            let p = load_pull(w, &r2, number)?;
            let mut t = thread_of(w, &p, &id)?;
            if t.resolved != input.resolved {
                t.resolved = input.resolved;
                w.put(
                    store::THREADS,
                    &model::thread_key(&p.repo, p.number, t.root_seq),
                    &t,
                )?;
                touch(w, &r2, number, By::Nobody, &util::now())?;
            }
            Ok(threads_of(w, &p, &head_sha, &url)?
                .into_iter()
                .find(|x| x.id == t.id))
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    view.ok_or_else(|| AppError::internal("resolved thread vanished"))
}

/// Gives (`on`) or takes back the caller's reaction to a conversation
/// comment, a thread comment, or a review of the pull request.
pub async fn react(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    input: NewReaction,
    on: bool,
) -> AppResult<Option<ReactionAdded>> {
    domain::check_reaction(&input.reaction)?;
    let (repo, _) = visible(app, caller, repo_path, number).await?;
    let author = caller.name().to_string();
    let r2 = repo.clone();
    let added = app
        .store
        .write(move |w| {
            let p = load_pull(w, &r2, number)?;
            let not_found = || {
                AppError::not_found(format!(
                    "Entry {} was not found on !{number}.",
                    input.target_id
                ))
            };
            let entry =
                domain::entry(w, &input.target_id, &target(&p).owner())?.ok_or_else(not_found)?;
            let key = domain::entry_key(&entry)?;
            let r = match entry.kind.as_str() {
                "comment" => {
                    let mut c: model::Comment =
                        w.get(store::COMMENTS, &key)?.ok_or_else(not_found)?;
                    let r = domain::set_reaction(&mut c.reactions, &author, &input.reaction, on);
                    w.put(store::COMMENTS, &key, &c)?;
                    r
                }
                "review" => {
                    let mut x: Review = w.get(store::REVIEWS, &key)?.ok_or_else(not_found)?;
                    let r = domain::set_reaction(&mut x.reactions, &author, &input.reaction, on);
                    w.put(store::REVIEWS, &key, &x)?;
                    r
                }
                "inline" => {
                    let mut c: InlineComment = w.get(store::INLINE, &key)?.ok_or_else(not_found)?;
                    let r = domain::set_reaction(&mut c.reactions, &author, &input.reaction, on);
                    w.put(store::INLINE, &key, &c)?;
                    r
                }
                _ => return Err(not_found()),
            };
            Ok(r.map(|r| ReactionAdded {
                id: r.id,
                reaction: r.reaction,
            }))
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    Ok(added)
}

// ---------------------------------------------------------------- files and checks

pub async fn files(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    page: PageReq,
) -> AppResult<Page<FileChange>> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let tips = tips(app, &repo).await?;
    let all = match diff_range(&p, &tips) {
        Some(range) => {
            app.git
                .diff_files(&app.git.repo_dir(&repo.path), &range, PATCH_LIMIT)
                .await?
        }
        None => Vec::new(),
    };
    let positioned = all
        .into_iter()
        .enumerate()
        .map(|(i, f)| (Key::new().u(i as u64).bytes(), f))
        .collect();
    let (items, next_cursor) = page.slice(positioned);
    Ok(Page { items, next_cursor })
}

/// The commits the head has and the base does not, oldest first.
pub async fn commits(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    page: PageReq,
) -> AppResult<Page<CommitSummary>> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let tips = tips(app, &repo).await?;
    let all = match base_of(&p, &tips).zip(head_of(&p, &tips)) {
        Some((base, head)) => {
            app.git
                .commits(&app.git.repo_dir(&repo.path), &format!("{base}..{head}"))
                .await?
        }
        None => Vec::new(),
    };
    let positioned = all
        .into_iter()
        .enumerate()
        .map(|(i, c)| (Key::new().u(i as u64).bytes(), c))
        .collect();
    let (items, next_cursor) = page.slice(positioned);
    Ok(Page { items, next_cursor })
}

#[derive(Serialize, Debug)]
pub struct ChecksView {
    pub head_sha: String,
    pub base_sha: String,
    /// `None` when the head or the base branch is gone.
    pub behind_by: Option<u64>,
    pub checks: Vec<Check>,
    pub checks_expected: bool,
    pub base_checks: usize,
}

pub async fn checks(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
) -> AppResult<ChecksView> {
    let (repo, p) = visible(app, caller, repo_path, number).await?;
    let tips = tips(app, &repo).await?;
    let head_sha = head_of(&p, &tips).unwrap_or_default();
    let base_sha = tips.get(&p.base).cloned().unwrap_or_default();
    let behind_by = if head_sha.is_empty() || base_sha.is_empty() {
        None
    } else {
        Some(
            app.git
                .count_between(&app.git.repo_dir(&repo.path), &head_sha, &base_sha)
                .await?,
        )
    };
    let head = if head_sha.is_empty() {
        crate::ci::Checks {
            run_registered: false,
            checks: Vec::new(),
            checks_expected: !repo.required_checks.is_empty(),
        }
    } else {
        app.ci.head_checks(&repo, &head_sha).await?
    };
    let base_checks = if base_sha.is_empty() {
        0
    } else {
        app.ci
            .head_checks(&repo, &base_sha)
            .await?
            .checks
            .iter()
            .filter(|c| c.recorded())
            .count()
    };
    let mut checks = head.checks;
    checks.extend(crate::landing::checks(app, &repo, number, &head_sha).await?);
    app.ci.estimate(&repo, &head_sha, &mut checks).await;
    if let Some(published) = &p.upstream
        && published.pushed.as_deref() == Some(head_sha.as_str())
    {
        let provider = &published.provider;
        checks.extend(published.checks.iter().map(|c| Check {
            name: format!("{provider}/{}", c.name),
            status: c.status.clone(),
            conclusion: c.conclusion.clone(),
            attempt_id: Some(format!("{provider}-{}", c.id)),
            details_url: c.url.clone(),
            details: c.summary.clone(),
            ..Check::default()
        }));
        if p.state == "open" && published.checks_to_come() {
            checks.push(Check {
                name: provider.clone(),
                status: "queued".into(),
                details: Some(format!(
                    "{} may still add checks of this commit",
                    app.upstream.host_name(provider)
                )),
                ..Check::default()
            });
        }
    }
    Ok(ChecksView {
        head_sha,
        base_sha,
        behind_by,
        checks,
        checks_expected: head.checks_expected,
        base_checks,
    })
}

// ---------------------------------------------------------------- merge

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct MergeInput {
    pub expected_head: String,
    pub expected_base: String,
}

fn not_mergeable(message: impl Into<String>) -> AppError {
    AppError::conflict_code("not_mergeable", message)
}

/// The checks a merge and a publish share: open, not a draft, the head and
/// base the owner saw, successful required head checks and approvals, and a
/// passed gate at the front of the current base queue; for one
/// made by the repository itself (`auto`), that it still lands by itself.
/// Returns the checked head, base, and gate. Call under the repository lock with the
/// repository and the pull request read under it.
pub async fn check_landable(
    app: &App,
    repo: &Repo,
    p: &Pull,
    input: &MergeInput,
    auto: bool,
) -> AppResult<(String, String, String)> {
    let number = p.number;
    if auto && !repo.auto_land {
        return Err(not_mergeable(format!(
            "{} no longer lands its pull requests by itself.",
            repo.path
        )));
    }
    if p.state != "open" {
        return Err(not_mergeable(format!("!{number} is not open.")));
    }
    if p.draft {
        return Err(not_mergeable(format!("!{number} is a draft.")));
    }
    let tips = tips(app, repo).await?;
    let head = tips.get(&p.head).cloned().unwrap_or_default();
    let base = tips.get(&p.base).cloned().unwrap_or_default();
    if head != input.expected_head {
        return Err(AppError::conflict_code(
            "head_moved",
            format!(
                "The head of !{number} is {head}, not {}. Nothing was written.",
                input.expected_head
            ),
        ));
    }
    if base != input.expected_base {
        return Err(AppError::conflict_code(
            "base_moved",
            format!(
                "The base of !{number} is {base}, not {}. Nothing was written.",
                input.expected_base
            ),
        ));
    }
    let checks = app.ci.head_checks(repo, &head).await?;
    let missing: Vec<&str> = repo
        .required_checks
        .iter()
        .filter(|name| {
            !checks
                .checks
                .iter()
                .any(|c| &c.name == *name && c.head_satisfied())
        })
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(not_mergeable(format!(
            "Required checks did not succeed on {head}: {}.",
            missing.join(", ")
        )));
    }
    let approved = approvals(&read_review_states(app, p, &head).await?);
    if approved < repo.required_approvals {
        return Err(not_mergeable(format!(
            "Approvals of {head}: {approved} / {} required.",
            repo.required_approvals
        )));
    }
    let id = repo.id.clone();
    let branch = p.base.clone();
    let gate = app
        .store
        .read(move |r| {
            let queue = crate::landing::active(r, &id)?;
            Ok(queue.into_iter().find(|e| e.base == branch))
        })
        .await?;
    let gate = gate
        .filter(|e| {
            e.number == number && e.state == "passed" && e.head_sha == head && e.base_sha == base
        })
        .and_then(|e| e.gate)
        .ok_or_else(|| {
            not_mergeable(format!(
                "!{number} has not passed the current landing gate at the front of its queue."
            ))
        })?;
    let gate_checks = app.ci.head_checks(repo, &gate).await?;
    if !repo.required_checks.iter().all(|n| {
        gate_checks
            .checks
            .iter()
            .any(|c| &c.name == n && c.succeeded())
    }) {
        return Err(not_mergeable(
            "The required landing gate checks have not succeeded.",
        ));
    }
    Ok((head, base, gate))
}

pub async fn merge(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    number: u64,
    input: MergeInput,
) -> AppResult<PullView> {
    let (repo, _) = visible(app, caller, repo_path, number).await?;
    caller.require_owner()?;
    merge_pull(app, &repo, number, input, false).await
}

/// Merges a pull request of `repo`, by the owner or (`auto`) by the
/// repository landing automatically (`autoland`).
pub async fn merge_pull(
    app: &App,
    repo: &Repo,
    number: u64,
    input: MergeInput,
    auto: bool,
) -> AppResult<PullView> {
    let id = repo.id.clone();
    let (repo, merged) = app
        .locked(&repo.id, move |app| async move {
            merge_locked(&app, &id, number, input, auto).await
        })
        .await?;
    let tips = self::tips(app, &repo).await?;
    pull_view(app, &repo, &merged, &tips).await
}

/// `merge_pull` under the repository lock: returns the repository and the
/// merged pull request.
async fn merge_locked(
    app: &App,
    repo_id: &str,
    number: u64,
    input: MergeInput,
    auto: bool,
) -> AppResult<(Repo, Pull)> {
    // Everything below is re-read under the repository lock.
    let repo = {
        let id = repo_id.to_string();
        app.store.read(move |r| repo_by_id(r, &id)).await?
    };
    if let Some(up) = &repo.upstream {
        return Err(not_mergeable(format!(
            "{} merges on {}; publish !{number} instead.",
            repo.path,
            app.upstream.host_name(&up.provider)
        )));
    }
    let r2 = repo.clone();
    let p = app.store.read(move |r| load_pull(r, &r2, number)).await?;
    crate::landing::reconcile_locked(app, &repo).await?;
    let (head, base, gate) = check_landable(app, &repo, &p, &input, auto).await?;
    let dir = app.git.repo_dir(&repo.path);
    keep_merged(app, &repo, number, &head).await?;
    let entry = JournalEntry {
        id: util::new_id("jnl"),
        repo: repo.id.clone(),
        refname: format!("refs/heads/{}", p.base),
        old: base.clone(),
        new: gate.clone(),
        op: JournalOp::Merge { number },
        created_at: util::now(),
    };
    let e = entry.clone();
    app.store.write(move |w| refs::journal_begin(w, &e)).await?;
    app.failpoint("merge_before_update_ref");
    // When the ref cannot even be read, the entry stays: the next start
    // settles it from the ref.
    if !app
        .git
        .update_ref(
            &dir,
            &entry.refname,
            &gate,
            &base,
            &format!("branvil: merge !{number}"),
        )
        .await?
    {
        let id = entry.id.clone();
        app.store.write(move |w| refs::journal_end(w, &id)).await?;
        return Err(AppError::conflict_code(
            "base_moved",
            format!("The base of !{number} moved during the merge. Nothing was written."),
        ));
    }
    app.failpoint("merge_after_update_ref");
    let e = entry.clone();
    let merged = app
        .store
        .write(move |w| {
            let p = apply_journal(w, &e)?;
            refs::journal_end(w, &e.id)?;
            Ok(p)
        })
        .await?;
    crate::landing::remove_merged_ref(app, &repo, number).await?;
    retire_merged_head(app, &repo, &merged).await?;
    refs::record_refs_locked(app, &repo).await?;
    app.events.publish("pull", &repo.path, number.to_string());
    tickets::publish_closed_by(app, &merged).await;
    Ok((repo, merged))
}

/// Points the hidden ref `refs/branvil/merged/<number>` at the head a pull
/// request merges, for a caller that holds the repository lock, before the
/// merge is recorded: every merged pull request has it. Its view reads that
/// commit after its branch is gone or moved, and a squash or a rebase on the
/// upstream host leaves it on no branch (unreachable, it would be missing from
/// backups and pruned by Git).
pub async fn keep_merged(app: &App, repo: &Repo, number: u64, sha: &str) -> AppResult<()> {
    app.git
        .run_ok(
            Some(&app.git.repo_dir(&repo.path)),
            &[
                "update-ref",
                "-m",
                &format!("branvil: merging !{number}"),
                &format!("refs/branvil/merged/{number}"),
                sha,
            ],
        )
        .await?;
    Ok(())
}

/// Deletes the head branch of a merged pull request (`keep_merged` kept what
/// it merged), for a caller that holds the repository lock and records the
/// ref changes after it (the recording takes a branch's move before a
/// deletion, so a commit merged into the default branch keeps its CI run).
/// The branch stays when it is protected, when an open pull request has it as
/// its head or base, or when it is no longer at what was merged.
pub async fn retire_merged_head(app: &App, repo: &Repo, p: &Pull) -> AppResult<()> {
    let Some(merged) = head_of(p, &Tips::new()) else {
        return Ok(());
    };
    let (id, branch) = (repo.id.clone(), p.head.clone());
    let kept = app
        .store
        .read(move |r| {
            if repo_by_id(r, &id)?.is_protected(&branch) {
                return Ok(true);
            }
            let open = r.prefix_raw(
                store::IX_OPEN_PULLS,
                &Key::new().s(&id).bytes(),
                None,
                usize::MAX,
            )?;
            Ok(open.iter().any(|(k, _)| {
                store::decode_key(k, "sss")
                    .is_some_and(|p| p[1].s() == branch || p[2].s() == branch)
            }))
        })
        .await?;
    if !kept {
        app.git
            .update_ref(
                &app.git.repo_dir(&repo.path),
                &format!("refs/heads/{}", p.head),
                ZERO_SHA,
                &merged,
                &format!("branvil: delete the head of merged !{}", p.number),
            )
            .await?;
    }
    Ok(())
}

/// Applies the database side of a journaled ref change whose Git side is
/// done. Idempotent: a merge already applied is left as it is.
pub fn apply_journal(w: &Wtx, entry: &JournalEntry) -> AppResult<Pull> {
    match &entry.op {
        JournalOp::Merge { number, .. } => {
            let repo = repo_by_id(w, &entry.repo)?;
            mark_merged(w, &repo, *number, &entry.new, Some(entry.old.clone()))
        }
    }
}

/// Records a merge (here or on the upstream host) and closes the tickets the
/// pull request closes. Idempotent: a merged pull request is left as it is.
pub fn mark_merged(
    w: &Wtx,
    repo: &Repo,
    number: u64,
    merge_sha: &str,
    merged_base: Option<String>,
) -> AppResult<Pull> {
    let old = load_pull(w, repo, number)?;
    if old.merged {
        return Ok(old);
    }
    let now = util::now();
    let mut p = old.clone();
    p.state = "closed".into();
    p.merged = true;
    p.merged_at = Some(now.clone());
    p.closed_at = Some(now.clone());
    p.merge_sha = Some(merge_sha.to_string());
    p.merged_head = if p.upstream.as_ref().is_some_and(|x| x.state == "merged") {
        p.upstream.as_ref().and_then(|x| x.pushed.clone())
    } else {
        crate::landing::current(w, &repo.id, number)?.map(|e| e.head_sha)
    }
    .or_else(|| Some(merge_sha.to_string()));
    crate::landing::mark_merged(w, &repo.id, number)?;
    p.merged_base = merged_base;
    p.updated_at = now.clone();
    p.revision += 1;
    write_pull(w, Some(&old), &p)?;
    // A link closes only a ticket of this repository (`resolve_links`). A
    // parent closes after the children the merge closes, whatever the order
    // of the links.
    let mut left: Vec<_> = p
        .tickets
        .iter()
        .filter(|l| l.closes)
        .filter_map(|l| l.local.clone())
        .collect();
    loop {
        let before = left.len();
        let mut open = Vec::new();
        for t in left {
            if !tickets::close_by_merge(w, &t, &now)? {
                open.push(t);
            }
        }
        if open.len() == before {
            break;
        }
        left = open;
    }
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::domain::members::{NewMember, insert_member};
    use crate::domain::repos::{self, NewRepo, NewWorkspace};
    use crate::model::MemberKind;
    use std::time::Duration;

    async fn fixture() -> (tempfile::TempDir, crate::app::Shared, Caller, Repo, Pull) {
        let temp = tempfile::tempdir().unwrap();
        let config = Config::load(None, Some(temp.path()), None, None).unwrap();
        let layout = config.layout();
        layout.create().unwrap();
        let store = store::Store::open(&layout.db(), 1 << 20, 2).unwrap();
        let member = store
            .write_sync(|w| {
                insert_member(
                    w,
                    &NewMember {
                        name: "owner1".into(),
                        display_name: None,
                        kind: MemberKind::Owner,
                    },
                )
            })
            .unwrap();
        let caller = Caller { member };
        let app = App::new(
            config,
            "http://127.0.0.1:7878".into(),
            store,
            vec![],
            "http://127.0.0.1:7878".into(),
        );
        repos::create_workspace(
            &app,
            &caller,
            NewWorkspace {
                name: "acme".into(),
            },
        )
        .await
        .unwrap();
        repos::create(
            &app,
            &caller,
            NewRepo {
                path: "acme/demo".into(),
                default_branch: None,
                labels: vec![],
            },
        )
        .await
        .unwrap();
        let repo = repos::visible(&app, &caller, "acme/demo").await.unwrap();
        let p = Pull {
            repo: repo.id.clone(),
            number: 1,
            title: "Serialize closing".into(),
            body: String::new(),
            state: "open".into(),
            merged: false,
            draft: false,
            author: "owner1".into(),
            head: "feature".into(),
            base: "main".into(),
            reviewers: vec![],
            tickets: vec![],
            labels: vec![],
            reviewed_by: vec![],
            ready_at: None,
            created_at: util::now(),
            updated_at: util::now(),
            closed_at: None,
            merged_at: None,
            merge_sha: None,
            merged_head: None,
            merged_base: None,
            upstream: None,
            revision: 1,
        };
        (temp, app, caller, repo, p)
    }

    #[tokio::test]
    async fn equal_update_times_page_by_descending_number_without_losing_ties() {
        let (_temp, app, caller, repo, pull) = fixture().await;
        app.store
            .write(move |w| {
                for number in 1..=35 {
                    let p = Pull {
                        number,
                        updated_at: "2026-10-20T00:00:00.000Z".into(),
                        ..pull.clone()
                    };
                    write_pull(w, None, &p)?;
                }
                Ok(())
            })
            .await
            .unwrap();
        for limit in [1, 30, 100] {
            let mut cursor = None;
            let mut seen = Vec::new();
            loop {
                let page =
                    PageReq::new(b"key", "pulls".into(), Some(limit), cursor.as_deref()).unwrap();
                let result = list(&app, &caller, &repo.path, PullQuery::default(), page)
                    .await
                    .unwrap();
                seen.extend(result.items.into_iter().map(|p| p.number));
                cursor = result.next_cursor;
                if cursor.is_none() {
                    break;
                }
            }
            assert_eq!(seen, (1..=35).rev().collect::<Vec<_>>());
        }
    }

    #[tokio::test]
    async fn closing_waits_for_queue_work_and_reopening_releases_the_lock_before_ci() {
        let (_temp, app, caller, repo, p) = fixture().await;
        app.store
            .write(move |w| write_pull(w, None, &p))
            .await
            .unwrap();
        let held = app.hold_lock(&repo.id).await;
        let mut closing = Box::pin(set_open(&app, &caller, &repo.path, 1, false));
        assert!(
            tokio::time::timeout(Duration::from_millis(50), closing.as_mut())
                .await
                .is_err(),
            "closing must wait for the in-flight queue update"
        );
        let r2 = repo.clone();
        assert_eq!(
            app.store
                .read(move |r| Ok(load_pull(r, &r2, 1)?.state))
                .await
                .unwrap(),
            "open"
        );
        drop(held);
        assert_eq!(closing.await.unwrap().state, "closed");
        let reopened = tokio::time::timeout(
            Duration::from_secs(5),
            set_open(&app, &caller, &repo.path, 1, true),
        )
        .await
        .expect("reopening must not hold the repository lock while registering CI")
        .unwrap();
        assert_eq!(reopened.state, "open");
    }

    fn comment(id: &str, author: &str, at: &str, meta: bool, reactors: &[&str]) -> CommentView {
        CommentView {
            id: id.into(),
            author: author.into(),
            body: "b".into(),
            mentions: None,
            meta: meta.then(|| serde_json::json!({"kind": "notice"})),
            created_at: at.into(),
            reactions: reactors
                .iter()
                .map(|a| ReactionView {
                    author: a.to_string(),
                    reaction: "ack".into(),
                })
                .collect(),
            url: format!("u#{id}"),
        }
    }

    fn review(id: &str, author: &str, at: &str, body: &str, reactors: &[&str]) -> ReviewView {
        let c = comment(id, author, at, false, reactors);
        ReviewView {
            id: c.id,
            author: c.author,
            verdict: "approve".into(),
            body: body.into(),
            commit: "c".into(),
            submitted_at: c.created_at,
            reply_only: false,
            reactions: c.reactions,
            url: c.url,
        }
    }

    fn thread(id: &str, resolved: bool, comments: Vec<CommentView>) -> ThreadView {
        ThreadView {
            id: id.into(),
            resolved,
            outdated: false,
            path: "f".into(),
            line: 1,
            side: "RIGHT".into(),
            start_line: None,
            start_side: None,
            commit: "c".into(),
            comments: comments
                .into_iter()
                .map(|comment| ThreadCommentView {
                    comment,
                    reply_to_id: None,
                })
                .collect(),
        }
    }

    fn ids(a: Activity) -> Vec<String> {
        unanswered_in("Aiko", &a)
            .into_iter()
            .map(|x| x.id)
            .collect()
    }

    fn activity(
        comments: Vec<CommentView>,
        reviews: Vec<ReviewView>,
        threads: Vec<ThreadView>,
    ) -> Activity {
        Activity {
            comments,
            reviews,
            threads,
        }
    }

    #[test]
    fn statements_wait_until_the_author_answers_each_its_own_way() {
        let (t1, t2) = ("2026-10-01T00:00:01.000Z", "2026-10-01T00:00:02.000Z");
        // Each kind of statement, unanswered.
        assert_eq!(
            ids(activity(
                vec![comment("c", "ken", t1, false, &[])],
                vec![],
                vec![]
            )),
            ["c"]
        );
        assert_eq!(
            ids(activity(
                vec![],
                vec![review("r", "ken", t1, "nit", &[])],
                vec![]
            )),
            ["r"],
            "an approval with a body is a statement"
        );
        assert_eq!(
            ids(activity(
                vec![],
                vec![],
                vec![thread(
                    "t",
                    false,
                    vec![comment("i", "ken", t1, false, &[])]
                )]
            )),
            ["i"],
            "a thread names its last comment"
        );
        // Not statements: a notice, a review without a body, a resolved
        // thread, and the author's own words (names fold).
        assert!(
            ids(activity(
                vec![
                    comment("c", "ken", t1, true, &[]),
                    comment("own", "aiko", t1, false, &[])
                ],
                vec![review("r", "ken", t1, " \n", &[])],
                vec![
                    thread("t", true, vec![comment("i", "ken", t1, false, &[])]),
                    thread("u", false, vec![comment("j", "AIKO", t1, false, &[])])
                ]
            ))
            .is_empty()
        );
        // A reaction answers each kind.
        assert!(
            ids(activity(
                vec![comment("c", "ken", t1, false, &["aiko"])],
                vec![review("r", "ken", t1, "nit", &["Aiko"])],
                vec![thread(
                    "t",
                    false,
                    vec![comment("i", "ken", t1, false, &["aiko"])]
                )]
            ))
            .is_empty()
        );
        // A thread is answered by a reply in it, not by a reaction to an
        // earlier comment or by speaking elsewhere.
        assert!(
            ids(activity(
                vec![],
                vec![],
                vec![thread(
                    "t",
                    false,
                    vec![
                        comment("i", "ken", t1, false, &[]),
                        comment("j", "aiko", t2, false, &[])
                    ]
                )]
            ))
            .is_empty()
        );
        assert_eq!(
            ids(activity(
                vec![comment("later", "aiko", t2, false, &[])],
                vec![],
                vec![thread(
                    "t",
                    false,
                    vec![
                        comment("i", "aiko", t1, false, &[]),
                        comment("j", "ken", t1, false, &["aiko"]),
                        comment("k", "ken", t1, false, &[])
                    ]
                )]
            )),
            ["k"]
        );
        // A comment or review is answered by anything the author says after
        // it: a comment, a review, or an inline comment; not by a notice,
        // and not by words before it.
        let statements = || {
            (
                vec![comment("c", "ken", t1, false, &[])],
                vec![review("r", "ken", t1, "nit", &[])],
            )
        };
        let (c, r) = statements();
        let mut answered = c.clone();
        answered.push(comment("a", "aiko", t2, false, &[]));
        assert!(ids(activity(answered, r.clone(), vec![])).is_empty());
        let mut reviewed = r.clone();
        reviewed.push(review("a", "aiko", t2, "", &[]));
        assert!(ids(activity(c.clone(), reviewed, vec![])).is_empty());
        assert!(
            ids(activity(
                c.clone(),
                r.clone(),
                vec![thread(
                    "t",
                    false,
                    vec![comment("a", "aiko", t2, false, &[])]
                )]
            ))
            .is_empty()
        );
        let mut noticed = c.clone();
        noticed.push(comment("n", "aiko", t2, true, &[]));
        assert_eq!(ids(activity(noticed, r.clone(), vec![])), ["c", "r"]);
        let mut earlier = c.clone();
        earlier.insert(
            0,
            comment("a", "aiko", "2026-10-01T00:00:00.000Z", false, &[]),
        );
        assert_eq!(ids(activity(earlier, r, vec![])), ["c", "r"]);
    }

    #[test]
    fn the_patrol_orders_by_what_is_left_to_wait_for() {
        use crate::landing::HeadChecks::*;
        let rows = [
            (false, false, Passed, 1),
            (true, true, Passed, 2),
            (true, true, Running, 2),
            (false, false, Running, 3),
            (true, true, Failed, 4),
            (true, false, Passed, 5),
            (true, false, Running, 5),
            (true, false, Failed, 6),
            (false, false, Failed, 7),
            (false, true, Passed, 8),
            (false, true, Running, 8),
            (false, true, Failed, 8),
        ];
        for (author, approved, checks, rank) in rows {
            assert_eq!(
                patrol_order(author, approved, checks),
                rank,
                "author {author}, approved {approved}, {checks:?}"
            );
        }
    }
}
