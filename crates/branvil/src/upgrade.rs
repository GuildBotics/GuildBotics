//! 27 → 28: an optional explicit repair ticket responder on every repository.
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 27;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    let repos = db.read(|r| r.prefix_raw(store::REPOS, &[], None, usize::MAX))?;
    Ok(Some(db.finish(|w| {
        for (key, bytes) in repos {
            let mut v: serde_json::Value = serde_json::from_slice(&bytes)?;
            v["responder"] = serde_json::Value::Null;
            let repo: crate::model::Repo = serde_json::from_value(v)?;
            w.put(store::REPOS, &key, &repo)
                .map_err(|e| anyhow::anyhow!(e.message))?;
        }
        Ok("repository responders start automatic".into())
    })?))
}
