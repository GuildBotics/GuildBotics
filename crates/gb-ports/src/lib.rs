//! Contracts shared by service providers and their callers.
//! No provider, transport, credential store, or host dependency belongs here.

mod models;
mod resources;
mod status;

pub use models::*;
pub use resources::*;
pub use status::*;

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

/// All ports fail through this one hierarchy. Messages are fixed, safe text;
/// raw provider responses and credentials must never become service errors.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServiceError {
    Failed,
    NotFound,
    /// Only when the operation never reached the service.
    Unavailable,
    Unsupported,
    InvalidResource,
    InvalidParameters,
    InvalidContinuation,
    InvalidResult,
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Failed => "The service operation failed.",
            Self::NotFound => "The item was not found in this service.",
            Self::Unavailable => "No connection was made to the service.",
            Self::Unsupported => "This service does not support the operation.",
            Self::InvalidResource => "Unknown service resource.",
            Self::InvalidParameters => "Invalid resource parameters.",
            Self::InvalidContinuation => "This resource does not accept a continuation.",
            Self::InvalidResult => "The service returned an invalid result.",
        })
    }
}

impl std::error::Error for ServiceError {}
