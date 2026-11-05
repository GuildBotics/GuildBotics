//! Native member command dispatch. Invocation authority is injected by the
//! host, rather than reconstructed from caller-controlled arguments or cwd.
use crate::{
    MemberError,
    chat::{Chat, Reference},
    command::{self, Parsed},
    git::Workspace,
    memory::{Edit, Memory, Scope},
    repository::Repository,
};
use gb_ports::*;
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    path::Path,
    time::{Duration, Instant},
};

pub trait Control: Send + Sync {
    fn command<'a>(
        &'a self,
        parsed: &'a Parsed,
        content: &'a str,
    ) -> crate::git::Pending<'a, Value>;
    fn record(&self, command: &str, read_only: bool, payload: &Value) -> crate::Result<()>;
}
pub struct Session<'a> {
    pub person_id: &'a str,
    pub person_type: &'a str,
    pub delegated_to: Option<&'a str>,
    pub workflow: bool,
    pub repository: Repository<'a>,
    pub chat: Option<Chat<'a>>,
    pub memory: Option<Memory<'a>>,
    pub git: Option<Workspace<'a>>,
    pub context: Value,
    pub control: &'a dyn Control,
    pub git_root: &'a Path,
}
#[derive(Default)]
pub struct Answer {
    pub payload: Value,
    pub text: Option<String>,
    pub format: String,
    pub download: Option<Vec<u8>>,
    pub download_metadata: Option<Value>,
}
fn value(value: impl Serialize) -> anyhow::Result<Value> {
    Ok(serde_json::to_value(value)?)
}
fn invalid(key: &'static str) -> anyhow::Error {
    MemberError::Capability(key).into()
}
fn decoded<T: serde::de::DeserializeOwned>(value: &str) -> anyhow::Result<T> {
    serde_json::from_value(json!(value)).map_err(|_| invalid("cli.member.option_choice"))
}
fn title(value: &str) -> anyhow::Result<String> {
    if value.trim().is_empty() || value.contains(['\n', '\r']) {
        Err(invalid("cli.member.title_invalid"))
    } else {
        Ok(value.trim().into())
    }
}
fn iso_time(value: &str) -> anyhow::Result<chrono::DateTime<chrono::Utc>> {
    use chrono::{Local, NaiveDate, NaiveDateTime, TimeZone};
    if let Ok(moment) = chrono::DateTime::parse_from_rfc3339(value) {
        return Ok(moment.to_utc());
    }
    let moment = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f"))
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M"))
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M"))
        .or_else(|_| {
            NaiveDate::parse_from_str(value, "%Y-%m-%d")
                .map(|date| date.and_hms_opt(0, 0, 0).unwrap())
        })
        .map_err(|_| invalid("cli.member.time_invalid"))?;
    Local
        .from_local_datetime(&moment)
        .earliest()
        .map(|date| date.to_utc())
        .ok_or_else(|| invalid("cli.member.time_invalid"))
}

impl Session<'_> {
    pub async fn execute(&self, arguments: &[String], stdin: &str) -> anyhow::Result<Answer> {
        let p = command::parse(arguments)?;
        if p.help || p.command == "help" {
            return Ok(Answer {
                text: Some(crate::context::help().into()),
                ..Default::default()
            });
        }
        let body = p.content(stdin)?;
        if p.text("person") != self.person_id
            && !(p.command == "task status" && p.text("person").is_empty())
        {
            return Err(invalid("cli.member.person_mismatch"));
        }
        if self.person_type != "agent" {
            return Err(invalid("cli.member.human_subject"));
        }
        if self.workflow && !p.read_only && self.delegated_to != Some(self.person_id) {
            return Err(invalid("cli.member.lease.invalid_delegation"));
        }
        let text = body.as_deref().unwrap_or("");
        let mut answer = Answer {
            format: p.text("format").into(),
            ..Default::default()
        };
        let r = &self.repository;
        let approved = p.flag("human-approved");
        if p.command == "pr create" || p.command == "pr update" {
            if p.flag("closes-issue") && p.list("issue-url").is_empty() {
                return Err(invalid("cli.member.ticket_links_required"));
            }
            if p.list("issue-url").iter().any(|v| v.trim().is_empty()) {
                return Err(invalid("cli.member.ticket_links_invalid"));
            }
        }
        let links = || {
            p.list("issue-url")
                .into_iter()
                .map(|url| TicketLink {
                    url,
                    closes: p.flag("closes-issue"),
                })
                .collect::<Vec<_>>()
        };
        answer.payload = match p.command.as_str() {
            "context" => {
                let mut context = self.context.clone();
                if p.flag("check-credentials") {
                    if r.services.code_configured() {
                        let identity = r.services.code()?.identity(true).await?;
                        context
                            .as_object_mut()
                            .ok_or_else(|| invalid("cli.member.context_invalid"))?
                            .extend(identity.into_iter().map(|(k, v)| (k, v.into())));
                        context["credential_status"] = "ok".into();
                    } else {
                        context["credential_status"] = "unchecked".into();
                    }
                    if let Some(chat) = &self.chat
                        && let Ok(status) = chat.check_credentials().await
                    {
                        context["chat_credentials"] = status;
                    }
                }
                context
            }
            "ticket read" | "pr read" | "repo-info read" => {
                let resource: Resource = p.text("resource").parse()?;
                let parameters = if p.text("params").is_empty() {
                    None
                } else {
                    Some(
                        serde_json::from_str::<Value>(p.text("params"))
                            .map_err(|_| invalid("cli.member.params_invalid"))?,
                    )
                };
                r.read(
                    resource,
                    p.text("subject"),
                    parameters.as_ref(),
                    p.text("continuation"),
                )
                .await?
            }
            "ticket comment" => r.ticket_comment(p.text("url"), text).await?,
            "ticket create" => {
                r.ticket_create(
                    CreateTicket {
                        tracker: p.text("tracker").into(),
                        title: title(p.text("title"))?,
                        body: text.into(),
                        labels: p.list("label"),
                        add_to_board: p.flag("add-to-board"),
                        interactive: p.flag("interactive"),
                        assignee: None,
                        lane: p.optional("lane").map(|v| decoded(&v)).transpose()?,
                        parent: p.optional("parent"),
                        blocked_by: p.list("blocked-by"),
                    },
                    p.optional("assignee").as_deref(),
                    approved,
                )
                .await?
            }
            "ticket update" => {
                if p.optional("state-reason").is_some() && p.text("state") != "closed" {
                    return Err(invalid("cli.member.state_reason"));
                }
                r.ticket_update(
                    p.text("url"),
                    UpdateTicket {
                        body,
                        title: p.optional("title").map(|v| title(&v)).transpose()?,
                        add_labels: p.list("add-label"),
                        remove_labels: p.list("remove-label"),
                        state: p.optional("state"),
                        state_reason: p.optional("state-reason"),
                        parent: p.optional("parent"),
                        add_blocked_by: p.list("add-blocked-by"),
                        remove_blocked_by: p.list("remove-blocked-by"),
                        interactive: p
                            .provided
                            .contains("interactive")
                            .then(|| p.flag("interactive")),
                    },
                    approved,
                )
                .await?
            }
            "ticket reaction add" => {
                r.ticket_reaction(
                    p.text("url"),
                    p.text("comment-id"),
                    decoded(p.text("reaction"))?,
                )
                .await?
            }
            "pr create" => {
                r.pr_create(CreatePullRequest {
                    repository: p.text("repo").into(),
                    head: p.text("head").into(),
                    base: p.text("base").into(),
                    title: title(p.text("title"))?,
                    body: text.into(),
                    draft: p.text("draft") == "true",
                    tickets: links(),
                })
                .await?
            }
            "pr update" => {
                if p.flag("drop-issue-links") && !p.list("issue-url").is_empty() {
                    return Err(invalid("cli.member.ticket_links_exclusive"));
                }
                if p.flag("closes-issue") && p.list("issue-url").is_empty() {
                    return Err(invalid("cli.member.ticket_links_required"));
                }
                r.pr_update(
                    p.text("url"),
                    UpdatePullRequest {
                        body,
                        title: p.optional("title").map(|v| title(&v)).transpose()?,
                        tickets: if p.flag("drop-issue-links") || !p.list("issue-url").is_empty() {
                            Some(links())
                        } else {
                            None
                        },
                    },
                )
                .await?
            }
            "pr comment" => r.pr_comment(p.text("url"), text).await?,
            "pr review" => {
                r.pr_review(
                    p.text("url"),
                    text,
                    decoded(p.text("event"))?,
                    p.text("head"),
                )
                .await?
            }
            "pr review-comment" => {
                r.pr_review_comment(
                    p.text("url"),
                    ReviewComment {
                        body: text.into(),
                        head: p.text("head").into(),
                        path: p.text("path").into(),
                        line: p.integer("line").unwrap_or(0),
                        side: p.text("side").into(),
                        start_line: p.integer("start-line"),
                        start_side: p.optional("start-side"),
                    },
                )
                .await?
            }
            "pr reply" => {
                r.pr_reply(p.text("url"), p.text("reply-target-id"), text)
                    .await?
            }
            "pr reaction add" => {
                r.pr_reaction(
                    p.text("url"),
                    p.text("comment-id"),
                    decoded(p.text("reaction"))?,
                )
                .await?
            }
            "pr artifact download" => {
                let download = r.artifact(p.text("url"), p.text("name")).await?;
                answer.download_metadata =
                    Some(json!({"destination":p.text("dest"),"attachment":[]}));
                answer.download = Some(download.content);
                value(download.metadata)?
            }
            "ticket attachment download" => {
                let download = r.attachment(p.text("url"), p.text("id")).await?;
                answer.download_metadata = Some(
                    json!({"destination":p.text("dest"),"attachment":[download.metadata.name,download.metadata.id]}),
                );
                answer.download = Some(download.content);
                value(download.metadata)?
            }
            command if command.starts_with("memory ") => {
                let memory = self
                    .memory
                    .as_ref()
                    .ok_or_else(|| invalid("cli.member.memory_identity"))?;
                let scope = match p.text("scope") {
                    "team" => Scope::Team,
                    _ => Scope::Personal,
                };
                let selected = p.provided.contains("team").then_some(Scope::Team);
                match command {
                    "memory record" => memory.record(
                        scope,
                        self.memory_edit(&p, body)?,
                        p.flag("policy-approved"),
                    )?,
                    "memory update" => memory.update(
                        p.text("id"),
                        selected,
                        self.memory_edit(&p, body)?,
                        p.flag("policy-approved"),
                    )?,
                    "memory get" => memory.get(p.text("id"), selected)?,
                    "memory touch" => memory.touch(p.text("id"), selected)?,
                    "memory archive" => {
                        memory.archive(p.text("id"), selected, p.flag("policy-approved"))?
                    }
                    "memory promote" => memory.promote(p.text("id"))?,
                    "memory recall" => memory.recall(
                        &p.list("query"),
                        p.flag("meta-only"),
                        p.integer("limit").unwrap_or(20) as usize,
                    )?,
                    _ => return Err(invalid("cli.member.unknown_command")),
                }
            }
            "git show" => {
                crate::git::show(
                    self.git_root,
                    r.services.code()?,
                    p.text("repo"),
                    &p.list("path"),
                    self.git
                        .as_ref()
                        .map(|g| g.deadline)
                        .unwrap_or_else(|| Instant::now() + Duration::from_secs(300)),
                )
                .await?
            }
            "git prepare" => {
                if !p.text("pr-url").is_empty() && !p.text("repo").is_empty() {
                    return Err(invalid("cli.member.prepare_repo_pr"));
                }
                if !p.text("branch").is_empty()
                    && (!p.text("pr-url").is_empty() || !p.text("issue-url").is_empty())
                {
                    return Err(invalid("cli.member.prepare_branch_anchor"));
                }
                if p.text("pr-url").is_empty()
                    && p.text("issue-url").is_empty()
                    && (p.text("repo").is_empty() || p.text("branch").is_empty())
                {
                    return Err(invalid("capabilities.member_git.prepare_anchor"));
                }
                let git = self
                    .git
                    .as_ref()
                    .ok_or_else(|| invalid("capabilities.member_git.work_required"))?;
                if git.work.is_none() {
                    return Err(invalid("capabilities.member_git.work_required"));
                }
                let (repository, branch, mode) = if !p.text("pr-url").is_empty() {
                    let head = r
                        .services
                        .code()?
                        .pull_request_head(p.text("pr-url"))
                        .await?;
                    (head.repository, head.branch, "pull_request_review")
                } else if !p.text("issue-url").is_empty() {
                    let ticket = r
                        .services
                        .tickets()?
                        .read(Resource::Tickets, p.text("issue-url"), None, "")
                        .await?;
                    let ticket = ticket.items().first().ok_or(ServiceError::InvalidResult)?;
                    let repository = ticket["repository"]
                        .as_str()
                        .filter(|v| !v.is_empty())
                        .unwrap_or(p.text("repo"))
                        .to_owned();
                    if repository.is_empty() {
                        return Err(MemberError::CapabilityArgs {
                            key: "cli.member.ticket_no_repository",
                            args: [("url".into(), p.text("issue-url").into())].into(),
                        }
                        .into());
                    }
                    if !p.text("repo").is_empty() && p.text("repo") != repository {
                        return Err(MemberError::CapabilityArgs {
                            key: "cli.member.ticket_repository_mismatch",
                            args: [
                                ("url".into(), p.text("issue-url").into()),
                                ("repository".into(), repository),
                                ("requested".into(), p.text("repo").into()),
                            ]
                            .into(),
                        }
                        .into());
                    }
                    (
                        repository,
                        format!(
                            "ticket/{}",
                            ticket["key"].as_str().ok_or(ServiceError::InvalidResult)?
                        ),
                        "issue",
                    )
                } else {
                    (p.text("repo").into(), p.text("branch").into(), "branch")
                };
                let mut result = git.prepare(&repository, &branch).await?;
                result["issue_url"] = p.text("issue-url").into();
                result["pr_url"] = p.text("pr-url").into();
                result["mode"] = mode.into();
                result
            }
            "git commit" | "git push" | "git publish" => {
                if p.text("workspace-mode") == "current" {
                    return Err(invalid("cli.member.current_requires_exchange"));
                }
                let git = self
                    .git
                    .as_ref()
                    .ok_or_else(|| invalid("capabilities.member_git.work_required"))?;
                let path = git.resolve(Path::new(p.text("repo-path")))?;
                match p.command.as_str() {
                    "git commit" => value(git.commit(&path, text).await?)?,
                    "git push" => git.push(&path).await?,
                    _ => git.publish(&path, text).await?,
                }
            }
            "chat identity"
            | "chat resolve-channel"
            | "chat inspect thread"
            | "chat inspect channel"
            | "chat post"
            | "chat reply"
            | "chat reaction add" => {
                let chat = self.chat.as_ref().ok_or(ServiceError::Unavailable)?;
                let reference = Reference {
                    channel_id: (!p.text("channel-id").is_empty()).then(|| p.text("channel-id")),
                    channel_name: (!p.text("channel-name").is_empty())
                        .then(|| p.text("channel-name")),
                    message_id: if p.command == "chat reaction add" {
                        Some(p.text("message-id"))
                    } else {
                        Some(p.text("thread-id"))
                    },
                    message_url: (!p.text("message-url").is_empty()).then(|| p.text("message-url")),
                };
                match p.command.as_str() {
                    "chat identity" => value(chat.identity().await?)?,
                    "chat resolve-channel" => {
                        value(chat.resolve_channel(p.text("channel-name")).await?)?
                    }
                    "chat inspect thread" => {
                        chat.inspect_thread(&reference, p.integer("limit").unwrap_or(100) as usize)
                            .await?
                    }
                    "chat inspect channel" => {
                        let date =
                            |key: &str| p.optional(key).map(|value| iso_time(&value)).transpose();
                        chat.inspect_channel(
                            &reference,
                            ChannelEventsQuery {
                                limit: p.integer("limit").unwrap_or(50) as usize,
                                since: date("since")?,
                                until: date("until")?,
                                cursor: None,
                            },
                        )
                        .await?
                    }
                    "chat post" | "chat reply" => value(
                        chat.post(&reference, text, p.command == "chat reply")
                            .await?,
                    )?,
                    _ => value(chat.react(&reference, decoded(p.text("reaction"))?).await?)?,
                }
            }
            "chat updates" | "chat noop" | "chat complete" | "task complete" | "task status"
            | "checks await" => self.control.command(&p, text).await?,
            _ => return Err(invalid("cli.member.unknown_command")),
        };
        self.control
            .record(&p.command, p.read_only, &answer.payload)?;
        Ok(answer)
    }
    fn memory_edit(&self, p: &Parsed, body: Option<String>) -> anyhow::Result<Edit> {
        let mut source = vec![];
        for kind in ["ticket", "pr", "channel", "thread"] {
            for url in p.list(kind) {
                if !url.trim().is_empty() {
                    source.push(json!({"type":kind,"url":url.trim()}));
                }
            }
        }
        let mut params = serde_json::Map::new();
        for value in p.list("set") {
            let (key, value) = value
                .split_once('=')
                .filter(|(k, _)| !k.trim().is_empty())
                .ok_or_else(|| invalid("cli.member.set_invalid"))?;
            let scalar = match value.trim().to_lowercase().as_str() {
                "true" => true.into(),
                "false" => false.into(),
                _ => value
                    .trim()
                    .parse::<i64>()
                    .map(Value::from)
                    .unwrap_or_else(|_| value.trim().into()),
            };
            params.insert(key.trim().into(), scalar);
        }
        Ok(Edit {
            title: p.optional("title").map(|v| title(&v)).transpose()?,
            summary: p.optional("summary"),
            body,
            keywords: p.provided.contains("keyword").then(|| p.list("keyword")),
            add_keywords: p.list("add-keyword"),
            remove_keywords: p.list("remove-keyword"),
            source: if source.is_empty() {
                None
            } else {
                Some(source)
            },
            pinned: if p.provided.contains("unpin") {
                Some(false)
            } else if p.provided.contains("pin") {
                Some(p.flag("pin"))
            } else {
                None
            },
            kind: p.optional("kind"),
            params,
        })
    }
}

#[cfg(test)]
mod time_tests {
    use super::*;
    #[test]
    fn channel_times_accept_local_dates_and_datetimes_and_preserve_offsets() {
        use chrono::TimeZone;
        for value in [
            "2026-05-01",
            "2026-05-01T09:00:00",
            "2026-05-01 09:00:00",
            "2026-05-01T09:00",
            "2026-05-01 09:00",
        ] {
            let result = iso_time(value).unwrap();
            let hour = if value.len() == 10 { 0 } else { 9 };
            assert_eq!(
                result,
                chrono::Local
                    .with_ymd_and_hms(2026, 5, 1, hour, 0, 0)
                    .earliest()
                    .unwrap()
                    .to_utc()
            );
        }
        assert_eq!(
            iso_time("2026-05-01T09:00:00+09:00").unwrap().to_rfc3339(),
            "2026-05-01T00:00:00+00:00"
        );
        assert_eq!(
            iso_time("invalid").unwrap_err().to_string(),
            "cli.member.time_invalid"
        );
    }
    #[test]
    fn usage_failures_have_the_usage_exit_code() {
        for key in [
            "cli.member.prepare_repo_pr",
            "cli.member.prepare_branch_anchor",
            "cli.member.ticket_update_empty",
            "cli.member.pr_update_empty",
            "cli.member.time_invalid",
        ] {
            assert_eq!(MemberError::Capability(key).exit_code(), 2, "{key}");
        }
        assert_eq!(
            MemberError::Capability("capabilities.task_runs.store_failed").exit_code(),
            1
        );
    }
}
