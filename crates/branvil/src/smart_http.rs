//! Git Smart HTTP, delegated to `git http-backend` (CGI). Both fetch
//! (`git-upload-pack`) and push (`git-receive-pack`) need HTTP Basic with the
//! member's name and one of its tokens.

use std::process::Stdio;
use std::time::Duration;

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, HeaderValue, Method, Response, StatusCode, Uri, header};
use base64::Engine;
use futures_util::StreamExt;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};

use crate::app::Shared;
use crate::auth;
use crate::error::AppError;
use crate::git;
use crate::hooks;
use crate::model::{Member, MemberKind, Repo};
use crate::util::fold;

fn challenge() -> Response<Body> {
    let mut resp = Response::new(Body::from("Branvil needs your member name and a token.\n"));
    *resp.status_mut() = StatusCode::UNAUTHORIZED;
    resp.headers_mut().insert(
        header::WWW_AUTHENTICATE,
        HeaderValue::from_static("Basic realm=\"Branvil\", charset=\"UTF-8\""),
    );
    resp
}

fn plain(status: StatusCode, text: &str) -> Response<Body> {
    let mut resp = Response::new(Body::from(format!("{text}\n")));
    *resp.status_mut() = status;
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    resp
}

async fn basic_member(app: &Shared, headers: &HeaderMap) -> Result<Member, Response<Body>> {
    let Some(value) = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
    else {
        return Err(challenge());
    };
    let Some(encoded) = value.strip_prefix("Basic ") else {
        return Err(challenge());
    };
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(encoded.trim())
        .ok()
        .and_then(|b| String::from_utf8(b).ok())
        .ok_or_else(challenge)?;
    let (user, token) = decoded.split_once(':').ok_or_else(challenge)?;
    let member = auth::authenticate_token(app, token)
        .await
        .map_err(|_| challenge())?;
    if fold(&member.name) != fold(user) {
        // The username must be the token's member.
        return Err(challenge());
    }
    Ok(member)
}

/// Splits `acme/demo.git/info/refs` into the repository path and the service path.
fn split(rest: &str) -> Option<(&str, &str)> {
    let i = rest.find(".git/")?;
    Some((&rest[..i], &rest[i + 5..]))
}

pub async fn handle(
    State(app): State<Shared>,
    Path(rest): Path<String>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Body,
) -> Response<Body> {
    let Some((repo_path, service_path)) = split(&rest) else {
        return plain(StatusCode::NOT_FOUND, "Not a Branvil Git URL.");
    };
    let query = uri.query().unwrap_or("").to_string();
    match (method.as_str(), service_path) {
        ("GET", "info/refs") => match query.split('&').find_map(|kv| kv.strip_prefix("service=")) {
            Some("git-upload-pack" | "git-receive-pack") => {}
            _ => return plain(StatusCode::FORBIDDEN, "Only the smart protocol is offered."),
        },
        ("POST", "git-upload-pack" | "git-receive-pack") => {}
        _ => return plain(StatusCode::NOT_FOUND, "Not a Smart HTTP endpoint."),
    }
    let member = match basic_member(&app, &headers).await {
        Ok(m) => m,
        Err(resp) => return resp,
    };
    let path = repo_path.to_string();
    let repo: Repo = match app
        .store
        .read(move |r| crate::domain::repo_by_path(r, &path))
        .await
    {
        Ok(r) => r,
        Err(_) => return plain(StatusCode::NOT_FOUND, "Repository not found."),
    };
    if member.kind == MemberKind::Runner {
        return plain(StatusCode::FORBIDDEN, "A runner uses only the runner API.");
    }
    match run_backend(
        &app,
        &repo,
        &member,
        &method,
        service_path,
        &query,
        &headers,
        body,
    )
    .await
    {
        Ok(resp) => resp,
        Err(e) => plain(e.status, &e.message),
    }
}

#[allow(clippy::too_many_arguments)]
async fn run_backend(
    app: &Shared,
    repo: &Repo,
    member: &Member,
    method: &Method,
    service_path: &str,
    query: &str,
    headers: &HeaderMap,
    body: Body,
) -> Result<Response<Body>, AppError> {
    let permit = app
        .transfers
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| AppError::unavailable("Git is shutting down."))?;
    let layout = app.git.layout().clone();
    let mut cmd = git::base_command(&layout.git_config());
    let header_text = |name: header::HeaderName| {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string()
    };
    // PATH_INFO uses the stored spelling of the path, whatever case the
    // client used.
    cmd.arg("http-backend")
        .env("GIT_PROJECT_ROOT", layout.repos())
        .env("GIT_HTTP_EXPORT_ALL", "1")
        .env("PATH_INFO", format!("/{}.git/{service_path}", repo.path))
        .env("REQUEST_METHOD", method.as_str())
        .env("QUERY_STRING", query)
        .env("CONTENT_TYPE", header_text(header::CONTENT_TYPE))
        .env("REMOTE_USER", &member.name)
        .env("REMOTE_ADDR", "127.0.0.1")
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "core.hooksPath")
        .env("GIT_CONFIG_VALUE_0", layout.hooks())
        .env(hooks::ENV_URL, &app.hook_url)
        .env(hooks::ENV_SECRET, &app.hook_secret)
        .env(hooks::ENV_REPO, &repo.id)
        .env(hooks::ENV_MEMBER, &member.name)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let encoding = header_text(header::CONTENT_ENCODING);
    if !encoding.is_empty() {
        cmd.env("HTTP_CONTENT_ENCODING", encoding);
    }
    let protocol = headers
        .get("git-protocol")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !protocol.is_empty() {
        cmd.env("GIT_PROTOCOL", protocol);
    }
    if let Some(len) = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
    {
        cmd.env("CONTENT_LENGTH", len);
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| AppError::internal(format!("cannot start git http-backend: {e}")))?;

    // Request body → stdin, bounded.
    let max_body = app.config.limits.git_push_max_mib << 20;
    let mut stdin = child.stdin.take().expect("piped stdin");
    tokio::spawn(async move {
        let mut stream = body.into_data_stream();
        let mut total: u64 = 0;
        while let Some(chunk) = stream.next().await {
            let Ok(chunk) = chunk else { break };
            total += chunk.len() as u64;
            if total > max_body {
                tracing::warn!("git request body above {max_body} bytes; cut");
                break;
            }
            if stdin.write_all(&chunk).await.is_err() {
                break;
            }
        }
        let _ = stdin.shutdown().await;
    });

    // stderr → log, bounded.
    let mut stderr = child.stderr.take().expect("piped stderr");
    tokio::spawn(async move {
        let mut buf = Vec::new();
        let _ = (&mut stderr).take(64 << 10).read_to_end(&mut buf).await;
        let _ = tokio::io::copy(&mut stderr, &mut tokio::io::sink()).await;
        if !buf.is_empty() {
            tracing::debug!("git http-backend: {}", String::from_utf8_lossy(&buf).trim());
        }
    });

    // The child is killed when it outlives the request limit; the permit is
    // held until it exits.
    let timeout = Duration::from_secs(app.config.limits.git_http_timeout_secs);
    let mut stdout = BufReader::new(child.stdout.take().expect("piped stdout"));
    tokio::spawn(async move {
        if tokio::time::timeout(timeout, child.wait()).await.is_err() {
            tracing::warn!("git http-backend exceeded {timeout:?}; killed");
            let _ = child.kill().await;
        }
        drop(permit);
    });

    // CGI headers.
    let mut status = StatusCode::OK;
    let mut resp_headers = HeaderMap::new();
    loop {
        let mut line = String::new();
        let n = tokio::time::timeout(timeout, stdout.read_line(&mut line))
            .await
            .map_err(|_| AppError::unavailable("git http-backend timed out."))?
            .map_err(|e| AppError::internal(format!("git http-backend: {e}")))?;
        if n == 0 {
            return Err(AppError::internal(
                "git http-backend ended before its headers",
            ));
        }
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            break;
        }
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        let value = value.trim();
        if name.eq_ignore_ascii_case("status") {
            let code = value
                .split_whitespace()
                .next()
                .and_then(|c| c.parse::<u16>().ok())
                .unwrap_or(500);
            status = StatusCode::from_u16(code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        } else if let (Ok(n), Ok(v)) = (
            header::HeaderName::try_from(name),
            HeaderValue::try_from(value),
        ) {
            resp_headers.append(n, v);
        }
    }

    let stream = futures_util::stream::unfold(stdout, |mut out| async move {
        let mut buf = vec![0u8; 32 << 10];
        match out.read(&mut buf).await {
            Ok(0) | Err(_) => None,
            Ok(n) => {
                buf.truncate(n);
                Some((Ok::<_, std::io::Error>(bytes::Bytes::from(buf)), out))
            }
        }
    });
    let mut resp = Response::new(Body::from_stream(stream));
    *resp.status_mut() = status;
    *resp.headers_mut() = resp_headers;
    Ok(resp)
}
