//! Ticket selection and settlement, shared by patrol and one-off commands.
use crate::{dispatcher::DispatchFailure, service::Pending, work_stops::WorkStop};
use gb_observability::{Diagnostics, Trace, runs::RunRecord};
use gb_ports::{CodeHostingService, MoveLane, ServiceError, Task, TicketService, WorkflowStatus};
use gb_wire::{CommandReply, WorkflowInvocation};
use serde_json::{Value, json};

pub const COMMAND: &str = "workflows/ticket_driven_workflow";
pub const REVIEW_LIMIT: &str = "pull_request_review_limit";

struct ReviewSettlement<'a> {
    trace: Trace,
    task: &'a Task,
    runner: &'a dyn Runner,
    draft: Option<Value>,
    ended: bool,
}
impl ReviewSettlement<'_> {
    fn payload(&self, error: &str) -> serde_json::Map<String, Value> {
        serde_json::from_value(json!({"run_id":self.trace.id(), "pull_request":{"url":self.task.pull_request_url,"label":self.task.label,"title":self.task.title},"count":3,"error":error})).unwrap()
    }
    fn finish(&mut self, event: &str) {
        self.ended = true;
        self.trace.event(
            event,
            self.payload(if event == "review_limit.failed" {
                "ServiceError"
            } else {
                ""
            }),
        );
    }
    fn drafted(&mut self, comment_url: &str, error: &str) {
        if let Some(mut fact) = self.draft.take() {
            fact["comment_url"] = json!(comment_url);
            fact["comment_error"] = json!(error);
            self.runner
                .activity(&self.trace, "pull_request.review_limit", fact.clone());
            self.trace.event(
                "pull_request.review_limit",
                serde_json::from_value(fact).expect("review limit fact"),
            );
        }
    }
}
impl Drop for ReviewSettlement<'_> {
    fn drop(&mut self) {
        if !self.ended {
            let error = if std::thread::panicking() {
                "panic"
            } else {
                "CancelledError"
            };
            self.drafted("", error);
            self.trace.event("review_limit.failed", self.payload(error));
        }
    }
}

/// Commands run under the caller's existing trace and execution lease.
pub trait Runner: Send + Sync {
    fn run<'a>(
        &'a self,
        invocation: WorkflowInvocation,
        trace: &'a Trace,
    ) -> Pending<'a, Result<CommandReply, DispatchFailure>>;
    fn say<'a>(
        &'a self,
        text: &'a str,
        invocation: &'a WorkflowInvocation,
        trace: &'a Trace,
    ) -> Pending<'a, Option<String>>;
    fn evidence(&self, run_id: &str, kind: &str, payload: Value) -> anyhow::Result<()>;
    fn record(&self, run_id: &str) -> anyhow::Result<Option<RunRecord>>;
    fn activity(&self, trace: &Trace, kind: &str, payload: Value);
    fn notify<'a>(&'a self, stop: WorkStop, trace: &'a Trace) -> Pending<'a, ()>;
    fn completed<'a>(
        &'a self,
        task: &'a Task,
        run_id: &'a str,
        trace: &'a Trace,
    ) -> Pending<'a, ()>;
}

pub struct Selector<'a> {
    pub diagnostics: &'a Diagnostics,
    pub person: &'a str,
    pub source: &'a str,
    pub language: &'a str,
    pub ticket: &'a dyn TicketService,
    pub code: Option<&'a dyn CodeHostingService>,
    pub runner: &'a dyn Runner,
}

impl Selector<'_> {
    /// Selection is read-only until a refreshed task is handed to the caller's
    /// execution boundary. An idle patrol opens neither a trace nor a run.
    pub async fn next(&self) -> Result<Option<(Task, WorkflowInvocation)>, ServiceError> {
        for candidate in self.candidates().await? {
            if let Some(invocation) = self.refresh(&candidate).await? {
                let task = serde_json::from_value(invocation.payload["task"].clone())
                    .map_err(|_| ServiceError::InvalidResult)?;
                return Ok(Some((task, invocation)));
            }
        }
        Ok(None)
    }
    pub async fn candidates(&self) -> Result<Vec<Task>, ServiceError> {
        let mut candidates = Vec::new();
        if let Some(code) = self.code {
            for task in code.pull_request_candidates().await? {
                if task.trigger_reason.as_deref() == Some(REVIEW_LIMIT) {
                    self.hand_over(&task).await;
                } else {
                    candidates.push(task);
                }
            }
        }
        candidates.extend(self.ticket.candidates().await?);
        Ok(candidates)
    }

    pub async fn refresh(&self, task: &Task) -> Result<Option<WorkflowInvocation>, ServiceError> {
        let task = if task.pull_request_url.is_some() {
            self.code
                .ok_or(ServiceError::Unsupported)?
                .refresh_pull_request(task)
                .await?
        } else {
            self.ticket.refresh(task).await?
        };
        let Some(task) = task else { return Ok(None) };
        if task.trigger_reason.as_deref() == Some(REVIEW_LIMIT) {
            self.hand_over(&task).await;
            return Ok(None);
        }
        let mut invocation = self.invocation(&task)?;
        if task.resumes.is_none() && task.trigger_reason.as_deref() == Some("pull_request_feedback")
        {
            let code = self.code.ok_or(ServiceError::Unsupported)?;
            let url = task
                .pull_request_url
                .as_deref()
                .ok_or(ServiceError::InvalidResult)?;
            if let Some(ticket) = gb_member::repository::author_ticket(code, self.ticket, url)
                .await
                .unwrap_or(None)
            {
                invocation.work = Some(
                    serde_json::from_value(json!({"kind":"ticket", "identity":ticket}))
                        .map_err(|_| ServiceError::InvalidResult)?,
                );
            }
        }
        Ok(Some(invocation))
    }

    pub fn invocation(&self, task: &Task) -> Result<WorkflowInvocation, ServiceError> {
        let url = task.url.as_deref().ok_or(ServiceError::InvalidResult)?;
        let (kind, identity, awaited_id, summary) = if let Some(resumes) = &task.resumes {
            let summary = self
                .runner
                .record(&resumes.run_id)
                .ok()
                .flatten()
                .map(|r| r.safe_summary)
                .unwrap_or_default();
            (
                resumes.work_kind.as_str(),
                resumes.work_identity.as_str(),
                resumes.run_id.as_str(),
                summary,
            )
        } else {
            (
                if task.pull_request_url.is_some() {
                    if task.trigger_reason.as_deref() == Some("pull_request_feedback") {
                        "pull_request"
                    } else {
                        "pull_request_review"
                    }
                } else if task.assignee.as_deref() != Some(self.person)
                    && task.trigger_reason.as_deref() == Some("issue_mention")
                {
                    "ticket_mention"
                } else {
                    "ticket"
                },
                url,
                "",
                String::new(),
            )
        };
        serde_json::from_value(json!({"command":COMMAND,"person_id":self.person,"source":self.source,"trigger_type":"ticket",
            "work":{"kind":kind,"identity":identity},"payload":{"task":task,"ticket_url":url,
            "pull_request_url":task.pull_request_url.as_deref().unwrap_or(""),"trigger_reason":task.trigger_reason.as_deref().unwrap_or(""),
            "awaited_run_id":awaited_id,"awaited_summary":summary}})).map_err(|_|ServiceError::InvalidResult)
    }

    pub async fn run(
        &self,
        task: &Task,
        mut invocation: WorkflowInvocation,
        trace: &Trace,
        attempts: usize,
    ) -> Result<CommandReply, DispatchFailure> {
        let trace = task_trace(trace.clone(), task);
        let trace = &trace;
        invocation.run_id = trace.id().into();
        invocation
            .payload
            .insert("max_completion_attempts".into(), json!(attempts.max(1)));
        let result = async {
            let mention = invocation
                .work
                .as_ref()
                .is_some_and(|work| work.kind == "ticket_mention");
            if !mention && task.status == "ready" && task.id.is_some() {
                self.ticket
                    .move_ticket(task, MoveLane::InProgress)
                    .await
                    .map_err(DispatchFailure::Service)?;
            }
            if task.resumes.is_some() {
                self.runner
                    .evidence(
                        trace.id(),
                        "checks_resumed",
                        json!({"target":{"kind":"pull_request","url":task.url}}),
                    )
                    .map_err(|_| DispatchFailure::Journal)?;
            }
            self.runner.run(invocation.clone(), trace).await
        }
        .await;
        match result {
            Ok(reply) => {
                self.settle_checks(task, trace).await;
                if self.source != "manual" {
                    self.runner.completed(task, trace.id(), trace).await;
                }
                Ok(reply)
            }
            Err(error)
                if matches!(error, DispatchFailure::Cancelled) || error.deferred(self.source) =>
            {
                Err(error)
            }
            Err(error) => {
                if let Some(body) = self.settle_failure(task, &invocation, trace, &error).await {
                    Ok(CommandReply {
                        text_output: body,
                        ..CommandReply::default()
                    })
                } else {
                    Err(error)
                }
            }
        }
    }

    async fn comment(
        &self,
        task: &Task,
        body: &str,
        status: &WorkflowStatus,
    ) -> Result<String, ServiceError> {
        let url = task.url.as_deref().ok_or(ServiceError::InvalidResult)?;
        let written = if task.pull_request_url.is_some() {
            self.code
                .ok_or(ServiceError::Unsupported)?
                .comment(url, body, Some(status))
                .await?
        } else {
            self.ticket.comment(url, body, Some(status)).await?
        };
        Ok(written.url.unwrap_or_default())
    }

    async fn settle_failure(
        &self,
        task: &Task,
        invocation: &WorkflowInvocation,
        trace: &Trace,
        error: &DispatchFailure,
    ) -> Option<String> {
        let details = match error {
            DispatchFailure::Guest(failure) => failure.cli_agent_result.as_ref(),
            _ => None,
        };
        let category = details
            .and_then(|v| v.get("error_category"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let limited = category == "rate_limited";
        let text = |key: &str| {
            details
                .and_then(|v| v.get("error_details"))
                .and_then(|v| v.get(key))
                .and_then(Value::as_str)
                .unwrap_or("")
        };
        let retry_at = text("retry_after_at");
        let retry_text = text("retry_after_text");
        let body = if limited {
            rate_limit_notice(self.language, retry_at, retry_text, false)
        } else {
            let text = crate::i18n::t(
                self.language,
                if category == "timeout" {
                    "drivers.task_scheduler.turn_timeout"
                } else {
                    "drivers.task_scheduler.task_error"
                },
                &[("hours", "3")],
            );
            self.runner
                .say(&text, invocation, trace)
                .await
                .filter(|s| !s.is_empty())
                .unwrap_or(text)
        };
        let status = status(
            if limited { "rate_limited" } else { "failed" },
            self.person,
            trace.id(),
            task.url.as_deref().unwrap_or(""),
            retry_at,
            retry_text,
        );
        if let Ok(url) = self.comment(task, &body, &status).await
            && !limited
            && self.source != "manual"
        {
            self.runner
                .notify(WorkStop::of(task, "failed", trace.id(), &url), trace)
                .await;
        }
        if limited {
            let trace = rate_limit_attributes(retry_at, retry_text)
                .into_iter()
                .fold(trace.clone(), |trace, (key, value)| {
                    trace.attribute(&key, value)
                });
            self.runner.activity(&trace,"workflow.rate_limited",json!({"category":"rate_limited","retry_after_at":retry_at,"retry_after_text":retry_text,"run_id":trace.id(),"subject_id":task.url,"source_event_id":""}));
            trace.event("workflow.rate_limited", serde_json::from_value(json!({"category":"rate_limited","retry_after_at":retry_at,"retry_after_text":retry_text,"run_id":trace.id(),"subject_id":task.url,"source_event_id":""})).unwrap());
        }
        limited.then_some(body)
    }

    async fn settle_checks(&self, task: &Task, trace: &Trace) {
        if task.resumes.is_none() {
            return;
        }
        let (Some(url), Some(code)) = (&task.pull_request_url, self.code) else {
            return;
        };
        let Ok(record) = self.runner.record(trace.id()) else {
            return;
        };
        if record
            .as_ref()
            .is_some_and(|r| awaits(&r.provider_evidence, url))
        {
            return;
        }
        let body = crate::i18n::t(
            self.language,
            "integrations.pull_request_patrol.checks_settled",
            &[],
        );
        let _ = code
            .comment(
                url,
                &body,
                Some(&status(
                    "checks_settled",
                    self.person,
                    trace.id(),
                    url,
                    "",
                    "",
                )),
            )
            .await;
    }

    async fn hand_over(&self, task: &Task) {
        let trace = task_trace(
            Trace::open(self.diagnostics, "review_limit")
                .person(self.person)
                .command("pull_request_review_limit"),
            task,
        );
        let (Some(url), Some(code)) = (&task.pull_request_url, self.code) else {
            return;
        };
        let mut settlement = ReviewSettlement {
            trace: trace.clone(),
            task,
            runner: self.runner,
            draft: None,
            ended: false,
        };
        trace.event("review_limit.started", settlement.payload(""));
        let (reason, key) = match code.convert_pull_request_to_draft(url).await {
            Ok(false) => {
                settlement.finish("review_limit.finished");
                return;
            }
            Err(error) if error.is_unavailable() => {
                settlement.finish("review_limit.deferred");
                return;
            }
            Err(_) => (
                "failed",
                "integrations.pull_request_patrol.review_limit_draft_failed",
            ),
            Ok(true) => (
                "review_limit",
                "integrations.pull_request_patrol.review_limit_reached",
            ),
        };
        if reason == "review_limit" {
            settlement.draft = Some(
                json!({"run_id":trace.id(),"reason":"review_limit","action":"draft","count":3,
            "pull_request":{"url":url,"label":task.label,"title":task.title},"error":""}),
            );
        }
        let body = crate::i18n::t(self.language, key, &[("count", "3")]);
        let comment = code
            .comment(
                url,
                &body,
                Some(&status(reason, self.person, trace.id(), url, "", "")),
            )
            .await;
        let comment_failed = comment.is_err();
        let comment_url = comment.ok().and_then(|c| c.url).unwrap_or_default();
        if reason == "review_limit" {
            settlement.drafted(
                &comment_url,
                if comment_failed { "ServiceError" } else { "" },
            );
        }
        self.runner
            .notify(WorkStop::of(task, reason, trace.id(), &comment_url), &trace)
            .await;
        settlement.finish(if reason == "failed" {
            "review_limit.failed"
        } else {
            "review_limit.finished"
        });
    }
}

pub fn task_attributes(task: &Task) -> serde_json::Map<String, Value> {
    [
        ("target.provider", task.provider.as_str()),
        ("target.label", task.label.as_str()),
        (
            "target.kind",
            if task.pull_request_url.is_some() {
                "pull_request"
            } else {
                "ticket"
            },
        ),
        ("target.title", task.title.as_str()),
        ("target.url", task.url.as_deref().unwrap_or("")),
    ]
    .into_iter()
    .map(|(key, value)| (key.into(), json!(value)))
    .collect()
}
pub fn task_trace(mut trace: Trace, task: &Task) -> Trace {
    for (key, value) in task_attributes(task) {
        trace = trace.attribute(&key, value);
    }
    trace
}

fn status(
    reason: &str,
    person: &str,
    run: &str,
    url: &str,
    retry_at: &str,
    retry_text: &str,
) -> WorkflowStatus {
    serde_json::from_value(json!({"reason":reason,"person_id":person,"run_id":run,"subject":url,"retry_after_at":retry_at,"retry_after_text":retry_text})).expect("known status")
}

fn awaits(evidence: &[Value], url: &str) -> bool {
    evidence
        .iter()
        .any(|item| item["type"] == "checks_awaited" && item["payload"]["target"]["url"] == url)
}

pub fn rate_limit_notice(language: &str, retry_at: &str, retry_text: &str, chat: bool) -> String {
    rate_limit_notice_display(language, retry_at, retry_text, chat, |time| {
        time.with_timezone(&chrono::Local)
            .format("%Y-%m-%d %H:%M:%S%:z")
            .to_string()
    })
}
fn rate_limit_notice_display(
    language: &str,
    retry_at: &str,
    retry_text: &str,
    chat: bool,
    display: impl FnOnce(chrono::DateTime<chrono::FixedOffset>) -> String,
) -> String {
    let reset = chrono::DateTime::parse_from_rfc3339(retry_at.trim())
        .ok()
        .map(display);
    let suffix = if reset.is_some() {
        "_with_reset"
    } else if !retry_text.is_empty() {
        "_with_hint"
    } else {
        ""
    };
    let guidance = crate::i18n::t(
        language,
        &format!(
            "commands.workflows.common.{}",
            if chat {
                "rate_limited_retry_chat"
            } else if reset.is_some() {
                "rate_limited_retry_at"
            } else {
                "rate_limited_retry_ticket"
            }
        ),
        &[],
    );
    crate::i18n::t(
        language,
        &format!("commands.workflows.common.rate_limited_escalation{suffix}"),
        &[
            ("retry_after", reset.as_deref().unwrap_or(retry_text)),
            ("retry_guidance", &guidance),
        ],
    )
}

pub fn max_attempts(raw: &str) -> usize {
    raw.trim()
        .parse::<i64>()
        .map(|n| n.max(1) as usize)
        .unwrap_or(5)
}

#[cfg(test)]
mod tests;

fn rate_limit_attributes(at: &str, hint: &str) -> serde_json::Map<String, Value> {
    gb_member::events::rate_limited("", "", "", at, hint)["attributes"]
        .as_object()
        .unwrap()
        .clone()
}
