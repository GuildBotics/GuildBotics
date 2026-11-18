use super::*;

impl TicketAgent {
    pub fn read<'a>(
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
                    let _open = self.api.guard().map_err(|e| error(e, "list_tickets"))?;
                    let caller = m.caller().await.map_err(|e| error(e, "caller"))?;
                    let mut entries = crate::domain::tickets::list_tickets(
                        &self.api.app,
                        &caller,
                        subject,
                        query(&c),
                        crate::domain::PageReq::all(),
                    )
                    .await
                    .map_err(|e| error(e, "list_tickets"))?
                    .items;
                    entries.retain(|t| self.on_board(t));
                    entries.sort_by(|a, b| a.rank.cmp(&b.rank));
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
                    return ReadPage::of(entries.into_iter().skip(start).take(size), next, None);
                }
                let p = m
                    .tickets_list_tickets(
                        subject,
                        query(&c),
                        self.page(&m, resource, subject, &c, continuation)?,
                    )
                    .await
                    .map_err(|e| error(e, "tickets_list_tickets"))?;
                return ReadPage::of(p.items, p.next_cursor, None);
            }
            let (r, k) = self.item(subject, "tickets")?;
            if resource == Resource::Tickets {
                let v = m
                    .tickets_get_ticket(&r, &k)
                    .await
                    .map_err(|e| error(e, "tickets_get_ticket"))?;
                let target = self.target_ticket(&v);
                return ReadPage::of([v], None, Some(target));
            }
            let page = self.page(&m, resource, subject, &c, continuation)?;
            match resource {
                Resource::TicketComments => {
                    let p = m
                        .tickets_list_comments(&r, &k, page)
                        .await
                        .map_err(|e| error(e, "tickets_list_comments"))?;
                    ReadPage::of(p.items, p.next_cursor, None)
                }
                Resource::TicketAttachments => {
                    let p = m
                        .attachments_of_ticket(&r, &k, page)
                        .await
                        .map_err(|e| error(e, "attachments_of_ticket"))?;
                    ReadPage::of(p.items, p.next_cursor, None)
                }
                _ => Err(ServiceError::InvalidResource),
            }
        })
    }
    pub fn attachment<'a>(
        &'a self,
        url: &'a str,
        id: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<Attachment>> {
        Box::pin(async move {
            let (r, k) = self.item(url, "tickets")?;
            let m = self.member().await?;
            let _open = self.api.guard().map_err(|e| error(e, "attachments"))?;
            let caller = m.caller().await.map_err(|e| error(e, "caller"))?;
            let p = crate::domain::attachments::of_ticket(
                &self.api.app,
                &caller,
                &r,
                &k,
                crate::domain::PageReq::all(),
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
            Err(ServiceError::AttachmentMissing {
                url: url.to_owned(),
                id: id.to_owned(),
            })
        })
    }
    pub fn comment<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, Written<crate::domain::CommentView>> {
        Box::pin(async move {
            let (r, k) = self.item(url, "tickets")?;
            self.writable(&r)?;
            let m = self.member().await?;
            let t = m
                .tickets_get_ticket(&r, &k)
                .await
                .map_err(|e| error(e, "tickets_get_ticket"))?;
            let c = m
                .tickets_add_comment(
                    &r,
                    &k,
                    tickets::NewComment {
                        body: self.comment_body(body, status, &t.author),
                        meta: status.map(WorkflowStatus::payload),
                    },
                )
                .await
                .map_err(|e| error(e, "tickets_add_comment"))?;
            Ok(Written {
                view: c,
                target: self.target_ticket(&t),
            })
        })
    }
    pub fn create_ticket(
        &self,
        r: CreateTicket,
    ) -> ServiceFuture<'_, Written<tickets::TicketView>> {
        Box::pin(async move {
            self.repository(&r.tracker)?;
            self.writable(&r.tracker)?;
            self.related(r.parent.iter().chain(&r.blocked_by).cloned())?;
            let lane = creation_lane(r.lane, r.parent.as_deref(), &r.blocked_by);
            let t = self
                .member()
                .await?
                .tickets_create_ticket(
                    &r.tracker,
                    tickets::NewTicket {
                        title: r.title,
                        body: r.body,
                        labels: r.labels,
                        lane: Some(create_lane(lane)),
                        interactive: r.interactive,
                        assignee: r.assignee.map(|a| a.0),
                        parent: r.parent,
                        blocked_by: r.blocked_by,
                        upstream_issue: None,
                    },
                )
                .await
                .map_err(|e| error(e, "tickets_create_ticket"))?;
            Ok(Written {
                target: self.target_ticket(&t),
                view: t,
            })
        })
    }
    pub fn update_ticket<'a>(
        &'a self,
        url: &'a str,
        r: UpdateTicket,
    ) -> ServiceFuture<'a, Written<tickets::TicketView>> {
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
                let own = m
                    .tickets_get_ticket(&repo, &key)
                    .await
                    .map_err(|e| error(e, "tickets_get_ticket"))?;
                let anchor = m
                    .tickets_get_ticket(&anchor_repo, &anchor_key)
                    .await
                    .map_err(|e| error(e, "tickets_get_ticket"))?;
                if repo != anchor_repo
                    || key == anchor_key
                    || !self.on_board(&own)
                    || !self.on_board(&anchor)
                    || own.lane != anchor.lane
                    || r.lane.is_some_and(|lane| own.lane != move_lane(lane))
                {
                    return Err(diagnostic("integrations.resources.ticket_placement", []));
                }
                Some((own.lane.clone(), format!("{anchor_repo}#{anchor_key}")))
            } else {
                None
            };
            let patch = tickets::TicketPatch {
                title: r.title,
                body: r.body,
                add_labels: r.add_labels,
                remove_labels: r.remove_labels,
                interactive: r.interactive,
                state: r.state.clone(),
                state_reason: r.state.as_ref().and(r.state_reason.map(Some)),
                parent: r.parent.map(|p| (!p.is_empty()).then_some(p)),
                add_blocked_by: r.add_blocked_by,
                remove_blocked_by: r.remove_blocked_by,
                upstream_issue: None,
                revision: None,
            };
            let t = self
                .member()
                .await?
                .tickets_update_ticket(&repo, &key, patch)
                .await
                .map_err(|e| error(e, "tickets_update_ticket"))?;
            let changed = t.state_changed;
            if let Some((lane, anchor)) = placement {
                m.tickets_put_on_board(
                    &repo,
                    &key,
                    tickets::Placement {
                        lane,
                        before: matches!(r.placement, Some(TicketPlacement::Before(_)))
                            .then(|| anchor.clone()),
                        after: matches!(r.placement, Some(TicketPlacement::After(_)))
                            .then_some(anchor),
                    },
                )
                .await
                .map_err(|e| error(e, "tickets_put_on_board"))?;
            }
            if let Some(actor) = r.assignee {
                self.ticket_assign(url, &actor.0).await?;
            }
            if let Some(lane) = r.lane.filter(|_| r.placement.is_none()) {
                self.move_ticket(
                    &Task {
                        url: Some(url.into()),
                        ..Default::default()
                    },
                    lane,
                )
                .await?;
            }
            let v = self.ticket_view(url).await?;
            let mut target = self.target_ticket(&v);
            target.closed = changed && v.state == "closed";
            Ok(Written { view: v, target })
        })
    }
    pub fn add_reaction<'a>(
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
}

impl TicketAgent {
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
}

fn query(c: &Value) -> tickets::TicketQuery {
    let string = |k: &str| c[k].as_str().filter(|s| !s.is_empty()).map(str::to_owned);
    tickets::TicketQuery {
        q: string("text"),
        state: string("state"),
        lane: string("lane"),
        label: string("label"),
        assignee: None,
    }
}
