//! Minimal workspace helpers — `data_dir()` and `set_env_var()`.
//!
//! The legacy workspace.rs is a full 1843-line session/process/browser manager
//! that the new Python-agent-based app doesn't need. Only the vault module
//! (and potentially future settings modules) need a durable app-data directory
//! and an env-file persistence helper.

use std::path::{Path, PathBuf};

/// The app data directory. Uses `dirs_next::data_dir()` — the real OS-specific
/// app-data path (`~/Library/Application Support/com.daimon.app` on macOS).
/// Falls back to the git checkout root (via `CARGO_MANIFEST_DIR`) when the
/// OS directory isn't available (tests, dev without a bundle).
pub(crate) fn data_dir() -> PathBuf {
    if let Some(dir) = dirs_next::data_dir() {
        let path = dir.join("com.daimon.app");
        let _ = std::fs::create_dir_all(&path);
        return path;
    }
    // Fallback for environments where the OS data dir isn't available
    // (e.g. some CI runners, or before the app is fully initialized).
    project_root()
}

fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent directory")
        .to_path_buf()
}

/// Upserts `KEY=value` into the app data directory's `.env` file and mirrors
/// it into this process's live environment. Used by `set_vault_path` to persist
/// the user's chosen vault directory and by any future settings command.
pub(crate) fn set_env_var(key: &str, value: &str) -> Result<(), String> {
    let env_path = data_dir().join(".env");
    let mut lines: Vec<String> = if env_path.exists() {
        std::fs::read_to_string(&env_path)
            .map_err(|e| format!("failed to read .env: {e}"))?
            .lines()
            .map(|l| l.to_string())
            .collect()
    } else {
        Vec::new()
    };

    let prefix = format!("{key}=");
    let mut found = false;
    for line in lines.iter_mut() {
        if line.starts_with(&prefix) {
            *line = format!("{key}={value}");
            found = true;
            break;
        }
    }
    if !found {
        lines.push(format!("{key}={value}"));
    }

    if let Some(parent) = env_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    std::fs::write(&env_path, lines.join("\n") + "\n")
        .map_err(|e| format!("failed to write .env: {e}"))?;

    // SAFETY: single-value update from a user-initiated Tauri command; not
    // realistically raced concurrently in this app's usage.
    unsafe {
        std::env::set_var(key, value);
    }

    Ok(())
}
