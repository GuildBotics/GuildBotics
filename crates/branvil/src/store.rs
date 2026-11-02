//! redb access: table definitions, order-preserving composite keys, JSON record
//! encoding, and a bounded blocking executor so database work never blocks the
//! async runtime. See `docs/storage-design.md` for the tables and why each
//! exists.

use std::ops::Bound;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use redb::{
    Database, ReadTransaction, ReadableDatabase, ReadableTable, TableDefinition, WriteTransaction,
};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::sync::Semaphore;

use crate::error::{AppError, AppResult};

pub type Tbl = TableDefinition<'static, &'static [u8], &'static [u8]>;

/// The schema version this build reads and writes. A database of any other
/// version is refused instead of being misread; one of the previous version
/// is upgraded by `upgrade` (`branvil admin upgrade`, `branvil restore`).
pub const SCHEMA_VERSION: u64 = 25;

pub const META: Tbl = TableDefinition::new("meta");
pub const COUNTERS: Tbl = TableDefinition::new("counters");
pub const MEMBERS: Tbl = TableDefinition::new("members");
pub const AVATARS: Tbl = TableDefinition::new("avatars");
pub const TOKENS: Tbl = TableDefinition::new("tokens");
pub const SESSIONS: Tbl = TableDefinition::new("sessions");
pub const REPOS: Tbl = TableDefinition::new("repos");
pub const WORKSPACES: Tbl = TableDefinition::new("workspaces");
pub const TICKETS: Tbl = TableDefinition::new("tickets");
pub const COMMENTS: Tbl = TableDefinition::new("comments");
pub const ATTACHMENTS: Tbl = TableDefinition::new("attachments");
pub const LANDING: Tbl = TableDefinition::new("landing");
pub const PULLS: Tbl = TableDefinition::new("pulls");
pub const REVIEWS: Tbl = TableDefinition::new("reviews");
pub const THREADS: Tbl = TableDefinition::new("threads");
pub const INLINE: Tbl = TableDefinition::new("inline_comments");
pub const REFS: Tbl = TableDefinition::new("ref_snapshots");
pub const JOURNAL: Tbl = TableDefinition::new("ref_journal");
pub const IDEMPOTENCY: Tbl = TableDefinition::new("idempotency");
pub const CI_RUNS: Tbl = TableDefinition::new("ci_runs");
pub const CI_ATTEMPTS: Tbl = TableDefinition::new("ci_attempts");

// Derived indexes: rebuilt from the tables above by `branvil admin reindex`.
pub const IX_MEMBER_TOKENS: Tbl = TableDefinition::new("ix_member_tokens");
pub const IX_PATHS: Tbl = TableDefinition::new("ix_paths");
pub const IX_CARDS: Tbl = TableDefinition::new("ix_cards");
pub const IX_TICKET_LABELS: Tbl = TableDefinition::new("ix_ticket_labels");
pub const IX_TICKET_MENTIONS: Tbl = TableDefinition::new("ix_ticket_mentions");
pub const IX_CLOSED_TICKETS: Tbl = TableDefinition::new("ix_closed_tickets");
/// (to repository, to number, `parent` | `blocked_by`, from repository, from
/// number): the tickets that name a ticket as their parent or blocker.
pub const IX_TICKET_REFS: Tbl = TableDefinition::new("ix_ticket_refs");
/// (repository id, attachment id, ticket number, source): the ticket bodies
/// (source 0) and ticket comments (their seq) that refer to an attachment.
pub const IX_ATTACHMENT_REFS: Tbl = TableDefinition::new("ix_attachment_refs");
pub const IX_ENTRIES: Tbl = TableDefinition::new("ix_entries");
pub const IX_OPEN_PULLS: Tbl = TableDefinition::new("ix_open_pulls");
pub const IX_INVOLVED: Tbl = TableDefinition::new("ix_involved");
pub const IX_TICKET_PULLS: Tbl = TableDefinition::new("ix_ticket_pulls");
pub const IX_CLOSED_PULLS: Tbl = TableDefinition::new("ix_closed_pulls");
pub const IX_IDEMPOTENCY_TIME: Tbl = TableDefinition::new("ix_idempotency_time");
pub const IX_CI_SHA: Tbl = TableDefinition::new("ix_ci_sha");
pub const IX_CI_PENDING: Tbl = TableDefinition::new("ix_ci_pending");
pub const IX_CI_ACTIVE: Tbl = TableDefinition::new("ix_ci_active");
pub const IX_CI_WAITING: Tbl = TableDefinition::new("ix_ci_waiting");
pub const IX_CI_TREE: Tbl = TableDefinition::new("ix_ci_tree");
pub const IX_CI_UNNOTIFIED: Tbl = TableDefinition::new("ix_ci_unnotified");
pub const IX_CI_RECOVERED: Tbl = TableDefinition::new("ix_ci_recovered");
pub const IX_CI_REFRESH: Tbl = TableDefinition::new("ix_ci_refresh");

pub const PRIMARY_TABLES: &[Tbl] = &[
    META,
    COUNTERS,
    MEMBERS,
    AVATARS,
    TOKENS,
    SESSIONS,
    REPOS,
    WORKSPACES,
    TICKETS,
    COMMENTS,
    ATTACHMENTS,
    PULLS,
    LANDING,
    REVIEWS,
    THREADS,
    INLINE,
    REFS,
    JOURNAL,
    IDEMPOTENCY,
    CI_RUNS,
    CI_ATTEMPTS,
];

pub const INDEX_TABLES: &[Tbl] = &[
    IX_MEMBER_TOKENS,
    IX_PATHS,
    IX_CARDS,
    IX_TICKET_LABELS,
    IX_TICKET_MENTIONS,
    IX_CLOSED_TICKETS,
    IX_TICKET_REFS,
    IX_ATTACHMENT_REFS,
    IX_ENTRIES,
    IX_OPEN_PULLS,
    IX_INVOLVED,
    IX_TICKET_PULLS,
    IX_CLOSED_PULLS,
    IX_IDEMPOTENCY_TIME,
    IX_CI_SHA,
    IX_CI_PENDING,
    IX_CI_ACTIVE,
    IX_CI_WAITING,
    IX_CI_TREE,
    IX_CI_UNNOTIFIED,
    IX_CI_REFRESH,
    IX_CI_RECOVERED,
];

/// Order-preserving composite key. Strings are escaped (`00` → `00 FF`) and
/// terminated with `00 01`, integers are 8-byte big-endian, so element-wise
/// order equals byte order and no two element lists encode to the same bytes.
#[derive(Default, Clone)]
pub struct Key(Vec<u8>);

impl Key {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn s(mut self, value: &str) -> Self {
        for &b in value.as_bytes() {
            self.0.push(b);
            if b == 0 {
                self.0.push(0xff);
            }
        }
        self.0.extend_from_slice(&[0, 1]);
        self
    }

    pub fn u(mut self, value: u64) -> Self {
        self.0.extend_from_slice(&value.to_be_bytes());
        self
    }

    pub fn bytes(self) -> Vec<u8> {
        self.0
    }
}

/// Decodes a key made of the given element kinds (`'s'` string, `'u'` u64).
pub fn decode_key(mut raw: &[u8], kinds: &str) -> Option<Vec<KeyPart>> {
    let mut parts = Vec::new();
    for kind in kinds.chars() {
        match kind {
            's' => {
                let mut out = Vec::new();
                loop {
                    let (&b, rest) = raw.split_first()?;
                    raw = rest;
                    if b == 0 {
                        let (&next, rest) = raw.split_first()?;
                        raw = rest;
                        match next {
                            0xff => out.push(0),
                            1 => break,
                            _ => return None,
                        }
                    } else {
                        out.push(b);
                    }
                }
                parts.push(KeyPart::S(String::from_utf8(out).ok()?));
            }
            'u' => {
                if raw.len() < 8 {
                    return None;
                }
                let (n, rest) = raw.split_at(8);
                raw = rest;
                parts.push(KeyPart::U(u64::from_be_bytes(n.try_into().ok()?)));
            }
            _ => return None,
        }
    }
    raw.is_empty().then_some(parts)
}

#[derive(Debug, Clone, PartialEq)]
pub enum KeyPart {
    S(String),
    U(u64),
}

impl KeyPart {
    pub fn s(&self) -> &str {
        match self {
            KeyPart::S(s) => s,
            KeyPart::U(_) => "",
        }
    }
    pub fn u(&self) -> u64 {
        match self {
            KeyPart::U(n) => *n,
            KeyPart::S(_) => 0,
        }
    }
}

/// The smallest byte string greater than every string starting with `prefix`.
pub fn prefix_end(prefix: &[u8]) -> Option<Vec<u8>> {
    let mut end = prefix.to_vec();
    while let Some(last) = end.pop() {
        if last < 0xff {
            end.push(last + 1);
            return Some(end);
        }
    }
    None
}

pub type Raw = (Vec<u8>, Vec<u8>);

/// Read access shared by read and write transactions.
pub trait Rd {
    fn get_raw(&self, table: Tbl, key: &[u8]) -> AppResult<Option<Vec<u8>>>;
    fn range_raw(
        &self,
        table: Tbl,
        start: Bound<Vec<u8>>,
        end: Bound<Vec<u8>>,
        limit: usize,
    ) -> AppResult<Vec<Raw>>;

    /// Like `range_raw`, from the end of the range backwards.
    fn range_rev_raw(
        &self,
        table: Tbl,
        start: Bound<Vec<u8>>,
        end: Bound<Vec<u8>>,
        limit: usize,
    ) -> AppResult<Vec<Raw>>;

    fn get<T: DeserializeOwned>(&self, table: Tbl, key: &[u8]) -> AppResult<Option<T>> {
        match self.get_raw(table, key)? {
            Some(bytes) => Ok(Some(decode(&bytes)?)),
            None => Ok(None),
        }
    }

    fn exists(&self, table: Tbl, key: &[u8]) -> AppResult<bool> {
        Ok(self.get_raw(table, key)?.is_some())
    }

    /// Up to `limit` entries whose key starts with `prefix`, strictly after
    /// `after` when given.
    fn prefix_raw(
        &self,
        table: Tbl,
        prefix: &[u8],
        after: Option<&[u8]>,
        limit: usize,
    ) -> AppResult<Vec<Raw>> {
        let start = match after {
            Some(a) => Bound::Excluded(a.to_vec()),
            None => Bound::Included(prefix.to_vec()),
        };
        let end = match prefix_end(prefix) {
            Some(e) => Bound::Excluded(e),
            None => Bound::Unbounded,
        };
        self.range_raw(table, start, end, limit)
    }

    /// Every record under `prefix`, decoded.
    fn prefix_all<T: DeserializeOwned>(&self, table: Tbl, prefix: &[u8]) -> AppResult<Vec<T>> {
        self.prefix_raw(table, prefix, None, usize::MAX)?
            .iter()
            .map(|(_, v)| decode(v))
            .collect()
    }

    fn prefix_keys(&self, table: Tbl, prefix: &[u8]) -> AppResult<Vec<Vec<u8>>> {
        Ok(self
            .prefix_raw(table, prefix, None, usize::MAX)?
            .into_iter()
            .map(|(k, _)| k)
            .collect())
    }
}

fn range_of<T: ReadableTable<&'static [u8], &'static [u8]>>(
    table: &T,
    start: Bound<Vec<u8>>,
    end: Bound<Vec<u8>>,
    limit: usize,
    reverse: bool,
) -> AppResult<Vec<Raw>> {
    let bounds = (
        start.as_ref().map(|v| v.as_slice()),
        end.as_ref().map(|v| v.as_slice()),
    );
    let range = table.range::<&[u8]>(bounds)?;
    let items: Box<dyn Iterator<Item = _>> = if reverse {
        Box::new(range.rev())
    } else {
        Box::new(range)
    };
    let mut out = Vec::new();
    for item in items {
        let (k, v) = item?;
        out.push((k.value().to_vec(), v.value().to_vec()));
        if out.len() >= limit {
            break;
        }
    }
    Ok(out)
}

impl Rd for ReadTransaction {
    fn get_raw(&self, table: Tbl, key: &[u8]) -> AppResult<Option<Vec<u8>>> {
        let t = self.open_table(table)?;
        Ok(t.get(key)?.map(|v| v.value().to_vec()))
    }

    fn range_raw(
        &self,
        table: Tbl,
        start: Bound<Vec<u8>>,
        end: Bound<Vec<u8>>,
        limit: usize,
    ) -> AppResult<Vec<Raw>> {
        let t = self.open_table(table)?;
        range_of(&t, start, end, limit, false)
    }

    fn range_rev_raw(
        &self,
        table: Tbl,
        start: Bound<Vec<u8>>,
        end: Bound<Vec<u8>>,
        limit: usize,
    ) -> AppResult<Vec<Raw>> {
        let t = self.open_table(table)?;
        range_of(&t, start, end, limit, true)
    }
}

/// A write transaction. Every method opens the table for the one operation, so
/// helpers can be combined freely.
pub struct Wtx(pub WriteTransaction);

impl Rd for Wtx {
    fn get_raw(&self, table: Tbl, key: &[u8]) -> AppResult<Option<Vec<u8>>> {
        let t = self.0.open_table(table)?;
        let value = t.get(key)?.map(|v| v.value().to_vec());
        Ok(value)
    }

    fn range_raw(
        &self,
        table: Tbl,
        start: Bound<Vec<u8>>,
        end: Bound<Vec<u8>>,
        limit: usize,
    ) -> AppResult<Vec<Raw>> {
        let t = self.0.open_table(table)?;
        range_of(&t, start, end, limit, false)
    }

    fn range_rev_raw(
        &self,
        table: Tbl,
        start: Bound<Vec<u8>>,
        end: Bound<Vec<u8>>,
        limit: usize,
    ) -> AppResult<Vec<Raw>> {
        let t = self.0.open_table(table)?;
        range_of(&t, start, end, limit, true)
    }
}

impl Wtx {
    pub fn put<T: Serialize>(&self, table: Tbl, key: &[u8], value: &T) -> AppResult<()> {
        let bytes = encode(value)?;
        self.put_raw(table, key, &bytes)
    }

    pub fn put_raw(&self, table: Tbl, key: &[u8], value: &[u8]) -> AppResult<()> {
        let mut t = self.0.open_table(table)?;
        t.insert(key, value)?;
        Ok(())
    }

    pub fn del(&self, table: Tbl, key: &[u8]) -> AppResult<bool> {
        let mut t = self.0.open_table(table)?;
        let removed = t.remove(key)?.is_some();
        Ok(removed)
    }

    /// Removes every entry of a table (used by the index rebuild).
    pub fn clear(&self, table: Tbl) -> AppResult<()> {
        let mut t = self.0.open_table(table)?;
        t.retain(|_, _| false)?;
        Ok(())
    }

    /// The next value of a named counter, starting at 1.
    pub fn next(&self, counter: &str) -> AppResult<u64> {
        let key = Key::new().s(counter).bytes();
        let current: u64 = self.get(COUNTERS, &key)?.unwrap_or(0);
        let next = current + 1;
        self.put(COUNTERS, &key, &next)?;
        Ok(next)
    }
}

pub fn encode<T: Serialize>(value: &T) -> AppResult<Vec<u8>> {
    serde_json::to_vec(value).map_err(|e| AppError::internal(format!("encode: {e}")))
}

pub fn decode<T: DeserializeOwned>(bytes: &[u8]) -> AppResult<T> {
    serde_json::from_slice(bytes).map_err(|e| AppError::internal(format!("decode: {e}")))
}

/// Counts of values in buckets about 19% wide: values below 4 have a bucket
/// each, and every power-of-two range above is split into four.
pub struct Histogram {
    buckets: [AtomicU64; Self::BUCKETS],
    sum: AtomicU64,
    max: AtomicU64,
}

impl Histogram {
    /// Up to 2^41 (microseconds: 25 days); larger values share the last bucket.
    const BUCKETS: usize = 4 + 4 * 39;

    fn new() -> Self {
        Self {
            buckets: std::array::from_fn(|_| AtomicU64::new(0)),
            sum: AtomicU64::new(0),
            max: AtomicU64::new(0),
        }
    }

    fn bucket(value: u64) -> usize {
        if value < 4 {
            return value as usize;
        }
        let exp = (u64::BITS - 1 - value.leading_zeros()) as usize;
        let sub = ((value >> (exp - 2)) & 3) as usize;
        (4 + (exp - 2) * 4 + sub).min(Self::BUCKETS - 1)
    }

    /// The largest value of a bucket.
    fn upper(bucket: usize) -> u64 {
        if bucket < 4 {
            return bucket as u64;
        }
        let exp = (bucket - 4) / 4 + 2;
        let sub = ((bucket - 4) % 4) as u64;
        ((5 + sub) << (exp - 2)) - 1
    }

    pub fn record(&self, value: u64) {
        self.buckets[Self::bucket(value)].fetch_add(1, Ordering::Relaxed);
        self.sum.fetch_add(value, Ordering::Relaxed);
        self.max.fetch_max(value, Ordering::Relaxed);
    }

    /// `{"count", "sum", "max", "buckets": [[largest value of the bucket, count], ...]}`
    /// with the non-empty buckets only.
    pub fn snapshot(&self) -> serde_json::Value {
        let buckets: Vec<(u64, u64)> = self
            .buckets
            .iter()
            .enumerate()
            .map(|(i, b)| (Self::upper(i), b.load(Ordering::Relaxed)))
            .filter(|(_, n)| *n > 0)
            .collect();
        serde_json::json!({
            "count": buckets.iter().map(|(_, n)| n).sum::<u64>(),
            "sum": self.sum.load(Ordering::Relaxed),
            "max": self.max.load(Ordering::Relaxed),
            "buckets": buckets,
        })
    }
}

/// How storage work queues behind the single writer and the bounded readers
/// (`GET /api/v1/admin/diagnostics`, `docs/performance.md`).
pub struct StoreStats {
    /// Write transactions waiting for the writer or running now.
    write_queue: AtomicU64,
    /// Writers already queued (waiting or running) when a writer arrived.
    write_queue_on_arrival: Histogram,
    /// Microseconds from asking to write until the writer slot was granted.
    write_wait_us: Histogram,
    /// Microseconds the writer slot was held: the transaction's work and commit.
    write_hold_us: Histogram,
    /// Microseconds of `commit()` alone (the durable sync).
    commit_us: Histogram,
    read_queue: AtomicU64,
    read_wait_us: Histogram,
    read_hold_us: Histogram,
}

impl StoreStats {
    fn new() -> Self {
        Self {
            write_queue: AtomicU64::new(0),
            write_queue_on_arrival: Histogram::new(),
            write_wait_us: Histogram::new(),
            write_hold_us: Histogram::new(),
            commit_us: Histogram::new(),
            read_queue: AtomicU64::new(0),
            read_wait_us: Histogram::new(),
            read_hold_us: Histogram::new(),
        }
    }

    pub fn snapshot(&self) -> serde_json::Value {
        serde_json::json!({
            "writes": {
                "queued": self.write_queue.load(Ordering::Relaxed),
                "queue_on_arrival": self.write_queue_on_arrival.snapshot(),
                "wait_us": self.write_wait_us.snapshot(),
                "hold_us": self.write_hold_us.snapshot(),
                "commit_us": self.commit_us.snapshot(),
            },
            "reads": {
                "queued": self.read_queue.load(Ordering::Relaxed),
                "wait_us": self.read_wait_us.snapshot(),
                "hold_us": self.read_hold_us.snapshot(),
            },
        })
    }
}

/// Counts one queued transaction until it is dropped.
struct Queued<'a>(&'a AtomicU64);

impl<'a> Queued<'a> {
    fn enter(counter: &'a AtomicU64) -> (Self, u64) {
        let ahead = counter.fetch_add(1, Ordering::Relaxed);
        (Self(counter), ahead)
    }
}

impl Drop for Queued<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

fn micros(since: Instant) -> u64 {
    since.elapsed().as_micros().try_into().unwrap_or(u64::MAX)
}

/// The database plus the bounded paths that run its synchronous work.
#[derive(Clone)]
pub struct Store {
    db: Arc<Database>,
    reads: Arc<Semaphore>,
    writes: Arc<Semaphore>,
    stats: Arc<StoreStats>,
}

impl Store {
    /// Opens (or creates) the database, creates missing tables, and checks the
    /// schema version.
    pub fn open(path: &Path, cache_bytes: usize, read_concurrency: usize) -> anyhow::Result<Self> {
        let db = open_db(path, cache_bytes)?;
        let wtx = Wtx(db.begin_write()?);
        create_tables(&wtx)?;
        match version_of(&wtx).map_err(|e| anyhow::anyhow!("{}", e.message))? {
            None => wtx
                .put(META, &version_key(), &SCHEMA_VERSION)
                .map_err(|e| anyhow::anyhow!("{}", e.message))?,
            Some(v) if v == crate::upgrade::FROM => anyhow::bail!(
                "{} has schema version {v}; this build reads version {SCHEMA_VERSION}. Take a backup with the previous build (`branvil backup`), stop the service, and run `branvil admin upgrade`",
                path.display()
            ),
            Some(v) if v != SCHEMA_VERSION => anyhow::bail!(
                "{} has schema version {v}; this build reads only version {SCHEMA_VERSION} and upgrades only version {} (start from a new data directory)",
                path.display(),
                crate::upgrade::FROM
            ),
            Some(_) => {}
        }
        wtx.0.commit()?;
        Ok(Self {
            db: Arc::new(db),
            reads: Arc::new(Semaphore::new(read_concurrency.max(1))),
            writes: Arc::new(Semaphore::new(1)),
            stats: Arc::new(StoreStats::new()),
        })
    }

    pub fn stats(&self) -> &StoreStats {
        &self.stats
    }

    /// Whether an upgrade changed the database and no `branvil admin check`
    /// has passed since.
    pub fn needs_check(&self) -> AppResult<bool> {
        self.read_sync(is_unchecked)
    }

    /// Records that the database has passed `branvil admin check`.
    pub fn checked(&self) -> AppResult<()> {
        self.write_sync(|w| w.del(META, &unchecked_key()).map(|_| ()))
    }

    pub async fn read<T, F>(&self, f: F) -> AppResult<T>
    where
        T: Send + 'static,
        F: FnOnce(&ReadTransaction) -> AppResult<T> + Send + 'static,
    {
        let (_queued, _) = Queued::enter(&self.stats.read_queue);
        let asked = Instant::now();
        let _permit = self
            .reads
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| AppError::unavailable("Storage is shutting down."))?;
        self.stats.read_wait_us.record(micros(asked));
        let db = self.db.clone();
        let stats = self.stats.clone();
        tokio::task::spawn_blocking(move || {
            let started = Instant::now();
            let txn = db.begin_read()?;
            let out = f(&txn);
            stats.read_hold_us.record(micros(started));
            out
        })
        .await
        .map_err(|e| AppError::internal(format!("storage task: {e}")))?
    }

    /// Runs `f` in one write transaction and commits it when `f` succeeds.
    /// Writers queue on an async semaphore, so at most one blocking thread
    /// waits on redb's writer lock.
    pub async fn write<T, F>(&self, f: F) -> AppResult<T>
    where
        T: Send + 'static,
        F: FnOnce(&Wtx) -> AppResult<T> + Send + 'static,
    {
        let (_queued, ahead) = Queued::enter(&self.stats.write_queue);
        self.stats.write_queue_on_arrival.record(ahead);
        let asked = Instant::now();
        let _permit = self
            .writes
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| AppError::unavailable("Storage is shutting down."))?;
        self.stats.write_wait_us.record(micros(asked));
        let db = self.db.clone();
        let stats = self.stats.clone();
        tokio::task::spawn_blocking(move || {
            let started = Instant::now();
            let out = Self::write_now(&db, Some(&stats), f);
            stats.write_hold_us.record(micros(started));
            out
        })
        .await
        .map_err(|e| AppError::internal(format!("storage task: {e}")))?
    }

    fn write_now<T>(
        db: &Database,
        stats: Option<&StoreStats>,
        f: impl FnOnce(&Wtx) -> AppResult<T>,
    ) -> AppResult<T> {
        let wtx = Wtx(db.begin_write()?);
        match f(&wtx) {
            Ok(value) => {
                let started = Instant::now();
                wtx.0.commit()?;
                if let Some(stats) = stats {
                    stats.commit_us.record(micros(started));
                }
                Ok(value)
            }
            Err(e) => {
                let _ = wtx.0.abort();
                Err(e)
            }
        }
    }

    /// Synchronous variants for the offline admin commands.
    pub fn read_sync<T>(&self, f: impl FnOnce(&ReadTransaction) -> AppResult<T>) -> AppResult<T> {
        let txn = self.db.begin_read()?;
        f(&txn)
    }

    pub fn write_sync<T>(&self, f: impl FnOnce(&Wtx) -> AppResult<T>) -> AppResult<T> {
        Self::write_now(&self.db, None, f)
    }

    /// redb's own structural check. Needs exclusive access.
    pub fn check_integrity(self) -> anyhow::Result<bool> {
        let mut db = Arc::try_unwrap(self.db)
            .map_err(|_| anyhow::anyhow!("the database is still in use"))?;
        Ok(db.check_integrity()?)
    }

    pub fn cache_stats(&self) -> redb::CacheStats {
        self.db.cache_stats()
    }

    /// Writes a copy of every table, as of one read transaction, into a new
    /// database at `path` (backups). Writers keep running meanwhile; the copy
    /// is the consistent snapshot the read transaction sees. Returns the
    /// table names and entry counts.
    pub async fn export_to(&self, path: &Path) -> AppResult<Vec<(String, u64)>> {
        let db = self.db.clone();
        let path = path.to_path_buf();
        tokio::task::spawn_blocking(move || copy_tables(&db.begin_read()?, &path))
            .await
            .map_err(|e| AppError::internal(format!("storage task: {e}")))?
    }
}

/// What an upgrade did to `db`, for the command's output.
pub fn upgrade_report(db: &Path, done: &Option<Upgraded>) -> String {
    match done {
        None => format!(
            "{} already has schema version {SCHEMA_VERSION}",
            db.display()
        ),
        Some(u) => format!(
            "upgraded {} from schema version {} to {SCHEMA_VERSION}: {}\nthe database as it was: {}\n{}",
            db.display(),
            u.from,
            u.summary,
            u.copy.display(),
            check_step(db)
        ),
    }
}

/// The next step for the upgraded database `db`: it is marked until
/// `branvil admin check` passes, and `branvil serve` refuses it till then.
pub fn check_step(db: &Path) -> String {
    let dir = db.parent().unwrap_or(Path::new(".")).display();
    format!(
        "`branvil admin check --data-dir {dir}` has to pass before `branvil serve --data-dir {dir}` starts it"
    )
}

/// What an upgrade did.
pub struct Upgraded {
    pub from: u64,
    /// The database as it was before the upgrade.
    pub copy: PathBuf,
    /// What the upgrade changed (`crate::upgrade`).
    pub summary: String,
}

/// A database of the previous schema version, opened exclusively for its
/// upgrade (`crate::upgrade`): read it, then `finish`. Nothing is written
/// before `finish`.
pub struct Upgrading {
    db: Database,
    path: PathBuf,
}

impl Upgrading {
    /// Opens the database at `path` for its upgrade, or returns `None` when it
    /// already has this build's version.
    pub fn open(path: &Path) -> anyhow::Result<Option<Self>> {
        if !path.exists() {
            anyhow::bail!("{} does not exist", path.display());
        }
        let db = open_db(path, 16 << 20)?;
        let version =
            version_of(&db.begin_read()?).map_err(|e| anyhow::anyhow!("{}", e.message))?;
        match version {
            Some(SCHEMA_VERSION) => Ok(None),
            Some(crate::upgrade::FROM) => Ok(Some(Self {
                db,
                path: path.to_path_buf(),
            })),
            Some(v) if v > SCHEMA_VERSION => anyhow::bail!(
                "{} has schema version {v}, newer than this build's {SCHEMA_VERSION}; run a build that reads it",
                path.display()
            ),
            other => anyhow::bail!(
                "{} has schema version {}; this build upgrades only version {}",
                path.display(),
                other.map_or("none".to_string(), |v| v.to_string()),
                crate::upgrade::FROM
            ),
        }
    }

    pub fn read<T>(&self, f: impl FnOnce(&ReadTransaction) -> AppResult<T>) -> anyhow::Result<T> {
        f(&self.db.begin_read()?).map_err(|e| anyhow::anyhow!("{}", e.message))
    }

    /// Writes a copy of the database as it is next to it
    /// (`branvil-schema-<version>-<time>.redb`, never overwritten and never
    /// removed by Branvil), then applies `apply` and this build's version in
    /// one write transaction: when `apply` fails, the database keeps the
    /// previous version. `apply` returns what it changed.
    pub fn finish(
        self,
        apply: impl FnOnce(&Wtx) -> anyhow::Result<String>,
    ) -> anyhow::Result<Upgraded> {
        let path = &self.path;
        let dir = path.parent().unwrap_or(Path::new("."));
        let name = format!(
            "branvil-schema-{}-{}.redb",
            crate::upgrade::FROM,
            chrono::Utc::now().format("%Y%m%dT%H%M%S%.3fZ")
        );
        let copy = dir.join(&name);
        let partial = dir.join(format!("{name}.partial"));
        // The copy gets its name only when complete, and never another's.
        let copied = copy_tables(&self.db.begin_read()?, &partial)
            .map_err(|e| anyhow::anyhow!("{}", e.message))
            .and_then(|_| {
                if copy.exists() {
                    anyhow::bail!("{} exists", copy.display());
                }
                Ok(std::fs::rename(&partial, &copy)?)
            });
        if let Err(e) = copied {
            let _ = std::fs::remove_file(&partial);
            anyhow::bail!(
                "copying {} to {} failed, so it was not upgraded: {e}",
                path.display(),
                copy.display()
            );
        }
        let wtx = Wtx(self.db.begin_write()?);
        create_tables(&wtx)?;
        let summary = apply(&wtx).map_err(|e| {
            anyhow::anyhow!(
                "upgrading {} failed, and it was left at schema version {}: {e}",
                path.display(),
                crate::upgrade::FROM
            )
        })?;
        wtx.put(META, &version_key(), &SCHEMA_VERSION)
            .map_err(|e| anyhow::anyhow!("{}", e.message))?;
        // Until `branvil admin check` passes, `branvil serve` refuses it.
        wtx.put(META, &unchecked_key(), &true)
            .map_err(|e| anyhow::anyhow!("{}", e.message))?;
        wtx.0.commit()?;
        Ok(Upgraded {
            from: crate::upgrade::FROM,
            copy,
            summary,
        })
    }
}

fn open_db(path: &Path, cache_bytes: usize) -> anyhow::Result<Database> {
    Database::builder()
        .set_cache_size(cache_bytes)
        .create(path)
        .map_err(|e| match e {
            redb::DatabaseError::DatabaseAlreadyOpen => anyhow::anyhow!(
                "{} is already open by another Branvil process (stop `branvil serve` first)",
                path.display()
            ),
            other => anyhow::anyhow!("cannot open {}: {other}", path.display()),
        })
}

fn create_tables(w: &Wtx) -> anyhow::Result<()> {
    for table in PRIMARY_TABLES.iter().chain(INDEX_TABLES) {
        w.0.open_table(*table)?;
    }
    Ok(())
}

fn version_key() -> Vec<u8> {
    Key::new().s("schema_version").bytes()
}

fn version_of(r: &impl Rd) -> AppResult<Option<u64>> {
    r.get(META, &version_key())
}

/// Set by an upgrade, in the transaction that applies it, and removed by a
/// passing `branvil admin check`; absent in a database no upgrade changed.
fn unchecked_key() -> Vec<u8> {
    Key::new().s("unchecked").bytes()
}

fn is_unchecked(r: &impl Rd) -> AppResult<bool> {
    Ok(r.get::<bool>(META, &unchecked_key())?.unwrap_or(false))
}

/// Whether the database at `path` was upgraded and has not passed
/// `branvil admin check` since.
pub fn needs_check(path: &Path) -> anyhow::Result<bool> {
    let db = open_db(path, 1 << 20)?;
    is_unchecked(&db.begin_read()?).map_err(|e| anyhow::anyhow!("{}", e.message))
}

/// Copies every table, as `txn` sees it, into a new database file at `path`
/// (refused when the file exists). Returns the table names and entry counts.
fn copy_tables(txn: &ReadTransaction, path: &Path) -> AppResult<Vec<(String, u64)>> {
    if txn.list_multimap_tables()?.next().is_some() {
        return Err(AppError::internal("export: multimap tables are not copied"));
    }
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| AppError::internal(format!("export: {}: {e}", path.display())))?;
    let out = Database::builder()
        .create_file(file)
        .map_err(|e| AppError::internal(format!("export: {e}")))?;
    let wtx = out.begin_write()?;
    let mut counts = Vec::new();
    for handle in txn.list_tables()? {
        let name = redb::TableHandle::name(&handle).to_string();
        let def: TableDefinition<&[u8], &[u8]> = TableDefinition::new(&name);
        let source = txn.open_table(def).map_err(|e| {
            AppError::internal(format!("export: table {name} is not a byte table: {e}"))
        })?;
        let mut target = wtx.open_table(def)?;
        let mut n = 0u64;
        for item in source.iter()? {
            let (k, v) = item?;
            target.insert(k.value(), v.value())?;
            n += 1;
        }
        counts.push((name, n));
    }
    wtx.commit()?;
    Ok(counts)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_current_schema_version_opens_and_only_the_previous_upgrades() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.redb");
        for other in [SCHEMA_VERSION - 2, SCHEMA_VERSION - 1, SCHEMA_VERSION + 1] {
            let store = Store::open(&path, 1 << 20, 1).unwrap();
            store
                .write_sync(|w| w.put(META, &version_key(), &other))
                .unwrap();
            drop(store);
            let err = Store::open(&path, 1 << 20, 1).err().unwrap().to_string();
            assert!(err.contains(&format!("schema version {other}")), "{err}");
            assert_eq!(
                err.contains("branvil admin upgrade"),
                other == crate::upgrade::FROM,
                "{err}"
            );
            if other != crate::upgrade::FROM {
                let err = Upgrading::open(&path).err().unwrap().to_string();
                let expected = if other > SCHEMA_VERSION {
                    "newer than this build's"
                } else {
                    "upgrades only version"
                };
                assert!(err.contains(expected), "{err}");
            }
            std::fs::remove_file(&path).unwrap();
        }
        Store::open(&path, 1 << 20, 1).unwrap();
        drop(Store::open(&path, 1 << 20, 1).unwrap());
        assert!(Upgrading::open(&path).unwrap().is_none());
        // While an upgrade has it open, nothing else opens it.
        let store = Store::open(&path, 1 << 20, 1).unwrap();
        store
            .write_sync(|w| w.put(META, &version_key(), &crate::upgrade::FROM))
            .unwrap();
        drop(store);
        let upgrading = Upgrading::open(&path).unwrap().unwrap();
        let err = Store::open(&path, 1 << 20, 1).err().unwrap().to_string();
        assert!(err.contains("already open"), "{err}");
        drop(upgrading);
    }

    #[test]
    fn a_failed_upgrade_leaves_the_previous_version_and_a_copy() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("branvil.redb");
        let store = Store::open(&path, 1 << 20, 1).unwrap();
        let run = br#"{"sha":"s1"}"#;
        store
            .write_sync(|w| {
                w.put_raw(CI_RUNS, b"k", run)?;
                w.put(META, &version_key(), &crate::upgrade::FROM)
            })
            .unwrap();
        drop(store);
        // The upgrade writes, then fails: nothing of it stays.
        let err = Upgrading::open(&path)
            .unwrap()
            .unwrap()
            .finish(|w| {
                w.put_raw(CI_RUNS, b"k", b"changed")
                    .map_err(|e| anyhow::anyhow!("{}", e.message))?;
                anyhow::bail!("an unexpected record")
            })
            .err()
            .unwrap()
            .to_string();
        let previous = format!("schema version {}", crate::upgrade::FROM);
        assert!(err.contains(&format!("left at {previous}")), "{err}");
        assert!(err.contains("an unexpected record"), "{err}");
        let err = Store::open(&path, 1 << 20, 1).err().unwrap().to_string();
        assert!(err.contains(&previous), "{err}");
        let db = Database::open(&path).unwrap();
        let txn = db.begin_read().unwrap();
        assert_eq!(txn.get_raw(CI_RUNS, b"k").unwrap().unwrap(), run);
        drop((txn, db));
        // The copy was written first, under a name of its own.
        let copies: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|n| n != "branvil.redb")
            .collect();
        assert_eq!(copies.len(), 1, "{copies:?}");
        assert!(
            copies[0].starts_with(&format!("branvil-schema-{}-", crate::upgrade::FROM))
                && copies[0].ends_with(".redb"),
            "{copies:?}"
        );
    }

    #[test]
    fn an_upgrade_marks_the_database_unchecked_in_its_own_transaction() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("branvil.redb");
        let previous = || {
            let store = Store::open(&path, 1 << 20, 1).unwrap();
            store
                .write_sync(|w| w.put(META, &version_key(), &crate::upgrade::FROM))
                .unwrap();
        };
        // A database no upgrade changed is not marked.
        drop(Store::open(&path, 1 << 20, 1).unwrap());
        assert!(!needs_check(&path).unwrap());
        // A failed upgrade marks nothing.
        previous();
        Upgrading::open(&path)
            .unwrap()
            .unwrap()
            .finish(|_| anyhow::bail!("an unexpected record"))
            .err()
            .unwrap();
        assert!(!needs_check(&path).unwrap());
        // One that applies marks it, until a check records it as checked.
        Upgrading::open(&path)
            .unwrap()
            .unwrap()
            .finish(|_| Ok("nothing".into()))
            .unwrap();
        assert!(needs_check(&path).unwrap());
        let store = Store::open(&path, 1 << 20, 1).unwrap();
        assert!(store.needs_check().unwrap());
        store.checked().unwrap();
        assert!(!store.needs_check().unwrap());
        drop(store);
        assert!(!needs_check(&path).unwrap());
    }

    #[test]
    fn keys_order_element_wise() {
        let a = Key::new().s("a").u(2).bytes();
        let ab = Key::new().s("ab").u(1).bytes();
        let a10 = Key::new().s("a").u(10).bytes();
        assert!(
            a < ab,
            "shorter string sorts first regardless of the next element"
        );
        assert!(a < a10, "integers sort numerically");
        // "a" + "b|c" must not collide with "a|b" + "c".
        assert_ne!(
            Key::new().s("a").s("b\0c").bytes(),
            Key::new().s("a\0b").s("c").bytes()
        );
        let decoded = decode_key(&Key::new().s("x\0y").u(7).bytes(), "su").unwrap();
        assert_eq!(decoded, vec![KeyPart::S("x\0y".into()), KeyPart::U(7)]);
    }

    #[test]
    fn prefix_end_covers_every_extension() {
        let p = Key::new().s("t").bytes();
        let end = prefix_end(&p).unwrap();
        let inside = Key::new().s("t").u(u64::MAX).bytes();
        assert!(inside < end);
        assert!(Key::new().s("u").bytes() >= end);
        assert_eq!(prefix_end(&[0xff, 0xff]), None);
    }

    #[test]
    fn histogram_buckets_cover_every_value_once() {
        for v in (0..5000u64).chain([u64::MAX / 2, u64::MAX]) {
            let b = Histogram::bucket(v);
            if b < Histogram::BUCKETS - 1 {
                assert!(v <= Histogram::upper(b), "{v} above its bucket {b}");
                assert!(
                    b == 0 || v > Histogram::upper(b - 1),
                    "{v} below bucket {b}"
                );
            }
        }
        assert_eq!(Histogram::upper(7), 7);
        assert_eq!(Histogram::upper(8), 9);
        assert_eq!(Histogram::upper(11), 15);
        let h = Histogram::new();
        for v in [0, 1, 1, 9, 1000] {
            h.record(v);
        }
        let s = h.snapshot();
        assert_eq!(s["count"], 5);
        assert_eq!(s["max"], 1000);
        assert_eq!(s["sum"], 1011);
        assert_eq!(
            s["buckets"],
            serde_json::json!([[0, 1], [1, 2], [9, 1], [1023, 1]])
        );
    }

    #[tokio::test]
    async fn writes_and_reads_are_counted() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(&dir.path().join("b.redb"), 1 << 20, 2).unwrap();
        let mut tasks = Vec::new();
        for i in 0..8u64 {
            let s = store.clone();
            tasks.push(tokio::spawn(async move {
                s.write(move |w| w.put(MEMBERS, &Key::new().u(i).bytes(), &i))
                    .await
            }));
        }
        for t in tasks {
            t.await.unwrap().unwrap();
        }
        store
            .read(|r| r.get::<u64>(MEMBERS, &Key::new().u(3).bytes()))
            .await
            .unwrap();
        let s = store.stats().snapshot();
        assert_eq!(s["writes"]["queued"], 0, "nothing is left queued");
        assert_eq!(s["writes"]["wait_us"]["count"], 8);
        assert_eq!(s["writes"]["hold_us"]["count"], 8);
        assert_eq!(s["writes"]["commit_us"]["count"], 8);
        assert_eq!(s["writes"]["queue_on_arrival"]["count"], 8);
        assert_eq!(s["reads"]["wait_us"]["count"], 1);
        assert_eq!(s["reads"]["queued"], 0);
    }

    #[test]
    fn store_survives_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("b.redb");
        {
            let store = Store::open(&path, 1 << 20, 2).unwrap();
            store
                .write_sync(|w| {
                    w.put(MEMBERS, b"k", &serde_json::json!({"x": 1}))?;
                    w.next("c")?;
                    w.next("c")
                })
                .unwrap();
        }
        let store = Store::open(&path, 1 << 20, 2).unwrap();
        let v: serde_json::Value = store.read_sync(|r| r.get(MEMBERS, b"k")).unwrap().unwrap();
        assert_eq!(v["x"], 1);
        assert_eq!(store.write_sync(|w| w.next("c")).unwrap(), 3);
    }
}
