//! Run completion is authorized here, before persistence in RunStore. The
//! invocation owns every identifier and the evidence checked at completion.
use crate::{
    MemberError, Result,
    command::Parsed,
    context::PublicMember,
    repository::{self, AwaitWork, Repository},
    session::Control,
};
use gb_base::shared_writes::SharedWrites;
use gb_observability::{
    redaction::Redactor,
    runs::{Completion, RunCompletion, RunRecord, RunResult, RunStore},
};
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::Path};

pub trait Recipients: Send + Sync {
    fn receives<'a>(&'a self, person: &'a str, subject: &'a str) -> crate::git::Pending<'a, bool>;
}
pub struct Identity<'a> {
    pub run_id: &'a str,
    pub person_id: &'a str,
    pub work_kind: &'a str,
    pub work_identity: &'a str,
    pub subject_type: &'a str,
    pub subject_id: &'a str,
    pub subject_url: &'a str,
}
pub struct RunControl<'a> {
    pub chat: Option<&'a crate::chat_updates::Gate<'a>>,
    pub identity: Identity<'a>,
    pub workspace: &'a Path,
    pub writes: &'a SharedWrites,
    pub repository: &'a Repository<'a>,
    pub roster: &'a [PublicMember],
    pub recipients: &'a dyn Recipients,
    pub redactor: &'a Redactor,
    pub observe: &'a (dyn Fn(&str, bool, &Value) -> Result<()> + Send + Sync),
    pub render_notice: &'a (dyn Fn(&str) -> String + Send + Sync),
    pub render_wait: &'a (dyn Fn(&gb_ports::Readiness) -> String + Send + Sync),
}
fn failed(_: anyhow::Error) -> MemberError {
    MemberError::Capability("capabilities.task_runs.store_failed")
}
fn comment_type(kind: &str) -> bool {
    matches!(
        kind,
        "issue_comment" | "pr_comment" | "pr_review" | "pr_review_comment" | "pr_reply"
    )
}
fn evidence_kind(command: &str) -> Option<&'static str> {
    Some(match command {
        "ticket comment" => "issue_comment",
        "ticket create" => "issue_create",
        "ticket update" => "issue_update",
        "pr comment" => "pr_comment",
        "pr create" => "pr_create",
        "pr update" => "pr_update",
        "pr review" => "pr_review",
        "pr review-comment" => "pr_review_comment",
        "pr reply" => "pr_reply",
        "ticket reaction add" | "pr reaction add" => "reaction_add",
        "git push" | "git publish" => "git_publish",
        "chat post" => "chat_post",
        "chat reply" => "chat_reply",
        "chat reaction add" => "chat_reaction",
        "chat noop" => "chat_noop",
        _ => return None,
    })
}
impl RunControl<'_> {
    fn snapshot(&self) -> Result<RunRecord> {
        let record = RunStore::open(self.workspace)
            .read(self.identity.run_id)
            .map_err(failed)?
            .ok_or(MemberError::Capability("cli.member.run.required_any"))?;
        if record.member_id != self.identity.person_id
            || record.run_id != self.identity.run_id
            || record.work_kind != self.identity.work_kind
            || record.work_identity.as_ref().is_some_and(|work| {
                work.get("kind")
                    .is_some_and(|v| v != self.identity.work_kind)
                    || work
                        .get("identity")
                        .is_some_and(|v| v != self.identity.work_identity)
            })
        {
            return Err(MemberError::Capability(
                "capabilities.task_runs.identity_changed",
            ));
        }
        Ok(record)
    }
    fn append(&self, kind: &str, payload: &Value) -> Result<()> {
        if self.identity.run_id.is_empty() {
            return Ok(());
        }
        let store = RunStore::open(self.workspace);
        self.writes
            .transaction(|writer| {
                let current = self.snapshot()?;
                store.append(
                    writer,
                    current,
                    &json!({"kind":"evidence","evidence_type":kind,"payload":payload}),
                    &chrono::Utc::now().to_rfc3339(),
                    self.redactor,
                )?;
                Ok(())
            })
            .map_err(failed)
    }
    fn check_evidence(&self, record: &RunRecord, status: Completion, summary: &str) -> Result<()> {
        let present = record
            .provider_evidence
            .iter()
            .filter_map(|v| v["type"].as_str())
            .collect::<BTreeSet<_>>();
        if matches!(status, Completion::Asking | Completion::Blocked)
            && self.identity.subject_type != "chat"
            && !present.iter().any(|v| comment_type(v))
        {
            return Err(MemberError::CapabilityArgs {
                key: "capabilities.task_runs.comment_required",
                args: [
                    ("run_id".into(), self.identity.run_id.into()),
                    (
                        "status".into(),
                        if status == Completion::Asking {
                            "asking"
                        } else {
                            "blocked"
                        }
                        .into(),
                    ),
                ]
                .into(),
            });
        }
        let enough = match status {
            Completion::AwaitingChecks => present.contains("checks_awaited"),
            Completion::Blocked if summary.trim().is_empty() => false,
            Completion::Asking | Completion::Blocked if self.identity.subject_type != "chat" => {
                true
            }
            Completion::Blocked => true,
            Completion::Asking => present
                .iter()
                .any(|v| matches!(*v, "chat_post" | "chat_reply")),
            Completion::Done if self.identity.subject_type == "chat" => present.iter().any(|v| {
                matches!(
                    *v,
                    "chat_post" | "chat_reply" | "chat_reaction" | "chat_noop"
                )
            }),
            Completion::Done => {
                let write = present.iter().any(|v| {
                    matches!(
                        *v,
                        "issue_comment"
                            | "pr_comment"
                            | "pr_review"
                            | "pr_review_comment"
                            | "pr_reply"
                            | "reaction_add"
                            | "pr_create"
                            | "pr_update"
                            | "git_publish"
                            | "issue_create"
                    )
                });
                let code = record.provider_evidence.iter().any(|v| {
                    v["type"] == "git_publish"
                        && (v["payload"]["pushed"] == true
                            || v["payload"]["commit_sha"].is_string())
                });
                write && (!code || present.contains("pr_create"))
            }
        };
        if enough {
            Ok(())
        } else {
            Err(MemberError::Capability(
                "capabilities.task_runs.evidence_required",
            ))
        }
    }
    async fn waiting(
        &self,
        p: &Parsed,
        record: &RunRecord,
        status: Completion,
    ) -> Result<Vec<String>> {
        let mut recipients = vec![];
        for id in p.list("waiting-for") {
            if recipients.contains(&id) {
                continue;
            }
            if !matches!(status, Completion::Asking | Completion::Blocked) {
                return Err(MemberError::Capability(
                    "capabilities.task_runs.waiting_status",
                ));
            }
            let person = self
                .roster
                .iter()
                .find(|v| v.person_id == id && v.person_id != self.identity.person_id)
                .ok_or_else(|| MemberError::CapabilityArgs {
                    key: "capabilities.task_runs.waiting_member",
                    args: [("person".into(), id.clone())].into(),
                })?;
            let mut received = person.person_type == "human";
            if !received && person.is_active {
                for evidence in &record.provider_evidence {
                    if !evidence["type"].as_str().is_some_and(comment_type) {
                        continue;
                    }
                    if let Some(url) = evidence["payload"]["target"]["url"].as_str()
                        && self.recipients.receives(&id, url).await?
                    {
                        received = true;
                        break;
                    }
                }
            }
            if !received {
                return Err(MemberError::CapabilityArgs {
                    key: "capabilities.task_runs.waiting_item",
                    args: [("person".into(), id)].into(),
                });
            }
            recipients.push(id);
        }
        Ok(recipients)
    }
    fn status(&self, record: &RunRecord) -> Result<Value> {
        let result = record
            .result
            .as_ref()
            .filter(|_| {
                record.finished_at.is_some()
                    && record.status != gb_observability::runs::RunState::Running
            })
            .ok_or(MemberError::Capability(
                "capabilities.task_runs.not_completed",
            ))?;
        self.check_evidence(record, result.status, &record.safe_summary)?;
        let types = record
            .provider_evidence
            .iter()
            .filter_map(|v| v["type"].as_str())
            .collect::<BTreeSet<_>>();
        Ok(
            json!({"completed":true,"run_id":record.run_id,"person_id":record.member_id,"subject_type":result.subject_type,"subject_id":result.subject_id,"subject_url":result.subject_url,"status":result.status,"summary":record.safe_summary,"completed_at":record.finished_at,"waiting_for":result.waiting_for,"evidence_count":types.len(),"evidence_types":types}),
        )
    }
    async fn complete(&self, p: &Parsed, summary: &str) -> Result<Value> {
        if self.identity.run_id.is_empty() {
            return Err(MemberError::Capability("cli.member.run.required_any"));
        }
        if p.command.starts_with("task ") && self.identity.subject_type == "chat" {
            return Err(MemberError::Capability("cli.member.run.chat_event"));
        }
        if p.command == "chat complete" && self.identity.subject_type != "chat" {
            return Err(MemberError::Capability(
                "cli.member.chat_updates.invalid_run",
            ));
        }
        let status = if p.command == "checks await" {
            Completion::AwaitingChecks
        } else {
            match p.text("status") {
                "done" => Completion::Done,
                "asking" => Completion::Asking,
                "blocked" => Completion::Blocked,
                _ => return Err(MemberError::Capability("cli.member.option_choice")),
            }
        };
        self.repository.guard.ensure_current()?;
        let mut snapshot = self.snapshot()?;
        let recipients = self.waiting(p, &snapshot, status).await?;
        let mut readiness = vec![];
        let ticket = matches!(self.identity.work_kind, "ticket" | "pull_request")
            .then_some(self.identity.work_identity);
        if status == Completion::Done {
            readiness = repository::completion_readiness(
                self.repository.services,
                ticket,
                &snapshot.provider_evidence,
            )
            .await?;
        } else if status == Completion::AwaitingChecks {
            let results = repository::answered(
                self.repository.services.code()?,
                ticket,
                &snapshot.provider_evidence,
            )
            .await?;
            self.repository
                .await_checks(
                    &results,
                    AwaitWork {
                        person_id: self.identity.person_id,
                        run_id: self.identity.run_id,
                        work_kind: self.identity.work_kind,
                        work_identity: self.identity.work_identity,
                        subject: self.identity.subject_id,
                    },
                    self.render_wait,
                    |notice| self.append("checks_awaited", notice),
                )
                .await?;
            readiness = results.into_iter().map(|(_, r)| r).collect();
            snapshot = self.snapshot()?;
        }
        self.check_evidence(&snapshot, status, summary)?;
        let store = RunStore::open(self.workspace);
        let completed = self
            .writes
            .transaction(|writer| {
                let current = self.snapshot()?;
                anyhow::ensure!(
                    current == snapshot,
                    "capabilities.task_runs.identity_changed"
                );
                self.check_evidence(&current, status, summary)?;
                store.record_completion(
                    writer,
                    self.identity.run_id,
                    RunCompletion {
                        result: RunResult {
                            subject_type: self.identity.subject_type.into(),
                            subject_id: self.identity.subject_id.into(),
                            subject_url: self.identity.subject_url.into(),
                            status,
                            waiting_for: recipients,
                        },
                        member_id: self.identity.person_id,
                        summary,
                        now: &chrono::Utc::now().to_rfc3339(),
                    },
                    self.redactor,
                )
            })
            .map_err(failed)?;
        let mut payload = self.status(&completed)?;
        if status == Completion::Done {
            payload["pr_readiness"] = serde_json::to_value(readiness)
                .map_err(|_| MemberError::Capability("capabilities.task_runs.store_failed"))?;
        }

        if matches!(status, Completion::Asking | Completion::Blocked)
            && payload["waiting_for"]
                .as_array()
                .is_none_or(|v| v.is_empty())
        {
            payload["notification"] =
                (self.render_notice)("capabilities.task_runs.owner_notified").into();
        }
        Ok(payload)
    }
}
impl Control for RunControl<'_> {
    fn command<'a>(&'a self, p: &'a Parsed, content: &'a str) -> crate::git::Pending<'a, Value> {
        Box::pin(async move {
            if p.command == "task status" {
                let record = self.snapshot()?;
                return self.status(&record);
            }
            if p.command == "chat noop" {
                if self.identity.subject_type != "chat" {
                    return Err(MemberError::Capability(
                        "cli.member.chat_updates.invalid_run",
                    ));
                }
                return self
                    .chat
                    .ok_or(MemberError::Capability(
                        "cli.member.chat_updates.invalid_run",
                    ))?
                    .noop(content);
            }
            if p.command == "chat updates" {
                return self
                    .chat
                    .ok_or(MemberError::Capability(
                        "cli.member.chat_updates.invalid_run",
                    ))?
                    .check();
            }
            self.complete(p, content).await
        })
    }
    fn record(&self, command: &str, read_only: bool, payload: &Value) -> Result<()> {
        if let Some(kind) = evidence_kind(command) {
            self.append(kind, payload)?;
        }
        (self.observe)(command, read_only, payload)
    }
}
