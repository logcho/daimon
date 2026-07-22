mod automation;
mod oauth;
mod session;
mod settings;
mod vault;
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
        .invoke_handler(tauri::generate_handler![
            session::start_session,
            session::send_message,
            session::end_session,
            settings::get_api_key_status,
            settings::set_api_key,
            oauth::get_google_client_id_status,
            oauth::set_google_client_id,
            oauth::connect_gmail_account,
            oauth::get_gmail_account,
            oauth::disconnect_gmail_account,
            vault::get_vault_path_status,
            vault::set_vault_path,
            vault::list_vault_files,
            vault::read_vault_file,
            automation::list_automations,
            automation::create_automation,
            automation::set_automation_enabled,
            automation::delete_automation
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Load ANTHROPIC_API_KEY (and any other secrets) from a .env file at the
    // project root, so configuring them doesn't depend on the exact shell
    // session that happens to launch the app — a real env var still wins if
    // one is already set.
    let _ = dotenvy::from_path(workspace::project_root().join(".env"));

    // Registered here (not in `build_app`, see the comment there) so both the
    // real app and `cargo test` runs get exactly one global logger init.
    // Targets default to stdout (visible in a `tauri dev` terminal) plus the
    // plugin's app-log-dir target — a real file on disk, surviving app
    // restarts, that the user can locate and hand over for debugging a stuck
    // task after the fact.
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_log::Builder::new().level(log::LevelFilter::Info).build());

    #[allow(unused_mut)]
    let mut app = build_app(builder);

    // Only meaningful once the log plugin above has finished its setup (it
    // runs synchronously inside `build_app`'s `.build()` call), so logged
    // rather than printed straight to stdout, and after `build_app` rather
    // than before it.
    log::info!(
        "ANTHROPIC_API_KEY present: {}",
        std::env::var("ANTHROPIC_API_KEY").is_ok()
    );

    setup_tray(app.handle()).expect("failed to set up the tray icon");

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
    // session's container. So on exit, sweep every session this process
    // still believes is open, rather than leaving orphaned
    // `daimon-workspace-*` containers running indefinitely after quit.
    // `teardown_workspace`/`end_session_workspace` are async and the process
    // is about to die, so this blocks on a background runtime with a bounded
    // wait — best-effort, not a hang: if Docker is slow to respond, the app
    // still exits and just logs a warning rather than waiting forever.
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
        }
    });
}
