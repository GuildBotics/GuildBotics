use super::*;
use std::{collections::BTreeSet, fs};
use unicode_casefold::UnicodeCaseFold;
impl LocalService {
    fn related(&self, url: &str) -> Result<String, ServiceError> {
        let r = Ref::locate(url, WorkKind::Ticket)?;
        self.store.load(&r)?;
        self.store.scope.authorize(&r.place)?;
        Ok(r.url())
    }
    pub(super) fn ancestor(&self, v: &Value) -> Result<Option<String>, ServiceError> {
        let mut v = v.clone();
        let mut seen = BTreeSet::new();
        loop {
            let p = text(&v, "parent").to_owned();
            if p.is_empty() || !seen.insert(p.clone()) {
                return Ok(None);
            }
            v = self.store.load(&Ref::locate(&p, WorkKind::Ticket)?)?;
            if v["interactive"] == true {
                return Ok(Some(p));
            }
        }
    }
    fn relative(&self, url: &str) -> Result<Value, ServiceError> {
        let r = Ref::locate(url, WorkKind::Ticket)?;
        let v = self.store.load(&r)?;
        let mut target = serde_json::to_value(r.target(&v)).map_err(|_| ServiceError::Failed)?;
        target["state"] = v["state"].clone();
        Ok(target)
    }
    fn labels(&self, p: &str, labels: &[String]) -> Result<Vec<String>, ServiceError> {
        let d = self.store.tracker(p)?;
        let defined = array(&d, "labels");
        let mut out = vec![];
        for l in labels {
            if !defined.contains(&json!(l)) {
                return Err(ServiceError::InvalidParameters);
            }
            if !out.contains(l) {
                out.push(l.clone())
            }
        }
        Ok(out)
    }
    fn attachments(&self, r: &Ref, v: &Value) -> Result<Vec<Attachment>, ServiceError> {
        let d = self
            .store
            .dir(WorkKind::Ticket, &r.place)
            .join("attachments");
        if !d.exists() {
            return Ok(vec![]);
        }
        let mut paths = fs::read_dir(&d)
            .map_err(|_| ServiceError::Failed)?
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|e| e == "json"))
            .collect::<Vec<_>>();
        paths.sort();
        let mut texts = vec![("body".into(), text(v, "body").to_owned())];
        texts.extend(
            array(v, "comments")
                .iter()
                .map(|c| (text(c, "id").to_owned(), text(c, "body").to_owned())),
        );
        let mut out = vec![];
        for p in paths {
            let id = p
                .file_stem()
                .and_then(|s| s.to_str())
                .ok_or(ServiceError::Failed)?;
            let url = format!("local://tickets/{}/attachments/{id}", r.place);
            let re = regex::Regex::new(&format!(r"{}(?:[^\w-]|$)", regex::escape(&url)))
                .map_err(|_| ServiceError::Failed)?;
            let refs: Vec<String> = texts
                .iter()
                .filter(|(_, s)| re.is_match(s))
                .map(|(id, _)| id.clone())
                .collect();
            if !refs.is_empty() {
                let m = read(&p)?;
                out.push(Attachment {
                    id: id.into(),
                    name: text(&m, "name").into(),
                    media_type: text(&m, "media_type").into(),
                    size: fs::metadata(d.join(id))
                        .map_err(|_| ServiceError::Failed)?
                        .len() as i64,
                    referenced_by: refs,
                });
            }
        }
        Ok(out)
    }
    fn ticket_read(
        &self,
        r: Resource,
        s: &str,
        p: Option<&Value>,
        cursor: &str,
    ) -> Result<ReadPage, ServiceError> {
        let c = read_conditions(r, p, cursor)?;
        if !r.is_ticket_service() {
            return Err(ServiceError::InvalidResource);
        }
        if r == Resource::TrackerTickets {
            self.store.tracker(s)?;
            let terms = text(&c, "text")
                .case_fold()
                .collect::<String>()
                .split_whitespace()
                .map(str::to_owned)
                .collect::<Vec<_>>();
            let mut entries = vec![];
            for (r, v) in self.store.items(WorkKind::Ticket, Some(s))? {
                let searchable = format!("{}\n{}", text(&v, "title"), text(&v, "body"))
                    .case_fold()
                    .collect::<String>();
                if (c["state"] == "all" || c["state"] == v["state"])
                    && (c["label"] == "" || array(&v, "labels").contains(&c["label"]))
                    && terms.iter().all(|t| searchable.contains(t))
                {
                    let mut e =
                        serde_json::to_value(r.target(&v)).map_err(|_| ServiceError::Failed)?;
                    for k in ["state", "lane", "labels", "updated_at"] {
                        e[k] = v[k].clone()
                    }
                    e["key"] = json!(r.name);
                    entries.push(e)
                }
            }
            return page(r, entries, &c, cursor);
        }
        let rf = Ref::locate(s, WorkKind::Ticket)?;
        let v = self.store.load(&rf)?;
        if r == Resource::Tickets {
            let mut e = serde_json::to_value(rf.target(&v)).map_err(|_| ServiceError::Failed)?;
            for k in [
                "body",
                "state",
                "assignees",
                "labels",
                "lane",
                "interactive",
            ] {
                e[k] = v[k].clone()
            }
            e["key"] = json!(rf.name);
            e["tracker"] = json!(rf.place);
            e["repository"] = self.store.tracker(&rf.place)?["repository"].clone();
            e["interactive_ancestor"] = json!(self.ancestor(&v)?);
            e["parent"] = if text(&v, "parent").is_empty() {
                Value::Null
            } else {
                self.relative(text(&v, "parent"))?
            };
            e["blocked_by"] = json!(
                array(&v, "blocked_by")
                    .iter()
                    .map(|u| self.relative(u.as_str().unwrap_or("")))
                    .collect::<Result<Vec<_>, _>>()?
            );
            let all = self.store.items(WorkKind::Ticket, None)?;
            e["children"] = json!(
                all.iter()
                    .filter(|(_, i)| i["parent"] == rf.url())
                    .map(|(r, _)| self.relative(&r.url()))
                    .collect::<Result<Vec<_>, _>>()?
            );
            e["blocks"] = json!(
                all.iter()
                    .filter(|(_, i)| array(i, "blocked_by").contains(&json!(rf.url())))
                    .map(|(r, _)| self.relative(&r.url()))
                    .collect::<Result<Vec<_>, _>>()?
            );
            return ReadPage::of(r, [e], None, Some(rf.target(&v)));
        }
        let entries = if r == Resource::TicketAttachments {
            self.attachments(&rf, &v)?
                .into_iter()
                .map(|a| serde_json::to_value(a).map_err(|_| ServiceError::Failed))
                .collect::<Result<Vec<_>, _>>()?
        } else {
            array(&v, "comments")
                .iter()
                .map(|v| self.comment_value(v))
                .collect()
        };
        page(r, entries, &c, cursor)
    }
    fn ticket_work(&self, r: &Ref, v: &Value) -> Result<Option<Task>, ServiceError> {
        if v["state"] != "open"
            || self.store.scope.authorize(&r.place).is_err()
            || v["interactive"] == true
            || self.ancestor(v)?.is_some()
        {
            return Ok(None);
        }
        let assigned = array(v, "assignees").contains(&json!(self.actor));
        let lane = text(v, "lane");
        if assigned {
            if !["ready", "in_progress"].contains(&lane) {
                return Ok(None);
            }
            if lane == "ready" {
                for blocker in array(v, "blocked_by") {
                    if self.relative(blocker.as_str().unwrap_or(""))?["state"] == "open" {
                        return Ok(None);
                    }
                }
            }
        }
        let mut t = self.task(r, v);
        t.status = if lane.is_empty() { "new" } else { lane }.into();
        t.assignee = assigned.then(|| self.person_id.clone());
        let mentioned = |s: &str| {
            let folded = s.case_fold().collect::<String>();
            super::chat::mention_ids(&folded).contains(&self.actor.case_fold().collect::<String>())
        };
        let pending = crate::mention_pending(
            mentioned(&t.description),
            t.comments.iter().map(|c| (c, mentioned(&c.content))),
        );
        if !assigned {
            return Ok(pending.then(|| {
                t.trigger_reason = Some("issue_mention".into());
                t
            }));
        }
        let relevant = crate::relevant_comments(t.comments.iter().map(|c| {
            let ids = super::chat::mention_ids(&c.content.case_fold().collect::<String>());
            (c, !ids.is_empty() && !mentioned(&c.content))
        }));
        let latest = relevant.last().and_then(|last| {
            array(v, "comments")
                .iter()
                .find(|c| {
                    text(c, "created_at") == last.timestamp && text(c, "body") == last.content
                })
                .and_then(|c| WorkflowStatus::from_payload(&c["status"]))
        });
        let relevant = relevant.into_iter().cloned().collect::<Vec<_>>();
        let d = crate::select_ticket(
            lane,
            &relevant,
            latest.as_ref(),
            pending,
            None,
            true,
            chrono::Utc::now(),
        );
        Ok(d.reason.map(|reason| {
            t.trigger_reason = Some(reason.into());
            t
        }))
    }
}
impl TicketService for LocalService {
    fn read<'a>(
        &'a self,
        r: Resource,
        s: &'a str,
        p: Option<&'a Value>,
        c: &'a str,
    ) -> ServiceFuture<'a, ReadPage> {
        Box::pin(async move { self.ticket_read(r, s, p, c) })
    }
    fn attachment<'a>(
        &'a self,
        u: &'a str,
        id: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<Attachment>> {
        Box::pin(async move {
            let r = Ref::locate(u, WorkKind::Ticket)?;
            let a = self
                .attachments(&r, &self.store.load(&r)?)?
                .into_iter()
                .find(|a| a.id == id)
                .ok_or_else(|| ServiceError::AttachmentMissing {
                    url: u.into(),
                    id: id.into(),
                })?;
            if a.size < 0 {
                return Err(ServiceError::InvalidResult);
            }
            if a.size as usize > limit {
                return Err(ServiceError::AttachmentTooLarge {
                    name: a.name.clone(),
                    limit,
                });
            }
            let content = fs::read(
                self.store
                    .dir(WorkKind::Ticket, &r.place)
                    .join("attachments")
                    .join(&a.id),
            )
            .map_err(|_| ServiceError::Failed)?;
            if content.len() > limit {
                return Err(ServiceError::AttachmentTooLarge {
                    name: a.name.clone(),
                    limit,
                });
            }
            Ok(Download {
                metadata: a,
                content,
            })
        })
    }
    fn comment<'a>(
        &'a self,
        u: &'a str,
        b: &'a str,
        s: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        Box::pin(async move { self.write_comment(u, WorkKind::Ticket, b, s) })
    }
    fn create_ticket(&self, q: CreateTicket) -> ServiceFuture<'_, TicketCreated> {
        Box::pin(async move {
            let _g = lock()?;
            let labels = self.labels(&q.tracker, &q.labels)?;
            let parent = q
                .parent
                .as_deref()
                .filter(|s| !s.is_empty())
                .map(|p| self.related(p))
                .transpose()?;
            let mut blockers = vec![];
            for u in &q.blocked_by {
                let u = self.related(u)?;
                if !blockers.contains(&u) {
                    blockers.push(u)
                }
            }
            let (r, mut v) = self.store.new_item(
                WorkKind::Ticket,
                &q.tracker,
                &q.title,
                &q.body,
                &self.actor,
            )?;
            v["labels"] = json!(labels);
            v["lane"] = if q.add_to_board {
                json!(creation_lane(q.lane, parent.as_deref(), &q.blocked_by))
            } else {
                Value::Null
            };
            v["interactive"] = json!(q.interactive);
            v["assignees"] = json!(q.assignee.into_iter().collect::<Vec<_>>());
            v["parent"] = json!(parent);
            v["blocked_by"] = json!(blockers);
            self.store.save(&r, &mut v)?;
            Ok(TicketCreated {
                labels,
                target: r.target(&v),
            })
        })
    }
    fn update_ticket<'a>(
        &'a self,
        u: &'a str,
        q: UpdateTicket,
    ) -> ServiceFuture<'a, TicketUpdated> {
        Box::pin(async move {
            let _g = lock()?;
            let r = Ref::locate(u, WorkKind::Ticket)?;
            let mut v = self.store.load(&r)?;
            let labels = self.labels(&r.place, &q.add_labels)?;
            if let Some(p) = q.parent {
                if !text(&v, "parent").is_empty() {
                    self.related(text(&v, "parent"))?;
                }
                v["parent"] = if p.is_empty() {
                    Value::Null
                } else {
                    json!(self.related(&p)?)
                };
            }
            let removed = q
                .remove_blocked_by
                .iter()
                .map(|u| self.related(u))
                .collect::<Result<Vec<_>, _>>()?;
            let mut blockers = array(&v, "blocked_by")
                .into_iter()
                .filter(|b| !removed.iter().any(|u| b == u))
                .collect::<Vec<_>>();
            for u in q.add_blocked_by {
                let u = json!(self.related(&u)?);
                if !blockers.contains(&u) {
                    blockers.push(u)
                }
            }
            v["blocked_by"] = json!(blockers);
            let changed = q.state.as_ref().is_some_and(|s| v["state"] != *s);
            if let Some(s) = q.state {
                if !["open", "closed"].contains(&s.as_str()) {
                    return Err(ServiceError::InvalidParameters);
                }
                v["state"] = json!(s);
                if changed {
                    v["closed_at"] = if s == "closed" {
                        json!(now())
                    } else {
                        Value::Null
                    }
                }
            }
            if let Some(b) = q.body {
                v["body"] = json!(b)
            }
            if let Some(t) = q.title {
                v["title"] = json!(t)
            }
            if let Some(i) = q.interactive {
                v["interactive"] = json!(i)
            }
            let mut ls = array(&v, "labels")
                .into_iter()
                .filter(|l| !q.remove_labels.iter().any(|s| l == s))
                .collect::<Vec<_>>();
            for l in labels {
                let l = json!(l);
                if !ls.contains(&l) {
                    ls.push(l)
                }
            }
            v["labels"] = json!(ls);
            let ancestor = self.ancestor(&v)?;
            self.store.save(&r, &mut v)?;
            Ok(TicketUpdated {
                body: text(&v, "body").into(),
                state: text(&v, "state").into(),
                state_changed: changed,
                labels: ls
                    .iter()
                    .filter_map(|l| l.as_str().map(str::to_owned))
                    .collect(),
                target: r.target(&v),
                interactive: v["interactive"] == true,
                interactive_ancestor: ancestor,
            })
        })
    }
    fn add_reaction<'a>(
        &'a self,
        u: &'a str,
        id: &'a str,
        r: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded> {
        Box::pin(async move { self.react(u, WorkKind::Ticket, id, r) })
    }
    fn candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        Box::pin(async {
            let mut items = self.store.items(WorkKind::Ticket, None)?;
            items.sort_by_key(|(_, v)| {
                (
                    match text(v, "lane") {
                        "ready" => 0,
                        "in_progress" => 1,
                        _ => 2,
                    },
                    text(v, "created_at").to_owned(),
                )
            });
            items
                .iter()
                .map(|(r, v)| self.ticket_work(r, v))
                .filter_map(Result::transpose)
                .collect()
        })
    }
    fn refresh<'a>(&'a self, t: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        Box::pin(async move {
            let r = Ref::locate(
                t.url.as_deref().ok_or(ServiceError::NotFound)?,
                WorkKind::Ticket,
            )?;
            self.ticket_work(&r, &self.store.load(&r)?)
        })
    }
    fn can_receive_work<'a>(&'a self, u: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(async move {
            let r = Ref::locate(u, WorkKind::Ticket)?;
            Ok(self.ticket_work(&r, &self.store.load(&r)?)?.is_some())
        })
    }
    fn move_ticket<'a>(&'a self, t: &'a Task, l: MoveLane) -> ServiceFuture<'a, bool> {
        Box::pin(async move {
            let _g = lock()?;
            let r = Ref::locate(
                t.url.as_deref().ok_or(ServiceError::NotFound)?,
                WorkKind::Ticket,
            )?;
            let mut v = self.store.load(&r)?;
            v["lane"] = json!(l);
            self.store.save(&r, &mut v)?;
            Ok(true)
        })
    }
    fn closed_since(
        &self,
        s: chrono::DateTime<chrono::Utc>,
        e: chrono::DateTime<chrono::Utc>,
    ) -> ServiceFuture<'_, Vec<ClosedItem>> {
        Box::pin(async move {
            let mut out = vec![];
            for k in [WorkKind::Ticket, WorkKind::PullRequest] {
                for (r, v) in self.store.items(k, None)? {
                    if chrono::DateTime::parse_from_rfc3339(text(&v, "closed_at"))
                        .is_ok_and(|t| t >= s && t <= e)
                    {
                        out.push(ClosedItem {
                            kind: k,
                            url: r.url(),
                            title: text(&v, "title").into(),
                            label: r.label(),
                            closed_at: text(&v, "closed_at").into(),
                            merged: v["merged"] == true,
                        })
                    }
                }
            }
            Ok(out)
        })
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}
