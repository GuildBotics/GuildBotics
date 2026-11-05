//! Provider-independent ticket, PR and repository operations.
use crate::{MemberError, Result, WriteGuard};
use gb_ports::*;
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;

/// Lazy access preserves approval and chat validation before provider creation.
pub trait RepositoryServices {
    fn tickets(&self) -> Result<&dyn TicketService>;
    fn code(&self) -> Result<&dyn CodeHostingService>;
}
pub struct Repository<'a> {
    pub services: &'a dyn RepositoryServices,
    pub guard: &'a dyn WriteGuard,
    /// Already resolved public accounts, including human roster members.
    pub roster: &'a BTreeMap<String, ActorName>,
}

fn payload(value: impl Serialize) -> Result<Value> {
    serde_json::to_value(value).map_err(|_| ServiceError::InvalidResult.into())
}
fn cleaned(values: &mut Vec<String>) {
    *values = values
        .iter()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
        .collect();
}
pub fn require_human(
    approved: bool,
    action: &'static str,
    args: BTreeMap<String, String>,
) -> Result<()> {
    if approved {
        Ok(())
    } else {
        Err(MemberError::Approval { action, args })
    }
}
pub fn validate_location(request: &ReviewComment) -> Result<()> {
    let valid_side = |side: &str| matches!(side, "LEFT" | "RIGHT");
    let error = if request.path.trim().is_empty() {
        Some("cli.member.review_path")
    } else if request.line < 1 {
        Some("cli.member.review_line")
    } else if !valid_side(&request.side) {
        Some("cli.member.review_side")
    } else if request
        .start_side
        .as_deref()
        .is_some_and(|s| !valid_side(s))
    {
        Some("cli.member.review_start_side")
    } else if request.start_line.is_some() != request.start_side.is_some() {
        Some("cli.member.review_range")
    } else if request.start_line.is_some_and(|n| n < 1) {
        Some("cli.member.review_start_line")
    } else if request
        .start_side
        .as_deref()
        .is_some_and(|s| s != request.side)
    {
        Some("cli.member.review_range_side")
    } else if request.start_line.is_some_and(|n| n > request.line) {
        Some("cli.member.review_range_order")
    } else {
        None
    };
    if let Some(error) = error {
        return Err(MemberError::Capability(error));
    }
    Ok(())
}
impl Repository<'_> {
    /// Services return raw bytes: the CLI transport sends these to the guest,
    /// which owns the destination. No guest-controlled path is opened here.
    pub async fn artifact(&self, url: &str, name: &str) -> Result<Download<ArtifactRef>> {
        let name = name.trim();
        if name.is_empty() {
            return Err(MemberError::Capability("cli.member.artifact_name_required"));
        }
        Ok(self
            .services
            .code()?
            .artifact(url, name, 100 * 1024 * 1024)
            .await?)
    }
    pub async fn attachment(&self, url: &str, id: &str) -> Result<Download<Attachment>> {
        Ok(self
            .services
            .tickets()?
            .attachment(url, id, 10 * 1024 * 1024)
            .await?)
    }
    pub async fn read(
        &self,
        resource: Resource,
        subject: &str,
        parameters: Option<&Value>,
        continuation: &str,
    ) -> Result<Value> {
        read_conditions(resource, parameters, continuation)?;
        let page = if resource.is_ticket_service() {
            self.services
                .tickets()?
                .read(resource, subject, parameters, continuation)
                .await?
        } else {
            self.services
                .code()?
                .read(resource, subject, parameters, continuation)
                .await?
        };
        let mut result = payload(&page)?;
        if page.target().is_none() {
            result.as_object_mut().unwrap().remove("target");
        }
        if serde_json::to_vec(&result)
            .map_err(|_| ServiceError::InvalidResult)?
            .len()
            > MAX_PAGE_BYTES
        {
            return Err(ServiceError::ResponseTooLarge.into());
        }
        Ok(result)
    }
    pub async fn ticket_create(
        &self,
        mut request: CreateTicket,
        assignee: Option<&str>,
        approved: bool,
    ) -> Result<Value> {
        require_human(approved, "cli.member.ticket_create", BTreeMap::new())?;
        request.assignee = assignee
            .map(|id| {
                self.roster
                    .get(id)
                    .cloned()
                    .filter(|a| !a.0.is_empty())
                    .ok_or_else(|| MemberError::CapabilityArgs {
                        key: "cli.member.ticket_assignee",
                        args: [("person".into(), id.into())].into(),
                    })
            })
            .transpose()?;
        cleaned(&mut request.labels);
        cleaned(&mut request.blocked_by);
        self.guard.ensure_current()?;
        payload(self.services.tickets()?.create_ticket(request).await?)
    }
    pub async fn ticket_update(
        &self,
        url: &str,
        mut request: UpdateTicket,
        approved: bool,
    ) -> Result<Value> {
        if request.interactive.is_some() {
            require_human(approved, "cli.member.ticket_interactive", BTreeMap::new())?;
        }
        if let Some(state) = &request.state {
            require_human(
                approved,
                "cli.member.ticket_state",
                [("state".into(), state.clone())].into(),
            )?;
        }
        cleaned(&mut request.add_labels);
        cleaned(&mut request.remove_labels);
        cleaned(&mut request.add_blocked_by);
        cleaned(&mut request.remove_blocked_by);
        if request.body.is_none()
            && request.title.is_none()
            && request.state.is_none()
            && request.parent.is_none()
            && request.interactive.is_none()
            && request.add_labels.is_empty()
            && request.remove_labels.is_empty()
            && request.add_blocked_by.is_empty()
            && request.remove_blocked_by.is_empty()
        {
            return Err(MemberError::Capability("cli.member.ticket_update_empty"));
        }
        self.guard.ensure_current()?;
        payload(self.services.tickets()?.update_ticket(url, request).await?)
    }
    pub async fn ticket_comment(&self, url: &str, body: &str) -> Result<Value> {
        self.guard.ensure_current()?;
        payload(self.services.tickets()?.comment(url, body, None).await?)
    }
    pub async fn ticket_reaction(
        &self,
        url: &str,
        id: &str,
        reaction: SemanticReaction,
    ) -> Result<Value> {
        self.guard.ensure_current()?;
        payload(
            self.services
                .tickets()?
                .add_reaction(url, id, reaction)
                .await?,
        )
    }
    pub async fn pr_create(&self, request: CreatePullRequest) -> Result<Value> {
        self.guard.ensure_current()?;
        payload(self.services.code()?.create_pull_request(request).await?)
    }
    pub async fn pr_update(&self, url: &str, request: UpdatePullRequest) -> Result<Value> {
        if request.body.is_none() && request.title.is_none() && request.tickets.is_none() {
            return Err(MemberError::Capability("cli.member.pr_update_empty"));
        }
        self.guard.ensure_current()?;
        payload(
            self.services
                .code()?
                .update_pull_request(url, request)
                .await?,
        )
    }
    pub async fn pr_comment(&self, url: &str, body: &str) -> Result<Value> {
        self.guard.ensure_current()?;
        payload(self.services.code()?.comment(url, body, None).await?)
    }
    pub async fn pr_review(
        &self,
        url: &str,
        body: &str,
        verdict: ReviewVerdict,
        head: &str,
    ) -> Result<Value> {
        self.guard.ensure_current()?;
        payload(
            self.services
                .code()?
                .review(url, body, verdict, head)
                .await?,
        )
    }
    pub async fn pr_review_comment(&self, url: &str, request: ReviewComment) -> Result<Value> {
        validate_location(&request)?;
        self.guard.ensure_current()?;
        payload(self.services.code()?.review_comment(url, request).await?)
    }
    pub async fn pr_reply(&self, url: &str, reply_to: &str, body: &str) -> Result<Value> {
        self.guard.ensure_current()?;
        payload(self.services.code()?.reply(url, reply_to, body).await?)
    }
    pub async fn pr_reaction(
        &self,
        url: &str,
        id: &str,
        reaction: SemanticReaction,
    ) -> Result<Value> {
        self.guard.ensure_current()?;
        payload(
            self.services
                .code()?
                .add_reaction(url, id, reaction)
                .await?,
        )
    }
}

/// Pull requests mentioned by writes, pushes, and resumed-check evidence.
pub fn evidence_urls(evidence: &[Value]) -> Vec<String> {
    let mut urls = Vec::new();
    for record in evidence {
        let p = &record["payload"];
        if p["target"]["kind"] == "pull_request" {
            push_url(&mut urls, &p["target"]["url"]);
        }
        for item in p["pull_requests"].as_array().into_iter().flatten() {
            push_url(&mut urls, &item["url"]);
        }
    }
    urls
}
fn push_url(urls: &mut Vec<String>, value: &Value) {
    if let Some(url) = value.as_str().filter(|s| !s.is_empty())
        && !urls.iter().any(|u| u == url)
    {
        urls.push(url.into());
    }
}
pub async fn answered(
    code: &dyn CodeHostingService,
    ticket: Option<&str>,
    evidence: &[Value],
) -> Result<Vec<(Value, Readiness)>> {
    let mut urls = Vec::new();
    if let Some(ticket) = ticket {
        match code.read(Resource::PullRequests, ticket, None, "").await {
            Ok(page) => {
                let pr = page.items().first().ok_or(ServiceError::InvalidResult)?;
                push_url(&mut urls, &pr["url"]);
            }
            Err(e) if e.is_not_found() => {
                let mut continuation = String::new();
                loop {
                    let page = match code
                        .read(Resource::TicketPullRequests, ticket, None, &continuation)
                        .await
                    {
                        Ok(page) => page,
                        Err(e) if e.is_unsupported() => break,
                        Err(e) => return Err(e.into()),
                    };
                    for item in page.items() {
                        push_url(&mut urls, &item["url"]);
                    }
                    match page.continuation().filter(|s| !s.is_empty()) {
                        Some(next) => continuation = next.into(),
                        None => break,
                    }
                }
            }
            Err(e) => return Err(e.into()),
        }
    }
    for url in evidence_urls(evidence) {
        push_url(&mut urls, &url.into());
    }
    let mut results = Vec::new();
    for url in urls {
        let page = code.read(Resource::PullRequests, &url, None, "").await?;
        let pr = page.items().first().ok_or(ServiceError::InvalidResult)?;
        if pr["state"] != "open" || pr["author_type"] != "Assistant" {
            continue;
        }
        let checks = code.readiness(&url, false, MAX_LOG_TAIL_BYTES).await?;
        if checks.readiness != ReadinessReadiness::NotApplicable {
            results.push((pr.clone(), checks));
        }
    }
    Ok(results)
}
pub fn ensure_ready(results: &[(Value, Readiness)]) -> Result<()> {
    if results
        .iter()
        .any(|(_, r)| r.readiness != ReadinessReadiness::Ready)
    {
        Err(MemberError::CapabilityArgs {
            key: "cli.member.readiness_blocked",
            args: [("blockers".into(), blocker_summary(results))].into(),
        })
    } else {
        Ok(())
    }
}
fn blocker_summary(results: &[(Value, Readiness)]) -> String {
    results
        .iter()
        .filter(|(_, r)| r.readiness != ReadinessReadiness::Ready)
        .map(|(_, r)| {
            format!(
                "{}: {}",
                r.target.url,
                r.completion_blockers
                    .iter()
                    .map(|b| b.message.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })
        .collect::<Vec<_>>()
        .join("; ")
}
pub fn ensure_awaitable(results: &[(Value, Readiness)]) -> Result<Vec<&Readiness>> {
    let awaited: Vec<_> = results
        .iter()
        .filter(|(_, r)| r.readiness != ReadinessReadiness::Ready)
        .collect();
    if awaited.is_empty() {
        return Err(MemberError::Capability("cli.member.checks_none"));
    }
    if awaited.iter().any(|(pr, r)| {
        pr["draft"] == true
            || r.completion_blockers.is_empty()
            || r.completion_blockers
                .iter()
                .any(|b| b.code != "checks_pending")
    }) {
        return Err(MemberError::CapabilityArgs {
            key: "cli.member.checks_not_awaitable",
            args: [("blockers".into(), blocker_summary(results))].into(),
        });
    }
    Ok(awaited.into_iter().map(|(_, r)| r).collect())
}

pub async fn open_pull_request_readiness(
    code: &dyn CodeHostingService,
    repository: &str,
    branch: &str,
) -> Result<Vec<Value>> {
    let mut results = Vec::new();
    for url in code.open_pull_requests(repository, branch).await? {
        let checks = code.readiness(&url, false, MAX_LOG_TAIL_BYTES).await?;
        if checks.readiness != ReadinessReadiness::NotApplicable {
            results.push(serde_json::json!({"url":checks.target.url,"readiness":checks.readiness,"completion_blockers":checks.completion_blockers}));
        }
    }
    Ok(results)
}
/// A mention response with no PR evidence requires no code-host service.
pub async fn completion_readiness(
    services: &dyn RepositoryServices,
    ticket: Option<&str>,
    evidence: &[Value],
) -> Result<Vec<Readiness>> {
    if ticket.is_none() && evidence_urls(evidence).is_empty() {
        return Ok(vec![]);
    }
    let results = answered(services.code()?, ticket, evidence).await?;
    ensure_ready(&results)?;
    Ok(results.into_iter().map(|(_, r)| r).collect())
}
pub struct AwaitWork<'a> {
    pub person_id: &'a str,
    pub run_id: &'a str,
    pub work_kind: &'a str,
    pub work_identity: &'a str,
    pub subject: &'a str,
}
impl Repository<'_> {
    /// Return successful notices for the invocation owner to persist as run
    /// evidence. The status retains the original work for patrol resumption.
    pub async fn await_checks(
        &self,
        results: &[(Value, Readiness)],
        work: AwaitWork<'_>,
        render: impl Fn(&Readiness) -> String,
        mut record: impl FnMut(&Value) -> Result<()>,
    ) -> Result<Vec<Value>> {
        let awaited = ensure_awaitable(results)?;
        self.guard.ensure_current()?;
        let mut notices = Vec::new();
        for readiness in awaited {
            let status:WorkflowStatus=serde_json::from_value(serde_json::json!({"reason":"awaiting_checks","person_id":work.person_id,
                "run_id":work.run_id,"work_kind":work.work_kind,"work_identity":work.work_identity,"subject":work.subject,"head":readiness.head_sha}))
                .map_err(|_|ServiceError::InvalidParameters)?;
            let notice = self
                .services
                .code()?
                .comment(&readiness.target.url, &render(readiness), Some(&status))
                .await?;
            let notice = payload(notice)?;
            record(&notice)?;
            notices.push(notice);
        }
        Ok(notices)
    }
}
