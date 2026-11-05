//! Public member context and the canonical data-file reference.
use crate::{MemberError, Result};
use gb_ports::CodeHostingService;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use unicode_casefold::UnicodeCaseFold;

pub fn help() -> &'static str {
    include_str!("../../../guildbotics/templates/member/reference.md")
}
pub fn command_summary(command: &str) -> Result<String> {
    help()
        .lines()
        .filter_map(|line| {
            line.strip_prefix("- `guildbotics member ")?
                .split_once("` — ")
        })
        .find(|(usage, _)| {
            usage
                .split_whitespace()
                .take_while(|token| {
                    let mut bytes = token.bytes();
                    bytes.next().is_some_and(|b| b.is_ascii_lowercase())
                        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
                })
                .collect::<Vec<_>>()
                .join(" ")
                == command
        })
        .map(|(_, summary)| summary.to_owned())
        .ok_or(MemberError::Capability(
            "cli.member.catalog_command_missing",
        ))
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PublicMember {
    pub person_id: String,
    pub name: String,
    pub person_type: String,
    pub is_active: bool,
    pub roles: Value,
    pub profile: Value,
    pub speaking_style: String,
    pub accounts: BTreeMap<String, String>,
}
impl PublicMember {
    pub fn roster_entry(&self, providers: &[String]) -> Value {
        let mut services = serde_json::Map::new();
        for provider in providers {
            let account = if provider == "local" {
                format!("local-{}", self.person_id)
            } else {
                self.accounts.get(provider).cloned().unwrap_or_default()
            };
            let account = if provider == "branvil" {
                account.trim().case_fold().collect::<String>()
            } else {
                account.trim().to_owned()
            };
            if account.is_empty() {
                continue;
            }
            let mention = if provider == "slack" {
                format!("<@{account}>")
            } else {
                format!("@{account}")
            };
            services.insert(
                provider.clone(),
                json!({"names":[account],"mention":mention}),
            );
        }
        json!({"person_id":self.person_id,"name":self.name,"person_type":self.person_type,"services":services})
    }
    pub fn communication_style(&self) -> Value {
        let mut parts = Vec::new();
        let mut add = |label: &str, value: &str| {
            if !value.trim().is_empty() {
                parts.push(format!("- {label}: {}", value.trim()));
            }
        };
        add("Explicit speaking style", &self.speaking_style);
        let c = &self.profile["character"];
        add("Character archetype", c["archetype"].as_str().unwrap_or(""));
        for (key, label) in [
            ("traits", "Traits to reflect lightly"),
            ("interests", "Interests that can shape perspective"),
        ] {
            add(label, &strings(&c[key]).join(", "));
        }
        add(
            "Conversation contribution style",
            &strings(&c["conversation_preferences"]["contribution_style"]).join(", "),
        );
        let summaries = self
            .roles
            .as_object()
            .into_iter()
            .flat_map(|m| m.values())
            .filter_map(|v| v["summary"].as_str())
            .filter(|s| !s.trim().is_empty())
            .collect::<Vec<_>>()
            .join(", ");
        add("Role perspective", &summaries);
        let voice = if parts.is_empty() {
            "Use the member's configured profile and roles as the source of truth.".into()
        } else {
            parts.join("\n")
        };
        json!({"voice_basis":voice,
            "active_member_instruction":format!("Treat {} (person_id: {}) as the active GuildBotics member for this session until the user explicitly switches or clears the member. Do not ask the user to repeat the person id while this active member is established.",self.name,self.person_id),
            "interactive_replies":format!("Write interactive replies to the user as {}. Use the user's language, reflect the voice_basis lightly, and keep technical details accurate and actionable. Do not turn this into exaggerated roleplay.",self.name),
            "comments":format!("Write issue comments, PR conversation comments, inline review comments, and review thread replies in {}'s natural conversational voice, while staying clear about technical facts and next actions.",self.name),
            "neutral_documents":"Use the project's neutral document style for issue titles/bodies, PR titles/bodies, commit messages, and durable task summaries. The member's judgment may shape the content, but the prose should remain document-like.",
            "machine_outputs":"Do not apply the member voice to command output, command arguments, IDs, paths, logs, machine-readable JSON, workflow AgentResponse.message, or other control data. Keep those values factual and valid."})
    }
}
fn strings(value: &Value) -> Vec<&str> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect()
}
pub async fn member_context(
    member: &PublicMember,
    roster: &[PublicMember],
    providers: &[String],
    memory: Value,
    code: Option<&dyn CodeHostingService>,
    check_credentials: bool,
) -> Result<Value> {
    let mut result = json!({"person_id":member.person_id,"name":member.name,"person_type":member.person_type,
        "is_active":member.is_active,"roles":member.roles,"profile":member.profile,"speaking_style":member.speaking_style,
        "communication_style":member.communication_style(),"credential_status":"unchecked",
        "roster":roster.iter().map(|m|m.roster_entry(providers)).collect::<Vec<_>>(),"memory":memory,"capabilities":help()});
    if let Some(code) = code {
        result.as_object_mut().unwrap().extend(
            code.identity(check_credentials)
                .await?
                .into_iter()
                .map(|(k, v)| (k, v.into())),
        );
        if check_credentials {
            result["credential_status"] = "ok".into();
        }
    }
    Ok(result)
}
