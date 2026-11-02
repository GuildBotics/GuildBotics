//! Domain operations. Each public function is one API operation: it checks
//! what the caller's kind may do, reads and writes the store in short
//! transactions (never across Git), and returns the API view.

pub mod apps;
pub mod attachments;
pub mod markdown;
pub mod members;
pub mod pulls;
pub mod repos;
pub mod tickets;

use serde::Serialize;

use crate::error::{AppError, AppResult};
use crate::model::{self, Member, Repo, Workspace};
use crate::store::{self, Rd};
use crate::util;

/// One page of a collection.
#[derive(Serialize, Debug)]
pub struct Page<T> {
    pub items: Vec<T>,
    pub next_cursor: Option<String>,
}

/// A page request whose cursor was verified against the collection binding.
#[derive(Clone, Debug)]
pub struct PageReq {
    pub limit: usize,
    pub after: Option<Vec<u8>>,
    binding: String,
    key: Vec<u8>,
}

impl PageReq {
    /// `binding` names the collection and every condition of the request; a
    /// cursor from any other collection or condition does not verify.
    pub fn new(
        key: &[u8],
        binding: String,
        limit: Option<usize>,
        cursor: Option<&str>,
    ) -> AppResult<Self> {
        let limit = limit.unwrap_or(30);
        if !(1..=100).contains(&limit) {
            return Err(AppError::invalid("limit must be between 1 and 100."));
        }
        let mut req = Self {
            limit,
            after: None,
            binding,
            key: key.to_vec(),
        };
        if let Some(cursor) = cursor.filter(|c| !c.is_empty()) {
            let (pos, mac) = cursor
                .split_once('.')
                .ok_or_else(AppError::invalid_cursor)?;
            let pos = util::unb64(pos).ok_or_else(AppError::invalid_cursor)?;
            let mac = util::unb64(mac).ok_or_else(AppError::invalid_cursor)?;
            let expected = req.mac(&pos);
            if !bool::from(subtle::ConstantTimeEq::ct_eq(
                mac.as_slice(),
                expected.as_slice(),
            )) {
                return Err(AppError::invalid_cursor());
            }
            req.after = Some(pos);
        }
        Ok(req)
    }

    fn mac(&self, position: &[u8]) -> Vec<u8> {
        let mut message = self.binding.as_bytes().to_vec();
        message.push(0);
        message.extend_from_slice(position);
        util::hmac_sha256(&self.key, &message)[..16].to_vec()
    }

    pub fn cursor(&self, position: &[u8]) -> String {
        format!("{}.{}", util::b64(position), util::b64(&self.mac(position)))
    }

    /// Pages a list already sorted by ascending position.
    pub fn slice<T>(&self, items: Vec<(Vec<u8>, T)>) -> (Vec<T>, Option<String>) {
        let mut out = Vec::new();
        let mut last = None;
        let mut more = false;
        for (pos, item) in items {
            if let Some(after) = &self.after
                && pos.as_slice() <= after.as_slice()
            {
                continue;
            }
            if out.len() == self.limit {
                more = true;
                break;
            }
            last = Some(pos);
            out.push(item);
        }
        let next = if more {
            last.map(|p| self.cursor(&p))
        } else {
            None
        };
        (out, next)
    }

    /// Scans `prefix` of a table in key order from the cursor, keeping the
    /// entries `f` maps to an item, until one page and one more are found.
    pub fn scan<T>(
        &self,
        r: &impl Rd,
        table: store::Tbl,
        prefix: &[u8],
        f: impl FnMut(&[u8], &[u8]) -> AppResult<Option<T>>,
    ) -> AppResult<(Vec<T>, Option<String>)> {
        self.scan_prefixes(r, table, &[prefix.to_vec()], f)
    }

    /// Like `scan` over several prefixes, in ascending order and not
    /// overlapping, as one collection: entries between them are never read.
    pub fn scan_prefixes<T>(
        &self,
        r: &impl Rd,
        table: store::Tbl,
        prefixes: &[Vec<u8>],
        mut f: impl FnMut(&[u8], &[u8]) -> AppResult<Option<T>>,
    ) -> AppResult<(Vec<T>, Option<String>)> {
        let mut out = Vec::new();
        let mut last_taken: Option<Vec<u8>> = None;
        for prefix in prefixes {
            // A cursor past this prefix skips it; one inside it starts there.
            let mut position = match &self.after {
                Some(a)
                    if store::prefix_end(prefix)
                        .is_some_and(|end| a.as_slice() >= end.as_slice()) =>
                {
                    continue;
                }
                Some(a) if a.as_slice() >= prefix.as_slice() => Some(a.clone()),
                _ => None,
            };
            loop {
                let batch = r.prefix_raw(table, prefix, position.as_deref(), 256)?;
                if batch.is_empty() {
                    break;
                }
                for (k, v) in &batch {
                    if let Some(item) = f(k, v)? {
                        if out.len() == self.limit {
                            let next = last_taken.map(|p| self.cursor(&p));
                            return Ok((out, next));
                        }
                        out.push(item);
                        last_taken = Some(k.clone());
                    }
                }
                position = batch.last().map(|(k, _)| k.clone());
            }
        }
        Ok((out, None))
    }
}

pub fn path_id(r: &impl Rd, kind: &str, path: &str) -> AppResult<Option<String>> {
    Ok(r.get_raw(store::IX_PATHS, &model::path_key(kind, path))?
        .map(|v| String::from_utf8_lossy(&v).into_owned()))
}

pub fn repo_by_path(r: &impl Rd, path: &str) -> AppResult<Repo> {
    let not_found = || AppError::not_found(format!("Repository {path} was not found."));
    let id = path_id(r, "repo", path)?.ok_or_else(not_found)?;
    r.get(store::REPOS, &Repo::key(&id))?.ok_or_else(not_found)
}

pub fn repo_by_id(r: &impl Rd, id: &str) -> AppResult<Repo> {
    r.get(store::REPOS, &Repo::key(id))?
        .ok_or_else(|| AppError::internal(format!("repository {id} is missing")))
}

pub fn workspace_by_name(r: &impl Rd, name: &str) -> AppResult<Workspace> {
    let not_found = || AppError::not_found(format!("Workspace {name} was not found."));
    let id = path_id(r, "workspace", name)?.ok_or_else(not_found)?;
    r.get(store::WORKSPACES, &Workspace::key(&id))?
        .ok_or_else(not_found)
}

pub fn member_by_name(r: &impl Rd, name: &str) -> AppResult<Member> {
    r.get(store::MEMBERS, &Member::key(name))?
        .ok_or_else(|| AppError::not_found(format!("Member {name} was not found.")))
}

/// Body text limit for titles, bodies, and comments.
pub const MAX_TEXT: usize = 256 << 10;
pub const MAX_TITLE: usize = 1024;
pub const MAX_META: usize = 4 << 10;

pub fn check_text(field: &str, value: &str, max: usize) -> AppResult<()> {
    if value.len() > max {
        return Err(AppError::too_large(format!(
            "{field} is longer than {max} bytes."
        )));
    }
    Ok(())
}

/// The media type of an image (a member's picture, a ticket's attachment),
/// read from its first bytes: PNG, JPEG, GIF, or WebP (no SVG, which can
/// carry script).
pub fn image_type(bytes: &[u8]) -> Option<&'static str> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
        Some("image/jpeg")
    } else if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        Some("image/webp")
    } else {
        None
    }
}

pub fn check_title(title: &str) -> AppResult<()> {
    if title.trim().is_empty() {
        return Err(AppError::invalid("title must not be empty."));
    }
    check_text("title", title, MAX_TITLE)
}

pub fn check_meta(meta: &Option<serde_json::Value>) -> AppResult<()> {
    if let Some(meta) = meta {
        if !meta.is_object() {
            return Err(AppError::invalid("meta must be a JSON object."));
        }
        if serde_json::to_vec(meta)
            .map(|v| v.len())
            .unwrap_or(usize::MAX)
            > MAX_META
        {
            return Err(AppError::too_large("meta is larger than 4 KiB."));
        }
    }
    Ok(())
}

pub const REACTIONS: [&str; 4] = ["ack", "agree", "celebrate", "support"];

pub fn check_reaction(reaction: &str) -> AppResult<()> {
    if REACTIONS.contains(&reaction) {
        Ok(())
    } else {
        Err(AppError::invalid(format!(
            "reaction must be one of {}.",
            REACTIONS.join(", ")
        )))
    }
}

/// Gives (`on`) or takes back an author's reaction in a list. Giving one the
/// author already gave keeps it, taking back one they did not give changes
/// nothing; the reaction given is returned.
pub fn set_reaction(
    list: &mut Vec<model::Reaction>,
    author: &str,
    reaction: &str,
    on: bool,
) -> Option<model::Reaction> {
    let theirs =
        |r: &model::Reaction| util::fold(&r.author) == util::fold(author) && r.reaction == reaction;
    if !on {
        list.retain(|r| !theirs(r));
        return None;
    }
    if let Some(existing) = list.iter().find(|r| theirs(r)) {
        return Some(existing.clone());
    }
    let r = model::Reaction {
        id: util::new_id("rct"),
        author: author.to_string(),
        reaction: reaction.to_string(),
    };
    list.push(r.clone());
    Some(r)
}

#[derive(Serialize, Clone, Debug)]
pub struct ReactionView {
    pub author: String,
    pub reaction: String,
}

pub fn reaction_views(list: &[model::Reaction]) -> Vec<ReactionView> {
    list.iter()
        .map(|r| ReactionView {
            author: r.author.clone(),
            reaction: r.reaction.clone(),
        })
        .collect()
}

#[derive(Serialize, Clone, Debug)]
pub struct CommentView {
    pub id: String,
    pub author: String,
    pub body: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mentions: Option<Vec<String>>,
    pub meta: Option<serde_json::Value>,
    pub created_at: String,
    pub reactions: Vec<ReactionView>,
    pub url: String,
}

pub fn comment_view(c: &model::Comment, item_url: &str) -> CommentView {
    CommentView {
        id: c.id.clone(),
        author: c.author.clone(),
        body: c.body.clone(),
        mentions: Some(c.mentions.clone()),
        meta: c.meta.clone(),
        created_at: c.created_at.clone(),
        reactions: reaction_views(&c.reactions),
        url: format!("{item_url}#{}", c.id),
    }
}

/// Resolves an entry id to its record key, only when it belongs to `owner`.
pub fn entry(r: &impl Rd, id: &str, owner: &str) -> AppResult<Option<model::EntryRef>> {
    let found: Option<model::EntryRef> =
        r.get(store::IX_ENTRIES, &store::Key::new().s(id).bytes())?;
    Ok(found.filter(|e| e.owner == owner))
}

pub fn entry_key(e: &model::EntryRef) -> AppResult<Vec<u8>> {
    util::unb64(&e.key).ok_or_else(|| AppError::internal("bad entry key"))
}

/// Distinguishes a missing field from an explicit `null`.
pub fn double_option<'de, D, T>(d: D) -> Result<Option<Option<T>>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: serde::Deserialize<'de>,
{
    serde::Deserialize::deserialize(d).map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{Key, Store};

    #[test]
    fn scan_prefixes_pages_across_prefixes_and_skips_what_lies_between() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(&dir.path().join("t.redb"), 1 << 20, 2).unwrap();
        store
            .write_sync(|w| {
                for lane in 0..4u64 {
                    for n in 0..3u64 {
                        w.put(
                            store::IX_CARDS,
                            &Key::new().s("b").u(lane).u(n).bytes(),
                            &(lane * 10 + n),
                        )?;
                    }
                }
                Ok(())
            })
            .unwrap();
        let prefixes: Vec<Vec<u8>> = [1u64, 3]
            .iter()
            .map(|l| Key::new().s("b").u(*l).bytes())
            .collect();
        let mut seen = Vec::new();
        let mut visited = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let page = PageReq::new(b"k", "cards".into(), Some(2), cursor.as_deref()).unwrap();
            let (items, next) = store
                .read_sync(|r| {
                    page.scan_prefixes(r, store::IX_CARDS, &prefixes, |_, v| {
                        let n: u64 = store::decode(v)?;
                        visited.push(n);
                        Ok((n % 10 != 1).then_some(n))
                    })
                })
                .unwrap();
            seen.extend(items);
            match next {
                Some(c) => cursor = Some(c),
                None => break,
            }
        }
        assert_eq!(seen, [10, 12, 30, 32]);
        assert!(
            visited.iter().all(|n| n / 10 == 1 || n / 10 == 3),
            "entries of other lanes were read: {visited:?}"
        );
    }
}
