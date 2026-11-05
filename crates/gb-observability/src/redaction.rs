//! Values cross the shared boundary once, before serialization. Secret
//! discovery belongs to the host; recording never opens a keychain.
use serde_json::Value;

pub const MAX_SHARED_TEXT_CHARS: usize = 500;
const LOCAL_ONLY: &[&str] = &["stdout", "stderr", "prompt", "response", "messages"];

/// A snapshot of the indexed secrets visible to the recording host.
/// Do not derive Debug: this object contains secret material.
#[derive(Clone, Default)]
pub struct Redactor(Vec<String>);

impl Redactor {
    /// Host callers supply the names from their secret indexes. Read only
    /// values already in the process, exactly like Python's shared boundary.
    pub fn from_environment(keys: impl IntoIterator<Item = String>) -> Self {
        Self::new(keys.into_iter().filter_map(|key| std::env::var(key).ok()))
    }

    pub fn new(values: impl IntoIterator<Item = String>) -> Self {
        let mut values: Vec<_> = values
            .into_iter()
            .filter(|v| v.chars().count() >= 4)
            .collect();
        values.sort_by_key(|v| std::cmp::Reverse(v.chars().count()));
        values.dedup();
        Self(values)
    }

    pub fn text(&self, text: &str) -> String {
        let mut masked = text.to_owned();
        for secret in &self.0 {
            masked = masked.replace(secret, "***");
        }
        masked.chars().take(MAX_SHARED_TEXT_CHARS).collect()
    }

    /// Mask a local transcript without dropping its request/response detail.
    /// The shared projection remains governed by `redact` and `text`.
    pub fn local(&self, value: Value) -> Value {
        match value {
            Value::String(mut text) => {
                for secret in &self.0 {
                    text = text.replace(secret, "***");
                }
                text.into()
            }
            Value::Array(items) => items.into_iter().map(|v| self.local(v)).collect(),
            Value::Object(items) => {
                Value::Object(items.into_iter().map(|(k, v)| (k, self.local(v))).collect())
            }
            scalar => scalar,
        }
    }

    pub fn redact(&self, value: Value) -> Value {
        match value {
            Value::String(s) => self.text(&s).into(),
            Value::Array(items) => items.into_iter().map(|v| self.redact(v)).collect(),
            Value::Object(items) => Value::Object(
                items
                    .into_iter()
                    .filter(|(k, _)| !LOCAL_ONLY.contains(&k.as_str()))
                    .map(|(k, v)| (k, self.redact(v)))
                    .collect(),
            ),
            scalar => scalar,
        }
    }
}
