//! Member capabilities. CLI parsing and member git belong to their callers.
pub mod archive;
pub mod audit;
pub mod chat;
pub mod chat_updates;
pub mod command;
pub mod context;
pub mod control;
pub mod events;
pub mod git;
mod identity;
pub mod memory;
pub mod output;
pub mod repository;
pub mod session;

use gb_ports::ServiceError;

/// Capability validation is distinct from the provider's error hierarchy.
#[derive(Debug)]
pub enum MemberError {
    Capability(&'static str),
    Approval {
        action: &'static str,
        args: std::collections::BTreeMap<String, String>,
    },
    CapabilityArgs {
        key: &'static str,
        args: std::collections::BTreeMap<String, String>,
    },
    Service(ServiceError),
}
impl From<ServiceError> for MemberError {
    fn from(error: ServiceError) -> Self {
        Self::Service(error)
    }
}
impl std::fmt::Display for MemberError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Capability(key) => f.write_str(key),
            Self::Approval { .. } => f.write_str("cli.member.human_approval_required"),
            Self::CapabilityArgs { key, .. } => f.write_str(key),
            Self::Service(error) => error.fmt(f),
        }
    }
}
impl std::error::Error for MemberError {}
impl MemberError {
    pub fn exit_code(&self) -> i32 {
        let key = match self {
            Self::Capability(key) | Self::CapabilityArgs { key, .. } => *key,
            _ => return 1,
        };
        if matches!(
            key,
            "cli.member.option_choice"
                | "cli.member.option_flag"
                | "cli.member.option_integer"
                | "cli.member.option_required"
                | "cli.member.option_value"
                | "cli.member.unknown_command"
                | "cli.member.unknown_option"
                | "cli.member.content.empty"
                | "cli.member.content.exclusive"
                | "cli.member.content.file_in_environment"
                | "cli.member.content.required"
        ) || matches!(
            key,
            "cli.member.title_invalid"
                | "cli.member.params_invalid"
                | "cli.member.state_reason"
                | "cli.member.ticket_links_exclusive"
                | "cli.member.ticket_links_required"
                | "cli.member.ticket_links_invalid"
                | "capabilities.member_git.prepare_anchor"
        ) {
            2
        } else {
            1
        }
    }
    /// The presentation layer supplies its locale renderer. Approval actions
    /// are translated before insertion into the human-approval guidance.
    pub fn render(
        &self,
        t: impl Fn(&str, &std::collections::BTreeMap<String, String>) -> String,
    ) -> String {
        let empty = Default::default();
        match self {
            Self::Capability(key) => t(key, &empty),
            Self::CapabilityArgs { key, args } => t(key, args),
            Self::Approval { action, args } => t(
                "cli.member.human_approval_required",
                &[("action".into(), t(action, args))].into(),
            ),
            Self::Service(error) => t(error.translation_key(), &error.translation_args()),
        }
    }
}
pub type Result<T> = std::result::Result<T, MemberError>;

/// A write must consult the invocation's durable chat queue immediately before
/// accessing the service. The invocation owner provides this dependency.
pub trait WriteGuard: Send + Sync {
    fn ensure_current(&self) -> Result<()>;
}
