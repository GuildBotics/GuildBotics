//! Provider-boundary readiness cases from the frozen Branvil code service.
use super::*;

async fn upload(base: &str, runner: &str, attempt: &Value, suffix: &str, body: &[u8]) {
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .unwrap();
    let url = format!(
        "{base}/api/v1/runner/attempts/{}{suffix}",
        attempt["attempt_id"].as_str().unwrap()
    );
    let request = if suffix == "/log" {
        client.post(url)
    } else {
        client.put(url)
    };
    let response = request
        .bearer_auth(runner)
        .header("x-branvil-lease", attempt["lease"].as_str().unwrap())
        .body(body.to_vec())
        .send()
        .await
        .unwrap();
    assert!(response.status().is_success(), "{}", response.status());
}

async fn finish_claim(base: &str, runner: &str, attempt: &Value, conclusion: &str) {
    api_call(
        base,
        &format!(
            "/runner/attempts/{}/finish",
            attempt["attempt_id"].as_str().unwrap()
        ),
        runner,
        json!({"lease":attempt["lease"],"conclusion":conclusion,"image_digest":format!("sha256:{}","2".repeat(64))}),
    )
    .await;
}

async fn fixture(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &dyn CodeHostingService,
    owner_token: &str,
    name: &str,
    jobs: &[&str],
) -> (Git, String, String, PullRequestCreated) {
    let repo = format!("acme/readiness-{name}");
    owner
        .repos_create(input(json!({"path":repo})))
        .await
        .unwrap();
    let git = Git::new(&root.join(name));
    let main = git.commit("", "README.md", "base\n");
    let remote = code.clone_url(&repo).await.unwrap();
    git.push(&remote, &main, "main", "owner", owner_token);
    let pipeline = jobs.iter().map(|job| format!("[[jobs]]\nname = \"{job}\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\nartifacts = [{{name = \"report\", paths = [\"report.txt\"]}}]\n")).collect::<String>();
    let head = git.commit(&main, ".branvil/ci.toml", &pipeline);
    let credential = code.git_credential().await.unwrap().unwrap();
    git.push(
        &remote,
        &head,
        "feature",
        &credential.username,
        &credential.password,
    );
    let pull = code
        .create_pull_request(CreatePullRequest {
            repository: repo.clone(),
            head: "feature".into(),
            base: "main".into(),
            title: name.into(),
            body: String::new(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap();
    (git, repo, head, pull)
}

async fn claim_head(base: &str, runner: &str, head: &str) -> Value {
    for _ in 0..100 {
        let attempt = next_claim(base, runner).await;
        if attempt["sha"] == head {
            return attempt;
        }
        finish_claim(base, runner, &attempt, "success").await;
    }
    panic!("readiness fixture head was never claimed");
}

pub(super) async fn primary_failure_logs_and_cancelled_fallback(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &dyn CodeHostingService,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    for only_cancelled in [false, true] {
        let name = if only_cancelled {
            "cancelled"
        } else {
            "primary"
        };
        let jobs: &[&str] = if only_cancelled {
            &["lint"]
        } else {
            &["test", "lint", "build"]
        };
        let (_, _, head, pull) = fixture(root, owner, code, owner_token, name, jobs).await;
        let mut expected_id = String::new();
        for _ in jobs {
            let attempt = claim_head(base, runner, &head).await;
            let job = attempt["job"]["name"].as_str().unwrap();
            let conclusion = match job {
                "test" => "failure",
                "lint" => "cancelled",
                _ => "success",
            };
            let log = match job {
                "test" => "boom",
                "lint" => "stopped",
                _ => "built",
            };
            upload(base, runner, &attempt, "/log", log.as_bytes()).await;
            if job == "test" {
                upload(
                    base,
                    runner,
                    &attempt,
                    "/artifacts/report",
                    b"PK\x03\x04report",
                )
                .await;
            }
            if job == "test" || only_cancelled {
                expected_id = attempt["attempt_id"].as_str().unwrap().into();
            }
            finish_claim(base, runner, &attempt, conclusion).await;
        }
        let readiness = code
            .readiness(&pull.target.url, true, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap();
        assert_eq!(readiness.rollup, ReadinessRollup::Failure);
        assert_eq!(readiness.readiness, ReadinessReadiness::Blocked);
        assert_eq!(
            readiness
                .completion_blockers
                .iter()
                .map(|b| b.code.as_str())
                .collect::<Vec<_>>(),
            ["checks_failed"]
        );
        let logs = readiness.failed_logs.unwrap();
        assert_eq!(logs.len(), 1);
        let log = &logs[0];
        assert_eq!(log.job_id, expected_id);
        assert_eq!(log.name, if only_cancelled { "lint" } else { "test" });
        assert_eq!(log.log, if only_cancelled { "stopped" } else { "boom" });
        assert_eq!(
            log.conclusion,
            if only_cancelled {
                "cancelled"
            } else {
                "failure"
            }
        );
        assert_eq!(
            log.artifact_names,
            if only_cancelled {
                vec![]
            } else {
                vec!["report".to_string()]
            }
        );
        assert!(!log.truncated);
        assert_eq!(log.log_bytes, log.log.len() as i64);
        assert_eq!(log.tail_limit_bytes, MAX_LOG_TAIL_BYTES as i64);
    }
}

pub(super) async fn ten_failed_logs_fit_one_page(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &dyn CodeHostingService,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let names = (0..10).map(|n| format!("job{n}")).collect::<Vec<_>>();
    let names = names.iter().map(String::as_str).collect::<Vec<_>>();
    let (_, _, head, pull) = fixture(root, owner, code, owner_token, "budget", &names).await;
    let mut ids = std::collections::BTreeMap::new();
    for _ in 0..10 {
        let attempt = claim_head(base, runner, &head).await;
        ids.insert(
            attempt["job"]["name"].as_str().unwrap().to_owned(),
            attempt["attempt_id"].as_str().unwrap().to_owned(),
        );
        upload(base, runner, &attempt, "/log", &vec![b'x'; MAX_PAGE_BYTES]).await;
        finish_claim(base, runner, &attempt, "failure").await;
    }
    let page = code
        .read(
            Resource::PullRequestReadiness,
            &pull.target.url,
            Some(&json!({"failed_logs":true})),
            "",
        )
        .await
        .unwrap();
    assert!(serde_json::to_vec(&page).unwrap().len() <= MAX_PAGE_BYTES);
    let logs = page.items()[0]["failed_logs"].as_array().unwrap();
    assert_eq!(logs.len(), 10);
    for log in logs {
        assert_eq!(log["job_id"], ids[log["name"].as_str().unwrap()]);
        assert_eq!(log["truncated"], true);
        assert_eq!(log["tail_limit_bytes"], log["log_bytes"]);
        assert_eq!(
            log["log_bytes"].as_u64().unwrap() as usize,
            log["log"].as_str().unwrap().len()
        );
        assert!(log["log"].as_str().unwrap().bytes().all(|b| b == b'x'));
    }
}

pub(super) async fn pending_closed_and_advanced_heads(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &dyn CodeHostingService,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let (git, repo, head, pull) =
        fixture(root, owner, code, owner_token, "freshness", &["test"]).await;
    let pending = code
        .readiness(&pull.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(pending.head_sha, head);
    assert_eq!(pending.current_head_sha, head);
    assert_eq!(pending.readiness, ReadinessReadiness::Blocked);
    assert!(pending.checks_expected);
    assert_eq!(pending.completion_blockers.len(), 1);
    assert_eq!(pending.completion_blockers[0].code, "checks_pending");
    // Registration can still be pending immediately after the push.
    assert!(
        pending.completion_blockers[0].message.contains("pending")
            || pending.completion_blockers[0]
                .message
                .contains("not been registered")
    );
    let attempt = claim_head(base, runner, &head).await;
    finish_claim(base, runner, &attempt, "success").await;
    assert_eq!(
        code.readiness(&pull.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .readiness,
        ReadinessReadiness::Ready
    );
    let advanced = git.commit(&head, "next.txt", "new head\n");
    let credential = code.git_credential().await.unwrap().unwrap();
    git.push(
        &code.clone_url(&repo).await.unwrap(),
        &advanced,
        "feature",
        &credential.username,
        &credential.password,
    );
    let moved = code
        .readiness(&pull.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(moved.head_sha, advanced);
    assert_eq!(moved.current_head_sha, advanced);
    assert_ne!(moved.readiness, ReadinessReadiness::Ready);
    let number = pull.target.url.rsplit('/').next().unwrap().parse().unwrap();
    owner.pulls_set_open(&repo, number, false).await.unwrap();
    let closed = code
        .readiness(&pull.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(closed.readiness, ReadinessReadiness::NotApplicable);
    assert_eq!(closed.base_sha, None);
    assert!(!closed.checks_expected);
    assert!(closed.completion_blockers.is_empty());
    let attempt = claim_head(base, runner, &advanced).await;
    finish_claim(base, runner, &attempt, "success").await;
}

async fn author_yields_running_checks(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &dyn CodeHostingService,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let (_, repo, head, pull) = fixture(root, owner, code, owner_token, "yield", &["test"]).await;
    let attempt = claim_head(base, runner, &head).await;
    let n = pull.target.url.rsplit('/').next().unwrap().parse().unwrap();
    let current = owner.repos_get(&repo).await.unwrap();
    owner
        .repos_update(
            &repo,
            input(json!({"revision":current.revision,"required_approvals":1})),
        )
        .await
        .unwrap();
    owner
        .pulls_review(
            &repo,
            n,
            input(json!({"verdict":"request_changes","commit":head,"body":"Fix before landing"})),
        )
        .await
        .unwrap();
    assert_eq!(
        code.yield_pull_request_checks(&pull.target.url)
            .await
            .unwrap(),
        1
    );
    assert_eq!(
        code.yield_pull_request_checks(&pull.target.url)
            .await
            .unwrap(),
        0
    );
    finish_claim(base, runner, &attempt, "failure").await;
    let ready = code
        .readiness(&pull.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert!(
        ready
            .checks
            .iter()
            .all(|c| c.conclusion.as_deref() != Some("cancelled"))
    );
    let replacement = claim_head(base, runner, &head).await;
    assert_ne!(replacement["attempt_id"], attempt["attempt_id"]);
    finish_claim(base, runner, &replacement, "success").await;
}

pub(super) async fn contract(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &dyn CodeHostingService,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    author_yields_running_checks(root, owner, code, base, owner_token, runner).await;
    primary_failure_logs_and_cancelled_fallback(root, owner, code, base, owner_token, runner).await;
    ten_failed_logs_fit_one_page(root, owner, code, base, owner_token, runner).await;
    pending_closed_and_advanced_heads(root, owner, code, base, owner_token, runner).await;
}
