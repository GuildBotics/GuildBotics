//! The provider boundary: everything that knows an upstream code host. The
//! rest of `upstream/` talks to a host only through `Host` and the neutral
//! types here (`docs/architecture.md`, "The provider boundary").

use std::fmt;

use serde::{Deserialize, Serialize};

use super::github::GitHub;
use crate::config::UpstreamConfig;
use crate::error::{AppError, AppResult};
use crate::model::AccountKind;

/// The longest token accepted.
const MAX_TOKEN: usize = 4096;
/// The longest app private key accepted.
const MAX_KEY: usize = 16 * 1024;

/// A failure the host reported or that happened talking to it, in words a
/// person can act on, never carrying the token.
#[derive(Debug, Clone, PartialEq)]
pub struct HostError {
    pub status: Option<u16>,
    pub message: String,
}

impl fmt::Display for HostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

pub type HostResult<T> = Result<T, HostError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PullState {
    Open,
    Closed,
    Merged,
}

impl PullState {
    pub fn as_str(self) -> &'static str {
        match self {
            PullState::Open => "open",
            PullState::Closed => "closed",
            PullState::Merged => "merged",
        }
    }
}

/// A pull request on the host.
#[derive(Debug, Clone, PartialEq)]
pub struct HostPull {
    pub number: u64,
    pub url: String,
    /// The login that opened it.
    pub author: String,
    pub state: PullState,
    /// The commit the merge made, once merged.
    pub merge_sha: Option<String>,
}

/// Where a dependency alert stands, in Branvil's words.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AlertState {
    Open,
    Resolved,
    Dismissed,
}

/// A dependency vulnerability the host reports for a repository
/// (`docs/api.md`, "Dependency alerts").
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DependencyAlert {
    pub id: String,
    pub state: AlertState,
    pub url: Option<String>,
    pub package: Option<String>,
    pub ecosystem: Option<String>,
    pub manifest_path: Option<String>,
    pub severity: Option<String>,
    pub identifiers: Vec<AdvisoryId>,
    pub summary: Option<String>,
    pub description: Option<String>,
    pub affected_versions: Option<String>,
    /// `None` when no version fixes it.
    pub patched_version: Option<String>,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
}

/// An identifier of the advisory (`GHSA`, `CVE`).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AdvisoryId {
    #[serde(rename = "type")]
    pub kind: String,
    pub value: String,
}

/// The parts of a pull request Branvil sends; `None` leaves one unchanged.
#[derive(Debug, Clone, Default)]
pub struct PullChange {
    pub title: Option<String>,
    pub body: Option<String>,
    pub open: Option<bool>,
}

/// What a member's account on a host is, as the owner set it and as its file
/// keeps it: `{"token": "..."}` or `{"app": {...}}`. Never logged or shown,
/// so it has no `Debug`.
#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum Credential {
    /// A token of the account itself.
    Token(String),
    /// An app installed on the host, of which the host makes short-lived
    /// tokens.
    App(AppIdentity),
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct AppIdentity {
    pub app_id: String,
    pub installation_id: String,
    /// PEM.
    pub private_key: String,
}

impl Credential {
    pub fn kind(&self) -> AccountKind {
        match self {
            Credential::Token(_) => AccountKind::Token,
            Credential::App(_) => AccountKind::App,
        }
    }

    /// Sizes and characters; what the values mean is the host's to check.
    pub fn validate(&self) -> AppResult<()> {
        let fine = |v: &str, max: usize, lines: bool| {
            !v.is_empty()
                && v.len() <= max
                && !v
                    .chars()
                    .any(|c| c.is_control() && !(lines && c.is_whitespace()))
        };
        match self {
            Credential::Token(t) if !fine(t, MAX_TOKEN, false) => Err(AppError::invalid(format!(
                "The token must be 1-{MAX_TOKEN} characters without control characters."
            ))),
            Credential::App(a)
                if !(fine(&a.app_id, 64, false)
                    && fine(&a.installation_id, 64, false)
                    && fine(&a.private_key, MAX_KEY, true)) =>
            {
                Err(AppError::invalid(format!(
                    "The app id and installation id must be 1-64 characters and the private key 1-{MAX_KEY}, without control characters."
                )))
            }
            _ => Ok(()),
        }
    }
}

/// Who reads or writes: the member whose account it is (for the host's
/// per-account answers) and a token of the account.
#[derive(Clone, Copy)]
pub struct As<'a> {
    pub member: &'a str,
    pub token: &'a str,
}

/// One upstream code host.
pub enum Host {
    GitHub(GitHub),
}

macro_rules! each {
    ($self:ident, $h:ident => $e:expr) => {
        match $self {
            Host::GitHub($h) => $e,
        }
    };
}

impl Host {
    /// The provider name kept in records (`github`).
    pub fn name(&self) -> &'static str {
        each!(self, h => h.name())
    }

    /// The name people know it by ("GitHub").
    pub fn display_name(&self) -> &'static str {
        each!(self, h => h.display_name())
    }

    pub fn validate_repository(&self, repository: &str) -> AppResult<()> {
        each!(self, h => h.validate_repository(repository))
    }

    pub fn git_url(&self, repository: &str) -> String {
        each!(self, h => h.git_url(repository))
    }

    pub fn web_url(&self, repository: &str) -> String {
        each!(self, h => h.web_url(repository))
    }

    /// The user name Git sends with a token.
    pub fn git_username(&self) -> &'static str {
        each!(self, h => h.git_username())
    }

    /// The canonical URL of an issue of this host, or `None` when `url` is not
    /// one.
    pub fn issue_url(&self, url: &str) -> Option<String> {
        each!(self, h => h.parse_issue(url).map(|(r, n)| h.issue_link(&r, n)))
    }

    /// The line of a pull request body of `repository` that closes (or only
    /// refers to) the issue at `url`; `None` when `url` is not an issue here.
    pub fn issue_line(&self, repository: &str, url: &str, closes: bool) -> Option<String> {
        each!(self, h => h.issue_line(repository, url, closes))
    }

    /// How a pull request of `repository` refers to the issue at `url`
    /// (`#12`, `owner/other#12`); `None` when `url` is not an issue here.
    pub fn issue_reference(&self, repository: &str, url: &str) -> Option<String> {
        each!(self, h => h.issue_reference(repository, url))
    }

    /// How a pull request of `repository` refers to pull request `number` of
    /// `target`.
    pub fn pull_reference(&self, repository: &str, target: &str, number: u64) -> String {
        each!(self, h => h.pull_reference(repository, target, number))
    }

    /// `text` written so the host makes no reference or link of it.
    pub fn literal(&self, text: &str) -> String {
        each!(self, h => h.literal(text))
    }

    /// Checks a member's credential with the host and answers the login it
    /// acts as.
    pub async fn check(&self, member: &str, credential: &Credential) -> HostResult<String> {
        each!(self, h => h.check(member, credential).await)
    }

    /// A token of a credential, made by the host when the credential is an
    /// app's (kept in memory and made again before it expires).
    pub async fn token(&self, credential: &Credential) -> HostResult<String> {
        each!(self, h => h.token(credential).await)
    }

    pub async fn branch_tip(
        &self,
        who: As<'_>,
        repository: &str,
        branch: &str,
    ) -> HostResult<Option<String>> {
        each!(self, h => h.branch_tip(who, repository, branch).await)
    }

    pub async fn open_pull_with_head(
        &self,
        who: As<'_>,
        repository: &str,
        head: &str,
    ) -> HostResult<Option<HostPull>> {
        each!(self, h => h.open_pull_with_head(who, repository, head).await)
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
        each!(self, h => h.create_pull(who, repository, head, base, title, body).await)
    }

    pub async fn read_pull(
        &self,
        who: As<'_>,
        repository: &str,
        number: u64,
    ) -> HostResult<HostPull> {
        each!(self, h => h.read_pull(who, repository, number).await)
    }

    pub async fn change_pull(
        &self,
        who: As<'_>,
        repository: &str,
        number: u64,
        change: &PullChange,
    ) -> HostResult<HostPull> {
        each!(self, h => h.change_pull(who, repository, number, change).await)
    }

    /// The checks of `sha`, and whether the host has no more of it to come
    /// (none of its runs of checks is still going).
    pub async fn checks(
        &self,
        who: As<'_>,
        repository: &str,
        sha: &str,
    ) -> HostResult<(Vec<crate::model::HostCheck>, bool)> {
        each!(self, h => h.checks(who, repository, sha).await)
    }

    /// The last `tail` bytes of a check run's log and the log's whole size.
    pub async fn check_log(
        &self,
        who: As<'_>,
        repository: &str,
        id: &str,
        tail: usize,
    ) -> HostResult<(Vec<u8>, u64)> {
        each!(self, h => h.check_log(who, repository, id, tail).await)
    }

    /// At most `limit` of the repository's alerts in `state`, from where the
    /// host's cursor `after` points, and the host's cursor of the rest.
    pub async fn dependency_alerts(
        &self,
        who: As<'_>,
        repository: &str,
        state: AlertState,
        limit: usize,
        after: Option<&str>,
    ) -> HostResult<(Vec<DependencyAlert>, Option<String>)> {
        each!(self, h => h.dependency_alerts(who, repository, state, limit, after).await)
    }

    pub async fn dependency_alert(
        &self,
        who: As<'_>,
        repository: &str,
        id: &str,
    ) -> HostResult<DependencyAlert> {
        each!(self, h => h.dependency_alert(who, repository, id).await)
    }
}

/// Every provider this build knows.
pub struct Hosts(Vec<Host>);

impl Hosts {
    pub fn new(config: &UpstreamConfig) -> Self {
        Hosts(vec![Host::GitHub(GitHub::new(&config.github))])
    }

    pub fn get(&self, provider: &str) -> AppResult<&Host> {
        self.0.iter().find(|h| h.name() == provider).ok_or_else(|| {
            let known: Vec<&str> = self.0.iter().map(Host::name).collect();
            AppError::invalid(format!(
                "Unknown provider '{provider}'; known: {}.",
                known.join(", ")
            ))
        })
    }
}
