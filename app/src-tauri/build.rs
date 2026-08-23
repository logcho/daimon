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
            "close_agent",
            "set_window_vibrancy",
            "activate_and_focus_window",
            "deactivate_app",
            "set_panel_expanded",
            "voice_model_status",
            "download_voice_model",
            "start_dictation",
            "stop_dictation",
            "accessibility_trusted",
            "set_vault_path",
            "list_skills",
            "read_skill",
            "list_models",
            "delete_skill",
            "list_notes",
            "read_note",
            "write_note",
            "delete_note",
            "list_folders",
            "create_folder",
            "delete_folder",
            "move_note",
            "list_vault_entries",
            "write_vault_bytes",
            "reveal_in_finder",
            "get_config",
            "remote_status",
            "start_remote",
            "stop_remote",
            "update_config",
        ]));
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
