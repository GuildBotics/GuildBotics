//! 29 → 30: repair ticket markers and durable pending failures.
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 29;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    let tickets = db.read(|r| r.prefix_raw(store::TICKETS, &[], None, usize::MAX))?;
    Ok(Some(db.finish(|w| {
        for (key, bytes) in tickets {
            let mut v: serde_json::Value = serde_json::from_slice(&bytes)?;
            v["repair"] = serde_json::Value::Null;
            let ticket: crate::model::Ticket = serde_json::from_value(v)?;
            w.put(store::TICKETS, &key, &ticket)
                .map_err(|e| anyhow::anyhow!(e.message))?;
        }
        Ok("tickets start without repair markers".into())
    })?))
}
