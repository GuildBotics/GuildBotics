//! Frozen code-host behavior through the public Branvil provider.
use super::*;
const REPOSITORY: &str = "acme/code-parity";
struct Fixture<'a> {
    owner: &'a ::branvil::api::MemberApi,
    code: &'a dyn CodeHostingService,
    tickets: &'a dyn TicketService,
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
impl Fixture<'_> {
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
            .pulls_set_reviewer(REPOSITORY, pull.number, "aiko-service", true)
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
async fn author_feedback_reactions_and_holds(f: &mut Fixture<'_>) {
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
}
async fn requested_and_inline_reviews_follow_heads(f: &mut Fixture<'_>) {
    for inline in [false, true] {
        let mut p = f.open(false, vec![]).await;
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
}
async fn threads_answer_reviews_and_later_statements(f: &mut Fixture<'_>) {
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
}
async fn inline_coordinates_and_own_verdicts(f: &mut Fixture<'_>) {
    let p = f.open(true, vec![]).await;
    let files = f
        .code
        .read(Resource::PullRequestFiles, &p.url, None, "")
        .await
        .unwrap();
    let file = files.items().iter().find(|v| v["path"] == "a.py").unwrap();
    assert_eq!(file["patch_complete"], true);
    assert_eq!(file["changes"], 2);
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
            .verdict,
        ReviewVerdict::Comment
    );
    assert_eq!(
        f.code
            .read(Resource::PullRequestComments, &p.url, None, "forged")
            .await
            .unwrap_err(),
        ServiceError::InvalidContinuation
    );
    f.close(&p).await;
}
async fn interactive_authors_reviewers_and_ancestors(f: &mut Fixture<'_>) {
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
}
async fn review_limit_is_read_only_and_requests_override_it(f: &mut Fixture<'_>) {
    let mut p = f.open(false, vec![]).await;
    for _ in 0..3 {
        f.code
            .review(&p.url, "Reviewed", ReviewVerdict::Comment, &p.head)
            .await
            .unwrap();
        f.advance(&mut p).await;
    }
    let before =
        serde_json::to_value(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap()).unwrap();
    let comments = f
        .code
        .read(Resource::PullRequestComments, &p.url, None, "")
        .await
        .unwrap();
    f.expected(&p, Some("pull_request_review_limit"), false)
        .await;
    assert_eq!(
        serde_json::to_value(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap()).unwrap(),
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
    let before =
        serde_json::to_value(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap()).unwrap();
    f.expected(&p, Some("pull_request_review"), true).await;
    assert_eq!(
        serde_json::to_value(f.owner.pulls_get(REPOSITORY, p.number).await.unwrap()).unwrap(),
        before
    );
    f.close(&p).await;
}
async fn draft_conversion_preserves_other_writers(f: &mut Fixture<'_>) {
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
}
async fn body_updates_preserve_or_drop_links_before_validation(f: &mut Fixture<'_>) {
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
        assert_eq!(
            f.code
                .update_pull_request(
                    &p.url,
                    UpdatePullRequest {
                        title: Some("Changed".into()),
                        body: Some("Changed".into()),
                        tickets: Some(vec![
                            TicketLink {
                                url: ticket.clone(),
                                closes: true
                            },
                            TicketLink {
                                url: foreign,
                                closes: true
                            }
                        ])
                    }
                )
                .await
                .unwrap_err(),
            ServiceError::Unsupported
        );
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
}
async fn outside_scope_pull_requests_are_not_patrolled(f: &mut Fixture<'_>) {
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
        .pulls_set_reviewer("outside/repo", pull.pull.number, "aiko-service", true)
        .await
        .unwrap();
    let p = Pull {
        url: pull.pull.url,
        number: pull.pull.number,
        head,
        branch: "feature".into(),
    };
    f.expected(&p, None, false).await;
    let before =
        serde_json::to_value(f.owner.pulls_get("outside/repo", p.number).await.unwrap()).unwrap();
    assert_eq!(
        f.code
            .convert_pull_request_to_draft(&p.url)
            .await
            .unwrap_err(),
        ServiceError::OutOfScope
    );
    assert_eq!(
        serde_json::to_value(f.owner.pulls_get("outside/repo", p.number).await.unwrap()).unwrap(),
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
}
pub(super) async fn contract(
    root: &Path,
    owner: &::branvil::api::MemberApi,
    code: &dyn CodeHostingService,
    tickets: &dyn TicketService,
    owner_token: &str,
) {
    owner
        .repos_create(input(json!({"path":REPOSITORY})))
        .await
        .unwrap();
    let git = Git::new(&root.join("code-parity"));
    let base = git.commit("", "a.py", "keep\nold\n");
    let remote = code.clone_url(REPOSITORY).await.unwrap();
    git.push(&remote, &base, "main", "owner", owner_token);
    let mut f = Fixture {
        owner,
        code,
        tickets,
        git,
        remote,
        base,
        owner_token,
        next: 0,
    };
    author_feedback_reactions_and_holds(&mut f).await;
    requested_and_inline_reviews_follow_heads(&mut f).await;
    threads_answer_reviews_and_later_statements(&mut f).await;
    inline_coordinates_and_own_verdicts(&mut f).await;
    interactive_authors_reviewers_and_ancestors(&mut f).await;
    review_limit_is_read_only_and_requests_override_it(&mut f).await;
    draft_conversion_preserves_other_writers(&mut f).await;
    body_updates_preserve_or_drop_links_before_validation(&mut f).await;
    outside_scope_pull_requests_are_not_patrolled(&mut f).await;
}
