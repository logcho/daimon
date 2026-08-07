mod automation;
mod fn_key;
mod oauth;
mod recordings;
mod session;
mod settings;
mod skills;
mod spotify_oauth;
mod terminal;
mod vault;
mod vibrancy;
mod voice;
mod window_focus;
mod workspace;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;

fn setup_tray<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> tauri::Result<()> {
    let quit = MenuItem::with_id(app, "quit", "Quit Daimon", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&quit])?;

    let mut tray = TrayIconBuilder::new().menu(&menu).show_menu_on_left_click(true);
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }

    tray.on_menu_event(|app, event| {
        if event.id() == "quit" {
            app.exit(0);
        }
    })
    .build(app)?;

    Ok(())
}

/// Whether this process currently holds macOS Accessibility trust — a
/// prerequisite for `fn_key`'s global Fn-key monitor to receive any events at
/// all. Exposed to the frontend so Settings can surface it (with a button
/// that deep-links to System Settings -> Privacy & Security -> Accessibility
/// via the existing `@tauri-apps/plugin-opener`) rather than leaving a
/// silently-inert trigger with no explanation. Deliberately doesn't attempt
/// to trigger the system permission prompt itself (that's a separate,
/// meaningfully bigger FFI surface — `AXIsProcessTrustedWithOptions` with a
/// `CFDictionary` argument — left out of scope here).
#[tauri::command]
async fn get_accessibility_trust_status() -> Result<bool, String> {
    Ok(fn_key::accessibility_trusted())
}

/// Forces real OS-level keyboard focus onto Daimon's pill/panel window — see
/// `window_focus.rs`'s module doc for the full accessory-app focus quirk
/// this exists to work around. Called from the frontend in addition to (or
/// instead of) the plain `getCurrentWindow().setFocus()` JS API, whenever a
/// programmatic path (dictation finishing, `expandToPanel()`) needs the very
/// next keystroke to actually reach Daimon rather than whatever app the OS
/// still considers frontmost.
#[tauri::command]
async fn activate_and_focus_window<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    window_focus::activate_and_focus(&app).await
}

/// Applies (or re-applies) the native macOS vibrancy material behind the
/// pill/panel window with the given corner radius — see `vibrancy.rs`. Called
/// once from the frontend on mount rather than at Rust startup, so the window
/// is guaranteed to exist and be shown by the time it runs.
#[tauri::command]
async fn set_window_vibrancy<R: tauri::Runtime>(app: tauri::AppHandle<R>, radius: f64) -> Result<(), String> {
    vibrancy::apply(&app, radius).await
}

/// Shared app construction so tests can exercise the exact same command
/// wiring as the real app, without invoking `tauri::generate_context!()`
/// (which may only appear once per crate) a second time.
///
/// The global-shortcut plugin is deliberately *not* registered here (unlike
/// in a typical setup) — it grabs an OS-level singleton hotkey manager that
/// can only be initialized once per process, and every test that calls this
/// function would otherwise panic on the second `App` built in the same
/// `cargo test` run. It's applied to the builder separately, only in `run()`.
///
/// The log plugin has the same constraint for the same reason: it installs a
/// process-global `log` backend on setup (`log::set_boxed_logger`), which can
/// only succeed once per process. `cargo test` runs every `#[test]` in this
/// crate in one process, and several call `build_app` directly — a second
/// call would hit `SetLoggerError` and `panic!` via the `.expect(..)` below.
/// So, like global-shortcut, it's registered on the builder only in `run()`.
pub(crate) fn build_app<R: tauri::Runtime>(builder: tauri::Builder<R>) -> tauri::App<R> {
    builder
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            session::start_session,
            session::send_message,
            session::end_session,
            settings::get_agent_auth_mode,
            settings::set_agent_auth_mode,
            settings::get_api_key_status,
            settings::set_api_key,
            oauth::get_google_client_id_status,
            oauth::set_google_client_id,
            oauth::connect_gmail_account,
            oauth::get_gmail_account,
            oauth::disconnect_gmail_account,
            spotify_oauth::get_spotify_client_id_status,
            spotify_oauth::set_spotify_client_id,
            spotify_oauth::connect_spotify_account,
            spotify_oauth::get_spotify_account,
            spotify_oauth::disconnect_spotify_account,
            skills::list_skills,
            skills::create_skill,
            skills::delete_skill,
            vault::get_vault_path_status,
            vault::set_vault_path,
            vault::list_vault_files,
            vault::read_vault_file,
            automation::list_automations,
            automation::create_automation,
            automation::create_reminder,
            automation::set_automation_enabled,
            automation::delete_automation,
            voice::get_voice_model_status,
            voice::download_voice_model,
            voice::toggle_dictation,
            workspace::get_chromium_status,
            workspace::download_chromium,
            workspace::login_browser_profile,
            workspace::finish_browser_login,
            workspace::get_browser_login_status,
            terminal::start_terminal,
            terminal::write_to_terminal,
            terminal::resize_terminal,
            terminal::close_terminal,
            terminal::get_claude_cli_status,
            recordings::list_recordings,
            recordings::read_recording_file,
            get_accessibility_trust_status,
            activate_and_focus_window,
            set_window_vibrancy
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Registered here (not in `build_app`, see the comment there) so both the
    // real app and `cargo test` runs get exactly one global logger init.
    // Targets default to stdout (visible in a `tauri dev` terminal) plus the
    // plugin's app-log-dir target — a real file on disk, surviving app
    // restarts, that the user can locate and hand over for debugging a stuck
    // task after the fact.
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_log::Builder::new().level(log::LevelFilter::Info).build())
        .plugin(tauri_plugin_notification::init());

    #[allow(unused_mut)]
    let mut app = build_app(builder);

    // Resolves the real OS app-data directory (via Tauri's own
    // `app.path()`, only available once the app is built) that every data
    // directory — memory/vault/automations/pinchtab-profiles/run/.env — now
    // goes through instead of the dev-only `project_root()`. Must happen
    // before the `.env` load right below, and before anything else that
    // might touch a data directory (nothing does yet at this point in
    // startup — commands only start firing once `app.run()` begins the
    // event loop further down). Tests never call this and transparently
    // fall back to `project_root()` — see `workspace::data_dir`'s doc
    // comment.
    workspace::init_data_dir(app.handle());

    // Resolves the real OS resource directory a bundled `.app` copies
    // `tauri.conf.json`'s `bundle.resources` entries into (`Contents/
    // Resources/` on macOS) — the pinned Chromium build and the compiled+
    // pruned `agents/` production bundle both live under here once
    // `scripts/fetch-sidecars.sh` and `tauri build` have run. Same "resolve
    // once, early" timing as `init_data_dir` right above; see
    // `workspace::resource_dir`'s doc comment for the dev-checkout fallback
    // this transparently uses when unset (tests never call this either).
    workspace::init_resource_dir(app.handle());

    // Load ANTHROPIC_API_KEY (and any other secrets) from a .env file in the
    // app data directory, so configuring them doesn't depend on the exact
    // shell session that happens to launch the app — a real env var still
    // wins if one is already set.
    let _ = dotenvy::from_path(workspace::data_dir().join(".env"));

    // Only meaningful once the log plugin above has finished its setup (it
    // runs synchronously inside `build_app`'s `.build()` call), so logged
    // rather than printed straight to stdout, and after `build_app` rather
    // than before it.
    log::info!(
        "ANTHROPIC_API_KEY present: {}",
        std::env::var("ANTHROPIC_API_KEY").is_ok()
    );

    // A crash or force-quit skips the `RunEvent::Exit` sweep below entirely,
    // so any session's workspace processes it left running stay up
    // indefinitely — invisible to this new process's (freshly empty)
    // `SESSIONS` cache, and therefore never torn down by anything else.
    // Swept once here, before the UI becomes interactive but without
    // blocking it (spawned, not awaited) — see
    // `reconcile_orphaned_sessions`'s doc comment for why "any PID file
    // found" is unambiguous at this specific point.
    tauri::async_runtime::spawn(workspace::reconcile_orphaned_sessions());

    setup_tray(app.handle()).expect("failed to set up the tray icon");

    // The bare Fn key, additive alongside the CommandOrControl+Shift+D hotkey
    // registered above/from the frontend — see `fn_key.rs` for why this needs
    // native NSEvent monitors (both a global one and a local one — see that
    // module's doc comment) rather than the global-shortcut plugin. Needs a
    // real `AppHandle`, so this runs here rather than in `build_app` (same
    // timing as the automation scheduler loop spawned below); macOS-only,
    // matching where this whole feature is scoped.
    #[cfg(target_os = "macos")]
    fn_key::install_fn_key_monitors(app.handle().clone());

    // Phase 10's scheduler loop: promotes any pending agent-created
    // automation requests and fires whatever's due. 30s keeps "daily at
    // 8am" landing within a few minutes of 8am (not hours late) without
    // being a busy-loop — automations are, by design, an infrequent,
    // low-precision mechanism, so there's no benefit to polling faster than
    // this. Spawned only here (not in `build_app`, see that function's own
    // comment on why the global-shortcut/log plugins are handled the same
    // way) — every `#[test]` that calls `build_app` directly would
    // otherwise get its own competing scheduler loop mutating the same
    // process-global `automations.json`.
    {
        let app_handle = app.handle().clone();
        tauri::async_runtime::spawn(async move {
            loop {
                automation::poll_and_fire(&app_handle).await;
                tokio::time::sleep(std::time::Duration::from_secs(30)).await;
            }
        });
    }

    // Ambient widget, not a regular app: no Dock icon, no Cmd+Tab entry.
    #[cfg(target_os = "macos")]
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);

    // Sessions no longer tear down automatically after every message (Phase
    // 8) — only an explicit `end_session` or the app quitting removes a
    // session's workspace processes. So on exit, sweep every session this
    // process still believes is open, rather than leaving orphaned
    // PinchTab/Node process trees running indefinitely after quit.
    // `end_session_workspace` is async and the process is about to die, so
    // this blocks on a background runtime with a bounded wait —
    // best-effort, not a hang: if a workspace's `pinchtab server stop` is
    // slow to respond, the app still exits and just logs a warning rather
    // than waiting forever.
    app.run(|_app, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            let sweep = tauri::async_runtime::spawn(async {
                let session_ids = workspace::all_session_ids().await;
                for session_id in session_ids {
                    log::info!("app exit: tearing down still-open session {session_id}");
                    workspace::end_session_workspace(&session_id).await;
                }
            });
            if tauri::async_runtime::block_on(async {
                tokio::time::timeout(std::time::Duration::from_secs(10), sweep).await
            })
            .is_err()
            {
                log::warn!("app exit: session cleanup sweep did not finish within 10s, exiting anyway");
            }

            // Phase 11's host-shell terminal is a real OS process directly on
            // the user's machine (not a Docker container like the sweep
            // above) — a plain synchronous kill is all that's needed, and all
            // that's possible, since there's no async Docker daemon to wait
            // on here.
            log::info!("app exit: killing any still-running terminal shell process");
            terminal::kill_terminal_on_exit();
        }
    });
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

    /// Same ACL-reachability pattern used by every other command module in
    /// this crate (e.g. `voice.rs`'s `voice_commands_clear_the_acl`) — a
    /// missing `"allow-<command>"` capability entry compiles fine and only
    /// fails at runtime, so this is worth catching here rather than only
    /// finding out from a real Settings screen later.
    #[test]
    fn get_accessibility_trust_status_clears_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let response = get_ipc_response(
            &webview,
            invoke_request("get_accessibility_trust_status", serde_json::json!({})),
        )
        .expect("get_accessibility_trust_status should be allowed by the capability");
        let _trusted: bool = response
            .deserialize()
            .expect("expected a plain bool — value itself is environment-dependent, not asserted here");
    }
}
