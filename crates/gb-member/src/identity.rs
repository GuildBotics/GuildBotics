//! Stable IDs retain the frozen host's JSON separators and ASCII escaping.
use serde::Serialize;
use serde_json::Value;
pub fn bytes(value: impl Serialize) -> Vec<u8> {
    fn render(value: &Value) -> String {
        match value {
            Value::Array(values) => format!(
                "[{}]",
                values.iter().map(render).collect::<Vec<_>>().join(", ")
            ),
            Value::Object(values) => format!(
                "{{{}}}",
                values
                    .iter()
                    .map(|(key, value)| format!(
                        "{}: {}",
                        render(&Value::String(key.clone())),
                        render(value)
                    ))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Value::String(_) => serde_json::to_string(value)
                .expect("JSON string")
                .chars()
                .map(|c| {
                    if c.is_ascii() {
                        c.to_string()
                    } else {
                        let mut units = [0; 2];
                        c.encode_utf16(&mut units)
                            .iter()
                            .map(|v| format!("\\u{v:04x}"))
                            .collect()
                    }
                })
                .collect(),
            _ => value.to_string(),
        }
    }
    render(&serde_json::to_value(value).expect("identity JSON")).into_bytes()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ids_use_frozen_separators_and_surrogate_pairs() {
        assert_eq!(
            String::from_utf8(bytes(("service", "日本語😀", 2))).unwrap(),
            "[\"service\", \"\\u65e5\\u672c\\u8a9e\\ud83d\\ude00\", 2]"
        );
    }
}
