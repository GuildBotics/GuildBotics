//! CI logs and artifacts on disk: `<data>/ci/logs/<attempt id>.log` and
//! `<data>/ci/artifacts/<attempt id>/<name>.zip`. redb only records which
//! artifacts an attempt has; the bytes never enter the database.

use std::path::PathBuf;

use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};

use super::Ci;
use super::queue;
use crate::app::App;
use crate::error::{AppError, AppResult};
use crate::store;
use crate::util;

/// An attempt id as Branvil makes them (`att_` + ULID), so it is safe as a
/// file name.
pub fn valid_attempt_id(id: &str) -> bool {
    id.strip_prefix("att_")
        .is_some_and(|u| u.len() == 26 && u.bytes().all(|b| b.is_ascii_alphanumeric()))
}

impl Ci {
    /// These attempts never reach a runner. Reconciliation also repairs a
    /// log missed if the process stopped after the queue transaction.
    pub(super) async fn write_skip_logs(
        &self,
        repo: &crate::model::Repo,
        sha: &str,
    ) -> AppResult<()> {
        let (id, sha) = (repo.id.clone(), sha.to_string());
        let attempts = self
            .inner
            .store
            .read(move |r| queue::attempts_of(r, &id, &sha))
            .await?;
        for a in attempts {
            if !a.files_pruned
                && let Some(skip) = a.path_skip
            {
                let path = self.log_path(&a.id);
                if !tokio::fs::try_exists(&path)
                    .await
                    .map_err(AppError::internal)?
                {
                    tokio::fs::create_dir_all(self.inner.layout.ci_logs())
                        .await
                        .map_err(AppError::internal)?;
                    let temporary = path.with_extension("tmp");
                    tokio::fs::write(
                        &temporary,
                        format!("[branvil] skipped: {}\n", skip.reason()),
                    )
                    .await
                    .map_err(AppError::internal)?;
                    tokio::fs::rename(temporary, path)
                        .await
                        .map_err(AppError::internal)?;
                }
            }
        }
        Ok(())
    }
    pub fn log_path(&self, id: &str) -> PathBuf {
        self.inner.layout.ci_logs().join(format!("{id}.log"))
    }

    pub fn artifact_dir(&self, id: &str) -> PathBuf {
        self.inner.layout.ci_artifacts().join(id)
    }

    pub fn artifact_path(&self, id: &str, name: &str) -> PathBuf {
        self.artifact_dir(id).join(format!("{name}.zip"))
    }

    fn log_max(&self) -> u64 {
        self.inner.config.log_max_mib << 20
    }

    pub fn artifact_max(&self) -> u64 {
        self.inner.config.artifact_max_mib << 20
    }

    /// Appends to an attempt's log up to the per-attempt limit; past it, one
    /// truncation notice is written and later bytes are dropped. Returns the
    /// log size and whether it was truncated.
    pub async fn append_log(&self, id: &str, bytes: &[u8]) -> AppResult<(u64, bool)> {
        let _serial = self.inner.log_lock.lock().await;
        let path = self.log_path(id);
        let current = tokio::fs::metadata(&path)
            .await
            .map(|m| m.len())
            .unwrap_or(0);
        let max = self.log_max();
        let notice = format!("\n[branvil] log truncated at {max} bytes\n");
        if current >= max {
            return Ok((current, true));
        }
        let mut file = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .await
            .map_err(AppError::internal)?;
        let room = (max - current) as usize;
        let truncated = bytes.len() > room;
        if truncated {
            file.write_all(&bytes[..room])
                .await
                .map_err(AppError::internal)?;
            file.write_all(notice.as_bytes())
                .await
                .map_err(AppError::internal)?;
        } else {
            file.write_all(bytes).await.map_err(AppError::internal)?;
        }
        file.flush().await.map_err(AppError::internal)?;
        let size = file.metadata().await.map_err(AppError::internal)?.len();
        Ok((size, truncated))
    }

    pub async fn log_size(&self, id: &str) -> u64 {
        tokio::fs::metadata(self.log_path(id))
            .await
            .map(|m| m.len())
            .unwrap_or(0)
    }

    /// The last `tail` bytes of the log (lossy UTF-8) and its full size.
    pub async fn log_tail(&self, id: &str, tail: u64) -> AppResult<(String, u64)> {
        let path = self.log_path(id);
        let mut file = match tokio::fs::File::open(&path).await {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok((String::new(), 0));
            }
            Err(e) => return Err(AppError::internal(e)),
        };
        let size = file.metadata().await.map_err(AppError::internal)?.len();
        let start = size.saturating_sub(tail);
        file.seek(std::io::SeekFrom::Start(start))
            .await
            .map_err(AppError::internal)?;
        let mut buf = Vec::with_capacity((size - start) as usize);
        file.take(size - start)
            .read_to_end(&mut buf)
            .await
            .map_err(AppError::internal)?;
        Ok((String::from_utf8_lossy(&buf).into_owned(), size))
    }
}

/// Deletes log and artifact files that belong to no attempt (left by a crash
/// between writing a file and recording it, or by a removed record).
pub async fn collect_orphans(app: &App) -> AppResult<usize> {
    let mut removed = 0;
    let logs = app.layout.ci_logs();
    let arts = app.layout.ci_artifacts();
    let mut candidates = Vec::new();
    for (dir, is_log) in [(logs, true), (arts, false)] {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for e in entries.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let id = if is_log {
                name.strip_suffix(".log").unwrap_or("").to_string()
            } else {
                name.clone()
            };
            candidates.push((e.path(), id));
        }
    }
    for (path, id) in candidates {
        let known = if valid_attempt_id(&id) {
            let i = id.clone();
            app.store
                .read(move |r| Ok(queue::get_attempt(r, &i)?.is_some_and(|a| !a.files_pruned)))
                .await?
        } else {
            false
        };
        if !known {
            let _ = if path.is_dir() {
                std::fs::remove_dir_all(&path)
            } else {
                std::fs::remove_file(&path)
            };
            removed += 1;
        }
    }
    Ok(removed)
}

/// Deletes the logs and artifacts of attempts finished more than
/// `ci.retention_days` ago. The attempt records stay (their checks keep
/// their conclusions); their log reads come back empty.
pub async fn apply_retention(app: &App) -> AppResult<usize> {
    let days = app.ci.config().retention_days;
    let cutoff = queue::earlier(&util::now(), days * 86_400);
    let mut done = 0;
    loop {
        let c = cutoff.clone();
        let batch = app.store.read(move |r| queue::prunable(r, &c, 256)).await?;
        if batch.is_empty() {
            return Ok(done);
        }
        for a in batch {
            // During a backup it leaves the rest to the next pass.
            let Ok(_deletions) = app.file_deletions.try_read() else {
                return Ok(done);
            };
            let _ = std::fs::remove_file(app.ci.log_path(&a.id));
            let _ = std::fs::remove_dir_all(app.ci.artifact_dir(&a.id));
            let id = a.id.clone();
            app.store.write(move |w| queue::mark_pruned(w, &id)).await?;
            done += 1;
        }
    }
}

/// The newest attempt of `sha` that has an artifact named `name`.
pub fn artifact_attempt(
    r: &impl store::Rd,
    repo: &str,
    sha: &str,
    name: &str,
) -> AppResult<Option<queue::Attempt>> {
    let mut attempts = queue::attempts_of(r, repo, sha)?;
    attempts.sort_by(|a, b| (&b.created_at, b.seq).cmp(&(&a.created_at, a.seq)));
    for a in attempts {
        if a.purpose != queue::Purpose::Check {
            continue;
        }
        let source = queue::result_attempt(r, &a)?;
        if !source.files_pruned && source.artifacts.iter().any(|x| x.name == name) {
            return Ok(Some(source));
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn attempt_ids_are_file_safe() {
        assert!(valid_attempt_id("att_01JABCDEFGHJKMNPQRSTVWXYZ0"));
        assert!(!valid_attempt_id("att_../../etc/passwd"));
        assert!(!valid_attempt_id("cmt_01JABCDEFGHJKMNPQRSTVWXYZ0"));
        assert!(!valid_attempt_id("att_01JA"));
    }
}
