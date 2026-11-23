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
    code: &CodeAgent,
    owner_token: &str,
    name: &str,
    jobs: &[&str],
) -> (Git, String, String, PullRequestCreated) {
    let repo = format!("acme/readiness-{name}");
    owner
        .repos_create(input(json!({"path":repo})))
        .await
        .unwrap();
    let revision = owner.repos_get(&repo).await.unwrap().revision;
    owner
        .repos_update(
            &repo,
            input(json!({"revision":revision,"required_checks":jobs})),
        )
        .await
        .unwrap();
    let git = Git::new(&root.join(name));
    let main = git.commit("", "README.md", "base\n");
    let remote = code.clone_url(&repo).await.unwrap();
    git.push(&remote, &main, "main", "owner", owner_token);
    let pipeline = jobs.iter().map(|job| format!("[[jobs]]\nname = \"{job}\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\nartifacts = [{{name = \"report\", paths = [\"report.txt\"]}}]\n")).collect::<String>();
    let head = git.commit(&main, ".guildbotics-ci.toml", &pipeline);
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
    code: &CodeAgent,
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
            .readiness(&pull.written.target.url, true, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap();
        assert_eq!(readiness.report.required_checks, RequiredChecks::Failed);
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
    code: &CodeAgent,
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
            &pull.written.target.url,
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
    code: &CodeAgent,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let (git, repo, head, pull) =
        fixture(root, owner, code, owner_token, "freshness", &["test"]).await;
    let pending = code
        .readiness(&pull.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(pending.report.head_sha, head);
    assert_eq!(pending.report.required_checks, RequiredChecks::Running);
    let attempt = claim_head(base, runner, &head).await;
    finish_claim(base, runner, &attempt, "success").await;
    assert_eq!(
        code.readiness(&pull.written.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .report
            .required_checks,
        RequiredChecks::Passed
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
        .readiness(&pull.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(moved.report.head_sha, advanced);
    assert_ne!(moved.report.required_checks, RequiredChecks::Passed);
    let number = pull
        .written
        .target
        .url
        .rsplit('/')
        .next()
        .unwrap()
        .parse()
        .unwrap();
    owner.pulls_set_open(&repo, number, false).await.unwrap();
    let closed = code
        .readiness(&pull.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(closed.report.head_sha, advanced);
    let attempt = claim_head(base, runner, &advanced).await;
    finish_claim(base, runner, &attempt, "success").await;
}

pub(super) async fn author_yields_running_checks(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &CodeAgent,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let (_, repo, head, pull) = fixture(root, owner, code, owner_token, "yield", &["test"]).await;
    let attempt = claim_head(base, runner, &head).await;
    let n = pull
        .written
        .target
        .url
        .rsplit('/')
        .next()
        .unwrap()
        .parse()
        .unwrap();
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
        code.yield_pull_request_checks(&pull.written.target.url)
            .await
            .unwrap(),
        1
    );
    assert_eq!(
        code.yield_pull_request_checks(&pull.written.target.url)
            .await
            .unwrap(),
        0
    );
    finish_claim(base, runner, &attempt, "failure").await;
    let ready = code
        .readiness(&pull.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert!(
        ready
            .report
            .checks
            .iter()
            .all(|c| c.conclusion.as_deref() != Some("cancelled"))
    );
    let replacement = claim_head(base, runner, &head).await;
    assert_ne!(replacement["attempt_id"], attempt["attempt_id"]);
    finish_claim(base, runner, &replacement, "success").await;
}

pub(super) async fn retry_main_verification(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &CodeAgent,
    tickets: &TicketAgent,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let repo = "acme/retry-main".to_string();
    let (workspace, name) = repo.split_once('/').unwrap();
    owner
        .repos_create(input(json!({"path":repo})))
        .await
        .unwrap();
    let git = Git::new(&root.join("retry-main"));
    let head = git.commit("", ".guildbotics-ci.toml", PIPELINE);
    git.push(
        &code.clone_url(&repo).await.unwrap(),
        &head,
        "main",
        "owner",
        owner_token,
    );
    owner
        .members_create(input(json!({"name":"retry-runner","kind":"runner"})))
        .await
        .unwrap();
    let alternate = owner
        .members_issue_token("retry-runner".into(), input(json!({"label":"fixture"})))
        .await
        .unwrap();
    let mut runners = [runner, alternate.token.as_str()].into_iter().cycle();
    let mut selected = runners.next().unwrap();
    let mut attempt = claim_head(base, selected, &head).await;
    loop {
        let before = serde_json::to_value(owner.repos_verification(&repo).await.unwrap()).unwrap()
            ["current"]["retry"]["number"]
            .as_u64()
            .unwrap();
        finish_claim(base, selected, &attempt, "failure").await;
        let failed = tokio::time::timeout(std::time::Duration::from_secs(15), async {
            loop {
                let state =
                    serde_json::to_value(owner.repos_verification(&repo).await.unwrap()).unwrap();
                if state["current"]["state"] == "failed" {
                    break true;
                }
                if state["current"]["retry"]["number"].as_u64().unwrap() > before {
                    break false;
                }
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("verification did not settle or enqueue its automatic retry");
        if failed {
            break;
        }
        selected = runners.next().unwrap();
        attempt = claim_head(base, selected, &head).await;
    }
    let url = format!(
        "{base}/workspaces/{workspace}/repos/{name}/jobs/{}",
        attempt["attempt_id"].as_str().unwrap()
    );
    assert!(
        code.retry_job(
            &format!("{base}/workspaces/{workspace}/repos/{name}/jobs/missing"),
            Default::default()
        )
        .await
        .unwrap_err()
        .is_not_found()
    );
    assert!(matches!(
        code.retry_job(
            &format!("{base}/workspaces/elsewhere/repos/repo/jobs/missing"),
            Default::default()
        )
        .await,
        Err(ServiceError::OutOfScope)
    ));
    let repair = tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let page = owner
                .tickets_cards(
                    "acme",
                    input(json!({"repair":true,"state":"open","comments":"none"})),
                    owner.page("repairs".into(), Some(100), None).unwrap(),
                )
                .await
                .unwrap();
            if let Some(ticket) = page
                .items
                .into_iter()
                .map(|c| c.ticket)
                .find(|t| t.repository == repo)
            {
                break ticket;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("failed verification did not create its repair ticket");
    assert!(repair.repair);
    assert_eq!(repair.assignee.as_ref().unwrap().name, "aiko");
    let tasks = tickets.candidates().await.unwrap();
    let task = tasks
        .iter()
        .find(|task| task.url.as_deref() == Some(&repair.url))
        .unwrap();
    assert!(
        tickets.refresh(task).await.unwrap().is_some(),
        "a repair is assigned to an active agent at creation"
    );

    assert!(
        tasks
            .iter()
            .any(|task| task.url.as_deref() == Some(&repair.url) && task.repair)
    );
    let next = code.retry_job(&url, Default::default()).await.unwrap();
    assert_ne!(next, url);
    let verification = owner.repos_verification(&repo).await.unwrap();
    assert_eq!(
        serde_json::to_value(verification).unwrap()["current"]["state"],
        "running"
    );
    let retry = claim_head(base, runner, &head).await;
    assert!(next.ends_with(retry["attempt_id"].as_str().unwrap()));
    assert!(code.retry_job(&next, Default::default()).await.is_err());
    finish_claim(base, runner, &retry, "success").await;
}
