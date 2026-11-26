//! Provider-independent operations. Providers own no host configuration types.
use super::*;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Task {
    /// Created by the code host after default-branch verification failed, or linked to that ticket. Performed before other work.
    pub repair: bool,
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
    /// Latest ordinary mention of a member outside the assignment.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mention_at: Option<DateTime<Utc>>,
    pub resumes: Option<AwaitedRun>,
    /// A pull request's automatic re-review limit, as its repository set it
    /// when the patrol read it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub review_rounds: Option<u32>,
}
impl Default for Task {
    fn default() -> Self {
        Self {
            repair: false,
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
            mention_at: None,
            resumes: None,
            review_rounds: None,
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
    pub interactive: bool,
    pub assignee: Option<ActorName>,
    pub lane: Option<CreateLane>,
    pub parent: Option<String>,
    pub blocked_by: Vec<String>,
}
#[derive(Clone, Debug)]
pub enum TicketPlacement {
    Before(String),
    After(String),
}
impl TicketPlacement {
    pub fn url(&self) -> &str {
        match self {
            Self::Before(url) | Self::After(url) => url,
        }
    }
}
#[derive(Clone, Debug, Default)]
pub struct UpdateTicket {
    pub assignee: Option<ActorName>,
    pub lane: Option<MoveLane>,
    /// Reorder within the current tracker and lane. Unsupported providers must reject
    /// the whole update before writing any other fields.
    pub placement: Option<TicketPlacement>,
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
