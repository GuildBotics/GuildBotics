//! 44 → 45: repositories drop `post_merge_jobs`; the pipeline marks such jobs.
use anyhow::Context;
use serde_json::Value;

use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};

pub const FROM: u64 = 44;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    db.finish(|w| {
        let mut dropped = Vec::new();
        let mut after = None;
        loop {
            let rows = w.prefix_raw(store::REPOS, &[], after.as_deref(), 512)?;
            if rows.is_empty() {
                break;
            }
            for (key, raw) in rows {
                let mut repo: Value = store::decode(&raw)?;
                let record = repo
                    .as_object_mut()
                    .context("repository record is not an object")?;
                if let Some(Value::Array(jobs)) = record.remove("post_merge_jobs")
                    && !jobs.is_empty()
                {
                    let names: Vec<_> = jobs.iter().filter_map(Value::as_str).collect();
                    let path = record.get("path").and_then(Value::as_str).unwrap_or_default();
                    dropped.push(format!("{path} ({})", names.join(", ")));
                }
                w.put(store::REPOS, &key, &repo)?;
                after = Some(key);
            }
        }
        Ok(if dropped.is_empty() {
            "no repository had post-merge only jobs in its settings".into()
        } else {
            format!(
                "removed the post-merge only jobs of the repository settings; mark them post_merge = true in .guildbotics-ci.toml: {}",
                dropped.join("; ")
            )
        })
    })
    .map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{Key, Store};
    use serde_json::json;

    #[test]
    fn upgrade_removes_the_repository_post_merge_jobs() {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        layout.create().unwrap();
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.write_sync(|w| {
            w.put(
                store::REPOS,
                b"repo",
                &json!({"path":"acme/demo","post_merge_jobs":["release"],"ci_network":false}),
            )?;
            w.put(store::META, &Key::new().s("schema_version").bytes(), &FROM)
        })
        .unwrap();
        drop(db);

        let upgraded = upgrade(&layout).unwrap().unwrap();
        assert_eq!(upgraded.from, FROM);
        assert!(
            upgraded
                .summary
                .ends_with(".guildbotics-ci.toml: acme/demo (release)"),
            "{}",
            upgraded.summary
        );
        let db = redb::Database::open(layout.db()).unwrap();
        let r = redb::ReadableDatabase::begin_read(&db).unwrap();
        let repo: Value = r.get(store::REPOS, b"repo").unwrap().unwrap();
        assert_eq!(repo, json!({"path":"acme/demo","ci_network":false}));
        let version: u64 = r
            .get(store::META, &Key::new().s("schema_version").bytes())
            .unwrap()
            .unwrap();
        assert_eq!(version, store::SCHEMA_VERSION);
    }
}
