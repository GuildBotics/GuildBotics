//! Durable failure handoff; Git and log reads never run in write transactions.
use crate::app::App;
use crate::ci::{queue, verify};
use crate::domain::tickets;
use crate::error::AppResult;
use crate::model::{self, Assignee, MemberKind, Pull, Repair, Repo, Target, Ticket};
use crate::store::{self, Key, Rd, Wtx};
use serde::{Deserialize, Serialize};

#[derive(Clone, Serialize, Deserialize)]
struct Pending {
    repo: String,
    sha: String,
    failures: Vec<verify::FailedJob>,
}

pub fn failed(w: &Wtx, repo: &str, sha: &str, failures: &[verify::FailedJob]) -> AppResult<()> {
    w.put(
        store::REPAIR_PENDING,
        &Key::new().s(repo).s(sha).bytes(),
        &Pending {
            repo: repo.into(),
            sha: sha.into(),
            failures: failures.to_vec(),
        },
    )
}

pub fn open_ticket(r: &impl Rd, repo: &str) -> AppResult<Option<Ticket>> {
    let first = r.prefix_raw(store::IX_OPEN_REPAIRS, &Key::new().s(repo).bytes(), None, 1)?;
    first
        .first()
        .map(|(key, _)| r.get(store::TICKETS, key))
        .transpose()
        .map(Option::flatten)
}

pub fn passed(w: &Wtx, repo: &str, sha: &str) -> AppResult<()> {
    for key in w.prefix_keys(store::REPAIR_PENDING, &Key::new().s(repo).bytes())? {
        w.del(store::REPAIR_PENDING, &key)?;
    }
    for key in w.prefix_keys(store::IX_OPEN_REPAIRS, &Key::new().s(repo).bytes())? {
        let old: Ticket = w.get(store::TICKETS, &key)?.expect("indexed ticket");
        let now = crate::util::now();
        let mut t = tickets::place(w, &old, "done", None, None)?;
        t.state = "closed".into();
        t.state_reason = Some("completed".into());
        t.closed_at = Some(now.clone());
        t.updated_at = now;
        t.revision += 1;
        let merged: Vec<_> = tickets::linked_pulls(w, "", &t, false)?
            .into_iter()
            .filter(|p| p.merged)
            .map(|p| p.label)
            .collect();
        let explanation = if merged.is_empty() {
            "No pull request linked here was merged before it passed: the failure may be intermittent.".into()
        } else {
            format!("Merged pull requests linked here: {}.", merged.join(", "))
        };
        tickets::insert_comment(
            w,
            &Target::Ticket {
                repo: repo.into(),
                number: t.number,
            },
            "branvil",
            format!(
                "Verification of main passed at {}. Closing.\n\n{explanation}",
                short(sha)
            ),
            None,
        )?;
        tickets::write_ticket(w, Some(&old), &t)?;
    }
    Ok(())
}

pub(super) fn short(sha: &str) -> &str {
    &sha[..sha.len().min(7)]
}

// Prevent evidence originating in commit subjects, titles or logs from making mentions.
pub(super) fn text(s: &str) -> String {
    s.replace('@', "&#64;")
        .replace('|', "&#124;")
        .replace(['\r', '\n'], " ")
}

async fn evidence(app: &App, repo: &Repo, pending: &Pending) -> AppResult<(String, Vec<Pull>)> {
    let id = repo.id.clone();
    let verified = app
        .store
        .read(move |r| verify::latest_verified(r, &id))
        .await?;
    let dir = app.git.repo_dir(&repo.path);
    let verified = match verified {
        Some(v) if app.git.is_ancestor(&dir, &v.sha, &pending.sha).await? => Some(v),
        _ => None,
    };
    let commits: Vec<String> = match &verified {
        Some(v) => app
            .git
            .run_ok(
                Some(&dir),
                &[
                    "rev-list",
                    "--first-parent",
                    "--reverse",
                    &format!("{}..{}", v.sha, pending.sha),
                    "--",
                ],
            )
            .await?
            .lines()
            .map(str::to_owned)
            .collect(),
        None => vec![pending.sha.clone()],
    };
    let id = repo.id.clone();
    let pulls = app
        .store
        .read(move |r| r.prefix_all::<Pull>(store::PULLS, &Key::new().s(&id).bytes()))
        .await?;
    let matched: Vec<Pull> = commits
        .iter()
        .filter_map(|sha| {
            pulls
                .iter()
                .filter(|p| p.merged && p.merge_sha.as_ref() == Some(sha))
                .min_by_key(|p| p.number)
                .cloned()
        })
        .collect();
    let root = format!("{}/{}", app.base_url, repo.path);
    let commit_link = |sha: &str| format!("[{}]({root}/-/commit/{sha})", short(sha));
    let mut body = format!(
        "Branvil created this repair ticket because verification of {} failed at {}. Restore passing verification of main, regardless of whose change caused the failure. Branvil will close this ticket when verification of main passes.\n\n## Failed jobs\n",
        text(&repo.default_branch),
        commit_link(&pending.sha)
    );
    for f in &pending.failures {
        let attempt_id = f.attempt_id.clone();
        let conclusion = app
            .store
            .read(move |r| {
                Ok(attempt_id
                    .as_deref()
                    .map(|id| queue::get_attempt(r, id))
                    .transpose()?
                    .flatten()
                    .and_then(|a| a.conclusion))
            })
            .await?;
        let (label, link, log) = if let Some(id) = &f.attempt_id {
            (
                "Job",
                format!("{root}/-/jobs/{id}"),
                app.ci.log_tail(id, 8192).await?.0,
            )
        } else {
            (
                "Commit",
                format!("{root}/-/commit/{}", pending.sha),
                String::new(),
            )
        };
        body.push_str(&format!(
            "\n### {}\n\n[{label}]({link}): {}\n",
            text(&f.name),
            text(
                f.details
                    .as_deref()
                    .or(conclusion.as_deref())
                    .unwrap_or("failure")
            )
        ));
        if !log.is_empty() {
            let fence = "`".repeat(
                log.split(|c| c != '`')
                    .map(str::len)
                    .max()
                    .unwrap_or(0)
                    .max(2)
                    + 1,
            );
            body.push_str(&format!(
                "\n{fence}\n{}\n{fence}\n",
                log.replace('@', "&#64;")
            ));
        }
    }
    body.push_str("\n## Range\n\n");
    body.push_str(&verified.as_ref().map_or_else(
        || "main has not passed verification yet.\n".into(),
        |v| format!("Last verified: {}.\n", commit_link(&v.sha)),
    ));
    body.push_str("\n| Commit | Pull request | Author |");
    for f in &pending.failures {
        body.push_str(&format!(" {} |", text(&f.name)));
    }
    body.push_str(&format!(
        "\n| --- | --- | --- |{}\n",
        " --- |".repeat(pending.failures.len())
    ));
    let mut unrun = vec![!matched.is_empty(); pending.failures.len()];
    for (position, sha) in commits.iter().enumerate() {
        let pull = matched.iter().find(|p| p.merge_sha.as_ref() == Some(sha));
        let cells = if let Some(p) = pull {
            format!(
                "[!{}: {}]({root}/-/pulls/{}) | {}",
                p.number,
                text(&p.title),
                p.number,
                text(&p.author)
            )
        } else {
            let author = app
                .git
                .run_ok(Some(&dir), &["show", "-s", "--format=%an", sha, "--"])
                .await?;
            format!(
                "Not from a pull request of this Branvil | {}",
                text(author.trim())
            )
        };
        if position < 100 {
            body.push_str(&format!("| {} | {cells} |", commit_link(sha)));
        }
        let (id, head) = (repo.id.clone(), pull.and_then(|p| p.merged_head.clone()));
        let attempts = app
            .store
            .read(move |r| {
                let Some(head) = head else {
                    return Ok(vec![]);
                };
                let Some(run) = queue::get_run(r, &id, &head)? else {
                    return Ok(vec![]);
                };
                let queue::PipelineState::Jobs { jobs } = run.pipeline else {
                    return Ok(vec![]);
                };
                queue::current_attempts(r, &id, &head, &jobs)
            })
            .await?;
        for (i, f) in pending.failures.iter().enumerate() {
            let attempt = attempts.iter().find(|a| a.job.name == f.name);
            let attempt = attempt.cloned();
            let (result, missing) = app
                .store
                .read(move |r| head_result(r, attempt.as_ref()))
                .await?;
            if pull.is_some() {
                unrun[i] &= missing;
            }
            if position < 100 {
                body.push_str(&format!(" {} |", text(&result)));
            }
        }
        if position < 100 {
            body.push('\n');
        }
    }
    let jobs = pending
        .failures
        .iter()
        .zip(unrun)
        .filter(|(_, missing)| *missing)
        .map(|(f, _)| text(&f.name))
        .collect::<Vec<_>>();
    if !jobs.is_empty() {
        body.push_str(&format!("\nNo pull request's head in the range ran {}: they skipped it because no file matching its paths changed, reused a result with the same contents of its paths, or have no record. If the failure comes from a file outside the job's paths, add that file to the job's paths in .branvil/ci.toml as part of the repair.\n",jobs.join(", ")));
    }
    if commits.len() > 100 {
        body.push_str(&format!("\n{} more commits.\n", commits.len() - 100));
    }
    Ok((body, matched))
}

fn head_result(r: &impl Rd, a: Option<&queue::Attempt>) -> AppResult<(String, bool)> {
    let Some(a) = a else {
        return Ok(("No record".into(), true));
    };
    if let Some(skip) = &a.path_skip {
        return Ok(match skip.reason {
            queue::SkipReason::Paths => ("Skipped by paths".into(), true),
            queue::SkipReason::PostMerge => ("Post-merge only".into(), false),
        });
    }
    if a.conclusion.as_deref() == Some("success") && a.reused_from.is_some() {
        let source = queue::source_of(r, a)?;
        if source.basis == Some(queue::Basis::Paths) {
            return Ok((
                format!(
                    "reused {} (same contents of paths)",
                    short(&source.attempt.sha)
                ),
                true,
            ));
        }
    }
    Ok((
        a.conclusion
            .clone()
            .unwrap_or_else(|| "No conclusion".into()),
        false,
    ))
}

fn create(
    w: &Wtx,
    repo: &Repo,
    pending: &Pending,
    mut body: String,
    pulls: &[Pull],
) -> AppResult<Ticket> {
    let first = pulls.first();
    let author = first
        .map(|p| crate::domain::member_by_name(w, &p.author))
        .transpose()?;
    let (name, reason) = if let Some(m) = author.filter(|m| m.kind == MemberKind::Agent) {
        (
            m.name,
            format!(
                "the author of the first pull request after the last verified commit (!{}).",
                first.unwrap().number
            ),
        )
    } else {
        let responder = super::responder(w, repo)?;
        let why = first.map_or_else(
            || {
                "no commit after the last verified commit is from a pull request of this Branvil"
                    .into()
            },
            |p| {
                format!(
                    "the first pull request after the last verified commit (!{}) is the owner's",
                    p.number
                )
            },
        );
        let mut reason = format!("the repository's default responder, because {why}.");
        if !responder.chosen {
            let members: Vec<model::Member> = w.prefix_all(store::MEMBERS, &[])?;
            let has_merged = w
                .prefix_all::<Pull>(store::PULLS, &Key::new().s(&repo.id).bytes())?
                .iter()
                .any(|p| {
                    p.merged
                        && p.merged_at.is_some()
                        && members.iter().any(|m| {
                            m.kind == MemberKind::Agent
                                && crate::util::fold(&m.name) == crate::util::fold(&p.author)
                        })
                });
            reason.push_str(&format!(
                " No default responder is chosen for this repository; {} {}.",
                text(&responder.member),
                if has_merged {
                    "authored its most recently merged pull request by a member"
                } else {
                    "is its first member"
                }
            ));
        }
        (responder.member, reason)
    };
    body.push_str(&format!("\n## Assignee\n\n{}: {reason}\n", text(&name)));
    body.push_str("\nHints from running the failed jobs on the commits in between are added as comments. They do not change the assignee.\n");
    let now = crate::util::now();
    let t = Ticket {
        repo: repo.id.clone(),
        workspace: repo.workspace.clone(),
        number: w.next(&format!("ticket:{}", repo.id))?,
        title: format!(
            "Repair {}: verification failed ({})",
            repo.default_branch,
            pending
                .failures
                .iter()
                .map(|f| f.name.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ),
        body,
        mentions: vec![],
        author: "branvil".into(),
        state: "open".into(),
        state_reason: None,
        assignee: Some(Assignee {
            name,
            assigned_at: now.clone(),
        }),
        labels: vec![],
        interactive: false,
        upstream_issue: None,
        lane: "ready".into(),
        rank: tickets::rank_first(w, &repo.workspace, "ready")?,
        parent: None,
        blocked_by: vec![],
        repair: Some(Repair {
            jobs: pending.failures.iter().map(|f| f.name.clone()).collect(),
            reported: pending.sha.clone(),
        }),
        created_at: now.clone(),
        updated_at: now,
        closed_at: None,
        revision: 1,
    };
    tickets::write_ticket(w, None, &t)?;
    if let Some(v) = verify::latest_verified(w, &repo.id)? {
        for f in &pending.failures {
            if let Some(n) = crate::ci::narrow::concluded(w, &repo.id, &v.sha, &f.name)?
                && let Some(hint) = super::Hint::from_conclusion(&n)
            {
                super::hint(w, &repo.id, &n.failed_sha, &f.name, hint)?;
            }
        }
    }
    Ok(t)
}

pub async fn sweep(app: &App) -> AppResult<()> {
    let pending = app
        .store
        .read(|r| r.prefix_all::<Pending>(store::REPAIR_PENDING, &[]))
        .await?;
    for p in pending {
        if let Err(error) = sweep_pending(app, p.clone()).await {
            tracing::warn!(repo = %p.repo, sha = %p.sha, "repair ticket: {error}");
        }
    }
    Ok(())
}

async fn sweep_pending(app: &App, p: Pending) -> AppResult<()> {
    let id = p.repo.clone();
    let repo = app
        .store
        .read(move |r| crate::domain::repo_by_id(r, &id))
        .await?;
    let id = p.repo.clone();
    let old = app.store.read(move |r| open_ticket(r, &id)).await?;
    let mut lines = Vec::new();
    let mut merged = Vec::new();
    if let Some(t) = &old {
        let repair = t.repair.as_ref().unwrap();
        let (t2, id) = (t.clone(), repo.id.clone());
        let pulls = app
            .store
            .read(move |r| {
                Ok(
                    r.prefix_all::<Pull>(store::PULLS, &Key::new().s(&id).bytes())?
                        .into_iter()
                        .filter(|p| {
                            p.merged
                                && p.tickets
                                    .iter()
                                    .any(|l| l.local.as_ref() == Some(&(id.clone(), t2.number)))
                        })
                        .collect::<Vec<_>>(),
                )
            })
            .await?;
        let dir = app.git.repo_dir(&repo.path);
        for pull in pulls {
            if let Some(sha) = pull.merge_sha
                && app.git.is_ancestor(&dir, &sha, &p.sha).await?
                && !app.git.is_ancestor(&dir, &sha, &repair.reported).await?
            {
                merged.push(pull.number);
            }
        }
        for f in &p.failures {
            let root = format!("{}/{}", app.base_url, repo.path);
            let link = f.attempt_id.as_ref().map_or_else(
                || format!("{root}/-/commit/{}", p.sha),
                |id| format!("{root}/-/jobs/{id}"),
            );
            let job = text(&f.name);
            if !repair.jobs.contains(&f.name) {
                lines.push(format!("- New failing job: {job} — {link}"));
            }
            for n in &merged {
                lines.push(format!(
                    "- Still failing after !{n} (linked here) was merged: {job} — {link}"
                ));
            }
            if repair.reported == p.sha {
                lines.push(format!(
                    "- Still failing after its failed jobs were retried: {job} — {link}"
                ));
            }
        }
    }
    let evidence = if old.is_none() {
        Some(evidence(app, &repo, &p).await?)
    } else {
        None
    };
    let path = repo.path.clone();
    let changed = app
        .store
        .write(move |w| {
            let key = Key::new().s(&p.repo).s(&p.sha).bytes();
            if w.get::<Pending>(store::REPAIR_PENDING, &key)?.is_none() {
                return Ok(None);
            }
            let current = open_ticket(w, &p.repo)?;
            if current
                .as_ref()
                .map(|t| (&t.number, &t.repair.as_ref().unwrap().reported))
                != old
                    .as_ref()
                    .map(|t| (&t.number, &t.repair.as_ref().unwrap().reported))
            {
                return Ok(None);
            }
            let changed = if let Some(old) = current {
                if lines.is_empty() {
                    None
                } else {
                    let mut t = if matches!(old.lane.as_str(), "new" | "done") {
                        Ticket {
                            lane: "ready".into(),
                            rank: tickets::rank_first(w, &old.workspace, "ready")?,
                            ..old.clone()
                        }
                    } else {
                        old.clone()
                    };
                    let repair = t.repair.as_mut().unwrap();
                    repair.reported = p.sha.clone();
                    for f in &p.failures {
                        if !repair.jobs.contains(&f.name) {
                            repair.jobs.push(f.name.clone());
                        }
                    }
                    t.revision += 1;
                    t.updated_at = crate::util::now();
                    tickets::insert_comment(
                        w,
                        &Target::Ticket {
                            repo: p.repo.clone(),
                            number: t.number,
                        },
                        "branvil",
                        format!(
                            "Verification of main failed again at {}.\n\n{}",
                            short(&p.sha),
                            lines.join("\n")
                        ),
                        None,
                    )?;
                    tickets::write_ticket(w, Some(&old), &t)?;
                    Some(t.number)
                }
            } else {
                let (body, pulls) = evidence.unwrap();
                Some(create(w, &repo, &p, body, &pulls)?.number)
            };
            w.del(store::REPAIR_PENDING, &key)?;
            Ok(changed)
        })
        .await?;
    if let Some(number) = changed {
        app.events.publish("ticket", &path, number.to_string());
        app.events
            .publish("board", tickets::workspace_of(&path), "");
        app.events.publish("verification", &path, "");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ci::narrow::Narrowing;
    use crate::model::{Comment, Member};
    use serde_json::json;

    fn repo() -> Repo {
        serde_json::from_value(json!({"id":"repo","path":"acme/demo","workspace":"ws","default_branch":"main","protected_branches":[],"required_checks":[],"post_merge_jobs":[],"required_approvals":0,"auto_land":false,"ci_network":false,"labels":[],"revision":1,"created_at":"0"})).unwrap()
    }
    fn conclusion(state: &str) -> Narrowing {
        serde_json::from_value(json!({"repo":"repo","base":"aaaaaaa0","job":"test","failed_sha":"fffffff0","commits":["aaaaaaa0","bbbbbbb0"],"history_ready":true,"results":[[{"attempt":"pass","outcome":"success","probe":true}],[{"attempt":"fail","outcome":"failure","probe":true}]],"conclusion":state,"boundary":["aaaaaaa0","bbbbbbb0"],"started_at":"0"})).unwrap()
    }
    fn comments(r: &impl Rd, t: &Ticket) -> Vec<Comment> {
        r.prefix_all(
            store::COMMENTS,
            &Target::Ticket {
                repo: t.repo.clone(),
                number: t.number,
            }
            .prefix(),
        )
        .unwrap()
    }
    #[tokio::test]
    async fn hints_before_and_after_creation_are_unique_and_leave_ticket_unchanged() {
        for state in [
            "found",
            "outside",
            "inconsistent",
            "undetermined",
            "abandoned",
        ] {
            for before in [true, false] {
                let dir = tempfile::tempdir().unwrap();
                let store = store::Store::open(&dir.path().join("test.redb"), 1 << 20, 1).unwrap();
                store.write(move |w| {
                    let repo = repo();
                    w.put(store::REPOS, &Repo::key(&repo.id), &repo)?;
                    let m: Member = serde_json::from_value(json!({"name":"member","display_name":"Member","kind":"agent","accounts":[],"reviewers":[],"created_at":"0"})).unwrap();
                    w.put(store::MEMBERS, &Member::key(&m.name), &m)?;
                    let n = conclusion(state);
                    // No open ticket: no comment and no extra repair record.
                    if let Some(h) = super::super::Hint::from_conclusion(&n) {
                        super::super::hint(w, &repo.id, &n.failed_sha, &n.job, h)?;
                    }
                    assert!(w.prefix_all::<Comment>(store::COMMENTS, &[])?.is_empty());
                    let save = |w: &Wtx| -> AppResult<()> {
                        w.put(store::CI_NARROWINGS, &Key::new().s("repo").s(&n.base).s(&n.job).bytes(), &n)?;
                        w.put(store::CI_VERIFICATIONS, &Key::new().s("repo").bytes(), &json!({"sha":n.failed_sha,"base":n.base,"state":"failed","started_at":"0","failed_jobs":[],"last_verified":{"sha":n.base,"at":"0"}}))
                    };
                    if before { save(w)?; }
                    let pending = Pending { repo: repo.id.clone(), sha: n.failed_sha.clone(), failures: vec![verify::FailedJob { name: n.job.clone(), attempt_id: None, details: None }] };
                    let t = create(w, &repo, &pending, "Evidence".into(), &[])?;
                    assert!(t.body.contains("Hints from running the failed jobs"));
                    assert_eq!(comments(w, &t).len(), usize::from(before && super::super::Hint::from_conclusion(&n).is_some()));
                    if !before { save(w)?; }
                    for _ in 0..2 {
                        if let Some(h) = super::super::Hint::from_conclusion(&n) {
                            super::super::hint(w, &repo.id, &n.failed_sha, &n.job, h)?;
                        }
                    }
                    let cs = comments(w, &t);
                    assert_eq!(cs.len(), usize::from(super::super::Hint::from_conclusion(&n).is_some()));
                    if let Some(c) = cs.first() {
                        assert_eq!(c.author, "branvil");
                        assert_eq!(c.meta, Some(json!({"repair_hint":{"sha":n.failed_sha,"job":"test"}})));
                        match state {
                            "found" => assert!(c.body.contains("passes at aaaaaaa and fails at bbbbbbb, the first commit where it fails (not from a pull request of this Branvil)")),
                            "outside" => assert!(c.body.contains("also fails at aaaaaaa, the last verified commit")),
                            "inconsistent" => assert!(c.body.contains("aaaaaaa: success [Job](/acme/demo/-/jobs/pass), bbbbbbb: failure [Job](/acme/demo/-/jobs/fail)")),
                            _ => unreachable!(),
                        }
                    }
                    if let Some(mut c) = cs.first().cloned() {
                        c.meta.as_mut().unwrap()["repair_hint"]["extra"] = json!(true);
                        let target = Target::Ticket { repo: repo.id.clone(), number: t.number };
                        w.put(store::COMMENTS, &target.comment_key(c.seq), &c)?;
                        super::super::hint(w, &repo.id, &n.failed_sha, &n.job, super::super::Hint::FailsOnVerified { sha: n.base.clone() })?;
                        assert_eq!(comments(w, &t).len(), cs.len());
                    }
                    let saved: Ticket = w.get(store::TICKETS, &Ticket::key(&repo.id, t.number))?.unwrap();
                    assert_eq!(serde_json::to_value(&t).unwrap(), serde_json::to_value(&saved).unwrap());
                    let closed = Ticket { state: "closed".into(), ..t.clone() };
                    tickets::write_ticket(w, Some(&t), &closed)?;
                    if let Some(h) = super::super::Hint::from_conclusion(&n) {
                        super::super::hint(w, &repo.id, "another failure", &n.job, h)?;
                    }
                    assert_eq!(comments(w, &t).len(), cs.len());
                    Ok(())
                }).await.unwrap();
            }
        }
    }
    #[tokio::test]
    async fn head_evidence_distinguishes_execution_paths_tree_and_post_merge() {
        let dir = tempfile::tempdir().unwrap();
        let store = store::Store::open(&dir.path().join("test.redb"), 1 << 20, 1).unwrap();
        store
            .write(|w| {
                let jobs = crate::ci::pipeline::parse(
                    "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\npaths=['src/**']\n",
                )
                .unwrap();
                for (sha, tree) in [("source0", "tree1"), ("head000", "tree2")] {
                    queue::register(
                        w,
                        queue::Registration {
                            order: vec![],
                            selection: None,
                            repo: "repo".into(),
                            repo_path: "acme/demo".into(),
                            sha: sha.into(),
                            tree: Some(tree.into()),
                            branch: sha.into(),
                            protected: false,
                            pipeline: queue::PipelineState::Jobs { jobs: jobs.clone() },
                            network: false,
                            inputs: Default::default(),
                        },
                        100,
                        "0",
                    )?;
                }
                let mut source = queue::attempts_of(w, "repo", "source0")?.remove(0);
                source.status = "completed".into();
                source.conclusion = Some("success".into());
                w.put(
                    store::CI_ATTEMPTS,
                    &queue::Attempt::key(&source.id),
                    &source,
                )?;
                let mut a = queue::attempts_of(w, "repo", "head000")?.remove(0);
                assert_eq!(head_result(w, None)?, ("No record".into(), true));
                for outcome in ["success", "failure", "timed_out"] {
                    a.conclusion = Some(outcome.into());
                    assert_eq!(head_result(w, Some(&a))?, (outcome.into(), false));
                }
                for (reason, label, missing) in [
                    (queue::SkipReason::Paths, "Skipped by paths", true),
                    (queue::SkipReason::PostMerge, "Post-merge only", false),
                ] {
                    a.path_skip = Some(queue::PathSkip {
                        reason,
                        paths: vec![],
                        bases: vec![],
                    });
                    assert_eq!(head_result(w, Some(&a))?, (label.into(), missing));
                }
                a.path_skip = None;
                a.conclusion = Some("success".into());
                a.reused_from = Some(source.id);
                assert_eq!(
                    head_result(w, Some(&a))?,
                    ("reused source0 (same contents of paths)".into(), true)
                );
                let mut run = queue::get_run(w, "repo", "head000")?.unwrap();
                run.tree = Some("tree1".into());
                queue::put_run(w, &run)?;
                assert_eq!(head_result(w, Some(&a))?, ("success".into(), false));
                Ok(())
            })
            .await
            .unwrap();
    }
}
