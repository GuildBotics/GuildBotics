//! 41 → 42: keep recorded CI jobs on disk-backed workspaces.
use anyhow::Context;
use serde_json::Value;

use crate::config::Layout;
use crate::store::{self, Rd, Tbl, Upgraded, Upgrading, Wtx};

pub const FROM: u64 = 41;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    db.finish(|w| {
        let attempts = update_rows(w, store::CI_ATTEMPTS, |attempt| {
            disk_workspace(&mut attempt["job"])
        })?;
        let runs = update_rows(w, store::CI_RUNS, |run| {
            let pipeline = &mut run["pipeline"];
            match pipeline["state"].as_str() {
                Some("jobs") => {
                    let jobs = pipeline["jobs"]
                        .as_array_mut()
                        .context("CI pipeline is missing its jobs")?;
                    for job in jobs {
                        disk_workspace(job)?;
                    }
                }
                Some("absent" | "invalid") => {}
                _ => anyhow::bail!("CI run has an invalid pipeline state"),
            }
            Ok(())
        })?;
        Ok(format!(
            "set disk workspaces for jobs in {attempts} CI attempts and {runs} CI runs"
        ))
    })
    .map(Some)
}

fn disk_workspace(job: &mut Value) -> anyhow::Result<()> {
    job.as_object_mut()
        .context("CI record is missing its job definition")?
        .insert("ram_workspace".into(), Value::Null);
    Ok(())
}

fn update_rows(
    w: &Wtx,
    table: Tbl,
    update: impl Fn(&mut Value) -> anyhow::Result<()>,
) -> anyhow::Result<usize> {
    let mut count = 0;
    let mut after = None;
    loop {
        let rows = w.prefix_raw(table, &[], after.as_deref(), 512)?;
        if rows.is_empty() {
            return Ok(count);
        }
        for (key, raw) in rows {
            let mut value = store::decode(&raw)?;
            update(&mut value)?;
            w.put(table, &key, &value)?;
            count += 1;
            after = Some(key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ci::{pipeline::JobDef, queue::PipelineState};
    use crate::store::{Key, Store};
    use serde_json::json;

    #[test]
    fn upgrade_keeps_all_saved_jobs_on_disk() {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        let db = Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let job = json!({
            "verified_sha": false, "name": "test", "image": "alpine", "commands": ["true"],
            "paths": [], "timeout_secs": null, "tmpfs_mib": 1024, "artifacts": [], "cache": null,
        });
        let mut lint = job.clone();
        lint["name"] = json!("lint");
        let absent = json!({"pipeline": {"state": "absent"}});
        let invalid = json!({"pipeline": {"state": "invalid", "message": "bad pipeline"}});
        assert!(serde_json::from_value::<JobDef>(job.clone()).is_err());
        db.write_sync(|w| {
            w.put(store::CI_ATTEMPTS, b"a", &json!({"job": job, "number": 3}))?;
            w.put(
                store::CI_RUNS,
                b"jobs",
                &json!({"pipeline": {"state": "jobs", "jobs": [job, lint]}}),
            )?;
            w.put(store::CI_RUNS, b"absent", &absent)?;
            w.put(store::CI_RUNS, b"invalid", &invalid)?;
            w.put(store::META, &Key::new().s("schema_version").bytes(), &FROM)
        })
        .unwrap();
        drop(db);

        let upgraded = upgrade(&layout).unwrap().unwrap();
        assert_eq!(upgraded.from, FROM);
        let db = Upgrading::open(&layout.db()).unwrap();
        assert!(db.is_none());
        let db = redb::Database::open(layout.db()).unwrap();
        let r = redb::ReadableDatabase::begin_read(&db).unwrap();
        let attempt: Value = r.get(store::CI_ATTEMPTS, b"a").unwrap().unwrap();
        assert_eq!(attempt["number"], 3);
        let job: JobDef = serde_json::from_value(attempt["job"].clone()).unwrap();
        assert_eq!(
            (job.name.as_str(), job.tmpfs_mib, job.ram_workspace),
            ("test", Some(1024), None)
        );
        let run: Value = r.get(store::CI_RUNS, b"jobs").unwrap().unwrap();
        let PipelineState::Jobs { jobs } = serde_json::from_value(run["pipeline"].clone()).unwrap()
        else {
            panic!("expected jobs in the upgraded pipeline");
        };
        assert_eq!(
            jobs.iter()
                .map(|job| (job.name.as_str(), job.tmpfs_mib, job.ram_workspace.as_ref()))
                .collect::<Vec<_>>(),
            [("test", Some(1024), None), ("lint", Some(1024), None)]
        );
        assert_eq!(r.get(store::CI_RUNS, b"absent").unwrap(), Some(absent));
        assert_eq!(r.get(store::CI_RUNS, b"invalid").unwrap(), Some(invalid));
    }
}
