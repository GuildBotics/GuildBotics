//! Provider-boundary readiness cases from the frozen Branvil code service.
use super::*;
use std::io::{Cursor, Write};

const JUNIT_ARTIFACT: &str = r#"artifacts = [{ name = "report", paths = ["junit.xml"], junit = "junit.xml" }]
"#;
const TEST_NAME: &str = "suite::case[1] & more";
const TEST_QUERY: &str = "test-failures?name=suite%3A%3Acase%5B1%5D%20%26%20more";

async fn upload_junit_failure(runner: &Runner, attempt: &Value) {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    zip.start_file("junit.xml", zip::write::SimpleFileOptions::default())
        .unwrap();
    write!(
        zip,
        "<testsuite><testcase name='{}'><failure/></testcase></testsuite>",
        TEST_NAME.replace('&', "&amp;")
    )
    .unwrap();
    let (status, _) = runner
        .raw(
            reqwest::Method::PUT,
            attempt,
            "artifacts/report",
            zip.finish().unwrap().into_inner(),
        )
        .await;
    assert_eq!(status, 200);
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
    let git = Git::new(&root.join(name));
    git.commit("", "README.md", "base\n");
    let pipeline = jobs.iter().map(|job| format!("[[jobs]]\nname = \"{job}\"\nrequired = true\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\n{JUNIT_ARTIFACT}")).collect::<String>();
    let main = git.commit("", ".guildbotics-ci.toml", &pipeline);
    let remote = code.clone_url(&repo).await.unwrap();
    git.push(&remote, &main, "main", "owner", owner_token);
    let head = git.commit(&main, "feature.txt", "feature\n");
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

pub(super) async fn primary_failure_logs_and_cancelled_fallback(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &CodeAgent,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let runner = Runner::at(base, runner);
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
        let (_, repo, head, pull) = fixture(root, owner, code, owner_token, name, jobs).await;
        let mut expected_id = String::new();
        let mut earlier_url = None;
        for _ in 0..jobs.len() + usize::from(!only_cancelled) {
            let attempt = runner.claim_head(&head).await;
            let job = attempt["job"]["name"].as_str().unwrap();
            if job == "test" && earlier_url.is_none() {
                assert_eq!(runner.finish(&attempt, "success").await.0, 200);
                let url = format!(
                    "{base}/workspaces/acme/repos/{}/jobs/{}",
                    repo.split_once('/').unwrap().1,
                    attempt["attempt_id"].as_str().unwrap()
                );
                code.retry_job(&url, Default::default()).await.unwrap();
                earlier_url = Some(url);
                continue;
            }
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
            assert!(
                runner
                    .raw(
                        reqwest::Method::POST,
                        &attempt,
                        "log",
                        log.as_bytes().to_vec()
                    )
                    .await
                    .0
                    .is_success()
            );
            if job == "test" {
                upload_junit_failure(&runner, &attempt).await;
                let phase = json!({"cpu_time_ms": 250, "host_cpu_peak_bps": 9500});
                let (status, result) = runner
                    .post(
                        &attempt, "finish", json!({
                            "lease": attempt["lease"], "conclusion": "failure",
                            "resources": {"restore": {}, "commands": phase, "save": {}, "guest_peak_memory_mib": 32}
                        }),
                    )
                    .await;
                assert_eq!(status, 200, "{result}");
                expected_id = attempt["attempt_id"].as_str().unwrap().into();
                continue;
            }
            if only_cancelled {
                expected_id = attempt["attempt_id"].as_str().unwrap().into();
            }
            assert_eq!(runner.finish(&attempt, conclusion).await.0, 200);
        }
        let readiness = code
            .readiness(&pull.written.target.url, true, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap();
        assert_eq!(
            readiness.report.required_checks,
            Some(RequiredChecks::Failed)
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
        let data = serde_json::to_value(log).unwrap();
        let evidence = &data["evidence"];
        assert_eq!(evidence["sha"], head);
        assert!(evidence["runner"].is_string());
        assert_eq!(
            evidence["failed_test_count"],
            if only_cancelled {
                Value::Null
            } else {
                json!(1)
            }
        );
        if let Some(earlier_url) = earlier_url {
            assert_eq!(evidence["attempt"], 2);
            assert!(evidence["runner_claim"].is_object());
            assert!(evidence["allocation"]["cpus"].is_number());
            assert_eq!(evidence["resources"]["commands"]["host_cpu_peak_bps"], 9500);
            assert_eq!(evidence["resources"]["guest_peak_memory_mib"], 32);
            assert_eq!(evidence["other_attempts"][0]["conclusion"], "success");
            assert_eq!(evidence["other_attempts"][0]["url"], earlier_url);
            assert!(evidence["other_attempts"][0]["reused_from"].is_null());
            let history = &evidence["test_failures"][0];
            assert_eq!(history["name"], TEST_NAME);
            assert!(history["url"].as_str().unwrap().ends_with(TEST_QUERY));
            assert_eq!(history["failures"][0]["attempt_id"], expected_id);
            assert_eq!(history["failures"][0]["runner"], evidence["runner"]);
        }
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
    let runner = Runner::at(base, runner);
    let names = (0..10).map(|n| format!("job{n}")).collect::<Vec<_>>();
    let names = names.iter().map(String::as_str).collect::<Vec<_>>();
    let (_, _, head, pull) = fixture(root, owner, code, owner_token, "budget", &names).await;
    let mut ids = std::collections::BTreeMap::new();
    for _ in 0..10 {
        let attempt = runner.claim_head(&head).await;
        ids.insert(
            attempt["job"]["name"].as_str().unwrap().to_owned(),
            attempt["attempt_id"].as_str().unwrap().to_owned(),
        );
        assert!(
            runner
                .raw(
                    reqwest::Method::POST,
                    &attempt,
                    "log",
                    vec![b'x'; MAX_PAGE_BYTES]
                )
                .await
                .0
                .is_success()
        );
        assert_eq!(runner.finish(&attempt, "failure").await.0, 200);
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
    let runner = Runner::at(base, runner);
    let (git, repo, head, pull) =
        fixture(root, owner, code, owner_token, "freshness", &["test"]).await;
    let pending = code
        .readiness(&pull.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(pending.report.head_sha, head);
    assert_eq!(
        pending.report.required_checks,
        Some(RequiredChecks::Running)
    );
    let attempt = runner.claim_head(&head).await;
    assert_eq!(runner.finish(&attempt, "success").await.0, 200);
    assert_eq!(
        code.readiness(&pull.written.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .report
            .required_checks,
        Some(RequiredChecks::Passed)
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
    assert_ne!(moved.report.required_checks, Some(RequiredChecks::Passed));
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
    let attempt = runner.claim_head(&advanced).await;
    assert_eq!(runner.finish(&attempt, "success").await.0, 200);
}

pub(super) async fn author_yields_running_checks(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &CodeAgent,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let runner = Runner::at(base, runner);
    let (_, repo, head, pull) = fixture(root, owner, code, owner_token, "yield", &["test"]).await;
    let attempt = runner.claim_head(&head).await;
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
    assert_eq!(runner.finish(&attempt, "failure").await.0, 200);
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
    let replacement = runner.claim_head(&head).await;
    assert_ne!(replacement["attempt_id"], attempt["attempt_id"]);
    assert_eq!(runner.finish(&replacement, "success").await.0, 200);
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
    let head = git.commit(
        "",
        ".guildbotics-ci.toml",
        &format!(
            "{}{JUNIT_ARTIFACT}",
            PIPELINE.split("artifacts =").next().unwrap()
        ),
    );
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
    let runner = Runner::at(base, runner);
    let alternate = Runner::at(base, &alternate.token);
    let mut runners = [&runner, &alternate].into_iter().cycle();
    let mut selected = runners.next().unwrap();
    let mut attempt = selected.claim_head(&head).await;
    loop {
        let before = serde_json::to_value(owner.repos_verification(&repo).await.unwrap()).unwrap()
            ["current"]["retry"]["number"]
            .as_u64()
            .unwrap();
        upload_junit_failure(selected, &attempt).await;
        assert_eq!(selected.finish(&attempt, "failure").await.0, 200);
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
        attempt = selected.claim_head(&head).await;
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
    assert!(
        repair
            .body
            .contains("Other attempts of this job at the same commit")
    );
    assert!(repair.body.contains("runner_claim"));
    assert!(repair.body.contains(TEST_QUERY), "{}", repair.body);
    assert!(repair.body.contains("retry-runner"));
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
    let retry = runner.claim_head(&head).await;
    assert!(next.ends_with(retry["attempt_id"].as_str().unwrap()));
    assert!(code.retry_job(&next, Default::default()).await.is_err());
    assert_eq!(runner.finish(&retry, "success").await.0, 200);
}
