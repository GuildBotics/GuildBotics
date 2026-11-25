//! The providers' part of a setup, as the server's registry knows it. The
//! service models of Branvil and Slack and Slack's member model are validated
//! with their setup screens; a provider without a model keeps the items its
//! form names.

use crate::api::Shared;
use gb_setup::{
    InvalidInput, MemberAccount, ProviderModels, SavedMember, ServiceKind, SetupError,
    StoredAccount,
};
use serde::Deserialize;
use serde_json::json;
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
    ("local", &[ServiceKind::ChatService]),
    ("slack", &[ServiceKind::ChatService]),
];

/// The secrets a member holds with each provider, by member-relative name.
const MEMBER_SECRETS: &[&str] = &["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"];

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
        if name == "branvil" {
            if form
                .as_object()
                .is_none_or(|m| m.keys().any(|k| k != "name"))
            {
                return Err(InvalidInput("unknown service field".into()).into());
            }
            return Ok(json!({"name":"branvil"}));
        }
        if name == "slack"
            && form
                .as_object()
                .is_none_or(|m| m.keys().any(|k| k != "name" && k != "base_url"))
        {
            return Err(InvalidInput("unknown service field".into()).into());
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
        vec!["slack".into()]
    }

    fn account_info_keys(&self, provider: &str) -> Vec<String> {
        match provider {
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
            "slack" => {
                json!({"user_id":info.get("slack_user_id").cloned().unwrap_or_default(),"has_bot_token":has("SLACK_BOT_TOKEN"),"has_app_token":has("SLACK_APP_TOKEN")})
            }
            _ => json!({}),
        }
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
}

pub(crate) fn message_for(e: branvil::api::AppError, operation: &str, lang: &str) -> String {
    match refused_for(e, operation).sentence {
        gb_setup::Sentence::Key(key, params) => {
            let args: Vec<_> = params
                .iter()
                .map(|(k, v)| (k.as_str(), v.as_str()))
                .collect();
            crate::i18n::t(lang, &key, &args)
        }
        gb_setup::Sentence::Text(text) => text,
    }
}
/// Branvil's refusal as a setup error with the shared diagnostic key.
pub(crate) fn refused_for(e: branvil::api::AppError, operation: &str) -> SetupError {
    let error = branvil::agent::error(e, operation);
    let mut result = SetupError::new("branvil", error.translation_key());
    if error.is_unavailable() {
        result.status = 503;
    }
    for (key, value) in error.translation_args() {
        result = result.with(&key, value);
    }
    result
}

#[cfg(test)]
mod refusal_tests {
    use super::*;
    use axum::http::StatusCode;

    #[test]
    fn branvil_errors_keep_shared_locale_keys_and_operation_parameters() {
        for (status, code, message, key) in [
            (
                401,
                "unauthorized",
                "No valid token or session.",
                "authentication",
            ),
            (404, "not_found", "missing workspace", "not_found"),
            (422, "invalid", "unsupported operation", "unsupported"),
            (409, "head_moved", "stale head", "head_moved"),
            (400, "label_undefined", "label absent", "label_undefined"),
            (
                503,
                "unavailable",
                "integrations.branvil.api.stopped",
                "stopped",
            ),
            (403, "forbidden", "owner required", "refused"),
            (500, "internal", "storage failed", "refused"),
            (503, "unavailable", "storage unavailable", "refused"),
        ] {
            let make = || {
                branvil::api::AppError::new(StatusCode::from_u16(status).unwrap(), code, message)
            };
            let error = refused_for(make(), "members_create");
            let gb_setup::Sentence::Key(actual_key, params) = &error.sentence else {
                panic!("Branvil errors must retain a translation key");
            };
            assert_eq!(actual_key, &format!("integrations.branvil.api.{key}"));
            assert_eq!(error.status, if status == 503 { 503 } else { 400 });
            if key == "refused" {
                assert!(params.contains(&("operation".into(), "members_create".into())));
                assert!(params.contains(&("status".into(), status.to_string())));
                assert!(params.contains(&("code".into(), code.into())));
                assert!(params.contains(&("message".into(), message.into())));
            }
            for lang in ["en", "ja"] {
                let args: Vec<_> = params
                    .iter()
                    .map(|(k, v)| (k.as_str(), v.as_str()))
                    .collect();
                let rendered = message_for(make(), "members_create", lang);
                assert_eq!(rendered, crate::i18n::t(lang, actual_key, &args));
                assert!(!rendered.contains("%{"));
                assert!(!rendered.contains("integrations.branvil.api."));
            }
        }
        let error = refused_for(branvil::api::AppError::invalid_cursor(), "page");
        assert_eq!(
            error.sentence,
            gb_setup::Sentence::Key("integrations.resources.continuation".into(), vec![])
        );
        for lang in ["en", "ja"] {
            assert_eq!(
                message_for(branvil::api::AppError::unauthorized(), "authenticate", lang),
                crate::i18n::t(lang, "integrations.branvil.api.authentication", &[])
            );
        }
    }
}
