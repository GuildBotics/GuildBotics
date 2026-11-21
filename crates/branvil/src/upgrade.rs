//! 38 → 39: keep old cache saves as historical attempts.
use crate::ci::queue::{Attempt, attempt_index};
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};

pub const FROM: u64 = 38;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    db.finish(|w| {
        let mut count = 0;
        let now = crate::util::now();
        let mut after = None;
        loop {
            let rows = w.prefix_raw(store::CI_ATTEMPTS, &[], after.as_deref(), 512)?;
            if rows.is_empty() {
                break;
            }
            for (key, raw) in rows {
                let mut value: serde_json::Value = store::decode(&raw)?;
                let was_refresh = value["purpose"] == "cache_refresh";
                if was_refresh {
                    value["purpose"] = "historical".into();
                    count += 1;
                }
                let mut attempt: Attempt =
                    serde_json::from_value(value).map_err(crate::error::AppError::internal)?;
                if was_refresh && attempt.status != "completed" {
                    let old_index = attempt_index(&attempt);
                    attempt.status = "completed".into();
                    attempt.conclusion = Some("cancelled".into());
                    attempt.details = Some("Cache refresh retired by the schema upgrade.".into());
                    attempt.finished_at = Some(now.clone());
                    attempt.lease_hash = None;
                    attempt.lease_expires_at = None;
                    crate::model::reindex(w, old_index, attempt_index(&attempt))?;
                }
                w.put(store::CI_ATTEMPTS, &key, &attempt)?;
                after = Some(key);
            }
        }
        // No stored attempt, log, artifact, or old index data is deleted.
        Ok(format!("retired {count} cache refresh attempts"))
    })
    .map(Some)
}
