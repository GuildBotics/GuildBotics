//! Display order by remaining work, with author replies breaking ties.
use super::*;
use crate::landing::HeadChecks;
pub(super) async fn rank(app: &App, repo: &Repo, p: &Pull, tips: &Tips) -> AppResult<[u64; 3]> {
    if p.state != "open" {
        return Ok([7, 0, 0]);
    }
    let head = head_of(p, tips).unwrap_or_default();
    let base = base_of(p, tips).unwrap_or_default();
    let checks = display_checks(app, repo, p, &head, &base).await?;
    let names = app.ci.pull_required(repo, &head, &base).await?.names;
    let required = crate::landing::head_checks(&names, &checks.checks);
    let l = crate::landing::view(app, repo, p.number, tips).await?;
    let Some(l) = l else {
        return Ok([7, 0, 0]);
    };
    Ok(order(&l.next, &l.blockers, required, ci_rank(&checks)))
}
fn order(
    next: &str,
    blockers: &[crate::model::LandingReason],
    required: HeadChecks,
    ci: u64,
) -> [u64; 3] {
    let has = |code| blockers.iter().any(|b| b.code == code);
    let stage = if has("draft") {
        6
    } else {
        match next {
            "following" | "owner_merge_host" | "auto_merge" | "owner_merge" => 0,
            "publishing" | "owner_publish" | "host_checks" => 1,
            "conditions" => {
                if [
                    "conflict",
                    "host_failed",
                    "already_in_base",
                    "publish_requirements",
                ]
                .iter()
                .any(|code| has(code))
                    || required == HeadChecks::Failed
                {
                    5
                } else if has("approvals") || has("required_removal") {
                    4
                } else if has("head_checks") {
                    3
                } else {
                    2
                }
            }
            _ => 5,
        }
    };
    [stage, u64::from(has("unanswered")), ci]
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

#[cfg(test)]
mod tests {
    use super::*;
    fn reasons(codes: &[&str]) -> Vec<crate::model::LandingReason> {
        codes
            .iter()
            .map(|code| crate::model::LandingReason {
                count: None,
                required: None,
                held: None,
                code: (*code).into(),
                message: String::new(),
                jobs: vec![],
                files: vec![],
            })
            .collect()
    }
    #[test]
    fn stages_follow_remaining_work_and_author_replies_break_ties() {
        for (next, codes, checks, stage) in [
            ("following", vec![], HeadChecks::Passed, 0),
            ("owner_merge_host", vec![], HeadChecks::Passed, 0),
            ("owner_merge", vec![], HeadChecks::Passed, 0),
            ("auto_merge", vec![], HeadChecks::Passed, 0),
            ("publishing", vec![], HeadChecks::Passed, 1),
            ("owner_publish", vec![], HeadChecks::Passed, 1),
            ("host_checks", vec![], HeadChecks::Passed, 1),
            ("conditions", vec!["checking"], HeadChecks::Passed, 2),
            ("conditions", vec![], HeadChecks::Passed, 2),
            ("conditions", vec!["head_checks"], HeadChecks::Running, 3),
            ("conditions", vec!["approvals"], HeadChecks::Passed, 4),
            (
                "conditions",
                vec!["approvals", "head_checks"],
                HeadChecks::Running,
                4,
            ),
            ("conditions", vec!["conflict"], HeadChecks::Passed, 5),
            ("conditions", vec!["host_failed"], HeadChecks::Passed, 5),
            ("conditions", vec!["already_in_base"], HeadChecks::Passed, 5),
            (
                "conditions",
                vec!["publish_requirements"],
                HeadChecks::Passed,
                5,
            ),
            (
                "conditions",
                vec!["head_checks", "approvals"],
                HeadChecks::Failed,
                5,
            ),
            ("owner_fix_auto", vec![], HeadChecks::Passed, 5),
            ("owner_fix_following", vec![], HeadChecks::Passed, 5),
            ("host_closed", vec![], HeadChecks::Passed, 5),
            (
                "conditions",
                vec!["draft", "conflict"],
                HeadChecks::Failed,
                6,
            ),
        ] {
            assert_eq!(order(next, &reasons(&codes), checks, 3), [stage, 0, 3]);
            let mut waiting = codes;
            waiting.push("unanswered");
            assert_eq!(order(next, &reasons(&waiting), checks, 3), [stage, 1, 3]);
        }
    }
}
