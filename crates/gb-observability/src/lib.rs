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

mod diagnostics;
mod reader;
mod status;
mod title;
mod trace;

pub use diagnostics::Diagnostics;
pub use trace::{Failure, JoinedTrace, SpanContext, Trace};

pub use status::{TRACE_COMPLETED_EVENT_TYPES, TraceStatus};
pub use title::{
    first_seen_attributes, is_read_only_action, resolve_trace_title, target_reference_label,
};
