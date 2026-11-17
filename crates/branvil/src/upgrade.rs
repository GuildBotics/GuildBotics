//! 35 → 36: revoke the tokens GuildBotics kept, which it no longer uses.
use crate::config::Layout;
use crate::model::Token;
use crate::store::{self, Rd, Upgraded, Upgrading};
pub const FROM: u64 = 35;
const _: () = assert!(FROM + 1 == store::SCHEMA_VERSION);

/// GuildBotics' owner tokens from the consent screen (`app:<client>`) and
/// its agents' member tokens (`guildbotics`).
fn kept_by_guildbotics(label: &str) -> bool {
    label == "guildbotics" || label.starts_with("app:")
}

pub fn upgrade(layout: &Layout) -> anyhow::Result<Option<Upgraded>> {
    let Some(db) = Upgrading::open(&layout.db())? else {
        return Ok(None);
    };
    Ok(Some(db.finish(|w| {
        let mut revoked = 0;
        for token in w.prefix_all::<Token>(store::TOKENS, &[])? {
            if kept_by_guildbotics(&token.label) {
                crate::auth::revoke(w, &token)?;
                revoked += 1;
            }
        }
        Ok(format!("revoked {revoked} token(s) kept by GuildBotics"))
    })?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_tokens_guildbotics_kept_are_revoked_with_their_index() {
        let dir = tempfile::tempdir().unwrap();
        let config = crate::config::Config::load(None, Some(dir.path()), None, None).unwrap();
        config.layout().create().unwrap();
        let layout = config.layout();
        let s = store::Store::open(&layout.db(), 1 << 20, 1).unwrap();
        let kept = [
            ("alice", "owner-file"),
            ("runner1", "auto"),
            ("alice", "git:laptop"),
            ("aiko", "ci"),
            ("aiko", "guildbotics-git"),
            ("aiko", "app"),
        ];
        let revoked = [
            ("alice", "app:GuildBotics"),
            ("alice", "app:"),
            ("aiko", "guildbotics"),
            ("yuki", "guildbotics"),
        ];
        s.write_sync(|w| {
            w.put(
                store::META,
                &store::Key::new().s("schema_version").bytes(),
                &FROM,
            )?;
            for (member, label) in kept.iter().chain(&revoked) {
                crate::auth::issue_token(w, member, label)?;
            }
            Ok(())
        })
        .unwrap();
        drop(s);
        let done = upgrade(&layout).unwrap().unwrap();
        assert_eq!(done.summary, "revoked 4 token(s) kept by GuildBotics");
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
        drop(s);
        assert!(upgrade(&layout).unwrap().is_none());
    }
}
