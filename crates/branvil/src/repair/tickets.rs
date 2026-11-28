//! Durable failure handoff; Git and log reads never run in write transactions.
use crate::app::App;
use crate::ci::{diagnostics, queue, verify};
use crate::domain::tickets;
use crate::error::AppResult;
use crate::model::{self, Assignee, MemberKind, Pull, Repair, Repo, Target, Ticket};
use crate::store::{self, Key, Rd, Wtx};
use serde::{Deserialize, Serialize};

#[derive(Clone, Serialize, Deserialize)]
struct Pending {
    repo: String,
    branch: Option<String>,
    sha: String,
    failures: Vec<verify::FailedJob>,
}

pub fn failed(w: &Wtx, repo: &str, sha: &str, failures: &[verify::FailedJob]) -> AppResult<()> {
    failed_for(w, repo, None, sha, failures)
}

pub(crate) fn failed_for(
    w: &Wtx,
    repo: &str,
    branch: Option<&str>,
    sha: &str,
    failures: &[verify::FailedJob],
) -> AppResult<()> {
    if branch.is_some() {
        clear_pending(w, repo, branch)?;
    }
    let pending = Pending {
        repo: repo.into(),
        branch: branch.map(str::to_owned),
        sha: sha.into(),
        failures: failures.to_vec(),
    };
    w.put(store::REPAIR_PENDING, &pending.key(), &pending)
}

impl Pending {
    fn key(&self) -> Vec<u8> {
        let key = Key::new().s(&self.repo).s(&self.sha);
        match &self.branch {
            Some(branch) => key.s(branch).bytes(),
            None => key.bytes(),
        }
    }
}

pub fn open_ticket(r: &impl Rd, repo: &str) -> AppResult<Option<Ticket>> {
    open_ticket_for(r, repo, None)
}

pub(crate) fn open_ticket_for(
    r: &impl Rd,
    repo: &str,
    branch: Option<&str>,
) -> AppResult<Option<Ticket>> {
    Ok(open_tickets_for(r, repo, branch)?.into_iter().next())
}

fn open_tickets_for(r: &impl Rd, repo: &str, branch: Option<&str>) -> AppResult<Vec<Ticket>> {
    let mut tickets = Vec::new();
    for key in r.prefix_keys(store::IX_OPEN_REPAIRS, &Key::new().s(repo).bytes())? {
        let t: Ticket = r.get(store::TICKETS, &key)?.expect("indexed ticket");
        if t.repair
            .as_ref()
            .is_some_and(|repair| repair.branch.as_deref() == branch)
        {
            tickets.push(t);
        }
    }
    Ok(tickets)
}

#[cfg(test)]
pub fn passed(w: &Wtx, repo: &str, sha: &str) -> AppResult<()> {
    passed_for(w, repo, None, "main", sha).map(|_| ())
}

pub(crate) fn passed_for(
    w: &Wtx,
    repo: &str,
    branch: Option<&str>,
    branch_name: &str,
    sha: &str,
) -> AppResult<Vec<u64>> {
    clear_pending(w, repo, branch)?;
    let mut closed = Vec::new();
    for old in open_tickets_for(w, repo, branch)? {
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
                "Verification of {} passed at {}. Closing.\n\n{explanation}",
                text(branch_name),
                short(sha)
            ),
            None,
        )?;
        tickets::write_ticket(w, Some(&old), &t)?;
        closed.push(t.number);
    }
    Ok(closed)
}

fn clear_pending(w: &Wtx, repo: &str, branch: Option<&str>) -> AppResult<()> {
    for key in w.prefix_keys(store::REPAIR_PENDING, &Key::new().s(repo).bytes())? {
        let pending: Pending = w.get(store::REPAIR_PENDING, &key)?.expect("indexed repair");
        if pending.branch.as_deref() == branch {
            w.del(store::REPAIR_PENDING, &key)?;
        }
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

async fn failed_job(
    app: &App,
    repo: &Repo,
    root: &str,
    sha: &str,
    failure: &verify::FailedJob,
) -> AppResult<String> {
    let Some(id) = &failure.attempt_id else {
        return Ok(format!(
            "\n### {}\n\n[Commit]({root}/commits/{sha}): {}\n",
            text(&failure.name),
            text(failure.details.as_deref().unwrap_or("failure")),
        ));
    };
    let evidence = diagnostics::load(app, &repo.id, id).await?;
    let conclusion = evidence.as_ref().and_then(|e| e.conclusion.as_deref());
    let mut body = format!(
        "\n### {}\n\n[Job]({root}/jobs/{id}): {}\n",
        text(&failure.name),
        text(
            failure
                .details
                .as_deref()
                .or(conclusion)
                .unwrap_or("failure")
        )
    );
    if let Some(evidence) = evidence {
        write_evidence(&mut body, &evidence);
    }
    let (log, _) = app.ci.log_tail(id, 8192).await?;
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
    Ok(body)
}

fn write_evidence(body: &mut String, evidence: &diagnostics::JobEvidence) {
    use std::fmt::Write as _;

    let runner = serde_json::json!({
        "sha": evidence.sha,
        "attempt": evidence.attempt,
        "runner": evidence.runner,
        "allocation": evidence.allocation,
        "runner_claim": evidence.runner_claim,
        "retry_decision": evidence.retry_decision,
        "runner_deferral_count": evidence.runner_deferral_count,
        "runner_deferrals": evidence.runner_deferrals,
        "resources": evidence.resources,
        "peak_memory_mib": evidence.peak_memory_mib,
    });
    // Single-line JSON cannot close the fence. Escaping mentions keeps runner data inert.
    let runner = runner.to_string().replace('@', r"\u0040");
    let _ = write!(body, "\nRunner and resources\n\n```json\n{runner}\n```\n");
    let _ = writeln!(
        body,
        "\nOther attempts of this job at the same commit ({}/{})",
        evidence.other_attempts.len(),
        evidence.other_attempt_count,
    );
    for attempt in &evidence.other_attempts {
        let reuse = if attempt.reused_from.is_some() {
            " (reused result)"
        } else {
            ""
        };
        let _ = writeln!(
            body,
            "- [#{}]({}): {}, {}{reuse}",
            attempt.attempt,
            attempt.url,
            text(attempt.conclusion.as_deref().unwrap_or(&attempt.status)),
            text(attempt.runner.as_deref().unwrap_or("no runner")),
        );
    }
    let Some(count) = evidence.failed_test_count else {
        body.push_str("\nNo readable JUnit report recorded for this attempt.\n");
        return;
    };
    let _ = writeln!(
        body,
        "\nTest failure history ({}/{count} names from JUnit)",
        evidence.test_failures.len()
    );
    for history in &evidence.test_failures {
        let name = text(&history.name)
            .replace('[', "&#91;")
            .replace(']', "&#93;")
            .replace('<', "&lt;")
            .replace('\\', "&#92;");
        let _ = writeln!(
            body,
            "- [{name}]({}) ({}/{} retained failures)",
            history.url,
            history.failures.len(),
            history.failure_count
        );
        for failure in &history.failures {
            let _ = writeln!(
                body,
                "  - {} at {} on {} ({})",
                text(&failure.job),
                short(&failure.sha),
                text(&failure.runner),
                text(&failure.finished_at)
            );
        }
    }
}

async fn evidence(
    app: &App,
    repo: &Repo,
    pending: &Pending,
) -> AppResult<(String, Vec<Pull>, String)> {
    let (id, branch) = (repo.id.clone(), pending.branch.clone());
    let verified = app
        .store
        .read(
            move |r| Ok(verify::get_for(r, &id, branch.as_deref())?.and_then(|v| v.last_verified)),
        )
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
                .filter(|p| {
                    p.merged
                        && p.merge_sha.as_ref() == Some(sha)
                        && pending
                            .branch
                            .as_ref()
                            .is_none_or(|branch| &p.base == branch)
                })
                .min_by_key(|p| p.number)
                .cloned()
        })
        .collect();
    let root = format!(
        "{}{}",
        app.base_url,
        crate::util::repository_page(&repo.path)
    );
    let commit_link = |sha: &str| format!("[{}]({root}/commits/{sha})", short(sha));
    let branch = text(pending.branch.as_deref().unwrap_or(&repo.default_branch));
    let mut body = format!(
        "GuildBotics created this repair ticket because verification of {branch} failed at {}. Restore passing verification of {branch}, regardless of whose change caused the failure. GuildBotics will close this ticket when verification of {branch} passes.\n\n## Failed jobs\n",
        commit_link(&pending.sha)
    );
    for f in &pending.failures {
        body.push_str(&failed_job(app, repo, &root, &pending.sha, f).await?);
    }
    body.push_str("\n## Range\n\n");
    body.push_str(&verified.as_ref().map_or_else(
        || format!("{branch} has not passed verification yet.\n"),
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
                "[!{}: {}]({root}/pulls/{}) | {}",
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
                "Not from a pull request of this server | {}",
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
        body.push_str(&format!("\nNo pull request's head in the range ran {}: they skipped it because no file matching its paths changed, reused a result with the same contents of its paths, or have no record. If the failure comes from a file outside the job's paths, add that file to the job's paths in .guildbotics-ci.toml as part of the repair.\n",jobs.join(", ")));
    }
    if commits.len() > 100 {
        body.push_str(&format!("\n{} more commits.\n", commits.len() - 100));
    }
    Ok((
        body,
        matched,
        verified.map_or_else(|| "no verified commit".into(), |v| short(&v.sha).to_owned()),
    ))
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

#[expect(
    clippy::too_many_arguments,
    reason = "Repair evidence and the host roster snapshot are consumed in one transaction."
)]
fn create(
    w: &Wtx,
    repo: &Repo,
    pending: &Pending,
    mut body: String,
    pulls: &[Pull],
    verified: &str,
    base_url: &str,
    active: Option<&[String]>,
) -> AppResult<Ticket> {
    let first = pulls.first();
    let author = first
        .map(|p| w.get::<model::Member>(store::MEMBERS, &model::Member::key(&p.author)))
        .transpose()?
        .flatten();
    let (name, reason) = if let Some(m) = author.filter(|m| {
        m.kind == MemberKind::Agent
            && active.is_none_or(|a| {
                a.iter()
                    .any(|n| crate::util::fold(n) == crate::util::fold(&m.name))
            })
    }) {
        (
            m.name,
            format!(
                "the author of the first pull request after the last verified commit (!{}).",
                first.unwrap().number
            ),
        )
    } else {
        let responder = super::responder_for(w, repo, active)?;
        let why = first.map_or_else(
            || {
                "no commit after the last verified commit is from a pull request of this server"
                    .into()
            },
            |p| {
                format!(
                    "the first pull request after the last verified commit (!{}) is not by an active agent",
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
                                && active.is_none_or(|a| {
                                    a.iter()
                                        .any(|n| crate::util::fold(n) == crate::util::fold(&m.name))
                                })
                                && crate::util::fold(&m.name) == crate::util::fold(&p.author)
                        })
                });
            reason.push_str(&format!(
                " No default responder is chosen for this repository; {} {}.",
                text(&responder.member),
                if has_merged {
                    "authored its most recently merged pull request by a member"
                } else {
                    "is its first active agent by person_id"
                }
            ));
        }
        (responder.member, reason)
    };
    if name.is_empty() {
        return Err(crate::error::AppError::unavailable(
            "No active agent is available for repair work.",
        ));
    }
    body.push_str(&format!("\n## Assignee\n\n{}: {reason}\n", text(&name)));
    if pending.branch.is_none() {
        body.push_str("\nHints from running the failed jobs on the commits in between are added as comments. They do not change the assignee.\n");
    }
    let branch = pending.branch.as_deref().unwrap_or(&repo.default_branch);
    let scope = if pending.branch.is_none() {
        "Main"
    } else {
        "Collection branch"
    };
    let now = crate::util::now();
    let number = w.next(&format!("ticket:{}", repo.id))?;
    let t = Ticket {
        repo: repo.id.clone(),
        workspace: repo.workspace.clone(),
        number,
        title: format!(
            "Repair {}: verification failed ({})",
            branch,
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
            name: name.clone(),
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
            branch: pending.branch.clone(),
            notice: Some(format!(
                "{scope} verification failed: {} {} {} ({})\nRange: {}..{}, {} pull request(s): {}\nRepair ticket: {}, assigned to {}",
                repo.path,
                branch,
                short(&pending.sha),
                pending
                    .failures
                    .iter()
                    .map(|f| f.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", "),
                verified,
                short(&pending.sha),
                pulls.len(),
                pulls
                    .iter()
                    .map(|p| format!("!{}", p.number))
                    .collect::<Vec<_>>()
                    .join(" "),
                tickets::ticket_url(base_url, repo, number),
                name
            )),
            jobs: pending.failures.iter().map(|f| f.name.clone()).collect(),
            reported: pending.sha.clone(),
        }),
        created_at: now.clone(),
        updated_at: now,
        closed_at: None,
        started_at: None,
        revision: 1,
    };
    tickets::write_ticket(w, None, &t)?;
    if pending.branch.is_none()
        && let Some(v) = verify::latest_verified(w, &repo.id)?
    {
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
    let (id, branch) = (p.repo.clone(), p.branch.clone());
    let old = app
        .store
        .read(move |r| open_ticket_for(r, &id, branch.as_deref()))
        .await?;
    // Existing repairs need no new assignee: roster failures cannot suppress updates.
    let active = if old.is_none() {
        let roster = super::active_agents(app)?;
        if let Some(names) = &roster {
            crate::domain::members::ensure_agents(
                app,
                names
                    .iter()
                    .map(|(id, name)| (id.clone(), name.clone()))
                    .collect(),
            )
            .await?;
        }
        roster.map(|names| names.into_keys().collect::<Vec<_>>())
    } else {
        None
    };
    let mut lines = Vec::new();
    let mut merged = Vec::new();
    let mut failure_details = String::new();
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
            let previous_lines = lines.len();
            let root = format!(
                "{}{}",
                app.base_url,
                crate::util::repository_page(&repo.path)
            );
            let link = f.attempt_id.as_ref().map_or_else(
                || format!("{root}/commits/{}", p.sha),
                |id| format!("{root}/jobs/{id}"),
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
                let description = if p.branch.is_some() {
                    "Still failing at this commit"
                } else {
                    "Still failing after its failed jobs were retried"
                };
                lines.push(format!("- {description}: {job} — {link}"));
            }
            if lines.len() != previous_lines && f.attempt_id.is_some() {
                failure_details.push_str(&failed_job(app, &repo, &root, &p.sha, f).await?);
            }
        }
    }
    let evidence = if old.is_none() {
        Some(evidence(app, &repo, &p).await?)
    } else {
        None
    };
    let path = repo.path.clone();
    let base_url = app.base_url.clone();
    let changed = app
        .store
        .write(move |w| {
            let key = p.key();
            if w.get::<Pending>(store::REPAIR_PENDING, &key)?.is_none() {
                return Ok(None);
            }
            let current = open_ticket_for(w, &p.repo, p.branch.as_deref())?;
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
                            "Verification of {} failed again at {}.\n\n{}\n{failure_details}",
                            text(p.branch.as_deref().unwrap_or(&repo.default_branch)),
                            short(&p.sha),
                            lines.join("\n")
                        ),
                        None,
                    )?;
                    tickets::write_ticket(w, Some(&old), &t)?;
                    Some(t.number)
                }
            } else {
                let (body, pulls, verified) = evidence.unwrap();
                Some(
                    create(
                        w,
                        &repo,
                        &p,
                        body,
                        &pulls,
                        &verified,
                        &base_url,
                        active.as_deref(),
                    )?
                    .number,
                )
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
        serde_json::from_value(json!({"id":"repo","path":"acme/demo","workspace":"ws","default_branch":"main","protected_branches":[],"required_approvals":0,"auto_land":false,"hold_while_failing":false,"review_rounds":3,"ci_network":false,"labels":[],"revision":1,"created_at":"0"})).unwrap()
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
    async fn creation_chooses_only_active_agents_and_never_takes_over() {
        let dir = tempfile::tempdir().unwrap();
        let store = store::Store::open(&dir.path().join("test.redb"), 1 << 20, 1).unwrap();
        store.write(|w| {
            let mut repo=repo();
            w.put(store::REPOS,&Repo::key(&repo.id),&repo)?;
            for (name,kind) in [("owner",MemberKind::Owner),("absent",MemberKind::Agent),("bob",MemberKind::Agent),("alice",MemberKind::Agent),("runner",MemberKind::Runner)] {
                crate::domain::members::insert_member(w,&crate::domain::members::NewMember{name:name.into(),display_name:None,kind})?;
            }
            let active=vec!["bob".into(),"alice".into()];
            let pending=Pending{repo:repo.id.clone(),branch:None,sha:"failed0".into(),failures:vec![]};
            for responder in ["owner","absent","runner","unknown","bob"] {
                repo.responder=Some(responder.into());
                for author in ["owner","absent","runner","unknown","bob"] {
                    let pull:Pull=serde_json::from_value(json!({"repo":"repo","number":1,"title":"test","body":"","state":"closed","merged":true,"draft":false,"author":author,"head":"feature","base":"main","reviewers":[],"tickets":[],"labels":[],"reviewed_by":[],"ready_at":null,"created_at":"0","updated_at":"0","closed_at":null,"merged_at":"1","merge_sha":null,"merged_head":null,"merged_base":null,"upstream":null,"revision":1})).unwrap();
                    let t=create(w,&repo,&pending,String::new(),&[pull],"verified","http://example.test",Some(&active))?;
                    assert_eq!(t.assignee.as_ref().unwrap().name,if author=="bob" || responder=="bob" {"bob"} else {"alice"});
                    assert!(comments(w,&t).is_empty(),"no takeover comment is needed");
                    passed(w,&repo.id,"passed0")?;
                }
            }
            assert!(create(w,&repo,&pending,String::new(),&[],"verified","http://example.test",Some(&[])).is_err());
            Ok(())
        }).await.unwrap();
    }

    #[tokio::test]
    async fn hints_before_and_after_creation_are_unique_and_leave_ticket_unchanged() {
        for (state, has_hint) in [
            ("found", true),
            ("outside", true),
            ("inconsistent", true),
            ("not_reproduced", true),
            ("undetermined", false),
            ("abandoned", false),
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
                    let pending = Pending { repo: repo.id.clone(), branch: None, sha: n.failed_sha.clone(), failures: vec![verify::FailedJob { name: n.job.clone(), attempt_id: None, details: None }] };
                    let t = create(w, &repo, &pending, "Evidence".into(), &[], "aaaaaaa", "http://example.test", None)?;
                    assert!(t.body.contains("Hints from running the failed jobs"));
                    assert_eq!(comments(w, &t).len(), usize::from(before && has_hint));
                    if !before { save(w)?; }
                    for _ in 0..2 {
                        if let Some(h) = super::super::Hint::from_conclusion(&n) {
                            super::super::hint(w, &repo.id, &n.failed_sha, &n.job, h)?;
                        }
                    }
                    let cs = comments(w, &t);
                    assert_eq!(cs.len(), usize::from(has_hint));
                    if let Some(c) = cs.first() {
                        assert_eq!(c.author, "branvil");
                        assert_eq!(c.meta, Some(json!({"repair_hint":{"sha":n.failed_sha,"job":"test"}})));
                        match state {
                            "found" => assert!(c.body.contains("passes at aaaaaaa and fails at bbbbbbb, the first commit where it fails (not from a pull request of this server)")),
                            "outside" => assert!(c.body.contains("also fails at aaaaaaa, the last verified commit")),
                            "inconsistent" => assert!(c.body.contains("aaaaaaa: success [Job](/workspaces/acme/repos/demo/jobs/pass), bbbbbbb: failure [Job](/workspaces/acme/repos/demo/jobs/fail)")),
                            "not_reproduced" => assert_eq!(c.body, "Hint: Main verification marked test as failed at fffffff. Narrowing observed no failing result for the job and stopped after two successful diagnostic runs at that commit."),
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
    use std::sync::{Arc, Mutex};

    fn app(path: &std::path::Path) -> crate::app::Shared {
        let config = crate::config::Config::load(None, path).unwrap();
        config.layout().create().unwrap();
        let store = store::Store::open(&config.layout().db(), 1 << 20, 1).unwrap();
        let vault = crate::test_support::FileVault(path.join("vault"));
        App::new(
            config,
            "http://127.0.0.1:7878".into(),
            store,
            Arc::new(vault),
            "http://127.0.0.1:7878".into(),
        )
    }

    #[tokio::test]
    async fn creation_delivery_retry_restart_and_closure_keep_one_notice() {
        let dir = tempfile::tempdir().unwrap();
        let app = app(dir.path());
        let repo: Repo = serde_json::from_value(json!({"id":"repo","path":"acme/demo","workspace":"ws","default_branch":"main","protected_branches":[],"required_approvals":0,"auto_land":false,"hold_while_failing":false,"review_rounds":3,"ci_network":false,"responder":null,"labels":[],"upstream":null,"revision":1,"created_at":"0"})).unwrap();
        app.store.write_sync(|w| {
            w.put(store::REPOS, &Repo::key(&repo.id), &repo)?;
            let m: model::Member = serde_json::from_value(json!({"name":"agent","display_name":"Agent","kind":"agent","accounts":[],"reviewers":[],"created_at":"0","avatar":null})).unwrap();
            w.put(store::MEMBERS, &model::Member::key(&m.name), &m)
        }).unwrap();
        let pulls: Vec<Pull> = [12,13].map(|n| serde_json::from_value(json!({"repo":"repo","number":n,"title":"test","body":"","state":"closed","merged":true,"draft":false,"author":"agent","head":"feature","base":"main","reviewers":[],"tickets":[],"labels":[],"reviewed_by":[],"ready_at":null,"created_at":"0","updated_at":"0","closed_at":null,"merged_at":"1","merge_sha":null,"merged_head":null,"merged_base":null,"upstream":null,"revision":1})).unwrap()).to_vec();
        let pending = Pending {
            repo: repo.id.clone(),
            sha: "abcdef0123".into(),
            branch: None,
            failures: vec![verify::FailedJob {
                name: "test".into(),
                attempt_id: None,
                details: None,
            }],
        };
        let ticket = app
            .store
            .write_sync(|w| {
                create(
                    w,
                    &repo,
                    &pending,
                    "body".into(),
                    &pulls,
                    "1234567",
                    "http://127.0.0.1:7878",
                    None,
                )
            })
            .unwrap();
        assert_eq!(
            ticket.repair.as_ref().unwrap().notice.as_deref(),
            Some(
                "Main verification failed: acme/demo main abcdef0 (test)\nRange: 1234567..abcdef0, 2 pull request(s): !12 !13\nRepair ticket: http://127.0.0.1:7878/workspaces/acme/repos/demo/tickets/1, assigned to agent"
            )
        );
        async fn sweep(app: &App) {
            crate::ci::notify::sweep(app).await.unwrap();
        }
        fn indexed(app: &App) -> Vec<Vec<u8>> {
            app.store
                .read_sync(|r| r.prefix_keys(store::IX_REPAIR_UNNOTIFIED, &[]))
                .unwrap()
        }
        let index = indexed(&app);
        assert_eq!(index.len(), 1);
        app.store.write_sync(model::rebuild_indexes).unwrap();
        assert_eq!(indexed(&app), index);
        sweep(&app).await;
        assert_eq!(indexed(&app), index, "unset webhook retains notice");
        let received = Arc::new(Mutex::new(Vec::<serde_json::Value>::new()));
        let copy = received.clone();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/webhook", listener.local_addr().unwrap());
        let router = axum::Router::new().route(
            "/webhook",
            axum::routing::post(move |axum::Json(v): axum::Json<serde_json::Value>| {
                let copy = copy.clone();
                async move {
                    let mut items = copy.lock().unwrap();
                    items.push(v);
                    if items.len() == 1 {
                        axum::http::StatusCode::SERVICE_UNAVAILABLE
                    } else {
                        axum::http::StatusCode::OK
                    }
                }
            }),
        );
        let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        app.secrets.set(app.secrets.webhook(), url).await.unwrap();
        sweep(&app).await;
        assert_eq!(received.lock().unwrap().len(), 1);
        assert_eq!(indexed(&app), index, "503 retains notice");
        // Close while delivery is pending: acknowledgment must preserve closure.
        app.store
            .write_sync(|w| passed(w, &repo.id, "passed"))
            .unwrap();
        assert_eq!(indexed(&app), index);
        sweep(&app).await;
        sweep(&app).await;
        assert_eq!(received.lock().unwrap().len(), 2);
        assert_eq!(
            received.lock().unwrap()[1]["text"],
            ticket.repair.unwrap().notice.unwrap()
        );
        assert!(indexed(&app).is_empty());
        let t: Ticket = app
            .store
            .read_sync(|r| Ok(r.get(store::TICKETS, &Ticket::key(&repo.id, 1))?.unwrap()))
            .unwrap();
        assert_eq!(t.state, "closed");
        assert!(t.repair.unwrap().notice.is_none());
        drop(app);
        let app = self::app(dir.path());
        sweep(&app).await;
        app.store.write_sync(model::rebuild_indexes).unwrap();
        assert!(indexed(&app).is_empty());
        assert_eq!(received.lock().unwrap().len(), 2);
        let second = app
            .store
            .write_sync(|w| {
                create(
                    w,
                    &repo,
                    &pending,
                    "body".into(),
                    &[],
                    "no verified commit",
                    "http://127.0.0.1:7878",
                    None,
                )
            })
            .unwrap();
        assert!(
            second
                .repair
                .unwrap()
                .notice
                .unwrap()
                .contains("Range: no verified commit..abcdef0, 0 pull request(s): ")
        );
        sweep(&app).await;
        sweep(&app).await;
        assert_eq!(received.lock().unwrap().len(), 3);
        // Protected CI alone has no notification lifecycle, including recovery.
        for (sha, success) in [("failed-only", false), ("recovered-only", true)] {
            app.store.write_sync(|w| {
                queue::register(w, queue::Registration {
                    repo: repo.id.clone(), repo_path: repo.path.clone(), sha: sha.into(),
                    tree: None, branch: "protected".into(), protected: true,
                    pipeline: queue::PipelineState::Jobs { jobs: vec![serde_json::from_value(json!({
                        "name":"test", "image":"alpine", "commands":["true"],
                        "timeout_secs":null, "tmpfs_mib":null, "ram_workspace":null, "artifacts":[], "cache":null, "paths":[], "verified_sha":false
                    })).unwrap()] }, network: false, order: vec![], inputs: Default::default(), selection: None
                }, 10, "2026-01-01T00:00:00Z")?;
                let mut number = 0;
                while let Some(c) = queue::claim(w, "runner", 60, "2026-01-01T00:00:00Z")? {
                    queue::finish(w, &c.attempt.id, &c.lease, if success && number > 0 { "success" } else { "failure" }, None, queue::ResultInfo::default(), "2026-01-01T00:00:00Z")?;
                    number += 1;
                }
                assert_eq!(number, if success { 2 } else { 4 });
                Ok(())
            }).unwrap();
            sweep(&app).await;
            assert_eq!(received.lock().unwrap().len(), 3);
        }
        task.abort();
    }
}
