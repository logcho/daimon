mod agent;
mod fn_key;
mod session;
mod terminal;
mod timefmt;
mod vault;
mod vibrancy;
mod voice;
mod window_focus;
mod workspace;

use agent::{AgentManager, AgentStatus};
use serde::Serialize;
use tauri::Manager;

/// Every NDJSON event the agent streams is forwarded to the webview wrapped
/// in this envelope (the legacy `session-status` channel, kept verbatim).
#[derive(Clone, Serialize)]
pub(crate) struct SessionStatusPayload {
    pub session_id: String,
    pub event: serde_json::Value,
}

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
async fn send_message(
    app: tauri::AppHandle,
    state: tauri::State<'_, AgentManager>,
    session_id: String,
    instruction: String,
    agent: Option<String>,
) -> Result<(), String> {
    let status = state.ensure(&app).await?;
    session::spawn_turn(app, status.port, session_id, instruction, agent.unwrap_or_else(|| "general".to_string()));
    Ok(())
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

/// Shared app builder — used by both `run()` and `#[test]` ACL reachability
/// tests (see vault.rs). Tests pass `mock_builder()` so no real window/event
/// loop is created. Uses the concrete `Wry` runtime so `#[tauri::command]`
/// macros resolve correctly (generic `R` breaks `AppHandle` deserialization).
pub(crate) fn build_app(builder: tauri::Builder<tauri::Wry>) -> tauri::App<tauri::Wry> {
    builder
        .manage(AgentManager::new())
        .invoke_handler(tauri::generate_handler![
            agent_status,
            start_chat,
            send_message,
            close_agent,
            set_window_vibrancy,
            activate_and_focus_window,
            terminal::start_terminal,
            terminal::write_to_terminal,
            terminal::resize_terminal,
            terminal::close_terminal,
            voice::voice_model_status,
            voice::download_voice_model,
            voice::start_dictation,
            voice::stop_dictation,
            accessibility_trusted,
            vault::get_vault_path_status,
            vault::set_vault_path,
            vault::list_vault_files,
            vault::read_vault_file,
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
            // The agent server (and its kernels) must not outlive us —
            // and neither must any open terminal shells.
            app_handle.state::<AgentManager>().kill_sync();
            terminal::kill_terminal_on_exit();
        }
    });
}
