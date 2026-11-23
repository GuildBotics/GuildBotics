//! Diagnostic comments share the conclusion/creation transaction and own no state.
use crate::ci::narrow::Narrowing;
use crate::domain::tickets;
use crate::error::AppResult;
use crate::model::{Comment, Pull, Target};
use crate::store::{self, Key, Rd, Wtx};

pub enum Hint {
    FirstFailing {
        sha: String,
        parent: String,
    },
    FailsOnVerified {
        sha: String,
    },
    Inconsistent {
        results: Vec<(String, String, Option<String>)>,
    },
    NotReproduced,
}

impl Hint {
    pub fn from_conclusion(n: &Narrowing) -> Option<Self> {
        match n.conclusion.as_str() {
            "found" => n.boundary.as_ref().map(|(parent, sha)| Self::FirstFailing {
                sha: sha.clone(),
                parent: parent.clone(),
            }),
            "outside" => Some(Self::FailsOnVerified {
                sha: n.base.clone(),
            }),
            "inconsistent" => Some(Self::Inconsistent {
                results: n
                    .commits
                    .iter()
                    .zip(&n.results)
                    .flat_map(|(sha, rs)| {
                        rs.iter()
                            .map(|r| (sha.clone(), r.outcome.clone(), r.attempt.clone()))
                    })
                    .collect(),
            }),
            "not_reproduced" => Some(Self::NotReproduced),
            _ => None,
        }
    }
}

pub fn hint(w: &Wtx, repo: &str, failed_sha: &str, job: &str, narrowing: Hint) -> AppResult<()> {
    let Some(t) = super::open_ticket(w, repo)? else {
        return Ok(());
    };
    let target = Target::Ticket {
        repo: repo.into(),
        number: t.number,
    };
    let meta = serde_json::json!({"repair_hint":{"sha":failed_sha,"job":job}});
    if w.prefix_all::<Comment>(store::COMMENTS, &target.prefix())?
        .iter()
        .any(|c| {
            c.meta
                .as_ref()
                .and_then(|m| m.get("repair_hint"))
                .is_some_and(|h| {
                    h.get("sha").and_then(serde_json::Value::as_str) == Some(failed_sha)
                        && h.get("job").and_then(serde_json::Value::as_str) == Some(job)
                })
        })
    {
        return Ok(());
    }
    let repository = crate::domain::repo_by_id(w, repo)?;
    let root = crate::util::repository_page(&repository.path);
    let job = super::tickets::text(job);
    let short = super::tickets::short;
    let body = match narrowing {
        Hint::FirstFailing { sha, parent } => {
            let pull = w
                .prefix_all::<Pull>(store::PULLS, &Key::new().s(repo).bytes())?
                .into_iter()
                .filter(|p| p.merged && p.merge_sha.as_ref() == Some(&sha))
                .min_by_key(|p| p.number);
            let origin = pull.map_or_else(
                || "not from a pull request of this Branvil".into(),
                |p| {
                    format!(
                        "[!{}]({root}/pulls/{}) by {}",
                        p.number,
                        p.number,
                        super::tickets::text(&p.author)
                    )
                },
            );
            format!(
                "Hint: {job} passes at {} and fails at {}, the first commit where it fails ({origin}). It only helps to find the cause; the assignment stays.",
                short(&parent),
                short(&sha)
            )
        }
        Hint::FailsOnVerified { sha } => format!(
            "Hint: {job} also fails at {}, the last verified commit, which passed it before. The cause is outside the commits since: the environment, a dependency, the date or time, or an intermittent test.",
            short(&sha)
        ),
        Hint::Inconsistent { results } => {
            let results = results
                .iter()
                .map(|(sha, result, id)| {
                    let link = id
                        .as_ref()
                        .map_or_else(String::new, |id| format!(" [Job]({root}/jobs/{id})"));
                    format!("{}: {}{link}", short(sha), super::tickets::text(result))
                })
                .collect::<Vec<_>>()
                .join(", ");
            format!(
                "Hint: {job} on the commits in between does not split into passing and failing at one commit ({results}). It may be intermittent."
            )
        }
        Hint::NotReproduced => format!(
            "Hint: Main verification marked {job} as failed at {}. Narrowing observed no failing result for the job and stopped after two successful diagnostic runs at that commit.",
            short(failed_sha)
        ),
    };
    tickets::insert_comment(w, &target, "branvil", body, Some(meta))?;
    Ok(())
}
