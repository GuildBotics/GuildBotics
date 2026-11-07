//! The task-run journal. Completion authorization remains with the member
//! capability; this store owns persistence and execution lifecycle only.
use crate::{
    activity::{children, readable_children, timestamp},
    redaction::Redactor,
};
use chrono::{DateTime, Utc};
use gb_base::{files::SHARED_RECORD_SCHEMA_VERSION, shared_writes::Writer};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionMode {
    Autonomous,
    UserInitiated,
    Remote,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunState {
    #[default]
    Running,
    Succeeded,
    Failed,
    Cancelled,
    Interrupted,
    ResultUnknown,
    Deferred,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Completion {
    Done,
    Asking,
    Blocked,
    AwaitingChecks,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunResult {
    pub subject_type: String,
    pub subject_id: String,
    #[serde(default)]
    pub subject_url: String,
    pub status: Completion,
    #[serde(default)]
    pub waiting_for: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunRecord {
    #[serde(default = "generation")]
    pub schema_version: u64,
    pub run_id: String,
    pub work_kind: String,
    #[serde(default)]
    pub work_identity: Option<BTreeMap<String, String>>,
    pub execution_mode: ExecutionMode,
    pub member_id: String,
    pub device_id: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub attributes: BTreeMap<String, String>,
    pub started_at: String,
    #[serde(default)]
    pub finished_at: Option<String>,
    #[serde(default)]
    pub status: RunState,
    #[serde(default)]
    pub safe_summary: String,
    #[serde(default)]
    pub result: Option<RunResult>,
    #[serde(default)]
    pub provider_evidence: Vec<Value>,
}
fn generation() -> u64 {
    SHARED_RECORD_SCHEMA_VERSION
}

impl RunRecord {
    pub fn left_work_undone(&self) -> bool {
        self.result.is_none()
            && matches!(
                self.status,
                RunState::Failed | RunState::Cancelled | RunState::Interrupted | RunState::Deferred
            )
    }
}

pub struct RunStore {
    root: PathBuf,
}
pub struct RunFinish<'a> {
    pub state: RunState,
    pub summary: &'a str,
    pub attributes: BTreeMap<String, String>,
    pub now: &'a str,
}
pub struct RunCompletion<'a> {
    pub result: RunResult,
    pub member_id: &'a str,
    pub summary: &'a str,
    pub now: &'a str,
}
impl RunStore {
    pub fn open(workspace: &Path) -> Self {
        Self {
            root: workspace.join(".guildbotics/state/task-runs"),
        }
    }
    fn path(&self, run_id: &str) -> anyhow::Result<PathBuf> {
        checked_id(run_id.trim())?;
        Ok(self.root.join(run_id.trim()).join("result.json"))
    }
    pub fn read(&self, run_id: &str) -> anyhow::Result<Option<RunRecord>> {
        gb_base::files::read_if_present(&self.path(run_id)?)?
            .map(|bytes| serde_json::from_slice(&bytes).map_err(Into::into))
            .transpose()
    }
    fn save(&self, writer: &mut Writer<'_>, mut record: RunRecord) -> anyhow::Result<RunRecord> {
        record.schema_version = generation();
        writer.write_json(&self.path(&record.run_id)?, &record)?;
        Ok(record)
    }
    /// Read and write through the same held capability. Caller supplies the
    /// run's trace identity, device identity, and clock, rather than globals.
    pub fn start(
        &self,
        writer: &mut Writer<'_>,
        mut incoming: RunRecord,
        redactor: &Redactor,
    ) -> anyhow::Result<RunRecord> {
        writer.check(&self.path(&incoming.run_id)?)?;
        let current = self.read(&incoming.run_id)?;
        let Some(mut record) = current else {
            let attributes = std::mem::take(&mut incoming.attributes);
            first_seen(&mut incoming.attributes, attributes, redactor);
            return self.save(writer, incoming);
        };
        if record.finished_at.is_some() && !record.left_work_undone() {
            return self.save(writer, record);
        }
        record.work_kind = incoming.work_kind;
        record.execution_mode = incoming.execution_mode;
        record.member_id = incoming.member_id;
        record.device_id = incoming.device_id;
        record.work_identity = incoming.work_identity;
        if !incoming.source.is_empty() {
            record.source = incoming.source;
        }
        first_seen(&mut record.attributes, incoming.attributes, redactor);
        record.status = RunState::Running;
        record.finished_at = None;
        self.save(writer, record)
    }
    pub fn append(
        &self,
        writer: &mut Writer<'_>,
        mut seed: RunRecord,
        item: &Value,
        now: &str,
        redactor: &Redactor,
    ) -> anyhow::Result<RunRecord> {
        writer.check(&self.path(&seed.run_id)?)?;
        let attributes = std::mem::take(&mut seed.attributes);
        first_seen(&mut seed.attributes, attributes, redactor);
        let mut record = self.read(&seed.run_id)?.unwrap_or(seed);
        for key in ["work_kind", "member_id", "device_id"] {
            if let Some(value) = item[key].as_str().filter(|s| !s.is_empty()) {
                match key {
                    "work_kind" => record.work_kind = value.into(),
                    "member_id" => record.member_id = value.into(),
                    _ => record.device_id = value.into(),
                }
            }
        }
        if let Ok(mode) = serde_json::from_value::<ExecutionMode>(item["execution_mode"].clone()) {
            record.execution_mode = mode;
        }
        if item["kind"] == "evidence" {
            if let Some(kind) = item["evidence_type"].as_str().filter(|s| !s.is_empty()) {
                record.provider_evidence.push(json!({"recorded_at": now, "type": kind,
                    "payload": redactor.redact(item.get("payload").cloned().unwrap_or(json!({})))}));
            }
        } else if let Some(message) = item["message"].as_str().filter(|s| !s.is_empty()) {
            record.safe_summary = redactor.text(message);
        }
        // Stamp after the incoming mapping, including a foreign generation.
        self.save(writer, record)
    }
    pub fn finish(
        &self,
        writer: &mut Writer<'_>,
        id: &str,
        finish: RunFinish<'_>,
        redactor: &Redactor,
    ) -> anyhow::Result<Option<RunRecord>> {
        let RunFinish {
            state,
            summary,
            attributes,
            now,
        } = finish;
        anyhow::ensure!(state != RunState::Running, "invalid terminal state");
        writer.check(&self.path(id)?)?;
        let Some(mut record) = self.read(id)? else {
            return Ok(None);
        };
        first_seen(&mut record.attributes, attributes, redactor);
        if record.finished_at.is_none() {
            record.finished_at = Some(now.into());
            record.status = state;
            record.safe_summary = redactor.text(summary);
        }
        self.save(writer, record).map(Some)
    }
    /// Persist a completion after the host's capability has checked its
    /// evidence and PR readiness, using the same held transaction.
    pub fn record_completion(
        &self,
        writer: &mut Writer<'_>,
        id: &str,
        completion: RunCompletion<'_>,
        redactor: &Redactor,
    ) -> anyhow::Result<RunRecord> {
        writer.check(&self.path(id)?)?;
        let mut record = self
            .read(id)?
            .ok_or_else(|| anyhow::anyhow!("task run has not started"))?;
        record.status = match completion.result.status {
            Completion::Done | Completion::AwaitingChecks => RunState::Succeeded,
            Completion::Asking | Completion::Blocked => RunState::Failed,
        };
        if record.work_kind == "member-work" {
            record.work_kind = completion.result.subject_type.clone();
        }
        record.result = Some(completion.result);
        record.member_id = completion.member_id.into();
        record.safe_summary = redactor.text(completion.summary);
        record.finished_at = Some(completion.now.into());
        self.save(writer, record)
    }

    /// Strict iteration is for workflow decisions; a malformed record must
    /// never become an apparent absence of completed work.
    pub fn records(&self) -> anyhow::Result<Vec<RunRecord>> {
        let mut records = Vec::new();
        for dir in children(&self.root)? {
            let path = dir.join("result.json");
            if !path.is_file() {
                continue;
            }
            records.push(serde_json::from_slice(&std::fs::read(path)?)?);
        }
        Ok(records)
    }
    /// Timeline reads skip corrupt records, like the Python screen reader.
    pub fn records_between(
        &self,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> anyhow::Result<Vec<RunRecord>> {
        let mut matched = Vec::new();
        for record in self.readable_records()? {
            let value = serde_json::to_value(&record)?;
            if timestamp(&value, "started_at").is_some_and(|t| t <= end)
                && timestamp(&value, "finished_at").is_none_or(|t| t >= start)
            {
                matched.push(record);
            }
        }
        Ok(matched)
    }

    fn readable_records(&self) -> anyhow::Result<Vec<RunRecord>> {
        let mut records = Vec::new();
        for dir in readable_children(&self.root) {
            let Ok(bytes) = std::fs::read(dir.join("result.json")) else {
                continue;
            };
            let Ok(record) = serde_json::from_slice::<RunRecord>(&bytes) else {
                continue;
            };
            records.push(record);
        }
        Ok(records)
    }

    pub fn summaries_by_subject(&self) -> anyhow::Result<BTreeMap<(String, String), String>> {
        let mut latest: BTreeMap<(String, String), (String, String)> = BTreeMap::new();
        for record in self.readable_records()? {
            let (Some(finished), Some(result)) = (record.finished_at, record.result) else {
                continue;
            };
            if result.subject_id.is_empty() || record.safe_summary.trim().is_empty() {
                continue;
            }
            let entry = latest
                .entry((result.subject_id, record.member_id))
                .or_default();
            if finished >= entry.0 {
                *entry = (finished, record.safe_summary.trim().into());
            }
        }
        Ok(latest
            .into_iter()
            .map(|(k, (_, summary))| (k, summary))
            .collect())
    }
}

pub(crate) fn first_seen(
    current: &mut BTreeMap<String, String>,
    incoming: BTreeMap<String, String>,
    redactor: &Redactor,
) {
    for (key, value) in incoming {
        if !value.is_empty()
            && !matches!(
                key.as_str(),
                "stdout" | "stderr" | "prompt" | "response" | "messages"
            )
        {
            current.entry(key).or_insert_with(|| redactor.text(&value));
        }
    }
}

pub(crate) fn checked_id(id: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        !id.is_empty() && id != "." && id != ".." && !id.contains(['/', '\\']),
        "invalid record identifier"
    );
    Ok(())
}
