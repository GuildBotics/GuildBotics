//! The providers' part of a setup, as the server's registry knows it
//! (the Python version's `integrations/factory.py` `PROVIDERS` and each
//! provider's `setup`). The service and member models of Branvil and Slack
//! are validated with their setup screens; a provider without a model keeps
//! the items its form names.

use crate::api::Shared;
use gb_setup::{
    Hook, InvalidInput, MemberAccount, ProviderModels, SavedMember, ServiceKind, SetupError,
    StoredAccount,
};
use serde::Deserialize;
use serde_json::json;
pub(crate) mod accounts;
pub mod diagnostics;
#[cfg(test)]
mod models_tests;
use serde_json::{Map, Value};
use std::collections::BTreeMap;

/// Each provider and the kinds of service it serves.
const PROVIDERS: &[(&str, &[ServiceKind])] = &[
    (
        "branvil",
        &[ServiceKind::CodeHostingService, ServiceKind::TicketService],
    ),
    (
        "local",
        &[
            ServiceKind::ChatService,
            ServiceKind::CodeHostingService,
            ServiceKind::TicketService,
        ],
    ),
    ("slack", &[ServiceKind::ChatService]),
];

/// The secrets a member holds with each provider, by member-relative name.
const MEMBER_SECRETS: &[&str] = &[
    "BRANVIL_TOKEN",
    "BRANVIL_TOKEN_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
];

#[derive(Clone, Default)]
pub struct Registry(pub Option<Shared>);

impl ProviderModels for Registry {
    fn member_secret_names(&self) -> Vec<String> {
        MEMBER_SECRETS.iter().map(|name| name.to_string()).collect()
    }

    fn service(&self, kind: ServiceKind, form: &Value) -> anyhow::Result<Value> {
        let named = form.get("name").and_then(Value::as_str).unwrap_or("");
        let name = named.to_lowercase();
        if !PROVIDERS
            .iter()
            .any(|(provider, kinds)| *provider == name && kinds.contains(&kind))
        {
            let kind = serde_json::to_value(kind)?;
            return Err(InvalidInput(format!(
                "'{named}' provides no {}",
                kind.as_str().unwrap_or("")
            ))
            .into());
        }
        if name == "branvil" || name == "slack" {
            let allowed = if name == "branvil" {
                "workspace"
            } else {
                "base_url"
            };
            if form
                .as_object()
                .is_none_or(|m| m.keys().any(|k| k != "name" && k != allowed))
            {
                return Err(InvalidInput("unknown service field".into()).into());
            }
        }
        if name == "branvil" {
            let workspace = form["workspace"].as_str().unwrap_or("").trim();
            if workspace.contains('/')
                || !gb_integrations::valid_repository_path(&format!("{workspace}/name"))
            {
                return Err(InvalidInput("workspace must be one name, such as acme".into()).into());
            }
            return Ok(json!({"name":"branvil","workspace":workspace}));
        }
        if name == "slack" {
            let mut base = form.get("base_url").cloned().unwrap_or(Value::Null);
            if let Some(value) = base.as_str() {
                base = Value::String(value.trim().to_owned());
            }
            if !base.is_null() && !base.is_string() {
                return Err(InvalidInput("base_url must be a string".into()).into());
            }
            return Ok(if base.is_null() {
                json!({"name":"slack"})
            } else {
                json!({"name":"slack","base_url":base})
            });
        }
        let mut item = form.as_object().cloned().unwrap_or_default();
        item.insert("name".into(), name.into());
        Ok(Value::Object(item))
    }

    fn member_providers(&self) -> Vec<String> {
        vec!["branvil".into(), "slack".into()]
    }

    fn account_info_keys(&self, provider: &str) -> Vec<String> {
        match provider {
            "branvil" => vec!["branvil_username".into()],
            "slack" => vec!["slack_user_id".into()],
            _ => vec![],
        }
    }
    fn account(&self, provider: &str, form: &Value) -> anyhow::Result<Box<dyn MemberAccount>> {
        let invalid = |e: serde_json::Error| InvalidInput(e.to_string());
        let mut form = form.clone();
        if let Some(fields) = form.as_object_mut() {
            for value in fields.values_mut() {
                if let Some(text) = value.as_str() {
                    *value = Value::String(text.trim().into());
                }
            }
        }
        match provider {
            "slack" => Ok(Box::new(
                serde_json::from_value::<SlackAccount>(form.clone()).map_err(invalid)?,
            )),
            "branvil" => {
                let mut account = serde_json::from_value::<accounts::BranvilAccount>(form.clone())
                    .map_err(invalid)?;
                if !account.username.is_empty()
                    && !regex::Regex::new(r"^[A-Za-z0-9_.-]{1,64}$")
                        .expect("member grammar")
                        .is_match(&account.username)
                {
                    return Err(
                        InvalidInput("username must be a Branvil member name".into()).into(),
                    );
                }
                account.registry = self.clone();
                Ok(Box::new(account))
            }
            _ => Err(InvalidInput(format!("'{provider}' has no member account")).into()),
        }
    }
    fn view(
        &self,
        provider: &str,
        info: &BTreeMap<String, String>,
        has: &dyn Fn(&str) -> bool,
    ) -> Value {
        match provider {
            "branvil" => {
                json!({"username":info.get("branvil_username").cloned().unwrap_or_default(),"has_token":has("BRANVIL_TOKEN")})
            }
            "slack" => {
                json!({"user_id":info.get("slack_user_id").cloned().unwrap_or_default(),"has_bot_token":has("SLACK_BOT_TOKEN"),"has_app_token":has("SLACK_APP_TOKEN")})
            }
            _ => json!({}),
        }
    }
    fn removed<'a>(&'a self, provider: &'a str, member: &'a SavedMember) -> Hook<'a> {
        Box::pin(async move {
            if provider == "branvil" {
                self.remove_branvil(member).await?;
            }
            Ok(())
        })
    }
    fn avatar_changed<'a>(&'a self, provider: &'a str, member: &'a SavedMember) -> Hook<'a> {
        Box::pin(async move {
            let name = member
                .account_info
                .get("branvil_username")
                .map(String::as_str)
                .unwrap_or("");
            if provider == "branvil"
                && !name.is_empty()
                && let Some((_, admin)) = self.admin().await?
            {
                accounts::picture(&admin, name, member).await?;
            }
            Ok(())
        })
    }
    fn channel(&self, services: &Value, reference: &str) -> anyhow::Result<Map<String, Value>> {
        let mut chat = Map::new();
        let key = if services["chat_service"]["name"] == "slack"
            && regex::Regex::new(r"^[CGD][A-Z0-9]{8,}$")
                .expect("channel grammar")
                .is_match(reference.trim())
        {
            "channel_id"
        } else {
            "channel_name"
        };
        chat.insert(key.into(), reference.into());
        Ok(chat)
    }
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct SlackAccount {
    #[serde(default)]
    user_id: String,
    #[serde(default)]
    bot_token: String,
    #[serde(default)]
    app_token: String,
}
impl MemberAccount for SlackAccount {
    fn prepare<'a>(&'a mut self, _: &'a SavedMember) -> Hook<'a> {
        Box::pin(async { Ok(()) })
    }
    fn stored(&self, _: &str, human: bool) -> Result<StoredAccount, SetupError> {
        let mut result = StoredAccount::default();
        if human {
            if !self.user_id.is_empty() {
                result
                    .account_info
                    .insert("slack_user_id".into(), self.user_id.clone());
            }
        } else {
            for (name, value) in [
                ("SLACK_BOT_TOKEN", &self.bot_token),
                ("SLACK_APP_TOKEN", &self.app_token),
            ] {
                if !value.is_empty() {
                    result.secrets.insert(name.into(), value.clone());
                }
            }
        }
        Ok(result)
    }
    fn settled(self: Box<Self>, _: bool) -> std::pin::Pin<Box<dyn Future<Output = ()> + Send>> {
        Box::pin(async {})
    }
}
