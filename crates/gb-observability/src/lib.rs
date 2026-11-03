//! GuildBotics' records of what ran, carried over from the Python version's
//! `guildbotics/observability` and `guildbotics/utils/correlation.py`: traces,
//! and the diagnostics index and transcripts the screens read.

mod diagnostics;
mod reader;
mod trace;

pub use diagnostics::Diagnostics;
pub use trace::{Failure, Trace};
