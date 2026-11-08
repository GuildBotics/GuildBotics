//! Human-facing command text, using Python shlex.join's POSIX quoting rules.
use std::path::Path;

pub fn join(values: &[String]) -> String {
    values
        .iter()
        .map(|value| {
            if !value.is_empty()
                && value
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
            {
                value.clone()
            } else {
                format!("'{}'", value.replace('\'', "'\"'\"'"))
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// The managed executable and arguments, as every host instruction spells them.
pub fn managed_cli_command(executable: &Path, arguments: &[&str]) -> String {
    let mut values = vec![executable.to_string_lossy().into_owned()];
    values.extend(arguments.iter().map(|s| (*s).to_owned()));
    join(&values)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn managed_cli_matches_frozen_shlex_for_plain_spaces_quotes_and_metacharacters() {
        for (path, expected) in [
            ("/home/me/cli", "/home/me/cli"),
            ("/home/my user/cli", "'/home/my user/cli'"),
            ("/home/o'neil/cli", "'/home/o'\"'\"'neil/cli'"),
            ("/home/$(cmd)/cli", "'/home/$(cmd)/cli'"),
        ] {
            for args in [
                &["environment", "login", "codex"][..],
                &["git-credential"][..],
                &["environment", "image", "load"][..],
            ] {
                assert_eq!(
                    managed_cli_command(Path::new(path), args),
                    format!("{expected} {}", args.join(" "))
                );
            }
        }
        assert_eq!(join(&["".into(), "a b".into(), "ok".into()]), "'' 'a b' ok");
    }
}
