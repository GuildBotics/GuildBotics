//! Frozen URL and member-credential failures without the removed HTTP client.
use super::*;
use branvil::api::{Running, initialize};

#[test]
fn frozen_error_classifications_preserve_branvil_guidance_and_arguments() {
    let stopped = error(
        AppError::unavailable("integrations.branvil.api.stopped"),
        "authenticate",
    );
    assert!(stopped.is_unavailable());
    assert_eq!(stopped, diagnostic("integrations.branvil.api.stopped", []));
    let internal = error(AppError::internal("private storage detail"), "pulls_review");
    assert!(!internal.translation_args()["message"].contains("private storage detail"));
    for (failure, key, args) in [
        (
            AppError::unauthorized(),
            "integrations.branvil.api.authentication",
            vec![],
        ),
        (
            AppError::not_found("No such ticket."),
            "integrations.branvil.api.not_found",
            vec![("message", "No such ticket.")],
        ),
        (
            AppError::unsupported("No upstream."),
            "integrations.branvil.api.unsupported",
            vec![("message", "No upstream.")],
        ),
        (
            AppError::conflict_code("head_moved", "Changed."),
            "integrations.branvil.api.head_moved",
            vec![],
        ),
        (
            AppError::conflict_code("label_undefined", "Label bug."),
            "integrations.branvil.api.label_undefined",
            vec![("message", "Label bug.")],
        ),
        (
            AppError::forbidden("Permission denied."),
            "integrations.branvil.api.refused",
            vec![
                ("operation", "pulls_review"),
                ("status", "403"),
                ("code", "forbidden"),
                ("message", "Permission denied."),
            ],
        ),
        (
            AppError::invalid("Invalid title."),
            "integrations.branvil.api.refused",
            vec![
                ("operation", "pulls_review"),
                ("status", "400"),
                ("code", "invalid"),
                ("message", "Invalid title."),
            ],
        ),
    ] {
        let mapped = error(failure, "pulls_review");
        assert!(!mapped.is_unavailable());
        assert_eq!(mapped.translation_key(), key);
        assert_eq!(
            mapped.translation_args(),
            args.into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect()
        );
    }
    assert_eq!(
        error(AppError::invalid_cursor(), "page"),
        ServiceError::InvalidContinuation
    );
    assert_eq!(
        error(AppError::not_found("x".repeat(600)), "pulls_get").translation_args()["message"]
            .len(),
        500
    );
    assert_eq!(
        decode::<u64>(json!("bad")).unwrap_err(),
        diagnostic("integrations.branvil.api.invalid", [])
    );
}
#[tokio::test]
async fn frozen_remote_and_pull_address_population() {
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
        String::new(),
        "owner1".into(),
        "acme".into(),
        "aiko".into(),
        std::sync::Arc::new(|_| {}),
    );
    let base = running.data.base_url();
    let repo = "acme/demo";
    let remote = service.clone_url(repo).await.unwrap();
    assert_eq!(remote, format!("{base}/git/{repo}.git"));
    for url in [&remote, &remote.trim_end_matches(".git").to_owned()] {
        assert_eq!(service.repository_from_remote(url), Some(repo.into()));
    }
    assert_eq!(
        service.commit_url(repo, "abc"),
        format!("{base}/{repo}/-/commits/abc")
    );
    for url in [
        format!("http://127.0.0.1:7879/git/{repo}.git"),
        base.replacen("://", "://aiko@", 1) + &format!("/git/{repo}.git"),
        format!("{base}/{repo}.git"),
        format!("{base}/git/demo.git"),
    ] {
        assert_eq!(service.repository_from_remote(&url), None, "{url}");
    }
    for url in [
        format!("https://github.com/{repo}/pull/1"),
        format!("http://127.0.0.1:7879/{repo}/-/pulls/1"),
        format!("{base}/{repo}/-/tickets/1"),
        format!("{base}/{repo}/-/pulls/0"),
        format!("{base}/{repo}/-/pulls/1?x=1"),
        format!("{base}/demo/-/pulls/1"),
    ] {
        // Missing credentials would be a different error if validation reached the API.
        assert_eq!(
            CodeHostingService::comment(&service, &url, "Hi", None)
                .await
                .unwrap_err(),
            diagnostic(
                "integrations.branvil.api.not_pull_request",
                [("url", url.clone())]
            ),
            "{url}"
        );
    }
    running.stop().await.unwrap();
}
#[tokio::test]
async fn frozen_git_credentials_identity_and_missing_token_operations() {
    let temp = tempfile::tempdir().unwrap();
    let token = initialize(temp.path(), "owner1").unwrap();
    let running = Running::start(
        temp.path(),
        std::sync::Arc::new(::branvil::vault::Memory::default()),
    )
    .await
    .unwrap();
    let mut service = BranvilService::new(
        running.data.clone(),
        token.clone(),
        "owner1".into(),
        "acme".into(),
        "aiko".into(),
        std::sync::Arc::new(|_| {}),
    );
    let credential = service.git_credential().await.unwrap().unwrap();
    assert_eq!(credential.username, "owner1");
    assert_eq!(credential.password, token);
    assert_eq!(
        service.identity(true).await.unwrap(),
        [("branvil_username".into(), "owner1".into())].into()
    );
    service.token.clear();
    for error in [
        service.git_credential().await.err().unwrap(),
        service.default_branch("acme/demo").await.unwrap_err(),
    ] {
        assert_eq!(
            error.translation_key(),
            "integrations.branvil.api.token_missing"
        );
        assert_eq!(
            error.translation_args(),
            [("name".into(), "AIKO_BRANVIL_TOKEN".into())].into()
        );
    }
    running.stop().await.unwrap();
}

#[tokio::test]
async fn draft_conflicts_reread_both_human_writer_states() {
    use std::sync::{Arc, Mutex};
    #[derive(Default)]
    struct State {
        revision: u64,
        draft: bool,
        title: String,
        reads: Vec<u64>,
        writes: Vec<u64>,
    }
    for human_draft in [true, false] {
        let state = Arc::new(Mutex::new(State {
            revision: 1,
            title: "Initial".into(),
            ..Default::default()
        }));
        let read_state = state.clone();
        let write_state = state.clone();
        let result = patrol::draft_transition(
            move || {
                let state = read_state.clone();
                Box::pin(async move {
                    let mut s = state.lock().unwrap();
                    let revision = s.revision;
                    s.reads.push(revision);
                    Ok((!s.draft).then_some(revision))
                })
            },
            move |revision| {
                let state = write_state.clone();
                Box::pin(async move {
                    let mut s = state.lock().unwrap();
                    s.writes.push(revision);
                    if s.writes.len() == 1 {
                        // The human wins after this caller read the old revision.
                        s.revision = 2;
                        s.draft = human_draft;
                        s.title = "Human update".into();
                        return Ok(false);
                    }
                    assert_eq!(revision, 2);
                    assert_eq!(s.title, "Human update");
                    s.draft = true;
                    s.revision += 1;
                    Ok(true)
                })
            },
        )
        .await
        .unwrap();
        assert_eq!(result, !human_draft);
        let s = state.lock().unwrap();
        assert_eq!(s.reads, vec![1, 2]);
        assert_eq!(s.writes, if human_draft { vec![1] } else { vec![1, 2] });
        assert_eq!(s.title, "Human update");
        assert!(s.draft);
    }
}
#[tokio::test]
async fn draft_refusals_propagate_without_retry_or_success() {
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    let reads = Arc::new(AtomicUsize::new(0));
    let writes = Arc::new(AtomicUsize::new(0));
    for fail_read in [true, false] {
        reads.store(0, Ordering::SeqCst);
        writes.store(0, Ordering::SeqCst);
        let r = reads.clone();
        let w = writes.clone();
        let result = patrol::draft_transition(
            move || {
                r.fetch_add(1, Ordering::SeqCst);
                Box::pin(async move {
                    if fail_read {
                        Err(ServiceError::Unavailable)
                    } else {
                        Ok(Some(1))
                    }
                })
            },
            move |_| {
                w.fetch_add(1, Ordering::SeqCst);
                Box::pin(async { Err(ServiceError::Failed) })
            },
        )
        .await;
        assert_eq!(
            result,
            Err(if fail_read {
                ServiceError::Unavailable
            } else {
                ServiceError::Failed
            })
        );
        assert_eq!(reads.load(Ordering::SeqCst), 1);
        assert_eq!(writes.load(Ordering::SeqCst), usize::from(!fail_read));
    }
}

#[test]
fn unavailable_population_keeps_guidance_and_retry_category() {
    for message in [
        "Storage is shutting down.",
        "Git is shutting down.",
        "git http-backend timed out.",
        "Upstream is unavailable.",
    ] {
        let mapped = error(AppError::unavailable(message), "pulls_get");
        assert!(mapped.is_unavailable(), "{message}");
        assert!(!mapped.is_not_found());
        assert!(!mapped.is_unsupported());
        assert_eq!(mapped.translation_key(), "integrations.branvil.api.refused");
        assert_eq!(
            mapped.translation_args(),
            [
                ("operation".into(), "pulls_get".into()),
                ("status".into(), "503".into()),
                ("code".into(), "unavailable".into()),
                ("message".into(), message.into()),
            ]
            .into()
        );
    }
}
