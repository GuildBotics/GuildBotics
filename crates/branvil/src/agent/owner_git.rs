//! Server-side issuance for the embedded host of the requested origin.
use crate::api::{DataApi, members};
use gb_ports::GitCredential;

pub async fn credential(
    origin: &str,
    data: &DataApi,
    device_id: &str,
    display_name: &str,
) -> anyhow::Result<Option<GitCredential>> {
    if !origin.eq_ignore_ascii_case(data.base_url().trim_end_matches('/')) {
        return Ok(None);
    }
    let admin = data.owner();
    let owner = data
        .owner_name()
        .map_err(|e| super::error(e, "owner_name"))?;
    let suffix = format!("({})", &device_id[device_id.len() - 8..]);
    let mut cursor = None;
    let mut expired = Vec::new();
    loop {
        let page = admin
            .page(
                format!("owner-git:{device_id}"),
                Some(100),
                cursor.as_deref(),
            )
            .map_err(|e| super::error(e, "page"))?;
        let page = admin
            .members_list_tokens(owner.clone(), page)
            .await
            .map_err(|e| super::error(e, "members_list_tokens"))?;
        expired.extend(
            page.items
                .into_iter()
                .filter(|t| t.label.starts_with("git:") && t.label.ends_with(&suffix))
                .map(|t| t.id),
        );
        cursor = page.next_cursor;
        if cursor.is_none() {
            break;
        }
    }
    for id in expired {
        if let Err(e) = admin.members_revoke_token(owner.clone(), id).await
            && e.status.as_u16() != 404
        {
            return Err(super::error(e, "members_revoke_token").into());
        }
    }
    let issued = admin
        .members_issue_token(
            owner.clone(),
            members::NewToken {
                label: format!("git:{display_name} {suffix}"),
                replace: false,
            },
        )
        .await
        .map_err(|e| super::error(e, "members_issue_token"))?;
    Ok(Some(GitCredential {
        username: owner,
        password: issued.token,
    }))
}

#[cfg(all(test, feature = "test-support"))]
mod tests {
    use super::*;
    #[tokio::test]
    async fn issuing_for_two_devices_preserves_the_other_devices_token() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("branvil");
        crate::api::initialize(&root, "owner").unwrap();
        let running =
            crate::api::Running::start(&root, std::sync::Arc::new(crate::vault::Memory::default()))
                .await
                .unwrap();
        let admin = running.data.owner();
        let origin = running.data.base_url();
        let first = "019691c2-8dc0-7654-8000-0000abc01234";
        let second = "019691c2-8dc0-7654-8000-0000deadbeef";
        let a = credential(origin, &running.data, first, "First")
            .await
            .unwrap()
            .unwrap();
        let b = credential(origin, &running.data, second, "Second")
            .await
            .unwrap()
            .unwrap();
        let renewed = credential(origin, &running.data, first, "Renamed")
            .await
            .unwrap()
            .unwrap();
        let member = |token| crate::test_support::token_member(&running.data, token);
        assert_eq!(a.username, "owner");
        assert_eq!(member(&a.password).await, None);
        assert_eq!(member(&b.password).await.as_deref(), Some("owner"));
        assert_eq!(member(&renewed.password).await.as_deref(), Some("owner"));
        assert!(
            credential("http://127.0.0.1:1", &running.data, first, "First")
                .await
                .unwrap()
                .is_none()
        );
        let page = admin.page("test".into(), Some(100), None).unwrap();
        let tokens = admin
            .members_list_tokens("owner".into(), page)
            .await
            .unwrap();
        assert!(
            tokens
                .items
                .iter()
                .any(|t| t.label == "git:Renamed (abc01234)")
        );
        assert!(
            tokens
                .items
                .iter()
                .any(|t| t.label == "git:Second (deadbeef)")
        );
    }
}
