//! Frozen shared patrol decisions; GitHub GraphQL parsing/query tests belong to its adapter.
use chrono::{DateTime, Utc};
use gb_integrations::*;
use gb_ports::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
};
const ME: &str = "local-aiko";
const OTHER: &str = "other";
const FEEDBACK: Option<&str> = Some("pull_request_feedback");
const REVIEW: Option<&str> = Some("pull_request_review");
const LIMIT: Option<&str> = Some("pull_request_review_limit");
fn now() -> DateTime<Utc> {
    "2026-11-05T00:00:00Z".parse().unwrap()
}
fn own() -> PatrolPullRequest {
    PatrolPullRequest {
        id: "1".into(),
        url: "local://code/acme/repo/pulls/1".into(),
        author: ME.into(),
        open: true,
        head_oid: "head-2".into(),
        created_at: "2026-01-01T00:00:00Z".into(),
        ..Default::default()
    }
}
fn theirs() -> PatrolPullRequest {
    PatrolPullRequest {
        author: OTHER.into(),
        ..own()
    }
}
fn work(pr: &PatrolPullRequest) -> Option<&'static str> {
    pull_request_work(pr, ME, now())
}
fn review(author: &str, head: &str, at: &str, body: &str) -> PatrolReview {
    PatrolReview {
        author: author.into(),
        body: body.into(),
        commit_oid: head.into(),
        submitted_at: at.into(),
        ..Default::default()
    }
}
fn comment(author: &str, at: &str) -> PatrolComment {
    PatrolComment {
        author: author.into(),
        created_at: at.into(),
        ..Default::default()
    }
}
fn thread(authors: &[&str], at: &str) -> PatrolThread {
    PatrolThread {
        participants: authors.iter().map(|v| v.to_string()).collect(),
        last_author: authors.last().unwrap().to_string(),
        last_created_at: at.into(),
        ..Default::default()
    }
}
fn status(reason: &str, extra: Value) -> WorkflowStatus {
    let mut p =
        json!({"kind":"workflow_error","reason":reason,"person_id":"aiko","run_id":"run-1"});
    p.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    WorkflowStatus::from_payload(&p).unwrap()
}
fn notice(author: &str, at: &str, reason: &str) -> PatrolComment {
    PatrolComment {
        status: Some(status(reason, json!({}))),
        ..comment(author, at)
    }
}
fn rounds(n: usize) -> Vec<PatrolReview> {
    (1..=n)
        .map(|i| review(ME, &format!("round-{i}"), &format!("{i:02}"), ""))
        .collect()
}
fn statement(pr: &mut PatrolPullRequest, kind: &str, at: &str, reactors: &[&str]) {
    let reactors: BTreeSet<_> = reactors.iter().map(|v| v.to_string()).collect();
    match kind {
        "thread" => pr.threads.push(PatrolThread {
            last_reactors: reactors,
            ..thread(&[ME, OTHER], at)
        }),
        "review" => pr.reviews.push(PatrolReview {
            reactors,
            ..review(OTHER, "head-2", at, "Advice")
        }),
        "comment" => pr.comments.push(PatrolComment {
            reactors,
            ..comment(OTHER, at)
        }),
        _ => unreachable!(),
    }
}

#[test]
fn author_feedback_and_answers() {
    let mut pr = own();
    assert_eq!(work(&pr), None);
    pr.threads.push(thread(&[OTHER], "02"));
    assert_eq!(work(&pr), FEEDBACK);
    for answered in 0..3 {
        let mut p = pr.clone();
        match answered {
            0 => p.threads[0].resolved = true,
            1 => p.threads[0].last_author = ME.into(),
            _ => {
                p.threads[0].last_reactors.insert(ME.into());
            }
        }
        assert_eq!(work(&p), None);
    }
    let mut pr = own();
    pr.comments.push(comment(OTHER, "03"));
    assert_eq!(work(&pr), FEEDBACK);
    pr.comments.push(comment(ME, "04"));
    assert_eq!(work(&pr), None);
    let mut pr = own();
    pr.reviews.push(review(OTHER, "head-2", "03", "Fix"));
    assert_eq!(work(&pr), FEEDBACK);
    pr.reviews.push(review(ME, "head-2", "04", ""));
    assert_eq!(work(&pr), None);
    for body in ["", "   "] {
        let mut p = own();
        p.reviews.push(review(OTHER, "head-2", "03", body));
        assert_eq!(work(&p), None);
    }
    let mut pr = own();
    pr.reviews
        .push(review(OTHER, "head-2", "03", "LGTM; maybe also"));
    assert_eq!(work(&pr), FEEDBACK);
    pr.comments.push(comment(ME, "04"));
    assert_eq!(work(&pr), None);
}
#[test]
fn each_statement_is_answered_individually_by_only_my_reaction() {
    for kind in ["thread", "review", "comment"] {
        for reactors in [vec![], vec!["someone-else"], vec![ME]] {
            let mut p = own();
            statement(&mut p, kind, "05", &reactors);
            assert_eq!(
                work(&p),
                if reactors.contains(&ME) {
                    None
                } else {
                    FEEDBACK
                },
                "{kind}/{reactors:?}"
            );
        }
    }
    let mut p = own();
    statement(&mut p, "review", "03", &[ME]);
    statement(&mut p, "comment", "04", &[]);
    assert_eq!(work(&p), FEEDBACK);
}
#[test]
fn notices_are_neither_statements_nor_answers() {
    let mut p = own();
    p.comments.push(comment(OTHER, "03"));
    let mut limited = notice(ME, "04", "rate_limited");
    limited.status = Some(status(
        "rate_limited",
        json!({"retry_after_at":"2000-01-01T00:00:00Z"}),
    ));
    p.comments.push(limited);
    assert_eq!(work(&p), FEEDBACK);
    let mut p = own();
    p.comments.push(notice(OTHER, "03", "review_limit"));
    assert_eq!(work(&p), None);
}
#[test]
fn holds_lift_for_thread_review_comment_or_ready_but_not_other_notices() {
    for kind in ["thread", "review", "comment"] {
        let mut p = own();
        statement(&mut p, kind, "02", &[]);
        p.comments.push(notice(ME, "03", "failed"));
        assert_eq!(work(&p), None);
        statement(&mut p, kind, "04", &[]);
        assert_eq!(work(&p), FEEDBACK, "{kind}");
    }
    let mut p = theirs();
    p.reviews = rounds(3);
    p.comments = vec![notice(ME, "05", "failed"), notice("bob", "06", "failed")];
    assert_eq!(work(&p), None);
    p.comments.push(comment(OTHER, "07"));
    assert_eq!(work(&p), LIMIT);
    let mut p = theirs();
    p.comments.push(notice(ME, "03", "failed"));
    p.threads.push(thread(&[ME, OTHER], "04"));
    assert_eq!(work(&p), REVIEW);
}
#[test]
fn reviewer_requests_heads_and_thread_participation() {
    let mut p = theirs();
    p.threads.push(thread(&[OTHER], "02"));
    assert_eq!(work(&p), None);
    p.requested_reviewers.insert(ME.into());
    assert_eq!(work(&p), REVIEW);
    p.requested_reviewers.clear();
    p.threads.clear();
    p.reviews.push(review(ME, "head-2", "02", "LGTM"));
    assert_eq!(work(&p), None);
    p.head_oid = "head-3".into();
    assert_eq!(work(&p), REVIEW);
    let mut p = theirs();
    p.threads.push(thread(&[ME, OTHER], "02"));
    assert_eq!(work(&p), REVIEW);
    p.threads[0].last_author = ME.into();
    assert_eq!(work(&p), None);
    let mut p = theirs();
    p.reviews.push(review(ME, "head-1", "02", ""));
    p.reviews.push(PatrolReview {
        reply_only: true,
        ..review(ME, "head-2", "03", "")
    });
    assert_eq!(review_rounds(&p, ME), BTreeSet::from(["head-1"]));
    assert_eq!(work(&p), REVIEW);
    let mut p = theirs();
    p.reviews = vec![
        review(ME, "round-1", "01", ""),
        review(ME, "head-2", "02", ""),
    ];
    p.threads.push(thread(&[ME, OTHER], "03"));
    assert_eq!(work(&p), REVIEW);
}
#[test]
fn review_limit_round_counts_restart_only_count_new_heads() {
    for n in [2, 3] {
        let mut p = theirs();
        p.reviews = rounds(n);
        assert_eq!(work(&p), if n == 2 { REVIEW } else { LIMIT });
    }
    for author in [ME, "bob"] {
        let mut p = theirs();
        p.reviews = rounds(4);
        p.reviews.extend([
            review(ME, "after-5", "05a", ""),
            review(ME, "after-6", "06a", ""),
        ]);
        assert_eq!(review_rounds(&p, ME).len(), 6);
        p.comments.push(notice(author, "05", "review_limit"));
        assert_eq!(
            review_rounds(&p, ME),
            BTreeSet::from(["after-5", "after-6"])
        );
        assert_eq!(work(&p), REVIEW);
        p.reviews.push(review(ME, "after-7", "07a", ""));
        assert_eq!(work(&p), LIMIT);
    }
    for trigger in ["commits", "thread", "comment"] {
        let mut p = theirs();
        p.reviews = rounds(3);
        if trigger != "commits" {
            statement(&mut p, trigger, "06", &[]);
        }
        assert_eq!(work(&p), LIMIT);
        p.comments.insert(0, notice(ME, "05", "review_limit"));
        p.is_draft = true;
        assert_eq!(work(&p), None);
        p.is_draft = false;
        assert_eq!(work(&p), REVIEW);
    }
    for head in ["round-3", "round-4"] {
        let mut p = theirs();
        p.reviews = rounds(3);
        p.head_oid = head.into();
        p.requested_reviewers.insert(ME.into());
        assert_eq!(work(&p), REVIEW);
    }
}
#[test]
fn marking_ready_restarts_holds_and_rounds_in_both_handover_orders() {
    for author in [ME, OTHER] {
        for failed_first in [true, false] {
            let mut p = own();
            p.author = author.into();
            if author == OTHER {
                p.reviews = rounds(3);
            }
            p.threads.push(thread(&["reviewer"], "04"));
            p.comments = if failed_first {
                vec![
                    notice(ME, "05", "failed"),
                    notice("bob", "06", "review_limit"),
                ]
            } else {
                vec![
                    notice("bob", "05", "review_limit"),
                    notice(ME, "06", "failed"),
                ]
            };
            assert_eq!(work(&p), None);
            p.is_draft = true;
            assert_eq!(work(&p), None);
            p.is_draft = false;
            p.ready_at = "07".into();
            assert_eq!(work(&p), if author == ME { FEEDBACK } else { REVIEW });
        }
    }
    let mut p = theirs();
    p.reviews = rounds(3);
    p.ready_at = "05".into();
    p.comments = vec![
        notice("bob", "04", "review_limit"),
        notice(ME, "06", "failed"),
    ];
    assert_eq!(work(&p), None);
    let mut p = theirs();
    p.reviews = rounds(3);
    p.comments.push(notice(ME, "05", "failed"));
    assert_eq!(work(&p), None);
    p.ready_at = "06".into();
    assert!(review_rounds(&p, ME).is_empty());
    assert_eq!(work(&p), REVIEW);
}
#[test]
fn reviewer_statements_use_full_history_and_ignore_empty_approvals() {
    for kind in ["thread", "review", "comment"] {
        for reactors in [vec![], vec!["someone-else"], vec![ME]] {
            let mut p = theirs();
            p.reviews.push(review(ME, "head-2", "02", "LGTM"));
            statement(&mut p, kind, "05", &reactors);
            assert_eq!(work(&p), if reactors.contains(&ME) { None } else { REVIEW });
        }
    }
    for kind in ["review", "comment"] {
        let mut p = theirs();
        statement(&mut p, kind, "01", &[]);
        p.reviews.push(review(ME, "head-2", "02", ""));
        assert_eq!(work(&p), None);
    }
    let mut p = theirs();
    statement(&mut p, "review", "05", &[]);
    statement(&mut p, "comment", "05", &[]);
    assert_eq!(work(&p), None);
    let mut p = theirs();
    p.reviews.push(review(ME, "head-2", "02", ""));
    p.reviews.push(review("bob", "head-2", "05", ""));
    p.comments.push(notice(OTHER, "05", "failed"));
    assert_eq!(work(&p), None);
    for kind in ["thread", "review", "comment"] {
        for head in ["round-3", "round-1", "round-4"] {
            let mut p = theirs();
            p.reviews = rounds(3);
            p.head_oid = head.into();
            statement(&mut p, kind, "05", &[]);
            assert_eq!(work(&p), if head == "round-4" { LIMIT } else { REVIEW });
        }
    }
    let mut p = theirs();
    p.reviews = rounds(3);
    p.reviews
        .extend((5..=7).map(|i| review(ME, &format!("after-{i}"), &format!("{i:02}a"), "")));
    p.head_oid = "round-1".into();
    p.ready_at = "04".into();
    p.comments.push(comment(OTHER, "08"));
    assert_eq!(review_rounds(&p, ME).len(), 3);
    assert_eq!(work(&p), REVIEW);
}

struct NoSecrets;
impl gb_config::secrets::Keychain for NoSecrets {
    fn read(&self) -> anyhow::Result<Option<String>> {
        Ok(None)
    }
    fn create(&self, _: &str) -> anyhow::Result<()> {
        anyhow::bail!("the local provider requires no keychain access")
    }
}
struct AwaitedFixture {
    _dir: tempfile::TempDir,
    code: Arc<dyn CodeHostingService>,
    bare: PathBuf,
    checks: PathBuf,
    url: String,
    head: String,
}
fn git(at: &Path, args: &[&str]) -> String {
    let mut command = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    let output = command
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
        ])
        .args(args)
        .current_dir(at)
        .env("HOME", at)
        .env("USERPROFILE", at)
        .env("XDG_CONFIG_HOME", at)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env(
            "GIT_CONFIG_GLOBAL",
            if cfg!(windows) { "NUL" } else { "/dev/null" },
        )
        .env("GIT_ALLOW_PROTOCOL", "file")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}
impl AwaitedFixture {
    async fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let service_root = root.join("services");
        let repo = service_root.join("code/acme%2Frepo");
        let bare = repo.join("repository.git");
        fs::create_dir_all(&repo).unwrap();
        git(
            root,
            &[
                "init",
                "--bare",
                "--initial-branch=main",
                bare.to_str().unwrap(),
            ],
        );
        let tree = git(&bare, &["mktree"]);
        let head = git(&bare, &["commit-tree", &tree, "-m", "initial"]);
        for branch in ["main", "feature"] {
            git(
                &bare,
                &["update-ref", &format!("refs/heads/{branch}"), &head],
            );
        }
        let writes = gb_base::shared_writes::SharedWrites::new(root.join("identity"), None);
        let keys = NoSecrets;
        let secrets = gb_config::secrets::SecretStore::new(
            &writes,
            gb_config::secrets::Scope::Identity,
            gb_config::secrets::DeviceKey {
                machine_root: root.join("machine"),
                keychain: &keys,
            },
        )
        .unwrap();
        let factory = ServiceIntegrationFactory::new(
            IntegrationConfig {
                local_root: service_root,
                identity_root: root.join("identity"),
                person_id: "aiko".into(),
                display_name: "Aiko".into(),
                accounts: Default::default(),
                ticket_provider: "local".into(),
                code_provider: "local".into(),
                chat_provider: "local".into(),
                ticket_scope: "acme".into(),
                code_scope: "acme".into(),
                checks: "pending".into(),
                slack_base_url: String::new(),
            },
            &secrets,
            None,
            Arc::new(|_| {}),
        )
        .unwrap();
        let code = factory.create_code_hosting_service().unwrap();
        let url = code
            .create_pull_request(CreatePullRequest {
                repository: "acme/repo".into(),
                head: "feature".into(),
                base: "main".into(),
                title: "Title".into(),
                body: "Body".into(),
                draft: false,
                tickets: vec![],
            })
            .await
            .unwrap()
            .target
            .url;
        let wait = status(
            "awaiting_checks",
            json!({"head":head,"work_kind":"ticket","work_identity":"local://tickets/acme/team/T-1","subject":"local://tickets/acme/team/T-1"}),
        );
        code.comment(&url, "Waiting for CI", Some(&wait))
            .await
            .unwrap();
        Self {
            _dir: dir,
            code,
            bare,
            checks: repo.join("checks"),
            url,
            head,
        }
    }
    fn conclude(&self, value: &str) {
        fs::create_dir_all(&self.checks).unwrap();
        fs::write(self.checks.join(&self.head), value).unwrap();
    }
    async fn woken(&self) -> Vec<Task> {
        let tasks = self.code.pull_request_candidates().await.unwrap();
        for task in &tasks {
            assert_eq!(
                self.code.refresh_pull_request(task).await.unwrap().as_ref(),
                Some(task)
            );
        }
        tasks
    }
    async fn notice(&self, reason: &str, extra: Value) {
        self.code
            .comment(&self.url, "Notice", Some(&status(reason, extra)))
            .await
            .unwrap();
    }
    fn assert_resumes(&self, task: &Task, reason: &str) {
        assert_eq!(task.url.as_deref(), Some(self.url.as_str()));
        assert_eq!(task.pull_request_url.as_deref(), Some(self.url.as_str()));
        assert_eq!(task.trigger_reason.as_deref(), Some(reason));
        assert_eq!(
            task.resumes,
            Some(AwaitedRun {
                run_id: "run-1".into(),
                work_kind: "ticket".into(),
                work_identity: "local://tickets/acme/team/T-1".into()
            })
        );
    }
}
#[tokio::test]
async fn no_work_while_checks_pending() {
    let f = AwaitedFixture::new().await;
    assert!(f.woken().await.is_empty());
}
#[tokio::test]
async fn success_and_failure_checks_both_resume_the_member() {
    for conclusion in ["success", "failure"] {
        let f = AwaitedFixture::new().await;
        f.conclude(conclusion);
        let tasks = f.woken().await;
        assert_eq!(tasks.len(), 1);
        f.assert_resumes(&tasks[0], "pull_request_checks");
    }
}
#[tokio::test]
async fn new_head_resumes_work_even_with_pending_checks() {
    let f = AwaitedFixture::new().await;
    let tree = git(&f.bare, &["mktree"]);
    let next = git(
        &f.bare,
        &["commit-tree", &tree, "-p", &f.head, "-m", "more"],
    );
    git(&f.bare, &["update-ref", "refs/heads/feature", &next]);
    let tasks = f.woken().await;
    assert_eq!(tasks.len(), 1);
    f.assert_resumes(&tasks[0], "pull_request_checks");
}
#[tokio::test]
async fn closed_merged_or_draft_prs_never_resume() {
    for (state, merged, draft) in [
        ("closed", true, false),
        ("closed", false, false),
        ("open", false, true),
    ] {
        let f = AwaitedFixture::new().await;
        f.conclude("success");
        let p = f.bare.parent().unwrap().join("pulls/1.json");
        let mut v: Value = serde_json::from_slice(&fs::read(&p).unwrap()).unwrap();
        v["state"] = json!(state);
        v["merged"] = json!(merged);
        v["draft"] = json!(draft);
        fs::write(p, serde_json::to_vec(&v).unwrap()).unwrap();
        assert!(f.woken().await.is_empty());
    }
}
#[tokio::test]
async fn only_next_notice_ends_wait_and_ordinary_comments_do_not() {
    let f = AwaitedFixture::new().await;
    f.conclude("success");
    f.code.comment(&f.url, "CI passed", None).await.unwrap();
    assert_eq!(f.woken().await.len(), 1);
    f.notice("checks_settled", json!({})).await;
    assert!(f.woken().await.is_empty());
}
#[tokio::test]
async fn someone_elses_wait_does_not_resume_my_work() {
    let f = AwaitedFixture::new().await;
    f.conclude("success");
    let p = f.bare.parent().unwrap().join("pulls/1.json");
    let mut v: Value = serde_json::from_slice(&fs::read(&p).unwrap()).unwrap();
    v["author"] = json!(OTHER);
    fs::write(p, serde_json::to_vec(&v).unwrap()).unwrap();
    assert!(f.woken().await.is_empty());
}
#[tokio::test]
async fn failed_check_read_waits_without_hiding_another_pr_feedback() {
    let f = AwaitedFixture::new().await;
    fs::create_dir_all(f.checks.join(&f.head)).unwrap();
    git(&f.bare, &["update-ref", "refs/heads/feedback", &f.head]);
    let other = f
        .code
        .create_pull_request(CreatePullRequest {
            repository: "acme/repo".into(),
            head: "feedback".into(),
            base: "main".into(),
            title: "Other".into(),
            body: "".into(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap()
        .target
        .url;
    let p = f.bare.parent().unwrap().join("pulls/2.json");
    let mut v: Value = serde_json::from_slice(&fs::read(&p).unwrap()).unwrap();
    v["comments"] = json!([{"id":"comment-1","author":OTHER,"body":"Please fix","created_at":"2026-11-05T00:00:00Z","reactions":[]}]);
    fs::write(p, serde_json::to_vec(&v).unwrap()).unwrap();
    let tasks = f.woken().await;
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].url.as_deref(), Some(other.as_str()));
    assert_eq!(
        tasks[0].trigger_reason.as_deref(),
        Some("pull_request_feedback")
    );
    fs::remove_dir(f.checks.join(&f.head)).unwrap();
    f.conclude("success");
    assert!(
        f.woken()
            .await
            .iter()
            .any(|t| t.url.as_deref() == Some(f.url.as_str()))
    );
}
#[tokio::test]
async fn missing_head_resumes_nothing() {
    let f = AwaitedFixture::new().await;
    git(&f.bare, &["update-ref", "-d", "refs/heads/feature"]);
    assert!(f.woken().await.is_empty());
}
#[tokio::test]
async fn resumed_rate_limited_run_waits_for_reset() {
    for (retry, resumed) in [
        ("2000-01-01T00:00:00Z", true),
        ("2999-01-01T00:00:00Z", false),
    ] {
        let f = AwaitedFixture::new().await;
        f.conclude("success");
        f.notice("rate_limited", json!({"retry_after_at":retry}))
            .await;
        let tasks = f.woken().await;
        assert_eq!(tasks.len(), usize::from(resumed));
        if resumed {
            f.assert_resumes(&tasks[0], "pull_request_checks");
        }
    }
}
#[tokio::test]
async fn feedback_and_later_checks_both_resume_original_work() {
    let f = AwaitedFixture::new().await;
    let p = f.bare.parent().unwrap().join("pulls/1.json");
    let mut v: Value = serde_json::from_slice(&fs::read(&p).unwrap()).unwrap();
    v["comments"].as_array_mut().unwrap().push(json!({"id":"comment-other","author":OTHER,"body":"Please fix","created_at":"2026-01-01T00:00:00Z","reactions":[]}));
    fs::write(p, serde_json::to_vec(&v).unwrap()).unwrap();
    let tasks = f.woken().await;
    assert_eq!(tasks.len(), 1);
    f.assert_resumes(&tasks[0], "pull_request_feedback");
    f.code.comment(&f.url, "Thanks", None).await.unwrap();
    f.conclude("success");
    let tasks = f.woken().await;
    assert_eq!(tasks.len(), 1);
    f.assert_resumes(&tasks[0], "pull_request_checks");
}
#[test]
fn a_wait_names_the_work_it_resumes() {
    assert!(
        WorkflowStatus::from_payload(
            &json!({"kind":"workflow_error","reason":"awaiting_checks","person_id":"aiko"})
        )
        .is_none()
    );
    assert!(
        serde_json::from_value::<WorkflowStatus>(
            json!({"reason":"awaiting_checks","person_id":"aiko","run_id":"run-1"})
        )
        .is_err()
    );
}

// Frozen origin/python-host test_pull_request_patrol.py -> actual Rust test names.
// Parameter families are explicit loops above. Review verdict is intentionally absent
// from PatrolReview: a nonempty body is feedback in every verdict state.
// test_parse_lower_cases_logins_and_orders_by_time -> EXCLUDED: GitHub GraphQL parser/query/identity adapter, outside shared typed patrol snapshots
// test_graphql_bot_logins_match_the_members_bot_username -> EXCLUDED: GitHub GraphQL parser/query/identity adapter, outside shared typed patrol snapshots
// test_query_asks_for_everything_the_decision_reads -> EXCLUDED: GitHub GraphQL parser/query/identity adapter, outside shared typed patrol snapshots
// test_own_pr_without_activity_asks_nothing -> author_feedback_and_answers
// test_own_pr_with_thread_answered_by_someone_else_is_feedback -> author_feedback_and_answers
// test_own_pr_thread_is_answered -> author_feedback_and_answers
// test_own_pr_with_new_conversation_comment_is_feedback -> author_feedback_and_answers
// test_own_pr_comment_answered_by_my_later_reply_asks_nothing -> author_feedback_and_answers
// test_own_pr_review_summary_is_feedback_until_i_reply -> author_feedback_and_answers
// test_own_pr_review_body_is_feedback_in_any_state -> author_feedback_and_answers
// test_own_pr_review_without_a_body_is_not_feedback -> author_feedback_and_answers
// test_own_pr_statement_is_answered_by_my_reaction -> each_statement_is_answered_individually_by_only_my_reaction
// test_own_pr_statements_are_answered_one_by_one -> each_statement_is_answered_individually_by_only_my_reaction
// test_own_pr_approval_body_is_answered_by_my_later_reply -> author_feedback_and_answers
// test_own_pr_status_notices_are_neither_feedback_nor_replies -> notices_are_neither_statements_nor_answers
// test_failed_notice_as_my_latest_comment_suppresses_earlier_feedback -> holds_lift_for_thread_review_comment_or_ready_but_not_other_notices
// test_activity_after_a_failure_notice_lifts_the_suppression -> holds_lift_for_thread_review_comment_or_ready_but_not_other_notices
// test_another_members_notice_does_not_lift_my_failure_hold -> holds_lift_for_thread_review_comment_or_ready_but_not_other_notices
// test_thread_reply_after_a_failure_notice_restarts_review_work -> holds_lift_for_thread_review_comment_or_ready_but_not_other_notices
// test_someone_elses_pr_i_never_touched_asks_nothing -> reviewer_requests_heads_and_thread_participation
// test_requested_review_is_review_work -> reviewer_requests_heads_and_thread_participation
// test_submitted_review_consumes_the_request_and_settles_the_pr -> reviewer_requests_heads_and_thread_participation
// test_reply_in_my_thread_is_review_work -> reviewer_requests_heads_and_thread_participation
// test_new_commits_after_my_review_are_review_work -> reviewer_requests_heads_and_thread_participation
// test_thread_replies_do_not_count_as_reviewing_the_new_head -> reviewer_requests_heads_and_thread_participation
// test_re_review_stops_at_the_round_limit -> review_limit_round_counts_restart_only_count_new_heads
// test_rounds_are_counted_from_the_last_limit_notice -> review_limit_round_counts_restart_only_count_new_heads
// test_the_limit_notice_starts_the_count_over_once_the_pr_is_ready -> review_limit_round_counts_restart_only_count_new_heads
// test_the_limit_is_reached_again_three_rounds_after_the_notice -> review_limit_round_counts_restart_only_count_new_heads
// test_marking_ready_lifts_a_failure_hold_from_before -> marking_ready_restarts_holds_and_rounds_in_both_handover_orders
// test_a_failure_after_marking_ready_still_holds -> marking_ready_restarts_holds_and_rounds_in_both_handover_orders
// test_marking_ready_without_a_notice_starts_the_count_over -> marking_ready_restarts_holds_and_rounds_in_both_handover_orders
// test_explicit_request_outlives_the_limit_on_any_head -> review_limit_round_counts_restart_only_count_new_heads
// test_thread_reply_before_the_review_limit_is_review_work -> reviewer_requests_heads_and_thread_participation
// test_statement_after_my_review_is_review_work_until_answered -> reviewer_statements_use_full_history_and_ignore_empty_approvals
// test_statement_before_my_review_was_answered_by_it -> reviewer_statements_use_full_history_and_ignore_empty_approvals
// test_statements_on_a_pr_i_never_spoke_on_ask_nothing -> reviewer_statements_use_full_history_and_ignore_empty_approvals
// test_reviewer_is_not_started_by_empty_approvals_or_notices -> reviewer_statements_use_full_history_and_ignore_empty_approvals
// test_statements_at_the_limit_only_stop_for_an_unreviewed_head -> reviewer_statements_use_full_history_and_ignore_empty_approvals
// test_returning_to_a_head_reviewed_before_ready_does_not_hit_the_limit -> reviewer_statements_use_full_history_and_ignore_empty_approvals
