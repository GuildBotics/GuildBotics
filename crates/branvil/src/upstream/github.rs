//! GitHub as an upstream host: its REST API (with conditional requests), its
//! URLs, and GitHub Apps (a JWT signed with the app's key buys an
//! installation token). Nothing outside this file knows GitHub.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use aws_lc_rs::rand::SystemRandom;
use aws_lc_rs::signature::{RSA_PKCS1_SHA256, RsaKeyPair};
use base64::Engine;
use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use chrono::{DateTime, Utc};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, percent_decode_str, utf8_percent_encode};
use reqwest::{Method, StatusCode};
use serde_json::{Value, json};

use super::host::{
    AdvisoryId, AlertState, AppIdentity, As, Credential, DependencyAlert, HostError, HostPull,
    HostResult, PullChange, PullState,
};
use crate::config::GitHubConfig;
use crate::error::{AppError, AppResult};
use crate::model::HostCheck;
use crate::util;

pub struct GitHub {
    api: String,
    web: String,
    git: String,
    client: reqwest::Client,
    /// `member\nurl` → the `ETag` and body of the last full answer, so the
    /// next read of an unchanged resource is a free `304`.
    cache: Mutex<HashMap<String, (String, Value)>>,
    /// `app id/installation id` → an installation token and when it expires.
    /// Only in memory.
    installations: Mutex<HashMap<String, (String, DateTime<Utc>)>>,
}

/// At most this many cached answers; the polled set is far smaller.
const CACHE_MAX: usize = 1024;
/// An installation token (valid for an hour) is made again when it has less
/// than this left.
const RENEW_BEFORE_SECS: i64 = 300;

/// What stays as is in a branch name inside a URL: unreserved characters and,
/// in a path, `/` (Git branch names may hold `+`, `#`, `&`, `%`, ...).
const IN_QUERY: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'.')
    .remove(b'_')
    .remove(b'~');
const IN_PATH: &AsciiSet = &IN_QUERY.remove(b'/');

fn branch_in_path(branch: &str) -> String {
    utf8_percent_encode(branch, IN_PATH).to_string()
}

fn segment_ok(s: &str, owner: bool) -> bool {
    !s.is_empty()
        && s.len() <= 100
        && !s.starts_with('.')
        && !(owner && (s.starts_with('-') || s.ends_with('-')))
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || (!owner && (b == b'.' || b == b'_')))
}

fn number_ok(s: &str) -> Option<u64> {
    (!s.starts_with('0') && s.bytes().all(|b| b.is_ascii_digit()))
        .then(|| s.parse().ok())
        .flatten()
}

/// What a refusal of Dependabot alerts may come from: GitHub's answer does
/// not tell which.
const ALERTS_ACCESS: &str = "Check that the reader's account can read the repository's Dependabot alerts: a GitHub App needs Repository permissions → Dependabot alerts: Read-only and access to the repository, with the added permission approved on the installation; a fine-grained token needs the same permission, a classic token the security_events (or repo) scope. Dependabot alerts must also be enabled for the repository.";

/// What a refusal of workflow runs may come from.
const ACTIONS_ACCESS: &str = "Check that the reader's account can read the repository's GitHub Actions: a GitHub App needs Repository permissions → Actions: Read-only, a fine-grained token the same permission, a classic token the repo scope.";

/// The `after` cursor of the `next` link GitHub sends with a page that has
/// more, `None` on the last page. The link itself is not followed (GitHub
/// may spell it otherwise), and a next page without a cursor is a failure,
/// never the end of the list.
fn next_after(headers: &reqwest::header::HeaderMap) -> HostResult<Option<String>> {
    let Some(next) = headers
        .get("Link")
        .and_then(|v| v.to_str().ok())
        .and_then(|l| l.split(',').find(|l| l.contains(r#"rel="next""#)))
    else {
        return Ok(None);
    };
    // Percent-decoded only: a cursor's `+` is not a space.
    next.trim()
        .strip_prefix('<')
        .and_then(|n| n.split_once('>'))
        .and_then(|(url, _)| url.split_once('?'))
        .and_then(|(_, query)| query.split('&').find_map(|p| p.strip_prefix("after=")))
        .and_then(|v| percent_decode_str(v).decode_utf8().ok())
        .map(|v| Some(v.into_owned()))
        .ok_or_else(|| {
            host_error(
                None,
                "GitHub sent a next page of Dependabot alerts that Branvil cannot follow.",
            )
        })
}

/// A Dependabot alert in Branvil's words.
fn alert(v: &Value) -> HostResult<DependencyAlert> {
    let unreadable = || host_error(None, "GitHub sent a Dependabot alert Branvil cannot read.");
    let id = v["number"]
        .as_u64()
        .filter(|n| *n > 0)
        .ok_or_else(unreadable)?;
    let state = match v["state"].as_str() {
        Some("open") => AlertState::Open,
        Some("fixed") => AlertState::Resolved,
        Some("dismissed" | "auto_dismissed") => AlertState::Dismissed,
        _ => return Err(unreadable()),
    };
    let text = |v: &Value| v.as_str().map(str::to_string);
    let time = |v: &Value| v.as_str().and_then(|t| util::parse_time(t).ok());
    let (dependency, advisory, vulnerability) = (
        &v["dependency"],
        &v["security_advisory"],
        &v["security_vulnerability"],
    );
    Ok(DependencyAlert {
        id: id.to_string(),
        state,
        url: text(&v["html_url"]),
        package: text(&dependency["package"]["name"]),
        ecosystem: text(&dependency["package"]["ecosystem"]),
        manifest_path: text(&dependency["manifest_path"]),
        severity: text(&vulnerability["severity"]).or_else(|| text(&advisory["severity"])),
        identifiers: advisory["identifiers"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default()
            .iter()
            .filter_map(|i| {
                Some(AdvisoryId {
                    kind: text(&i["type"])?,
                    value: text(&i["value"])?,
                })
            })
            .collect(),
        summary: text(&advisory["summary"]),
        description: text(&advisory["description"]),
        affected_versions: text(&vulnerability["vulnerable_version_range"]),
        patched_version: text(&vulnerability["first_patched_version"]["identifier"]),
        created_at: time(&v["created_at"]),
        updated_at: time(&v["updated_at"]),
    })
}

fn host_error(status: Option<u16>, message: impl Into<String>) -> HostError {
    HostError {
        status,
        message: message.into(),
    }
}

fn app_key(app: &AppIdentity) -> String {
    format!("{}/{}", app.app_id, app.installation_id)
}

/// The JWT that authenticates as the app for a few minutes (RS256, issued a
/// minute early against clock drift, as GitHub advises).
fn app_jwt(app: &AppIdentity) -> HostResult<String> {
    let invalid = |m: &str| host_error(Some(400), m);
    let id = number_ok(&app.app_id).ok_or_else(|| invalid("The app id must be a number."))?;
    number_ok(&app.installation_id)
        .ok_or_else(|| invalid("The installation id must be a number."))?;
    let key = rsa_key(&app.private_key)
        .ok_or_else(|| invalid("The private key is not an RSA private key in PEM form."))?;
    let now = Utc::now().timestamp();
    let part = |v: Value| URL_SAFE_NO_PAD.encode(v.to_string());
    let message = format!(
        "{}.{}",
        part(json!({"alg": "RS256", "typ": "JWT"})),
        part(json!({"iss": id, "iat": now - 60, "exp": now + 9 * 60})),
    );
    let mut signature = vec![0; key.public_modulus_len()];
    key.sign(
        &RSA_PKCS1_SHA256,
        &SystemRandom::new(),
        message.as_bytes(),
        &mut signature,
    )
    .map_err(|_| invalid("The private key cannot sign."))?;
    Ok(format!("{message}.{}", URL_SAFE_NO_PAD.encode(signature)))
}

/// An RSA key in PEM: PKCS#1 (what GitHub hands out) or PKCS#8.
fn rsa_key(pem: &str) -> Option<RsaKeyPair> {
    let (label, rest) = pem
        .trim()
        .strip_prefix("-----BEGIN ")?
        .split_once("-----")?;
    let body = rest.strip_suffix(&format!("-----END {label}-----"))?;
    let der = STANDARD
        .decode(body.split_whitespace().collect::<String>())
        .ok()?;
    match label {
        "RSA PRIVATE KEY" => RsaKeyPair::from_der(&der).ok(),
        "PRIVATE KEY" => RsaKeyPair::from_pkcs8(&der).ok(),
        _ => None,
    }
}

impl GitHub {
    pub fn new(config: &GitHubConfig) -> Self {
        let trim = |s: &str| s.trim_end_matches('/').to_string();
        GitHub {
            api: trim(&config.api_url),
            web: trim(&config.web_url),
            git: trim(&config.git_url),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(30))
                .user_agent(concat!("branvil/", env!("CARGO_PKG_VERSION")))
                .build()
                .expect("an HTTP client"),
            cache: Mutex::new(HashMap::new()),
            installations: Mutex::new(HashMap::new()),
        }
    }

    pub fn name(&self) -> &'static str {
        "github"
    }

    pub fn display_name(&self) -> &'static str {
        "GitHub"
    }

    pub fn git_username(&self) -> &'static str {
        "x-access-token"
    }

    pub fn validate_repository(&self, repository: &str) -> AppResult<()> {
        match repository.split_once('/') {
            Some((owner, name)) if segment_ok(owner, true) && segment_ok(name, false) => Ok(()),
            _ => Err(AppError::invalid(format!(
                "'{repository}' is not a GitHub repository name (owner/name)."
            ))),
        }
    }

    pub fn git_url(&self, repository: &str) -> String {
        format!("{}/{repository}.git", self.git)
    }

    pub fn web_url(&self, repository: &str) -> String {
        format!("{}/{repository}", self.web)
    }

    /// `(owner/name, number)` of `https://github.com/owner/name/issues/12`.
    pub fn parse_issue(&self, url: &str) -> Option<(String, u64)> {
        let rest = url.strip_prefix(&self.web)?.strip_prefix('/')?;
        match rest.split('/').collect::<Vec<_>>()[..] {
            [owner, name, "issues", n] if segment_ok(owner, true) && segment_ok(name, false) => {
                Some((format!("{owner}/{name}"), number_ok(n)?))
            }
            _ => None,
        }
    }

    pub fn issue_link(&self, repository: &str, number: u64) -> String {
        format!("{}/{repository}/issues/{number}", self.web)
    }

    pub fn issue_line(&self, repository: &str, url: &str, closes: bool) -> Option<String> {
        let reference = self.issue_reference(repository, url)?;
        Some(format!(
            "{} {reference}",
            if closes { "Closes" } else { "Refs" }
        ))
    }

    pub fn issue_reference(&self, repository: &str, url: &str) -> Option<String> {
        let (issue_repo, n) = self.parse_issue(url)?;
        Some(self.pull_reference(repository, &issue_repo, n))
    }

    /// Issues and pull requests share GitHub's numbers and reference form.
    pub fn pull_reference(&self, repository: &str, target: &str, number: u64) -> String {
        if target.eq_ignore_ascii_case(repository) {
            format!("#{number}")
        } else {
            format!("{target}#{number}")
        }
    }

    /// A code span: GitHub makes no references or links in code.
    pub fn literal(&self, text: &str) -> String {
        format!("`{text}`")
    }

    fn request(&self, method: Method, who: As<'_>, path: &str) -> reqwest::RequestBuilder {
        self.client
            .request(method, format!("{}{path}", self.api))
            .bearer_auth(who.token)
            .header("Accept", "application/vnd.github+json")
            .header("X-GitHub-Api-Version", "2022-11-28")
    }

    /// A refusal; a used-up rate limit (`429`, or `403` saying so) is `429`.
    async fn failure(response: reqwest::Response) -> HostError {
        let status = response.status();
        let header = |name: &str| {
            response
                .headers()
                .get(name)
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        };
        if status == StatusCode::TOO_MANY_REQUESTS
            || (status == StatusCode::FORBIDDEN
                && (header("x-ratelimit-remaining").as_deref() == Some("0")
                    || header("retry-after").is_some()))
        {
            let reset = header("x-ratelimit-reset")
                .and_then(|s| s.parse().ok())
                .and_then(|s| DateTime::from_timestamp(s, 0))
                .map(|t| format!(" until {}", util::format_time(t)))
                .unwrap_or_default();
            return host_error(
                Some(429),
                format!("GitHub's rate limit for the account is used up{reset}; try again later."),
            );
        }
        let detail = response
            .json::<Value>()
            .await
            .ok()
            .and_then(|v| v["message"].as_str().map(str::to_string))
            .unwrap_or_default();
        let what = match status {
            StatusCode::UNAUTHORIZED => "GitHub refused the token".to_string(),
            StatusCode::FORBIDDEN => {
                "GitHub refused the request (permissions or rate limit)".to_string()
            }
            StatusCode::NOT_FOUND => {
                "GitHub has no such resource, or the token cannot see it".to_string()
            }
            s => format!("GitHub answered {}", s.as_u16()),
        };
        host_error(
            Some(status.as_u16()),
            if detail.is_empty() {
                format!("{what}.")
            } else {
                format!("{what}: {detail}")
            },
        )
    }

    fn transport(e: reqwest::Error) -> HostError {
        host_error(
            None,
            format!("GitHub could not be reached: {}", e.without_url()),
        )
    }

    /// A conditional GET: an unchanged resource comes from the cache.
    /// `Ok(None)` is a `404`.
    async fn get(&self, who: As<'_>, path: &str) -> HostResult<Option<Value>> {
        let key = format!("{}\n{path}", who.member);
        let cached = self.cache.lock().unwrap().get(&key).cloned();
        let mut request = self.request(Method::GET, who, path);
        if let Some((etag, _)) = &cached {
            request = request.header("If-None-Match", etag);
        }
        let response = request.send().await.map_err(Self::transport)?;
        match response.status() {
            StatusCode::NOT_MODIFIED => match cached {
                Some((_, body)) => Ok(Some(body)),
                None => Err(host_error(
                    Some(304),
                    "GitHub answered 304 to a first read.",
                )),
            },
            StatusCode::NOT_FOUND => {
                self.cache.lock().unwrap().remove(&key);
                Ok(None)
            }
            s if s.is_success() => {
                let etag = response
                    .headers()
                    .get("ETag")
                    .and_then(|v| v.to_str().ok())
                    .map(str::to_string);
                let body: Value = response.json().await.map_err(Self::transport)?;
                if let Some(etag) = etag {
                    let mut cache = self.cache.lock().unwrap();
                    if cache.len() >= CACHE_MAX {
                        cache.clear();
                    }
                    cache.insert(key, (etag, body.clone()));
                }
                Ok(Some(body))
            }
            _ => Err(Self::failure(response).await),
        }
    }

    async fn get_found(&self, who: As<'_>, path: &str) -> HostResult<Value> {
        self.get(who, path).await?.ok_or_else(|| {
            host_error(
                Some(404),
                "GitHub has no such resource, or the token cannot see it.",
            )
        })
    }

    async fn send(
        &self,
        method: Method,
        who: As<'_>,
        path: &str,
        body: Value,
    ) -> HostResult<Value> {
        let response = self
            .request(method, who, path)
            .json(&body)
            .send()
            .await
            .map_err(Self::transport)?;
        if !response.status().is_success() {
            return Err(Self::failure(response).await);
        }
        response.json().await.map_err(Self::transport)
    }

    fn pull(v: &Value) -> HostResult<HostPull> {
        let number = v["number"]
            .as_u64()
            .ok_or_else(|| host_error(None, "GitHub sent a pull request without a number."))?;
        let merged = v["merged"].as_bool() == Some(true) || v["merged_at"].is_string();
        Ok(HostPull {
            number,
            url: v["html_url"].as_str().unwrap_or_default().to_string(),
            author: v["user"]["login"].as_str().unwrap_or_default().to_string(),
            state: if merged {
                PullState::Merged
            } else if v["state"] == "closed" {
                PullState::Closed
            } else {
                PullState::Open
            },
            merge_sha: merged
                .then(|| v["merge_commit_sha"].as_str().map(str::to_string))
                .flatten(),
        })
    }

    /// A token's login (`GET /user`), or an app's (`<slug>[bot]`, from
    /// `GET /app`) once an installation token was made with it.
    pub async fn check(&self, member: &str, credential: &Credential) -> HostResult<String> {
        match credential {
            Credential::Token(token) => self.field(As { member, token }, "/user", "login").await,
            Credential::App(app) => {
                let jwt = app_jwt(app)?;
                let who = As {
                    member,
                    token: &jwt,
                };
                let slug = self.field(who, "/app", "slug").await?;
                self.installation_token(app, &jwt).await?;
                Ok(format!("{slug}[bot]"))
            }
        }
    }

    async fn field(&self, who: As<'_>, path: &str, field: &str) -> HostResult<String> {
        let v = self.get_found(who, path).await?;
        v[field]
            .as_str()
            .map(str::to_string)
            .ok_or_else(|| host_error(None, format!("GitHub sent no {field}.")))
    }

    pub async fn token(&self, credential: &Credential) -> HostResult<String> {
        let app = match credential {
            Credential::Token(token) => return Ok(token.clone()),
            Credential::App(app) => app,
        };
        let kept = self
            .installations
            .lock()
            .unwrap()
            .get(&app_key(app))
            .cloned();
        match kept {
            Some((token, expires))
                if expires - Utc::now() > chrono::Duration::seconds(RENEW_BEFORE_SECS) =>
            {
                Ok(token)
            }
            _ => self.installation_token(app, &app_jwt(app)?).await,
        }
    }

    /// Makes an installation token and keeps it.
    async fn installation_token(&self, app: &AppIdentity, jwt: &str) -> HostResult<String> {
        let path = format!("/app/installations/{}/access_tokens", app.installation_id);
        let who = As {
            member: "",
            token: jwt,
        };
        let v = self.send(Method::POST, who, &path, json!({})).await?;
        let token = v["token"].as_str().map(str::to_string);
        let expires = v["expires_at"]
            .as_str()
            .and_then(|t| DateTime::parse_from_rfc3339(t).ok());
        let (Some(token), Some(expires)) = (token, expires) else {
            return Err(host_error(None, "GitHub sent no installation token."));
        };
        self.installations
            .lock()
            .unwrap()
            .insert(app_key(app), (token.clone(), expires.to_utc()));
        Ok(token)
    }

    pub async fn branch_tip(
        &self,
        who: As<'_>,
        repository: &str,
        branch: &str,
    ) -> HostResult<Option<String>> {
        let v = self
            .get(
                who,
                &format!(
                    "/repos/{repository}/git/ref/heads/{}",
                    branch_in_path(branch)
                ),
            )
            .await?;
        // A prefix of several branches answers with a list: not this branch.
        Ok(v.and_then(|v| v["object"]["sha"].as_str().map(str::to_string)))
    }

    pub async fn open_pull_with_head(
        &self,
        who: As<'_>,
        repository: &str,
        head: &str,
    ) -> HostResult<Option<HostPull>> {
        let owner = repository.split('/').next().unwrap_or_default();
        let head = utf8_percent_encode(&format!("{owner}:{head}"), IN_QUERY).to_string();
        let v = self
            .get_found(
                who,
                &format!("/repos/{repository}/pulls?state=open&head={head}"),
            )
            .await?;
        v.as_array()
            .and_then(|a| a.first())
            .map(Self::pull)
            .transpose()
    }

    pub async fn create_pull(
        &self,
        who: As<'_>,
        repository: &str,
        head: &str,
        base: &str,
        title: &str,
        body: &str,
    ) -> HostResult<HostPull> {
        let v = self
            .send(
                Method::POST,
                who,
                &format!("/repos/{repository}/pulls"),
                json!({"title": title, "head": head, "base": base, "body": body}),
            )
            .await?;
        Self::pull(&v)
    }

    pub async fn read_pull(
        &self,
        who: As<'_>,
        repository: &str,
        number: u64,
    ) -> HostResult<HostPull> {
        let v = self
            .get_found(who, &format!("/repos/{repository}/pulls/{number}"))
            .await?;
        Self::pull(&v)
    }

    pub async fn change_pull(
        &self,
        who: As<'_>,
        repository: &str,
        number: u64,
        change: &PullChange,
    ) -> HostResult<HostPull> {
        let mut body = serde_json::Map::new();
        if let Some(t) = &change.title {
            body.insert("title".into(), json!(t));
        }
        if let Some(b) = &change.body {
            body.insert("body".into(), json!(b));
        }
        if let Some(open) = change.open {
            body.insert("state".into(), json!(if open { "open" } else { "closed" }));
        }
        let v = self
            .send(
                Method::PATCH,
                who,
                &format!("/repos/{repository}/pulls/{number}"),
                Value::Object(body),
            )
            .await?;
        Self::pull(&v)
    }

    /// Every item under `key` of the pages of `path` (which ends in `?` or
    /// `&`).
    async fn pages(&self, who: As<'_>, path: &str, key: &str) -> HostResult<Vec<Value>> {
        let mut items = Vec::new();
        for page in 1.. {
            let v = self
                .get_found(who, &format!("{path}per_page=100&page={page}"))
                .await?;
            let more = v[key].as_array().cloned().unwrap_or_default();
            let last = more.len() < 100;
            items.extend(more);
            if last {
                break;
            }
        }
        Ok(items)
    }

    /// The check runs of `sha`, and whether no more are to come: every
    /// GitHub Actions workflow run of it finished (one creates the check run
    /// of a job only once it starts the job, after the jobs it needs, a
    /// concurrency group, or an approval), and so did every other app's check
    /// suite that has check runs (one without any never reports). A workflow
    /// run that failed without a check run (an invalid workflow file) is a
    /// failed check of its own.
    pub async fn checks(
        &self,
        who: As<'_>,
        repository: &str,
        sha: &str,
    ) -> HostResult<(Vec<HostCheck>, bool)> {
        // Check runs last: one that a workflow run or suite finished after
        // they were read is among the check runs read.
        let at = format!("/repos/{repository}/commits/{sha}");
        let workflows = self
            .pages(
                who,
                &format!("/repos/{repository}/actions/runs?head_sha={sha}&"),
                "workflow_runs",
            )
            .await
            .map_err(|e| match e.status {
                Some(401 | 403 | 404) => host_error(e.status, format!("{e} {ACTIONS_ACCESS}")),
                _ => e,
            })?;
        let suites = self
            .pages(who, &format!("{at}/check-suites?"), "check_suites")
            .await?;
        let runs = self
            .pages(who, &format!("{at}/check-runs?"), "check_runs")
            .await?;
        let text = |v: &Value, k: &str| v[k].as_str().map(str::to_string);
        let mut checks: Vec<HostCheck> = runs
            .iter()
            .filter_map(|c| {
                Some(HostCheck {
                    id: c["id"].as_u64()?.to_string(),
                    name: text(c, "name")?,
                    status: text(c, "status").unwrap_or_else(|| "queued".into()),
                    conclusion: text(c, "conclusion"),
                    url: text(c, "html_url"),
                    summary: c["output"]["title"].as_str().map(str::to_string),
                })
            })
            .collect();
        let with_runs: Vec<&Value> = runs.iter().map(|c| &c["check_suite"]["id"]).collect();
        checks.extend(
            workflows
                .iter()
                .filter(|w| !with_runs.contains(&&w["check_suite_id"]))
                .map(|w| HostCheck {
                    id: format!("run-{}", w["id"]),
                    name: text(w, "name").unwrap_or_else(|| "workflow".into()),
                    status: text(w, "status").unwrap_or_else(|| "queued".into()),
                    conclusion: text(w, "conclusion"),
                    url: text(w, "html_url"),
                    summary: Some("The workflow run has no job".into()),
                })
                .filter(|c| c.status == "completed" && !c.succeeded()),
        );
        let settled = workflows.iter().all(|w| w["status"] == "completed")
            && suites.iter().all(|s| {
                s["status"] == "completed"
                    || s["latest_check_runs_count"].as_u64().unwrap_or(0) == 0
            });
        Ok((checks, settled))
    }

    /// The log of a check run that is a GitHub Actions job (their ids are the
    /// same).
    pub async fn check_log(
        &self,
        who: As<'_>,
        repository: &str,
        id: &str,
        tail: usize,
    ) -> HostResult<(Vec<u8>, u64)> {
        if number_ok(id).is_none() {
            return Err(host_error(Some(404), "No such check run."));
        }
        let mut response = self
            .request(
                Method::GET,
                who,
                &format!("/repos/{repository}/actions/jobs/{id}/logs"),
            )
            .send()
            .await
            .map_err(Self::transport)?;
        if !response.status().is_success() {
            return Err(Self::failure(response).await);
        }
        // Streamed: only the tail is kept, however long the log is.
        let (mut kept, mut size) = (Vec::new(), 0u64);
        while let Some(chunk) = response.chunk().await.map_err(Self::transport)? {
            size += chunk.len() as u64;
            kept.extend_from_slice(&chunk);
            if kept.len() > 2 * tail {
                kept.drain(..kept.len() - tail);
            }
        }
        kept.drain(..kept.len().saturating_sub(tail));
        Ok((kept, size))
    }

    /// One read of Dependabot alerts, never cached (not polled), and the
    /// `after` cursor of the rest.
    async fn alerts(&self, who: As<'_>, path: &str) -> HostResult<(Value, Option<String>)> {
        let response = self
            .request(Method::GET, who, path)
            .send()
            .await
            .map_err(Self::transport)?;
        if !response.status().is_success() {
            let e = Self::failure(response).await;
            return Err(match e.status {
                Some(401 | 403 | 404) => host_error(e.status, format!("{} {ALERTS_ACCESS}", e)),
                _ => e,
            });
        }
        let next = next_after(response.headers())?;
        Ok((response.json().await.map_err(Self::transport)?, next))
    }

    pub async fn dependency_alerts(
        &self,
        who: As<'_>,
        repository: &str,
        state: AlertState,
        limit: usize,
        after: Option<&str>,
    ) -> HostResult<(Vec<DependencyAlert>, Option<String>)> {
        let state = match state {
            AlertState::Open => "open",
            AlertState::Resolved => "fixed",
            AlertState::Dismissed => "dismissed,auto_dismissed",
        };
        let mut path =
            format!("/repos/{repository}/dependabot/alerts?state={state}&per_page={limit}");
        if let Some(after) = after {
            path.push_str(&format!("&after={}", utf8_percent_encode(after, IN_QUERY)));
        }
        let (v, next) = self.alerts(who, &path).await?;
        let alerts = v
            .as_array()
            .ok_or_else(|| host_error(None, "GitHub sent no list of Dependabot alerts."))?
            .iter()
            .map(alert)
            .collect::<HostResult<_>>()?;
        Ok((alerts, next))
    }

    pub async fn dependency_alert(
        &self,
        who: As<'_>,
        repository: &str,
        id: &str,
    ) -> HostResult<DependencyAlert> {
        if number_ok(id).is_none() {
            return Err(host_error(
                Some(404),
                format!("'{id}' names no Dependabot alert."),
            ));
        }
        let path = format!("/repos/{repository}/dependabot/alerts/{id}");
        alert(&self.alerts(who, &path).await?.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gh() -> GitHub {
        GitHub::new(&GitHubConfig::default())
    }

    #[test]
    fn an_app_key_in_pkcs8_signs_a_jwt_of_ten_minutes_for_the_app() {
        use aws_lc_rs::encoding::{AsDer, Pkcs8V1Der};
        let pair = RsaKeyPair::generate(aws_lc_rs::rsa::KeySize::Rsa2048).unwrap();
        let der: Pkcs8V1Der = pair.as_der().unwrap();
        let pem = format!(
            "-----BEGIN PRIVATE KEY-----\n{}\n-----END PRIVATE KEY-----\n",
            STANDARD.encode(der.as_ref())
        );
        let app = AppIdentity {
            app_id: "12".into(),
            installation_id: "34".into(),
            private_key: pem,
        };
        let jwt = app_jwt(&app).unwrap();
        let claims: Value = serde_json::from_slice(
            &URL_SAFE_NO_PAD
                .decode(jwt.split('.').nth(1).unwrap())
                .unwrap(),
        )
        .unwrap();
        assert_eq!(claims["iss"], 12);
        let (iat, exp) = (
            claims["iat"].as_i64().unwrap(),
            claims["exp"].as_i64().unwrap(),
        );
        assert_eq!(exp - iat, 600);
        assert!(iat < Utc::now().timestamp());
        let wrong = AppIdentity {
            private_key: "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----".into(),
            ..app.clone()
        };
        assert_eq!(app_jwt(&wrong).unwrap_err().status, Some(400));
        let path_like = AppIdentity {
            installation_id: "34/../x".into(),
            ..app
        };
        assert_eq!(app_jwt(&path_like).unwrap_err().status, Some(400));
    }

    #[test]
    fn the_next_page_of_alerts_is_its_after_cursor_or_a_failure() {
        let link = |value: &str| {
            let mut headers = reqwest::header::HeaderMap::new();
            headers.insert("Link", value.parse().unwrap());
            next_after(&headers)
        };
        assert_eq!(next_after(&Default::default()), Ok(None));
        assert_eq!(
            link(
                r#"<https://api.github.com/repositories/9/dependabot/alerts?per_page=2&after=a+b%2F%3D>; rel="next""#
            ),
            Ok(Some("a+b/=".into()))
        );
        assert_eq!(
            link(r#"<https://x/a?before=z>; rel="prev", <https://x/a?after=y>; rel="next""#),
            Ok(Some("y".into()))
        );
        assert_eq!(link(r#"<https://x/a?before=z>; rel="prev""#), Ok(None));
        for unfollowable in [
            r#"<https://x/a?per_page=30&page=2>; rel="next""#,
            r#"<https://x/a>; rel="next""#,
        ] {
            assert_eq!(
                link(unfollowable).unwrap_err().status,
                None,
                "{unfollowable}"
            );
        }
    }

    #[test]
    fn repository_names_are_owner_and_name() {
        let g = gh();
        for ok in ["GuildBotics/GuildBotics", "a-b/c.d_e", "o/n"] {
            assert!(g.validate_repository(ok).is_ok(), "{ok}");
        }
        for bad in ["", "o", "o/n/x", "o/.n", "o_x/n", "-/n", "o/n?x", "../n"] {
            assert!(g.validate_repository(bad).is_err(), "{bad}");
        }
        assert_eq!(g.git_url("o/n"), "https://github.com/o/n.git");
    }

    #[test]
    fn branch_names_are_encoded_in_urls() {
        assert_eq!(
            branch_in_path("ticket/c++#2&x%"),
            "ticket/c%2B%2B%232%26x%25"
        );
        assert_eq!(
            utf8_percent_encode("o:fix/c++", IN_QUERY).to_string(),
            "o%3Afix%2Fc%2B%2B"
        );
    }

    #[test]
    fn issues_are_referenced_from_the_pull_request_repository() {
        let g = gh();
        let url = "https://github.com/acme/demo/issues/12";
        assert_eq!(
            g.issue_line("acme/demo", url, true).as_deref(),
            Some("Closes #12")
        );
        assert_eq!(
            g.issue_line("ACME/Demo", url, false).as_deref(),
            Some("Refs #12")
        );
        assert_eq!(
            g.issue_line("acme/other", url, true).as_deref(),
            Some("Closes acme/demo#12")
        );
        for bad in [
            "https://github.com/acme/demo/pull/12",
            "https://github.com/acme/demo/issues/012",
            "https://github.com/acme/demo/issues/12/x",
            "http://127.0.0.1:7878/acme/demo/-/tickets/12",
        ] {
            assert_eq!(g.issue_line("acme/demo", bad, true), None, "{bad}");
        }
    }
}
