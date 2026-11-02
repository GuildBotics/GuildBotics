//! How Branvil's commands are started on the running executable, which starts
//! them again itself: the hook scripts Git runs and the automatic runner.

use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::OnceLock;

/// The words before Branvil's own commands on the running executable's
/// command line: none on `branvil`, `branvil` on `guildbotics branvil`.
#[derive(Clone, Copy, Debug)]
pub struct Invocation {
    prefix: &'static [&'static str],
}

static INSTALLED: OnceLock<Invocation> = OnceLock::new();

impl Invocation {
    /// The `branvil` executable.
    pub const fn standalone() -> Self {
        Invocation { prefix: &[] }
    }

    /// Branvil's commands under `prefix` on another executable.
    pub const fn under(prefix: &'static [&'static str]) -> Self {
        Invocation { prefix }
    }

    /// Makes this how the process starts Branvil again; the first one
    /// installed stays.
    pub fn install(self) {
        let _ = INSTALLED.set(self);
    }

    /// The command line's name in help and errors under another executable:
    /// its name and the prefix (none for `branvil`, whose program name is the
    /// first word of its command line).
    pub(crate) fn bin_name(&self) -> Option<String> {
        if self.prefix.is_empty() {
            return None;
        }
        let program = std::env::current_exe()
            .ok()
            .and_then(|exe| exe.file_stem().map(|s| s.to_string_lossy().into_owned()))
            .unwrap_or_default();
        Some(
            std::iter::once(program.as_str())
                .chain(self.prefix.iter().copied())
                .collect::<Vec<_>>()
                .join(" "),
        )
    }
}

/// The running executable, and the words that start Branvil's command `words`
/// on it, as installed (`branvil`'s when nothing was).
pub(crate) fn command(words: &[&str]) -> std::io::Result<(PathBuf, Vec<OsString>)> {
    let prefix = INSTALLED
        .get()
        .copied()
        .unwrap_or(Invocation::standalone())
        .prefix;
    Ok((
        std::env::current_exe()?,
        prefix.iter().chain(words).map(OsString::from).collect(),
    ))
}
