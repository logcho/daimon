use futures_util::StreamExt;
use tauri::Emitter;

use crate::workspace;

#[derive(Clone, serde::Serialize)]
struct TaskStatusPayload {
    task_id: String,
    event: serde_json::Value,
}

#[tauri::command]
pub async fn start_task<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    instruction: String,
) -> Result<String, String> {
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

async fn run_and_stream<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    task_id: &str,
    instruction: String,
) -> Result<(), String> {
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

fn emit<R: tauri::Runtime>(app: &tauri::AppHandle<R>, task_id: &str, event: serde_json::Value) {
    let _ = app.emit(
        "task-status",
        TaskStatusPayload {
            task_id: task_id.to_string(),
            event,
        },
    );
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};
    use std::time::Duration;
    use tauri::ipc::CallbackFn;
    use tauri::test::{mock_builder, get_ipc_response, INVOKE_KEY};
    use tauri::webview::InvokeRequest;
    use tauri::{Listener, WebviewWindowBuilder};

    /// Exercises the real `start_task` command end to end against the live
    /// background workspace (Docker container + agent server), through
    /// Tauri's own IPC test harness rather than a reimplementation.
    #[tokio::test(flavor = "multi_thread")]
    async fn start_task_streams_events_from_the_workspace() {
        let app = crate::build_app(mock_builder());

        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let events: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
        let events_for_listener = events.clone();
        app.listen("task-status", move |event| {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                events_for_listener.lock().unwrap().push(payload);
            }
        });

        let response = get_ipc_response(
            &webview,
            InvokeRequest {
                cmd: "start_task".into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: "tauri://localhost".parse().unwrap(),
                body: serde_json::json!({ "instruction": "apply to jobs" }).into(),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            },
        )
        .expect("start_task invocation returned an error");

        let task_id: String = response.deserialize().expect("expected a task id string");
        assert!(!task_id.is_empty());

        let mut saw_done = false;
        for _ in 0..60 {
            if events.lock().unwrap().iter().any(|e| e["event"]["type"] == "done") {
                saw_done = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }

        let captured = events.lock().unwrap().clone();
        assert!(saw_done, "expected a done event within 30s, got {captured:?}");
        assert!(
            captured.iter().any(|e| e["event"]["type"] == "step"),
            "expected at least one step event, got {captured:?}"
        );
        assert!(
            captured.iter().all(|e| e["task_id"] == task_id),
            "all emitted events should be tagged with this task's id, got {captured:?}"
        );
    }
}
