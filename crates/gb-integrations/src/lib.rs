//! Shared provider policy. Providers collect observations; these functions
//! decide completion without transport, credentials, or writes.
pub mod factory;
pub use factory::{
    IntegrationConfig, IntegrationEvent, IntegrationObserver, ServiceIntegrationFactory,
};
mod diff_lines;
pub use diff_lines::*;
mod patrol;
mod readiness;
mod scope;
mod selection;

pub use patrol::*;
pub use readiness::*;
pub use scope::*;
pub use selection::*;
