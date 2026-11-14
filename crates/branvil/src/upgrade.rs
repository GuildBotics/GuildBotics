//! 26 → 27: main verification records and opt-in verified SHA inputs.
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 26;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    let (runs, attempts) = db.read(|r| {
        Ok((
            r.prefix_raw(store::CI_RUNS, &[], None, usize::MAX)?,
            r.prefix_raw(store::CI_ATTEMPTS, &[], None, usize::MAX)?,
        ))
    })?;
    Ok(Some(db.finish(|w| {
        for (key, bytes) in runs {
            let mut v: serde_json::Value = serde_json::from_slice(&bytes)?;
            if let Some(jobs) = v["pipeline"]["jobs"].as_array_mut() {
                for job in jobs {
                    job["verified_sha"] = false.into();
                }
            }
            let run: crate::ci::queue::Run = serde_json::from_value(v)?;
            w.put(store::CI_RUNS, &key, &run)
                .map_err(|e| anyhow::anyhow!(e.message))?;
        }
        for (key, bytes) in attempts {
            let mut v: serde_json::Value = serde_json::from_slice(&bytes)?;
            v["verified_sha"] = serde_json::Value::Null;
            v["job"]["verified_sha"] = false.into();
            let a: crate::ci::queue::Attempt = serde_json::from_value(v)?;
            w.put(store::CI_ATTEMPTS, &key, &a)
                .map_err(|e| anyhow::anyhow!(e.message))?;
        }
        Ok("main verification starts empty; existing CI inputs preserved".into())
    })?))
}
