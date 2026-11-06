//! Branvil's authenticated data API; no HTTP client is involved.
mod code;
pub(super) mod owner_git;
mod patrol;
mod tickets;
use super::{IntegrationEvent, IntegrationObserver};
use crate::WriteScope;
use branvil::api::{AppError, DataApi, MemberApi};
use gb_ports::*;
use serde::Serialize;
use serde_json::{Value, json};

pub(super) struct BranvilService {
    api: DataApi,
    observer: IntegrationObserver,
    token: String,
    username: String,
    person_id: String,
    workspace: String,
    scope: WriteScope,
    members: std::collections::BTreeMap<String, String>,
}
impl BranvilService {
    pub(super) fn new(
        api: DataApi,
        token: String,
        username: String,
        workspace: String,
        person_id: String,
        observer: IntegrationObserver,
    ) -> Self {
        Self {
            api,
            observer,
            token,
            username,
            person_id,
            scope: WriteScope::new(&workspace),
            workspace,
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
    fn api_error(&self, e: AppError) -> ServiceError {
        if e.code == "unauthorized" {
            (self.observer)(IntegrationEvent::CredentialFailed {
                provider: "branvil",
                code: "unauthorized",
            });
        }
        error(e, "authenticate")
    }
    async fn member(&self) -> Result<MemberApi, ServiceError> {
        if self.token.is_empty() {
            return Err(ServiceError::Diagnostic {
                class: ServiceErrorClass::Failure,
                key: "integrations.branvil.api.token_missing",
                args: [(
                    "name".into(),
                    gb_config::team::person_secret_name(&self.person_id, "BRANVIL_TOKEN"),
                )]
                .into(),
            });
        }
        if self.username.is_empty() {
            return Err(ServiceError::Diagnostic {
                class: ServiceErrorClass::Failure,
                key: "integrations.branvil.api.username_missing",
                args: Default::default(),
            });
        }
        let m = self
            .api
            .authenticate(self.token.clone())
            .await
            .map_err(|e| self.api_error(e))?;
        let name = m.identity().await.map_err(|e| self.api_error(e))?.name;
        if !name.eq_ignore_ascii_case(&self.username) {
            (self.observer)(IntegrationEvent::CredentialFailed {
                provider: "branvil",
                code: "identity_mismatch",
            });
            return Err(diagnostic(
                "integrations.branvil.api.identity_mismatch",
                [("name", name), ("username", self.username.clone())],
            ));
        }
        Ok(m)
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
    fn related(&self, urls: impl IntoIterator<Item = String>) -> Result<(), ServiceError> {
        for url in urls {
            let (repo, _) = self.item(&url, "tickets")?;
            self.writable(&repo)?;
        }
        Ok(())
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
    fn relative(&self, v: &Value) -> Value {
        json!({"kind":"ticket","url":v["url"],"title":v["title"],"label":format!("#{}",text(v,"key")),"state":v["state"]})
    }
    fn on_board(&self, v: &Value) -> bool {
        text(v, "repository")
            .rsplit_once('/')
            .is_some_and(|(workspace, _)| workspace.eq_ignore_ascii_case(&self.workspace))
    }
    fn ticket_value(&self, v: Value) -> Value {
        let target = self.target(&v, WorkKind::Ticket);
        json!({"kind":"ticket","url":target.url,"title":target.title,"label":target.label,"key":v["key"],"tracker":v["repository"],"repository":v["repository"],"body":v["body"],"state":v["state"],"assignees":v["assignee"]["name"].as_str().map(|s|vec![s]).unwrap_or_default(),"labels":v["labels"],"lane":if self.on_board(&v){v["lane"].clone()}else{Value::Null},"parent":v["parent"].as_object().map(|_|self.relative(&v["parent"])),"children":v["children"].as_array().into_iter().flatten().map(|x|self.relative(x)).collect::<Vec<_>>(),"blocked_by":v["blocked_by"].as_array().into_iter().flatten().map(|x|self.relative(x)).collect::<Vec<_>>(),"blocks":v["blocks"].as_array().into_iter().flatten().map(|x|self.relative(x)).collect::<Vec<_>>(),"interactive":v["interactive"],"interactive_ancestor":v["interactive_ancestor"]["url"].as_str()})
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
        } else if patrol::fold(author) == patrol::fold(&self.username) {
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
        let (r, k) = self.item(url, "tickets")?;
        value(
            self.member()
                .await?
                .tickets_get_ticket(&r, &k)
                .await
                .map_err(|e| error(e, "tickets_get_ticket"))?,
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
fn error(e: AppError, operation: &str) -> ServiceError {
    // AppError's contract guarantees secret-free messages; never substitute a raw transport body.
    if e.code == "unavailable" && e.message == "integrations.branvil.api.stopped" {
        return diagnostic("integrations.branvil.api.stopped", []);
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
#[cfg(test)]
mod tests;
#[cfg(test)]
mod ticket_parity;

#[cfg(test)]
mod code_parity;
#[cfg(test)]
mod readiness_parity;
