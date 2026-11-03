//! GuildBotics' server: its port, its owner's credentials, and the API the
//! screens and the command line use, carried over from the Python version
//! (`guildbotics/app_api`) piece by piece.

mod api;
mod auth;
pub mod client;
mod commands;
mod i18n;
mod screens;
mod server;
mod ui;

pub use server::{Held, PORT_ENV, PORT_NOTICE_PREFIX, Server};
