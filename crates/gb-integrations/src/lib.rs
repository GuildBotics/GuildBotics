//! Shared provider policy. Providers collect observations; these functions
//! decide completion without transport, credentials, or writes.
// The shared chat contract in tests/support names this crate as its tests do.
#[cfg(test)]
extern crate self as gb_integrations;
pub mod chat;
pub mod factory;
pub use factory::{
    IntegrationConfig, IntegrationEvent, IntegrationObserver, ServiceIntegrationFactory,
};
mod diff_lines;
pub use diff_lines::*;
mod patrol;
mod readiness;
mod scope;

pub use patrol::*;
pub use readiness::*;
pub use scope::*;
