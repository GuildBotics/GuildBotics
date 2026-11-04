use crate::{WriteScope, valid_repository_path};
use gb_ports::*;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::{Mutex, MutexGuard},
};
static LOCK: Mutex<()> = Mutex::new(());
pub(super) fn lock() -> Result<MutexGuard<'static, ()>, ServiceError> {
    LOCK.lock().map_err(|_| ServiceError::Failed)
}
pub(super) fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}
pub(super) fn encode(s: &str) -> String {
    percent_encoding::utf8_percent_encode(s, percent_encoding::NON_ALPHANUMERIC)
        .to_string()
        .replace("%2D", "-")
        .replace("%5F", "_")
        .replace("%2E", ".")
        .replace("%7E", "~")
}
#[derive(Clone, Debug)]
pub(super) struct Ref {
    pub kind: WorkKind,
    pub place: String,
    pub name: String,
}
impl Ref {
    pub fn locate(url: &str, kind: WorkKind) -> Result<Self, ServiceError> {
        if url.split('/').any(|p| p == "." || p == "..") || url.contains('%') {
            return Err(ServiceError::NotFound);
        }
        let u = url::Url::parse(url).map_err(|_| ServiceError::NotFound)?;
        let path = u.path().trim_matches('/');
        let (place, name) = if kind == WorkKind::PullRequest {
            path.rsplit_once("/pulls/")
        } else {
            path.rsplit_once('/')
        }
        .ok_or(ServiceError::NotFound)?;
        let n = if kind == WorkKind::Ticket {
            name.strip_prefix("T-").unwrap_or("")
        } else {
            name
        };
        if u.scheme() != "local"
            || u.host_str()
                != Some(if kind == WorkKind::Ticket {
                    "tickets"
                } else {
                    "code"
                })
            || u.query().is_some()
            || u.fragment().is_some()
            || !valid_repository_path(place)
            || n.starts_with('0')
            || n.parse::<u64>().ok().filter(|n| *n > 0).is_none()
        {
            return Err(ServiceError::NotFound);
        }
        Ok(Self {
            kind,
            place: place.into(),
            name: name.into(),
        })
    }
    pub fn url(&self) -> String {
        if self.kind == WorkKind::Ticket {
            format!("local://tickets/{}/{}", self.place, self.name)
        } else {
            format!("local://code/{}/pulls/{}", self.place, self.name)
        }
    }
    pub fn label(&self) -> String {
        if self.kind == WorkKind::Ticket {
            self.name.clone()
        } else {
            format!("!{}", self.name)
        }
    }
    pub fn target(&self, item: &Value) -> WorkTarget {
        WorkTarget {
            kind: self.kind,
            url: self.url(),
            title: text(item, "title").into(),
            label: self.label(),
        }
    }
}
pub(super) fn text<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap_or("")
}
pub(super) fn array(v: &Value, k: &str) -> Vec<Value> {
    v[k].as_array().cloned().unwrap_or_default()
}
pub(super) fn read(path: &Path) -> Result<Value, ServiceError> {
    let b = fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            ServiceError::NotFound
        } else {
            ServiceError::Failed
        }
    })?;
    serde_json::from_slice(&b).map_err(|_| ServiceError::Failed)
}
pub(super) struct Store {
    pub root: PathBuf,
    pub scope: WriteScope,
}
impl Store {
    pub fn dir(&self, kind: WorkKind, place: &str) -> PathBuf {
        self.root
            .join(if kind == WorkKind::Ticket {
                "tickets"
            } else {
                "code"
            })
            .join(encode(place))
    }
    pub fn path(&self, r: &Ref) -> PathBuf {
        let d = self.dir(r.kind, &r.place);
        if r.kind == WorkKind::Ticket {
            d.join(format!("{}.json", r.name))
        } else {
            d.join("pulls").join(format!("{}.json", r.name))
        }
    }
    pub fn load(&self, r: &Ref) -> Result<Value, ServiceError> {
        read(&self.path(r))
    }
    pub fn save(&self, r: &Ref, item: &mut Value) -> Result<(), ServiceError> {
        if let Err(error) = self.scope.authorize(&r.place) {
            tracing::warn!(provider="local", key=error.translation_key(), target=%r.place, "integration write refused");
            return Err(error);
        }
        item["updated_at"] = json!(now());
        let p = self.path(r);
        fs::create_dir_all(p.parent().ok_or(ServiceError::Failed)?)
            .map_err(|_| ServiceError::Failed)?;
        fs::write(
            p,
            serde_json::to_vec_pretty(item).map_err(|_| ServiceError::Failed)?,
        )
        .map_err(|_| ServiceError::Failed)
    }
    pub fn tracker(&self, p: &str) -> Result<Value, ServiceError> {
        if !valid_repository_path(p) {
            return Err(ServiceError::NotFound);
        }
        read(&self.dir(WorkKind::Ticket, p).join("tracker.json"))
    }
    pub fn items(
        &self,
        kind: WorkKind,
        place: Option<&str>,
    ) -> Result<Vec<(Ref, Value)>, ServiceError> {
        let mut places = if let Some(p) = place {
            vec![p.to_owned()]
        } else {
            let dir = self.root.join(if kind == WorkKind::Ticket {
                "tickets"
            } else {
                "code"
            });
            if !dir.exists() {
                vec![]
            } else {
                fs::read_dir(dir)
                    .map_err(|_| ServiceError::Failed)?
                    .map(|e| {
                        let s = e.map_err(|_| ServiceError::Failed)?.file_name();
                        percent_encoding::percent_decode_str(&s.to_string_lossy())
                            .decode_utf8()
                            .map(|s| s.into_owned())
                            .map_err(|_| ServiceError::Failed)
                    })
                    .collect::<Result<Vec<_>, _>>()?
            }
        };
        places.sort();
        let mut out = vec![];
        for p in places {
            let mut dir = self.dir(kind, &p);
            if kind == WorkKind::PullRequest {
                dir = dir.join("pulls")
            };
            if !dir.exists() {
                continue;
            }
            let mut loaded = vec![];
            for e in fs::read_dir(dir).map_err(|_| ServiceError::Failed)? {
                let path = e.map_err(|_| ServiceError::Failed)?.path();
                if path.extension().is_some_and(|s| s == "json")
                    && path.file_name().is_some_and(|s| s != "tracker.json")
                {
                    let v = read(&path)?;
                    let n = v["number"].as_u64().ok_or(ServiceError::Failed)?;
                    loaded.push((n, v));
                }
            }
            loaded.sort_by_key(|(n, _)| *n);
            for (n, v) in loaded {
                out.push((
                    Ref {
                        kind,
                        place: p.clone(),
                        name: if kind == WorkKind::Ticket {
                            format!("T-{n}")
                        } else {
                            n.to_string()
                        },
                    },
                    v,
                ))
            }
        }
        Ok(out)
    }
    pub fn new_item(
        &self,
        kind: WorkKind,
        p: &str,
        title: &str,
        body: &str,
        author: &str,
    ) -> Result<(Ref, Value), ServiceError> {
        let n = self
            .items(kind, Some(p))?
            .iter()
            .filter_map(|(_, v)| v["number"].as_u64())
            .max()
            .unwrap_or(0)
            + 1;
        Ok((
            Ref {
                kind,
                place: p.into(),
                name: if kind == WorkKind::Ticket {
                    format!("T-{n}")
                } else {
                    n.to_string()
                },
            },
            json!({"number":n,"title":title,"body":body,"author":author,"state":"open","labels":[],"assignees":[],"created_at":now(),"closed_at":null,"comments":[]}),
        ))
    }
    pub fn bare(&self, p: &str) -> PathBuf {
        self.dir(WorkKind::PullRequest, p).join("repository.git")
    }
    pub fn git_bytes(&self, p: &str, args: &[&str]) -> Result<Vec<u8>, ServiceError> {
        let bare = self.bare(p);
        if !bare.is_dir() {
            return Err(ServiceError::NotFound);
        }
        let mut c = Command::new("git");
        c.args(["-c", "credential.helper="])
            .args(args)
            .current_dir(bare);
        for (k, _) in std::env::vars_os() {
            if k.to_string_lossy().starts_with("GIT_") {
                c.env_remove(k);
            }
        }
        c.env("GIT_CONFIG_NOSYSTEM", "1")
            .env(
                "GIT_CONFIG_GLOBAL",
                if cfg!(windows) { "NUL" } else { "/dev/null" },
            )
            .env("GIT_TERMINAL_PROMPT", "0");
        let o = c.output().map_err(|_| ServiceError::Failed)?;
        if !o.status.success() {
            return Err(ServiceError::Failed);
        }
        Ok(o.stdout)
    }
    pub fn git(&self, p: &str, args: &[&str]) -> Result<String, ServiceError> {
        String::from_utf8(self.git_bytes(p, args)?)
            .map(|s| s.trim().into())
            .map_err(|_| ServiceError::Failed)
    }
    pub fn tip(&self, p: &str, branch: &str) -> Result<String, ServiceError> {
        if !self.bare(p).is_dir() {
            return Ok(String::new());
        }
        let target = format!("refs/heads/{branch}");
        let refs = self.git(
            p,
            &["for-each-ref", "--format=%(objectname) %(refname)", &target],
        )?;
        Ok(refs
            .lines()
            .find_map(|line| {
                line.split_once(' ')
                    .filter(|(_, name)| *name == target)
                    .map(|(sha, _)| sha.to_owned())
            })
            .unwrap_or_default())
    }
}
pub(super) fn entry(
    item: &Value,
    kind: &str,
    author: &str,
    body: &str,
    status: Option<&WorkflowStatus>,
) -> Value {
    let n = ["comments", "reviews", "review_comments"]
        .iter()
        .flat_map(|k| array(item, k))
        .filter_map(|v| {
            text(&v, "id")
                .rsplit_once('-')
                .and_then(|(_, n)| n.parse::<u64>().ok())
        })
        .max()
        .unwrap_or(0)
        + 1;
    let mut v = json!({"id":format!("{kind}-{n}"),"author":author,"body":body.trim_end(),"created_at":now()});
    if let Some(s) = status {
        v["status"] = s.payload()
    }
    v
}
pub(super) fn page(
    r: Resource,
    entries: Vec<Value>,
    conditions: &Value,
    cursor: &str,
) -> Result<ReadPage, ServiceError> {
    let start = if cursor.is_empty() {
        0
    } else {
        if !cursor.bytes().all(|b| b.is_ascii_digit()) {
            return Err(ServiceError::InvalidContinuation);
        }
        cursor
            .parse::<usize>()
            .map_err(|_| ServiceError::InvalidContinuation)?
    };
    let size = conditions["page_size"].as_u64().unwrap_or(30) as usize;
    let end = start.saturating_add(size);
    ReadPage::of(
        r,
        entries.iter().skip(start).take(size).cloned(),
        (end < entries.len()).then(|| end.to_string()),
        None,
    )
}
pub(super) fn file_lock(path: &Path) -> Result<fs::File, ServiceError> {
    let f = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(path)
        .map_err(|_| ServiceError::Failed)?;
    f.lock().map_err(|_| ServiceError::Failed)?;
    Ok(f)
}
