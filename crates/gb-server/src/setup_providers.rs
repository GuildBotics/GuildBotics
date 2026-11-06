//! The providers' part of a setup, as the server's registry knows it
//! (the Python version's `integrations/factory.py` `PROVIDERS` and each
//! provider's `setup`). The service and member models of Branvil and Slack
//! are carried over with their setup screens; until then a provider keeps
//! the items its form names, as a provider without a model does.

use gb_setup::{
    Hook, HostCredential, InvalidInput, MemberAccount, ProviderModels, SavedMember, ServiceKind,
};
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

pub struct Registry;

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
        let mut item = form.as_object().cloned().unwrap_or_default();
        item.insert("name".into(), name.into());
        Ok(Value::Object(item))
    }

    fn member_providers(&self) -> Vec<String> {
        Vec::new()
    }

    fn account_info_keys(&self, _: &str) -> Vec<String> {
        Vec::new()
    }

    fn account(&self, provider: &str, _: &Value) -> anyhow::Result<Box<dyn MemberAccount>> {
        Err(InvalidInput(format!("'{provider}' has no member account")).into())
    }

    fn view(&self, _: &str, _: &BTreeMap<String, String>, _: &dyn Fn(&str) -> bool) -> Value {
        Value::Object(Map::new())
    }

    fn credential(&self, _: &str, _: &SavedMember) -> Option<HostCredential> {
        None
    }

    fn removed<'a>(&'a self, _: &'a str, _: &'a SavedMember) -> Hook<'a> {
        Box::pin(async { Ok(()) })
    }

    fn avatar_changed<'a>(&'a self, _: &'a str, _: &'a SavedMember) -> Hook<'a> {
        Box::pin(async { Ok(()) })
    }

    fn channel(&self, _: &Value, reference: &str) -> anyhow::Result<Map<String, Value>> {
        let mut chat = Map::new();
        chat.insert("channel_name".into(), reference.into());
        Ok(chat)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn provider_registry_canonicalizes_names_and_keeps_unmodelled_items() {
        assert_eq!(
            Registry
                .service(ServiceKind::ChatService, &json!({"name": "Slack"}))
                .unwrap(),
            json!({"name": "slack"})
        );
        assert_eq!(
            Registry
                .service(
                    ServiceKind::TicketService,
                    &json!({"name": "Local", "tracker": "acme", "anything": [1, 2]})
                )
                .unwrap(),
            json!({"name": "local", "tracker": "acme", "anything": [1, 2]})
        );
    }

    #[test]
    fn provider_registry_refuses_what_it_does_not_serve() {
        for (kind, form) in [
            (ServiceKind::TicketService, json!({"name": "slack"})),
            (ServiceKind::TicketService, json!({"name": "jira"})),
            (ServiceKind::ChatService, json!({"name": "branvil"})),
            (ServiceKind::CodeHostingService, json!({})),
        ] {
            let error = Registry.service(kind, &form).unwrap_err();
            assert!(error.is::<InvalidInput>(), "{form}");
        }
        for provider in ["local", "branvil", "slack", "github"] {
            let error = Registry.account(provider, &json!({})).err().unwrap();
            assert!(error.is::<InvalidInput>(), "{provider}");
        }
        assert_eq!(
            Registry.channel(&json!({}), "C0123456789").unwrap(),
            json!({"channel_name": "C0123456789"})
                .as_object()
                .unwrap()
                .clone()
        );
    }
}
