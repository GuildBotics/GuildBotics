use branvil::agent::pr_patrol::*;
use chrono::Utc;
#[test]
fn patrol_reviews_statements_individually_and_limits_only_new_heads() {
    let mut pr = PatrolPullRequest {
        open: true,
        author: "aiko".into(),
        head_oid: "head4".into(),
        ..Default::default()
    };
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
    pr.reviews.push(PatrolReview {
        author: "yuki".into(),
        body: "approved with advice".into(),
        submitted_at: "2".into(),
        ..Default::default()
    });
    pr.unanswered = true;
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_feedback")
    );
    pr.reviews[0].reactors.insert("aiko".into());
    pr.unanswered = false;
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
    pr.author = "owner".into();
    pr.reviews.clear();
    for n in 1..=3 {
        pr.reviews.push(PatrolReview {
            author: "aiko".into(),
            commit_oid: format!("head{n}"),
            submitted_at: format!("{n}"),
            ..Default::default()
        });
    }
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_review_limit")
    );
    pr.requested_reviewers.insert("aiko".into());
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_review")
    );
    pr.requested_reviewers.clear();
    pr.head_oid = "head1".into();
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
    pr.comments.push(PatrolComment {
        author: "owner".into(),
        created_at: "4".into(),
        ..Default::default()
    });
    assert_eq!(
        pull_request_work(&pr, "aiko", Utc::now()),
        Some("pull_request_review")
    );
    pr.is_draft = true;
    assert_eq!(pull_request_work(&pr, "aiko", Utc::now()), None);
}
