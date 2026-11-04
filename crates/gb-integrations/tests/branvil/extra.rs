//! Frozen reused-CI and behind-head landing cases from test_smoke.py.
use super::*;
async fn open(c: &dyn CodeHostingService, repo: &str, branch: &str) -> PullRequestCreated {
    c.create_pull_request(CreatePullRequest {
        repository: repo.into(),
        head: branch.into(),
        base: String::new(),
        title: branch.into(),
        body: String::new(),
        draft: false,
        tickets: vec![],
    })
    .await
    .unwrap()
}
async fn landing(owner: &::branvil::api::MemberApi, repo: &str, n: u64, state: &str) -> Value {
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let p = serde_json::to_value(owner.pulls_get(repo, n).await.unwrap()).unwrap();
            if p["landing"]["state"] == state {
                return p["landing"].clone();
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("landing fixture did not reach expected state")
}
pub(super) async fn reused_and_gates(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    c: &dyn CodeHostingService,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let credential = c.git_credential().await.unwrap().unwrap();
    for conclusion in ["success", "failure", "conflict"] {
        let repo = format!("acme/gate-{conclusion}");
        owner
            .repos_create(input(json!({"path":repo})))
            .await
            .unwrap();
        let rev = owner.repos_get(&repo).await.unwrap().revision;
        owner
            .repos_update(
                &repo,
                input(json!({"revision":rev,"required_checks":["test"]})),
            )
            .await
            .unwrap();
        let git = Git::new(&root.join(conclusion));
        let remote = c.clone_url(&repo).await.unwrap();
        git.commit("", ".branvil/ci.toml", PIPELINE);
        let main = git.commit("", "a.py", "base\n");
        git.push(&remote, &main, "main", "owner", owner_token);
        let head = git.commit(&main, "a.py", "feature\n");
        git.push(
            &remote,
            &head,
            "feature",
            &credential.username,
            &credential.password,
        );
        let feature = open(c, &repo, "feature").await;
        let number: u64 = feature
            .target
            .url
            .rsplit('/')
            .next()
            .unwrap()
            .parse()
            .unwrap();
        finish(base, runner, &head, "success").await;
        if conclusion == "success" {
            git.run(&["checkout", "-q", "--detach", &head], "owner", "");
            git.run(
                &["commit", "-q", "--allow-empty", "-m", "same tree"],
                "owner",
                "",
            );
            let same = git.run(&["rev-parse", "HEAD"], "owner", "");
            git.push(
                &remote,
                &same,
                "same",
                &credential.username,
                &credential.password,
            );
            let pr = open(c, &repo, "same").await;
            let attempt = api_call(
                base,
                "/runner/claim",
                runner,
                json!({"executor":"microsandbox","executor_state":"ready"}),
            )
            .await;
            assert_eq!(attempt["sha"], same);
            assert_eq!(attempt["reuse"]["sha"], head);
            api_call(base,&format!("/runner/attempts/{}/finish",attempt["attempt_id"].as_str().unwrap()),runner,json!({"lease":attempt["lease"],"conclusion":"success","image_digest":format!("sha256:{}","2".repeat(64)),"reused_from":attempt["reuse"]["attempt_id"]})).await;
            assert_eq!(
                c.readiness(&pr.target.url, false, MAX_LOG_TAIL_BYTES)
                    .await
                    .unwrap()
                    .readiness,
                ReadinessReadiness::Ready
            );
        }
        let file = if conclusion == "conflict" {
            "a.py"
        } else {
            "base.txt"
        };
        let changed = git.commit(&main, file, "base changed\n");
        git.push(
            &remote,
            &changed,
            "advance",
            &credential.username,
            &credential.password,
        );
        let other = open(c, &repo, "advance").await;
        let other_n = other
            .target
            .url
            .rsplit('/')
            .next()
            .unwrap()
            .parse()
            .unwrap();
        finish(base, runner, &changed, "success").await;
        owner
            .pulls_review(
                &repo,
                other_n,
                input(json!({"verdict":"approve","commit":changed})),
            )
            .await
            .unwrap();
        owner
            .pulls_merge(
                &repo,
                other_n,
                input(json!({"expected_head":changed,"expected_base":main})),
            )
            .await
            .unwrap();
        owner
            .pulls_review(
                &repo,
                number,
                input(json!({"verdict":"approve","commit":head})),
            )
            .await
            .unwrap();
        let name = if conclusion == "conflict" {
            "gate"
        } else {
            "gate/test"
        };
        if conclusion == "conflict" {
            let left = landing(owner, &repo, number, "left").await;
            assert_eq!(left["reason"]["files"], json!(["a.py"]));
        } else {
            let gate = landing(owner, &repo, number, "gating").await;
            let pending = c
                .readiness(&feature.target.url, false, MAX_LOG_TAIL_BYTES)
                .await
                .unwrap();
            assert_eq!(pending.behind_by, Some(1));
            assert_eq!(pending.out_of_date, Some(true));
            assert!(
                pending
                    .checks
                    .iter()
                    .any(|c| c.name == name && c.conclusion.is_none())
            );
            assert_eq!(pending.readiness, ReadinessReadiness::Blocked);
            assert!(
                pending
                    .completion_blockers
                    .iter()
                    .any(|b| b.code == "checks_pending")
            );
            assert!(
                !pending
                    .completion_blockers
                    .iter()
                    .any(|b| b.code == "base_behind")
            );
            finish(base, runner, gate["gate"].as_str().unwrap(), conclusion).await;
            landing(
                owner,
                &repo,
                number,
                if conclusion == "success" {
                    "passed"
                } else {
                    "left"
                },
            )
            .await;
        }
        let result = c
            .readiness(&feature.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap();
        assert_eq!(result.head_sha, head);
        assert_eq!(result.behind_by, Some(1));
        assert_eq!(result.out_of_date, Some(true));
        let gate = result.checks.iter().find(|c| c.name == name).unwrap();
        assert_eq!(gate.status, "completed");
        assert_eq!(
            gate.conclusion.as_deref(),
            Some(if conclusion == "success" {
                "success"
            } else {
                "failure"
            })
        );
        if conclusion != "success" {
            assert_eq!(result.readiness, ReadinessReadiness::Blocked);
            assert!(
                result
                    .completion_blockers
                    .iter()
                    .any(|b| b.code == "checks_failed")
            );
            assert!(
                !result
                    .completion_blockers
                    .iter()
                    .any(|b| b.code == "base_behind")
            );
            if conclusion == "conflict" {
                assert!(gate.details.as_deref().unwrap().contains("a.py"));
                let page = c
                    .read(
                        Resource::PullRequestReadiness,
                        &feature.target.url,
                        Some(&json!({"failed_logs":true})),
                        "",
                    )
                    .await
                    .unwrap();
                assert_eq!(page.items()[0]["failed_logs"], json!([]));
                assert!(
                    page.items()[0]["checks"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|check| check["name"] == "gate"
                            && check["details"]
                                .as_str()
                                .unwrap_or_default()
                                .contains("a.py"))
                );
            }
        }
    }
}
