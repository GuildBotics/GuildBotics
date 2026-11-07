//! Device-local interactive identity and its one shared lifecycle record.
use crate::{
    activity::{readable_children, timestamp},
    redaction::Redactor,
    runs::{checked_id, first_seen},
};
use chrono::{DateTime, Duration, Utc};
use gb_base::{files::SHARED_RECORD_SCHEMA_VERSION, shared_writes::Writer};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct InteractiveSession {
    pub trace_id: String,
    pub person_id: String,
    pub workspace: String,
    pub host: String,
    #[serde(default)]
    pub thread_key: String,
    pub started_at: String,
    pub last_seen_at: String,
    pub expires_at: String,
}

pub struct InteractiveSessions {
    root: PathBuf,
}
pub struct SessionCommand<'a> {
    pub command: &'a str,
    pub status: &'a str,
    pub attributes: BTreeMap<String, String>,
    pub now: &'a str,
}
impl InteractiveSessions {
    pub fn open(workspace: &Path) -> Self {
        Self {
            root: workspace.to_owned(),
        }
    }
    /// The local state is also changed under the caller's workspace lock:
    /// simultaneous commands must select the same conversation identity.
    pub fn start_or_touch(
        &self,
        writer: &mut Writer<'_>,
        person: &str,
        host: &str,
        thread: &str,
        now: DateTime<Utc>,
        idle: Duration,
    ) -> anyhow::Result<InteractiveSession> {
        let path = self
            .root
            .join(".guildbotics/local/run/interactive_trace_state.json");
        writer.check(&path)?;
        let mut sessions: BTreeMap<String, InteractiveSession> =
            gb_base::files::read_if_present(&path)?
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                .and_then(|v| v["sessions"].as_object().cloned())
                .map(|entries| {
                    entries
                        .into_iter()
                        .filter_map(|(k, v)| {
                            serde_json::from_value(v).ok().map(|session| (k, session))
                        })
                        .collect()
                })
                .unwrap_or_default();
        sessions.retain(|_, s| DateTime::parse_from_rfc3339(&s.expires_at).is_ok_and(|t| t >= now));
        let workspace = self.root.to_string_lossy().to_string();
        let key = [&workspace, person, host, thread].join("\0");
        let session = sessions.entry(key).or_insert_with(|| InteractiveSession {
            trace_id: uuid::Uuid::new_v4().simple().to_string(),
            person_id: person.into(),
            workspace,
            host: host.into(),
            thread_key: thread.into(),
            started_at: now.to_rfc3339(),
            last_seen_at: now.to_rfc3339(),
            expires_at: (now + idle).to_rfc3339(),
        });
        session.last_seen_at = now.to_rfc3339();
        session.expires_at = (now + idle).to_rfc3339();
        let result = session.clone();
        writer.write_json(&path, &json!({"sessions": sessions}))?;
        Ok(result)
    }

    pub fn record(
        &self,
        writer: &mut Writer<'_>,
        session: &InteractiveSession,
        ended: SessionCommand<'_>,
        redactor: &Redactor,
    ) -> anyhow::Result<Value> {
        let SessionCommand {
            command,
            status,
            attributes,
            now,
        } = ended;
        checked_id(&session.trace_id)?;
        let path = self.root.join(format!(
            ".guildbotics/state/sessions/{}.json",
            session.trace_id
        ));
        writer.check(&path)?;
        let mut record: Value = gb_base::files::read_if_present(&path)?
            .map(|b| serde_json::from_slice(&b))
            .transpose()?
            .unwrap_or(json!({}));
        if !record.is_object() {
            record = json!({});
        }
        let mut merged: BTreeMap<String, String> =
            serde_json::from_value(record["attributes"].clone()).unwrap_or_default();
        first_seen(
            &mut merged,
            attributes
                .into_iter()
                .filter(|(k, _)| k.starts_with("target."))
                .collect(),
            redactor,
        );
        if record["command"].as_str().is_none_or(str::is_empty) {
            record["command"] = command.into();
        }
        if record["started_at"].as_str().is_none_or(str::is_empty) {
            record["started_at"] = session.started_at.clone().into();
        }
        record["schema_version"] = SHARED_RECORD_SCHEMA_VERSION.into();
        record["trace_id"] = session.trace_id.clone().into();
        record["person_id"] = session.person_id.clone().into();
        record["source"] = "interactive".into();
        record["last_seen_at"] = now.into();
        record["status"] = status.into();
        record["attributes"] = serde_json::to_value(merged)?;
        writer.write_json(&path, &record)?;
        Ok(record)
    }
    pub fn list_between(
        &self,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> anyhow::Result<Vec<Value>> {
        let mut records = Vec::new();
        for path in readable_children(&self.root.join(".guildbotics/state/sessions")) {
            if path.extension().is_none_or(|e| e != "json") {
                continue;
            }
            let Ok(bytes) = std::fs::read(path) else {
                continue;
            };
            let Ok(v) = serde_json::from_slice::<Value>(&bytes) else {
                continue;
            };
            if timestamp(&v, "started_at").is_some_and(|t| t <= end)
                && timestamp(&v, "last_seen_at").is_some_and(|t| t >= start)
            {
                records.push(v);
            }
        }
        Ok(records)
    }
}
