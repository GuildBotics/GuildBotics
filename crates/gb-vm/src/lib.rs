//! Local runtime paths are shared by the Hub's CI and isolated commands.

use std::path::{Path, PathBuf};

pub const IMAGE_LOCK: &str = "images.lock";

pub const RUNTIME_VERSION: &str = "0.6.17";

pub fn digest_reference(reference: &str) -> bool {
    reference.contains("@sha256:")
}

pub fn home(data: &Path) -> PathBuf {
    data.join("msb")
}

#[cfg(feature = "host")]
mod host;
#[cfg(feature = "host")]
pub use host::*;
#[cfg(feature = "host")]
pub use microsandbox as sdk;
