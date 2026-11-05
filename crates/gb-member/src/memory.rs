//! Documents and recency use one shared-write transaction per mutation.
//! Audit writes are best effort after the document has committed.
use anyhow::{Result, ensure};
use gb_base::{
    files::SHARED_RECORD_SCHEMA_VERSION,
    shared_writes::{SharedWrites, Writer},
};
use gb_observability::redaction::Redactor;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};
use unicode_casefold::UnicodeCaseFold;

pub const MAX_MEMORY_FILE_BYTES: usize = 1_048_576;
pub struct Memory<'a> {
    pub writes: &'a SharedWrites,
    pub workspace: &'a Path,
    pub person_id: &'a str,
    /// Resolved before constructing this service, never after a mutation.
    pub device_id: String,
    pub autonomous: bool,
    pub correlation: Value,
    pub audit_sink: Option<&'a (dyn Fn(Value) -> Result<()> + Sync)>,
    /// Construct with `Redactor::with_min_length(values, 4)` for the memory boundary.
    pub redactor: &'a Redactor,
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Personal,
    Team,
}
#[derive(Default, Clone)]
pub struct Edit {
    pub title: Option<String>,
    pub summary: Option<String>,
    pub body: Option<String>,
    pub keywords: Option<Vec<String>>,
    pub add_keywords: Vec<String>,
    pub remove_keywords: Vec<String>,
    pub source: Option<Vec<Value>>,
    pub pinned: Option<bool>,
    pub kind: Option<String>,
    pub params: serde_json::Map<String, Value>,
}
struct Document {
    id: String,
    scope: Scope,
    path: PathBuf,
    meta: Value,
    body: String,
}
impl<'a> Memory<'a> {
    /// Resolve the device before any document, recency, or audit mutation.
    pub fn open(
        writes: &'a SharedWrites,
        workspace: &'a Path,
        person_id: &'a str,
        data: &Path,
        autonomous: bool,
        redactor: &'a Redactor,
        correlation: Value,
    ) -> Result<Self> {
        let device = gb_workspace::identity::ensure_device_identity(data)?;
        let memory = Self {
            writes,
            workspace,
            person_id,
            device_id: device.device_id,
            autonomous,
            redactor,
            correlation,
            audit_sink: None,
        };
        memory.check_ids()?;
        Ok(memory)
    }

    fn check_ids(&self) -> Result<()> {
        for id in [self.person_id, self.device_id.as_str()] {
            ensure!(
                !id.is_empty()
                    && id
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
                "cli.member.memory_identity"
            );
        }
        Ok(())
    }
    fn root(&self) -> PathBuf {
        self.workspace.join(".guildbotics/state/documents")
    }
    fn scope(&self, scope: Scope) -> PathBuf {
        match scope {
            Scope::Team => self.root().join("team"),
            Scope::Personal => self.root().join("personal").join(self.person_id),
        }
    }
    fn policy_allowed(&self, approved: bool) -> Result<()> {
        ensure!(!self.autonomous, "cli.member.memory_policy_autonomous");
        ensure!(approved, "cli.member.memory_policy_approval");
        Ok(())
    }
    fn read(&self, id: &str, scope: Option<Scope>) -> Result<Document> {
        self.check_ids()?;
        let id = id.trim();
        ensure!(
            !id.is_empty() && ![".", "..", "archived"].contains(&id) && !id.contains(['/', '\\']),
            "cli.member.memory_id"
        );
        for s in [Scope::Personal, Scope::Team] {
            if scope.is_some_and(|scope| scope != s) {
                continue;
            }
            let path = self.scope(s).join(id);
            let metadata = path.join("meta.yml");
            if !metadata.is_file() {
                continue;
            }
            let meta: Value = serde_saphyr::from_str(&fs::read_to_string(metadata)?)?;
            ensure!(meta.is_object(), "cli.member.memory_metadata");
            let body = match fs::read_to_string(path.join("body.md")) {
                Ok(text) => text,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
                Err(e) => return Err(e.into()),
            };
            return Ok(Document {
                id: id.into(),
                scope: s,
                path,
                meta,
                body,
            });
        }
        anyhow::bail!("cli.member.memory_missing")
    }
    fn documents(&self) -> Result<Vec<Document>> {
        self.documents_in(&[Scope::Personal, Scope::Team])
    }
    fn documents_in(&self, scopes: &[Scope]) -> Result<Vec<Document>> {
        let mut docs = Vec::new();
        for &s in scopes {
            for path in entries(&self.scope(s))? {
                let id = path.file_name().unwrap().to_string_lossy().into_owned();
                if id != "archived"
                    && path.is_dir()
                    && path.join("meta.yml").is_file()
                    && let Ok(doc) = self.read(&id, Some(s))
                {
                    docs.push(doc);
                }
            }
        }
        Ok(docs)
    }
    fn summary(&self, doc: &Document) -> Value {
        json!({"doc_id":doc.id,"path":self.logical(&doc.path),"title":doc.meta["title"].as_str().unwrap_or(""),"summary":doc.meta["summary"].as_str().unwrap_or("")})
    }
    fn logical(&self, path: &Path) -> String {
        document_path(&path.strip_prefix(self.root()).unwrap().to_string_lossy())
    }
    fn write(&self, writer: &mut Writer<'_>, doc: &Document) -> Result<()> {
        let mut meta = self.redactor.local(doc.meta.clone());
        meta["schema_version"] = SHARED_RECORD_SCHEMA_VERSION.into();
        let metadata = serde_saphyr::to_string(&meta)?;
        let body = self
            .redactor
            .local(doc.body.clone().into())
            .as_str()
            .unwrap()
            .to_owned();
        ensure!(
            metadata.len() <= MAX_MEMORY_FILE_BYTES && body.len() <= MAX_MEMORY_FILE_BYTES,
            "cli.member.memory_too_large"
        );
        writer.write_text(&doc.path.join("meta.yml"), &metadata)?;
        writer.write_text(&doc.path.join("body.md"), &body)?;
        self.touch_in(writer, &doc.id)
    }
    fn touch_in(&self, writer: &mut Writer<'_>, id: &str) -> Result<()> {
        let path = self
            .root()
            .join("recency")
            .join(format!("{}.json", self.device_id));
        writer.update_json(&path, |current| {
            let mut record = current.unwrap_or_else(
                || json!({"schema_version":SHARED_RECORD_SCHEMA_VERSION,"members":{}}),
            );
            if record["members"].get(self.person_id).is_none() {
                record["members"][self.person_id] = json!({});
            }
            record["members"][self.person_id][id] = chrono::Utc::now()
                .format("%Y-%m-%dT%H:%M:%S%.6fZ")
                .to_string()
                .into();
            while gb_base::files::dump_shared_json(&record).len() > MAX_MEMORY_FILE_BYTES {
                let oldest = record["members"]
                    .as_object()
                    .into_iter()
                    .flat_map(|m| m.iter())
                    .flat_map(|(person, uses)| {
                        uses.as_object().into_iter().flat_map(move |uses| {
                            uses.iter().map(move |(id, time)| {
                                (
                                    time.as_str().unwrap_or("").to_owned(),
                                    person.clone(),
                                    id.clone(),
                                )
                            })
                        })
                    })
                    .min();
                let Some((_, person, id)) = oldest else {
                    break;
                };
                record["members"][person]
                    .as_object_mut()
                    .unwrap()
                    .remove(&id);
            }
            Ok(Some(record))
        })?;
        Ok(())
    }
    fn audit_payload(&self, doc: &Document, fields: Vec<String>) -> Value {
        json!({"doc_id":doc.id,"path":self.logical(&doc.path),"changed_fields":fields,"_snapshot":{"scope":if doc.scope==Scope::Team{"team"}else{"personal"},"kind":doc.meta["kind"],"title":doc.meta["title"],"summary":doc.meta["summary"],"source":doc.meta["source"]}})
    }
    pub fn record(&self, scope: Scope, mut edit: Edit, approved: bool) -> Result<Value> {
        self.check_ids()?;
        let (doc,action,fields)=self.writes.transaction(|w| {
            let kind=edit.kind.as_deref().unwrap_or("note").trim();
            edit.kind=Some(if kind.is_empty(){"note"}else{kind}.into());
            let policy=edit.kind.as_deref()==Some("policy");
            if policy {
                self.policy_allowed(approved)?;edit.pinned=Some(true);
                if let Some(doc)=self.documents_in(&[Scope::Team])?.into_iter().find(|d|d.meta["kind"]=="policy") {
                    let (doc,fields)=self.update_in(w,doc,edit,approved)?;return Ok((doc,"update",fields));
                }
            }
            let scope=if policy {Scope::Team}else{scope};
            let id=loop {let id=uuid::Uuid::new_v4().simple().to_string()[..12].to_owned();if !self.scope(scope).join(&id).exists(){break id;}};
            let now=chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs,true);
            let doc=Document{id:id.clone(),scope,path:self.scope(scope).join(id),body:String::new(),meta:json!({"title":"","summary":"","keywords":[],"source":[],"created_at":now,"created_by":self.person_id,"kind":"note","pinned":policy})};
            let (doc,_)=self.update_in(w,doc,edit,approved)?;Ok((doc,"record",vec![]))
        })?;
        self.audit(action, self.audit_payload(&doc, fields));
        Ok(self.summary(&doc))
    }
    fn update_in(
        &self,
        writer: &mut Writer<'_>,
        mut doc: Document,
        mut edit: Edit,
        approved: bool,
    ) -> Result<(Document, Vec<String>)> {
        let before = doc.meta.clone();
        let body_changed = edit.body.is_some();
        if let Some(kind) = &edit.kind {
            let kind = if kind.is_empty() {
                doc.meta["kind"].as_str().unwrap_or("note")
            } else {
                kind
            };
            edit.kind = Some(
                if kind.trim().is_empty() {
                    "note"
                } else {
                    kind.trim()
                }
                .into(),
            );
        }
        let kind = edit
            .kind
            .as_deref()
            .unwrap_or(doc.meta["kind"].as_str().unwrap_or("note"));
        ensure!(["note", "policy"].contains(&kind), "cli.member.memory_kind");
        if kind == "policy" || doc.meta["kind"] == "policy" || !edit.params.is_empty() {
            ensure!(kind == "policy", "cli.member.memory_set_note");
            self.policy_allowed(approved)?;
        }
        for (key, value) in [
            ("title", edit.title),
            ("summary", edit.summary),
            ("kind", edit.kind),
        ] {
            if let Some(value) = value {
                doc.meta[key] = value.trim().into();
            }
        }
        if let Some(body) = edit.body {
            doc.body = body;
        }
        if let Some(pinned) = edit.pinned {
            doc.meta["pinned"] = pinned.into();
        }
        if let Some(source) = edit.source {
            doc.meta["source"] = source.into();
        }
        let explicit_keywords = edit.keywords.is_some();
        let mut keywords = edit.keywords.unwrap_or_else(|| {
            doc.meta["keywords"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        });
        if !explicit_keywords {
            for keyword in edit.add_keywords {
                if !keywords.contains(&keyword) {
                    keywords.push(keyword);
                }
            }
            keywords.retain(|k| !edit.remove_keywords.contains(k));
        }
        doc.meta["keywords"] = keywords.into();
        doc.meta.as_object_mut().unwrap().extend(edit.params);
        let mut fields = if body_changed {
            vec!["body".into()]
        } else {
            vec![]
        };
        for key in before
            .as_object()
            .unwrap()
            .keys()
            .chain(doc.meta.as_object().unwrap().keys())
            .collect::<std::collections::BTreeSet<_>>()
        {
            if !["created_at", "updated_at", "updated_by", "schema_version"].contains(&key.as_str())
                && before[key] != doc.meta[key]
            {
                fields.push(key.clone());
            }
        }
        doc.meta["updated_at"] = chrono::Utc::now()
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
            .into();
        doc.meta["updated_by"] = self.person_id.into();
        self.write(writer, &doc)?;
        Ok((self.read(&doc.id, Some(doc.scope))?, fields))
    }
    pub fn update(
        &self,
        id: &str,
        scope: Option<Scope>,
        edit: Edit,
        approved: bool,
    ) -> Result<Value> {
        let (doc, fields) = self
            .writes
            .transaction(|w| self.update_in(w, self.read(id, scope)?, edit, approved))?;
        self.audit("update", self.audit_payload(&doc, fields));
        Ok(self.summary(&doc))
    }
    pub fn get(&self, id: &str, scope: Option<Scope>) -> Result<Value> {
        let doc = self.read(id, scope)?;
        let mut result = self.summary(&doc);
        self.audit("get", self.audit_payload(&doc, vec![]));
        result["meta"] = doc.meta;
        result["body"] = doc.body.into();
        Ok(result)
    }
    pub fn touch(&self, id: &str, scope: Option<Scope>) -> Result<Value> {
        let doc = self.writes.transaction(|w| {
            let doc = self.read(id, scope)?;
            self.touch_in(w, &doc.id)?;
            Ok(doc)
        })?;
        self.audit("touch", self.audit_payload(&doc, vec![]));
        Ok(json!({"doc_id":doc.id,"path":self.logical(&doc.path)}))
    }
    pub fn archive(&self, id: &str, scope: Option<Scope>, approved: bool) -> Result<Value> {
        let doc = self.writes.transaction(|w| {
            let mut doc = self.read(id, scope)?;
            if doc.meta["kind"] == "policy" {
                self.policy_allowed(approved)?;
            }
            let target = self.scope(doc.scope).join("archived").join(&doc.id);
            w.check(&target)?;
            w.check(&doc.path)?;
            ensure!(!target.exists(), "cli.member.memory_archive_exists");
            fs::create_dir_all(target.parent().unwrap())?;
            fs::rename(&doc.path, &target)?;
            doc.path = target;
            Ok(doc)
        })?;
        self.audit("archive", self.audit_payload(&doc, vec![]));
        Ok(json!({"doc_id":doc.id,"path":self.logical(&doc.path)}))
    }
    pub fn promote(&self, id: &str) -> Result<Value> {
        let doc = self.writes.transaction(|w| {
            let mut doc = self.read(id, Some(Scope::Personal))?;
            ensure!(
                doc.meta["kind"] != "policy",
                "cli.member.memory_promote_policy"
            );
            let target = self.scope(Scope::Team).join(&doc.id);
            w.check(&target)?;
            w.check(&doc.path)?;
            ensure!(!target.exists(), "cli.member.memory_team_exists");
            fs::create_dir_all(target.parent().unwrap())?;
            fs::rename(&doc.path, &target)?;
            doc.path = target;
            doc.scope = Scope::Team;
            self.touch_in(w, &doc.id)?;
            Ok(doc)
        })?;
        self.audit("promote", self.audit_payload(&doc, vec!["scope".into()]));
        Ok(self.summary(&doc))
    }
    pub fn recall(&self, queries: &[String], meta_only: bool, limit: usize) -> Result<Value> {
        let started = std::time::Instant::now();
        let normalized: Vec<_> = queries
            .iter()
            .map(|q| q.trim().to_owned())
            .filter(|q| !q.is_empty())
            .collect();
        let queries: Vec<_> = normalized
            .iter()
            .map(|q| q.case_fold().collect::<String>())
            .collect();
        let mut results = Vec::new();
        for doc in self.documents()? {
            let mut haystack = serde_saphyr::to_string(&doc.meta)?;
            if !meta_only {
                haystack.push_str(&doc.body);
            }
            let folded = haystack.case_fold().collect::<String>();
            if queries.is_empty() || queries.iter().any(|q| folded.contains(q)) {
                let mut result = self.summary(&doc);
                if !queries.is_empty()
                    && let Some(line) = haystack.lines().find(|line| {
                        queries
                            .iter()
                            .any(|q| line.case_fold().collect::<String>().contains(q))
                    })
                {
                    let folded = line.case_fold().collect::<String>();
                    let index = queries.iter().find_map(|q| folded.find(q)).unwrap_or(0);
                    let chars = folded[..index].chars().count();
                    result["snippet"] = line
                        .chars()
                        .skip(chars.saturating_sub(80))
                        .take(200)
                        .collect::<String>()
                        .into();
                }
                results.push(result);
                if results.len() >= limit {
                    break;
                }
            }
        }
        self.audit(
            "recall",
            json!({"query_keywords":normalized,"result_count":results.len(),"duration_ms":started.elapsed().as_secs_f64()*1000.0}),
        );
        Ok(json!({"results":results}))
    }
    pub fn context(&self) -> Result<Value> {
        let mut latest = std::collections::BTreeMap::<String, String>::new();
        for path in entries(&self.root().join("recency"))? {
            if path.extension().is_none_or(|e| e != "json") {
                continue;
            }
            let record: Value = serde_json::from_slice(&fs::read(path)?)?;
            for (id, time) in record["members"][self.person_id]
                .as_object()
                .into_iter()
                .flat_map(|m| m.iter())
            {
                let time = time.as_str().unwrap_or("").to_owned();
                let old = latest.entry(id.clone()).or_default();
                if time > *old {
                    *old = time;
                }
            }
        }
        let mut uses: Vec<_> = latest.into_iter().collect();
        uses.sort_by(|a, b| (&b.1, &b.0).cmp(&(&a.1, &a.0)));
        let docs = self.documents()?;
        let limit = docs
            .iter()
            .find(|d| d.scope == Scope::Team && d.meta["kind"] == "policy")
            .and_then(|d| {
                d.meta["digest_n"]
                    .as_u64()
                    .or_else(|| d.meta["digest_n"].as_str().and_then(|s| s.parse().ok()))
            })
            .filter(|n| *n > 0)
            .unwrap_or(20) as usize;
        let digest: Vec<_> = uses
            .iter()
            .filter_map(|(id, _)| docs.iter().find(|d| &d.id == id))
            .take(limit)
            .map(|d| self.summary(d))
            .collect();
        let mut pinned = vec![baseline()];
        for doc in docs.iter().filter(|d| d.meta["pinned"] == true) {
            let mut p = self.summary(doc);
            p["body"] = doc.body.clone().into();
            pinned.push(p);
        }
        Ok(json!({"digest":digest,"pinned":pinned}))
    }
    fn audit(&self, action: &str, mut payload: Value) {
        let mut attributes = self.correlation["attributes"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        attributes.insert("memory.action".into(), action.into());
        if let Some(id) = payload["doc_id"].as_str() {
            attributes.insert("memory.doc_id".into(), id.into());
        }
        for field in ["result_count", "duration_ms"] {
            if let Some(value) = payload.get(field) {
                attributes.insert(format!("memory.{field}"), value.clone());
            }
        }
        if let Some(snapshot) = payload.as_object_mut().unwrap().remove("_snapshot") {
            for key in ["scope", "kind"] {
                attributes.insert(format!("memory.{key}"), snapshot[key].clone());
            }
            attributes.insert("memory.path".into(), payload["path"].clone());
            for key in ["title", "summary", "source"] {
                payload[key] = snapshot[key].clone();
            }
        }
        if payload.get("changed_fields").is_none() {
            payload["changed_fields"] = json!([]);
        }
        let message = if action == "recall" {
            format!(
                "memory recall: {}",
                payload["query_keywords"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        } else {
            format!(
                "memory {action}: {}",
                payload["title"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .or(payload["doc_id"].as_str())
                    .unwrap_or("")
            )
        };
        let mut row = self.correlation.as_object().cloned().unwrap_or_default();
        row.extend(json!({"schema_version":SHARED_RECORD_SCHEMA_VERSION,"kind":"memory","type":format!("memory.{action}"),
            "timestamp":chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs,true),"person_id":self.person_id,"message":message,"attributes":attributes,"payload":payload}).as_object().unwrap().clone());
        let row = self.redactor.redact(row.into());
        gb_observability::Diagnostics::open(self.workspace)
            .record(row.as_object().unwrap().clone());
        if let Some(sink) = self.audit_sink {
            let _ = sink(row);
            return;
        }
        let _ = crate::audit::Audit {
            writes: self.writes,
            directory: &self.root().join("memory_events"),
            device_id: &self.device_id,
            max_bytes: 8 * 1024 * 1024,
        }
        .record(row);
    }
}
fn entries(path: &Path) -> Result<Vec<PathBuf>> {
    let read = match fs::read_dir(path) {
        Ok(read) => read,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e.into()),
    };
    let mut paths = read
        .map(|e| e.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    paths.sort();
    Ok(paths)
}
pub fn baseline() -> Value {
    json!({"doc_id":"baseline-policy","path":"documents/team/baseline-policy","title":"Baseline memory policy","summary":"Default memory rules used when no team policy overrides them.","body":include_str!("baseline-policy.txt")})
}

/// A logical path has the same spelling on every writing device.
pub fn document_path(relative: &str) -> String {
    format!("documents/{}", relative.replace('\\', "/"))
}
