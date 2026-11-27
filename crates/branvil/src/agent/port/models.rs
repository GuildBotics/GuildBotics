//! What the agent's operations return besides Branvil's own views.
use serde::{Deserialize, Serialize};

/// The ticket or pull request an operation worked on, which the member's
/// trace and run evidence record. The member's output leaves it out.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct WorkTarget {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
    /// The write closed the ticket, rather than finding it closed.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub closed: bool,
}

/// A write's result: Branvil's view of what it wrote, and its work target.
#[derive(Clone, Debug, Serialize)]
pub struct Written<T> {
    #[serde(flatten)]
    pub view: T,
    pub target: WorkTarget,
}

/// A created pull request, or the open one that already had its branches.
#[derive(Clone, Debug, Serialize)]
pub struct PullRequestCreated {
    #[serde(flatten)]
    pub written: Written<crate::api::pulls::PullView>,
    pub created: bool,
}

/// The reaction a member added, and what it reacted to by the id its read gave it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ReactionAdded {
    pub reaction: gb_base::service::SemanticReaction,
    pub id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Attachment {
    pub id: String,
    pub name: String,
    pub media_type: String,
    pub size: i64,
    pub referenced_by: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FailedLog {
    pub job_id: String,
    pub name: String,
    pub conclusion: String,
    pub url: String,
    pub artifact_names: Vec<String>,
    pub log: String,
    pub log_bytes: i64,
    pub tail_limit_bytes: i64,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) evidence: Option<crate::ci::diagnostics::JobEvidence>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub(crate) evidence_truncated: bool,
}

/// One remark on a pull request, by its opaque id and when it was made.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Remark {
    pub id: String,
    pub at: String,
}

/// How many remarks of one kind were made, and the latest of them.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Remarks {
    pub count: i64,
    pub latest: Option<Remark>,
}

/// What people other than the pull request's author have said on it, leaving
/// out workflow status notices. A reader that compares two reads sees any
/// change as new feedback.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Feedback {
    pub reviews: Remarks,
    pub comments: Remarks,
    pub thread_comments: Remarks,
}

/// Branvil's checks of a pull request's head, with the feedback on it and,
/// when asked, the tails of its failed jobs' logs.
#[derive(Clone, Debug, Serialize)]
pub struct Readiness {
    #[serde(skip)]
    pub target: WorkTarget,
    #[serde(flatten)]
    pub report: crate::api::pulls::ChecksView,
    pub feedback: Feedback,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failed_logs: Option<Vec<FailedLog>>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ClosedItem {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
    pub closed_at: String,
    pub merged: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PullRequestHead {
    pub repository: String,
    pub branch: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ArtifactRef {
    pub name: String,
    pub id: String,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum WorkKind {
    #[default]
    #[serde(rename = "ticket")]
    Ticket,
    #[serde(rename = "pull_request")]
    PullRequest,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReviewVerdict {
    #[serde(rename = "approve")]
    Approve,
    #[serde(rename = "request_changes")]
    RequestChanges,
    #[serde(rename = "comment")]
    Comment,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RequiredChecks {
    Passed,
    Running,
    Failed,
}
