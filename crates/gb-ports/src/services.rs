//! Provider-independent operations. Providers own no host configuration types.
use crate::*;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Task {
    pub id: Option<String>,
    pub url: Option<String>,
    pub title: String,
    pub provider: String,
    pub label: String,
    pub description: String,
    pub comments: Vec<TaskMessage>,
    pub status: String,
    pub assignee: Option<String>,
    pub pull_request_url: Option<String>,
    pub trigger_reason: Option<String>,
    pub created_at: Option<DateTime<Utc>>,
    pub resumes: Option<AwaitedRun>,
}
impl Default for Task {
    fn default() -> Self {
        Self {
            id: None,
            url: None,
            title: String::new(),
            provider: String::new(),
            label: String::new(),
            description: String::new(),
            comments: vec![],
            status: "new".into(),
            assignee: None,
            pull_request_url: None,
            trigger_reason: None,
            created_at: None,
            resumes: None,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct TaskMessage {
    pub content: String,
    pub author: String,
    pub author_type: String,
    pub timestamp: String,
    pub reactions: Vec<Value>,
    pub file_info: Vec<Value>,
}
impl Default for TaskMessage {
    fn default() -> Self {
        Self {
            content: String::new(),
            author: String::new(),
            author_type: "User".into(),
            timestamp: String::new(),
            reactions: vec![],
            file_info: vec![],
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct AwaitedRun {
    pub run_id: String,
    pub work_kind: String,
    pub work_identity: String,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CreateLane {
    New,
    Ready,
    InProgress,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum MoveLane {
    Ready,
    InProgress,
    Done,
}
pub fn creation_lane(
    lane: Option<CreateLane>,
    parent: Option<&str>,
    blocked_by: &[String],
) -> CreateLane {
    lane.unwrap_or(
        if parent.is_some_and(|p| !p.is_empty()) || !blocked_by.is_empty() {
            CreateLane::Ready
        } else {
            CreateLane::New
        },
    )
}
#[derive(Clone, Debug)]
pub struct Download<T> {
    pub metadata: T,
    pub content: Vec<u8>,
}
/// Credentials are deliberately excluded from Debug and serialization.
#[derive(Clone)]
pub struct GitCredential {
    pub username: String,
    pub password: String,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct TicketLink {
    pub url: String,
    pub closes: bool,
}
#[derive(Clone, Debug)]
pub struct CreateTicket {
    pub tracker: String,
    pub title: String,
    pub body: String,
    pub labels: Vec<String>,
    pub add_to_board: bool,
    pub interactive: bool,
    pub assignee: Option<ActorName>,
    pub lane: Option<CreateLane>,
    pub parent: Option<String>,
    pub blocked_by: Vec<String>,
}
#[derive(Clone, Debug, Default)]
pub struct UpdateTicket {
    pub body: Option<String>,
    pub title: Option<String>,
    pub add_labels: Vec<String>,
    pub remove_labels: Vec<String>,
    pub state: Option<String>,
    pub state_reason: Option<String>,
    pub parent: Option<String>,
    pub add_blocked_by: Vec<String>,
    pub remove_blocked_by: Vec<String>,
    pub interactive: Option<bool>,
}
#[derive(Clone, Debug)]
pub struct CreatePullRequest {
    pub repository: String,
    pub head: String,
    pub base: String,
    pub title: String,
    pub body: String,
    pub draft: bool,
    pub tickets: Vec<TicketLink>,
}
#[derive(Clone, Debug, Default)]
pub struct UpdatePullRequest {
    pub body: Option<String>,
    pub title: Option<String>,
    pub tickets: Option<Vec<TicketLink>>,
}
#[derive(Clone, Debug)]
pub struct ReviewComment {
    pub body: String,
    pub head: String,
    pub path: String,
    pub line: i64,
    pub side: String,
    pub start_line: Option<i64>,
    pub start_side: Option<String>,
}

pub trait TicketService: Send + Sync {
    fn read<'a>(
        &'a self,
        resource: Resource,
        subject: &'a str,
        parameters: Option<&'a Value>,
        continuation: &'a str,
    ) -> ServiceFuture<'a, ReadPage>;
    fn attachment<'a>(
        &'a self,
        url: &'a str,
        id: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<Attachment>>;
    fn comment<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten>;
    fn create_ticket(&self, request: CreateTicket) -> ServiceFuture<'_, TicketCreated>;
    fn update_ticket<'a>(
        &'a self,
        url: &'a str,
        request: UpdateTicket,
    ) -> ServiceFuture<'a, TicketUpdated>;
    fn add_reaction<'a>(
        &'a self,
        url: &'a str,
        target_id: &'a str,
        reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded>;
    fn candidates(&self) -> ServiceFuture<'_, Vec<Task>>;
    fn refresh<'a>(&'a self, task: &'a Task) -> ServiceFuture<'a, Option<Task>>;
    fn can_receive_work<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool>;
    fn move_ticket<'a>(&'a self, task: &'a Task, lane: MoveLane) -> ServiceFuture<'a, bool>;
    fn closed_since(
        &self,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> ServiceFuture<'_, Vec<ClosedItem>>;
    fn aclose(&self) -> ServiceFuture<'_, ()>;
}
pub trait CodeHostingService: Send + Sync {
    /// Canonical ticket links, including reference-only links.
    fn linked_tickets<'a>(&'a self, _url: &'a str) -> ServiceFuture<'a, Vec<TicketLink>> {
        Box::pin(async { Err(ServiceError::Unsupported) })
    }

    fn read<'a>(
        &'a self,
        resource: Resource,
        subject: &'a str,
        parameters: Option<&'a Value>,
        continuation: &'a str,
    ) -> ServiceFuture<'a, ReadPage>;
    fn identity(&self, check: bool) -> ServiceFuture<'_, BTreeMap<String, String>>;
    fn comment<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten>;
    fn create_pull_request(
        &self,
        request: CreatePullRequest,
    ) -> ServiceFuture<'_, PullRequestCreated>;
    fn update_pull_request<'a>(
        &'a self,
        url: &'a str,
        request: UpdatePullRequest,
    ) -> ServiceFuture<'a, PullRequestUpdated>;
    fn review<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        verdict: ReviewVerdict,
        head: &'a str,
    ) -> ServiceFuture<'a, ReviewSubmitted>;
    fn review_comment<'a>(
        &'a self,
        url: &'a str,
        request: ReviewComment,
    ) -> ServiceFuture<'a, ReviewCommentWritten>;
    fn reply<'a>(
        &'a self,
        url: &'a str,
        reply_to: &'a str,
        body: &'a str,
    ) -> ServiceFuture<'a, ReplyWritten>;
    fn add_reaction<'a>(
        &'a self,
        url: &'a str,
        target_id: &'a str,
        reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded>;
    fn readiness<'a>(
        &'a self,
        url: &'a str,
        failed_logs: bool,
        log_tail_bytes: usize,
    ) -> ServiceFuture<'a, Readiness>;
    fn open_pull_requests<'a>(
        &'a self,
        repository: &'a str,
        branch: &'a str,
    ) -> ServiceFuture<'a, Vec<String>>;
    fn artifact<'a>(
        &'a self,
        url: &'a str,
        name: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<ArtifactRef>>;
    fn clone_url<'a>(&'a self, repository: &'a str) -> ServiceFuture<'a, String>;
    fn default_branch<'a>(&'a self, repository: &'a str) -> ServiceFuture<'a, String>;
    fn pull_request_head<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, PullRequestHead>;
    fn git_credential(&self) -> ServiceFuture<'_, Option<GitCredential>>;
    fn check_push(&self, repository: &str) -> Result<(), ServiceError>;
    fn commit_url(&self, repository: &str, sha: &str) -> String;
    fn repository_from_remote(&self, remote_url: &str) -> Option<String>;
    fn pull_request_candidates(&self) -> ServiceFuture<'_, Vec<Task>>;
    fn refresh_pull_request<'a>(&'a self, task: &'a Task) -> ServiceFuture<'a, Option<Task>>;
    fn convert_pull_request_to_draft<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool>;
    fn can_receive_work<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool>;
    fn aclose(&self) -> ServiceFuture<'_, ()>;
}
