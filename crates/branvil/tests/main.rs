//! The integration tests, built as one test crate: each test crate links all
//! of Branvil's dependencies statically, so one crate per file multiplied the
//! link time and the size of `target`. Run one file's tests by its module
//! path, e.g. `cargo test --test main real_executor::`.

mod common;

mod attachments;
mod autoland;
mod backup;
mod ci;
mod ci_settings;
mod git_http;
mod gui;
mod landing;
mod landing_acceptance;
mod maintenance;
mod markdown;
mod mentions;
mod pulls;
mod real_executor;
mod recovery;
mod security;
mod tickets;
mod upstream;

mod responder;

#[cfg(feature = "test-support")]
#[path = "agent_code/main.rs"]
mod agent_code;
#[cfg(feature = "test-support")]
mod agent_patrol;
mod agent_readiness;
mod agent_selection_pr;
