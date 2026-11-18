//! Unified diff coordinates shared by code hosts.
use gb_ports::ServiceError;
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
pub fn file_change(
    path: &str,
    status: &str,
    patch: Option<String>,
    additions: i64,
    deletions: i64,
) -> Result<Value, ServiceError> {
    if additions < 0 || deletions < 0 {
        return Err(ServiceError::InvalidResult);
    }
    let changes = additions
        .checked_add(deletions)
        .ok_or(ServiceError::InvalidResult)?;
    let coordinates = commentable_lines(patch.as_deref().unwrap_or(""))?;
    let added = coordinates
        .iter()
        .filter(|v| v["side"] == "RIGHT" && v.get("left_line").is_none())
        .count() as i64;
    let deleted = coordinates.iter().filter(|v| v["side"] == "LEFT").count() as i64;
    Ok(
        json!({"path":path,"status":status,"additions":additions,"deletions":deletions,"changes":changes,"patch":patch,"patch_available":patch.is_some(),"patch_complete":patch.is_some()&&added==additions&&deleted==deletions,"commentable_lines":coordinates}),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_counts_are_errors() {
        assert!(file_change("a", "modified", None, -1, 0).is_err());
        assert!(file_change("a", "modified", None, i64::MAX, 1).is_err());
    }
    #[test]
    fn context_and_multiple_hunks_preserve_both_sides() {
        let patch = "header\n@@ -1,2 +1,2 @@\n old\n-removed\n+added\n\\ No newline at end of file\n@@ -8 +10 @@\n+next";
        let v = file_change("a", "modified", Some(patch.into()), 2, 1).unwrap();
        assert_eq!(v["commentable_lines"].as_array().unwrap().len(), 4);
        assert_eq!(v["commentable_lines"][0]["left_line"], 1);
        assert_eq!(v["commentable_lines"][3]["line"], 10);
        assert_eq!(v["patch_complete"], true);
        assert_eq!(
            file_change("a", "modified", Some(patch.into()), 3, 1).unwrap()["patch_complete"],
            false
        );
        assert_eq!(
            file_change("a", "modified", None, 0, 0).unwrap()["patch_available"],
            false
        );
    }
}
