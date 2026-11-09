//! Chat data and operations, independent of provider, host and member config.
use std::{collections::BTreeMap, future::Future, pin::Pin};

use chrono::{DateTime, FixedOffset, TimeZone, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{ActorName, SemanticReaction, ServiceError, WorkflowStatus};

pub type ServiceFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, ServiceError>> + Send + 'a>>;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatIdentity {
    pub user_id: String,
    #[serde(default)]
    pub display_name: String,
    #[serde(default)]
    pub workspace: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatIdentityOutput {
    pub service: String,
    pub user_id: String,
    pub display_name: String,
    pub person_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatPostOutput {
    pub service: String,
    pub channel_id: String,
    pub message_id: String,
    pub thread_id: String,
    pub occurred_at: String,
    pub text: String,
    pub posted: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatReactionOutput {
    pub service: String,
    pub channel_id: String,
    pub message_id: String,
    pub reaction: SemanticReaction,
    pub reacted: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatChannelOutput {
    pub service: String,
    pub channel_name: String,
    pub channel_id: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialStatus {
    Ok,
    Failed,
    Unconfigured,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CredentialCheck {
    pub name: String,
    pub status: CredentialStatus,
    #[serde(default)]
    pub error: String,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub error_args: BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatMessageRef {
    pub channel_id: String,
    pub message_id: String,
    pub thread_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatMessage {
    pub channel_id: String,
    pub message_id: String,
    pub thread_id: String,
    pub occurred_at: DateTime<FixedOffset>,
    pub author_id: Option<String>,
    pub text: String,
    #[serde(default)]
    pub mentions: Vec<String>,
    #[serde(default)]
    pub is_bot_message: bool,
}

/// UTC at fixed microsecond width, followed by an opaque identifier.
pub fn message_position<Tz: TimeZone>(occurred_at: DateTime<Tz>, message_id: &str) -> String {
    format!(
        "{} {message_id}",
        occurred_at
            .with_timezone(&Utc)
            .format("%Y-%m-%dT%H:%M:%S%.6fZ")
    )
}

impl ChatMessage {
    pub fn position(&self) -> String {
        message_position(self.occurred_at, &self.message_id)
    }
    pub fn is_from_user(&self, user_id: Option<&str>) -> bool {
        user_id.is_some_and(|id| !id.is_empty() && self.author_id.as_deref() == Some(id))
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatEvent {
    #[serde(flatten)]
    pub message: ChatMessage,
    pub event_id: String,
    #[serde(default)]
    pub is_thread_reply: bool,
    #[serde(default)]
    pub status: Option<WorkflowStatus>,
}

impl ChatEvent {
    /// The guest's event output deliberately excludes internal status data.
    pub fn payload(&self) -> Value {
        let m = &self.message;
        let time = if m.occurred_at.timestamp_subsec_micros() == 0 {
            m.occurred_at.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
        } else {
            m.occurred_at.format("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
        };
        json!({"event_id": self.event_id, "channel_id": m.channel_id,
            "message_id": m.message_id, "thread_id": m.thread_id,
            "occurred_at": time, "author_id": m.author_id.as_deref().unwrap_or(""),
            "text": m.text, "mentions": m.mentions, "is_bot_message": m.is_bot_message,
            "is_thread_reply": self.is_thread_reply})
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatEventPage {
    pub events: Vec<ChatEvent>,
    pub cursor: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatPostResult {
    pub channel_id: String,
    pub message_id: String,
    pub thread_id: String,
    pub occurred_at: DateTime<FixedOffset>,
}

#[derive(Clone, Debug)]
pub struct ChannelEventsQuery {
    pub cursor: Option<String>,
    pub since: Option<DateTime<Utc>>,
    pub until: Option<DateTime<Utc>>,
    pub limit: usize,
}

impl Default for ChannelEventsQuery {
    fn default() -> Self {
        Self {
            cursor: None,
            since: None,
            until: None,
            limit: 100,
        }
    }
}

/// Each instance is already bound to its member by the provider factory.
/// Identity syntax takes a service account resolved from the member roster,
/// so the port does not depend on host config or re-resolve a Person.
pub trait ChatService: Send + Sync {
    fn get_user_avatar_url<'a>(&'a self, _user: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async { Err(ServiceError::Unsupported) })
    }
    fn probe_read_scopes(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async { Err(ServiceError::Unsupported) })
    }
    fn get_bot_identity(&self) -> ServiceFuture<'_, ChatIdentity>;
    fn check_credentials(&self) -> ServiceFuture<'_, Vec<CredentialCheck>>;
    fn self_user_id(&self, account: &ActorName) -> String;
    fn parse_message_url(&self, url: &str) -> Result<ChatMessageRef, ServiceError>;
    /// Return each mentioned service account once, preserving first appearance.
    fn mentioned_user_ids(&self, text: &str) -> Vec<String>;
    /// Channel history contains roots, with inclusive since/until bounds.
    fn list_channel_events<'a>(
        &'a self,
        channel: &'a str,
        query: ChannelEventsQuery,
    ) -> ServiceFuture<'a, ChatEventPage>;
    /// Thread history includes its root; a missing thread is NotFound.
    fn list_thread_events<'a>(
        &'a self,
        channel: &'a str,
        thread: &'a str,
        cursor: Option<String>,
        limit: usize,
    ) -> ServiceFuture<'a, ChatEventPage>;
    fn resolve_channel_id<'a>(&'a self, name: &'a str) -> ServiceFuture<'a, Option<String>>;
    fn post_message<'a>(
        &'a self,
        channel: &'a str,
        text: &'a str,
        thread: Option<&'a str>,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, ChatPostResult>;
    /// Repeating the same member's semantic reaction is idempotent.
    fn add_reaction<'a>(
        &'a self,
        channel: &'a str,
        message: &'a str,
        reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ()>;
    fn normalize_participant_text(&self, text: &str, _labels: &BTreeMap<String, String>) -> String {
        text.into()
    }
    fn render_participant_text(&self, text: &str, _labels: &BTreeMap<String, String>) -> String {
        text.into()
    }
}

/// One provider connection; queued events survive reconnects and stop/start.
pub trait EventListener: Send + Sync {
    /// The reception task exited; disconnection during retries is not termination.
    fn terminated(&self) -> bool {
        false
    }
    fn connected(&self) -> bool;
    fn auth_failed(&self) -> bool;
    fn start(&self) -> ServiceFuture<'_, ()>;
    fn stop(&self) -> ServiceFuture<'_, ()>;
    fn drain_events(&self) -> Vec<ChatEvent>;
}
