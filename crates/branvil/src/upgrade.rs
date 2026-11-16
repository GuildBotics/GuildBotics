//! 34 → 35: repositories opt in to holding ordinary PRs while main fails.
use crate::config::Layout;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 34;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);
pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    Ok(Some(db.finish(|w| {
        for key in w.prefix_keys(store::REPOS, &[])? {
            let mut repo: serde_json::Value = w.get(store::REPOS, &key)?.unwrap();
            repo.as_object_mut()
                .unwrap()
                .insert("hold_while_failing".into(), false.into());
            w.put(store::REPOS, &key, &repo)?;
        }
        Ok("added hold_while_failing=false to repositories".into())
    })?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    #[test]
    fn every_previous_repository_defaults_to_not_held() {
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
            for name in ["one", "two"] {
                w.put(store::REPOS, name.as_bytes(), &json!({"path":name}))?;
            }
            Ok(())
        })
        .unwrap();
        drop(s);
        assert!(upgrade(&layout).unwrap().is_some());
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        s.read_sync(|r| {
            for name in ["one", "two"] {
                assert_eq!(
                    r.get::<Value>(store::REPOS, name.as_bytes())?.unwrap(),
                    json!({"path":name,"hold_while_failing":false})
                );
            }
            Ok(())
        })
        .unwrap();
    }
}
