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
//! routing depends on the latter, not the former. This is a well-documented
//! quirk of accessory-policy apps in general, not a bug specific to Daimon.
//!
//! This module fixes two separate, independently-necessary gaps, both
//! required to get a real keystroke (e.g. Enter, right after a dictation
//! result lands in the input) actually delivered into Daimon's own webview:
//!
//! 1. **App-level activation.** An explicit `-[NSApplication activate]` call
//!    (the modern, parameterless replacement for the deprecated
//!    `activateIgnoringOtherApps:` — `objc2-app-kit` 0.3.2 still exposes
//!    both, but only the parameterless one is un-deprecated). This turned
//!    out *not* to be the actual missing piece in practice — tao's own
//!    `Window::set_focus()` already calls `activateIgnoringOtherApps:`
//!    internally (see `tao`'s `platform_impl::macos::util::async::set_focus`)
//!    — but it's kept here since it's the modern, non-deprecated form and
//!    doesn't hurt to be explicit about it.
//! 2. **Webview-level first-responder assignment** (`focus_window_and_webview`
//!    below) — the piece that was actually missing, found by reading
//!    `tauri-runtime-wry`'s dispatcher: `WebviewWindow::set_focus()` only
//!    sends `WindowMessage::SetFocus` (`makeKeyAndOrderFront:`), never the
//!    separate `WebviewMessage::SetFocus` that calls
//!    `window.makeFirstResponder(&webview)`. Without the WKWebView being the
//!    key window's actual first responder, AppKit never routes real keydown
//!    events into it at all — `el.focus()` in JS only sets
//!    `document.activeElement` inside a webview that was never listening,
//!    which is indistinguishable from "focus isn't working" no matter how
//!    correct the app-activation half is. This is why the app-activation fix
//!    alone (this module's first version) didn't resolve the reported
//!    symptom.
//!
//! Real keyboard-focus routing still can't be proven by this module's own
//! test — it depends on the live WindowServer session and genuinely having
//! some *other* app holding focus at the moment this runs, neither of which
//! a headless/CI check can reproduce. `activate_and_focus_window_clears_the_acl`
//! below only confirms the command is reachable and runs to completion — see
//! its doc comment.

/// Command-facing entry point, split into a real macOS implementation and a
/// non-macOS no-op stub below — same split as `fn_key.rs`.
#[cfg(target_os = "macos")]
pub async fn activate_and_focus<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
    imp::activate_and_focus(app).await
}

#[cfg(not(target_os = "macos"))]
pub async fn activate_and_focus<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
    // Accessory-policy activation is a macOS-specific concept (see this
    // module's doc comment) — nothing to do on other platforms, and
    // `tauri::Manager::get_webview_window`/`set_focus()` is still safe and
    // meaningful everywhere, so at least do that much.
    if let Some(window) = tauri::Manager::get_webview_window(app, "pill") {
        focus_window_and_webview(&window)?;
    }
    Ok(())
}

/// `WebviewWindow::set_focus()` only sends `WindowMessage::SetFocus` — on
/// macOS that's `makeKeyAndOrderFront:` (plus, as it happens,
/// `activateIgnoringOtherApps:`, done internally by tao regardless of this
/// module's own explicit `NSApplication::activate()` call above). It never
/// sends the *separate* `WebviewMessage::SetFocus`, which is the one that
/// actually calls `window.makeFirstResponder(&webview)` (see
/// `wry::WebView::focus()`). A key window's first responder — not anything
/// DOM-level — is what AppKit's event dispatch uses to decide where real
/// keystrokes go; without this, `el.focus()` in JS only ever set
/// `document.activeElement` inside a webview that the OS wasn't actually
/// routing keyboard events to yet, which is indistinguishable from "focus
/// isn't working" no matter how correct the window/app-activation half is.
/// `WebviewWindow` exposes the underlying `Webview` via `AsRef`, which has
/// its own `set_focus()` — a genuinely different call from `Window`'s
/// despite the identical name.
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

    /// `-[NSApplication activate]` must run on the main thread (`objc2`
    /// enforces this via `MainThreadMarker`, which can only be constructed
    /// there) — but this is invoked from an async Tauri command, which may
    /// run on any tokio worker thread. `AppHandle::run_on_main_thread`
    /// dispatches the actual AppKit call onto the real main thread; the
    /// oneshot channel bridges its synchronous completion back into this
    /// `async fn` so the command doesn't return before activation has
    /// actually happened (or definitively failed).
    ///
    /// `WebviewWindow::set_focus()` itself is *not* dispatched through this
    /// — Tauri's window dispatcher already proxies it safely from any
    /// thread (that's the whole point of the dispatcher pattern), so it's
    /// called directly after activation completes.
    pub async fn activate_and_focus<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
        let (tx, rx) = tokio::sync::oneshot::channel::<Result<(), String>>();

        app.run_on_main_thread(move || {
            let result = (|| -> Result<(), String> {
                let mtm = MainThreadMarker::new().ok_or_else(|| {
                    "activate_and_focus_window: run_on_main_thread callback did not run on \
                     the main thread"
                        .to_string()
                })?;
                // Brings the *application* forward, not just this window —
                // see this module's top-level doc comment for why that
                // distinction is exactly what plain `set_focus()` was
                // missing for an Accessory-policy app.
                NSApplication::sharedApplication(mtm).activate();
                Ok(())
            })();
            // The receiver may already be gone if the async fn below somehow
            // returned early — that's fine, there's nothing left to report
            // to at that point.
            let _ = tx.send(result);
        })
        .map_err(|e| format!("activate_and_focus_window: failed to dispatch to main thread: {e}"))?;

        rx.await
            .map_err(|_| {
                "activate_and_focus_window: main-thread activation closure was dropped before \
                 it could respond"
                    .to_string()
            })??;

        // Now bring Daimon's specific window to front/key, *and* make its
        // webview the actual first responder — see `focus_window_and_webview`
        // for why both calls are required. Safe to call from this (non-main)
        // thread — see the doc comment above.
        if let Some(window) = tauri::Manager::get_webview_window(app, "pill") {
            super::focus_window_and_webview(&window)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use tauri::ipc::CallbackFn;
    use tauri::test::{get_ipc_response, mock_builder, INVOKE_KEY};
    use tauri::webview::InvokeRequest;
    use tauri::WebviewWindowBuilder;

    fn invoke_request(cmd: &str, body: serde_json::Value) -> InvokeRequest {
        InvokeRequest {
            cmd: cmd.into(),
            callback: CallbackFn(0),
            error: CallbackFn(1),
            url: "tauri://localhost".parse().unwrap(),
            body: body.into(),
            headers: Default::default(),
            invoke_key: INVOKE_KEY.to_string(),
        }
    }

    /// Same ACL-reachability pattern used elsewhere in this crate (e.g.
    /// `fn_key.rs`'s `accessibility_trusted_runs_without_crashing`, `lib.rs`'s
    /// `get_accessibility_trust_status_clears_the_acl`) — but deliberately
    /// does *not* assert `Ok`, unlike those. `tauri::test`'s mock runtime has
    /// no real WindowServer session, and `#[test]` functions run on a thread
    /// spawned by the test harness, not this process's actual OS main thread
    /// — so the real `MainThreadMarker::new()` check inside the main-thread
    /// dispatch this command performs is *expected* to fail here (the mock
    /// `run_on_main_thread` runs the closure synchronously on whatever thread
    /// called it, since no real event loop is running), and it should
    /// surface as an ordinary command error, not a panic.
    ///
    /// What this test actually confirms: the command is reachable through
    /// the ACL (a missing `"allow-activate-and-focus-window"` entry surfaces
    /// as a distinct "not allowed" error, checked for explicitly below) and
    /// the whole main-thread-dispatch/AppKit-call/window-lookup path runs to
    /// completion — success or a well-formed internal error — rather than
    /// panicking. It says nothing about whether real keyboard focus actually
    /// moves, which needs a live interactive macOS session with a genuinely
    /// foregrounded other app to observe against.
    #[test]
    fn activate_and_focus_window_clears_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let response = get_ipc_response(
            &webview,
            invoke_request("activate_and_focus_window", serde_json::json!({})),
        );

        if let Err(error) = &response {
            let message = error.to_string();
            assert!(
                !message.to_lowercase().contains("not allowed"),
                "activate_and_focus_window should be allowed by the capability, got: {message}"
            );
        }
    }
}
