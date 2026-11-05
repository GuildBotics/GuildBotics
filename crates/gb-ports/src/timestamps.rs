//! ISO timestamps shared by workflow status and service selection.
use chrono::{DateTime, Utc};

/// Frozen timestamp parsing treats an ISO time without a zone as UTC.
pub fn parse_timestamp(value: &str) -> Option<DateTime<Utc>> {
    parse_iso_timestamp(value.trim(), true)
}

/// A workflow reset must carry a zone; an unknown reset keeps work held.
pub(crate) fn parse_retry_timestamp(value: &str) -> Option<DateTime<Utc>> {
    parse_iso_timestamp(value, false)
}

fn parse_iso_timestamp(raw: &str, allow_naive: bool) -> Option<DateTime<Utc>> {
    if let Ok(time) = DateTime::parse_from_rfc3339(raw) {
        return Some(time.with_timezone(&Utc));
    }
    // ISO calendar and week dates, with optional time and zone. Chrono owns
    // calendar validation; the spelling is normalized before its time parser.
    for (width, format, monday) in [
        (10, "%Y-%m-%d", false),
        (8, "%Y%m%d", false),
        (10, "%G-W%V-%u", false),
        (8, "%GW%V%u", false),
        (8, "%G-W%V-%u", true),
        (7, "%GW%V%u", true),
    ] {
        let Some(part) = raw.get(..width) else {
            continue;
        };
        let date = if monday {
            format!("{part}{}1", if part.contains('-') { "-" } else { "" })
        } else {
            part.to_string()
        };
        let Ok(day) = chrono::NaiveDate::parse_from_str(&date, format) else {
            continue;
        };
        let rest = &raw[width..];
        if rest.is_empty() {
            return allow_naive.then(|| day.and_hms_opt(0, 0, 0).unwrap().and_utc());
        }
        let separator = rest.chars().next()?;
        let value = format!(
            "{}T{}",
            day.format("%Y-%m-%d"),
            &rest[separator.len_utf8()..]
        );
        for time in ["%H:%M:%S%.f", "%H:%M", "%H", "%H%M%S%.f", "%H%M"] {
            let pattern = format!("%Y-%m-%dT{time}");
            if allow_naive && let Ok(time) = chrono::NaiveDateTime::parse_from_str(&value, &pattern)
            {
                return Some(time.and_utc());
            }
            for zone in ["%#z", "%::z"] {
                if let Ok(time) = DateTime::parse_from_str(&value, &format!("{pattern}{zone}")) {
                    return Some(time.with_timezone(&Utc));
                }
            }
        }
    }
    None
}
