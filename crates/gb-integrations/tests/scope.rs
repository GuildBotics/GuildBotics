use gb_integrations::*;
use gb_ports::{Check, ReadinessRollup};

#[test]
fn log_failures_and_rollup_failures_are_distinct_populations() {
    let failures = [
        "action_required",
        "cancelled",
        "failure",
        "startup_failure",
        "stale",
        "timed_out",
    ];
    for conclusion in failures
        .into_iter()
        .chain(["error", "success", "skipped", "neutral", "unknown", ""])
    {
        assert_eq!(
            failed_conclusion(conclusion),
            failures.contains(&conclusion),
            "{conclusion}"
        );
        for status in ["queued", "in_progress", "completed"] {
            let checks = [Check {
                name: "ci".into(),
                status: status.into(),
                conclusion: Some(conclusion.into()),
                details_url: String::new(),
                details: None,
            }];
            if failures.contains(&conclusion) || conclusion == "error" {
                assert_eq!(check_rollup(&checks), ReadinessRollup::Failure);
            }
        }
    }
}

#[test]
fn writes_are_direct_children_of_the_configured_namespace() {
    for namespace in ["GuildBotics", "group/nested", "a-b.c_d"] {
        let scope = WriteScope::new(namespace);
        for leaf in ["repository", "a-b.c_d", "...", "0"] {
            let path = format!("{namespace}/{leaf}");
            let allowed = scope.authorize(&path).unwrap();
            assert_eq!(allowed.path(), path);
            assert_eq!(allowed.namespace(), namespace);
            assert!(scope.authorize(&path.to_uppercase()).is_ok());
            for outside in [
                format!("other/{leaf}"),
                format!("{namespace}/child/{leaf}"),
                format!("{namespace}-other/{leaf}"),
            ] {
                assert!(scope.authorize(&outside).is_err(), "{outside}");
            }
        }
    }
}

#[test]
fn malformed_paths_cannot_cross_the_write_boundary() {
    let scope = WriteScope::new("group");
    for path in [
        "group",
        "",
        "/group/repo",
        "group/repo/",
        "group//repo",
        "group/../repo",
        "group/./repo",
        "group/..",
        "group/.",
        "group/%2e%2e",
        "group/repo?x",
        "group/repo#x",
        "group/repo\\other",
        "group/日本",
        "group/repo\n",
        "group/repo\0",
        "https://host/group/repo",
    ] {
        assert!(scope.authorize(path).is_err(), "{path:?}");
        assert!(!valid_repository_path(path), "{path:?}");
    }
    assert!(WriteScope::new("").authorize("group/repo").is_err());
    for namespace in [
        "/group",
        "group/",
        ".",
        "..",
        "group//nested",
        "group/..",
        "日本",
        "group\n",
    ] {
        assert!(WriteScope::new(namespace).authorize("group/repo").is_err());
    }
}

#[test]
fn unicode_namespace_comparison_matches_python_casefold() {
    for (namespace, path) in [("K", "k/repo"), ("ſ", "s/repo"), ("Straße", "strasse/repo")] {
        assert!(WriteScope::new(namespace).authorize(path).is_ok());
        assert!(
            WriteScope::new(namespace)
                .authorize(&format!("{path}/child"))
                .is_err()
        );
    }
}

#[test]
fn every_ascii_character_is_checked_in_every_path_segment() {
    let scope = WriteScope::new("group");
    for byte in 0u8..=127 {
        let c = char::from(byte);
        let valid = c.is_ascii_alphanumeric() || "_.-".contains(c);
        let leaf = format!("group/a{c}b");
        let parent = format!("a{c}b/repo");
        assert_eq!(valid_repository_path(&leaf), valid || c == '/', "{byte}");
        assert_eq!(valid_repository_path(&parent), valid || c == '/', "{byte}");
        assert_eq!(scope.authorize(&leaf).is_ok(), valid, "{byte}");
    }
}
