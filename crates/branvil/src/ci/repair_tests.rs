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

async fn failure_fixture() -> (
    tempfile::TempDir,
    crate::app::Shared,
    Repo,
    std::path::PathBuf,
    String,
) {
    let (temp, ci, repo) = setup();
    let app = app(&temp, &ci);
    let dir = app.git.repo_dir(&repo.path);
    app.git.init_bare(&dir, "main").await.unwrap();
    let work = temp.path().join("work");
    app.git
        .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
        .await
        .unwrap();
    let base = commit_all(&app.git, &work, "base").await;
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
    let (r2, sha) = (repo.clone(), base.clone());
    app.store
        .write(move |w| {
            w.put(store::REPOS, &Repo::key(&r2.id), &r2)?;
            let m = Member {
                name: "owner".into(),
                display_name: "Owner".into(),
                kind: MemberKind::Owner,
                accounts: vec![],
                reviewers: vec![],
                created_at: "0".into(),
                avatar: None,
            };
            w.put(store::MEMBERS, &Member::key("owner"), &m)?;
            crate::domain::members::insert_member(w,&crate::domain::members::NewMember{name:"agent".into(),display_name:Some("Agent".into()),kind:MemberKind::Agent})?;
        let peer: Ticket = serde_json::from_value(json!({
            "repo":r2.id, "workspace":r2.workspace, "number":2, "title":"Existing ready card", "body":"", "mentions":[],
            "state":"open", "author":"owner", "labels":[], "interactive":false, "lane":"ready", "rank":"0|a",
            "blocked_by":[], "created_at":"0", "updated_at":"0", "revision":1
        })).unwrap();
        crate::domain::tickets::write_ticket(w, None, &peer)?;
            repair::failed(w, &r2.id, &sha, &[test_failure()])
        })
        .await
        .unwrap();
    repair::sweep(&app).await.unwrap();
    let id = repo.id.clone();
    app.store
        .read(move |r| {
            let repair = repair::open_ticket(r, &id)?.unwrap();
            let peer = r
                .get::<Ticket>(store::TICKETS, &Ticket::key(&id, 2))?
                .unwrap();
            assert!(
                repair.rank < peer.rank,
                "new repair must precede existing ready card"
            );
            Ok(())
        })
        .await
        .unwrap();
    // The manually inserted peer must reserve its number just like ordinary creation.
    let counter = Key::new().s(&format!("ticket:{}", repo.id)).bytes();
    app.store
        .write(move |w| w.put(store::COUNTERS, &counter, &2_u64))
        .await
        .unwrap();
    (temp, app, repo, work, base)
}

fn test_failure() -> FailedJob {
    FailedJob {
        name: "test".into(),
        attempt_id: None,
        details: None,
    }
}

#[tokio::test]
async fn failure_returns_to_ready_front_and_preserves_active_positions() {
    let (_temp, app, repo, _work, base) = failure_fixture().await;
    for lane in ["new", "done", "ready", "in_progress"] {
        let (id, sha) = (repo.id.clone(), base.clone());
        let (rank, other_rank) = app
            .store
            .write(move |w| {
                let old = repair::open_ticket(w, &id)?.unwrap();
                let mut other = old.clone();
                let previous = w.get::<Ticket>(store::TICKETS, &Ticket::key(&id, 2))?;
                other.number = 2;
                other.repair = None;
                other.lane = "ready".into();
                other.rank = crate::domain::tickets::rank_first(w, &old.workspace, "ready")?;
                crate::domain::tickets::write_ticket(w, previous.as_ref(), &other)?;
                let t = crate::domain::tickets::place(w, &old, lane, None, None)?;
                crate::domain::tickets::write_ticket(w, Some(&old), &t)?;
                repair::failed(w, &id, &sha, &[test_failure()])?;
                Ok((t.rank, other.rank))
            })
            .await
            .unwrap();
        repair::sweep(&app).await.unwrap();
        let id = repo.id.clone();
        app.store
            .read(move |r| {
                let t = repair::open_ticket(r, &id)?.unwrap();
                if matches!(lane, "new" | "done") {
                    assert_eq!(t.lane, "ready");
                    assert!(
                        t.rank < other_rank,
                        "{lane}: repair must precede existing ready card"
                    );
                } else {
                    assert_eq!(t.lane, lane);
                    assert_eq!(t.rank, rank);
                }
                Ok(())
            })
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn merged_repair_is_reported_only_after_inclusion_and_only_once() {
    let (_temp, app, repo, work, base) = failure_fixture().await;
    let before = commit_all(&app.git, &work, "before merge").await;
    let merged = commit_all(&app.git, &work, "repair merge").await;
    let after = commit_all(&app.git, &work, "after merge").await;
    let dir = app.git.repo_dir(&repo.path);
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
    let (id, merge_sha) = (repo.id.clone(), merged.clone());
    app.store
        .write(move |w| {
            let mut p = super::tests::open_pull(&id, 1, "fix", "main");
            p.state = "closed".into();
            p.merged = true;
            p.merge_sha = Some(merge_sha);
            p.tickets.push(model::TicketLink {
                url: "ticket".into(),
                local: Some((id.clone(), 1)),
                closes: true,
            });
            model::put_indexed(
                w,
                store::PULLS,
                &Pull::key(&id, 1),
                None,
                &p,
                model::pull_index(&p),
            )?;
            let old = repair::open_ticket(w, &id)?.unwrap();
            let t = crate::domain::tickets::place(w, &old, "done", None, None)?;
            crate::domain::tickets::write_ticket(w, Some(&old), &t)
        })
        .await
        .unwrap();
    for (sha, count, reported, lane) in [
        (before, 0, base, "done"),
        (merged.clone(), 1, merged.clone(), "ready"),
        (after, 1, merged, "ready"),
    ] {
        let id = repo.id.clone();
        app.store
            .write(move |w| repair::failed(w, &id, &sha, &[test_failure()]))
            .await
            .unwrap();
        repair::sweep(&app).await.unwrap();
        repair::sweep(&app).await.unwrap();
        let id = repo.id.clone();
        app.store
            .read(move |r| {
                let t = repair::open_ticket(r, &id)?.unwrap();
                assert_eq!(t.lane, lane);
                assert_eq!(t.repair.unwrap().reported, reported);
                let comments = crate::domain::tickets::comments_of(
                    r,
                    &model::Target::Ticket {
                        repo: id,
                        number: 1,
                    },
                )?;
                assert_eq!(comments.len(), count);
                if count > 0 {
                    assert!(
                        comments[0]
                            .body
                            .contains("Still failing after !1 (linked here) was merged: test")
                    );
                }
                Ok(())
            })
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn repeated_failures_and_repair_head_selection() {
    let (temp, ci, mut repo) = setup();
    let app = app(&temp, &ci);
    repo.post_merge_jobs = vec!["post".into()];
    let dir = app.git.repo_dir(&repo.path);
    app.git.init_bare(&dir, "main").await.unwrap();
    let work = temp.path().join("work");
    app.git
        .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
        .await
        .unwrap();
    std::fs::create_dir(work.join(".branvil")).unwrap();
    std::fs::write(work.join(".branvil/ci.toml"), "version = 1\n[[jobs]]\nname = 'post'\nimage = 'alpine'\ncommands = ['true']\n[[jobs]]\nname = 'test'\nimage = 'alpine'\ncommands = ['true']\npaths = ['src/**']\n").unwrap();
    let base = commit_all(&app.git, &work, "base").await;
    let head = commit_all(&app.git, &work, "head").await;
    app.git
        .run_ok(
            None,
            &[
                "-C",
                work.to_str().unwrap(),
                "push",
                "-q",
                dir.to_str().unwrap(),
                &format!("{base}:refs/heads/main"),
                &format!("{head}:refs/heads/fix"),
            ],
        )
        .await
        .unwrap();
    let (r2, b) = (repo.clone(), base.clone());
    app.store
        .write(move |w| {
            w.put(store::REPOS, &Repo::key(&r2.id), &r2)?;
            let m = Member {
                name: "owner".into(),
                display_name: "Owner".into(),
                kind: MemberKind::Owner,
                accounts: vec![],
                reviewers: vec![],
                created_at: "0".into(),
                avatar: None,
            };
            w.put(store::MEMBERS, &Member::key("owner"), &m)?;
            crate::domain::members::insert_member(
                w,
                &crate::domain::members::NewMember {
                    name: "agent".into(),
                    display_name: Some("Agent".into()),
                    kind: MemberKind::Agent,
                },
            )?;
            let p = super::tests::open_pull(&r2.id, 1, "fix", "main");
            model::put_indexed(
                w,
                store::PULLS,
                &Pull::key(&r2.id, 1),
                None,
                &p,
                model::pull_index(&p),
            )?;
            repair::failed(
                w,
                &r2.id,
                &b,
                &[FailedJob {
                    name: "test".into(),
                    attempt_id: None,
                    details: None,
                }],
            )
        })
        .await
        .unwrap();
    use futures_util::{FutureExt, StreamExt};
    let mut events = Box::pin(app.events.subscribe());
    repair::sweep(&app).await.unwrap();
    let mut verification_event = false;
    while let Some(Some(e)) = events.next().now_or_never() {
        if let crate::events::Notice::Change(e) = e {
            verification_event |= e.kind == "verification";
        }
    }
    assert!(verification_event);
    let state = super::verify::main_state(&app, &repo).await.unwrap();
    assert_eq!(state.repair.unwrap().number, 1);
    ci.on_pull_opened(&app, &repo, "fix").await;
    let (id, sha) = (repo.id.clone(), head.clone());
    app.store
        .read(move |r| {
            let attempts = queue::attempts_of(r, &id, &sha)?;
            assert_eq!(attempts.len(), 2);
            assert!(attempts.iter().all(|a| a.path_skip.is_some()));
            Ok(())
        })
        .await
        .unwrap();
    let (id, sha) = (repo.id.clone(), head.clone());
    app.store
        .write(move |w| {
            let mut t = repair::open_ticket(w, &id)?.unwrap();
            let old = t.clone();
            t.lane = "done".into();
            crate::domain::tickets::write_ticket(w, Some(&old), &t)?;
            let mut p = w.get::<Pull>(store::PULLS, &Pull::key(&id, 1))?.unwrap();
            let old = p.clone();
            p.tickets.push(model::TicketLink {
                url: "ticket".into(),
                local: Some((id.clone(), 1)),
                closes: true,
            });
            model::put_indexed(
                w,
                store::PULLS,
                &Pull::key(&id, 1),
                Some(model::pull_index(&old)),
                &p,
                model::pull_index(&p),
            )?;
            assert!(repair::is_pull(w, &p)?);
            repair::failed(
                w,
                &id,
                &sha,
                &[FailedJob {
                    name: "new".into(),
                    attempt_id: None,
                    details: None,
                }],
            )
        })
        .await
        .unwrap();
    ci.on_pull_opened(&app, &repo, "fix").await;
    let (id, sha) = (repo.id.clone(), head.clone());
    app.store
        .read(move |r| {
            let run = queue::get_run(r, &id, &sha)?.unwrap();
            assert!(run.selection.is_none());
            let super::queue::PipelineState::Jobs { jobs } = &run.pipeline else {
                panic!()
            };
            assert!(
                queue::current_attempts(r, &id, &sha, jobs)?
                    .iter()
                    .all(|a| a.path_skip.is_none() && a.status == "queued")
            );
            assert_eq!(
                super::priority::run_class(r, &run)?.class,
                super::priority::Class::Repair
            );
            Ok(())
        })
        .await
        .unwrap();
    repair::sweep(&app).await.unwrap();
    for retry in 0..3 {
        let (id, sha) = (repo.id.clone(), head.clone());
        app.store
            .write(move |w| {
                let t = repair::open_ticket(w, &id)?.unwrap();
                assert_eq!(t.lane, "ready");
                assert!(t.repair.unwrap().jobs.contains(&"new".to_string()));
                let comments = crate::domain::tickets::comments_of(
                    w,
                    &model::Target::Ticket {
                        repo: id.clone(),
                        number: 1,
                    },
                )?;
                assert_eq!(comments.len(), retry + 1);
                assert!(comments[0].body.contains("New failing job: new"));
                if retry < 2 {
                    repair::failed(
                        w,
                        &id,
                        &sha,
                        &[FailedJob {
                            name: "new".into(),
                            attempt_id: None,
                            details: None,
                        }],
                    )?;
                }
                Ok(())
            })
            .await
            .unwrap();
        repair::sweep(&app).await.unwrap();
        repair::sweep(&app).await.unwrap();
    }
    let (id, sha) = (repo.id.clone(), head.clone());
    let p = app
        .store
        .write(move |w| {
            for mut a in queue::attempts_of(w, &id, &sha)?
                .into_iter()
                .filter(|a| a.path_skip.is_none())
            {
                let old = a.clone();
                a.status = "completed".into();
                a.conclusion = Some(
                    if a.job.name == "post" {
                        "failure"
                    } else {
                        "success"
                    }
                    .into(),
                );
                model::put_indexed(
                    w,
                    store::CI_ATTEMPTS,
                    &queue::Attempt::key(&a.id),
                    Some(queue::attempt_index(&old)),
                    &a,
                    queue::attempt_index(&a),
                )?;
            }
            Ok(w.get::<Pull>(store::PULLS, &Pull::key(&id, 1))?.unwrap())
        })
        .await
        .unwrap();
    assert_eq!(
        crate::landing::pull_head_checks(&app, &repo, &p, &head)
            .await
            .unwrap(),
        crate::landing::HeadChecks::Failed
    );
    let tips = app.git.branch_tips(&dir).await.unwrap();
    assert!(
        crate::domain::pulls::pull_view(&app, &repo, &p, &tips)
            .await
            .unwrap()
            .repair
    );
    let mut ordinary = p.clone();
    ordinary.tickets.clear();
    assert!(
        !crate::domain::pulls::pull_view(&app, &repo, &ordinary, &tips)
            .await
            .unwrap()
            .repair
    );
    assert_eq!(
        crate::landing::pull_head_checks(&app, &repo, &ordinary, &head)
            .await
            .unwrap(),
        crate::landing::HeadChecks::Passed
    );
    let (id, sha) = (repo.id.clone(), head.clone());
    app.store
        .write(move |w| {
            for mut a in queue::attempts_of(w, &id, &sha)?
                .into_iter()
                .filter(|a| a.path_skip.is_none())
            {
                let old = a.clone();
                a.conclusion = Some("success".into());
                model::put_indexed(
                    w,
                    store::CI_ATTEMPTS,
                    &queue::Attempt::key(&a.id),
                    Some(queue::attempt_index(&old)),
                    &a,
                    queue::attempt_index(&a),
                )?;
            }
            Ok(())
        })
        .await
        .unwrap();
    assert_eq!(
        crate::landing::pull_head_checks(&app, &repo, &p, &head)
            .await
            .unwrap(),
        crate::landing::HeadChecks::Passed
    );
    let (id, sha) = (repo.id.clone(), head.clone());
    app.store
        .write(move |w| {
            let run = queue::get_run(w, &id, &sha)?.unwrap();
            for mut a in queue::attempts_of(w, &id, &sha)?
                .into_iter()
                .filter(|a| a.path_skip.is_none())
            {
                let old = a.clone();
                a.conclusion = Some("failure".into());
                model::put_indexed(
                    w,
                    store::CI_ATTEMPTS,
                    &queue::Attempt::key(&a.id),
                    Some(queue::attempt_index(&old)),
                    &a,
                    queue::attempt_index(&a),
                )?;
            }
            let mut v = Verification {
                sha: sha.clone(),
                base: None,
                state: "failed".into(),
                started_at: "0".into(),
                finished_at: None,
                failed_jobs: vec![FailedJob {
                    name: "test".into(),
                    attempt_id: None,
                    details: None,
                }],
                failing_since: Some(sha),
                last_verified: None,
            };
            for running in [false, true] {
                if running {
                    v.state = "running".into();
                    v.failed_jobs.clear();
                }
                w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &v)?;
                assert_eq!(
                    super::priority::run_class(w, &run)?.class,
                    super::priority::Class::Repair
                );
            }
            v.failing_since = None;
            w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &v)?;
            assert_eq!(
                super::priority::run_class(w, &run)?.class,
                super::priority::Class::Deferred
            );
            Ok(())
        })
        .await
        .unwrap();
    let merged = commit_all(&app.git, &work, "merge repair").await;
    app.git
        .run_ok(
            None,
            &[
                "-C",
                work.to_str().unwrap(),
                "push",
                "-q",
                dir.to_str().unwrap(),
                &format!("{merged}:refs/heads/main"),
            ],
        )
        .await
        .unwrap();
    let (id, sha) = (repo.id.clone(), merged);
    app.store
        .write(move |w| {
            let mut p = w.get::<Pull>(store::PULLS, &Pull::key(&id, 1))?.unwrap();
            let old = p.clone();
            p.state = "closed".into();
            p.merged = true;
            p.merge_sha = Some(sha.clone());
            model::put_indexed(
                w,
                store::PULLS,
                &Pull::key(&id, 1),
                Some(model::pull_index(&old)),
                &p,
                model::pull_index(&p),
            )?;
            assert!(!repair::is_pull(w, &p)?);
            let mut t = repair::open_ticket(w, &id)?.unwrap();
            let old = t.clone();
            t.lane = "in_progress".into();
            crate::domain::tickets::write_ticket(w, Some(&old), &t)?;
            repair::failed(
                w,
                &id,
                &sha,
                &[FailedJob {
                    name: "new".into(),
                    attempt_id: None,
                    details: None,
                }],
            )
        })
        .await
        .unwrap();
    repair::sweep(&app).await.unwrap();
    repair::sweep(&app).await.unwrap();
    let id = repo.id.clone();
    app.store
        .read(move |r| {
            assert_eq!(repair::open_ticket(r, &id)?.unwrap().lane, "in_progress");
            let comments = crate::domain::tickets::comments_of(
                r,
                &model::Target::Ticket {
                    repo: id,
                    number: 1,
                },
            )?;
            assert_eq!(comments.len(), 4);
            assert!(
                comments[3]
                    .body
                    .contains("Still failing after !1 (linked here) was merged")
            );
            Ok(())
        })
        .await
        .unwrap();
}

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
                name: "post".into(),
                attempt_id: None,
                details: None,
            },
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
                    let jobs: Vec<super::pipeline::JobDef> = ["test", "paths", "post"].iter().map(|name| super::pipeline::JobDef { name: (*name).into(), image: "alpine".into(), commands: vec!["true".into()], paths: vec![], verified_sha: false, timeout_secs: None, tmpfs_mib: None, artifacts: vec![], cache: None }).collect();
                    queue::register(w, queue::Registration {order:vec![],selection:None,repo:r2.id.clone(),repo_path:r2.path.clone(),sha:cs[i].clone(),tree:None,branch:"feature".into(),protected:false,pipeline:queue::PipelineState::Jobs {jobs:jobs.clone()},network:false,inputs:Default::default()}, 100, "now")?;
                    for mut a in queue::current_attempts(w,&r2.id,&cs[i],&jobs)? {
                        let old = a.clone();
                        a.status = "completed".into(); a.conclusion=Some("success".into());
                        if ["paths","post"].contains(&a.job.name.as_str()) {a.path_skip=Some(queue::PathSkip{reason:if a.job.name=="post" {queue::SkipReason::PostMerge} else {queue::SkipReason::Paths},paths:vec![],bases:vec![]});}
                        model::put_indexed(w,store::CI_ATTEMPTS,&queue::Attempt::key(&a.id),Some(queue::attempt_index(&old)),&a,queue::attempt_index(&a))?;
                    }
                    model::put_indexed(w, store::PULLS, &Pull::key(&r2.id,p.number), None, &p, model::pull_index(&p))?;
                }
            }
            if verified {
                let v = Verification { sha: sh.clone(), base: None, state: "failed".into(), started_at: "0".into(), finished_at: None, failed_jobs: fs.clone(), failing_since: Some(sh.clone()), last_verified: Some(Verified { sha: base, at: "0".into() }) };
                w.put(store::CI_VERIFICATIONS,&Key::new().s(&r2.id).bytes(),&v)?;
            }
            if let Some(mut p)=w.get::<Pull>(store::PULLS,&Pull::key(&r2.id,1))? {
                let mut broken=r2.clone(); broken.id="aaa-write".into();
                w.put(store::REPOS,&Repo::key(&broken.id),&broken)?;
                p.repo=broken.id.clone();p.author="missing-member".into();p.merge_sha=Some(sh.clone());
                // Missing authors are valid: a corrupt counter independently fails the write.
                w.put(store::COUNTERS,&Key::new().s("ticket:aaa-write").bytes(),&"not-a-counter")?;
                model::put_indexed(w,store::PULLS,&Pull::key(&p.repo,p.number),None,&p,model::pull_index(&p))?;
                repair::failed(w,&broken.id,&sh,&[])?;
            }
            repair::failed(w,"aaa-missing","bad",&[])?;
            repair::failed(w,&r2.id,"0000000000000000000000000000000000000000",&[])?;
            let mut broken=r2.clone(); broken.id="aaa-log".into();
            w.put(store::REPOS,&Repo::key(&broken.id),&broken)?;
            repair::failed(w,&broken.id,&sh,&[FailedJob{name:"log".into(),attempt_id:Some("att_00000000000000000000000001".into()),details:None}])?;
            repair::failed(w, &r2.id, &sh, &fs)
        }).await.unwrap();
        std::fs::create_dir(app.ci.log_path("att_00000000000000000000000001")).unwrap();
        repair::sweep(&app).await.unwrap();
        app.store
            .write(|w| {
                let keys = w.prefix_keys(store::REPAIR_PENDING, &[])?;
                assert_eq!(
                    keys.len(),
                    3 + usize::from(
                        w.get::<Repo>(store::REPOS, &Repo::key("aaa-write"))?
                            .is_some()
                    )
                );
                for key in keys {
                    w.del(store::REPAIR_PENDING, &key)?;
                }
                Ok(())
            })
            .await
            .unwrap();
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
            ["post", "test", "paths", "missing"]
        );
        assert!(t.body.contains("`````\n"));
        assert!(t.body.contains("```` &#64;alice end"));
        assert!(!t.body.contains(&"x".repeat(8193)));
        if t.body.contains("/pulls/") {
            assert!(t.body.contains("success"));
            assert!(t.body.contains("Skipped by paths"));
            assert!(t.body.contains("Post-merge only"));
        }
        assert!(t.body.contains("invalid pipeline &#64;alice"));
        assert!(t.body.contains("Hints from running the failed jobs"));
        let has_pr = t.body.contains("/pulls/");
        assert_eq!(
            t.body
                .contains("No pull request's head in the range ran paths, missing:"),
            has_pr
        );
        assert!(!t.body.contains("range ran post"));
        assert!(!t.body.contains("range ran test"));
        assert!(t.body.contains("No record"));
        assert!(t.body.contains("[Job]("));
        assert!(t.body.contains("[Commit]("));
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
                assert_eq!(comments.len(), 2);
                assert!(
                    comments[0]
                        .body
                        .contains("Still failing after its failed jobs were retried")
                );
                if r.get::<Pull>(store::PULLS, &Pull::key(&t.repo, 1))?
                    .is_some()
                {
                    assert!(
                        comments[1]
                            .body
                            .contains("Merged pull requests linked here: !1.")
                    );
                } else {
                    assert!(comments[1].body.contains("failure may be intermittent"));
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

#[tokio::test]
async fn repair_body_reports_paths_reuse_but_accepts_tree_reuse_and_execution() {
    for mode in ["paths", "tree", "executed", "one_runs"] {
        let (_temp, app, repo, work, base) = failure_fixture().await;
        let head = commit_all(&app.git, &work, "head").await;
        let merge = commit_all(&app.git, &work, "merge").await;
        app.git
            .run_ok(
                None,
                &[
                    "-C",
                    work.to_str().unwrap(),
                    "push",
                    "-q",
                    app.git.repo_dir(&repo.path).to_str().unwrap(),
                    "HEAD:refs/heads/main",
                ],
            )
            .await
            .unwrap();
        let (id, path, b, h, m) = (
            repo.id.clone(),
            repo.path.clone(),
            base.clone(),
            head.clone(),
            merge.clone(),
        );
        app.store.write(move |w| {
            let old = repair::open_ticket(w, &id)?.unwrap();
            let mut closed = old.clone(); closed.state = "closed".into();
            crate::domain::tickets::write_ticket(w, Some(&old), &closed)?;
            let jobs = super::pipeline::parse("[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\npaths=['src/**']\n[[jobs]]\nname='other'\nimage='alpine'\ncommands=['true']\n").unwrap();
            for (sha, tree) in [(&b, "source-tree"), (&h, if mode == "tree" {"source-tree"} else {"head-tree"})] {
                queue::register(w, queue::Registration {order:vec![],selection:None,repo:id.clone(),repo_path:path.clone(),sha:sha.clone(),tree:Some(tree.into()),branch:sha.clone(),protected:false,pipeline:queue::PipelineState::Jobs{jobs:jobs.clone()},network:false,inputs:Default::default()},100,"0")?;
            }
            for mut a in queue::attempts_of(w, &id, &b)? {
                a.status="completed".into(); a.conclusion=Some("success".into());
                w.put(store::CI_ATTEMPTS, &queue::Attempt::key(&a.id), &a)?;
            }
            let source = queue::attempts_of(w, &id, &b)?.into_iter().find(|a| a.job.name=="test").unwrap();
            for mut a in queue::attempts_of(w, &id, &h)? {
                a.status="completed".into(); a.conclusion=Some("success".into());
                if mode != "executed" && a.job.name=="test" { a.reused_from=Some(source.id.clone()); }
                w.put(store::CI_ATTEMPTS, &queue::Attempt::key(&a.id), &a)?;
            }
            let mut p = super::tests::open_pull(&id, 10, "feature", "main");
            p.merged=true; p.state="closed".into(); p.author="owner".into(); p.merge_sha=Some(m.clone()); p.merged_head=Some(h.clone());
            w.put(store::PULLS, &Pull::key(&id, p.number), &p)?;
            if mode == "one_runs" {
                p.number=11; p.merge_sha=Some(h); p.merged_head=Some(b.clone());
                w.put(store::PULLS, &Pull::key(&id, p.number), &p)?;
            }
            let failures = vec![test_failure(), FailedJob{name:"other".into(),attempt_id:None,details:None}];
            let v = Verification {sha:m.clone(),base:Some(b.clone()),state:"failed".into(),started_at:"0".into(),finished_at:None,failed_jobs:failures.clone(),failing_since:Some(m.clone()),last_verified:Some(Verified{sha:b,at:"0".into()})};
            w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &v)?;
            repair::failed(w, &id, &m, &failures)
        }).await.unwrap();
        repair::sweep(&app).await.unwrap();
        let id = repo.id.clone();
        let ticket = app
            .store
            .read(move |r| repair::open_ticket(r, &id))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            ticket
                .body
                .contains("No pull request's head in the range ran test:"),
            mode == "paths",
            "{mode}: {}",
            ticket.body
        );
        assert!(!ticket.body.contains("range ran other"));
        assert_eq!(
            ticket
                .body
                .contains(&format!("reused {} (same contents of paths)", &base[..7])),
            ["paths", "one_runs"].contains(&mode)
        );
    }
}

#[tokio::test]
async fn empty_roster_remains_visible_and_refresh_registers_only_valid_agents() {
    let (_temp, app, repo, _work, base) = failure_fixture().await;
    let roster = std::sync::Arc::new(std::sync::Mutex::new(std::collections::BTreeMap::<
        String,
        String,
    >::new()));
    let copy = roster.clone();
    app.active_agents
        .set(std::sync::Arc::new(move || {
            Ok(copy.lock().unwrap().clone())
        }))
        .ok()
        .unwrap();
    let (id, sha) = (repo.id.clone(), base.clone());
    app.store
        .write(move |w| {
            repair::passed(w, &id, &sha)?;
            let v = Verification {
                sha: sha.clone(),
                base: None,
                state: "failed".into(),
                started_at: "0".into(),
                finished_at: Some("1".into()),
                failed_jobs: vec![test_failure()],
                failing_since: Some(sha.clone()),
                last_verified: None,
            };
            w.put(store::CI_VERIFICATIONS, &Key::new().s(&id).bytes(), &v)?;
            repair::failed(w, &id, &sha, &[test_failure()])
        })
        .await
        .unwrap();
    repair::sweep(&app).await.unwrap();
    // This is the exact view returned by the verification HTTP API and CI screen.
    let state = super::verify::main_state(&app, &repo).await.unwrap();
    assert_eq!(state.state, "failing");
    assert_eq!(state.current.unwrap().state, "failed");
    assert_eq!(state.failing_since.as_deref(), Some(base.as_str()));
    assert!(state.repair.is_none());
    assert_eq!(
        app.store
            .read(|r| Ok(r.prefix_keys(store::REPAIR_PENDING, &[])?.len()))
            .await
            .unwrap(),
        1
    );
    roster.lock().unwrap().extend([
        ("invalid name".into(), "Invalid".into()),
        ("branvil".into(), "Reserved".into()),
        ("worker".into(), "Worker Display".into()),
    ]);
    repair::sweep(&app).await.unwrap();
    let id = repo.id.clone();
    app.store
        .read(move |r| {
            assert_eq!(repair::open_ticket(r, &id)?.unwrap().number, 3);
            assert_eq!(
                r.get::<Ticket>(store::TICKETS, &Ticket::key(&id, 2))?
                    .unwrap()
                    .title,
                "Existing ready card"
            );
            assert_eq!(
                repair::open_ticket(r, &id)?.unwrap().assignee.unwrap().name,
                "worker"
            );
            assert!(r.prefix_keys(store::REPAIR_PENDING, &[])?.is_empty());
            assert_eq!(
                r.get::<Member>(store::MEMBERS, &Member::key("worker"))?
                    .unwrap()
                    .display_name,
                "Worker Display"
            );
            Ok(())
        })
        .await
        .unwrap();
    // Once assigned, a broken roster cannot suppress subsequent failure evidence.
    let (_temp, app, repo, _work, base) = failure_fixture().await;
    app.active_agents
        .set(std::sync::Arc::new(|| Err("roster unavailable".into())))
        .ok()
        .unwrap();
    let id = repo.id.clone();
    app.store
        .write(move |w| {
            repair::failed(
                w,
                &id,
                &base,
                &[FailedJob {
                    name: "new-job".into(),
                    attempt_id: None,
                    details: None,
                }],
            )
        })
        .await
        .unwrap();
    repair::sweep(&app).await.unwrap();
    let id = repo.id.clone();
    app.store
        .read(move |r| {
            assert!(
                repair::open_ticket(r, &id)?
                    .unwrap()
                    .repair
                    .unwrap()
                    .jobs
                    .contains(&"new-job".into())
            );
            assert!(r.prefix_keys(store::REPAIR_PENDING, &[])?.is_empty());
            Ok(())
        })
        .await
        .unwrap();
}

#[tokio::test]
async fn embedded_recovery_installs_roster_before_the_first_repair_sweep() {
    let (_temp, app, repo, _work, base) = failure_fixture().await;
    let (id, sha) = (repo.id.clone(), base.clone());
    app.store
        .write(move |w| {
            repair::passed(w, &id, &sha)?;
            repair::failed(w, &id, &sha, &[test_failure()])
        })
        .await
        .unwrap();
    let store = app.store.clone();
    let config = app.config.clone();
    drop(app);
    let embedded = crate::service::recover(
        config,
        std::sync::Arc::new(crate::vault::Memory::default()),
        "http://127.0.0.1:7878".into(),
        "http://127.0.0.1:7878".into(),
        store,
        Some(std::sync::Arc::new(|| {
            Ok([("worker".into(), "Worker".into())].into())
        })),
    )
    .await
    .unwrap();
    let id = repo.id.clone();
    let repair = tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            let id = id.clone();
            if let Some(t) = embedded
                .app
                .store
                .read(move |r| repair::open_ticket(r, &id))
                .await
                .unwrap()
            {
                break t;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(repair.number, 3);
    assert_eq!(
        repair.assignee.unwrap().name,
        "worker",
        "a stored inactive agent must never win the startup race"
    );
    embedded.close_events();
    embedded.drain().await.unwrap();
    embedded.close().await;
}
