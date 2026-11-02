//! 24 → 25: a pull request's publication records whether the host may still
//! add checks of its pushed head; for those published before, the poller's
//! next read says. Only the immediately preceding format is read here.

use crate::config::Layout;
use crate::model::Pull;
use crate::store::{self, Rd, Upgraded, Upgrading};

pub const FROM: u64 = 24;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    let pulls = db
        .read(|r| r.prefix_raw(store::PULLS, &[], None, usize::MAX))?
        .into_iter()
        .map(|(key, bytes)| {
            let mut value: serde_json::Value = serde_json::from_slice(&bytes)?;
            if let Some(x) = value["upstream"].as_object_mut() {
                x.insert("checks_registered_by".into(), serde_json::Value::Null);
                x.insert("checks_settled".into(), false.into());
            }
            Ok((key, serde_json::from_value::<Pull>(value)?))
        })
        .collect::<anyhow::Result<Vec<_>>>()?;
    Ok(Some(db.finish(|w| {
        for (key, pull) in &pulls {
            w.put(store::PULLS, key, pull)
                .map_err(|e| anyhow::anyhow!(e.message))?;
        }
        Ok(format!(
            "{} pull request(s) upgraded to have their host checks read again",
            pulls.len()
        ))
    })?))
}
