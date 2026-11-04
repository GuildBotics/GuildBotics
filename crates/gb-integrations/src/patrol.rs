//! Pure PR patrol snapshots. Read operations never draft or announce a PR.
use chrono::{DateTime, Utc};
use gb_ports::*;
use std::collections::BTreeSet;

#[derive(Clone, Debug, Default)]
pub struct PatrolReview {
    pub author: String,
    pub body: String,
    pub submitted_at: String,
    pub commit_oid: String,
    pub reply_only: bool,
    pub reactors: BTreeSet<String>,
}
#[derive(Clone, Debug, Default)]
pub struct PatrolComment {
    pub author: String,
    pub created_at: String,
    pub reactors: BTreeSet<String>,
    pub status: Option<WorkflowStatus>,
}
#[derive(Clone, Debug, Default)]
pub struct PatrolThread {
    pub resolved: bool,
    pub participants: BTreeSet<String>,
    pub last_author: String,
    pub last_created_at: String,
    pub last_reactors: BTreeSet<String>,
}
#[derive(Clone, Debug, Default)]
pub struct PatrolPullRequest {
    pub id: String,
    pub label: String,
    pub url: String,
    pub title: String,
    pub body: String,
    pub open: bool,
    pub is_draft: bool,
    pub created_at: String,
    pub author: String,
    pub head_oid: String,
    pub ready_at: String,
    pub requested_reviewers: BTreeSet<String>,
    pub reviews: Vec<PatrolReview>,
    pub comments: Vec<PatrolComment>,
    pub threads: Vec<PatrolThread>,
}
fn last_spoken<'a>(pr: &'a PatrolPullRequest, me: &str) -> &'a str {
    pr.comments
        .iter()
        .filter(|c| c.author == me && c.status.is_none())
        .map(|c| c.created_at.as_str())
        .chain(
            pr.reviews
                .iter()
                .filter(|r| r.author == me)
                .map(|r| r.submitted_at.as_str()),
        )
        .max()
        .unwrap_or("")
}
fn thread_unanswered(pr: &PatrolPullRequest, me: &str, mine_only: bool) -> bool {
    pr.threads.iter().any(|t| {
        !t.resolved
            && t.last_author != me
            && !t.last_reactors.contains(me)
            && (!mine_only || t.participants.contains(me))
    })
}
fn statement_unanswered(pr: &PatrolPullRequest, me: &str) -> bool {
    let spoken = last_spoken(pr, me);
    pr.comments.iter().any(|c| {
        c.author != me
            && c.status.is_none()
            && c.created_at.as_str() > spoken
            && !c.reactors.contains(me)
    }) || pr.reviews.iter().any(|r| {
        r.author != me
            && !r.body.trim().is_empty()
            && r.submitted_at.as_str() > spoken
            && !r.reactors.contains(me)
    })
}
fn reviewed_heads<'a>(pr: &'a PatrolPullRequest, me: &str, since: &str) -> BTreeSet<&'a str> {
    pr.reviews
        .iter()
        .filter(|r| {
            r.author == me
                && !r.reply_only
                && !r.commit_oid.is_empty()
                && r.submitted_at.as_str() > since
        })
        .map(|r| r.commit_oid.as_str())
        .collect()
}
pub fn review_rounds<'a>(pr: &'a PatrolPullRequest, me: &str) -> BTreeSet<&'a str> {
    let restart = pr
        .comments
        .iter()
        .filter(|c| {
            c.status
                .as_ref()
                .is_some_and(|s| s.reason() == WorkflowStatusReason::ReviewLimit)
        })
        .map(|c| c.created_at.as_str())
        .chain([pr.ready_at.as_str()])
        .max()
        .unwrap_or("");
    reviewed_heads(pr, me, restart)
}
pub fn pull_request_work(
    pr: &PatrolPullRequest,
    me: &str,
    now: DateTime<Utc>,
) -> Option<&'static str> {
    if !pr.open || pr.is_draft {
        return None;
    }
    if let Some(latest) = pr.comments.iter().rev().find(|c| c.author == me)
        && latest
            .status
            .as_ref()
            .is_some_and(|s| s.holds_selection(now))
    {
        let lifted = pr
            .comments
            .iter()
            .filter(|c| c.author != me && c.status.is_none())
            .map(|c| c.created_at.as_str())
            .chain(
                pr.reviews
                    .iter()
                    .filter(|r| r.author != me)
                    .map(|r| r.submitted_at.as_str()),
            )
            .chain(
                pr.threads
                    .iter()
                    .filter(|t| t.last_author != me)
                    .map(|t| t.last_created_at.as_str()),
            )
            .chain([pr.ready_at.as_str()])
            .max()
            .unwrap_or("");
        if lifted <= latest.created_at.as_str() {
            return None;
        }
    }
    if pr.author == me {
        return (thread_unanswered(pr, me, false) || statement_unanswered(pr, me))
            .then_some("pull_request_feedback");
    }
    if pr.requested_reviewers.contains(me) {
        return Some("pull_request_review");
    }
    let reviewed = reviewed_heads(pr, me, "");
    if !((!reviewed.is_empty() && !reviewed.contains(pr.head_oid.as_str()))
        || thread_unanswered(pr, me, true)
        || (!last_spoken(pr, me).is_empty() && statement_unanswered(pr, me)))
    {
        return None;
    }
    Some(
        if !reviewed.contains(pr.head_oid.as_str()) && review_rounds(pr, me).len() >= 3 {
            "pull_request_review_limit"
        } else {
            "pull_request_review"
        },
    )
}
pub async fn patrol(
    code: &dyn CodeHostingService,
    pr: &PatrolPullRequest,
    me: &str,
    provider: &str,
    person_id: &str,
    now: DateTime<Utc>,
) -> Option<Task> {
    let awaited = pr
        .comments
        .iter()
        .filter(|c| c.author == me)
        .filter_map(|c| c.status.as_ref())
        .rfind(|s| s.reason() != WorkflowStatusReason::RateLimited || s.holds_selection(now))
        .filter(|s| {
            s.reason() == WorkflowStatusReason::AwaitingChecks
                && pr.author == me
                && pr.open
                && !pr.is_draft
                && !pr.head_oid.is_empty()
        });
    let mut work = pull_request_work(pr, me, now);
    if work.is_none() {
        let status = awaited?;
        let p = status.payload();
        if p["head"].as_str() == Some(pr.head_oid.as_str()) {
            let readiness = match code.readiness(&pr.url, false, MAX_LOG_TAIL_BYTES).await {
                Ok(readiness) => readiness,
                Err(_) => {
                    tracing::warn!(provider, key = "integrations.readiness.unavailable");
                    return None;
                }
            };
            if readiness
                .completion_blockers
                .iter()
                .any(|b| b.code == "checks_pending")
            {
                return None;
            }
        }
        work = Some("pull_request_checks");
    }
    let resumes = awaited.map(|s| {
        let p = s.payload();
        AwaitedRun {
            run_id: p["run_id"].as_str().unwrap_or("").into(),
            work_kind: p["work_kind"].as_str().unwrap_or("").into(),
            work_identity: p["work_identity"].as_str().unwrap_or("").into(),
        }
    });
    Some(Task {
        id: Some(pr.id.clone()),
        url: Some(pr.url.clone()),
        title: pr.title.clone(),
        provider: provider.into(),
        label: pr.label.clone(),
        description: pr.body.clone(),
        status: "in_progress".into(),
        assignee: Some(person_id.into()),
        pull_request_url: Some(pr.url.clone()),
        trigger_reason: work.map(str::to_owned),
        created_at: crate::parse_timestamp(&pr.created_at),
        resumes,
        ..Default::default()
    })
}
