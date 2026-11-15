//! Default responder validation and effective values over HTTP.
use crate::common::*;
use serde_json::json;

#[tokio::test]
async fn responder_patch_is_owner_only_validated_canonical_and_revisioned() {
    let w = World::new().await;
    let (st, fresh) = w
        .server
        .post("/repos", &w.owner, json!({"path": "acme/fresh"}))
        .await;
    assert_eq!(st, 201);
    assert_eq!(
        fresh["responder"],
        json!({"member": "ken", "chosen": false, "automatic": "ken"})
    );
    let path = w.repo_path();
    let original = w.server.ok(&path, &w.owner).await;
    assert_eq!(
        original["responder"],
        json!({"member": "ken", "chosen": false, "automatic": "ken"})
    );
    let revision = original["revision"].as_u64().unwrap();
    w.server.member(&w.owner, "runner", "runner").await;
    for name in ["alice", "runner", "missing"] {
        let (st, v) = w
            .server
            .patch(
                &path,
                &w.owner,
                json!({"revision": revision, "responder": name}),
            )
            .await;
        assert_eq!(st, 400, "{v}");
        assert_eq!(v["error"]["code"], "invalid");
        assert_eq!(w.server.ok(&path, &w.owner).await, original);
    }
    assert_eq!(
        w.server
            .patch(
                &path,
                &w.aiko,
                json!({"revision": revision, "responder": "aiko"})
            )
            .await
            .0,
        403
    );
    let (st, chosen) = w
        .server
        .patch(
            &path,
            &w.owner,
            json!({"revision": revision, "responder": "AiKo"}),
        )
        .await;
    assert_eq!(st, 200, "{chosen}");
    assert_eq!(
        chosen["responder"],
        json!({"member": "aiko", "chosen": true, "automatic": "ken"})
    );
    assert_eq!(
        w.server.ok(&path, &w.owner).await["responder"],
        chosen["responder"]
    );
    assert_eq!(
        w.server
            .patch(
                &path,
                &w.owner,
                json!({"revision": revision, "responder": null})
            )
            .await
            .0,
        409
    );
    let (st, kept) = w
        .server
        .patch(
            &path,
            &w.owner,
            json!({"revision": chosen["revision"], "ci_network": true}),
        )
        .await;
    assert_eq!(st, 200);
    assert_eq!(kept["responder"], chosen["responder"]);
    let (st, automatic) = w
        .server
        .patch(
            &path,
            &w.owner,
            json!({"revision": kept["revision"], "responder": null}),
        )
        .await;
    assert_eq!(st, 200);
    assert_eq!(automatic["responder"], original["responder"]);
    let listed = w.server.ok("/repos", &w.owner).await;
    assert!(
        listed["items"]
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["responder"]["automatic"] == "ken")
    );
}
