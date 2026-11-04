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

/// All ports fail through this hierarchy. Display returns a locale key;
/// only explicitly safe parameters may accompany it at a presentation boundary.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ServiceError {
    Failed,
    OutOfScope,
    BranvilTokenMissing {
        name: String,
    },
    BranvilUsernameMissing,
    MissingCredential {
        name: String,
    },
    CredentialPrefixConflict {
        member: String,
        conflicting_member: String,
    },
    MissingScope {
        needed: String,
        provided: String,
    },
    NotFound,
    /// Only when the operation never reached the service.
    Unavailable,
    Unsupported,
    InvalidResource,
    InvalidParameters,
    InvalidContinuation,
    InvalidResult,
}

impl ServiceError {
    pub fn translation_key(&self) -> &'static str {
        match self {
            Self::Failed => "integrations.resources.service_failed",
            Self::OutOfScope => "integrations.resources.out_of_scope",
            Self::BranvilTokenMissing { .. } => "integrations.branvil.api.token_missing",
            Self::BranvilUsernameMissing => "integrations.branvil.api.username_missing",
            Self::MissingCredential { .. } => "integrations.resources.missing_credential",
            Self::CredentialPrefixConflict { .. } => {
                "integrations.resources.credential_prefix_conflict"
            }
            Self::MissingScope { .. } => "integrations.slack.chat_service.missing_scope",
            Self::NotFound => "integrations.resources.not_found",
            Self::Unavailable => "integrations.resources.unavailable",
            Self::Unsupported => "integrations.resources.operation_unsupported",
            Self::InvalidResource => "integrations.resources.resource",
            Self::InvalidParameters => "integrations.resources.parameters",
            Self::InvalidContinuation => "integrations.resources.continuation",
            Self::InvalidResult => "integrations.resources.invalid_result",
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
            Self::MissingScope { needed, provided } => [
                ("needed".into(), needed.clone()),
                ("provided".into(), provided.clone()),
            ]
            .into(),
            Self::MissingCredential { name } | Self::BranvilTokenMissing { name } => {
                [("name".into(), name.clone())].into()
            }
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
