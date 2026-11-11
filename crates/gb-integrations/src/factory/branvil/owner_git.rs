//! Server-side issuance through the provider that owns the requested origin.
use branvil::api::{DataApi, MemberApi, members};
use gb_ports::GitCredential;

pub(in crate::factory) async fn credential(
    origin: &str,
    data: &DataApi,
    admin: &MemberApi,
    device_id: &str,
    display_name: &str,
) -> anyhow::Result<Option<GitCredential>> {
    if !origin.eq_ignore_ascii_case(data.base_url().trim_end_matches('/')) {
        return Ok(None);
    }
    let owner = admin
        .identity()
        .await
        .map_err(|e| super::error(e, "identity"))?
        .name;
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

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn issuing_for_two_devices_preserves_the_other_devices_token() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("branvil");
        let token = branvil::api::initialize(&root, "owner").unwrap();
        let running = branvil::api::Running::start(
            &root,
            std::sync::Arc::new(branvil::vault::Memory::default()),
        )
        .await
        .unwrap();
        let admin = running.data.authenticate(token).await.unwrap();
        let origin = running.data.base_url();
        let first = "019691c2-8dc0-7654-8000-0000abc01234";
        let second = "019691c2-8dc0-7654-8000-0000deadbeef";
        let a = credential(origin, &running.data, &admin, first, "First")
            .await
            .unwrap()
            .unwrap();
        let b = credential(origin, &running.data, &admin, second, "Second")
            .await
            .unwrap()
            .unwrap();
        let renewed = credential(origin, &running.data, &admin, first, "Renamed")
            .await
            .unwrap()
            .unwrap();
        assert!(running.data.authenticate(a.password).await.is_err());
        assert!(running.data.authenticate(b.password).await.is_ok());
        assert!(running.data.authenticate(renewed.password).await.is_ok());
        assert!(
            credential("http://127.0.0.1:1", &running.data, &admin, first, "First")
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
