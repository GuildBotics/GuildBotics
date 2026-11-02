//! Resolve Markdown references for display. Parsing and target lookup are
//! shared with the upstream publisher; the GUI never interprets references.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::app::App;
use crate::auth::Caller;
use crate::domain::{repo_by_path, repos};
use crate::error::AppResult;
use crate::model::{Pull, Repo, Ticket};
use crate::references::{self, Kind, Reference};
use crate::store::{self, Rd};

pub enum Item {
    Ticket(Box<Ticket>),
    Pull(Box<Pull>),
}

/// A missing target belongs to neither display linking nor host rewriting.
/// Storage errors, unlike a missing repository, must not be hidden.
pub fn resolve(r: &impl Rd, own: &Repo, reference: &Reference) -> AppResult<Option<(Repo, Item)>> {
    let repo = match &reference.repository {
        None => own.clone(),
        Some(path) => match repo_by_path(r, path) {
            Ok(repo) => repo,
            Err(e) if e.code == "not_found" => return Ok(None),
            Err(e) => return Err(e),
        },
    };
    let n = reference.number;
    let item = match reference.kind {
        Kind::Ticket => r
            .get::<Ticket>(store::TICKETS, &Ticket::key(&repo.id, n))?
            .map(|t| Item::Ticket(Box::new(t))),
        Kind::Pull => r
            .get::<Pull>(store::PULLS, &Pull::key(&repo.id, n))?
            .map(|p| Item::Pull(Box::new(p))),
    };
    Ok(item.map(|item| (repo, item)))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub source: String,
}

#[derive(Serialize)]
pub struct View {
    /// Original Markdown with resolved prose references replaced by HTML
    /// anchors. Still untrusted content: clients must sanitize after rendering.
    pub markdown: String,
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
        // These are HTML attribute text embedded in Markdown (including
        // table rows); raw pipes and line breaks would change its structure.
        .replace('|', "&#124;")
        .replace('\n', "&#10;")
        .replace('\r', "&#13;")
}

pub async fn render(app: &App, caller: &Caller, path: &str, source: String) -> AppResult<View> {
    let own = repos::visible(app, caller, path).await?;
    super::check_text("source", &source, super::MAX_TEXT)?;
    let base = app.base_url.clone();
    app.store.read(move |r| {
        let mut targets = HashMap::new();
        let mut markdown = String::with_capacity(source.len());
        let mut at = 0;
        for (range, reference) in references::find(&source, &base) {
            let target = match targets.entry(reference.clone()) {
                std::collections::hash_map::Entry::Occupied(e) => e.into_mut(),
                std::collections::hash_map::Entry::Vacant(e) => e.insert(resolve(r, &own, &reference)?),
            };
            markdown.push_str(&source[at..range.start]);
            let label = &source[range.clone()];
            if let Some((repo, item)) = target {
                let (kind, state, title, route) = match item {
                    Item::Ticket(t) => ("ticket", t.state.as_str(), t.title.as_str(), "tickets"),
                    Item::Pull(p) => ("pull", if p.merged { "merged" } else { p.state.as_str() }, p.title.as_str(), "pulls"),
                };
                let url = format!("{base}/{}/-/{route}/{}", repo.path, reference.number);
                markdown.push_str(&format!(
                    "<a href=\"{}\" data-branvil-kind=\"{kind}\" data-branvil-state=\"{state}\" title=\"{}\">{}</a>",
                    escape(&url), escape(title), escape(label),
                ));
            } else if reference.url {
                // GFM auto-links bare URLs even when they name nothing. An
                // entity keeps this unresolved URL literal without changing
                // its displayed text (explicit Markdown links stay links).
                markdown.push_str("&#104;");
                markdown.push_str(&label[1..]);
            } else {
                markdown.push_str(label);
            }
            at = range.end;
        }
        markdown.push_str(&source[at..]);
        Ok(View { markdown })
    }).await
}
