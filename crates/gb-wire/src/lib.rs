//! The wire between GuildBotics' host and a command's isolated environment:
//! what the environment is told of the command (`CommandFacts`), what it runs
//! (`CommandRequest`), and how it ended (`CommandReply`). The types are
//! generated from `wire/schema`, which the Python version writes from its own
//! types (`guildbotics/intelligences/agent_runtime/wire.py`).

#![allow(clippy::all, missing_docs)]

include!(concat!(env!("OUT_DIR"), "/wire.rs"));
