use gb_base::shared_writes::SharedWrites;
use gb_member::{
    archive,
    audit::Audit,
    context::{self, PublicMember},
    events,
};
use gb_observability::{activity::ActivityEvents, redaction::Redactor};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::{Cursor, Write},
};
fn bundle(names: &[&str]) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for name in names {
        zip.start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(b"details").unwrap();
    }
    zip.finish().unwrap().into_inner()
}
#[test]
fn archive_refuses_unsafe_paths_duplicates_collisions_and_preserves_files() {
    let d = tempfile::tempdir().unwrap();
    let out = d.path().join("out");
    for name in ["../escape", "/root", "a/../../escape", ".", "a\\..\\escape"] {
        assert!(archive::extract(&bundle(&[name]), &out).is_err(), "{name}");
        assert!(!out.exists());
    }
    assert!(archive::extract(b"invalid zip", &out).is_err());
    let paths = archive::extract(&bundle(&["report.md", "nested/log.txt"]), &out).unwrap();
    assert_eq!(paths.len(), 2);
    assert_eq!(fs::read(&paths[0]).unwrap(), b"details");
    assert!(archive::extract(&bundle(&["report.md"]), &out).is_err());
    assert_eq!(fs::read(&paths[0]).unwrap(), b"details");
    let mut symlink = zip::ZipWriter::new(Cursor::new(Vec::new()));
    symlink
        .add_symlink(
            "link",
            "../escape",
            zip::write::SimpleFileOptions::default(),
        )
        .unwrap();
    assert!(archive::extract(&symlink.finish().unwrap().into_inner(), &out).is_err());
}
#[test]
fn archive_checks_the_expanded_size_and_duplicate_names_before_writing() {
    let d = tempfile::tempdir().unwrap();
    let out = d.path().join("out");
    let mut large = bundle(&["report"]);
    let header = large.windows(4).position(|w| w == b"PK\x01\x02").unwrap();
    large[header + 24..header + 28]
        .copy_from_slice(&((archive::MAX_ARTIFACT_BYTES + 1) as u32).to_le_bytes());
    let error = archive::extract(&large, &out).unwrap_err();
    let args = typed(&error, "cli.member.artifact_too_large");
    assert_eq!(args["size"], (archive::MAX_ARTIFACT_BYTES + 1).to_string());
    assert_eq!(args["limit"], archive::MAX_ARTIFACT_BYTES.to_string());
    assert!(!out.exists());
    let mut duplicate = bundle(&["one", "two"]);
    let positions = duplicate
        .windows(3)
        .enumerate()
        .filter_map(|(i, w)| (w == b"two").then_some(i))
        .collect::<Vec<_>>();
    for i in positions {
        duplicate[i..i + 3].copy_from_slice(b"one");
    }
    let error = archive::extract(&duplicate, &out).unwrap_err();
    assert_eq!(
        typed(&error, "cli.member.artifact_duplicate")["path"],
        "one"
    );
    assert!(!out.exists());
}
#[test]
fn attachment_names_fall_back_to_ids_and_never_replace_a_file() {
    let d = tempfile::tempdir().unwrap();
    for (i, name) in ["screen.png", "../screen.png", "", "a\\b", "bad:name"]
        .iter()
        .enumerate()
    {
        let target = d.path().join(i.to_string());
        let written = archive::attachment(b"png", &target, name, "screen-1").unwrap();
        assert_eq!(
            written.file_name().unwrap(),
            if i == 0 { "screen.png" } else { "screen-1" }
        );
        assert_eq!(fs::read(&written).unwrap(), b"png");
        assert!(archive::attachment(b"new", &target, name, "screen-1").is_err());
        assert_eq!(fs::read(written).unwrap(), b"png");
    }
    assert!(archive::attachment(b"png", d.path(), "../bad", "../bad").is_err());
}
#[test]
fn domain_payloads_preserve_targets_transitions_comments_and_attempt_limits() {
    let target =
        json!({"provider":"branvil","kind":"ticket","label":"T-7","title":" Title ","url":"url"});
    let inspected = events::inspected(&target, true);
    assert_eq!(inspected["type"], "target.recorded");
    assert_eq!(inspected["attributes"]["target.action"], "inspected");
    assert_eq!(
        inspected["payload"]["ticket"],
        json!({"label":"T-7","title":"Title","url":"url"})
    );
    assert_eq!(
        events::inspected(&target, false)["attributes"]["target.action"],
        ""
    );
    assert!(events::pr_created(&json!({"created":false,"target":target})).is_none());
    let pr = json!({"kind":"pull_request","url":"pr","label":"!1","title":"PR"});
    assert_eq!(
        events::pr_created(&json!({"created":true,"target":pr})).unwrap()["payload"]["pull_request"]
            ["merged"],
        false
    );
    for (state, changed, expected) in [
        ("closed", true, true),
        ("closed", false, false),
        ("open", true, false),
    ] {
        assert_eq!(
            events::ticket_closed(&json!({"target":target,"state":state,"state_changed":changed}))
                .is_some(),
            expected
        );
    }
    let comment =
        events::ticket_commented(&json!({"target":target,"id":"comment","url":"diagnostic"}));
    assert_eq!(
        comment["payload"]["comment"],
        json!({"id":"comment","url":"diagnostic"})
    );
    assert!(events::pushed(&json!({"pushed":false}), "local").is_none());
    assert_eq!(
        events::pushed(
            &json!({"pushed":true,"branch":"feature","commits":[{"sha":"a"}]}),
            "local"
        )
        .unwrap()["payload"]["ref"],
        "refs/heads/feature"
    );
    for attempt in [0, 1, 5, 6, 100] {
        assert_eq!(
            events::dispatch("event", "run", attempt, 5, None, "error", "failed")["payload"]["attempt_count"],
            attempt.min(5)
        );
    }
    assert_eq!(
        events::completed("run", 2, true, "awaiting_checks")["payload"]["status"],
        "awaiting_checks"
    );
    assert_eq!(
        events::completion_missing("run", 2, 5, "error")["attributes"]["workflow.completion"],
        "missing"
    );
}
#[test]
fn rate_limit_and_review_limit_payloads_preserve_each_field() {
    let limited = events::rate_limited(
        "run-1",
        "sub-1",
        "evt-1",
        "2026-07-04T11:44:00+09:00",
        "11:44 AM",
    );
    assert_eq!(
        limited["attributes"],
        json!({"error.category":"rate_limited","rate_limit.retry_after_at":"2026-07-04T11:44:00+09:00","rate_limit.retry_after_text":"11:44 AM"})
    );
    assert_eq!(
        limited["payload"],
        json!({"category":"rate_limited","retry_after_at":"2026-07-04T11:44:00+09:00","retry_after_text":"11:44 AM","run_id":"run-1","subject_id":"sub-1","source_event_id":"evt-1"})
    );
    let task = gb_ports::Task {
        pull_request_url: Some("pr".into()),
        title: "Title".into(),
        label: "!1".into(),
        provider: "local".into(),
        ..Default::default()
    };
    for event_type in [
        "pull_request.review_limit",
        "review_limit.failed",
        "review_limit.deferred",
        "review_limit.started",
        "review_limit.finished",
    ] {
        let event = events::review_limit(
            event_type,
            &task,
            "run",
            "comment-url",
            "CancelledError",
            "ServiceError",
        );
        assert_eq!(event["type"], event_type);
        assert_eq!(
            event["payload"]["action"],
            if event_type == "pull_request.review_limit" {
                "draft"
            } else {
                ""
            }
        );
        assert_eq!(event["payload"]["error"], "CancelledError");
        assert_eq!(event["payload"]["comment_error"], "ServiceError");
        assert_eq!(
            event["payload"]["pull_request"],
            json!({"url":"pr","label":"!1","title":"Title"})
        );
    }
}
#[test]
fn domain_recording_uses_existing_storage_redaction_and_correlation() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::new(["secret-value-123".into()]);
    let path=writes.transaction(|w|events::record(d.path(),w,events::completed("run",1,false,"done"),&json!({"person_id":"aiko","trace_id":"trace","attributes":{"message":"secret-value-123"}}),&redactor)).unwrap();
    let event: Value = serde_json::from_slice(&fs::read(path.unwrap()).unwrap()).unwrap();
    assert_eq!(event["member_id"], "aiko");
    assert_eq!(event["local_trace_id"], "trace");
    assert_eq!(event["attributes"]["message"], "***");
    let inspected = writes
        .transaction(|w| {
            events::record(
                d.path(),
                w,
                events::inspected(&json!({"kind":"ticket","url":"ticket"}), true),
                &json!({"trace_id":"trace"}),
                &redactor,
            )
        })
        .unwrap();
    assert!(inspected.is_none());
    for index in 0..21 {
        writes.transaction(|w|events::record(d.path(),w,json!({"type":"ticket.item","attributes":{"target.activity_id":format!("inside-{index}")}}),&json!({}),&redactor)).unwrap();
    }
    writes.transaction(|w|events::record(d.path(),w,json!({"type":"ticket.item","timestamp":(chrono::Utc::now()+chrono::Duration::days(3)).to_rfc3339(),"attributes":{"target.activity_id":"outside"}}),&json!({}),&redactor)).unwrap();
    let window = ActivityEvents::open(d.path())
        .list_between(
            chrono::Utc::now() - chrono::Duration::days(1),
            chrono::Utc::now() + chrono::Duration::days(1),
        )
        .unwrap();
    let ids = window
        .iter()
        .filter_map(|r| r["attributes"]["target.activity_id"].as_str())
        .collect::<Vec<_>>();
    assert_eq!(ids.len(), 21);
    assert!(!ids.contains(&"outside"));
    assert_eq!(
        ActivityEvents::open(d.path())
            .list_between(
                chrono::Utc::now() - chrono::Duration::days(1),
                chrono::Utc::now() + chrono::Duration::days(1)
            )
            .unwrap()
            .len(),
        22
    );
}
#[test]
fn audit_bounds_only_its_device_and_compares_timestamps_as_instants() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let dir = d.path().join(".guildbotics/state/documents/memory_events");
    let audit = Audit {
        writes: &writes,
        directory: &dir,
        device_id: "one",
        max_bytes: 800,
    };
    assert_eq!(audit.usage().unwrap(), (0, 800));
    for i in 0..20 {
        audit.record(json!({"kind":"memory","timestamp":"2026-01-01T09:00:00+09:00","person_id":"aiko","payload":{"index":i}})).unwrap();
    }
    let first = fs::read(audit.path().unwrap()).unwrap();
    assert!(first.len() <= 800);
    let other = Audit {
        device_id: "two",
        ..audit
    };
    other
        .record(json!({"timestamp":"2026-01-01T00:00:01Z"}))
        .unwrap();
    assert_eq!(fs::read(audit.path().unwrap()).unwrap(), first);
    let start = chrono::DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z").unwrap();
    let end = chrono::DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z").unwrap();
    assert!(
        !audit
            .list(&json!({"person_id":"aiko"}), Some(start), Some(end), None)
            .unwrap()
            .is_empty()
    );
    audit.record(json!({"timestamp":"2026-01-01T00:00:02Z","payload":{"large":"x".repeat(1000)},"attributes":{"memory.action":"get"}})).unwrap();
    let rows = audit.list(&json!({}), None, None, Some(1)).unwrap();
    assert!(rows[0]["message"].as_str().unwrap().contains("omitted"));
    let tiny = Audit {
        max_bytes: 10,
        ..audit
    };
    tiny.record(json!({"large":"x".repeat(1000)})).unwrap();
    assert!(audit.usage().unwrap().0 <= 1600);
}
fn member(kind: &str) -> PublicMember {
    PublicMember {
        person_id: "reviewer".into(),
        name: "Reviewer".into(),
        person_type: kind.into(),
        is_active: true,
        roles: json!({"architect":{"summary":"Boundaries"}}),
        profile: json!({"character":{"traits":["Careful"]}}),
        speaking_style: "Clear".into(),
        accounts: BTreeMap::new(),
    }
}
#[tokio::test]
async fn context_roster_exposes_all_members_and_only_public_configured_accounts() {
    for kind in ["human", "agent"] {
        for configured in [false, true] {
            for provider in ["branvil", "slack", "local"] {
                let mut m = member(kind);
                if configured {
                    m.accounts.insert(provider.into(), "public-account".into());
                }
                let result = context::member_context(
                    &m,
                    &[m.clone(), {
                        let mut owner = member("human");
                        owner.person_id = "owner".into();
                        owner.name = "Owner".into();
                        owner
                    }],
                    &[provider.into()],
                    json!({"digest":[],"pinned":[]}),
                    None,
                    false,
                )
                .await
                .unwrap();
                assert_eq!(result["roster"].as_array().unwrap().len(), 2);
                assert_eq!(result["roster"][1]["person_id"], "owner");
                assert_eq!(result["credential_status"], "unchecked");
                assert_eq!(result["capabilities"], context::help());
                assert_eq!(result["roster"][0]["person_type"], kind);
                let services = &result["roster"][0]["services"];
                if configured || provider == "local" {
                    assert_eq!(services.as_object().unwrap().len(), 1);
                } else {
                    assert_eq!(services, &json!({}));
                }
                if provider == "slack" && configured {
                    assert_eq!(services[provider]["mention"], "<@public-account>");
                }
                assert!(
                    result["communication_style"]["voice_basis"]
                        .as_str()
                        .unwrap()
                        .contains("Boundaries")
                );
            }
        }
    }
}

fn typed(error: &anyhow::Error, key: &str) -> std::collections::BTreeMap<String, String> {
    match error
        .downcast_ref::<gb_member::MemberError>()
        .expect("typed diagnostic")
    {
        gb_member::MemberError::CapabilityArgs { key: actual, args } => {
            assert_eq!(*actual, key);
            args.clone()
        }
        other => panic!("{other}"),
    }
}
#[test]
fn downloads_preserve_diagnostic_arguments_and_unix_colons() {
    let d = tempfile::tempdir().unwrap();
    let out = d.path().join("out");
    let error = archive::extract(&bundle(&["../outside.txt"]), &out).unwrap_err();
    assert_eq!(
        typed(&error, "cli.member.artifact_path")["path"],
        "../outside.txt"
    );
    typed(
        &archive::extract(b"not zip", &out).unwrap_err(),
        "cli.member.artifact_invalid",
    );
    let mut data = bundle(&["file"]);
    let offset = zip::ZipArchive::new(Cursor::new(data.clone()))
        .unwrap()
        .by_index(0)
        .unwrap()
        .data_start() as usize;
    data[offset] ^= 0xff;
    typed(
        &archive::extract(&data, &out).unwrap_err(),
        "cli.member.artifact_invalid",
    );
    let target = archive::attachment(b"mine", d.path(), "screen.png", "id").unwrap();
    let args = typed(
        &archive::attachment(b"new", d.path(), "screen.png", "id").unwrap_err(),
        "cli.member.attachment_exists",
    );
    assert_eq!(args["path"], target.display().to_string());
    assert_eq!(fs::read(target).unwrap(), b"mine");
    #[cfg(not(windows))]
    assert!(archive::extract(&bundle(&["logs/12:00:00.txt"]), &d.path().join("colon")).is_ok());
}
#[test]
fn empty_attributes_cannot_erase_correlated_targets() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let path=writes.transaction(|w|events::record(d.path(),w,json!({"type":"pull_request.review_limit","attributes":{"target.title":"","target.url":null,"target.action":false}}),&json!({"attributes":{"target.title":"Fix login","target.url":"U"}}),&redactor)).unwrap().unwrap();
    let result: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert_eq!(
        result["attributes"],
        json!({"target.title":"Fix login","target.url":"U"})
    );
}

#[test]
fn mapped_domain_and_roster_conditions_are_complete() {
    for kind in ["ticket", "pull_request"] {
        let target =
            json!({"kind":kind,"provider":"local","url":"U","label":"L","title":" Title "});
        for read_only in [false, true] {
            let event = events::inspected(&target, read_only);
            assert_eq!(
                event["payload"][kind],
                json!({"url":"U","label":"L","title":"Title"})
            );
            assert_eq!(event["attributes"]["target.kind"], kind);
            assert_eq!(
                event["attributes"]["target.action"],
                if read_only { "inspected" } else { "" }
            );
        }
        for action in ["opened", "closed"] {
            let event = events::target(&target, action, None);
            assert_eq!(event["type"], format!("{kind}.item"));
            assert_eq!(
                event["payload"],
                json!({"action":action,kind:{"url":"U","label":"L","title":"Title"}})
            );
        }
    }
    let target = json!({"kind":"ticket","provider":"local","url":"U","label":"L","title":"Title"});
    let opened = events::target(&target, "opened", None);
    assert_eq!(opened["type"], "ticket.item");
    assert_eq!(
        opened["payload"],
        json!({"action":"opened","ticket":{"url":"U","label":"L","title":"Title"}})
    );
    let closed =
        events::ticket_closed(&json!({"target":target,"state":"closed","state_changed":true}))
            .unwrap();
    assert_eq!(closed["payload"]["action"], "closed");
    assert_eq!(closed["payload"]["ticket"]["title"], "Title");
    let created=events::pr_created(&json!({"created":true,"target":{"kind":"pull_request","url":"PR","title":"Title","label":"!1"}})).unwrap();
    assert_eq!(created["type"], "pull_request.item");
    assert_eq!(
        created["payload"],
        json!({"action":"opened","pull_request":{"url":"PR","title":"Title","label":"!1","merged":false}})
    );
    let mut a = member("agent");
    a.accounts.insert("branvil".into(), "  AIKO  ".into());
    let mut b = member("human");
    b.person_id = "owner".into();
    b.accounts.insert("branvil".into(), "   ".into());
    for (entry, local, branvil) in [
        (
            a.roster_entry(&["local".into(), "branvil".into()]),
            "local-reviewer",
            Some("aiko"),
        ),
        (
            b.roster_entry(&["local".into(), "branvil".into()]),
            "local-owner",
            None,
        ),
    ] {
        assert_eq!(
            entry["services"]["local"],
            json!({"names":[local],"mention":format!("@{local}")})
        );
        if let Some(name) = branvil {
            assert_eq!(
                entry["services"]["branvil"],
                json!({"names":[name],"mention":format!("@{name}")})
            );
        } else {
            assert!(entry["services"].get("branvil").is_none());
        }
    }
    let d = tempfile::tempdir().unwrap();
    for (i, name) in ["..", " ", "a\nb", "a\u{8}b.png"].iter().enumerate() {
        assert_eq!(
            archive::attachment(b"x", &d.path().join(i.to_string()), name, "id")
                .unwrap()
                .file_name()
                .unwrap(),
            "id"
        );
    }
}
#[test]
fn audit_filters_and_trimming_preserve_other_journals_and_unwritable_rows() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let dir = d.path().join(".guildbotics/state/documents/memory_events");
    let audit = Audit {
        writes: &writes,
        directory: &dir,
        device_id: "one",
        max_bytes: 1000,
    };
    let other = Audit {
        device_id: "two",
        ..audit
    };
    other.record(json!({"message":"keep"})).unwrap();
    let original = fs::read(other.path().unwrap()).unwrap();
    for _ in 0..30 {
        audit.record(json!({"type":"memory.record","message":"Find This","payload":{"source":[{"url":"https://GITHUB.example/1"}]}})).unwrap();
    }
    assert_eq!(fs::read(other.path().unwrap()).unwrap(), original);
    assert!(
        !audit
            .list(&json!({"source":"github"}), None, None, None)
            .unwrap()
            .is_empty()
    );
    assert!(
        !audit
            .list(&json!({"query":"find this"}), None, None, None)
            .unwrap()
            .is_empty()
    );
    let before = fs::read(audit.path().unwrap()).unwrap();
    let tiny = Audit {
        max_bytes: 1,
        ..audit
    };
    tiny.record(json!({"message":"x".repeat(100)})).unwrap();
    assert_eq!(fs::read(audit.path().unwrap()).unwrap(), before);
}
