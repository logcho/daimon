use futures_util::StreamExt;
use tauri::Emitter;

use crate::workspace;

#[derive(Clone, serde::Serialize)]
struct TaskStatusPayload {
    task_id: String,
    event: serde_json::Value,
}

#[tauri::command]
pub async fn start_task(app: tauri::AppHandle, instruction: String) -> Result<String, String> {
    let task_id = uuid::Uuid::new_v4().to_string();

    let app_for_task = app.clone();
    let task_id_for_task = task_id.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(message) = run_and_stream(&app_for_task, &task_id_for_task, instruction).await {
            emit(&app_for_task, &task_id_for_task, serde_json::json!({ "type": "error", "message": message }));
        }
    });

    Ok(task_id)
}

async fn run_and_stream(app: &tauri::AppHandle, task_id: &str, instruction: String) -> Result<(), String> {
    let port = workspace::ensure_workspace().await?;

    let client = reqwest::Client::new();
    let url = format!("http://127.0.0.1:{port}/task");
    let response = client
        .post(&url)
        .json(&serde_json::json!({ "instruction": instruction }))
        .send()
        .await
        .map_err(|e| format!("failed to reach background workspace: {e}"))?;

    let mut stream = response.bytes_stream();
    let mut buffer = String::new();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("workspace stream error: {e}"))?;
        buffer.push_str(&String::from_utf8_lossy(&chunk));

        while let Some(newline_pos) = buffer.find('\n') {
            let line = buffer[..newline_pos].trim().to_string();
            buffer.drain(..=newline_pos);
            if line.is_empty() {
                continue;
            }
            let event: serde_json::Value =
                serde_json::from_str(&line).map_err(|e| format!("malformed workspace event: {e}"))?;
            emit(app, task_id, event);
        }
    }

    Ok(())
}

fn emit(app: &tauri::AppHandle, task_id: &str, event: serde_json::Value) {
    let _ = app.emit(
        "task-status",
        TaskStatusPayload {
            task_id: task_id.to_string(),
            event,
        },
    );
}
