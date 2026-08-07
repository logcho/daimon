//! Browsing and playback for the agent's recorded browser-session videos —
//! see `agents/src/browser.ts`'s `finishRecording` for how a `.webm` file
//! ends up in this directory in the first place, and `run.ts`'s periodic
//! `live_frame` events (a separate, unrelated mechanism) for the *live*
//! half of the in-app "watch it work" viewer this module's commands back.
//!
//! Same shape as `vault.rs` throughout, deliberately: recordings live on the
//! host filesystem (passed to every session's native Node agent process as
//! the `DAIMON_RECORDINGS_DIR` env var — see `workspace.rs`'s
//! `spawn_node_agent`), so browsing them never needs a workspace process
//! running at all, and `sanitize_filename` guards the same path-traversal
//! concern a browse-only IPC surface always has to.
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

pub(crate) fn recordings_dir() -> PathBuf {
    let dir = workspace::data_dir().join("recordings");
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

/// A single JPEG frame from a recording, base64-encoded, for the gallery grid.
///
/// The frontend used to build these itself: for every file in the list it
/// called `read_recording_file`, which base64-encodes the *entire* `.webm`
/// over IPC, then seeked a detached `<video>` to 0.3s and drew it to a
/// canvas. Opening the tab therefore downloaded every recording in full to
/// produce a handful of thumbnails — by a wide margin the most expensive
/// thing the frontend did.
///
/// `ffmpeg` is already bundled as a sidecar (for PinchTab's own recording
/// encode step), so extracting one frame host-side costs a few tens of KB
/// over IPC instead of the whole video. Results are cached under
/// `recordings/.thumbs/` — the leading dot keeps them out of `list_recordings`,
/// which only reports `.webm` files.
///
/// Seeks to 0.3s rather than 0 because the first frame of a browser capture is
/// reliably a blank white or black page, before anything has painted.
///
/// `durationSeconds` comes from the same invocation: ffmpeg prints the
/// container's `Duration:` header on stderr before decoding anything, so the
/// gallery's duration badge survives without a second process or a bundled
/// ffprobe. 0.0 when the header can't be parsed — the frontend hides the badge
/// rather than showing a wrong one.
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RecordingThumbnail {
    /// Base64-encoded JPEG.
    pub data: String,
    pub duration_seconds: f64,
}

/// Parses ffmpeg's `  Duration: 00:01:02.34, start: ...` stderr line.
fn parse_duration(stderr: &str) -> f64 {
    for line in stderr.lines() {
        let Some(rest) = line.trim().strip_prefix("Duration:") else {
            continue;
        };
        let value = rest.split(',').next().unwrap_or("").trim();
        let mut parts = value.split(':');
        let (Some(h), Some(m), Some(s)) = (parts.next(), parts.next(), parts.next()) else {
            continue;
        };
        if let (Ok(h), Ok(m), Ok(s)) = (h.parse::<f64>(), m.parse::<f64>(), s.parse::<f64>()) {
            return h * 3600.0 + m * 60.0 + s;
        }
    }
    0.0
}

#[tauri::command]
pub async fn get_recording_thumbnail(name: String) -> Result<RecordingThumbnail, String> {
    let safe_name = sanitize_filename(&name)?;
    let source = recordings_dir().join(&safe_name);
    if !source.exists() {
        return Err(format!("no such recording: {safe_name}"));
    }

    let cache_dir = recordings_dir().join(".thumbs");
    std::fs::create_dir_all(&cache_dir).map_err(|e| format!("failed to create thumbnail cache: {e}"))?;
    let cached = cache_dir.join(format!("{safe_name}.jpg"));
    // Duration is cheap to store alongside the frame and would otherwise cost
    // a second ffmpeg run on every cache hit.
    let cached_duration = cache_dir.join(format!("{safe_name}.dur"));

    // Regenerate if the recording has been rewritten since the thumbnail was
    // made. A recording is normally write-once, but `finish_recording` can
    // reuse a name, and a stale thumbnail is worse than a slow one.
    let fresh = match (std::fs::metadata(&cached), std::fs::metadata(&source)) {
        (Ok(thumb), Ok(video)) => match (thumb.modified(), video.modified()) {
            (Ok(t), Ok(v)) => t >= v,
            _ => false,
        },
        _ => false,
    };

    if fresh {
        if let Ok(bytes) = std::fs::read(&cached) {
            let duration_seconds = std::fs::read_to_string(&cached_duration)
                .ok()
                .and_then(|s| s.trim().parse().ok())
                .unwrap_or(0.0);
            return Ok(RecordingThumbnail { data: STANDARD.encode(bytes), duration_seconds });
        }
    }

    let ffmpeg = workspace::ffmpeg_binary()
        .ok_or_else(|| "ffmpeg sidecar not found — run scripts/fetch-sidecars.sh".to_string())?;

    let output = tokio::process::Command::new(&ffmpeg)
        .args(["-y", "-ss", "0.3", "-i"])
        .arg(&source)
        // -frames:v 1 stops after one frame; scale caps width at 480 and
        // derives the height (-2 keeps the aspect ratio and an even number of
        // lines, which the JPEG encoder requires).
        .args(["-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5"])
        .arg(&cached)
        .output()
        .await
        .map_err(|e| format!("failed to run ffmpeg: {e}"))?;

    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        // ffmpeg is extremely verbose on stderr even when it succeeds, so only
        // the tail is worth surfacing.
        let tail: String = stderr.lines().rev().take(3).collect::<Vec<_>>().join(" | ");
        return Err(format!("ffmpeg could not extract a frame from \"{safe_name}\": {tail}"));
    }

    let duration_seconds = parse_duration(&stderr);
    let _ = std::fs::write(&cached_duration, duration_seconds.to_string());

    let bytes = std::fs::read(&cached).map_err(|e| format!("failed to read generated thumbnail: {e}"))?;
    Ok(RecordingThumbnail { data: STANDARD.encode(bytes), duration_seconds })
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
