//! Image preflight and diagnostics do not need a microVM or real image store.
use gb_config::agent_environment::{BaseImage, PATH, image};
use gb_environment::{
    Sentence,
    image::{declaration_problem, status},
};

fn base() -> BaseImage {
    BaseImage {
        reference: "local/agent:1".into(),
        digests: [("arm64".into(), format!("sha256:{}", "a".repeat(64)))].into(),
    }
}

#[test]
fn local_content_determines_readiness_and_declaration_differences_only_warn() {
    let image = base();
    let missing = status(&image, "arm64", None, "load instruction");
    assert_eq!(
        missing.refusal,
        Some(Sentence::Key(
            "intelligences.agent_environment.image.missing",
            vec![
                ("reference", image.reference.clone()),
                ("architecture", "arm64".into()),
                ("command", "load instruction".into()),
            ]
        ))
    );
    assert_eq!(missing.warning, None);
    let held = image.digests["arm64"].clone();
    let matched = status(&image, "arm64", Some(held.clone()), "load instruction");
    assert_eq!(matched.held, Some(held));
    assert_eq!((matched.refusal, matched.warning), (None, None));
    let other = format!("sha256:{}", "b".repeat(64));
    let mismatch = status(&image, "arm64", Some(other.clone()), "load instruction");
    assert_eq!(mismatch.held, Some(other.clone()));
    assert_eq!(mismatch.refusal, None);
    let Some(Sentence::Key(key, params)) = mismatch.warning else {
        panic!("no warning")
    };
    assert_eq!(key, "intelligences.agent_environment.image.mismatch");
    assert!(params.contains(&("held", "sha256:bbbbbbbbbbbb".into())));
    assert!(params.contains(&("digest", "sha256:aaaaaaaaaaaa".into())));
    let undeclared = status(&image, "amd64", Some(other.clone()), "load instruction");
    assert_eq!(undeclared.refusal, None);
    assert_eq!(undeclared.held, Some(other));
    let Some(Sentence::Key(key, params)) = undeclared.warning else {
        panic!("no warning")
    };
    assert_eq!(key, "intelligences.agent_environment.image.undeclared");
    assert!(params.contains(&("declared", "arm64".into())));
    assert!(params.contains(&("architecture", "amd64".into())));
}

#[test]
fn every_invalid_image_value_carries_its_specific_diagnostic() {
    let config = tempfile::tempdir().unwrap();
    let templates = tempfile::tempdir().unwrap();
    for selected in [config.path(), templates.path()] {
        let path = selected.join(PATH);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let digest = format!("sha256:{}", "a".repeat(64));
        for (value, suffix, param) in [
            (
                serde_json::json!({"reference":"-bad", "digests":{"arm64":digest}}),
                "not_an_image",
                Some(("reference", "-bad")),
            ),
            (
                serde_json::json!({"reference":"local/agent:1", "digests":{}}),
                "no_digests",
                None,
            ),
            (
                serde_json::json!({"reference":"local/agent:1", "digests":{"ARM":digest}}),
                "not_an_architecture",
                Some(("architecture", "ARM")),
            ),
            (
                serde_json::json!({"reference":"local/agent:1", "digests":{"arm64":"bad-digest"}}),
                "not_a_digest",
                Some(("digest", "bad-digest")),
            ),
        ] {
            std::fs::write(&path, serde_json::json!({"image":value}).to_string()).unwrap();
            let error = image(config.path(), templates.path()).unwrap_err();
            let display = error.to_string();
            assert!(display.starts_with(&format!("{}: ", path.display())));
            assert!(
                !display.contains("Digest(")
                    && !display.contains("Reference(")
                    && !display.contains("NoDigests")
            );
            let problem = declaration_problem(error);
            let Sentence::AtPath(source, reason) = problem else {
                panic!("missing source path")
            };
            assert_eq!(source, path);
            let Sentence::Key(key, params) = *reason else {
                panic!("not a localized diagnosis")
            };
            assert_eq!(
                key,
                format!("intelligences.agent_environment.declaration.{suffix}")
            );
            assert_eq!(
                params,
                param
                    .into_iter()
                    .map(|(k, v)| (k, v.to_owned()))
                    .collect::<Vec<_>>()
            );
        }
        std::fs::write(&path, "image: []").unwrap();
        assert_eq!(
            declaration_problem(image(config.path(), templates.path()).unwrap_err()),
            Sentence::Key(
                "intelligences.agent_environment.declaration.invalid_image",
                vec![("path", path.display().to_string())]
            )
        );
        std::fs::remove_file(path).unwrap();
    }
}
