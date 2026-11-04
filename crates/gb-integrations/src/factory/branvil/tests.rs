use super::*;
use branvil::api::{Running, initialize};
#[tokio::test]
async fn member_identity_scope_and_resource_boundaries() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let token = std::fs::read_to_string(temp.path().join("secrets/owner.token")).unwrap();
    let running = Running::start(temp.path()).await.unwrap();
    let m = running.data.authenticate(token.clone()).await.unwrap();
    m.repos_create_workspace(decode(json!({"name":"test"})).unwrap())
        .await
        .unwrap();
    m.repos_create(decode(json!({"path":"test/repo","labels":["bug"]})).unwrap())
        .await
        .unwrap();
    let events = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let observed = events.clone();
    let observer: IntegrationObserver =
        std::sync::Arc::new(move |event| observed.lock().unwrap().push(event));
    let service = BranvilService::new(
        running.data.clone(),
        token.clone(),
        "owner1".into(),
        "test".into(),
        "owner_id".into(),
        observer.clone(),
    );
    for limit in [0, MAX_LOG_TAIL_BYTES + 1] {
        for logs in [false, true] {
            assert!(matches!(
                service.readiness("not-a-url", logs, limit).await,
                Err(ServiceError::InvalidParameters)
            ));
        }
    }
    assert_eq!(
        service.commit_url("test/repo", "sha"),
        format!("{}/test/repo/-/commits/sha", running.data.base_url())
    );
    assert_eq!(
        service.repository_from_remote(&format!(
            "  {}/git/test/repo.git  ",
            running.data.base_url()
        )),
        Some("test/repo".into())
    );
    assert_eq!(
        service.repository_from_remote(&format!("{}/git/test/repo", running.data.base_url())),
        Some("test/repo".into())
    );
    assert_eq!(
        service.repository_from_remote("https://foreign.invalid/git/test/repo.git"),
        None
    );
    let request = || CreateTicket {
        tracker: "test/repo".into(),
        title: "ticket".into(),
        body: "body".into(),
        labels: vec!["bug".into()],
        add_to_board: true,
        interactive: false,
        assignee: None,
        lane: None,
        parent: None,
        blocked_by: vec![],
    };
    let t = service.create_ticket(request()).await.unwrap();
    assert_eq!(t.target.label, "#1");
    let read = TicketService::read(&service, Resource::Tickets, &t.target.url, None, "")
        .await
        .unwrap();
    assert_eq!(read.items()[0]["tracker"], "test/repo");
    assert_eq!(read.items()[0]["assignees"], json!([]));
    let c = TicketService::comment(&service, &t.target.url, "comment \n", None)
        .await
        .unwrap();
    let read = TicketService::read(&service, Resource::TicketComments, &t.target.url, None, "")
        .await
        .unwrap();
    assert_eq!(read.items()[0]["body"], "comment");
    assert_eq!(read.items()[0]["author_type"], "Assistant");
    TicketService::add_reaction(&service, &t.target.url, &c.id, SemanticReaction::Ack)
        .await
        .unwrap();
    assert!(
        TicketService::read(
            &service,
            Resource::Tickets,
            &t.target.url.replace("/tickets/", "/pulls/"),
            None,
            ""
        )
        .await
        .is_err()
    );
    assert!(
        TicketService::read(&service, Resource::PullRequests, &t.target.url, None, "")
            .await
            .is_err()
    );
    let wrong = BranvilService::new(
        running.data.clone(),
        token.clone(),
        "other".into(),
        "test".into(),
        "other".into(),
        observer.clone(),
    );
    assert!(wrong.create_ticket(request()).await.is_err());
    let outside = BranvilService::new(
        running.data.clone(),
        token,
        "owner1".into(),
        "other".into(),
        "other".into(),
        observer.clone(),
    );
    assert_eq!(
        outside.create_ticket(request()).await.unwrap_err(),
        ServiceError::OutOfScope
    );
    let outside_read = TicketService::read(&outside, Resource::Tickets, &t.target.url, None, "")
        .await
        .unwrap();
    assert_eq!(outside_read.items()[0]["lane"], Value::Null);
    service.create_ticket(request()).await.unwrap();
    let first = TicketService::read(
        &service,
        Resource::TrackerTickets,
        "test/repo",
        Some(&json!({"page_size":1})),
        "",
    )
    .await
    .unwrap();
    let cursor = first.continuation().unwrap();
    let second = TicketService::read(
        &service,
        Resource::TrackerTickets,
        "test/repo",
        Some(&json!({"page_size":1})),
        cursor,
    )
    .await
    .unwrap();
    assert_ne!(first.items()[0]["key"], second.items()[0]["key"]);
    assert!(
        TicketService::read(
            &service,
            Resource::TrackerTickets,
            "test/repo",
            Some(&json!({"page_size":1,"label":"bug"})),
            cursor
        )
        .await
        .is_err()
    );
    let invalid = BranvilService::new(
        running.data.clone(),
        "invalid-secret".into(),
        "owner1".into(),
        "test".into(),
        "agent-one".into(),
        observer.clone(),
    );
    assert!(invalid.member().await.is_err());
    let missing = BranvilService::new(
        running.data.clone(),
        String::new(),
        "owner1".into(),
        "test".into(),
        "agent-one".into(),
        observer,
    );
    assert_eq!(
        missing.member().await.err(),
        Some(ServiceError::MissingCredential {
            name: "AGENT_ONE_BRANVIL_TOKEN".into()
        })
    );
    assert_eq!(
        *events.lock().unwrap(),
        vec![
            IntegrationEvent::CredentialFailed {
                provider: "branvil",
                code: "identity_mismatch"
            },
            IntegrationEvent::CredentialFailed {
                provider: "branvil",
                code: "unauthorized"
            }
        ]
    );
    running.stop().await.unwrap();
}

#[test]
fn mentions_match_the_frozen_ascii_name_boundaries() {
    for text in [
        "@agent.one",
        "hello @AGENT.ONE.",
        "(@agent.one)",
        "@agent.one: please",
    ] {
        assert!(mentions(text, "agent.one"), "{text}");
    }
    for text in [
        "email@agent.one",
        "a.@agent.one",
        "@@agent.one",
        "-@agent.one",
        "@agent.one.two",
        "@agent.one-extra",
        "@agent.one_other",
    ] {
        assert!(!mentions(text, "agent.one"), "{text}");
    }
}
