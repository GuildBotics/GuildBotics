//! GuildBotics' server: its port, its owner's credentials, and the API the
//! screens and the command line use, carried over from the Python version
//! (`guildbotics/app_api`) piece by piece.

mod api;
mod auth;
mod home;
mod i18n;
mod server;

pub use home::HOME_ENV;
pub use server::{Held, PORT_ENV, PORT_NOTICE_PREFIX, Server};
