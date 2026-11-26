use super::tests::{DIGEST, T0, reg, store};
use super::*;
use crate::ci::priority::{Class, class_of};
use crate::model::{self, Pull, Repo, Review};

fn repo(w: &Wtx, approvals: u64) -> AppResult<()> {
    w.put(
        store::REPOS,
        &Repo::key("repo_1"),
        &Repo {
            id: "repo_1".into(),
            path: "acme/demo".into(),
            workspace: "acme".into(),
            default_branch: "main".into(),
            protected_branches: vec!["release".into()],
            required_approvals: approvals,
            auto_land: false,
            hold_while_failing: false,
            review_rounds: 3,
            ci_network: false,
            responder: None,
            labels: vec![],
            upstream: None,
            revision: 1,
            created_at: T0.into(),
        },
    )
}

/// What the base requires of pull 1 at `head`, as the landing sweep records it.
fn required(w: &Wtx, head: &str, names: &[&str]) -> AppResult<()> {
    w.put(
        store::LANDING,
        &Pull::key("repo_1", 1),
        &model::Landing {
            repo: "repo_1".into(),
            number: 1,
            head_sha: head.into(),
            base_sha: "base".into(),
            conflicts: vec![],
            required_jobs: names.iter().map(|s| s.to_string()).collect(),
            told: vec![],
            notified_at: None,
            error: None,
        },
    )
}

fn pull(w: &Wtx, number: u64, head: &str, draft: bool) -> AppResult<Pull> {
    let p = Pull {
        repo: "repo_1".into(),
        number,
        title: head.into(),
        body: String::new(),
        state: "open".into(),
        merged: false,
        draft,
        author: "author".into(),
        head: head.into(),
        base: "main".into(),
        reviewers: vec![],
        tickets: vec![],
        labels: vec![],
        reviewed_by: vec![],
        ready_at: None,
        created_at: T0.into(),
        updated_at: T0.into(),
        closed_at: None,
        merged_at: None,
        merge_sha: None,
        merged_head: None,
        merged_base: None,
        merge_checks: None,
        upstream: None,
        revision: 1,
    };
    save_pull(w, None, &p)?;
    Ok(p)
}

fn save_pull(w: &Wtx, old: Option<&Pull>, p: &Pull) -> AppResult<()> {
    model::put_indexed(
        w,
        store::PULLS,
        &Pull::key(&p.repo, p.number),
        old.map(model::pull_index),
        p,
        model::pull_index(p),
    )
}

fn review(
    w: &Wtx,
    number: u64,
    author: &str,
    verdict: &str,
    body: &str,
    commit: &str,
) -> AppResult<()> {
    let seq = w.next("review")?;
    w.put(
        store::REVIEWS,
        &Key::new().s("repo_1").u(number).u(seq).bytes(),
        &Review {
            id: format!("review-{seq}"),
            seq,
            author: author.into(),
            verdict: verdict.into(),
            body: body.into(),
            commit: commit.into(),
            submitted_at: T0.into(),
            accepted_removals: vec![],
            reactions: vec![],
        },
    )
}

fn enqueue(w: &Wtx, sha: &str, branch: &str) -> AppResult<()> {
    register(w, reg(sha, branch, &["test"]), 100, T0)?;
    Ok(())
}

fn class(w: &Wtx, sha: &str) -> AppResult<Class> {
    Ok(class_of(w, &attempts_of(w, "repo_1", sha)?[0])?.class)
}

fn take(w: &Wtx) -> AppResult<Claimed> {
    Ok(claim(w, "runner", 60, T0)?.unwrap())
}

#[test]
fn protected_registration_replaces_a_queued_pr_retry_with_runner_selection() {
    let (_d, store) = store();
    store
        .write_sync(|w| {
            repo(w, 1)?;
            pull(w, 1, "feature", false)?;
            enqueue(w, "head", "feature")?;
            let c = take(w)?;
            let old = finish(
                w,
                &c.attempt.id,
                &c.lease,
                "failure",
                None,
                ResultInfo::default(),
                T0,
            )?;
            let selected = retry_selected(
                w,
                &old.id,
                100,
                T0,
                "author",
                RunnerSelection {
                    runner: Some("other".into()),
                    exclude_runners: Vec::new(),
                },
            )?;
            enqueue(w, "head", "main")?;
            let cancelled = get_attempt(w, &selected.id)?.unwrap();
            assert_eq!(cancelled.conclusion.as_deref(), Some("cancelled"));
            assert_eq!(cancelled.retry_decision, selected.retry_decision);
            let protected = take(w)?.attempt;
            assert_eq!(protected.cache_scope, "protected");
            assert!(protected.retry_decision.is_none());
            Ok(())
        })
        .unwrap();
}

#[test]
fn only_retries_of_failed_main_jobs_run_before_pr_checks() {
    for manual in [false, true] {
        let (_d, store) = store();
        store
            .write_sync(|w| {
                repo(w, 1)?;
                let verification = crate::ci::verify::Verification {
                    sha: "main-sha".into(),
                    base: None,
                    state: "running".into(),
                    started_at: T0.into(),
                    finished_at: None,
                    failed_jobs: vec![],
                    failing_since: Some("earlier-failure".into()),
                    last_verified: None,
                };
                w.put(
                    store::CI_VERIFICATIONS,
                    &Key::new().s("repo_1").bytes(),
                    &verification,
                )?;
                register(w, reg("main-sha", "main", &["test", "lint"]), 10, T0)?;
                let tries = if manual { 1 + MAX_AUTO_RETRIES } else { 1 };
                for n in 0..tries {
                    let c = claim_fixture(w, ("acme/demo", "main-sha"), T0)?;
                    let old = finish(
                        w,
                        &c.attempt.id,
                        &c.lease,
                        "failure",
                        None,
                        ResultInfo::default(),
                        T0,
                    )?;
                    if manual && n + 1 == tries {
                        retry(w, &old.id, 10, T0)?;
                    }
                }
                pull(w, 1, "pr", false)?;
                enqueue(w, "pr-sha", "pr")?;
                for (sha, job) in [
                    ("main-sha", "test"),
                    ("pr-sha", "test"),
                    ("main-sha", "lint"),
                ] {
                    let a = take(w)?.attempt;
                    assert_eq!(
                        (a.sha.as_str(), a.job.name.as_str()),
                        (sha, job),
                        "manual retry: {manual}"
                    );
                }
                Ok(())
            })
            .unwrap();
    }
}

#[test]
fn a_successful_head_loses_its_pending_cache_when_the_pull_leaves_it() {
    use gb_runner::ci::api::CacheDecision::{Discard, Keep};
    for close in [false, true] {
        let (_dir, s) = store();
        s.write_sync(|w| {
            repo(w, 0)?;
            let pull = pull(w, 1, "feature", false)?;
            let reference = Key::new().s("repo_1").s("refs/heads/feature").bytes();
            w.put(store::REFS, &reference, &"first")?;
            let mut run = reg("first", "feature", &["build"]);
            let PipelineState::Jobs { jobs } = &mut run.pipeline else {
                unreachable!()
            };
            jobs[0].cache = Some(super::super::pipeline::CacheDef {
                paths: vec!["target".into()],
                key_files: vec![],
            });
            register(w, run, 10, T0)?;
            let c = take(w)?;
            let id = &c.attempt.id;
            assert!(keeps_cache(w, &c.attempt)?);
            finish(
                w,
                id,
                &c.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    cache_key: Some("a".repeat(64)),
                    ..Default::default()
                },
                T0,
            )?;
            assert!(matches!(cache_decision(w, "runner", id)?, Keep));
            if close {
                let mut closed = pull.clone();
                closed.state = "closed".into();
                save_pull(w, Some(&pull), &closed)?;
            } else {
                w.put(store::REFS, &reference, &"second")?;
            }
            assert!(matches!(cache_decision(w, "runner", id)?, Discard));
            Ok(())
        })
        .unwrap();
    }
}

#[test]
fn repair_precedes_approved_work_and_refuses_paths_reuse() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        let ticket: model::Ticket = serde_json::from_value(serde_json::json!({
            "repo":"repo_1", "workspace":"acme", "number":1, "title":"Repair", "body":"", "mentions":[],
            "state":"open", "author":"branvil", "labels":[], "interactive":false, "lane":"ready", "rank":"a",
            "blocked_by":[], "created_at":T0, "updated_at":T0, "revision":1,
            "repair":{"reported":"main", "jobs":["test"]}
        })).unwrap();
        crate::domain::tickets::write_ticket(w, None, &ticket)?;
        pull(w, 1, "approved", false)?;
        review(w, 1, "reviewer", "approve", "", "approved")?;
        pull(w, 2, "ordinary", false)?;
        let mut p = pull(w, 3, "fix", false)?;
        let old = p.clone();
        p.tickets.push(model::TicketLink {url:"ticket".into(), local:Some(("repo_1".into(),1)), closes:false});
        save_pull(w, Some(&old), &p)?;
        enqueue(w, "approved", "approved")?;
        enqueue(w, "ordinary", "ordinary")?;
        enqueue(w, "repair", "fix")?;
        assert_eq!(take(w)?.attempt.sha, "repair");
        assert_eq!(take(w)?.attempt.sha, "approved");
        assert_eq!(take(w)?.attempt.sha, "ordinary");
        // A real result on another tree with matching inputs must not be offered.
        let source = || {
        let mut source = reg("source", "source", &["paths"]);
        if let PipelineState::Jobs {jobs} = &mut source.pipeline { jobs[0].paths = vec!["src/**".into()]; }
        source.inputs.insert("paths".into(), "same-inputs".into());
        source
        };
        register(w, source(), 100, T0)?;
        let c = take(w)?;
        finish(w, &c.attempt.id, &c.lease, "success", None, ResultInfo {image_digest:Some(DIGEST.into()), ..Default::default()}, T0)?;
        let mut dest = source(); dest.sha = "dest".into(); dest.branch = "fix".into(); dest.tree = Some("different-tree".into());
        register(w, dest, 100, T0)?;
        let c = take(w)?;
        assert_eq!(c.attempt.sha, "dest");
        assert!(c.reuse.is_none());
        let mut same = source(); same.sha = "same-tree".into(); same.branch = "fix".into();
        register(w, same, 100, T0)?;
        let c = take(w)?;
        assert_eq!(c.reuse.as_ref().map(|r| r.basis), Some(Basis::Tree));
        Ok(())
    }).unwrap();
}

#[test]
fn classes_override_registration_order_and_do_not_cancel_running_work() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        pull(w, 1, "draft", true)?;
        enqueue(w, "last", "draft")?;
        let running = take(w)?;
        enqueue(w, "last", "draft")?;
        let verification = crate::ci::verify::Verification {
            sha: "verify".into(),
            base: None,
            state: "running".into(),
            started_at: T0.into(),
            finished_at: None,
            failed_jobs: vec![],
            failing_since: None,
            last_verified: None,
        };
        w.put(
            store::CI_VERIFICATIONS,
            &Key::new().s("repo_1").bytes(),
            &verification,
        )?;
        enqueue(w, "verify", "main")?;
        assert_eq!(class(w, "verify")?, Class::Verify);
        assert_eq!(
            crate::ci::priority::attempt_details(w, &attempts_of(w, "repo_1", "verify")?[0])?
                .as_deref(),
            Some("Deferred: main is verified after the jobs that lead to a merge.")
        );
        // Keep another last-class job queued while the first is running.
        pull(w, 6, "draft2", true)?;
        enqueue(w, "last2", "draft2")?;
        pull(w, 2, "deferred", false)?;
        enqueue(w, "deferred", "deferred")?;
        review(
            w,
            2,
            "reviewer",
            "request_changes",
            "Please fix",
            "deferred",
        )?;
        pull(w, 3, "normal", false)?;
        enqueue(w, "normal", "normal")?;
        pull(w, 4, "landing", false)?;
        enqueue(w, "landing", "landing")?;
        review(w, 4, "reviewer", "approve", "", "landing")?;
        enqueue(w, "protected", "release")?;
        for expected in [
            "protected",
            "landing",
            "normal",
            "deferred",
            "verify",
            "last2",
        ] {
            let planned = scheduled(w, false)?;
            assert_eq!(planned.first().unwrap().0.sha, expected);
            assert_eq!(take(w)?.attempt.sha, expected);
        }
        assert!(
            !get_attempt(w, &running.attempt.id)?
                .unwrap()
                .cancel_requested
        );
        assert_eq!(
            finish(
                w,
                &running.attempt.id,
                &running.lease,
                "success",
                None,
                ResultInfo::default(),
                T0
            )?
            .conclusion
            .as_deref(),
            Some("success")
        );
        Ok(())
    })
    .unwrap();
}

#[test]
fn reviews_drafts_and_answers_are_recomputed_at_claim_time() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        pull(w, 1, "older", false)?;
        enqueue(w, "older", "older")?;
        let p = pull(w, 2, "newer", false)?;
        enqueue(w, "newer", "newer")?;
        review(w, 2, "reviewer", "request_changes", "", "newer")?;
        assert_eq!(class(w, "newer")?, Class::Normal);
        review(w, 2, "reviewer", "request_changes", "Fix this", "newer")?;
        assert_eq!(class(w, "newer")?, Class::Deferred);
        review(w, 2, "author", "comment", "Answered", "newer")?;
        assert_eq!(class(w, "newer")?, Class::Normal);
        review(w, 2, "reviewer", "approve", "", "newer")?;
        assert_eq!(scheduled(w, false)?[0].0.sha, "newer");
        assert_eq!(take(w)?.attempt.sha, "newer");
        let mut draft = p.clone();
        draft.draft = true;
        save_pull(w, Some(&p), &draft)?;
        assert_eq!(class(w, "newer")?, Class::Last);
        save_pull(w, Some(&draft), &p)?;
        assert_eq!(class(w, "newer")?, Class::Landing);
        enqueue(w, "pushed", "newer")?;
        assert_eq!(class(w, "pushed")?, Class::Normal);
        // Sufficient approvals dominate a different reviewer's objection.
        repo(w, 2)?;
        review(w, 2, "one", "approve", "", "pushed")?;
        review(w, 2, "two", "approve", "", "pushed")?;
        review(w, 2, "three", "request_changes", "Fix", "pushed")?;
        assert_eq!(class(w, "pushed")?, Class::Landing);
        Ok(())
    })
    .unwrap();
}

#[test]
fn required_failures_missing_jobs_and_manual_retry_share_check_rules() {
    const FAILED_DETAILS: &str = concat!(
        "Deferred: required check test failed on this commit. ",
        "It runs when no protected-branch, near-landing or normal job is queued."
    );
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        pull(w, 1, "feature", false)?;
        required(w, "sha", &["test"])?;
        register(w, reg("sha", "feature", &["test", "optional"]), 100, T0)?;
        let c = take(w)?;
        assert_eq!(c.attempt.job.name, "test");
        let failed = finish(
            w,
            &c.attempt.id,
            &c.lease,
            "failure",
            None,
            ResultInfo::default(),
            T0,
        )?;
        assert_eq!(class(w, "sha")?, Class::Deferred);
        let queued = attempts_of(w, "repo_1", "sha")?
            .into_iter()
            .find(|a| a.job.name == "optional")
            .unwrap();
        assert_eq!(queued.status, "queued");
        assert_eq!(
            crate::ci::priority::attempt_details(w, &queued)?.as_deref(),
            Some(FAILED_DETAILS)
        );
        assert_eq!(
            crate::ci::priority::attempt_details(w, &failed)?,
            failed.display_details()
        );
        retry(w, &failed.id, 100, T0)?;
        assert_eq!(
            crate::ci::priority::attempt_details(w, &queued)?,
            queued.display_details()
        );
        assert_eq!(class(w, "sha")?, Class::Normal);
        let optional = take(w)?;
        assert_eq!(optional.attempt.job.name, "optional");
        finish(
            w,
            &optional.attempt.id,
            &optional.lease,
            "failure",
            None,
            ResultInfo::default(),
            T0,
        )?;
        assert_eq!(class(w, "sha")?, Class::Normal);
        review(w, 1, "reviewer", "approve", "", "sha")?;
        assert_eq!(class(w, "sha")?, Class::Landing);
        required(w, "sha", &["missing"])?;
        assert_eq!(class(w, "sha")?, Class::Deferred);
        Ok(())
    })
    .unwrap();
}

#[test]
fn gate_leaving_closed_pulls_and_multiple_branches_choose_the_best_class() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        let p = pull(w, 1, "feature", true)?;
        enqueue(w, "sha", "feature")?;
        pull(w, 2, "feature", false)?;
        review(w, 2, "reviewer", "approve", "", "sha")?;
        assert_eq!(class(w, "sha")?, Class::Landing);
        let mut closed = p.clone();
        closed.state = "closed".into();
        save_pull(w, Some(&p), &closed)?;
        enqueue(w, "closed", "unused")?;
        assert_eq!(class(w, "closed")?, Class::Last);
        enqueue(w, "normal", "normal")?;
        pull(w, 3, "normal", false)?;
        enqueue(w, "gate", "gate/2")?;
        assert_eq!(class(w, "gate")?, Class::Last);
        enqueue(w, "sha", "release")?;
        assert_eq!(class(w, "sha")?, Class::Protected);
        assert_eq!(take(w)?.attempt.sha, "sha");
        assert_eq!(take(w)?.attempt.sha, "normal");
        Ok(())
    })
    .unwrap();
}

#[test]
fn reuse_is_preferred_only_within_class_and_retries_must_execute() {
    let (_d, s) = store();
    s.write_sync(|w| {
        // Without repository records all jobs share Last, preserving old FIFO.
        enqueue(w, "source", "feature")?;
        let c = take(w)?;
        finish(
            w,
            &c.attempt.id,
            &c.lease,
            "success",
            None,
            ResultInfo {
                image_digest: Some(DIGEST.into()),
                ..Default::default()
            },
            T0,
        )?;
        enqueue(w, "older", "older")?;
        let mut registration = reg("reuse", "feature", &["test"]);
        registration.tree = Some("tree-source".into());
        register(w, registration, 100, T0)?;
        assert_eq!(scheduled(w, false)?[0].0.sha, "reuse");
        let reused = take(w)?;
        assert_eq!(reused.attempt.sha, "reuse");
        assert!(reused.reuse.is_some());
        finish(
            w,
            &reused.attempt.id,
            &reused.lease,
            "success",
            None,
            ResultInfo::default(),
            T0,
        )?;
        retry(w, &reused.attempt.id, 100, T0)?;
        assert_eq!(take(w)?.attempt.sha, "older");
        assert!(take(w)?.reuse.is_none());
        // A reusable last-class job cannot overtake a normal job.
        repo(w, 1)?;
        pull(w, 1, "normal", false)?;
        enqueue(w, "normal", "normal")?;
        let mut registration = reg("last", "unused", &["test"]);
        registration.tree = Some("tree-source".into());
        register(w, registration, 100, T0)?;
        assert_eq!(take(w)?.attempt.sha, "normal");
        assert_eq!(take(w)?.attempt.sha, "last");
        pull(w, 2, "normal2", false)?;
        enqueue(w, "normal2", "normal2")?;
        pull(w, 3, "deferred", false)?;
        let mut registration = reg("deferred", "deferred", &["test"]);
        registration.tree = Some("tree-source".into());
        register(w, registration, 100, T0)?;
        review(w, 3, "reviewer", "request_changes", "Fix", "deferred")?;
        assert_eq!(take(w)?.attempt.sha, "normal2");
        let deferred = take(w)?;
        assert_eq!(deferred.attempt.sha, "deferred");
        assert!(deferred.reuse.is_some());
        Ok(())
    })
    .unwrap();
}

#[test]
fn unconsumed_results_are_last_with_specific_reasons() {
    let (_d, s) = store();
    s.write_sync(|w| {
        enqueue(w, "sha", "feature")?;
        let a = attempts_of(w, "repo_1", "sha")?.remove(0);
        assert_eq!(
            class_of(w, &a)?.reason.as_deref(),
            Some("no repository record")
        );
        let mut orphan = a.clone();
        orphan.sha = "absent".into();
        assert_eq!(class_of(w, &orphan)?.reason.as_deref(), Some("no CI run"));
        repo(w, 1)?;
        let p = pull(w, 1, "feature", false)?;
        let mut closed = p.clone();
        closed.state = "closed".into();
        closed.merged = true;
        save_pull(w, Some(&p), &closed)?;
        assert_eq!(
            class_of(w, &a)?.reason.as_deref(),
            Some("no open pull request")
        );
        branch_left(w, "repo_1", "sha", "feature", "next", T0)?;
        let retried = retry(w, &a.id, 100, T0)?;
        assert_eq!(
            class_of(w, &retried)?.reason.as_deref(),
            Some("no branch wants this commit")
        );
        assert_eq!(take(w)?.attempt.id, retried.id);
        Ok(())
    })
    .unwrap();
}

#[test]
fn protected_automatic_retries_overtake_older_pull_work_without_reuse() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        pull(w, 1, "feature", false)?;
        enqueue(w, "older", "feature")?;
        enqueue(w, "main", "main")?;
        let c = take(w)?;
        assert_eq!(c.attempt.sha, "main");
        finish(
            w,
            &c.attempt.id,
            &c.lease,
            "failure",
            None,
            ResultInfo::default(),
            T0,
        )?;
        let automatic = take(w)?;
        assert_eq!(automatic.attempt.sha, "main");
        assert!(automatic.attempt.auto_retry.is_some());
        assert!(!automatic.attempt.allow_reuse);
        assert!(automatic.reuse.is_none());
        assert_eq!(take(w)?.attempt.sha, "older");
        Ok(())
    })
    .unwrap();
}

#[test]
fn admission_reserves_capacity_for_active_classes_and_reports_waiting_drafts() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        pull(w, 1, "draft", true)?;
        pull(w, 2, "approved", false)?;
        review(w, 2, "reviewer", "approve", "", "approved-sha")?;
        register(w, reg("draft-sha", "draft", &["a", "b", "c", "d"]), 4, T0)?;
        register(w, reg("approved-sha", "approved", &["one", "two"]), 4, T0)?;
        assert!(!get_run(w, "repo_1", "approved-sha")?.unwrap().waiting);
        register(w, reg("waiting", "draft", &["test"]), 4, T0)?;
        let run = get_run(w, "repo_1", "waiting")?.unwrap();
        assert!(run.waiting);
        let priority = crate::ci::priority::run_class(w, &run)?;
        assert_eq!(
            priority
                .details(Some("Waiting for room".into()), true)
                .as_deref(),
            Some("Waiting for room Deferred: draft pull request.")
        );
        assert_eq!(take(w)?.attempt.sha, "approved-sha");
        assert_eq!(take(w)?.attempt.sha, "approved-sha");
        Ok(())
    })
    .unwrap();
}

#[test]
fn waiting_admission_sorts_all_runs_by_class_then_sequence() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        pull(w, 1, "normal", false)?;
        pull(w, 2, "approved", false)?;
        review(w, 2, "reviewer", "approve", "", "approved-sha")?;
        register(w, reg("busy", "normal", &["a", "b", "c", "d"]), 4, T0)?;
        for i in 0..70 {
            register(w, reg(&format!("normal-{i}"), "normal", &["test"]), 4, T0)?;
        }
        register(w, reg("approved-sha", "approved", &["test"]), 4, T0)?;
        assert!(get_run(w, "repo_1", "approved-sha")?.unwrap().waiting);
        take(w)?;
        assert_eq!(
            admit_waiting(w, 4, &BTreeMap::new(), T0)?,
            vec![("acme/demo".into(), "approved-sha".into())]
        );
        take(w)?;
        assert_eq!(
            admit_waiting(w, 4, &BTreeMap::new(), T0)?,
            vec![("acme/demo".into(), "normal-0".into())]
        );
        Ok(())
    })
    .unwrap();
}

#[test]
fn manual_retry_uses_the_class_after_replacing_only_its_own_failure() {
    for (draft, failures, approved, accepted) in [
        (false, 1, false, true),
        (false, 1, true, true),
        (true, 1, false, false),
        (false, 2, false, false),
    ] {
        let (_d, s) = store();
        s.write_sync(|w| {
            repo(w, 1)?;
            pull(w, 1, "feature", draft)?;
            required(w, "sha", &["test", "other"])?;
            pull(w, 2, "backlog", true)?;
            if approved {
                review(w, 1, "reviewer", "approve", "", "sha")?;
            }
            register(w, reg("sha", "feature", &["test", "other"]), 4, T0)?;
            let mut failed = None;
            for i in 0..2 {
                let c = take(w)?;
                let a = finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    if i < failures { "failure" } else { "success" },
                    None,
                    ResultInfo::default(),
                    T0,
                )?;
                if i == 0 {
                    failed = Some(a);
                }
            }
            let failed = failed.unwrap();
            register(
                w,
                reg("backlog-sha", "backlog", &["a", "b", "c", "d"]),
                4,
                T0,
            )?;
            let result = retry(w, &failed.id, 4, T0);
            assert_eq!(
                result.is_ok(),
                accepted,
                "draft={draft}, failures={failures}"
            );
            if !accepted {
                let error = result.unwrap_err();
                assert_eq!(error.status.as_u16(), 503);
                assert_eq!(error.code, "unavailable");
                assert_eq!(
                    current_attempts(w, "repo_1", "sha", std::slice::from_ref(&failed.job))?[0].id,
                    failed.id
                );
            }
            Ok(())
        })
        .unwrap();
    }
}

#[test]
fn deferred_details_follow_current_records_and_preserve_retry_explanations() {
    use crate::ci::priority::attempt_details;
    const NO_REPOSITORY: &str = concat!(
        "Deferred: no repository record. ",
        "It runs when no other job is queued."
    );
    const CHANGES_REQUESTED: &str = concat!(
        "Deferred: changes requested by reviewer (approvals 0/1). ",
        "It runs when no protected-branch, near-landing or normal job is queued."
    );
    const AUTOMATIC_RETRY: &str = "Automatic retry 1/3 of root. runner interruption Deferred:";
    const MISSING_CHECK: &str = concat!(
        "Deferred: required check missing failed on this commit. ",
        "It runs when no protected-branch, near-landing or normal job is queued."
    );
    let (_d, s) = store();
    s.write_sync(|w| {
        enqueue(w, "sha", "feature")?;
        let mut a = attempts_of(w, "repo_1", "sha")?.remove(0);
        assert_eq!(attempt_details(w, &a)?.unwrap(), NO_REPOSITORY);
        repo(w, 1)?;
        let p = pull(w, 1, "feature", false)?;
        assert_eq!(attempt_details(w, &a)?, a.display_details());
        review(w, 1, "reviewer", "request_changes", "Please fix", "sha")?;
        let details = attempt_details(w, &a)?.unwrap();
        assert!(details.contains(CHANGES_REQUESTED));
        a.auto_retry = Some(AutoRetry {
            root_attempt: "root".into(),
            number: 1,
        });
        a.details = Some("runner interruption".into());
        assert!(
            attempt_details(w, &a)?
                .unwrap()
                .starts_with(AUTOMATIC_RETRY)
        );
        a.auto_retry = None;
        a.details = None;
        required(w, "sha", &["missing"])?;
        assert_eq!(attempt_details(w, &a)?.unwrap(), MISSING_CHECK);
        a.status = "in_progress".into();
        assert_eq!(attempt_details(w, &a)?, a.display_details());
        a.status = "queued".into();
        let mut closed = p.clone();
        closed.state = "closed".into();
        save_pull(w, Some(&p), &closed)?;
        assert!(
            attempt_details(w, &a)?
                .unwrap()
                .contains("Deferred: no open pull request.")
        );
        branch_left(w, "repo_1", "sha", "feature", "next", T0)?;
        let retried = retry(w, &a.id, 100, T0)?;
        assert!(
            attempt_details(w, &retried)?
                .unwrap()
                .contains("Deferred: no branch wants this commit.")
        );
        Ok(())
    })
    .unwrap();
}

#[test]
fn probe_admission_and_claim_use_class_two_even_on_protected_commits() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        pull(w, 1, "feature", false)?;
        register(w, reg("normal", "feature", &["test"]), 10, T0)?;
        register(w, reg("base", "main", &["test"]), 10, T0)?;
        let repository: Repo = w.get(store::REPOS, &Repo::key("repo_1"))?.unwrap();
        let PipelineState::Jobs { jobs } = reg("base", "main", &["test"]).pipeline else {
            unreachable!()
        };
        assert!(probe(w, &repository, "middle", &jobs[0], 2, T0)?.is_none());
        let diagnostic = probe(w, &repository, "base", &jobs[0], 3, T0)?.unwrap();
        assert_eq!(class_of(w, &diagnostic)?.class, Class::Landing);
        assert_eq!(claim(w, "runner", 60, T0)?.unwrap().attempt.sha, "base");
        let c = claim(w, "runner", 60, T0)?.unwrap();
        assert_eq!(c.attempt.id, diagnostic.id);
        assert_eq!(c.attempt.cache_scope, "probe");
        assert_eq!(claim(w, "runner", 60, T0)?.unwrap().attempt.sha, "normal");
        // Class 4b fills the nominal queue, but does not consume probe capacity.
        register(w, reg("last", "unwanted", &["test"]), 1, T0)?;
        assert!(probe(w, &repository, "middle", &jobs[0], 1, T0)?.is_some());
        assert!(get_run(w, "repo_1", "middle")?.is_none());
        Ok(())
    })
    .unwrap();
}

#[test]
fn held_approved_work_is_normal_until_main_recovers_or_repair_closes() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, 1)?;
        let mut r: Repo = w.get(store::REPOS, &Repo::key("repo_1"))?.unwrap();
        let mut p = pull(w, 1, "approved", false)?;
        review(w, 1, "reviewer", "approve", "", "approved")?;
        enqueue(w, "approved", "approved")?;
        let mut ticket: model::Ticket = serde_json::from_value(serde_json::json!({
            "repo":"repo_1", "workspace":"acme", "number":42, "title":"Repair", "body":"", "mentions":[],
            "state":"open", "author":"branvil", "labels":[], "interactive":false, "lane":"ready", "rank":"a",
            "assignee":{"name":"fixer", "assigned_at":T0},
            "blocked_by":[], "created_at":T0, "updated_at":T0, "revision":1,
            "repair":{"reported":"main", "jobs":["test"]}
        })).unwrap();
        crate::domain::tickets::write_ticket(w, None, &ticket)?;
        let mut v = crate::ci::verify::Verification {
            sha:"main".into(), base:None, state:"failed".into(), started_at:T0.into(),
            finished_at:None, failed_jobs:vec![], failing_since:Some("broken".into()), last_verified:None,
        };
        let key = Key::new().s("repo_1").bytes();
        w.put(store::CI_VERIFICATIONS, &key, &v)?;
        assert!(crate::landing::held(w, &r, &p)?.is_none());
        assert_eq!(class(w, "approved")?, Class::Landing);
        r.hold_while_failing = true;
        w.put(store::REPOS, &Repo::key(&r.id), &r)?;
        for state in ["failed", "running"] {
            v.state = state.into();
            w.put(store::CI_VERIFICATIONS, &key, &v)?;
            let hold = crate::landing::held(w, &r, &p)?.unwrap();
            assert_eq!(hold.number, 42);
            assert_eq!(hold.assignee.as_deref(), Some("fixer"));
            assert_eq!(class(w, "approved")?, Class::Normal);
        }
        p.base = "release".into();
        assert!(crate::landing::held(w, &r, &p)?.is_none());
        p.base = "main".into();
        let old = p.clone();
        p.tickets.push(model::TicketLink {url:"repair".into(), local:Some((r.id.clone(),42)), closes:false});
        save_pull(w, Some(&old), &p)?;
        assert!(crate::landing::held(w, &r, &p)?.is_none());
        assert_eq!(class(w, "approved")?, Class::Repair);
        let old = p.clone();
        p.tickets.clear();
        save_pull(w, Some(&old), &p)?;
        v.failing_since = None;
        w.put(store::CI_VERIFICATIONS, &key, &v)?;
        assert!(crate::landing::held(w, &r, &p)?.is_none());
        assert_eq!(class(w, "approved")?, Class::Landing);
        v.failing_since = Some("broken".into());
        w.put(store::CI_VERIFICATIONS, &key, &v)?;
        let old = ticket.clone();
        ticket.state = "closed".into();
        crate::domain::tickets::write_ticket(w, Some(&old), &ticket)?;
        assert!(crate::landing::held(w, &r, &p)?.is_none());
        assert_eq!(class(w, "approved")?, Class::Landing);
        Ok(())
    }).unwrap();
}

#[cfg(any(test, feature = "test-support"))]
#[test]
fn fixture_target_is_not_hidden_by_another_reusable_attempt() {
    let (_d, s) = store();
    s.write_sync(|w| {
        enqueue(w, "source", "feature")?;
        let source = take(w)?;
        finish(
            w,
            &source.attempt.id,
            &source.lease,
            "success",
            None,
            ResultInfo {
                image_digest: Some(DIGEST.into()),
                ..Default::default()
            },
            T0,
        )?;
        enqueue(w, "target", "target")?;
        let mut registration = reg("reusable", "feature", &["test"]);
        registration.tree = Some("tree-source".into());
        register(w, registration, 100, T0)?;
        assert_eq!(scheduled(w, true)?[0].0.sha, "reusable");
        let target = claim_fixture(w, ("acme/demo", "target"), T0)?;
        assert_eq!(target.attempt.sha, "target");
        assert_eq!(target.attempt.status, "in_progress");
        assert!(target.reuse.is_none());
        let remaining = scheduled(w, false)?;
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0].0.sha, "reusable");
        assert_eq!(remaining[0].0.status, "queued");
        assert!(remaining[0].1.is_some());
        Ok(())
    })
    .unwrap();
}
