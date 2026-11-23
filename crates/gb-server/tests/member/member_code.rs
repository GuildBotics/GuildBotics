use branvil::agent::port::*;
use branvil::{
    agent::{CodeAgent, TicketAgent},
    test_support::Fixture,
};
use serde_json::json;
pub async fn member_show_contract(service: &CodeAgent, repository: &str) {
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
    let code = &CodeAgent::new(f.data(), "acme".into(), "aiko".into(), "Aiko".into()) as &CodeAgent;
    let tickets =
        &branvil::agent::TicketAgent::new(f.data(), "acme".into(), "aiko".into(), "Aiko".into())
            as &TicketAgent;
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
        .read(Resource::PullRequests, &pr.written.target.url, None, "")
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
            &pr.written.target.url,
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
            gb_server::member::repository::author_ticket(code, tickets, &pr.written.target.url)
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
        gb_server::member::repository::author_ticket(code, tickets, &pr.written.target.url)
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
        interactive: false,
        assignee: None,
        lane: None,
        parent: None,
        blocked_by: vec![],
    }
}

#[tokio::test]
async fn agents_supply_roster_names_to_task_comments() {
    let fixture = Fixture::new("owner1").await;
    let owner = &fixture.owner;
    owner
        .repos_create_workspace(serde_json::from_value(json!({"name":"acme"})).unwrap())
        .await
        .unwrap();
    owner
        .repos_create(serde_json::from_value(json!({"path":"acme/repo"})).unwrap())
        .await
        .unwrap();
    fixture.member("aiko").await;
    let ticket = owner
        .tickets_create_ticket(
            "acme/repo",
            serde_json::from_value(
                json!({"title":"task","body":"please","lane":"ready","assignee":"aiko"}),
            )
            .unwrap(),
        )
        .await
        .unwrap();
    owner
        .tickets_add_comment(
            "acme/repo",
            &ticket.key,
            serde_json::from_value(json!({"body":"please"})).unwrap(),
        )
        .await
        .unwrap();
    let member = |person_id: &str, name: &str, account: &str| {
        serde_json::from_value::<gb_member::context::PublicMember>(json!({
            "person_id":person_id,"name":name,"person_type":"agent","is_active":true,
            "roles":{},"profile":{},"speaking_style":"","accounts":{"branvil":account}
        }))
        .unwrap()
    };
    let aiko = member("aiko", "Aiko", "aiko");
    // Deliberately vary case and include account whitespace as in saved identities.
    let roster = [aiko.clone(), member("ototadana", "Otota", " OWNER1 ")];
    let project = json!({"services":{"ticket_service":{"name":"branvil"}}});
    let tasks = gb_server::member::Agents::new(
        Some(&fixture.data()),
        &project,
        Some("acme"),
        &aiko,
        &roster,
    )
    .tickets()
    .unwrap()
    .candidates()
    .await
    .unwrap();
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].comments[0].author, "Otota");
    assert_eq!(tasks[0].comments[0].author_type, "User");
    fixture.stop().await;
}
