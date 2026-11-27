//! Behavior contracts through the embedded Branvil service.
mod alerts;
mod code_behavior;
mod contracts;
mod extra;
mod readiness;
use crate::ci::Runner;
use crate::common::{git_as_ok, git_ok};
use branvil::agent::port::*;
use branvil::agent::{CodeAgent, TicketAgent};
use branvil::test_support::ticket_request;
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};
const REPO: &str = "acme/demo";
const PIPELINE: &str = "[[jobs]]\nname = \"test\"\nrequired = true\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\nartifacts = [{name = \"report\", paths = [\"report.txt\"]}]\n";
fn input<T: serde::de::DeserializeOwned>(v: Value) -> T {
    serde_json::from_value(v).unwrap()
}
struct Git {
    root: PathBuf,
}
impl Git {
    fn new(root: &Path) -> Self {
        let root = root.join("work");
        std::fs::create_dir_all(&root).unwrap();
        git_ok(&root, &["init", "-q", "-b", "main"]);
        Self { root }
    }

    fn run(&self, args: &[&str], user: &str, token: &str) -> String {
        if token.is_empty() {
            git_ok(&self.root, args)
        } else {
            git_as_ok(&self.root, user, token, args)
        }
    }

    fn commit(&self, start: &str, file: &str, body: &str) -> String {
        if !start.is_empty() {
            self.run(&["checkout", "-q", "--detach", start], "owner", "");
        }
        let p = self.root.join(file);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, body).unwrap();
        self.run(&["add", "."], "owner", "");
        self.run(&["commit", "-q", "-m", "change"], "owner", "");
        self.run(&["rev-parse", "HEAD"], "owner", "")
    }
    fn push(&self, url: &str, sha: &str, branch: &str, user: &str, token: &str) {
        self.run(
            &["push", "-q", url, &format!("{sha}:refs/heads/{branch}")],
            user,
            token,
        );
    }
}
async fn finish(base: &str, token: &str, sha: &str, conclusion: &str) {
    let runner = Runner::at(base, token);
    let attempt = runner.claim_head(sha).await;
    assert_eq!(runner.finish(&attempt, conclusion).await.0, 200);
}
async fn create(t: &TicketAgent, title: &str) -> String {
    t.create_ticket(ticket_request(REPO, title))
        .await
        .unwrap()
        .target
        .url
}
async fn merge_checked(
    owner: &::branvil::api::MemberApi,
    repo: &str,
    n: u64,
    head: &str,
    base_sha: &str,
) {
    tokio::time::timeout(std::time::Duration::from_secs(120), async {
        let mut previous=json!({"unset":true});
        loop {
            match owner
                .pulls_merge(
                    repo,
                    n,
                    input(json!({"expected_head":head,"expected_base":base_sha})),
                )
                .await
            {
                Ok(_) => break,
                Err(error) => {
                    assert_eq!(error.code, "not_mergeable", "fixture merge failed: {error}")
                }
            }
            let p = serde_json::to_value(owner.pulls_get(repo, n).await.unwrap()).unwrap();
            if p["landing"]!=previous {eprintln!("fixture {repo}!{n}: {}",json!({"landing":p["landing"],"draft":p["draft"],"approvals":p["approvals"],"reviews":p["review_states"],"unanswered":p["unanswered"],"head":p["head"],"base":p["base"]}));previous=p["landing"].clone();}
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("fixture merge did not become ready");
}
async fn ticket_relations_contract(t: &TicketAgent) {
    let parent = create(t, "Parent").await;
    let blocker = create(t, "Blocker").await;
    let child = create(t, "Child").await;
    t.update_ticket(
        &child,
        UpdateTicket {
            parent: Some(parent.clone()),
            add_blocked_by: vec![blocker.clone()],
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let p = t.read(Resource::Tickets, &parent, None, "").await.unwrap();
    assert_eq!(p.items()[0]["children"][0]["url"], child);
    let b = t.read(Resource::Tickets, &blocker, None, "").await.unwrap();
    assert_eq!(b.items()[0]["blocks"][0]["url"], child);
    t.update_ticket(
        &child,
        UpdateTicket {
            parent: Some(String::new()),
            remove_blocked_by: vec![blocker],
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert!(
        t.read(Resource::Tickets, &parent, None, "")
            .await
            .unwrap()
            .items()[0]["children"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
struct Context {
    temp: tempfile::TempDir,
    fixture: ::branvil::test_support::Fixture,
    alert_host: alerts::Host,
    owner: ::branvil::api::MemberApi,
    owner_token: String,
    base: String,
    runner: String,
    t: Arc<TicketAgent>,
    c: Arc<CodeAgent>,
    other_code: Arc<CodeAgent>,
}
impl Context {
    async fn new() -> Self {
        Self::with_repository(REPO).await
    }
    async fn with_repository(repository: &str) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let (fixture, alert_host) =
            ::branvil::test_support::Fixture::configured("owner", alerts::Host::configure).await;
        fixture.install_test_hooks("agent_code::hook");
        let owner_token = fixture.owner_token.clone();
        let running = &fixture.running;
        let base = running.data.base_url().to_owned();
        let owner = running.data.owner();
        owner
            .repos_create_workspace(input(json!({"name":"acme"})))
            .await
            .unwrap();
        owner
            .repos_create(input(json!({"path":repository,"labels":["bug"]})))
            .await
            .unwrap();
        owner
            .members_create(input(json!({"name":"aiko","kind":"agent"})))
            .await
            .unwrap();
        owner
            .members_create(input(json!({"name":"runner1","kind":"runner"})))
            .await
            .unwrap();
        let runner = owner
            .members_issue_token("runner1".into(), input(json!({"label":"fixture"})))
            .await
            .unwrap();
        let t: Arc<TicketAgent> = Arc::new(TicketAgent::new(
            running.data.clone(),
            "acme".into(),
            "aiko".into(),
            "Aiko".into(),
        ));
        let c: Arc<CodeAgent> = Arc::new(CodeAgent::new(
            running.data.clone(),
            "acme".into(),
            "aiko".into(),
            "Aiko".into(),
        ));
        let other_code: Arc<CodeAgent> = Arc::new(CodeAgent::new(
            running.data.clone(),
            "acme".into(),
            "kenji".into(),
            "Kenji".into(),
        ));
        Self {
            temp,
            fixture,
            alert_host,
            owner,
            owner_token,
            base,
            runner: runner.token,
            t,
            c,
            other_code,
        }
    }
    async fn git(&self) -> (Git, String, String) {
        self.repository(".guildbotics-ci.toml", PIPELINE).await
    }
    async fn repository(&self, file: &str, body: &str) -> (Git, String, String) {
        let git = Git::new(self.temp.path());
        let main = git.commit("", file, body);
        let remote = self.c.clone_url(REPO).await.unwrap();
        assert!(remote.starts_with(&self.base));
        git.push(&remote, &main, "main", "owner", self.owner_token.trim());
        (git, main, remote)
    }
    async fn open(&self) -> (Git, String, PullRequestCreated) {
        let (git, main, remote) = self.git().await;
        let credential = self.c.git_credential().await.unwrap().unwrap();
        let head = git.commit(&main, "feature.txt", "feature\n");
        git.push(
            &remote,
            &head,
            "next",
            &credential.username,
            &credential.password,
        );
        let next = self
            .c
            .create_pull_request(CreatePullRequest {
                repository: REPO.into(),
                head: "next".into(),
                base: String::new(),
                title: "Next".into(),
                body: String::new(),
                draft: false,
                tickets: vec![],
            })
            .await
            .unwrap();
        (git, remote, next)
    }
    async fn stop(self) {
        self.c.aclose().await.unwrap();
        self.other_code.aclose().await.unwrap();
        drop(self.c);
        drop(self.t);
        drop(self.other_code);
        drop(self.owner);
        self.fixture.stop().await;
    }
}
#[test]
fn hook() {
    ::branvil::test_support::hook();
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ticket_relations() {
    let f = Context::new().await;
    let Context { t, .. } = &f;
    ticket_relations_contract(t.as_ref()).await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn merge_closes_ticket() {
    let f = Context::new().await;
    let Context {
        owner,
        base,
        runner,
        t,
        c,
        ..
    } = &f;
    let (git, main, remote) = f.git().await;
    let credential = c.git_credential().await.unwrap().unwrap();
    let head = git.commit(&main, "feature.txt", "feature\n");
    git.push(
        &remote,
        &head,
        "feature",
        &credential.username,
        &credential.password,
    );
    let ticket = create(t.as_ref(), "Merge closes this").await;
    let pr = c
        .create_pull_request(CreatePullRequest {
            repository: REPO.into(),
            head: "feature".into(),
            base: String::new(),
            title: "Feature".into(),
            body: String::new(),
            draft: false,
            tickets: vec![TicketLink {
                url: ticket.clone(),
                closes: true,
            }],
        })
        .await
        .unwrap();
    let pending = c
        .readiness(&pr.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(pending.report.head_sha, head);
    assert_eq!(
        pending.report.required_checks,
        Some(RequiredChecks::Running)
    );
    finish(base, runner, &head, "success").await;
    let ready = c
        .readiness(&pr.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(ready.report.required_checks, Some(RequiredChecks::Passed));
    let number = pr
        .written
        .target
        .url
        .rsplit('/')
        .next()
        .unwrap()
        .parse()
        .unwrap();
    owner
        .pulls_review(
            REPO,
            number,
            input(json!({"verdict":"approve","commit":head})),
        )
        .await
        .unwrap();
    owner
        .pulls_merge(
            REPO,
            number,
            input(json!({"expected_head":head,"expected_base":main})),
        )
        .await
        .unwrap();
    assert_eq!(
        c.read(Resource::PullRequests, &pr.written.target.url, None, "")
            .await
            .unwrap()
            .items()[0]["merged"],
        true
    );
    assert_eq!(
        t.read(Resource::Tickets, &ticket, None, "")
            .await
            .unwrap()
            .items()[0]["state"],
        "closed"
    );
    contracts::code_closed_contract(c.as_ref(), &pr.written.target.url).await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn dependency_alerts() {
    let f = Context::new().await;
    let Context {
        owner_token,
        base,
        c,
        ..
    } = &f;
    let (_, main, _) = f.git().await;
    alerts::contract(&f.alert_host, c.as_ref(), base, owner_token.trim(), &main).await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn empty_runner_queue() {
    let f = Context::new().await;
    let Context { base, runner, .. } = &f;
    // An empty runner queue is temporary while push processing registers jobs.
    assert!(
        tokio::time::timeout(
            std::time::Duration::from_millis(100),
            Runner::at(base, runner).next_claim()
        )
        .await
        .is_err()
    );

    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn artifacts_and_stale_head() {
    let f = Context::new().await;
    let Context {
        base, runner, c, ..
    } = &f;
    let (git, main, remote) = f.git().await;
    let credential = c.git_credential().await.unwrap().unwrap();
    let moved = git.commit(&main, "later.txt", "later\n");
    git.push(
        &remote,
        &moved,
        "next",
        &credential.username,
        &credential.password,
    );
    let next = c
        .create_pull_request(CreatePullRequest {
            repository: REPO.into(),
            head: "next".into(),
            base: String::new(),
            title: "Next".into(),
            body: String::new(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap();
    let again = c
        .readiness(&next.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(again.report.head_sha, moved);
    assert_eq!(again.report.required_checks, Some(RequiredChecks::Running));
    let artifact = b"PK\x03\x04synthetic test artifact";
    let scripted = Runner::at(base, runner);
    let attempt = scripted.claim_head(&moved).await;
    let (status, _) = scripted
        .raw(
            reqwest::Method::PUT,
            &attempt,
            "artifacts/report",
            artifact.to_vec(),
        )
        .await;
    assert!(status.is_success(), "fixture artifact upload {status}");
    assert_eq!(scripted.finish(&attempt, "success").await.0, 200);
    let too_large = c
        .artifact(&next.written.target.url, "report", 1)
        .await
        .unwrap_err();
    assert_eq!(
        too_large.translation_key(),
        "integrations.branvil.api.artifact_too_large"
    );
    assert_eq!(
        too_large.translation_args(),
        [
            ("name".into(), "report".into()),
            ("limit".into(), "1".into())
        ]
        .into()
    );
    contracts::code_artifact_contract(c.as_ref(), &next.written.target.url, &moved, artifact).await;
    assert_eq!(
        c.readiness(&next.written.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .report
            .required_checks,
        Some(RequiredChecks::Passed)
    );
    let advanced = git.commit(&moved, "advance.txt", "advanced\n");
    git.push(
        &remote,
        &advanced,
        "next",
        &credential.username,
        &credential.password,
    );
    let changed = c
        .readiness(&next.written.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(changed.report.head_sha, advanced);
    assert_eq!(
        changed.report.required_checks,
        Some(RequiredChecks::Running)
    );
    contracts::code_stale_head_contract(c.as_ref(), &next.written.target.url, &moved).await;
    contracts::code_old_artifact_contract(c.as_ref(), &next.written.target.url).await;
    finish(base, runner, &advanced, "failure").await;
    assert_eq!(
        c.readiness(&next.written.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .report
            .required_checks,
        Some(RequiredChecks::Failed)
    );

    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn code_contract_and_boundaries() {
    let f = Context::new().await;
    let Context { t, c, .. } = &f;
    let (git, main, remote) = f.git().await;
    let credential = c.git_credential().await.unwrap().unwrap();
    let head = git.commit(&main, "feature.txt", "feature\n");
    git.commit(&head, "README.md", "contract line\n");
    let contract = git.commit("", "日本.md", "Unicode content\n");
    git.push(
        &remote,
        &contract,
        "contract-feature",
        &credential.username,
        &credential.password,
    );
    for branch in [
        "contract-feature-other",
        "contract-feature-wrong-kind-false",
        "contract-feature-wrong-kind-true",
    ] {
        git.push(
            &remote,
            &contract,
            branch,
            &credential.username,
            &credential.password,
        );
    }
    contracts::code_contract(c.as_ref(), REPO, "contract-feature", "main").await;
    let contract_pr = c
        .open_pull_requests(REPO, "contract-feature")
        .await
        .unwrap()
        .remove(0);
    contracts::code_utf8_files_contract(c.as_ref(), &contract_pr, "日本.md", "Unicode content")
        .await;

    contracts::code_kind_boundary_contract(c.as_ref(), t.as_ref(), REPO, &contract_pr).await;

    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ticket_search_decoy() {
    let f = Context::new().await;
    let Context { t, c, .. } = &f;
    let (git, main, remote) = f.git().await;
    let credential = c.git_credential().await.unwrap().unwrap();
    let head = git.commit(&main, "feature.txt", "feature\n");
    git.push(
        &remote,
        &head,
        "ticket-search-decoy",
        &credential.username,
        &credential.password,
    );
    contracts::ticket_search_decoy_contract(
        t.as_ref(),
        c.as_ref(),
        REPO,
        REPO,
        "ticket-search-decoy",
        "main",
    )
    .await;

    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn code_open_order() {
    let f = Context::new().await;
    let Context { c, .. } = &f;
    let (git, main, remote) = f.repository("README.md", "base\n").await;
    let credential = c.git_credential().await.unwrap().unwrap();
    let head = git.commit(&main, "feature.txt", "feature\n");
    let bases = (0..100)
        .map(|n| format!("order-base-{n}"))
        .collect::<Vec<_>>();
    let args = std::iter::once("push".to_owned())
        .chain(std::iter::once("-q".to_owned()))
        .chain(std::iter::once(remote.clone()))
        .chain(std::iter::once(format!("{head}:refs/heads/contract-order")))
        .chain(bases.iter().map(|name| format!("{main}:refs/heads/{name}")))
        .collect::<Vec<_>>();
    git.run(
        &args.iter().map(String::as_str).collect::<Vec<_>>(),
        &credential.username,
        &credential.password,
    );
    let first = c
        .create_pull_request(CreatePullRequest {
            repository: REPO.into(),
            head: "contract-order".into(),
            base: "main".into(),
            title: "First pull".into(),
            body: String::new(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap();
    assert!(first.created);
    let mut created = vec![first.written.target.url];
    created.extend(
        branvil::test_support::seed_open_pulls(
            &f.fixture.data(),
            REPO,
            "contract-order",
            bases.len(),
        )
        .await,
    );
    contracts::code_open_order_contract(c.as_ref(), REPO, "contract-order", created).await;

    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn code_links() {
    let f = Context::new().await;
    let Context {
        owner,
        base,
        runner,
        t,
        c,
        ..
    } = &f;
    let (git, main, remote) = f.git().await;
    let credential = c.git_credential().await.unwrap().unwrap();
    let head = git.commit(&main, "feature.txt", "feature\n");
    git.commit(&head, "README.md", "contract line\n");
    let contract = git.commit("", "日本.md", "Unicode content\n");
    git.push(
        &remote,
        &contract,
        "contract-feature",
        &credential.username,
        &credential.password,
    );
    let mut owned = vec!["push".to_owned(), "-q".to_owned(), remote.clone()];
    let mut link_head = contract.clone();
    for n in [7, 0, 1, 2, 3, 4, 5, 6] {
        link_head = git.commit(&link_head, &format!("links-{n}.txt"), "link case\n");
        owned.push(format!("{link_head}:refs/heads/contract-feature-links-{n}"));
    }
    git.run(
        &owned.iter().map(String::as_str).collect::<Vec<_>>(),
        &credential.username,
        &credential.password,
    );
    let cases = contracts::code_link_population(
        c.as_ref(),
        t.as_ref(),
        REPO,
        REPO,
        "contract-feature",
        "main",
    )
    .await;
    for case in cases {
        let current_head = c
            .read(Resource::PullRequests, &case.url, None, "")
            .await
            .unwrap()
            .items()[0]["head_sha"]
            .as_str()
            .unwrap()
            .to_owned();
        let current_base = owner.repos_branch(REPO, "main").await.unwrap().sha;
        let n = case.url.rsplit('/').next().unwrap().parse().unwrap();
        // The one shared commit's job may have finished for an earlier case.
        if c.readiness(&case.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .report
            .required_checks
            == Some(RequiredChecks::Running)
        {
            finish(base, runner, &current_head, "success").await;
        }
        owner
            .pulls_review(
                REPO,
                n,
                input(json!({"verdict":"approve","commit":current_head})),
            )
            .await
            .unwrap();
        merge_checked(owner, REPO, n, &current_head, &current_base).await;
        contracts::code_link_case_finished(t.as_ref(), &case).await;
    }

    f.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn code_other_actor() {
    let f = Context::new().await;
    let Context { c, .. } = &f;
    let (_, _, next) = f.open().await;
    contracts::code_other_actor_contract(
        c.as_ref(),
        f.other_code.as_ref(),
        &next.written.target.url,
    )
    .await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn member_show_and_missing_base() {
    let f = Context::new().await;
    let Context {
        owner,
        owner_token,
        c,
        ..
    } = &f;
    let (git, remote, next) = f.open().await;
    let final_base = owner.repos_branch(REPO, "main").await.unwrap().sha;
    let repo_config = owner.repos_get(REPO).await.unwrap();
    owner.repos_update(REPO,input(json!({"revision":repo_config.revision,"default_branch":"main/next","protected_branches":[]}))).await.unwrap();
    git.run(
        &["push", "-q", &remote, ":refs/heads/main"],
        "owner",
        owner_token.trim(),
    );
    git.run(
        &[
            "push",
            "-q",
            &remote,
            &format!("{final_base}:refs/heads/main/next"),
        ],
        "owner",
        owner_token.trim(),
    );
    contracts::code_missing_base_contract(c.as_ref(), &next.written.target.url).await;

    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn reused_and_gates() {
    let f = Context::new().await;
    let Context {
        temp,
        owner,
        owner_token,
        base,
        runner,
        c,
        ..
    } = &f;
    extra::reused_and_gates(
        temp.path(),
        owner,
        c.as_ref(),
        base,
        owner_token.trim(),
        runner,
    )
    .await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn retry_main_verification() {
    let f = Context::new().await;
    let Context {
        temp,
        owner,
        owner_token,
        base,
        runner,
        t,
        c,
        ..
    } = &f;
    readiness::retry_main_verification(
        temp.path(),
        owner,
        c.as_ref(),
        t.as_ref(),
        base,
        owner_token.trim(),
        runner,
    )
    .await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn author_yields_running_checks() {
    let f = Context::new().await;
    let Context {
        temp,
        owner,
        owner_token,
        base,
        runner,
        c,
        ..
    } = &f;
    readiness::author_yields_running_checks(
        temp.path(),
        owner,
        c.as_ref(),
        base,
        owner_token.trim(),
        runner,
    )
    .await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn primary_failure_logs_and_cancelled_fallback() {
    let f = Context::new().await;
    let Context {
        temp,
        owner,
        owner_token,
        base,
        runner,
        c,
        ..
    } = &f;
    readiness::primary_failure_logs_and_cancelled_fallback(
        temp.path(),
        owner,
        c.as_ref(),
        base,
        owner_token.trim(),
        runner,
    )
    .await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ten_failed_logs_fit_one_page() {
    let f = Context::new().await;
    let Context {
        temp,
        owner,
        owner_token,
        base,
        runner,
        c,
        ..
    } = &f;
    readiness::ten_failed_logs_fit_one_page(
        temp.path(),
        owner,
        c.as_ref(),
        base,
        owner_token.trim(),
        runner,
    )
    .await;
    f.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn pending_closed_and_advanced_heads() {
    let f = Context::new().await;
    let Context {
        temp,
        owner,
        owner_token,
        base,
        runner,
        c,
        ..
    } = &f;
    readiness::pending_closed_and_advanced_heads(
        temp.path(),
        owner,
        c.as_ref(),
        base,
        owner_token.trim(),
        runner,
    )
    .await;
    f.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ticket_and_code_scope() {
    let f = Context::new().await;
    let Context { owner, c, .. } = &f;
    let (_, _, next) = f.open().await;
    owner
        .repos_create_workspace(input(json!({"name":"elsewhere"})))
        .await
        .unwrap();
    owner
        .repos_create(input(json!({"path":"elsewhere/repo","labels":["bug"]})))
        .await
        .unwrap();
    let their = owner
        .tickets_create_ticket("elsewhere/repo", input(json!({"title":"Their ticket"})))
        .await
        .unwrap();
    let _their_comment = owner
        .tickets_add_comment(
            "elsewhere/repo",
            &their.key,
            input(json!({"body":"Their comment"})),
        )
        .await
        .unwrap();
    contracts::code_cross_repo_link_contract(c.as_ref(), &next.written.target.url, &their.url)
        .await;
    f.stop().await;
}
