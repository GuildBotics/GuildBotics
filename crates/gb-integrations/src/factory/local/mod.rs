mod chat;
mod code;
mod store;
mod tickets;
pub(super) use chat::{LocalChat, LocalListener};
use gb_ports::*;
use serde_json::{Value, json};
use std::path::PathBuf;
use store::*;
pub(super) struct LocalService {
    store: Store,
    actor: String,
    person_id: String,
    checks: String,
}
impl LocalService {
    pub(super) fn new(
        root: PathBuf,
        scope: &str,
        actor: &str,
        person_id: &str,
        _display: &str,
        checks: &str,
    ) -> Self {
        Self {
            store: Store {
                root,
                scope: crate::WriteScope::new(scope),
            },
            actor: actor.into(),
            person_id: person_id.into(),
            checks: if checks.is_empty() { "success" } else { checks }.into(),
        }
    }
    fn author_type(&self, a: &str) -> &str {
        if a == self.actor { "Assistant" } else { "User" }
    }
    fn comment_value(&self, v: &Value) -> Value {
        json!({"id":v["id"],"body":v["body"],"author":v["author"],"author_type":self.author_type(text(v,"author")),"created_at":v["created_at"],"url":null})
    }
    fn write_comment(
        &self,
        url: &str,
        kind: WorkKind,
        body: &str,
        status: Option<&WorkflowStatus>,
    ) -> Result<CommentWritten, ServiceError> {
        let _g = lock()?;
        let r = Ref::locate(url, kind)?;
        let mut v = self.store.load(&r)?;
        let e = entry(&v, "comment", &self.actor, body, status);
        v["comments"]
            .as_array_mut()
            .ok_or(ServiceError::Failed)?
            .push(e.clone());
        self.store.save(&r, &mut v)?;
        Ok(CommentWritten {
            id: text(&e, "id").into(),
            url: None,
            author: self.actor.clone(),
            created_at: Some(text(&e, "created_at").into()),
            target: r.target(&v),
        })
    }
    fn react(
        &self,
        url: &str,
        kind: WorkKind,
        id: &str,
        reaction: SemanticReaction,
    ) -> Result<ReactionAdded, ServiceError> {
        let _g = lock()?;
        let r = Ref::locate(url, kind)?;
        let mut v = self.store.load(&r)?;
        let mut found = false;
        for key in ["comments", "reviews", "review_comments"] {
            if let Some(entries) = v[key].as_array_mut()
                && let Some(e) = entries.iter_mut().find(|e| text(e, "id") == id)
            {
                if e.get("reactions").is_none() {
                    e["reactions"] = json!([])
                }
                e["reactions"]
                    .as_array_mut()
                    .ok_or(ServiceError::Failed)?
                    .push(json!({"author":self.actor,"reaction":reaction}));
                found = true;
                break;
            }
        }
        if !found {
            return Err(ServiceError::NotFound);
        }
        self.store.save(&r, &mut v)?;
        Ok(ReactionAdded {
            reaction,
            id: id.into(),
        })
    }
    fn task(&self, r: &Ref, v: &Value) -> Task {
        Task {
            id: Some(r.url()),
            url: Some(r.url()),
            title: text(v, "title").into(),
            description: text(v, "body").into(),
            provider: "local".into(),
            label: r.label(),
            created_at: crate::parse_timestamp(text(v, "created_at")),
            comments: array(v, "comments")
                .iter()
                .map(|c| TaskMessage {
                    content: text(c, "body").into(),
                    author: text(c, "author").into(),
                    author_type: self.author_type(text(c, "author")).into(),
                    timestamp: text(c, "created_at").into(),
                    ..Default::default()
                })
                .collect(),
            ..Default::default()
        }
    }
}
#[cfg(test)]
mod tests;
