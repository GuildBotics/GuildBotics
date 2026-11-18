//! What the services a member works through share: how their operations
//! fail, the reactions a member adds, the account names they show, and the
//! status a run that did not finish leaves where its work came from.
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{future::Future, pin::Pin};

pub type ServiceFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, ServiceError>> + Send + 'a>>;

/// Credentials are deliberately excluded from Debug and serialization.
#[derive(Clone)]
pub struct GitCredential {
    pub username: String,
    pub password: String,
}


/// A service's public account name, resolved through the member roster.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ActorName(pub String);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticReaction {
    Ack,
    Agree,
    Celebrate,
    Support,
}

/// Categories remain available independently of localized provider guidance.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServiceErrorClass {
    Failure,
    NotFound,
    Unsupported,
    Unavailable,
}

/// All ports fail through this hierarchy. Display returns a locale key;
/// only explicitly safe parameters may accompany it at a presentation boundary.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ServiceError {
    Failed,
    OutOfScope,
    /// The provider owns the localized diagnosis and supplies only safe arguments.
    Diagnostic {
        class: ServiceErrorClass,
        key: &'static str,
        args: std::collections::BTreeMap<String, String>,
    },
    MissingCredential {
        name: String,
    },
    CredentialPrefixConflict {
        member: String,
        conflicting_member: String,
    },
    NotFound,
    /// Only when the operation never reached the service.
    Unavailable,
    Unsupported,
    InvalidResource,
    InvalidParameters,
    InvalidContinuation,
    InvalidResult,
    ResponseTooLarge,
    AttachmentMissing {
        url: String,
        id: String,
    },
    AttachmentTooLarge {
        name: String,
        limit: usize,
    },
}

impl ServiceError {
    pub fn is_unavailable(&self) -> bool {
        matches!(
            self,
            Self::Unavailable
                | Self::Diagnostic {
                    class: ServiceErrorClass::Unavailable,
                    ..
                }
        )
    }
    pub fn is_not_found(&self) -> bool {
        matches!(
            self,
            Self::NotFound
                | Self::AttachmentMissing { .. }
                | Self::Diagnostic {
                    class: ServiceErrorClass::NotFound,
                    ..
                }
        )
    }
    pub fn is_unsupported(&self) -> bool {
        matches!(
            self,
            Self::Unsupported
                | Self::Diagnostic {
                    class: ServiceErrorClass::Unsupported,
                    ..
                }
        )
    }
    pub fn translation_key(&self) -> &'static str {
        match self {
            Self::Failed => "integrations.resources.service_failed",
            Self::OutOfScope => "integrations.resources.out_of_scope",
            Self::Diagnostic { key, .. } => key,
            Self::MissingCredential { .. } => "integrations.resources.missing_credential",
            Self::CredentialPrefixConflict { .. } => {
                "integrations.resources.credential_prefix_conflict"
            }
            Self::NotFound => "integrations.resources.not_found",
            Self::Unavailable => "integrations.resources.unavailable",
            Self::Unsupported => "integrations.resources.operation_unsupported",
            Self::InvalidResource => "integrations.resources.resource",
            Self::InvalidParameters => "integrations.resources.parameters",
            Self::InvalidContinuation => "integrations.resources.continuation",
            Self::InvalidResult => "integrations.resources.invalid_result",
            Self::ResponseTooLarge => "integrations.resources.too_large",
            Self::AttachmentMissing { .. } => "integrations.resources.attachment_missing",
            Self::AttachmentTooLarge { .. } => "integrations.resources.attachment_too_large",
        }
    }
    pub fn translation_args(&self) -> std::collections::BTreeMap<String, String> {
        match self {
            Self::CredentialPrefixConflict {
                member,
                conflicting_member,
            } => [
                ("member".into(), member.clone()),
                ("conflicting_member".into(), conflicting_member.clone()),
            ]
            .into(),
            Self::Diagnostic { args, .. } => args.clone(),
            Self::MissingCredential { name } => [("name".into(), name.clone())].into(),
            Self::AttachmentMissing { url, id } => {
                [("url".into(), url.clone()), ("id".into(), id.clone())].into()
            }
            Self::AttachmentTooLarge { name, limit } => [
                ("name".into(), name.clone()),
                ("limit".into(), limit.to_string()),
            ]
            .into(),
            _ => Default::default(),
        }
    }
}
impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.translation_key())
    }
}

impl std::error::Error for ServiceError {}

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
                parse_retry_timestamp(&self.0.retry_after_at)
                    .is_none_or(|retry| retry > now)
            }
            _ => false,
        }
    }
}

// ISO timestamps shared by workflow status and service selection.

/// Frozen timestamp parsing treats an ISO time without a zone as UTC.
pub fn parse_timestamp(value: &str) -> Option<DateTime<Utc>> {
    parse_iso_timestamp(value.trim(), true)
}

/// A workflow reset must carry a zone; an unknown reset keeps work held.
pub(crate) fn parse_retry_timestamp(value: &str) -> Option<DateTime<Utc>> {
    parse_iso_timestamp(value, false)
}

fn parse_iso_timestamp(raw: &str, allow_naive: bool) -> Option<DateTime<Utc>> {
    if let Ok(time) = DateTime::parse_from_rfc3339(raw) {
        return Some(time.with_timezone(&Utc));
    }
    // ISO calendar and week dates, with optional time and zone. Chrono owns
    // calendar validation; the spelling is normalized before its time parser.
    for (width, format, monday) in [
        (10, "%Y-%m-%d", false),
        (8, "%Y%m%d", false),
        (10, "%G-W%V-%u", false),
        (8, "%GW%V%u", false),
        (8, "%G-W%V-%u", true),
        (7, "%GW%V%u", true),
    ] {
        let Some(part) = raw.get(..width) else {
            continue;
        };
        let date = if monday {
            format!("{part}{}1", if part.contains('-') { "-" } else { "" })
        } else {
            part.to_string()
        };
        let Ok(day) = chrono::NaiveDate::parse_from_str(&date, format) else {
            continue;
        };
        let rest = &raw[width..];
        if rest.is_empty() {
            return allow_naive.then(|| day.and_hms_opt(0, 0, 0).unwrap().and_utc());
        }
        let separator = rest.chars().next()?;
        let value = format!(
            "{}T{}",
            day.format("%Y-%m-%d"),
            &rest[separator.len_utf8()..]
        );
        for time in ["%H:%M:%S%.f", "%H:%M", "%H", "%H%M%S%.f", "%H%M"] {
            let pattern = format!("%Y-%m-%dT{time}");
            if allow_naive && let Ok(time) = chrono::NaiveDateTime::parse_from_str(&value, &pattern)
            {
                return Some(time.and_utc());
            }
            for zone in ["%#z", "%::z"] {
                if let Ok(time) = DateTime::parse_from_str(&value, &format!("{pattern}{zone}")) {
                    return Some(time.with_timezone(&Utc));
                }
            }
        }
    }
    None
}
