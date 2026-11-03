//! Resolve plain scalars with PyYAML's YAML 1.1 rules before serde sees them.
//! The event parser owns YAML syntax (quoting, folding, anchors and comments);
//! serde-saphyr still owns document construction, aliases and merge keys.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;
use serde_saphyr::granit_parser::{Event, Parser, ScalarStyle};

static INT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        "^([-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(0|[1-9][0-9_]*)|",
        "[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(:[0-5]?[0-9])+)$"
    ))
    .unwrap()
});
static FLOAT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r"^([-+]?[0-9][0-9_]*\.[0-9_]*([eE][-+][0-9]+)?|",
        r"\.[0-9][0-9_]*([eE][-+][0-9]+)?|",
        r"[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*|",
        r"[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN))$"
    ))
    .unwrap()
});

fn resolved(text: &str) -> Result<String, ()> {
    match text {
        "" | "~" | "null" | "Null" | "NULL" => return Ok("null".into()),
        "yes" | "Yes" | "YES" | "true" | "True" | "TRUE" | "on" | "On" | "ON" => {
            return Ok("true".into());
        }
        "no" | "No" | "NO" | "false" | "False" | "FALSE" | "off" | "Off" | "OFF" => {
            return Ok("false".into());
        }
        _ => {}
    }
    let clean = text.replace('_', "");
    let negative = clean.starts_with('-');
    let unsigned = clean.trim_start_matches(['+', '-']);
    if INT.is_match(text) {
        let number = if unsigned.contains(':') {
            unsigned
                .split(':')
                .try_fold(0i128, |n, part| {
                    n.checked_mul(60)?.checked_add(part.parse::<i128>().ok()?)
                })
                .ok_or(())?
        } else {
            let (digits, radix) = if let Some(digits) = unsigned.strip_prefix("0b") {
                (digits, 2)
            } else if let Some(digits) = unsigned.strip_prefix("0x") {
                (digits, 16)
            } else if unsigned.starts_with('0') {
                (unsigned, 8)
            } else {
                (unsigned, 10)
            };
            i128::from_str_radix(digits, radix).map_err(|_| ())?
        };
        return Ok(if negative { -number } else { number }.to_string());
    }
    if FLOAT.is_match(text) {
        let number = if unsigned.contains(':') {
            let mut number = 0.0;
            let mut base = 1.0;
            for part in unsigned.rsplit(':') {
                number += part.parse::<f64>().map_err(|_| ())? * base;
                base *= 60.0;
            }
            number * if negative { -1.0 } else { 1.0 }
        } else {
            clean.parse::<f64>().map_err(|_| ())?
        };
        // Configuration travels as JSON; non-finite numbers have no JSON value.
        let number = serde_json::Number::from_f64(number).ok_or(())?;
        // Untagged text works as both a float value and a JSON mapping key.
        // Python repr uses signed, zero-padded exponents for JSON float keys.
        let text = number.to_string();
        let (sign, unsigned) = text
            .strip_prefix('-')
            .map_or(("", text.as_str()), |s| ("-", s));
        let text = if let Some(fraction) = unsigned.strip_prefix("0.0000") {
            // JSON prints 10^-5 in fixed notation; Python repr uses scientific.
            let digits = fraction.trim_end_matches('0');
            let (first, rest) = digits.split_at(1);
            let dot = if rest.is_empty() { "" } else { "." };
            format!("{sign}{first}{dot}{rest}e-5")
        } else {
            text
        };
        return Ok(if let Some((mantissa, exponent)) = text.split_once('e') {
            format!(
                "{mantissa}e{:+03}",
                exponent.parse::<i32>().map_err(|_| ())?
            )
        } else {
            text
        });
    }
    serde_json::to_string(text).map_err(|_| ())
}

/// The document `text` holds; null when it holds none.
pub fn value(text: &str) -> Result<Value, ()> {
    let mut replacements = Vec::new();
    for event in Parser::new_from_str(text) {
        let (event, span) = event.map_err(|_| ())?;
        if let Event::Scalar(scalar, ScalarStyle::Plain, _, None) = event {
            // Implicit empty nodes already deserialize as null. Preserve the
            // merge-key token so serde-saphyr can flatten inherited mappings.
            if span.start.index() != span.end.index() && scalar != "<<" {
                replacements.push((
                    span.start.byte_offset().ok_or(())?,
                    span.end.byte_offset().ok_or(())?,
                    resolved(&scalar)?,
                ));
            }
        }
    }
    let mut normalized = text.to_owned();
    for (start, end, replacement) in replacements.into_iter().rev() {
        normalized.replace_range(start..end, &replacement);
    }
    let options = serde_saphyr::options! {
        duplicate_keys: serde_saphyr::DuplicateKeyPolicy::LastWins,
        reject_unsupported_tags: true,
        legacy_octal_numbers: true,
    };
    serde_saphyr::from_str_with_options(&normalized, options).map_err(|_| ())
}

#[cfg(test)]
mod tests {
    #[test]
    fn unsupported_json_scalars() {
        // Null keys remain unsupported; non-finite numbers are never JSON values.
        for source in [
            "~: a",
            "null: a",
            "v: .inf",
            "v: .nan",
            "v: 1.5e+400",
            ".inf: a",
        ] {
            assert!(super::value(source).is_err(), "{source}");
        }
    }

    #[test]
    fn pyyaml_golden() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../../../records/golden/yaml.json")).unwrap();
        for case in fixture.as_array().unwrap() {
            assert_eq!(
                super::value(case["source"].as_str().unwrap()).ok(),
                Some(case["expected"].clone()),
                "{}",
                case["source"]
            );
        }
    }
}
