//! YAML as the Python version reads it with PyYAML: a key given twice takes
//! its last value. Only `true` and `false` (in any case) are booleans: PyYAML
//! also reads `yes`, `no`, `on` and `off` as booleans, and those stay words
//! here, so a declaration that spells a boolean so is refused rather than
//! read otherwise than it means.

use serde_json::Value;

/// The document `text` holds; null when it holds none.
pub fn value(text: &str) -> Result<Value, ()> {
    if text.trim().is_empty() {
        return Ok(Value::Null);
    }
    let options = serde_saphyr::options! {
        duplicate_keys: serde_saphyr::options::DuplicateKeyPolicy::LastWins,
        strict_booleans: true,
    };
    serde_saphyr::from_str_with_options(text, options).map_err(|_| ())
}
