//! Inject failures at the ticket port while retaining the real local board.
use gb_ports::*;
use serde_json::Value;
use std::sync::{Arc, Mutex};
pub struct Faults {
    pub inner: Arc<dyn TicketService>,
    pub errors: Mutex<std::collections::BTreeMap<&'static str, ServiceError>>,
    pub omit_comment_url: bool,
}
impl Faults {
    pub fn new(inner: Arc<dyn TicketService>) -> Self {
        Self {
            inner,
            errors: Default::default(),
            omit_comment_url: false,
        }
    }
    fn check(&self, operation: &str) -> Result<(), ServiceError> {
        self.errors
            .lock()
            .unwrap()
            .get(operation)
            .cloned()
            .map_or(Ok(()), Err)
    }
}
impl TicketService for Faults {
    fn read<'a>(
        &'a self,
        resource: Resource,
        subject: &'a str,
        parameters: Option<&'a Value>,
        continuation: &'a str,
    ) -> ServiceFuture<'a, ReadPage> {
        self.inner.read(resource, subject, parameters, continuation)
    }
    fn attachment<'a>(
        &'a self,
        url: &'a str,
        id: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<Attachment>> {
        self.inner.attachment(url, id, limit)
    }
    fn comment<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        Box::pin(async move {
            self.check("comment")?;
            let mut written = self.inner.comment(url, body, status).await?;
            if self.omit_comment_url {
                written.url = None;
            }
            Ok(written)
        })
    }
    fn create_ticket(&self, request: CreateTicket) -> ServiceFuture<'_, TicketCreated> {
        self.inner.create_ticket(request)
    }
    fn update_ticket<'a>(
        &'a self,
        url: &'a str,
        request: UpdateTicket,
    ) -> ServiceFuture<'a, TicketUpdated> {
        self.inner.update_ticket(url, request)
    }
    fn add_reaction<'a>(
        &'a self,
        url: &'a str,
        target_id: &'a str,
        reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded> {
        self.inner.add_reaction(url, target_id, reaction)
    }
    fn candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        Box::pin(async {
            self.check("candidates")?;
            self.inner.candidates().await
        })
    }
    fn refresh<'a>(&'a self, task: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        Box::pin(async move {
            self.check("refresh")?;
            self.inner.refresh(task).await
        })
    }
    fn can_receive_work<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool> {
        self.inner.can_receive_work(url)
    }
    fn move_ticket<'a>(&'a self, task: &'a Task, lane: MoveLane) -> ServiceFuture<'a, bool> {
        Box::pin(async move {
            self.check("move")?;
            self.inner.move_ticket(task, lane).await
        })
    }
    fn closed_since(
        &self,
        start: chrono::DateTime<chrono::Utc>,
        end: chrono::DateTime<chrono::Utc>,
    ) -> ServiceFuture<'_, Vec<ClosedItem>> {
        self.inner.closed_since(start, end)
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        self.inner.aclose()
    }
}
