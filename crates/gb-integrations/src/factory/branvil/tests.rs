use super::*;
use branvil::api::{Running, initialize};
#[tokio::test]
async fn member_identity_scope_and_resource_boundaries() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(::branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    let m = running.data.owner();
    m.repos_create_workspace(decode(json!({"name":"test"})).unwrap())
        .await
        .unwrap();
    m.repos_create(decode(json!({"path":"test/repo","labels":["bug"]})).unwrap())
        .await
        .unwrap();
    let service = BranvilService::new(
        running.data.clone(),
        "test".into(),
        "agent1".into(),
        "Agent One".into(),
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
    assert_eq!(
        service.clone_url("invalid").await.unwrap_err(),
        diagnostic(
            "integrations.branvil.api.not_path",
            [("path", "invalid".into())]
        )
    );
    let foreign = "https://tracker.invalid/T-1";
    assert_eq!(
        CodeHostingService::read(&service, Resource::TicketPullRequests, foreign, None, "")
            .await
            .unwrap_err(),
        diagnostic(
            "integrations.branvil.api.foreign_ticket",
            [("url", foreign.into())]
        )
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
    let mut off_board = request();
    off_board.add_to_board = false;
    assert_eq!(
        service.create_ticket(off_board).await.unwrap_err(),
        diagnostic("integrations.branvil.api.every_on_board", [])
    );
    let mut undefined = request();
    undefined.labels = vec!["undefined".into()];
    let label_error = service.create_ticket(undefined).await.unwrap_err();
    assert_eq!(
        label_error.translation_key(),
        "integrations.branvil.api.label_undefined"
    );
    assert!(label_error.translation_args()["message"].contains("undefined"));
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
    // The owner is not an agent, so a member named like it cannot act.
    let owner_named = BranvilService::new(
        running.data.clone(),
        "test".into(),
        "owner1".into(),
        "Owner".into(),
    );
    assert_eq!(
        owner_named
            .create_ticket(request())
            .await
            .unwrap_err()
            .translation_key(),
        "integrations.branvil.api.refused"
    );
    let outside = BranvilService::new(
        running.data.clone(),
        "other".into(),
        "agent1".into(),
        "Agent One".into(),
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

#[tokio::test]
async fn ticket_task_comments_use_roster_names_and_role_fallbacks() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(::branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    let mut service = BranvilService::new(
        running.data.clone(),
        "test".into(),
        "aiko-service".into(),
        "Aiko".into(),
    );
    for key in ["1", "9999999999"] {
        assert!(
            service
                .item(
                    &format!("{}/test/repo/-/pulls/{key}", running.data.base_url()),
                    "pulls"
                )
                .is_ok()
        );
    }
    for key in ["0", "01", "+1", "10000000000", "-1", "１"] {
        assert!(
            service
                .item(
                    &format!("{}/test/repo/-/pulls/{key}", running.data.base_url()),
                    "pulls"
                )
                .is_err(),
            "{key}"
        );
    }
    // Supply the roster at the same boundary as the integration factory.
    service.members = [
        ("aiko-service".into(), "Aiko".into()),
        ("otota".into(), "Otota".into()),
        ("strasse".into(), "Street".into()),
        ("nameless".into(), String::new()),
    ]
    .into();
    let card = json!({"repository":"test/repo","state":"open","lane":"ready","assignee":{"name":"aiko-service"},"interactive_ancestor":null,"comments":[
        {"author":"AIKO-SERVICE","body":"started"},
        {"author":"OTOTA","body":"continue"},
        {"author":"external","body":"hello"},
        {"author":"Straße","body":"hello"},
        {"author":"nameless","body":"hello"},
        {"author":"","body":"anonymous"}
    ]});
    let task = service.select_card(&card, false).await.unwrap().unwrap();
    assert_eq!(
        task.comments
            .iter()
            .map(|c| (c.author.as_str(), c.author_type.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("Aiko", "Assistant"),
            ("Otota", "User"),
            ("User", "User"),
            ("Street", "User"),
            ("User", "User"),
            ("", "")
        ]
    );
    service.person_id = "Straße".into();
    assert_eq!(service.author_type("STRASSE"), "Assistant");
    service.person_id.clear();
    assert_eq!(service.author_type(""), "");
    // Resource reads continue to expose the code host login.
    assert_eq!(
        service.comment_value(json!({"author":"OTOTA"}))["author"],
        "OTOTA"
    );
    running.stop().await.unwrap();
}

#[tokio::test]
async fn interactive_links_are_read_only_after_pull_patrol_selects_work() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(::branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    let service = BranvilService::new(
        running.data.clone(),
        "test".into(),
        "aiko".into(),
        "Aiko".into(),
    );
    let mut pull = json!({"url":format!("{}/test/repo/-/pulls/1", running.data.base_url()),"repository":"test/repo","author":"aiko","head_sha":"head","state":"open","tickets":[{"url":format!("{}/test/repo/-/tickets/999",running.data.base_url())}]});
    for patch in [
        json!({}),
        json!({"draft":true}),
        json!({"state":"closed"}),
        json!({"head_sha":""}),
        json!({"repository":"outside/repo"}),
    ] {
        let mut inactive = pull.clone();
        inactive
            .as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        assert!(
            service
                .pull_work(&inactive, &json!({}))
                .await
                .unwrap()
                .is_none()
        );
    }
    let activity = json!({"comments":[{"author":"human","created_at":"2026-01-01T00:00:00Z"}]});
    assert!(matches!(
        service.pull_work(&pull, &activity).await,
        Err(ref error) if error.is_not_found()
    ));
    pull["author"] = json!("human");
    pull["reviewers"] = json!(["aiko"]);
    assert!(
        service
            .pull_work(&pull, &json!({}))
            .await
            .unwrap()
            .is_some()
    );
    pull["author"] = json!("aiko");
    let owner = running.data.owner();
    owner
        .repos_create_workspace(decode(json!({"name":"test"})).unwrap())
        .await
        .unwrap();
    owner
        .repos_create(decode(json!({"path":"test/repo"})).unwrap())
        .await
        .unwrap();
    let ticket = owner
        .tickets_create_ticket(
            "test/repo",
            decode(json!({"title":"interactive","body":"","interactive":true})).unwrap(),
        )
        .await
        .unwrap();
    pull["tickets"] = json!([{"url":ticket.url}]);
    assert!(service.pull_work(&pull, &activity).await.unwrap().is_none());
    running.stop().await.unwrap();
}

#[test]
fn pull_numbers_have_the_frozen_canonical_decimal_shape() {
    for good in ["1", "9999999999"] {
        assert!(number(good).is_ok());
    }
    for bad in ["", "0", "01", "+1", "10000000000", "-1", "１"] {
        assert!(number(bad).is_err(), "{bad}");
    }
}

async fn token_labels(owner: &MemberApi, name: &str) -> Vec<String> {
    let page = owner.page("labels".into(), Some(100), None).unwrap();
    let tokens = owner.members_list_tokens(name.into(), page).await.unwrap();
    tokens.items.into_iter().map(|t| t.label).collect()
}

#[tokio::test]
async fn git_credential_lends_one_agent_token_until_the_service_is_closed() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(::branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    let data = &running.data;
    let owner = data.owner();
    owner
        .repos_create_workspace(decode(json!({"name":"test"})).unwrap())
        .await
        .unwrap();
    owner
        .repos_create(decode(json!({"path":"test/repo"})).unwrap())
        .await
        .unwrap();
    let service = BranvilService::new(data.clone(), "test".into(), "aiko".into(), "Aiko".into());
    let lent = service.git_credential().await.unwrap().unwrap();
    let again = service.git_credential().await.unwrap().unwrap();
    assert_eq!(lent.username, "aiko");
    assert_eq!(again.password, lent.password);
    assert_eq!(token_labels(&owner, "aiko").await, ["guildbotics-git"]);
    // The token authenticates the agent over HTTP and over Git.
    let member = || ::branvil::test_support::token_member(data, &lent.password);
    assert_eq!(member().await.as_deref(), Some("aiko"));
    let refs = format!(
        "{}/git/test/repo.git/info/refs?service=git-upload-pack",
        data.base_url()
    );
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    let git = |password: &str| {
        client
            .get(&refs)
            .basic_auth(&lent.username, Some(password))
            .send()
    };
    assert!(git(&lent.password).await.unwrap().status().is_success());
    assert_eq!(git("wrong").await.unwrap().status().as_u16(), 401);
    CodeHostingService::aclose(&service).await.unwrap();
    assert!(token_labels(&owner, "aiko").await.is_empty());
    assert_eq!(member().await, None);
    // A second close, after the token is gone, still succeeds.
    CodeHostingService::aclose(&service).await.unwrap();
    // A service used again after its close lends a new token, not the revoked one.
    let renewed = service.git_credential().await.unwrap().unwrap();
    assert_ne!(renewed.password, lent.password);
    assert!(git(&renewed.password).await.unwrap().status().is_success());
    CodeHostingService::aclose(&service).await.unwrap();
    assert!(token_labels(&owner, "aiko").await.is_empty());
    // A close that cannot revoke keeps the run's outcome; the token is left
    // to the revocation at the next start.
    service.git_credential().await.unwrap().unwrap();
    assert_eq!(data.revoke_labelled("unrelated").await.unwrap(), 0);
    let left = data.clone();
    running.stop().await.unwrap();
    CodeHostingService::aclose(&service).await.unwrap();
    assert!(
        left.revoke_labelled(crate::factory::GIT_TOKEN_LABEL)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn a_roster_member_that_never_acted_can_be_assigned() {
    let temp = tempfile::tempdir().unwrap();
    initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(::branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    let data = &running.data;
    let owner = data.owner();
    owner
        .repos_create_workspace(decode(json!({"name":"test"})).unwrap())
        .await
        .unwrap();
    owner
        .repos_create(decode(json!({"path":"test/repo"})).unwrap())
        .await
        .unwrap();
    // The roster names the owner (the human), the acting agent, an agent
    // that has never acted, and a name no member may have.
    let roster = [
        ("owner1", "Owner"),
        ("aiko", "Aiko"),
        ("kenji", "Kenji"),
        ("branvil", "Reserved"),
    ]
    .map(|(login, name)| (login.to_owned(), name.to_owned()))
    .into();
    let service = BranvilService::new(data.clone(), "test".into(), "aiko".into(), "Aiko".into())
        .with_members(&roster);
    let created = service
        .create_ticket(CreateTicket {
            tracker: "test/repo".into(),
            title: "for kenji".into(),
            body: String::new(),
            labels: vec![],
            add_to_board: true,
            interactive: false,
            assignee: Some(ActorName("kenji".into())),
            lane: None,
            parent: None,
            blocked_by: vec![],
        })
        .await
        .unwrap();
    let ticket = service.ticket(&created.target.url).await.unwrap();
    assert_eq!(ticket["assignee"]["name"], "kenji");
    let kenji = owner.members_get("kenji".into()).await.unwrap();
    assert_eq!(
        (kenji.kind, kenji.display_name.as_str()),
        (::branvil::api::MemberKind::Agent, "Kenji")
    );
    assert_eq!(
        owner.members_get("owner1".into()).await.unwrap().kind,
        ::branvil::api::MemberKind::Owner
    );
    assert_eq!(
        owner
            .members_get("branvil".into())
            .await
            .err()
            .unwrap()
            .code,
        "not_found"
    );
    running.stop().await.unwrap();
}
