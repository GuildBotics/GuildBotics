//! Card ranks: strings over `0-9a-z` that sort lexicographically, with a new
//! rank always available between two neighbours (fractional indexing), so a
//! move rewrites one card only. Ranks are shown as `0|<digits>`.

const DIGITS: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";
const PREFIX: &str = "0|";

fn digit(c: u8) -> usize {
    DIGITS.iter().position(|d| *d == c).unwrap_or(0)
}

/// A key strictly between `a` and `b` (`""` is below everything, `None` above).
/// Keys never end in `0`, which keeps a key available below every key.
fn midpoint(a: &[u8], b: Option<&[u8]>) -> Vec<u8> {
    if let Some(b) = b {
        let mut n = 0;
        while n < b.len() && a.get(n).copied().unwrap_or(b'0') == b[n] {
            n += 1;
        }
        if n > 0 {
            let mut out = b[..n].to_vec();
            out.extend(midpoint(a.get(n..).unwrap_or(&[]), Some(&b[n..])));
            return out;
        }
    }
    let da = a.first().map(|c| digit(*c)).unwrap_or(0);
    let db = b
        .and_then(|b| b.first())
        .map(|c| digit(*c))
        .unwrap_or(DIGITS.len());
    if db.saturating_sub(da) > 1 {
        return vec![DIGITS[(da + db) / 2]];
    }
    match b {
        Some(b) if b.len() > 1 => vec![b[0]],
        _ => {
            let mut out = vec![DIGITS[da]];
            out.extend(midpoint(a.get(1..).unwrap_or(&[]), None));
            out
        }
    }
}

/// A rank between two ranks (either may be absent).
pub fn between(before: Option<&str>, after: Option<&str>) -> String {
    let a = before.map(strip).unwrap_or_default();
    // Neighbours out of order (never written by Branvil) fall back to "after
    // `before`" instead of panicking.
    let b = after.map(strip).filter(|b| a.as_str() < b.as_str());
    let key = midpoint(a.as_bytes(), b.as_deref().map(str::as_bytes));
    format!("{PREFIX}{}", String::from_utf8(key).unwrap_or_default())
}

fn strip(rank: &str) -> String {
    rank.strip_prefix(PREFIX).unwrap_or(rank).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranks_stay_ordered_under_repeated_inserts() {
        let mut ranks: Vec<String> = vec![between(None, None)];
        // Insert at the front, the back, and repeatedly between the first two.
        for i in 0..300 {
            let r = match i % 3 {
                0 => between(None, Some(&ranks[0])),
                1 => between(Some(ranks.last().unwrap()), None),
                _ => between(Some(&ranks[0]), Some(&ranks[1])),
            };
            match i % 3 {
                0 => ranks.insert(0, r),
                1 => ranks.push(r),
                _ => ranks.insert(1, r),
            }
            let mut sorted = ranks.clone();
            sorted.sort();
            assert_eq!(sorted, ranks, "order broke at insert {i}");
            let mut dedup = sorted.clone();
            dedup.dedup();
            assert_eq!(dedup.len(), sorted.len());
        }
        assert!(
            ranks
                .iter()
                .all(|r| r.starts_with("0|") && !r.ends_with('0'))
        );
    }
}
