//! Branvil's authenticated data API; no HTTP client is involved.
mod code;
pub(super) mod owner_git;
mod patrol;
use crate::WriteScope;
use branvil::api::{DataApi, MemberApi};
use gb_ports::*;
use serde::Serialize;
use serde_json::{Value, json};

/// The label of the tokens lent to runs for their git transfers.
pub const GIT_TOKEN_LABEL: &str = "guildbotics-git";

pub(super) struct BranvilService {
    api: DataApi,
    /// The agent acted as: the member's person_id, also its Branvil name.
    person_id: String,
    display_name: String,
    /// The token lent for this service's git transfers until it is closed.
    git_token: tokio::sync::Mutex<Option<branvil::api::members::IssuedToken>>,
    /// Set once the roster's members all exist, so any of them can be named.
    roster_ready: tokio::sync::OnceCell<()>,
    scope: WriteScope,
    members: std::collections::BTreeMap<String, String>,
}
impl BranvilService {
    pub(super) fn new(
        api: DataApi,
        workspace: String,
        person_id: String,
        display_name: String,
    ) -> Self {
        Self {
            api,
            person_id,
            display_name,
            git_token: Default::default(),
            roster_ready: Default::default(),
            scope: WriteScope::new(&workspace),
            members: Default::default(),
        }
    }
    pub(super) fn with_members(
        mut self,
        members: &std::collections::BTreeMap<String, String>,
    ) -> Self {
        self.members = members
            .iter()
            .filter(|(login, _)| !login.trim().is_empty())
            .map(|(login, name)| (patrol::fold(login.trim()), name.clone()))
            .collect();
        self
    }
    async fn member(&self) -> Result<MemberApi, ServiceError> {
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
    fn item(&self, url: &str, kind: &str) -> Result<(String, String), ServiceError> {
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
        if !crate::valid_repository_path(repo) || found != kind || number(key).is_err() {
            return Err(invalid());
        }
        Ok((repo.into(), key.into()))
    }
    fn repository(&self, path: &str) -> Result<(), ServiceError> {
        if crate::valid_repository_path(path) {
            Ok(())
        } else {
            Err(diagnostic(
                "integrations.branvil.api.not_path",
                [("path", path.to_owned())],
            ))
        }
    }
    fn writable(&self, repo: &str) -> Result<(), ServiceError> {
        self.scope.authorize(repo).map(|_| ()).inspect_err(|error| {
            tracing::warn!(
                provider = "branvil",
                key = error.translation_key(),
                target = repo
            );
        })
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
        }
    }
    fn comment_body(&self, body: &str, status: Option<&WorkflowStatus>, author: &str) -> String {
        let body = body.trim_end();
        let addressed = mentions(body, author);
        if status.is_some() && self.author_type(author) == "User" && !addressed {
            format!("@{author}\n\n{body}")
        } else {
            body.into()
        }
    }
    fn author_type(&self, author: &str) -> &'static str {
        if author.is_empty() {
            ""
        } else if patrol::fold(author) == patrol::fold(&self.person_id) {
            "Assistant"
        } else {
            "User"
        }
    }
    fn comment_value(&self, v: Value) -> Value {
        json!({"id":v["id"],"body":v["body"],"author":v["author"],"author_type":self.author_type(text(&v,"author")),"created_at":v["created_at"],"url":v["url"]})
    }
    fn page(
        &self,
        m: &MemberApi,
        resource: Resource,
        subject: &str,
        conditions: &Value,
        continuation: &str,
    ) -> Result<branvil::api::PageReq, ServiceError> {
        m.page(
            format!("{}:{subject}:{conditions}", resource.name()),
            Some(conditions["page_size"].as_u64().unwrap_or(30) as usize),
            (!continuation.is_empty()).then_some(continuation),
        )
        .map_err(|e| error(e, "page"))
    }
    async fn ticket(&self, url: &str) -> Result<Value, ServiceError> {
        value(
            ::branvil::agent::TicketAgent::new(
                self.api.clone(),
                String::new(),
                self.person_id.clone(),
                self.display_name.clone(),
            )
            .with_members(&self.members)
            .ticket_view(url)
            .await?,
        )
    }
    async fn pull(&self, url: &str) -> Result<Value, ServiceError> {
        let (r, k) = self.item(url, "pulls")?;
        value(
            self.member()
                .await?
                .pulls_get(&r, number(&k)?)
                .await
                .map_err(|e| error(e, "pulls_get"))?,
        )
    }
}
fn mentions(body: &str, name: &str) -> bool {
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
fn number(key: &str) -> Result<u64, ServiceError> {
    if key.is_empty()
        || key.len() > 10
        || key.starts_with('0')
        || !key.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(ServiceError::NotFound);
    }
    key.parse().map_err(|_| ServiceError::NotFound)
}
fn diagnostic<const N: usize>(key: &'static str, args: [(&str, String); N]) -> ServiceError {
    let class = match key {
        "integrations.branvil.api.not_found"
        | "integrations.branvil.api.not_ticket"
        | "integrations.branvil.api.not_pull_request"
        | "integrations.branvil.api.not_path" => ServiceErrorClass::NotFound,
        "integrations.branvil.api.unsupported"
        | "integrations.branvil.api.foreign_ticket"
        | "integrations.branvil.api.closes_foreign"
        | "integrations.branvil.api.every_on_board" => ServiceErrorClass::Unsupported,
        "integrations.branvil.api.stopped" => ServiceErrorClass::Unavailable,
        _ => ServiceErrorClass::Failure,
    };
    ServiceError::Diagnostic {
        class,
        key,
        args: args.into_iter().map(|(k, v)| (k.to_owned(), v)).collect(),
    }
}
use ::branvil::agent::error;
fn reaction(r: SemanticReaction) -> &'static str {
    match r {
        SemanticReaction::Ack => "ack",
        SemanticReaction::Agree => "agree",
        SemanticReaction::Celebrate => "celebrate",
        SemanticReaction::Support => "support",
    }
}
#[cfg(test)]
mod tests;

#[cfg(test)]
mod code_parity;
#[cfg(test)]
mod readiness_parity;
