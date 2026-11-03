//! The microVM is told the host's UI language as the Python version tells it:
//! `records/golden/environment.json`, which
//! `tests/guildbotics/test_environment_golden.py` checks against the Python
//! version.

use std::path::Path;

use gb_environment::language::{gettext_name, macos_tag, posix_tag};
use serde_json::{Value, json};

/// Tags this declines to name a language for, which the Python version
/// names: deprecated or macro codes langcodes replaces, regions and whole
/// tags it replaces, and tags of more than a language, a script and a region.
const DECLINED: &[&str] = &[
    "ja-JP-u-ca-japanese",
    "en-GB-oxendict",
    "iw-IL",
    "in",
    "x-klingon",
    "i-klingon",
    "art-lojban",
    "sgn-BE-FR",
    "en-US-x-private",
    "123",
    "de-DE-1996",
    "mo",
    "sh",
    "tl",
    "ja-999",
    "en-UK",
    "zh-yue",
    "sh-BA",
    "en-1234",
    "sgn-BR",
    "sgn_de",
    "sgn-US.UTF-8",
    "no-bok",
    "en-GB-oed",
];

fn fixture() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../records/golden/environment.json");
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

#[test]
fn a_tag_names_the_python_versions_language_or_none() {
    for case in fixture()["tags"].as_array().unwrap() {
        let tag = case["tag"].as_str().unwrap();
        let named = json!(gettext_name(tag));
        if DECLINED.contains(&tag) {
            assert_eq!(named, Value::Null, "{tag}");
            assert_ne!(
                case["expected"],
                Value::Null,
                "{tag} is declined needlessly"
            );
        } else {
            assert_eq!(named, case["expected"], "{tag}");
        }
    }
}

#[test]
fn macos_names_the_python_versions_tag() {
    for case in fixture()["macos"].as_array().unwrap() {
        let stdout = case["stdout"].as_str().unwrap();
        assert_eq!(json!(macos_tag(stdout)), case["expected"], "{stdout:?}");
    }
}

#[test]
fn the_variables_name_the_python_versions_tag() {
    for case in fixture()["posix"].as_array().unwrap() {
        let environ = case["environ"].as_object().unwrap();
        let tag = posix_tag(|name| environ.get(name).map(|v| v.as_str().unwrap().to_owned()));
        assert_eq!(json!(tag), case["expected"], "{environ:?}");
    }
}
