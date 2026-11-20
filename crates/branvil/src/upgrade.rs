//! 37 → 38: record CI VM allocations and measured peaks on attempts.
use crate::ci::queue::Attempt;
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};

pub const FROM: u64 = 37;
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
                let attempt: Attempt = store::decode(&raw)?;
                w.put(store::CI_ATTEMPTS, &key, &attempt)?;
                after = Some(key);
                count += 1;
            }
        }
        Ok(format!("added CI resource fields to {count} attempts"))
    })
    .map(Some)
}
