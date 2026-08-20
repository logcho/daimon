//! Turn streaming: POST /task to the agent server, frame the NDJSON lines,
//! and forward each event to the webview on the `session-status` channel
//! (the legacy session.rs pattern, verbatim envelope).

use std::time::Duration;

use futures_util::StreamExt;
use tauri::{AppHandle, Emitter};

use crate::SessionStatusPayload;

/// Transport backstop only. The agent server owns the real "has this turn
/// stalled" decision (`DAIMON_INACTIVITY_TIMEOUT_S`, 180s by default) and
/// reports it as a proper `error` event with a result attached. This timer
/// exists for the case where the server goes away without closing the socket,
/// so it must sit comfortably above the server's own limit — when it fires
/// first, a working turn dies with a message that explains nothing.
const STALL_TIMEOUT: Duration = Duration::from_secs(300);

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
    // Bytes, not a String. Decoding each chunk on arrival mangles any
    // multi-byte character that straddles a chunk boundary — `from_utf8_lossy`
    // turns the split halves into U+FFFD, and the line then fails to parse as
    // JSON. Framing on `\n` first is safe because a newline is always its own
    // byte in UTF-8, so a complete line is a complete sequence.
    let mut buffer: Vec<u8> = Vec::new();
    let mut saw_terminal = false;

    loop {
        let next = tokio::time::timeout(STALL_TIMEOUT, stream.next())
            .await
            .map_err(|_| {
                format!(
                    "lost contact with the agent server — nothing arrived on the stream \
                     for {}s",
                    STALL_TIMEOUT.as_secs()
                )
            })?;
        let Some(chunk) = next else { break };
        let chunk = match chunk {
            Ok(bytes) => bytes,
            // Connection died after the terminal event was delivered — the
            // server can still hold the body open through reflection/teardown
            // when it's killed (dev-watcher restart, kill_group), and hyper's
            // final read then fails with "incomplete message". The result is
            // already out; treat this as a clean end-of-stream. A *truncated*
            // terminal line still has saw_terminal == false and errors below.
            Err(_) if saw_terminal => break,
            // Whatever hyper says here ("error decoding response body") tells
            // the user nothing they can act on. The cause is almost always the
            // agent server going away mid-turn.
            Err(e) => {
                return Err(format!(
                    "lost the connection to the agent server mid-turn — it may have \
                     been restarted or stopped ({e})"
                ))
            }
        };
        buffer.extend_from_slice(&chunk);

        while let Some(newline_pos) = buffer.iter().position(|&b| b == b'\n') {
            let line_bytes: Vec<u8> = buffer.drain(..=newline_pos).collect();
            let line = String::from_utf8_lossy(&line_bytes[..newline_pos]);
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let event: serde_json::Value = serde_json::from_str(line)
                .map_err(|e| format!("malformed agent event: {e}"))?;
            // `ask` joined done|error as a terminal event when the agent
            // gained the ability to stop and confer. The app doesn't advertise
            // that capability so it shouldn't arrive, but a stream that ends
            // on one must not be reported as having produced no result.
            if matches!(
                event.get("type").and_then(|t| t.as_str()),
                Some("done") | Some("error") | Some("ask")
            ) {
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
