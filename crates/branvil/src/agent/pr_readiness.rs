use gb_ports::{Check, Feedback, MAX_PAGE_BYTES, Readiness, Remark, Remarks, ServiceError};
use serde_json::{Value, json};

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

pub fn effective_tail_bytes(requested: usize, jobs: usize) -> Result<usize, ServiceError> {
    if jobs == 0 {
        return Err(ServiceError::InvalidParameters);
    }
    Ok(requested.min((MAX_PAGE_BYTES / jobs).max(1)))
}

/// A remark as a provider read it: its author, opaque id, and time. Authors
/// are spelled as the provider compares actors; times as the provider orders them.
/// A notice is a comment carrying a workflow status, which nobody answers.
pub struct Said {
    pub author: String,
    pub id: String,
    pub at: String,
    pub notice: bool,
}

/// Count each kind of remark not made by the PR's `author` and not a notice,
/// and pick the latest by time; a tie goes to the one the provider listed last.
pub fn feedback_of(
    author: &str,
    reviews: Vec<Said>,
    comments: Vec<Said>,
    thread_comments: Vec<Said>,
) -> Feedback {
    let remarks = |said: Vec<Said>| {
        let others: Vec<_> = said
            .into_iter()
            .filter(|s| s.author != author && !s.notice)
            .collect();
        Remarks {
            count: others.len() as i64,
            latest: others
                .into_iter()
                .max_by(|a, b| a.at.cmp(&b.at))
                .map(|s| Remark { id: s.id, at: s.at }),
        }
    };
    Feedback {
        reviews: remarks(reviews),
        comments: remarks(comments),
        thread_comments: remarks(thread_comments),
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

pub fn fit_log_tails(result: &mut Readiness) -> Result<(), ServiceError> {
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
