use super::*;
use crate::api::tickets;
use chrono::{DateTime, Utc};
use std::collections::BTreeMap;
pub(super) fn fold(v: &str) -> String {
    use unicode_casefold::UnicodeCaseFold;
    v.case_fold().collect()
}
impl TicketAgent {
    async fn board_cards(&self, mentioned: bool) -> Result<Vec<tickets::CardView>, ServiceError> {
        let _open = self.api.guard().map_err(|e| error(e, "cards"))?;
        let caller = self
            .member()
            .await?
            .caller()
            .await
            .map_err(|e| error(e, "caller"))?;
        let app = &self.api.app;
        let page = crate::domain::PageReq::all();
        let result = if mentioned {
            tickets::mentioned(
                app,
                &caller,
                &self.workspace,
                tickets::MentionQuery {
                    mentions: Some("me".into()),
                    state: Some("open".into()),
                    comments: Some("all".into()),
                },
                page,
            )
            .await
        } else {
            tickets::cards(
                app,
                &caller,
                &self.workspace,
                tickets::CardQuery {
                    lane: Some("ready,in_progress".into()),
                    assignee: Some("me".into()),
                    state: Some("open".into()),
                    comments: Some("all".into()),
                    ..Default::default()
                },
                page,
            )
            .await
        };
        result.map(|p| p.items).map_err(|e| error(e, "cards"))
    }
    async fn ticket_card(&self, url: &str) -> Result<tickets::CardView, ServiceError> {
        let (r, k) = self.item(url, "tickets")?;
        let _open = self.api.guard().map_err(|e| error(e, "card"))?;
        let caller = self
            .member()
            .await?
            .caller()
            .await
            .map_err(|e| error(e, "caller"))?;
        let repo = crate::domain::repos::visible(&self.api.app, &caller, &r)
            .await
            .map_err(|e| error(e, "repo"))?;
        let base = self.api.base_url().to_owned();
        self.api
            .app
            .store
            .read(move |rd| {
                let (repo, t) = tickets::ticket_by_key(rd, &repo, &k)?;
                tickets::card_view(rd, &base, &repo, &t, true)
            })
            .await
            .map_err(|e| error(e, "card"))
    }
    pub(super) async fn select_card(
        &self,
        card: &tickets::CardView,
        finish: bool,
    ) -> Result<Option<Task>, ServiceError> {
        let t = &card.ticket;
        let me = fold(&self.person_id);
        let assigned = fold(t.assignee.as_ref().map_or("", |a| a.name.as_str())) == me;
        let lane = t.lane.as_str();
        if me.is_empty()
            || t.state.as_str() != "open"
            || t.interactive
            || t.interactive_ancestor.is_some()
            || self.scope.authorize(&t.repository).is_err()
            || assigned
                && (!["ready", "in_progress"].contains(&lane)
                    || lane == "ready" && t.blocked_by.iter().any(|b| b.state == "open"))
        {
            return Ok(None);
        }
        let comments = &card.comments;
        let messages: Vec<TaskMessage> = comments
            .iter()
            .map(|c| TaskMessage {
                content: c.body.as_str().into(),
                author: self
                    .members
                    .get(&fold(c.author.as_str()))
                    .filter(|name| !name.is_empty())
                    .map(String::as_str)
                    .unwrap_or_else(|| self.author_type(c.author.as_str()))
                    .into(),
                author_type: self.author_type(c.author.as_str()).into(),
                timestamp: c.created_at.as_str().into(),
                ..Default::default()
            })
            .collect();
        let mentions = |names: &[String]| names.iter().any(|n| fold(n) == me);
        let mut task = Task {
            repair: t.repair,
            id: Some(t.url.as_str().into()),
            url: Some(t.url.as_str().into()),
            title: t.title.as_str().into(),
            description: t.body.as_str().into(),
            provider: "branvil".into(),
            label: format!("#{}", t.key.as_str()),
            status: lane.into(),
            assignee: assigned.then(|| self.person_id.clone()),
            created_at: parse_timestamp(t.created_at.as_str()),
            comments: messages.clone(),
            ..Default::default()
        };
        if !assigned {
            if !mention_pending(
                mentions(&t.mentions),
                messages
                    .iter()
                    .zip(comments)
                    .map(|(m, c)| (m, mentions(c.mentions.as_deref().unwrap_or_default()))),
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
                c.mentions.as_ref().is_none_or(Vec::is_empty)
                    || mentions(c.mentions.as_deref().unwrap_or_default())
                    || m.author_type == "Assistant"
            })
            .map(|(m, _)| m.clone())
            .collect();
        let since: Vec<TaskMessage> = since_assignment(
            &relevant,
            t.assignee.as_ref().map_or("", |a| a.assigned_at.as_str()),
        )
        .into_iter()
        .cloned()
        .collect();
        let selected: Vec<_> = messages
            .iter()
            .zip(comments)
            .filter(|(m, _)| since.contains(m))
            .collect();
        let latest = selected
            .last()
            .and_then(|(_, c)| c.meta.as_ref().and_then(WorkflowStatus::from_payload));
        // Ordinary comments and transient errors do not erase the ticket's outcome.
        let outcomes: Vec<_> = selected
            .iter()
            .rev()
            .filter(|(m, _)| m.author_type == "Assistant")
            .filter_map(|(_, c)| c.meta.as_ref().and_then(WorkflowStatus::from_payload))
            .filter(|s| {
                let payload = s.payload();
                payload["person_id"] == self.person_id
                    && payload["work_kind"] == "ticket"
                    && payload["work_identity"] == t.url.as_str()
                    && matches!(
                        s.reason(),
                        WorkflowStatusReason::Completed
                            | WorkflowStatusReason::WorkRemaining
                            | WorkflowStatusReason::Asking
                            | WorkflowStatusReason::Blocked
                            | WorkflowStatusReason::AwaitingChecks
                    )
            })
            .map(|s| s.reason())
            .collect();
        let work_status = outcomes.first().copied();
        // Run waits do not decide whether the ticket's scope is complete.
        let work_remaining = outcomes
            .iter()
            .find(|s| {
                matches!(
                    s,
                    WorkflowStatusReason::Completed | WorkflowStatusReason::WorkRemaining
                )
            })
            .is_some_and(|s| *s == WorkflowStatusReason::WorkRemaining);
        let pending = mention_pending(
            mentions(&t.mentions),
            selected
                .iter()
                .map(|(m, c)| (*m, mentions(c.mentions.as_deref().unwrap_or_default()))),
        );
        let linked = linked_pull_state(card.pull_requests.iter().map(|p| {
            (
                if p.merged { "merged" } else { p.state.as_str() },
                p.updated_at.as_str(),
            )
        }));
        let decision = select_ticket(
            lane,
            &since,
            latest.as_ref(),
            pending,
            linked,
            TicketPolicy {
                finish_merged: finish,
                repair: task.repair,
                work_status,
                work_remaining,
            },
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
            unique.insert(card.ticket.url.clone(), card);
        }
        let mut cards: Vec<_> = unique.into_values().collect();
        cards.sort_by(|a, b| {
            let key = |c: &tickets::CardView| {
                (
                    c.ticket.lane != "ready",
                    if c.ticket.lane == "ready" {
                        c.ticket.rank.clone()
                    } else {
                        c.ticket.created_at.clone()
                    },
                )
            };
            key(a).cmp(&key(b))
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
        let _open = self.api.guard().map_err(|e| error(e, "closed"))?;
        let caller = self
            .member()
            .await?
            .caller()
            .await
            .map_err(|e| error(e, "caller"))?;
        tickets::closed(
            &self.api.app,
            &caller,
            &self.workspace,
            tickets::ClosedQuery {
                since: Some(start.to_rfc3339()),
                until: Some(end.to_rfc3339()),
            },
            crate::domain::PageReq::all(),
        )
        .await
        .map(|p| {
            p.items
                .into_iter()
                .map(|i| ClosedItem {
                    kind: if i.kind == "pull_request" {
                        WorkKind::PullRequest
                    } else {
                        WorkKind::Ticket
                    },
                    url: i.url,
                    title: i.title,
                    label: i.label,
                    closed_at: i.closed_at,
                    merged: i.merged,
                })
                .collect()
        })
        .map_err(|e| error(e, "closed"))
    }
}
