//! The embedded code host serves its own endpoints. Its host serves the SPA.

use crate::common;

use common::*;

#[tokio::test]
async fn the_code_host_does_not_serve_application_pages() {
    let dir = tempfile::tempdir().unwrap();
    init(dir.path(), "alice");
    let s = Server::start(dir.path());
    let http = reqwest::Client::new();
    for (path, status) in [
        ("/workspaces/acme/repos/demo/tickets/1", 404),
        ("/-/hooks/pre-receive", 405),
        ("/-/hooks/other", 404),
    ] {
        let resp = http.get(format!("{}{path}", s.url)).send().await.unwrap();
        assert_eq!(resp.status(), status);
    }
}
