//! The maintenance of the code host's data against a stopped service (the
//! code host's part of `guildbotics server hub-data`) and the runner's
//! credential.

use crate::common;

use common::*;

fn text(out: &std::process::Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

#[tokio::test]
async fn maintenance_refuses_a_running_service_and_the_runner_needs_its_own_token() {
    let mut w = World::new().await;
    let dir = w.dir.path().to_path_buf();
    // The runner never falls back to the owner's credential.
    let out = branvil_command()
        .args(["runner", "--url", &w.server.url, "--work-dir"])
        .arg(dir.join("runner-work"))
        .env_remove("GUILDBOTICS_RUNNER_TOKEN")
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(
        text(&out).contains("runner token is needed"),
        "{}",
        text(&out)
    );
    // Offline commands refuse a running service, then work once it stops.
    let out = w.server.cli(&w.owner, &["admin", "check"]);
    assert!(!out.status.success());
    assert!(text(&out).contains("already open"), "{}", text(&out));
    w.server.stop();
    let out = w.server.cli(&w.owner, &["admin", "reindex"]);
    assert!(out.status.success(), "{}", text(&out));
    let out = w.server.cli(&w.owner, &["admin", "check"]);
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("indexes: ok"));
}

/// These guards survive schema changes; only the previous-schema fixture
/// below needs to move when the stored format changes.
#[tokio::test]
async fn an_upgraded_database_serves_only_after_a_successful_check() {
    let mut w = World::new().await;
    w.ticket("Check upgrade").await;
    w.server.stop();
    let data = w.server.data.clone();
    let db = data.join("branvil.redb");
    as_schema_43(&db);
    let admin = |what: &str| {
        branvil_command()
            .args(["admin", what, "--data-dir"])
            .arg(&data)
            .output()
            .unwrap()
    };
    async fn refuses_to_serve(data: &std::path::Path, why: &str) {
        let mut serve = async_branvil_command();
        serve
            .args(["serve", "--listen", "127.0.0.1:0", "--data-dir"])
            .arg(data)
            .env("GUILDBOTICS_TEST_VAULT", data.with_file_name("vault"))
            .kill_on_drop(true);
        let out = tokio::time::timeout(std::time::Duration::from_secs(20), serve.output())
            .await
            .expect("serve did not refuse the database")
            .unwrap();
        assert!(!out.status.success(), "{}", text(&out));
        assert!(text(&out).contains(why), "{}", text(&out));
    }
    let unchecked = "has not passed `guildbotics server hub-data check` since";
    // The previous version's data, its configuration included, is refused
    // with the upgrade named, before the configuration is read.
    let upgrade = "run `guildbotics server hub-data upgrade`";
    refuses_to_serve(&data, upgrade).await;
    let out = admin("check");
    assert!(!out.status.success(), "{}", text(&out));
    assert!(text(&out).contains(upgrade), "{}", text(&out));
    let out = admin("upgrade");
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("upgraded"), "{}", text(&out));
    let out = admin("upgrade");
    assert!(out.status.success(), "{}", text(&out));
    assert!(
        text(&out).contains("already has schema version"),
        "{}",
        text(&out)
    );
    refuses_to_serve(&data, unchecked).await;
    {
        let database = redb::Database::open(&db).unwrap();
        let txn = database.begin_write().unwrap();
        {
            let mut cards = txn
                .open_table(redb::TableDefinition::<&[u8], &[u8]>::new("ix_cards"))
                .unwrap();
            let key = redb::ReadableTable::iter(&cards)
                .unwrap()
                .next()
                .unwrap()
                .unwrap()
                .0
                .value()
                .to_vec();
            cards.remove(key.as_slice()).unwrap();
        }
        txn.commit().unwrap();
    }
    let out = admin("check");
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    assert!(text(&out).contains("index problem"), "{}", text(&out));
    refuses_to_serve(&data, unchecked).await;
    let out = admin("reindex");
    assert!(out.status.success(), "{}", text(&out));
    // A reindex does not mark the database checked.
    refuses_to_serve(&data, unchecked).await;
    // The check reads the configuration the server reads next.
    let config = data.join("branvil.toml");
    let converted = std::fs::read_to_string(&config).unwrap();
    std::fs::write(&config, format!("{converted}\nunknown = 1\n")).unwrap();
    let out = admin("check");
    assert!(!out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("unknown field"), "{}", text(&out));
    std::fs::write(&config, converted).unwrap();
    let out = admin("check");
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("indexes: ok"), "{}", text(&out));
    let checked = tables(&db);
    let out = admin("upgrade");
    assert!(out.status.success(), "{}", text(&out));
    assert!(
        text(&out).contains("already has schema version"),
        "{}",
        text(&out)
    );
    assert_eq!(tables(&db), checked);
    w.restart(&[]);
    assert_eq!(w.server.ok("/me", &w.owner).await["name"], "alice");
}
