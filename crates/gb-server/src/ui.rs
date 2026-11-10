//! The screens the server hands to a browser, as the Python version does
//! (`guildbotics/app_api/ui.py`): the Vite build of `desktop/` in the
//! checkout the server runs from. The pages hold no data: what they show,
//! they read from the API with the session the entry link gave the browser.

use std::path::{Component, Path, PathBuf};

use axum::Router;
use axum::extract::Path as Captured;
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;

use crate::i18n;

/// The main screen's paths: its own routes are in the fragment, and the
/// entry and consent pages are told apart by their path.
const MAIN_PATHS: &[&str] = &[
    "/-/guildbotics/",
    "/-/guildbotics/index.html",
    "/-/guildbotics/-/enter",
    "/-/guildbotics/-/authorize",
];

/// The build of the checkout's screens.
pub fn directory() -> PathBuf {
    gb_base::home::python_package()
        .parent()
        .expect("the checkout")
        .join("desktop/dist")
}

pub fn router<S: Clone + Send + Sync + 'static>(directory: PathBuf) -> Router<S> {
    let mut router = Router::new();
    for path in MAIN_PATHS {
        let directory = directory.clone();
        router = router.route(path, get(move || page(directory.join("index.html"))));
    }
    router.route(
        "/-/guildbotics/assets/{*path}",
        get(move |Captured(path): Captured<String>| asset(directory.join("assets"), path)),
    )
}

/// The page itself, always asked for again: a new build replaces the pages'
/// asset names, while an asset never changes under one name.
async fn page(path: PathBuf) -> Response {
    match tokio::fs::read(&path).await {
        Ok(bytes) => (
            [
                (header::CONTENT_TYPE, "text/html; charset=utf-8"),
                (header::CACHE_CONTROL, "no-cache"),
            ],
            bytes,
        )
            .into_response(),
        Err(_) => (
            StatusCode::SERVICE_UNAVAILABLE,
            i18n::t(
                "en",
                "app_api.ui.not_built",
                &[("path", &path.display().to_string())],
            ),
        )
            .into_response(),
    }
}

/// A file of the build's assets, never one a path or a link leads out to,
/// and nothing but a file: what is read is the file the path resolves to,
/// inside the assets.
async fn asset(assets: PathBuf, path: String) -> Response {
    let not_found = || StatusCode::NOT_FOUND.into_response();
    // Nothing is looked up for any other path: an absolute one reaches any
    // file, and on Windows any share.
    if !names_below(&path) {
        return not_found();
    }
    let file = assets.join(&path);
    let (Ok(assets), Ok(file)) = (
        tokio::fs::canonicalize(&assets).await,
        tokio::fs::canonicalize(&file).await,
    ) else {
        return not_found();
    };
    let regular = tokio::fs::metadata(&file)
        .await
        .is_ok_and(|metadata| metadata.is_file());
    if !regular || !file.starts_with(&assets) {
        return not_found();
    }
    let Ok(bytes) = tokio::fs::read(&file).await else {
        return not_found();
    };
    let mime = mime_guess::from_path(&file).first_or_octet_stream();
    // As Starlette names the text it serves.
    let content_type = if mime.type_() == mime_guess::mime::TEXT {
        format!("{}; charset=utf-8", mime.essence_str())
    } else {
        mime.essence_str().to_owned()
    };
    ([(header::CONTENT_TYPE, content_type)], bytes).into_response()
}

/// Whether `path` is only names, each below the one before.
fn names_below(path: &str) -> bool {
    Path::new(path)
        .components()
        .all(|part| matches!(part, Component::Normal(_)))
}

#[cfg(test)]
mod tests {
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    use super::*;

    fn built() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("assets")).unwrap();
        std::fs::write(dir.path().join("index.html"), "main").unwrap();
        std::fs::write(dir.path().join("quick.html"), "quick").unwrap();
        std::fs::write(dir.path().join("assets/app.js"), "js").unwrap();
        dir
    }

    async fn get(directory: &Path, path: &str) -> (StatusCode, header::HeaderMap, String) {
        let response = router::<()>(directory.to_path_buf())
            .oneshot(Request::get(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        let (parts, body) = response.into_parts();
        let bytes = axum::body::to_bytes(body, usize::MAX).await.unwrap();
        (
            parts.status,
            parts.headers,
            String::from_utf8(bytes.to_vec()).unwrap(),
        )
    }

    #[tokio::test]
    async fn the_main_page_answers_its_own_paths() {
        let dir = built();
        for path in MAIN_PATHS {
            let (status, headers, text) = get(dir.path(), path).await;
            assert_eq!((status, text.as_str()), (StatusCode::OK, "main"), "{path}");
            assert_eq!(headers[header::CACHE_CONTROL], "no-cache");
            assert_eq!(headers[header::CONTENT_TYPE], "text/html; charset=utf-8");
        }
        let (status, headers, text) = get(dir.path(), "/-/guildbotics/assets/app.js").await;
        assert_eq!((status, text.as_str()), (StatusCode::OK, "js"));
        assert_eq!(
            headers[header::CONTENT_TYPE],
            "text/javascript; charset=utf-8"
        );
    }

    #[tokio::test]
    async fn only_the_pages_and_their_assets_are_served() {
        let dir = built();
        let outside = tempfile::tempdir().unwrap();
        std::fs::write(outside.path().join("secret.js"), "secret").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(
            outside.path().join("secret.js"),
            dir.path().join("assets/linked.js"),
        )
        .unwrap();
        for path in [
            "/quick.html",
            "/service",
            "/-/guildbotics/assets/../index.html",
            "/-/guildbotics/assets/%2E%2E/index.html",
            "/-/guildbotics/assets/linked.js",
            "/-/guildbotics/assets/missing.js",
        ] {
            assert_eq!(
                get(dir.path(), path).await.0,
                StatusCode::NOT_FOUND,
                "{path}"
            );
        }
    }

    #[test]
    fn an_asset_is_named_by_names_below_the_assets() {
        assert!(names_below("app.js") && names_below("a/b.js"));
        for other in ["/etc/passwd", "../index.html", "a/../../x", "./a"] {
            assert!(!names_below(other), "{other}");
        }
        #[cfg(windows)]
        for other in [r"\\host\share\x", r"C:\x", r"C:x"] {
            assert!(!names_below(other), "{other}");
        }
    }

    /// A read that never ends -- of a FIFO, inside the assets or reached by
    /// an absolute path -- is never started.
    #[cfg(unix)]
    #[tokio::test]
    async fn nothing_but_a_file_inside_is_opened() {
        use std::os::unix::fs::OpenOptionsExt;

        let dir = built();
        let outside = tempfile::tempdir().unwrap();
        let fifos = [outside.path().join("pipe"), dir.path().join("assets/pipe")];
        for fifo in &fifos {
            let made = std::process::Command::new("mkfifo").arg(fifo).status();
            assert!(made.unwrap().success());
        }
        let absolute = fifos[0].display().to_string();
        for path in [
            format!("/-/guildbotics/assets/{}", absolute.replace('/', "%2F")),
            "/-/guildbotics/assets/pipe".to_owned(),
        ] {
            let answered =
                tokio::time::timeout(std::time::Duration::from_secs(5), get(dir.path(), &path))
                    .await;
            let Ok(answer) = answered else {
                // Ends the read that started, so the test fails rather than hangs.
                for fifo in &fifos {
                    let _ = std::fs::OpenOptions::new()
                        .write(true)
                        .custom_flags(libc::O_NONBLOCK)
                        .open(fifo);
                }
                panic!("{path} was opened");
            };
            assert_eq!(answer.0, StatusCode::NOT_FOUND, "{path}");
        }
    }

    #[tokio::test]
    async fn unbuilt_screens_say_so() {
        let dir = tempfile::tempdir().unwrap();
        let (status, _, text) = get(&dir.path().join("missing"), "/-/guildbotics/").await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert!(text.contains("are not built"), "{text}");
    }
}
