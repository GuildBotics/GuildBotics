//! Repair tickets and their default assignee.
mod tickets;
use crate::error::{AppError, AppResult};
use crate::model::{Member, MemberKind, Pull, Repo};
use crate::store::{self, Rd};
pub use tickets::{failed, open_ticket, passed, sweep};

/// One predicate for API, scheduling, selection and landing.
pub fn is_pull(r: &impl Rd, p: &Pull) -> AppResult<bool> {
    Ok(p.state == "open"
        && open_ticket(r, &p.repo)?.is_some_and(|t| {
            p.tickets
                .iter()
                .any(|l| l.local.as_ref() == Some(&(p.repo.clone(), t.number)))
        }))
}

/// Whether any open repair pull wants this run's commit.
pub fn is_run(r: &impl Rd, run: &crate::ci::queue::Run) -> AppResult<bool> {
    for p in r.prefix_all::<Pull>(store::PULLS, &store::Key::new().s(&run.repo).bytes())? {
        if run.branches.contains(&p.head) && is_pull(r, &p)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Failures inherited from main do not demote unrelated PR work.
pub fn failing_jobs(r: &impl Rd, repo: &str) -> AppResult<Vec<String>> {
    let Some(v) = crate::ci::verify::get(r, repo)?.filter(|v| v.failing_since.is_some()) else {
        return Ok(vec![]);
    };
    let mut jobs: Vec<_> = v.failed_jobs.into_iter().map(|j| j.name).collect();
    if let Some(t) = open_ticket(r, repo)? {
        jobs.extend(t.repair.unwrap().jobs);
    }
    Ok(jobs)
}

/// Effective repair assignee and the current automatic candidate.
#[derive(serde::Serialize, Debug, Clone, PartialEq, Eq)]
pub struct Responder {
    pub member: String,
    pub chosen: bool,
    pub automatic: String,
}

/// Resolve the effective and automatic choices together.
pub fn responder(r: &impl Rd, repo: &Repo) -> AppResult<Responder> {
    let automatic = automatic(r, repo)?;
    Ok(Responder {
        member: repo.responder.clone().unwrap_or_else(|| automatic.clone()),
        chosen: repo.responder.is_some(),
        automatic,
    })
}

fn automatic(r: &impl Rd, repo: &Repo) -> AppResult<String> {
    let members: Vec<Member> = r.prefix_all(store::MEMBERS, &[])?;
    let pulls: Vec<Pull> = r.prefix_all(store::PULLS, &store::Key::new().s(&repo.id).bytes())?;
    let latest = pulls
        .iter()
        .filter(|p| p.merged && p.merged_at.is_some())
        .filter_map(|p| {
            members
                .iter()
                .find(|m| {
                    m.kind == MemberKind::Agent
                        && crate::util::fold(&m.name) == crate::util::fold(&p.author)
                })
                .map(|m| (p, m))
        })
        .max_by(|(a, _), (b, _)| (&a.merged_at, a.number).cmp(&(&b.merged_at, b.number)))
        .map(|(_, m)| m);
    let picked = latest
        .or_else(|| {
            members
                .iter()
                .filter(|m| m.kind == MemberKind::Agent)
                .min_by(|a, b| (&a.created_at, &a.name).cmp(&(&b.created_at, &b.name)))
        })
        .or_else(|| members.iter().find(|m| m.kind == MemberKind::Owner))
        .ok_or_else(|| AppError::invalid("No owner exists."))?;
    Ok(picked.name.clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[tokio::test]
    async fn automatic_order_and_explicit_override() {
        let dir = tempfile::tempdir().unwrap();
        let s = store::Store::open(&dir.path().join("test.redb"), 1 << 20, 1).unwrap();
        let mut repo: Repo = serde_json::from_value(json!({"id":"repo","path":"acme/demo","workspace":"ws","default_branch":"main","protected_branches":[],"required_checks":[],"post_merge_jobs":[],"required_approvals":0,"auto_land":false,"ci_network":false,"responder":null,"labels":[],"upstream":null,"revision":1,"created_at":"0"})).unwrap();
        async fn check(s: &store::Store, repo: &Repo, name: &str, chosen: bool, automatic: &str) {
            let repo = repo.clone();
            assert_eq!(
                s.read(move |r| responder(r, &repo)).await.unwrap(),
                Responder {
                    member: name.into(),
                    chosen,
                    automatic: automatic.into(),
                }
            );
        }
        async fn member(s: &store::Store, name: &str, kind: MemberKind, created: &str) {
            let m = Member {
                name: name.into(),
                display_name: name.into(),
                kind,
                accounts: vec![],
                reviewers: vec![],
                created_at: created.into(),
                avatar: None,
            };
            s.write(move |w| w.put(store::MEMBERS, &Member::key(&m.name), &m))
                .await
                .unwrap();
        }
        member(&s, "owner", MemberKind::Owner, "0").await;
        member(&s, "runner", MemberKind::Runner, "0").await;
        check(&s, &repo, "owner", false, "owner").await;
        member(&s, "new", MemberKind::Agent, "2").await;
        member(&s, "old", MemberKind::Agent, "1").await;
        check(&s, &repo, "old", false, "old").await;
        s.write(|w| {
            for (n,r,a,t,merged) in [(1,"repo","new","3",true),(2,"repo","old","2",true),(3,"repo","owner","4",true),(4,"other","old","5",true),(5,"repo","old","6",false)] {
                let p: Pull = serde_json::from_value(json!({"repo":r,"number":n,"title":"test","body":"","state":"closed","merged":merged,"draft":false,"author":a,"head":"feature","base":"main","reviewers":[],"tickets":[],"labels":[],"reviewed_by":[],"ready_at":null,"created_at":"0","updated_at":"0","closed_at":null,"merged_at":t,"merge_sha":null,"merged_head":null,"merged_base":null,"upstream":null,"revision":1})).unwrap();
                w.put(store::PULLS,&Pull::key(r,n),&p)?;
            }
            Ok(())
        }).await.unwrap();
        check(&s, &repo, "new", false, "new").await;
        repo.responder = Some("old".into());
        check(&s, &repo, "old", true, "new").await;
    }
}
