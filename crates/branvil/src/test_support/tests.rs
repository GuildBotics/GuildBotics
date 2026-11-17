use super::*;

#[tokio::test]
async fn preparing_one_heads_result_keeps_other_heads_pending() {
    let f = Fixture::new("owner").await;
    f.workspace("fixture").await;
    f.repository("fixture/repo").await;
    let agent = f.member("agent").await;
    let base = f.commit("fixture/repo", "main", None, "README.md", "base");
    for (branch, conclusion) in [("pending", None), ("success", Some("success"))] {
        let head = f.commit("fixture/repo", branch, Some(&base), branch, branch);
        f.pull(
            &agent,
            "fixture/repo",
            json!({"head":branch,"title":branch}),
        )
        .await;
        f.check("fixture/repo", branch, &head, conclusion).await;
    }
    let pending = f.owner.pulls_checks("fixture/repo", 1).await.unwrap();
    let success = f.owner.pulls_checks("fixture/repo", 2).await.unwrap();
    assert_eq!(pending.checks[0].status, "queued");
    assert_eq!(pending.checks[0].conclusion, None);
    assert_eq!(success.checks[0].status, "completed");
    assert_eq!(success.checks[0].conclusion.as_deref(), Some("success"));
    f.stop().await;
}

#[tokio::test]
async fn checks_and_merge_use_real_heads_and_queue_states() {
    for conclusion in [Some("success"), Some("failure"), None] {
        let f = Fixture::new("owner").await;
        f.workspace("fixture").await;
        f.repository("fixture/repo").await;
        let agent = f.member("agent").await;
        let ticket = f
            .ticket(
                "fixture/repo",
                json!({"title":"Work"}),
                Some("agent"),
                "ready",
            )
            .await;
        let base = f.commit("fixture/repo", "main", None, "README.md", "base");
        let head = f.commit(
            "fixture/repo",
            "feature",
            Some(&base),
            "feature.txt",
            "feature",
        );
        let url = f
            .pull(
                &agent,
                "fixture/repo",
                json!({"head":"feature","title":"Work","tickets":[{"url":ticket,"closes":true}]}),
            )
            .await;
        let number = url.rsplit('/').next().unwrap().parse().unwrap();
        f.check("fixture/repo", "feature", &head, conclusion).await;
        let checks = f.owner.pulls_checks("fixture/repo", number).await.unwrap();
        assert_eq!(checks.head_sha, head);
        assert_eq!(checks.checks.len(), 1);
        assert_eq!(
            checks.checks[0].status,
            if conclusion.is_some() {
                "completed"
            } else {
                "queued"
            }
        );
        assert_eq!(checks.checks[0].conclusion.as_deref(), conclusion);
        if conclusion == Some("success") {
            f.merge("fixture/repo", number, &head, &base).await;
            assert!(
                f.owner
                    .pulls_get("fixture/repo", number)
                    .await
                    .unwrap()
                    .merged
            );
            assert_eq!(
                f.owner
                    .tickets_get_ticket("fixture/repo", "1")
                    .await
                    .unwrap()
                    .state,
                "closed"
            );
            assert_eq!(
                f.owner
                    .repos_branch("fixture/repo", "main")
                    .await
                    .unwrap()
                    .sha,
                head
            );
        }
        f.stop().await;
    }
}

#[tokio::test]
async fn independent_services_keep_ticket_relations_and_lifecycle_isolated() {
    let first = Fixture::new("owner").await;
    let second = Fixture::new("owner").await;
    for f in [&first, &second] {
        f.workspace("fixture").await;
        f.repository("fixture/repo").await;
        f.member("agent").await;
        let blocker = f
            .ticket("fixture/repo", json!({"title":"Blocker"}), None, "done")
            .await;
        let parent = f
            .ticket(
                "fixture/repo",
                json!({"title":"Parent","interactive":true}),
                None,
                "ready",
            )
            .await;
        f.ticket(
            "fixture/repo",
            json!({"title":"Child","parent":parent,"blocked_by":[blocker]}),
            Some("agent"),
            "in_progress",
        )
        .await;
        let child = f
            .owner
            .tickets_get_ticket("fixture/repo", "3")
            .await
            .unwrap();
        assert_eq!(child.lane, "in_progress");
        assert_eq!(child.assignee.unwrap().name, "agent");
        assert_eq!(child.parent.unwrap().url, parent);
        assert_eq!(child.interactive_ancestor.unwrap().url, parent);
        assert_eq!(child.blocked_by[0].url, blocker);
    }
    let stopped = first.data();
    first.stop().await;
    assert!(stopped.health().is_err());
    assert!(second.data().health().is_ok());
    assert_eq!(
        second
            .owner
            .tickets_get_ticket("fixture/repo", "3")
            .await
            .unwrap()
            .title,
        "Child"
    );
    second.stop().await;
}

#[tokio::test]
async fn target_results_ignore_other_heads_priority_class() {
    for draft in [Some(false), Some(true), None] {
        for conclusion in ["success", "failure"] {
            let f = Fixture::new("owner").await;
            f.workspace("fixture").await;
            f.repository("fixture/repo").await;
            let agent = f.member("agent").await;
            let base = f.commit("fixture/repo", "main", None, "README.md", "base");
            f.check("fixture/repo", "main", &base, None).await;
            let head = f.commit(
                "fixture/repo",
                "feature",
                Some(&base),
                "feature.txt",
                "feature",
            );
            if let Some(draft) = draft {
                f.pull(
                    &agent,
                    "fixture/repo",
                    json!({"head":"feature","title":"Work","draft":draft}),
                )
                .await;
            }
            f.check("fixture/repo", "feature", &head, Some(conclusion))
                .await;
            let attempts = f
                .data()
                .app
                .store
                .read(move |r| {
                    let repo = crate::domain::repo_by_path(r, "fixture/repo")?;
                    Ok((
                        crate::ci::queue::attempts_of(r, &repo.id, &base)?,
                        crate::ci::queue::attempts_of(r, &repo.id, &head)?,
                    ))
                })
                .await
                .unwrap();
            assert_eq!(attempts.0.len(), 1);
            assert_eq!(attempts.0[0].status, "queued");
            assert_eq!(attempts.0[0].conclusion, None);
            assert_eq!(attempts.1.len(), 1);
            assert_eq!(attempts.1[0].status, "completed");
            assert_eq!(attempts.1[0].conclusion.as_deref(), Some(conclusion));
            f.stop().await;
        }
    }
}

#[tokio::test]
async fn a_failed_check_can_be_retried_on_the_same_head() {
    let f = Fixture::new("owner").await;
    f.workspace("fixture").await;
    f.repository("fixture/repo").await;
    let agent = f.member("agent").await;
    let base = f.commit("fixture/repo", "main", None, "README.md", "base");
    let head = f.commit(
        "fixture/repo",
        "feature",
        Some(&base),
        "feature.txt",
        "feature",
    );
    f.pull(
        &agent,
        "fixture/repo",
        json!({"head":"feature","title":"Work"}),
    )
    .await;
    for conclusion in [None, Some("failure"), Some("success")] {
        f.check("fixture/repo", "feature", &head, conclusion).await;
        let checks = f.owner.pulls_checks("fixture/repo", 1).await.unwrap();
        assert_eq!(checks.head_sha, head);
        assert_eq!(checks.checks[0].conclusion.as_deref(), conclusion);
    }
    f.stop().await;
}
