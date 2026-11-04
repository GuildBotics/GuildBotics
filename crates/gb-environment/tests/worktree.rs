//! Changes are data, and are judged as a population before writes.
#[cfg(unix)]
mod unix {
    use gb_environment::worktree::{Copy, write_back};
    use sha2::{Digest, Sha256};
    fn hash(data: &[u8]) -> String {
        Sha256::digest(data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }
    fn copied(path: &str, bytes: &[u8]) -> Vec<u8> {
        serde_json::to_vec(
            &serde_json::json!({"path":path,"sha256":hash(bytes),"executable":false}),
        )
        .unwrap()
    }
    #[test]
    fn writes_and_deletes_only_when_originals_still_match() {
        let d = tempfile::tempdir().unwrap();
        let root = d.path().join("root");
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("a"), b"old").unwrap();
        let copy = Copy::new(root.clone(), "/work".into(), vec![], d.path().join("lock")).unwrap();
        let records = copied("a", b"old");
        let changed = br#"{"path":"a","content":"bmV3"}"#;
        write_back(&copy, &records, changed).unwrap();
        assert_eq!(std::fs::read(root.join("a")).unwrap(), b"new");
        assert!(write_back(&copy, &records, changed).is_err());
        write_back(
            &copy,
            &copied("a", b"new"),
            br#"{"path":"a","deleted":true}"#,
        )
        .unwrap();
        assert!(!root.join("a").exists());
    }
    #[test]
    fn rejects_all_bad_paths_and_conflicts_before_writing_good_changes() {
        let d = tempfile::tempdir().unwrap();
        let root = d.path().join("root");
        std::fs::create_dir(&root).unwrap();
        let copy = Copy::new(
            root.clone(),
            "/work".into(),
            vec!["mounted".into()],
            d.path().join("lock"),
        )
        .unwrap();
        for bad in [
            "../out",
            "/out",
            ".git/config",
            "x/../out",
            "mounted/a",
            "x\\a",
        ] {
            let changes = format!(
                "{{\"path\":\"good\",\"content\":\"eA==\"}}\n{}",
                serde_json::json!({"path":bad,"content":"eA=="})
            );
            assert!(write_back(&copy, b"", changes.as_bytes()).is_err());
            assert!(!root.join("good").exists());
        }
        std::os::unix::fs::symlink(d.path(), root.join("link")).unwrap();
        assert!(write_back(&copy, b"", br#"{"path":"link/out","content":"eA=="}"#).is_err());
        assert!(!d.path().join("out").exists());
    }

    #[test]
    fn preserves_modes_and_refuses_replaced_roots_and_executable_conflicts() {
        use std::os::unix::fs::PermissionsExt;
        let d = tempfile::tempdir().unwrap();
        let root = d.path().join("root");
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("script"), b"old").unwrap();
        std::fs::set_permissions(root.join("script"), std::fs::Permissions::from_mode(0o640))
            .unwrap();
        let copy = Copy::new(root.clone(), "/work".into(), vec![], d.path().join("lock")).unwrap();
        write_back(
            &copy,
            &copied("script", b"old"),
            br#"{"path":"script","content":"bmV3","executable":true}"#,
        )
        .unwrap();
        assert_eq!(
            std::fs::metadata(root.join("script"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o750
        );
        // A host chmod is a conflict when the guest requests an executable change.
        assert!(
            write_back(
                &copy,
                &copied("script", b"new"),
                br#"{"path":"script","content":"eA==","executable":false}"#
            )
            .is_err()
        );
        assert_eq!(std::fs::read(root.join("script")).unwrap(), b"new");
        std::fs::rename(&root, d.path().join("moved")).unwrap();
        std::fs::create_dir(&root).unwrap();
        assert!(write_back(&copy, b"", br#"{"path":"new","content":"eA=="}"#).is_err());
        assert!(!root.join("new").exists());
    }
}
