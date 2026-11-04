//! Shared provider policy. Providers collect observations; these functions
//! decide completion without transport, credentials, or writes.
mod readiness;

pub use readiness::*;
