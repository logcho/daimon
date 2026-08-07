//! The notes vault: either a real Obsidian vault directory the user pointed
//! us at in Settings, or (absent that) a Daimon-managed default folder under
//! `workspace::data_dir()`. See `ARCHITECTURE.md` §3D and `PROMPT.md`'s
//! Phase 9 section — same mechanism either way, just a different host
//! directory, and it's this directory that `workspace::spawn_node_agent`
//! passes to every session's native Node agent process as
//! `DAIMON_VAULT_DIR` so its `write_note`/`read_note`/`list_notes` tools
//! (see `agents/src/vault.ts`) see the exact same files a browse-only caller
//! sees here.
//!
//! Browsing (`list_vault_files`/`read_vault_file`) never touches a
//! workspace process — the vault lives on the host filesystem, so the
//! daemon can read it directly for the UI without spinning up a session at
//! all.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crate::workspace;
use crate::timefmt::format_rfc3339;

/// Resolves the vault directory: `OBSIDIAN_VAULT_PATH` if set and non-empty,
/// otherwise `<app data dir>/vault`. Always ensures the directory exists
/// before returning — for the default case nothing else would create it; for
/// a user-configured path this is a no-op if it already exists (by the time
/// anything calls this for real use, `set_vault_path` should already have
/// validated it).
pub fn vault_path() -> PathBuf {
    let path = std::env::var("OBSIDIAN_VAULT_PATH")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| workspace::data_dir().join("vault"));

    let _ = std::fs::create_dir_all(&path);
    path
}

fn is_default_vault() -> bool {
    std::env::var("OBSIDIAN_VAULT_PATH")
        .map(|v| v.trim().is_empty())
        .unwrap_or(true)
}

/// Reduces an arbitrary IPC string to a bare filename — no path separators,
/// no `..` components — before it's ever joined onto `vault_path()`. Never
/// trust an IPC string argument for a filesystem path, even one the frontend
/// only ever echoes back from `list_vault_files`.
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
pub async fn set_vault_path(path: String) -> Result<(), String> {
    let path = path.trim();
    if path.is_empty() {
        return Err("vault path cannot be empty".into());
    }

    let candidate = PathBuf::from(path);
    if !candidate.is_dir() {
        return Err(format!(
            "\"{path}\" doesn't exist or isn't a directory — point this at an existing Obsidian vault folder"
        ));
    }

    workspace::set_env_var("OBSIDIAN_VAULT_PATH", path)
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
    let entries = std::fs::read_dir(&dir).map_err(|e| format!("failed to read vault directory: {e}"))?;

    let mut files = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| format!("failed to read vault directory entry: {e}"))?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        let metadata = entry.metadata().map_err(|e| format!("failed to read file metadata: {e}"))?;
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
    std::fs::read_to_string(&path).map_err(|e| format!("failed to read \"{safe_name}\": {e}"))
}

#[cfg(test)]
mod tests {
    use tauri::ipc::CallbackFn;
    use tauri::test::{get_ipc_response, mock_builder, INVOKE_KEY};
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
        // `Path::file_name()` on a path with directory components yields just
        // its last, normal component — "passwd" here, never a separator or
        // a literal "..", so joining it onto `vault_path()` can't escape it.
        let sanitized = super::sanitize_filename("../../etc/passwd").expect("should sanitize to a bare name");
        assert_eq!(sanitized, "passwd");
    }

    #[test]
    fn sanitize_filename_rejects_bare_dotdot() {
        assert!(super::sanitize_filename("..").is_err());
    }

    /// Same ACL-reachability pattern as `settings.rs`/`oauth.rs` — a missing
    /// `"allow-<command>"` capability entry compiles fine and only fails at
    /// runtime.
    #[test]
    fn vault_commands_clear_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let status = get_ipc_response(&webview, invoke_request("get_vault_path_status", serde_json::json!({})))
            .expect("get_vault_path_status should be allowed by the capability");
        let _: super::VaultPathStatus = status.deserialize().expect("expected a VaultPathStatus");

        let list = get_ipc_response(&webview, invoke_request("list_vault_files", serde_json::json!({})))
            .expect("list_vault_files should be allowed by the capability");
        let _: Vec<super::VaultFile> = list.deserialize().expect("expected a Vec<VaultFile>");

        // Empty path is expected to be rejected by set_vault_path's own
        // validation — the point here is that it's *reachable* at all (an
        // Err from application logic, not an ACL "not allowed" Err).
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
        // read itself, same reachability point as above.
        let read_result = get_ipc_response(
            &webview,
            invoke_request(
                "read_vault_file",
                serde_json::json!({ "name": "does-not-exist.md" }),
            ),
        );
        assert!(read_result.is_err(), "reading a nonexistent file should error");
        let message = read_result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "read_vault_file should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
    }
}
