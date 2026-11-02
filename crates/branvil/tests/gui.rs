//! The single-page GUI is served for every page URL, app routes included.

use crate::common;

use common::*;

#[tokio::test]
async fn every_page_url_is_the_gui_but_hooks_are_not() {
    let dir = tempfile::tempdir().unwrap();
    init(dir.path(), "alice");
    let s = Server::start(dir.path());
    let http = reqwest::Client::new();
    for page in [
        "/-/settings",
        "/-/enter",
        "/acme",
        "/acme/demo",
        "/acme/demo/-/pulls/1",
        "/acme/demo/-/tickets/1",
    ] {
        let resp = http.get(format!("{}{page}", s.url)).send().await.unwrap();
        let kind = resp.headers()[reqwest::header::CONTENT_TYPE]
            .to_str()
            .unwrap()
            .to_string();
        // Served from web/dist when it is built; otherwise the "not built"
        // notice. Never the API's JSON "No such endpoint.".
        assert!(!kind.starts_with("application/json"), "{page}: {kind}");
    }
    // The hook endpoints take only the hooks' POSTs, and no other path below
    // them is a page.
    for (path, status) in [("/-/hooks/pre-receive", 405), ("/-/hooks/other", 404)] {
        let resp = http.get(format!("{}{path}", s.url)).send().await.unwrap();
        assert_eq!(resp.status(), status, "{path}");
    }
}
