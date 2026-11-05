use gb_ports::{
    Check, CompletionBlocker, FailedLog, MAX_PAGE_BYTES, Readiness, ReadinessReadiness,
    ReadinessRollup, ServiceError, WorkTarget,
};
use serde_json::{Value, json};

/// The comparison and the subsequent observation of an open PR's base.
/// Closed PRs have no freshness observation: completion does not apply.
pub struct Freshness {
    pub base_sha: String,
    pub behind_by: i64,
    pub current_base_sha: String,
    pub requires_up_to_date: bool,
}

/// Conclusions whose jobs provide failure logs. A provider's synthetic
/// `error` affects rollup, but does not identify a job whose log can be read.
pub fn failed_conclusion(conclusion: &str) -> bool {
    matches!(
        conclusion,
        "action_required" | "cancelled" | "failure" | "startup_failure" | "stale" | "timed_out"
    )
}

/// Prefer primary failures over cancellation fallout when fetching logs.
pub fn inspected(checks: &[Check]) -> Vec<&Check> {
    let primary: Vec<_> = checks
        .iter()
        .filter(|c| {
            matches!(
                c.conclusion.as_deref(),
                Some("action_required" | "failure" | "startup_failure" | "timed_out")
            )
        })
        .collect();
    if primary.is_empty() {
        checks.iter().collect()
    } else {
        primary
    }
}

pub fn check_rollup(checks: &[Check]) -> ReadinessRollup {
    if checks.is_empty() {
        ReadinessRollup::NoChecks
    } else if checks.iter().any(|c| {
        c.conclusion
            .as_deref()
            .is_some_and(|s| failed_conclusion(s) || s == "error")
    }) {
        ReadinessRollup::Failure
    } else if checks.iter().all(|c| {
        c.status == "completed"
            && matches!(
                c.conclusion.as_deref(),
                Some("success" | "neutral" | "skipped")
            )
    }) {
        ReadinessRollup::Success
    } else {
        ReadinessRollup::Pending
    }
}

pub fn effective_tail_bytes(requested: usize, jobs: usize) -> Result<usize, ServiceError> {
    if jobs == 0 {
        return Err(ServiceError::InvalidParameters);
    }
    Ok(requested.min((MAX_PAGE_BYTES / jobs).max(1)))
}

/// Checks belong to `head_sha`; callers re-read the PR after collecting them.
/// No observation of another head or base is accepted as completion evidence.
pub fn readiness_of(
    target: WorkTarget,
    head_sha: String,
    current_head_sha: String,
    checks: Vec<Check>,
    checks_expected: bool,
    freshness: Option<Freshness>,
    failed_logs: Option<Vec<FailedLog>>,
) -> Result<Readiness, ServiceError> {
    let rollup = check_rollup(&checks);
    let mut result = Readiness {
        target,
        base_sha: None,
        head_sha,
        behind_by: None,
        out_of_date: None,
        current_base_sha: None,
        current_head_sha,
        rollup,
        checks,
        checks_expected: false,
        readiness: ReadinessReadiness::NotApplicable,
        completion_blockers: vec![],
        failed_logs,
    };
    if let Some(f) = freshness {
        if f.behind_by < 0 {
            return Err(ServiceError::InvalidResult);
        }
        let b = &mut result.completion_blockers;
        if f.requires_up_to_date && f.behind_by > 0 {
            b.push(blocker(
                "base_out_of_date",
                format!("The PR head is {} commit(s) behind its base.", f.behind_by),
                "Merge or rebase the base branch, push, and check again.",
            ));
        }
        if result.current_head_sha != result.head_sha {
            b.push(blocker(
                "head_changed",
                "The PR head changed while readiness was being checked.",
                "Check the new head SHA and its CI results again.",
            ));
        }
        if f.current_base_sha != f.base_sha {
            b.push(blocker(
                "base_changed",
                "The PR base changed while readiness was being checked.",
                "Check freshness against the new base SHA again.",
            ));
        }
        match rollup {
            ReadinessRollup::Failure => b.push(blocker(
                "checks_failed",
                "CI checks are failing.",
                "Inspect failed logs, fix the failures, and check again.",
            )),
            ReadinessRollup::Pending => b.push(blocker(
                "checks_pending",
                "CI checks are still pending.",
                "Wait for every check to finish, then check again.",
            )),
            ReadinessRollup::NoChecks if checks_expected => b.push(blocker(
                "checks_pending",
                "CI checks have not been registered for the PR head yet.",
                "Wait for CI to start, then check again.",
            )),
            _ => (),
        }
        result.readiness = if b.is_empty() {
            ReadinessReadiness::Ready
        } else {
            ReadinessReadiness::Blocked
        };
        result.base_sha = Some(f.base_sha);
        result.behind_by = Some(f.behind_by);
        result.out_of_date = Some(f.behind_by > 0);
        result.current_base_sha = Some(f.current_base_sha);
        result.checks_expected = checks_expected;
    }
    fit_log_tails(&mut result)?;
    Ok(result)
}

fn blocker(code: &str, message: impl Into<String>, next_action: &str) -> CompletionBlocker {
    CompletionBlocker {
        code: code.into(),
        message: message.into(),
        next_action: next_action.into(),
    }
}

/// Size as Python's json.dumps (ASCII escapes and spaces), preserving the
/// frozen guest's page budget even when a Rust transport uses compact UTF-8.
pub fn page_bytes(result: &Readiness) -> Result<usize, ServiceError> {
    let page = json!({"items": [result], "continuation": null, "target": result.target});
    json_bytes(&page)
}

fn json_bytes(value: &Value) -> Result<usize, ServiceError> {
    Ok(match value {
        Value::String(s) => string_bytes(s),
        Value::Array(a) => {
            2 + a
                .iter()
                .map(json_bytes)
                .collect::<Result<Vec<_>, _>>()?
                .iter()
                .sum::<usize>()
                + a.len().saturating_sub(1) * 2
        }
        Value::Object(o) => {
            2 + o
                .iter()
                .map(|(k, v)| Ok(string_bytes(k) + 2 + json_bytes(v)?))
                .collect::<Result<Vec<usize>, ServiceError>>()?
                .iter()
                .sum::<usize>()
                + o.len().saturating_sub(1) * 2
        }
        _ => serde_json::to_vec(value)
            .map_err(|_| ServiceError::InvalidResult)?
            .len(),
    })
}

fn string_bytes(text: &str) -> usize {
    2 + text
        .chars()
        .map(|c| match c {
            '"' | '\\' | '\n' | '\r' | '\t' | '\u{8}' | '\u{c}' => 2,
            '\u{0}'..='\u{1f}' => 6,
            ' '..='~' => 1,
            c => 6 * c.len_utf16(),
        })
        .sum::<usize>()
}

fn fit_log_tails(result: &mut Readiness) -> Result<(), ServiceError> {
    let Some(logs) = result.failed_logs.as_mut().filter(|l| !l.is_empty()) else {
        return Ok(());
    };
    let tails: Vec<_> = logs
        .iter_mut()
        .map(|l| {
            l.log_bytes = l.log.len() as i64;
            std::mem::take(&mut l.log)
        })
        .collect();
    let budget = MAX_PAGE_BYTES
        .checked_sub(page_bytes(result)?)
        .ok_or(ServiceError::ResponseTooLarge)?
        / tails.len();
    for (log, tail) in result
        .failed_logs
        .as_mut()
        .expect("logs exist")
        .iter_mut()
        .zip(tails)
    {
        let boundaries: Vec<_> = tail
            .char_indices()
            .map(|(i, _)| i)
            .chain(std::iter::once(tail.len()))
            .collect();
        let count = boundaries.len() - 1;
        let (mut low, mut high) = (0, count);
        while low < high {
            let length = (low + high).div_ceil(2);
            if string_bytes(&tail[boundaries[count - length]..]) - 2 <= budget {
                low = length;
            } else {
                high = length - 1;
            }
        }
        log.log = tail[boundaries[count - low]..].into();
        log.log_bytes = log.log.len() as i64;
        if low < count {
            log.tail_limit_bytes = log.tail_limit_bytes.min(log.log_bytes);
            log.truncated = true;
        }
    }
    if page_bytes(result)? > MAX_PAGE_BYTES {
        return Err(ServiceError::ResponseTooLarge);
    }
    Ok(())
}
