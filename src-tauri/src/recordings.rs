//! Browsing and playback for the agent's recorded browser-session videos —
//! see `agents/src/browser.ts`'s `finishRecording` for how a `.webm` file
//! ends up in this directory in the first place, and `run.ts`'s periodic
//! `live_frame` events (a separate, unrelated mechanism) for the *live*
//! half of the in-app "watch it work" viewer this module's commands back.
//!
//! Same shape as `vault.rs` throughout, deliberately: recordings live on the
//! host filesystem (bind-mounted into every session's container — see
//! `workspace.rs`'s `recordings_mount`), so browsing them never needs a
//! container running at all, and `sanitize_filename` guards the same
//! path-traversal concern a browse-only IPC surface always has to.
//!
//! `read_recording_file` returns the video as a base64 string rather than
//! wiring up Tauri's asset-protocol/fs-scope machinery for a one-off need —
//! the same "just base64 it over IPC" choice already made for the
//! terminal's PTY output and `browser.ts`'s own screenshots, not a new
//! pattern. Simpler than a scoped asset URL, at the cost of paying IPC/
//! base64 overhead on however large the video is; fine for the short
//! task-demonstration clips this is meant for, worth revisiting if
//! recordings end up regularly multi-minutes-long.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use base64::{engine::general_purpose::STANDARD, Engine as _};

use crate::workspace;

fn recordings_dir() -> PathBuf {
    let dir = workspace::project_root().join("recordings");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// Dependency-free `SystemTime` -> RFC3339-ish string — copied verbatim from
/// `vault.rs`'s `format_rfc3339` rather than shared, since it's a handful of
/// lines and pulling in a shared-utility module for one function isn't worth
/// it yet; worth factoring out if a third caller ever needs the same thing.
fn format_rfc3339(time: SystemTime) -> String {
    let secs = time
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    let days_since_epoch = (secs / 86_400) as i64;
    let secs_of_day = secs % 86_400;
    let (hour, minute, second) = (secs_of_day / 3600, (secs_of_day % 3600) / 60, secs_of_day % 60);

    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { y + 1 } else { y };

    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

/// Same reasoning/implementation as `vault.rs`'s `sanitize_filename` — never
/// trust an IPC string argument for a filesystem path, even one the frontend
/// only ever echoes back from `list_recordings`.
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
pub struct RecordingFile {
    name: String,
    size_bytes: u64,
    modified_at: String,
}

#[tauri::command]
pub async fn list_recordings() -> Result<Vec<RecordingFile>, String> {
    let dir = recordings_dir();
    let entries = std::fs::read_dir(&dir).map_err(|e| format!("failed to read recordings directory: {e}"))?;

    let mut files = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| format!("failed to read recordings directory entry: {e}"))?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("webm") {
            continue;
        }
        let metadata = entry.metadata().map_err(|e| format!("failed to read file metadata: {e}"))?;
        // A recording context that was never explicitly finished (see
        // `finishRecording`'s doc comment — Playwright only writes the file
        // on context close, not continuously) leaves a genuine 0-byte
        // placeholder on disk. Skip those rather than showing the user an
        // entry that can't actually play.
        if !metadata.is_file() || metadata.len() == 0 {
            continue;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        let modified = metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH);
        files.push((
            RecordingFile {
                name,
                size_bytes: metadata.len(),
                modified_at: format_rfc3339(modified),
            },
            modified,
        ));
    }

    // Most-recently-modified first — same default as `vault.rs`'s file list,
    // surfacing whatever the agent just recorded without the user having to
    // hunt for it.
    files.sort_by(|a, b| b.1.cmp(&a.1));
    Ok(files.into_iter().map(|(f, _)| f).collect())
}

/// Returns the video's raw bytes, base64-encoded — see this module's doc
/// comment for why base64-over-IPC rather than the asset protocol.
#[tauri::command]
pub async fn read_recording_file(name: String) -> Result<String, String> {
    let safe_name = sanitize_filename(&name)?;
    let path = recordings_dir().join(&safe_name);
    let bytes = std::fs::read(&path).map_err(|e| format!("failed to read \"{safe_name}\": {e}"))?;
    Ok(STANDARD.encode(bytes))
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
        let sanitized = super::sanitize_filename("../../etc/passwd").expect("should sanitize to a bare name");
        assert_eq!(sanitized, "passwd");
    }

    #[test]
    fn sanitize_filename_rejects_bare_dotdot() {
        assert!(super::sanitize_filename("..").is_err());
    }

    /// Same ACL-reachability pattern as `vault.rs`'s `vault_commands_clear_the_acl`.
    #[test]
    fn recordings_commands_clear_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let list = get_ipc_response(&webview, invoke_request("list_recordings", serde_json::json!({})))
            .expect("list_recordings should be allowed by the capability");
        let _: Vec<super::RecordingFile> = list.deserialize().expect("expected a Vec<RecordingFile>");

        // A nonexistent file name is expected to error from the filesystem
        // read itself — the point here is that it's *reachable* at all (an
        // Err from application logic, not an ACL "not allowed" Err).
        let read_result = get_ipc_response(
            &webview,
            invoke_request(
                "read_recording_file",
                serde_json::json!({ "name": "does-not-exist.webm" }),
            ),
        );
        assert!(read_result.is_err(), "reading a nonexistent file should error");
        let message = read_result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "read_recording_file should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
    }
}
