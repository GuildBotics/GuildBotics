//! Provider-independent result shapes, matching the frozen Python host.
use crate::ActorName;
use crate::SemanticReaction;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkTarget {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicketRelative {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
    pub state: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicketSummary {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
    pub key: String,
    pub state: String,
    #[serde(deserialize_with = "required_nullable")]
    pub lane: Option<Lane>,
    pub labels: Vec<String>,
    pub updated_at: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ticket {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
    pub key: String,
    pub tracker: String,
    #[serde(deserialize_with = "required_nullable")]
    pub repository: Option<String>,
    pub body: String,
    pub state: String,
    pub assignees: Vec<ActorName>,
    pub labels: Vec<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub lane: Option<Lane>,
    #[serde(deserialize_with = "required_nullable")]
    pub parent: Option<TicketRelative>,
    pub children: Vec<TicketRelative>,
    pub blocked_by: Vec<TicketRelative>,
    pub blocks: Vec<TicketRelative>,
    pub interactive: bool,
    #[serde(deserialize_with = "required_nullable")]
    pub interactive_ancestor: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PullRequest {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
    pub repository: String,
    pub body: String,
    pub state: String,
    pub assignees: Vec<ActorName>,
    pub author: ActorName,
    pub requested_reviewers: Vec<ActorName>,
    pub labels: Vec<String>,
    pub author_type: String,
    pub head: String,
    pub head_repository: String,
    pub head_sha: String,
    pub base: String,
    pub merged: bool,
    pub draft: bool,
    #[serde(deserialize_with = "required_nullable")]
    pub changed_files: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Comment {
    pub id: String,
    pub body: String,
    pub author: ActorName,
    pub author_type: String,
    #[serde(deserialize_with = "required_nullable")]
    pub created_at: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub url: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadComment {
    pub id: String,
    pub body: String,
    pub author: ActorName,
    pub author_type: String,
    #[serde(deserialize_with = "required_nullable")]
    pub created_at: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub url: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub reply_to_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Attachment {
    pub id: String,
    pub name: String,
    pub media_type: String,
    pub size: i64,
    pub referenced_by: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicketPullRequest {
    pub url: String,
    pub label: String,
    pub title: String,
    pub state: String,
    pub merged: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Review {
    pub id: String,
    pub body: String,
    pub author: ActorName,
    pub author_type: String,
    pub verdict: ReviewState,
    #[serde(deserialize_with = "required_nullable")]
    pub commit: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub submitted_at: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub url: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommentableLine {
    pub line: i64,
    pub side: CommentableLineSide,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub left_line: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub right_line: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileChange {
    pub path: String,
    pub status: String,
    pub additions: i64,
    pub deletions: i64,
    pub changes: i64,
    #[serde(deserialize_with = "required_nullable")]
    pub patch: Option<String>,
    pub patch_available: bool,
    pub patch_complete: bool,
    pub commentable_lines: Vec<CommentableLine>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewThread {
    pub id: String,
    #[serde(deserialize_with = "required_nullable")]
    pub resolved: Option<bool>,
    #[serde(deserialize_with = "required_nullable")]
    pub outdated: Option<bool>,
    pub comments: Vec<ThreadComment>,
    pub comments_complete: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Check {
    pub name: String,
    pub status: String,
    #[serde(deserialize_with = "required_nullable")]
    pub conclusion: Option<String>,
    pub details_url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub details: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompletionBlocker {
    pub code: String,
    pub message: String,
    pub next_action: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
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
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Readiness {
    pub target: WorkTarget,
    #[serde(deserialize_with = "required_nullable")]
    pub base_sha: Option<String>,
    pub head_sha: String,
    #[serde(deserialize_with = "required_nullable")]
    pub behind_by: Option<i64>,
    #[serde(deserialize_with = "required_nullable")]
    pub out_of_date: Option<bool>,
    #[serde(deserialize_with = "required_nullable")]
    pub current_base_sha: Option<String>,
    pub current_head_sha: String,
    pub rollup: ReadinessRollup,
    pub checks: Vec<Check>,
    pub checks_expected: bool,
    pub readiness: ReadinessReadiness,
    pub completion_blockers: Vec<CompletionBlocker>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failed_logs: Option<Vec<FailedLog>>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdvisoryIdentifier {
    #[serde(rename = "type")]
    pub kind: String,
    pub value: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyAlert {
    pub id: String,
    pub state: DependencyAlertState,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub package: Option<String>,
    #[serde(default)]
    pub ecosystem: Option<String>,
    #[serde(default)]
    pub manifest_path: Option<String>,
    #[serde(default)]
    pub severity: Option<String>,
    #[serde(default)]
    pub identifiers: Vec<AdvisoryIdentifier>,
    #[serde(default)]
    pub summary: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub affected_versions: Option<String>,
    #[serde(default)]
    pub patched_version: Option<String>,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommentWritten {
    pub id: String,
    #[serde(deserialize_with = "required_nullable")]
    pub url: Option<String>,
    pub author: String,
    #[serde(deserialize_with = "required_nullable")]
    pub created_at: Option<String>,
    pub target: WorkTarget,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReactionAdded {
    pub reaction: SemanticReaction,
    pub id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicketCreated {
    pub labels: Vec<String>,
    pub target: WorkTarget,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicketUpdated {
    pub body: String,
    pub state: String,
    pub state_changed: bool,
    pub labels: Vec<String>,
    pub target: WorkTarget,
    pub interactive: bool,
    #[serde(deserialize_with = "required_nullable")]
    pub interactive_ancestor: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClosedItem {
    pub kind: WorkKind,
    pub url: String,
    pub title: String,
    pub label: String,
    pub closed_at: String,
    pub merged: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PullRequestHead {
    pub repository: String,
    pub branch: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PullRequestCreated {
    pub created: bool,
    pub draft: bool,
    pub head: String,
    pub base: String,
    pub target: WorkTarget,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PullRequestUpdated {
    pub body: String,
    pub target: WorkTarget,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewSubmitted {
    pub verdict: ReviewVerdict,
    pub commit: String,
    #[serde(deserialize_with = "required_nullable")]
    pub url: Option<String>,
    pub target: WorkTarget,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewCommentWritten {
    pub id: String,
    #[serde(deserialize_with = "required_nullable")]
    pub url: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub created_at: Option<String>,
    pub path: String,
    pub line: i64,
    pub side: String,
    pub target: WorkTarget,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplyWritten {
    pub id: String,
    #[serde(deserialize_with = "required_nullable")]
    pub url: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub created_at: Option<String>,
    pub target: WorkTarget,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArtifactRef {
    pub name: String,
    pub id: String,
}

fn required_nullable<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::deserialize(deserializer)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum WorkKind {
    #[serde(rename = "ticket")]
    Ticket,
    #[serde(rename = "pull_request")]
    PullRequest,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Lane {
    #[serde(rename = "new")]
    New,
    #[serde(rename = "ready")]
    Ready,
    #[serde(rename = "in_progress")]
    InProgress,
    #[serde(rename = "done")]
    Done,
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
pub enum ReviewState {
    Approve,
    RequestChanges,
    Comment,
    Dismissed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum CommentableLineSide {
    #[serde(rename = "LEFT")]
    Left,
    #[serde(rename = "RIGHT")]
    Right,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReadinessRollup {
    #[serde(rename = "success")]
    Success,
    #[serde(rename = "failure")]
    Failure,
    #[serde(rename = "pending")]
    Pending,
    #[serde(rename = "no_checks")]
    NoChecks,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReadinessReadiness {
    #[serde(rename = "ready")]
    Ready,
    #[serde(rename = "blocked")]
    Blocked,
    #[serde(rename = "not_applicable")]
    NotApplicable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum DependencyAlertState {
    #[serde(rename = "open")]
    Open,
    #[serde(rename = "resolved")]
    Resolved,
    #[serde(rename = "dismissed")]
    Dismissed,
}
