use branvil::{agent::CodeAgent, test_support::Fixture};
use branvil::agent::port::*;
use serde_json::json;
pub async fn member_show_contract(service: &dyn CodeHostingService, repository: &str) {
    let temp = tempfile::tempdir().unwrap();
    let branch = service.default_branch(repository).await.unwrap();
    let result = gb_server::member::git::show(
        temp.path(),
        service,
        repository,
        &[".".into(), "does-not-exist.fixture".into()],
        std::time::Instant::now() + std::time::Duration::from_secs(30),
    )
    .await
    .unwrap();
    assert_eq!(result["default_branch"], branch);
    assert_eq!(result["items"][0]["kind"], "directory");
    assert!(
        result["items"][0]["entries"]
            .as_array()
            .is_some_and(|v| !v.is_empty())
    );
    assert_eq!(
        result["items"][1],
        json!({"path":"does-not-exist.fixture","kind":"missing"})
    );
    assert!(!temp.path().join("clones").exists());
}
#[tokio::test]
async fn member_show_reads_the_embedded_repository() {
    let f = Fixture::new("owner").await;
    f.workspace("acme").await;
    f.repository("acme/demo").await;
    f.commit("acme/demo", "main", None, "readme.md", "base");
    let code = CodeAgent::new(f.data(), "acme".into(), "aiko".into(), "Aiko".into());
    member_show_contract(&code, "acme/demo").await;
    f.stop().await;
}

#[tokio::test]
async fn author_ticket_requires_exactly_one_open_assigned_link() {
    let f = Fixture::new("owner").await;
    f.workspace("acme").await;
    f.repository("acme/demo").await;
    let base = f.commit("acme/demo", "main", None, "a", "base");
    f.commit("acme/demo", "feature", Some(&base), "a", "feature");
    let code = &CodeAgent::new(f.data(), "acme".into(), "aiko".into(), "Aiko".into())
        as &dyn CodeHostingService;
    let tickets =
        &branvil::agent::TicketAgent::new(f.data(), "acme".into(), "aiko".into(), "Aiko".into())
            as &dyn TicketService;
    let tracker = "acme/demo";
    let pr = code
        .create_pull_request(CreatePullRequest {
            repository: tracker.into(),
            head: "feature".into(),
            base: "main".into(),
            title: "Work".into(),
            body: String::new(),
            draft: false,
            tickets: vec![],
        })
        .await
        .unwrap();
    let original = code
        .read(Resource::PullRequests, &pr.target.url, None, "")
        .await
        .unwrap();
    let author: ActorName = serde_json::from_value(original.items()[0]["author"].clone()).unwrap();
    let mut request = ticket_request(tracker, "owned work");
    request.assignee = Some(author.clone());
    let one = tickets.create_ticket(request).await.unwrap().target.url;
    let mut request = ticket_request(tracker, "second owned work");
    request.assignee = Some(author);
    let two = tickets.create_ticket(request).await.unwrap().target.url;
    for links in [vec![], vec![one.clone()], vec![one.clone(), two.clone()]] {
        code.update_pull_request(
            &pr.target.url,
            UpdatePullRequest {
                tickets: Some(
                    links
                        .iter()
                        .map(|u| TicketLink {
                            url: u.clone(),
                            closes: false,
                        })
                        .collect(),
                ),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert_eq!(
            gb_server::member::repository::author_ticket(code, tickets, &pr.target.url)
                .await
                .unwrap(),
            if links.len() == 1 {
                Some(one.clone())
            } else {
                None
            }
        );
    }
    tickets
        .update_ticket(
            &two,
            UpdateTicket {
                state: Some("closed".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(
        gb_server::member::repository::author_ticket(code, tickets, &pr.target.url)
            .await
            .unwrap(),
        Some(one)
    );
    f.stop().await;
}
fn ticket_request(tracker: &str, title: &str) -> CreateTicket {
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
