use gb_base::shared_writes::SharedWrites;
use gb_member::memory::{Edit, MAX_MEMORY_FILE_BYTES, Memory, Scope};
use gb_observability::redaction::Redactor;
use serde_json::{Value, json};
use std::{fs, path::Path};
fn edit(title: &str, body: &str) -> Edit {
    Edit {
        title: Some(title.into()),
        body: Some(body.into()),
        ..Default::default()
    }
}
fn memory<'a>(writes: &'a SharedWrites, root: &'a Path, redactor: &'a Redactor) -> Memory<'a> {
    Memory {
        writes,
        workspace: root,
        person_id: "aiko",
        device_id: "device-one".into(),
        autonomous: false,
        correlation: json!({}),
        audit_sink: None,
        redactor,
    }
}
#[test]
fn document_lifecycle_redaction_search_and_recency() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::new(["a-secret-value-123".into()]);
    let m = memory(&writes, d.path(), &redactor);
    let first = m
        .record(
            Scope::Personal,
            edit("First", "alpha a-secret-value-123"),
            false,
        )
        .unwrap();
    let id = first["doc_id"].as_str().unwrap();
    assert_eq!(m.get(id, None).unwrap()["body"], "alpha ***");
    let second = m
        .record(Scope::Personal, edit("Second", "βήτα"), false)
        .unwrap();
    let second_id = second["doc_id"].as_str().unwrap();
    assert_eq!(
        m.recall(&[" ".into(), "ALPHA".into()], false, 20).unwrap()["results"][0]["doc_id"],
        id
    );
    assert_eq!(
        m.recall(&["ALPHA".into()], true, 20).unwrap()["results"],
        json!([])
    );
    assert_eq!(
        m.recall(&["βΉΤΑ".into()], false, 20).unwrap()["results"][0]["doc_id"],
        second_id
    );
    m.update(
        id,
        None,
        Edit {
            body: Some("fixed".into()),
            add_keywords: vec!["fixed".into(), "fixed".into()],
            ..Default::default()
        },
        false,
    )
    .unwrap();
    assert_eq!(
        m.get(id, None).unwrap()["meta"]["keywords"],
        json!(["fixed"])
    );
    assert_eq!(m.context().unwrap()["digest"][0]["doc_id"], id);
    m.touch(second_id, None).unwrap();
    assert_eq!(m.context().unwrap()["digest"][0]["doc_id"], second_id);
    assert_eq!(
        m.promote(second_id).unwrap()["path"],
        format!("documents/team/{second_id}")
    );
    assert_eq!(
        m.archive(id, None, false).unwrap()["path"],
        format!("documents/personal/aiko/archived/{id}")
    );
    assert!(m.get(id, None).is_err());
    assert_eq!(m.context().unwrap()["digest"].as_array().unwrap().len(), 1);
    assert!(
        m.context().unwrap()["pinned"][0]["body"]
            .as_str()
            .unwrap()
            .contains("team memory")
    );
    for path in walk(d.path()) {
        if path.is_file() {
            assert!(
                !String::from_utf8_lossy(&fs::read(path).unwrap()).contains("a-secret-value-123")
            );
        }
    }
}
fn walk(path: &Path) -> Vec<std::path::PathBuf> {
    let mut paths = Vec::new();
    for e in fs::read_dir(path).unwrap() {
        let p = e.unwrap().path();
        if p.is_dir() {
            paths.extend(walk(&p));
        } else {
            paths.push(p);
        }
    }
    paths
}
#[test]
fn size_and_identifier_rejection_preserve_the_document() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let m = memory(&writes, d.path(), &redactor);
    for id in ["", ".", "..", "archived", "../other", "a\\b"] {
        assert!(m.get(id, None).is_err());
    }
    let doc = m
        .record(Scope::Team, edit("title", "original"), false)
        .unwrap();
    let id = doc["doc_id"].as_str().unwrap();
    assert!(
        m.update(
            id,
            None,
            Edit {
                title: Some("should not land".into()),
                body: Some("あ".repeat(MAX_MEMORY_FILE_BYTES / 3 + 1)),
                ..Default::default()
            },
            false
        )
        .is_err()
    );
    assert_eq!(m.get(id, None).unwrap()["body"], "original");
    assert_eq!(m.get(id, None).unwrap()["title"], "title");
    assert!(
        m.record(
            Scope::Team,
            Edit {
                params: serde_json::from_value(json!({"digest_n":1})).unwrap(),
                ..edit("title", "body")
            },
            false
        )
        .is_err()
    );
}
#[test]
fn team_creator_and_updater_and_legacy_metadata() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let m = memory(&writes, d.path(), &redactor);
    let doc = m.record(Scope::Team, edit("title", "body"), false).unwrap();
    let id = doc["doc_id"].as_str().unwrap();
    let other = Memory {
        person_id: "bob",
        ..memory(&writes, d.path(), &redactor)
    };
    other
        .update(id, None, edit("new", "new body"), false)
        .unwrap();
    let updated = m.get(id, None).unwrap();
    assert_eq!(updated["meta"]["created_by"], "aiko");
    assert_eq!(updated["meta"]["updated_by"], "bob");
    let path = d
        .path()
        .join(format!(".guildbotics/state/documents/team/{id}/meta.yml"));
    let mut meta: Value = serde_saphyr::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
    meta.as_object_mut().unwrap().remove("created_by");
    fs::write(&path, serde_saphyr::to_string(&meta).unwrap()).unwrap();
    other
        .update(id, None, edit("third", "third body"), false)
        .unwrap();
    assert!(m.get(id, None).unwrap()["meta"].get("created_by").is_none());
}
#[test]
fn policy_approval_replacement_and_autonomous_rejection() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let m = memory(&writes, d.path(), &redactor);
    let policy = || Edit {
        kind: Some("policy".into()),
        params: serde_json::from_value(json!({"digest_n":1})).unwrap(),
        ..edit("Policy", "Rules")
    };
    assert!(m.record(Scope::Personal, policy(), false).is_err());
    let first = m.record(Scope::Personal, policy(), true).unwrap();
    let id = first["doc_id"].as_str().unwrap();
    assert_eq!(m.record(Scope::Team, policy(), true).unwrap()["doc_id"], id);
    assert_eq!(m.context().unwrap()["pinned"].as_array().unwrap().len(), 2);
    assert!(
        m.update(id, None, edit("changed", "changed"), false)
            .is_err()
    );
    assert!(m.archive(id, None, false).is_err());
    let autonomous = Memory {
        autonomous: true,
        ..memory(&writes, d.path(), &redactor)
    };
    assert!(autonomous.record(Scope::Team, policy(), true).is_err());
    assert!(autonomous.archive(id, None, true).is_err());
}
#[test]
fn digest_merges_device_recency_without_relying_on_audit_retention() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let m = memory(&writes, d.path(), &redactor);
    let a = m.record(Scope::Team, edit("A", "body"), false).unwrap();
    let b = m.record(Scope::Team, edit("B", "body"), false).unwrap();
    let id = a["doc_id"].as_str().unwrap();
    let recency = d
        .path()
        .join(".guildbotics/state/documents/recency/other.json");
    fs::write(
        recency,
        serde_json::to_vec(&json!({"members":{"aiko":{id:"2099-01-01T00:00:00.000000Z"}}}))
            .unwrap(),
    )
    .unwrap();
    fs::remove_dir_all(d.path().join(".guildbotics/state/documents/memory_events")).unwrap();
    assert_eq!(m.context().unwrap()["digest"][0]["doc_id"], a["doc_id"]);
    assert_eq!(m.context().unwrap()["digest"][1]["doc_id"], b["doc_id"]);
}
#[test]
fn audit_failure_does_not_undo_committed_documents_or_updates() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let m = memory(&writes, d.path(), &redactor);
    let directory = d.path().join(".guildbotics/state/documents/memory_events");
    fs::create_dir_all(directory.parent().unwrap()).unwrap();
    fs::write(directory, "not a directory").unwrap();
    let doc = m.record(Scope::Team, edit("A", "body"), false).unwrap();
    let id = doc["doc_id"].as_str().unwrap();
    m.update(id, None, edit("B", "updated"), false).unwrap();
    assert_eq!(m.get(id, None).unwrap()["body"], "updated");
    assert_eq!(m.context().unwrap()["digest"][0]["doc_id"], id);
}
#[test]
fn audit_payloads_keep_correlation_and_emit_each_action_once() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let rows = std::sync::Mutex::new(Vec::new());
    let sink = |row| {
        rows.lock().unwrap().push(row);
        Ok(())
    };
    let m = Memory {
        correlation: json!({"trace_id":"trace-1","source":"manual","command":"workflows/demo","attributes":{"run_id":"run-1","task_run_id":"task-1"}}),
        audit_sink: Some(&sink),
        ..memory(&writes, d.path(), &redactor)
    };
    let doc = m
        .record(
            Scope::Personal,
            Edit {
                summary: Some("Summary".into()),
                source: Some(vec![json!({"type":"ticket","url":"ticket"})]),
                ..edit("Title", "Body")
            },
            false,
        )
        .unwrap();
    let id = doc["doc_id"].as_str().unwrap();
    m.touch(id, None).unwrap();
    m.update(
        id,
        None,
        Edit {
            summary: Some("New".into()),
            ..Default::default()
        },
        false,
    )
    .unwrap();
    m.get(id, None).unwrap();
    m.recall(&["Title".into()], false, 20).unwrap();
    let rows = rows.lock().unwrap();
    assert_eq!(
        rows.iter()
            .map(|r| r["type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec![
            "memory.record",
            "memory.touch",
            "memory.update",
            "memory.get",
            "memory.recall"
        ]
    );
    assert_eq!(rows[0]["trace_id"], "trace-1");
    assert_eq!(rows[0]["attributes"]["run_id"], "run-1");
    assert_eq!(rows[0]["attributes"]["memory.scope"], "personal");
    assert_eq!(
        rows[0]["payload"]["source"],
        json!([{"type":"ticket","url":"ticket"}])
    );
    assert_eq!(rows[2]["payload"]["changed_fields"], json!(["summary"]));
    assert!(rows[0]["timestamp"].as_str().unwrap().ends_with('Z'));
    assert_eq!(rows[4]["payload"]["query_keywords"], json!(["Title"]));
    assert_eq!(rows[4]["payload"]["result_count"], 1);
    assert_eq!(rows[4]["attributes"]["memory.result_count"], 1);
    assert!(rows[4]["payload"]["duration_ms"].as_f64().is_some());
    assert!(rows[4]["message"].as_str().unwrap().contains("Title"));
}
#[test]
fn busy_audit_is_best_effort_and_invalid_identity_fails_before_writing() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let busy = |_| {
        Err(gb_base::shared_writes::SharedWriteBusyError(std::path::PathBuf::from("lock")).into())
    };
    let m = Memory {
        audit_sink: Some(&busy),
        ..memory(&writes, d.path(), &redactor)
    };
    let doc = m.record(Scope::Team, edit("Title", "Body"), false).unwrap();
    let id = doc["doc_id"].as_str().unwrap();
    m.update(id, None, edit("New", "New body"), false).unwrap();
    assert_eq!(m.get(id, None).unwrap()["body"], "New body");
    let invalid = Memory {
        device_id: "../bad".into(),
        ..memory(&writes, d.path(), &redactor)
    };
    let before = walk(d.path()).len();
    assert!(
        invalid
            .record(Scope::Team, edit("Title", "Body"), false)
            .is_err()
    );
    assert_eq!(walk(d.path()).len(), before);
}
#[test]
fn concurrent_policy_records_share_one_document_without_reentrant_locking() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let barrier = std::sync::Barrier::new(4);
    std::thread::scope(|scope| {
        let mut handles = Vec::new();
        for _ in 0..4 {
            handles.push(scope.spawn(|| {
                barrier.wait();
                memory(&writes, d.path(), &redactor)
                    .record(
                        Scope::Team,
                        Edit {
                            kind: Some("policy".into()),
                            ..edit("Policy", "Rules")
                        },
                        true,
                    )
                    .unwrap()["doc_id"]
                    .clone()
            }));
        }
        let ids = handles
            .into_iter()
            .map(|h| h.join().unwrap())
            .collect::<Vec<_>>();
        assert!(ids.iter().all(|id| id == &ids[0]));
    });
}
#[test]
fn logical_paths_and_all_metadata_fields_are_redacted_on_each_device() {
    assert_eq!(
        gb_member::memory::document_path("personal\\aiko\\abc"),
        "documents/personal/aiko/abc"
    );
    assert_eq!(
        gb_member::memory::document_path("personal/aiko/abc"),
        "documents/personal/aiko/abc"
    );
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::new(["postgres://user:pass@db/example".into()]);
    let m = memory(&writes, d.path(), &redactor);
    let doc = m
        .record(
            Scope::Personal,
            Edit {
                title: Some("postgres://user:pass@db/example".into()),
                summary: Some("postgres://user:pass@db/example".into()),
                keywords: Some(vec!["postgres://user:pass@db/example".into()]),
                source: Some(vec![json!({"url":"postgres://user:pass@db/example"})]),
                ..Default::default()
            },
            false,
        )
        .unwrap();
    let full = m.get(doc["doc_id"].as_str().unwrap(), None).unwrap();
    assert_eq!(full["title"], "***");
    assert_eq!(full["summary"], "***");
    assert_eq!(full["meta"]["keywords"], json!(["***"]));
    assert_eq!(full["meta"]["source"][0]["url"], "***");
}
#[test]
fn unreadable_device_identity_prevents_service_construction_and_document_writes() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let data = d.path().join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("device.json"), "not json").unwrap();
    assert!(
        Memory::open(
            &writes,
            d.path(),
            "aiko",
            &data,
            false,
            &redactor,
            json!({})
        )
        .is_err()
    );
    assert!(!d.path().join(".guildbotics/state/documents").exists());
}

#[test]
fn audits_keep_scoped_snapshots_and_corrupt_neighbors_do_not_break_discovery() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::new([
        "four".into(),
        "five5".into(),
        "sixsix".into(),
        "seven77".into(),
    ]);
    let rows = std::sync::Mutex::new(vec![]);
    let sink = |v| {
        rows.lock().unwrap().push(v);
        Ok(())
    };
    let m = Memory {
        audit_sink: Some(&sink),
        ..memory(&writes, d.path(), &redactor)
    };
    let doc = m
        .record(
            Scope::Personal,
            edit("Title", "x four five5 sixsix seven77 y"),
            false,
        )
        .unwrap();
    let id = doc["doc_id"].as_str().unwrap();
    assert_eq!(m.get(id, None).unwrap()["body"], "x *** *** *** *** y");
    m.touch(id, None).unwrap();
    m.update(id, None, edit("Title", "new"), false).unwrap();
    m.promote(id).unwrap();
    let archived = m.archive(id, None, false).unwrap();
    let guard = rows.lock().unwrap();
    for row in guard.iter() {
        assert!(
            row["attributes"]["memory.path"]
                .as_str()
                .unwrap()
                .starts_with("documents/")
        );
    }
    let last = guard.last().unwrap();
    assert_eq!(last["attributes"]["memory.path"], archived["path"]);
    assert_eq!(last["attributes"]["memory.scope"], "team");
    assert_eq!(last["attributes"]["memory.kind"], "note");
    assert_eq!(last["message"], "memory archive: Title");
    drop(guard);
    for scope in ["team", "personal/aiko"] {
        let path = d
            .path()
            .join(format!(".guildbotics/state/documents/{scope}/broken"));
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("meta.yml"), "- a").unwrap();
    }
    let policy = || Edit {
        kind: Some("policy".into()),
        ..edit("Policy", "rules")
    };
    m.record(Scope::Team, policy(), true).unwrap();
    m.record(
        Scope::Team,
        Edit {
            summary: Some("changed".into()),
            ..policy()
        },
        true,
    )
    .unwrap();
    assert!(
        !m.context().unwrap()["pinned"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(
        !m.recall(&["Policy".into()], false, 20).unwrap()["results"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(m.get("broken", Some(Scope::Team)).is_err());
    let rows = rows.lock().unwrap();
    assert_eq!(rows.last().unwrap()["type"], "memory.recall");
    assert!(rows.iter().any(|r| {
        r["type"] == "memory.update"
            && r["payload"]["changed_fields"]
                .as_array()
                .unwrap()
                .contains(&json!("summary"))
    }));
}

#[test]
fn audit_snapshots_disambiguate_identical_document_ids() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let rows = std::sync::Mutex::new(vec![]);
    let sink = |v| {
        rows.lock().unwrap().push(v);
        Ok(())
    };
    let m = Memory {
        audit_sink: Some(&sink),
        ..memory(&writes, d.path(), &redactor)
    };
    let doc = m
        .record(Scope::Personal, edit("Personal", "body"), false)
        .unwrap();
    let id = doc["doc_id"].as_str().unwrap();
    let root = d.path().join(".guildbotics/state/documents");
    let team = root.join("team").join(id);
    fs::create_dir_all(&team).unwrap();
    fs::copy(
        root.join("personal/aiko").join(id).join("meta.yml"),
        team.join("meta.yml"),
    )
    .unwrap();
    m.update(id, Some(Scope::Team), edit("Team", "body"), false)
        .unwrap();
    m.get(id, Some(Scope::Team)).unwrap();
    let guard = rows.lock().unwrap();
    assert_eq!(guard.last().unwrap()["message"], "memory get: Team");
    assert_eq!(
        guard.last().unwrap()["attributes"]["memory.path"],
        format!("documents/team/{id}")
    );
}

#[test]
fn memory_policy_errors_digest_limit_and_query_snippets_remain_observable() {
    let d = tempfile::tempdir().unwrap();
    let writes = SharedWrites::new(d.path().into(), None);
    let redactor = Redactor::default();
    let m = memory(&writes, d.path(), &redactor);
    let policy = || Edit {
        kind: Some("policy".into()),
        params: serde_json::from_value(json!({"digest_n":1})).unwrap(),
        ..edit("Policy", "---\ndigest_n: 99\n---\nrules")
    };
    assert_eq!(
        m.record(Scope::Team, policy(), false)
            .unwrap_err()
            .to_string(),
        "cli.member.memory_policy_approval"
    );
    assert_eq!(
        m.record(
            Scope::Team,
            Edit {
                params: serde_json::from_value(json!({"digest_n":1})).unwrap(),
                ..edit("N", "B")
            },
            false
        )
        .unwrap_err()
        .to_string(),
        "cli.member.memory_set_note"
    );
    let auto = Memory {
        autonomous: true,
        ..memory(&writes, d.path(), &redactor)
    };
    assert_eq!(
        auto.record(Scope::Team, policy(), true)
            .unwrap_err()
            .to_string(),
        "cli.member.memory_policy_autonomous"
    );
    m.record(Scope::Team, policy(), true).unwrap();
    let first = m
        .record(
            Scope::Personal,
            edit("First", &format!("{}needle", "x".repeat(400))),
            false,
        )
        .unwrap();
    m.record(Scope::Personal, edit("Second", "body"), false)
        .unwrap();
    assert_eq!(m.context().unwrap()["digest"].as_array().unwrap().len(), 1);
    let hits = m
        .recall(&[" needle ".into(), " ".into()], false, 20)
        .unwrap();
    assert_eq!(
        hits["results"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v["doc_id"].clone())
            .collect::<Vec<_>>(),
        vec![first["doc_id"].clone()]
    );
    assert!(
        hits["results"][0]["snippet"]
            .as_str()
            .unwrap()
            .contains("needle")
    );
}
