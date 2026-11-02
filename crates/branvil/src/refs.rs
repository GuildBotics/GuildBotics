//! Ref-change coordination.
//!
//! Git ref updates and redb transactions are not one atomic operation, so:
//!
//! - Every ref change Branvil records or makes for a repository runs under that
//!   repository's async mutex, to its end (`App::locked`).
//! - A ref change Branvil makes itself (a merge) writes a journal entry with
//!   the expected old and new object before `git update-ref <ref> <new> <old>`
//!   and removes it in the transaction that applies the change's effects.
//! - Pushes are recorded after the fact: the post-receive hook asks the service
//!   to compare the repository's refs with the last snapshot it recorded.
//! - At startup, pending journal entries are finished (the ref is at `new`) or
//!   dropped (the ref is still at `old`), and every repository is rescanned, so
//!   pushes that landed while the service was down are recorded too.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::app::App;
use crate::error::{AppError, AppResult};
use crate::git::ZERO_SHA;
use crate::model::{JournalEntry, Member, MemberKind, Pull, Repo};
use crate::store::{self, Key, Rd, Wtx};
use crate::util;

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct RefChange {
    pub refname: String,
    /// `ZERO_SHA` when the ref was created.
    pub old: String,
    /// `ZERO_SHA` when the ref was deleted.
    pub new: String,
}

fn snapshot_prefix(repo_id: &str) -> Vec<u8> {
    Key::new().s(repo_id).bytes()
}

fn snapshot_key(repo_id: &str, refname: &str) -> Vec<u8> {
    Key::new().s(repo_id).s(refname).bytes()
}

/// Compares the repository's refs with the recorded snapshot, records the
/// difference, and applies its effects. Takes the repository lock.
pub async fn record_refs(app: &App, repo: &Repo) -> AppResult<Vec<RefChange>> {
    let r = repo.clone();
    app.locked(&repo.id, move |app| async move {
        record_refs_locked(&app, &r).await
    })
    .await
}

/// `record_refs` for a caller that already holds the repository lock.
pub async fn record_refs_locked(app: &App, repo: &Repo) -> AppResult<Vec<RefChange>> {
    let dir = app.git.repo_dir(&repo.path);
    let actual = app.git.all_refs(&dir).await?;
    let repo_id = repo.id.clone();
    let changes = app
        .store
        .write(move |w| apply_snapshot(w, &repo_id, &actual))
        .await?;
    if !changes.is_empty() {
        app.events.publish("repo", &repo.path, "");
        app.ci.on_ref_changes(repo, &changes).await;
        app.upstream.on_ref_changes(repo, &changes);
    }
    Ok(changes)
}

fn apply_snapshot(
    w: &Wtx,
    repo_id: &str,
    actual: &BTreeMap<String, String>,
) -> AppResult<Vec<RefChange>> {
    let mut recorded = BTreeMap::new();
    for (k, v) in w.prefix_raw(store::REFS, &snapshot_prefix(repo_id), None, usize::MAX)? {
        let parts = store::decode_key(&k, "ss").ok_or_else(|| AppError::internal("bad ref key"))?;
        recorded.insert(parts[1].s().to_string(), store::decode::<String>(&v)?);
    }
    let mut changes = Vec::new();
    for (name, sha) in actual {
        if recorded.get(name) != Some(sha) {
            changes.push(RefChange {
                refname: name.clone(),
                old: recorded
                    .get(name)
                    .cloned()
                    .unwrap_or_else(|| ZERO_SHA.to_string()),
                new: sha.clone(),
            });
            w.put(store::REFS, &snapshot_key(repo_id, name), sha)?;
        }
    }
    for (name, sha) in &recorded {
        if !actual.contains_key(name) {
            changes.push(RefChange {
                refname: name.clone(),
                old: sha.clone(),
                new: ZERO_SHA.to_string(),
            });
            w.del(store::REFS, &snapshot_key(repo_id, name))?;
        }
    }
    // A new head of an open pull request is activity on it.
    let now = util::now();
    for change in &changes {
        let Some(branch) = change.refname.strip_prefix("refs/heads/") else {
            continue;
        };
        let prefix = Key::new().s(repo_id).s(branch).bytes();
        for (_, number) in w.prefix_raw(store::IX_OPEN_PULLS, &prefix, None, usize::MAX)? {
            let number: u64 = String::from_utf8_lossy(&number).parse().unwrap_or(0);
            let key = Pull::key(repo_id, number);
            if let Some(mut pull) = w.get::<Pull>(store::PULLS, &key)? {
                pull.updated_at = now.clone();
                w.put(store::PULLS, &key, &pull)?;
            }
        }
    }
    Ok(changes)
}

pub fn journal_begin(w: &Wtx, entry: &JournalEntry) -> AppResult<()> {
    w.put(store::JOURNAL, &Key::new().s(&entry.id).bytes(), entry)
}

pub fn journal_end(w: &Wtx, id: &str) -> AppResult<()> {
    w.del(store::JOURNAL, &Key::new().s(id).bytes())?;
    Ok(())
}

/// Finishes or drops every pending journal entry, then rescans every
/// repository. Runs at startup before the service accepts requests.
pub async fn recover(app: &App) -> AppResult<RecoveryReport> {
    let mut report = RecoveryReport::default();
    let pending: Vec<JournalEntry> = app
        .store
        .read(|r| r.prefix_all(store::JOURNAL, &[]))
        .await?;
    for entry in pending {
        let repo = {
            let id = entry.repo.clone();
            app.store
                .read(move |r| crate::domain::repo_by_id(r, &id))
                .await?
        };
        let dir = app.git.repo_dir(&repo.path);
        let refs = app.git.all_refs(&dir).await?;
        let actual = refs
            .get(&entry.refname)
            .cloned()
            .unwrap_or_else(|| ZERO_SHA.to_string());
        if actual == entry.new {
            tracing::warn!(
                "recovery: {} {} reached {}; applying the recorded operation",
                repo.path,
                entry.refname,
                entry.new
            );
            let e = entry.clone();
            app.store
                .write(move |w| {
                    crate::domain::pulls::apply_journal(w, &e)?;
                    journal_end(w, &e.id)
                })
                .await?;
            report.completed += 1;
        } else {
            if actual == entry.old {
                tracing::warn!(
                    "recovery: {} {} was not moved; dropping the operation",
                    repo.path,
                    entry.refname
                );
            } else {
                tracing::error!(
                    "recovery: {} {} is at {actual}, neither the expected old {} nor new {}; dropping the operation",
                    repo.path,
                    entry.refname,
                    entry.old,
                    entry.new
                );
            }
            let id = entry.id.clone();
            app.store.write(move |w| journal_end(w, &id)).await?;
            report.rolled_back += 1;
        }
    }
    let repos: Vec<Repo> = app.store.read(|r| r.prefix_all(store::REPOS, &[])).await?;
    for repo in repos {
        // A gate's database record may precede its hidden ref after a crash.
        // Restore it before recording refs, so existing jobs are not cancelled.
        let r = repo.clone();
        let changes = app
            .locked(&repo.id, move |app| async move {
                crate::landing::repair_refs_locked(&app, &r).await
            })
            .await?;
        if !changes.is_empty() {
            tracing::info!(
                "recovery: recorded {} ref change(s) of {}",
                changes.len(),
                repo.path
            );
            report.rescanned_changes += changes.len();
        }
    }
    Ok(report)
}

#[derive(Default, Debug, Serialize)]
pub struct RecoveryReport {
    pub completed: usize,
    pub rolled_back: usize,
    pub rescanned_changes: usize,
}

/// One line of the pre-receive input.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Update {
    pub old: String,
    pub new: String,
    pub refname: String,
}

/// Decides whether a push may update the given refs. `Err` carries the
/// message shown to the pusher.
pub fn check_push(
    repo: &Repo,
    member: &Member,
    updates: &[Update],
    existing: &BTreeMap<String, String>,
) -> Result<(), String> {
    if member.kind == MemberKind::Runner {
        return Err(format!("{} cannot push to {}.", member.name, repo.path));
    }
    let owner = member.kind == MemberKind::Owner;
    for u in updates {
        let creating = u.old == ZERO_SHA;
        if let Some(branch) = u.refname.strip_prefix("refs/heads/") {
            if !crate::git::valid_branch(branch) {
                return Err(format!("{branch} is not a branch name Branvil accepts."));
            }
            if repo.upstream.is_some() && branch == repo.default_branch {
                return Err(format!(
                    "{branch} follows the upstream: it changes there, and Branvil follows it."
                ));
            }
            if repo.is_protected(branch) {
                // A protected branch changes only through a merge. The one
                // exception is its first creation in a repository that has no
                // such branch yet (the initial import), by the owner.
                let fresh = creating && !existing.contains_key(&u.refname);
                if !(fresh && owner) {
                    return Err(format!(
                        "{branch} is protected: change it through a pull request (direct pushes, deletions, and force pushes are refused)."
                    ));
                }
            }
        } else if u.refname.starts_with("refs/tags/") {
            if !(creating || owner) {
                return Err(format!(
                    "{} exists; only the owner moves or deletes a tag.",
                    u.refname
                ));
            }
        } else {
            return Err(format!(
                "{} is not a branch or tag; Branvil refuses it.",
                u.refname
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn repo() -> Repo {
        Repo {
            id: "r".into(),
            path: "acme/demo".into(),
            workspace: "ws".into(),
            default_branch: "main".into(),
            protected_branches: vec!["release".into()],
            required_checks: vec![],
            required_approvals: 1,
            auto_land: false,
            ci_network: false,
            labels: vec![],
            upstream: None,
            revision: 1,
            created_at: String::new(),
        }
    }

    fn member(kind: MemberKind) -> Member {
        Member {
            name: "aiko".into(),
            display_name: "Aiko".into(),
            kind,
            accounts: Vec::new(),
            reviewers: Vec::new(),
            created_at: String::new(),
            avatar: None,
        }
    }

    fn update(refname: &str, old: &str, new: &str) -> Update {
        Update {
            old: old.into(),
            new: new.into(),
            refname: refname.into(),
        }
    }

    const A: &str = "1111111111111111111111111111111111111111";
    const B: &str = "2222222222222222222222222222222222222222";

    #[test]
    fn protected_branches_change_only_by_merge() {
        let mut existing = BTreeMap::new();
        existing.insert("refs/heads/main".to_string(), A.to_string());
        let w = member(MemberKind::Agent);
        let m = member(MemberKind::Owner);
        assert!(
            check_push(
                &repo(),
                &w,
                &[update("refs/heads/feature", ZERO_SHA, B)],
                &existing
            )
            .is_ok()
        );
        assert!(check_push(&repo(), &w, &[update("refs/heads/main", A, B)], &existing).is_err());
        assert!(check_push(&repo(), &m, &[update("refs/heads/main", A, B)], &existing).is_err());
        assert!(
            check_push(
                &repo(),
                &m,
                &[update("refs/heads/main", A, ZERO_SHA)],
                &existing
            )
            .is_err()
        );
        assert!(
            check_push(
                &repo(),
                &w,
                &[update("refs/heads/release", ZERO_SHA, B)],
                &existing
            )
            .is_err()
        );
        assert!(
            check_push(
                &repo(),
                &m,
                &[update("refs/heads/release", ZERO_SHA, B)],
                &existing
            )
            .is_ok()
        );
        assert!(
            check_push(
                &repo(),
                &member(MemberKind::Runner),
                &[update("refs/heads/f", ZERO_SHA, B)],
                &existing
            )
            .is_err()
        );
        assert!(
            check_push(
                &repo(),
                &w,
                &[update("refs/branvil/x", ZERO_SHA, B)],
                &existing
            )
            .is_err()
        );
        assert!(
            check_push(
                &repo(),
                &w,
                &[update("refs/tags/v1", ZERO_SHA, B)],
                &existing
            )
            .is_ok()
        );
        assert!(check_push(&repo(), &w, &[update("refs/tags/v1", A, B)], &existing).is_err());
        assert!(check_push(&repo(), &m, &[update("refs/tags/v1", A, B)], &existing).is_ok());
    }
}
