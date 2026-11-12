use super::*;
use branvil::api::{pulls, tickets};
use std::collections::BTreeMap;
impl BranvilService {
    async fn validate_ticket_links(
        &self,
        repository: &str,
        links: &[TicketLink],
    ) -> Result<Vec<TicketLink>, ServiceError> {
        let mut result = Vec::with_capacity(links.len());
        for link in links {
            let own = link.url.starts_with(&format!("{}/", self.api.base_url()));
            let item = if own {
                Some(self.ticket(&link.url).await?)
            } else {
                None
            };
            if link.closes
                && item
                    .as_ref()
                    .is_none_or(|v| !text(v, "repository").eq_ignore_ascii_case(repository))
            {
                return Err(diagnostic(
                    "integrations.branvil.api.closes_foreign",
                    [
                        ("repository", repository.to_owned()),
                        ("url", link.url.clone()),
                    ],
                ));
            }
            result.push(TicketLink {
                url: item
                    .as_ref()
                    .map(|v| text(v, "url").to_owned())
                    .unwrap_or_else(|| link.url.clone()),
                closes: link.closes,
            });
        }
        Ok(result)
    }
    fn pull_value(&self, v: Value) -> Value {
        let t = self.target(&v, WorkKind::PullRequest);
        json!({"kind":"pull_request","url":t.url,"title":t.title,"label":t.label,"repository":v["repository"],"body":v["body"],"state":v["state"],"assignees":[],"author":v["author"],"author_type":self.author_type(text(&v,"author")),"requested_reviewers":v["reviewers"],"labels":v["labels"],"head":v["head"],"head_repository":v["head_repository"],"head_sha":v["head_sha"],"base":v["base"],"merged":v["merged"],"draft":v["draft"],"changed_files":v["changed_files"]})
    }
    fn thread_comment(&self, v: Value) -> Value {
        let reply = v["reply_to_id"].clone();
        let mut v = self.comment_value(v);
        v["reply_to_id"] = reply;
        v
    }
}
impl CodeHostingService for BranvilService {
    fn linked_tickets<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, Vec<TicketLink>> {
        Box::pin(async move {
            let (repo, key) = self.item(url, "pulls")?;
            let p = self
                .member()
                .await?
                .pulls_get(&repo, number(&key)?)
                .await
                .map_err(|e| error(e, "pulls_get"))?;
            Ok(p.tickets
                .into_iter()
                .map(|t| TicketLink {
                    url: t.url,
                    closes: t.closes,
                })
                .collect())
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
            if resource.is_ticket_service() {
                return Err(ServiceError::InvalidResource);
            }
            if resource == Resource::DependencyAlerts {
                self.repository(subject)?;
                let m = self.member().await?;
                if let Some(id) = c["id"].as_str().filter(|s| !s.is_empty()) {
                    let alert = m
                        .upstream_dependency_alert(subject, id)
                        .await
                        .map_err(|e| error(e, "upstream_dependency_alert"))?;
                    return ReadPage::of(resource, [value(alert)?], None, None);
                }
                use branvil::api::AlertState;
                let state = match text(&c, "state") {
                    "resolved" => AlertState::Resolved,
                    "dismissed" => AlertState::Dismissed,
                    _ => AlertState::Open,
                };
                let p = m
                    .upstream_dependency_alerts(
                        subject,
                        state,
                        self.page(&m, resource, subject, &c, continuation)?,
                    )
                    .await
                    .map_err(|e| error(e, "upstream_dependency_alerts"))?;
                return ReadPage::of(
                    resource,
                    p.items
                        .into_iter()
                        .map(value)
                        .collect::<Result<Vec<_>, _>>()?,
                    p.next_cursor,
                    None,
                );
            }
            if resource == Resource::PullRequestReadiness {
                let r = CodeHostingService::readiness(
                    self,
                    subject,
                    c["failed_logs"] == true,
                    c["log_tail_bytes"].as_u64().unwrap() as usize,
                )
                .await?;
                let target = r.target.clone();
                return ReadPage::of(resource, [value(r)?], None, Some(target));
            }
            let m = self.member().await?;
            if resource == Resource::TicketPullRequests {
                if !subject.starts_with(&format!("{}/", self.api.base_url())) {
                    return Err(diagnostic(
                        "integrations.branvil.api.foreign_ticket",
                        [("url", subject.to_owned())],
                    ));
                }
                let (repo, key) = self.item(subject, "tickets")?;
                let p = m
                    .tickets_ticket_pulls(
                        &repo,
                        &key,
                        self.page(&m, resource, subject, &c, continuation)?,
                    )
                    .await
                    .map_err(|e| error(e, "tickets_ticket_pulls"))?;
                return ReadPage::of(resource,p.items.into_iter().map(|v|json!({"url":v.url,"title":v.title,"label":v.label,"state":v.state,"merged":v.merged})),p.next_cursor,None);
            }
            let (repo, key) = self.item(subject, "pulls")?;
            let n = number(&key)?;
            if resource == Resource::PullRequests {
                let v = value(
                    m.pulls_get(&repo, n)
                        .await
                        .map_err(|e| error(e, "pulls_get"))?,
                )?;
                let t = self.target(&v, WorkKind::PullRequest);
                return ReadPage::of(resource, [self.pull_value(v)], None, Some(t));
            }
            let page = self.page(&m, resource, subject, &c, continuation)?;
            let (items, next) = match resource {
                Resource::PullRequestComments => {
                    let p = m
                        .pulls_list_comments(&repo, n, page)
                        .await
                        .map_err(|e| error(e, "pulls_list_comments"))?;
                    (
                        p.items
                            .into_iter()
                            .map(|v| value(v).map(|v| self.comment_value(v)))
                            .collect::<Result<Vec<_>, _>>()?,
                        p.next_cursor,
                    )
                }
                Resource::PullRequestReviews => {
                    let p = m
                        .pulls_list_reviews(&repo, n, page)
                        .await
                        .map_err(|e| error(e, "pulls_list_reviews"))?;
                    (p.items.into_iter().map(|v|json!({"id":v.id,"body":v.body,"author":v.author,"author_type":self.author_type(&v.author),"verdict":v.verdict,"commit":v.commit,"submitted_at":v.submitted_at,"url":v.url})).collect(),p.next_cursor)
                }
                Resource::PullRequestThreads => {
                    let p = m
                        .pulls_list_threads(&repo, n, page)
                        .await
                        .map_err(|e| error(e, "pulls_list_threads"))?;
                    (p.items.into_iter().map(|v|Ok(json!({"id":v.id,"resolved":v.resolved,"outdated":v.outdated,"comments":v.comments.into_iter().map(|c|value(c).map(|v|self.thread_comment(v))).collect::<Result<Vec<_>,_>>()?,"comments_complete":true}))).collect::<Result<Vec<_>,ServiceError>>()?,p.next_cursor)
                }
                Resource::ReviewThreadComments => {
                    let p = m
                        .pulls_thread_comments(&repo, n, text(&c, "node"), page)
                        .await
                        .map_err(|e| error(e, "pulls_thread_comments"))?;
                    (
                        p.items
                            .into_iter()
                            .map(|v| value(v).map(|v| self.thread_comment(v)))
                            .collect::<Result<Vec<_>, _>>()?,
                        p.next_cursor,
                    )
                }
                Resource::PullRequestFiles => {
                    let p = m
                        .pulls_files(&repo, n, page)
                        .await
                        .map_err(|e| error(e, "pulls_files"))?;
                    (
                        p.items
                            .into_iter()
                            .map(|v| {
                                crate::file_change(
                                    &v.path,
                                    v.status,
                                    v.patch,
                                    v.additions as i64,
                                    v.deletions as i64,
                                )
                            })
                            .collect::<Result<Vec<_>, _>>()?,
                        p.next_cursor,
                    )
                }
                _ => return Err(ServiceError::InvalidResource),
            };
            ReadPage::of(resource, items, next, None)
        })
    }
    fn identity(&self, check: bool) -> ServiceFuture<'_, BTreeMap<String, String>> {
        Box::pin(async move {
            if check {
                let m = self.member().await?;
                m.members_get(self.username.clone())
                    .await
                    .map_err(|e| error(e, "members_get"))?;
            }
            Ok(BTreeMap::from([(
                "branvil_username".into(),
                self.username.clone(),
            )]))
        })
    }
    fn comment<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, CommentWritten> {
        Box::pin(async move {
            let (r, k) = self.item(url, "pulls")?;
            self.writable(&r)?;
            let m = self.member().await?;
            let t = value(
                m.pulls_get(&r, number(&k)?)
                    .await
                    .map_err(|e| error(e, "pulls_get"))?,
            )?;
            let c = m
                .pulls_add_comment(
                    &r,
                    number(&k)?,
                    tickets::NewComment {
                        body: self.comment_body(body, status, text(&t, "author")),
                        meta: status.map(WorkflowStatus::payload),
                    },
                )
                .await
                .map_err(|e| error(e, "pulls_add_comment"))?;
            let v = value(c)?;
            Ok(CommentWritten {
                id: text(&v, "id").into(),
                url: v["url"].as_str().map(Into::into),
                author: text(&v, "author").into(),
                created_at: v["created_at"].as_str().map(Into::into),
                target: self.target(&t, WorkKind::PullRequest),
            })
        })
    }
    fn create_pull_request(&self, r: CreatePullRequest) -> ServiceFuture<'_, PullRequestCreated> {
        Box::pin(async move {
            self.repository(&r.repository)?;
            let links = self
                .validate_ticket_links(&r.repository, &r.tickets)
                .await?;
            self.writable(&r.repository)?;
            let p=self.member().await?.pulls_create(&r.repository,decode(json!({"head":r.head,"base":r.base,"title":r.title,"body":r.body,"draft":r.draft,"tickets":links}))?).await.map_err(|e| error(e, "pulls_create"))?;
            let created = p.created;
            let v = value(p.pull)?;
            Ok(PullRequestCreated {
                created,
                draft: v["draft"] == true,
                head: text(&v, "head").into(),
                base: text(&v, "base").into(),
                target: self.target(&v, WorkKind::PullRequest),
            })
        })
    }
    fn update_pull_request<'a>(
        &'a self,
        url: &'a str,
        r: UpdatePullRequest,
    ) -> ServiceFuture<'a, PullRequestUpdated> {
        Box::pin(async move {
            let (repo, key) = self.item(url, "pulls")?;
            self.writable(&repo)?;
            let links = match &r.tickets {
                Some(links) => Some(self.validate_ticket_links(&repo, links).await?),
                None => None,
            };
            let v = value(
                self.member()
                    .await?
                    .pulls_update(
                        &repo,
                        number(&key)?,
                        decode(json!({"title":r.title,"body":r.body,"tickets":links}))?,
                    )
                    .await
                    .map_err(|e| error(e, "pulls_update"))?,
            )?;
            Ok(PullRequestUpdated {
                body: text(&v, "body").into(),
                target: self.target(&v, WorkKind::PullRequest),
            })
        })
    }
    fn review<'a>(
        &'a self,
        url: &'a str,
        body: &'a str,
        verdict: ReviewVerdict,
        head: &'a str,
    ) -> ServiceFuture<'a, gb_ports::ReviewSubmitted> {
        Box::pin(async move {
            let (r, k) = self.item(url, "pulls")?;
            self.writable(&r)?;
            let m = self.member().await?;
            let t = value(
                m.pulls_get(&r, number(&k)?)
                    .await
                    .map_err(|e| error(e, "pulls_get"))?,
            )?;
            let v = m
                .pulls_review(
                    &r,
                    number(&k)?,
                    decode(json!({"body":body.trim_end(),"verdict":verdict,"commit":head}))?,
                )
                .await
                .map_err(|e| error(e, "pulls_review"))?;
            Ok(gb_ports::ReviewSubmitted {
                verdict,
                commit: v.commit,
                url: Some(v.url),
                target: self.target(&t, WorkKind::PullRequest),
            })
        })
    }
    fn review_comment<'a>(
        &'a self,
        url: &'a str,
        r: ReviewComment,
    ) -> ServiceFuture<'a, ReviewCommentWritten> {
        Box::pin(async move {
            let (repo, key) = self.item(url, "pulls")?;
            self.writable(&repo)?;
            let m = self.member().await?;
            let t = value(
                m.pulls_get(&repo, number(&key)?)
                    .await
                    .map_err(|e| error(e, "pulls_get"))?,
            )?;
            let c=value(m.pulls_inline_comment(&repo,number(&key)?,decode(json!({"body":r.body.trim_end(),"commit":r.head,"path":r.path,"line":r.line,"side":r.side,"start_line":r.start_line.filter(|_|r.start_side.is_some()),"start_side":r.start_side.as_ref().filter(|_|r.start_line.is_some())}))?).await.map_err(|e| error(e, "pulls_inline_comment"))?)?;
            Ok(ReviewCommentWritten {
                id: text(&c, "id").into(),
                url: c["url"].as_str().map(Into::into),
                created_at: c["created_at"].as_str().map(Into::into),
                path: r.path,
                line: r.line,
                side: r.side,
                target: self.target(&t, WorkKind::PullRequest),
            })
        })
    }
    fn reply<'a>(
        &'a self,
        url: &'a str,
        id: &'a str,
        body: &'a str,
    ) -> ServiceFuture<'a, ReplyWritten> {
        Box::pin(async move {
            let (r, k) = self.item(url, "pulls")?;
            self.writable(&r)?;
            let m = self.member().await?;
            let t = value(
                m.pulls_get(&r, number(&k)?)
                    .await
                    .map_err(|e| error(e, "pulls_get"))?,
            )?;
            let c = value(
                m.pulls_reply(
                    &r,
                    number(&k)?,
                    id,
                    pulls::NewReply {
                        body: body.trim_end().into(),
                    },
                )
                .await
                .map_err(|e| error(e, "pulls_reply"))?,
            )?;
            Ok(ReplyWritten {
                id: text(&c, "id").into(),
                url: c["url"].as_str().map(Into::into),
                created_at: c["created_at"].as_str().map(Into::into),
                target: self.target(&t, WorkKind::PullRequest),
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
            let (repo, key) = self.item(url, "pulls")?;
            self.writable(&repo)?;
            self.member()
                .await?
                .pulls_react(
                    &repo,
                    number(&key)?,
                    tickets::NewReaction {
                        target_id: id.into(),
                        reaction: reaction(r).into(),
                    },
                    true,
                )
                .await
                .map_err(|e| error(e, "pulls_react"))?;
            Ok(ReactionAdded {
                reaction: r,
                id: id.into(),
            })
        })
    }
    fn readiness<'a>(
        &'a self,
        url: &'a str,
        logs: bool,
        tail: usize,
    ) -> ServiceFuture<'a, Readiness> {
        Box::pin(self.pull_readiness(url, logs, tail))
    }
    fn open_pull_requests<'a>(
        &'a self,
        repo: &'a str,
        branch: &'a str,
    ) -> ServiceFuture<'a, Vec<String>> {
        Box::pin(async move {
            self.repository(repo)?;
            let m = self.member().await?;
            let mut out = vec![];
            let mut cursor = None;
            loop {
                let p = m
                    .pulls_list(
                        repo,
                        decode(json!({"state":"open","head":branch}))?,
                        m.page(
                            format!("open:{repo}:{branch}"),
                            Some(100),
                            cursor.as_deref(),
                        )
                        .map_err(|e| error(e, "page"))?,
                    )
                    .await
                    .map_err(|e| error(e, "pulls_list"))?;
                out.extend(p.items.into_iter().map(|p| p.url));
                cursor = p.next_cursor;
                if cursor.is_none() {
                    return Ok(out);
                }
            }
        })
    }
    fn artifact<'a>(
        &'a self,
        url: &'a str,
        name: &'a str,
        limit: usize,
    ) -> ServiceFuture<'a, Download<ArtifactRef>> {
        Box::pin(self.pull_artifact(url, name, limit))
    }
    fn clone_url<'a>(&'a self, repo: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async move {
            self.repository(repo)?;
            Ok(format!("{}/git/{repo}.git", self.api.base_url()))
        })
    }
    fn default_branch<'a>(&'a self, repo: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async move {
            self.repository(repo)?;
            Ok(self
                .member()
                .await?
                .repos_get(repo)
                .await
                .map_err(|e| error(e, "repos_get"))?
                .default_branch)
        })
    }
    fn pull_request_head<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, PullRequestHead> {
        Box::pin(async move {
            let v = self.pull(url).await?;
            Ok(PullRequestHead {
                repository: text(&v, "head_repository").into(),
                branch: text(&v, "head").into(),
            })
        })
    }
    fn git_credential(&self) -> ServiceFuture<'_, Option<GitCredential>> {
        Box::pin(async move {
            self.member().await?;
            Ok(Some(GitCredential {
                username: self.username.clone(),
                password: self.token.clone(),
            }))
        })
    }
    fn check_push(&self, repo: &str) -> Result<(), ServiceError> {
        self.writable(repo)
    }
    fn commit_url(&self, repo: &str, sha: &str) -> String {
        format!("{}/{repo}/-/commits/{sha}", self.api.base_url())
    }
    fn repository_from_remote(&self, url: &str) -> Option<String> {
        let path = url
            .trim()
            .strip_prefix(&format!("{}/git/", self.api.base_url()))?;
        let repo = path.strip_suffix(".git").unwrap_or(path);
        crate::valid_repository_path(repo).then(|| repo.into())
    }
    fn pull_request_candidates(&self) -> ServiceFuture<'_, Vec<Task>> {
        Box::pin(self.pull_candidates())
    }
    fn refresh_pull_request<'a>(&'a self, t: &'a Task) -> ServiceFuture<'a, Option<Task>> {
        Box::pin(self.pull_refresh(t))
    }
    fn convert_pull_request_to_draft<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(self.pull_draft(url))
    }
    fn can_receive_work<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, bool> {
        Box::pin(self.pull_can_receive(url))
    }
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}
impl BranvilService {
    async fn pull_artifact(
        &self,
        url: &str,
        name: &str,
        limit: usize,
    ) -> Result<Download<ArtifactRef>, ServiceError> {
        let (r, k) = self.item(url, "pulls")?;
        let m = self.member().await?;
        let p = m
            .pulls_get(&r, number(&k)?)
            .await
            .map_err(|e| error(e, "pulls_get"))?;
        let content = m
            .artifact(&r, &p.head_sha, name, limit)
            .await
            .map_err(|e| {
                if e.code == "too_large" {
                    diagnostic(
                        "integrations.branvil.api.artifact_too_large",
                        [("name", name.to_owned()), ("limit", limit.to_string())],
                    )
                } else {
                    error(e, "artifact")
                }
            })?;
        Ok(Download {
            metadata: ArtifactRef {
                name: name.into(),
                id: format!("{}/{name}", p.head_sha),
            },
            content,
        })
    }
    async fn pull_readiness(
        &self,
        url: &str,
        logs: bool,
        tail: usize,
    ) -> Result<Readiness, ServiceError> {
        if tail == 0 || tail > MAX_LOG_TAIL_BYTES {
            return Err(ServiceError::InvalidParameters);
        }
        let (repo, key) = self.item(url, "pulls")?;
        let m = self.member().await?;
        let report = m
            .pulls_checks(&repo, number(&key)?)
            .await
            .map_err(|e| error(e, "pulls_checks"))?;
        let mut failed = Vec::new();
        if logs {
            let failed_checks = report
                .checks
                .iter()
                .filter(|c| {
                    c.attempt_id.is_some()
                        && c.conclusion
                            .as_deref()
                            .is_some_and(crate::failed_conclusion)
                })
                .collect::<Vec<_>>();
            let primary = failed_checks
                .iter()
                .filter(|c| {
                    matches!(
                        c.conclusion.as_deref(),
                        Some("action_required" | "failure" | "startup_failure" | "timed_out")
                    )
                })
                .copied()
                .collect::<Vec<_>>();
            let selected = if primary.is_empty() {
                failed_checks
            } else {
                primary
            };
            if !selected.is_empty() {
                let bytes = crate::effective_tail_bytes(tail, selected.len())?;
                for c in selected {
                    let id = c.attempt_id.as_deref().ok_or(ServiceError::InvalidResult)?;
                    let v = m
                        .job_log(&repo, id, bytes as u64)
                        .await
                        .map_err(|e| error(e, "job_log"))?;
                    let log = text(&v, "log").to_string();
                    failed.push(FailedLog {
                        job_id: id.into(),
                        name: c.name.clone(),
                        conclusion: c.conclusion.clone().unwrap_or_default(),
                        url: c.details_url.clone().unwrap_or_default(),
                        artifact_names: c.artifact_names.clone(),
                        log_bytes: log.len() as i64,
                        log,
                        tail_limit_bytes: bytes as i64,
                        truncated: v["truncated"] == true,
                    });
                }
            }
        }
        let current = m
            .pulls_get(&repo, number(&key)?)
            .await
            .map_err(|e| error(e, "pulls_get"))?;
        self.readiness_result(report, value(&current)?, logs.then_some(failed))
    }

    pub(super) fn readiness_result(
        &self,
        report: pulls::ChecksView,
        current: Value,
        failed: Option<Vec<FailedLog>>,
    ) -> Result<Readiness, ServiceError> {
        let checks = report
            .checks
            .iter()
            .map(|c| Check {
                name: c.name.clone(),
                status: c.status.clone(),
                conclusion: c.conclusion.clone(),
                details_url: c.details_url.clone().unwrap_or_default(),
                details: Some(c.details.clone().unwrap_or_default()),
            })
            .collect::<Vec<_>>();
        let freshness = if text(&current, "state") == "open" {
            if report.head_sha.is_empty() {
                return Err(diagnostic(
                    "integrations.branvil.api.no_head",
                    [("url", text(&current, "url").to_owned())],
                ));
            }
            if report.base_sha.is_empty() || text(&current, "base_sha").is_empty() {
                return Err(diagnostic(
                    "integrations.branvil.api.no_base",
                    [("url", text(&current, "url").to_owned())],
                ));
            }
            Some(crate::Freshness {
                base_sha: report.base_sha.clone(),
                behind_by: report.behind_by.ok_or(ServiceError::InvalidResult)? as i64,
                current_base_sha: text(&current, "base_sha").into(),
                requires_up_to_date: false,
            })
        } else {
            None
        };
        crate::readiness_of(
            self.target(&current, WorkKind::PullRequest),
            report.head_sha,
            text(&current, "head_sha").into(),
            checks,
            report.checks_expected,
            freshness,
            failed,
        )
    }
}
