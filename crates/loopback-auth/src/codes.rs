//! Single-use codes: an entry link's, a consent's.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use crate::secret::{digest, random_secret};

/// How long a single-use code stays usable.
pub const CODE_SECONDS: u64 = 60;

/// Unused single-use codes, in memory only (a restart invalidates them):
/// SHA-256 hex of the code → (what it stands for, expiry).
pub struct Codes<T>(HashMap<String, (T, Instant)>);

impl<T> Default for Codes<T> {
    fn default() -> Self {
        Codes(HashMap::new())
    }
}

impl<T> Codes<T> {
    /// Makes a code of 32 random bytes for `value`, usable once within
    /// `CODE_SECONDS`, and forgets the expired ones.
    pub fn issue(&mut self, value: T, now: Instant) -> String {
        let code = random_secret::<32>();
        self.0.retain(|_, (_, expires)| *expires > now);
        self.0.insert(
            digest(&code),
            (value, now + Duration::from_secs(CODE_SECONDS)),
        );
        code
    }

    /// Removes a code (each works once) and returns its value if it has not
    /// expired.
    pub fn take(&mut self, code: &str, now: Instant) -> Option<T> {
        self.0
            .remove(&digest(code))
            .filter(|(_, expires)| *expires > now)
            .map(|(value, _)| value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_work_once_and_expire() {
        let now = Instant::now();
        let later = now + Duration::from_secs(CODE_SECONDS);
        let mut codes = Codes::default();
        let live = codes.issue("alice", now);
        let old = codes.issue("bob", now);
        assert_ne!(live, old);
        assert_eq!(codes.take(&old, later), None);
        assert_eq!(codes.take("other", now), None);
        assert_eq!(codes.take(&live, now), Some("alice"));
        assert_eq!(codes.take(&live, now), None);
        // Only hashes are kept, and issuing forgets the expired ones.
        codes.issue("carol", now);
        let kept = codes.issue("dave", later);
        assert_eq!(codes.0.len(), 1);
        assert!(!codes.0.contains_key(&kept));
    }
}
