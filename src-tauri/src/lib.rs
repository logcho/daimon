mod settings;
mod task;
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
pub(crate) fn build_app<R: tauri::Runtime>(builder: tauri::Builder<R>) -> tauri::App<R> {
    builder
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            task::start_task,
            settings::get_api_key_status,
            settings::set_api_key
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
    println!(
        "[daimon] ANTHROPIC_API_KEY present: {}",
        std::env::var("ANTHROPIC_API_KEY").is_ok()
    );

    let builder = tauri::Builder::default().plugin(tauri_plugin_global_shortcut::Builder::new().build());

    #[allow(unused_mut)]
    let mut app = build_app(builder);

    setup_tray(app.handle()).expect("failed to set up the tray icon");

    // Ambient widget, not a regular app: no Dock icon, no Cmd+Tab entry.
    #[cfg(target_os = "macos")]
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);

    app.run(|_app, _event| {});
}
