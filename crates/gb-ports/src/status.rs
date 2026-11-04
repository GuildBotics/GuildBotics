use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkflowStatusReason {
    Failed,
    RateLimited,
    ReviewLimit,
    AwaitingChecks,
    ChecksSettled,
    Asking,
    Blocked,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Fields {
    reason: WorkflowStatusReason,
    person_id: String,
    run_id: String,
    #[serde(default)]
    subject: String,
    #[serde(default)]
    retry_after_at: String,
    #[serde(default)]
    retry_after_text: String,
    #[serde(default)]
    head: String,
    #[serde(default)]
    work_kind: String,
    #[serde(default)]
    work_identity: String,
}

/// A status carrier is never work. An awaiting-checks status always names
/// the work that the patrol must resume.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "Fields", into = "Fields")]
pub struct WorkflowStatus(Fields);

impl TryFrom<Fields> for WorkflowStatus {
    type Error = &'static str;
    fn try_from(fields: Fields) -> Result<Self, Self::Error> {
        if fields.reason == WorkflowStatusReason::AwaitingChecks
            && (fields.work_kind.is_empty() || fields.work_identity.is_empty())
        {
            return Err("A wait for checks names the work it resumes.");
        }
        Ok(Self(fields))
    }
}

impl From<WorkflowStatus> for Fields {
    fn from(status: WorkflowStatus) -> Self {
        status.0
    }
}

impl WorkflowStatus {
    pub fn reason(&self) -> WorkflowStatusReason {
        self.0.reason
    }

    pub fn payload(&self) -> Value {
        let mut value = serde_json::to_value(&self.0).expect("status fields serialize");
        let object = value.as_object_mut().expect("status is an object");
        object.retain(|_, v| v != "");
        object.insert("kind".into(), json!("workflow_error"));
        object.insert("routing".into(), json!("suppress"));
        value
    }

    pub fn from_payload(payload: &Value) -> Option<Self> {
        let object = payload.as_object()?;
        if object.get("kind")? != "workflow_error" {
            return None;
        }
        let mut known = json!({"run_id":""});
        for name in [
            "reason",
            "person_id",
            "run_id",
            "subject",
            "retry_after_at",
            "retry_after_text",
            "head",
            "work_kind",
            "work_identity",
        ] {
            if let Some(value) = object.get(name) {
                known[name] = value.clone();
            }
        }
        serde_json::from_value(known).ok()
    }

    pub fn holds_selection(&self, now: DateTime<Utc>) -> bool {
        match self.0.reason {
            WorkflowStatusReason::Failed => true,
            WorkflowStatusReason::RateLimited => {
                DateTime::parse_from_rfc3339(&self.0.retry_after_at)
                    .map_or(true, |retry| retry > now)
            }
            _ => false,
        }
    }
}
