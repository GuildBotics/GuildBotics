//! Contracts shared by service providers and their callers.
//! No provider, transport, credential store, or host dependency belongs here.

mod chat;
mod models;
mod resources;
mod services;
mod status;
mod timestamps;

pub use chat::*;
pub use models::*;
pub use resources::*;
pub use services::*;
pub use status::*;
pub use timestamps::*;

use serde::{Deserialize, Serialize};

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
