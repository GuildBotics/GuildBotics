use super::*;
pub(super) struct Driver<'a> {
    pub owner: &'a ::branvil::api::MemberApi,
    pub code: &'a dyn CodeHostingService,
    pub git: &'a Git,
    pub base: &'a str,
    pub runner: &'a str,
    pub sequence: std::sync::atomic::AtomicUsize,
}
impl contracts::TicketContractDriver for Driver<'_> {
    fn human_comment<'a>(&'a self, url: &'a str, body: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let key = url.rsplit('/').next().unwrap();
            self.owner
                .tickets_add_comment(REPO, key, input(json!({"body":body})))
                .await
                .map_err(|_| ServiceError::Failed)?;
            Ok(())
        })
    }
    fn assign<'a>(&'a self, url: &'a str, actor: &'a ActorName) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let key = url.rsplit('/').next().unwrap();
            self.owner
                .tickets_set_assignee(REPO, key, Some(actor.0.clone()))
                .await
                .map_err(|_| ServiceError::Failed)?;
            self.owner
                .tickets_put_on_board(REPO, key, input(json!({"lane":"ready"})))
                .await
                .map_err(|_| ServiceError::Failed)?;
            Ok(())
        })
    }
    fn merge_reference<'a>(&'a self, url: &'a str) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let main = self
                .owner
                .repos_branch(REPO, "main")
                .await
                .map_err(|_| ServiceError::Failed)?
                .sha;
            let branch = format!(
                "contract-merge-{}",
                self.sequence
                    .fetch_add(1, std::sync::atomic::Ordering::SeqCst)
            );
            let sha = self.git.commit(&main, &format!("{branch}.txt"), "merge\n");
            let credential = self.code.git_credential().await?.unwrap();
            self.git.push(
                &self.code.clone_url(REPO).await?,
                &sha,
                &branch,
                &credential.username,
                &credential.password,
            );
            let pr = self
                .code
                .create_pull_request(CreatePullRequest {
                    repository: REPO.into(),
                    head: branch,
                    base: "main".into(),
                    title: "Reference".into(),
                    body: String::new(),
                    draft: false,
                    tickets: vec![TicketLink {
                        url: url.into(),
                        closes: false,
                    }],
                })
                .await?;
            finish(self.base, self.runner, &sha, "success").await;
            let n = pr.target.url.rsplit('/').next().unwrap().parse().unwrap();
            self.owner
                .pulls_review(REPO, n, input(json!({"verdict":"approve","commit":sha})))
                .await
                .map_err(|_| ServiceError::Failed)?;
            merge_checked(self.owner, REPO, n, &sha, &main, self.base, self.runner).await;
            Ok(())
        })
    }
}
