//! Owner-only artifact access after the guest has gone away.
use crate::api::{ApiError, Shared};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use serde_json::json;

fn list(
    dir: &cap_std::fs::Dir,
    prefix: &str,
    files: &mut Vec<String>,
    depth: usize,
) -> std::io::Result<()> {
    if depth > 32 {
        return Ok(());
    }
    for entry in dir.entries()? {
        if files.len() >= 1000 {
            break;
        }
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let path = format!("{prefix}{name}");
        if entry.file_type()?.is_file() {
            if gb_environment::run_files::regular(dir, &name).is_ok() {
                files.push(path);
            }
        } else if entry.file_type()?.is_dir() {
            use cap_fs_ext::DirExt;
            if let Ok(child) = dir.open_dir_nofollow(&name) {
                list(&child, &format!("{path}/"), files, depth + 1)?;
            }
        }
    }
    Ok(())
}
pub(crate) async fn index(
    State(api): State<Shared>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let data = api.data.clone();
    match tokio::task::spawn_blocking(move || -> anyhow::Result<Vec<String>> {
        let mut files = vec![];
        if let Ok(dir) = gb_environment::run_files::completed(&data, &id) {
            list(&dir, "", &mut files, 0)?;
        }
        files.sort();
        Ok(files)
    })
    .await
    {
        Ok(Ok(files)) => Json(json!({"files": files})).into_response(),
        _ => ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            &headers,
        )
        .into_response(),
    }
}
#[derive(Deserialize)]
pub(crate) struct FileQuery {
    path: String,
}
pub(crate) async fn download(
    State(api): State<Shared>,
    Path(id): Path<String>,
    Query(query): Query<FileQuery>,
    headers: HeaderMap,
) -> Response {
    let data = api.data.clone();
    match tokio::task::spawn_blocking(move || -> anyhow::Result<Vec<u8>> {
        use std::io::Read;
        let root = gb_environment::run_files::completed(&data, &id)?;
        let file = gb_environment::run_files::regular(&root, &query.path)?;
        let mut bytes = Vec::new();
        file.take(64 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
        anyhow::ensure!(bytes.len() <= 64 * 1024 * 1024, "Artifact too large");
        Ok(bytes)
    })
    .await
    {
        Ok(Ok(bytes)) => (
            [
                ("content-type", "application/octet-stream"),
                ("content-disposition", "attachment"),
                ("x-content-type-options", "nosniff"),
            ],
            bytes,
        )
            .into_response(),
        _ => ApiError::new(StatusCode::NOT_FOUND, "command_run_file_missing", &headers)
            .into_response(),
    }
}
