//! GuildBotics' server: its port, its owner's credentials, and the API the
//! screens and the command line use, carried over from the Python version
//! (`guildbotics/app_api`) piece by piece.

#[cfg(feature = "server")]
mod activity;
#[cfg(feature = "server")]
mod api;
#[cfg(feature = "server")]
mod auth;
mod auth_protocol;
#[cfg(feature = "server")]
pub mod branvil_vault;
#[cfg(feature = "server")]
pub mod chat_dispatch;
#[cfg(feature = "server")]
pub mod chat_receiver;
#[cfg(feature = "server")]
pub mod chat_state;
pub mod client;
#[cfg(feature = "server")]
mod command_authoring;
#[cfg(feature = "server")]
mod command_catalog;
#[cfg(feature = "server")]
mod command_files_api;
#[cfg(feature = "server")]
mod commands;
#[cfg(feature = "server")]
mod config_api;
#[cfg(feature = "server")]
pub mod diagnostic_logging;
#[cfg(feature = "server")]
mod diagnostics;
#[cfg(feature = "server")]
pub mod dispatcher;
#[cfg(feature = "server")]
pub mod environment;
#[cfg(feature = "server")]
pub mod events;
#[cfg(feature = "server")]
pub mod execution;
#[cfg(feature = "server")]
mod health;
#[cfg(feature = "server")]
pub mod host_window;
#[cfg(feature = "server")]
pub mod hub_data;
pub mod i18n;
#[cfg(feature = "server")]
mod input_files;
#[cfg(feature = "server")]
mod intelligences;
#[cfg(feature = "server")]
mod keep_awake;
#[cfg(feature = "server")]
pub mod member;
#[cfg(feature = "server")]
pub mod native_scheduler;
#[cfg(feature = "server")]
pub mod required_io;
#[cfg(feature = "server")]
mod run_files;
#[cfg(feature = "server")]
pub mod schedule;
#[cfg(feature = "server")]
pub mod scheduler;
#[cfg(feature = "server")]
mod screens;
#[cfg(feature = "server")]
mod server;
#[cfg(feature = "server")]
pub mod service;
#[cfg(feature = "server")]
pub mod service_runtime;
#[cfg(feature = "server")]
pub mod service_settings;
#[cfg(feature = "server")]
pub mod setup_providers;
#[cfg(test)]
#[cfg(feature = "server")]
mod test_chat;
#[cfg(test)]
#[cfg(feature = "server")]
mod test_logs;
#[cfg(test)]
#[cfg(feature = "server")]
mod test_ticket;
#[cfg(feature = "server")]
pub mod ticket_selector;
#[cfg(feature = "server")]
pub mod tool_login;
#[cfg(feature = "server")]
mod ui;
#[cfg(feature = "server")]
mod verify;
#[cfg(feature = "server")]
pub mod work_stops;
#[cfg(feature = "server")]
pub mod workflow_runtime;
pub mod workspace;

#[cfg(feature = "server")]
pub use server::{
    Held, PORT_ENV, PORT_NOTICE_PREFIX, RunningServer, Server, Shutdown, port, running,
};

#[cfg(feature = "server")]
mod provider_api;

#[cfg(feature = "server")]
mod git_credentials;

mod command_protocol;
pub mod endpoint;
pub mod member_protocol;

pub mod client_release;
