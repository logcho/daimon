//! The notes vault: either a user-configured directory pointed at via
//! `DAIMON_VAULT_DIR` (matching the Python agent's env var — see
//! `agents/src/daimon_agent/config.py:114`), or (absent that) a Daimon-managed
//! default folder under `workspace::data_dir()`.
//!
//! This module used to browse the vault directly off the host filesystem for
//! the UI. It no longer does: the agent server owns the vault (it indexes
//! notes for recall, and it is what the agent's own tools write through), and
//! the app proxies every read and write to `/vault/*` — see `agent.rs`. What
//! is left is choosing *which* directory the vault is, which is genuinely a
//! desktop concern: it writes the choice to the app's own `.env` and restarts
//! the server, because the server pins its confinement root at spawn.

use std::path::PathBuf;

use crate::workspace;

/// Resolves the vault directory: `DAIMON_VAULT_DIR` if set and non-empty,
/// otherwise `<app data dir>/vault`. Always ensures the directory exists
/// before returning.
pub fn vault_path() -> PathBuf {
    let path = std::env::var("DAIMON_VAULT_DIR")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| workspace::data_dir().join("vault"));

    let _ = std::fs::create_dir_all(&path);
    path
}

#[tauri::command]
pub async fn set_vault_path(
    path: String,
    state: tauri::State<'_, crate::agent::AgentManager>,
) -> Result<(), String> {
    let path = path.trim();
    if path.is_empty() {
        return Err("vault path cannot be empty".into());
    }

    let candidate = PathBuf::from(path);
    if !candidate.is_dir() {
        return Err(format!(
            "\"{path}\" doesn't exist or isn't a directory — point this at an existing folder"
        ));
    }

    workspace::set_env_var("DAIMON_VAULT_DIR", path)?;
    // The running server (if any) confined itself to the old directory at
    // spawn time — restart so the change takes effect on the next message
    // instead of silently continuing against the stale root.
    state.shutdown().await
}

#[cfg(test)]
mod tests {
    use tauri::ipc::CallbackFn;
    use tauri::test::{get_ipc_response, mock_builder, mock_context, noop_assets, INVOKE_KEY};
    use tauri::webview::InvokeRequest;
    use tauri::WebviewWindowBuilder;

    fn invoke_request(cmd: &str, body: serde_json::Value) -> InvokeRequest {
        InvokeRequest {
            cmd: cmd.into(),
            callback: CallbackFn(0),
            error: CallbackFn(1),
            url: "tauri://localhost".parse().unwrap(),
            body: body.into(),
            headers: Default::default(),
            invoke_key: INVOKE_KEY.to_string(),
        }
    }

    /// Same ACL-reachability pattern as other modules — a missing
    /// `"allow-<command>"` capability entry compiles fine and only fails at
    /// runtime.
    #[test]
    fn set_vault_path_clears_the_acl() {
        let app = mock_builder()
            .invoke_handler(tauri::generate_handler![super::set_vault_path])
            .manage(crate::agent::AgentManager::default())
            .build(mock_context(noop_assets()))
            .expect("error while building test app");
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        // An empty path is rejected by set_vault_path's own validation — the
        // point is that the call is *reachable* at all.
        let result = get_ipc_response(
            &webview,
            invoke_request("set_vault_path", serde_json::json!({ "path": "" })),
        );
        assert!(result.is_err(), "empty path should be rejected");
        let message = result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "set_vault_path should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
    }
}
