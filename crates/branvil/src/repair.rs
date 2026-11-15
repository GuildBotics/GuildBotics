//! The repository's default repair ticket assignee.
use crate::error::{AppError, AppResult};
use crate::model::{Member, MemberKind, Pull, Reader, Repo};
use crate::store::{self, Rd};

/// Resolve the explicit choice or pick deterministically from current records.
pub fn responder(r: &impl Rd, repo: &Repo) -> AppResult<Reader> {
    if let Some(name) = &repo.responder {
        return Ok(Reader {
            member: name.clone(),
            chosen: true,
        });
    }
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
    Ok(Reader {
        member: picked.name.clone(),
        chosen: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[tokio::test]
    async fn automatic_order_and_explicit_override() {
        let dir = tempfile::tempdir().unwrap();
        let s = store::Store::open(&dir.path().join("test.redb"), 1 << 20, 1).unwrap();
        let mut repo: Repo = serde_json::from_value(json!({"id":"repo","path":"acme/demo","workspace":"ws","default_branch":"main","protected_branches":[],"required_checks":[],"required_approvals":0,"auto_land":false,"ci_network":false,"responder":null,"labels":[],"upstream":null,"revision":1,"created_at":"0"})).unwrap();
        async fn check(s: &store::Store, repo: &Repo, name: &str, chosen: bool) {
            let repo = repo.clone();
            assert_eq!(
                s.read(move |r| responder(r, &repo)).await.unwrap(),
                Reader {
                    member: name.into(),
                    chosen
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
        check(&s, &repo, "owner", false).await;
        member(&s, "new", MemberKind::Agent, "2").await;
        member(&s, "old", MemberKind::Agent, "1").await;
        check(&s, &repo, "old", false).await;
        s.write(|w| {
            for (n,r,a,t,merged) in [(1,"repo","new","3",true),(2,"repo","old","2",true),(3,"repo","owner","4",true),(4,"other","old","5",true),(5,"repo","old","6",false)] {
                let p: Pull = serde_json::from_value(json!({"repo":r,"number":n,"title":"test","body":"","state":"closed","merged":merged,"draft":false,"author":a,"head":"feature","base":"main","reviewers":[],"tickets":[],"labels":[],"reviewed_by":[],"ready_at":null,"created_at":"0","updated_at":"0","closed_at":null,"merged_at":t,"merge_sha":null,"merged_head":null,"merged_base":null,"upstream":null,"revision":1})).unwrap();
                w.put(store::PULLS,&Pull::key(r,n),&p)?;
            }
            Ok(())
        }).await.unwrap();
        check(&s, &repo, "new", false).await;
        repo.responder = Some("old".into());
        check(&s, &repo, "old", true).await;
    }
}
