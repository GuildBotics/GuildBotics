//! Bounded per-device memory journals, merged by instant on read.
use anyhow::{Result, ensure};
use gb_base::{files::SHARED_RECORD_SCHEMA_VERSION, shared_writes::SharedWrites};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};
pub struct Audit<'a> {
    pub writes: &'a SharedWrites,
    pub directory: &'a Path,
    pub device_id: &'a str,
    pub max_bytes: usize,
}
impl Audit<'_> {
    pub fn path(&self) -> Result<PathBuf> {
        ensure!(
            !self.device_id.is_empty()
                && self
                    .device_id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
            "cli.member.memory_device_id"
        );
        Ok(self.directory.join(format!("{}.jsonl", self.device_id)))
    }
    pub fn record(&self, mut item: Value) -> Result<()> {
        item["schema_version"] = SHARED_RECORD_SCHEMA_VERSION.into();
        let mut line = serde_json::to_string(&item)?;
        let original_size = line.len() + 1;
        if line.len() + 1 > self.max_bytes {
            let mut compact = item.as_object().cloned().unwrap_or_default();
            for key in ["attributes", "payload", "message"] {
                compact.remove(key);
            }
            compact.insert(
                "message".into(),
                "memory audit payload omitted because it exceeded the file limit".into(),
            );
            let attributes = item["attributes"]
                .as_object()
                .into_iter()
                .flat_map(|m| m.iter())
                .filter(|(k, _)| {
                    [
                        "memory.action",
                        "memory.doc_id",
                        "memory.scope",
                        "memory.kind",
                    ]
                    .contains(&k.as_str())
                })
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<serde_json::Map<_, _>>();
            compact.insert("attributes".into(), attributes.into());
            compact.insert(
                "payload".into(),
                json!({"truncated":true,"original_size_bytes":original_size}),
            );
            line = serde_json::to_string(&compact)?;
        }
        if line.len() + 1 > self.max_bytes {
            line = serde_json::to_string(
                &json!({"truncated":true,"original_size_bytes":original_size}),
            )?;
        }
        if line.len() + 1 > self.max_bytes {
            return Ok(());
        }
        let path = self.path()?;
        self.writes.transaction(|writer| {
            let size = match fs::metadata(&path) {
                Ok(m) => m.len() as usize,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
                Err(e) => return Err(e.into()),
            };
            if size + line.len() < self.max_bytes {
                return writer.append_text(&path, &(line + "\n"));
            }
            let current = fs::read_to_string(&path)?;
            let mut retained = vec![line];
            let mut size = retained[0].len() + 1;
            for line in current.lines().rev() {
                if size + line.len() + 1 > self.max_bytes {
                    break;
                }
                retained.push(line.into());
                size += line.len() + 1;
            }
            retained.reverse();
            writer.write_text(&path, &(retained.join("\n") + "\n"))
        })
    }
    fn journals(&self) -> Result<Vec<PathBuf>> {
        let read = match fs::read_dir(self.directory) {
            Ok(r) => r,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            Err(e) => return Err(e.into()),
        };
        let mut paths = read
            .map(|e| e.map(|e| e.path()))
            .collect::<std::io::Result<Vec<_>>>()?;
        paths.retain(|p| p.extension().is_some_and(|e| e == "jsonl"));
        paths.sort();
        Ok(paths)
    }
    pub fn usage(&self) -> Result<(usize, usize)> {
        let sizes = self
            .journals()?
            .iter()
            .filter_map(|p| fs::metadata(p).ok())
            .map(|m| m.len() as usize)
            .collect::<Vec<_>>();
        Ok((sizes.iter().sum(), self.max_bytes * sizes.len().max(1)))
    }
    pub fn list(
        &self,
        filters: &Value,
        since: Option<chrono::DateTime<chrono::FixedOffset>>,
        until: Option<chrono::DateTime<chrono::FixedOffset>>,
        limit: Option<usize>,
    ) -> Result<Vec<Value>> {
        let mut matches = Vec::new();
        for path in self.journals()? {
            let Ok(text) = fs::read_to_string(path) else {
                continue;
            };
            for line in text.lines().rev() {
                let Ok(item) = serde_json::from_str::<Value>(line) else {
                    continue;
                };
                if !item.is_object() {
                    continue;
                }
                let time = item["timestamp"]
                    .as_str()
                    .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok());
                if (since.is_some() || until.is_some()) && time.is_none() {
                    continue;
                }
                if since.zip(time).is_some_and(|(a, b)| b < a)
                    || until.zip(time).is_some_and(|(a, b)| b > a)
                {
                    continue;
                }
                let mut selected = true;
                for (key, value) in filters.as_object().into_iter().flat_map(|m| m.iter()) {
                    let field = match key.as_str() {
                        "action" => &item["attributes"]["memory.action"],
                        "doc_id" => &item["attributes"]["memory.doc_id"],
                        _ => &item[key],
                    };
                    if field != value {
                        selected = false;
                        break;
                    }
                }
                if selected {
                    matches.push(item);
                }
            }
        }
        matches.sort_by_key(|i| {
            std::cmp::Reverse(
                i["timestamp"]
                    .as_str()
                    .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok()),
            )
        });
        if let Some(limit) = limit {
            matches.truncate(limit.max(1));
        }
        Ok(matches)
    }
}
