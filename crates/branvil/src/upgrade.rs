//! 32 → 33: add diagnostic narrowings without changing existing records.
use crate::config::Layout;
use crate::store::{self, Upgraded, Upgrading};
pub const FROM: u64 = 32;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);
pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    Ok(Some(db.finish(|_| {
        Ok("added CI narrowings and running index".into())
    })?))
}
