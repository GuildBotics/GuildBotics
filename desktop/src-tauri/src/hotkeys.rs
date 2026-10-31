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

use tauri::{AppHandle, Emitter, Manager, ResourceId, WebviewWindow};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

pub const QUICK_WINDOW: &str = "quick";
pub const MAIN_WINDOW: &str = "main";
/// Asks the main window's frontend to go to a route (the payload).
pub const NAVIGATE: &str = "app://navigate";

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
    /// Clipboard image captured as a resource owned by the quick webview.
    image: Option<ResourceId>,
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

/// Panel subclass that can take keyboard focus.
///
/// The quick window is borderless, and a borderless window reports
/// `canBecomeKeyWindow == false` by default — the window class Tauri builds
/// overrides that, so swapping to a plain `NSPanel` throws the override away
/// and leaves the window unable to accept typing.
///
/// Swapping an object's class is sound only when both classes lay the instance
/// out identically. The classes between `NSWindow` and the window's current
/// class (the one Tauri builds, and the observer subclass KVO inserts above it)
/// add instance variables that are read by name afterwards, so the panel
/// reproduces each one at the same offset with the same span. That holds only
/// while `NSPanel` is laid out like `NSWindow`, so `None` otherwise.
#[cfg(target_os = "macos")]
fn quick_panel_class(
    window_class: &objc2::runtime::AnyClass,
) -> Option<&'static objc2::runtime::AnyClass> {
    use objc2::ffi;
    use objc2::runtime::{AnyClass, AnyObject, Bool, Imp, Ivar, Sel};
    use objc2::sel;

    const NAME: &std::ffi::CStr = c"GuildBoticsQuickPanel";
    if let Some(existing) = AnyClass::get(NAME) {
        return Some(existing);
    }

    let window = AnyClass::get(c"NSWindow")?;
    let panel = AnyClass::get(c"NSPanel")?;
    if panel.instance_size() != window.instance_size() {
        return None;
    }

    extern "C" fn can_become_key(_this: &AnyObject, _cmd: Sel) -> Bool {
        Bool::YES
    }

    let mut subclasses: Vec<&AnyClass> =
        std::iter::successors(Some(window_class), |class| class.superclass())
            .take_while(|class| !std::ptr::eq(*class, window))
            .collect();
    subclasses.reverse();
    let ivars: Vec<&Ivar> = subclasses
        .iter()
        .flat_map(|class| class.instance_variables().to_vec())
        .collect();
    let ends = ivars
        .iter()
        .skip(1)
        .map(|ivar| ivar.offset())
        .chain([window_class.instance_size() as isize]);
    let key_method = panel.instance_method(sel!(canBecomeKeyWindow))?;
    // Safety: the class pair is registered only after every ivar and method is
    // added; the implementation matches `- (BOOL)canBecomeKeyWindow`, whose
    // type encoding is taken from the method it overrides.
    unsafe {
        let class = ffi::objc_allocateClassPair(panel, NAME.as_ptr(), 0);
        if class.is_null() {
            return None;
        }
        for (ivar, end) in ivars.iter().zip(ends) {
            // Byte alignment places each ivar right after the previous one,
            // which is where the window class put it, padding included.
            ffi::class_addIvar(
                class,
                ivar.name().as_ptr(),
                (end - ivar.offset()) as usize,
                0,
                ivar.type_encoding().as_ptr(),
            );
        }
        ffi::class_addMethod(
            class,
            sel!(canBecomeKeyWindow),
            std::mem::transmute::<extern "C" fn(&AnyObject, Sel) -> Bool, Imp>(can_become_key),
            ffi::method_getTypeEncoding(key_method),
        );
        ffi::objc_registerClassPair(class);
        Some(&*class)
    }
}

/// Turn the quick-run window into a non-activating panel.
///
/// A normal window can only take keyboard focus while its application is
/// active, and activating the application makes macOS raise every other window
/// it owns — so the main window jumped forward (on whichever display it lives
/// on) whenever the quick window appeared or closed. A panel with
/// `NonactivatingPanel` takes key input without activating us at all, which is
/// how launcher-style windows avoid this. When the layouts cannot match, the
/// window stays a normal window rather than risk an unsound class swap.
#[cfg(target_os = "macos")]
fn make_non_activating_panel(window: &tauri::WebviewWindow) {
    use objc2::runtime::AnyObject;
    use objc2_app_kit::{
        NSMainMenuWindowLevel, NSWindow, NSWindowCollectionBehavior, NSWindowStyleMask,
    };

    let Ok(handle) = window.ns_window() else {
        return;
    };
    // Safety: the pointer is the live NSWindow backing this Tauri window.
    let object: &AnyObject = unsafe { &*(handle as *const AnyObject) };
    let Some(panel_class) = quick_panel_class(object.class())
        .filter(|panel| panel.instance_size() == object.class().instance_size())
    else {
        return;
    };

    // Safety: the panel class subclasses NSPanel (itself an NSWindow) and lays
    // the instance out exactly like the window's current class.
    let ns_window: &NSWindow = unsafe {
        AnyObject::set_class(object, panel_class);
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
    /// Image resource, present instead of text when the clipboard holds an image.
    image: Option<ResourceId>,
}

fn read_clipboard(window: &WebviewWindow) -> (String, Option<ResourceId>) {
    if let Ok(image) = window.clipboard().read_image() {
        let resource_id = window.resources_table().add(image.to_owned());
        return (String::new(), Some(resource_id));
    }
    (window.clipboard().read_text().unwrap_or_default(), None)
}

#[tauri::command]
pub fn clipboard_watch_supported() -> bool {
    pasteboard_change_count().is_some()
}

#[tauri::command]
pub async fn poll_clipboard(window: WebviewWindow, since: i64) -> ClipboardPoll {
    let Some(change_count) = pasteboard_change_count() else {
        return ClipboardPoll {
            change_count: since,
            text: None,
            image: None,
        };
    };
    if since < 0 || change_count == since {
        return ClipboardPoll {
            change_count,
            text: None,
            image: None,
        };
    }
    let (text, image) = tauri::async_runtime::spawn_blocking(move || read_clipboard(&window))
        .await
        .unwrap_or_default();
    ClipboardPoll {
        change_count,
        text: image.is_none().then_some(text),
        image,
    }
}

#[tauri::command]
pub fn hide_quick_window(app: AppHandle) {
    if let Some(window) = app.get_webview_window(QUICK_WINDOW) {
        let _ = window.hide();
    }
}

pub fn show_main_window(app: AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Reveal the main window at a route: what a control in the quick-run window
/// (its own webview, with no router of the main window's) uses to send the
/// user to a settings screen.
#[tauri::command]
pub fn open_main_window(app: AppHandle, route: String) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.emit(NAVIGATE, route);
    }
    show_main_window(app);
}

fn on_hotkey(app: &AppHandle, shortcut: &Shortcut) {
    let command = match app.state::<HotkeyState>().assignments.lock() {
        Ok(state) => match state.get(&shortcut.id()) {
            Some(command) => command.clone(),
            None => return,
        },
        Err(_) => return,
    };

    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let Some(window) = app.get_webview_window(QUICK_WINDOW) else {
            return;
        };
        // Read before revealing the window: the clipboard belongs to whatever
        // the user was working in, and showing the window takes focus away from it.
        let (text, image) = read_clipboard(&window);
        let _ = app.run_on_main_thread(move || {
            let _ = window.emit(
                "hotkey://triggered",
                HotkeyTrigger {
                    command,
                    text,
                    image,
                },
            );
            let _ = window.show();
            focus_quick_window(&window);
        });
    });
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
