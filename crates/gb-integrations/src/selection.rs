//! Ticket selection from observed conversation; no provider writes here.
use chrono::{DateTime, Utc};
use gb_ports::{TaskMessage, WorkflowStatus};

pub use gb_ports::parse_timestamp;

pub fn since_assignment<'a>(
    comments: &'a [TaskMessage],
    assigned_at: &str,
) -> Vec<&'a TaskMessage> {
    let since = parse_timestamp(assigned_at);
    comments
        .iter()
        .filter(|m| {
            since.is_none() || parse_timestamp(&m.timestamp).is_none_or(|t| Some(t) >= since)
        })
        .collect()
}
pub fn mention_pending<'a>(
    described: bool,
    comments: impl IntoIterator<Item = (&'a TaskMessage, bool)>,
) -> bool {
    comments
        .into_iter()
        .fold(described, |pending, (m, mentioned)| {
            (pending || mentioned) && m.author_type != "Assistant"
        })
}
pub fn relevant_comments<'a>(
    comments: impl IntoIterator<Item = (&'a TaskMessage, bool)>,
) -> Vec<&'a TaskMessage> {
    comments
        .into_iter()
        .filter_map(|(m, elsewhere)| (!elsewhere || m.author_type == "Assistant").then_some(m))
        .collect()
}
pub fn linked_pull_state<'a>(
    pulls: impl IntoIterator<Item = (&'a str, &'a str)>,
) -> Option<&'a str> {
    let all: Vec<_> = pulls.into_iter().collect();
    let open = all.iter().any(|(s, _)| *s == "open");
    all.into_iter()
        .filter(|(s, _)| !open || *s == "open")
        .reduce(|first, next| if next.1 > first.1 { next } else { first })
        .map(|(s, _)| s)
}
#[derive(Debug, PartialEq, Eq)]
pub struct TicketDecision {
    pub reason: Option<&'static str>,
    pub finish: bool,
}
pub fn select_ticket(
    status: &str,
    since: &[TaskMessage],
    latest_status: Option<&WorkflowStatus>,
    pending: bool,
    linked_pull: Option<&str>,
    finish_merged: bool,
    now: DateTime<Utc>,
) -> TicketDecision {
    let mine = since.last().is_some_and(|m| m.author_type == "Assistant");
    let held = mine && latest_status.is_some_and(|s| s.holds_selection(now));
    let reason = if status == "ready" {
        if held || (mine && !pending) {
            None
        } else {
            Some("ready_lane")
        }
    } else if held {
        None
    } else if !since.is_empty() && !mine {
        Some("issue_comment")
    } else if pending {
        Some("issue_mention")
    } else if linked_pull.is_some() || !since.is_empty() {
        None
    } else {
        Some("working_lane")
    };
    TicketDecision {
        reason,
        finish: status != "ready"
            && !held
            && (since.is_empty() || mine)
            && !pending
            && linked_pull == Some("merged")
            && finish_merged,
    }
}
