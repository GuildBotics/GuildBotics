//! Chat: its data and operations, and the Slack and local providers behind them.
// The shared chat contract in tests/support names this crate as its tests do.
#[cfg(test)]
extern crate self as gb_integrations;
pub mod chat;
pub mod factory;
pub use factory::{
    IntegrationConfig, IntegrationEvent, IntegrationObserver, ServiceIntegrationFactory,
};
