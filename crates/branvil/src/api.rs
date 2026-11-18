//! Authenticated in-process access to the same operations used by HTTP.
use crate::app::Shared;
use crate::auth::Caller;
use crate::domain::tickets::{NewComment, NewReaction, ReactionAdded};
pub use crate::domain::{CommentView, Page};
pub use crate::domain::{attachments, members, pulls, repos, tickets};
pub use crate::error::{AppError, AppResult};
use crate::git::{CommitSummary, FileChange};
use crate::model::Member;
pub use crate::model::{MemberKind, Repo, Workspace};
use crate::store::Rd;
pub use crate::upstream::host::{AlertState, DependencyAlert};

/// Paging input verified against the actual operation, caller and filters at dispatch.
#[derive(Clone, Debug)]
pub struct PageReq {
    binding: String,
    limit: Option<usize>,
    cursor: Option<String>,
}

/// A clone shares the service's locks, transactions, events and CI queue.
#[derive(Clone)]
pub struct DataApi {
    pub(crate) app: Shared,
}

pub type HubStatus = std::sync::Arc<
    dyn Fn() -> std::pin::Pin<Box<dyn std::future::Future<Output = serde_json::Value> + Send>>
        + Send
        + Sync,
>;

impl DataApi {
    /// Supply device-local maintenance information from the embedding host.
    pub fn attach_hub_status(&self, status: HubStatus) {
        let _ = self.app.hub_status.set(status);
    }
    /// Status reported by the embedding host, with this service's live CI count.
    pub async fn hub_status(&self) -> serde_json::Value {
        let Some(status) = self.app.hub_status.get() else {
            return serde_json::Value::Null;
        };
        let mut value = status().await;
        if value["operation"].is_object() {
            value["operation"]["waiting"]["ci_jobs"] =
                self.app.ci.diagnostics().await["queue"]["in_progress"].clone();
        }
        value
    }
    pub fn hub_status_changed(&self) {
        self.app.events.publish("hub_status_changed", "hub", "");
    }
    pub async fn running_ci_jobs(&self) -> Option<u64> {
        self.app.ci.diagnostics().await["queue"]["in_progress"].as_u64()
    }
    pub(crate) fn new(app: Shared) -> Self {
        Self { app }
    }
    pub(crate) fn guard(&self) -> AppResult<tokio::sync::OwnedRwLockReadGuard<bool>> {
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
    /// Whether the embedded service accepts work.
    pub fn health(&self) -> AppResult<()> {
        let _open = self.guard()?;
        Ok(())
    }
    pub fn base_url(&self) -> &str {
        &self.app.base_url
    }
    /// Act as the owner: the embedding host shares this process, so it is
    /// trusted without a token.
    pub fn owner(&self) -> MemberApi {
        MemberApi {
            api: self.clone(),
            name: None,
        }
    }
    /// The owner's member name.
    pub fn owner_name(&self) -> AppResult<String> {
        let _open = self.guard()?;
        self.app
            .store
            .read_sync(|r| crate::auth::owner(r).map(|m| m.name))
    }
    /// Revoke every token labelled `label`: the embedding host's tokens that
    /// a run it no longer has did not return.
    pub async fn revoke_labelled(&self, label: &str) -> AppResult<usize> {
        let _open = self.guard()?;
        let label = label.to_owned();
        self.app
            .store
            .write(move |w| {
                let mut revoked = 0;
                for token in w.prefix_all::<crate::model::Token>(crate::store::TOKENS, &[])? {
                    if token.label == label {
                        crate::auth::revoke(w, &token)?;
                        revoked += 1;
                    }
                }
                Ok(revoked)
            })
            .await
    }
    /// Create, as agents, the members of `members` (name, display name)
    /// that do not exist yet. A name no member may have is left out, and a
    /// display name a member may not have gives way to the name, so one entry
    /// never keeps the others from being created.
    pub async fn ensure_agents(&self, members: Vec<(String, String)>) -> AppResult<()> {
        let _open = self.guard()?;
        let wanted: Vec<_> = members
            .into_iter()
            .filter(|(name, _)| members::check_name(name).is_ok())
            .collect();
        let missing = self
            .app
            .store
            .read(move |r| {
                let mut missing = Vec::new();
                for (name, display_name) in wanted {
                    if r.get::<Member>(crate::store::MEMBERS, &Member::key(&name))?
                        .is_none()
                    {
                        missing.push((name, display_name));
                    }
                }
                Ok(missing)
            })
            .await?;
        if missing.is_empty() {
            return Ok(());
        }
        // The write rereads, so a concurrent creation is kept rather than refused.
        self.app
            .store
            .write(move |w| {
                for (name, display_name) in missing {
                    if w.get::<Member>(crate::store::MEMBERS, &Member::key(&name))?
                        .is_none()
                    {
                        members::insert_member(
                            w,
                            &members::NewMember {
                                name,
                                display_name: crate::domain::check_text(
                                    "display_name",
                                    &display_name,
                                    256,
                                )
                                .is_ok()
                                .then_some(display_name),
                                kind: MemberKind::Agent,
                            },
                        )?;
                    }
                }
                Ok(())
            })
            .await
    }
    /// Act as the agent `name`, which is created with `display_name` when
    /// missing. Each operation reads the member again, so it acts with the
    /// member's current permissions.
    pub async fn agent(&self, name: &str, display_name: &str) -> AppResult<MemberApi> {
        members::check_name(name)?;
        self.ensure_agents(vec![(name.into(), display_name.into())])
            .await?;
        let key = name.to_owned();
        let member = self
            .app
            .store
            .read(move |r| crate::domain::member_by_name(r, &key))
            .await?;
        if member.kind != MemberKind::Agent {
            return Err(AppError::invalid(format!("Member {name} is not an agent.")));
        }
        Ok(MemberApi {
            api: self.clone(),
            name: Some(member.name),
        })
    }
}

#[derive(Clone)]
pub struct MemberApi {
    api: DataApi,
    /// The member acted as; `None` for the owner.
    name: Option<String>,
}
impl MemberApi {
    pub async fn upstream_dependency_alerts(
        &self,
        repo: &str,
        state: AlertState,
        page: PageReq,
    ) -> AppResult<Page<DependencyAlert>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        let page = self.bound_page(page, &caller, "upstream_dependency_alerts", (&repo, &state))?;
        crate::upstream::ops::dependency_alerts(&self.api.app, &caller, repo, state, page).await
    }
    pub async fn upstream_dependency_alert(
        &self,
        repo: &str,
        id: &str,
    ) -> AppResult<DependencyAlert> {
        let _open = self.api.guard()?;
        crate::upstream::ops::dependency_alert(&self.api.app, &self.caller().await?, repo, id).await
    }
    pub async fn identity(&self) -> AppResult<members::MemberView> {
        let _open = self.api.guard()?;
        Ok(members::member_view(&self.caller().await?.member))
    }
    /// The CI settings and the operation in progress (owner).
    pub async fn ci_settings(&self) -> AppResult<serde_json::Value> {
        let _open = self.api.guard()?;
        crate::ci::admin::data_settings(&self.api.app, &self.caller().await?).await
    }
    /// Start loading an archive on this machine into the automatic runner (owner).
    pub async fn ci_load_image(&self, path: &str) -> AppResult<serde_json::Value> {
        let _open = self.api.guard()?;
        crate::ci::admin::data_load(&self.api.app, &self.caller().await?, path).await
    }
    pub async fn job_retry(&self, repo: &str, id: &str) -> AppResult<crate::ci::api::AttemptView> {
        let _open = self.api.guard()?;
        crate::ci::api::data_job_retry(&self.api.app, &self.caller().await?, repo, id).await
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
    pub(crate) async fn caller(&self) -> AppResult<Caller> {
        let name = self.name.clone();
        let member = self
            .api
            .app
            .store
            .read(move |r| match name {
                Some(name) => crate::domain::member_by_name(r, &name),
                None => crate::auth::owner(r),
            })
            .await?;
        Ok(Caller { member })
    }
    pub fn page(
        &self,
        binding: String,
        limit: Option<usize>,
        cursor: Option<&str>,
    ) -> AppResult<PageReq> {
        if !(1..=100).contains(&limit.unwrap_or(30)) {
            return Err(AppError::invalid("limit must be between 1 and 100."));
        }
        Ok(PageReq {
            binding,
            limit,
            cursor: cursor.map(str::to_owned),
        })
    }
    fn bound_page(
        &self,
        page: PageReq,
        caller: &Caller,
        operation: &str,
        conditions: impl std::fmt::Debug,
    ) -> AppResult<crate::domain::PageReq> {
        crate::domain::PageReq::new(
            &self.api.app.cursor_key,
            format!(
                "{}\n{}\n{operation}\n{conditions:?}",
                page.binding,
                crate::util::fold(caller.name())
            ),
            page.limit,
            page.cursor.as_deref(),
        )
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
        let page = self.bound_page(page, &caller, "tickets_list_tickets", (&repo_path, &q))?;
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
        let page = self.bound_page(page, &caller, "tickets_list_comments", (&repo_path, &key))?;
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
        let page = self.bound_page(page, &caller, "tickets_ticket_pulls", (&repo_path, &key))?;
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
        let page = self.bound_page(page, &caller, "tickets_mentioned", (&workspace, &q))?;
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
        let page = self.bound_page(page, &caller, "tickets_cards", (&workspace, &q))?;
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
        let page = self.bound_page(page, &caller, "tickets_closed", (&workspace, &q))?;
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
        let page = self.bound_page(page, &caller, "pulls_list", (&repo_path, &q))?;
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
        let page = self.bound_page(page, &caller, "pulls_patrol", (&q,))?;
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
        let page = self.bound_page(page, &caller, "pulls_list_comments", (&repo_path, &number))?;
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
        let page = self.bound_page(page, &caller, "pulls_list_reviews", (&repo_path, &number))?;
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
        let page = self.bound_page(page, &caller, "pulls_list_threads", (&repo_path, &number))?;
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
        let page = self.bound_page(
            page,
            &caller,
            "pulls_thread_comments",
            (&repo_path, &number, &id),
        )?;
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
        let page = self.bound_page(page, &caller, "pulls_files", (&repo_path, &number))?;
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
        let page = self.bound_page(page, &caller, "pulls_commits", (&repo_path, &number))?;
        pulls::commits(&self.api.app, &caller, repo_path, number, page).await
    }
    pub async fn pulls_yield_checks(
        &self,
        repo_path: &str,
        number: u64,
    ) -> AppResult<crate::ci::api::YieldChecksView> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        crate::ci::api::yield_pull_checks(&self.api.app, &caller, repo_path, number).await
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
        let page = self.bound_page(page, &caller, "repos_list", ())?;
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
    pub async fn repos_verification(&self, path: &str) -> AppResult<crate::ci::verify::MainState> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        repos::verification(&self.api.app, &caller, path).await
    }
    pub async fn repos_verification_commits(
        &self,
        path: &str,
        page: PageReq,
    ) -> AppResult<Page<repos::VerificationCommit>> {
        let _open = self.api.guard()?;
        let caller = self.caller().await?;
        let page = self.bound_page(page, &caller, "repos_verification_commits", path)?;
        repos::verification_commits(&self.api.app, &caller, path, page).await
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
        let page = self.bound_page(page, &caller, "repos_list_workspaces", ())?;
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
        let page = self.bound_page(page, &caller, "members_list", ())?;
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
        let page = self.bound_page(page, &caller, "members_list_tokens", (&name,))?;
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
        let page = self.bound_page(page, &caller, "attachments_of_ticket", (&repo_path, &key))?;
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
    pub async fn start(
        data_dir: &std::path::Path,
        vault: std::sync::Arc<dyn crate::vault::Vault>,
    ) -> anyhow::Result<Self> {
        let config = crate::config::Config::load(None, Some(data_dir), None, None)?;
        let (stop, stopped) = tokio::sync::oneshot::channel();
        let (ready, started) = tokio::sync::oneshot::channel();
        let mut task = tokio::spawn(crate::cli::serve_with_api(
            config,
            vault,
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

/// Initialize an empty data directory and return the owner's token. This is an
/// administrative operation.
pub fn initialize(data_dir: &std::path::Path, owner: &str) -> anyhow::Result<String> {
    crate::cli::init(None, Some(data_dir), owner, None, "127.0.0.1:0", None)?;
    Ok(std::fs::read_to_string(
        crate::config::Layout::new(data_dir).owner_token(),
    )?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn input<T: serde::de::DeserializeOwned>(v: serde_json::Value) -> T {
        serde_json::from_value(v).unwrap()
    }
    #[tokio::test]
    async fn in_process_members_act_with_their_permissions() {
        let temp = tempfile::tempdir().unwrap();
        let token = initialize(temp.path(), "owner1").unwrap();
        let running = Running::start(
            temp.path(),
            std::sync::Arc::new(crate::vault::Memory::default()),
        )
        .await
        .unwrap();
        let api = &running.data;
        assert_eq!(api.owner_name().unwrap(), "owner1");
        let owner = api.owner();
        assert_eq!(owner.identity().await.unwrap().name, "owner1");
        owner
            .repos_create_workspace(input(json!({"name":"test"})))
            .await
            .unwrap();
        owner
            .repos_create(input(json!({"path":"test/repo","labels":["bug"]})))
            .await
            .unwrap();
        // The owner and a runner are not agents, so they cannot be acted as one.
        owner
            .members_create(input(json!({"name":"runner1","kind":"runner"})))
            .await
            .unwrap();
        for name in ["owner1", "runner1"] {
            assert_eq!(api.agent(name, "Any").await.err().unwrap().code, "invalid");
        }
        // A missing agent is created with the display name, then reused as is.
        let agent = api.agent("agent1", "Agent One").await.unwrap();
        let again = api.agent("agent1", "Renamed").await.unwrap();
        for acting in [&agent, &again] {
            let me = acting.identity().await.unwrap();
            assert_eq!(
                (me.name.as_str(), me.display_name.as_str(), me.kind),
                ("agent1", "Agent One", MemberKind::Agent)
            );
        }
        assert!(
            agent
                .repos_create_workspace(input(json!({"name":"forbidden"})))
                .await
                .is_err()
        );
        assert_eq!(agent.ci_settings().await.err().unwrap().code, "forbidden");
        assert!(owner.ci_settings().await.is_ok());
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
        // An arbitrary identical client binding cannot defeat operation/caller/filter binding.
        agent
            .tickets_create_ticket("test/repo", input(json!({"title":"Second"})))
            .await
            .unwrap();
        let first = agent
            .tickets_list_tickets(
                "test/repo",
                Default::default(),
                agent.page("same".into(), Some(1), None).unwrap(),
            )
            .await
            .unwrap();
        let cursor = first.next_cursor.unwrap();
        let next = || agent.page("same".into(), Some(2), Some(&cursor)).unwrap();
        assert_eq!(
            agent
                .tickets_list_tickets("test/repo", Default::default(), next())
                .await
                .unwrap()
                .items
                .len(),
            1
        );
        macro_rules! rejects {
            ($call:expr) => {
                assert_eq!($call.await.unwrap_err().code, "invalid_cursor");
            };
        }
        rejects!(owner.tickets_list_tickets(
            "test/repo",
            Default::default(),
            owner.page("same".into(), Some(2), Some(&cursor)).unwrap()
        ));
        rejects!(agent.tickets_list_tickets("test/other", Default::default(), next()));
        for q in [
            tickets::TicketQuery {
                q: Some("Second".into()),
                ..Default::default()
            },
            tickets::TicketQuery {
                state: Some("all".into()),
                ..Default::default()
            },
            tickets::TicketQuery {
                lane: Some("ready".into()),
                ..Default::default()
            },
            tickets::TicketQuery {
                assignee: Some("me".into()),
                ..Default::default()
            },
            tickets::TicketQuery {
                label: Some("bug".into()),
                ..Default::default()
            },
        ] {
            rejects!(agent.tickets_list_tickets("test/repo", q, next()));
        }
        rejects!(agent.tickets_list_comments("test/repo", &ticket.key, next()));
        rejects!(agent.tickets_ticket_pulls("test/repo", &ticket.key, next()));
        rejects!(agent.tickets_mentioned("test", input(json!({"mentions":"me"})), next()));
        rejects!(agent.tickets_cards("test", Default::default(), next()));
        rejects!(agent.tickets_closed("test", Default::default(), next()));
        rejects!(agent.pulls_list("test/repo", Default::default(), next()));
        rejects!(agent.pulls_patrol(Default::default(), next()));
        rejects!(agent.pulls_list_comments("test/repo", 1, next()));
        rejects!(agent.pulls_list_reviews("test/repo", 1, next()));
        rejects!(agent.pulls_list_threads("test/repo", 1, next()));
        rejects!(agent.pulls_thread_comments("test/repo", 1, "thread", next()));
        rejects!(agent.pulls_files("test/repo", 1, next()));
        rejects!(agent.pulls_commits("test/repo", 1, next()));
        rejects!(agent.repos_list(next()));
        rejects!(agent.repos_list_workspaces(next()));
        rejects!(agent.members_list(next()));
        rejects!(agent.members_list_tokens("agent1".into(), next()));
        rejects!(agent.attachments_of_ticket("test/repo", &ticket.key, next()));
        rejects!(agent.upstream_dependency_alerts("test/repo", AlertState::Open, next()));
        // App::finish is the domain boundary shared by HTTP and in-process writes.
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = tokio::sync::oneshot::channel();
        let (written_tx, written_rx) = tokio::sync::oneshot::channel();
        let app = api.app.clone();
        let caller = owner.caller().await.unwrap();
        let waiting = tokio::spawn(async move {
            app.finish(move |app| async move {
                started_tx.send(()).unwrap();
                release_rx.await.unwrap();
                let ticket = tickets::create_ticket(
                    &app,
                    &caller,
                    "test/repo",
                    input(json!({"title":"Survives cancellation"})),
                )
                .await
                .unwrap();
                written_tx.send(ticket.key).unwrap();
            })
            .await;
        });
        started_rx.await.unwrap();
        waiting.abort();
        assert!(waiting.await.unwrap_err().is_cancelled());
        release_tx.send(()).unwrap();
        let key = tokio::time::timeout(std::time::Duration::from_secs(5), written_rx)
            .await
            .unwrap()
            .unwrap();
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .unwrap();
        let url = format!("{}/api/v1/repos/test%2Frepo/tickets/{key}", api.base_url());
        let observed: serde_json::Value = client
            .get(&url)
            .bearer_auth(&token)
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(observed["title"], "Survives cancellation");
        let posted: serde_json::Value = client
            .post(format!("{url}/comments"))
            .bearer_auth(&token)
            .json(&json!({"body":"From HTTP"}))
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap()
            .json()
            .await
            .unwrap();
        let comments = owner
            .tickets_list_comments(
                "test/repo",
                &key,
                owner.page("comments".into(), Some(10), None).unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(comments.items.len(), 1);
        assert_eq!(comments.items[0].id, posted["id"].as_str().unwrap());
        assert_eq!(comments.items[0].body, "From HTTP");
        // One unacceptable entry does not keep the others from being created.
        api.ensure_agents(vec![
            ("roster1".into(), "あ".repeat(100)),
            ("branvil".into(), "Reserved".into()),
            ("roster2".into(), "Roster Two".into()),
        ])
        .await
        .unwrap();
        let long = owner.members_get("roster1".into()).await.unwrap();
        assert_eq!(long.display_name, "roster1");
        let named = owner.members_get("roster2".into()).await.unwrap();
        assert_eq!(named.display_name, "Roster Two");
        assert_eq!(
            api.agent("branvil", "Reserved").await.err().unwrap().code,
            "invalid"
        );
        // Only the tokens of the named label are revoked.
        for (name, label) in [("agent1", "lent"), ("agent1", "kept"), ("owner1", "lent")] {
            owner
                .members_issue_token(name.into(), input(json!({ "label": label })))
                .await
                .unwrap();
        }
        assert_eq!(api.revoke_labelled("lent").await.unwrap(), 2);
        assert_eq!(api.revoke_labelled("lent").await.unwrap(), 0);
        let labels = |name: &'static str| {
            let owner = owner.clone();
            async move {
                let page = owner
                    .page(format!("labels:{name}"), Some(100), None)
                    .unwrap();
                owner
                    .members_list_tokens(name.into(), page)
                    .await
                    .unwrap()
                    .items
                    .into_iter()
                    .map(|t| t.label)
                    .collect::<Vec<_>>()
            }
        };
        assert!(labels("agent1").await.contains(&"kept".to_owned()));
        assert!(!labels("agent1").await.contains(&"lent".to_owned()));
        assert!(!labels("owner1").await.contains(&"lent".to_owned()));
        drop((agent, again, owner));
        let stopped = api.clone();
        running.stop().await.unwrap();
        assert_eq!(
            stopped
                .agent("agent1", "Agent One")
                .await
                .err()
                .unwrap()
                .code,
            "unavailable"
        );
        assert_eq!(stopped.owner_name().err().unwrap().code, "unavailable");
    }
}
