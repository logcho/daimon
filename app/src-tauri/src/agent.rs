//! Supervision of the agent server process (the legacy workspace.rs pattern,
//! scaled down to the chat app's single always-on server).
//!
//! Flow: probe the fixed port — if a server answers /health, adopt it (the
//! user may have started `uv run daimon-agent` themselves). Otherwise spawn
//! `uv run daimon-agent` with cwd = agents/ (so its cwd-relative .env, vault
//! and memory resolve), on a freshly allocated port, then health-poll until
//! ready. Shutdown kills the whole process group — the agent spawns ipykernel
//! children, so a plain child-kill would orphan them.

use serde::{Deserialize, Serialize};
use std::fs::{File, OpenOptions};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager};

const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(500);
const HEALTH_POLL_TRIES: u32 = 60; // 30s budget, as the legacy daemon
const DEFAULT_PORT: u16 = 4711;

#[derive(Clone, Serialize)]
pub struct AgentStatus {
    pub running: bool,
    pub adopted: bool,
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
    Adopted { port: u16 },
}

pub struct AgentManager {
    inner: Mutex<Option<ChildState>>,
    /// Serializes ensure() — two concurrent calls (e.g. the mount effect
    /// double-firing in dev) must collapse into one spawn, not leak two.
    ensure_lock: tokio::sync::Mutex<()>,
    /// Preferred port — the adoption-probe target.
    port: u16,
    agents_dir: PathBuf,
    /// The orphan sweep runs once per app lifetime, not per ensure().
    reconciled: std::sync::Once,
}

impl AgentManager {
    pub fn new() -> Self {
        let agents_dir = std::env::var_os("DAIMON_AGENT_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../agents"));
        let port = std::env::var("DAIMON_APP_PORT")
            .ok()
            .and_then(|p| p.parse().ok())
            .unwrap_or(DEFAULT_PORT);
        Self {
            inner: Mutex::new(None),
            ensure_lock: tokio::sync::Mutex::new(()),
            port,
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
        {
            let cached: Option<(bool, u16)> = {
                let guard = self.inner.lock().unwrap();
                match &*guard {
                    Some(ChildState::Managed { port, .. }) => Some((false, *port)),
                    Some(ChildState::Adopted { port }) => Some((true, *port)),
                    None => None,
                }
            };
            if let Some((adopted, port)) = cached {
                if health(port).await.is_ok() {
                    return Ok(AgentStatus { running: true, adopted, port, busy: false, active_turns: 0, sessions: vec![] });
                }
                *self.inner.lock().unwrap() = None; // dead — respawn below
            }
        }
        // Nothing cached — probe the fixed port and adopt an external server.
        if health(self.port).await.is_ok() {
            *self.inner.lock().unwrap() = Some(ChildState::Adopted { port: self.port });
            return Ok(AgentStatus { running: true, adopted: true, port: self.port, busy: false, active_turns: 0, sessions: vec![] });
        }
        self.spawn(app, &run_dir).await
    }

    async fn spawn(&self, _app: &AppHandle, run_dir: &std::path::Path) -> Result<AgentStatus, String> {
        std::fs::create_dir_all(run_dir).map_err(|e| format!("cannot create run dir: {e}"))?;

        let out_path = run_dir.join("daimon-agent.out.log");
        let err_path = run_dir.join("daimon-agent.err.log");
        let log_out = OpenOptions::new().create(true).append(true).open(&out_path)
            .map_err(|e| format!("cannot open {}: {e}", out_path.display()))?;
        let log_err = OpenOptions::new().create(true).append(true).open(&err_path)
            .map_err(|e| format!("cannot open {}: {e}", err_path.display()))?;

        let port = allocate_port()?;
        let child = tokio::process::Command::new("uv")
            .args(["run", "daimon-agent"])
            .current_dir(&self.agents_dir)
            .env("PORT", port.to_string())
            .process_group(0) // group leader: kill(-pid) takes the tree down
            .stdout(std::process::Stdio::from(log_out.try_clone().map_err(|e| e.to_string())?))
            .stderr(std::process::Stdio::from(log_err.try_clone().map_err(|e| e.to_string())?))
            .spawn()
            .map_err(|e| format!("failed to spawn the agent server (is uv on PATH?): {e}"))?;
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
                return Ok(AgentStatus { running: true, adopted: false, port, busy: false, active_turns: 0, sessions: vec![] });
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

pub async fn delete_note(port: u16, name: &str) -> Result<serde_json::Value, String> {
    delete_json(port, &format!("vault/{}", encode_path(name))).await
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
