//! Reuse by the contents of paths (`Basis::Paths`) and where it never applies.

use super::tests::{DIGEST, T0, reg, store};
use super::*;

const T1: &str = "2026-10-14T10:00:30.000Z";
const T2: &str = "2026-10-14T10:02:00.000Z";

/// A head of `branch` whose job `test` has paths, with its own tree and the
/// input digest `digest` of the job (none when empty).
fn head(sha: &str, branch: &str, digest: &str) -> Registration {
    let mut reg = reg(sha, branch, &["test"]);
    if let PipelineState::Jobs { jobs } = &mut reg.pipeline {
        jobs[0].paths = vec!["src/**".into()];
    }
    if !digest.is_empty() {
        reg.inputs = BTreeMap::from([("test".to_string(), digest.to_string())]);
    }
    reg
}

fn info(reused_from: Option<&str>) -> ResultInfo {
    ResultInfo {
        image_digest: Some(DIGEST.into()),
        reused_from: reused_from.map(str::to_string),
        ..Default::default()
    }
}

/// Registers `reg`, claims its attempt, and ends it with `conclusion` by
/// actually executing (declining any offered reuse).
fn run(w: &Wtx, reg: Registration, conclusion: &str, now: &str) -> AppResult<Attempt> {
    let sha = reg.sha.clone();
    register(w, reg, 10, now)?;
    let c = claim(w, "r", 60, now)?.unwrap();
    assert_eq!(c.attempt.sha, sha);
    finish(
        w,
        &c.attempt.id,
        &c.lease,
        conclusion,
        None,
        info(None),
        10,
        now,
    )
}

/// Registers `reg` and claims its attempt.
fn offer(w: &Wtx, reg: Registration, now: &str) -> AppResult<Claimed> {
    let sha = reg.sha.clone();
    register(w, reg, 10, now)?;
    let c = claim(w, "r", 60, now)?.unwrap();
    assert_eq!(c.attempt.sha, sha);
    Ok(c)
}

fn offered(c: &Claimed) -> Option<(&str, Basis)> {
    c.reuse.as_ref().map(|r| (r.attempt_id.as_str(), r.basis))
}

fn reuse(w: &Wtx, c: &Claimed, source: &str, now: &str) -> AppResult<Attempt> {
    finish(
        w,
        &c.attempt.id,
        &c.lease,
        "success",
        None,
        info(Some(source)),
        10,
        now,
    )
}

#[test]
fn a_later_head_reuses_an_earlier_heads_success_while_its_paths_contents_stay() {
    let (_d, s) = store();
    s.write_sync(|w| {
        let first = run(w, head("h1", "feature", "d1"), "success", T0)?;
        // h2 changed only docs/: another tree, the same digest.
        let c = offer(w, head("h2", "feature", "d1"), T1)?;
        assert_eq!(offered(&c), Some((first.id.as_str(), Basis::Paths)));
        assert_eq!(c.reuse.as_ref().unwrap().sha, "h1");
        let done = reuse(w, &c, &first.id, T1)?;
        let source = source_of(w, &done)?;
        assert_eq!(source.attempt.id, first.id);
        assert_eq!(source.basis, Some(Basis::Paths));
        // h3 changed src/: no reuse.
        let c = offer(w, head("h3", "feature", "d3"), T2)?;
        assert_eq!(offered(&c), None);
        Ok(())
    })
    .unwrap();
    // A failure is never a source.
    let (_d, s) = store();
    s.write_sync(|w| {
        run(w, head("h1", "feature", "d1"), "failure", T0)?;
        assert_eq!(offered(&offer(w, head("h2", "feature", "d1"), T1)?), None);
        Ok(())
    })
    .unwrap();
}

#[test]
fn any_purpose_supplies_a_source_but_only_actual_kept_successes_of_the_same_job() {
    for case in [
        "other_pull",
        "protected",
        "refresh",
        "definition",
        "network",
        "pruned",
        "reused",
    ] {
        let (_d, s) = store();
        s.write_sync(|w| {
            let branch = match case {
                "protected" => "main",
                "other_pull" => "other",
                _ => "feature",
            };
            let mut source = head("h1", branch, "d1");
            if case == "network" {
                source.network = true;
            }
            let first = run(w, source, "success", T0)?;
            let mut changed = first.clone();
            match case {
                "refresh" => changed.purpose = Purpose::CacheRefresh,
                "pruned" => changed.files_pruned = true,
                _ => {}
            }
            put_attempt(w, Some(&first), &changed)?;
            let mut source_id = first.id.clone();
            if case == "reused" {
                // A reused success on another tree is not a source either.
                let c = offer(w, head("h2", "other", "d1"), T1)?;
                let done = reuse(w, &c, &first.id, T1)?;
                let mut gone = first.clone();
                gone.files_pruned = true;
                put_attempt(w, Some(&changed), &gone)?;
                source_id = done.id;
            }
            let mut next = head("h3", "feature", "d1");
            if case == "definition"
                && let PipelineState::Jobs { jobs } = &mut next.pipeline
            {
                jobs[0].commands.push("false".into());
            }
            let c = offer(w, next, T2)?;
            let expected = matches!(case, "other_pull" | "protected" | "refresh");
            assert_eq!(
                offered(&c),
                expected.then_some((source_id.as_str(), Basis::Paths)),
                "{case}"
            );
            Ok(())
        })
        .unwrap();
    }
}

#[test]
fn without_inputs_or_paths_or_on_protected_and_gate_runs_only_the_tree_counts() {
    for case in ["no_inputs", "no_paths", "protected", "gate"] {
        let (_d, s) = store();
        s.write_sync(|w| {
            let first = run(w, head("h1", "feature", "d1"), "success", T0)?;
            let mut next = match case {
                "no_inputs" => head("h2", "feature", ""),
                "protected" => head("h2", "main", "d1"),
                "gate" => head("h2", "gate/1", "d1"),
                _ => head("h2", "feature", "d1"),
            };
            if case == "no_paths" {
                if let PipelineState::Jobs { jobs } = &mut next.pipeline {
                    jobs[0].paths.clear();
                }
                // The source's definition must match.
                let PipelineState::Jobs { jobs } = &next.pipeline else {
                    unreachable!()
                };
                let mut source = first.clone();
                source.job = jobs[0].clone();
                put_attempt(w, Some(&first), &source)?;
            }
            let c = offer(w, next, T1)?;
            assert_eq!(offered(&c), None, "{case}: another tree");
            // Declined, it reports another tree's result: refused.
            let err = reuse(w, &c, &first.id, T1).unwrap_err();
            assert_eq!(err.code, "invalid", "{case}");
            // The same tree is still reused.
            let branch = match case {
                "protected" => "main",
                "gate" => "gate/1",
                _ => "feature",
            };
            let mut same = head("h3", branch, "d1");
            same.tree = Some("tree-h2".into());
            if case == "no_inputs" {
                same.inputs.clear();
            }
            if case == "no_paths"
                && let PipelineState::Jobs { jobs } = &mut same.pipeline
            {
                jobs[0].paths.clear();
            }
            let done = finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                info(None),
                10,
                T1,
            )?;
            let c = offer(w, same, T2)?;
            assert_eq!(offered(&c), Some((done.id.as_str(), Basis::Tree)), "{case}");
            Ok(())
        })
        .unwrap();
    }
}

#[test]
fn finish_refuses_a_source_outside_the_basis_it_was_claimed_with() {
    let (_d, s) = store();
    s.write_sync(|w| {
        let other_digest = run(w, head("h1", "feature", "d0"), "success", T0)?;
        let c = offer(w, head("h3", "feature", "d1"), T1)?;
        assert_eq!(offered(&c), None);
        // Paths basis: the digest differs.
        let err = reuse(w, &c, &other_digest.id, T1).unwrap_err();
        assert_eq!(err.code, "invalid");
        Ok(())
    })
    .unwrap();
}

#[test]
fn a_protected_branch_runs_a_paths_reused_job_of_its_commit_again() {
    let (_d, s) = store();
    s.write_sync(|w| {
        let first = run(w, head("h1", "feature", "d1"), "success", T0)?;
        let c = offer(w, head("h2", "feature", "d1"), T1)?;
        let reused = reuse(w, &c, &first.id, T1)?;
        // A fast-forward merge of h2.
        register(w, head("h2", "main", "d1"), 10, T2)?;
        let c = claim(w, "r", 60, T2)?.unwrap();
        assert_eq!((c.attempt.sha.as_str(), c.attempt.number), ("h2", 2));
        assert_eq!(c.attempt.cache_scope, "protected");
        assert_eq!(offered(&c), None, "another tree is not reused");
        let done = finish(
            w,
            &c.attempt.id,
            &c.lease,
            "success",
            None,
            info(None),
            10,
            T2,
        )?;
        let current = current_attempts(w, "repo_1", "h2", std::slice::from_ref(&done.job))?;
        assert_eq!(current[0].id, done.id);
        assert_ne!(current[0].id, reused.id);
        // Registered again, nothing more runs.
        register(w, head("h2", "main", "d1"), 10, T2)?;
        assert!(claim(w, "r", 60, T2)?.is_none());
        Ok(())
    })
    .unwrap();
}

#[test]
fn a_paths_reuse_finished_after_the_commit_became_protected_runs_again() {
    let (_d, s) = store();
    s.write_sync(|w| {
        let first = run(w, head("h1", "feature", "d1"), "success", T0)?;
        let c = offer(w, head("h2", "feature", "d1"), T1)?;
        assert_eq!(offered(&c), Some((first.id.as_str(), Basis::Paths)));
        // Merged while the runner checks the image.
        register(w, head("h2", "main", "d1"), 10, T1)?;
        let ended = reuse(w, &c, &first.id, T1)?;
        assert_eq!(ended.conclusion.as_deref(), Some("cancelled"));
        assert_eq!(
            ended.details.as_deref(),
            Some("Stopped: the commit became a protected branch's; it runs again as one.")
        );
        assert!(ended.reused_from.is_none());
        let next = claim(w, "r", 60, T2)?.unwrap();
        assert_eq!((next.attempt.sha.as_str(), next.attempt.number), ("h2", 2));
        assert_eq!(next.attempt.seq, ended.seq);
        assert!(next.attempt.allow_reuse);
        assert_eq!(offered(&next), None, "only the same tree may be reused");
        Ok(())
    })
    .unwrap();
    // A source of the same tree still finishes as a reuse.
    let (_d, s) = store();
    s.write_sync(|w| {
        let first = run(w, head("h1", "feature", "d1"), "success", T0)?;
        let mut same = head("h2", "feature", "d1");
        same.tree = Some("tree-h1".into());
        let c = offer(w, same, T1)?;
        register(w, head("h2", "main", "d1"), 10, T1)?;
        let done = reuse(w, &c, &first.id, T1)?;
        assert_eq!(done.conclusion.as_deref(), Some("success"));
        assert_eq!(source_of(w, &done)?.basis, Some(Basis::Tree));
        Ok(())
    })
    .unwrap();
}

#[test]
fn a_paths_reusable_attempt_is_claimed_before_an_older_one_without_a_source() {
    let (_d, s) = store();
    s.write_sync(|w| {
        let first = run(w, head("h1", "feature", "d1"), "success", T0)?;
        register(w, head("older", "older", "d0"), 10, T1)?;
        register(w, head("h2", "feature", "d1"), 10, T1)?;
        let c = claim(w, "r", 60, T1)?.unwrap();
        assert_eq!(c.attempt.sha, "h2");
        assert_eq!(offered(&c), Some((first.id.as_str(), Basis::Paths)));
        Ok(())
    })
    .unwrap();
}

#[test]
fn retries_execute_even_with_a_source_of_the_same_paths_contents() {
    let (_d, s) = store();
    s.write_sync(|w| {
        run(w, head("h1", "feature", "d1"), "success", T0)?;
        let failed = run(w, head("h2", "feature", "d1"), "failure", T1)?;
        retry(w, &failed.id, 10, T1)?;
        assert_eq!(offered(&claim(w, "r", 60, T2)?.unwrap()), None);
        Ok(())
    })
    .unwrap();
}
