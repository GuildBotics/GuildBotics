//! Workspaces and their repositories: creation of managed bare repositories,
//! settings, branches, and commit comparison.

use serde::{Deserialize, Serialize};

use crate::app::App;
use crate::auth::Caller;
use crate::domain::{Page, PageReq, repo_by_path, workspace_by_name};
use crate::error::{AppError, AppResult};
use crate::model::{self, LANES, Repo, Workspace};
use crate::store::{self, Rd, Wtx};
use crate::util;

#[derive(Serialize, Debug, Clone)]
pub struct RepoView {
    pub path: String,
    pub default_branch: String,
    pub clone_url: String,
    pub url: String,
    pub protected_branches: Vec<String>,
    pub required_approvals: u64,
    pub auto_land: bool,
    pub hold_while_failing: bool,
    pub ci_network: bool,
    pub responder: crate::repair::Responder,
    pub labels: Vec<String>,
    /// The upstream it follows and publishes to, with what the last poll found.
    pub upstream: Option<crate::upstream::UpstreamView>,
    pub revision: u64,
}

pub async fn repo_view(app: &App, r: &Repo) -> AppResult<RepoView> {
    let repo = r.clone();
    let responder = app
        .store
        .read(move |rd| crate::repair::responder(rd, &repo))
        .await?;
    Ok(RepoView {
        path: r.path.clone(),
        default_branch: r.default_branch.clone(),
        clone_url: app.clone_url(&r.path),
        url: app.repo_url(&r.path),
        protected_branches: r.protected(),
        required_approvals: r.required_approvals,
        auto_land: r.auto_land,
        hold_while_failing: r.hold_while_failing,
        ci_network: r.ci_network,
        responder,
        labels: r.labels.clone(),
        upstream: crate::upstream::upstream_view(app, r),
        revision: r.revision,
    })
}

fn check_labels(labels: &[String]) -> AppResult<()> {
    for (i, l) in labels.iter().enumerate() {
        if l.trim().is_empty() || l.len() > 64 || l.chars().any(char::is_control) {
            return Err(AppError::invalid(format!("'{l}' is not a valid label.")));
        }
        if labels[..i].contains(l) {
            return Err(AppError::invalid(format!("Label '{l}' is listed twice.")));
        }
    }
    Ok(())
}

/// Loads a repository for the owner or an agent.
pub async fn visible(app: &App, caller: &Caller, path: &str) -> AppResult<Repo> {
    caller.require_agent_or_owner()?;
    let p = path.to_string();
    app.store.read(move |r| repo_by_path(r, &p)).await
}

pub async fn get(app: &App, caller: &Caller, path: &str) -> AppResult<RepoView> {
    repo_view(app, &visible(app, caller, path).await?).await
}

pub async fn list(app: &App, caller: &Caller, page: PageReq) -> AppResult<Page<RepoView>> {
    caller.require_agent_or_owner()?;
    let repos: Vec<Repo> = app.store.read(|r| r.prefix_all(store::REPOS, &[])).await?;
    let mut all = Vec::new();
    for r in &repos {
        all.push((util::fold(&r.path).into_bytes(), repo_view(app, r).await?));
    }
    all.sort_by(|a, b| a.0.cmp(&b.0));
    let (items, next_cursor) = page.slice(all);
    Ok(Page { items, next_cursor })
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewRepo {
    pub path: String,
    pub default_branch: Option<String>,
    #[serde(default)]
    pub labels: Vec<String>,
}

pub async fn create(app: &App, caller: &Caller, input: NewRepo) -> AppResult<RepoView> {
    let event_actor = Some(caller.member.kind);
    caller.require_owner()?;
    let NewRepo {
        path,
        default_branch,
        labels,
    } = input;
    util::validate_path(&path)?;
    check_labels(&labels)?;
    let default_branch = default_branch.unwrap_or_else(|| "main".to_string());
    if !crate::git::valid_branch(&default_branch) {
        return Err(AppError::invalid(format!(
            "{default_branch} is not a valid branch name."
        )));
    }
    // Repository creation is serialized so the disk and the path index agree.
    let repo = app
        .locked("<create>", move |app| async move {
            create_locked(&app, path, default_branch, labels).await
        })
        .await?;
    app.events.publish_as("repo", &repo.path, "", event_actor);
    repo_view(app, &repo).await
}

async fn create_locked(
    app: &App,
    requested: String,
    default_branch: String,
    labels: Vec<String>,
) -> AppResult<Repo> {
    let (ws, name) = requested.split_once('/').unwrap_or_default();
    let (ws, path) = (ws.to_string(), requested.clone());
    let (workspace, taken) = app
        .store
        .read(move |r| {
            Ok((
                workspace_by_name(r, &ws)?,
                super::path_id(r, "repo", &path)?,
            ))
        })
        .await?;
    if taken.is_some() {
        return Err(AppError::conflict(format!(
            "Repository {requested} already exists."
        )));
    }
    // The path spells the workspace as the workspace does.
    let path = format!("{}/{name}", workspace.name);
    let dir = app.git.repo_dir(&path);
    if dir.exists() {
        return Err(AppError::conflict(format!(
            "{} exists on disk without a repository record; move it away first.",
            dir.display()
        )));
    }
    app.git.init_bare(&dir, &default_branch).await?;
    let repo = Repo {
        id: util::new_id("repo"),
        path,
        workspace: workspace.id,
        default_branch,
        protected_branches: Vec::new(),
        required_approvals: 1,
        auto_land: false,
        hold_while_failing: false,
        ci_network: false,
        responder: None,
        labels,
        upstream: None,
        revision: 1,
        created_at: util::now(),
    };
    let record = repo.clone();
    let written = app
        .store
        .write(move |w| {
            if super::path_id(w, "repo", &record.path)?.is_some() {
                return Err(AppError::conflict(format!(
                    "Repository {} already exists.",
                    record.path
                )));
            }
            model::put_indexed(
                w,
                store::REPOS,
                &Repo::key(&record.id),
                None,
                &record,
                model::repo_index(&record),
            )
        })
        .await;
    if let Err(e) = written {
        let _ = std::fs::remove_dir_all(&dir);
        return Err(e);
    }
    Ok(repo)
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct RepoPatch {
    pub revision: u64,
    pub default_branch: Option<String>,
    pub required_approvals: Option<u64>,
    pub auto_land: Option<bool>,
    pub hold_while_failing: Option<bool>,
    pub protected_branches: Option<Vec<String>>,
    pub ci_network: Option<bool>,
    #[serde(default, deserialize_with = "crate::domain::double_option")]
    pub responder: Option<Option<String>>,
    pub labels: Option<Vec<String>>,
}

pub async fn update(
    app: &App,
    caller: &Caller,
    path: &str,
    patch: RepoPatch,
) -> AppResult<RepoView> {
    let event_actor = Some(caller.member.kind);
    caller.require_owner()?;
    let repo = visible(app, caller, path).await?;
    if let Some(b) = &patch.default_branch
        && !crate::git::valid_branch(b)
    {
        return Err(AppError::invalid(format!(
            "{b} is not a valid branch name."
        )));
    }
    for b in patch.protected_branches.iter().flatten() {
        if !crate::git::valid_branch(b) {
            return Err(AppError::invalid(format!(
                "{b} is not a valid branch name."
            )));
        }
    }
    if let Some(l) = &patch.labels {
        check_labels(l)?;
    }
    // HEAD follows the default branch: under the lock, to its end.
    let (id, old_branch) = (repo.id.clone(), repo.default_branch.clone());
    let updated = app
        .locked(&repo.id, move |app| async move {
            let updated = update_record(&app, id, patch).await?;
            if updated.default_branch != old_branch {
                let dir = app.git.repo_dir(&updated.path);
                app.git.set_head(&dir, &updated.default_branch).await?;
            }
            AppResult::Ok(updated)
        })
        .await?;
    app.events
        .publish_as("repo", &updated.path, "", event_actor);
    repo_view(app, &updated).await
}

async fn update_record(app: &App, id: String, patch: RepoPatch) -> AppResult<Repo> {
    app.store
        .write(move |w| {
            let mut r: Repo = super::repo_by_id(w, &id)?;
            if r.revision != patch.revision {
                return Err(AppError::conflict(format!(
                    "Repository {} is at revision {}, not {}.",
                    r.path, r.revision, patch.revision
                )));
            }
            if let Some(b) = patch.default_branch
                && b != r.default_branch
            {
                if r.upstream.is_some() {
                    return Err(AppError::conflict(format!(
                        "{} follows its upstream's default branch; remove the upstream to change it.",
                        r.path
                    )));
                }
                r.default_branch = b;
            }
            if let Some(name) = patch.responder {
                r.responder = name.map(|name| {
                    let member: model::Member = w.get(store::MEMBERS, &model::Member::key(&name))?
                        .ok_or_else(|| AppError::invalid("Responder must be an existing agent."))?;
                    if member.kind != model::MemberKind::Agent {
                        return Err(AppError::invalid("Responder must be an agent."));
                    }
                    Ok(member.name)
                }).transpose()?;
            }
            if let Some(n) = patch.required_approvals {
                r.required_approvals = n;
            }
            if let Some(a) = patch.hold_while_failing {
                r.hold_while_failing = a;
            }
            if let Some(a) = patch.auto_land {
                r.auto_land = a;
            }
            if let Some(p) = patch.protected_branches {
                r.protected_branches = p;
            }
            if let Some(n) = patch.ci_network {
                r.ci_network = n;
            }
            if let Some(l) = patch.labels {
                r.labels = l;
            }
            r.revision += 1;
            w.put(store::REPOS, &Repo::key(&r.id), &r)?;
            Ok(r)
        })
        .await
}

#[derive(Serialize, Debug)]
pub struct BranchView {
    pub name: String,
    pub sha: String,
}

pub async fn branch(app: &App, caller: &Caller, path: &str, branch: &str) -> AppResult<BranchView> {
    let repo = visible(app, caller, path).await?;
    let not_found =
        || AppError::not_found(format!("Branch {branch} was not found in {}.", repo.path));
    if !crate::git::valid_branch(branch) {
        return Err(not_found());
    }
    let tips = app.git.branch_tips(&app.git.repo_dir(&repo.path)).await?;
    let sha = tips.get(branch).cloned().ok_or_else(not_found)?;
    Ok(BranchView {
        name: branch.to_string(),
        sha,
    })
}

#[derive(Serialize, Debug)]
pub struct CommitView {
    pub sha: String,
    pub message: String,
    pub author: String,
    pub authored_at: String,
    pub parents: Vec<String>,
}

pub async fn commit(app: &App, caller: &Caller, path: &str, sha: &str) -> AppResult<CommitView> {
    commit_of(app, &visible(app, caller, path).await?, sha).await
}

async fn commit_of(app: &App, repo: &Repo, sha: &str) -> AppResult<CommitView> {
    let not_found = || AppError::not_found(format!("Commit {sha} was not found in {}.", repo.path));
    if !util::is_sha(sha) {
        return Err(not_found());
    }
    let (sha, author, date, parents, message) = app
        .git
        .commit_info(&app.git.repo_dir(&repo.path), sha)
        .await?
        .ok_or_else(not_found)?;
    Ok(CommitView {
        sha,
        message,
        author,
        authored_at: util::parse_time(&date).unwrap_or(date),
        parents,
    })
}

#[derive(Serialize, Debug)]
pub struct CommitChecksView {
    pub sha: String,
    #[serde(flatten)]
    pub checks: crate::ci::Checks,
}

/// The checks of a commit, as a pull request's head has them (`docs/api.md`,
/// "Checks").
pub async fn commit_checks(
    app: &App,
    caller: &Caller,
    path: &str,
    sha: &str,
) -> AppResult<CommitChecksView> {
    let repo = visible(app, caller, path).await?;
    let sha = commit_of(app, &repo, sha).await?.sha;
    let mut checks = app.ci.head_checks(&repo, &sha).await?;
    app.ci.estimate(&repo, &sha, &mut checks.checks).await;
    app.ci.order_checks(&mut checks.checks).await?;
    Ok(CommitChecksView { checks, sha })
}

// ---------------------------------------------------------------- workspaces

#[derive(Serialize, Debug)]
pub struct WorkspaceView {
    pub name: String,
    /// The lanes of its board.
    pub lanes: [&'static str; 4],
}

fn workspace_view(x: &Workspace) -> WorkspaceView {
    WorkspaceView {
        name: x.name.clone(),
        lanes: LANES,
    }
}

pub async fn get_workspace(app: &App, caller: &Caller, name: &str) -> AppResult<WorkspaceView> {
    Ok(workspace_view(&visible_workspace(app, caller, name).await?))
}

/// Loads a workspace for the owner or an agent.
pub async fn visible_workspace(app: &App, caller: &Caller, name: &str) -> AppResult<Workspace> {
    caller.require_agent_or_owner()?;
    let name = name.to_string();
    app.store.read(move |r| workspace_by_name(r, &name)).await
}

pub async fn list_workspaces(
    app: &App,
    caller: &Caller,
    page: PageReq,
) -> AppResult<Page<WorkspaceView>> {
    caller.require_agent_or_owner()?;
    let all: Vec<Workspace> = app
        .store
        .read(|r| r.prefix_all(store::WORKSPACES, &[]))
        .await?;
    let mut all: Vec<(Vec<u8>, WorkspaceView)> = all
        .iter()
        .map(|x| (util::fold(&x.name).into_bytes(), workspace_view(x)))
        .collect();
    all.sort_by(|a, b| a.0.cmp(&b.0));
    let (items, next_cursor) = page.slice(all);
    Ok(Page { items, next_cursor })
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NewWorkspace {
    pub name: String,
}

pub async fn create_workspace(
    app: &App,
    caller: &Caller,
    input: NewWorkspace,
) -> AppResult<WorkspaceView> {
    caller.require_owner()?;
    if !util::valid_name(&input.name) {
        return Err(AppError::invalid(format!(
            "'{}' is not a valid workspace name.",
            input.name
        )));
    }
    app.store
        .write(move |w| {
            if super::path_id(w, "workspace", &input.name)?.is_some() {
                return Err(AppError::conflict(format!(
                    "Workspace {} already exists.",
                    input.name
                )));
            }
            Ok(workspace_view(&insert_workspace(w, input.name, None)?))
        })
        .await
}

pub(crate) fn insert_workspace(
    w: &Wtx,
    name: String,
    execution_root: Option<std::path::PathBuf>,
) -> AppResult<Workspace> {
    if !util::valid_name(&name) {
        return Err(AppError::invalid(format!(
            "'{name}' is not a valid workspace name."
        )));
    }
    let x = Workspace {
        id: util::new_id("ws"),
        name,
        execution_root,
        created_at: util::now(),
    };
    model::put_indexed(
        w,
        store::WORKSPACES,
        &Workspace::key(&x.id),
        None,
        &x,
        model::workspace_index(&x),
    )?;
    Ok(x)
}

pub(crate) fn assign_execution_root(
    w: &Wtx,
    name: String,
    root: std::path::PathBuf,
) -> AppResult<Workspace> {
    let workspaces = w.prefix_all::<Workspace>(store::WORKSPACES, &[])?;
    if workspaces.iter().any(|ws| {
        util::fold(&ws.name) != util::fold(&name) && ws.execution_root.as_ref() == Some(&root)
    }) {
        return Err(AppError::conflict(
            "The execution settings already belong to another workspace.",
        ));
    }
    let Some(mut workspace) = workspaces
        .into_iter()
        .find(|ws| util::fold(&ws.name) == util::fold(&name))
    else {
        return insert_workspace(w, name, Some(root));
    };
    match &workspace.execution_root {
        Some(existing) if *existing != root => {
            return Err(AppError::conflict(format!(
                "Workspace {} already has execution settings.",
                workspace.name
            )));
        }
        Some(_) => return Ok(workspace),
        None => {}
    }
    workspace.execution_root = Some(root);
    w.put(
        store::WORKSPACES,
        &Workspace::key(&workspace.id),
        &workspace,
    )?;
    Ok(workspace)
}

pub async fn verification(
    app: &App,
    caller: &Caller,
    path: &str,
) -> AppResult<crate::ci::verify::MainState> {
    let repo = visible(app, caller, path).await?;
    let id = repo.id.clone();
    app.locked(&id, move |app| async move {
        crate::ci::verify::main_state(&app, &repo).await
    })
    .await
}

#[derive(Serialize, Debug)]
pub struct VerificationCommit {
    pub sha: String,
    pub title: String,
    pub author: String,
    pub authored_at: String,
    pub mark: Option<&'static str>,
}

pub async fn verification_commits(
    app: &App,
    caller: &Caller,
    path: &str,
    page: PageReq,
) -> AppResult<Page<VerificationCommit>> {
    let repo = visible(app, caller, path).await?;
    let id = repo.id.clone();
    app.locked(&id, move |app| async move {
        let state = crate::ci::verify::main_state(&app, &repo).await?;
        let Some(tip) = state.tip else {
            return Ok(Page {
                items: vec![],
                next_cursor: None,
            });
        };
        let dir = app.git.repo_dir(&repo.path);
        let refs = app.git.all_refs(&dir).await?;
        // SHA positions keep pagination stable if main advances between pages.
        let after = page
            .after
            .as_ref()
            .map(|v| String::from_utf8_lossy(v).into_owned());
        if after.as_ref().is_some_and(|s| !util::is_sha(s)) {
            return Err(AppError::invalid_cursor());
        }
        let start = after.as_ref().map_or(tip, |sha| format!("{sha}^"));
        let max_count = format!("--max-count={}", page.limit + 1);
        let args = [
            "log",
            &max_count,
            "--first-parent",
            "--format=%H%x00%s%x00%an%x00%aI",
            &start,
            "--",
        ];
        // A root used as a cursor has no parent.
        let text = if let Some(sha) = &after {
            if app
                .git
                .commit_info(&dir, sha)
                .await?
                .is_none_or(|c| c.3.is_empty())
            {
                String::new()
            } else {
                app.git.run_ok(Some(&dir), &args).await?
            }
        } else {
            app.git.run_ok(Some(&dir), &args).await?
        };
        let mut items = Vec::new();
        let mut more = false;
        for line in text.lines() {
            if items.len() == page.limit {
                more = true;
                break;
            }
            let fields: Vec<_> = line.splitn(4, '\0').collect();
            if fields.len() != 4 {
                return Err(AppError::internal("Invalid Git log record"));
            }
            let sha = fields[0];
            let mark = if refs
                .get(&format!("refs/branvil/verified/{sha}"))
                .is_some_and(|v| v == sha)
            {
                Some("verified")
            } else if state
                .current
                .as_ref()
                .is_some_and(|v| v.sha == sha && v.state == "running")
            {
                Some("verifying")
            } else if state.failing_since.as_deref() == Some(sha)
                || state
                    .current
                    .as_ref()
                    .is_some_and(|v| v.sha == sha && v.state == "failed")
            {
                Some("failed")
            } else {
                None
            };
            items.push(VerificationCommit {
                sha: sha.into(),
                title: fields[1].into(),
                author: fields[2].into(),
                authored_at: util::parse_time(fields[3])?,
                mark,
            });
        }
        let next_cursor = if more {
            items.last().map(|c| page.cursor(c.sha.as_bytes()))
        } else {
            None
        };
        Ok(Page { items, next_cursor })
    })
    .await
}
