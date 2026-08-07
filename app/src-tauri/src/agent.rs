//! Supervision of the agent server process (the legacy workspace.rs pattern,
//! scaled down to the chat app's single always-on server).
//!
//! Flow: probe the fixed port — if a server answers /health, adopt it (the
//! user may have started `uv run daimon-agent` themselves). Otherwise spawn
//! `uv run daimon-agent` with cwd = agents/ (so its cwd-relative .env, vault
//! and memory resolve), on a freshly allocated port, then health-poll until
//! ready. Shutdown kills the whole process group — the agent spawns ipykernel
//! children, so a plain child-kill would orphan them.

use serde::Serialize;
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
    /// Preferred port — the adoption-probe target.
    port: u16,
    agents_dir: PathBuf,
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
            port,
            agents_dir,
        }
    }

    /// Report status, self-healing the cached state: a server that no longer
    /// answers /health is dropped (the next ensure() will spawn a fresh one).
    /// The mutex is never held across an await (Tauri commands must be Send).
    pub async fn status(&self) -> AgentStatus {
        let cached: Option<(bool, u16)> = {
            let guard = self.inner.lock().unwrap();
            match &*guard {
                Some(ChildState::Managed { port, .. }) => Some((false, *port)),
                Some(ChildState::Adopted { port }) => Some((true, *port)),
                None => None,
            }
        };
        match cached {
            Some((adopted, port)) => {
                if health(port).await.is_ok() {
                    AgentStatus { running: true, adopted, port }
                } else {
                    *self.inner.lock().unwrap() = None;
                    AgentStatus { running: false, adopted: false, port }
                }
            }
            None => {
                if health(self.port).await.is_ok() {
                    *self.inner.lock().unwrap() = Some(ChildState::Adopted { port: self.port });
                    AgentStatus { running: true, adopted: true, port: self.port }
                } else {
                    AgentStatus { running: false, adopted: false, port: self.port }
                }
            }
        }
    }

    /// Make sure a server is up, then report its address. Idempotent.
    pub async fn ensure(&self, app: &AppHandle) -> Result<AgentStatus, String> {
        {
            let guard = self.inner.lock().unwrap();
            match &*guard {
                Some(ChildState::Managed { port, .. }) => {
                    return Ok(AgentStatus { running: true, adopted: false, port: *port });
                }
                Some(ChildState::Adopted { port }) => {
                    return Ok(AgentStatus { running: true, adopted: true, port: *port });
                }
                None => {}
            }
        }
        // Nothing cached — probe the fixed port and adopt an external server.
        if health(self.port).await.is_ok() {
            *self.inner.lock().unwrap() = Some(ChildState::Adopted { port: self.port });
            return Ok(AgentStatus { running: true, adopted: true, port: self.port });
        }
        self.spawn(app).await
    }

    async fn spawn(&self, app: &AppHandle) -> Result<AgentStatus, String> {
        let run_dir = app
            .path()
            .app_data_dir()
            .map(|d| d.join("run"))
            .map_err(|e| format!("no app data dir: {e}"))?;
        std::fs::create_dir_all(&run_dir).map_err(|e| format!("cannot create run dir: {e}"))?;
        self.reconcile_orphans(&run_dir);

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
                let _ = std::fs::remove_file(&pid_path);
                return Ok(AgentStatus { running: true, adopted: false, port });
            }
            tokio::time::sleep(HEALTH_POLL_INTERVAL).await;
        }
        let _ = kill_group(pid);
        Err(format!(
            "agent server did not become healthy on port {port} — see {}",
            err_path.display()
        ))
    }

    /// Kill a server left behind by a crashed previous run (recorded in the
    /// pid file). Checks the command name first so a recycled pid is never
    /// killed blind.
    fn reconcile_orphans(&self, run_dir: &std::path::Path) {
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
