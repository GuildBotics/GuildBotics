//! Authenticated in-process access to the same operations used by HTTP.
use crate::app::Shared;
use crate::auth::{self, Caller};
use crate::domain::tickets::{NewComment, NewReaction, ReactionAdded};
pub use crate::domain::{CommentView, Page, PageReq};
pub use crate::domain::{attachments, members, pulls, repos, tickets};
pub use crate::error::{AppError, AppResult};
use crate::git::{CommitSummary, FileChange};
pub use crate::model::{MemberKind, Repo, Workspace};

/// A clone shares the service's locks, transactions, events and CI queue.
#[derive(Clone)]
pub struct DataApi {
    pub(crate) app: Shared,
}

impl DataApi {
    pub(crate) fn new(app: Shared) -> Self {
        Self { app }
    }
    fn guard(&self) -> AppResult<tokio::sync::OwnedRwLockReadGuard<bool>> {
        let guard = self
            .app
            .api_requests
            .clone()
            .try_read_owned()
            .map_err(|_| AppError::unavailable("integrations.branvil.api.stopped"))?;
        if !*guard {
            return Err(AppError::unavailable("integrations.branvil.api.stopped"));
        }
        Ok(guard)
    }
    pub fn base_url(&self) -> &str {
        &self.app.base_url
    }
    /// Authentication is repeated on each operation, so revocation takes effect.
    pub async fn authenticate(&self, token: String) -> AppResult<MemberApi> {
        let _open = self.guard()?;
        auth::authenticate_token(&self.app, &token).await?;
        Ok(MemberApi {
            api: self.clone(),
            token,
        })
    }
}

#[derive(Clone)]
pub struct MemberApi {
    api: DataApi,
    token: String,
}
impl MemberApi {
    pub async fn identity(&self) -> AppResult<members::MemberView> {
        let _open = self.api.guard()?;
        Ok(members::member_view(&self.caller().await?.member))
    }
    pub async fn job_log(&self, repo: &str, id: &str, tail: u64) -> AppResult<serde_json::Value> {
        let _open = self.api.guard()?;
        crate::ci::api::data_job_log(&self.api.app, &self.caller().await?, repo, id, tail).await
    }
    pub async fn artifact(
        &self,
        repo: &str,
        sha: &str,
        name: &str,
        limit: usize,
    ) -> AppResult<Vec<u8>> {
        let _open = self.api.guard()?;
        use tokio::io::AsyncReadExt;
        let file = crate::ci::api::data_artifact_file(
            &self.api.app,
            &self.caller().await?,
            repo,
            sha,
            name,
        )
        .await?;
        let mut bytes = Vec::new();
        file.take(limit.saturating_add(1) as u64)
            .read_to_end(&mut bytes)
            .await
            .map_err(AppError::internal)?;
        if bytes.len() > limit {
            return Err(AppError::too_large(
                "integrations.branvil.api.artifact_too_large",
            ));
        }
        Ok(bytes)
    }
    async fn caller(&self) -> AppResult<Caller> {
        Ok(Caller {
            member: auth::authenticate_token(&self.api.app, &self.token).await?,
        })
    }
    pub fn page(
        &self,
        binding: String,
        limit: Option<usize>,
        cursor: Option<&str>,
    ) -> AppResult<PageReq> {
        PageReq::new(&self.api.app.cursor_key, binding, limit, cursor)
    }
    pub async fn tickets_create_ticket(
        &self,
        repo_path: &str,
        input: tickets::NewTicket,
    ) -> AppResult<tickets::TicketView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::create_ticket(&self.api.app, &caller, repo_path, input).await
    }
    pub async fn tickets_list_tickets(
        &self,
        repo_path: &str,
        q: tickets::TicketQuery,
        page: PageReq,
    ) -> AppResult<Page<tickets::TicketView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::list_tickets(&self.api.app, &caller, repo_path, q, page).await
    }
    pub async fn tickets_get_ticket(
        &self,
        repo_path: &str,
        key: &str,
    ) -> AppResult<tickets::TicketView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::get_ticket(&self.api.app, &caller, repo_path, key).await
    }
    pub async fn tickets_delete_ticket(&self, repo_path: &str, key: &str) -> AppResult<()> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::delete_ticket(&self.api.app, &caller, repo_path, key).await
    }
    pub async fn tickets_update_ticket(
        &self,
        repo_path: &str,
        key: &str,
        patch: tickets::TicketPatch,
    ) -> AppResult<tickets::TicketUpdated> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::update_ticket(&self.api.app, &caller, repo_path, key, patch).await
    }
    pub async fn tickets_set_assignee(
        &self,
        repo_path: &str,
        key: &str,
        name: Option<String>,
    ) -> AppResult<tickets::TicketView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::set_assignee(&self.api.app, &caller, repo_path, key, name).await
    }
    pub async fn tickets_put_on_board(
        &self,
        repo_path: &str,
        key: &str,
        p: tickets::Placement,
    ) -> AppResult<tickets::TicketView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::put_on_board(&self.api.app, &caller, repo_path, key, p).await
    }
    pub async fn tickets_list_comments(
        &self,
        repo_path: &str,
        key: &str,
        page: PageReq,
    ) -> AppResult<Page<CommentView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::list_comments(&self.api.app, &caller, repo_path, key, page).await
    }
    pub async fn tickets_add_comment(
        &self,
        repo_path: &str,
        key: &str,
        input: tickets::NewComment,
    ) -> AppResult<CommentView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::add_comment(&self.api.app, &caller, repo_path, key, input).await
    }
    pub async fn tickets_change_comment(
        &self,
        repo_path: &str,
        key: &str,
        id: &str,
        body: Option<String>,
    ) -> AppResult<Option<CommentView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::change_comment(&self.api.app, &caller, repo_path, key, id, body).await
    }
    pub async fn tickets_react(
        &self,
        repo_path: &str,
        key: &str,
        input: tickets::NewReaction,
        on: bool,
    ) -> AppResult<Option<tickets::ReactionAdded>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::react(&self.api.app, &caller, repo_path, key, input, on).await
    }
    pub async fn tickets_ticket_pulls(
        &self,
        repo_path: &str,
        key: &str,
        page: PageReq,
    ) -> AppResult<Page<tickets::LinkedPull>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::ticket_pulls(&self.api.app, &caller, repo_path, key, page).await
    }
    pub async fn tickets_mentioned(
        &self,
        workspace: &str,
        q: tickets::MentionQuery,
        page: PageReq,
    ) -> AppResult<Page<tickets::CardView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::mentioned(&self.api.app, &caller, workspace, q, page).await
    }
    pub async fn tickets_cards(
        &self,
        workspace: &str,
        q: tickets::CardQuery,
        page: PageReq,
    ) -> AppResult<Page<tickets::CardView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::cards(&self.api.app, &caller, workspace, q, page).await
    }
    pub async fn tickets_closed(
        &self,
        workspace: &str,
        q: tickets::ClosedQuery,
        page: PageReq,
    ) -> AppResult<Page<tickets::ClosedItem>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        tickets::closed(&self.api.app, &caller, workspace, q, page).await
    }
    pub async fn pulls_create(
        &self,
        repo_path: &str,
        input: pulls::NewPull,
    ) -> AppResult<pulls::PullCreated> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::create(&self.api.app, &caller, repo_path, input).await
    }
    pub async fn pulls_list(
        &self,
        repo_path: &str,
        q: pulls::PullQuery,
        page: PageReq,
    ) -> AppResult<Page<pulls::PullView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::list(&self.api.app, &caller, repo_path, q, page).await
    }
    pub async fn pulls_get(&self, repo_path: &str, number: u64) -> AppResult<pulls::PullView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::get(&self.api.app, &caller, repo_path, number).await
    }
    pub async fn pulls_activity(&self, repo_path: &str, number: u64) -> AppResult<pulls::Activity> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::activity(&self.api.app, &caller, repo_path, number).await
    }
    pub async fn pulls_patrol(
        &self,
        q: pulls::PatrolQuery,
        page: PageReq,
    ) -> AppResult<Page<pulls::PatrolEntry>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::patrol(&self.api.app, &caller, q, page).await
    }
    pub async fn pulls_update(
        &self,
        repo_path: &str,
        number: u64,
        patch: pulls::PullPatch,
    ) -> AppResult<pulls::PullView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::update(&self.api.app, &caller, repo_path, number, patch).await
    }
    pub async fn pulls_set_reviewer(
        &self,
        repo_path: &str,
        number: u64,
        name: &str,
        add: bool,
    ) -> AppResult<pulls::PullView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::set_reviewer(&self.api.app, &caller, repo_path, number, name, add).await
    }
    pub async fn pulls_set_open(
        &self,
        repo_path: &str,
        number: u64,
        open: bool,
    ) -> AppResult<pulls::PullView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::set_open(&self.api.app, &caller, repo_path, number, open).await
    }
    pub async fn pulls_list_comments(
        &self,
        repo_path: &str,
        number: u64,
        page: PageReq,
    ) -> AppResult<Page<CommentView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::list_comments(&self.api.app, &caller, repo_path, number, page).await
    }
    pub async fn pulls_add_comment(
        &self,
        repo_path: &str,
        number: u64,
        input: NewComment,
    ) -> AppResult<CommentView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::add_comment(&self.api.app, &caller, repo_path, number, input).await
    }
    pub async fn pulls_review(
        &self,
        repo_path: &str,
        number: u64,
        input: pulls::NewReview,
    ) -> AppResult<pulls::ReviewSubmitted> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::review(&self.api.app, &caller, repo_path, number, input).await
    }
    pub async fn pulls_list_reviews(
        &self,
        repo_path: &str,
        number: u64,
        page: PageReq,
    ) -> AppResult<Page<pulls::ReviewView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::list_reviews(&self.api.app, &caller, repo_path, number, page).await
    }
    pub async fn pulls_inline_comment(
        &self,
        repo_path: &str,
        number: u64,
        input: pulls::NewInline,
    ) -> AppResult<pulls::ThreadCommentView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::inline_comment(&self.api.app, &caller, repo_path, number, input).await
    }
    pub async fn pulls_list_threads(
        &self,
        repo_path: &str,
        number: u64,
        page: PageReq,
    ) -> AppResult<Page<pulls::ThreadView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::list_threads(&self.api.app, &caller, repo_path, number, page).await
    }
    pub async fn pulls_thread_comments(
        &self,
        repo_path: &str,
        number: u64,
        id: &str,
        page: PageReq,
    ) -> AppResult<Page<pulls::ThreadCommentView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::thread_comments(&self.api.app, &caller, repo_path, number, id, page).await
    }
    pub async fn pulls_reply(
        &self,
        repo_path: &str,
        number: u64,
        id: &str,
        input: pulls::NewReply,
    ) -> AppResult<pulls::ThreadCommentView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::reply(&self.api.app, &caller, repo_path, number, id, input).await
    }
    pub async fn pulls_resolve(
        &self,
        repo_path: &str,
        number: u64,
        id: &str,
        input: pulls::Resolution,
    ) -> AppResult<pulls::ThreadView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::resolve(&self.api.app, &caller, repo_path, number, id, input).await
    }
    pub async fn pulls_react(
        &self,
        repo_path: &str,
        number: u64,
        input: NewReaction,
        on: bool,
    ) -> AppResult<Option<ReactionAdded>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::react(&self.api.app, &caller, repo_path, number, input, on).await
    }
    pub async fn pulls_files(
        &self,
        repo_path: &str,
        number: u64,
        page: PageReq,
    ) -> AppResult<Page<FileChange>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::files(&self.api.app, &caller, repo_path, number, page).await
    }
    pub async fn pulls_commits(
        &self,
        repo_path: &str,
        number: u64,
        page: PageReq,
    ) -> AppResult<Page<CommitSummary>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::commits(&self.api.app, &caller, repo_path, number, page).await
    }
    pub async fn pulls_checks(&self, repo_path: &str, number: u64) -> AppResult<pulls::ChecksView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::checks(&self.api.app, &caller, repo_path, number).await
    }
    pub async fn pulls_merge(
        &self,
        repo_path: &str,
        number: u64,
        input: pulls::MergeInput,
    ) -> AppResult<pulls::PullView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        pulls::merge(&self.api.app, &caller, repo_path, number, input).await
    }
    pub async fn repos_visible(&self, path: &str) -> AppResult<Repo> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::visible(&self.api.app, &caller, path).await
    }
    pub async fn repos_get(&self, path: &str) -> AppResult<repos::RepoView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::get(&self.api.app, &caller, path).await
    }
    pub async fn repos_list(&self, page: PageReq) -> AppResult<Page<repos::RepoView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::list(&self.api.app, &caller, page).await
    }
    pub async fn repos_create(&self, input: repos::NewRepo) -> AppResult<repos::RepoView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::create(&self.api.app, &caller, input).await
    }
    pub async fn repos_update(
        &self,
        path: &str,
        patch: repos::RepoPatch,
    ) -> AppResult<repos::RepoView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::update(&self.api.app, &caller, path, patch).await
    }
    pub async fn repos_branch(&self, path: &str, branch: &str) -> AppResult<repos::BranchView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::branch(&self.api.app, &caller, path, branch).await
    }
    pub async fn repos_commit(&self, path: &str, sha: &str) -> AppResult<repos::CommitView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::commit(&self.api.app, &caller, path, sha).await
    }
    pub async fn repos_commit_checks(
        &self,
        path: &str,
        sha: &str,
    ) -> AppResult<repos::CommitChecksView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::commit_checks(&self.api.app, &caller, path, sha).await
    }
    pub async fn repos_compare(
        &self,
        path: &str,
        base: &str,
        head: &str,
    ) -> AppResult<repos::Compare> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::compare(&self.api.app, &caller, path, base, head).await
    }
    pub async fn repos_get_workspace(&self, name: &str) -> AppResult<repos::WorkspaceView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::get_workspace(&self.api.app, &caller, name).await
    }
    pub async fn repos_visible_workspace(&self, name: &str) -> AppResult<Workspace> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::visible_workspace(&self.api.app, &caller, name).await
    }
    pub async fn repos_list_workspaces(
        &self,
        page: PageReq,
    ) -> AppResult<Page<repos::WorkspaceView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::list_workspaces(&self.api.app, &caller, page).await
    }
    pub async fn repos_create_workspace(
        &self,
        input: repos::NewWorkspace,
    ) -> AppResult<repos::WorkspaceView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::create_workspace(&self.api.app, &caller, input).await
    }
    pub async fn members_set_avatar(
        &self,
        name: String,
        image: Option<Vec<u8>>,
    ) -> AppResult<members::MemberView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::set_avatar(&self.api.app, &caller, name, image).await
    }
    pub async fn members_get(&self, name: String) -> AppResult<members::MemberView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::get(&self.api.app, &caller, name).await
    }
    pub async fn members_list(&self, page: PageReq) -> AppResult<Page<members::MemberView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::list(&self.api.app, &caller, page).await
    }
    pub async fn members_create(
        &self,
        input: members::NewMember,
    ) -> AppResult<members::MemberView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::create(&self.api.app, &caller, input).await
    }
    pub async fn members_update(
        &self,
        name: String,
        patch: members::MemberPatch,
    ) -> AppResult<members::MemberView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::update(&self.api.app, &caller, name, patch).await
    }
    pub async fn members_set_reviewer(
        &self,
        name: String,
        reviewer: String,
        add: bool,
    ) -> AppResult<members::MemberView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::set_reviewer(&self.api.app, &caller, name, reviewer, add).await
    }
    pub async fn members_issue_token(
        &self,
        name: String,
        input: members::NewToken,
    ) -> AppResult<members::IssuedToken> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::issue_token(&self.api.app, &caller, name, input).await
    }
    pub async fn members_list_tokens(
        &self,
        name: String,
        page: PageReq,
    ) -> AppResult<Page<members::TokenView>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::list_tokens(&self.api.app, &caller, name, page).await
    }
    pub async fn members_revoke_token(&self, name: String, id: String) -> AppResult<()> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        members::revoke_token(&self.api.app, &caller, name, id).await
    }
    pub async fn attachments_upload(
        &self,
        repo_path: &str,
        name: Option<String>,
        bytes: Vec<u8>,
    ) -> AppResult<attachments::AttachmentView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        attachments::upload(&self.api.app, &caller, repo_path, name, bytes).await
    }
    pub async fn attachments_get(&self, repo_path: &str, id: &str) -> AppResult<(String, Vec<u8>)> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        attachments::get(&self.api.app, &caller, repo_path, id).await
    }
    pub async fn attachments_of_ticket(
        &self,
        repo_path: &str,
        key: &str,
        page: PageReq,
    ) -> AppResult<Page<attachments::TicketAttachment>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        attachments::of_ticket(&self.api.app, &caller, repo_path, key, page).await
    }
}

/// Owns the embedded service, including Smart HTTP for Git and hook callbacks.
/// Data operations use `data` directly without any HTTP request.
pub struct Running {
    pub data: DataApi,
    stop: Option<tokio::sync::oneshot::Sender<()>>,
    task: Option<tokio::task::JoinHandle<anyhow::Result<u8>>>,
}
impl Running {
    pub async fn start(data_dir: &std::path::Path) -> anyhow::Result<Self> {
        let config = crate::config::Config::load(None, Some(data_dir), None, None)?;
        let (stop, stopped) = tokio::sync::oneshot::channel();
        let (ready, started) = tokio::sync::oneshot::channel();
        let mut task = tokio::spawn(crate::cli::serve_with_api(
            config,
            async {
                let _ = stopped.await;
            },
            std::future::pending(),
            false,
            Some(ready),
        ));
        let data = tokio::select! {
            result = started => result.map_err(|_| anyhow::anyhow!("integrations.branvil.api.start_failed"))?,
            result = &mut task => { result??; anyhow::bail!("integrations.branvil.api.start_failed"); }
        };
        Ok(Self {
            data,
            stop: Some(stop),
            task: Some(task),
        })
    }
    pub async fn stop(mut self) -> anyhow::Result<u8> {
        self.stop.take();
        self.task.take().expect("running service task").await?
    }
}
impl Drop for Running {
    fn drop(&mut self) {
        self.stop.take();
    }
}

/// Initialize an empty data directory. This is an administrative operation.
pub fn initialize(data_dir: &std::path::Path, owner: &str) -> anyhow::Result<()> {
    crate::cli::init(None, Some(data_dir), owner, None, "127.0.0.1:0", None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn input<T: serde::de::DeserializeOwned>(v: serde_json::Value) -> T {
        serde_json::from_value(v).unwrap()
    }
    #[tokio::test]
    async fn authenticated_data_operations_share_permissions_and_revocation() {
        let temp = tempfile::tempdir().unwrap();
        initialize(temp.path(), "owner1").unwrap();
        let token = std::fs::read_to_string(temp.path().join("secrets/owner.token")).unwrap();
        let running = Running::start(temp.path()).await.unwrap();
        let api = &running.data;
        assert!(api.authenticate("invalid".into()).await.is_err());
        let owner = api.authenticate(token).await.unwrap();
        owner
            .repos_create_workspace(input(json!({"name":"test"})))
            .await
            .unwrap();
        owner
            .repos_create(input(json!({"path":"test/repo","labels":["bug"]})))
            .await
            .unwrap();
        owner
            .members_create(input(json!({"name":"agent1","kind":"agent"})))
            .await
            .unwrap();
        let issued = owner
            .members_issue_token("agent1".into(), input(json!({"label":"contract"})))
            .await
            .unwrap();
        let agent = api.authenticate(issued.token).await.unwrap();
        assert!(
            agent
                .repos_create_workspace(input(json!({"name":"forbidden"})))
                .await
                .is_err()
        );
        let ticket = agent
            .tickets_create_ticket("test/repo", input(json!({"title":"Test","labels":["bug"]})))
            .await
            .unwrap();
        assert_eq!(ticket.author, "agent1");
        assert_eq!(
            owner
                .tickets_get_ticket("test/repo", &ticket.key)
                .await
                .unwrap()
                .title,
            "Test"
        );
        let page = agent
            .page("tickets:test/repo".into(), Some(1), None)
            .unwrap();
        assert_eq!(
            agent
                .tickets_list_tickets("test/repo", Default::default(), page)
                .await
                .unwrap()
                .items
                .len(),
            1
        );
        owner
            .members_revoke_token("agent1".into(), issued.id)
            .await
            .unwrap();
        assert_eq!(
            agent
                .tickets_get_ticket("test/repo", &ticket.key)
                .await
                .unwrap_err()
                .code,
            "unauthorized"
        );
        drop(agent);
        drop(owner);
        let stopped = api.clone();
        running.stop().await.unwrap();
        assert_eq!(
            stopped
                .authenticate("invalid".into())
                .await
                .err()
                .unwrap()
                .code,
            "unavailable"
        );
    }
}
