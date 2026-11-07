//! GuildBotics' server: its port, its owner's credentials, and the API the
//! screens and the command line use, carried over from the Python version
//! (`guildbotics/app_api`) piece by piece.

mod api;
mod auth;
pub mod chat_dispatch;
pub mod chat_receiver;
pub mod chat_state;
pub mod client;
mod command_authoring;
mod command_catalog;
mod command_files_api;
mod commands;
mod config_api;
pub mod diagnostic_logging;
mod diagnostics;
pub mod dispatcher;
pub mod environment;
pub mod events;
pub mod execution;
mod health;
pub mod host_window;
pub mod i18n;
mod input_files;
mod intelligences;
mod keep_awake;
pub mod member;
pub mod native_scheduler;
pub mod required_io;
pub mod schedule;
pub mod scheduler;
mod screens;
mod server;
pub mod service;
pub mod service_runtime;
pub mod service_settings;
pub mod setup_providers;
#[cfg(test)]
mod test_chat;
#[cfg(test)]
mod test_logs;
#[cfg(test)]
mod test_ticket;
pub mod ticket_selector;
pub mod tool_login;
mod ui;
mod verify;
pub mod work_stops;
pub mod workflow_runtime;
pub mod workspace;

pub use server::{Held, PORT_ENV, PORT_NOTICE_PREFIX, RunningServer, Server, Shutdown, running};

mod provider_api;
