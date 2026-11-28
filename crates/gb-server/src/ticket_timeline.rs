//! Each ticket's work in a window, for finding where it waited: the bars of
//! the GuildBotics runs and conversations, joined to the code host's pull
//! requests, reviews and CI attempts by their URLs.
use crate::activity::{ParsedWindow, Window};
use crate::api::{ApiError, Shared};
use axum::{
    Json,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use branvil::api::{AppResult, MemberApi};
use chrono::{DateTime, Utc};
use gb_observability::timeline::{iso_timestamp, parse_timestamp};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

fn time(v: &Value, key: &str) -> Option<DateTime<Utc>> {
    parse_timestamp(text(v, key)).map(|t| t.to_utc())
}

/// How far back the last thing that ran on a ticket before the window is
/// looked for, to say what the window's first wait followed.
const LOOKBACK_DAYS: i64 = 14;

pub(crate) async fn read(
    State(api): State<Shared>,
    headers: HeaderMap,
    query: Result<Query<Window>, axum::extract::rejection::QueryRejection>,
) -> Response {
    let (_, ParsedWindow { start, end, .. }, workspace, language) =
        match crate::activity::prepare(&api, &headers, query).await {
            Ok(prepared) => prepared,
            Err(refused) => return refused,
        };
    let (from, to) = (start.to_utc(), end.to_utc());
    let identities = api.identity_root.clone();
    let root = workspace.clone();
    let read = tokio::task::spawn_blocking(move || {
        let config = workspace.join(".guildbotics/config");
        let team = gb_config::team::Team::load(&config, &identities)?;
        let roster = team.screen_summary(&config, &identities)?["members"].clone();
        let since = from - chrono::Duration::days(LOOKBACK_DAYS);
        let history = crate::activity::read(&workspace, &identities, since, to, language)?;
        // What each run worked on: not every ticket it touched on the way.
        let subjects: BTreeMap<String, String> = gb_observability::runs::RunStore::open(&workspace)
            .records_between(since, to)?
            .into_iter()
            .filter_map(|r| Some((r.run_id, r.attributes.get("target.url")?.clone())))
            .collect();
        // What each conversation worked on, and when.
        let touched: BTreeMap<String, Vec<Value>> =
            gb_observability::interactive::InteractiveSessions::open(&workspace)
                .list_between(since, to)?
                .into_iter()
                .filter_map(|r| {
                    Some((
                        text(&r, "trace_id").to_owned(),
                        r["targets"].as_array()?.clone(),
                    ))
                })
                .collect();
        anyhow::Ok((roster, history, subjects, touched))
    })
    .await;
    let (roster, history, subjects, touched) = match read {
        Ok(Ok(read)) => read,
        Ok(Err(e)) => return crate::screens::answer(Err(e), &headers),
        Err(_) => {
            return ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                &headers,
            )
            .into_response();
        }
    };
    let Some(data) = api.branvil.get() else {
        return ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "branvil_stopped", &headers)
            .locale("integrations.branvil.api.stopped", vec![])
            .into_response();
    };
    let members: Vec<Value> = roster
        .as_array()
        .into_iter()
        .flatten()
        .map(
            |m| json!({"person_id":m["person_id"],"name":m["name"],"person_type":m["person_type"]}),
        )
        .collect();
    // A human member acts on the code host as its owner (`member::native::public`).
    let owner = data.owner_name().unwrap_or_default();
    let people: BTreeMap<String, String> = members
        .iter()
        .map(|m| {
            let account = if m["person_type"] == "human" {
                owner.clone()
            } else {
                text(m, "person_id").to_owned()
            };
            (account, text(m, "person_id").to_owned())
        })
        .collect();
    let join = Join {
        data,
        owner: data.owner(),
        people,
        subjects,
        touched,
        from,
        to,
    };
    let joined = async {
        let tickets = join
            .tickets(history["sessions"].as_array().cloned().unwrap_or_default())
            .await?;
        // A workspace not registered on the code host has nothing it closed.
        let summary = match crate::workspace::execution_name(Some(data), &root) {
            Ok(Some(name)) => join.closed(&name).await?,
            _ => Value::Null,
        };
        AppResult::Ok((tickets, summary))
    }
    .await;
    match joined {
        Ok((tickets, summary)) => Json(
            json!({"start":iso_timestamp(start),"end":iso_timestamp(end),
            "members":members,"summary":summary,"tickets":tickets}),
        )
        .into_response(),
        Err(e) => {
            tracing::error!("cannot read the ticket timeline: {e}");
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                &headers,
            )
            .into_response()
        }
    }
}

struct Join<'a> {
    data: &'a branvil::api::DataApi,
    owner: MemberApi,
    /// person_id by code-host account name.
    people: BTreeMap<String, String>,
    /// The URL each run worked on, by run id.
    subjects: BTreeMap<String, String>,
    /// The targets of each conversation, with when it worked on them.
    touched: BTreeMap<String, Vec<Value>>,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
}

/// The tickets and pull requests a session's links name.
type Named = BTreeSet<(String, String)>;

/// The bars of one session: one per stretch it actually ran.
fn bars(session: &Value) -> Vec<Value> {
    let segments = session["segments"]
        .as_array()
        .cloned()
        .unwrap_or_else(|| vec![session.clone()]);
    let last = segments.len().saturating_sub(1);
    segments
        .iter()
        .enumerate()
        .map(|(i, segment)| {
            json!({"trace_id":session["trace_id"],"person_id":session["person_id"],
                "mode":session["mode"],"title":session["title"],
                "started_at":segment["started_at"],"ended_at":segment["ended_at"],
                // Only the last stretch has the run's own outcome.
                "outcome":if i == last {session["outcome"].clone()} else {segment["status"].clone()}})
        })
        .collect()
}

impl Join<'_> {
    fn person(&self, account: &str) -> String {
        self.people
            .get(account)
            .cloned()
            .unwrap_or_else(|| account.to_owned())
    }

    fn within(&self, at: Option<DateTime<Utc>>) -> bool {
        at.is_some_and(|t| t >= self.from && t <= self.to)
    }

    fn named(&self, session: &Value) -> (Named, Named) {
        let (mut tickets, mut pulls) = (BTreeSet::new(), BTreeSet::new());
        for link in session["links"].as_array().into_iter().flatten() {
            let url = text(link, "url");
            if let Some(item) = self.data.item_of(url, "tickets") {
                tickets.insert(item);
            } else if let Some(item) = self.data.item_of(url, "pulls") {
                pulls.insert(item);
            }
        }
        (tickets, pulls)
    }

    async fn tickets(&self, sessions: Vec<Value>) -> AppResult<Vec<Value>> {
        // The tickets of the window's sessions, in the order their work is
        // first seen; sessions that ended before it are only earlier work.
        let mut order: Vec<String> = vec![];
        let (mut worked, mut earlier) = (vec![], vec![]);
        for session in sessions {
            let (mut tickets, pulls) = self.named(&session);
            // A run is the work of its own subject; a conversation, of every
            // ticket and pull request it worked on.
            let mut subject_pulls = pulls.clone();
            if session["mode"] == "workflow" {
                let subject = self.subjects.get(text(&session, "trace_id"));
                let subject = subject.map_or("", String::as_str);
                tickets = self.data.item_of(subject, "tickets").into_iter().collect();
                subject_pulls = self.data.item_of(subject, "pulls").into_iter().collect();
            }
            if time(&session, "ended_at").is_some_and(|end| end < self.from) {
                earlier.push((session, tickets, subject_pulls));
                continue;
            }
            for (repo, number) in &subject_pulls {
                let Ok(number) = number.parse::<u64>() else {
                    continue;
                };
                // A pull request without a ticket of this service has no row.
                if let Ok(pull) = self.owner.pulls_get(repo, number).await {
                    for link in pull.tickets {
                        tickets.extend(self.data.item_of(&link.url, "tickets"));
                    }
                }
            }
            for ticket in &tickets {
                let key = format!("{}#{}", ticket.0, ticket.1);
                if !order.contains(&key) {
                    order.push(key);
                }
            }
            worked.push((session, tickets, pulls));
        }
        let mut result = vec![];
        for key in order {
            let (repo, number) = key.split_once('#').expect("ticket keys");
            let ticket = (repo.to_owned(), number.to_owned());
            result.push(self.ticket(&ticket, &worked, &earlier).await?);
        }
        Ok(result)
    }

    async fn ticket(
        &self,
        ticket: &(String, String),
        worked: &[(Value, Named, Named)],
        earlier: &[(Value, Named, Named)],
    ) -> AppResult<Value> {
        let (repo, key) = ticket;
        let view = self.owner.tickets_get_ticket(repo, key).await?;
        let linked = self
            .all(&format!("timeline-pulls:{repo}#{key}"), |page| {
                self.owner.tickets_ticket_pulls(repo, key, page)
            })
            .await?;
        let mut events = vec![];
        for c in self
            .all(&format!("timeline-comments:{repo}#{key}"), |page| {
                self.owner.tickets_list_comments(repo, key, page)
            })
            .await?
        {
            events.push(json!({"kind":"comment","at":c.created_at,
                "person_id":self.person(&c.author),"url":c.url}));
        }
        let mut views = vec![];
        for link in linked {
            let Some((pull_repo, number)) = self.data.item_of(&link.url, "pulls") else {
                continue;
            };
            let Ok(number) = number.parse::<u64>() else {
                continue;
            };
            let pull = self.owner.pulls_get(&pull_repo, number).await?;
            views.push((pull_repo, number, self.person(&pull.author), pull));
        }
        // A session belongs to the pull request it named, or else to the one
        // its member authored that was open while it ran; the rest is the
        // ticket's own work. None names its member's own: a review.
        let place = |session: &Value, links: &Named| -> Option<(usize, bool)> {
            let person = text(session, "person_id");
            let (start, end) = (time(session, "started_at"), time(session, "ended_at"));
            let named = views
                .iter()
                .position(|(r, n, ..)| links.contains(&(r.clone(), n.to_string())));
            let authored = || {
                views.iter().position(|(.., author, pull)| {
                    author == person
                        && time(&json!({"t":pull.created_at}), "t") <= end
                        && pull
                            .closed_at
                            .as_ref()
                            .is_none_or(|c| time(&json!({"t":c}), "t") >= start)
                })
            };
            named.or_else(authored).map(|i| (i, views[i].2 != person))
        };
        // A conversation's part in this ticket: from when it last worked on
        // something else before this ticket (or its start) until it last
        // worked on this ticket or one of its pull requests. Its writes come
        // at the end of each piece of work, so their times alone would leave
        // only moments.
        let urls: Vec<&str> = std::iter::once(view.url.as_str())
            .chain(views.iter().map(|(.., pull)| pull.url.as_str()))
            .collect();
        let part = |session: &Value| -> Value {
            let mut part = session.clone();
            let targets = self.touched.get(text(session, "trace_id"));
            let (own, others): (Vec<&Value>, Vec<&Value>) = targets
                .into_iter()
                .flatten()
                .partition(|t| urls.contains(&text(t, "target.url")));
            let first = own.iter().filter_map(|t| time(t, "first_at")).min();
            let last = own.iter().filter_map(|t| time(t, "last_at")).max();
            if let (Some(first), Some(last)) = (first, last) {
                if let Some(before) = others
                    .iter()
                    .filter_map(|t| time(t, "last_at"))
                    .filter(|t| *t <= first)
                    .max()
                {
                    part["started_at"] = before.to_rfc3339().into();
                }
                part["ended_at"] = last.to_rfc3339().into();
            }
            part
        };
        let (mut work, mut rows) = (vec![], vec![(vec![], vec![]); views.len()]);
        for (session, tickets, links) in worked {
            if !tickets.contains(ticket) {
                continue;
            }
            let session = &part(session);
            match place(session, links) {
                Some((i, false)) => rows[i].0.extend(bars(session)),
                Some((i, true)) => rows[i].1.extend(bars(session)),
                None => work.extend(bars(session)),
            }
        }
        // The last thing of the ticket that ran before the window since its
        // work started: planning before the start is no work waited on.
        let started = view
            .started_at
            .as_ref()
            .and_then(|t| time(&json!({"t":t}), "t"));
        let mut previous: Option<Value> = None;
        let mut later = |bar: Value| {
            let end = time(&bar, "ended_at");
            if started.is_some_and(|s| end >= Some(s))
                && previous.as_ref().is_none_or(|p| end > time(p, "ended_at"))
            {
                previous = Some(bar);
            }
        };
        for (session, tickets, links) in earlier {
            let on_pull = views
                .iter()
                .any(|(r, n, ..)| links.contains(&(r.clone(), n.to_string())));
            if !tickets.contains(ticket) && !on_pull {
                continue;
            }
            let session = &part(session);
            let review = place(session, links).is_some_and(|(_, review)| review);
            for mut bar in bars(session) {
                bar["kind"] = if review {
                    json!("review")
                } else {
                    bar["mode"].clone()
                };
                later(bar);
            }
        }
        let mut pulls = vec![];
        for ((pull_repo, number, author, pull), (work, reviews)) in views.into_iter().zip(rows) {
            let activity = self.owner.pulls_activity(&pull_repo, number).await?;
            for c in activity.comments {
                events.push(json!({"kind":"comment","at":c.created_at,
                    "person_id":self.person(&c.author),"url":c.url}));
            }
            for r in activity.reviews.iter().filter(|r| !r.reply_only) {
                events.push(
                    json!({"kind":"review","at":r.submitted_at,"verdict":r.verdict,
                    "person_id":self.person(&r.author),"url":r.url}),
                );
            }
            for (kind, at) in [
                ("pull_created", Some(&pull.created_at)),
                ("pull_merged", pull.merged_at.as_ref()),
            ] {
                if let Some(at) = at {
                    events.push(json!({"kind":kind,"at":at,"person_id":author,"url":pull.url}));
                }
            }
            let mut jobs: BTreeMap<String, Vec<Value>> = BTreeMap::new();
            for a in self.owner.pull_attempts(&pull_repo, number).await? {
                let a = serde_json::to_value(a).expect("attempts serialize");
                let end = time(&a, "finished_at").unwrap_or_else(Utc::now);
                if end < self.from {
                    // Only a job a runner ran was something running.
                    if a["started_at"].is_null() {
                        continue;
                    }
                    later(json!({"kind":"job","job":a["job"],"runner":a["runner"],
                        "outcome":a["conclusion"],"started_at":a["started_at"],"ended_at":a["finished_at"]}));
                } else if time(&a, "created_at").is_some_and(|t| t <= self.to) {
                    jobs.entry(text(&a, "job").to_owned()).or_default().push(a);
                }
            }
            pulls.push(json!({"url":pull.url,"label":pull.label,"title":pull.title,
                "state":pull.state,"merged":pull.merged,"person_id":author,
                "created_at":pull.created_at,"merged_at":pull.merged_at,"closed_at":pull.closed_at,
                "work":work,"reviews":reviews,
                "jobs":jobs.into_iter().map(|(name, attempts)| json!({"name":name,"attempts":attempts})).collect::<Vec<_>>()}));
        }
        events.retain(|e| self.within(time(e, "at")));
        events.sort_by(|a, b| text(a, "at").cmp(text(b, "at")));
        Ok(
            json!({"url":view.url,"label":format!("#{}", view.key),"title":view.title,
            "state":view.state,"created_at":view.created_at,"closed_at":view.closed_at,
            "started_at":view.started_at,"previous":previous,"work":work,"pulls":pulls,"events":events}),
        )
    }

    /// What the workspace closed in the window: every ticket and pull
    /// request, including those no bar of the window belongs to.
    async fn closed(&self, workspace: &str) -> AppResult<Value> {
        let query = || branvil::api::tickets::ClosedQuery {
            since: Some(self.from.to_rfc3339()),
            until: Some(self.to.to_rfc3339()),
        };
        let items = self
            .all(&format!("timeline-closed:{workspace}"), |page| {
                self.owner.tickets_closed(workspace, query(), page)
            })
            .await?;
        let count = |kind: &str, merged: bool| {
            items
                .iter()
                .filter(|i| i.kind == kind && (i.merged || !merged))
                .count()
        };
        Ok(
            json!({"closed_tickets":count("ticket", false),"merged_pulls":count("pull_request", true)}),
        )
    }

    /// Every page of one of the owner's collection reads.
    async fn all<T, F, Fut>(&self, binding: &str, read: F) -> AppResult<Vec<T>>
    where
        F: Fn(branvil::api::PageReq) -> Fut,
        Fut: std::future::Future<Output = AppResult<branvil::api::Page<T>>>,
    {
        let mut items = vec![];
        let mut cursor: Option<String> = None;
        loop {
            let page = self
                .owner
                .page(binding.to_owned(), Some(100), cursor.as_deref())?;
            let page = read(page).await?;
            items.extend(page.items);
            match page.next_cursor {
                Some(next) => cursor = Some(next),
                None => return Ok(items),
            }
        }
    }
}
