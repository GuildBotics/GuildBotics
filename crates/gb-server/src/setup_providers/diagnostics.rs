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

    /// The secrets of the provider's connection, which every workspace's
    /// members use and the identities' store holds.
    pub fn identity_secret_keys(provider: &str) -> &'static [&'static str] {
        match provider {
            "branvil" => &["BRANVIL_ADMIN_TOKEN"],
            _ => &[],
        }
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
        if provider == "branvil" {
            keys.push((
                "branvil_username",
                "account_info.branvil_username".to_owned(),
                member
                    .account_info
                    .get("branvil_username")
                    .is_some_and(|s| !s.is_empty()),
            ));
            if !member.human {
                keys.push((
                    "BRANVIL_TOKEN",
                    gb_config::team::person_secret_name(&member.person_id, "BRANVIL_TOKEN"),
                    !member.secret("BRANVIL_TOKEN").is_empty(),
                ));
            }
        } else if provider == "slack" && !member.human && subscribed {
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
    /// Read Branvil itself, member identities and each distinct workspace.
    pub async fn diagnose_branvil(
        &self,
        members: &[SavedMember],
        services: &Value,
        lang: &str,
    ) -> Vec<ProviderCheck> {
        let scopes: Vec<_> = ["code_hosting_service", "ticket_service"]
            .into_iter()
            .filter_map(|k| {
                if services[k]["name"] == "branvil" {
                    Some(services[k]["workspace"].as_str().unwrap_or("").to_owned())
                } else {
                    None
                }
            })
            .collect();
        if scopes.is_empty() {
            return vec![];
        }
        let Some(data) = self.0.as_ref().and_then(|api| api.branvil.get()) else {
            return vec![ProviderCheck::new(
                "branvil",
                "branvil_reachable",
                "error",
                say(lang, "integrations.branvil.api.stopped", &[]),
            )];
        };
        let base = data.base_url();
        if let Err(e) = data.health() {
            return vec![
                ProviderCheck::new(
                    "branvil",
                    "branvil_reachable",
                    "error",
                    accounts::message_for(e, "health", lang),
                )
                .target(base),
            ];
        }
        let mut checks = vec![
            ProviderCheck::new(
                "branvil",
                "branvil_reachable",
                "ok",
                say(
                    lang,
                    "integrations.branvil.provider.reachable",
                    &[("base_url", base)],
                ),
            )
            .target(base),
        ];
        let owner = if members.iter().any(|m| m.human) {
            match self.admin().await {
                Ok(Some((_, m))) => m
                    .identity()
                    .await
                    .map(|m| m.name)
                    .map_err(|e| accounts::message_for(e, "identity", lang)),
                Ok(None) => Err(say(lang, "integrations.branvil.api.not_connected", &[])),
                Err(e) => Err(accounts::render(e, lang)),
            }
        } else {
            Ok(String::new())
        };
        let mut remaining: Vec<String> = vec![];
        for scope in scopes {
            if !remaining.contains(&scope) {
                remaining.push(scope);
            }
        }
        for m in members {
            let name = m
                .account_info
                .get("branvil_username")
                .map(String::as_str)
                .unwrap_or("");
            if m.human {
                let result = match &owner {
                    Ok(owner) if name.eq_ignore_ascii_case(owner) => Ok(say(
                        lang,
                        "integrations.branvil.provider.owner",
                        &[("owner", owner)],
                    )),
                    Ok(owner) => Err(say(
                        lang,
                        "integrations.branvil.provider.not_owner",
                        &[("name", name), ("owner", owner)],
                    )),
                    Err(e) => Err(e.clone()),
                };
                checks.push(
                    ProviderCheck::new(
                        "branvil",
                        "branvil_owner",
                        if result.is_ok() { "ok" } else { "error" },
                        result.unwrap_or_else(|e| e),
                    )
                    .member(m, true)
                    .target(name),
                );
                continue;
            }
            let access = async {
                if name.is_empty() {
                    return Err(say(lang, "integrations.branvil.api.username_missing", &[]));
                }
                if m.secret("BRANVIL_TOKEN").is_empty() {
                    return Err(say(
                        lang,
                        "integrations.branvil.api.token_missing",
                        &[(
                            "name",
                            &gb_config::team::person_secret_name(&m.person_id, "BRANVIL_TOKEN"),
                        )],
                    ));
                }
                let api = data
                    .authenticate(m.secret("BRANVIL_TOKEN").into())
                    .await
                    .map_err(|e| accounts::message_for(e, "authenticate", lang))?;
                let identity = api
                    .identity()
                    .await
                    .map_err(|e| accounts::message_for(e, "identity", lang))?;
                if !name.eq_ignore_ascii_case(&identity.name) {
                    return Err(say(
                        lang,
                        "integrations.branvil.api.identity_mismatch",
                        &[("name", &identity.name), ("username", name)],
                    ));
                }
                Ok(api)
            }
            .await;
            checks.push(
                ProviderCheck::new(
                    "branvil",
                    "branvil_access",
                    if access.is_ok() { "ok" } else { "error" },
                    access
                        .as_ref()
                        .map(|_| say(lang, "integrations.branvil.provider.access", &[]))
                        .unwrap_or_else(|e| e.clone()),
                )
                .member(m, true),
            );
            if let Ok(api) = access {
                for scope in remaining.drain(..) {
                    let result = api.repos_get_workspace(&scope).await;
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
                                .unwrap_or_else(|e| {
                                    accounts::message_for(e, "repos_get_workspace", lang)
                                }),
                        )
                        .target(&scope),
                    );
                }
            }
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
