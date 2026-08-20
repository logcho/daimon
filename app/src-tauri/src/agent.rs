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

/// Where `uv` is commonly installed, in the order they're tried. A GUI process
/// has none of these on its PATH, and the first is uv's own installer default.
const UV_WELL_KNOWN_PATHS: &[&str] = &[
    "~/.local/bin/uv",
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
    "~/.cargo/bin/uv",
];

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
    /// Whether the previous run's leftover server has been swept yet. The
    /// sweep is a launch-time job, not a per-call one — see `ensure`.
    orphans_reconciled: std::sync::atomic::AtomicBool,
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

/// The first directory in `dirs` that actually holds a `uv` file. Split out
/// from the `PATH` lookup below so it's testable without mutating the
/// process environment, which tests running in parallel can't do safely.
fn find_uv_in(dirs: impl Iterator<Item = PathBuf>) -> Option<PathBuf> {
    dirs.map(|dir| dir.join("uv")).find(|candidate| candidate.is_file())
}

/// Search `PATH` for an executable named `uv`, the way a shell would.
fn uv_on_path() -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    find_uv_in(std::env::split_paths(&path))
}

/// Resolve a `~/`-prefixed candidate against `home`. Anything else is already
/// absolute and passes through untouched.
fn expand_home(candidate: &str, home: Option<&PathBuf>) -> Option<PathBuf> {
    match candidate.strip_prefix("~/") {
        Some(rest) => home.map(|home| home.join(rest)),
        None => Some(PathBuf::from(candidate)),
    }
}

/// Ask the user's login shell where `uv` is. The catch-all for installs this
/// module can't enumerate — mise, asdf, nix, a custom prefix — because a login
/// shell sources the same profile that puts `uv` on PATH in a real terminal.
/// Costs one shell spawn, and only when every cheaper lookup has already
/// failed, which is why it's last.
fn uv_from_login_shell() -> Option<PathBuf> {
    let shell = std::env::var("SHELL").ok()?;
    let output = std::process::Command::new(shell)
        .args(["-lc", "command -v uv"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let found = PathBuf::from(String::from_utf8(output.stdout).ok()?.trim());
    found.is_file().then_some(found)
}

/// Absolute path to the `uv` binary, resolved once per process.
///
/// A bare `Command::new("uv")` works in `tauri dev` (which inherits the
/// developer's shell environment) and fails in the bundled app, because a
/// process launched from Finder or a .dmg gets a minimal
/// `/usr/bin:/bin:/usr/sbin:/sbin` PATH containing no package manager's bin
/// directory. The failure surfaces as a bare ENOENT that reads like uv isn't
/// installed at all, when it's on PATH in every terminal the user has ever
/// opened — so resolve it explicitly instead of trusting inheritance.
fn resolve_uv() -> Option<&'static PathBuf> {
    static UV: std::sync::OnceLock<Option<PathBuf>> = std::sync::OnceLock::new();
    UV.get_or_init(|| {
        // An explicit override wins outright — the escape hatch for anyone
        // whose install defeats every heuristic below.
        if let Some(explicit) = std::env::var_os("DAIMON_UV_PATH").map(PathBuf::from) {
            if explicit.is_file() {
                return Some(explicit);
            }
            log::warn!("DAIMON_UV_PATH is set to {} but no file is there", explicit.display());
        }
        if let Some(found) = uv_on_path() {
            return Some(found);
        }
        let home = home_dir();
        for candidate in UV_WELL_KNOWN_PATHS {
            match expand_home(candidate, home.as_ref()) {
                Some(expanded) if expanded.is_file() => return Some(expanded),
                _ => continue,
            }
        }
        uv_from_login_shell()
    })
    .as_ref()
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
            orphans_reconciled: std::sync::atomic::AtomicBool::new(false),
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
        let run_dir = app
            .path()
            .app_data_dir()
            .map(|d| d.join("run"))
            .map_err(|e| format!("no app data dir: {e}"))?;
        // Sweep a server left behind by an abruptly-killed previous run (the
        // pid file is kept after a successful spawn for exactly this) —
        // **once per process, before the first spawn**, never on later calls.
        //
        // The pid file is a cross-launch handoff: it names a server this
        // process did not start. From the moment `spawn` writes it, though, it
        // names *our own* live child, and a sweep on the next call would read
        // it back, see a `uv` process, and kill the server the app is actively
        // using. With the frontend polling /status every 1.5s that is not a
        // rare race — it is a permanent kill-and-respawn loop, and any turn
        // streaming at the time dies with "error decoding response body".
        if !self
            .orphans_reconciled
            .swap(true, std::sync::atomic::Ordering::SeqCst)
        {
            self.reconcile_orphans(&run_dir);
        }
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

        // Checked before the spawn purely for the error message: a missing cwd
        // fails with the same bare ENOENT a missing binary does, so without
        // this the user gets "is uv on PATH?" for a problem that has nothing
        // to do with uv.
        if !self.agents_dir.is_dir() {
            return Err(format!(
                "the agent directory {} does not exist — set DAIMON_AGENT_DIR to the repo's agents/ folder",
                self.agents_dir.display()
            ));
        }
        let uv = resolve_uv().ok_or_else(|| {
            format!(
                "could not find the `uv` binary. Looked on PATH, then in {}.                  Set DAIMON_UV_PATH to its full path if it lives somewhere else.",
                UV_WELL_KNOWN_PATHS.join(", ")
            )
        })?;

        let port = allocate_port()?;
        let mut command = tokio::process::Command::new(uv);
        // uv manages its own Python toolchains, but anything it shells out to
        // resolves through PATH — so put its own directory on the front rather
        // than leaving the child with the same threadbare GUI PATH that made
        // uv itself unfindable.
        if let Some(uv_dir) = uv.parent() {
            let existing = std::env::var_os("PATH").unwrap_or_default();
            let combined = std::iter::once(uv_dir.to_path_buf())
                .chain(std::env::split_paths(&existing))
                .collect::<Vec<_>>();
            if let Ok(joined) = std::env::join_paths(combined) {
                command.env("PATH", joined);
            }
        }
        let child = command
            .args(["run", "daimon-agent"])
            .current_dir(&self.agents_dir)
            .env("PORT", port.to_string())
            .process_group(0) // group leader: kill(-pid) takes the tree down
            .stdout(std::process::Stdio::from(log_out.try_clone().map_err(|e| e.to_string())?))
            .stderr(std::process::Stdio::from(log_err.try_clone().map_err(|e| e.to_string())?))
            .spawn()
            .map_err(|e| format!("failed to spawn the agent server via {}: {e}", uv.display()))?;
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
    fn reconcile_orphans(&self, run_dir: &std::path::Path) {
        let pid_path = run_dir.join("daimon-agent.pid");
        let Ok(pid_text) = std::fs::read_to_string(&pid_path) else { return };
        let _ = std::fs::remove_file(&pid_path);
        let Ok(pid) = pid_text.trim().parse::<u32>() else { return };
        // Belt and braces alongside the once-per-process guard in `ensure`:
        // whatever else happens, never kill the child we are currently
        // supervising.
        if let Some(ChildState::Managed { pid: ours, .. }) = &*self.inner.lock().unwrap() {
            if *ours == pid {
                return;
            }
        }
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
pub async fn update_config(port: u16, api_key: &str) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("http://127.0.0.1:{port}/config"))
        .json(&serde_json::json!({ "api_key": api_key }))
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

#[cfg(test)]
mod tests {
    use super::{expand_home, find_uv_in, UV_WELL_KNOWN_PATHS};
    use std::path::PathBuf;

    /// The lookup must ignore directories that merely *could* hold uv and
    /// return the first one that really does — the bug this replaced was a
    /// bare `Command::new("uv")` trusting a PATH that had none of them.
    #[test]
    fn find_uv_in_picks_the_first_directory_that_has_it() {
        let dir = std::env::temp_dir().join("daimon-uv-lookup-test");
        let _ = std::fs::remove_dir_all(&dir);
        let empty = dir.join("empty");
        let real = dir.join("real");
        std::fs::create_dir_all(&empty).unwrap();
        std::fs::create_dir_all(&real).unwrap();
        std::fs::write(real.join("uv"), b"#!/bin/sh\n").unwrap();

        let found = find_uv_in([empty.clone(), real.clone(), dir.clone()].into_iter());
        assert_eq!(found, Some(real.join("uv")));

        // Nothing anywhere -> None, rather than a path that doesn't exist.
        assert_eq!(find_uv_in([empty, dir.clone()].into_iter()), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn expand_home_resolves_tilde_and_passes_absolute_paths_through() {
        let home = PathBuf::from("/Users/someone");
        assert_eq!(
            expand_home("~/.local/bin/uv", Some(&home)),
            Some(PathBuf::from("/Users/someone/.local/bin/uv"))
        );
        assert_eq!(
            expand_home("/opt/homebrew/bin/uv", Some(&home)),
            Some(PathBuf::from("/opt/homebrew/bin/uv"))
        );
        // A `~/` candidate with no HOME is skipped, not joined onto nothing.
        assert_eq!(expand_home("~/.local/bin/uv", None), None);
        assert_eq!(
            expand_home("/usr/local/bin/uv", None),
            Some(PathBuf::from("/usr/local/bin/uv"))
        );
    }

    /// uv's own installer puts it in ~/.local/bin, which is exactly the
    /// directory a Finder-launched app's PATH lacks — so it must stay first.
    #[test]
    fn uv_installer_default_is_tried_first() {
        assert_eq!(UV_WELL_KNOWN_PATHS.first(), Some(&"~/.local/bin/uv"));
    }
}
