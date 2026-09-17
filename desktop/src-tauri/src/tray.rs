//! Menu bar residency.
//!
//! Global hotkeys only fire while the process is alive, so closing the main
//! window hides it instead of quitting. The tray icon is what makes that
//! recoverable: it is the way back to the window, and every Quit item the app
//! owns (the tray's, and the macOS app menu's with its Cmd+Q) asks the frontend
//! first instead of quitting.

use std::sync::Mutex;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, WindowEvent, Wry};

use crate::hotkeys::{MAIN_WINDOW, QUICK_WINDOW};

/// Raised when the user picks a Quit item, so the frontend can refuse while service or
/// command work is still running instead of orphaning it.
pub const QUIT_REQUESTED: &str = "app://quit-requested";

/// The menus exist before the webview has loaded its translations, so they
/// start in English and the frontend relabels them once i18n is ready.
struct TrayItems {
    show: MenuItem<Wry>,
    quit: MenuItem<Wry>,
    /// The macOS app menu's Quit; other platforms have no app menu.
    app_quit: Option<MenuItem<Wry>>,
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
        .build(app)?;

    #[cfg(target_os = "macos")]
    let app_quit = Some(build_app_menu(app)?);
    #[cfg(not(target_os = "macos"))]
    let app_quit = None;

    app.on_menu_event(|app, event| match event.id.as_ref() {
        "show" => crate::hotkeys::show_main_window(app.clone()),
        "quit" | APP_QUIT => {
            // The window may be hidden, and the guard answers with a modal
            // inside it; without this the quit would look unresponsive.
            crate::hotkeys::show_main_window(app.clone());
            let _ = app.emit_to(MAIN_WINDOW, QUIT_REQUESTED, ());
        }
        _ => {}
    });

    if let Ok(mut state) = app.state::<TrayState>().items.lock() {
        *state = Some(TrayItems {
            show,
            quit,
            app_quit,
        });
    }
    Ok(())
}

const APP_QUIT: &str = "app-quit";

/// Install the macOS app menu and return its Quit item.
///
/// Tauri's default menu ends in the predefined Quit, which sends `terminate:`
/// straight to NSApp: Cmd+Q and the menu bar would quit without the guard ever
/// hearing of it. This is that menu with a Quit the app handles itself. The
/// Edit menu has to stay, because the webview's copy and paste shortcuts are
/// its key equivalents.
#[cfg(target_os = "macos")]
fn build_app_menu(app: &AppHandle) -> tauri::Result<MenuItem<Wry>> {
    use tauri::menu::{PredefinedMenuItem, Submenu};

    let name = app.package_info().name.clone();
    let quit = MenuItem::with_id(app, APP_QUIT, format!("Quit {name}"), true, Some("Cmd+Q"))?;
    let menu = Menu::with_items(
        app,
        &[
            &Submenu::with_items(
                app,
                name,
                true,
                &[
                    &PredefinedMenuItem::about(app, None, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::services(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::hide(app, None)?,
                    &PredefinedMenuItem::hide_others(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &quit,
                ],
            )?,
            &Submenu::with_items(
                app,
                "Edit",
                true,
                &[
                    &PredefinedMenuItem::undo(app, None)?,
                    &PredefinedMenuItem::redo(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::cut(app, None)?,
                    &PredefinedMenuItem::copy(app, None)?,
                    &PredefinedMenuItem::paste(app, None)?,
                    &PredefinedMenuItem::select_all(app, None)?,
                ],
            )?,
            &Submenu::with_items(
                app,
                "View",
                true,
                &[&PredefinedMenuItem::fullscreen(app, None)?],
            )?,
            &Submenu::with_items(
                app,
                "Window",
                true,
                &[
                    &PredefinedMenuItem::minimize(app, None)?,
                    &PredefinedMenuItem::maximize(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::close_window(app, None)?,
                ],
            )?,
        ],
    )?;
    app.set_menu(menu)?;
    Ok(quit)
}

#[tauri::command]
pub fn set_tray_labels(app: AppHandle, show: String, quit: String, app_quit: String) {
    if let Ok(state) = app.state::<TrayState>().items.lock() {
        if let Some(items) = state.as_ref() {
            let _ = items.show.set_text(show);
            let _ = items.quit.set_text(quit);
            if let Some(item) = &items.app_quit {
                let _ = item.set_text(app_quit);
            }
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
