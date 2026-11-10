//! Browser authentication supplied by an embedding host. Bearer tokens remain
//! Branvil credentials; a host session always identifies Branvil's owner.

pub enum Denied {
    Unauthorized,
    CsrfMismatch,
}

pub trait BrowserSession: Send + Sync {
    /// Return the live session's CSRF value, checking a supplied write value.
    fn check(&self, cookie: &str, csrf: Option<&str>) -> Result<String, Denied>;
    /// A single-use entry link and its expiry, landing on the Branvil screen.
    fn entry_link(&self) -> (String, String);
    /// Where a browser without an entry code obtains sign-in guidance.
    fn entry_path(&self) -> &str;
}
