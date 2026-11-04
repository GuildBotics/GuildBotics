use gb_ports::ServiceError;
use unicode_casefold::UnicodeCaseFold;

/// The namespace configured for member writes, independently of credentials.
/// Repository paths use ASCII names; namespace comparison follows the
/// frozen host's full Unicode case folding. An empty scope disables writes.
#[derive(Clone, Debug)]
pub struct WriteScope(String);

impl WriteScope {
    pub fn new(namespace: &str) -> Self {
        Self(namespace.into())
    }

    /// Authorize the exact repository/tracker path, not a URL or prefix.
    /// The returned path is tied to this scope and cannot be forged by a
    /// caller. Providers must still apply the service's member permissions.
    pub fn authorize<'a>(&'a self, path: &'a str) -> Result<ScopedPath<'a>, ServiceError> {
        if !self.0.is_empty()
            && valid_repository_path(path)
            && path
                .rsplit_once('/')
                .is_some_and(|(parent, _)| parent.case_fold().eq(self.0.case_fold()))
        {
            Ok(ScopedPath { scope: self, path })
        } else {
            Err(ServiceError::Failed)
        }
    }
}

/// A repository or tracker accepted by a particular configured write scope.
#[derive(Debug)]
pub struct ScopedPath<'a> {
    scope: &'a WriteScope,
    path: &'a str,
}

impl ScopedPath<'_> {
    pub fn path(&self) -> &str {
        self.path
    }
    pub fn namespace(&self) -> &str {
        &self.scope.0
    }
}

pub fn valid_repository_path(path: &str) -> bool {
    path.contains('/') && path.split('/').all(valid_name)
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
}
