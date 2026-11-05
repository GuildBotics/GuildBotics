//! Ports of frozen test_ticket_selection.py and test_pull_request_patrol.py.
use chrono::Utc;
use gb_integrations::*;
use gb_ports::*;
use serde_json::json;

#[test]
fn working_conversation_precedes_linked_pr_and_holds_win() {
    for pull in [None, Some("open"), Some("merged")] {
        for (conversation, expected) in [
            ("silent", Some("working_lane")),
            ("mine", None),
            ("other", Some("issue_comment")),
            ("mention", Some("issue_mention")),
            ("failed", None),
        ] {
            let since = match conversation {
                "mine" | "failed" => vec![TaskMessage {
                    author_type: "Assistant".into(),
                    ..Default::default()
                }],
                "other" => vec![TaskMessage::default()],
                _ => vec![],
            };
            let held = WorkflowStatus::from_payload(
                &json!({"kind":"workflow_error","reason":"failed","person_id":"aiko"}),
            );
            let decision = select_ticket(
                "in_progress",
                &since,
                if conversation == "failed" {
                    held.as_ref()
                } else {
                    None
                },
                conversation == "mention" || conversation == "failed",
                pull,
                true,
                Utc::now(),
            );
            assert_eq!(
                decision.reason,
                if conversation == "silent" && pull.is_some() {
                    None
                } else {
                    expected
                },
                "{conversation}/{pull:?}"
            );
            assert_eq!(
                decision.finish,
                (conversation == "mine" || conversation == "silent") && pull == Some("merged")
            );
        }
    }
}

#[test]
fn assigned_history_and_mentions_preserve_unparseable_evidence() {
    let expected = parse_timestamp("2026-01-02T00:00:00Z").unwrap();
    for value in [
        "2026-01-02T00:00",
        "2026-01-02T00:00+00:00",
        "20260102",
        "20260102T0000",
        "2026-W01-5T00:00",
        "2026W015T0000",
        "2026-01-02🦀00:00",
    ] {
        assert_eq!(parse_timestamp(value), Some(expected), "{value}");
    }
    let history = vec![
        TaskMessage {
            timestamp: "2026-01-01T00:00:00Z".into(),
            author_type: "Assistant".into(),
            ..Default::default()
        },
        TaskMessage {
            timestamp: "broken".into(),
            ..Default::default()
        },
    ];
    assert_eq!(since_assignment(&history, "2026-01-02T00:00:00Z").len(), 1);
    assert_eq!(since_assignment(&history, "broken").len(), 2);
    assert_eq!(since_assignment(&history, " 2026-01-02T00:00:00 ").len(), 1);
    assert!(!mention_pending(
        true,
        history.iter().take(1).map(|m| (m, true))
    ));
    assert!(mention_pending(false, history.iter().map(|m| (m, true))));
    assert_eq!(
        relevant_comments(history.iter().map(|m| (m, true))).len(),
        1
    );
}

#[test]
fn patrol_reviews_statements_individually_and_limits_only_new_heads() {
    let mut pr = PatrolPullRequest {
        open: true,
        author: "aiko".into(),
        head_oid: "head4".into(),
        ..Default::default()
    };
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
    pr.reviews.push(PatrolReview {
        author: "yuki".into(),
        body: "approved with advice".into(),
        submitted_at: "2".into(),
        ..Default::default()
    });
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_feedback")
    );
    pr.reviews[0].reactors.insert("aiko".into());
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
    pr.author = "owner".into();
    pr.reviews.clear();
    for n in 1..=3 {
        pr.reviews.push(PatrolReview {
            author: "aiko".into(),
            commit_oid: format!("head{n}"),
            submitted_at: format!("{n}"),
            ..Default::default()
        });
    }
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_review_limit")
    );
    pr.requested_reviewers.insert("aiko".into());
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_review")
    );
    pr.requested_reviewers.clear();
    pr.head_oid = "head1".into();
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
    pr.comments.push(PatrolComment {
        author: "owner".into(),
        created_at: "4".into(),
        ..Default::default()
    });
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_review")
    );
    pr.is_draft = true;
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
}

#[test]
fn linked_pull_timestamp_ties_keep_the_first_observed_state() {
    for states in [["merged", "closed"], ["closed", "merged"]] {
        assert_eq!(
            linked_pull_state(states.iter().map(|s| (*s, "same"))),
            Some(states[0])
        );
    }
    assert_eq!(
        linked_pull_state([("merged", "later"), ("open", "earlier")]),
        Some("open")
    );
    assert_eq!(
        linked_pull_state([("closed", "earlier"), ("merged", "later")]),
        Some("merged")
    );
    assert_eq!(linked_pull_state([]), None);
}
