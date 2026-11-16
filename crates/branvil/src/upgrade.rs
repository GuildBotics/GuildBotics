//! 33 → 34: replace CI failure/recovery notices with repair-ticket notices.
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 33;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);
pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    Ok(Some(db.finish(|w| {
        for (table, fields) in [
            (
                store::CI_RUNS,
                &["failure_pending", "failure_notified_at"][..],
            ),
            (store::CI_ATTEMPTS, &["recovery_notified_at"][..]),
        ] {
            for key in w.prefix_keys(table, &[])? {
                let mut value: serde_json::Value = w.get(table, &key)?.unwrap();
                for field in fields {
                    value.as_object_mut().unwrap().remove(*field);
                }
                w.put(table, &key, &value)?;
            }
        }
        for key in w.prefix_keys(store::TICKETS, &[])? {
            let mut value: serde_json::Value = w.get(store::TICKETS, &key)?.unwrap();
            if let Some(repair) = value["repair"].as_object_mut() {
                repair.insert("notice".into(), serde_json::Value::Null);
                w.put(store::TICKETS, &key, &value)?;
            }
        }
        for name in ["ix_ci_unnotified", "ix_ci_recovered"] {
            w.0.delete_table(store::Tbl::new(name))?;
        }
        Ok("replaced CI notices with repair-ticket notices".into())
    })?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    #[test]
    fn previous_schema_removes_delivery_state_and_does_not_page_old_repairs() {
        let dir = tempfile::tempdir().unwrap();
        let config = crate::config::Config::load(None, Some(dir.path()), None, None).unwrap();
        config.layout().create().unwrap();
        let layout = config.layout();
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        s.write_sync(|w| {
            w.put(
                store::META,
                &store::Key::new().s("schema_version").bytes(),
                &FROM,
            )?;
            w.put(
                store::CI_RUNS,
                b"run",
                &json!({"failure_pending":true,"failure_notified_at":"old","sha":"keep"}),
            )?;
            w.put(
                store::CI_ATTEMPTS,
                b"attempt",
                &json!({"recovery_notified_at":"old","auto_retry":{"number":3}}),
            )?;
            for key in [b"open".as_slice(), b"closed".as_slice()] {
                w.put(
                    store::TICKETS,
                    key,
                    &json!({"repair":{"jobs":["test"],"reported":"sha"}}),
                )?;
            }
            for name in ["ix_ci_unnotified", "ix_ci_recovered"] {
                w.put_raw(store::Tbl::new(name), b"key", b"value")?;
            }
            Ok(())
        })
        .unwrap();
        drop(s);
        assert!(upgrade(&layout).unwrap().is_some());
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        s.read_sync(|r| {
            assert_eq!(
                r.get::<Value>(store::CI_RUNS, b"run")?.unwrap(),
                json!({"sha":"keep"})
            );
            assert_eq!(
                r.get::<Value>(store::CI_ATTEMPTS, b"attempt")?.unwrap(),
                json!({"auto_retry":{"number":3}})
            );
            for key in [b"open".as_slice(), b"closed".as_slice()] {
                assert_eq!(
                    r.get::<Value>(store::TICKETS, key)?.unwrap()["repair"],
                    json!({"jobs":["test"],"reported":"sha","notice":null})
                );
            }
            assert!(r.prefix_keys(store::IX_REPAIR_UNNOTIFIED, &[])?.is_empty());
            Ok(())
        })
        .unwrap();
    }
}
