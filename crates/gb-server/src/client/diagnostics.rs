//! Read the workspace's diagnostics locally, including when the server is stopped.

use std::path::Path;

use gb_observability::Diagnostics;
use serde_json::{Value, json};

pub use gb_observability::TraceSearch;

pub fn traces(workspace: &Path, search: TraceSearch<'_>) -> std::io::Result<Value> {
    Diagnostics::open(workspace).search_traces(search)
}

pub fn trace(
    workspace: &Path,
    id: &str,
    kinds: &[&str],
    level: Option<&str>,
    limit: usize,
) -> std::io::Result<Value> {
    let detail = Diagnostics::open(workspace).trace_detail(id)?;
    let records = detail["records"].as_array().expect("diagnostics records");
    let filtered = records
        .iter()
        .filter(|record| {
            (kinds.is_empty() || kinds.iter().any(|kind| record["kind"] == *kind))
                && level.is_none_or(|level| {
                    record["level"]
                        .as_str()
                        .is_some_and(|found| level.eq_ignore_ascii_case(found))
                })
        })
        .collect::<Vec<_>>();
    let start = filtered.len().saturating_sub(limit);
    Ok(
        json!({"trace_id": id, "summary": detail["summary"], "record_count": filtered.len(), "records": &filtered[start..]}),
    )
}

pub fn system(workspace: &Path, limit: usize) -> std::io::Result<Value> {
    Diagnostics::open(workspace).latest_system_records(limit)
}
