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
    load_command: &str,
) -> Status {
    let mut params = vec![
        ("reference", image.reference.clone()),
        ("architecture", architecture.into()),
        ("command", load_command.into()),
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
    let (key, params) = match problem {
        DeclarationProblem::ImageField(ImageIssue::Reference(value)) => (
            "intelligences.agent_environment.declaration.not_an_image",
            vec![("reference", value)],
        ),
        DeclarationProblem::ImageField(ImageIssue::NoDigests) => (
            "intelligences.agent_environment.declaration.no_digests",
            vec![],
        ),
        DeclarationProblem::ImageField(ImageIssue::Architecture(value)) => (
            "intelligences.agent_environment.declaration.not_an_architecture",
            vec![("architecture", value)],
        ),
        DeclarationProblem::ImageField(ImageIssue::Digest(value)) => (
            "intelligences.agent_environment.declaration.not_a_digest",
            vec![("digest", value)],
        ),
        DeclarationProblem::Image(path) => (
            "intelligences.agent_environment.declaration.invalid_image",
            vec![("path", path.display().to_string())],
        ),
        other => return Sentence::Text(other.to_string()),
    };
    Sentence::Key(key, params)
}
