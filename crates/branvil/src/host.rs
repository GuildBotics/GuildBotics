//! The embedding host's authentication. Its owner is Branvil's owner: the
//! host's owner Bearer token and its browser session both identify them.

#[derive(serde::Serialize, Debug)]
pub struct Navigation {
    pub label: String,
    pub url: String,
}

pub enum Denied {
    Unauthorized,
    CsrfMismatch,
}

pub trait Host: Send + Sync {
    /// Return the live session's CSRF value, checking a supplied write value.
    fn check(&self, cookie: &str, csrf: Option<&str>) -> Result<String, Denied>;
    /// Whether `token` is the host owner's Bearer token.
    fn owner_token(&self, token: &str) -> bool;
    /// Where a browser without a session obtains sign-in guidance.
    fn entry_path(&self) -> &str;
    /// The embedding host's main screen, shown in the browser shell.
    fn navigation(&self) -> Navigation;
}
