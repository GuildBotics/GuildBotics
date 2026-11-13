//! 25 → 26: CI runs hold the input digest of each job with paths, and
//! attempts the one they were claimed with. Every run and attempt starts
//! without one (no Git here): the reconciler's next registration gives a
//! run its digests, and attempts claimed afterwards carry them. Only the
//! immediately preceding format is read here.

use crate::ci::queue::{Attempt, Run};
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};

pub const FROM: u64 = 25;
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
    let runs = with_field::<Run>(runs, serde_json::json!({}))?;
    let attempts = with_field::<Attempt>(attempts, serde_json::Value::Null)?;
    Ok(Some(db.finish(|w| {
        for (key, run) in &runs {
            w.put(store::CI_RUNS, key, run)
                .map_err(|e| anyhow::anyhow!(e.message))?;
        }
        for (key, attempt) in &attempts {
            w.put(store::CI_ATTEMPTS, key, attempt)
                .map_err(|e| anyhow::anyhow!(e.message))?;
        }
        Ok(format!(
            "{} CI run(s) and {} attempt(s) upgraded to have no input digests yet",
            runs.len(),
            attempts.len()
        ))
    })?))
}

/// The records with `inputs` set to `empty`.
fn with_field<T: serde::de::DeserializeOwned>(
    records: Vec<(Vec<u8>, Vec<u8>)>,
    empty: serde_json::Value,
) -> anyhow::Result<Vec<(Vec<u8>, T)>> {
    records
        .into_iter()
        .map(|(key, bytes)| {
            let mut value: serde_json::Value = serde_json::from_slice(&bytes)?;
            if let Some(x) = value.as_object_mut() {
                x.insert("inputs".into(), empty.clone());
            }
            Ok((key, serde_json::from_value::<T>(value)?))
        })
        .collect()
}
