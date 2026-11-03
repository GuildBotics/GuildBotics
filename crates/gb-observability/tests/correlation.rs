use gb_observability::{Diagnostics, SpanContext, Trace};
use serde_json::{Map, json};

#[test]
fn joined_operations_share_trace_and_nested_spans_without_recording_boundaries() {
    let root = tempfile::tempdir().unwrap();
    let store = Diagnostics::open(root.path());
    let owner = Trace::open(&store, "manual");
    let joined = Trace::join(&store, owner.id())
        .person("aiko")
        .attribute("target.title", "work");
    let first = joined.span("provider");
    first.event("span.started", Map::new());
    first.span("nested").event("span.finished", Map::new());
    first.event("span.finished", Map::new());
    joined.event("target.recorded", Map::new());
    let detail = store.trace_detail(owner.id()).unwrap();
    assert_eq!(detail["summary"]["status"], "running");
    assert_eq!(detail["summary"]["title"], "work");
    let records = detail["records"].as_array().unwrap();
    assert_eq!(records.len(), 4);
    for record in records {
        assert_eq!(record["trace_id"], owner.id());
        assert_eq!(record["person_id"], "aiko");
        assert_eq!(record["source"], "");
        assert!(!record["type"].as_str().unwrap().starts_with("command."));
    }
    assert_eq!(records[0]["span_id"], records[2]["span_id"]);
    assert_eq!(records[0]["call_id"], records[2]["call_id"]);
    assert_ne!(records[0]["call_id"], records[1]["call_id"]);
    assert_eq!(records[1]["parent_id"], records[0]["span_id"]);
    assert!(records[0]["parent_id"].is_null());
    assert!(records[3]["span_id"].is_null());
    assert_eq!(records[1]["span"], "nested");
    assert_eq!(records[0]["attributes"], json!({"target.title":"work"}));

    Trace::join(&store, owner.id())
        .command("inspect")
        .bind_span(SpanContext {
            id: "remote-span".into(),
            call_id: "remote-call".into(),
            parent_id: Some("remote-parent".into()),
            name: "remote".into(),
        })
        .event("span.finished", Map::new());
    let detail = store.trace_detail(owner.id()).unwrap();
    let remote = &detail["records"][4];
    assert_eq!(remote["command"], "inspect");
    assert_eq!(remote["span_id"], "remote-span");
    assert_eq!(remote["call_id"], "remote-call");
    assert_eq!(remote["parent_id"], "remote-parent");
}

#[tokio::test]
async fn concurrent_traces_cannot_inherit_each_others_correlation() {
    let root = tempfile::tempdir().unwrap();
    let store = Diagnostics::open(root.path());
    let mut jobs = Vec::new();
    for person in ["a", "b"] {
        let trace = Trace::open(&store, "test").person(person);
        let id = trace.id().to_owned();
        jobs.push(tokio::spawn(async move {
            tokio::task::yield_now().await;
            trace.span("operation").event("span.finished", Map::new());
            (id, person)
        }));
    }
    for job in jobs {
        let (id, person) = job.await.unwrap();
        let detail = store.trace_detail(&id).unwrap();
        assert_eq!(detail["records"][0]["person_id"], person);
        assert_eq!(detail["records"][0]["trace_id"], id);
    }
}
