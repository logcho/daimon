mod task;
mod workspace;

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

    build_app(tauri::Builder::default()).run(|_app, _event| {});
}
