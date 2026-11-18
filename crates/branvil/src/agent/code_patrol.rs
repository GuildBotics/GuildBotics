use super::pr_patrol::*;
use super::*;
use crate::api::pulls;
use chrono::Utc;
use std::collections::BTreeSet;
pub(super) fn array<'a>(v: &'a Value, k: &str) -> &'a [Value] {
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
pub(super) fn status(v: &Value) -> Option<WorkflowStatus> {
    WorkflowStatus::from_payload(&v["meta"])
}
impl CodeAgent {
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
            repair: p["repair"] == true,
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
        let (r, k) = self.item(text(p, "url"), "pulls")?;
        let _open = self.data().guard().map_err(|e| error(e, "interactive"))?;
        let caller = self
            .member()
            .await?
            .caller()
            .await
            .map_err(|e| error(e, "caller"))?;
        let (_, pull) = crate::domain::pulls::visible(&self.data().app, &caller, &r, number(&k)?)
            .await
            .map_err(|e| error(e, "pull"))?;
        self.data()
            .app
            .store
            .read(move |rd| crate::domain::pulls::interactive(rd, &pull))
            .await
            .map_err(|e| error(e, "interactive"))
    }
    pub(super) async fn pull_work(
        &self,
        p: &Value,
        a: &Value,
    ) -> Result<Option<Task>, ServiceError> {
        if text(p, "head_sha").is_empty()
            || self.write_scope().authorize(text(p, "repository")).is_err()
        {
            return Ok(None);
        }
        let me = fold(self.person_id());
        let task = super::pr_patrol::patrol(
            self,
            &self.snapshot(p, a),
            &me,
            "branvil",
            self.person_id(),
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
        if self.write_scope().authorize(&r).is_err() {
            return Ok(false);
        }
        let p = self.pull(url).await?;
        let me = fold(self.person_id());
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
            super::pr_patrol::pull_request_work(&self.snapshot(&p, &a), &me, Utc::now()),
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
