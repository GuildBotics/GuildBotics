//! 39 → 40: add the recorded decision for CI retries.
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};

pub const FROM: u64 = 39;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    db.finish(|w| {
        let mut count = 0;
        let mut after = None;
        loop {
            let rows = w.prefix_raw(store::CI_ATTEMPTS, &[], after.as_deref(), 512)?;
            if rows.is_empty() {
                break;
            }
            for (key, raw) in rows {
                let mut value: serde_json::Value = store::decode(&raw)?;
                value["retry_decision"] = serde_json::Value::Null;
                value["runner_deferrals"] = serde_json::json!({});
                w.put(store::CI_ATTEMPTS, &key, &value)?;
                count += 1;
                after = Some(key);
            }
        }
        Ok(format!(
            "initialized retry decisions for {count} CI attempts"
        ))
    })
    .map(Some)
}
