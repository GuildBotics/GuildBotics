//! 30 → 31: `ticket_moves`, where each ticket moved to another repository
//! went. The table starts empty and no record changes, yet a build of
//! version 30 must not open the database: it would not follow the moves, and
//! its attachment sweep, which counts the references of the uploading
//! repository's tickets only, would delete the images of moved tickets.
use crate::config::Layout;
use crate::store::{self, Upgraded, Upgrading};
pub const FROM: u64 = 30;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    // Finishing creates the tables the database lacks.
    Ok(Some(db.finish(|_| {
        Ok("tickets may move between the repositories of a workspace".into())
    })?))
}
