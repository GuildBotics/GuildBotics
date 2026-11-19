//! Frozen reused-CI and behind-head landing cases from test_smoke.py.
use super::*;
async fn open(c: &CodeAgent, repo: &str, branch: &str) -> PullRequestCreated {
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

pub(super) async fn reused_and_gates(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    c: &CodeAgent,
    base: &str,
    owner_token: &str,
    runner: &str,
) {
    let credential = c.git_credential().await.unwrap().unwrap();
    for conclusion in ["success", "conflict"] {
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
        // Artifact jobs must execute for every commit; this fixture exercises reuse.
        git.commit("", ".branvil/ci.toml", "[[jobs]]\nname = \"test\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\n");
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
            .written
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
                json!({"executor":"microsandbox","executor_state":"ready", "images":[{"reference":"docker.io/library/alpine:3.22","architecture":"arm64","digest":format!("sha256:{}","2".repeat(64))}]}),
            )
            .await;
            assert_eq!(attempt["sha"], same);
            assert_eq!(attempt["reuse"]["sha"], head);
            api_call(base,&format!("/runner/attempts/{}/finish",attempt["attempt_id"].as_str().unwrap()),runner,json!({"lease":attempt["lease"],"conclusion":"success","image_digest":format!("sha256:{}","2".repeat(64)),"reused_from":attempt["reuse"]["attempt_id"]})).await;
            assert_eq!(
                c.readiness(&pr.written.target.url, false, MAX_LOG_TAIL_BYTES)
                    .await
                    .unwrap()
                    .report
                    .required_checks,
                RequiredChecks::Passed
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
            .written
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
        if conclusion == "conflict" {
            let before_review = c
                .readiness(&feature.written.target.url, false, MAX_LOG_TAIL_BYTES)
                .await
                .unwrap();
            assert_eq!(before_review.report.required_checks, RequiredChecks::Passed);
            assert!(before_review.report.checks.iter().all(|c| c.name != "gate"));
        }
        owner
            .pulls_review(
                &repo,
                number,
                input(json!({"verdict":"approve","commit":head})),
            )
            .await
            .unwrap();
        let result = c
            .readiness(&feature.written.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap();
        assert_eq!(result.report.head_sha, head);
        if conclusion == "conflict" {
            assert_eq!(result.report.required_checks, RequiredChecks::Passed);
            let page = c
                .read(
                    Resource::PullRequestReadiness,
                    &feature.written.target.url,
                    Some(&json!({"failed_logs":true})),
                    "",
                )
                .await
                .unwrap();
            assert_eq!(page.items()[0]["failed_logs"], json!([]));
        } else {
            assert!(
                result
                    .report
                    .checks
                    .iter()
                    .all(|c| !c.name.starts_with("gate/"))
            );
            assert_eq!(result.report.required_checks, RequiredChecks::Passed);
        }
    }
}
