//! The language the host's operating system shows its user interface in,
//! as the microVM is told it: `LANGUAGE` in gettext's form (`ja_JP`), as the
//! Python version tells it (`guildbotics/utils/os_language.py`, the
//! `_ui_language` of `guildbotics/environment/spec.py`).
//!
//! The Python version reads the operating system's tag with langcodes, which
//! replaces deprecated and macro codes by others in ways that depend on the
//! whole tag. This reads the plain shape every operating system gives -- a
//! language, perhaps a script, perhaps a region -- and names no language for
//! anything else, nor for a code langcodes would replace
//! (`language_aliases.json`, which the Python version writes): an
//! environment told no language says so, where one told another than the
//! Python version's would speak it.

use std::collections::HashSet;
use std::sync::LazyLock;

/// The codes langcodes reads as other ones, and the whole tags it replaces.
struct Aliases {
    languages: HashSet<String>,
    regions: HashSet<String>,
    tags: HashSet<String>,
}

static ALIASES: LazyLock<Aliases> = LazyLock::new(|| {
    let table: serde_json::Value =
        serde_json::from_str(include_str!("language_aliases.json")).expect("JSON");
    let codes = |key: &str| {
        table[key]
            .as_array()
            .expect("a list")
            .iter()
            .map(|code| code.as_str().expect("a code").to_owned())
            .collect()
    };
    Aliases {
        languages: codes("languages"),
        regions: codes("regions"),
        tags: codes("tags"),
    }
});

/// How long macOS may take to say its languages.
#[cfg(target_os = "macos")]
const DEFAULTS_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);

/// The operating system's UI language as gettext names it, read afresh; none
/// when the operating system names none this reads.
pub async fn ui_language() -> Option<String> {
    gettext_name(&os_tag().await?)
}

#[cfg(target_os = "macos")]
async fn os_tag() -> Option<String> {
    let mut defaults = tokio::process::Command::new("/usr/bin/defaults");
    defaults
        .args(["read", "-g", "AppleLanguages"])
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true);
    let output = tokio::time::timeout(DEFAULTS_TIMEOUT, defaults.output())
        .await
        .ok()?
        .ok()?;
    if !output.status.success() {
        return None;
    }
    macos_tag(&String::from_utf8_lossy(&output.stdout))
}

#[cfg(all(unix, not(target_os = "macos")))]
async fn os_tag() -> Option<String> {
    // A value that is not Unicode is still the first named: it names no tag.
    posix_tag(|name| std::env::var_os(name).map(|v| v.to_string_lossy().into_owned()))
}

/// Windows keeps the language in its own settings, which this does not read
/// yet: the environment is told none rather than another.
#[cfg(windows)]
async fn os_tag() -> Option<String> {
    None
}

/// The first tag of the languages macOS lists (`defaults read -g
/// AppleLanguages`).
pub fn macos_tag(stdout: &str) -> Option<String> {
    stdout
        .lines()
        .map(|line| line.trim_matches(|c| " \t\",();".contains(c)))
        .find(|candidate| looks_like_tag(candidate))
        .map(str::to_owned)
}

/// `[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*`, as the Python version matches it.
fn looks_like_tag(candidate: &str) -> bool {
    let mut parts = candidate.split(['-', '_']);
    let first = parts.next().unwrap_or_default();
    (2..=3).contains(&first.len())
        && first.bytes().all(|b| b.is_ascii_alphabetic())
        && parts.all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_alphanumeric()))
}

/// The first of the gettext variables that names a language.
pub fn posix_tag(var: impl Fn(&str) -> Option<String>) -> Option<String> {
    ["LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"]
        .iter()
        .filter_map(|name| var(name))
        .find(|value| !value.is_empty())
        .map(|value| value.split(':').next().unwrap_or_default().to_owned())
}

/// The language `tag` names, as gettext names it (`ja_JP`); none for a tag
/// that names none, or one this does not read as the Python version does.
pub fn gettext_name(tag: &str) -> Option<String> {
    let tag = tag.split('.').next()?.split('@').next()?;
    let mut subtags = tag.split(['-', '_']);
    let language = subtags.next()?.to_ascii_lowercase();
    if !(2..=3).contains(&language.len())
        || !language.bytes().all(|b| b.is_ascii_alphabetic())
        || language == "und"
        || ALIASES.languages.contains(&language)
    {
        return None;
    }
    let mut next = subtags.next();
    if next.is_some_and(|s| s.len() == 4 && s.bytes().all(|b| b.is_ascii_alphabetic())) {
        next = subtags.next();
    }
    let region = match next {
        None => None,
        Some(s) if s.len() == 2 && s.bytes().all(|b| b.is_ascii_alphabetic()) => {
            Some(s.to_ascii_uppercase())
        }
        Some(s) if s.len() == 3 && s.bytes().all(|b| b.is_ascii_digit()) => Some(s.to_owned()),
        Some(_) => return None,
    };
    let replaced = region.as_ref().is_some_and(|region| {
        ALIASES.regions.contains(region)
            || ALIASES
                .tags
                .contains(&format!("{language}-{}", region.to_ascii_lowercase()))
    });
    if subtags.next().is_some() || replaced {
        return None;
    }
    Some(match region {
        Some(region) => format!("{language}_{region}"),
        None => language,
    })
}
