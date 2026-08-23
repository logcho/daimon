mod agent;
mod fn_key;
mod paths;
mod remote;
mod vault;
mod vibrancy;
mod voice;
mod window_focus;
mod workspace;

use agent::{AgentManager, AgentStatus};
use tauri::Manager;

/// Whether the panel is expanded or collapsed to the pill.
///
/// The frontend owns this state (`App.tsx`'s `expanded`); this is a mirror it
/// pushes down on every transition, because `fn_key.rs` has to branch on it
/// from inside an `NSEvent` monitor with no way to ask the webview. Nothing on
/// the Rust side writes it — see `set_panel_expanded`, called from
/// `expandToPanel`/`collapseToPill` in `lib/window.ts`.
pub(crate) struct PanelExpanded(pub std::sync::atomic::AtomicBool);


#[tauri::command]
async fn agent_status(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
) -> Result<AgentStatus, String> {
    // The app owns its server: mounting the chat window brings the agent up.
    let mut status = state.ensure(&app).await?;
    if status.running {
        let bs = agent::fetch_busy_state(status.port).await;
        status.busy = bs.busy;
        status.active_turns = bs.active_turns;
        status.sessions = bs.sessions;
    }
    Ok(status)
}

#[tauri::command]
fn start_chat() -> String {
    uuid::Uuid::new_v4().to_string()
}


#[tauri::command]
async fn get_config(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::fetch_config(status.port).await
}

#[tauri::command]
async fn update_config(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    fields: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::update_config(status.port, fields).await
}

/// GET /models from the agent server — every provider's models plus whether
/// it's installed and keyed, so the picker can show what exists and grey out
/// what can't be selected.
#[tauri::command]
async fn list_skills(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::list_skills(status.port).await
}

#[tauri::command]
async fn read_skill(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    name: String,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::read_skill(status.port, &name).await
}

#[tauri::command]
async fn list_notes(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::list_notes(status.port).await
}

#[tauri::command]
async fn read_note(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    name: String,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::read_note(status.port, &name).await
}

#[tauri::command]
async fn delete_note(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    name: String,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::delete_note(status.port, &name).await
}

#[tauri::command]
async fn write_note(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    name: String,
    content: String,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::write_note(status.port, &name, &content).await
}

#[tauri::command]
async fn list_folders(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::list_folders(status.port).await
}

#[tauri::command]
async fn create_folder(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    path: String,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::create_folder(status.port, &path).await
}

#[tauri::command]
async fn delete_folder(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    path: String,
    recursive: bool,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::delete_folder(status.port, &path, recursive).await
}

#[tauri::command]
async fn move_note(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    from: String,
    to: String,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::move_note(status.port, &from, &to).await
}

#[tauri::command]
async fn delete_skill(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    name: String,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::delete_skill(status.port, &name).await
}

#[tauri::command]
async fn list_models(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
) -> Result<serde_json::Value, String> {
    let status = state.ensure(&app).await?;
    agent::list_models(status.port).await
}

#[tauri::command]
async fn close_agent(state: tauri::State<'_, AgentManager>) -> Result<(), String> {
    state.shutdown().await
}

#[tauri::command]
fn accessibility_trusted() -> bool {
    fn_key::accessibility_trusted()
}

#[tauri::command]
async fn set_window_vibrancy<R: tauri::Runtime>(app: tauri::AppHandle<R>, radius: f64) -> Result<(), String> {
    vibrancy::apply(&app, radius).await
}

#[tauri::command]
async fn activate_and_focus_window<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    window_focus::activate_and_focus(&app).await
}

#[tauri::command]
async fn deactivate_app<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    window_focus::deactivate(&app).await
}

/// Mirrors the frontend's pill/panel state down to Rust so the Fn-key gesture
/// machine can branch on it. Fire-and-forget from the frontend's side: a
/// dropped update only means an Fn press is read against a stale state, never
/// anything worse.
#[tauri::command]
fn set_panel_expanded(state: tauri::State<'_, PanelExpanded>, expanded: bool) {
    state.0.store(expanded, std::sync::atomic::Ordering::Relaxed);
}

/// Shared app builder — used by both `run()` and `#[test]` ACL reachability
/// tests (see vault.rs). Tests pass `mock_builder()` so no real window/event
/// loop is created. Uses the concrete `Wry` runtime so `#[tauri::command]`
/// macros resolve correctly (generic `R` breaks `AppHandle` deserialization).
pub(crate) fn build_app(builder: tauri::Builder<tauri::Wry>) -> tauri::App<tauri::Wry> {
    // Before AgentManager::new() reads the environment: a GUI launch gets
    // launchd's environment, not a shell's, so anything the user "exported"
    // in .zshrc is absent and `launchctl setenv` doesn't survive a reboot.
    // This file is the app's own durable store for those settings — it was
    // already being written (workspace::set_env_var) but never read back,
    // so every value in it was lost at the next launch.
    workspace::load_persisted_env();
    builder
        .manage(AgentManager::new())
        .manage(remote::RemoteManager::new())
        // Starts collapsed — App.tsx's mount effect calls collapseToPill(),
        // which pushes the same value straight back down.
        .manage(PanelExpanded(std::sync::atomic::AtomicBool::new(false)))
        .invoke_handler(tauri::generate_handler![
            agent_status,
            start_chat,
            remote::remote_status,
            remote::start_remote,
            remote::stop_remote,
            get_config,
            update_config,
            close_agent,
            set_window_vibrancy,
            activate_and_focus_window,
            deactivate_app,
            set_panel_expanded,
            voice::voice_model_status,
            voice::download_voice_model,
            voice::start_dictation,
            voice::stop_dictation,
            accessibility_trusted,
            vault::set_vault_path,
            list_skills,
            read_skill,
            delete_skill,
            list_notes,
            read_note,
            write_note,
            delete_note,
            list_folders,
            create_folder,
            delete_folder,
            move_note,
            list_models,
        ])
        .setup(|app| {
            voice::init(app.handle());
            #[cfg(target_os = "macos")]
            fn_key::install_fn_key_monitors(app.handle().clone());

            // SIGTERM — exit cleanly so the Exit handler sweeps the agent server.
            #[cfg(unix)]
            {
                use tokio::signal::unix::{signal, SignalKind};
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    let mut term = match signal(SignalKind::terminate()) {
                        Ok(s) => s,
                        Err(_) => return,
                    };
                    term.recv().await;
                    handle.exit(0);
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building daimon app")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut app = build_app(tauri::Builder::default());

    // Ambient widget, not a regular app: no Dock icon, no Cmd+Tab entry.
    // This is also what makes `alwaysOnTop` + `visibleOnAllWorkspaces` actually
    // work as intended on macOS — without Accessory policy the window still
    // participates in normal app-switching and can get hidden when the user
    // switches to another Space. Ported from legacy `lib.rs`.
    #[cfg(target_os = "macos")]
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            // The agent server (and its kernels) must not outlive us. Open
            // terminals used to be swept here too; they live in the server
            // now, which kills them on its own shutdown — and which is what
            // lets a shell survive the app being restarted.
            app_handle.state::<AgentManager>().kill_sync();
        }
    });
}
