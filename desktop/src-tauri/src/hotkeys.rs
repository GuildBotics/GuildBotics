//! Global hotkeys and the quick-run window.
//!
//! The workspace config owns which combination maps to which command; this
//! module only registers what the frontend hands it, reads the clipboard when a
//! combination fires, and reveals the quick-run window. Registration can fail
//! when another application already holds a combination, so `sync` reports the
//! rejected accelerators instead of failing the whole request.

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

pub const QUICK_WINDOW: &str = "quick";
pub const MAIN_WINDOW: &str = "main";

/// Registered shortcut to command name. `None` is the generic quick-run window,
/// which lets the user pick the command after it opens.
///
/// Keyed by the shortcut's own id rather than the accelerator text: the plugin
/// renders a pressed shortcut back as its normalized spelling (`Command+G`
/// becomes `super+KeyG`), which never matches what was stored.
#[derive(Default)]
pub struct HotkeyState {
    assignments: Mutex<HashMap<u32, Option<String>>>,
    /// Accelerators to re-register after recording, in their original spelling.
    registered: Mutex<Vec<String>>,
}

#[derive(serde::Deserialize)]
pub struct HotkeySettings {
    #[serde(default)]
    quick_run: String,
    #[serde(default)]
    commands: HashMap<String, String>,
}

#[derive(serde::Serialize, Clone)]
pub struct HotkeyTrigger {
    /// Command to run, or null when the window should offer a choice.
    command: Option<String>,
    /// Clipboard text at the moment the combination fired.
    text: String,
}

#[derive(serde::Serialize)]
pub struct SyncResult {
    /// Accelerators the OS refused, usually because another app holds them.
    rejected: Vec<String>,
}

/// Register one accelerator, returning the id the handler will report for it.
fn register(app: &AppHandle, accelerator: &str) -> Option<u32> {
    let shortcut = Shortcut::from_str(accelerator).ok()?;
    let id = shortcut.id();
    app.global_shortcut().register(shortcut).ok()?;
    Some(id)
}

/// Replace every registered combination with the given assignments.
#[tauri::command]
pub fn sync_hotkeys(app: AppHandle, settings: HotkeySettings) -> SyncResult {
    let manager = app.global_shortcut();
    let _ = manager.unregister_all();

    let mut assignments: HashMap<String, Option<String>> = HashMap::new();
    if !settings.quick_run.is_empty() {
        assignments.insert(settings.quick_run.clone(), None);
    }
    for (command, accelerator) in settings.commands {
        if !accelerator.is_empty() {
            assignments.insert(accelerator, Some(command));
        }
    }

    let mut rejected = Vec::new();
    let mut accepted = HashMap::new();
    let mut registered = Vec::new();
    for (accelerator, command) in assignments {
        match register(&app, &accelerator) {
            Some(id) => {
                accepted.insert(id, command);
                registered.push(accelerator);
            }
            None => rejected.push(accelerator),
        }
    }

    let state = app.state::<HotkeyState>();
    if let Ok(mut current) = state.assignments.lock() {
        *current = accepted;
    }
    if let Ok(mut current) = state.registered.lock() {
        *current = registered;
    }
    rejected.sort();
    SyncResult { rejected }
}

/// Release every combination while the UI records a new one.
///
/// A registered shortcut is consumed by the OS before any window sees it, so
/// the combination already in use could never be typed into the recorder.
#[tauri::command]
pub fn suspend_hotkeys(app: AppHandle) {
    let _ = app.global_shortcut().unregister_all();
}

#[tauri::command]
pub fn resume_hotkeys(app: AppHandle) {
    let accelerators = app
        .state::<HotkeyState>()
        .registered
        .lock()
        .map(|state| state.clone())
        .unwrap_or_default();
    for accelerator in accelerators {
        register(&app, &accelerator);
    }
}

/// Generation counter of the system clipboard, or `None` where unsupported.
///
/// macOS has no clipboard-change notification, so watching means polling. This
/// counter changes on every copy without reading the contents, which keeps the
/// poll cheap and avoids touching the clipboard text on every tick.
#[cfg(target_os = "macos")]
fn pasteboard_change_count() -> Option<i64> {
    use objc2_app_kit::NSPasteboard;

    Some(NSPasteboard::generalPasteboard().changeCount() as i64)
}

#[cfg(not(target_os = "macos"))]
fn pasteboard_change_count() -> Option<i64> {
    None
}

/// Turn the quick-run window into a non-activating panel.
///
/// A normal window can only take keyboard focus while its application is
/// active, and activating the application makes macOS raise every other window
/// it owns — so the main window jumped forward (on whichever display it lives
/// on) whenever the quick window appeared or closed. A panel with
/// `NonactivatingPanel` takes key input without activating us at all, which is
/// how launcher-style windows avoid this.
/// Panel subclass that can take keyboard focus.
///
/// The quick window is borderless, and a borderless window reports
/// `canBecomeKeyWindow == false` by default — the window class Tauri builds
/// overrides that, so swapping to a plain `NSPanel` throws the override away
/// and leaves the window unable to accept typing.
#[cfg(target_os = "macos")]
fn quick_panel_class() -> Option<&'static objc2::runtime::AnyClass> {
    use objc2::runtime::{AnyClass, AnyObject, Bool, ClassBuilder, Sel};
    use objc2::sel;

    const NAME: &std::ffi::CStr = c"GuildBoticsQuickPanel";
    if let Some(existing) = AnyClass::get(NAME) {
        return Some(existing);
    }

    extern "C" fn can_become_key(_this: &AnyObject, _cmd: Sel) -> Bool {
        Bool::YES
    }

    let mut builder = ClassBuilder::new(NAME, AnyClass::get(c"NSPanel")?)?;
    // Safety: the signature matches `- (BOOL)canBecomeKeyWindow`.
    unsafe {
        builder.add_method(
            sel!(canBecomeKeyWindow),
            can_become_key as extern "C" fn(_, _) -> _,
        );
    }
    Some(builder.register())
}

#[cfg(target_os = "macos")]
fn make_non_activating_panel(window: &tauri::WebviewWindow) {
    use objc2::runtime::AnyObject;
    use objc2_app_kit::{
        NSMainMenuWindowLevel, NSWindow, NSWindowCollectionBehavior, NSWindowStyleMask,
    };

    let Ok(handle) = window.ns_window() else {
        return;
    };
    let Some(panel_class) = quick_panel_class() else {
        return;
    };

    // Safety: the pointer is the live NSWindow backing this Tauri window, and
    // NSPanel is a subclass of NSWindow, so the instance layout is compatible.
    let ns_window: &NSWindow = unsafe {
        AnyObject::set_class(&*(handle as *const AnyObject), panel_class);
        &*(handle as *const NSWindow)
    };

    ns_window.setStyleMask(ns_window.styleMask() | NSWindowStyleMask::NonactivatingPanel);
    ns_window.setLevel(NSMainMenuWindowLevel as isize + 1);
    // Follow the user to whichever space and display they are working on.
    ns_window.setCollectionBehavior(
        NSWindowCollectionBehavior::CanJoinAllSpaces
            | NSWindowCollectionBehavior::FullScreenAuxiliary,
    );
}

#[cfg(not(target_os = "macos"))]
fn make_non_activating_panel(_window: &tauri::WebviewWindow) {}

/// Show the quick window without activating the application.
#[cfg(target_os = "macos")]
fn focus_quick_window(window: &tauri::WebviewWindow) {
    use objc2_app_kit::NSWindow;

    let Ok(handle) = window.ns_window() else {
        let _ = window.set_focus();
        return;
    };
    // Safety: the pointer is the live NSWindow backing this Tauri window.
    let ns_window: &NSWindow = unsafe { &*(handle as *const NSWindow) };
    ns_window.makeKeyAndOrderFront(None);
}

#[cfg(not(target_os = "macos"))]
fn focus_quick_window(window: &tauri::WebviewWindow) {
    let _ = window.set_focus();
}

#[derive(serde::Serialize)]
pub struct ClipboardPoll {
    change_count: i64,
    /// Contents, read only when the counter moved since `since`.
    text: Option<String>,
}

#[tauri::command]
pub fn clipboard_watch_supported() -> bool {
    pasteboard_change_count().is_some()
}

#[tauri::command]
pub fn poll_clipboard(app: AppHandle, since: i64) -> ClipboardPoll {
    let Some(change_count) = pasteboard_change_count() else {
        return ClipboardPoll {
            change_count: since,
            text: None,
        };
    };
    if change_count == since {
        return ClipboardPoll {
            change_count,
            text: None,
        };
    }
    ClipboardPoll {
        change_count,
        text: Some(app.clipboard().read_text().unwrap_or_default()),
    }
}

#[tauri::command]
pub fn hide_quick_window(app: AppHandle) {
    if let Some(window) = app.get_webview_window(QUICK_WINDOW) {
        let _ = window.hide();
    }
}

#[tauri::command]
pub fn show_main_window(app: AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn on_hotkey(app: &AppHandle, shortcut: &Shortcut) {
    let command = match app.state::<HotkeyState>().assignments.lock() {
        Ok(state) => match state.get(&shortcut.id()) {
            Some(command) => command.clone(),
            None => return,
        },
        Err(_) => return,
    };

    // Read before revealing the window: the clipboard belongs to whatever the
    // user was working in, and showing the window takes focus away from it.
    let text = app.clipboard().read_text().unwrap_or_default();
    let _ = app.emit("hotkey://triggered", HotkeyTrigger { command, text });

    if let Some(window) = app.get_webview_window(QUICK_WINDOW) {
        let _ = window.show();
        focus_quick_window(&window);
    }
}

/// Prepare the quick-run window. Called once, after the windows exist.
pub fn init(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(QUICK_WINDOW) {
        make_non_activating_panel(&window);
    }
}

pub fn plugin() -> tauri::plugin::TauriPlugin<tauri::Wry> {
    tauri_plugin_global_shortcut::Builder::new()
        .with_handler(|app, shortcut, event| {
            if event.state() == ShortcutState::Pressed {
                on_hotkey(app, shortcut);
            }
        })
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The bug this guards: assignments used to be keyed by the accelerator
    /// text, but the plugin reports a pressed shortcut in its own normalized
    /// spelling, so the lookup never matched and the window never opened.
    #[test]
    fn pressed_shortcut_does_not_round_trip_to_its_accelerator() {
        let shortcut = Shortcut::from_str("Command+G").expect("accelerator parses");

        assert_ne!(shortcut.into_string(), "Command+G");
    }

    #[test]
    fn shortcut_id_is_stable_for_the_same_accelerator() {
        let registered = Shortcut::from_str("Command+G").expect("accelerator parses");
        let pressed = Shortcut::from_str("Command+G").expect("accelerator parses");

        assert_eq!(registered.id(), pressed.id());
    }

    #[test]
    fn different_accelerators_get_different_ids() {
        let first = Shortcut::from_str("Command+G").expect("accelerator parses");
        let second = Shortcut::from_str("Control+Alt+G").expect("accelerator parses");

        assert_ne!(first.id(), second.id());
    }
}
