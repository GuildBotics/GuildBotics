//! GuildBotics' configuration as running a command reads it, carried over
//! from the Python version piece by piece: the team, and which file a command
//! name runs with what that command declares.

pub mod commands;
mod python_literal;
pub mod team;
mod yaml;
