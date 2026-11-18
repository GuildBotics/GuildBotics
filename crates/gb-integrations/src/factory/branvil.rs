//! Branvil's authenticated data API; no HTTP client is involved.
mod code;
pub(super) mod owner_git;
mod patrol;
use branvil::api::DataApi;
use gb_ports::*;
use serde::Serialize;
use serde_json::{Value, json};

/// The label of the tokens lent to runs for their git transfers.
pub const GIT_TOKEN_LABEL: &str = "guildbotics-git";

pub(super) struct BranvilService {
    agent: ::branvil::agent::TicketAgent,
    git_token: tokio::sync::Mutex<Option<branvil::api::members::IssuedToken>>,
}
impl std::ops::Deref for BranvilService {
    type Target = ::branvil::agent::TicketAgent;
    fn deref(&self) -> &Self::Target {
        &self.agent
    }
}
impl std::ops::DerefMut for BranvilService {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.agent
    }
}
impl BranvilService {
    pub(super) fn new(
        api: DataApi,
        workspace: String,
        person_id: String,
        display_name: String,
    ) -> Self {
        Self {
            agent: ::branvil::agent::TicketAgent::new(api, workspace, person_id, display_name),
            git_token: Default::default(),
        }
    }
    pub(super) fn with_members(
        mut self,
        members: &std::collections::BTreeMap<String, String>,
    ) -> Self {
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
        }
    }
    fn comment_value(&self, v: Value) -> Value {
        json!({"id":v["id"],"body":v["body"],"author":v["author"],"author_type":self.author_type(text(&v,"author")),"created_at":v["created_at"],"url":v["url"]})
    }
    async fn ticket(&self, url: &str) -> Result<Value, ServiceError> {
        value(self.agent.ticket_view(url).await?)
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
fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
fn value(v: impl Serialize) -> Result<Value, ServiceError> {
    serde_json::to_value(v).map_err(|_| diagnostic("integrations.branvil.api.invalid", []))
}
fn decode<T: serde::de::DeserializeOwned>(v: Value) -> Result<T, ServiceError> {
    serde_json::from_value(v).map_err(|_| diagnostic("integrations.branvil.api.invalid", []))
}
use ::branvil::agent::{diagnostic, error, number};
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
