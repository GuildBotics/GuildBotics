//! 43 → 44: record the required check attempts used for new merges.
use crate::config::Layout;
use crate::store::{self, Upgraded, Upgrading};

pub const FROM: u64 = 43;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    // Old records carry no decision evidence. Later attempts cannot recreate it.
    db.finish(|_| Ok("enabled recording of merge decision checks".into()))
        .map(Some)
}
