use super::*;
use branvil::api::tickets;
use chrono::{DateTime, Utc};
impl TicketService for BranvilService {
    fn comment_takeover<'a>(&'a self, url: &'a str, body: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let (repo, key) = self.item(url, "tickets")?;
            self.writable(&repo)?;
            self.member()
                .await?
                .tickets_add_comment(
                    &repo,
                    &key,
                    tickets::NewComment {
                        body: body.into(),
                        meta: Some(json!({"repair_takeover": true})),
                    },
                )
                .await
                .map_err(|e| error(e, "tickets_add_comment"))?;
            Ok(())
        })
    }

    fn repair_tickets(&self) -> ServiceFuture<'_, Vec<Ticket>> {
        Box::pin(async {
            let m = self.member().await?;
            let mut out = vec![];
            let mut cursor = None;
            loop {
                let page = m
                    .page(
                        format!("gb:repairs:{}", self.workspace),
                        Some(100),
                        cursor.as_deref(),
                    )
                    .map_err(|e| error(e, "page"))?;
                let page = m
                    .tickets_cards(
                        &self.workspace,
                        tickets::CardQuery {
                            repair: Some(true),
                            state: Some("open".into()),
                            comments: Some("none".into()),
                            ..Default::default()
                        },
                        page,
                    )
                    .await
                    .map_err(|e| error(e, "tickets_cards"))?;
                for card in page.items {
                    let v = value(card)?;
                    if self.scope.authorize(text(&v, "repository")).is_ok() {
                        out.push(decode(self.ticket_value(v))?);
                    }
                }
                cursor = page.next_cursor;
                if cursor.is_none() {
                    break;
                }
            }
            Ok(out)
        })
    }
    fn assign_to_me<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            self.ticket_assign(url, &self.person_id).await?;
            Ok(())
        })
    }

    fn read<'a>(
        &'a self,
        resource: Resource,
        subject: &'a str,
        parameters: Option<&'a Value>,
        continuation: &'a str,
    ) -> ServiceFuture<'a, ReadPage> {
        Box::pin(async move {
            let c = read_conditions(resource, parameters, continuation)?;
            if !resource.is_ticket_service() {
                return Err(ServiceError::InvalidResource);
            }
            let m = self.member().await?;
            if resource == Resource::TrackerTickets {
                self.repository(subject)?;
                if c["lane"].is_string() {
                    let mut cursor = None;
                    let mut entries = vec![];
                    loop {
                        let p = m.tickets_list_tickets(subject, decode(json!({"q":c["text"],"state":c["state"],"lane":c["lane"],"label":c["label"].as_str().filter(|s|!s.is_empty())}))?, m.page(format!("gb:lane:{subject}:{c}"), Some(100), cursor.as_deref()).map_err(|e|error(e,"page"))?).await.map_err(|e|error(e,"tickets_list_tickets"))?;
                        for t in p.items {
                            let v = value(t)?;
                            if self.on_board(&v) {
                                entries.push(v);
                            }
                        }
                        cursor = p.next_cursor;
                        if cursor.is_none() {
                            break;
                        }
                    }
                    entries.sort_by(|a, b| text(a, "rank").cmp(text(b, "rank")));
                    let start = if continuation.is_empty() {
                        0
                    } else {
                        continuation
                            .parse::<usize>()
                            .map_err(|_| ServiceError::InvalidContinuation)?
                    };
                    let size = c["page_size"].as_u64().unwrap_or(30) as usize;
                    let next = (start.saturating_add(size) < entries.len())
                        .then(|| start.saturating_add(size).to_string());
                    return ReadPage::of(
                        resource,
                        entries
                            .iter()
                            .skip(start)
                            .take(size)
                            .map(|v| self.ticket_summary(v)),
                        next,
                        None,
                    );
                }
                let p=m.tickets_list_tickets(subject,decode(json!({"q":c["text"],"state":c["state"],"label":c["label"].as_str().filter(|s|!s.is_empty())}))?,self.page(&m,resource,subject,&c,continuation)?).await.map_err(|e| error(e, "tickets_list_tickets"))?;
                let items = p
                    .items
                    .into_iter()
                    .map(|t| value(t).map(|v| self.ticket_summary(&v)))
                    .collect::<Result<Vec<_>, ServiceError>>()?;
                return ReadPage::of(resource, items, p.next_cursor, None);
            }
            let (r, k) = self.item(subject, "tickets")?;
            if resource == Resource::Tickets {
                let v = value(
                    m.tickets_get_ticket(&r, &k)
                        .await
                        .map_err(|e| error(e, "tickets_get_ticket"))?,
                )?;
                let target = self.target(&v, WorkKind::Ticket);
                return ReadPage::of(resource, [self.ticket_value(v)], None, Some(target));
            }
            let page = self.page(&m, resource, subject, &c, continuation)?;
            match resource {
                Resource::TicketComments => {
                    let p = m
                        .tickets_list_comments(&r, &k, page)
                        .await
                        .map_err(|e| error(e, "tickets_list_comments"))?;
                    ReadPage::of(
                        resource,
                        p.items
                            .into_iter()
                            .map(|v| value(v).map(|v| self.comment_value(v)))
                            .collect::<Result<Vec<_>, _>>()?,
                        p.next_cursor,
                        None,
                    )
                }
                Resource::TicketAttachments => {
                    let p = m
                        .attachments_of_ticket(&r, &k, page)
                        .await
                        .map_err(|e| error(e, "attachments_of_ticket"))?;
                    ReadPage::of(resource,p.items.into_iter().map(|a|json!({"id":a.attachment.id,"name":a.attachment.name,"media_type":a.attachment.media_type,"size":a.attachment.size,"referenced_by":a.referenced_by})),p.next_cursor,None)
                }
                _ => Err(ServiceError::InvalidResource),
            }
        })
    }
    fn attachment<'a>(
        &'a self,
        url: &'a str,
        id: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<Attachment>> {
        Box::pin(async move {
            let (r, k) = self.item(url, "tickets")?;
            let m = self.member().await?;
            let mut cursor = None;
            loop {
                let p = m
                    .attachments_of_ticket(
                        &r,
                        &k,
                        m.page(format!("attachment:{url}"), Some(100), cursor.as_deref())
                            .map_err(|e| error(e, "page"))?,
                    )
                    .await
                    .map_err(|e| error(e, "attachments_of_ticket"))?;
                for a in p.items {
                    if a.attachment.id == id {
                        if a.attachment.size > limit as u64 {
                            return Err(ServiceError::AttachmentTooLarge {
                                name: a.attachment.name,
                                limit,
                            });
                        }
                        let (_, content) = m
                            .attachments_get(&r, id)
                            .await
                            .map_err(|e| error(e, "attachments_get"))?;
                        if content.len() > limit {
                            return Err(ServiceError::AttachmentTooLarge {
                                name: a.attachment.name,
                                limit,
                            });
                        }
                        return Ok(Download {
                            metadata: Attachment {
                                id: id.into(),
                                name: a.attachment.name,
                                media_type: a.attachment.media_type,
                                size: a.attachment.size as i64,
                                referenced_by: a.referenced_by,
                            },
                            content,
                        });
                    }
                }
                cursor = p.next_cursor;
                if cursor.is_none() {
                    return Err(ServiceError::AttachmentMissing {
                        url: url.to_owned(),
                        id: id.to_owned(),
                    });
                }
            }
        })
    }
    fn comment<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        Box::pin(async move {
            let (r, k) = self.item(url, "tickets")?;
            self.writable(&r)?;
            let m = self.member().await?;
            let t = value(
                m.tickets_get_ticket(&r, &k)
                    .await
                    .map_err(|e| error(e, "tickets_get_ticket"))?,
            )?;
            let c = m
                .tickets_add_comment(
                    &r,
                    &k,
                    tickets::NewComment {
                        body: self.comment_body(body, status, text(&t, "author")),
                        meta: status.map(WorkflowStatus::payload),
                    },
                )
                .await
                .map_err(|e| error(e, "tickets_add_comment"))?;
            let v = value(c)?;
            Ok(CommentWritten {
                id: text(&v, "id").into(),
                url: v["url"].as_str().map(Into::into),
                author: text(&v, "author").into(),
                created_at: v["created_at"].as_str().map(Into::into),
                target: self.target(&t, WorkKind::Ticket),
            })
        })
    }
    fn create_ticket(&self, r: CreateTicket) -> ServiceFuture<'_, TicketCreated> {
        Box::pin(async move {
            if !r.add_to_board {
                return Err(diagnostic("integrations.branvil.api.every_on_board", []));
            }
            self.repository(&r.tracker)?;
            self.writable(&r.tracker)?;
            self.related(r.parent.iter().chain(&r.blocked_by).cloned())?;
            let lane = creation_lane(r.lane, r.parent.as_deref(), &r.blocked_by);
            let v=value(self.member().await?.tickets_create_ticket(&r.tracker,decode(json!({"title":r.title,"body":r.body,"labels":r.labels,"lane":lane,"interactive":r.interactive,"assignee":r.assignee,"parent":r.parent,"blocked_by":r.blocked_by}))?).await.map_err(|e| error(e, "tickets_create_ticket"))?)?;
            Ok(TicketCreated {
                labels: decode(v["labels"].clone())?,
                target: self.target(&v, WorkKind::Ticket),
            })
        })
    }
    fn update_ticket<'a>(
        &'a self,
        url: &'a str,
        r: UpdateTicket,
    ) -> ServiceFuture<'a, TicketUpdated> {
        Box::pin(async move {
            let (repo, key) = self.item(url, "tickets")?;
            self.writable(&repo)?;
            self.related(
                r.parent
                    .iter()
                    .filter(|s| !s.is_empty())
                    .chain(&r.add_blocked_by)
                    .chain(&r.remove_blocked_by)
                    .cloned(),
            )?;
            let m = self.member().await?;
            let placement = if let Some(ref placement) = r.placement {
                let (anchor_repo, anchor_key) = self.item(placement.url(), "tickets")?;
                self.writable(&anchor_repo)?;
                let own = value(
                    m.tickets_get_ticket(&repo, &key)
                        .await
                        .map_err(|e| error(e, "tickets_get_ticket"))?,
                )?;
                let anchor = value(
                    m.tickets_get_ticket(&anchor_repo, &anchor_key)
                        .await
                        .map_err(|e| error(e, "tickets_get_ticket"))?,
                )?;
                if repo != anchor_repo
                    || key == anchor_key
                    || !self.on_board(&own)
                    || !self.on_board(&anchor)
                    || own["lane"] != anchor["lane"]
                    || r.lane.is_some_and(|lane| own["lane"] != json!(lane))
                {
                    return Err(diagnostic("integrations.resources.ticket_placement", []));
                }
                Some((own["lane"].clone(), format!("{anchor_repo}#{anchor_key}")))
            } else {
                None
            };
            let include_reason = r.state.is_some();
            let mut patch = json!({"title":r.title,"body":r.body,"add_labels":r.add_labels,"remove_labels":r.remove_labels,"interactive":r.interactive,"state":r.state,"add_blocked_by":r.add_blocked_by,"remove_blocked_by":r.remove_blocked_by});
            if let Some(p) = r.parent {
                patch["parent"] = if p.is_empty() { Value::Null } else { json!(p) }
            }
            if let Some(reason) = r.state_reason.filter(|_| include_reason) {
                patch["state_reason"] = json!(reason)
            }
            let t = self
                .member()
                .await?
                .tickets_update_ticket(&repo, &key, decode(patch)?)
                .await
                .map_err(|e| error(e, "tickets_update_ticket"))?;
            let changed = t.state_changed;
            if let Some((lane, anchor)) = placement {
                let mut board = json!({"lane":lane});
                board[match r.placement {
                    Some(TicketPlacement::Before(_)) => "before",
                    _ => "after",
                }] = json!(anchor);
                m.tickets_put_on_board(&repo, &key, decode(board)?)
                    .await
                    .map_err(|e| error(e, "tickets_put_on_board"))?;
            }
            if let Some(actor) = r.assignee {
                self.ticket_assign(url, &actor.0).await?;
            }
            if let Some(lane) = r.lane.filter(|_| r.placement.is_none()) {
                self.ticket_move(
                    &Task {
                        url: Some(url.into()),
                        ..Default::default()
                    },
                    lane,
                )
                .await?;
            }
            let v = value(
                self.member()
                    .await?
                    .tickets_get_ticket(&repo, &key)
                    .await
                    .map_err(|e| error(e, "tickets_get_ticket"))?,
            )?;
            Ok(TicketUpdated {
                assignees: decode(self.ticket_value(v.clone())["assignees"].clone())?,
                lane: if self.on_board(&v) {
                    decode(v["lane"].clone())?
                } else {
                    None
                },
                body: text(&v, "body").into(),
                state: text(&v, "state").into(),
                state_changed: changed,
                labels: decode(v["labels"].clone())?,
                target: self.target(&v, WorkKind::Ticket),
                interactive: v["interactive"] == true,
                interactive_ancestor: v["interactive_ancestor"]["url"].as_str().map(Into::into),
            })
        })
    }
    fn add_reaction<'a>(
        &'a self,
        url: &'a str,
        id: &'a str,
        r: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded> {
        Box::pin(async move {
            let (repo, key) = self.item(url, "tickets")?;
            self.writable(&repo)?;
            self.member()
                .await?
                .tickets_react(
                    &repo,
                    &key,
                    tickets::NewReaction {
                        target_id: id.into(),
                        reaction: reaction(r).into(),
                    },
                    true,
                )
                .await
                .map_err(|e| error(e, "tickets_react"))?;
            Ok(ReactionAdded {
                reaction: r,
                id: id.into(),
            })
        })
    }
    fn candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        Box::pin(self.ticket_candidates())
    }
    fn refresh<'a>(&'a self, t: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        Box::pin(self.ticket_refresh(t))
    }
    fn can_receive_work<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(self.ticket_can_receive(url))
    }
    fn move_ticket<'a>(&'a self, t: &'a Task, l: MoveLane) -> ServiceFuture<'a, bool> {
        Box::pin(self.ticket_move(t, l))
    }
    fn closed_since(
        &self,
        s: DateTime<Utc>,
        e: DateTime<Utc>,
    ) -> ServiceFuture<'_, Vec<ClosedItem>> {
        Box::pin(self.ticket_closed(s, e))
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}

impl BranvilService {
    async fn ticket_assign(&self, url: &str, actor: &str) -> Result<(), ServiceError> {
        let (repo, key) = self.item(url, "tickets")?;
        self.writable(&repo)?;
        self.member()
            .await?
            .tickets_set_assignee(&repo, &key, Some(actor.into()))
            .await
            .map_err(|e| error(e, "tickets_set_assignee"))?;
        Ok(())
    }
    fn ticket_summary(&self, v: &Value) -> Value {
        let t = self.ticket_value(v.clone());
        json!({"kind":t["kind"],"url":t["url"],"title":t["title"],"label":t["label"],"key":t["key"],"state":t["state"],"lane":t["lane"],"labels":t["labels"],"updated_at":v["updated_at"],"assignees":t["assignees"]})
    }
}
