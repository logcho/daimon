//! Turn streaming: POST /task to the agent server, frame the NDJSON lines,
//! and forward each event to the webview on the `session-status` channel
//! (the legacy session.rs pattern, verbatim envelope).

use std::time::Duration;

use futures_util::StreamExt;
use tauri::{AppHandle, Emitter};

use crate::SessionStatusPayload;

const STALL_TIMEOUT: Duration = Duration::from_secs(90);

/// Kicks off a turn. Returns once the HTTP request is away — the stream runs
/// on the async runtime and emits events as they arrive.
pub(crate) fn spawn_turn(
    app: AppHandle,
    port: u16,
    session_id: String,
    instruction: String,
    agent: String,
) {
    tauri::async_runtime::spawn(async move {
        if let Err(message) = run_and_stream(&app, port, &session_id, &instruction, &agent).await {
            let _ = app.emit(
                "session-status",
                SessionStatusPayload {
                    session_id,
                    event: serde_json::json!({ "type": "error", "message": message }),
                },
            );
        }
    });
}

async fn run_and_stream(
    app: &AppHandle,
    port: u16,
    session_id: &str,
    instruction: &str,
    agent: &str,
) -> Result<(), String> {
    let client = reqwest::Client::new();
    let response = client
        .post(format!("http://127.0.0.1:{port}/task"))
        .json(&serde_json::json!({
            "instruction": instruction,
            "session_id": session_id,
            "agent": agent,
        }))
        .send()
        .await
        .map_err(|e| format!("failed to reach the agent server: {e}"))?;
    if !response.status().is_success() {
        return Err(format!("agent server rejected the task: HTTP {}", response.status()));
    }

    let mut stream = response.bytes_stream();
    let mut buffer = String::new();
    let mut saw_terminal = false;

    loop {
        let next = tokio::time::timeout(STALL_TIMEOUT, stream.next())
            .await
            .map_err(|_| "agent stalled (no output for 90s)".to_string())?;
        let Some(chunk) = next else { break };
        let chunk = match chunk {
            Ok(bytes) => bytes,
            // Connection died after the terminal (done|error) event was
            // delivered — the server can still hold the body open through
            // reflection/teardown when it's killed (dev-watcher restart,
            // kill_group), and hyper's final read then fails with "incomplete
            // message". The result is already out; treat this as a clean
            // end-of-stream, not a fatal error. A *truncated* terminal line
            // still has saw_terminal == false and correctly errors.
            Err(_) if saw_terminal => break,
            Err(e) => return Err(format!("agent stream error: {e}")),
        };
        buffer.push_str(&String::from_utf8_lossy(&chunk));

        while let Some(newline_pos) = buffer.find('\n') {
            let line = buffer[..newline_pos].trim().to_string();
            buffer.drain(..=newline_pos);
            if line.is_empty() {
                continue;
            }
            let event: serde_json::Value = serde_json::from_str(&line)
                .map_err(|e| format!("malformed agent event: {e}"))?;
            if matches!(event.get("type").and_then(|t| t.as_str()), Some("done") | Some("error")) {
                saw_terminal = true;
            }
            let _ = app.emit(
                "session-status",
                SessionStatusPayload {
                    session_id: session_id.to_string(),
                    event,
                },
            );
        }
    }

    if !saw_terminal {
        return Err("agent stream ended without a result".to_string());
    }
    Ok(())
}
