use super::*;
use crate::{PatrolComment, PatrolPullRequest, PatrolReview, PatrolThread};
use ::branvil::api::{pulls, tickets};
use chrono::{DateTime, Utc};
use std::collections::{BTreeMap, BTreeSet};
fn array<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    v[k].as_array().map(Vec::as_slice).unwrap_or(&[])
}
pub(super) fn fold(v: &str) -> String {
    use unicode_casefold::UnicodeCaseFold;
    v.case_fold().collect()
}
fn reactors(v: &Value) -> BTreeSet<String> {
    array(v, "reactions")
        .iter()
        .map(|r| fold(text(r, "author")))
        .collect()
}
fn status(v: &Value) -> Option<WorkflowStatus> {
    WorkflowStatus::from_payload(&v["meta"])
}
impl BranvilService {
    async fn board_cards(&self, mentioned: bool) -> Result<Vec<Value>, ServiceError> {
        let m = self.member().await?;
        let mut found = vec![];
        let mut cursor = None;
        loop {
            let p = m
                .page(
                    format!("gb:board:{}:{mentioned}", self.workspace),
                    Some(100),
                    cursor.as_deref(),
                )
                .map_err(|e| error(e, "page"))?;
            let (items, next) = if mentioned {
                let page = m
                    .tickets_mentioned(
                        &self.workspace,
                        tickets::MentionQuery {
                            mentions: Some("me".into()),
                            state: Some("open".into()),
                            comments: Some("all".into()),
                        },
                        p,
                    )
                    .await
                    .map_err(|e| error(e, "tickets_mentioned"))?;
                (page.items, page.next_cursor)
            } else {
                let page = m
                    .tickets_cards(
                        &self.workspace,
                        tickets::CardQuery {
                            lane: Some("ready,in_progress".into()),
                            assignee: Some("me".into()),
                            state: Some("open".into()),
                            comments: Some("all".into()),
                        },
                        p,
                    )
                    .await
                    .map_err(|e| error(e, "tickets_cards"))?;
                (page.items, page.next_cursor)
            };
            for item in items {
                found.push(value(item)?)
            }
            cursor = next;
            if cursor.is_none() {
                break;
            }
        }
        Ok(found)
    }
    async fn ticket_card(&self, url: &str) -> Result<Value, ServiceError> {
        let mut card = self.ticket(url).await?;
        let (r, k) = self.item(url, "tickets")?;
        let m = self.member().await?;
        let mut comments = vec![];
        let mut cursor = None;
        loop {
            let p = m
                .page(format!("gb:comments:{url}"), Some(100), cursor.as_deref())
                .map_err(|e| error(e, "page"))?;
            let page = m
                .tickets_list_comments(&r, &k, p)
                .await
                .map_err(|e| error(e, "tickets_list_comments"))?;
            for c in page.items {
                comments.push(value(c)?)
            }
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        let mut pulls = vec![];
        let mut cursor = None;
        loop {
            let p = m
                .page(format!("gb:links:{url}"), Some(100), cursor.as_deref())
                .map_err(|e| error(e, "page"))?;
            let page = m
                .tickets_ticket_pulls(&r, &k, p)
                .await
                .map_err(|e| error(e, "tickets_ticket_pulls"))?;
            for c in page.items {
                pulls.push(value(c)?)
            }
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        card["comments"] = json!(comments);
        card["pull_requests"] = json!(pulls);
        Ok(card)
    }
    pub(super) async fn select_card(
        &self,
        card: &Value,
        finish: bool,
    ) -> Result<Option<Task>, ServiceError> {
        let me = fold(&self.username);
        let assigned = fold(text(&card["assignee"], "name")) == me;
        let lane = text(card, "lane");
        if me.is_empty()
            || text(card, "state") != "open"
            || card["interactive"].as_bool() == Some(true)
            || !card["interactive_ancestor"].is_null()
            || self.scope.authorize(text(card, "repository")).is_err()
            || assigned
                && (!["ready", "in_progress"].contains(&lane)
                    || lane == "ready"
                        && array(card, "blocked_by")
                            .iter()
                            .any(|b| text(b, "state") == "open"))
        {
            return Ok(None);
        }
        let comments = array(card, "comments");
        let messages: Vec<TaskMessage> = comments
            .iter()
            .map(|c| TaskMessage {
                content: text(c, "body").into(),
                author: self
                    .members
                    .get(&fold(text(c, "author")))
                    .filter(|name| !name.is_empty())
                    .map(String::as_str)
                    .unwrap_or_else(|| self.author_type(text(c, "author")))
                    .into(),
                author_type: self.author_type(text(c, "author")).into(),
                timestamp: text(c, "created_at").into(),
                ..Default::default()
            })
            .collect();
        let mentions = |v: &Value| {
            array(v, "mentions")
                .iter()
                .any(|n| n.as_str().is_some_and(|s| fold(s) == me))
        };
        let mut task = Task {
            id: Some(text(card, "url").into()),
            url: Some(text(card, "url").into()),
            title: text(card, "title").into(),
            description: text(card, "body").into(),
            provider: "branvil".into(),
            label: format!("#{}", text(card, "key")),
            status: lane.into(),
            assignee: assigned.then(|| self.person_id.clone()),
            created_at: crate::parse_timestamp(text(card, "created_at")),
            comments: messages.clone(),
            ..Default::default()
        };
        if !assigned {
            if !crate::mention_pending(
                mentions(card),
                messages.iter().zip(comments).map(|(m, c)| (m, mentions(c))),
            ) {
                return Ok(None);
            }
            task.trigger_reason = Some("issue_mention".into());
            return Ok(Some(task));
        }
        let relevant: Vec<TaskMessage> = messages
            .iter()
            .zip(comments)
            .filter(|(m, c)| {
                array(c, "mentions").is_empty() || mentions(c) || m.author_type == "Assistant"
            })
            .map(|(m, _)| m.clone())
            .collect();
        let since: Vec<TaskMessage> =
            crate::since_assignment(&relevant, text(&card["assignee"], "assigned_at"))
                .into_iter()
                .cloned()
                .collect();
        let selected: Vec<_> = messages
            .iter()
            .zip(comments)
            .filter(|(m, _)| since.contains(m))
            .collect();
        let latest = selected.last().and_then(|(_, c)| status(c));
        let pending = crate::mention_pending(
            mentions(card),
            selected.iter().map(|(m, c)| (*m, mentions(c))),
        );
        let linked = crate::linked_pull_state(array(card, "pull_requests").iter().map(|p| {
            (
                if p["merged"].as_bool() == Some(true) {
                    "merged"
                } else {
                    text(p, "state")
                },
                text(p, "updated_at"),
            )
        }));
        let decision = crate::select_ticket(
            lane,
            &since,
            latest.as_ref(),
            pending,
            linked,
            finish,
            Utc::now(),
        );
        if decision.finish {
            self.ticket_move(&task, MoveLane::Done).await?;
        }
        task.trigger_reason = decision.reason.map(str::to_owned);
        Ok(decision.reason.map(|_| task))
    }
    pub(super) async fn ticket_candidates(&self) -> Result<Vec<Task>, ServiceError> {
        let mut unique = BTreeMap::new();
        for card in self
            .board_cards(false)
            .await?
            .into_iter()
            .chain(self.board_cards(true).await?)
        {
            unique.insert(text(&card, "url").to_owned(), card);
        }
        let mut cards: Vec<_> = unique.into_values().collect();
        cards.sort_by(|a, b| {
            let key = |c: &Value| {
                if text(c, "lane") == "ready" {
                    "rank"
                } else {
                    "created_at"
                }
            };
            (text(a, "lane") != "ready", text(a, key(a)))
                .cmp(&(text(b, "lane") != "ready", text(b, key(b))))
        });
        let mut out = vec![];
        for c in cards {
            if let Some(t) = self.select_card(&c, true).await? {
                out.push(t)
            }
        }
        Ok(out)
    }
    pub(super) async fn ticket_refresh(&self, task: &Task) -> Result<Option<Task>, ServiceError> {
        self.select_card(
            &self
                .ticket_card(task.url.as_deref().ok_or(ServiceError::NotFound)?)
                .await?,
            true,
        )
        .await
    }
    pub(super) async fn ticket_can_receive(&self, url: &str) -> Result<bool, ServiceError> {
        Ok(self
            .select_card(&self.ticket_card(url).await?, false)
            .await?
            .is_some())
    }
    pub(super) async fn ticket_move(
        &self,
        task: &Task,
        lane: MoveLane,
    ) -> Result<bool, ServiceError> {
        let (r, k) = self.item(
            task.url.as_deref().ok_or(ServiceError::NotFound)?,
            "tickets",
        )?;
        self.writable(&r)?;
        self.member()
            .await?
            .tickets_put_on_board(
                &r,
                &k,
                tickets::Placement {
                    lane: match lane {
                        MoveLane::Ready => "ready",
                        MoveLane::InProgress => "in_progress",
                        MoveLane::Done => "done",
                    }
                    .into(),
                    before: None,
                    after: None,
                },
            )
            .await
            .map_err(|e| error(e, "tickets_put_on_board"))?;
        Ok(true)
    }
    pub(super) async fn ticket_closed(
        &self,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<Vec<ClosedItem>, ServiceError> {
        let m = self.member().await?;
        let mut out = vec![];
        let mut cursor = None;
        loop {
            let p = m
                .page(
                    format!("gb:closed:{}:{start}:{end}", self.workspace),
                    Some(100),
                    cursor.as_deref(),
                )
                .map_err(|e| error(e, "page"))?;
            let page = m
                .tickets_closed(
                    &self.workspace,
                    tickets::ClosedQuery {
                        since: Some(start.to_rfc3339()),
                        until: Some(end.to_rfc3339()),
                    },
                    p,
                )
                .await
                .map_err(|e| error(e, "tickets_closed"))?;
            for i in page.items {
                out.push(decode(value(i)?)?)
            }
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        Ok(out)
    }
    fn snapshot(&self, p: &Value, a: &Value) -> PatrolPullRequest {
        let mut reviews: Vec<_> = array(a, "reviews")
            .iter()
            .map(|r| PatrolReview {
                author: fold(text(r, "author")),
                body: text(r, "body").into(),
                submitted_at: text(r, "submitted_at").into(),
                commit_oid: text(r, "commit").into(),
                reply_only: r["reply_only"].as_bool().unwrap_or(false),
                reactors: reactors(r),
            })
            .collect();
        let mut threads = vec![];
        for t in array(a, "threads") {
            let comments = array(t, "comments");
            for c in comments {
                reviews.push(PatrolReview {
                    author: fold(text(c, "author")),
                    body: String::new(),
                    submitted_at: text(c, "created_at").into(),
                    commit_oid: text(t, "commit").into(),
                    reply_only: !c["reply_to_id"].is_null(),
                    reactors: BTreeSet::new(),
                })
            }
            if let Some(last) = comments.last() {
                threads.push(PatrolThread {
                    resolved: t["resolved"].as_bool().unwrap_or(false),
                    participants: comments.iter().map(|c| fold(text(c, "author"))).collect(),
                    last_author: fold(text(last, "author")),
                    last_created_at: text(last, "created_at").into(),
                    last_reactors: reactors(last),
                })
            }
        }
        reviews.sort_by(|a, b| a.submitted_at.cmp(&b.submitted_at));
        let mut comments: Vec<_> = array(a, "comments")
            .iter()
            .map(|c| PatrolComment {
                author: fold(text(c, "author")),
                created_at: text(c, "created_at").into(),
                reactors: reactors(c),
                status: status(c),
            })
            .collect();
        comments.sort_by(|a, b| a.created_at.cmp(&b.created_at));
        PatrolPullRequest {
            id: text(p, "url").into(),
            url: text(p, "url").into(),
            label: text(p, "label").into(),
            title: text(p, "title").into(),
            body: text(p, "body").into(),
            open: text(p, "state") == "open",
            is_draft: p["draft"].as_bool().unwrap_or(false),
            created_at: text(p, "created_at").into(),
            author: fold(text(p, "author")),
            head_oid: text(p, "head_sha").into(),
            ready_at: text(p, "ready_at").into(),
            requested_reviewers: array(p, "reviewers")
                .iter()
                .filter_map(Value::as_str)
                .map(fold)
                .collect(),
            reviews,
            comments,
            threads,
        }
    }
    pub(super) async fn pull_interactive(&self, p: &Value) -> Result<bool, ServiceError> {
        for link in array(p, "tickets") {
            let url = text(link, "url");
            if self.item(url, "tickets").is_ok() {
                let t = self.ticket(url).await?;
                if t["interactive"].as_bool() == Some(true) || !t["interactive_ancestor"].is_null()
                {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }
    pub(super) async fn pull_work(
        &self,
        p: &Value,
        a: &Value,
    ) -> Result<Option<Task>, ServiceError> {
        if text(p, "head_sha").is_empty() || self.scope.authorize(text(p, "repository")).is_err() {
            return Ok(None);
        }
        let me = fold(&self.username);
        let task = crate::patrol(
            self,
            &self.snapshot(p, a),
            &me,
            "branvil",
            &self.person_id,
            Utc::now(),
        )
        .await;
        if task.is_some() && fold(text(p, "author")) == me && self.pull_interactive(p).await? {
            return Ok(None);
        }
        Ok(task)
    }
    pub(super) async fn pull_candidates(&self) -> Result<Vec<Task>, ServiceError> {
        let m = self.member().await?;
        let mut out = vec![];
        let mut cursor = None;
        loop {
            let p = m
                .page("gb:patrol".into(), Some(100), cursor.as_deref())
                .map_err(|e| error(e, "page"))?;
            let page = m
                .pulls_patrol(
                    pulls::PatrolQuery {
                        involves: Some("me".into()),
                        state: Some("open".into()),
                        draft: Some("false".into()),
                    },
                    p,
                )
                .await
                .map_err(|e| error(e, "pulls_patrol"))?;
            for p in page.items {
                let v = value(p)?;
                if let Some(t) = self.pull_work(&v, &v["activity"]).await? {
                    out.push(t)
                }
            }
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        Ok(out)
    }
    pub(super) async fn pull_refresh(&self, task: &Task) -> Result<Option<Task>, ServiceError> {
        let url = task
            .pull_request_url
            .as_deref()
            .ok_or(ServiceError::NotFound)?;
        let (r, k) = self.item(url, "pulls")?;
        self.pull_work(
            &self.pull(url).await?,
            &value(
                self.member()
                    .await?
                    .pulls_activity(&r, number(&k)?)
                    .await
                    .map_err(|e| error(e, "pulls_activity"))?,
            )?,
        )
        .await
    }
    pub(super) async fn pull_can_receive(&self, url: &str) -> Result<bool, ServiceError> {
        let (r, k) = self.item(url, "pulls")?;
        if self.scope.authorize(&r).is_err() {
            return Ok(false);
        }
        let p = self.pull(url).await?;
        let me = fold(&self.username);
        if text(&p, "head_sha").is_empty()
            || fold(text(&p, "author")) == me && self.pull_interactive(&p).await?
        {
            return Ok(false);
        }
        let a = value(
            self.member()
                .await?
                .pulls_activity(&r, number(&k)?)
                .await
                .map_err(|e| error(e, "pulls_activity"))?,
        )?;
        Ok(matches!(
            crate::pull_request_work(&self.snapshot(&p, &a), &me, Utc::now()),
            Some("pull_request_feedback" | "pull_request_review")
        ))
    }
    pub(super) async fn pull_draft(&self, url: &str) -> Result<bool, ServiceError> {
        let (r, k) = self.item(url, "pulls")?;
        self.writable(&r)?;
        draft_transition(
            || {
                Box::pin(async {
                    let p = self.pull(url).await?;
                    if text(&p, "state") != "open" || p["draft"].as_bool() == Some(true) {
                        return Ok(None);
                    }
                    Ok(Some(
                        p["revision"].as_u64().ok_or(ServiceError::InvalidResult)?,
                    ))
                })
            },
            |revision| {
                let r = &r;
                let k = &k;
                Box::pin(async move {
                    let patch: pulls::PullPatch =
                        decode(json!({"draft":true,"revision":revision}))?;
                    match self
                        .member()
                        .await?
                        .pulls_update(r, number(k)?, patch)
                        .await
                    {
                        Ok(_) => Ok(true),
                        Err(e) if e.code == "conflict" => Ok(false),
                        Err(e) => Err(error(e, "pulls_patch")),
                    }
                })
            },
        )
        .await
    }
}

// A revision conflict restarts with a fresh read; all other failures terminate.
pub(super) async fn draft_transition<'a>(
    mut read: impl FnMut() -> ServiceFuture<'a, Option<u64>> + Send,
    mut write: impl FnMut(u64) -> ServiceFuture<'a, bool> + Send,
) -> Result<bool, ServiceError> {
    loop {
        let Some(revision) = read().await? else {
            return Ok(false);
        };
        if write(revision).await? {
            return Ok(true);
        }
    }
}
