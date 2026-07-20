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
    build_app(tauri::Builder::default()).run(|_app, _event| {});
}
