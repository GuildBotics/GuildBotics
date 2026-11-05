//! Required replay evidence. Unlike optional diagnostics, every failure reaches
//! the caller so a judgment cannot omit work without its durable input.
use gb_observability::redaction::Redactor;
use serde_json::{Value, json};
use std::{io::Write, path::PathBuf, sync::OnceLock};
type Adoption = Box<dyn Fn(&Value) -> Result<(), String> + Send + Sync>;

pub struct RequiredIo {
    directory: PathBuf,
    trace: String,
    person: String,
    masks: OnceLock<Result<Redactor, String>>,
    load: Box<dyn Fn() -> Result<Redactor, String> + Send + Sync>,
    adopt: Option<Adoption>,
}

impl RequiredIo {
    pub fn new(
        workspace: PathBuf,
        trace: String,
        person: String,
        load: impl Fn() -> Result<Redactor, String> + Send + Sync + 'static,
    ) -> Self {
        Self {
            directory: workspace.join(".guildbotics/local/run/required-io"),
            trace,
            person,
            masks: OnceLock::new(),
            load: Box::new(load),
            adopt: None,
        }
    }

    pub fn with_adoption(
        mut self,
        adopt: impl Fn(&Value) -> Result<(), String> + Send + Sync + 'static,
    ) -> Self {
        self.adopt = Some(Box::new(adopt));
        self
    }

    pub fn write(&self, id: &str, payload: Value) -> Result<(), String> {
        if id.is_empty()
            || id.len() > 64
            || !id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err("Invalid IO record ID".into());
        }
        let masks = self
            .masks
            .get_or_init(|| (self.load)())
            .as_ref()
            .map_err(Clone::clone)?;
        let record = mask(
            masks,
            json!({"trace_id":self.trace,"person_id":self.person,
            "timestamp":chrono::Utc::now().to_rfc3339(),"payload":payload}),
        );
        let bytes = serde_json::to_vec(&record).map_err(|_| "Required IO serialization failed")?;
        std::fs::create_dir_all(&self.directory).map_err(|_| "Required IO store unavailable")?;
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        let mut file = options
            .open(self.directory.join(format!("{id}.json")))
            .map_err(|_| "Required IO record could not be created")?;
        file.write_all(&bytes)
            .and_then(|()| file.write_all(b"\n"))
            .and_then(|()| file.sync_all())
            .map_err(|_| "Required IO record could not be persisted".to_owned())?;
        if let Some(adopt) = &self.adopt {
            adopt(&record["payload"])?;
        }
        Ok(())
    }
}

fn mask(masks: &Redactor, value: Value) -> Value {
    match value {
        Value::Object(entries) => Value::Object(
            entries
                .into_iter()
                .map(|(key, value)| {
                    let key = masks.local(Value::String(key)).as_str().unwrap().to_owned();
                    (key, mask(masks, value))
                })
                .collect(),
        ),
        Value::Array(entries) => Value::Array(
            entries
                .into_iter()
                .map(|value| mask(masks, value))
                .collect(),
        ),
        other => masks.local(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    #[test]
    fn durable_judgment_promotes_only_its_bound_threads_effort_and_never_downgrades_it() {
        let dir = tempfile::tempdir().unwrap();
        let state = Arc::new(crate::chat_state::Store {
            workspace: dir.path().into(),
            writes: Arc::new(gb_base::shared_writes::SharedWrites::new(
                dir.path().into(),
                Some(dir.path().into()),
            )),
            limit: 500,
        });
        let subject = serde_json::from_value(json!({"service":"local","channel_id":"C1","thread_id":"T1","event_id":"E1","self_user_id":"local-aiko"})).unwrap();
        state
            .update_thread(
                "local",
                "aiko",
                "C1",
                "T1",
                &json!({"thread_topic":"keep","effort":"default"}),
            )
            .unwrap();
        let bound = state.clone();
        let directory = dir.path().join(".guildbotics/local/run/required-io");
        let io = RequiredIo::new(dir.path().into(), "trace".into(), "aiko".into(), || {
            Ok(Redactor::default())
        })
        .with_adoption(move |payload| {
            let id = payload["evaluation_id"].as_str().unwrap();
            assert!(directory.join(format!("{id}.json")).is_file());
            bound
                .adopt_effort(&subject, "aiko", payload)
                .map_err(|_| "not adopted".into())
        });
        for (id, person, effort, accepted) in [
            ("a0", "aiko", "high", true),
            ("a1", "aiko", "default", true),
            ("a2", "other", "high", false),
        ] {
            assert_eq!(io.write(id,json!({"adopt_response_effort":true,"evaluation_id":id,"person_id":person,"selection":{"route":"agent","response_effort":effort}})).is_ok(),accepted);
            let thread = state.thread("local", "aiko", "C1", "T1").unwrap();
            assert_eq!(thread["effort"], "high");
            assert_eq!(thread["thread_topic"], "keep");
        }
        assert_eq!(
            state.thread("local", "aiko", "C1", "other").unwrap(),
            json!({})
        );
        assert_eq!(
            state.thread("local", "other", "C1", "T1").unwrap(),
            json!({})
        );
        for (index, (stored, candidate, expected)) in [
            ("", Some("default"), "default"),
            ("", Some("high"), "high"),
            ("default", Some("high"), "high"),
            ("high", Some("default"), "high"),
            ("high", None, "high"),
            ("default", None, "default"),
            ("", None, ""),
        ]
        .into_iter()
        .enumerate()
        {
            let thread = format!("effort-{index}");
            state
                .update_thread(
                    "local",
                    "aiko",
                    "C1",
                    &thread,
                    &json!({"effort":stored,"thread_topic":"keep"}),
                )
                .unwrap();
            let subject=serde_json::from_value(json!({"service":"local","channel_id":"C1","thread_id":thread,"event_id":"event","self_user_id":"local-aiko"})).unwrap();
            state.adopt_effort(&subject,"aiko",&json!({"adopt_response_effort":true,"person_id":"aiko","selection":{"route":"agent","response_effort":candidate}})).unwrap();
            assert_eq!(
                state.thread("local", "aiko", "C1", &thread).unwrap()["effort"],
                expected
            );
        }
    }

    #[test]
    fn replay_input_is_full_redacted_and_exclusive_and_masks_are_loaded_once() {
        let dir = tempfile::tempdir().unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let store = RequiredIo::new(
            dir.path().into(),
            "trace".into(),
            "aiko".into(),
            move || {
                observed.fetch_add(1, Ordering::SeqCst);
                Ok(Redactor::with_min_length(["xyz".into()], 1))
            },
        );
        let text = "long input ".repeat(1000);
        store
            .write("a0", json!({"prompt":text,"xyz-key":"xyz"}))
            .unwrap();
        store.write("a1", json!({"phase":"resolved"})).unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let record: Value = serde_json::from_slice(
            &std::fs::read(
                dir.path()
                    .join(".guildbotics/local/run/required-io/a0.json"),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(record["payload"]["prompt"], text);
        assert_eq!(record["payload"]["***-key"], "***");
        assert!(store.write("a0", json!({})).is_err());
        for id in ["", "../escape", "ABC", "0/1"] {
            assert!(store.write(id, json!({})).is_err());
        }
    }

    #[test]
    fn unavailable_masks_and_unwritable_store_propagate_instead_of_acknowledging() {
        let dir = tempfile::tempdir().unwrap();
        let store = RequiredIo::new(dir.path().into(), "trace".into(), "aiko".into(), || {
            Err("masks unavailable".into())
        });
        assert!(store.write("ab", json!({})).is_err());
        assert!(!dir.path().join(".guildbotics").exists());
        std::fs::write(dir.path().join(".guildbotics"), b"not a directory").unwrap();
        let store = RequiredIo::new(dir.path().into(), "trace".into(), "aiko".into(), || {
            Ok(Redactor::default())
        });
        assert!(store.write("ab", json!({})).is_err());
    }
}
