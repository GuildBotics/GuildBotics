//! The only provider selection boundary; provider modules stay private.
mod branvil;
pub use ::branvil::agent::error as branvil_error;
pub use branvil::GIT_TOKEN_LABEL;
mod local;
mod slack;
use gb_config::secrets::SecretStore;
use gb_ports::*;
use sha2::{Digest, Sha256};
pub use slack::{IntegrationEvent, IntegrationObserver};
use std::{collections::BTreeMap, path::PathBuf, sync::Arc};

/// Issue through the provider serving the origin; the server validates and locks the device.
pub async fn human_git_credential(
    origin: &str,
    data: &::branvil::api::DataApi,
    device_id: &str,
    display_name: &str,
) -> anyhow::Result<Option<GitCredential>> {
    branvil::owner_git::credential(origin, data, device_id, display_name).await
}

/// Resolve a public commit link without loading a member's credentials.
/// URL rules remain those of the configured provider.
pub fn commit_url_from_remote(
    provider: &str,
    remote: &str,
    sha: &str,
    embedded: Option<&::branvil::api::DataApi>,
) -> Option<String> {
    match provider.to_lowercase().as_str() {
        "branvil" => {
            let api = embedded?;
            let repository = branvil::BranvilService::repository_for_remote(api, remote)?;
            Some(branvil::BranvilService::commit_url_for(
                api,
                &repository,
                sha,
            ))
        }
        _ => None,
    }
}

#[derive(Clone, Debug)]
pub struct IntegrationConfig {
    pub local_root: PathBuf,
    pub identity_root: PathBuf,
    pub person_id: String,
    pub display_name: String,
    pub accounts: BTreeMap<String, ActorName>,
    /// Branvil logins and display names of the workspace team.
    pub branvil_members: BTreeMap<String, String>,
    pub chat_provider: String,
    pub slack_base_url: String,
}
/// Chat capabilities shared by receive-state selection and service construction.
#[derive(Clone, Copy)]
pub enum ChatProvider {
    Local,
    Slack,
}
impl ChatProvider {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "local" => Some(Self::Local),
            "slack" => Some(Self::Slack),
            _ => None,
        }
    }
}
/// A factory is bound to one roster member and the identity secret store.
pub struct ServiceIntegrationFactory<'a> {
    config: IntegrationConfig,
    secrets: &'a SecretStore<'a>,
    branvil: Option<::branvil::api::DataApi>,
    observer: IntegrationObserver,
}
impl<'a> ServiceIntegrationFactory<'a> {
    pub fn new(
        config: IntegrationConfig,
        secrets: &'a SecretStore<'a>,
        branvil: Option<::branvil::api::DataApi>,
        observer: IntegrationObserver,
    ) -> Result<Self, ServiceError> {
        if !secrets.is_identity() || !gb_config::team::valid_person_id(&config.person_id) {
            return Err(ServiceError::InvalidParameters);
        }
        Ok(Self {
            config,
            secrets,
            branvil,
            observer,
        })
    }
    fn actor(&self, name: &str) -> ActorName {
        if name == "local" {
            ActorName(format!("local-{}", self.config.person_id))
        } else {
            self.config
                .accounts
                .get(name)
                .cloned()
                .unwrap_or(ActorName(String::new()))
        }
    }
    fn secret(&self, name: &str) -> Result<String, ServiceError> {
        let prefix = gb_config::team::person_secret_prefix(
            self.secrets.identity_root(),
            &self.config.person_id,
        )
        .map_err(
            |error| match error.downcast::<gb_config::team::PrefixConflict>() {
                Ok(conflict) => ServiceError::CredentialPrefixConflict {
                    member: conflict.person_id,
                    conflicting_member: conflict.conflicting.join(", "),
                },
                Err(_) => ServiceError::Failed,
            },
        )?;
        self.secrets
            .get(&(prefix + name))
            .map(|s| s.unwrap_or_default())
            .map_err(|_| ServiceError::Failed)
    }
    fn required_secret(&self, name: &str) -> Result<String, ServiceError> {
        let value = self.secret(name)?;
        if value.is_empty() {
            Err(ServiceError::MissingCredential {
                name: gb_config::team::person_secret_name(&self.config.person_id, name),
            })
        } else {
            Ok(value)
        }
    }
    fn branvil(&self, scope: &str) -> Result<branvil::BranvilService, ServiceError> {
        Ok(branvil::BranvilService::new(
            self.branvil.clone().ok_or(ServiceError::Unavailable)?,
            scope.into(),
            self.config.person_id.clone(),
            self.config.display_name.clone(),
        )
        .with_members(&self.config.branvil_members))
    }
    fn unsupported_provider(service: &str, name: &str) -> ServiceError {
        ServiceError::Diagnostic {
            class: ServiceErrorClass::Unsupported,
            key: "integrations.resources.unsupported",
            args: [
                ("service".into(), service.into()),
                ("name".into(), name.into()),
            ]
            .into(),
        }
    }
    pub fn create_ticket_service(
        &self,
        workspace: &str,
    ) -> Result<Arc<dyn TicketService>, ServiceError> {
        if workspace.is_empty() {
            return Err(ServiceError::Unsupported);
        }
        Ok(Arc::new(
            ::branvil::agent::TicketAgent::new(
                self.branvil.clone().ok_or(ServiceError::Unavailable)?,
                workspace.into(),
                self.config.person_id.clone(),
                self.config.display_name.clone(),
            )
            .with_members(&self.config.branvil_members),
        ))
    }
    pub fn create_code_hosting_service(
        &self,
        workspace: &str,
    ) -> Result<Arc<dyn CodeHostingService>, ServiceError> {
        if workspace.is_empty() {
            return Err(ServiceError::Unsupported);
        }
        Ok(Arc::new(self.branvil(workspace)?))
    }
    pub fn create_chat_service(&self) -> Result<Arc<dyn ChatService>, ServiceError> {
        match ChatProvider::parse(&self.config.chat_provider) {
            Some(ChatProvider::Local) => Ok(Arc::new(local::LocalChat::new(
                self.config.local_root.clone(),
                &self.actor("local").0,
                &self.config.display_name,
            ))),
            Some(ChatProvider::Slack) => Ok(Arc::new(
                slack::SlackChatService::new(
                    self.secret("SLACK_BOT_TOKEN")?,
                    self.secret("SLACK_APP_TOKEN")?,
                    self.slack_base(),
                    Arc::new(slack::WebTransport::new()?),
                    self.observer.clone(),
                )
                .with_member(self.config.person_id.clone())
                .with_token_name(gb_config::team::person_secret_name(
                    &self.config.person_id,
                    "SLACK_BOT_TOKEN",
                )),
            )),
            None => Err(Self::unsupported_provider(
                "chat_service",
                &self.config.chat_provider,
            )),
        }
    }
    fn slack_base(&self) -> String {
        if self.config.slack_base_url.is_empty() {
            slack::BASE_URL.into()
        } else {
            self.config.slack_base_url.trim_end_matches('/').into()
        }
    }
    /// Check token presence for a subscribed Slack agent. The host owns
    /// subscription and human/agent eligibility when selecting this check.
    pub fn verify_chat_credentials(&self) -> Result<(), ServiceError> {
        match self.config.chat_provider.as_str() {
            "slack" => {
                self.required_secret("SLACK_BOT_TOKEN")?;
                self.required_secret("SLACK_APP_TOKEN")?;
                Ok(())
            }
            "local" => Ok(()),
            _ => Err(Self::unsupported_provider(
                "chat_service",
                &self.config.chat_provider,
            )),
        }
    }
    pub fn listener_key(&self) -> Result<String, ServiceError> {
        match self.config.chat_provider.as_str() {
            "local" => Ok(format!("local:{}", self.config.local_root.display())),
            "slack" => {
                let token = self.required_secret("SLACK_APP_TOKEN")?;
                if token.is_empty() {
                    return Err(ServiceError::Unavailable);
                }
                Ok(format!(
                    "slack:{:x}",
                    Sha256::digest(format!("{}\0{token}", self.slack_base()).as_bytes())
                ))
            }
            _ => Err(Self::unsupported_provider(
                "chat_service",
                &self.config.chat_provider,
            )),
        }
    }
    pub fn create_event_listener(
        &self,
        on_activity: Arc<dyn Fn() + Send + Sync>,
    ) -> Result<Arc<dyn EventListener>, ServiceError> {
        match self.config.chat_provider.as_str() {
            "local" => Ok(Arc::new(local::LocalListener::new(
                self.config.local_root.clone(),
                on_activity,
                self.observer.clone(),
            ))),
            "slack" => Ok(Arc::new(
                slack::SlackListener::new(
                    self.required_secret("SLACK_APP_TOKEN")?,
                    self.slack_base(),
                    Arc::new(slack::WebTransport::new()?),
                    on_activity,
                    self.observer.clone(),
                )
                .with_member(self.config.person_id.clone()),
            )),
            _ => Err(Self::unsupported_provider(
                "chat_service",
                &self.config.chat_provider,
            )),
        }
    }
}
