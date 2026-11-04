//! Slack Web API and Socket Mode adapters. Credentials are resolved by the factory.
mod events;
mod listener;
#[cfg(test)]
mod tests;

use events::*;
use gb_ports::*;
pub(super) use listener::SlackListener;
use regex::{Captures, Regex};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    sync::{Arc, LazyLock, Mutex},
    time::Duration,
};

/// Provider observations contain classifications and counters, never credentials.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IntegrationEvent {
    ReceiveRecordSkipped {
        provider: &'static str,
        code: &'static str,
    },
    CredentialFailed {
        provider: &'static str,
        code: &'static str,
    },
    ReceiveConnected {
        provider: &'static str,
    },
    ReceiveDisconnected {
        provider: &'static str,
    },
    ReceiveAuthFailed {
        provider: &'static str,
        code: &'static str,
        member: String,
    },
    ReceiveReconnecting {
        provider: &'static str,
        attempt: u64,
    },
    ReceiveStopped {
        provider: &'static str,
        connect_attempts: u64,
        reconnects: u64,
        received_events: u64,
        acks_sent: u64,
    },
    AwaitedChecksReadFailed {
        provider: String,
    },
}
pub type IntegrationObserver = Arc<dyn Fn(IntegrationEvent) + Send + Sync>;

pub(super) const BASE_URL: &str = "https://slack.com/api";
pub(super) type Form = BTreeMap<String, String>;

/// The transport returns only classifications, never a URL, response body or token.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum SlackFailure {
    Unavailable,
    InvalidResult,
    Api,
    ApiCode(&'static str),
    Auth(&'static str),
    ThreadMissing,
    AlreadyReacted,
    MissingCredential(String),
    MissingScope { needed: String, provided: String },
}
impl SlackFailure {
    fn port(self) -> ServiceError {
        match self {
            Self::Unavailable => ServiceError::Unavailable,
            Self::InvalidResult => ServiceError::InvalidResult,
            Self::ThreadMissing => ServiceError::NotFound,
            Self::MissingCredential(name) => ServiceError::MissingCredential { name },
            Self::MissingScope { needed, provided } => ServiceError::Diagnostic {
                key: "integrations.slack.chat_service.missing_scope",
                args: [("needed".into(), needed), ("provided".into(), provided)].into(),
            },
            _ => ServiceError::Failed,
        }
    }
}
pub(super) type SlackFuture<'a, T> =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<T, SlackFailure>> + Send + 'a>>;
pub(super) trait SlackTransport: Send + Sync {
    fn post<'a>(
        &'a self,
        base: &'a str,
        method: &'a str,
        token: &'a str,
        form: Form,
    ) -> SlackFuture<'a, Value>;
}
pub(super) struct WebTransport(reqwest::Client);
impl WebTransport {
    pub(super) fn new() -> Result<Self, ServiceError> {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .map(Self)
            .map_err(|_| ServiceError::Unavailable)
    }
}
impl SlackTransport for WebTransport {
    fn post<'a>(
        &'a self,
        base: &'a str,
        method: &'a str,
        token: &'a str,
        form: Form,
    ) -> SlackFuture<'a, Value> {
        Box::pin(async move {
            let response = self
                .0
                .post(format!("{base}/{method}"))
                .bearer_auth(token)
                .form(&form)
                .send()
                .await
                .map_err(|e| {
                    if e.is_connect() || e.is_builder() {
                        SlackFailure::Unavailable
                    } else {
                        SlackFailure::Api
                    }
                })?;
            if response.status() == reqwest::StatusCode::UNAUTHORIZED {
                return Err(SlackFailure::Auth("unauthorized"));
            }
            if !response.status().is_success() {
                return Err(SlackFailure::Api);
            }
            response
                .json()
                .await
                .map_err(|_| SlackFailure::InvalidResult)
        })
    }
}
pub(super) fn answer(payload: Value) -> Result<Value, SlackFailure> {
    if !payload.is_object() {
        return Err(SlackFailure::InvalidResult);
    }
    if payload["ok"] == true {
        return Ok(payload);
    }
    Err(match text(&payload, "error") {
        "invalid_auth" => SlackFailure::Auth("invalid_auth"),
        "not_authed" => SlackFailure::Auth("not_authed"),
        "token_revoked" => SlackFailure::Auth("token_revoked"),
        "token_expired" => SlackFailure::Auth("token_expired"),
        "account_inactive" => SlackFailure::Auth("account_inactive"),
        "no_permission" => SlackFailure::Auth("no_permission"),
        "not_allowed_token_type" => SlackFailure::Auth("not_allowed_token_type"),
        "thread_not_found" => SlackFailure::ThreadMissing,
        "already_reacted" => SlackFailure::AlreadyReacted,
        "missing_scope" => SlackFailure::MissingScope {
            needed: safe_scopes(text(&payload, "needed")),
            provided: safe_scopes(text(&payload, "provided")),
        },
        "ratelimited" => SlackFailure::ApiCode("ratelimited"),
        "team_access_not_granted" => SlackFailure::ApiCode("team_access_not_granted"),
        _ => SlackFailure::Api,
    })
}
/// OAuth scope grammar excludes tokens, URLs, controls and arbitrary error text.
fn safe_scopes(value: &str) -> String {
    if value.len() > 2048 {
        return String::new();
    }
    value
        .split(',')
        .take(32)
        .map(str::trim)
        .filter(|scope| {
            if *scope == "identify" {
                return true;
            }
            scope.len() <= 64
                && scope.contains(':')
                && scope.split(':').all(|part| {
                    !part.is_empty() && part.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
                })
        })
        .collect::<Vec<_>>()
        .join(",")
}
pub(super) struct SlackChatService {
    member: String,
    token: String,
    token_name: String,
    app_token: String,
    base: String,
    transport: Arc<dyn SlackTransport>,
    channels: Mutex<BTreeMap<String, String>>,
    observer: IntegrationObserver,
}
impl SlackChatService {
    pub(super) fn new(
        token: String,
        app_token: String,
        base: String,
        transport: Arc<dyn SlackTransport>,
        observer: IntegrationObserver,
    ) -> Self {
        Self {
            member: String::new(),
            token,
            token_name: "SLACK_BOT_TOKEN".into(),
            app_token,
            base: base.trim_end_matches('/').into(),
            transport,
            channels: Mutex::new(BTreeMap::new()),
            observer,
        }
    }
    pub(super) fn with_member(mut self, member: String) -> Self {
        self.member = member;
        self
    }
    pub(super) fn with_token_name(mut self, name: String) -> Self {
        self.token_name = name;
        self
    }
    async fn post(&self, method: &str, form: Form) -> Result<Value, SlackFailure> {
        if self.token.is_empty() {
            return Err(SlackFailure::MissingCredential(self.token_name.clone()));
        }
        let result = match self
            .transport
            .post(&self.base, method, &self.token, form)
            .await
        {
            Ok(payload) => answer(payload),
            Err(error) => Err(error),
        };
        if let Err(SlackFailure::Auth(code)) = &result {
            (self.observer)(IntegrationEvent::CredentialFailed {
                provider: "slack",
                code,
            });
        }
        result
    }
}
fn form(items: &[(&str, &str)]) -> Form {
    items
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}
fn named(payload: &Value, key: &str) -> Result<String, ServiceError> {
    let v = text(payload, key);
    if v.is_empty() {
        Err(ServiceError::InvalidResult)
    } else {
        Ok(v.into())
    }
}
fn listing<'a>(
    payload: &'a Value,
    key: &str,
) -> Result<(&'a Vec<Value>, Option<String>), ServiceError> {
    let list = payload[key]
        .as_array()
        .filter(|v| v.iter().all(Value::is_object))
        .ok_or(ServiceError::InvalidResult)?;
    if payload
        .get("response_metadata")
        .is_some_and(|m| !m.is_object())
    {
        return Err(ServiceError::InvalidResult);
    }
    let cursor = text(&payload["response_metadata"], "next_cursor");
    Ok((list, (!cursor.is_empty()).then(|| cursor.into())))
}
fn page(channel: &str, payload: Value) -> Result<ChatEventPage, ServiceError> {
    let (messages, cursor) = listing(&payload, "messages")?;
    let mut events = Vec::new();
    for raw in messages {
        if let Some(e) = event(channel, raw)? {
            events.push(e);
        }
    }
    Ok(ChatEventPage { events, cursor })
}
impl ChatService for SlackChatService {
    fn get_user_avatar_url<'a>(&'a self, user: &'a str) -> ServiceFuture<'a, String> {
        Box::pin(async move {
            let payload = self
                .post("users.info", form(&[("user", user)]))
                .await
                .map_err(SlackFailure::port)?;
            for key in ["image_512", "image_192", "image_72", "image_original"] {
                let image = text(&payload["user"]["profile"], key);
                if !image.is_empty() {
                    return Ok(image.into());
                }
            }
            Err(ServiceError::InvalidResult)
        })
    }
    fn probe_read_scopes(&self) -> ServiceFuture<'_, ()> {
        Box::pin(async move {
            self.post(
                "conversations.list",
                form(&[
                    ("limit", "1"),
                    ("exclude_archived", "true"),
                    ("types", "public_channel,private_channel"),
                ]),
            )
            .await
            .map(|_| ())
            .map_err(SlackFailure::port)
        })
    }
    fn get_bot_identity(&self) -> ServiceFuture<'_, ChatIdentity> {
        Box::pin(async move {
            let p = self
                .post("auth.test", Form::new())
                .await
                .map_err(SlackFailure::port)?;
            Ok(ChatIdentity {
                user_id: named(&p, "user_id")?,
                display_name: text(&p, "user").into(),
                workspace: text(&p, "team").into(),
            })
        })
    }
    fn check_credentials(&self) -> ServiceFuture<'_, Vec<CredentialCheck>> {
        Box::pin(async move {
            let mut checks = Vec::new();
            for (name, token, method) in [
                ("bot_token", self.token.as_str(), "auth.test"),
                (
                    "app_token",
                    self.app_token.as_str(),
                    "apps.connections.open",
                ),
            ] {
                let result = if token.is_empty() {
                    None
                } else {
                    Some(
                        async {
                            let p = if name == "bot_token" {
                                self.post(method, Form::new()).await?
                            } else {
                                answer(
                                    self.transport
                                        .post(&self.base, method, token, Form::new())
                                        .await?,
                                )?
                            };
                            if name == "bot_token" && named(&p, "user_id").is_err() {
                                return Err(SlackFailure::InvalidResult);
                            }
                            Ok(())
                        }
                        .await,
                    )
                };
                let mut error_args = BTreeMap::new();
                let (status, error) = match result {
                    None => (CredentialStatus::Unconfigured, ""),
                    Some(Ok(())) => (CredentialStatus::Ok, ""),
                    Some(Err(SlackFailure::Auth(code))) => {
                        error_args.insert("member".into(), self.member.clone());
                        error_args.insert("code".into(), code.into());
                        error_args.insert(
                            "name".into(),
                            if name == "bot_token" {
                                self.token_name.clone()
                            } else {
                                self.token_name.strip_suffix("BOT_TOKEN").map_or_else(
                                    || "SLACK_APP_TOKEN".into(),
                                    |prefix| format!("{prefix}APP_TOKEN"),
                                )
                            },
                        );
                        (
                            CredentialStatus::Failed,
                            "integrations.slack.chat_service.authentication_failed",
                        )
                    }
                    Some(Err(SlackFailure::ApiCode(code))) => {
                        error_args.insert("code".into(), code.into());
                        (
                            CredentialStatus::Failed,
                            "integrations.slack.chat_service.api_failed",
                        )
                    }
                    Some(Err(SlackFailure::MissingScope { needed, provided })) => {
                        error_args.insert("needed".into(), needed);
                        error_args.insert("provided".into(), provided);
                        (
                            CredentialStatus::Failed,
                            "integrations.slack.chat_service.missing_scope",
                        )
                    }
                    Some(Err(_)) => (
                        CredentialStatus::Failed,
                        "integrations.slack.chat_service.request_failed",
                    ),
                };
                checks.push(CredentialCheck {
                    name: name.into(),
                    status,
                    error: error.into(),
                    error_args,
                });
            }
            Ok(checks)
        })
    }
    fn self_user_id(&self, account: &ActorName) -> String {
        account.0.trim().into()
    }
    fn parse_message_url(&self, url: &str) -> Result<ChatMessageRef, ServiceError> {
        let parsed = reqwest::Url::parse(url)
            .or_else(|_| {
                reqwest::Url::parse("https://slack.invalid/")
                    .unwrap()
                    .join(url)
            })
            .map_err(|_| ServiceError::NotFound)?;
        let parts: Vec<_> = parsed.path().trim_matches('/').split('/').collect();
        if parts.len() < 3 || parts[parts.len() - 3] != "archives" {
            return Err(ServiceError::NotFound);
        }
        let digits = parts[parts.len() - 1]
            .strip_prefix('p')
            .unwrap_or(parts[parts.len() - 1]);
        if digits.len() <= 6 || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Err(ServiceError::NotFound);
        }
        let ts = format!(
            "{}.{}",
            &digits[..digits.len() - 6],
            &digits[digits.len() - 6..]
        );
        let thread = parsed
            .query_pairs()
            .find(|(k, v)| k == "thread_ts" && !v.is_empty())
            .map(|(_, v)| v.into_owned())
            .unwrap_or_else(|| ts.clone());
        Ok(ChatMessageRef {
            channel_id: parts[parts.len() - 2].into(),
            message_id: ts,
            thread_id: thread,
        })
    }
    fn mentioned_user_ids(&self, text: &str) -> Vec<String> {
        mentions(text)
    }
    fn list_channel_events<'a>(
        &'a self,
        channel: &'a str,
        q: ChannelEventsQuery,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            let mut f = form(&[
                ("channel", channel),
                ("limit", &q.limit.to_string()),
                ("include_all_metadata", "true"),
                ("inclusive", "true"),
            ]);
            if let Some(v) = q.cursor {
                f.insert("cursor".into(), v);
            }
            if let Some(v) = q.since {
                f.insert("oldest".into(), time_ts(v));
            }
            if let Some(v) = q.until {
                f.insert("latest".into(), time_ts(v));
            }
            page(
                channel,
                self.post("conversations.history", f)
                    .await
                    .map_err(SlackFailure::port)?,
            )
        })
    }
    fn list_thread_events<'a>(
        &'a self,
        channel: &'a str,
        thread: &'a str,
        cursor: Option<String>,
        limit: usize,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            let mut f = form(&[
                ("channel", channel),
                ("ts", thread),
                ("limit", &limit.to_string()),
                ("include_all_metadata", "true"),
            ]);
            if let Some(v) = cursor {
                f.insert("cursor".into(), v);
            }
            page(
                channel,
                self.post("conversations.replies", f)
                    .await
                    .map_err(SlackFailure::port)?,
            )
        })
    }
    fn resolve_channel_id<'a>(&'a self, name: &'a str) -> ServiceFuture<'a, Option<String>> {
        Box::pin(async move {
            let name = name.trim().trim_start_matches('#');
            if name.is_empty() {
                return Ok(None);
            }
            if let Some(id) = self.channels.lock().unwrap().get(name).cloned() {
                return Ok(Some(id));
            }
            let mut cursor = None;
            let mut seen = std::collections::BTreeSet::new();
            loop {
                let mut f = form(&[
                    ("limit", "1000"),
                    ("exclude_archived", "true"),
                    ("types", "public_channel,private_channel"),
                ]);
                if let Some(c) = cursor {
                    f.insert("cursor".into(), c);
                }
                let p = self
                    .post("conversations.list", f)
                    .await
                    .map_err(SlackFailure::port)?;
                let (channels, next) = listing(&p, "channels")?;
                for c in channels {
                    let id = text(c, "id");
                    if !id.is_empty()
                        && [text(c, "name"), text(c, "name_normalized")].contains(&name)
                    {
                        self.channels.lock().unwrap().insert(name.into(), id.into());
                        return Ok(Some(id.into()));
                    }
                }
                let Some(c) = next else {
                    return Ok(None);
                };
                if !seen.insert(c.clone()) {
                    return Err(ServiceError::InvalidResult);
                }
                cursor = Some(c);
            }
        })
    }
    fn post_message<'a>(
        &'a self,
        channel: &'a str,
        body: &'a str,
        thread: Option<&'a str>,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, ChatPostResult> {
        Box::pin(async move {
            let mut f = form(&[("channel", channel), ("text", body)]);
            if let Some(t) = thread.filter(|t| !t.is_empty()) {
                f.insert("thread_ts".into(), t.into());
            }
            if let Some(s) = status {
                f.insert("metadata".into(), metadata(s).to_string());
            }
            let p = self
                .post("chat.postMessage", f)
                .await
                .map_err(SlackFailure::port)?;
            let ts = named(&p, "ts")?;
            let occurred_at = timestamp(&ts)?;
            Ok(ChatPostResult {
                channel_id: channel.into(),
                thread_id: thread.filter(|t| !t.is_empty()).unwrap_or(&ts).into(),
                message_id: ts,
                occurred_at,
            })
        })
    }
    fn add_reaction<'a>(
        &'a self,
        channel: &'a str,
        message: &'a str,
        reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            let name = match reaction {
                SemanticReaction::Ack => "white_check_mark",
                SemanticReaction::Agree => "thumbsup",
                SemanticReaction::Celebrate => "tada",
                SemanticReaction::Support => "heart",
            };
            match self
                .post(
                    "reactions.add",
                    form(&[("channel", channel), ("timestamp", message), ("name", name)]),
                )
                .await
            {
                Ok(_) | Err(SlackFailure::AlreadyReacted) => Ok(()),
                Err(e) => Err(e.port()),
            }
        })
    }
    fn normalize_participant_text(&self, text: &str, labels: &BTreeMap<String, String>) -> String {
        MENTION
            .replace_all(text, |c: &Captures<'_>| {
                format!(
                    "@{}",
                    labels
                        .get(&c[1])
                        .map(String::as_str)
                        .unwrap_or("participant")
                )
            })
            .into_owned()
    }
    fn render_participant_text(&self, text: &str, labels: &BTreeMap<String, String>) -> String {
        static LABEL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"@([A-Za-z0-9_-]+)").unwrap());
        static EPHEMERAL: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(?i)^(?:user|agent)_\d+$").unwrap());
        LABEL
            .replace_all(text, |c: &Captures<'_>| {
                let label = &c[1];
                if labels
                    .values()
                    .any(|v| v.eq_ignore_ascii_case(label) && EPHEMERAL.is_match(v))
                {
                    return label.into();
                }
                labels
                    .iter()
                    .rev()
                    .find(|(id, v)| {
                        !id.is_empty()
                            && !v.is_empty()
                            && v.eq_ignore_ascii_case(label)
                            && !EPHEMERAL.is_match(v)
                    })
                    .map(|(id, _)| format!("<@{id}>"))
                    .unwrap_or_else(|| c[0].into())
            })
            .into_owned()
    }
}
