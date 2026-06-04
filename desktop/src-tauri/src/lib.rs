use std::net::TcpListener;
use std::sync::Mutex;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Holds the Local API sidecar process and the connection details the frontend
/// needs to talk to it.
///
/// The sidecar is spawned once per app process and torn down when the app
/// exits, so a closed/reopened window reuses the same backend instead of
/// starting a second one. A freshly picked port avoids colliding with a sidecar
/// that may have been orphaned by a previous force-quit.
struct BackendState {
    token: String,
    port: u16,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn backend_info(state: tauri::State<'_, BackendState>) -> serde_json::Value {
    serde_json::json!({
        "port": state.port,
        "token": state.token,
    })
}

/// Reserve a free loopback TCP port by binding to port 0 and reading back the
/// assigned port. Falls back to the historical default if the probe fails.
fn pick_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .ok()
        .and_then(|listener| listener.local_addr().ok())
        .map(|addr| addr.port())
        .unwrap_or(8765)
}

pub fn run() {
    let token = uuid::Uuid::new_v4().to_string();
    let port = pick_free_port();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![backend_info])
        .setup(move |app| {
            let port_arg = port.to_string();
            let (mut rx, child) = app
                .shell()
                .sidecar("guildbotics-app-api")?
                // The sidecar is a PyInstaller one-file binary whose worker can
                // outlive a killed bootloader. Hand it our PID so it can exit on
                // its own if this app ever dies without a clean teardown.
                .env("GUILDBOTICS_APP_API_PARENT_PID", std::process::id().to_string())
                .args(["--host", "127.0.0.1", "--port", &port_arg, "--token", &token])
                .spawn()?;

            // Keep the child's stdout/stderr pipe drained so it never blocks.
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Terminated(_) = event {
                        break;
                    }
                }
            });

            app.manage(BackendState {
                token,
                port,
                child: Mutex::new(Some(child)),
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building GuildBotics desktop application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendState>() {
                    if let Some(child) = state.child.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
