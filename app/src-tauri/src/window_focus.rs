//! Forces real OS-level keyboard focus onto Daimon's pill/panel window.
//!
//! Daimon runs with `tauri::ActivationPolicy::Accessory` on macOS (see
//! `lib.rs::run()`) so it never gets a Dock icon or Cmd+Tab entry — it's an
//! ambient widget, not a regular app the user switches to on purpose. That
//! same policy is exactly why plain `WebviewWindow::set_focus()` (which is
//! what the frontend's `getCurrentWindow().setFocus()` calls into) isn't
//! reliable here: `NSApplicationActivationPolicyAccessory` apps are treated
//! by macOS as background utilities, and `set_focus()`'s underlying
//! `makeKeyAndOrderFront:` can make the *window* nominally key without the
//! *application* itself ever becoming genuinely active — and keyboard event
//! routing depends on the latter, not the former.
//!
//! This module does two things, both required to get a real keystroke
//! actually delivered into Daimon's own webview:
//!
//! 1. **App-level activation.** An explicit `-[NSApplication activate]` call
//!    brings the application forward.
//! 2. **Webview-level first-responder assignment.** Tauri's
//!    `WebviewWindow::set_focus()` only sends `WindowMessage::SetFocus`
//!    (`makeKeyAndOrderFront:`), never the separate `WebviewMessage::SetFocus`
//!    that calls `window.makeFirstResponder(&webview)`. Without the WKWebView
//!    being the key window's actual first responder, AppKit never routes real
//!    keydown events into it at all.
//!
//! Ported from `legacy/src-tauri/src/window_focus.rs`.

/// Command-facing entry point, split into a real macOS implementation and a
/// non-macOS no-op stub. The `#[tauri::command]` wrapper lives in lib.rs
/// (see `activate_and_focus_window`) — this function is generic over `R` so
/// it cannot carry the attribute itself (the test runtime's `MockRuntime`
/// doesn't implement the required traits).
#[cfg(target_os = "macos")]
pub async fn activate_and_focus<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
    imp::activate_and_focus(app).await
}

#[cfg(not(target_os = "macos"))]
pub async fn activate_and_focus<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
    if let Some(window) = tauri::Manager::get_webview_window(app, "pill") {
        focus_window_and_webview(&window)?;
    }
    Ok(())
}

/// The mirror image of `activate_and_focus`: hands app-level activation back to
/// whatever the user was in before Daimon came forward. Without this, dismissing
/// the panel leaves a 96x96 always-on-top pill holding keyboard focus, and the
/// next thing typed goes nowhere.
///
/// `deactivate()`, deliberately not `hide()` — hiding would take the pill off
/// screen, and an ambient always-visible pill is the entire point of the app.
/// Same generic-over-`R` shape as above, so the `#[tauri::command]` wrapper
/// again lives in lib.rs (see `deactivate_app`).
#[cfg(target_os = "macos")]
pub async fn deactivate<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
    imp::deactivate(app).await
}

#[cfg(not(target_os = "macos"))]
pub async fn deactivate<R: tauri::Runtime>(_app: &tauri::AppHandle<R>) -> Result<(), String> {
    // No cross-platform equivalent — on Windows/Linux the window manager
    // handles focus handoff itself when a window shrinks out of the way.
    Ok(())
}

fn focus_window_and_webview<R: tauri::Runtime>(window: &tauri::WebviewWindow<R>) -> Result<(), String> {
    window.set_focus().map_err(|e| e.to_string())?;
    let webview: &tauri::Webview<R> = window.as_ref();
    webview.set_focus().map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(target_os = "macos")]
mod imp {
    use objc2::MainThreadMarker;
    use objc2_app_kit::NSApplication;

    pub async fn activate_and_focus<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
        let (tx, rx) = tokio::sync::oneshot::channel::<Result<(), String>>();

        app.run_on_main_thread(move || {
            let result = (|| -> Result<(), String> {
                let mtm = MainThreadMarker::new().ok_or_else(|| {
                    "activate_and_focus_window: run_on_main_thread callback did not run on \
                     the main thread"
                        .to_string()
                })?;
                NSApplication::sharedApplication(mtm).activate();
                Ok(())
            })();
            let _ = tx.send(result);
        })
        .map_err(|e| format!("activate_and_focus_window: failed to dispatch to main thread: {e}"))?;

        rx.await
            .map_err(|_| {
                "activate_and_focus_window: main-thread activation closure was dropped before \
                 it could respond"
                    .to_string()
            })??;

        if let Some(window) = tauri::Manager::get_webview_window(app, "pill") {
            super::focus_window_and_webview(&window)?;
        }
        Ok(())
    }

    pub async fn deactivate<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
        let (tx, rx) = tokio::sync::oneshot::channel::<Result<(), String>>();

        app.run_on_main_thread(move || {
            let result = (|| -> Result<(), String> {
                let mtm = MainThreadMarker::new().ok_or_else(|| {
                    "deactivate_app: run_on_main_thread callback did not run on the main thread"
                        .to_string()
                })?;
                NSApplication::sharedApplication(mtm).deactivate();
                Ok(())
            })();
            let _ = tx.send(result);
        })
        .map_err(|e| format!("deactivate_app: failed to dispatch to main thread: {e}"))?;

        rx.await.map_err(|_| {
            "deactivate_app: main-thread deactivation closure was dropped before it could respond"
                .to_string()
        })?
    }
}

// ACL-reachability tests omitted — these commands take a generic `AppHandle<R>`
// so they can't carry `#[tauri::command]` directly (MockRuntime isn't `Wry`),
// and `crate::build_app` is also concrete (`Builder<Wry>`). Command wiring is
// verified by their presence in lib.rs's handler, build.rs's command list, and
// capabilities/default.json. Actual focus behavior can only be verified in a
// live macOS session with a real WindowServer.
