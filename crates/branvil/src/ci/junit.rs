//! Capture failure metadata while the attempt's declared JUnit artifact is available.

use std::collections::BTreeSet;
use std::fs::File;
use std::io::Read;
use std::path::Path;

use serde::{Deserialize, Serialize};

use super::queue::Attempt;
use crate::app::App;
use crate::error::AppResult;
use crate::store::{CI_TEST_FAILURES, Key, Rd, Wtx};

const MAX_REPORT_BYTES: u64 = 8 << 20;
const MAX_REPORT_NODES: u32 = 250_000;
const MAX_FAILURES_PER_REPORT: usize = 256;
const MAX_ATTEMPTS_PER_TEST: usize = 20;

pub(super) fn valid_test_name(name: &str) -> bool {
    !name.trim().is_empty() && name.len() <= 512 && !name.chars().any(char::is_control)
}

fn failed_names(xml: &str) -> Result<BTreeSet<String>, roxmltree::Error> {
    let doc = roxmltree::Document::parse_with_options(
        xml,
        roxmltree::ParsingOptions {
            nodes_limit: MAX_REPORT_NODES,
            ..Default::default()
        },
    )?;
    let root = doc.root_element();
    if !matches!(root.tag_name().name(), "testsuite" | "testsuites") {
        return Ok(BTreeSet::new());
    }
    let mut names = BTreeSet::new();
    for case in root.descendants().filter(|node| {
        node.has_tag_name("testcase")
            && node
                .parent()
                .is_some_and(|parent| parent.has_tag_name("testsuite"))
    }) {
        if let Some(name) = case.attribute("name").filter(|name| valid_test_name(name))
            && case
                .children()
                .any(|node| node.has_tag_name("failure") || node.has_tag_name("error"))
        {
            names.insert(name.to_owned());
            if names.len() == MAX_FAILURES_PER_REPORT {
                break;
            }
        }
    }
    Ok(names)
}

fn read_report(archive: &Path, entry: &str) -> anyhow::Result<BTreeSet<String>> {
    let mut zip = zip::ZipArchive::new(File::open(archive)?)?;
    let file = zip.by_name(entry)?;
    anyhow::ensure!(file.is_file() && file.size() <= MAX_REPORT_BYTES);
    let mut xml = String::new();
    file.take(MAX_REPORT_BYTES + 1).read_to_string(&mut xml)?;
    anyhow::ensure!(xml.len() as u64 <= MAX_REPORT_BYTES);
    Ok(failed_names(&xml)?)
}

pub(super) async fn read_attempt(app: &App, attempt: &Attempt) -> Option<BTreeSet<String>> {
    let (artifact, report) = attempt.job.artifacts.iter().find_map(|artifact| {
        artifact
            .junit
            .as_ref()
            .map(|report| (&artifact.name, report))
    })?;
    if attempt.files_pruned || !attempt.artifacts.iter().any(|a| &a.name == artifact) {
        return None;
    }
    let archive = app.ci.artifact_path(&attempt.id, artifact);
    let report = report.clone();
    match tokio::task::spawn_blocking(move || read_report(&archive, &report)).await {
        Ok(Ok(names)) => Some(names),
        _ => {
            tracing::warn!(attempt = %attempt.id, "Could not read the declared JUnit report");
            None
        }
    }
}

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
pub struct Failure {
    pub attempt_id: String,
    pub job: String,
    pub sha: String,
    pub runner: String,
    pub finished_at: String,
}

pub(super) fn history(r: &impl Rd, repo: &str, name: &str) -> AppResult<Vec<Failure>> {
    let key = Key::new().s(repo).s(name).bytes();
    Ok(r.get(CI_TEST_FAILURES, &key)?.unwrap_or_default())
}

fn record(w: &Wtx, repo: &str, name: &str, failure: &Failure) -> AppResult<()> {
    let mut failures = history(w, repo, name)?;
    if failures
        .iter()
        .any(|row| row.attempt_id == failure.attempt_id)
    {
        return Ok(());
    }
    failures.push(failure.clone());
    failures.sort_unstable_by(|a, b| {
        (&b.finished_at, &b.attempt_id).cmp(&(&a.finished_at, &a.attempt_id))
    });
    failures.truncate(MAX_ATTEMPTS_PER_TEST);
    w.put(
        CI_TEST_FAILURES,
        &Key::new().s(repo).s(name).bytes(),
        &failures,
    )
}

pub(super) fn record_attempt(w: &Wtx, a: &Attempt, names: &BTreeSet<String>) -> AppResult<()> {
    let (Some(runner), Some(finished_at)) = (&a.runner, &a.finished_at) else {
        return Ok(());
    };
    let row = Failure {
        attempt_id: a.id.clone(),
        job: a.job.name.clone(),
        sha: a.sha.clone(),
        runner: runner.clone(),
        finished_at: finished_at.clone(),
    };
    for name in names {
        record(w, &a.repo, name, &row)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    #[test]
    fn reads_only_failing_cases_and_preserves_the_junit_name() {
        let xml = r#"<testsuites><testsuite name="nextest">
            <testcase classname="crate::suite" name="server::works"/>
            <testcase classname="crate::suite" name="server::skipped"><skipped/></testcase>
            <testcase classname="crate::suite" name="server::failed[1] &amp; retries">
                <failure message="assertion failed"/>
            </testcase>
            <testcase name="server::failed[1] &amp; retries"><failure/></testcase>
            <testcase name="server::errored"><error/></testcase>
            <testcase name=""><failure/></testcase>
        </testsuite></testsuites>"#;
        assert_eq!(
            failed_names(xml).unwrap(),
            ["server::errored", "server::failed[1] & retries"]
                .map(String::from)
                .into()
        );
        assert_eq!(
            failed_names(r#"<testsuite><testcase name="単独"><failure/></testcase></testsuite>"#)
                .unwrap(),
            ["単独".into()].into()
        );
        assert!(failed_names("<testsuite><testcase>").is_err());
        assert!(
            failed_names(
                r#"<!DOCTYPE testsuite [<!ENTITY x "expanded">]>
                            <testsuite><testcase name="&x;"><failure/></testcase></testsuite>"#
            )
            .is_err()
        );
    }

    #[test]
    fn caps_the_number_of_distinct_failed_names_per_report() {
        use std::fmt::Write;
        let mut xml = String::from("<testsuite>");
        for i in 0..=MAX_FAILURES_PER_REPORT {
            write!(xml, r#"<testcase name="case{i}"><failure/></testcase>"#).unwrap();
        }
        xml.push_str("</testsuite>");
        let names = failed_names(&xml).unwrap();
        assert_eq!(names.len(), MAX_FAILURES_PER_REPORT);
        assert!(!names.contains(&format!("case{MAX_FAILURES_PER_REPORT}")));
    }

    #[test]
    fn retains_only_the_latest_distinct_attempts_per_test_and_repository() {
        let temp = tempfile::tempdir().unwrap();
        let store = Store::open(&temp.path().join("branvil.redb"), 1 << 20, 1).unwrap();
        let failure = |i: usize| Failure {
            attempt_id: format!("att_{i:02}"),
            job: "test".into(),
            sha: format!("sha_{i}"),
            runner: "linux".into(),
            finished_at: format!("2026-11-27T00:00:{i:02}.000Z"),
        };
        store
            .write_sync(|w| {
                for i in (0..=MAX_ATTEMPTS_PER_TEST).rev() {
                    record(w, "repo_1", "runner::test", &failure(i))?;
                }
                record(w, "repo_1", "runner::test", &failure(MAX_ATTEMPTS_PER_TEST))?;
                record(w, "repo_1", "other", &failure(0))?;
                record(w, "repo_2", "runner::test", &failure(0))
            })
            .unwrap();
        store
            .read_sync(|r| {
                let rows = history(r, "repo_1", "runner::test")?;
                assert_eq!(
                    rows.iter().map(|row| row.sha.clone()).collect::<Vec<_>>(),
                    (1..=MAX_ATTEMPTS_PER_TEST)
                        .rev()
                        .map(|i| format!("sha_{i}"))
                        .collect::<Vec<_>>()
                );
                assert_eq!(history(r, "repo_1", "other")?.len(), 1);
                assert_eq!(history(r, "repo_2", "runner::test")?.len(), 1);
                assert!(history(r, "repo_1", "Runner::test")?.is_empty());
                Ok(())
            })
            .unwrap();
    }
}
