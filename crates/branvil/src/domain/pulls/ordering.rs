//! Display order from the same landing, review, and head-check rules as admission.
use super::*;
use crate::landing::{HeadChecks, LandingView};

pub(super) async fn rank(app: &App, repo: &Repo, p: &Pull, tips: &Tips) -> AppResult<[u64; 3]> {
    if p.state != "open" || p.draft {
        return Ok([if p.state != "open" { 9 } else { 8 }, 0, 0]);
    }
    let head = head_of(p, tips).unwrap_or_default();
    let base = tips.get(&p.base).cloned().unwrap_or_default();
    let checks = display_checks(app, repo, p, &head).await?;
    let mut required = crate::landing::head_checks(repo, &checks.checks);
    let ci = ci_rank(&checks);
    let landing = crate::landing::view(app, repo, p.number, tips).await?;
    if let Some(l) = &landing
        && crate::landing::failure_stands(app, repo, p, &l.entry, &head, &base).await?
    {
        required = HeadChecks::Failed;
    }
    let approved = approvals(&read_review_states(app, p, &head).await?) >= repo.required_approvals;
    let own = p.clone();
    let waiting_on_author = app
        .store
        .read(move |r| Ok(!unanswered(r, &own, "")?.is_empty()))
        .await?;
    Ok(order(
        &head,
        &base,
        approved,
        waiting_on_author,
        required,
        ci,
        landing.as_ref(),
    ))
}

fn ci_rank(checks: &crate::ci::Checks) -> u64 {
    if checks.checks.iter().any(|c| {
        c.status == "completed"
            && matches!(
                c.conclusion.as_deref(),
                Some("failure" | "cancelled" | "timed_out" | "startup_failure")
            )
    }) {
        3
    } else if checks.checks.is_empty() && checks.checks_expected {
        2
    } else if checks.checks.iter().any(|c| c.status != "completed") {
        1
    } else {
        0
    }
}

fn order(
    head: &str,
    base: &str,
    approved: bool,
    unanswered: bool,
    required: HeadChecks,
    ci: u64,
    landing: Option<&LandingView>,
) -> [u64; 3] {
    if let Some(l) = landing.filter(|l| {
        approved
            && !unanswered
            && required == HeadChecks::Passed
            && l.entry.head_sha == head
            && l.entry.base_sha == base
            && l.entry.state != "left"
    }) {
        let position = l.position.unwrap_or(u64::MAX);
        if position > 1 {
            return [3, position, ci];
        }
        if let Some(first) = &l.first {
            let stage = match first.next.as_str() {
                "following" | "owner_merge_host" | "auto_merge" | "owner_merge" => 0,
                "publishing" | "owner_publish" | "host_checks" => 1,
                "gate" => 2,
                _ => 7,
            };
            return [stage, 0, ci];
        }
    }
    let stage = if head.is_empty()
        || base.is_empty()
        || unanswered
        || matches!(required, HeadChecks::Failed)
    {
        7
    } else if !approved {
        6
    } else if matches!(required, HeadChecks::Running) {
        5
    } else {
        4
    };
    [stage, 0, ci]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn landing(next: &str, position: u64) -> LandingView {
        LandingView {
            entry: serde_json::from_value(serde_json::json!({
                "repo":"r", "number":1, "base":"main", "queued_at":"now", "sequence":1,
                "state":"passed", "state_at":"now", "gate":"head", "base_sha":"base", "head_sha":"head",
                "reason":null, "notified_at":null, "notify":false, "resume_publish":false, "error":null
            })).unwrap(),
            title: "Feature".into(), pull_state: "open".into(), draft: false, position: Some(position),
            first: Some(crate::landing::LandingFirst {
                number: 1, state: "passed".into(), state_at: "now".into(), error: None,
                next: next.into(), host_url: None, next_detail: None,
            }),
        }
    }

    #[test]
    fn stages_follow_remaining_work_and_queue_position() {
        for (next, expected) in [
            ("following", 0),
            ("owner_merge_host", 0),
            ("auto_merge", 0),
            ("owner_merge", 0),
            ("publishing", 1),
            ("owner_publish", 1),
            ("host_checks", 1),
            ("gate", 2),
            ("owner_fix_following", 7),
            ("owner_fix_auto", 7),
            ("host_closed", 7),
        ] {
            assert_eq!(
                order(
                    "head",
                    "base",
                    true,
                    false,
                    HeadChecks::Passed,
                    1,
                    Some(&landing(next, 1))
                ),
                [expected, 0, 1],
                "{next}"
            );
        }
        assert!(
            order(
                "head",
                "base",
                true,
                false,
                HeadChecks::Passed,
                3,
                Some(&landing("gate", 2))
            ) < order(
                "head",
                "base",
                true,
                false,
                HeadChecks::Passed,
                0,
                Some(&landing("gate", 3))
            )
        );
        for (approved, unanswered, checks, stage) in [
            (true, false, HeadChecks::Passed, 4),
            (true, false, HeadChecks::Running, 5),
            (false, false, HeadChecks::Passed, 6),
            (false, false, HeadChecks::Running, 6),
            (true, true, HeadChecks::Passed, 7),
            (true, false, HeadChecks::Failed, 7),
        ] {
            assert_eq!(
                order("head", "base", approved, unanswered, checks, 0, None),
                [stage, 0, 0]
            );
        }
    }

    #[test]
    fn stale_entries_and_new_blockers_cannot_keep_a_ready_position() {
        let mut l = landing("owner_merge", 1);
        for (head, base) in [("new-head", "base"), ("head", "new-base")] {
            assert_eq!(
                order(head, base, true, false, HeadChecks::Passed, 0, Some(&l))[0],
                4
            );
        }
        assert_eq!(
            order(
                "head",
                "base",
                false,
                false,
                HeadChecks::Passed,
                0,
                Some(&l)
            )[0],
            6
        );
        assert_eq!(
            order("head", "base", true, true, HeadChecks::Passed, 0, Some(&l))[0],
            7
        );
        assert_eq!(
            order("head", "base", true, false, HeadChecks::Failed, 3, Some(&l))[0],
            7
        );
        l.entry.state = "left".into();
        assert_eq!(
            order("head", "base", true, false, HeadChecks::Passed, 0, Some(&l))[0],
            4
        );
        assert_eq!(
            order("", "base", true, false, HeadChecks::Passed, 0, None)[0],
            7
        );
    }

    #[test]
    fn ci_breaks_ties_without_treating_missing_checks_as_success() {
        let mut checks = crate::ci::Checks {
            run_registered: true,
            checks: vec![],
            checks_expected: false,
        };
        assert_eq!(ci_rank(&checks), 0);
        checks.checks_expected = true;
        assert_eq!(ci_rank(&checks), 2);
        checks.checks.push(Check {
            status: "queued".into(),
            ..Check::default()
        });
        assert_eq!(ci_rank(&checks), 1);
        checks.checks.push(Check {
            status: "completed".into(),
            conclusion: Some("failure".into()),
            ..Check::default()
        });
        assert_eq!(ci_rank(&checks), 3);
    }
}
