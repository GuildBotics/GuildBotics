use chrono::{DateTime, Utc};
use gb_ports::*;
use serde_json::Value;
use std::collections::BTreeMap;
pub struct Probe {
    pub error: Option<ServiceError>,
    pub ticket_requests: std::sync::Mutex<Vec<CreateTicket>>,
    pub closed: Vec<ClosedItem>,
    pub pages: BTreeMap<String, std::result::Result<ReadPage, ServiceError>>,
    pub checks: BTreeMap<String, Readiness>,
    pub notices: std::sync::Mutex<Vec<Value>>,
    pub open: Vec<String>,
}
impl Probe {
    pub fn new(error: Option<ServiceError>) -> Self {
        Self {
            error,
            ticket_requests: Default::default(),
            closed: vec![],
            pages: BTreeMap::new(),
            checks: BTreeMap::new(),
            notices: Default::default(),
            open: vec![],
        }
    }
}

impl TicketService for Probe {
    fn read<'a>(
        &'a self,
        _resource: Resource,
        _subject: &'a str,
        _parameters: Option<&'a Value>,
        _continuation: &'a str,
    ) -> ServiceFuture<'a, ReadPage> {
        Box::pin(async move {
            self.pages
                .get(&format!(
                    "{}|{}|{}",
                    _resource.name(),
                    _subject,
                    _continuation
                ))
                .cloned()
                .unwrap_or_else(|| Err(self.error.clone().expect("unexpected provider access")))
        })
    }
    fn attachment<'a>(
        &'a self,
        _url: &'a str,
        _id: &'a str,
        _limit: usize,
    ) -> ServiceFuture<'a, Download<Attachment>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn comment<'a>(
        &'a self,
        _url: &'a str,
        _body: &'a str,
        _status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn create_ticket(&self, request: CreateTicket) -> ServiceFuture<'_, TicketCreated> {
        Box::pin(async move {
            self.ticket_requests.lock().unwrap().push(request);
            Err(self.error.clone().expect("unexpected provider access"))
        })
    }
    fn update_ticket<'a>(
        &'a self,
        _url: &'a str,
        _request: UpdateTicket,
    ) -> ServiceFuture<'a, TicketUpdated> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn add_reaction<'a>(
        &'a self,
        _url: &'a str,
        _target_id: &'a str,
        _reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn refresh<'a>(&'a self, _task: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn can_receive_work<'a>(&'a self, _url: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn move_ticket<'a>(&'a self, _task: &'a Task, _lane: MoveLane) -> ServiceFuture<'a, bool> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn closed_since(
        &self,
        _start: DateTime<Utc>,
        _end: DateTime<Utc>,
    ) -> ServiceFuture<'_, Vec<ClosedItem>> {
        Box::pin(async {
            if let Some(e) = &self.error {
                Err(e.clone())
            } else {
                Ok(self.closed.clone())
            }
        })
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async { self.error.clone().map_or(Ok(()), Err) })
    }
}
impl CodeHostingService for Probe {
    fn read<'a>(
        &'a self,
        _resource: Resource,
        _subject: &'a str,
        _parameters: Option<&'a Value>,
        _continuation: &'a str,
    ) -> ServiceFuture<'a, ReadPage> {
        Box::pin(async move {
            self.pages
                .get(&format!(
                    "{}|{}|{}",
                    _resource.name(),
                    _subject,
                    _continuation
                ))
                .cloned()
                .unwrap_or_else(|| Err(self.error.clone().expect("unexpected provider access")))
        })
    }
    fn identity(&self, _check: bool) -> ServiceFuture<'_, BTreeMap<String, String>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn comment<'a>(
        &'a self,
        _url: &'a str,
        _body: &'a str,
        _status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        Box::pin(async move {
            if let Some(e) = &self.error {
                return Err(e.clone());
            }
            self.notices
                .lock()
                .unwrap()
                .push(_status.map(WorkflowStatus::payload).unwrap_or(Value::Null));
            serde_json::from_value(serde_json::json!({"id":"notice","url":null,"author":"local-aiko","created_at":"2026-01-01T00:00:00Z","target":self.checks.get(_url).expect("expected URL").target})).map_err(|_|ServiceError::InvalidResult)
        })
    }
    fn create_pull_request(
        &self,
        _request: CreatePullRequest,
    ) -> ServiceFuture<'_, PullRequestCreated> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn update_pull_request<'a>(
        &'a self,
        _url: &'a str,
        _request: UpdatePullRequest,
    ) -> ServiceFuture<'a, PullRequestUpdated> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn review<'a>(
        &'a self,
        _url: &'a str,
        _body: &'a str,
        _verdict: ReviewVerdict,
        _head: &'a str,
    ) -> ServiceFuture<'a, ReviewSubmitted> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn review_comment<'a>(
        &'a self,
        _url: &'a str,
        _request: ReviewComment,
    ) -> ServiceFuture<'a, ReviewCommentWritten> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn reply<'a>(
        &'a self,
        _url: &'a str,
        _reply_to: &'a str,
        _body: &'a str,
    ) -> ServiceFuture<'a, ReplyWritten> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn add_reaction<'a>(
        &'a self,
        _url: &'a str,
        _target_id: &'a str,
        _reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn readiness<'a>(
        &'a self,
        _url: &'a str,
        _failed_logs: bool,
        _log_tail_bytes: usize,
    ) -> ServiceFuture<'a, Readiness> {
        Box::pin(async move {
            self.checks
                .get(_url)
                .cloned()
                .ok_or_else(|| self.error.clone().expect("unexpected readiness access"))
        })
    }
    fn open_pull_requests<'a>(
        &'a self,
        _repository: &'a str,
        _branch: &'a str,
    ) -> ServiceFuture<'a, Vec<String>> {
        Box::pin(async {
            if let Some(e) = &self.error {
                Err(e.clone())
            } else {
                Ok(self.open.clone())
            }
        })
    }
    fn artifact<'a>(
        &'a self,
        _url: &'a str,
        _name: &'a str,
        _limit: usize,
    ) -> ServiceFuture<'a, Download<ArtifactRef>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn clone_url<'a>(&'a self, _repository: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn default_branch<'a>(&'a self, _repository: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn pull_request_head<'a>(&'a self, _url: &'a str) -> ServiceFuture<'a, PullRequestHead> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn git_credential(&self) -> ServiceFuture<'_, Option<GitCredential>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn check_push(&self, _repository: &str) -> Result<(), ServiceError> {
        panic!("unused synchronous operation")
    }
    fn commit_url(&self, _repository: &str, _sha: &str) -> String {
        panic!("unused synchronous operation")
    }
    fn repository_from_remote(&self, _remote_url: &str) -> Option<String> {
        panic!("unused synchronous operation")
    }
    fn pull_request_candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn refresh_pull_request<'a>(&'a self, _task: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn convert_pull_request_to_draft<'a>(&'a self, _url: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn can_receive_work<'a>(&'a self, _url: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(async { Err(self.error.clone().expect("unexpected provider access")) })
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async { self.error.clone().map_or(Ok(()), Err) })
    }
}

impl gb_member::repository::RepositoryServices for Probe {
    fn tickets(&self) -> gb_member::Result<&dyn TicketService> {
        Ok(self)
    }
    fn code(&self) -> gb_member::Result<&dyn CodeHostingService> {
        Ok(self)
    }
}
