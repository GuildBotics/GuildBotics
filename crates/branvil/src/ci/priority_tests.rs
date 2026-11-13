use super::tests::{DIGEST, T0, reg, store};
use super::*;
use crate::ci::priority::{Class, class_of};
use crate::model::{self, Landing, Pull, Repo, Review};

fn repo(w: &Wtx, required: &[&str], approvals: u64) -> AppResult<()> {
    w.put(
        store::REPOS,
        &Repo::key("repo_1"),
        &Repo {
            id: "repo_1".into(),
            path: "acme/demo".into(),
            workspace: "acme".into(),
            default_branch: "main".into(),
            protected_branches: vec!["release".into()],
            required_checks: required.iter().map(|s| s.to_string()).collect(),
            required_approvals: approvals,
            auto_land: false,
            ci_network: false,
            labels: vec![],
            upstream: None,
            revision: 1,
            created_at: T0.into(),
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
fn classes_override_registration_order_and_do_not_cancel_running_work() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, &[], 1)?;
        pull(w, 1, "draft", true)?;
        enqueue(w, "last", "draft")?;
        let running = take(w)?;
        enqueue(w, "last", "draft")?;
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
        enqueue(w, "protected", "main")?;
        for expected in ["protected", "landing", "normal", "deferred", "last2"] {
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
                100,
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
        repo(w, &[], 1)?;
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
        repo(w, &[], 2)?;
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
        repo(w, &["test"], 1)?;
        pull(w, 1, "feature", false)?;
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
            100,
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
            100,
            T0,
        )?;
        assert_eq!(class(w, "sha")?, Class::Normal);
        review(w, 1, "reviewer", "approve", "", "sha")?;
        assert_eq!(class(w, "sha")?, Class::Landing);
        repo(w, &["missing"], 1)?;
        assert_eq!(class(w, "sha")?, Class::Deferred);
        Ok(())
    })
    .unwrap();
}

#[test]
fn gate_leaving_closed_pulls_and_multiple_branches_choose_the_best_class() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, &[], 1)?;
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
        let mut gate = Landing {
            repo: "repo_1".into(),
            number: 2,
            base: "main".into(),
            queued_at: T0.into(),
            sequence: 1,
            state: "gating".into(),
            state_at: T0.into(),
            gate: Some("gate".into()),
            base_sha: "base".into(),
            head_sha: "sha".into(),
            reason: None,
            notified_at: None,
            notify: false,
            resume_publish: false,
            error: None,
        };
        w.put(store::LANDING, &Pull::key("repo_1", 2), &gate)?;
        assert_eq!(class(w, "gate")?, Class::Landing);
        gate.state = "left".into();
        w.put(store::LANDING, &Pull::key("repo_1", 2), &gate)?;
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
            100,
            T0,
        )?;
        enqueue(w, "older", "older")?;
        let mut registration = reg("reuse", "feature", &["test"]);
        registration.tree = Some("tree-source".into());
        register(w, registration, 100, T0)?;
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
            100,
            T0,
        )?;
        retry(w, &reused.attempt.id, 100, T0)?;
        assert_eq!(take(w)?.attempt.sha, "older");
        assert!(take(w)?.reuse.is_none());
        // A reusable last-class job cannot overtake a normal job.
        repo(w, &[], 1)?;
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
fn unconsumed_results_and_refreshes_are_last_with_specific_reasons() {
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
        repo(w, &[], 1)?;
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
        enqueue(w, "protected", "main")?;
        let mut refresh = attempts_of(w, "repo_1", "protected")?.remove(0);
        let old = refresh.clone();
        refresh.purpose = Purpose::CacheRefresh;
        put_attempt(w, Some(&old), &refresh)?;
        assert_eq!(
            class_of(w, &refresh)?.reason.as_deref(),
            Some("cache refresh")
        );
        // Last-class FIFO still works, including a retry of an unwanted commit.
        assert_eq!(take(w)?.attempt.id, retried.id);
        assert_eq!(take(w)?.attempt.id, refresh.id);
        Ok(())
    })
    .unwrap();
}

#[test]
fn protected_automatic_retries_overtake_older_pull_work_without_reuse() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, &["test"], 1)?;
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
            100,
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
        repo(w, &[], 1)?;
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
        // Refreshes still use total capacity, even for a protected source.
        let a = attempts_of(w, "repo_1", "draft-sha")?.remove(0);
        assert!(enqueue_refresh(w, &a, "cache-key", 4, T0)?.is_none());
        Ok(())
    })
    .unwrap();
}

#[test]
fn waiting_admission_sorts_all_runs_by_class_then_sequence() {
    let (_d, s) = store();
    s.write_sync(|w| {
        repo(w, &[], 1)?;
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
            repo(w, &["test", "other"], 1)?;
            pull(w, 1, "feature", draft)?;
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
                    4,
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
        repo(w, &[], 1)?;
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
        repo(w, &["missing"], 1)?;
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
        a.purpose = Purpose::CacheRefresh;
        assert_eq!(
            attempt_details(w, &a)?.unwrap(),
            "Deferred: cache refresh. It runs when no other job is queued."
        );
        Ok(())
    })
    .unwrap();
}
