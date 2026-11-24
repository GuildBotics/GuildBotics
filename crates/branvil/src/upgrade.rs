//! 43 → 44: repair failures and tickets can identify a collection branch.
use anyhow::Context;
use serde_json::Value;

use crate::config::Layout;
use crate::store::{self, Rd, Tbl, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 43;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    db.finish(|w| {
        let tickets = scope_repairs(w, store::TICKETS, |ticket| {
            ticket.get_mut("repair").filter(|r| !r.is_null())
        })?;
        let pending = scope_repairs(w, store::REPAIR_PENDING, |record| Some(record))?;
        Ok(format!(
            "scoped {tickets} repair tickets and {pending} pending failures to the default branch"
        ))
    })
    .map(Some)
}

fn scope_repairs(
    w: &Wtx,
    table: Tbl,
    repair: impl Fn(&mut Value) -> Option<&mut Value>,
) -> anyhow::Result<usize> {
    let mut count = 0;
    let mut after = None;
    loop {
        let rows = w.prefix_raw(table, &[], after.as_deref(), 512)?;
        if rows.is_empty() {
            return Ok(count);
        }
        for (key, raw) in rows {
            let mut value: Value = store::decode(&raw)?;
            if let Some(record) = repair(&mut value) {
                record
                    .as_object_mut()
                    .context("repair record is not an object")?
                    .insert("branch".into(), Value::Null);
                w.put(table, &key, &value)?;
                count += 1;
            }
            after = Some(key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{Key, Store};
    use serde_json::json;

    #[test]
    fn upgrade_keeps_existing_repairs_on_the_default_branch() {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        layout.create().unwrap();
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        db.write_sync(|w| {
            w.put(
                store::TICKETS,
                b"repair",
                &json!({"repair":{"reported":"a","jobs":["lint"]}}),
            )?;
            w.put(store::TICKETS, b"ordinary", &json!({"repair":null}))?;
            w.put(
                store::REPAIR_PENDING,
                b"pending",
                &json!({"repo":"r","sha":"a","failures":[]}),
            )?;
            w.put(store::META, &Key::new().s("schema_version").bytes(), &FROM)
        })
        .unwrap();
        drop(db);

        assert_eq!(upgrade(&layout).unwrap().unwrap().from, FROM);
        let db = redb::Database::open(layout.db()).unwrap();
        let r = redb::ReadableDatabase::begin_read(&db).unwrap();
        let repair: Value = r.get(store::TICKETS, b"repair").unwrap().unwrap();
        assert_eq!(
            repair["repair"],
            json!({"branch":null,"reported":"a","jobs":["lint"]})
        );
        let ordinary: Value = r.get(store::TICKETS, b"ordinary").unwrap().unwrap();
        assert_eq!(ordinary, json!({"repair":null}));
        let pending: Value = r.get(store::REPAIR_PENDING, b"pending").unwrap().unwrap();
        assert_eq!(
            pending,
            json!({"branch":null,"repo":"r","sha":"a","failures":[]})
        );
        let version: u64 = r
            .get(store::META, &Key::new().s("schema_version").bytes())
            .unwrap()
            .unwrap();
        assert_eq!(version, store::SCHEMA_VERSION);
    }
}
