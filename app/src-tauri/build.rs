fn main() {
    // `generate_context!()` embeds icon bytes at compile time, but Cargo has
    // no way to know icon files are an input to that macro unless told here
    // — without this, changing an icon file doesn't trigger a rebuild.
    println!("cargo:rerun-if-changed=icons");

    // The command list generates the `allow-<command>` capability entries.
    //
    // Adding a Tauri command means touching THREE places, and missing any one
    // of them fails differently: omit it here and the build panics with
    // "Permission allow-x not found"; omit it from `generate_handler!` in
    // lib.rs and the call 404s at runtime; omit it from
    // capabilities/default.json and the call is rejected as "not allowed" at
    // runtime. Only the first is caught at compile time.
    let attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(&[
            "agent_status",
            "start_chat",
            "send_message",
            "close_agent",
            "set_window_vibrancy",
            "activate_and_focus_window",
            "start_terminal",
            "write_to_terminal",
            "resize_terminal",
            "close_terminal",
            "voice_model_status",
            "download_voice_model",
            "start_dictation",
            "stop_dictation",
            "accessibility_trusted",
            "get_vault_path_status",
            "set_vault_path",
            "list_vault_files",
            "read_vault_file",
            "list_skills",
            "read_skill",
            "list_models",
            "get_config",
            "update_config",
        ]));
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
