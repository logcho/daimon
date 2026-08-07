fn main() {
    // `generate_context!()` embeds icon bytes at compile time, but Cargo has
    // no way to know icon files are an input to that macro unless told here
    // — without this, changing an icon file doesn't trigger a rebuild.
    println!("cargo:rerun-if-changed=icons");

    let attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(&[
            "start_session",
            "send_message",
            "end_session",
            "get_agent_auth_mode",
            "set_agent_auth_mode",
            "get_api_key_status",
            "set_api_key",
            "get_google_client_id_status",
            "set_google_client_id",
            "connect_gmail_account",
            "get_gmail_account",
            "disconnect_gmail_account",
            "get_spotify_client_id_status",
            "set_spotify_client_id",
            "connect_spotify_account",
            "get_spotify_account",
            "disconnect_spotify_account",
            "get_vault_path_status",
            "set_vault_path",
            "list_vault_files",
            "read_vault_file",
            "list_automations",
            "create_automation",
            "create_reminder",
            "set_automation_enabled",
            "delete_automation",
            "get_voice_model_status",
            "download_voice_model",
            "toggle_dictation",
            "get_chromium_status",
            "download_chromium",
            "login_browser_profile",
            "finish_browser_login",
            "get_browser_login_status",
            "start_terminal",
            "write_to_terminal",
            "resize_terminal",
            "close_terminal",
            "get_claude_cli_status",
            "list_recordings",
            "read_recording_file",
            "get_recording_thumbnail",
            "get_accessibility_trust_status",
            "activate_and_focus_window",
            "set_window_vibrancy",
        ]));
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
