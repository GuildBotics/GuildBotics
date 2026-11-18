//! Ports of frozen test_ticket_selection.py and test_pull_request_patrol.py.
use super::*;
use chrono::Utc;
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
                TicketPolicy {
                    finish_merged: true,
                    repair: false,
                    work_status: None,
                    work_remaining: false,
                },
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

#[test]
fn repair_failures_resume_but_rate_limits_still_hold() {
    let now = Utc::now();
    for lane in ["ready", "in_progress"] {
        for repair in [false, true] {
            for reason in ["failed", "rate_limited"] {
                let state = WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":reason,"person_id":"aiko","retry_after_at":(now+chrono::Duration::hours(1)).to_rfc3339()})).unwrap();
                let mine = vec![TaskMessage {
                    author_type: "Assistant".into(),
                    ..Default::default()
                }];
                let d = select_ticket(
                    lane,
                    &mine,
                    Some(&state),
                    false,
                    Some("merged"),
                    TicketPolicy {
                        finish_merged: true,
                        repair,
                        work_status: None,
                        work_remaining: false,
                    },
                    now,
                );
                assert_eq!(
                    d.reason,
                    (repair && reason == "failed").then_some("working_lane")
                );
                assert!(!d.finish);
            }
        }
    }
}

#[test]
fn a_merged_repair_pr_does_not_finish_a_ticket_before_main_verification() {
    let d = select_ticket(
        "in_progress",
        &[],
        None,
        false,
        Some("merged"),
        TicketPolicy {
            finish_merged: true,
            repair: true,
            work_status: None,
            work_remaining: false,
        },
        Utc::now(),
    );
    assert!(!d.finish);
}

#[test]
fn recorded_ticket_work_controls_merge_completion_and_resume() {
    use gb_ports::WorkflowStatusReason::*;
    let mine = [TaskMessage {
        author_type: "Assistant".into(),
        ..Default::default()
    }];
    for (state, remaining) in [Completed, WorkRemaining, Asking, Blocked, AwaitingChecks]
        .into_iter()
        .flat_map(|s| [false, true].map(|r| (s, r)))
    {
        for pull in [None, Some("open"), Some("merged"), Some("closed")] {
            let d = select_ticket(
                "in_progress",
                &mine,
                None,
                false,
                pull,
                TicketPolicy {
                    finish_merged: true,
                    repair: false,
                    work_status: Some(state),
                    work_remaining: remaining,
                },
                Utc::now(),
            );
            assert_eq!(d.finish, !remaining && pull == Some("merged"));
            assert_eq!(
                d.reason,
                (state == WorkRemaining && matches!(pull, Some("merged" | "closed")))
                    .then_some("working_lane")
            );
        }
    }
}
