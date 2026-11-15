//! Repair lifecycle tests use local Git and records only.
use super::{
    queue,
    tests::{app, commit_all, setup},
    verify::{FailedJob, Verification, Verified},
};
use crate::{
    model::{self, Member, MemberKind, Pull, Repo, Ticket},
    repair,
    store::{self, Key, Rd},
};
use serde_json::json;

#[tokio::test]
async fn assignments_evidence_and_lifecycle() {
    for (verified, authors, chosen, expected) in [
        (true, vec![Some("alice"), Some("bob")], None, "alice"),
        (
            true,
            vec![Some("owner"), Some("bob")],
            Some("alice"),
            "alice",
        ),
        (true, vec![None, Some("alice")], None, "alice"),
        (true, vec![None, None], Some("bob"), "bob"),
        (false, vec![Some("alice")], None, "alice"),
        (false, vec![None], None, "alice"),
        (true, vec![Some("owner"), Some("bob")], None, "bob"),
    ] {
        let (temp, ci, mut repo) = setup();
        let app = app(&temp, &ci);
        repo.responder = chosen.map(str::to_owned);
        let dir = app.git.repo_dir(&repo.path);
        app.git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        app.git
            .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        let base = commit_all(&app.git, &work, "base").await;
        let mut commits = vec![];
        for i in 0..authors.len() {
            commits.push(commit_all(&app.git, &work, &format!("commit {i}")).await);
        }
        app.git
            .run_ok(
                None,
                &[
                    "-C",
                    work.to_str().unwrap(),
                    "push",
                    "-q",
                    dir.to_str().unwrap(),
                    "HEAD:refs/heads/main",
                ],
            )
            .await
            .unwrap();
        let sha = commits.last().unwrap().clone();
        let failures = vec![
            FailedJob {
                name: "test".into(),
                attempt_id: None,
                details: Some("invalid pipeline @alice".into()),
            },
            FailedJob {
                name: "paths".into(),
                attempt_id: Some("att_00000000000000000000000000".into()),
                details: Some("failure".into()),
            },
            FailedJob {
                name: "missing".into(),
                attempt_id: None,
                details: None,
            },
        ];
        std::fs::create_dir_all(
            app.ci
                .log_path("att_00000000000000000000000000")
                .parent()
                .unwrap(),
        )
        .unwrap();
        std::fs::write(
            app.ci.log_path("att_00000000000000000000000000"),
            format!("{}\n```` @alice end", "x".repeat(9000)),
        )
        .unwrap();
        let (r2, sh, fs, cs) = (repo.clone(), sha.clone(), failures.clone(), commits.clone());
        app.store.write(move |w| {
            w.put(store::REPOS, &Repo::key(&r2.id), &r2)?;
            for (n, kind) in [("owner", MemberKind::Owner), ("alice", MemberKind::Agent), ("bob", MemberKind::Agent)] {
                let m = Member { name: n.into(), display_name: n.into(), kind, accounts: vec![], reviewers: vec![], created_at: n.into(), avatar: None };
                w.put(store::MEMBERS, &Member::key(n), &m)?;
            }
            for (i, author) in authors.iter().enumerate() {
                if let Some(author) = author {
                    let p: Pull = serde_json::from_value(json!({"repo":r2.id,"number":i+1,"title":"change @alice | title","body":"","state":"closed","merged":true,"draft":false,"author":author,"head":"feature","base":"main","reviewers":[],"tickets":[],"labels":[],"reviewed_by":[],"ready_at":null,"created_at":"0","updated_at":"0","closed_at":null,"merged_at":i.to_string(),"merge_sha":cs[i],"merged_head":cs[i],"merged_base":base,"upstream":null,"revision":1})).unwrap();
                    let jobs: Vec<super::pipeline::JobDef> = ["test", "paths"].iter().map(|name| super::pipeline::JobDef { name: (*name).into(), image: "alpine".into(), commands: vec!["true".into()], paths: vec![], verified_sha: false, timeout_secs: None, artifacts: vec![], cache: None }).collect();
                    queue::register(w, queue::Registration {order:vec![],selection:None,repo:r2.id.clone(),repo_path:r2.path.clone(),sha:cs[i].clone(),tree:None,branch:"feature".into(),protected:false,pipeline:queue::PipelineState::Jobs {jobs:jobs.clone()},network:false,inputs:Default::default()}, 100, "now")?;
                    for mut a in queue::current_attempts(w,&r2.id,&cs[i],&jobs)? {
                        let old = a.clone();
                        a.status = "completed".into(); a.conclusion=Some("success".into());
                        if a.job.name=="paths" {a.path_skip=Some(queue::PathSkip{paths:vec![],bases:vec![]});}
                        model::put_indexed(w,store::CI_ATTEMPTS,&queue::Attempt::key(&a.id),Some(queue::attempt_index(&old)),&a,queue::attempt_index(&a))?;
                    }
                    model::put_indexed(w, store::PULLS, &Pull::key(&r2.id,p.number), None, &p, model::pull_index(&p))?;
                }
            }
            if verified {
                let v = Verification { sha: sh.clone(), base: None, state: "failed".into(), started_at: "0".into(), finished_at: None, failed_jobs: fs.clone(), failing_since: Some(sh.clone()), last_verified: Some(Verified { sha: base, at: "0".into() }) };
                w.put(store::CI_VERIFICATIONS,&Key::new().s(&r2.id).bytes(),&v)?;
            }
            repair::failed(w, &r2.id, &sh, &fs)
        }).await.unwrap();
        repair::sweep(&app).await.unwrap();
        repair::sweep(&app).await.unwrap();
        let id = repo.id.clone();
        let t = app
            .store
            .read(move |r| repair::open_ticket(r, &id))
            .await
            .unwrap()
            .unwrap();
        let (view_repo, view_t) = (repo.clone(), t.clone());
        app.store
            .read(move |r| {
                assert!(
                    crate::domain::tickets::ticket_view(
                        r,
                        "http://example.test",
                        &view_repo,
                        &view_t
                    )?
                    .repair
                );
                let mut ordinary = view_t.clone();
                ordinary.repair = None;
                assert!(
                    !crate::domain::tickets::ticket_view(
                        r,
                        "http://example.test",
                        &view_repo,
                        &ordinary
                    )?
                    .repair
                );
                Ok(())
            })
            .await
            .unwrap();
        assert_eq!(t.author, "branvil");
        assert_eq!(t.lane, "ready");
        assert_eq!(t.assignee.as_ref().unwrap().name, expected);
        assert_eq!(t.repair.as_ref().unwrap().reported, sha);
        assert_eq!(
            t.repair.as_ref().unwrap().jobs,
            ["test", "paths", "missing"]
        );
        assert!(t.body.contains("`````\n"));
        assert!(t.body.contains("```` &#64;alice end"));
        assert!(!t.body.contains(&"x".repeat(8193)));
        if t.body.contains("/pulls/") {
            assert!(t.body.contains("success"));
            assert!(t.body.contains("Skipped by paths"));
        }
        assert!(t.body.contains("invalid pipeline &#64;alice"));
        assert!(t.body.contains("No record"));
        if t.body.contains("Not from a pull request of this Branvil") {
            assert!(t.body.contains("| Test |"));
        }
        assert!(!t.body.contains('@'));
        assert!(t.mentions.is_empty());
        let r2 = repo.clone();
        let sh = sha.clone();
        app.store
            .write(move |w| {
                // A closes link must wait for verification too.
                assert!(!crate::domain::tickets::close_by_merge(
                    w,
                    &(r2.id.clone(), t.number),
                    "now"
                )?);
                repair::failed(w, &r2.id, &sh, &failures)
            })
            .await
            .unwrap();
        repair::sweep(&app).await.unwrap();
        let id = repo.id.clone();
        app.store
            .read(move |r| {
                assert_eq!(r.prefix_all::<Ticket>(store::TICKETS, &[])?.len(), 1);
                assert!(r.prefix_keys(store::REPAIR_PENDING, &[])?.is_empty());
                assert!(repair::open_ticket(r, &id)?.is_some());
                Ok(())
            })
            .await
            .unwrap();
        let r2 = repo.clone();
        app.store
            .write(move |w| {
                if let Some(mut p) = w.get::<Pull>(store::PULLS, &Pull::key(&r2.id, 1))? {
                    let old = p.clone();
                    p.tickets.push(model::TicketLink {
                        url: "http://example.test/ticket/1".into(),
                        local: Some((r2.id.clone(), 1)),
                        closes: false,
                    });
                    model::put_indexed(
                        w,
                        store::PULLS,
                        &Pull::key(&r2.id, 1),
                        Some(model::pull_index(&old)),
                        &p,
                        model::pull_index(&p),
                    )?;
                }
                Ok(())
            })
            .await
            .unwrap();
        let id = repo.id.clone();
        let sh = sha.clone();
        app.store
            .write(move |w| repair::passed(w, &id, &sh))
            .await
            .unwrap();
        let id = repo.id.clone();
        app.store
            .read(move |r| {
                assert!(repair::open_ticket(r, &id)?.is_none());
                let t: Ticket = r.get(store::TICKETS, &Ticket::key(&id, 1))?.unwrap();
                assert_eq!(t.state, "closed");
                assert_eq!(t.lane, "done");
                assert_eq!(t.state_reason.as_deref(), Some("completed"));
                let comments = crate::domain::tickets::comments_of(
                    r,
                    &crate::model::Target::Ticket {
                        repo: id,
                        number: 1,
                    },
                )?;
                assert_eq!(comments.len(), 1);
                if r.get::<Pull>(store::PULLS, &Pull::key(&t.repo, 1))?
                    .is_some()
                {
                    assert!(
                        comments[0]
                            .body
                            .contains("Merged pull requests linked here: !1.")
                    );
                } else {
                    assert!(comments[0].body.contains("failure may be intermittent"));
                }
                Ok(())
            })
            .await
            .unwrap();
        let id = repo.id.clone();
        let sh = sha.clone();
        app.store
            .write(move |w| {
                repair::failed(w, &id, &sh, &[])?;
                repair::passed(w, &id, &sh)
            })
            .await
            .unwrap();
        repair::sweep(&app).await.unwrap();
        app.store
            .read(|r| {
                assert_eq!(r.prefix_all::<Ticket>(store::TICKETS, &[])?.len(), 1);
                Ok(())
            })
            .await
            .unwrap();
        let id = repo.id.clone();
        let sh = sha.clone();
        app.store
            .write(move |w| repair::failed(w, &id, &sh, &[]))
            .await
            .unwrap();
        repair::sweep(&app).await.unwrap();
        let id = repo.id.clone();
        app.store
            .write(move |w| {
                let old = repair::open_ticket(w, &id)?.unwrap();
                assert_eq!(old.number, 2);
                let mut closed = old.clone();
                closed.state = "closed".into();
                crate::domain::tickets::write_ticket(w, Some(&old), &closed)
            })
            .await
            .unwrap();
        let id = repo.id.clone();
        let sh = sha.clone();
        app.store
            .write(move |w| repair::failed(w, &id, &sh, &[]))
            .await
            .unwrap();
        repair::sweep(&app).await.unwrap();
        let id = repo.id.clone();
        app.store
            .read(move |r| {
                assert_eq!(repair::open_ticket(r, &id)?.unwrap().number, 3);
                Ok(())
            })
            .await
            .unwrap();
    }
}
