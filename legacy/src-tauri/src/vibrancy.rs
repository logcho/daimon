//! Native macOS window vibrancy (`NSVisualEffectView`) behind the transparent
//! pill/panel webview — the real, OS-composited "liquid glass" material.
//!
//! Why this exists instead of just CSS `backdrop-filter`: on a transparent
//! macOS window, `backdrop-filter` blurs the desktop it captured, but only
//! refreshes that capture when the *webview itself repaints*. Dragging the
//! window moves it without repainting its contents, so the blur stays frozen
//! against wherever the desktop was when it was captured — it doesn't track
//! the live desktop as the window moves. `NSVisualEffectView` is a real OS
//! material the window server recomposites continuously, so it stays correct
//! while the window is dragged. This was a direct, reported limitation of the
//! CSS-only approach; see PROMPT.md's UI-restyle notes.
//!
//! Applied once, with a fixed 28px corner radius, to the single "pill" window.
//! That one radius serves both window states because the window is square when
//! collapsed: at the 56px pill size a 28px radius is exactly a circle (28 =
//! 56/2), and at panel size it's the same 28px rounded-rectangle corner the
//! CSS panel uses (`rounded-[28px]`). The effect view is installed with an
//! autoresizing mask by `window-vibrancy`, so it tracks the window's size
//! through every collapse/expand without needing to be re-applied.
//!
//! The CSS layer on top (see `.liquid-glass` in `index.css`) no longer does
//! any blur of its own — it only contributes the dark tint, the border, and
//! the specular top-edge highlight *over* this native material.

#[cfg(target_os = "macos")]
pub async fn apply<R: tauri::Runtime>(app: &tauri::AppHandle<R>, radius: f64) -> Result<(), String> {
    use window_vibrancy::{apply_vibrancy, clear_vibrancy, NSVisualEffectMaterial, NSVisualEffectState};

    // `apply_vibrancy`/`clear_vibrancy` touch AppKit and window-vibrancy
    // returns a `NotMainThread` error if called off it — dispatch onto the
    // real main thread and bridge completion back, exactly like
    // `window_focus.rs` does for `NSApplication::activate()`.
    let (tx, rx) = tokio::sync::oneshot::channel::<Result<(), String>>();
    let app_for_closure = app.clone();
    app.run_on_main_thread(move || {
        let result = (|| -> Result<(), String> {
            // Guard the main thread ourselves before any native view work.
            // `apply_vibrancy` checks this internally, but `clear_vibrancy`
            // (called first, below) does NOT — off the main thread it would
            // dereference the window's ns_view immediately, which segfaults
            // on the mock runtime's fake handle (the ACL test below runs off
            // the OS main thread). In production this closure is dispatched
            // onto the real main thread, so this always passes; the guard
            // exists purely to keep that test from reaching the unsafe path.
            if objc2::MainThreadMarker::new().is_none() {
                return Err("vibrancy: not on the main thread".to_string());
            }
            let window = tauri::Manager::get_webview_window(&app_for_closure, "pill")
                .ok_or_else(|| "vibrancy: pill window not found".to_string())?;
            // `apply_vibrancy` always inserts a fresh tagged blur view, so a
            // repeat call would stack a second one — clear any existing view
            // first to make this idempotent. Ignore the result: a `false`
            // (nothing was there to clear) is the normal first-run case, and
            // an error here shouldn't block the apply that follows.
            let _ = clear_vibrancy(&window);
            apply_vibrancy(
                &window,
                // HudWindow: a dark, heads-up-display material — the right
                // base for a dark app, versus the light system materials.
                NSVisualEffectMaterial::HudWindow,
                // Always active, so the glass doesn't visibly de-saturate
                // whenever the (Accessory-policy, never-truly-"key") window
                // is considered inactive by the OS.
                Some(NSVisualEffectState::Active),
                Some(radius),
            )
            .map_err(|e| format!("vibrancy: apply_vibrancy failed: {e}"))?;
            Ok(())
        })();
        let _ = tx.send(result);
    })
    .map_err(|e| format!("vibrancy: failed to dispatch to main thread: {e}"))?;

    rx.await
        .map_err(|_| "vibrancy: main-thread closure was dropped before responding".to_string())?
}

#[cfg(not(target_os = "macos"))]
pub async fn apply<R: tauri::Runtime>(_app: &tauri::AppHandle<R>, _radius: f64) -> Result<(), String> {
    // Vibrancy is a macOS-specific material — a no-op elsewhere (the CSS
    // layer still renders its tint/border on those platforms, just without a
    // native blur behind it).
    Ok(())
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

    /// Same ACL-reachability pattern as `window_focus.rs`'s test — deliberately
    /// does *not* assert `Ok`, since the mock runtime has no real WindowServer
    /// and `#[test]`s don't run on the OS main thread, so the real
    /// `apply_vibrancy` path is expected to surface an ordinary error rather
    /// than succeed here. What this confirms: the command clears the ACL (a
    /// missing `"allow-set-window-vibrancy"` entry surfaces as a distinct "not
    /// allowed" error, checked for) and the whole path runs without panicking.
    #[test]
    fn set_window_vibrancy_clears_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let response = get_ipc_response(
            &webview,
            invoke_request("set_window_vibrancy", serde_json::json!({ "radius": 28.0 })),
        );
        if let Err(error) = &response {
            let message = error.to_string();
            assert!(
                !message.to_lowercase().contains("not allowed"),
                "set_window_vibrancy should be allowed by the capability, got: {message}"
            );
        }
    }
}
