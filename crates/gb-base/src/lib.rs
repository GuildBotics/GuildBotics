//! What every part of GuildBotics' host stands on, carried over from the
//! Python version's `guildbotics/utils`: where the machine's own records are,
//! host paths checked without following links, file locks, and the one way
//! shared files are written.

pub mod files;
pub mod home;
pub mod host_path;
pub mod shared_writes;
pub mod shell;
