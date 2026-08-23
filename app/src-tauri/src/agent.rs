//! Supervision of the agent server process (the legacy workspace.rs pattern,
//! scaled down to the chat app's single always-on server).
//!
//! Flow: the app owns its server outright — it never adopts whatever happens
//! to answer on some well-known port (that could just as easily be a
//! `daimon` CLI server for some project directory, with a different
//! confinement root; the CLI itself now runs one server *per workspace*, so
//! there is no single well-known port to adopt from any more). If there's no
//! live cached child, spawn `uv run daimon-agent` with cwd = agents/ (so its
//! cwd-relative .env resolves) on a freshly allocated port, with
//! DAIMON_WORKSPACE_DIR/DAIMON_VAULT_DIR pinned to one fixed app-owned
//! directory — chat has no per-tab or per-session "workspace" concept, every
//! tab confines to the same place — then health-poll until ready. Shutdown
//! kills the whole process group — the agent spawns ipykernel children, so a
//! plain child-kill would orphan them.

use serde::{Deserialize, Serialize};
use std::fs::{File, OpenOptions};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager};

const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(500);
const HEALTH_POLL_TRIES: u32 = 60; // 30s budget, as the legacy daemon

#[derive(Clone, Serialize)]
pub struct AgentStatus {
    pub running: bool,
    pub port: u16,
    #[serde(default)]
    pub busy: bool,
    #[serde(default)]
    pub active_turns: u32,
    #[serde(default)]
    pub sessions: Vec<String>,
}

enum ChildState {
    Managed {
        pid: u32,
        port: u16,
        _child: tokio::process::Child,
        _log_out: File,
        _log_err: File,
    },
}

pub struct AgentManager {
    inner: Mutex<Option<ChildState>>,
    /// Serializes ensure() — two concurrent calls (e.g. the mount effect
    /// double-firing in dev) must collapse into one spawn, not leak two.
    ensure_lock: tokio::sync::Mutex<()>,
    /// Manual fixed-port override (DAIMON_APP_PORT). `None` means "always
    /// allocate a fresh port" — the app never adopts a well-known port any
    /// more, so there's no adoption-probe target to default this to.
    fixed_port: Option<u16>,
    agents_dir: Option<PathBuf>,
    /// The orphan sweep runs once per app lifetime, not per ensure().
    reconciled: std::sync::Once,
}

impl AgentManager {
    pub fn new() -> Self {
        let agents_dir = crate::paths::agents_dir();
        let fixed_port = std::env::var("DAIMON_APP_PORT")
            .ok()
            .and_then(|p| p.parse().ok());
        Self {
            inner: Mutex::new(None),
            ensure_lock: tokio::sync::Mutex::new(()),
            fixed_port,
            agents_dir,
            reconciled: std::sync::Once::new(),
        }
    }

    /// Make sure a server is up, then report its address. Idempotent and
    /// self-healing: a cached child that no longer answers /health is dropped
    /// and a fresh one is spawned. The mutex is never held across an await.
    pub async fn ensure(&self, app: &AppHandle) -> Result<AgentStatus, String> {
        // Serialized: a concurrent caller waits for the in-flight spawn, then
        // sees the healthy cached child and returns instead of spawning its
        // own (which would leak the first server).
        let _guard = self.ensure_lock.lock().await;
        // Sweep a server left behind by an abruptly-killed previous run (the
        // pid file is kept after a successful spawn for exactly this) before
        // probing or spawning.
        let run_dir = app
            .path()
            .app_data_dir()
            .map(|d| d.join("run"))
            .map_err(|e| format!("no app data dir: {e}"))?;
        // Once per app lifetime. `spawn` deliberately leaves its pid file in
        // place so a crashed parent still gets swept next launch — which means
        // the file names *our own* child as soon as we've spawned one. Running
        // the sweep on every ensure() therefore killed the server the previous
        // call had just started, and the next call spawned another: an endless
        // respawn loop, with a fresh PinchTab each time. Nothing here needs to
        // run twice; the leftovers it looks for are from a previous process.
        self.reconciled.call_once(|| self.reconcile_orphans(&run_dir));

        let cached: Option<u16> = {
            let guard = self.inner.lock().unwrap();
            match &*guard {
                Some(ChildState::Managed { port, .. }) => Some(*port),
                None => None,
            }
        };
        if let Some(port) = cached {
            if health(port).await.is_ok() {
                return Ok(AgentStatus { running: true, port, busy: false, active_turns: 0, sessions: vec![] });
            }
            *self.inner.lock().unwrap() = None; // dead — respawn below
        }
        // The app owns its server: never adopt whatever answers some
        // well-known port — it could be a CLI-spawned server for a project
        // directory, with a different (or absent) confinement root.
        self.spawn(app, &run_dir).await
    }

    async fn spawn(&self, _app: &AppHandle, run_dir: &std::path::Path) -> Result<AgentStatus, String> {
        let agents_dir = self.agents_dir.as_deref().filter(|d| d.is_dir()).ok_or_else(|| {
            match &self.agents_dir {
                Some(tried) => format!(
                    "agents/ directory not found at {} — set DAIMON_AGENT_DIR to the \
                     correct absolute path and restart the app, e.g.:\n\n    \
                     export DAIMON_AGENT_DIR=/path/to/daimon/agents\n",
                    tried.display()
                ),
                None => "can't find the agents/ directory (the app isn't running from \
                         inside a daimon checkout) — set DAIMON_AGENT_DIR to its absolute \
                         path and restart the app, e.g.:\n\n    \
                         export DAIMON_AGENT_DIR=/path/to/daimon/agents\n"
                    .to_string(),
            }
        })?;

        std::fs::create_dir_all(run_dir).map_err(|e| format!("cannot create run dir: {e}"))?;

        let out_path = run_dir.join("daimon-agent.out.log");
        let err_path = run_dir.join("daimon-agent.err.log");
        let log_out = OpenOptions::new().create(true).append(true).open(&out_path)
            .map_err(|e| format!("cannot open {}: {e}", out_path.display()))?;
        let log_err = OpenOptions::new().create(true).append(true).open(&err_path)
            .map_err(|e| format!("cannot open {}: {e}", err_path.display()))?;

        // Resolved to an absolute path, never spawned as a bare "uv": a
        // Dock/Finder launch inherits only launchd's minimal PATH, which does
        // not include ~/.local/bin where uv installs itself. See paths.rs.
        let uv = crate::paths::uv_bin().ok_or_else(|| {
            "can't find the `uv` command, which runs the agent server. The app is \
             launched by macOS without your shell's PATH, so a uv installed under \
             your home directory isn't visible to it.\n\n\
             Install uv (https://docs.astral.sh/uv/), or point the app at an \
             existing one and restart it:\n\n    \
             launchctl setenv DAIMON_UV_BIN /full/path/to/uv\n"
                .to_string()
        })?;

        let port = match self.fixed_port {
            Some(p) => p,
            None => allocate_port()?,
        };
        // The one fixed app-owned directory every chat tab confines its
        // tools to — reuses vault::vault_path() so the app's file-browsing
        // "vault" and the agent's actual tool-confinement root are the same
        // directory (previously vault.rs's DAIMON_VAULT_DIR had zero effect
        // on the agent). Set explicitly rather than relying on inherited env,
        // in case a stray DAIMON_WORKSPACE_DIR is already exported in the
        // parent shell the app was launched from.
        let confinement_root = crate::vault::vault_path();
        let child = tokio::process::Command::new(&uv)
            .args(["run", "daimon-agent"])
            .current_dir(agents_dir)
            .env("PORT", port.to_string())
            .env("PATH", crate::paths::augmented_path())
            .env("DAIMON_WORKSPACE_DIR", &confinement_root)
            .env("DAIMON_VAULT_DIR", &confinement_root)
            .process_group(0) // group leader: kill(-pid) takes the tree down
            .stdout(std::process::Stdio::from(log_out.try_clone().map_err(|e| e.to_string())?))
            .stderr(std::process::Stdio::from(log_err.try_clone().map_err(|e| e.to_string())?))
            .spawn()
            .map_err(|e| format!(
                "failed to spawn `{} run daimon-agent` in {}: {e}",
                uv.display(),
                agents_dir.display()
            ))?;
        let pid = child.id().ok_or_else(|| "spawned process has no pid".to_string())?;
        // Deliberately kept after a successful spawn: reconcile_orphans reads
        // it on the next launch so an abruptly-killed parent (SIGKILL, crash)
        // still sweeps its server. A dead or recycled pid fails the comm
        // check and is ignored.
        let pid_path = run_dir.join("daimon-agent.pid");
        let _ = std::fs::write(&pid_path, pid.to_string());

        for _ in 0..HEALTH_POLL_TRIES {
            if health(port).await.is_ok() {
                *self.inner.lock().unwrap() = Some(ChildState::Managed {
                    pid,
                    port,
                    _child: child,
                    _log_out: log_out,
                    _log_err: log_err,
                });
                return Ok(AgentStatus { running: true, port, busy: false, active_turns: 0, sessions: vec![] });
            }
            tokio::time::sleep(HEALTH_POLL_INTERVAL).await;
        }
        let _ = kill_group(pid);
        Err(format!(
            "agent server did not become healthy on port {port} — see {}",
            err_path.display()
        ))
    }

    /// Kill a server whose parent app is gone but that is still alive — an
    /// abruptly-killed run (SIGKILL, crash) or a previous app instance that
    /// the dev watcher replaced. The pid file survives a successful spawn
    /// precisely so this works. Checks the command name first so a recycled
    /// pid is never killed blind.
    /// Kill a server left behind by an abruptly-killed previous run. Only
    /// ever called through `reconciled.call_once` — see the note at the call
    /// site for why running it more than once is destructive.
    fn reconcile_orphans(&self, run_dir: &std::path::Path) {
        // Never sweep a child we're currently managing.
        if matches!(&*self.inner.lock().unwrap(), Some(ChildState::Managed { .. })) {
            return;
        }
        let pid_path = run_dir.join("daimon-agent.pid");
        let Ok(pid_text) = std::fs::read_to_string(&pid_path) else { return };
        let _ = std::fs::remove_file(&pid_path);
        let Ok(pid) = pid_text.trim().parse::<u32>() else { return };
        if process_comm(pid).is_some_and(|comm| comm.contains("uv")) {
            let _ = kill_group(pid);
        }
    }

    pub async fn shutdown(&self) -> Result<(), String> {
        let mut guard = self.inner.lock().unwrap();
        if let Some(ChildState::Managed { pid, .. }) = guard.take() {
            let _ = kill_group(pid);
        }
        Ok(())
    }

    /// Synchronous variant for the RunEvent::Exit path (no runtime needed).
    pub fn kill_sync(&self) {
        let mut guard = self.inner.lock().unwrap();
        if let Some(ChildState::Managed { pid, .. }) = guard.take() {
            let _ = kill_group(pid);
        }
    }
}

impl Default for AgentManager {
    fn default() -> Self {
        Self::new()
    }
}

/// Deserialized from `GET /status` — unknown fields are ignored so an older
/// server binary (without the endpoint) degrades gracefully to not-busy.
#[derive(Deserialize)]
#[serde(default)]
pub struct BusyState {
    pub busy: bool,
    pub active_turns: u32,
    pub sessions: Vec<String>,
}

impl Default for BusyState {
    fn default() -> Self {
        Self { busy: false, active_turns: 0, sessions: vec![] }
    }
}

/// Fetch the global busy state from the agent server. An older server (no
/// /status endpoint) or a connection error returns the default: not busy.
pub async fn fetch_busy_state(port: u16) -> BusyState {
    let Ok(resp) = reqwest::get(format!("http://127.0.0.1:{port}/status")).await else {
        return BusyState::default();
    };
    resp.json::<BusyState>().await.unwrap_or_default()
}

/// Fetch the agent's non-secret configuration (settings tab).
pub async fn fetch_config(port: u16) -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("http://127.0.0.1:{port}/config"))
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("config endpoint returned {}", resp.status()));
    }
    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("invalid config response: {e}"))
}

/// Update agent configuration (currently: api_key).
/// GET /skills from the agent server.
///
/// The app used to walk the filesystem itself, which meant two independent
/// answers to "where do skills live" — and they disagreed, so the tab was
/// always empty. The server owns that question now.
pub async fn list_skills(port: u16) -> Result<serde_json::Value, String> {
    get_json(port, "skills").await
}

/// GET /skills/{name} — one skill with its full content.
pub async fn read_skill(port: u16, name: &str) -> Result<serde_json::Value, String> {
    get_json(port, &format!("skills/{name}")).await
}

async fn get_json(port: u16, path: &str) -> Result<serde_json::Value, String> {
    let resp = reqwest::Client::new()
        .get(format!("http://127.0.0.1:{port}/{path}"))
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("agent server returned {}", resp.status()));
    }
    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("invalid response: {e}"))
}

/// GET /vault — the agent's notes, from the server that owns the vault. The
/// app used to walk its own vault path and showed an entirely different set of
/// files, which would have made deletion act on the wrong ones.
pub async fn list_notes(port: u16) -> Result<serde_json::Value, String> {
    get_json(port, "vault").await
}

pub async fn read_note(port: u16, name: &str) -> Result<serde_json::Value, String> {
    get_json(port, &format!("vault/{}", encode_path(name))).await
}

/// GET /vault?all=1 — every file in the vault, not just the notes.
///
/// A separate call rather than a flag on `list_notes`: markdown-only is what
/// the agent's tools and a paired phone both mean by "note", and this is the
/// one surface that wants the whole folder.
pub async fn list_vault_entries(port: u16) -> Result<serde_json::Value, String> {
    get_json(port, "vault?all=1").await
}

/// PUT /vaultfile/{name} — write raw bytes, any file type.
///
/// Not `/vault/{name}`: that route is markdown-only, and shared with the bus
/// op a phone writes through. `application/octet-stream` rather than base64 so
/// an imported file survives the trip byte-for-byte without being re-encoded
/// at either end.
pub async fn write_vault_bytes(
    port: u16,
    name: &str,
    bytes: Vec<u8>,
) -> Result<serde_json::Value, String> {
    let resp = reqwest::Client::new()
        .put(format!(
            "http://127.0.0.1:{port}/vaultfile/{}",
            encode_path(name)
        ))
        .header("content-type", "application/octet-stream")
        .body(bytes)
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    unwrap_vault_response(resp, "could not write the file").await
}

pub async fn delete_note(port: u16, name: &str) -> Result<serde_json::Value, String> {
    delete_json(port, &format!("vault/{}", encode_path(name))).await
}

/// PUT /vault/{name} — create or overwrite a note. Same endpoint for both,
/// mirroring the server: a new note is just a write to a name that doesn't
/// exist yet.
pub async fn write_note(port: u16, name: &str, content: &str) -> Result<serde_json::Value, String> {
    let resp = reqwest::Client::new()
        .put(format!("http://127.0.0.1:{port}/vault/{}", encode_path(name)))
        .json(&serde_json::json!({ "content": content }))
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    unwrap_vault_response(resp, "write failed").await
}

/// GET /vault/folders — folders including empty ones, which the note listing
/// cannot show.
pub async fn list_folders(port: u16) -> Result<serde_json::Value, String> {
    get_json(port, "vault/folders").await
}

/// POST /vault/folders — create a folder.
pub async fn create_folder(port: u16, path: &str) -> Result<serde_json::Value, String> {
    let resp = reqwest::Client::new()
        .post(format!("http://127.0.0.1:{port}/vault/folders"))
        .json(&serde_json::json!({ "path": path }))
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    unwrap_vault_response(resp, "could not create the folder").await
}

/// DELETE /vault/folders/{path} — remove a folder. `recursive` is required by
/// the server for a folder that still holds notes.
pub async fn delete_folder(
    port: u16,
    path: &str,
    recursive: bool,
) -> Result<serde_json::Value, String> {
    let suffix = if recursive { "?recursive=1" } else { "" };
    let resp = reqwest::Client::new()
        .delete(format!(
            "http://127.0.0.1:{port}/vault/folders/{}{suffix}",
            encode_path(path)
        ))
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    unwrap_vault_response(resp, "could not delete the folder").await
}

/// POST /vault/move — rename a note or move it into another folder.
pub async fn move_note(port: u16, from: &str, to: &str) -> Result<serde_json::Value, String> {
    let resp = reqwest::Client::new()
        .post(format!("http://127.0.0.1:{port}/vault/move"))
        .json(&serde_json::json!({ "from": from, "to": to }))
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    unwrap_vault_response(resp, "could not move the note").await
}

/// Shared success/error unwrapping for the mutating vault endpoints. The
/// server explains *why* it refused (name already taken, folder not empty,
/// path outside the vault) in an `error` field; surfacing that beats a bare
/// status code, since every one of those is something the user can act on.
async fn unwrap_vault_response(
    resp: reqwest::Response,
    fallback: &str,
) -> Result<serde_json::Value, String> {
    let status = resp.status();
    let body = resp
        .json::<serde_json::Value>()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    if !status.is_success() {
        let message = body.get("error").and_then(|e| e.as_str()).unwrap_or(fallback);
        return Err(message.to_string());
    }
    Ok(body)
}

pub async fn delete_skill(port: u16, name: &str) -> Result<serde_json::Value, String> {
    delete_json(port, &format!("skills/{}", encode_path(name))).await
}

/// Percent-encode a name for use as a URL path, keeping `/` as a separator —
/// a note's name is a relative path like `notes/foo.md`, and the server's
/// route matches across slashes. Everything else outside the unreserved set is
/// escaped, so a filename with a space or a `#` doesn't truncate the request.
/// The server re-checks containment regardless; this is about transport, not
/// safety.
fn encode_path(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    for byte in name.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' => {
                out.push(*byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

async fn delete_json(port: u16, path: &str) -> Result<serde_json::Value, String> {
    let resp = reqwest::Client::new()
        .delete(format!("http://127.0.0.1:{port}/{path}"))
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("delete failed ({})", resp.status()));
    }
    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("invalid response: {e}"))
}

/// GET /models from the agent server.
pub async fn list_models(port: u16) -> Result<serde_json::Value, String> {
    let resp = reqwest::Client::new()
        .get(format!("http://127.0.0.1:{port}/models"))
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("invalid models response: {e}"))
}

/// POST /config with an arbitrary field map. The server decides what each
/// field means — including routing a bare `key` to the right provider by its
/// prefix — so this stays a passthrough rather than a second place that has to
/// know about providers.
pub async fn update_config(
    port: u16,
    fields: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("http://127.0.0.1:{port}/config"))
        .json(&fields)
        .send()
        .await
        .map_err(|e| format!("failed to reach agent server: {e}"))?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("config update failed ({status}): {body}"));
    }
    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("invalid config response: {e}"))
}

async fn health(port: u16) -> Result<(), String> {
    reqwest::get(format!("http://127.0.0.1:{port}/health"))
        .await
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// Bind-then-release port allocation (the legacy pattern — a tiny race that
/// is fine for a single app instance).
fn allocate_port() -> Result<u16, String> {
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0))
        .map_err(|e| format!("cannot allocate a port: {e}"))?;
    Ok(listener.local_addr().map_err(|e| e.to_string())?.port())
}

/// SIGKILL the process group whose leader is `pid` (its own pgid, since we
/// spawned it with process_group(0)).
fn kill_group(pid: u32) -> Result<(), String> {
    let rc = unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
    if rc == 0 {
        Ok(())
    } else {
        Err(format!("kill(-{pid}) failed: {}", std::io::Error::last_os_error()))
    }
}

fn process_comm(pid: u32) -> Option<String> {
    let out = std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "comm="])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let comm = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if comm.is_empty() { None } else { Some(comm) }
}
