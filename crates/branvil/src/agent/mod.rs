//! Branvil's authenticated data API; no HTTP client is involved.
#[path = "tickets.rs"]
mod operations;
mod patrol;
pub mod port;
pub mod scope;
mod selection;
use crate::agent::port::*;
use crate::api::tickets;
use crate::api::{AppError, DataApi, MemberApi};
use scope::WriteScope;
use selection::*;
use serde::Serialize;
use serde_json::{Value, json};

pub struct TicketAgent {
    pub(crate) api: DataApi,
    /// The agent acted as: the member's person_id, also its Branvil name.
    pub(crate) person_id: String,
    pub(crate) display_name: String,
    /// Set once the roster's members all exist, so any of them can be named.
    roster_ready: tokio::sync::OnceCell<()>,
    workspace: String,
    pub(crate) scope: WriteScope,
    pub(crate) members: std::collections::BTreeMap<String, String>,
    /// What a test makes this agent's operations do instead of their work.
    #[cfg(feature = "test-support")]
    pub faults: std::sync::Arc<Faults>,
}
/// The failures a test sets for single operations of an agent, by the names
/// of the agent's methods: an operation fails, never reaches the service
/// (`ServiceError::Unavailable`), or is refused as out of scope
/// (`ServiceError::OutOfScope`) before it does anything.
#[cfg(feature = "test-support")]
#[derive(Default)]
pub struct Faults {
    errors: std::sync::Mutex<std::collections::BTreeMap<&'static str, ServiceError>>,
    blocks: std::sync::Mutex<
        std::collections::BTreeMap<&'static str, std::sync::Arc<tokio::sync::Notify>>,
    >,
    calls: std::sync::Mutex<Vec<(&'static str, String)>>,
}
#[cfg(feature = "test-support")]
impl Faults {
    pub fn fail(&self, operation: &'static str, error: ServiceError) {
        self.errors.lock().unwrap().insert(operation, error);
    }
    pub fn clear(&self) {
        self.errors.lock().unwrap().clear();
    }
    /// The operation waits forever once it has notified its arrival, so only
    /// its caller's cancellation ends it.
    pub fn block(&self, operation: &'static str) -> std::sync::Arc<tokio::sync::Notify> {
        let arrived = std::sync::Arc::new(tokio::sync::Notify::new());
        self.blocks
            .lock()
            .unwrap()
            .insert(operation, arrived.clone());
        arrived
    }
    /// The operations called, with the item or repository each was called for.
    pub fn calls(&self) -> Vec<(&'static str, String)> {
        self.calls.lock().unwrap().clone()
    }
}
impl TicketAgent {
    async fn fault(&self, operation: &'static str, subject: &str) -> Result<(), ServiceError> {
        #[cfg(feature = "test-support")]
        {
            let faults = &self.faults;
            faults
                .calls
                .lock()
                .unwrap()
                .push((operation, subject.to_owned()));
            let blocked = faults.blocks.lock().unwrap().get(operation).cloned();
            if let Some(arrived) = blocked {
                arrived.notify_one();
                std::future::pending::<()>().await;
            }
            if let Some(error) = faults.errors.lock().unwrap().get(operation) {
                return Err(error.clone());
            }
        }
        let _ = (operation, subject);
        Ok(())
    }
}
impl TicketAgent {
    pub fn new(api: DataApi, workspace: String, person_id: String, display_name: String) -> Self {
        Self {
            api,
            person_id,
            display_name,
            roster_ready: Default::default(),
            scope: WriteScope::new(&workspace),
            workspace,
            members: Default::default(),
            #[cfg(feature = "test-support")]
            faults: Default::default(),
        }
    }
    /// Shared in-process API, without mutable access to the write boundary.
    pub fn data(&self) -> &DataApi {
        &self.api
    }
    pub fn person_id(&self) -> &str {
        &self.person_id
    }
    pub fn write_scope(&self) -> &WriteScope {
        &self.scope
    }

    pub fn with_members(mut self, members: &std::collections::BTreeMap<String, String>) -> Self {
        self.members = members
            .iter()
            .filter(|(login, _)| !login.trim().is_empty())
            .map(|(login, name)| (patrol::fold(login.trim()), name.clone()))
            .collect();
        self
    }
    pub async fn member(&self) -> Result<MemberApi, ServiceError> {
        // A roster member that has not acted yet can still be assigned or
        // asked to review: every missing one is created before any operation.
        self.roster_ready
            .get_or_try_init(|| {
                self.api.ensure_agents(
                    self.members
                        .iter()
                        .map(|(name, display_name)| (name.clone(), display_name.clone()))
                        .collect(),
                )
            })
            .await
            .map_err(|e| error(e, "ensure_agents"))?;
        self.api
            .agent(&self.person_id, &self.display_name)
            .await
            .map_err(|e| error(e, "agent"))
    }
    pub fn item(&self, url: &str, kind: &str) -> Result<(String, String), ServiceError> {
        let invalid = || {
            diagnostic(
                if kind == "tickets" {
                    "integrations.branvil.api.not_ticket"
                } else {
                    "integrations.branvil.api.not_pull_request"
                },
                [("url", url.to_owned())],
            )
        };
        let path = url
            .strip_prefix(self.api.base_url())
            .and_then(|p| p.strip_prefix('/'))
            .ok_or_else(invalid)?;
        let (repo, tail) = path.split_once("/-/").ok_or_else(invalid)?;
        let (found, key) = tail.split_once('/').ok_or_else(invalid)?;
        if !crate::agent::port::valid_repository_path(repo) || found != kind || number(key).is_err()
        {
            return Err(invalid());
        }
        Ok((repo.into(), key.into()))
    }
    pub fn repository(&self, path: &str) -> Result<(), ServiceError> {
        if crate::agent::port::valid_repository_path(path) {
            Ok(())
        } else {
            Err(diagnostic(
                "integrations.branvil.api.not_path",
                [("path", path.to_owned())],
            ))
        }
    }
    pub fn writable(&self, repo: &str) -> Result<(), ServiceError> {
        self.scope.authorize(repo).map(|_| ()).inspect_err(|error| {
            tracing::warn!(
                provider = "branvil",
                key = error.translation_key(),
                target = repo
            );
        })
    }
    fn related(&self, urls: impl IntoIterator<Item = String>) -> Result<(), ServiceError> {
        for url in urls {
            let (repo, _) = self.item(&url, "tickets")?;
            self.writable(&repo)?;
        }
        Ok(())
    }
    fn target_ticket(&self, t: &tickets::TicketView) -> WorkTarget {
        WorkTarget {
            kind: WorkKind::Ticket,
            url: t.url.clone(),
            title: t.title.clone(),
            label: format!("#{}", t.key),
            closed: false,
        }
    }
    fn on_board(&self, t: &tickets::TicketView) -> bool {
        t.repository
            .rsplit_once('/')
            .is_some_and(|(w, _)| w.eq_ignore_ascii_case(&self.workspace))
    }
    pub fn comment_body(
        &self,
        body: &str,
        status: Option<&WorkflowStatus>,
        author: &str,
    ) -> String {
        let body = body.trim_end();
        let addressed = mentions(body, author);
        if status.is_some() && self.author_type(author) == "User" && !addressed {
            format!("@{author}\n\n{body}")
        } else {
            body.into()
        }
    }
    pub fn author_type(&self, author: &str) -> &'static str {
        if author.is_empty() {
            ""
        } else if patrol::fold(author) == patrol::fold(&self.person_id) {
            "Assistant"
        } else {
            "User"
        }
    }
    pub fn page(
        &self,
        m: &MemberApi,
        resource: Resource,
        subject: &str,
        conditions: &Value,
        continuation: &str,
    ) -> Result<crate::api::PageReq, ServiceError> {
        m.page(
            format!("{}:{subject}:{conditions}", resource.name()),
            Some(conditions["page_size"].as_u64().unwrap_or(30) as usize),
            (!continuation.is_empty()).then_some(continuation),
        )
        .map_err(|e| error(e, "page"))
    }
    pub async fn ticket_view(&self, url: &str) -> Result<tickets::TicketView, ServiceError> {
        let (r, k) = self.item(url, "tickets")?;
        self.member()
            .await?
            .tickets_get_ticket(&r, &k)
            .await
            .map_err(|e| error(e, "tickets_get_ticket"))
    }
}
pub fn mentions(body: &str, name: &str) -> bool {
    if name.is_empty() {
        return false;
    }
    let bytes = body.as_bytes();
    let word = |b: u8| b.is_ascii_alphanumeric() || b == b'_';
    bytes.iter().enumerate().any(|(at, b)| {
        if *b != b'@' || at > 0 && (word(bytes[at - 1]) || b".@-".contains(&bytes[at - 1])) {
            return false;
        }
        let end = at + 1 + name.len();
        if !bytes
            .get(at + 1..end)
            .is_some_and(|v| v.eq_ignore_ascii_case(name.as_bytes()))
        {
            return false;
        }
        !bytes.get(end).is_some_and(|b| {
            word(*b)
                || *b == b'-'
                || *b == b'.' && bytes.get(end + 1).is_some_and(|n| word(*n) || *n == b'-')
        })
    })
}
fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
fn value(v: impl Serialize) -> Result<Value, ServiceError> {
    serde_json::to_value(v).map_err(|_| diagnostic("integrations.branvil.api.invalid", []))
}
fn decode<T: serde::de::DeserializeOwned>(v: Value) -> Result<T, ServiceError> {
    serde_json::from_value(v).map_err(|_| diagnostic("integrations.branvil.api.invalid", []))
}
pub fn number(key: &str) -> Result<u64, ServiceError> {
    if key.is_empty()
        || key.len() > 10
        || key.starts_with('0')
        || !key.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(ServiceError::NotFound);
    }
    key.parse().map_err(|_| ServiceError::NotFound)
}
pub fn diagnostic<const N: usize>(key: &'static str, args: [(&str, String); N]) -> ServiceError {
    let class = match key {
        "integrations.branvil.api.not_found"
        | "integrations.branvil.api.not_ticket"
        | "integrations.branvil.api.not_pull_request"
        | "integrations.branvil.api.not_path" => ServiceErrorClass::NotFound,
        "integrations.branvil.api.unsupported"
        | "integrations.branvil.api.foreign_ticket"
        | "integrations.branvil.api.closes_foreign" => ServiceErrorClass::Unsupported,
        "integrations.branvil.api.stopped" => ServiceErrorClass::Unavailable,
        _ => ServiceErrorClass::Failure,
    };
    classified_diagnostic(class, key, args)
}
fn classified_diagnostic<const N: usize>(
    class: ServiceErrorClass,
    key: &'static str,
    args: [(&str, String); N],
) -> ServiceError {
    ServiceError::Diagnostic {
        class,
        key,
        args: args.into_iter().map(|(k, v)| (k.to_owned(), v)).collect(),
    }
}
/// Translate Branvil failures into the diagnostics shared by service and setup callers.
pub fn error(e: AppError, operation: &str) -> ServiceError {
    // AppError's contract guarantees secret-free messages; never substitute a raw transport body.
    if e.code == "unavailable" && e.message == "integrations.branvil.api.stopped" {
        return diagnostic("integrations.branvil.api.stopped", []);
    }
    if e.code == "closes_foreign" {
        return match e.closing_link {
            Some((repository, url)) => diagnostic(
                "integrations.branvil.api.closes_foreign",
                [("repository", repository), ("url", url)],
            ),
            None => ServiceError::InvalidResult,
        };
    }
    let message = e.message.chars().take(500).collect();
    match e.status.as_u16() {
        401 => return diagnostic("integrations.branvil.api.authentication", []),
        404 => return diagnostic("integrations.branvil.api.not_found", [("message", message)]),
        422 => {
            return diagnostic(
                "integrations.branvil.api.unsupported",
                [("message", message)],
            );
        }
        _ => {}
    }
    match e.code {
        "head_moved" => diagnostic("integrations.branvil.api.head_moved", []),
        "label_undefined" => diagnostic(
            "integrations.branvil.api.label_undefined",
            [("message", message)],
        ),
        "invalid_cursor" => ServiceError::InvalidContinuation,
        _ => classified_diagnostic(
            if e.code == "unavailable" {
                ServiceErrorClass::Unavailable
            } else {
                ServiceErrorClass::Failure
            },
            "integrations.branvil.api.refused",
            [
                ("operation", operation.to_owned()),
                ("status", e.status.as_u16().to_string()),
                ("code", e.code.to_owned()),
                ("message", message),
            ],
        ),
    }
}
fn reaction(r: SemanticReaction) -> &'static str {
    match r {
        SemanticReaction::Ack => "ack",
        SemanticReaction::Agree => "agree",
        SemanticReaction::Celebrate => "celebrate",
        SemanticReaction::Support => "support",
    }
}
#[cfg(all(test, feature = "test-support"))]
mod ticket_parity;

/// The embedding host supplies a fresh roster at each repair sweep.
pub type ActiveAgents = std::sync::Arc<
    dyn Fn() -> Result<std::collections::BTreeMap<String, String>, String> + Send + Sync,
>;
impl DataApi {
    pub fn attach_active_agents(&self, roster: ActiveAgents) {
        let _ = self.app.active_agents.set(roster);
    }
}

fn move_lane(l: MoveLane) -> String {
    match l {
        MoveLane::Ready => "ready",
        MoveLane::InProgress => "in_progress",
        MoveLane::Done => "done",
    }
    .into()
}
fn create_lane(l: CreateLane) -> String {
    match l {
        CreateLane::New => "new",
        CreateLane::Ready => "ready",
        CreateLane::InProgress => "in_progress",
    }
    .into()
}

mod code;
mod code_patrol;
pub mod owner_git;
pub mod pr_diff_lines;
pub mod pr_patrol;
pub mod pr_readiness;
/// The label of the tokens lent to runs for their git transfers.
pub const GIT_TOKEN_LABEL: &str = "guildbotics-git";

pub struct CodeAgent {
    agent: TicketAgent,
    git_token: tokio::sync::Mutex<Option<crate::api::members::IssuedToken>>,
}
impl std::ops::Deref for CodeAgent {
    type Target = TicketAgent;
    fn deref(&self) -> &Self::Target {
        &self.agent
    }
}
impl CodeAgent {
    pub fn new(api: DataApi, workspace: String, person_id: String, display_name: String) -> Self {
        Self {
            agent: TicketAgent::new(api, workspace, person_id, display_name),
            git_token: Default::default(),
        }
    }
    pub fn with_members(mut self, members: &std::collections::BTreeMap<String, String>) -> Self {
        self.agent = self.agent.with_members(members);
        self
    }
    fn target(&self, v: &Value, kind: WorkKind) -> WorkTarget {
        let key = if kind == WorkKind::Ticket {
            text(v, "key")
        } else {
            text(v, "label")
        };
        WorkTarget {
            kind,
            url: text(v, "url").into(),
            title: text(v, "title").into(),
            label: if kind == WorkKind::Ticket {
                format!("#{key}")
            } else {
                key.into()
            },
            closed: false,
        }
    }
    pub async fn pull_view(&self, url: &str) -> Result<crate::api::pulls::PullView, ServiceError> {
        let (r, k) = self.item(url, "pulls")?;
        self.member()
            .await?
            .pulls_get(&r, number(&k)?)
            .await
            .map_err(|e| error(e, "pulls_get"))
    }
    async fn pull(&self, url: &str) -> Result<Value, ServiceError> {
        value(self.pull_view(url).await?)
    }
}

#[cfg(test)]
mod code_parity;
#[cfg(all(test, feature = "test-support"))]
mod code_tests;
