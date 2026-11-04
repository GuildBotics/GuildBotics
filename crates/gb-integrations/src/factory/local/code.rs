use super::*;
use crate::{Freshness, readiness_of};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
};
impl LocalService {
    fn head(&self, v: &Value) -> Result<String, ServiceError> {
        self.store.tip(text(v, "head_repository"), text(v, "head"))
    }
    fn at_head(&self, u: &str, head: &str) -> Result<(Ref, Value), ServiceError> {
        let r = Ref::locate(u, WorkKind::PullRequest)?;
        let v = self.store.load(&r)?;
        if self.head(&v)? != head {
            return Err(ServiceError::Failed);
        }
        Ok((r, v))
    }
    fn links(&self, links: &[TicketLink], repo: &str) -> Result<Value, ServiceError> {
        for l in links {
            let local = l.url.starts_with("local:");
            if local {
                let r = Ref::locate(&l.url, WorkKind::Ticket)?;
                self.store.load(&r)?;
                if l.closes && self.store.tracker(&r.place)?["repository"] != repo {
                    return Err(ServiceError::Unsupported);
                }
            } else if l.closes {
                return Err(ServiceError::Unsupported);
            }
        }
        serde_json::to_value(links).map_err(|_| ServiceError::Failed)
    }
    fn detail(&self, r: &Ref, v: &Value) -> Result<Value, ServiceError> {
        let mut e = serde_json::to_value(r.target(v)).map_err(|_| ServiceError::Failed)?;
        for k in [
            "body",
            "state",
            "assignees",
            "labels",
            "author",
            "head",
            "head_repository",
            "base",
            "merged",
            "draft",
        ] {
            e[k] = v[k].clone()
        }
        e["repository"] = json!(r.place);
        e["author_type"] = json!(self.author_type(text(v, "author")));
        e["requested_reviewers"] = v["reviewers"].clone();
        e["head_sha"] = json!(self.head(v)?);
        e["changed_files"] = Value::Null;
        Ok(e)
    }
    fn threads(&self, v: &Value) -> Vec<(Value, Vec<Value>)> {
        let all = array(v, "review_comments");
        all.iter()
            .filter(|c| c["in_reply_to"].is_null())
            .map(|root| {
                let mut cs = vec![root.clone()];
                cs.extend(
                    all.iter()
                        .filter(|c| c["in_reply_to"] == root["id"])
                        .cloned(),
                );
                (root.clone(), cs)
            })
            .collect()
    }
    fn thread_comment(&self, v: &Value) -> Value {
        let mut c = self.comment_value(v);
        c["reply_to_id"] = v["in_reply_to"].clone();
        c
    }
    fn files(&self, r: &Ref, v: &Value) -> Result<Vec<Value>, ServiceError> {
        let base = self.store.tip(&r.place, text(v, "base"))?;
        let head = self.head(v)?;
        if base.is_empty() || head.is_empty() {
            return Err(ServiceError::Failed);
        }
        let span = format!("{base}...{head}");
        let b = self.store.git_bytes(
            &r.place,
            &["diff", "--name-status", "-z", "--no-renames", &span],
        )?;
        let names = b.split(|b| *b == 0).collect::<Vec<_>>();
        let mut out = vec![];
        for pair in names.chunks_exact(2) {
            let status = std::str::from_utf8(pair[0]).map_err(|_| ServiceError::Failed)?;
            let path = std::str::from_utf8(pair[1]).map_err(|_| ServiceError::Failed)?;
            let bytes = self
                .store
                .git_bytes(&r.place, &["diff", "--no-color", &span, "--", path])?;
            let diff = String::from_utf8_lossy(&bytes);
            let patch = diff
                .find("\n@@")
                .map(|i| diff[i + 1..].trim_end().to_owned());
            let mut added = 0;
            let mut deleted = 0;
            for line in patch.as_deref().unwrap_or("").lines() {
                if line.starts_with('+') {
                    added += 1;
                }
                if line.starts_with('-') {
                    deleted += 1;
                }
            }
            out.push(crate::file_change(
                path,
                match status {
                    "A" => "added",
                    "D" => "removed",
                    "M" => "modified",
                    _ => "changed",
                },
                patch,
                added,
                deleted,
            )?);
        }
        Ok(out)
    }
    fn ready(&self, u: &str, logs: bool, limit: usize) -> Result<Readiness, ServiceError> {
        if limit == 0 || limit > MAX_LOG_TAIL_BYTES {
            return Err(ServiceError::InvalidParameters);
        }
        let r = Ref::locate(u, WorkKind::PullRequest)?;
        let v = self.store.load(&r)?;
        let head = self.head(&v)?;
        if head.is_empty() {
            return Err(ServiceError::Failed);
        }
        let c = match fs::read_to_string(
            self.store
                .dir(WorkKind::PullRequest, &r.place)
                .join("checks")
                .join(&head),
        ) {
            Ok(s) => s,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => self.checks.clone(),
            Err(_) => return Err(ServiceError::Failed),
        };
        let freshness = if v["state"] == "open" {
            let base = self.store.tip(&r.place, text(&v, "base"))?;
            if base.is_empty() {
                return Err(ServiceError::Failed);
            }
            let n = self
                .store
                .git(
                    &r.place,
                    &["rev-list", "--count", &format!("{head}..{base}")],
                )?
                .parse()
                .map_err(|_| ServiceError::Failed)?;
            Some(Freshness {
                base_sha: base.clone(),
                behind_by: n,
                current_base_sha: base,
                requires_up_to_date: true,
            })
        } else {
            None
        };
        let failed = if logs {
            let mut failed = vec![];
            if c == "failure" {
                let folder = self
                    .store
                    .dir(WorkKind::PullRequest, &r.place)
                    .join("artifacts")
                    .join(&head);
                let mut names: Vec<String> = fs::read_dir(folder)
                    .into_iter()
                    .flatten()
                    .filter_map(Result::ok)
                    .map(|e| e.path())
                    .filter(|p| p.extension().is_some_and(|x| x == "zip"))
                    .filter_map(|p| p.file_stem().and_then(|s| s.to_str()).map(str::to_owned))
                    .collect();
                names.sort();
                failed.push(FailedLog {
                    job_id: "local@1".into(),
                    name: "local".into(),
                    conclusion: c.clone(),
                    url: "".into(),
                    artifact_names: names,
                    log: "".into(),
                    log_bytes: 0,
                    tail_limit_bytes: limit as i64,
                    truncated: false,
                })
            }
            Some(failed)
        } else {
            None
        };
        readiness_of(
            r.target(&v),
            head.clone(),
            head,
            vec![Check {
                name: "local".into(),
                status: if c == "pending" {
                    "in_progress"
                } else {
                    "completed"
                }
                .into(),
                conclusion: (c != "pending").then_some(c),
                details_url: "".into(),
                details: None,
            }],
            false,
            freshness,
            failed,
        )
    }
    fn code_read(
        &self,
        r: Resource,
        s: &str,
        p: Option<&Value>,
        cursor: &str,
    ) -> Result<ReadPage, ServiceError> {
        let c = read_conditions(r, p, cursor)?;
        if r.is_ticket_service() {
            return Err(ServiceError::InvalidResource);
        }
        if r == Resource::DependencyAlerts {
            return Err(ServiceError::Unsupported);
        }
        if r == Resource::TicketPullRequests {
            if !s.starts_with("local:") {
                return Err(ServiceError::Unsupported);
            }
            let tr = Ref::locate(s, WorkKind::Ticket)?;
            self.store.load(&tr)?;
            let entries=self.store.items(WorkKind::PullRequest,None)?.iter().filter(|(_,v)|array(v,"tickets").iter().any(|l|l["url"]==tr.url())).map(|(r,v)|json!({"url":r.url(),"label":r.label(),"title":v["title"],"state":v["state"],"merged":v["merged"]})).collect();
            return page(r, entries, &c, cursor);
        }
        let rf = Ref::locate(s, WorkKind::PullRequest)?;
        let v = self.store.load(&rf)?;
        if r == Resource::PullRequests {
            return ReadPage::of(r, [self.detail(&rf, &v)?], None, Some(rf.target(&v)));
        }
        if r == Resource::PullRequestReadiness {
            let ready = self.ready(
                s,
                c["failed_logs"] == true,
                c["log_tail_bytes"]
                    .as_u64()
                    .unwrap_or(MAX_LOG_TAIL_BYTES as u64) as usize,
            )?;
            return ReadPage::of(r, [json!(ready)], None, Some(ready.target));
        }
        let entries=match r{Resource::PullRequestComments=>array(&v,"comments").iter().map(|v|self.comment_value(v)).collect(),Resource::PullRequestReviews=>array(&v,"reviews").iter().map(|v|json!({"id":v["id"],"body":v["body"],"author":v["author"],"author_type":self.author_type(text(v,"author")),"verdict":v["verdict"],"commit":v["commit"],"submitted_at":v["created_at"],"url":null})).collect(),Resource::PullRequestFiles=>self.files(&rf,&v)?,Resource::PullRequestThreads=>self.threads(&v).iter().map(|(root,cs)|json!({"id":root["id"],"resolved":null,"outdated":null,"comments":cs.iter().map(|c|self.thread_comment(c)).collect::<Vec<_>>(),"comments_complete":true})).collect(),Resource::ReviewThreadComments=>self.threads(&v).into_iter().find(|(root,_)|root["id"]==c["node"]).ok_or(ServiceError::NotFound)?.1.iter().map(|c|self.thread_comment(c)).collect(),_=>return Err(ServiceError::InvalidResource)};
        page(r, entries, &c, cursor)
    }
}
impl CodeHostingService for LocalService {
    fn read<'a>(
        &'a self,
        r: Resource,
        s: &'a str,
        p: Option<&'a Value>,
        c: &'a str,
    ) -> ServiceFuture<'a, ReadPage> {
        Box::pin(async move { self.code_read(r, s, p, c) })
    }
    fn identity(&self, _: bool) -> ServiceFuture<'_, BTreeMap<String, String>> {
        Box::pin(async { Ok(BTreeMap::new()) })
    }
    fn comment<'a>(
        &'a self,
        u: &'a str,
        b: &'a str,
        s: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        Box::pin(async move { self.write_comment(u, WorkKind::PullRequest, b, s) })
    }
    fn create_pull_request(&self, q: CreatePullRequest) -> ServiceFuture<'_, PullRequestCreated> {
        Box::pin(async move {
            let _g = lock()?;
            let base = if q.base.trim().is_empty() {
                self.store
                    .git(&q.repository, &["symbolic-ref", "--short", "HEAD"])?
            } else {
                q.base.trim().into()
            };
            let existing = self
                .store
                .items(WorkKind::PullRequest, Some(&q.repository))?
                .into_iter()
                .find(|(_, v)| v["state"] == "open" && v["head"] == q.head && v["base"] == base);
            let created = existing.is_none();
            let (r, v) = if let Some(pair) = existing {
                pair
            } else {
                let links = self.links(&q.tickets, &q.repository)?;
                let (r, mut v) = self.store.new_item(
                    WorkKind::PullRequest,
                    &q.repository,
                    &q.title,
                    &q.body,
                    &self.actor,
                )?;
                for (k, val) in [
                    ("head", json!(q.head)),
                    ("head_repository", json!(q.repository)),
                    ("base", json!(base)),
                    ("draft", json!(q.draft)),
                    ("merged", json!(false)),
                    ("reviewers", json!([])),
                    ("reviews", json!([])),
                    ("review_comments", json!([])),
                    ("tickets", links),
                ] {
                    v[k] = val
                }
                self.store.save(&r, &mut v)?;
                (r, v)
            };
            Ok(PullRequestCreated {
                created,
                draft: v["draft"] == true,
                head: q.head,
                base,
                target: r.target(&v),
            })
        })
    }
    fn update_pull_request<'a>(
        &'a self,
        u: &'a str,
        q: UpdatePullRequest,
    ) -> ServiceFuture<'a, PullRequestUpdated> {
        Box::pin(async move {
            let _g = lock()?;
            let r = Ref::locate(u, WorkKind::PullRequest)?;
            let mut v = self.store.load(&r)?;
            if let Some(ls) = q.tickets {
                v["tickets"] = self.links(&ls, &r.place)?
            }
            if let Some(b) = q.body {
                v["body"] = json!(b)
            }
            if let Some(t) = q.title {
                v["title"] = json!(t)
            }
            self.store.save(&r, &mut v)?;
            Ok(PullRequestUpdated {
                body: text(&v, "body").into(),
                target: r.target(&v),
            })
        })
    }
    fn review<'a>(
        &'a self,
        u: &'a str,
        b: &'a str,
        verdict: ReviewVerdict,
        head: &'a str,
    ) -> ServiceFuture<'a, ReviewSubmitted> {
        Box::pin(async move {
            let _g = lock()?;
            let (r, mut v) = self.at_head(u, head)?;
            let mut e = entry(&v, "review", &self.actor, b, None);
            e["verdict"] = json!(verdict);
            e["commit"] = json!(head);
            v["reviews"]
                .as_array_mut()
                .ok_or(ServiceError::Failed)?
                .push(e);
            v["reviewers"] = json!(
                array(&v, "reviewers")
                    .into_iter()
                    .filter(|r| r != &self.actor)
                    .collect::<Vec<_>>()
            );
            self.store.save(&r, &mut v)?;
            Ok(ReviewSubmitted {
                verdict,
                commit: head.into(),
                url: None,
                target: r.target(&v),
            })
        })
    }
    fn review_comment<'a>(
        &'a self,
        u: &'a str,
        q: ReviewComment,
    ) -> ServiceFuture<'a, ReviewCommentWritten> {
        Box::pin(async move {
            let _g = lock()?;
            let (r, mut v) = self.at_head(u, &q.head)?;
            let mut e = entry(&v, "inline", &self.actor, &q.body, None);
            for (k, val) in [
                ("path", json!(q.path)),
                ("line", json!(q.line)),
                ("side", json!(q.side)),
                ("start_line", json!(q.start_line)),
                ("start_side", json!(q.start_side)),
                ("commit", json!(q.head)),
                ("in_reply_to", Value::Null),
            ] {
                e[k] = val
            }
            v["review_comments"]
                .as_array_mut()
                .ok_or(ServiceError::Failed)?
                .push(e.clone());
            self.store.save(&r, &mut v)?;
            Ok(ReviewCommentWritten {
                id: text(&e, "id").into(),
                url: None,
                created_at: Some(text(&e, "created_at").into()),
                path: q.path,
                line: q.line,
                side: q.side,
                target: r.target(&v),
            })
        })
    }
    fn reply<'a>(&'a self, u: &'a str, to: &'a str, b: &'a str) -> ServiceFuture<'a, ReplyWritten> {
        Box::pin(async move {
            let _g = lock()?;
            let r = Ref::locate(u, WorkKind::PullRequest)?;
            let mut v = self.store.load(&r)?;
            let root = array(&v, "review_comments")
                .into_iter()
                .find(|c| c["id"] == to && c["in_reply_to"].is_null())
                .ok_or(ServiceError::NotFound)?;
            let mut e = entry(&v, "inline", &self.actor, b, None);
            for k in ["path", "line", "side", "commit"] {
                e[k] = root[k].clone()
            }
            e["in_reply_to"] = json!(to);
            v["review_comments"]
                .as_array_mut()
                .ok_or(ServiceError::Failed)?
                .push(e.clone());
            self.store.save(&r, &mut v)?;
            Ok(ReplyWritten {
                id: text(&e, "id").into(),
                url: None,
                created_at: Some(text(&e, "created_at").into()),
                target: r.target(&v),
            })
        })
    }
    fn add_reaction<'a>(
        &'a self,
        u: &'a str,
        id: &'a str,
        r: SemanticReaction,
    ) -> ServiceFuture<'a, ReactionAdded> {
        Box::pin(async move { self.react(u, WorkKind::PullRequest, id, r) })
    }
    fn readiness<'a>(&'a self, u: &'a str, f: bool, l: usize) -> ServiceFuture<'a, Readiness> {
        Box::pin(async move { self.ready(u, f, l) })
    }
    fn open_pull_requests<'a>(&'a self, p: &'a str, b: &'a str) -> ServiceFuture<'a, Vec<String>> {
        Box::pin(async move {
            Ok(self
                .store
                .items(WorkKind::PullRequest, Some(p))?
                .iter()
                .filter(|(_, v)| v["state"] == "open" && v["head"] == b)
                .map(|(r, _)| r.url())
                .collect())
        })
    }
    fn artifact<'a>(
        &'a self,
        u: &'a str,
        n: &'a str,
        l: usize,
    ) -> ServiceFuture<'a, Download<ArtifactRef>> {
        Box::pin(async move {
            if n.is_empty() || n.contains('/') || n.contains('\\') || n == ".." {
                return Err(ServiceError::NotFound);
            }
            let r = Ref::locate(u, WorkKind::PullRequest)?;
            let sha = self.head(&self.store.load(&r)?)?;
            if sha.is_empty() {
                return Err(ServiceError::NotFound);
            }
            let path = self
                .store
                .dir(WorkKind::PullRequest, &r.place)
                .join("artifacts")
                .join(&sha)
                .join(format!("{n}.zip"));
            let size = fs::metadata(&path)
                .map_err(|_| ServiceError::NotFound)?
                .len();
            if size > l as u64 {
                return Err(ServiceError::InvalidParameters);
            }
            let bytes = fs::read(path).map_err(|_| ServiceError::Failed)?;
            if bytes.len() > l {
                return Err(ServiceError::InvalidResult);
            }
            Ok(Download {
                metadata: ArtifactRef {
                    name: n.into(),
                    id: format!("{sha}/{n}"),
                },
                content: bytes,
            })
        })
    }
    fn clone_url<'a>(&'a self, p: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async move {
            self.store.git(p, &["rev-parse", "--git-dir"])?;
            url::Url::from_file_path(self.store.bare(p))
                .map(|u| u.into())
                .map_err(|_| ServiceError::Failed)
        })
    }
    fn default_branch<'a>(&'a self, p: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async move { self.store.git(p, &["symbolic-ref", "--short", "HEAD"]) })
    }
    fn pull_request_head<'a>(&'a self, u: &'a str) -> ServiceFuture<'a, PullRequestHead> {
        Box::pin(async move {
            let v = self.store.load(&Ref::locate(u, WorkKind::PullRequest)?)?;
            Ok(PullRequestHead {
                repository: text(&v, "head_repository").into(),
                branch: text(&v, "head").into(),
            })
        })
    }
    fn git_credential(&self) -> ServiceFuture<'_, Option<GitCredential>> {
        Box::pin(async { Ok(None) })
    }
    fn check_push(&self, p: &str) -> Result<(), ServiceError> {
        self.store.scope.authorize(p).map(|_| ())
    }
    fn commit_url(&self, _: &str, _: &str) -> String {
        String::new()
    }
    fn repository_from_remote(&self, u: &str) -> Option<String> {
        let path = url::Url::parse(u).ok()?.to_file_path().ok()?;
        if path.file_name()? != "repository.git" {
            return None;
        }
        let repo = percent_encoding::percent_decode_str(path.parent()?.file_name()?.to_str()?)
            .decode_utf8()
            .ok()?
            .into_owned();
        (path == self.store.bare(&repo)).then_some(repo)
    }
    fn pull_request_candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        Box::pin(async {
            let mut out = vec![];
            for (r, v) in self.store.items(WorkKind::PullRequest, None)? {
                if let Some(t) = self.pr_work(&r, &v).await? {
                    out.push(t)
                }
            }
            Ok(out)
        })
    }
    fn refresh_pull_request<'a>(&'a self, t: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        Box::pin(async move {
            let r = Ref::locate(
                t.pull_request_url
                    .as_deref()
                    .ok_or(ServiceError::NotFound)?,
                WorkKind::PullRequest,
            )?;
            self.pr_work(&r, &self.store.load(&r)?).await
        })
    }
    fn convert_pull_request_to_draft<'a>(&'a self, u: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(async move {
            let _g = lock()?;
            let r = Ref::locate(u, WorkKind::PullRequest)?;
            self.store.scope.authorize(&r.place)?;
            let path = self.store.path(&r).with_extension("lock");
            let _file = super::store::file_lock(&path)?;
            let mut v = self.store.load(&r)?;
            if v["state"] != "open" || v["draft"] == true {
                return Ok(false);
            }
            v["draft"] = json!(true);
            self.store.save(&r, &mut v)?;
            Ok(true)
        })
    }
    fn can_receive_work<'a>(&'a self, u: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(async move {
            let r = Ref::locate(u, WorkKind::PullRequest)?;
            if self.store.scope.authorize(&r.place).is_err() {
                return Ok(false);
            }
            let p = self.snapshot(&r, &self.store.load(&r)?)?;
            Ok(matches!(
                crate::pull_request_work(&p, &self.actor, chrono::Utc::now()),
                Some("pull_request_feedback" | "pull_request_review")
            ))
        })
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}
fn reactors(v: &Value) -> BTreeSet<String> {
    array(v, "reactions")
        .iter()
        .filter_map(|r| r["author"].as_str().map(str::to_owned))
        .collect()
}
impl LocalService {
    fn snapshot(&self, r: &Ref, v: &Value) -> Result<crate::PatrolPullRequest, ServiceError> {
        let mut reviews = array(v, "reviews")
            .iter()
            .map(|e| crate::PatrolReview {
                author: text(e, "author").into(),
                body: text(e, "body").into(),
                submitted_at: text(e, "created_at").into(),
                commit_oid: text(e, "commit").into(),
                reactors: reactors(e),
                reply_only: false,
            })
            .collect::<Vec<_>>();
        reviews.extend(
            array(v, "review_comments")
                .iter()
                .map(|e| crate::PatrolReview {
                    author: text(e, "author").into(),
                    body: String::new(),
                    submitted_at: text(e, "created_at").into(),
                    commit_oid: text(e, "commit").into(),
                    reply_only: !e["in_reply_to"].is_null(),
                    reactors: BTreeSet::new(),
                }),
        );
        reviews.sort_by(|a, b| a.submitted_at.cmp(&b.submitted_at));
        Ok(crate::PatrolPullRequest {
            id: r.url(),
            url: r.url(),
            label: r.label(),
            title: text(v, "title").into(),
            body: text(v, "body").into(),
            open: v["state"] == "open",
            is_draft: v["draft"] == true,
            created_at: text(v, "created_at").into(),
            author: text(v, "author").into(),
            head_oid: self.head(v)?,
            ready_at: String::new(),
            requested_reviewers: array(v, "reviewers")
                .iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect(),
            reviews,
            comments: array(v, "comments")
                .iter()
                .map(|c| crate::PatrolComment {
                    author: text(c, "author").into(),
                    created_at: text(c, "created_at").into(),
                    reactors: reactors(c),
                    status: WorkflowStatus::from_payload(&c["status"]),
                })
                .collect(),
            threads: self
                .threads(v)
                .iter()
                .filter_map(|(_, cs)| {
                    cs.last().map(|last| crate::PatrolThread {
                        resolved: false,
                        participants: cs.iter().map(|c| text(c, "author").into()).collect(),
                        last_author: text(last, "author").into(),
                        last_created_at: text(last, "created_at").into(),
                        last_reactors: reactors(last),
                    })
                })
                .collect(),
        })
    }
    async fn pr_work(&self, r: &Ref, v: &Value) -> Result<Option<Task>, ServiceError> {
        if self.store.scope.authorize(&r.place).is_err() {
            return Ok(None);
        }
        let p = self.snapshot(r, v)?;
        Ok(crate::patrol(
            self,
            &p,
            &self.actor,
            "local",
            &self.person_id,
            chrono::Utc::now(),
        )
        .await)
    }
}
