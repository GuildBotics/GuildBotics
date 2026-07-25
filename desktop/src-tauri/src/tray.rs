//! Menu bar residency.
//!
//! Global hotkeys only fire while the process is alive, so closing the main
//! window hides it instead of quitting. The tray icon is what makes that
//! recoverable: it is the only way back to the window, and the only way out.

use std::sync::Mutex;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, WindowEvent, Wry};

use crate::hotkeys::{MAIN_WINDOW, QUICK_WINDOW};

/// Raised when the user picks Quit, so the frontend can refuse while service or
/// command work is still running instead of orphaning it.
pub const QUIT_REQUESTED: &str = "app://quit-requested";

/// The tray exists before the webview has loaded its translations, so it starts
/// in English and the frontend relabels it once i18n is ready.
struct TrayItems {
    show: MenuItem<Wry>,
    quit: MenuItem<Wry>,
}

#[derive(Default)]
pub struct TrayState {
    items: Mutex<Option<TrayItems>>,
}

pub fn build(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Open GuildBotics", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    TrayIconBuilder::new()
        .icon(
            app.default_window_icon()
                .cloned()
                .ok_or_else(|| tauri::Error::AssetNotFound("default window icon".to_string()))?,
        )
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => crate::hotkeys::show_main_window(app.clone()),
            "quit" => {
                // The window may be hidden, and the guard answers with a modal
                // inside it; without this the quit would look unresponsive.
                crate::hotkeys::show_main_window(app.clone());
                let _ = app.emit_to(MAIN_WINDOW, QUIT_REQUESTED, ());
            }
            _ => {}
        })
        .build(app)?;

    if let Ok(mut state) = app.state::<TrayState>().items.lock() {
        *state = Some(TrayItems { show, quit });
    }
    Ok(())
}

#[tauri::command]
pub fn set_tray_labels(app: AppHandle, show: String, quit: String) {
    if let Ok(state) = app.state::<TrayState>().items.lock() {
        if let Some(items) = state.as_ref() {
            let _ = items.show.set_text(show);
            let _ = items.quit.set_text(quit);
        }
    }
}

/// Quit for real. The frontend calls this once it has confirmed no service or
/// command work would be orphaned.
#[tauri::command]
pub fn quit_app(app: AppHandle) {
    app.exit(0);
}

/// Hide instead of destroying, so the app keeps listening for hotkeys.
pub fn on_window_event(window: &tauri::Window, event: &WindowEvent) {
    if let WindowEvent::CloseRequested { api, .. } = event {
        if matches!(window.label(), MAIN_WINDOW | QUICK_WINDOW) {
            api.prevent_close();
            let _ = window.hide();
        }
    }
}
