//! Slack registration and probes: no credential values reach responses or logs.
use super::*;
const APPS: &str = "https://api.slack.com/apps";
#[derive(Deserialize)]
pub(crate) struct Registration {
    app_name: String,
}
pub(crate) async fn registration(headers: HeaderMap, Body(input): Body<Registration>) -> Response {
    if input.app_name.is_empty() {
        return error(
            gb_setup::InvalidInput("app_name must not be empty".into()),
            &headers,
        );
    }
    let name = input.app_name.trim();
    if name.is_empty() || name.chars().count() > 35 {
        return error(
            gb_setup::SetupError::new(
                "invalid_slack_app_name",
                "integrations.slack.setup.invalid_app_name",
            ),
            &headers,
        );
    }
    let manifest = manifest(name);
    let encoded =
        url::form_urlencoded::byte_serialize(manifest.to_string().as_bytes()).collect::<String>();
    Json(json!({"app_name":name,"registration_url":format!("{APPS}?new_app=1&manifest_json={encoded}"),"app_directory_url":APPS})).into_response()
}
fn manifest(name: &str) -> Value {
    let folded = regex::Regex::new("[^a-z0-9._-]+")
        .expect("display name grammar")
        .replace_all(&name.trim().to_lowercase(), "-")
        .trim_matches('-')
        .chars()
        .take(80)
        .collect::<String>();
    let display = if folded.is_empty() {
        "guildbotics"
    } else {
        &folded
    };
    json!({"display_information":{"name":name},"features":{"bot_user":{"display_name":display,"always_online":true}},"oauth_config":{"scopes":{"bot":["chat:write","reactions:write","channels:history","groups:history","im:history","mpim:history","channels:read","groups:read","users:read"]}},"settings":{"socket_mode_enabled":true,"event_subscriptions":{"bot_events":["message.channels","message.groups","message.im","message.mpim"]},"org_deploy_enabled":false,"token_rotation_enabled":false}})
}
#[derive(Default, Deserialize)]
pub(crate) struct TokenCheck {
    #[serde(default)]
    bot_token: String,
    #[serde(default)]
    app_token: String,
    #[serde(default)]
    person_id: String,
    #[serde(default)]
    channels: Vec<String>,
}
fn effective(entered: String, stored: &str) -> (String, &'static str) {
    if !entered.trim().is_empty() {
        (entered.trim().into(), "input")
    } else if !stored.trim().is_empty() {
        (stored.trim().into(), "stored")
    } else {
        (String::new(), "none")
    }
}
async fn saved(
    api: &Shared,
    workspace: Option<std::path::PathBuf>,
    id: &str,
) -> anyhow::Result<Option<gb_setup::SavedMember>> {
    let Some(workspace) = workspace else {
        return Ok(None);
    };
    let id = id.to_owned();
    let identity = api.identity_root.clone();
    crate::config_api::with_setup(api, workspace, move |setup| {
        let members = setup.list_members()?;
        for member in &members {
            if gb_config::team::person_secret_prefix(&identity, &member.person_id).is_err() {
                return Err(gb_setup::SetupError::new(
                    "member_config_invalid",
                    "app_api.errors.member_config_invalid",
                )
                .into());
            }
        }
        if !members.iter().any(|m| m.person_id == id) {
            return Ok(None);
        }
        setup.saved_member(&id).map(Some)
    })
    .await
}
fn base_url(workspace: Option<&std::path::Path>) -> String {
    workspace
        .and_then(|w| std::fs::read_to_string(w.join(".guildbotics/config/team/project.yml")).ok())
        .and_then(|s| gb_config::yaml_value(&s).ok())
        .and_then(|v| {
            v["services"]["chat_service"]["base_url"]
                .as_str()
                .map(str::to_owned)
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "https://slack.com/api".into())
}
struct Probe {
    http: reqwest::Client,
    base: String,
}
impl Probe {
    fn new(base: String) -> Result<Self, gb_setup::SetupError> {
        Ok(Self {
            base,
            http: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(10))
                .build()
                .map_err(|_| {
                    gb_setup::SetupError::new(
                        "avatar_import_failed",
                        "app_api.errors.avatar_import_failed",
                    )
                })?,
        })
    }
    async fn call(
        &self,
        method: &str,
        token: &str,
        form: &[(&str, &str)],
    ) -> Result<Value, (String, String)> {
        let bad = || (String::from("unreachable"), String::new());
        let response = self
            .http
            .post(format!("{}/{method}", self.base.trim_end_matches('/')))
            .bearer_auth(token)
            .form(form)
            .send()
            .await
            .map_err(|_| bad())?;
        if !response.status().is_success() {
            return Err(bad());
        }
        let value: Value = response.json().await.map_err(|_| bad())?;
        if value["ok"] == true {
            return Ok(value);
        }
        let code = value["error"].as_str().unwrap_or("unknown_error");
        let code = if code.len() <= 80 && code.bytes().all(|c| c.is_ascii_lowercase() || c == b'_')
        {
            code
        } else {
            "unknown_error"
        };
        let needed = value["needed"].as_str().unwrap_or("");
        let needed = if needed.len() < 2048
            && needed
                .bytes()
                .all(|c| c.is_ascii_lowercase() || b"_:, ".contains(&c))
        {
            needed
        } else {
            ""
        };
        Err((code.into(), needed.into()))
    }
}
pub(crate) async fn verify(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(input): Body<TokenCheck>,
) -> Response {
    let workspace = api.selected().workspace.clone();
    let stored = saved(&api, workspace.clone(), &input.person_id)
        .await
        .ok()
        .flatten();
    let bot = stored
        .as_ref()
        .map(|m| m.secret("SLACK_BOT_TOKEN"))
        .unwrap_or("");
    let app = stored
        .as_ref()
        .map(|m| m.secret("SLACK_APP_TOKEN"))
        .unwrap_or("");
    match Probe::new(base_url(workspace.as_deref())) {
        Ok(probe) => Json(verify_tokens(&probe, input, bot, app).await).into_response(),
        Err(e) => error(e, &headers),
    }
}
async fn verify_tokens(
    probe: &Probe,
    input: TokenCheck,
    stored_bot: &str,
    stored_app: &str,
) -> Value {
    let (bot, bot_source) = effective(input.bot_token, stored_bot);
    let (app, app_source) = effective(input.app_token, stored_app);
    let mut result = json!({"bot_ok":false,"bot_user_id":"","bot_display_name":"","workspace":"","bot_error":"","bot_source":bot_source,"scopes_ok":false,"scope_error":"","scope_needed":"","app_token_ok":false,"app_token_error":"","app_token_source":app_source,"channels":[]});
    for (token, prefix, field, method) in [
        (&bot, "xoxb-", "bot_error", "auth.test"),
        (&app, "xapp-", "app_token_error", "apps.connections.open"),
    ] {
        if token.is_empty() {
            result[field] = "missing".into();
            continue;
        }
        if !token.starts_with(prefix) {
            result[field] = "wrong_token_type".into();
            continue;
        }
        match probe.call(method, token, &[]).await {
            Err((code, _)) => result[field] = code.into(),
            Ok(answer) => {
                if field == "bot_error" {
                    result["bot_ok"] = true.into();
                    for (to, from) in [
                        ("bot_user_id", "user_id"),
                        ("bot_display_name", "user"),
                        ("workspace", "team"),
                    ] {
                        result[to] = answer[from].as_str().unwrap_or("").into();
                    }
                    match probe
                        .call(
                            "conversations.list",
                            token,
                            &[
                                ("limit", "1"),
                                ("exclude_archived", "true"),
                                ("types", "public_channel,private_channel"),
                            ],
                        )
                        .await
                    {
                        Ok(_) => result["scopes_ok"] = true.into(),
                        Err((code, needed)) => {
                            result["scope_error"] = code.into();
                            result["scope_needed"] = needed.into();
                        }
                    }
                } else {
                    result["app_token_ok"] = true.into();
                }
            }
        }
    }
    if result["scopes_ok"] == true {
        let mut channels = vec![];
        for name in input
            .channels
            .iter()
            .map(|n| n.trim())
            .filter(|n| !n.is_empty())
        {
            let outcome = channel(probe, &bot, name).await;
            channels.push(json!({"channel":name,"ok":outcome.is_ok(),"error":outcome.err().unwrap_or_default()}));
        }
        result["channels"] = channels.into();
    }
    result
}
async fn resolve_id(probe: &Probe, token: &str, name: &str) -> Result<String, String> {
    let reference = name.trim_start_matches('#');
    let id = if regex::Regex::new(r"^[CGD][A-Z0-9]{8,}$")
        .expect("channel ID")
        .is_match(reference)
    {
        reference.into()
    } else {
        let mut cursor = String::new();
        loop {
            let value = probe
                .call(
                    "conversations.list",
                    token,
                    &[
                        ("limit", "200"),
                        ("types", "public_channel,private_channel"),
                        ("exclude_archived", "true"),
                        ("cursor", &cursor),
                    ],
                )
                .await
                .map_err(|(code, _)| code)?;
            if let Some(id) = value["channels"]
                .as_array()
                .into_iter()
                .flatten()
                .find(|c| c["name"] == reference)
                .and_then(|c| c["id"].as_str())
            {
                break id.to_owned();
            }
            let next = value["response_metadata"]["next_cursor"]
                .as_str()
                .unwrap_or("");
            if next.is_empty() || next == cursor {
                return Err("not_found".into());
            }
            cursor = next.into();
        }
    };
    Ok(id)
}
async fn channel(probe: &Probe, token: &str, name: &str) -> Result<(), String> {
    let id = resolve_id(probe, token, name).await?;
    probe
        .call(
            "conversations.history",
            token,
            &[("channel", &id), ("limit", "1")],
        )
        .await
        .map(|_| ())
        .map_err(|(code, _)| code)
}

fn avatar_error() -> gb_setup::SetupError {
    let mut e = gb_setup::SetupError::new(
        "avatar_import_failed",
        "app_api.errors.avatar_import_failed",
    )
    .with("provider", "Slack");
    e.status = 500;
    e
}
fn avatar_setup(code: &str, key: &str) -> gb_setup::SetupError {
    gb_setup::SetupError::new(code, &format!("integrations.slack.setup.{key}"))
}
pub(crate) async fn avatar(
    State(api): State<Shared>,
    headers: HeaderMap,
    axum::extract::Path((id, provider)): axum::extract::Path<(String, String)>,
) -> Response {
    use crate::api::ApiError;
    if !gb_config::team::valid_person_id(&id) {
        return error(
            gb_setup::InvalidInput("person_id is not a member ID".into()),
            &headers,
        );
    }
    if provider != "slack" {
        return ApiError::new(StatusCode::NOT_FOUND, "avatar_provider_unknown", &headers)
            .into_response();
    }
    if gb_config::team::person_secret_prefix(&api.identity_root, &id).is_err() {
        return ApiError::new(StatusCode::BAD_REQUEST, "member_config_invalid", &headers)
            .into_response();
    }
    let workspace = api.selected().workspace.clone();
    let member = match saved(&api, workspace.clone(), &id).await {
        Ok(Some(m)) => m,
        Err(e) => return error(e, &headers),
        Ok(None) => {
            return ApiError::new(StatusCode::BAD_REQUEST, "person_not_found", &headers)
                .into_response();
        }
    };
    let action = async {
        let user = member
            .account_info
            .get("slack_user_id")
            .cloned()
            .unwrap_or_default();
        if member.human && user.is_empty() {
            return Err(avatar_setup("avatar_account_missing", "user_id_missing").into());
        }
        let mut token = member.secret("SLACK_BOT_TOKEN").to_owned();
        if token.is_empty()
            && member.human
            && let Some(workspace) = workspace.clone()
        {
            let ids = crate::config_api::with_setup(&api, workspace.clone(), |setup| {
                setup.list_members()
            })
            .await?;
            for m in ids {
                if let Ok(Some(m)) = saved(&api, Some(workspace.clone()), &m.person_id).await
                    && !m.secret("SLACK_BOT_TOKEN").is_empty()
                {
                    token = m.secret("SLACK_BOT_TOKEN").into();
                    break;
                }
            }
        }
        if token.is_empty() {
            return Err(avatar_setup("avatar_token_missing", "token_missing").into());
        }
        let probe = Probe::new(base_url(workspace.as_deref()))?;
        let user = if user.is_empty() {
            probe
                .call("auth.test", &token, &[])
                .await
                .map_err(|_| avatar_error())?["user_id"]
                .as_str()
                .unwrap_or("")
                .to_owned()
        } else {
            user
        };
        let profile = probe
            .call("users.info", &token, &[("user", &user)])
            .await
            .map_err(|(code, _)| {
                if code == "missing_scope" {
                    avatar_setup("avatar_missing_scope", "missing_scope")
                } else {
                    avatar_error()
                }
            })?;
        let url = ["image_512", "image_192", "image_72", "image_original"]
            .into_iter()
            .filter_map(|key| profile["user"]["profile"][key].as_str())
            .find(|s| !s.is_empty())
            .ok_or_else(avatar_error)?;
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(15))
            .build()
            .map_err(|_| avatar_error())?;
        let mut response = http
            .get(url)
            .send()
            .await
            .map_err(|_| avatar_error())?
            .error_for_status()
            .map_err(|_| avatar_error())?;
        let mime = response
            .headers()
            .get("content-type")
            .and_then(|h| h.to_str().ok())
            .unwrap_or("")
            .to_lowercase();
        let lower_url = url.to_lowercase();
        let suffix = if mime.contains("png") {
            "png"
        } else if mime.contains("jpeg") || mime.contains("jpg") {
            "jpg"
        } else if mime.contains("gif") {
            "gif"
        } else if mime.contains("webp") {
            "webp"
        } else {
            lower_url
                .split('?')
                .next()
                .and_then(|s| s.rsplit_once('.'))
                .map(|(_, s)| s)
                .filter(|s| ["png", "jpg", "jpeg", "gif", "webp"].contains(s))
                .unwrap_or("png")
        };
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| avatar_error())? {
            if bytes.len() + chunk.len() > gb_setup::AVATAR_LIMIT {
                return Err(
                    gb_setup::SetupError::new("avatar_invalid", "setup.avatar_too_large").into(),
                );
            }
            bytes.extend_from_slice(&chunk);
        }
        let workspace = workspace.clone().ok_or_else(avatar_error)?;
        let name = format!("avatar.{suffix}");
        let timestamp = crate::config_api::with_setup(&api, workspace, move |setup| {
            tokio::runtime::Handle::current().block_on(setup.change_avatar(&id, &bytes, &name))
        })
        .await?;
        Ok::<_, anyhow::Error>(timestamp)
    };
    match action.await {
        Ok(timestamp) => Json(json!({"avatar_timestamp":timestamp})).into_response(),
        Err(e) => error(e, &headers),
    }
}

pub(crate) async fn diagnose(
    api: Option<&Shared>,
    members: &[(&gb_setup::SavedMember, Vec<Value>)],
    lang: &str,
) -> Vec<crate::setup_providers::diagnostics::ProviderCheck> {
    use crate::setup_providers::diagnostics::ProviderCheck as Check;
    let selected = api.map(|api| api.selected());
    let base = base_url(
        selected
            .as_ref()
            .and_then(|selected| selected.workspace.as_deref()),
    );
    let probe = Probe::new(base);
    let mut checks = vec![];
    let mut subscribed = false;
    let message =
        |code: &str| crate::i18n::t(lang, &format!("integrations.slack.setup.{code}"), &[]);
    let user_id = regex::Regex::new(r"^[UW][A-Z0-9]{8,}$").expect("user ID");
    for (member, subscriptions) in members {
        if member.human {
            let user = member
                .account_info
                .get("slack_user_id")
                .map(|s| s.trim())
                .unwrap_or("");
            let (status, key) = if user.is_empty() {
                ("warning", "human_missing")
            } else if user_id.is_match(user) {
                ("ok", "human_configured")
            } else {
                ("error", "human_invalid")
            };
            checks.push(
                Check::new("slack", "human_slack_user_id", status, message(key))
                    .member(member, false)
                    .target(user),
            );
            continue;
        }
        if subscriptions.is_empty() {
            continue;
        }
        subscribed = true;
        let probe_ref = &probe;
        let call = |method, token, form| async move {
            match probe_ref {
                Ok(p) => p.call(method, token, form).await,
                Err(_) => Err(("unreachable".into(), String::new())),
            }
        };
        let app = member.secret("SLACK_APP_TOKEN");
        if app.is_empty() {
            checks.push(
                Check::new("slack", "slack_app_token", "error", message("app_missing"))
                    .member(member, true)
                    .target(&gb_config::team::person_secret_name(
                        &member.person_id,
                        "SLACK_APP_TOKEN",
                    )),
            );
        } else {
            let outcome = call("apps.connections.open", app, &[]).await;
            let mut c = Check::new(
                "slack",
                if outcome.is_ok() {
                    "slack_app_token"
                } else {
                    "slack_app_token_invalid"
                },
                if outcome.is_ok() { "ok" } else { "error" },
                message(if outcome.is_ok() {
                    "app_valid"
                } else {
                    "app_failed"
                }),
            )
            .member(member, true);
            if let Err((code, _)) = outcome {
                c.message = format!("{}: {code}", c.message);
                c.target =
                    gb_config::team::person_secret_name(&member.person_id, "SLACK_APP_TOKEN");
                c.context = json!({"error_type":if code=="unreachable"{"SlackUnreachableError"}else{"SlackApiError"}});
            }
            checks.push(c);
        }
        let bot_start = checks.len();
        match call("auth.test", member.secret("SLACK_BOT_TOKEN"), &[]).await {
            Err((code, _)) => {
                let mut c = Check::new(
                    "slack",
                    "slack_access",
                    "error",
                    format!("{}: {code}", message("bot_failed")),
                )
                .member(member, true);
                c.context = json!({"error_type":if code=="unreachable"{"SlackUnreachableError"}else{"SlackApiError"}});
                checks.push(c);
            }
            Ok(identity) => {
                let mut c = Check::new("slack", "slack_bot_auth", "ok", message("bot_valid"))
                    .member(member, true);
                c.context = json!({"bot_user_id":identity["user_id"].as_str().unwrap_or("")});
                checks.push(c);
                for sub in subscriptions {
                    let id = sub["channel_id"].as_str().unwrap_or("");
                    let name = sub["channel_name"].as_str().unwrap_or("");
                    let target = if id.is_empty() { name } else { id };
                    let mut resolved = String::new();
                    let outcome = match &probe {
                        Ok(p) => {
                            match resolve_id(p, member.secret("SLACK_BOT_TOKEN"), target).await {
                                Ok(id) => {
                                    resolved = id;
                                    p.call(
                                        "conversations.history",
                                        member.secret("SLACK_BOT_TOKEN"),
                                        &[("channel", &resolved), ("limit", "1")],
                                    )
                                    .await
                                    .map(|_| ())
                                    .map_err(|(code, _)| code)
                                }
                                Err(e) => Err(e),
                            }
                        }
                        Err(_) => Err("unreachable".into()),
                    };
                    let (code, key) = match outcome.as_ref().err().map(String::as_str) {
                        None => ("slack_channel_history", "channel_valid"),
                        Some("not_found") => ("slack_channel", "channel_missing"),
                        Some("not_in_channel") => {
                            ("slack_channel_not_joined", "channel_not_joined")
                        }
                        _ => ("slack_access", "bot_failed"),
                    };
                    let mut c = Check::new(
                        "slack",
                        code,
                        if outcome.is_ok() { "ok" } else { "error" },
                        message(key),
                    )
                    .member(member, code == "slack_access")
                    .target(target);
                    if code == "slack_channel_not_joined" {
                        c.context = json!({"channel_id":resolved,"channel":if name.is_empty(){target}else{name},"bot_name":identity["user"].as_str().filter(|s|!s.is_empty()).or_else(||identity["user_id"].as_str()).unwrap_or("")});
                    }
                    if code == "slack_access" {
                        checks.truncate(bot_start);
                        c.message = format!(
                            "{}: {}",
                            c.message,
                            outcome.as_ref().err().map(String::as_str).unwrap_or("")
                        );
                        c.context = json!({"error_type":if outcome.as_ref().err().is_some_and(|s|s=="unreachable"){"SlackUnreachableError"}else{"SlackApiError"}});
                        checks.push(c);
                        break;
                    }
                    checks.push(c);
                }
            }
        }
    }
    if !subscribed && members.iter().any(|(m, _)| !m.human) {
        checks.push(Check::new(
            "slack",
            "slack_not_configured",
            "ok",
            message("not_configured"),
        ));
    }
    checks
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn manifest_display_name_folds_punctuation_unicode_and_length() {
        for (input, expected) in [
            ("alice", "alice"),
            ("Alice Bot", "alice-bot"),
            ("guild.botics_1", "guild.botics_1"),
            ("  Team  Assistant  ", "team-assistant"),
            ("エージェント", "guildbotics"),
            ("---", "guildbotics"),
            (&"a".repeat(100), &"a".repeat(80)),
        ] {
            assert_eq!(
                manifest(input)["features"]["bot_user"]["display_name"],
                expected
            );
        }
    }
}
