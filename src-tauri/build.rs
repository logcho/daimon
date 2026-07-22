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
            "get_api_key_status",
            "set_api_key",
            "get_google_client_id_status",
            "set_google_client_id",
            "connect_gmail_account",
            "get_gmail_account",
            "disconnect_gmail_account",
            "get_vault_path_status",
            "set_vault_path",
            "list_vault_files",
            "read_vault_file",
            "list_automations",
            "create_automation",
            "set_automation_enabled",
            "delete_automation",
        ]));
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
