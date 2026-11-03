//! GuildBotics' configuration as running a command reads it, carried over
//! from the Python version piece by piece: the team, which file a command
//! name runs with what that command declares, and what its isolated
//! environment boots with.

pub mod agent_environment;
pub mod command_files;
pub mod commands;
mod python_literal;
pub mod team;
mod yaml;
