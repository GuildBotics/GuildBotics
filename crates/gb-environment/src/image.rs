//! The declared base image against local content, shared by preflight and status.

use crate::Sentence;
use gb_config::agent_environment::{BaseImage, DeclarationProblem, ImageIssue};

pub fn device_architecture() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    }
}

#[derive(Debug, PartialEq)]
pub struct Status {
    pub held: Option<String>,
    pub refusal: Option<Sentence>,
    pub warning: Option<Sentence>,
}

/// Only eligible local content is passed as `held`; an inventory error is
/// propagated by the caller, never treated as an absent image.
pub fn status(
    image: &BaseImage,
    architecture: &str,
    held: Option<String>,
    command: &str,
) -> Status {
    let mut params = vec![
        ("reference", image.reference.clone()),
        ("architecture", architecture.into()),
        ("command", command.into()),
    ];
    let Some(digest) = held.as_ref() else {
        return Status {
            held,
            refusal: Some(Sentence::Key(
                "intelligences.agent_environment.image.missing",
                params,
            )),
            warning: None,
        };
    };
    params.push(("held", short_digest(digest)));
    let warning = match image.digests.get(architecture) {
        Some(expected) if expected == digest => None,
        Some(expected) => {
            params.push(("digest", short_digest(expected)));
            Some(Sentence::Key(
                "intelligences.agent_environment.image.mismatch",
                params,
            ))
        }
        None => {
            params.push((
                "declared",
                image.digests.keys().cloned().collect::<Vec<_>>().join(", "),
            ));
            Some(Sentence::Key(
                "intelligences.agent_environment.image.undeclared",
                params,
            ))
        }
    };
    Status {
        held,
        refusal: None,
        warning,
    }
}

fn short_digest(digest: &str) -> String {
    digest.chars().take(19).collect()
}

pub fn declaration_problem(problem: DeclarationProblem) -> Sentence {
    if let DeclarationProblem::ImageField(path, issue) = problem {
        let (key, params) = match issue {
            ImageIssue::Reference(value) => (
                "intelligences.agent_environment.declaration.not_an_image",
                vec![("reference", value)],
            ),
            ImageIssue::NoDigests => (
                "intelligences.agent_environment.declaration.no_digests",
                vec![],
            ),
            ImageIssue::Architecture(value) => (
                "intelligences.agent_environment.declaration.not_an_architecture",
                vec![("architecture", value)],
            ),
            ImageIssue::Digest(value) => (
                "intelligences.agent_environment.declaration.not_a_digest",
                vec![("digest", value)],
            ),
        };
        return Sentence::AtPath(path, Box::new(Sentence::Key(key, params)));
    }
    match problem {
        DeclarationProblem::Image(path) => Sentence::Key(
            "intelligences.agent_environment.declaration.invalid_image",
            vec![("path", path.display().to_string())],
        ),
        other => Sentence::Text(other.to_string()),
    }
}

/// The installed CLI, shell-quoted so a person can paste the instruction.
pub fn load_command(home: &std::path::Path) -> Result<String, String> {
    if !home.is_absolute() {
        return Err("The home directory for the managed CLI must be absolute.".into());
    }
    let executable = home.join(".guildbotics/bin/guildbotics");
    let executable = executable
        .to_str()
        .ok_or("The managed CLI path is not UTF-8.")?;
    Ok(format!(
        "'{}' environment image load <archive.tar>",
        executable.replace('\'', "'\"'\"'")
    ))
}
