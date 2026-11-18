use super::code_patrol::{array, fold};
use super::*;
use crate::api::{pulls, tickets};
impl CodeAgent {
    pub fn commit_url_for(api: &crate::api::DataApi, repo: &str, sha: &str) -> String {
        format!("{}/{repo}/-/commits/{sha}", api.base_url())
    }
    pub fn repository_for_remote(api: &crate::api::DataApi, url: &str) -> Option<String> {
        let path = url
            .trim()
            .strip_prefix(&format!("{}/git/", api.base_url()))?;
        let repo = path.strip_suffix(".git").unwrap_or(path);
        gb_ports::valid_repository_path(repo).then(|| repo.into())
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
impl CodeHostingService for CodeAgent {
    fn retry_job<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async move {
            let path = url
                .strip_prefix(self.data().base_url())
                .and_then(|p| p.strip_prefix('/'))
                .ok_or(ServiceError::NotFound)?;
            let (repo, id) = path.split_once("/-/jobs/").ok_or(ServiceError::NotFound)?;
            if !gb_ports::valid_repository_path(repo)
                || id.is_empty()
                || id.contains(['/', '?', '#', '%'])
            {
                return Err(ServiceError::NotFound);
            }
            self.writable(repo)?;
            let attempt = self
                .member()
                .await?
                .job_retry(repo, id)
                .await
                .map_err(|e| error(e, "job_retry"))?;
            Ok(format!(
                "{}/{repo}/-/jobs/{}",
                self.data().base_url(),
                attempt.id
            ))
        })
    }

    fn yield_pull_request_checks<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, usize> {
        Box::pin(async move {
            let (repo, key) = self.item(url, "pulls")?;
            let result = self
                .member()
                .await?
                .pulls_yield_checks(&repo, number(&key)?)
                .await
                .map_err(|e| error(e, "pulls_yield_checks"))?;
            Ok(result.yielded.len())
        })
    }

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
                use crate::api::AlertState;
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
                if !subject.starts_with(&format!("{}/", self.data().base_url())) {
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
                                super::pr_diff_lines::file_change(
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
            let links = r.tickets;
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
            let links = r.tickets;
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
            Ok(format!("{}/git/{repo}.git", self.data().base_url()))
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
            let mut lent = self.git_token.lock().await;
            if lent.is_none() {
                self.member().await?;
                *lent = Some(
                    self.data()
                        .owner()
                        .members_issue_token(
                            self.person_id().to_owned(),
                            crate::api::members::NewToken {
                                label: super::GIT_TOKEN_LABEL.into(),
                                replace: false,
                            },
                        )
                        .await
                        .map_err(|e| error(e, "members_issue_token"))?,
                );
            }
            let issued = lent.as_ref().expect("lent above");
            Ok(Some(GitCredential {
                username: self.person_id().to_owned(),
                password: issued.token.clone(),
            }))
        })
    }
    fn check_push(&self, repo: &str) -> Result<(), ServiceError> {
        self.writable(repo)
    }
    fn commit_url(&self, repo: &str, sha: &str) -> String {
        Self::commit_url_for(self.data(), repo, sha)
    }
    fn repository_from_remote(&self, url: &str) -> Option<String> {
        Self::repository_for_remote(self.data(), url)
    }
    fn authored_open_pull_requests(&self) -> ServiceFuture<'_, Vec<String>> {
        Box::pin(async {
            let _open = self.data().guard().map_err(|e| error(e, "authored_open"))?;
            let me = fold(self.person_id());
            self.member().await?;
            let base = self.data().base_url().to_owned();
            let scope = self.write_scope().clone();
            self.data()
                .app
                .store
                .read(move |rd| {
                    use crate::{
                        model::Pull,
                        store::{self, Rd},
                    };
                    let mut out = Vec::new();
                    for (key, n) in rd.prefix_raw(store::IX_OPEN_PULLS, &[], None, usize::MAX)? {
                        let parts = store::decode_key(&key, "sss")
                            .ok_or_else(|| crate::api::AppError::internal("bad open pull index"))?;
                        let repo = crate::domain::repo_by_id(rd, parts[0].s())?;
                        if scope.authorize(&repo.path).is_err() {
                            continue;
                        }
                        let number = String::from_utf8_lossy(&n)
                            .parse()
                            .map_err(crate::api::AppError::internal)?;
                        if let Some(pull) =
                            rd.get::<Pull>(store::PULLS, &Pull::key(&repo.id, number))?
                            && pull.state == "open"
                            && fold(&pull.author) == me
                            && !crate::domain::pulls::interactive(rd, &pull)?
                        {
                            out.push(format!("{}/{}/-/pulls/{}", base, repo.path, pull.number));
                        }
                    }
                    out.sort();
                    out.dedup();
                    Ok(out)
                })
                .await
                .map_err(|e| error(e, "authored_open"))
        })
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
    /// Revokes the token lent for this run's git transfers. A token this
    /// cannot revoke is revoked when the server next starts, so the run's
    /// outcome stands.
    fn aclose(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async {
            if let Some(issued) = self.git_token.lock().await.take()
                && let Err(e) = self
                    .data()
                    .owner()
                    .members_revoke_token(self.person_id().to_owned(), issued.id)
                    .await
                && e.status.as_u16() != 404
            {
                tracing::warn!(
                    provider = "branvil",
                    code = e.code,
                    "the git token was not revoked"
                );
            }
            Ok(())
        })
    }
}
impl CodeAgent {
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
                            .is_some_and(super::pr_readiness::failed_conclusion)
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
                let bytes = super::pr_readiness::effective_tail_bytes(tail, selected.len())?;
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
        let activity = m
            .pulls_activity(&repo, number(&key)?)
            .await
            .map_err(|e| error(e, "pulls_activity"))?;
        let current = m
            .pulls_get(&repo, number(&key)?)
            .await
            .map_err(|e| error(e, "pulls_get"))?;
        self.readiness_result(
            report,
            &value(&activity)?,
            value(&current)?,
            logs.then_some(failed),
        )
    }

    pub(super) fn readiness_result(
        &self,
        report: pulls::ChecksView,
        activity: &Value,
        current: Value,
        failed: Option<Vec<FailedLog>>,
    ) -> Result<Readiness, ServiceError> {
        let said = |items: Vec<&Value>, at: &str| {
            items
                .into_iter()
                .map(|c| super::pr_readiness::Said {
                    author: fold(text(c, "author")),
                    id: text(c, "id").into(),
                    at: text(c, at).into(),
                    notice: super::code_patrol::status(c).is_some(),
                })
                .collect()
        };
        let feedback = super::pr_readiness::feedback_of(
            &fold(text(&current, "author")),
            said(array(activity, "reviews").iter().collect(), "submitted_at"),
            said(array(activity, "comments").iter().collect(), "created_at"),
            said(
                array(activity, "threads")
                    .iter()
                    .flat_map(|t| array(t, "comments"))
                    .collect(),
                "created_at",
            ),
        );
        let checks = report
            .checks
            .iter()
            .map(|c| Check {
                required: report.required_names.contains(&c.name),
                name: c.name.clone(),
                status: c.status.clone(),
                conclusion: c.conclusion.clone(),
                details_url: c.details_url.clone().unwrap_or_default(),
                details: Some(c.details.clone().unwrap_or_default()),
            })
            .collect::<Vec<_>>();
        let mut result = Readiness {
            target: self.target(&current, WorkKind::PullRequest),
            head_sha: report.head_sha,
            checks,
            required_checks: report.required_checks,
            feedback,
            failed_logs: failed,
        };
        super::pr_readiness::fit_log_tails(&mut result)?;
        Ok(result)
    }
}
