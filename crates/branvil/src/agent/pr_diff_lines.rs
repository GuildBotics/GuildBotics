//! Unified diff coordinates shared by code hosts.
use gb_base::service::ServiceError;
use serde_json::{Value, json};
use std::sync::LazyLock;
static HUNK: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@").expect("constant hunk pattern")
});
pub fn commentable_lines(patch: &str) -> Result<Vec<Value>, ServiceError> {
    let mut left = None;
    let mut right = None;
    let mut lines = vec![];
    for raw in patch.lines() {
        if let Some(c) = HUNK.captures(raw) {
            left = Some(
                c[1].parse::<u64>()
                    .map_err(|_| ServiceError::InvalidResult)?,
            );
            right = Some(
                c[2].parse::<u64>()
                    .map_err(|_| ServiceError::InvalidResult)?,
            );
            continue;
        }
        let (Some(l), Some(r)) = (left, right) else {
            continue;
        };
        match raw.as_bytes().first() {
            Some(b' ') => {
                lines.push(json!({"line":r,"side":"RIGHT","left_line":l,"right_line":r}));
                left = l.checked_add(1);
                right = r.checked_add(1)
            }
            Some(b'+') => {
                lines.push(json!({"line":r,"side":"RIGHT","right_line":r}));
                right = r.checked_add(1)
            }
            Some(b'-') => {
                lines.push(json!({"line":l,"side":"LEFT","left_line":l}));
                left = l.checked_add(1)
            }
            _ => {}
        }
    }
    Ok(lines)
}
/// Branvil's change of one file, with the lines an inline comment can name.
pub fn with_commentable_lines(file: crate::git::FileChange) -> Result<Value, ServiceError> {
    let lines = commentable_lines(file.patch.as_deref().unwrap_or(""))?;
    let mut value = serde_json::to_value(file).map_err(|_| ServiceError::InvalidResult)?;
    value["commentable_lines"] = lines.into();
    Ok(value)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn context_and_multiple_hunks_preserve_both_sides() {
        let patch = "header\n@@ -1,2 +1,2 @@\n old\n-removed\n+added\n\\ No newline at end of file\n@@ -8 +10 @@\n+next";
        let v = with_commentable_lines(crate::git::FileChange {
            path: "a".into(),
            previous_path: None,
            status: "modified",
            additions: 2,
            deletions: 1,
            patch: Some(patch.into()),
            patch_complete: true,
            binary: false,
        })
        .unwrap();
        assert_eq!(v["commentable_lines"].as_array().unwrap().len(), 4);
        assert_eq!(v["commentable_lines"][0]["left_line"], 1);
        assert_eq!(v["commentable_lines"][3]["line"], 10);
        assert_eq!(
            (v["path"].as_str(), v["patch_complete"].as_bool()),
            (Some("a"), Some(true))
        );
    }
}
