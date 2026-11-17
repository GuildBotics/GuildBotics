//! The observations consumed by the diagnostics screen and credential alerts.
use super::*;
use serde::Serialize;
#[derive(Serialize)]
pub struct ProviderCheck {
    pub section: String,
    pub code: String,
    pub status: String,
    pub message: String,
    pub target: String,
    pub person_id: String,
    pub context: Value,
    pub credential: bool,
}
impl ProviderCheck {
    pub(crate) fn new(section: &str, code: &str, status: &str, message: String) -> Self {
        Self {
            section: section.into(),
            code: code.into(),
            status: status.into(),
            message,
            target: String::new(),
            person_id: String::new(),
            context: json!({}),
            credential: false,
        }
    }
    pub(crate) fn member(mut self, m: &SavedMember, credential: bool) -> Self {
        self.person_id = m.person_id.clone();
        self.credential = credential;
        self
    }
    pub(crate) fn target(mut self, target: &str) -> Self {
        self.target = target.into();
        self
    }
}
fn say(lang: &str, key: &str, args: &[(&str, &str)]) -> String {
    crate::i18n::t(lang, key, args)
}
impl Registry {
    /// The providers `project`'s services name, each once, in the
    /// registry's order.
    pub fn named(project: &Value) -> Vec<&'static str> {
        PROVIDERS
            .iter()
            .map(|(provider, _)| *provider)
            .filter(|provider| {
                project["services"].as_object().is_some_and(|services| {
                    services.values().any(|service| {
                        service["name"]
                            .as_str()
                            .is_some_and(|name| name.eq_ignore_ascii_case(provider))
                    })
                })
            })
            .collect()
    }

    /// The settings the provider requires before a service starts.
    pub fn verify(
        &self,
        provider: &str,
        member: &SavedMember,
        subscribed: bool,
        lang: &str,
    ) -> Vec<ProviderCheck> {
        let mut keys = vec![];
        if provider == "slack" && !member.human && subscribed {
            for key in ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"] {
                keys.push((
                    key,
                    gb_config::team::person_secret_name(&member.person_id, key),
                    !member.secret(key).is_empty(),
                ));
            }
        }
        keys.into_iter()
            .map(|(key, target, ok)| {
                let mut check = ProviderCheck::new(
                    provider,
                    &format!("{provider}_credential"),
                    if ok { "ok" } else { "error" },
                    say(
                        lang,
                        if ok {
                            "setup.provider_configured"
                        } else {
                            "setup.provider_unconfigured"
                        },
                        &[("target", &target)],
                    ),
                );
                check.target = target;
                check.context = json!({"person_id":member.person_id,"key":key});
                check
            })
            .collect()
    }
    /// Whether each distinct workspace the services name exists.
    pub async fn diagnose_branvil(&self, services: &Value, lang: &str) -> Vec<ProviderCheck> {
        let mut scopes: Vec<String> = vec![];
        for kind in ["code_hosting_service", "ticket_service"] {
            let scope = services[kind]["workspace"]
                .as_str()
                .unwrap_or("")
                .to_owned();
            if services[kind]["name"] == "branvil" && !scopes.contains(&scope) {
                scopes.push(scope);
            }
        }
        let Some(data) = self.0.as_ref().and_then(|api| api.branvil.get()) else {
            return scopes
                .iter()
                .map(|scope| {
                    ProviderCheck::new(
                        "branvil",
                        "branvil_workspace",
                        "error",
                        say(lang, "integrations.branvil.api.stopped", &[]),
                    )
                    .target(scope)
                })
                .collect();
        };
        let mut checks = vec![];
        for scope in scopes {
            let result = data.owner().repos_get_workspace(&scope).await;
            checks.push(
                ProviderCheck::new(
                    "branvil",
                    "branvil_workspace",
                    if result.is_ok() { "ok" } else { "error" },
                    result
                        .map(|_| {
                            say(
                                lang,
                                "integrations.branvil.provider.workspace",
                                &[("workspace", &scope)],
                            )
                        })
                        .unwrap_or_else(|e| message_for(e, "repos_get_workspace", lang)),
                )
                .target(&scope),
            );
        }
        checks
    }
}

impl Registry {
    /// Read-only Slack checks for the members and their chat subscriptions.
    pub async fn diagnose_slack(
        &self,
        members: &[(&SavedMember, Vec<Value>)],
        lang: &str,
    ) -> Vec<ProviderCheck> {
        crate::provider_api::slack::diagnose(self.0.as_ref(), members, lang).await
    }
}
