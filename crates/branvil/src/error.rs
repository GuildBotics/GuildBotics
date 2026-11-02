//! The one error type of the service. Every failure that reaches a client is an
//! `AppError` with an HTTP status, a stable `code` from `docs/api.md`, and a
//! message that never carries secrets.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};

#[derive(Debug, Clone)]
pub struct AppError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
}

impl AppError {
    pub fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
        }
    }

    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "invalid", message)
    }

    pub fn invalid_cursor() -> Self {
        Self::new(
            StatusCode::BAD_REQUEST,
            "invalid_cursor",
            "The cursor is not one this collection gave.",
        )
    }

    pub fn unauthorized() -> Self {
        Self::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "No valid token or session.",
        )
    }

    pub fn forbidden(message: impl Into<String>) -> Self {
        Self::new(StatusCode::FORBIDDEN, "forbidden", message)
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, "not_found", message)
    }

    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new(StatusCode::CONFLICT, "conflict", message)
    }

    pub fn conflict_code(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(StatusCode::CONFLICT, code, message)
    }

    pub fn unsupported(message: impl Into<String>) -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, "unsupported", message)
    }

    pub fn too_large(message: impl Into<String>) -> Self {
        Self::new(StatusCode::PAYLOAD_TOO_LARGE, "too_large", message)
    }

    pub fn unavailable(message: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, "unavailable", message)
    }

    /// An unexpected failure. The detail goes to the log, the client gets a
    /// generic message.
    pub fn internal(detail: impl std::fmt::Display) -> Self {
        tracing::error!("internal error: {detail}");
        Self::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal",
            "Branvil hit an internal error; see the service log.",
        )
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{} ({}): {}",
            self.status.as_u16(),
            self.code,
            self.message
        )
    }
}

impl std::error::Error for AppError {}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let body = serde_json::json!({"error": {"code": self.code, "message": self.message}});
        (self.status, axum::Json(body)).into_response()
    }
}

impl From<redb::Error> for AppError {
    fn from(e: redb::Error) -> Self {
        Self::internal(format!("storage: {e}"))
    }
}

macro_rules! storage_error {
    ($($t:ty),*) => {
        $(impl From<$t> for AppError {
            fn from(e: $t) -> Self {
                Self::internal(format!("storage: {e}"))
            }
        })*
    };
}

storage_error!(
    redb::TransactionError,
    redb::TableError,
    redb::StorageError,
    redb::CommitError,
    redb::DatabaseError
);

pub type AppResult<T> = Result<T, AppError>;
