fn main() {
    // An app manifest gives every app command an `allow-` permission, so the
    // capabilities decide which pages may call it: the pages the backend
    // serves are a remote origin, which reaches no command without one.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "backend_entry",
            "restart_app",
            "enter_again",
            "bootstrap_log",
            "cli_agent_skill_statuses",
            "force_update_cli_agent_skill",
            "sync_hotkeys",
            "suspend_hotkeys",
            "resume_hotkeys",
            "hide_quick_window",
            "clipboard_watch_supported",
            "poll_clipboard",
            "open_main_window",
            "set_tray_labels",
            "quit_app",
        ]),
    ))
    .expect("failed to run the Tauri build script");
}
