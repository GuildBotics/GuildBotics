//! Member agents over an embedded Branvil, with its test hook for failures.
use branvil::agent::port::*;
use branvil::agent::{CodeAgent, TicketAgent};
use branvil::test_support::{Fixture, input};
use serde_json::json;

/// Every operation of a code agent and of the ticket agent it derefs to.
pub const OPERATIONS: &[&str] = &[
    "read",
    "attachment",
    "comment",
    "create_ticket",
    "update_ticket",
    "add_reaction",
    "candidates",
    "refresh",
    "can_receive_work",
    "move_ticket",
    "closed_since",
    "retry_job",
    "yield_pull_request_checks",
    "linked_tickets",
    "create_pull_request",
    "update_pull_request",
    "review",
    "review_comment",
    "reply",
    "readiness",
    "open_pull_requests",
    "artifact",
    "clone_url",
    "default_branch",
    "pull_request_head",
    "git_credential",
    "authored_open_pull_requests",
    "pull_request_candidates",
    "refresh_pull_request",
    "convert_pull_request_to_draft",
];
const PIPELINE: &str =
    "[[jobs]]\nname = \"test\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\n";
const REQUIRED_PIPELINE: &str = "[[jobs]]\nname = \"test\"\nrequired = true\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\n";
pub const WORKSPACE: &str = "acme";
pub const REPO: &str = "acme/repo";
/// A repository of another workspace: readable, outside the write scope.
pub const FORK: &str = "fork/repo";

/// The member `aiko` acting on `acme`, whose `acme/repo` main has a readme and a pipeline.
pub struct Embedded {
    pub fixture: Fixture,
    pub code: CodeAgent,
    pub main: String,
}
impl Embedded {
    pub async fn new() -> Self {
        let fixture = Fixture::new("owner").await;
        fixture.install_test_hooks("hook");
        fixture.workspace(WORKSPACE).await;
        fixture.repository(REPO).await;
        let first = fixture.commit(REPO, "main", None, "readme", "first\n");
        // Every head registers a `test` job; no runner ever finishes it.
        let main = fixture.commit(REPO, "main", Some(&first), ".guildbotics-ci.toml", PIPELINE);
        let code = CodeAgent::new(
            fixture.data(),
            WORKSPACE.into(),
            "aiko".into(),
            "Aiko".into(),
        );
        Self {
            fixture,
            code,
            main,
        }
    }
    /// A second workspace whose repository has the same first commit.
    pub async fn fork(&self) -> String {
        self.fixture.workspace("fork").await;
        self.fixture.repository(FORK).await;
        self.fixture.commit(FORK, "main", None, "readme", "first\n")
    }
    pub fn fail_all(&self, error: ServiceError) {
        for operation in OPERATIONS {
            self.code.faults.fail(operation, error.clone());
        }
    }
    pub fn calls(&self) -> Vec<(&'static str, String)> {
        self.code.faults.calls()
    }
    pub fn base(&self) -> String {
        self.fixture.data().base_url().to_owned()
    }
    /// A ticket of `acme/repo` created by the owner.
    pub async fn ticket(&self, title: &str) -> String {
        self.fixture
            .owner
            .tickets_create_ticket(REPO, input(json!({"title":title})))
            .await
            .unwrap()
            .url
    }
    pub async fn close_ticket(&self, url: &str) {
        self.code
            .update_ticket(
                url,
                UpdateTicket {
                    state: Some("closed".into()),
                    ..Default::default()
                },
            )
            .await
            .unwrap();
    }
    /// A pull request from a new branch of main's tip, by aiko or by the owner.
    pub async fn pull(&self, branch: &str, by_aiko: bool, tickets: &[&str]) -> String {
        let main = self.fixture.git(REPO, &["rev-parse", "refs/heads/main"]);
        self.fixture
            .commit(REPO, branch, Some(&main), "readme", branch);
        if by_aiko {
            self.code
                .create_pull_request(CreatePullRequest {
                    repository: REPO.into(),
                    head: branch.into(),
                    base: "main".into(),
                    title: branch.into(),
                    body: String::new(),
                    draft: false,
                    tickets: tickets
                        .iter()
                        .map(|url| TicketLink {
                            url: (*url).into(),
                            closes: false,
                        })
                        .collect(),
                })
                .await
                .unwrap()
                .written
                .target
                .url
        } else {
            self.fixture
                .pull(
                    &self.fixture.owner,
                    REPO,
                    json!({"title":branch,"head":branch,"base":"main",
                        "tickets":tickets.iter().map(|url| json!({"url":url})).collect::<Vec<_>>()}),
                )
                .await
        }
    }
    /// The status notices on a pull request, oldest first.
    pub async fn notices(&self, pull: &str) -> Vec<serde_json::Value> {
        self.code
            .read(Resource::PullRequestComments, pull, None, "")
            .await
            .unwrap()
            .items()
            .iter()
            .filter(|c| !c["meta"].is_null())
            .map(|c| c["meta"].clone())
            .collect()
    }
    pub async fn close_pull(&self, url: &str) {
        let number = url.rsplit('/').next().unwrap().parse().unwrap();
        self.fixture
            .owner
            .pulls_set_open(REPO, number, false)
            .await
            .unwrap();
    }
    /// main starts requiring `test`, so a later head of it without a finished
    /// `test` check is running.
    pub fn require_checks(&self) {
        let main = self.fixture.git(REPO, &["rev-parse", "refs/heads/main"]);
        self.fixture.commit(
            REPO,
            "main",
            Some(&main),
            ".guildbotics-ci.toml",
            REQUIRED_PIPELINE,
        );
    }
    /// Main's verification fails each automatic retry, so the service opens
    /// its repair ticket.
    pub async fn repair_ticket(&self) -> String {
        let owner = &self.fixture.owner;
        let state = || async {
            serde_json::to_value(owner.repos_verification(REPO).await.unwrap()).unwrap()
        };
        let mut poke = 0;
        tokio::time::timeout(std::time::Duration::from_secs(60), async {
            loop {
                let before = state().await;
                if let Some(url) = before["repair"]["url"].as_str() {
                    return url.to_owned();
                }
                self.fixture
                    .check(REPO, "main", &self.main, Some("failure"))
                    .await;
                // A new pull request registers its checks, which advances the verification.
                poke += 1;
                self.pull(&format!("verify-{poke}"), true, &[]).await;
                for _ in 0..100 {
                    let now = state().await;
                    if now["current"] != before["current"] || now["repair"].is_object() {
                        break;
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                }
            }
        })
        .await
        .expect("main's failed verification did not open its repair ticket")
    }
    pub async fn stop(self) {
        self.code.aclose().await.unwrap();
        drop(self.code);
        self.fixture.stop().await;
    }
}

impl gb_server::member::repository::RepositoryServices for Embedded {
    fn tickets(&self) -> gb_member::Result<&TicketAgent> {
        Ok(&self.code)
    }
    fn code(&self) -> gb_member::Result<&CodeAgent> {
        Ok(&self.code)
    }
}
