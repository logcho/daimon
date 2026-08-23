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

/// Resolve a UI-supplied vault-relative name to a real path, or refuse.
///
/// Same discipline as `agents/src/daimon_agent/vault.py:vault_file` — resolve
/// first, then check containment, so `..` and symlinks cannot walk out. Local
/// does not mean trusted: this name reaches the app from a webview, and the
/// only thing standing between it and `open -R /etc/…` is this function.
fn resolve_in_vault(name: &str) -> Result<PathBuf, String> {
    let root = vault_path()
        .canonicalize()
        .map_err(|e| format!("vault is unreadable: {e}"))?;
    let target = root
        .join(name)
        .canonicalize()
        .map_err(|_| "no such file".to_string())?;
    if target != root && !target.starts_with(&root) {
        return Err("path is outside the vault".into());
    }
    Ok(target)
}

/// Show a vault file in Finder, selected in its folder.
///
/// The escape hatch for everything the panel cannot do itself — a file type it
/// has no preview for, one too large to import, or just wanting the real thing
/// on disk. `open -R` rather than a plugin: the app carries none, and this is
/// one line of the one platform it ships on.
#[tauri::command]
pub async fn reveal_in_finder(name: String) -> Result<(), String> {
    let target = resolve_in_vault(&name)?;
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg("-R")
            .arg(&target)
            .spawn()
            .map_err(|e| format!("could not open Finder: {e}"))?;
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = target;
        Err("revealing a file is macOS-only".into())
    }
}

#[tauri::command]
pub async fn set_vault_path<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
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
    // The asset scope was granted for the *old* directory at launch, and
    // nothing restarts the app — without this, media in the new vault would
    // fail to load until the next launch, silently and with the panel
    // otherwise working. Scope grants are additive, so the stale entry is
    // harmless; canonicalized for the same /var -> /private/var reason as the
    // grant in lib.rs.
    if let Ok(resolved) = candidate.canonicalize() {
        use tauri::Manager as _;
        if let Err(err) = app.asset_protocol_scope().allow_directory(&resolved, true) {
            eprintln!("[daimon] could not scope the new vault for media: {err}");
        }
    }
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

    /// Same reachability check for the file-browser commands. Each of these
    /// needs its name in three places (build.rs, generate_handler!,
    /// capabilities/default.json) and only the first is caught at compile
    /// time — so this covers the other two.
    #[test]
    fn the_vault_file_commands_clear_the_acl() {
        let app = mock_builder()
            .invoke_handler(tauri::generate_handler![super::reveal_in_finder])
            .build(mock_context(noop_assets()))
            .expect("error while building test app");
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        // No such file, so this errors — the point is which error.
        let result = get_ipc_response(
            &webview,
            invoke_request("reveal_in_finder", serde_json::json!({ "name": "nope.png" })),
        );
        let message = result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "reveal_in_finder should be ACL-allowed, got: {message}"
        );
    }

    /// A name from the webview is untrusted even locally: `open -R` on a path
    /// that resolved out of the vault would show the user any file on disk.
    #[test]
    fn reveal_refuses_a_path_outside_the_vault() {
        let err = tauri::async_runtime::block_on(super::reveal_in_finder(
            "../../../../etc/passwd".into(),
        ))
        .expect_err("an escaping path must be refused");
        // Either it never resolved, or it resolved outside and was caught.
        assert!(
            err.contains("outside the vault") || err.contains("no such file"),
            "unexpected error: {err}"
        );
    }
}
