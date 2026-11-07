//! GuildBotics' records of what ran, carried over from the Python version's
//! `guildbotics/observability` and `guildbotics/utils/correlation.py`: traces,
//! and the diagnostics index and transcripts the screens read.
//!
//! A trace owns its command boundary (`run_command`); `Trace::join` returns a
//! participant with no boundary runner. Correlation is passed explicitly, not
//! inherited from a thread, so asynchronous tasks cannot exchange spans by
//! accident. `span` creates child operation ids; `bind_span` uses ids received
//! from the host. Both write through the same diagnostics store.
//!
//! `TraceStatus` folds lifecycle evidence in the Python resolver's precedence
//! order. Only the declared whole-execution events establish success; a child
//! span's completion leaves an execution running. Title resolution accepts a
//! lazy run-summary lookup and a localized chat-trigger formatter from its
//! caller; the diagnostics reader uses English titles until a language is
//! supplied by the screen layer. Python remains the running Python host's
//! implementation during the Rust host migration.
//!
//! `activity`, `runs`, and `interactive` own shared records under `state/`.
//! Their read-modify-write operations borrow `gb_base::shared_writes::Writer`
//! from a held workspace transaction. The host supplies identities, clocks,
//! and a `redaction::Redactor` snapshot of indexed environment secrets; the
//! stores never discover a workspace again or open the keychain. Member
//! completion authorization and workflow decisions belong to the host's
//! capabilities, above this persistence layer. Diagnostics transcripts remain
//! device-local, including process-wide system sessions and retention.

pub mod activity;
mod cursor;
mod diagnostics;
pub mod health;
pub mod interactive;
mod presentations;
mod reader;
pub mod redaction;
pub mod runs;
mod status;
mod streams;
mod title;
mod trace;

pub use diagnostics::Diagnostics;
pub use reader::TraceSearch;
pub use trace::{Announce, Failure, JoinedTrace, SpanContext, Trace};

pub use status::{TRACE_COMPLETED_EVENT_TYPES, TraceStatus};
pub use title::{
    first_seen_attributes, is_read_only_action, resolve_trace_title, target_reference_label,
};
