use std::time::Duration;

use futures_util::StreamExt;
use tauri::Emitter;

use crate::workspace;

/// How long `run_and_stream` will wait for the *next* line of the workspace's
/// event stream before giving up on this turn as stalled. This is the direct
/// fix for "sometimes the agent just doesn't respond" — nothing upstream of
/// this bounded the overall stream, so a hung LLM call or a wedged Playwright
/// wait could leave `stream.next().await` (and therefore the pill) waiting
/// forever. 90s is picked to comfortably clear a legitimately slow
/// multi-step browser task (the existing integration tests expect a real
/// `done` well within 30s) while still resolving well short of "the user has
/// given up on the pill and gone to check Activity Monitor". It's also
/// deliberately longer than the agent server's own 60s inactivity timeout
/// (see `agents/src/run.ts`), so that in the common case the container
/// produces its own, more specific error event first and this is only the
/// backstop for when the container itself is wedged (or unreachable) rather
/// than merely the agent loop being slow.
///
/// Unlike Phase 3's one-shot tasks, a stall here does *not* trigger any
/// teardown — sessions only tear down on an explicit `end_session` (or app
/// exit), so a stalled turn just surfaces an error event and leaves the
/// container up for the next message (or for a dev to inspect via `docker
/// logs`) exactly like every other outcome.
const STALL_TIMEOUT: Duration = Duration::from_secs(90);

#[derive(Clone, serde::Serialize)]
struct SessionStatusPayload {
    session_id: String,
    event: serde_json::Value,
}

/// Starts a brand-new session: generates a fresh id, creates its workspace,
/// and fires off the first instruction. Returns the session id immediately —
/// events for this (and every later) turn arrive asynchronously over the
/// `session-status` event channel, tagged with this id.
#[tauri::command]
pub async fn start_session<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    instruction: String,
) -> Result<String, String> {
    let session_id = uuid::Uuid::new_v4().to_string();
    log::info!("session {session_id}: started, instruction={instruction:?}");
    spawn_turn(app, session_id.clone(), instruction);
    Ok(session_id)
}

/// Sends a follow-up instruction into an already-open session. Reuses that
/// session's still-running container/port (via
/// `workspace::ensure_session_workspace`'s cache) rather than creating a new
/// one, so the agent's browser/page state carries over from the previous
/// turn. Errors synchronously if `session_id` isn't a session this daemon
/// currently believes to be open — otherwise, like `start_session`, this
/// returns as soon as the turn is kicked off, not once it completes.
#[tauri::command]
pub async fn send_message<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    session_id: String,
    instruction: String,
) -> Result<(), String> {
    if !workspace::all_session_ids().await.contains(&session_id) {
        return Err(format!("no active session with id {session_id}"));
    }
    log::info!("session {session_id}: message received, instruction={instruction:?}");
    spawn_turn(app, session_id, instruction);
    Ok(())
}

/// Explicitly tears down a session's workspace/container. Idempotent —
/// ending an already-ended or unknown session id is not an error.
#[tauri::command]
pub async fn end_session(session_id: String) -> Result<(), String> {
    log::info!("session {session_id}: end requested");
    workspace::end_session_workspace(&session_id).await;
    Ok(())
}

fn spawn_turn<R: tauri::Runtime>(app: tauri::AppHandle<R>, session_id: String, instruction: String) {
    tauri::async_runtime::spawn(async move {
        if let Err(message) = run_and_stream(&app, &session_id, instruction).await {
            log::error!("session {session_id}: turn failed: {message}");
            emit(
                &app,
                &session_id,
                serde_json::json!({ "type": "error", "message": message }),
            );
        }
    });
}

/// The outcome of one `run_ephemeral` call — what `automation.rs`'s
/// scheduler writes into an automation's `last_run_status`/`last_run_result`
/// fields once a fired run finishes, regardless of whether it succeeded.
/// `status` is always either `"done"` or `"error"`, mirroring the two
/// terminal event types a normal session can emit (see `events.ts`'s
/// `DoneEvent`/`ErrorEvent`).
pub(crate) struct EphemeralOutcome {
    pub status: String,
    pub result: Option<String>,
}

/// A variant of `start_session` for automation firings rather than
/// interactive chat: creates a brand-new session (fresh id, its own
/// container), runs exactly one turn, and **always tears its container down
/// before returning** — unlike every other session, which stays up until an
/// explicit `end_session` (see `workspace::end_session_workspace`'s doc
/// comment on that policy). An automation runs unattended and on a
/// recurring schedule, so leaving its container open the way an interactive
/// chat's is would just accumulate idle Chromium instances forever.
///
/// Events still stream over the ordinary `session-status` channel tagged
/// with this fresh session id — so a run that fires while the app happens to
/// be open shows up live like any other session — but the return value is
/// what makes the outcome visible even when nobody's watching: the
/// scheduler persists it into the automation's own record so "last run:
/// today 8:03am — done — <summary>" doesn't depend on the app having been
/// open at trigger time.
pub(crate) async fn run_ephemeral<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    instruction: String,
) -> EphemeralOutcome {
    let session_id = uuid::Uuid::new_v4().to_string();
    log::info!("automation run {session_id}: started (ephemeral), instruction={instruction:?}");

    let outcome = match run_and_stream(app, &session_id, instruction).await {
        Ok(result) => EphemeralOutcome { status: "done".to_string(), result },
        Err(message) => {
            log::error!("automation run {session_id}: turn failed: {message}");
            emit(
                app,
                &session_id,
                serde_json::json!({ "type": "error", "message": message }),
            );
            EphemeralOutcome { status: "error".to_string(), result: Some(message) }
        }
    };

    log::info!("automation run {session_id}: tearing down (ephemeral run complete)");
    workspace::end_session_workspace(&session_id).await;

    outcome
}

/// Shared by `start_session` and `send_message` — the workspace lookup
/// (`ensure_session_workspace`) is what actually decides "create a container"
/// vs. "reuse the existing one", so this function reads identically for a
/// session's first message and its fifth. Returns the final `done` event's
/// `result` string, if the turn produced one — `run_ephemeral` is the only
/// caller that needs it; `spawn_turn` (interactive sessions) just discards it
/// since the frontend already gets it live over `session-status`.
async fn run_and_stream<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    session_id: &str,
    instruction: String,
) -> Result<Option<String>, String> {
    let port = workspace::ensure_session_workspace(session_id).await?;
    let container_name = format!("daimon-workspace-{session_id}");
    log::info!("session {session_id}: workspace ready on port {port} (container {container_name})");

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
    let mut last_done_result: Option<String> = None;

    loop {
        let next = match tokio::time::timeout(STALL_TIMEOUT, stream.next()).await {
            Ok(next) => next,
            Err(_) => {
                // `docker logs {container_name}` is the only place the
                // agent-side detail of *why* it stalled still exists (see
                // `agents/src/run.ts`'s own, shorter inactivity timeout,
                // which — if it wins the race — would have logged a more
                // specific cause inside the container right before this
                // fires). The container is left running either way — a
                // stalled turn is just one more turn's outcome, not a reason
                // to tear the session down.
                log::error!(
                    "session {session_id}: stalled — no event from workspace within {}s (container {container_name} still running: `docker logs {container_name}`)",
                    STALL_TIMEOUT.as_secs()
                );
                let message = format!(
                    "Task stalled: no response from the background workspace within {}s.",
                    STALL_TIMEOUT.as_secs()
                );
                emit(
                    app,
                    session_id,
                    serde_json::json!({ "type": "error", "message": message.clone() }),
                );
                return Err(format!("workspace stream stalled for more than {}s", STALL_TIMEOUT.as_secs()));
            }
        };

        let Some(chunk) = next else {
            break;
        };
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

            match event.get("type").and_then(|t| t.as_str()) {
                Some("done") => {
                    log::info!("session {session_id}: turn done, result={:?}", event.get("result"));
                    last_done_result = event
                        .get("result")
                        .and_then(|r| r.as_str())
                        .map(|s| s.to_string());
                }
                Some("error") => log::error!("session {session_id}: workspace reported error: {:?}", event.get("message")),
                _ => log::debug!("session {session_id}: event={event}"),
            }

            emit(app, session_id, event);
        }
    }

    Ok(last_done_result)
}

fn emit<R: tauri::Runtime>(app: &tauri::AppHandle<R>, session_id: &str, event: serde_json::Value) {
    let _ = app.emit(
        "session-status",
        SessionStatusPayload {
            session_id: session_id.to_string(),
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

    fn container_id(name: &str) -> Option<String> {
        let output = std::process::Command::new("docker")
            .args(["inspect", "-f", "{{.Id}}", name])
            .output()
            .expect("failed to run docker inspect");
        if output.status.success() {
            Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
        } else {
            None
        }
    }

    async fn wait_for_container_removed(session_id: &str) {
        let name = format!("daimon-workspace-{session_id}");
        for _ in 0..20 {
            if container_id(&name).is_none() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        panic!("container {name} was not torn down within 5s of end_session");
    }

    fn invoke_start_session(webview: &tauri::WebviewWindow<tauri::test::MockRuntime>, instruction: &str) -> String {
        get_ipc_response(
            webview,
            InvokeRequest {
                cmd: "start_session".into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: "tauri://localhost".parse().unwrap(),
                body: serde_json::json!({ "instruction": instruction }).into(),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            },
        )
        .expect("start_session invocation returned an error")
        .deserialize::<String>()
        .expect("expected a session id string")
    }

    fn invoke_send_message(webview: &tauri::WebviewWindow<tauri::test::MockRuntime>, session_id: &str, instruction: &str) {
        get_ipc_response(
            webview,
            InvokeRequest {
                cmd: "send_message".into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: "tauri://localhost".parse().unwrap(),
                body: serde_json::json!({ "sessionId": session_id, "instruction": instruction }).into(),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            },
        )
        .expect("send_message invocation returned an error");
    }

    fn invoke_end_session(webview: &tauri::WebviewWindow<tauri::test::MockRuntime>, session_id: &str) {
        get_ipc_response(
            webview,
            InvokeRequest {
                cmd: "end_session".into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: "tauri://localhost".parse().unwrap(),
                body: serde_json::json!({ "sessionId": session_id }).into(),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            },
        )
        .expect("end_session invocation returned an error");
    }

    /// Exercises the real `start_session`/`end_session` commands end to end
    /// against the live background workspace (Docker container + agent
    /// server), through Tauri's own IPC test harness rather than a
    /// reimplementation. Phase 8's whole point is that a session's container
    /// is *not* torn down the moment a turn completes — so this asserts the
    /// container is still running right after `done`, and only goes away
    /// once `end_session` is called.
    #[tokio::test(flavor = "multi_thread")]
    async fn start_session_streams_events_and_stays_up_until_ended() {
        let app = crate::build_app(mock_builder());

        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let events: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
        let events_for_listener = events.clone();
        app.listen("session-status", move |event| {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                events_for_listener.lock().unwrap().push(payload);
            }
        });

        let session_id = invoke_start_session(&webview, "apply to jobs");
        assert!(!session_id.is_empty());

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
            captured.iter().all(|e| e["session_id"] == session_id),
            "all emitted events should be tagged with this session's id, got {captured:?}"
        );

        // The behavior change under test: no auto-teardown after `done`.
        let name = format!("daimon-workspace-{session_id}");
        assert!(
            container_id(&name).is_some(),
            "container {name} should still be running after a done event (sessions don't auto-teardown)"
        );

        invoke_end_session(&webview, &session_id);
        wait_for_container_removed(&session_id).await;
    }

    /// Phase 3's guarantee still holds for sessions: two started back-to-back
    /// must each get their own workspace container and complete
    /// independently, neither blocking the other nor leaking events across
    /// session ids.
    #[tokio::test(flavor = "multi_thread")]
    async fn two_concurrent_sessions_do_not_interfere() {
        let app = crate::build_app(mock_builder());

        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let events: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
        let events_for_listener = events.clone();
        app.listen("session-status", move |event| {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                events_for_listener.lock().unwrap().push(payload);
            }
        });

        let session_a = invoke_start_session(&webview, "apply to jobs");
        let session_b = invoke_start_session(&webview, "research the company");
        assert_ne!(session_a, session_b, "concurrent sessions must get distinct ids");

        let both_done = |events: &[serde_json::Value]| {
            [&session_a, &session_b].iter().all(|id| {
                events
                    .iter()
                    .any(|e| &e["session_id"] == *id && e["event"]["type"] == "done")
            })
        };

        let mut saw_both = false;
        for _ in 0..60 {
            if both_done(&events.lock().unwrap()) {
                saw_both = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }

        let captured = events.lock().unwrap().clone();
        assert!(saw_both, "expected both sessions to finish within 30s, got {captured:?}");

        for id in [&session_a, &session_b] {
            let for_this_session: Vec<_> = captured.iter().filter(|e| &e["session_id"] == id).collect();
            assert!(
                for_this_session.iter().any(|e| e["event"]["type"] == "step"),
                "expected at least one step event for {id}, got {captured:?}"
            );
        }

        invoke_end_session(&webview, &session_a);
        invoke_end_session(&webview, &session_b);
        wait_for_container_removed(&session_a).await;
        wait_for_container_removed(&session_b).await;
    }

    /// The actual Phase 8 behavior change: a second message into an existing
    /// session must reuse the exact same container (same container id and
    /// port throughout — a recreated container would get a new id even under
    /// the same name), proving real continuation rather than a fresh
    /// workspace per message.
    #[tokio::test(flavor = "multi_thread")]
    async fn send_message_continues_the_same_workspace_container() {
        let app = crate::build_app(mock_builder());

        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let events: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
        let events_for_listener = events.clone();
        app.listen("session-status", move |event| {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                events_for_listener.lock().unwrap().push(payload);
            }
        });

        let session_id = invoke_start_session(&webview, "open example.com and summarize it");

        let done_count = |events: &[serde_json::Value]| {
            events
                .iter()
                .filter(|e| e["session_id"] == session_id && e["event"]["type"] == "done")
                .count()
        };

        let mut saw_first_done = false;
        for _ in 0..60 {
            if done_count(&events.lock().unwrap()) >= 1 {
                saw_first_done = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        assert!(saw_first_done, "expected a first done event within 30s");

        let name = format!("daimon-workspace-{session_id}");
        let id_after_first = container_id(&name).expect("container should still be running after first turn");

        invoke_send_message(&webview, &session_id, "now tell me one more fact about it");

        let mut saw_second_done = false;
        for _ in 0..60 {
            if done_count(&events.lock().unwrap()) >= 2 {
                saw_second_done = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        let captured = events.lock().unwrap().clone();
        assert!(saw_second_done, "expected a second done event within 30s, got {captured:?}");

        let id_after_second = container_id(&name).expect("container should still be running after second turn");
        assert_eq!(
            id_after_first, id_after_second,
            "the container must not be recreated between messages in the same session"
        );

        invoke_end_session(&webview, &session_id);
        wait_for_container_removed(&session_id).await;
    }

    /// `send_message` against an id this daemon has never seen (or already
    /// ended) must fail fast with a clear error rather than silently
    /// creating a brand-new workspace under that id.
    #[tokio::test(flavor = "multi_thread")]
    async fn send_message_to_unknown_session_errors() {
        let app = crate::build_app(mock_builder());

        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let response = get_ipc_response(
            &webview,
            InvokeRequest {
                cmd: "send_message".into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: "tauri://localhost".parse().unwrap(),
                body: serde_json::json!({ "sessionId": "does-not-exist", "instruction": "hi" }).into(),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            },
        );

        assert!(response.is_err(), "send_message on an unknown session id should error");
    }
}
