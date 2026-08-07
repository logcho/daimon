mod agent;
mod session;

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
    Ok(state.ensure(&app).await?)
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
) -> Result<(), String> {
    let status = state.ensure(&app).await?;
    session::spawn_turn(app, status.port, session_id, instruction);
    Ok(())
}

#[tauri::command]
async fn close_agent(state: tauri::State<'_, AgentManager>) -> Result<(), String> {
    state.shutdown().await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AgentManager::new())
        .invoke_handler(tauri::generate_handler![
            agent_status,
            start_chat,
            send_message,
            close_agent
        ])
        .setup(|app| {
            // SIGTERM (e.g. `kill` from a terminal, or the dev watcher) does
            // not fire RunEvent::Exit on its own — exit cleanly instead so
            // the Exit handler sweeps the agent server.
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
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                // The agent server (and its kernels) must not outlive us.
                app_handle.state::<AgentManager>().kill_sync();
            }
        });
}
