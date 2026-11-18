use gb_ports::*;
use serde_json::json;
pub fn ticket_request(tracker: &str, title: &str) -> CreateTicket {
    CreateTicket {
        tracker: tracker.into(),
        title: title.into(),
        body: String::new(),
        labels: vec![],
        add_to_board: true,
        interactive: false,
        assignee: None,
        lane: None,
        parent: None,
        blocked_by: vec![],
    }
}
use branvil::test_support::ticket_items as tracker_items;
pub async fn ticket_search_decoy_contract(
    t: &dyn TicketService,
    c: &dyn CodeHostingService,
    tracker: &str,
    repository: &str,
    head: &str,
    base: &str,
) {
    let title = "Contractneedle-PR-only-match";
    let pr = c
        .create_pull_request(CreatePullRequest {
            repository: repository.into(),
            head: head.into(),
            base: base.into(),
            title: title.into(),
            body: String::new(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap();
    assert_eq!(pr.target.title, title);
    assert!(
        tracker_items(t, tracker, &json!({"text":title,"state":"all"}))
            .await
            .is_empty()
    );
}

pub async fn code_contract(
    service: &dyn CodeHostingService,
    repository: &str,
    head_branch: &str,
    base: &str,
) {
    // Invalid semantic values stop at the typed port boundary before a provider call.
    assert!(serde_json::from_value::<SemanticReaction>(json!("eyes")).is_err());
    let request = |branch: &str| CreatePullRequest {
        repository: repository.into(),
        head: branch.into(),
        base: base.into(),
        title: "contract PR".into(),
        body: "contract body".into(),
        draft: false,
        tickets: vec![],
    };
    let pr = service
        .create_pull_request(request(head_branch))
        .await
        .unwrap();
    assert!(pr.created);
    assert!(
        service
            .authored_open_pull_requests()
            .await
            .unwrap()
            .contains(&pr.target.url)
    );
    let again = service
        .create_pull_request(request(head_branch))
        .await
        .unwrap();
    assert!(!again.created);
    assert_eq!(again.target, pr.target);
    let other = service
        .create_pull_request(request(&format!("{head_branch}-other")))
        .await
        .unwrap();
    let url = &pr.target.url;
    let page = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert_eq!(page.target(), Some(&pr.target));
    let read = &page.items()[0];
    assert_eq!(read["head"], head_branch);
    assert_eq!(read["base"], base);
    assert_eq!(read["author_type"], "Assistant");
    assert_eq!(read["repository"], repository);
    assert_eq!(read["head_repository"], repository);
    let head = read["head_sha"].as_str().unwrap();
    assert!(!head.is_empty());
    let credential = service.git_credential().await.unwrap();
    if let Some(credential) = credential {
        assert!(!credential.username.is_empty());
        assert!(!credential.password.is_empty());
    }
    let remote = service.clone_url(repository).await.unwrap();
    assert_eq!(
        service.repository_from_remote(&remote).as_deref(),
        Some(repository)
    );
    assert_eq!(
        service.repository_from_remote("https://foreign.invalid/repo.git"),
        None
    );
    assert_eq!(service.default_branch(repository).await.unwrap(), base);
    service.check_push(repository).unwrap();
    let branch = service.pull_request_head(url).await.unwrap();
    assert_eq!(branch.repository, repository);
    assert_eq!(branch.branch, head_branch);
    assert_eq!(
        service
            .open_pull_requests(repository, head_branch)
            .await
            .unwrap(),
        vec![url.clone()]
    );
    assert!(
        service
            .open_pull_requests(repository, "contract-nonexistent")
            .await
            .unwrap()
            .is_empty()
    );
    let commit_url = service.commit_url(repository, head);
    assert!(commit_url.is_empty() || commit_url.contains(head));
    match service
        .read(Resource::DependencyAlerts, repository, None, "")
        .await
    {
        Ok(page) => {
            for alert in page.items() {
                assert!(alert["id"].as_str().is_some_and(|id| !id.is_empty()));
                assert!(
                    ["open", "resolved", "dismissed"].contains(&alert["state"].as_str().unwrap())
                );
            }
        }
        Err(error) if error.is_unsupported() => {}
        Err(error) => panic!("dependency alerts read failed: {error:?}"),
    }
    for limit in [0, MAX_LOG_TAIL_BYTES + 1] {
        for logs in [false, true] {
            assert!(matches!(
                service.readiness(url, logs, limit).await,
                Err(ServiceError::InvalidParameters)
            ));
        }
    }
    let ready = service
        .readiness(url, false, MAX_LOG_TAIL_BYTES)
        .await
        .unwrap();
    assert_eq!(ready.head_sha, head);
    assert_eq!(ready.current_head_sha, head);
    assert_eq!(ready.target, pr.target);
    assert!(ready.failed_logs.is_none());
    let read_ready = service
        .read(Resource::PullRequestReadiness, url, None, "")
        .await
        .unwrap();
    assert_eq!(read_ready.items()[0]["head_sha"], head);
    let files = service
        .read(Resource::PullRequestFiles, url, None, "")
        .await
        .unwrap();
    let readme = files
        .items()
        .iter()
        .find(|v| v["path"] == "README.md")
        .expect("README change");
    assert_eq!(readme["patch_available"], true);
    assert_eq!(readme["patch_complete"], true);
    assert!(
        readme["commentable_lines"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["side"] == "RIGHT" && v["line"] == 1)
    );
    let review = service
        .review(url, "review \n", ReviewVerdict::Comment, head)
        .await
        .unwrap();
    assert_eq!(review.commit, head);
    assert_eq!(review.verdict, ReviewVerdict::Comment);
    assert_eq!(review.target, pr.target);
    let reviews = service
        .read(Resource::PullRequestReviews, url, None, "")
        .await
        .unwrap();
    let review_id = reviews.items().last().unwrap()["id"]
        .as_str()
        .unwrap()
        .to_owned();
    assert_eq!(reviews.items().last().unwrap()["body"], "review");
    assert!(
        service
            .review(url, "stale", ReviewVerdict::Comment, "missing-head")
            .await
            .is_err()
    );
    assert_eq!(
        service
            .read(Resource::PullRequestReviews, url, None, "")
            .await
            .unwrap()
            .items()
            .len(),
        reviews.items().len()
    );
    let inline = service
        .review_comment(
            url,
            ReviewComment {
                body: "inline \n".into(),
                head: head.into(),
                path: "README.md".into(),
                line: 1,
                side: "RIGHT".into(),
                start_line: None,
                start_side: None,
            },
        )
        .await
        .unwrap();
    assert_eq!(inline.target, pr.target);
    assert_eq!(inline.path, "README.md");
    assert_eq!(inline.line, 1);
    let reply = service.reply(url, &inline.id, "reply \n").await.unwrap();
    assert_ne!(reply.id, inline.id);
    assert_eq!(reply.target, pr.target);
    let threads = service
        .read(Resource::PullRequestThreads, url, None, "")
        .await
        .unwrap();
    let thread = threads
        .items()
        .iter()
        .find(|t| {
            t["comments"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["id"] == inline.id)
        })
        .unwrap();
    assert_eq!(thread["comments"].as_array().unwrap().len(), 2);
    assert_eq!(thread["comments"][0]["body"], "inline");
    assert_eq!(thread["comments"][1]["body"], "reply");
    assert_eq!(thread["comments_complete"], true);
    let thread_comments = service
        .read(
            Resource::ReviewThreadComments,
            url,
            Some(&json!({"node":thread["id"]})),
            "",
        )
        .await
        .unwrap();
    assert_eq!(thread_comments.items().len(), 2);
    assert_eq!(thread_comments.items()[1]["reply_to_id"], inline.id);
    let written = service.comment(url, "comment \n", None).await.unwrap();
    assert_eq!(written.target, pr.target);
    let comments = service
        .read(Resource::PullRequestComments, url, None, "")
        .await
        .unwrap();
    assert_eq!(comments.items().last().unwrap()["body"], "comment");
    for id in [&written.id, &inline.id, &review_id] {
        assert!(matches!(
            service
                .add_reaction(&other.target.url, id, SemanticReaction::Agree)
                .await,
            Err(ref error) if error.is_not_found()
        ));
        assert!(matches!(
            service.reply(&other.target.url, id, "wrong PR").await,
            Err(ref error) if error.is_not_found()
        ));
        let r = service
            .add_reaction(url, id, SemanticReaction::Agree)
            .await
            .unwrap();
        assert_eq!(r.id, *id);
        assert_eq!(r.reaction, SemanticReaction::Agree);
    }
    let elsewhere = format!("outside/{repository}");
    assert!(matches!(
        service.check_push(&elsewhere),
        Err(ServiceError::OutOfScope)
    ));
    let mut out = request(head_branch);
    out.repository = elsewhere.clone();
    assert!(matches!(
        service.create_pull_request(out).await,
        Err(ServiceError::OutOfScope)
    ));
    let outside_url = url.replace(repository, &elsewhere);
    assert!(matches!(
        service.convert_pull_request_to_draft(&outside_url).await,
        Err(ServiceError::OutOfScope)
    ));
    let updated = service
        .update_pull_request(
            url,
            UpdatePullRequest {
                title: Some("updated".into()),
                body: Some("updated body".into()),
                tickets: None,
            },
        )
        .await
        .unwrap();
    assert_eq!(updated.body, "updated body");
    assert_eq!(updated.target.title, "updated");
    let unsupported = service
        .update_pull_request(
            url,
            UpdatePullRequest {
                title: Some("must not land".into()),
                tickets: Some(vec![TicketLink {
                    url: "https://foreign.invalid/tickets/1".into(),
                    closes: true,
                }]),
                ..Default::default()
            },
        )
        .await;
    assert!(matches!(unsupported, Err(ref error) if error.is_unsupported()));
    assert_eq!(
        service
            .read(Resource::PullRequests, url, None, "")
            .await
            .unwrap()
            .items()[0]["title"],
        "updated"
    );
    assert!(matches!(
        service
            .read(
                Resource::TicketPullRequests,
                "https://foreign.invalid/tickets/1",
                None,
                ""
            )
            .await,
        Err(ref error) if error.is_unsupported()
    ));
    let (a, b) = tokio::join!(
        service.convert_pull_request_to_draft(url),
        service.convert_pull_request_to_draft(url)
    );
    let mut converted = [a.unwrap(), b.unwrap()];
    converted.sort();
    assert_eq!(converted, [false, true]);
    assert!(!service.convert_pull_request_to_draft(url).await.unwrap());
    let counted = service.authored_open_pull_requests().await.unwrap();
    let detail = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert_eq!(
        counted.contains(&url.to_owned()),
        detail.items()[0]["state"] == "open"
    );
    assert_eq!(
        service
            .read(Resource::PullRequests, url, None, "")
            .await
            .unwrap()
            .items()[0]["draft"],
        true
    );
    service.pull_request_candidates().await.unwrap();
    assert!(!service.can_receive_work(url).await.unwrap());
    let task = Task {
        pull_request_url: Some(url.clone()),
        url: Some(url.clone()),
        ..Default::default()
    };
    assert!(service.refresh_pull_request(&task).await.unwrap().is_none());
    service.aclose().await.unwrap();
}

/// Run after a fixture advances the same PR's branch, preserving its old SHA.
pub async fn code_stale_head_contract(service: &dyn CodeHostingService, url: &str, old_head: &str) {
    let before = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    let head = before.items()[0]["head_sha"].as_str().unwrap();
    assert_ne!(head, old_head);
    let reviews = service
        .read(Resource::PullRequestReviews, url, None, "")
        .await
        .unwrap();
    let threads = service
        .read(Resource::PullRequestThreads, url, None, "")
        .await
        .unwrap();
    assert!(
        service
            .review(url, "stale", ReviewVerdict::Comment, old_head)
            .await
            .is_err()
    );
    assert!(
        service
            .review_comment(
                url,
                ReviewComment {
                    body: "stale".into(),
                    head: old_head.into(),
                    path: "README.md".into(),
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
        service
            .read(Resource::PullRequestReviews, url, None, "")
            .await
            .unwrap()
            .items(),
        reviews.items()
    );
    assert_eq!(
        service
            .read(Resource::PullRequestThreads, url, None, "")
            .await
            .unwrap()
            .items(),
        threads.items()
    );
    assert_eq!(
        service
            .review(url, "current", ReviewVerdict::Comment, head)
            .await
            .unwrap()
            .commit,
        head
    );
}
/// Run after a fixture removes the PR's base and adds `<base>/next`.
pub async fn code_missing_base_contract(service: &dyn CodeHostingService, url: &str) {
    assert!(
        service
            .readiness(url, false, MAX_LOG_TAIL_BYTES)
            .await
            .is_err()
    );
    assert!(
        service
            .read(Resource::PullRequestReadiness, url, None, "")
            .await
            .is_err()
    );
}
/// Run after the fixture merges or closes a PR.
pub async fn code_closed_contract(service: &dyn CodeHostingService, url: &str) {
    let p = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert_eq!(p.items()[0]["state"], "closed");
    assert!(!service.convert_pull_request_to_draft(url).await.unwrap());
    let counted = service.authored_open_pull_requests().await.unwrap();
    let detail = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert_eq!(
        counted.contains(&url.to_owned()),
        detail.items()[0]["state"] == "open"
    );
}
/// Run as a different member from the PR author. Both restored providers support these verdicts.
/// `author` opened the PR at `url`; `service` acts as another actor on it.
pub async fn code_other_actor_contract(
    author: &dyn CodeHostingService,
    service: &dyn CodeHostingService,
    url: &str,
) {
    let p = service
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert!(
        !service
            .authored_open_pull_requests()
            .await
            .unwrap()
            .contains(&url.to_owned())
    );
    let head = p.items()[0]["head_sha"].as_str().unwrap();
    let feedback = || async {
        let read = author
            .read(Resource::PullRequestReadiness, url, None, "")
            .await
            .unwrap();
        let ready = author
            .readiness(url, false, MAX_LOG_TAIL_BYTES)
            .await
            .unwrap();
        assert_eq!(read.items()[0]["feedback"], json!(ready.feedback));
        ready.feedback
    };
    let before = feedback().await;
    let own = author.comment(url, "own note", None).await.unwrap();
    assert_eq!(feedback().await, before, "the author's own comment");
    let status = WorkflowStatus::from_payload(&json!({"kind":"workflow_error","reason":"rate_limited","person_id":"aiko","run_id":"notice-run"})).unwrap();
    service.comment(url, "notice", Some(&status)).await.unwrap();
    assert_eq!(feedback().await, before, "another actor's status notice");
    let comment = service.comment(url, "a remark", None).await.unwrap();
    let after = feedback().await;
    assert_eq!(after.comments.count, before.comments.count + 1);
    assert_eq!(after.comments.latest.unwrap().id, comment.id);
    assert_ne!(comment.id, own.id);
    assert_eq!(
        (&after.reviews, &after.thread_comments),
        (&before.reviews, &before.thread_comments)
    );
    for verdict in [ReviewVerdict::Approve, ReviewVerdict::RequestChanges] {
        let r = service.review(url, "verdict", verdict, head).await.unwrap();
        assert_eq!(r.verdict, verdict);
        assert_eq!(r.commit, head);
    }
    let reviews = service
        .read(Resource::PullRequestReviews, url, None, "")
        .await
        .unwrap();
    let after = feedback().await;
    assert_eq!(after.reviews.count, before.reviews.count + 2);
    assert_eq!(
        json!(after.reviews.latest.unwrap().id),
        reviews.items().last().unwrap()["id"]
    );
    let files = service
        .read(Resource::PullRequestFiles, url, None, "")
        .await
        .unwrap();
    let (path, line) = files
        .items()
        .iter()
        .find_map(|f| Some((f["path"].clone(), f["commentable_lines"].get(0)?.clone())))
        .expect("a commentable line");
    let inline = service
        .review_comment(
            url,
            ReviewComment {
                body: "inline remark".into(),
                head: head.into(),
                path: path.as_str().unwrap().into(),
                line: line["line"].as_i64().unwrap(),
                side: line["side"].as_str().unwrap().into(),
                start_line: None,
                start_side: None,
            },
        )
        .await
        .unwrap();
    let after = feedback().await;
    assert_eq!(
        after.thread_comments.count,
        before.thread_comments.count + 1
    );
    assert_eq!(after.thread_comments.latest.as_ref().unwrap().id, inline.id);
    author.reply(url, &inline.id, "own reply").await.unwrap();
    assert_eq!(feedback().await, after, "the author's own reply");
}
/// The fixture installs the same ZIP bytes on the PR's current head.
pub async fn code_artifact_contract(
    service: &dyn CodeHostingService,
    url: &str,
    head: &str,
    bytes: &[u8],
) {
    let artifact = service
        .artifact(url, "report", bytes.len() + 1)
        .await
        .unwrap();
    assert_eq!(artifact.metadata.name, "report");
    assert_eq!(artifact.metadata.id, format!("{head}/report"));
    assert_eq!(artifact.content, bytes);
    assert!(service.artifact(url, "report", 1).await.is_err());
}
/// Run after advancing the head, leaving the old head's `report` artifact in place.
pub async fn code_old_artifact_contract(service: &dyn CodeHostingService, url: &str) {
    assert!(matches!(
        service.artifact(url, "report", 1024 * 1024).await,
        Err(ref error) if error.is_not_found()
    ));
}
/// The fixture includes a changed UTF-8 filename and UTF-8 patch text.
pub async fn code_utf8_files_contract(
    service: &dyn CodeHostingService,
    url: &str,
    path: &str,
    text: &str,
) {
    let p = service
        .read(Resource::PullRequestFiles, url, None, "")
        .await
        .unwrap();
    let file = p
        .items()
        .iter()
        .find(|v| v["path"] == path)
        .expect("UTF-8 path preserved");
    assert!(file["patch"].as_str().unwrap().contains(text));
    assert_eq!(file["patch_available"], true);
    assert_eq!(file["patch_complete"], true);
}

/// Wrong item kinds never cross the code-host boundary or cause writes.
pub async fn code_kind_boundary_contract(
    code: &dyn CodeHostingService,
    tickets: &dyn TicketService,
    tracker: &str,
    pr_url: &str,
) {
    let ticket = tickets
        .create_ticket(CreateTicket {
            tracker: tracker.into(),
            title: "kind boundary".into(),
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
        .url;
    assert!(matches!(
        code.comment(&ticket, "wrong kind", None).await,
        Err(ref error) if error.is_not_found()
    ));
    assert!(matches!(
        code.review(&ticket, "wrong kind", ReviewVerdict::Comment, "head")
            .await,
        Err(ref error) if error.is_not_found()
    ));
    assert!(matches!(
        code.readiness(&ticket, false, MAX_LOG_TAIL_BYTES).await,
        Err(ref error) if error.is_not_found()
    ));
    assert!(matches!(
        code.pull_request_head(&ticket).await,
        Err(ref error) if error.is_not_found()
    ));
    for resource in Resource::ALL {
        if resource.is_ticket_service() {
            assert!(code.read(*resource, &ticket, None, "").await.is_err());
        } else if matches!(
            resource,
            Resource::PullRequests
                | Resource::PullRequestComments
                | Resource::PullRequestReviews
                | Resource::PullRequestFiles
                | Resource::PullRequestReadiness
        ) {
            assert!(matches!(
                code.read(*resource, &ticket, None, "").await,
                Err(ref error) if error.is_not_found()
            ));
        }
    }
    assert!(matches!(
        code.read(Resource::TicketPullRequests, pr_url, None, "")
            .await,
        Err(ref error) if error.is_not_found()
    ));
    let before = code
        .read(Resource::PullRequests, pr_url, None, "")
        .await
        .unwrap();
    let (prefix, key) = ticket.rsplit_once('/').unwrap();
    let missing = format!(
        "{prefix}/{}999999999",
        if key.starts_with("T-") { "T-" } else { "" }
    );
    assert!(matches!(
        code.update_pull_request(
            pr_url,
            UpdatePullRequest {
                title: Some("must not land".into()),
                tickets: Some(vec![TicketLink {
                    url: missing,
                    closes: false
                }]),
                ..Default::default()
            }
        )
        .await,
        Err(ref error) if error.is_not_found()
    ));
    for closes in [false, true] {
        let p = &before.items()[0];
        assert!(matches!(
            code.create_pull_request(CreatePullRequest {
                repository: p["repository"].as_str().unwrap().into(),
                head: format!("{}-wrong-kind-{closes}", p["head"].as_str().unwrap()),
                base: p["base"].as_str().unwrap().into(),
                title: "must not land".into(),
                body: String::new(),
                draft: false,
                tickets: vec![TicketLink {
                    url: pr_url.into(),
                    closes
                }]
            })
            .await,
            Err(ref error) if error.is_not_found()
        ));
        assert!(matches!(
            code.update_pull_request(
                pr_url,
                UpdatePullRequest {
                    body: Some("must not land".into()),
                    tickets: Some(vec![TicketLink {
                        url: pr_url.into(),
                        closes
                    }]),
                    ..Default::default()
                }
            )
            .await,
            Err(ref error) if error.is_not_found()
        ));
    }
    assert_eq!(
        code.read(Resource::PullRequests, pr_url, None, "")
            .await
            .unwrap()
            .items(),
        before.items()
    );
}

pub struct CodeLinkCase {
    pub url: String,
    pub states: Vec<(String, String)>,
}
/// Fixtures prepare aliases `<head>-links-0` through `<head>-links-7`, then
/// merge each returned PR and pass it to `code_link_case_finished`.
pub async fn code_link_population(
    code: &dyn CodeHostingService,
    tickets: &dyn TicketService,
    repository: &str,
    tracker: &str,
    head: &str,
    base: &str,
) -> Vec<CodeLinkCase> {
    async fn ticket(t: &dyn TicketService, tracker: &str, title: &str) -> String {
        t.create_ticket(CreateTicket {
            tracker: tracker.into(),
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
    let mut cases = vec![];
    for i in 0..7 {
        let first = ticket(tickets, tracker, &format!("first {i}")).await;
        let second = ticket(tickets, tracker, &format!("second {i}")).await;
        let unlinked = ticket(tickets, tracker, &format!("unlinked {i}")).await;
        let body = format!("Context parent {second}");
        let initial = if i == 0 {
            vec![
                TicketLink {
                    url: first.clone(),
                    closes: true,
                },
                TicketLink {
                    url: second.clone(),
                    closes: true,
                },
            ]
        } else {
            vec![
                TicketLink {
                    url: first.clone(),
                    closes: i != 2 && i != 3,
                },
                TicketLink {
                    url: "https://foreign.invalid/tickets/1".into(),
                    closes: false,
                },
            ]
        };
        let pr = code
            .create_pull_request(CreatePullRequest {
                repository: repository.into(),
                head: format!("{head}-links-{i}"),
                base: base.into(),
                title: format!("links {i}"),
                body: body.clone(),
                draft: false,
                tickets: initial.clone(),
            })
            .await
            .unwrap();
        assert_eq!(code.linked_tickets(&pr.target.url).await.unwrap(), initial);
        assert!(
            code.authored_open_pull_requests()
                .await
                .unwrap()
                .contains(&pr.target.url)
        );
        for ancestor in [false, true] {
            tickets
                .update_ticket(
                    &second,
                    UpdateTicket {
                        interactive: Some(true),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            tickets
                .update_ticket(
                    &first,
                    UpdateTicket {
                        interactive: Some(!ancestor),
                        parent: ancestor.then(|| second.clone()),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            assert!(
                !code
                    .authored_open_pull_requests()
                    .await
                    .unwrap()
                    .contains(&pr.target.url)
            );
            tickets
                .update_ticket(
                    &first,
                    UpdateTicket {
                        interactive: Some(false),
                        parent: Some(String::new()),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            tickets
                .update_ticket(
                    &second,
                    UpdateTicket {
                        interactive: Some(false),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            assert!(
                code.authored_open_pull_requests()
                    .await
                    .unwrap()
                    .contains(&pr.target.url)
            );
        }
        let mut first_state = "closed";
        let mut second_state = if i == 0 { "closed" } else { "open" };
        match i {
            2 | 3 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![TicketLink {
                            url: first.clone(),
                            closes: true,
                        }]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            }
            4 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![TicketLink {
                            url: first.clone(),
                            closes: false,
                        }]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
                first_state = "open";
            }
            5 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
                first_state = "open";
            }
            6 => {
                code.update_pull_request(
                    &pr.target.url,
                    UpdatePullRequest {
                        tickets: Some(vec![TicketLink {
                            url: second.clone(),
                            closes: true,
                        }]),
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
                first_state = "open";
                second_state = "closed";
            }
            _ => {}
        }
        code.update_pull_request(
            &pr.target.url,
            UpdatePullRequest {
                body: Some(format!("{body}\nRefs {first}\nCloses {unlinked}")),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert!(
            code.read(Resource::TicketPullRequests, &unlinked, None, "")
                .await
                .unwrap()
                .items()
                .is_empty()
        );
        let refs = code
            .read(Resource::TicketPullRequests, &first, None, "")
            .await
            .unwrap();
        if i >= 5 {
            assert!(refs.items().is_empty());
        } else {
            assert!(refs.items().iter().any(|p| p["url"] == pr.target.url));
        }
        let before = code
            .read(Resource::PullRequests, &pr.target.url, None, "")
            .await
            .unwrap();
        assert!(before.items()[0]["body"].as_str().unwrap().contains(&body));
        assert!(matches!(
            code.update_pull_request(
                &pr.target.url,
                UpdatePullRequest {
                    title: Some("must not land".into()),
                    body: Some("must not land".into()),
                    tickets: Some(vec![
                        TicketLink {
                            url: first.clone(),
                            closes: true
                        },
                        TicketLink {
                            url: "https://foreign.invalid/tickets/1".into(),
                            closes: true
                        }
                    ])
                }
            )
            .await,
            Err(ref error) if error.is_unsupported()
        ));
        assert_eq!(
            code.read(Resource::PullRequests, &pr.target.url, None, "")
                .await
                .unwrap()
                .items(),
            before.items()
        );
        if i == 0 {
            let reference = code
                .create_pull_request(CreatePullRequest {
                    repository: repository.into(),
                    head: format!("{head}-links-7"),
                    base: base.into(),
                    title: "references".into(),
                    body: String::new(),
                    draft: false,
                    tickets: vec![
                        TicketLink {
                            url: first.clone(),
                            closes: false,
                        },
                        TicketLink {
                            url: unlinked.clone(),
                            closes: false,
                        },
                    ],
                })
                .await
                .unwrap();
            let refs = code
                .read(Resource::TicketPullRequests, &first, None, "")
                .await
                .unwrap();
            assert!(refs.items().iter().any(|v| v["url"] == pr.target.url));
            assert!(
                refs.items()
                    .iter()
                    .any(|v| v["url"] == reference.target.url)
            );
            cases.push(CodeLinkCase {
                url: reference.target.url,
                states: vec![
                    (first.clone(), "open".into()),
                    (second.clone(), "open".into()),
                    (unlinked.clone(), "open".into()),
                ],
            });
        }
        cases.push(CodeLinkCase {
            url: pr.target.url,
            states: vec![
                (first, first_state.into()),
                (second, second_state.into()),
                (unlinked, "open".into()),
            ],
        });
    }
    cases
}
pub async fn code_link_case_finished(tickets: &dyn TicketService, case: &CodeLinkCase) {
    for (url, state) in &case.states {
        assert_eq!(
            tickets
                .read(Resource::Tickets, url, None, "")
                .await
                .unwrap()
                .items()[0]["state"],
            *state,
            "{url}"
        );
    }
}

/// A same-service ticket of another repository can be referenced, but cannot
/// be closed by this PR. Failed replacement changes neither title nor body.
pub async fn code_cross_repo_link_contract(
    code: &dyn CodeHostingService,
    url: &str,
    other_ticket: &str,
) {
    let before = code
        .read(Resource::PullRequests, url, None, "")
        .await
        .unwrap();
    assert!(matches!(
        code.update_pull_request(
            url,
            UpdatePullRequest {
                title: Some("must not land".into()),
                body: Some("must not land".into()),
                tickets: Some(vec![TicketLink {
                    url: other_ticket.into(),
                    closes: true
                }])
            }
        )
        .await,
        Err(ref error) if error.is_unsupported()
    ));
    assert_eq!(
        code.read(Resource::PullRequests, url, None, "")
            .await
            .unwrap()
            .items(),
        before.items()
    );
    code.update_pull_request(
        url,
        UpdatePullRequest {
            tickets: Some(vec![TicketLink {
                url: other_ticket.into(),
                closes: false,
            }]),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert!(
        code.read(Resource::TicketPullRequests, other_ticket, None, "")
            .await
            .unwrap()
            .items()
            .iter()
            .any(|p| p["url"] == url)
    );
    code.update_pull_request(
        url,
        UpdatePullRequest {
            tickets: Some(vec![]),
            ..Default::default()
        },
    )
    .await
    .unwrap();
}

/// Branvil lists newest first across signed pages; Local preserves its frozen
/// ascending store order. Both traverse more than one provider page fully.
pub async fn code_open_order_contract(
    code: &dyn CodeHostingService,
    repository: &str,
    head: &str,
    bases: &[String],
    newest_first: bool,
) {
    assert!(
        bases.len() > 100,
        "fixture must cross Branvil's page boundary"
    );
    let mut created = vec![];
    for base in bases {
        let result = code
            .create_pull_request(CreatePullRequest {
                repository: repository.into(),
                head: head.into(),
                base: base.clone(),
                title: format!("page {base}"),
                body: String::new(),
                draft: false,
                tickets: vec![],
            })
            .await
            .unwrap();
        assert!(result.created);
        created.push(result.target.url);
    }
    let counted = code.authored_open_pull_requests().await.unwrap();
    assert!(created.iter().all(|url| counted.contains(url)));
    if newest_first {
        created.reverse();
    }
    assert_eq!(
        code.open_pull_requests(repository, head).await.unwrap(),
        created
    );
    assert!(
        code.open_pull_requests(repository, "contract-absent-head")
            .await
            .unwrap()
            .is_empty()
    );
}
