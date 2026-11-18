//! 36 → 37: Branvil keeps no browser sessions and no owner token of its own;
//! its host authenticates the owner. The configuration loses `listen`,
//! `base_url`, and `limits.session_hours`.
use redb::TableDefinition;

use crate::config::Layout;
use crate::model::Token;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 36;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

const SESSIONS: store::Tbl = TableDefinition::new("sessions");
/// The owner token whose text the data directory kept in `secrets/owner.token`.
const OWNER_FILE_LABEL: &str = "owner-file";

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    // Read before anything changes: an unreadable file leaves all as it was.
    // The configuration goes first, whole or not at all: the previous build
    // reads it without these keys, and a run cut after it converts the
    // database the next time.
    if let Some(text) = configuration(layout)? {
        let path = layout.config_file();
        let partial = path.with_extension("toml.partial");
        std::fs::write(&partial, text)?;
        std::fs::rename(&partial, &path)?;
    }
    let upgraded = db.finish(|w| {
        let mut revoked = 0;
        for token in w.prefix_all::<Token>(store::TOKENS, &[])? {
            if token.label == OWNER_FILE_LABEL {
                crate::auth::revoke(w, &token)?;
                revoked += 1;
            }
        }
        w.0.delete_table(SESSIONS)?;
        Ok(format!(
            "removed the browser sessions and revoked {revoked} owner file token(s)"
        ))
    })?;
    let secrets = layout.root.join("secrets");
    match std::fs::remove_file(secrets.join("owner.token")) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
        _ => {}
    }
    match std::fs::remove_dir(&secrets) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
        _ => {}
    }
    Ok(Some(upgraded))
}

/// The configuration file without the keys this version drops, when it has
/// any of them.
fn configuration(layout: &Layout) -> anyhow::Result<Option<String>> {
    let path = layout.config_file();
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let mut file: toml_edit::DocumentMut = text.parse()?;
    let mut changed = file.remove("listen").is_some() | file.remove("base_url").is_some();
    if let Some(limits) = file
        .get_mut("limits")
        .and_then(toml_edit::Item::as_table_like_mut)
    {
        changed |= limits.remove("session_hours").is_some();
    }
    Ok(changed.then(|| file.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sessions_and_the_owner_file_token_go_and_the_configuration_loses_their_keys() {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        layout.create().unwrap();
        std::fs::write(
            layout.config_file(),
            "listen = \"127.0.0.1:7878\"\nbase_url = \"http://127.0.0.1:7878\"\n\n[limits]\nsession_hours = 24\ngit_concurrency = 3\n",
        )
        .unwrap();
        std::fs::create_dir(dir.path().join("secrets")).unwrap();
        std::fs::write(dir.path().join("secrets/owner.token"), "bvt_x_y\n").unwrap();
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let kept = [("alice", "auto"), ("alice", "git:laptop"), ("aiko", "ci")];
        s.write_sync(|w| {
            w.put(
                store::META,
                &store::Key::new().s("schema_version").bytes(),
                &FROM,
            )?;
            for (member, label) in kept.iter().chain(&[("alice", OWNER_FILE_LABEL)]) {
                crate::auth::issue_token(w, member, label)?;
            }
            w.put(SESSIONS, b"fixture", &"fixture")?;
            Ok(())
        })
        .unwrap();
        drop(s);
        let done = upgrade(&layout).unwrap().unwrap();
        assert_eq!(
            done.summary,
            "removed the browser sessions and revoked 1 owner file token(s)"
        );
        assert!(!dir.path().join("secrets").exists());
        let config = crate::config::Config::load(None, dir.path()).unwrap();
        assert_eq!(config.limits.git_concurrency, 3);
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        s.read_sync(|r| {
            let mut left: Vec<(String, String)> = r
                .prefix_all::<Token>(store::TOKENS, &[])?
                .into_iter()
                .map(|t| (t.member, t.label))
                .collect();
            left.sort();
            let mut expected: Vec<(String, String)> = kept
                .iter()
                .map(|(m, l)| (m.to_string(), l.to_string()))
                .collect();
            expected.sort();
            assert_eq!(left, expected);
            assert_eq!(
                r.prefix_keys(store::IX_MEMBER_TOKENS, &[])?.len(),
                kept.len()
            );
            Ok(())
        })
        .unwrap();
        assert!(s.read_sync(|r| r.get_raw(SESSIONS, b"fixture")).is_err());
        drop(s);
        assert!(upgrade(&layout).unwrap().is_none());
    }

    /// The configuration is converted first: a run cut before the database
    /// leaves the previous build a configuration it reads, and the next run
    /// converts the database.
    #[test]
    fn a_run_cut_after_the_configuration_converts_the_database_next_time() {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        layout.create().unwrap();
        std::fs::write(layout.config_file(), "[limits]\ngit_concurrency = 3\n").unwrap();
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        s.write_sync(|w| {
            w.put(
                store::META,
                &store::Key::new().s("schema_version").bytes(),
                &FROM,
            )
        })
        .unwrap();
        drop(s);
        assert!(upgrade(&layout).unwrap().is_some());
        assert_eq!(
            std::fs::read_to_string(layout.config_file()).unwrap(),
            "[limits]\ngit_concurrency = 3\n"
        );
        assert!(!dir.path().join("branvil.toml.partial").exists());
    }

    #[test]
    fn an_unreadable_configuration_leaves_the_database_as_it_was() {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        layout.create().unwrap();
        std::fs::write(layout.config_file(), "listen = [").unwrap();
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        s.write_sync(|w| {
            w.put(
                store::META,
                &store::Key::new().s("schema_version").bytes(),
                &FROM,
            )
        })
        .unwrap();
        drop(s);
        assert!(upgrade(&layout).is_err());
        assert!(Upgrading::open(&layout.db()).unwrap().is_some());
    }
}
