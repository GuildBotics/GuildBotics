//! The only provider selection boundary; provider modules stay private.
mod branvil;
mod local;
mod slack;
use gb_config::secrets::SecretStore;
use gb_ports::*;
use sha2::{Digest, Sha256};
pub use slack::{IntegrationEvent, IntegrationObserver};
use std::{collections::BTreeMap, path::PathBuf, sync::Arc};

/// The providers that issue the human's Git credentials are selected here.
/// Read the credential for a human's remote. The host supplies its device
/// ID and display name on demand when the provider needs to issue a token.
pub async fn human_git_credential(
    origin: &str,
    device: impl FnOnce(&std::path::Path) -> anyhow::Result<(String, String)>,
) -> anyhow::Result<Option<GitCredential>> {
    branvil::owner_git::credential(origin, device).await
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
    pub ticket_provider: String,
    pub code_provider: String,
    pub chat_provider: String,
    pub ticket_scope: String,
    pub code_scope: String,
    pub checks: String,
    pub slack_base_url: String,
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
        let prefix = self.config.person_id.replace('-', "_").to_uppercase() + "_";
        let members = self
            .secrets
            .identity_root()
            .join(".guildbotics/config/team/members");
        let entries = match std::fs::read_dir(members) {
            Ok(entries) => Some(entries),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(_) => return Err(ServiceError::Failed),
        };
        for entry in entries.into_iter().flatten() {
            let entry = entry.map_err(|_| ServiceError::Failed)?;
            if !entry.path().is_dir() || !entry.path().join("identity.yml").is_file() {
                continue;
            }
            let id = entry.file_name().to_string_lossy().into_owned();
            if id != self.config.person_id && id.replace('-', "_").to_uppercase() + "_" == prefix {
                return Err(ServiceError::CredentialPrefixConflict {
                    member: self.config.person_id.clone(),
                    conflicting_member: id,
                });
            }
        }
        self.secrets
            .get(&(prefix + name))
            .map(|s| s.unwrap_or_default())
            .map_err(|_| ServiceError::Failed)
    }
    fn required_secret(&self, name: &str) -> Result<String, ServiceError> {
        let value = self.secret(name)?;
        if value.is_empty() {
            Err(ServiceError::MissingCredential {
                name: format!(
                    "{}_{}",
                    self.config.person_id.replace('-', "_").to_uppercase(),
                    name
                ),
            })
        } else {
            Ok(value)
        }
    }
    fn local(&self, scope: &str) -> local::LocalService {
        local::LocalService::new(
            self.config.local_root.clone(),
            scope,
            &self.actor("local").0,
            &self.config.person_id,
            &self.config.display_name,
            &self.config.checks,
        )
    }
    fn branvil(&self, scope: &str) -> Result<branvil::BranvilService, ServiceError> {
        Ok(branvil::BranvilService::new(
            self.branvil.clone().ok_or(ServiceError::Unavailable)?,
            self.secret("BRANVIL_TOKEN")?,
            self.actor("branvil").0,
            scope.into(),
            self.config.person_id.clone(),
            self.observer.clone(),
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
    pub fn create_ticket_service(&self) -> Result<Arc<dyn TicketService>, ServiceError> {
        match self.config.ticket_provider.as_str() {
            "local" => Ok(Arc::new(self.local(&self.config.ticket_scope))),
            "branvil" => Ok(Arc::new(self.branvil(&self.config.ticket_scope)?)),
            _ => Err(Self::unsupported_provider(
                "ticket_service",
                &self.config.ticket_provider,
            )),
        }
    }
    pub fn create_code_hosting_service(&self) -> Result<Arc<dyn CodeHostingService>, ServiceError> {
        match self.config.code_provider.as_str() {
            "local" => Ok(Arc::new(self.local(&self.config.code_scope))),
            "branvil" => Ok(Arc::new(self.branvil(&self.config.code_scope)?)),
            _ => Err(Self::unsupported_provider(
                "code_hosting_service",
                &self.config.code_provider,
            )),
        }
    }
    pub fn create_chat_service(&self) -> Result<Arc<dyn ChatService>, ServiceError> {
        match self.config.chat_provider.as_str() {
            "local" => Ok(Arc::new(local::LocalChat::new(
                self.config.local_root.clone(),
                &self.actor("local").0,
                &self.config.display_name,
            ))),
            "slack" => Ok(Arc::new(
                slack::SlackChatService::new(
                    self.secret("SLACK_BOT_TOKEN")?,
                    self.secret("SLACK_APP_TOKEN")?,
                    self.slack_base(),
                    Arc::new(slack::WebTransport::new()?),
                    self.observer.clone(),
                )
                .with_member(self.config.person_id.clone())
                .with_token_name(format!(
                    "{}_SLACK_BOT_TOKEN",
                    self.config.person_id.replace('-', "_").to_uppercase()
                )),
            )),
            _ => Err(Self::unsupported_provider(
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
