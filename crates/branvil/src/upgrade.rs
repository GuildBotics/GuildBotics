//! 27 → 28: repository post-merge jobs and service skip reasons.
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 27;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    let tables = db.read(|r| {
        [store::REPOS, store::CI_RUNS, store::CI_ATTEMPTS]
            .into_iter()
            .enumerate()
            .map(|(i, table)| Ok((i, table, r.prefix_raw(table, &[], None, usize::MAX)?)))
            .collect::<crate::error::AppResult<Vec<_>>>()
    })?;
    Ok(Some(db.finish(|w| {
        for (i, table, rows) in tables {
            for (key, bytes) in rows {
                let mut v: serde_json::Value = serde_json::from_slice(&bytes)?;
                match i {
                    0 => {
                        v["post_merge_jobs"] = serde_json::json!([]);
                        v["responder"] = serde_json::Value::Null;
                    }
                    1 if v["selection"].is_object() => {
                        v["selection"]["post_merge_jobs"] = serde_json::json!([]);
                    }
                    2 if v["path_skip"].is_object() => {
                        v["path_skip"]["reason"] = "paths".into();
                    }
                    _ => {}
                }
                w.put(table, &key, &v)
                    .map_err(|e| anyhow::anyhow!(e.message))?;
            }
        }
        Ok("post-merge jobs start empty; existing CI inputs preserved".into())
    })?))
}
