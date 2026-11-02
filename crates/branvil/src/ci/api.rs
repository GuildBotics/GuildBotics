//! CI endpoints: attempts, logs, retry/cancel, and artifacts for members
//! (`/api/v1/repos/{repo}/jobs/...`), and the runner API
//! (`/api/v1/runner/...`, runner-kind members only). See `docs/api.md`.

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use tokio::io::AsyncWriteExt;

use super::files::{artifact_attempt, valid_attempt_id};
use super::pipeline::JobDef;
use super::queue::{self, ArtifactMeta, Attempt};
use crate::app::Shared;
use crate::auth::Caller;
use crate::error::{AppError, AppResult};
use crate::http::{P as Path, Q};
use crate::model::{MemberKind, Repo};
use crate::util;

pub const LEASE_HEADER: &str = "x-branvil-lease";
/// One log append request at most.
const LOG_CHUNK_MAX: usize = 1 << 20;
const MAX_ARTIFACTS_PER_ATTEMPT: usize = 16;

type R = AppResult<Response>;

fn ok<T: Serialize>(v: T) -> Response {
    axum::Json(v).into_response()
}

fn json<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> AppResult<T> {
    let bytes = if bytes.is_empty() { b"{}" } else { bytes };
    serde_json::from_slice(bytes).map_err(|e| AppError::invalid(format!("Invalid JSON body: {e}")))
}

// ---------------------------------------------------------------- views

#[derive(Serialize, Debug)]
pub struct AttemptView {
    pub path_skip: Option<queue::PathSkip>,
    pub id: String,
    pub purpose: queue::Purpose,
    pub cache_refresh: Option<String>,
    pub url: String,
    pub repository: String,
    pub job: String,
    pub attempt: u64,
    pub sha: String,
    pub image_digest: Option<String>,
    pub reused_from: Option<super::ReusedFrom>,
    pub status: String,
    pub conclusion: Option<String>,
    pub details: Option<String>,
    pub cancel_requested: bool,
    pub created_at: String,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub artifact_names: Vec<String>,
    pub log_bytes: u64,
}

async fn view(app: &Shared, a: &Attempt) -> AppResult<AttemptView> {
    let own = a.clone();
    let source = app
        .store
        .read(move |r| queue::result_attempt(r, &own))
        .await?;
    Ok(AttemptView {
        path_skip: a.path_skip.clone(),
        id: a.id.clone(),
        purpose: a.purpose,
        cache_refresh: a.cache_refresh.clone(),
        url: format!("{}/{}/-/jobs/{}", app.base_url, a.repo_path, a.id),
        repository: a.repo_path.clone(),
        job: a.job.name.clone(),
        attempt: a.number,
        sha: a.sha.clone(),
        image_digest: a.image_digest.clone(),
        reused_from: app.ci.reused_from(a, &source),
        status: a.status.clone(),
        conclusion: a.conclusion.clone(),
        details: a.display_details(),
        cancel_requested: a.cancel_requested,
        created_at: a.created_at.clone(),
        started_at: a.started_at.clone(),
        finished_at: a.finished_at.clone(),
        artifact_names: if source.files_pruned {
            Vec::new()
        } else {
            source.artifacts.iter().map(|x| x.name.clone()).collect()
        },
        log_bytes: app.ci.log_size(&a.id).await,
    })
}

// ---------------------------------------------------------------- member endpoints

/// The attempt `id` of repository `repo` as the caller may see it.
async fn attempt_of(
    app: &Shared,
    caller: &Caller,
    repo: &str,
    id: &str,
) -> AppResult<(Repo, Attempt)> {
    let repo = crate::domain::repos::visible(app, caller, repo).await?;
    let not_found = || AppError::not_found(format!("No CI attempt {id} in {}.", repo.path));
    if !valid_attempt_id(id) {
        return Err(not_found());
    }
    let i = id.to_string();
    let a = app
        .store
        .read(move |r| queue::get_attempt(r, &i))
        .await?
        .filter(|a| a.repo == repo.id)
        .ok_or_else(not_found)?;
    Ok((repo, a))
}

async fn job_get(
    State(app): State<Shared>,
    c: Caller,
    Path((repo, id)): Path<(String, String)>,
) -> R {
    let (_, a) = attempt_of(&app, &c, &repo, &id).await?;
    Ok(ok(view(&app, &a).await?))
}

async fn job_log(
    State(app): State<Shared>,
    c: Caller,
    Path((repo, id)): Path<(String, String)>,
    q: Q,
) -> R {
    let tail = match q.0.get("tail_bytes") {
        Some(t) => t
            .parse::<u64>()
            .map_err(|_| AppError::invalid("tail_bytes must be a number."))?,
        None => 64 << 10,
    }
    .clamp(1, 4 << 20);
    // A check run of the upstream host, as a published pull request's
    // checks name it.
    if let Some(v) = crate::upstream::ops::check_log(&app, &c, &repo, &id, tail).await? {
        return Ok(ok(v));
    }
    let (_, a) = attempt_of(&app, &c, &repo, &id).await?;
    let (log, size) = app.ci.log_tail(&a.id, tail).await?;
    Ok(ok(serde_json::json!({
        "log": log,
        "log_bytes": size,
        "truncated": size > tail,
        "pruned": a.files_pruned,
    })))
}

async fn job_retry(
    State(app): State<Shared>,
    c: Caller,
    Path((repo, id)): Path<(String, String)>,
) -> R {
    let (repo, a) = attempt_of(&app, &c, &repo, &id).await?;
    // Ref cleanup may still be cancelling work after its landing state
    // became visible. A new explicit retry must follow that cleanup.
    let id = a.id.clone();
    let new = app
        .locked(&repo.id, move |app| async move {
            let (queue_max, now) = (app.ci.config().queue_max, util::now());
            app.store
                .write(move |w| queue::retry(w, &id, queue_max, &now))
                .await
        })
        .await?;
    app.events.publish("check", &repo.path, new.sha.clone());
    Ok(ok(view(&app, &new).await?))
}

async fn job_cancel(
    State(app): State<Shared>,
    c: Caller,
    Path((repo, id)): Path<(String, String)>,
) -> R {
    let (repo, a) = attempt_of(&app, &c, &repo, &id).await?;
    let (who, now, id) = (c.name().to_string(), util::now(), a.id.clone());
    let new = app
        .store
        .write(move |w| queue::cancel(w, &id, &who, &now))
        .await?;
    app.events.publish("check", &repo.path, new.sha.clone());
    Ok(ok(view(&app, &new).await?))
}

async fn artifact_get(
    State(app): State<Shared>,
    c: Caller,
    Path((repo, sha, name)): Path<(String, String, String)>,
) -> R {
    let repo = crate::domain::repos::visible(&app, &c, &repo).await?;
    let not_found =
        || AppError::not_found(format!("No artifact {name} of {sha} in {}.", repo.path));
    if !util::is_sha(&sha) || !util::valid_name(&name) {
        return Err(not_found());
    }
    let (id, s, n) = (repo.id.clone(), sha.to_lowercase(), name.clone());
    let a = app
        .store
        .read(move |r| artifact_attempt(r, &id, &s, &n))
        .await?
        .ok_or_else(not_found)?;
    let path = app.ci.artifact_path(&a.id, &name);
    let file = tokio::fs::File::open(&path)
        .await
        .map_err(|_| not_found())?;
    let len = file.metadata().await.map_err(AppError::internal)?.len();
    let stream = tokio_stream::wrappers::ReceiverStream::new(stream_file(file));
    let mut resp = Response::new(Body::from_stream(stream));
    let h = resp.headers_mut();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/zip"),
    );
    h.insert(header::CONTENT_LENGTH, HeaderValue::from(len));
    h.insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_str(&format!("attachment; filename=\"{name}.zip\""))
            .map_err(AppError::internal)?,
    );
    Ok(resp)
}

/// Streams a file in 64 KiB chunks.
fn stream_file(
    mut file: tokio::fs::File,
) -> tokio::sync::mpsc::Receiver<Result<bytes::Bytes, std::io::Error>> {
    let (tx, rx) = tokio::sync::mpsc::channel(4);
    tokio::spawn(async move {
        use tokio::io::AsyncReadExt;
        let mut buf = vec![0u8; 64 << 10];
        loop {
            match file.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => {
                    if tx
                        .send(Ok(bytes::Bytes::copy_from_slice(&buf[..n])))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                Err(e) => {
                    let _ = tx.send(Err(e)).await;
                    break;
                }
            }
        }
    });
    rx
}

// ---------------------------------------------------------------- runner API

fn require_runner(app: &Shared, c: &Caller) -> AppResult<()> {
    if c.member.kind != MemberKind::Runner {
        return Err(AppError::forbidden(
            "The runner API needs a token of a runner member (`branvil runner register`).",
        ));
    }
    app.ci.saw_runner(c.name(), None);
    Ok(())
}

fn lease_of(headers: &HeaderMap) -> AppResult<String> {
    headers
        .get(LEASE_HEADER)
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
        .map(str::to_string)
        .ok_or_else(|| AppError::invalid("The X-Branvil-Lease header is required."))
}

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct ClaimInput {
    #[serde(default)]
    executor: String,
    /// `ready` or `unavailable`.
    #[serde(default)]
    executor_state: Option<String>,
    #[serde(default)]
    executor_detail: Option<String>,
}

#[derive(Serialize)]
pub struct ClaimView {
    pub attempt_id: String,
    pub purpose: queue::Purpose,
    pub lease: String,
    pub expires_at: String,
    pub lease_secs: u64,
    pub repo: String,
    pub sha: String,
    pub attempt: u64,
    pub job: JobDef,
    pub cache_scope: String,
    pub network: bool,
    /// How long the attempt waited in the queue, in seconds.
    pub queued_secs: u64,
    pub reuse: Option<queue::Reuse>,
}

async fn claim(State(app): State<Shared>, c: Caller, body: axum::body::Bytes) -> R {
    require_runner(&app, &c)?;
    let input: ClaimInput = json(&body)?;
    app.ci.saw_runner(
        c.name(),
        Some((
            input.executor.clone(),
            input
                .executor_state
                .clone()
                .unwrap_or_else(|| "ready".into()),
            input.executor_detail.clone(),
        )),
    );
    let (runner, lease_secs, now) = (
        c.name().to_string(),
        app.ci.config().lease_secs,
        chrono::Utc::now(),
    );
    let at = util::format_time(now);
    // Checked in the write: `Ci::stop` counts the running jobs in a write
    // too, so a claim it does not see gives out nothing.
    let ci = app.ci.clone();
    let claimed = app
        .store
        .write(move |w| {
            if ci.stopping() {
                return Ok(None);
            }
            queue::claim(w, &runner, lease_secs, &at)
        })
        .await?;
    let Some(cl) = claimed else {
        return Ok(StatusCode::NO_CONTENT.into_response());
    };
    let a = &cl.attempt;
    app.events.publish("check", &a.repo_path, a.sha.clone());
    Ok(ok(ClaimView {
        attempt_id: a.id.clone(),
        purpose: a.purpose,
        lease: cl.lease.clone(),
        expires_at: a.lease_expires_at.clone().unwrap_or_default(),
        lease_secs,
        repo: a.repo_path.clone(),
        sha: a.sha.clone(),
        attempt: a.number,
        job: a.job.clone(),
        cache_scope: a.cache_scope.clone(),
        network: a.network,
        queued_secs: chrono::DateTime::parse_from_rfc3339(&a.created_at)
            .map_or(0, |t| (now - t.to_utc()).num_seconds().max(0) as u64),
        reuse: cl.reuse,
    }))
}

async fn leased(app: &Shared, id: &str, lease: &str) -> AppResult<Attempt> {
    if !valid_attempt_id(id) {
        return Err(AppError::conflict(format!(
            "No current lease for attempt {id}."
        )));
    }
    let (i, l, now) = (id.to_string(), lease.to_string(), util::now());
    app.store
        .read(move |r| queue::leased(r, &i, &l, &now))
        .await
}

/// The commit as a Git bundle: its history, and the tags whose commits it
/// reaches, with the commit as the bundle's `HEAD` (a v2 bundle is its
/// header of refs and a pack of the objects they reach).
async fn source(
    State(app): State<Shared>,
    c: Caller,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> R {
    require_runner(&app, &c)?;
    let a = leased(&app, &id, &lease_of(&headers)?).await?;
    let dir = app.git.repo_dir(&a.repo_path);
    let merged = format!("--merged={}", a.sha);
    let tags = app
        .git
        .run_ok(
            Some(&dir),
            &[
                "for-each-ref",
                &merged,
                "--format=%(objectname) %(refname)",
                "refs/tags",
            ],
        )
        .await?;
    let mut header = format!("# v2 git bundle\n{} HEAD\n", a.sha);
    let mut tips = format!("{}\n", a.sha);
    for line in tags.lines() {
        header.push_str(&format!("{line}\n"));
        tips.push_str(&format!("{}\n", line.split(' ').next().unwrap_or_default()));
    }
    header.push('\n');
    let mut cmd = crate::git::base_command(&app.layout.git_config());
    cmd.arg("--git-dir")
        .arg(&dir)
        .args([
            "pack-objects",
            "--revs",
            "--stdout",
            "-q",
            "--delta-base-offset",
        ])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null());
    let mut child = cmd
        .spawn()
        .map_err(|e| AppError::internal(format!("cannot start git pack-objects: {e}")))?;
    let mut stdin = child.stdin.take().expect("piped stdin");
    let stdout = child.stdout.take().expect("piped stdout");
    let permits = app.transfers.clone();
    let timeout = std::time::Duration::from_secs(app.config.limits.git_http_timeout_secs);
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<bytes::Bytes, std::io::Error>>(4);
    tokio::spawn(async move {
        use tokio::io::AsyncReadExt;
        let _permit = permits.acquire_owned().await;
        let work = async {
            // pack-objects reads every tip before it writes.
            stdin.write_all(tips.as_bytes()).await?;
            drop(stdin);
            if tx.send(Ok(header.into())).await.is_err() {
                return Ok(());
            }
            let mut out = stdout;
            let mut buf = vec![0u8; 64 << 10];
            loop {
                let n = out.read(&mut buf).await?;
                if n == 0 {
                    break;
                }
                if tx
                    .send(Ok(bytes::Bytes::copy_from_slice(&buf[..n])))
                    .await
                    .is_err()
                {
                    return Ok(());
                }
            }
            let status = child.wait().await?;
            if !status.success() {
                let _ = tx
                    .send(Err(std::io::Error::other("git pack-objects failed")))
                    .await;
            }
            Ok::<_, std::io::Error>(())
        };
        if tokio::time::timeout(timeout, work).await.is_err() {
            let _ = tx
                .send(Err(std::io::Error::other("git pack-objects timed out")))
                .await;
        }
    });
    let mut resp = Response::new(Body::from_stream(
        tokio_stream::wrappers::ReceiverStream::new(rx),
    ));
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/x-git-bundle"),
    );
    Ok(resp)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LeaseInput {
    lease: String,
}

async fn heartbeat(
    State(app): State<Shared>,
    c: Caller,
    Path(id): Path<String>,
    body: axum::body::Bytes,
) -> R {
    require_runner(&app, &c)?;
    let input: LeaseInput = json(&body)?;
    if !valid_attempt_id(&id) {
        return Err(AppError::conflict(format!(
            "No current lease for attempt {id}."
        )));
    }
    let (lease_secs, now) = (app.ci.config().lease_secs, util::now());
    let a = app
        .store
        .write(move |w| queue::heartbeat(w, &id, &input.lease, lease_secs, &now))
        .await?;
    Ok(ok(serde_json::json!({
        "cancel": a.cancel_requested,
        "expires_at": a.lease_expires_at,
    })))
}

async fn log_append(
    State(app): State<Shared>,
    c: Caller,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Body,
) -> R {
    require_runner(&app, &c)?;
    let a = leased(&app, &id, &lease_of(&headers)?).await?;
    let bytes = axum::body::to_bytes(body, LOG_CHUNK_MAX)
        .await
        .map_err(|_| {
            AppError::too_large(format!("A log chunk is at most {LOG_CHUNK_MAX} bytes."))
        })?;
    let (size, truncated) = app.ci.append_log(&a.id, &bytes).await?;
    Ok(ok(
        serde_json::json!({"log_bytes": size, "truncated": truncated}),
    ))
}

async fn artifact_put(
    State(app): State<Shared>,
    c: Caller,
    Path((id, name)): Path<(String, String)>,
    headers: HeaderMap,
    body: Body,
) -> R {
    require_runner(&app, &c)?;
    let lease = lease_of(&headers)?;
    let a = leased(&app, &id, &lease).await?;
    if !util::valid_name(&name) {
        return Err(AppError::invalid(format!(
            "'{name}' is not a valid artifact name."
        )));
    }
    if !a.job.artifacts.iter().any(|x| x.name == name) {
        return Err(AppError::invalid(format!(
            "Job {} declares no artifact named {name}.",
            a.job.name
        )));
    }
    if a.artifacts.len() >= MAX_ARTIFACTS_PER_ATTEMPT && !a.artifacts.iter().any(|x| x.name == name)
    {
        return Err(AppError::too_large("Too many artifacts for one attempt."));
    }
    let max = app.ci.artifact_max();
    let dir = app.ci.artifact_dir(&a.id);
    tokio::fs::create_dir_all(&dir)
        .await
        .map_err(AppError::internal)?;
    let tmp = dir.join(format!(".{name}.{}.part", util::random_secret::<8>()));
    let written = async {
        let mut file = tokio::fs::File::create(&tmp)
            .await
            .map_err(AppError::internal)?;
        let mut stream = body.into_data_stream();
        let mut size: u64 = 0;
        let mut head = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| AppError::invalid("The artifact upload was cut off."))?;
            size += chunk.len() as u64;
            if size > max {
                return Err(AppError::too_large(format!(
                    "An artifact is at most {} MiB.",
                    max >> 20
                )));
            }
            if head.len() < 4 {
                head.extend_from_slice(&chunk[..chunk.len().min(4 - head.len())]);
            }
            file.write_all(&chunk).await.map_err(AppError::internal)?;
        }
        file.flush().await.map_err(AppError::internal)?;
        file.sync_all().await.map_err(AppError::internal)?;
        if head != b"PK\x03\x04" && head != b"PK\x05\x06" {
            return Err(AppError::invalid("An artifact must be a zip archive."));
        }
        Ok(size)
    }
    .await;
    let size = match written {
        Ok(s) => s,
        Err(e) => {
            let _ = tokio::fs::remove_file(&tmp).await;
            return Err(e);
        }
    };
    let path = app.ci.artifact_path(&a.id, &name);
    tokio::fs::rename(&tmp, &path)
        .await
        .map_err(AppError::internal)?;
    let (i, n, now) = (a.id.clone(), name.clone(), util::now());
    let recorded = app
        .store
        .write(move |w| {
            queue::add_artifact(
                w,
                &i,
                &lease,
                ArtifactMeta {
                    name: n,
                    bytes: size,
                },
                &now,
            )
        })
        .await;
    if let Err(e) = recorded {
        let _ = tokio::fs::remove_file(&path).await;
        return Err(e);
    }
    Ok(ok(serde_json::json!({"name": name, "bytes": size})))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FinishInput {
    lease: String,
    conclusion: String,
    #[serde(default)]
    details: Option<String>,
    #[serde(default)]
    image_digest: Option<String>,
    reused_from: Option<String>,
    cache_key: Option<String>,
    #[serde(default)]
    refresh_cache: bool,
}

async fn finish(
    State(app): State<Shared>,
    c: Caller,
    Path(id): Path<String>,
    body: axum::body::Bytes,
) -> R {
    require_runner(&app, &c)?;
    let input: FinishInput = json(&body)?;
    if !valid_attempt_id(&id) {
        return Err(AppError::conflict(format!(
            "No current lease for attempt {id}."
        )));
    }
    let now = util::now();
    let queue_max = app.ci.config().queue_max;
    let a = app
        .store
        .write(move |w| {
            queue::finish(
                w,
                &id,
                &input.lease,
                &input.conclusion,
                input.details,
                queue::ResultInfo {
                    image_digest: input.image_digest,
                    reused_from: input.reused_from,
                    cache_key: input.cache_key,
                    refresh_cache: input.refresh_cache,
                },
                queue_max,
                &now,
            )
        })
        .await?;
    app.events.publish("check", &a.repo_path, a.sha.clone());
    Ok(ok(view(&app, &a).await?))
}

/// The CI routes, merged into `/api/v1`.
pub fn routes() -> Router<Shared> {
    Router::new()
        .route("/repos/{repo}/jobs/{id}", get(job_get))
        .route("/repos/{repo}/jobs/{id}/log", get(job_log))
        .route("/repos/{repo}/jobs/{id}/retry", post(job_retry))
        .route("/repos/{repo}/jobs/{id}/cancel", post(job_cancel))
        .route(
            "/repos/{repo}/commits/{sha}/artifacts/{name}",
            get(artifact_get),
        )
        .route("/runner/claim", post(claim))
        .route("/runner/attempts/{id}/source", get(source))
        .route("/runner/attempts/{id}/heartbeat", post(heartbeat))
        .route("/runner/attempts/{id}/log", post(log_append))
        .route("/runner/attempts/{id}/artifacts/{name}", put(artifact_put))
        .route("/runner/attempts/{id}/finish", post(finish))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::App;
    use crate::config::Config;
    use crate::model::{self, Member};
    use crate::store::{self, Store};
    use std::time::Duration;

    #[tokio::test]
    async fn retry_waits_for_ref_cleanup_and_keeps_the_new_attempt_claimable() {
        for branch in ["gate/3", "feature"] {
            let temp = tempfile::tempdir().unwrap();
            let config = Config::load(None, Some(temp.path()), None, None).unwrap();
            let layout = config.layout();
            layout.create().unwrap();
            let store = Store::open(&layout.db(), 1 << 20, 2).unwrap();
            let app = App::new(
                config,
                "http://127.0.0.1:7878".into(),
                store,
                vec![],
                "http://127.0.0.1:7878".into(),
            );
            let repo = Repo {
                id: "repo_1".into(),
                path: "acme/demo".into(),
                workspace: "ws_1".into(),
                default_branch: "main".into(),
                protected_branches: vec![],
                required_checks: vec!["build".into()],
                required_approvals: 0,
                auto_land: false,
                ci_network: false,
                labels: vec![],
                upstream: None,
                revision: 1,
                created_at: util::now(),
            };
            let c = Caller {
                member: Member {
                    name: "member1".into(),
                    display_name: "Member 1".into(),
                    kind: MemberKind::Agent,
                    accounts: vec![],
                    reviewers: vec![],
                    created_at: util::now(),
                    avatar: None,
                },
            };
            let r2 = repo.clone();
            let branch = branch.to_string();
            let b2 = branch.clone();
            let failed = app
                .store
                .write(move |w| {
                    model::put_indexed(
                        w,
                        store::REPOS,
                        &Repo::key(&r2.id),
                        None,
                        &r2,
                        model::repo_index(&r2),
                    )?;
                    let now = util::now();
                    queue::register(
                        w,
                        queue::Registration {
                            selection: None,
                            repo: r2.id,
                            repo_path: r2.path,
                            sha: "gate-sha".into(),
                            tree: Some("gate-tree".into()),
                            branch: b2,
                            protected: false,
                            pipeline: queue::PipelineState::Jobs {
                                jobs: vec![JobDef {
                                    paths: Vec::new(),
                                    name: "build".into(),
                                    image: "alpine".into(),
                                    commands: vec!["true".into()],
                                    timeout_secs: None,
                                    artifacts: vec![],
                                    cache: None,
                                }],
                            },
                            network: false,
                        },
                        10,
                        &now,
                    )?;
                    let claim = queue::claim(w, "runner1", 60, &now)?.unwrap();
                    queue::finish(
                        w,
                        &claim.attempt.id,
                        &claim.lease,
                        "failure",
                        None,
                        queue::ResultInfo::default(),
                        10,
                        &now,
                    )
                })
                .await
                .unwrap();
            let held = app.hold_lock(&repo.id).await;
            let mut retry = Box::pin(job_retry(
                State(app.clone()),
                c,
                Path((repo.path.clone(), failed.id.clone())),
            ));
            assert!(
                tokio::time::timeout(Duration::from_millis(50), retry.as_mut())
                    .await
                    .is_err(),
                "retry must wait until ref cleanup has finished"
            );
            let id = repo.id.clone();
            app.store
                .write(move |w| {
                    queue::branch_left(
                        w,
                        &id,
                        "gate-sha",
                        &branch,
                        "a deletion of the branch",
                        &util::now(),
                    )
                })
                .await
                .unwrap();
            drop(held);
            let response = retry.await.unwrap();
            assert_eq!(response.status(), StatusCode::OK);
            let claimed = app
                .store
                .write(|w| queue::claim(w, "runner1", 60, &util::now()))
                .await
                .unwrap()
                .expect("the accepted retry must still be claimable");
            assert_eq!(claimed.attempt.sha, "gate-sha");
            assert_eq!(claimed.attempt.number, failed.number + 1);
            assert!(!claimed.attempt.superseded);
            assert!(!claimed.attempt.allow_reuse);
            assert_eq!(claimed.attempt.cache_scope, failed.cache_scope);
        }
    }
}
