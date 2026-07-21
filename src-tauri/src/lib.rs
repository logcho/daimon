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
pub(crate) fn build_app<R: tauri::Runtime>(builder: tauri::Builder<R>) -> tauri::App<R> {
    builder
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .invoke_handler(tauri::generate_handler![task::start_task])
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

    #[allow(unused_mut)]
    let mut app = build_app(tauri::Builder::default());

    setup_tray(app.handle()).expect("failed to set up the tray icon");

    // Ambient widget, not a regular app: no Dock icon, no Cmd+Tab entry.
    #[cfg(target_os = "macos")]
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);

    app.run(|_app, _event| {});
}
