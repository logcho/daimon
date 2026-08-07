fn main() {
    // `generate_context!()` embeds icon bytes at compile time, but Cargo has
    // no way to know icon files are an input to that macro unless told here
    // — without this, changing an icon file doesn't trigger a rebuild.
    println!("cargo:rerun-if-changed=icons");

    // The command list generates the `allow-<command>` capability entries.
    let attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(&[
            "agent_status",
            "start_chat",
            "send_message",
            "close_agent",
        ]));
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
