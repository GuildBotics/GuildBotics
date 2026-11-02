//! Images attached to a repository's tickets. The bytes are files in
//! `<data>/attachments/`, the records are in `ATTACHMENTS`, and the bodies and
//! comments of the repository's tickets refer to them by id
//! (`IX_ATTACHMENT_REFS`). An attachment belongs to the repository, not to a
//! ticket, so it can be uploaded before its ticket exists; one no ticket
//! refers to is deleted `GRACE_SECS` after its upload.

use std::collections::HashSet;
use std::time::Duration;

use serde::Serialize;
use tokio::io::AsyncWriteExt;

use crate::app::App;
use crate::auth::Caller;
use crate::domain::{Page, PageReq, image_type, repos, tickets};
use crate::error::{AppError, AppResult};
use crate::model::{self, Attachment, Repo, Target};
use crate::store::{self, Rd};
use crate::util;

/// The largest image a ticket may have attached.
pub const MAX_BYTES: usize = 10 << 20;
/// How long an attachment no ticket refers to is kept after its upload.
pub const GRACE_SECS: i64 = 24 * 3600;
const MAX_NAME: usize = 255;

#[derive(Serialize, Debug)]
pub struct AttachmentView {
    pub id: String,
    pub name: String,
    pub media_type: String,
    pub size: u64,
    /// The same-origin path of the image, which a body refers to it by.
    pub url: String,
    pub created_at: String,
}

/// An attachment a ticket refers to, and where: `body`, or a comment's id.
#[derive(Serialize, Debug)]
pub struct TicketAttachment {
    #[serde(flatten)]
    pub attachment: AttachmentView,
    pub referenced_by: Vec<String>,
}

fn view(repo: &Repo, a: &Attachment) -> AttachmentView {
    AttachmentView {
        id: a.id.clone(),
        name: a.name.clone(),
        media_type: a.media_type.clone(),
        size: a.size,
        url: format!(
            "/api/v1/repos/{}/attachments/{}",
            repo.path.replace('/', "%2F"),
            a.id
        ),
        created_at: a.created_at.clone(),
    }
}

fn file(app: &App, id: &str) -> std::path::PathBuf {
    app.layout.attachments().join(id)
}

/// Keeps an image for the repository's tickets, by any member who sees it.
pub async fn upload(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    name: Option<String>,
    bytes: Vec<u8>,
) -> AppResult<AttachmentView> {
    let repo = repos::visible(app, caller, repo_path).await?;
    if bytes.len() > MAX_BYTES {
        return Err(AppError::too_large(format!(
            "An image may be at most {} MiB.",
            MAX_BYTES >> 20
        )));
    }
    let media_type = image_type(&bytes)
        .ok_or_else(|| AppError::invalid("An image must be a PNG, JPEG, GIF, or WebP image."))?;
    let name = name
        .map(|n| n.trim().to_string())
        .filter(|n| !n.is_empty())
        .unwrap_or_else(|| "image".into());
    if name.len() > MAX_NAME || name.chars().any(char::is_control) {
        return Err(AppError::invalid(format!(
            "name must be at most {MAX_NAME} bytes without control characters."
        )));
    }
    let a = Attachment {
        id: util::new_id("atc"),
        repo: repo.id.clone(),
        name,
        media_type: media_type.into(),
        size: bytes.len() as u64,
        author: caller.name().to_string(),
        created_at: util::now(),
    };
    // The whole file before its record: a crash between them leaves a file
    // without a record, which `sweep` removes.
    let path = file(app, &a.id);
    let temporary = path.with_extension("tmp");
    let mut out = tokio::fs::File::create(&temporary)
        .await
        .map_err(AppError::internal)?;
    out.write_all(&bytes).await.map_err(AppError::internal)?;
    out.sync_all().await.map_err(AppError::internal)?;
    tokio::fs::rename(&temporary, &path)
        .await
        .map_err(AppError::internal)?;
    let record = a.clone();
    app.store
        .write(move |w| {
            w.put(
                store::ATTACHMENTS,
                &Attachment::key(&record.repo, &record.id),
                &record,
            )
        })
        .await?;
    Ok(view(&repo, &a))
}

/// An attachment's bytes and media type, for any member who sees its
/// repository.
pub async fn get(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    id: &str,
) -> AppResult<(String, Vec<u8>)> {
    let repo = repos::visible(app, caller, repo_path).await?;
    let not_found =
        || AppError::not_found(format!("Attachment {id} was not found in {}.", repo.path));
    let key = Attachment::key(&repo.id, id);
    let a: Attachment = app
        .store
        .read(move |r| r.get(store::ATTACHMENTS, &key))
        .await?
        .ok_or_else(not_found)?;
    match tokio::fs::read(file(app, &a.id)).await {
        Ok(bytes) => Ok((a.media_type, bytes)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(not_found()),
        Err(e) => Err(AppError::internal(e)),
    }
}

/// The attachments of the repository the ticket's body and comments refer
/// to, in the order of their first reference.
pub async fn of_ticket(
    app: &App,
    caller: &Caller,
    repo_path: &str,
    key: &str,
    page: PageReq,
) -> AppResult<Page<TicketAttachment>> {
    let repo = repos::visible(app, caller, repo_path).await?;
    let key = key.to_string();
    app.store
        .read(move |r| {
            let t = tickets::ticket_by_key(r, &repo, &key)?;
            let target = Target::Ticket {
                repo: t.repo.clone(),
                number: t.number,
            };
            let mut sources = vec![("body".to_string(), t.body)];
            for c in tickets::comments_of(r, &target)? {
                sources.push((c.id, c.body));
            }
            let mut items: Vec<TicketAttachment> = Vec::new();
            for (source, text) in sources {
                for id in model::attachment_ids(&text) {
                    if let Some(item) = items.iter_mut().find(|i| i.attachment.id == id) {
                        item.referenced_by.push(source.clone());
                    } else if let Some(a) =
                        r.get::<Attachment>(store::ATTACHMENTS, &Attachment::key(&repo.id, &id))?
                    {
                        items.push(TicketAttachment {
                            attachment: view(&repo, &a),
                            referenced_by: vec![source.clone()],
                        });
                    }
                }
            }
            let positioned = items
                .into_iter()
                .enumerate()
                .map(|(i, item)| (store::Key::new().u(i as u64).bytes(), item))
                .collect();
            let (items, next_cursor) = page.slice(positioned);
            Ok(Page { items, next_cursor })
        })
        .await
}

/// Deletes the attachments no ticket refers to once their grace has passed,
/// the records whose file is gone, and the files that have had no record for
/// as long. Returns how many it deleted.
pub async fn sweep(app: &App) -> AppResult<usize> {
    // During a backup it leaves them to the next sweep.
    let Ok(_deletions) = app.file_deletions.try_read() else {
        return Ok(0);
    };
    let cutoff = util::format_time(chrono::Utc::now() - chrono::Duration::seconds(GRACE_SECS));
    let all: Vec<Attachment> = app
        .store
        .read(|r| r.prefix_all(store::ATTACHMENTS, &[]))
        .await?;
    let mut kept = HashSet::new();
    let mut removed = 0;
    for a in all {
        let path = file(app, &a.id);
        // One that cannot be looked at is kept, and the others are swept.
        let present = tokio::fs::try_exists(&path).await.unwrap_or(true);
        if present && a.created_at > cutoff {
            kept.insert(a.id);
            continue;
        }
        let key = Attachment::key(&a.repo, &a.id);
        // Checked in the transaction that deletes it, so a ticket written
        // meanwhile keeps it.
        let deleted = app
            .store
            .write(move |w| {
                if present
                    && !w
                        .prefix_raw(store::IX_ATTACHMENT_REFS, &key, None, 1)?
                        .is_empty()
                {
                    return Ok(false);
                }
                w.del(store::ATTACHMENTS, &key)?;
                Ok(true)
            })
            .await?;
        if deleted {
            let _ = tokio::fs::remove_file(&path).await;
            removed += 1;
        } else {
            kept.insert(a.id);
        }
    }
    let Ok(entries) = std::fs::read_dir(app.layout.attachments()) else {
        return Ok(removed);
    };
    let grace = Duration::from_secs(GRACE_SECS as u64);
    for e in entries.flatten() {
        let old = e
            .metadata()
            .and_then(|m| m.modified())
            .is_ok_and(|t| t.elapsed().is_ok_and(|age| age > grace));
        if old
            && !kept.contains(e.file_name().to_string_lossy().as_ref())
            && std::fs::remove_file(e.path()).is_ok()
        {
            removed += 1;
        }
    }
    Ok(removed)
}

/// Sweeps at start and every hour.
pub fn start(app: crate::app::Shared) {
    tokio::spawn(async move {
        loop {
            match sweep(&app).await {
                Ok(0) => {}
                Ok(n) => {
                    tracing::info!("attachments: removed {n} unreferenced attachment(s) or file(s)")
                }
                Err(e) => tracing::warn!("attachments: sweep failed: {e}"),
            }
            tokio::time::sleep(Duration::from_secs(3600)).await;
        }
    });
}
