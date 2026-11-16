//! Diagnostic first-parent search. Git reads happen outside short store writes;
//! callers serialize both with the repository lock.
use super::{Ci, pipeline::JobDef, queue, verify::Verification};
use crate::error::AppResult;
use crate::model::{IxEntry, Repo, put_indexed};
use crate::store::{self, Key, Rd, Wtx};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Result {
    pub attempt: Option<String>,
    pub outcome: String,
    pub probe: bool,
}
impl Result {
    fn passes(&self) -> Option<bool> {
        match self.outcome.as_str() {
            "success" | "not_run" => Some(true),
            "failure" | "timed_out" => Some(false),
            _ => None,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Narrowing {
    pub repo: String,
    pub base: String,
    pub job: String,
    pub failed_sha: String,
    /// Includes c0, followed by first-parent commits through cn.
    pub commits: Vec<String>,
    /// False until Git supplies the full history; initially only the endpoints are stored.
    pub history_ready: bool,
    pub results: Vec<Vec<Result>>,
    pub pending: Option<(usize, String)>,
    pub conclusion: String,
    pub boundary: Option<(String, String)>,
    pub reason: Option<String>,
    pub started_at: String,
    pub finished_at: Option<String>,
}
impl Narrowing {
    fn key(&self) -> Vec<u8> {
        key(&self.repo, &self.base, &self.job)
    }
}
fn key(repo: &str, base: &str, job: &str) -> Vec<u8> {
    Key::new().s(repo).s(base).s(job).bytes()
}
fn index(n: &Narrowing) -> Vec<IxEntry> {
    if n.conclusion != "running" {
        return vec![];
    }
    vec![IxEntry {
        table: store::IX_CI_NARROWING,
        key: n.key(),
        value: n.key(),
    }]
}
fn put(w: &Wtx, n: &Narrowing) -> AppResult<()> {
    let old: Option<Narrowing> = w.get(store::CI_NARROWINGS, &n.key())?;
    put_indexed(
        w,
        store::CI_NARROWINGS,
        &n.key(),
        old.as_ref().map(index),
        n,
        index(n),
    )
}
pub fn expected_index(r: &impl Rd) -> AppResult<Vec<IxEntry>> {
    Ok(r.prefix_all::<Narrowing>(store::CI_NARROWINGS, &[])?
        .iter()
        .flat_map(index)
        .collect())
}
pub fn concluded(r: &impl Rd, repo: &str, base: &str, job: &str) -> AppResult<Option<Narrowing>> {
    Ok(
        r.get::<Narrowing>(store::CI_NARROWINGS, &key(repo, base, job))?
            .filter(|n| n.conclusion != "running"),
    )
}
fn running(r: &impl Rd, repo: &str) -> AppResult<Vec<Narrowing>> {
    r.prefix_raw(
        store::IX_CI_NARROWING,
        &Key::new().s(repo).bytes(),
        None,
        usize::MAX,
    )?
    .into_iter()
    .map(|(_, k)| {
        r.get(store::CI_NARROWINGS, &k)?
            .ok_or_else(|| crate::error::AppError::internal("missing narrowing"))
    })
    .collect()
}
/// The only transaction body that records a conclusion. Repair hints attach here.
fn conclude(
    w: &Wtx,
    n: &mut Narrowing,
    state: &str,
    reason: Option<String>,
    now: &str,
) -> AppResult<()> {
    n.conclusion = state.into();
    n.reason = reason;
    n.finished_at = Some(now.into());
    put(w, n)?;
    if let Some(hint) = crate::repair::Hint::from_conclusion(n) {
        crate::repair::hint(w, &n.repo, &n.failed_sha, &n.job, hint)?;
    }
    Ok(())
}
pub(super) fn start(w: &Wtx, repo: &str, v: &Verification, now: &str) -> AppResult<()> {
    let Some(base) = &v.base else {
        return Ok(());
    };
    for job in &v.failed_jobs {
        if job.attempt_id.is_none()
            || w.get_raw(store::CI_NARROWINGS, &key(repo, base, &job.name))?
                .is_some()
        {
            continue;
        }
        let mut n = Narrowing {
            repo: repo.into(),
            base: base.clone(),
            job: job.name.clone(),
            failed_sha: v.sha.clone(),
            commits: vec![base.clone(), v.sha.clone()],
            history_ready: false,
            results: vec![vec![], vec![]],
            pending: None,
            conclusion: "running".into(),
            boundary: None,
            reason: None,
            started_at: now.into(),
            finished_at: None,
        };
        if let Some(last) = n.results.last_mut() {
            *last = queue::attempts_of(w, repo, &v.sha)?
                .into_iter()
                .filter(|a| {
                    a.purpose == queue::Purpose::Check
                        && a.job.name == job.name
                        && a.status == "completed"
                })
                .filter_map(|a| {
                    a.conclusion.map(|outcome| Result {
                        attempt: Some(a.id),
                        outcome,
                        probe: false,
                    })
                })
                .collect();
        }
        put(w, &n)?;
    }
    Ok(())
}
pub(super) fn abandon(w: &Wtx, repo: &str, now: &str) -> AppResult<()> {
    for mut n in running(w, repo)? {
        if let Some((pos, id)) = &n.pending
            && let Some(a) = queue::get_attempt(w, id)?
        {
            for a in queue::attempts_of(w, repo, &n.commits[*pos])?
                .into_iter()
                .filter(|b| b.purpose == queue::Purpose::Probe && b.seq == a.seq)
            {
                queue::cancel(w, &a.id, "main verification passed", now)?;
            }
        }
        conclude(w, &mut n, "abandoned", None, now)?;
    }
    Ok(())
}

enum Next {
    Probe(usize),
    Done(&'static str, Option<(usize, usize)>, Option<String>),
}
fn decide(n: &Narrowing) -> Next {
    let values: Vec<Vec<bool>> = n
        .results
        .iter()
        .map(|rs| rs.iter().filter_map(Result::passes).collect())
        .collect();
    // c0 is always the first actual observation, independent of cn's history.
    if values[0].is_empty() {
        return Next::Probe(0);
    }
    if !values[0][0] {
        if values[0].len() < 2 {
            return Next::Probe(0);
        }
        return Next::Done(
            if values[0][1] {
                "inconsistent"
            } else {
                "outside"
            },
            None,
            None,
        );
    }
    let mut failed = false;
    for rs in &values {
        if rs.contains(&true) && (failed || rs.contains(&false)) {
            return Next::Done("inconsistent", None, None);
        }
        failed |= rs.contains(&false);
    }
    let Some(f) = values.iter().position(|v| v.contains(&false)) else {
        return Next::Probe(values.len() - 1);
    };
    let p = (0..f)
        .rev()
        .find(|&i| values[i].contains(&true))
        .unwrap_or(0);
    if f - p > 1 {
        return Next::Probe(p + (f - p) / 2);
    }
    for i in [p, f] {
        if values[i].len() < 2 && !n.results[i].iter().any(|r| r.outcome == "not_run") {
            return Next::Probe(i);
        }
    }
    Next::Done("found", Some((p, f)), None)
}

impl Ci {
    pub(super) async fn narrowing_commits(
        &self,
        repo: &Repo,
        base: &str,
        sha: &str,
    ) -> AppResult<Vec<String>> {
        let dir = self.inner.git.repo_dir(&repo.path);
        let history = self
            .inner
            .git
            .run_ok(
                Some(&dir),
                &[
                    "rev-list",
                    "--first-parent",
                    "--reverse",
                    &format!("{base}..{sha}"),
                    "--",
                ],
            )
            .await?;
        Ok(std::iter::once(base.to_owned())
            .chain(history.lines().map(str::to_owned))
            .collect())
    }
    pub(super) async fn advance_narrowings(&self, repo: &Repo) -> AppResult<()> {
        let id = repo.id.clone();
        let ns = self.inner.store.read(move |r| running(r, &id)).await?;
        for mut n in ns {
            if !n.history_ready {
                let commits = self.narrowing_commits(repo, &n.base, &n.failed_sha).await?;
                let failures = n.results.remove(1);
                n.results = vec![vec![]; commits.len()];
                *n.results.last_mut().expect("history includes the base") = failures;
                n.commits = commits;
                n.history_ready = true;
                let saved = n.clone();
                self.inner.store.write(move |w| put(w, &saved)).await?;
            }
            // An interrupted attempt can have a replacement queued at its old sequence.
            if let Some((pos, id)) = n.pending.clone() {
                let copy = n.clone();
                let attempts = self
                    .inner
                    .store
                    .read(move |r| {
                        let Some(a) = queue::get_attempt(r, &id)? else {
                            return Ok(vec![]);
                        };
                        let mut chain: Vec<_> =
                            queue::attempts_of(r, &copy.repo, &copy.commits[pos])?
                                .into_iter()
                                .filter(|b| b.purpose == queue::Purpose::Probe && b.seq == a.seq)
                                .collect();
                        chain.sort_by_key(|a| a.number);
                        Ok(chain)
                    })
                    .await?;
                let mut active = None;
                for a in attempts {
                    if a.status != "completed" {
                        active = Some(a.id);
                        break;
                    }
                    if !n.results[pos]
                        .iter()
                        .any(|r| r.attempt.as_ref() == Some(&a.id))
                    {
                        n.results[pos].push(Result {
                            attempt: Some(a.id),
                            outcome: a.conclusion.unwrap_or_else(|| "unknown".into()),
                            probe: true,
                        });
                    }
                }
                n.pending = active.map(|id| (pos, id));
                if n.pending.is_some() {
                    self.inner.store.write(move |w| put(w, &n)).await?;
                    continue;
                }
            }
            loop {
                let now = crate::util::now();
                let invalid = n.results.iter().find(|rs| {
                    rs.len() >= 2
                        && rs[rs.len() - 2..]
                            .iter()
                            .all(|r| r.probe && r.passes().is_none())
                });
                let next = if invalid.is_some() {
                    Next::Done(
                        "undetermined",
                        None,
                        Some("two consecutive attempts produced no pass/fail result".into()),
                    )
                } else {
                    decide(&n)
                };
                match next {
                    Next::Done(state, boundary, reason) => {
                        n.boundary =
                            boundary.map(|(p, f)| (n.commits[p].clone(), n.commits[f].clone()));
                        self.inner
                            .store
                            .write(move |w| conclude(w, &mut n, state, reason, &now))
                            .await?;
                        break;
                    }
                    Next::Probe(pos) => {
                        let pipeline = self.read_pipeline(repo, &n.commits[pos]).await?;
                        let job: Option<JobDef> = match pipeline {
                            queue::PipelineState::Jobs { jobs } => {
                                jobs.into_iter().find(|j| j.name == n.job)
                            }
                            _ => None,
                        };
                        let Some(job) = job else {
                            n.results[pos].push(Result {
                                attempt: None,
                                outcome: "not_run".into(),
                                probe: false,
                            });
                            continue;
                        };
                        let repo = repo.clone();
                        let max = self.inner.config.queue_max;
                        self.inner
                            .store
                            .write(move |w| {
                                if let Some(a) =
                                    queue::probe(w, &repo, &n.commits[pos], &job, max, &now)?
                                {
                                    n.pending = Some((pos, a.id));
                                }
                                put(w, &n)
                            })
                            .await?;
                        break;
                    }
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample(results: &[&[&str]]) -> Narrowing {
        Narrowing {
            repo: "repo".into(),
            base: "c0".into(),
            job: "test".into(),
            failed_sha: "cn".into(),
            commits: (0..results.len()).map(|i| format!("c{i}")).collect(),
            history_ready: true,
            results: results
                .iter()
                .map(|rs| {
                    rs.iter()
                        .map(|s| Result {
                            attempt: Some(format!("attempt-{s}")),
                            outcome: (*s).into(),
                            probe: true,
                        })
                        .collect()
                })
                .collect(),
            pending: None,
            conclusion: "running".into(),
            boundary: None,
            reason: None,
            started_at: "now".into(),
            finished_at: None,
        }
    }
    #[test]
    fn search_rules_cover_outside_inconsistent_missing_and_adjacent_boundaries() {
        for (results, expected) in [
            (vec![vec!["failure", "failure"], vec!["failure"]], "outside"),
            (
                vec![vec!["failure", "success"], vec!["failure"]],
                "inconsistent",
            ),
            (
                vec![vec!["success"], vec!["failure", "success"], vec!["failure"]],
                "inconsistent",
            ),
            (
                vec![vec!["success"], vec!["failure"], vec!["success"]],
                "inconsistent",
            ),
            (vec![vec!["not_run"], vec!["failure", "timed_out"]], "found"),
        ] {
            let refs: Vec<Vec<&str>> = results;
            let rs: Vec<&[&str]> = refs.iter().map(Vec::as_slice).collect();
            let Next::Done(actual, _, _) = decide(&sample(&rs)) else {
                panic!("expected conclusion");
            };
            assert_eq!(actual, expected);
        }
        assert!(matches!(
            decide(&sample(&[&[], &["failure"]])),
            Next::Probe(0)
        ));
        assert!(matches!(
            decide(&sample(&[&["failure"], &["failure"]])),
            Next::Probe(0)
        ));
    }
    #[tokio::test]
    async fn conclusion_transaction_attaches_hint_and_resolves_local_pull() {
        let dir = tempfile::tempdir().unwrap();
        let store = store::Store::open(&dir.path().join("test.redb"), 1 << 20, 1).unwrap();
        store.write(|w| {
            let (_temp, _ci, mut repo) = super::super::tests::setup();
            repo.id = "repo".into();
            w.put(store::REPOS, &crate::model::Repo::key(&repo.id), &repo)?;
            let t: crate::model::Ticket = serde_json::from_value(serde_json::json!({
                "repo":"repo","workspace":"ws","number":1,"title":"repair","body":"","mentions":[],"state":"open","author":"branvil","labels":[],"interactive":false,"lane":"ready","rank":"0|a","blocked_by":[],"created_at":"0","updated_at":"0","revision":1,"repair":{"jobs":["test"],"reported":"cn"}
            })).unwrap();
            crate::domain::tickets::write_ticket(w, None, &t)?;
            let p: crate::model::Pull = serde_json::from_value(serde_json::json!({"repo":"repo","number":2,"title":"change","body":"","state":"closed","merged":true,"draft":false,"author":"agent","head":"feature","base":"main","reviewers":[],"tickets":[],"labels":[],"reviewed_by":[],"created_at":"0","updated_at":"0","revision":1,"merge_sha":"c1"})).unwrap();
            w.put(store::PULLS, &crate::model::Pull::key("repo", 2), &p)?;
            let mut n = sample(&[&["success"], &["failure"]]);
            n.boundary = Some(("c0".into(), "c1".into()));
            conclude(w, &mut n, "found", None, "now")?;
            assert_eq!(concluded(w, "repo", "c0", "test")?.unwrap().conclusion, "found");
            let target = crate::model::Target::Ticket { repo: "repo".into(), number: 1 };
            let comments = crate::domain::tickets::comments_of(w, &target)?;
            assert_eq!(comments.len(), 1);
            assert!(comments[0].body.contains("[!2](/acme/demo/-/pulls/2) by agent"));
            assert!(comments[0].body.contains("the assignment stays"));
            Ok(())
        }).await.unwrap();
    }
}
