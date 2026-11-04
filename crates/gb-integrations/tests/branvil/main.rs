//! Frozen integration/branvil/test_smoke.py, using the real in-process provider.
mod alerts;
mod code_parity;
#[path = "../support/contracts.rs"]
mod contracts;
mod driver;
mod extra;
mod readiness;
use gb_base::shared_writes::SharedWrites;
use gb_config::secrets::{DeviceKey, Keychain, Scope, SecretStore};
use gb_integrations::factory::*;
use gb_ports::*;
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::{Command, ExitCode},
    sync::{Arc, Mutex},
};
const REPO: &str = "acme/demo";
const PIPELINE: &str = "[[jobs]]\nname = \"test\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\nartifacts = [{name = \"report\", paths = [\"report.txt\"]}]\n";
struct MemoryKey(Mutex<Option<String>>);
impl Keychain for MemoryKey {
    fn read(&self) -> anyhow::Result<Option<String>> {
        Ok(self.0.lock().unwrap().clone())
    }
    fn create(&self, key: &str) -> anyhow::Result<()> {
        let mut slot = self.0.lock().unwrap();
        assert!(slot.is_none());
        *slot = Some(key.into());
        Ok(())
    }
}
fn input<T: serde::de::DeserializeOwned>(v: Value) -> T {
    serde_json::from_value(v).unwrap()
}
struct Git {
    root: PathBuf,
    home: PathBuf,
    askpass: PathBuf,
}
impl Git {
    fn new(root: &Path) -> Self {
        let home = root.join("git-home");
        std::fs::create_dir_all(&home).unwrap();
        let askpass = home.join("askpass.sh");
        std::fs::write(&askpass,"#!/bin/sh\ncase \"$1\" in\nUsername*) printf '%s\\n' \"$TEST_GIT_USER\" ;;\n*) printf '%s\\n' \"$TEST_GIT_TOKEN\" ;;\nesac\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&askpass, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let s = Self {
            root: root.join("work"),
            home,
            askpass,
        };
        s.run(
            &["init", "-q", "-b", "main", s.root.to_str().unwrap()],
            "owner",
            "",
        );
        s
    }
    fn run(&self, args: &[&str], user: &str, token: &str) -> String {
        let out = Command::new("git")
            .args(["-c", "credential.helper=", "-c", "core.askPass="])
            .args(args)
            .current_dir(if self.root.exists() {
                &self.root
            } else {
                &self.home
            })
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", &self.home)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", self.home.join("empty-config"))
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_ASKPASS", &self.askpass)
            .env("TEST_GIT_USER", user)
            .env("TEST_GIT_TOKEN", token)
            .env("GIT_AUTHOR_NAME", user)
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", user)
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "git {:?}: {}",
            args,
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap().trim().into()
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
async fn api_call(base: &str, path: &str, token: &str, body: Value) -> Value {
    let response = reqwest::Client::builder()
        .no_proxy()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .unwrap()
        .post(format!("{base}/api/v1{path}"))
        .bearer_auth(token)
        .json(&body)
        .send()
        .await
        .unwrap();
    assert!(
        response.status().is_success(),
        "fixture API {path} status {}",
        response.status()
    );
    response.json().await.unwrap()
}
async fn finish(base: &str, runner: &str, sha: &str, conclusion: &str) {
    finish_artifact(base, runner, sha, conclusion, None).await;
}
async fn next_claim(base: &str, runner: &str) -> Value {
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .unwrap();
        loop {
            let response = client
                .post(format!("{base}/api/v1/runner/claim"))
                .bearer_auth(runner)
                .json(&json!({"executor":"microsandbox","executor_state":"ready"}))
                .send()
                .await
                .unwrap();
            match response.status() {
                reqwest::StatusCode::NO_CONTENT => {
                    tokio::time::sleep(std::time::Duration::from_millis(30)).await;
                }
                reqwest::StatusCode::OK => {
                    let claim: Value = response.json().await.unwrap();
                    for field in ["attempt_id", "lease", "sha"] {
                        assert!(
                            claim[field].as_str().is_some_and(|v| !v.is_empty()),
                            "fixture claim missing {field}"
                        );
                    }
                    return claim;
                }
                status => panic!("fixture claim status {status}"),
            }
        }
    })
    .await
    .expect("fixture queued job did not appear within 15 seconds")
}
async fn finish_artifact(
    base: &str,
    runner: &str,
    sha: &str,
    conclusion: &str,
    artifact: Option<&[u8]>,
) {
    for _ in 0..100 {
        let attempt = next_claim(base, runner).await;
        let matching = attempt["sha"].as_str() == Some(sha);
        if matching && let Some(bytes) = artifact {
            let response = reqwest::Client::builder()
                .no_proxy()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .unwrap()
                .put(format!(
                    "{base}/api/v1/runner/attempts/{}/artifacts/report",
                    attempt["attempt_id"].as_str().unwrap()
                ))
                .bearer_auth(runner)
                .header("x-branvil-lease", attempt["lease"].as_str().unwrap())
                .body(bytes.to_vec())
                .send()
                .await
                .unwrap();
            assert!(
                response.status().is_success(),
                "fixture artifact upload {}",
                response.status()
            );
        }
        api_call(base,&format!("/runner/attempts/{}/finish",attempt["attempt_id"].as_str().unwrap()),runner,json!({"lease":attempt["lease"],"conclusion":if matching{conclusion}else{"success"},"image_digest":format!("sha256:{}","2".repeat(64))})).await;
        if matching {
            return;
        }
    }
    panic!("fixture job was never claimed")
}
async fn create(t: &dyn TicketService, title: &str) -> String {
    t.create_ticket(CreateTicket {
        tracker: REPO.into(),
        title: title.into(),
        body: String::new(),
        labels: vec![],
        add_to_board: true,
        interactive: false,
        assignee: None,
        lane: None,
        parent: None,
        blocked_by: vec![],
    })
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
    base_url: &str,
    runner: &str,
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
            if p["landing"]["state"] == "gating"
                && let Some(gate) = p["landing"]["gate"].as_str()
            {
                let report = owner.pulls_checks(repo, n).await.unwrap();
                if report.checks.iter().any(|c| {
                    c.name == "gate/test" && c.status == "queued" && c.attempt_id.is_some()
                }) {
                    finish(base_url, runner, gate, "success").await;
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("fixture merge did not become ready");
}
async fn ticket_relations(t: &dyn TicketService) {
    contracts::ticket_contract(t, REPO, "bug").await;
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
async fn scenario() {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("branvil");
    ::branvil::api::initialize(&data, "owner").unwrap();
    let owner_token = std::fs::read_to_string(data.join("secrets/owner.token")).unwrap();
    let alert_host = alerts::Host::configure(&data);
    let running = ::branvil::api::Running::start(&data).await.unwrap();
    let base = running.data.base_url().to_owned();
    let owner = running
        .data
        .authenticate(owner_token.trim().into())
        .await
        .unwrap();
    owner
        .repos_create_workspace(input(json!({"name":"acme"})))
        .await
        .unwrap();
    owner
        .repos_create(input(json!({"path":REPO,"labels":["bug"]})))
        .await
        .unwrap();
    owner
        .members_create(input(json!({"name":"aiko-service","kind":"agent"})))
        .await
        .unwrap();
    let member = owner
        .members_issue_token("aiko-service".into(), input(json!({"label":"fixture"})))
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
    let identity = temp.path().join("identity");
    let workspace = temp.path().join("workspace");
    std::fs::create_dir_all(identity.join(".guildbotics/config/team/members/aiko")).unwrap();
    let writes = SharedWrites::new(identity.clone(), Some(workspace.clone()));
    let key = MemoryKey(Mutex::new(None));
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: temp.path().join("machine"),
            keychain: &key,
        },
    )
    .unwrap();
    secrets.set("AIKO_BRANVIL_TOKEN", &member.token).unwrap();
    let config = IntegrationConfig {
        local_root: workspace.join(".guildbotics/local/services"),
        identity_root: identity,
        person_id: "aiko".into(),
        display_name: "Aiko".into(),
        accounts: [("branvil".into(), ActorName("aiko-service".into()))].into(),
        branvil_members: Default::default(),
        ticket_provider: "branvil".into(),
        code_provider: "branvil".into(),
        chat_provider: "local".into(),
        ticket_scope: "acme".into(),
        code_scope: "acme".into(),
        checks: String::new(),
        slack_base_url: String::new(),
    };
    let factory = ServiceIntegrationFactory::new(
        config.clone(),
        &secrets,
        Some(running.data.clone()),
        Arc::new(|_| {}),
    )
    .unwrap();
    let t = factory.create_ticket_service().unwrap();
    let c = factory.create_code_hosting_service().unwrap();
    ticket_relations(t.as_ref()).await;
    contracts::chat_contract(factory.create_chat_service().unwrap().as_ref(), "contract").await;
    let git = Git::new(temp.path());
    // An empty runner queue is temporary while push processing registers jobs.
    assert!(
        tokio::time::timeout(
            std::time::Duration::from_millis(100),
            next_claim(&base, &runner.token)
        )
        .await
        .is_err()
    );
    let main = git.commit("", ".branvil/ci.toml", PIPELINE);
    let remote = c.clone_url(REPO).await.unwrap();
    assert!(remote.starts_with(&base));
    git.push(&remote, &main, "main", "owner", owner_token.trim());
    alerts::contract(&alert_host, c.as_ref(), &base, owner_token.trim(), &main).await;
    let head = git.commit(&main, "feature.txt", "feature\n");
    let credential = c.git_credential().await.unwrap().unwrap();
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
        .readiness(&pr.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(pending.head_sha, head);
    assert_eq!(pending.rollup, ReadinessRollup::Pending);
    finish(&base, &runner.token, &head, "success").await;
    let ready = c
        .readiness(&pr.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(ready.readiness, ReadinessReadiness::Ready);
    let number = pr.target.url.rsplit('/').next().unwrap().parse().unwrap();
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
        c.read(Resource::PullRequests, &pr.target.url, None, "")
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
    let moved = git.commit(&head, "later.txt", "later\n");
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
        .readiness(&next.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(again.head_sha, moved);
    assert_eq!(again.rollup, ReadinessRollup::Pending);
    let artifact = b"PK\x03\x04synthetic test artifact";
    finish_artifact(&base, &runner.token, &moved, "success", Some(artifact)).await;
    contracts::code_artifact_contract(c.as_ref(), &next.target.url, &moved, artifact).await;
    assert_eq!(
        c.readiness(&next.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .readiness,
        ReadinessReadiness::Ready
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
        .readiness(&next.target.url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(changed.head_sha, advanced);
    assert_eq!(changed.rollup, ReadinessRollup::Pending);
    contracts::code_stale_head_contract(c.as_ref(), &next.target.url, &moved).await;
    contracts::code_old_artifact_contract(c.as_ref(), &next.target.url).await;
    finish(&base, &runner.token, &advanced, "failure").await;
    assert_eq!(
        c.readiness(&next.target.url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap()
            .rollup,
        ReadinessRollup::Failure
    );
    git.commit(&head, "README.md", "contract line\n");
    let contract = git.commit("", "日本.md", "Unicode content\n");
    git.push(
        &remote,
        &contract,
        "contract-feature",
        &credential.username,
        &credential.password,
    );
    let aliases = [
        "contract-feature-other",
        "contract-feature-wrong-kind-false",
        "contract-feature-wrong-kind-true",
        "contract-order",
        "ticket-search-decoy",
    ]
    .into_iter()
    .map(str::to_string)
    .collect::<Vec<_>>();
    let bases = (0..101)
        .map(|n| format!("order-base-{n}"))
        .collect::<Vec<_>>();
    let mut owned = std::iter::once("push".to_string())
        .chain(std::iter::once("-q".to_string()))
        .chain(std::iter::once(remote.clone()))
        .chain(
            aliases
                .iter()
                .map(|name| format!("{contract}:refs/heads/{name}")),
        )
        .chain(bases.iter().map(|name| format!("{head}:refs/heads/{name}")))
        .collect::<Vec<_>>();
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
    contracts::code_contract(c.as_ref(), REPO, "contract-feature", "main").await;
    let contract_pr = c
        .open_pull_requests(REPO, "contract-feature")
        .await
        .unwrap()
        .remove(0);
    contracts::code_utf8_files_contract(c.as_ref(), &contract_pr, "日本.md", "Unicode content")
        .await;
    let comment = c
        .comment(&contract_pr, "Wrong subject fixture", None)
        .await
        .unwrap();
    contracts::code_kind_boundary_contract(c.as_ref(), t.as_ref(), REPO, &contract_pr).await;
    contracts::ticket_wrong_subject_contract(t.as_ref(), &contract_pr, &comment.id).await;
    contracts::code_closed_contract(c.as_ref(), &pr.target.url).await;
    contracts::ticket_search_decoy_contract(
        t.as_ref(),
        c.as_ref(),
        REPO,
        REPO,
        "ticket-search-decoy",
        "main",
    )
    .await;
    contracts::code_open_order_contract(c.as_ref(), REPO, "contract-order", &bases, true).await;
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
            .rollup
            == ReadinessRollup::Pending
        {
            finish(&base, &runner.token, &current_head, "success").await;
        }
        owner
            .pulls_review(
                REPO,
                n,
                input(json!({"verdict":"approve","commit":current_head})),
            )
            .await
            .unwrap();
        merge_checked(
            &owner,
            REPO,
            n,
            &current_head,
            &current_base,
            &base,
            &runner.token,
        )
        .await;
        contracts::code_link_case_finished(t.as_ref(), &case).await;
    }
    let driver = driver::Driver {
        owner: &owner,
        code: c.as_ref(),
        git: &git,
        base: &base,
        runner: &runner.token,
        sequence: std::sync::atomic::AtomicUsize::new(0),
    };
    contracts::ticket_patrol_contract(
        t.as_ref(),
        &driver,
        REPO,
        "bug",
        ActorName("aiko-service".into()),
        ActorName("owner".into()),
    )
    .await;
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
    let their_comment = owner
        .tickets_add_comment(
            "elsewhere/repo",
            &their.key,
            input(json!({"body":"Their comment"})),
        )
        .await
        .unwrap();
    contracts::ticket_scope_contract(
        t.as_ref(),
        REPO,
        "elsewhere/repo",
        &their.url,
        &their_comment.id,
    )
    .await;
    contracts::code_cross_repo_link_contract(c.as_ref(), &next.target.url, &their.url).await;
    let screen = owner
        .attachments_upload(
            REPO,
            Some("screen.png".into()),
            b"\x89PNG\r\n\x1a\nscreen".to_vec(),
        )
        .await
        .unwrap();
    let log = owner
        .attachments_upload(REPO, Some("log.gif".into()), b"GIF89alog".to_vec())
        .await
        .unwrap();
    let unused = owner
        .attachments_upload(
            REPO,
            Some("unused.png".into()),
            b"\x89PNG\r\n\x1a\nunused".to_vec(),
        )
        .await
        .unwrap();
    contracts::ticket_attachment_contract(
        t.as_ref(),
        REPO,
        contracts::TicketAttachmentFixture {
            screen_url: screen.url,
            screen_id: screen.id,
            log_url: log.url,
            log_id: log.id,
            unused_id: unused.id,
        },
    )
    .await;
    secrets
        .set("HUMAN_BRANVIL_TOKEN", owner_token.trim())
        .unwrap();
    let mut other_config = config;
    other_config.person_id = "human".into();
    other_config
        .accounts
        .insert("branvil".into(), ActorName("owner".into()));
    let other_factory = ServiceIntegrationFactory::new(
        other_config,
        &secrets,
        Some(running.data.clone()),
        Arc::new(|_| {}),
    )
    .unwrap();
    contracts::code_other_actor_contract(
        other_factory
            .create_code_hosting_service()
            .unwrap()
            .as_ref(),
        &next.target.url,
    )
    .await;
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
    contracts::code_missing_base_contract(c.as_ref(), &next.target.url).await;
    extra::reused_and_gates(
        temp.path(),
        &owner,
        c.as_ref(),
        &base,
        owner_token.trim(),
        &runner.token,
    )
    .await;
    readiness::contract(
        temp.path(),
        &owner,
        c.as_ref(),
        &base,
        owner_token.trim(),
        &runner.token,
    )
    .await;
    code_parity::contract(
        temp.path(),
        &owner,
        c.as_ref(),
        t.as_ref(),
        owner_token.trim(),
    )
    .await;
    c.aclose().await.unwrap();
    t.aclose().await.unwrap();
    drop(c);
    drop(t);
    drop(owner);
    drop(factory);
    running.stop().await.unwrap();
}
fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().collect();
    if args.get(1).is_some_and(|a| a == "hook") {
        return ::branvil::main(::branvil::Invocation::standalone(), args);
    }
    if args.iter().any(|a| a == "--list") {
        println!("branvil_three_paths: test");
        return ExitCode::SUCCESS;
    }
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(4)
        .enable_all()
        .build()
        .unwrap()
        .block_on(scenario());
    ExitCode::SUCCESS
}
