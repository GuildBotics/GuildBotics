//! Status of a whole execution, in precedence order, shared by all readers.
use serde_json::Value;

pub const TRACE_COMPLETED_EVENT_TYPES: &[&str] = &[
    "command.finished",
    "member.command.finished",
    "system.finished",
    "diagnostics.completed",
    "verify.completed",
    "review_limit.finished",
];

#[derive(Default)]
pub struct TraceStatus {
    observed: &'static str,
    completion: &'static str,
    dispatch: &'static str,
    rate_limited: bool,
    deferred: bool,
}

impl TraceStatus {
    pub fn observe(&mut self, lifecycle: &str) {
        self.observed = match lifecycle {
            "running" => "running",
            "succeeded" | "success" => "success",
            "failed" | "cancelled" | "interrupted" => "failed",
            _ => "info",
        };
    }

    pub fn add(&mut self, record: &Value) {
        let text = |key| record[key].as_str().unwrap_or("");
        match text("kind") {
            "log" => {
                if matches!(text("level").to_uppercase().as_str(), "ERROR" | "CRITICAL") {
                    self.observed = "failed";
                }
                return;
            }
            "event" => {}
            _ => return,
        }
        match text("type") {
            "command.deferred" | "review_limit.deferred" => self.deferred = true,
            "workflow.rate_limited" => self.rate_limited = true,
            "chat_dispatch.abandoned" => self.dispatch = "abandoned",
            "chat_dispatch.retry_scheduled" => self.dispatch = "retry_scheduled",
            event @ ("workflow.completed" | "workflow.completion_missing") => {
                self.completion = if record["payload"]["status"] == "awaiting_checks" {
                    "awaiting_checks"
                } else if event == "workflow.completed" {
                    "success"
                } else {
                    "incomplete"
                };
            }
            event if event.ends_with(".failed") => self.observed = "failed",
            _ if matches!(self.observed, "failed" | "success") => {}
            event if TRACE_COMPLETED_EVENT_TYPES.contains(&event) => self.observed = "success",
            event if event.ends_with(".started") || event.ends_with(".finished") => {
                self.observed = "running"
            }
            _ => {}
        }
    }

    pub fn resolve(&self) -> &'static str {
        if self.dispatch == "abandoned" {
            "abandoned"
        } else if self.rate_limited {
            "rate_limited"
        } else if !self.dispatch.is_empty() {
            self.dispatch
        } else if !self.completion.is_empty() {
            self.completion
        } else if self.deferred || self.observed.is_empty() {
            "info"
        } else {
            self.observed
        }
    }
}
