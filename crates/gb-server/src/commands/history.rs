//! Resolve client remotes through the configured provider; credentials stay here.
use super::RunRequest;
use crate::api::Shared;
use base64::{Engine, engine::general_purpose::STANDARD};
use gb_config::{commands::CommandRoots, resolved_guest, secrets::*};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

async fn service(
    api: &Shared,
    person: &str,
) -> anyhow::Result<Arc<dyn gb_ports::CodeHostingService>> {
    let selected = api.selected();
    let workspace = selected
        .workspace
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("workspace missing"))?;
    let roots = CommandRoots {
        config: workspace.join(".guildbotics/config"),
        templates: gb_base::home::python_package().join("templates"),
    };
    let metadata = resolved_guest::member(&roots, &api.identity_root, person)?;
    let person = crate::member::native::public(&metadata);
    anyhow::ensure!(
        person.is_active && person.person_type == "agent",
        "inactive member"
    );
    let project = gb_config::yaml_value(&std::fs::read_to_string(
        roots.config.join("team/project.yml"),
    )?)?;
    let writes = gb_base::shared_writes::SharedWrites::new(
        api.identity_root.clone(),
        Some(workspace.clone()),
    );
    let secrets = SecretStore::new(
        &writes,
        Scope::Identity,
        DeviceKey {
            machine_root: api.data.parent().unwrap_or(&api.data).into(),
            keychain: api.keychain.as_ref(),
        },
    )?;
    let config =
        crate::member::native::integration(workspace, &api.identity_root, &project, &person, &[]);
    Ok(gb_integrations::ServiceIntegrationFactory::new(
        config,
        &secrets,
        api.branvil.get().cloned(),
        Arc::new(|_| {}),
    )?
    .create_code_hosting_service()?)
}

pub(super) async fn recognized(
    api: &Shared,
    person: &str,
    remotes: &std::collections::BTreeMap<String, String>,
) -> Vec<String> {
    if remotes.is_empty() {
        return vec![];
    }
    let Ok(code) = service(api, person).await else {
        return vec![];
    };
    let names = remotes
        .iter()
        .filter(|(_, url)| code.repository_from_remote(url).is_some())
        .map(|(name, _)| name.clone())
        .collect();
    let _ = code.aclose().await;
    names
}

pub(super) async fn complete(api: &Shared, request: &mut RunRequest) -> anyhow::Result<()> {
    let Some(upload) = request.upload.as_mut() else {
        return Ok(());
    };
    let incremental = upload.git.as_ref().is_some_and(|g| !g.bases.is_empty());
    if !incremental {
        return Ok(());
    }
    upload.validate().map_err(anyhow::Error::msg)?;
    let Some(git) = upload.git.as_mut().filter(|g| !g.bases.is_empty()) else {
        return Ok(());
    };
    let person = request
        .person
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("member missing"))?;
    let code = service(api, person).await?;
    let result = async {
        let root = api
            .selected()
            .workspace
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("workspace missing"))?
            .join(".guildbotics/local/member_git")
            .join(person);
        let deadline = Instant::now() + Duration::from_secs(60);
        let _lock =
            gb_base::files::FileLock::hold(&root.join("git.lock"), Duration::from_secs(60))?;
        let mut repositories = std::collections::BTreeSet::new();
        for base in &git.bases {
            anyhow::ensure!(git.refs.contains_key(base), "unknown tracking ref");
            let remote = base
                .strip_prefix("refs/remotes/")
                .and_then(|base| base.split_once('/'))
                .map(|(name, _)| name)
                .ok_or_else(|| anyhow::anyhow!("invalid tracking ref"))?;
            let repository = git
                .remotes
                .get(remote)
                .and_then(|url| code.repository_from_remote(url))
                .ok_or_else(|| anyhow::anyhow!("unknown remote"))?;
            repositories.insert(repository);
        }
        let mut origins = Vec::new();
        for repository in repositories {
            let origin = gb_member::git::Origin::open(
                &root,
                &repository,
                code.clone_url(&repository).await?,
                deadline,
            )
            .await?;
            origin.fetch(code.git_credential().await?).await?;
            origins.push(origin);
        }
        git.bundle = STANDARD.encode(
            origins[0]
                .complete_history(
                    &STANDARD.decode(&git.bundle)?,
                    &origins[1..],
                    &git.refs,
                    &git.head,
                )
                .await?,
        );
        git.bases.clear();
        Ok(())
    }
    .await;
    let closed = code.aclose().await;
    result?;
    closed?;
    upload.validate().map_err(anyhow::Error::msg)
}
