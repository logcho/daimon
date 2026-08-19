//! The notes vault: either a user-configured directory pointed at via
//! `DAIMON_VAULT_DIR` (matching the Python agent's env var — see
//! `agents/src/daimon_agent/config.py:114`), or (absent that) a Daimon-managed
//! default folder under `workspace::data_dir()`.
//!
//! Browsing (`list_vault_files`/`read_vault_file`) never touches the agent
//! server — the vault lives on the host filesystem, so the Tauri backend can
//! read it directly for the UI without a round-trip through the Python process.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crate::timefmt::format_rfc3339;
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

fn is_default_vault() -> bool {
    std::env::var("DAIMON_VAULT_DIR")
        .map(|v| v.trim().is_empty())
        .unwrap_or(true)
}

/// Reduces an arbitrary IPC string to a bare filename — no path separators,
/// no `..` components — before it's ever joined onto `vault_path()`.
fn sanitize_filename(name: &str) -> Result<String, String> {
    let candidate = Path::new(name)
        .file_name()
        .ok_or_else(|| "invalid file name".to_string())?
        .to_string_lossy()
        .to_string();

    if candidate.is_empty() || candidate == "." || candidate == ".." {
        return Err("invalid file name".to_string());
    }
    Ok(candidate)
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VaultPathStatus {
    path: String,
    is_default: bool,
}

#[tauri::command]
pub async fn get_vault_path_status() -> Result<VaultPathStatus, String> {
    Ok(VaultPathStatus {
        path: vault_path().display().to_string(),
        is_default: is_default_vault(),
    })
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

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VaultFile {
    name: String,
    size_bytes: u64,
    modified_at: String,
}

#[tauri::command]
pub async fn list_vault_files() -> Result<Vec<VaultFile>, String> {
    let dir = vault_path();
    let entries =
        std::fs::read_dir(&dir).map_err(|e| format!("failed to read vault directory: {e}"))?;

    let mut files = Vec::new();
    for entry in entries {
        let entry =
            entry.map_err(|e| format!("failed to read vault directory entry: {e}"))?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        let metadata = entry
            .metadata()
            .map_err(|e| format!("failed to read file metadata: {e}"))?;
        if !metadata.is_file() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        let modified = metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH);
        files.push((
            VaultFile {
                name,
                size_bytes: metadata.len(),
                modified_at: format_rfc3339(modified),
            },
            modified,
        ));
    }

    // Most-recently-modified first — the most useful default for a file
    // browser (surfaces whatever the agent or user just touched).
    files.sort_by(|a, b| b.1.cmp(&a.1));
    Ok(files.into_iter().map(|(f, _)| f).collect())
}

#[tauri::command]
pub async fn read_vault_file(name: String) -> Result<String, String> {
    let safe_name = sanitize_filename(&name)?;
    let path = vault_path().join(&safe_name);
    std::fs::read_to_string(&path)
        .map_err(|e| format!("failed to read \"{safe_name}\": {e}"))
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

    #[test]
    fn sanitize_filename_strips_traversal_to_a_bare_name() {
        let sanitized =
            super::sanitize_filename("../../etc/passwd").expect("should sanitize to a bare name");
        assert_eq!(sanitized, "passwd");
    }

    #[test]
    fn sanitize_filename_rejects_bare_dotdot() {
        assert!(super::sanitize_filename("..").is_err());
    }

    /// Same ACL-reachability pattern as other modules — a missing
    /// `"allow-<command>"` capability entry compiles fine and only fails at
    /// runtime.
    #[test]
    fn vault_commands_clear_the_acl() {
        let app = mock_builder()
            .invoke_handler(tauri::generate_handler![
                super::get_vault_path_status,
                super::set_vault_path,
                super::list_vault_files,
                super::read_vault_file,
            ])
            .manage(crate::agent::AgentManager::default())
            .build(mock_context(noop_assets()))
            .expect("error while building test app");
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let status =
            get_ipc_response(&webview, invoke_request("get_vault_path_status", serde_json::json!({})))
                .expect("get_vault_path_status should be allowed by the capability");
        let _: super::VaultPathStatus =
            status.deserialize().expect("expected a VaultPathStatus");

        let list =
            get_ipc_response(&webview, invoke_request("list_vault_files", serde_json::json!({})))
                .expect("list_vault_files should be allowed by the capability");
        let _: Vec<super::VaultFile> = list.deserialize().expect("expected a Vec<VaultFile>");

        // Empty path is expected to be rejected by set_vault_path's own
        // validation — the point is that it's *reachable* at all.
        let set_result = get_ipc_response(
            &webview,
            invoke_request("set_vault_path", serde_json::json!({ "path": "" })),
        );
        assert!(set_result.is_err(), "empty path should be rejected");
        let message = set_result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "set_vault_path should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );

        // A nonexistent file name is expected to error from the filesystem
        // read itself — same reachability point as above.
        let read_result = get_ipc_response(
            &webview,
            invoke_request(
                "read_vault_file",
                serde_json::json!({ "name": "does-not-exist.md" }),
            ),
        );
        assert!(
            read_result.is_err(),
            "reading a nonexistent file should error"
        );
        let message = read_result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "read_vault_file should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
    }
}
