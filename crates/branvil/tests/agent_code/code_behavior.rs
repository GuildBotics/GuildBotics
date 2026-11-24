//! Code-host behavior through the public Branvil provider.
use super::*;
// Landing is refreshed by a background worker, independently of the operation.
// Keep every other field so revisions, reviews and draft changes remain visible.
fn pull_state(pull: impl serde::Serialize) -> Value {
    let mut state = serde_json::to_value(pull).unwrap();
    state.as_object_mut().unwrap().remove("landing");
    state
}

#[test]
fn read_only_observations_allow_landing_progress_but_detect_pull_writes() {
    let before = json!({"draft":false,"revision":1,"title":"Work","body":"","landing":{"head_sha":"old","blockers":[{"code":"checking"}]}});
    let mut after = before.clone();
    after["landing"] = json!({"head_sha":"new","blockers":[]});
    assert_eq!(pull_state(&before), pull_state(&after));
    for (field, changed) in [
        ("draft", json!(true)),
        ("revision", json!(2)),
        ("title", json!("Changed")),
        ("body", json!("Changed")),
    ] {
        let mut written = after.clone();
        written[field] = changed;
        assert_ne!(pull_state(&before), pull_state(&written), "{field}");
    }
}

const REPOSITORY: &str = "acme/code-behavior";
struct Fixture<'a> {
    owner: &'a ::branvil::api::MemberApi,
    code: &'a CodeAgent,
    tickets: &'a TicketAgent,
    git: Git,
    remote: String,
    base: String,
    owner_token: &'a str,
    next: usize,
}
struct Pull {
    url: String,
    number: u64,
    head: String,
    branch: String,
}
impl<'a> Fixture<'a> {
    async fn new(context: &'a Context) -> Self {
        let Context {
            temp,
            owner,
            c,
            t,
            owner_token,
            ..
        } = context;
        let root = temp.path();
        let code = c.as_ref();
        let tickets = t.as_ref();
        let git = Git::new(&root.join("code-behavior"));
        let base = git.commit("", "a.py", "keep\nold\n");
        let remote = code.clone_url(REPOSITORY).await.unwrap();
        git.push(&remote, &base, "main", "owner", owner_token.trim());
        Self {
            owner,
            code,
            tickets,
            git,
            remote,
            base,
            owner_token: owner_token.trim(),
            next: 0,
        }
    }
    async fn ticket(&self, title: &str, interactive: bool, parent: Option<String>) -> String {
        let mut request = contracts::ticket_request(REPOSITORY, title);
        request.interactive = interactive;
        request.parent = parent;
        self.tickets
            .create_ticket(request)
            .await
            .unwrap()
            .target
            .url
    }

    async fn open(&mut self, own: bool, links: Vec<TicketLink>) -> Pull {
        self.next += 1;
        let branch = format!("case-{}", self.next);
        let first = self.git.commit(&self.base, "a.py", "keep\nnew\n");
        let head = self.git.commit(&first, "extra.txt", "other changed file\n");
        self.git
            .push(&self.remote, &head, &branch, "owner", self.owner_token);
        let request = json!({"head":branch,"base":"main","title":branch,"body":"","draft":false,"tickets":links});
        let url = if own {
            self.code
                .create_pull_request(CreatePullRequest {
                    repository: REPOSITORY.into(),
                    head: branch.clone(),
                    base: "main".into(),
                    title: branch.clone(),
                    body: String::new(),
                    draft: false,
                    tickets: links,
                })
                .await
                .unwrap()
                .written
                .target
                .url
        } else {
            self.owner
                .pulls_create(REPOSITORY, input(request))
                .await
                .unwrap()
                .pull
                .url
        };
        let number = url.rsplit('/').next().unwrap().parse().unwrap();
        Pull {
            url,
            number,
            head,
            branch,
        }
    }
    async fn expected(&self, pull: &Pull, reason: Option<&str>, eligible: bool) -> Option<Task> {
        let tasks = self.code.pull_request_candidates().await.unwrap();
        let task = tasks
            .into_iter()
            .find(|t| t.pull_request_url.as_deref() == Some(&pull.url));
        assert_eq!(
            task.as_ref().and_then(|t| t.trigger_reason.as_deref()),
            reason,
            "{}",
            pull.url
        );
        let probe = Task {
            pull_request_url: Some(pull.url.clone()),
            ..Default::default()
        };
        let refreshed = self.code.refresh_pull_request(&probe).await.unwrap();
        assert_eq!(refreshed, task);
        assert_eq!(
            self.code.can_receive_work(&pull.url).await.unwrap(),
            eligible
        );
        task
    }
    async fn human(&self, pull: &Pull, body: &str) {
        self.owner
            .pulls_add_comment(REPOSITORY, pull.number, input(json!({"body":body})))
            .await
            .unwrap();
    }
    async fn advance(&self, pull: &mut Pull) {
        let head = self
            .git
            .commit(&pull.head, "advance.txt", &format!("{}\n", pull.head));
        self.git
            .push(&self.remote, &head, &pull.branch, "owner", self.owner_token);
        pull.head = head;
    }
    async fn request(&self, pull: &Pull) {
        self.owner
            .pulls_set_reviewer(REPOSITORY, pull.number, "aiko", true)
            .await
            .unwrap();
    }
    async fn close(&self, pull: &Pull) {
        self.owner
            .pulls_set_open(REPOSITORY, pull.number, false)
            .await
            .unwrap();
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn author_feedback_reactions_and_holds() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    let p = f.open(true, vec![]).await;
    f.expected(&p, None, false).await;
    f.human(&p, "Please rename").await;
    let task = f
        .expected(&p, Some("pull_request_feedback"), true)
        .await
        .unwrap();
    assert_eq!(task.url, Some(p.url.clone()));
    assert_eq!(task.status, "in_progress");
    assert_eq!(task.label, format!("!{}", p.number));
    let comments = f
        .code
        .read(Resource::PullRequestComments, &p.url, None, "")
        .await
        .unwrap();
    let id = comments.items()[0]["id"].as_str().unwrap();
    f.code
        .add_reaction(&p.url, id, SemanticReaction::Ack)
        .await
        .unwrap();
    f.expected(&p, None, false).await;
    f.human(&p, "Try again").await;
    let failed = WorkflowStatus::from_payload(
        &json!({"kind":"workflow_error","reason":"failed","person_id":"aiko","run_id":"r"}),
    )
    .unwrap();
    f.code
        .comment(&p.url, "Failed", Some(&failed))
        .await
        .unwrap();
    f.expected(&p, None, false).await;
    f.human(&p, "Continue").await;
    f.expected(&p, Some("pull_request_feedback"), true).await;
    f.code
        .comment(&p.url, "Failed again", Some(&failed))
        .await
        .unwrap();
    f.expected(&p, None, false).await;
    let draft = f.owner.pulls_get(REPOSITORY, p.number).await.unwrap();
    f.owner
        .pulls_update(
            REPOSITORY,
            p.number,
            input(json!({"draft":true,"revision":draft.revision})),
        )
        .await
        .unwrap();
    let draft = f.owner.pulls_get(REPOSITORY, p.number).await.unwrap();
    f.owner
        .pulls_update(
            REPOSITORY,
            p.number,
            input(json!({"draft":false,"revision":draft.revision})),
        )
        .await
        .unwrap();
    f.expected(&p, Some("pull_request_feedback"), true).await;
    f.git.run(
        &[
            "push",
            "-q",
            &f.remote,
            &format!(":refs/heads/{}", p.branch),
        ],
        "owner",
        f.owner_token,
    );
    f.expected(&p, None, false).await;
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn requested_and_inline_reviews_follow_heads() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    for inline in [false, true] {
        let mut p = f.open(false, vec![]).await;
        let before = pull_state(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap());
        let stale = if inline {
            f.code
                .review_comment(
                    &p.url,
                    ReviewComment {
                        body: "Stale".into(),
                        head: "0".repeat(40),
                        path: "a.py".into(),
                        line: 2,
                        side: "RIGHT".into(),
                        start_line: None,
                        start_side: None,
                    },
                )
                .await
                .map(|_| ())
        } else {
            f.code
                .review(&p.url, "Stale", ReviewVerdict::Approve, &"0".repeat(40))
                .await
                .map(|_| ())
        };
        let failure = stale.unwrap_err();
        assert_eq!(
            failure.translation_key(),
            "integrations.branvil.api.head_moved"
        );
        assert!(failure.translation_args().is_empty());
        assert_eq!(
            pull_state(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap()),
            before
        );
        let page = f
            .code
            .read(
                if inline {
                    Resource::PullRequestThreads
                } else {
                    Resource::PullRequestReviews
                },
                &p.url,
                None,
                "",
            )
            .await
            .unwrap();
        assert!(page.items().is_empty());
        f.request(&p).await;
        f.expected(&p, Some("pull_request_review"), true).await;
        if inline {
            f.code
                .review_comment(
                    &p.url,
                    ReviewComment {
                        body: "Here".into(),
                        head: p.head.clone(),
                        path: "a.py".into(),
                        line: 2,
                        side: "RIGHT".into(),
                        start_line: None,
                        start_side: None,
                    },
                )
                .await
                .unwrap();
        } else {
            f.code
                .review(&p.url, "Looks good", ReviewVerdict::Approve, &p.head)
                .await
                .unwrap();
        }
        assert!(
            f.owner
                .pulls_get(REPOSITORY, p.number)
                .await
                .unwrap()
                .reviewers
                .is_empty()
        );
        f.expected(&p, None, false).await;
        f.advance(&mut p).await;
        f.expected(&p, Some("pull_request_review"), true).await;
        f.close(&p).await;
    }
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn threads_answer_reviews_and_later_statements() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    for own in [true, false] {
        let p = f.open(own, vec![]).await;
        let root = f
            .owner
            .pulls_inline_comment(
                REPOSITORY,
                p.number,
                input(
                    json!({"body":"Rename","commit":p.head,"path":"a.py","line":2,"side":"RIGHT"}),
                ),
            )
            .await
            .unwrap();
        if own {
            f.owner
                .pulls_review(
                    REPOSITORY,
                    p.number,
                    input(json!({"verdict":"request_changes","body":"See inline","commit":p.head})),
                )
                .await
                .unwrap();
            f.expected(&p, Some("pull_request_feedback"), true).await;
        }
        f.code
            .reply(&p.url, &root.comment.id, "Renamed")
            .await
            .unwrap();
        f.expected(&p, None, false).await;
        if !own {
            f.human(&p, "What about tests?").await;
            f.expected(&p, Some("pull_request_review"), true).await;
        }
        f.close(&p).await;
    }
    let p = f.open(true, vec![]).await;
    f.owner
        .pulls_inline_comment(
            REPOSITORY,
            p.number,
            input(json!({"body":"Rename","commit":p.head,"path":"a.py","line":2,"side":"RIGHT"})),
        )
        .await
        .unwrap();
    f.expected(&p, Some("pull_request_feedback"), true).await;
    let page = f
        .code
        .read(Resource::PullRequestThreads, &p.url, None, "")
        .await
        .unwrap();
    let thread = page.items()[0]["id"].as_str().unwrap();
    f.owner
        .pulls_resolve(
            REPOSITORY,
            p.number,
            thread,
            input(json!({"resolved":true})),
        )
        .await
        .unwrap();
    f.expected(&p, None, false).await;
    f.close(&p).await;
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn inline_coordinates_and_own_verdicts() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    let p = f.open(true, vec![]).await;
    let files = f
        .code
        .read(Resource::PullRequestFiles, &p.url, None, "")
        .await
        .unwrap();
    let file = files.items().iter().find(|v| v["path"] == "a.py").unwrap();
    assert_eq!(file["patch_complete"], true);
    assert_eq!(
        (&file["additions"], &file["deletions"]),
        (&json!(1), &json!(1))
    );
    let first = f
        .code
        .read(
            Resource::PullRequestFiles,
            &p.url,
            Some(&json!({"page_size":1})),
            "",
        )
        .await
        .unwrap();
    assert_eq!(first.items().len(), 1);
    assert_eq!(first.items()[0]["path"], "a.py");
    let rest = f
        .code
        .read(
            Resource::PullRequestFiles,
            &p.url,
            Some(&json!({"page_size":1})),
            first.continuation().unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(rest.items().len(), 1);
    assert_eq!(rest.items()[0]["path"], "extra.txt");
    assert_eq!(rest.continuation(), None);

    assert_eq!(
        file["commentable_lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|line| (
                line["line"].as_u64().unwrap(),
                line["side"].as_str().unwrap()
            ))
            .collect::<Vec<_>>(),
        vec![(1, "RIGHT"), (2, "LEFT"), (2, "RIGHT")]
    );
    let before = f
        .code
        .read(Resource::PullRequestThreads, &p.url, None, "")
        .await
        .unwrap();
    assert!(
        f.code
            .review_comment(
                &p.url,
                ReviewComment {
                    body: "No".into(),
                    head: p.head.clone(),
                    path: "b.py".into(),
                    line: 1,
                    side: "RIGHT".into(),
                    start_line: None,
                    start_side: None
                }
            )
            .await
            .is_err()
    );
    assert_eq!(
        f.code
            .read(Resource::PullRequestThreads, &p.url, None, "")
            .await
            .unwrap(),
        before
    );
    let before = f
        .code
        .read(Resource::PullRequestReviews, &p.url, None, "")
        .await
        .unwrap();
    for verdict in [ReviewVerdict::Approve, ReviewVerdict::RequestChanges] {
        assert!(
            f.code
                .review(&p.url, "Own", verdict, &p.head)
                .await
                .is_err()
        );
    }
    assert_eq!(
        f.code
            .read(Resource::PullRequestReviews, &p.url, None, "")
            .await
            .unwrap(),
        before
    );
    assert_eq!(
        f.code
            .review(&p.url, "Note", ReviewVerdict::Comment, &p.head)
            .await
            .unwrap()
            .view
            .verdict,
        "comment"
    );
    assert_eq!(
        f.code
            .read(Resource::PullRequestComments, &p.url, None, "forged")
            .await
            .unwrap_err(),
        ServiceError::InvalidContinuation
    );
    f.close(&p).await;
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn interactive_authors_reviewers_and_ancestors() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    for depth in [0, 2] {
        for role in [
            "pull_request_feedback",
            "pull_request_checks",
            "pull_request_review",
        ] {
            let holder = f.ticket("holder", false, None).await;
            let mut child = holder.clone();
            for _ in 0..depth {
                child = f.ticket("child", false, Some(child)).await;
            }
            let p = f
                .open(
                    role != "pull_request_review",
                    vec![TicketLink {
                        url: child.clone(),
                        closes: false,
                    }],
                )
                .await;
            if role == "pull_request_review" {
                f.request(&p).await;
            } else if role == "pull_request_feedback" {
                f.human(&p, "Please fix").await;
            } else {
                let status=WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"awaiting_checks","person_id":"aiko","run_id":"r","subject":child,"head":p.head,"work_kind":"ticket","work_identity":child})).unwrap();
                f.code
                    .comment(&p.url, "Waiting", Some(&status))
                    .await
                    .unwrap();
            }
            f.expected(&p, Some(role), role != "pull_request_checks")
                .await;
            let key = holder.rsplit('/').next().unwrap();
            f.owner
                .tickets_update_ticket(REPOSITORY, key, input(json!({"interactive":true})))
                .await
                .unwrap();
            let reviewed = role == "pull_request_review";
            f.expected(&p, if reviewed { Some(role) } else { None }, reviewed)
                .await;
            assert!(!f.owner.pulls_get(REPOSITORY, p.number).await.unwrap().draft);
            f.owner
                .tickets_update_ticket(REPOSITORY, key, input(json!({"interactive":false})))
                .await
                .unwrap();
            f.expected(&p, Some(role), role != "pull_request_checks")
                .await;
            f.close(&p).await;
        }
    }
    let quiet = f.ticket("quiet", false, None).await;
    let interactive = f.ticket("interactive", true, None).await;
    for (links, held) in [
        (vec!["https://tickets.example.com/T-1".to_string()], false),
        (vec![quiet.clone()], false),
        (
            vec!["https://tickets.example.com/T-1".into(), quiet, interactive],
            true,
        ),
    ] {
        let p = f
            .open(
                true,
                links
                    .into_iter()
                    .map(|url| TicketLink { url, closes: false })
                    .collect(),
            )
            .await;
        f.human(&p, "Please fix").await;
        f.expected(
            &p,
            if held {
                None
            } else {
                Some("pull_request_feedback")
            },
            !held,
        )
        .await;
        f.close(&p).await;
    }
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn review_limit_is_read_only_and_requests_override_it() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    let mut p = f.open(false, vec![]).await;
    for _ in 0..3 {
        f.code
            .review(&p.url, "Reviewed", ReviewVerdict::Comment, &p.head)
            .await
            .unwrap();
        f.advance(&mut p).await;
    }
    let before = pull_state(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap());
    let comments = f
        .code
        .read(Resource::PullRequestComments, &p.url, None, "")
        .await
        .unwrap();
    f.expected(&p, Some("pull_request_review_limit"), false)
        .await;
    assert_eq!(
        pull_state(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap()),
        before
    );
    assert_eq!(
        f.code
            .read(Resource::PullRequestComments, &p.url, None, "")
            .await
            .unwrap(),
        comments
    );
    assert_eq!(before["draft"], false);
    f.request(&p).await;
    let before = pull_state(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap());
    f.expected(&p, Some("pull_request_review"), true).await;
    assert_eq!(
        pull_state(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap()),
        before
    );
    f.close(&p).await;
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn draft_conversion_preserves_other_writers() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    for draft in [true, false] {
        let p = f.open(false, vec![]).await;
        let current = f.owner.pulls_get(REPOSITORY, p.number).await.unwrap();
        f.owner
            .pulls_update(
                REPOSITORY,
                p.number,
                input(json!({"revision":current.revision,"draft":draft,"title":"human update"})),
            )
            .await
            .unwrap();
        let stale = f
            .owner
            .pulls_update(
                REPOSITORY,
                p.number,
                input(json!({"revision":current.revision,"draft":true})),
            )
            .await
            .unwrap_err();
        assert_eq!(stale.code, "conflict");
        assert_eq!(
            f.code.convert_pull_request_to_draft(&p.url).await.unwrap(),
            !draft
        );
        let after = f.owner.pulls_get(REPOSITORY, p.number).await.unwrap();
        assert!(after.draft);
        assert_eq!(after.title, "human update");
        assert!(
            f.code
                .read(Resource::PullRequestComments, &p.url, None, "")
                .await
                .unwrap()
                .items()
                .is_empty()
        );
        f.close(&p).await;
    }
    let p = f.open(false, vec![]).await;
    let (first, second) = tokio::join!(
        f.code.convert_pull_request_to_draft(&p.url),
        f.code.convert_pull_request_to_draft(&p.url)
    );
    let mut results = [first.unwrap(), second.unwrap()];
    results.sort();
    assert_eq!(results, [false, true]);
    f.close(&p).await;
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn body_updates_preserve_or_drop_links_before_validation() {
    let context = Context::with_repository(REPOSITORY).await;
    let mut f = Fixture::new(&context).await;
    let ticket = f.ticket("link", false, None).await;
    let p = f
        .open(
            true,
            vec![TicketLink {
                url: ticket.clone(),
                closes: true,
            }],
        )
        .await;
    f.code
        .update_pull_request(
            &p.url,
            UpdatePullRequest {
                body: Some("New".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(
        f.code
            .read(Resource::TicketPullRequests, &ticket, None, "")
            .await
            .unwrap()
            .items()[0]["url"],
        p.url
    );
    f.owner
        .repos_create(input(json!({"path":"acme/code-other"})))
        .await
        .unwrap();
    let foreign_ticket = f
        .owner
        .tickets_create_ticket("acme/code-other", input(json!({"title":"Other","body":""})))
        .await
        .unwrap();
    for foreign in [
        "https://tickets.example.com/T-1".to_string(),
        foreign_ticket.url,
    ] {
        let before = f
            .code
            .read(Resource::PullRequests, &p.url, None, "")
            .await
            .unwrap();
        let error = f
            .code
            .update_pull_request(
                &p.url,
                UpdatePullRequest {
                    title: Some("Changed".into()),
                    body: Some("Changed".into()),
                    tickets: Some(vec![
                        TicketLink {
                            url: ticket.clone(),
                            closes: true,
                        },
                        TicketLink {
                            url: foreign.clone(),
                            closes: true,
                        },
                    ]),
                },
            )
            .await
            .unwrap_err();
        let expected = ServiceError::Diagnostic {
            class: ServiceErrorClass::Unsupported,
            key: "integrations.branvil.api.closes_foreign",
            args: [
                ("repository".into(), REPOSITORY.into()),
                ("url".into(), foreign.clone()),
            ]
            .into(),
        };
        assert_eq!(error, expected);
        f.git
            .push(&f.remote, &p.head, "foreign-close", "owner", f.owner_token);
        let error = f
            .code
            .create_pull_request(CreatePullRequest {
                repository: REPOSITORY.into(),
                head: "foreign-close".into(),
                base: "main".into(),
                title: "Refused".into(),
                body: String::new(),
                draft: false,
                tickets: vec![TicketLink {
                    url: foreign,
                    closes: true,
                }],
            })
            .await
            .unwrap_err();
        assert_eq!(error, expected);
        assert_eq!(
            f.code
                .read(Resource::PullRequests, &p.url, None, "")
                .await
                .unwrap(),
            before
        );
    }
    f.code
        .update_pull_request(
            &p.url,
            UpdatePullRequest {
                body: Some("Newer".into()),
                tickets: Some(vec![]),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert!(
        f.code
            .read(Resource::TicketPullRequests, &ticket, None, "")
            .await
            .unwrap()
            .items()
            .is_empty()
    );
    assert_eq!(
        f.code
            .read(Resource::PullRequests, &p.url, None, "")
            .await
            .unwrap()
            .items()[0]["body"],
        "Newer"
    );
    f.close(&p).await;
    context.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn outside_scope_pull_requests_are_not_patrolled() {
    let context = Context::with_repository(REPOSITORY).await;
    let f = Fixture::new(&context).await;
    f.owner
        .repos_create_workspace(input(json!({"name":"outside"})))
        .await
        .unwrap();
    f.owner
        .repos_create(input(json!({"path":"outside/repo"})))
        .await
        .unwrap();
    let remote = f.code.clone_url("outside/repo").await.unwrap();
    f.git.push(&remote, &f.base, "main", "owner", f.owner_token);
    let head = f.git.commit(&f.base, "outside.txt", "outside\n");
    f.git
        .push(&remote, &head, "feature", "owner", f.owner_token);
    let pull = f
        .owner
        .pulls_create(
            "outside/repo",
            input(
                json!({"head":"feature","base":"main","title":"Outside","body":"","draft":false}),
            ),
        )
        .await
        .unwrap();
    f.owner
        .pulls_set_reviewer("outside/repo", pull.pull.number, "aiko", true)
        .await
        .unwrap();
    let p = Pull {
        url: pull.pull.url,
        number: pull.pull.number,
        head,
        branch: "feature".into(),
    };
    f.expected(&p, None, false).await;
    let before = pull_state(f.owner.pulls_get("outside/repo", p.number).await.unwrap());
    assert_eq!(
        f.code
            .convert_pull_request_to_draft(&p.url)
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        pull_state(f.owner.pulls_get("outside/repo", p.number).await.unwrap()),
        before
    );
    assert!(
        f.code
            .read(Resource::PullRequestComments, &p.url, None, "")
            .await
            .unwrap()
            .items()
            .is_empty()
    );
    context.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ticket_base_uses_nearest_draft_ancestor_and_create_reports_it() {
    let context = Context::with_repository(REPOSITORY).await;
    let f = Fixture::new(&context).await;
    let root = f.ticket("root", false, None).await;
    let parent = f.ticket("parent", false, Some(root.clone())).await;
    let child = f.ticket("child", false, Some(parent.clone())).await;
    let key = |url: &str| url.rsplit('/').next().unwrap().to_string();
    let root_branch = format!("ticket/{}", key(&root));
    let parent_branch = format!("ticket/{}", key(&parent));
    let child_branch = format!("ticket/{}", key(&child));
    let head = f.git.commit(&f.base, "feature.txt", "feature");
    for branch in [&root_branch, &parent_branch, &child_branch] {
        f.git.push(&f.remote, &head, branch, "owner", f.owner_token);
    }
    assert_eq!(
        f.code.ticket_base(REPOSITORY, &key(&child)).await.unwrap(),
        "main"
    );
    let root_pr = f
        .owner
        .pulls_create(
            REPOSITORY,
            input(json!({"head":root_branch,"base":"main","title":"collect root","draft":true})),
        )
        .await
        .unwrap();
    assert_eq!(
        f.code.ticket_base(REPOSITORY, &key(&child)).await.unwrap(),
        root_branch
    );
    // A ready PR is not a collection signal.
    let parent_pr = f
        .owner
        .pulls_create(
            REPOSITORY,
            input(json!({"head":parent_branch,"base":"main","title":"parent","draft":false})),
        )
        .await
        .unwrap();
    assert_eq!(
        f.code.ticket_base(REPOSITORY, &key(&child)).await.unwrap(),
        root_branch
    );
    f.owner
        .pulls_update(
            REPOSITORY,
            parent_pr.pull.number,
            input(json!({"draft":true})),
        )
        .await
        .unwrap();
    assert_eq!(
        f.code.ticket_base(REPOSITORY, &key(&child)).await.unwrap(),
        parent_branch
    );
    let request = |base: &str| CreatePullRequest {
        repository: REPOSITORY.into(),
        head: child_branch.clone(),
        base: base.into(),
        title: "child".into(),
        body: String::new(),
        draft: false,
        tickets: vec![TicketLink {
            url: child.clone(),
            closes: false,
        }],
    };
    let made = f.code.create_pull_request(request("")).await.unwrap();
    assert_eq!(made.written.view.base, parent_branch);
    f.owner
        .pulls_update(
            REPOSITORY,
            parent_pr.pull.number,
            input(json!({"draft":false})),
        )
        .await
        .unwrap();
    let ready_reuse = f.code.create_pull_request(request("")).await.unwrap();
    assert!(!ready_reuse.created);
    assert_eq!(ready_reuse.written.view.number, made.written.view.number);
    f.owner
        .pulls_set_open(REPOSITORY, parent_pr.pull.number, false)
        .await
        .unwrap();
    assert_eq!(
        f.code.ticket_base(REPOSITORY, &key(&child)).await.unwrap(),
        root_branch
    );
    f.owner
        .pulls_update(
            REPOSITORY,
            root_pr.pull.number,
            input(json!({"draft":false})),
        )
        .await
        .unwrap();
    assert_eq!(
        f.code.ticket_base(REPOSITORY, &key(&child)).await.unwrap(),
        "main"
    );
    let reused = f.code.create_pull_request(request("")).await.unwrap();
    assert!(!reused.created);
    assert_eq!(reused.written.view.number, made.written.view.number);
    assert_eq!(reused.written.view.base, parent_branch);
    let explicit = f.code.create_pull_request(request("main")).await.unwrap();
    assert_eq!(explicit.written.view.base, "main");
    assert_eq!(
        f.code.create_pull_request(request("")).await.err().unwrap(),
        ::branvil::agent::diagnostic("integrations.branvil.api.ambiguous_base", [])
    );
    context.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn collecting_branch_failure_runs_repair_and_holds_only_its_children() {
    let context = Context::new().await;
    let Context {
        owner,
        t,
        c,
        base,
        runner,
        owner_token,
        temp,
        ..
    } = &context;
    let git = Git::new(&temp.path().join("collection-repair"));
    let remote = c.clone_url(REPO).await.unwrap();
    let main = git.commit(
        "",
        ".guildbotics-ci.toml",
        "[[jobs]]\nname='rust'\nrequired=true\nimage='docker.io/library/alpine:3.22'\ncommands=['true']\npaths=['src/**']\n",
    );
    git.push(&remote, &main, "main", "owner", owner_token.trim());
    finish(base, runner, &main, "success").await;

    let parent = create(t, "Collection").await;
    owner
        .tickets_create_ticket(
            REPO,
            input(json!({
                "title": "Ordinary task", "parent": parent, "assignee": "aiko", "lane": "ready",
            })),
        )
        .await
        .unwrap();
    let collecting = format!("ticket/{}", parent.rsplit('/').next().unwrap());

    let broken = git.commit(&main, "src/broken.txt", "broken");
    git.push(&remote, &broken, &collecting, "owner", owner_token.trim());
    let draft = owner
        .pulls_create(
            REPO,
            input(json!({
                "head": collecting, "base": "main", "title": "Collect", "draft": true,
            })),
        )
        .await
        .unwrap()
        .pull;
    assert!(draft.draft);
    finish(base, runner, &broken, "failure").await;

    let repair = tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let tasks = t.candidates().await.unwrap();
            if let Some(first) = tasks.first().filter(|task| task.repair) {
                assert_eq!(first.assignee.as_deref(), Some("aiko"));
                assert_eq!(first.status, "ready");
                assert_eq!(first.trigger_reason.as_deref(), Some("ready_lane"));
                return first.url.clone().unwrap();
            }
            tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        }
    })
    .await
    .expect("the patrol should select the repair ahead of ordinary work");
    let repair_key = repair.rsplit('/').next().unwrap();
    let repair_view = t.ticket_view(&repair).await.unwrap();
    assert_eq!(
        repair_view.repair_branch.as_deref(),
        Some(collecting.as_str())
    );
    assert_eq!(c.ticket_base(REPO, repair_key).await.unwrap(), collecting);

    let child = git.commit(&broken, "notes.txt", "documentation");
    git.push(&remote, &child, "ordinary", "owner", owner_token.trim());
    let ordinary = owner
        .pulls_create(
            REPO,
            input(json!({
                "head": "ordinary", "base": collecting, "title": "Change documentation",
            })),
        )
        .await
        .unwrap()
        .pull;
    let readiness = c
        .read(Resource::PullRequestReadiness, &ordinary.url, None, "")
        .await
        .unwrap();
    let report = &readiness.items()[0];
    assert_eq!(report["required_checks"], "passed", "{report}");
    assert_eq!(report["base_failure"]["sha"], broken);
    assert_eq!(report["base_failure"]["failed_jobs"][0]["name"], "rust");
    assert!(report["base_failure"]["failed_jobs"][0]["attempt_id"].is_string());
    assert_eq!(report["base_failure"]["repair"]["url"], repair);

    let repo = owner.repos_get(REPO).await.unwrap();
    let before = owner.pulls_get(REPO, ordinary.number).await.unwrap();
    assert!(
        before
            .landing
            .unwrap()
            .blockers
            .iter()
            .all(|b| b.code != "base_checks_failed")
    );
    owner
        .repos_update(
            REPO,
            input(json!({
                "revision": repo.revision, "hold_while_failing": true, "required_approvals": 0,
            })),
        )
        .await
        .unwrap();
    let view = owner.pulls_get(REPO, ordinary.number).await.unwrap();
    let blockers = view.landing.unwrap().blockers;
    assert!(
        blockers
            .iter()
            .any(|b| b.code == "base_checks_failed" && b.jobs == ["rust"])
    );
    let error = owner
        .pulls_merge(
            REPO,
            ordinary.number,
            input(json!({
                "expected_head": child, "expected_base": broken,
            })),
        )
        .await
        .unwrap_err();
    assert_eq!(error.code, "not_mergeable");
    assert!(error.message.contains("base_checks_failed"));

    let main_child = git.commit(&main, "unrelated.txt", "unrelated");
    git.push(
        &remote,
        &main_child,
        "unrelated",
        "owner",
        owner_token.trim(),
    );
    let unrelated = owner
        .pulls_create(
            REPO,
            input(json!({
                "head": "unrelated", "base": "main", "title": "Unrelated",
            })),
        )
        .await
        .unwrap()
        .pull;
    assert!(
        owner
            .pulls_checks(REPO, unrelated.number)
            .await
            .unwrap()
            .base_failure
            .is_none()
    );
    git.push(
        &remote,
        &main_child,
        "watch-main",
        "owner",
        owner_token.trim(),
    );
    let watch = owner
        .pulls_create(
            REPO,
            input(json!({
                "head": "watch-main", "base": "main", "title": "Watch main advance",
            })),
        )
        .await
        .unwrap()
        .pull;
    let advanced = owner
        .pulls_merge(
            REPO,
            unrelated.number,
            input(json!({
                "expected_head": main_child, "expected_base": main,
            })),
        )
        .await
        .unwrap()
        .merge_sha
        .unwrap();

    let wait_for_landing = |head: String| {
        let advanced = &advanced;
        async move {
            tokio::time::timeout(std::time::Duration::from_secs(15), async {
                loop {
                    let view = owner.pulls_get(REPO, watch.number).await.unwrap();
                    if view.landing.is_some_and(|landing| {
                        landing.entry.head_sha == head && landing.entry.base_sha == *advanced
                    }) {
                        break;
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(30)).await;
                }
            })
            .await
            .expect("landing should refresh the watch pull");
        }
    };
    let mut watch_head = main_child;
    wait_for_landing(watch_head.clone()).await;
    // Autoland can refresh landing twice in a sweep even with auto-land off.
    // Two further refreshes ensure a later sweep saw the new main tip.
    for marker in ["first", "second"] {
        watch_head = git.commit(&watch_head, "watch.txt", marker);
        git.push(
            &remote,
            &watch_head,
            "watch-main",
            "owner",
            owner_token.trim(),
        );
        wait_for_landing(watch_head.clone()).await;
    }
    let comments = t
        .read(Resource::TicketComments, &repair, None, "")
        .await
        .unwrap();
    assert!(
        comments.items().is_empty(),
        "moving main did not change the collection failure: {:?}",
        comments.items()
    );

    let attempt = report["base_failure"]["failed_jobs"][0]["attempt_id"]
        .as_str()
        .unwrap();
    let retry = owner
        .job_retry(REPO, attempt, Default::default())
        .await
        .unwrap();
    assert_ne!(retry.id, attempt);
    finish(base, runner, &broken, "failure").await;
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let comments = t
                .read(Resource::TicketComments, &repair, None, "")
                .await
                .unwrap();
            if let Some(comment) = comments.items().first() {
                assert_eq!(comments.items().len(), 1, "{:?}", comments.items());
                let body = comment["body"].as_str().unwrap();
                assert!(body.contains("Still failing at this commit"), "{body}");
                assert!(body.contains(&retry.id), "{body}");
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        }
    })
    .await
    .expect("a new failed attempt on the same commit should be reported");

    let fix = format!("ticket/{repair_key}");
    let fixed = git.commit(&broken, "src/broken.txt", "fixed");
    git.push(&remote, &fixed, &fix, "owner", owner_token.trim());
    let pr = c
        .create_pull_request(CreatePullRequest {
            repository: REPO.into(),
            head: fix,
            base: String::new(),
            title: "Repair the collection branch".into(),
            body: String::new(),
            draft: false,
            tickets: vec![TicketLink {
                url: repair.clone(),
                closes: false,
            }],
        })
        .await
        .unwrap()
        .written
        .view;
    assert_eq!(pr.base, collecting);
    finish(base, runner, &fixed, "success").await;
    assert_eq!(t.ticket_view(&repair).await.unwrap().state, "open");
    owner
        .pulls_merge(
            REPO,
            pr.number,
            input(json!({
                "expected_head": fixed, "expected_base": broken,
            })),
        )
        .await
        .unwrap();

    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let ticket = t.ticket_view(&repair).await.unwrap();
            if ticket.state == "closed" {
                assert_eq!(ticket.state_reason.as_deref(), Some("completed"));
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        }
    })
    .await
    .expect("passing checks of the collection tip should close the repair");
    assert!(
        owner
            .pulls_checks(REPO, ordinary.number)
            .await
            .unwrap()
            .base_failure
            .is_none()
    );

    context.stop().await;
}
