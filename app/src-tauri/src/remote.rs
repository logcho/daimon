//! Supervision of the remote gateway, so turning it on is a switch rather
//! than a terminal you have to keep open.
//!
//! Deliberately a sibling of `agent.rs` rather than part of it: the agent
//! server is what the app *is*, and dies with it; the gateway is optional,
//! off by default, and exists to expose this machine to a network. Those
//! belong on separate switches.
//!
//! The pairing code is read back out of the gateway's own log rather than
//! passed through a side channel. It is deliberately never written anywhere
//! else — it lives in the gateway's memory for ten minutes — and the log is
//! already how the CLI shows it to a human.

use std::fs::{File, OpenOptions};
use std::io::Read;
use std::path::PathBuf;
use std::os::unix::process::CommandExt;
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Manager};

/// The gateway's fixed port. Fixed on purpose: `tailscale serve` maps one
/// hostname to one port, so a freshly allocated one would mean reconfiguring
/// the tunnel every launch.
const GATEWAY_PORT: u16 = 4712;

const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(300);
const HEALTH_POLL_TRIES: u32 = 40;

#[derive(Clone, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct RemoteStatus {
    pub running: bool,
    pub port: u16,
    /// The tailnet URL to open, when Tailscale is up and serving.
    pub url: Option<String>,
    /// Present only while a pairing window is open.
    pub pairing_code: Option<String>,
    /// Whether paired devices may open shells on this machine.
    pub terminals: bool,
    pub error: Option<String>,
}

struct Running {
    pid: u32,
    child: std::process::Child,
    _log: File,
    terminals: bool,
}

#[derive(Default)]
pub struct RemoteManager {
    inner: Mutex<Option<Running>>,
}

impl RemoteManager {
    pub fn new() -> Self {
        Self::default()
    }

    fn log_path(app: &AppHandle) -> Result<PathBuf, String> {
        let dir = app
            .path()
            .app_data_dir()
            .map_err(|e| format!("no app data dir: {e}"))?
            .join("run");
        std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create run dir: {e}"))?;
        Ok(dir.join("daimon-remote.log"))
    }

    pub fn status(&self, app: &AppHandle) -> RemoteStatus {
        let guard = self.inner.lock().expect("remote state mutex poisoned");
        let Some(running) = guard.as_ref() else {
            return RemoteStatus { port: GATEWAY_PORT, ..Default::default() };
        };
        let log = Self::log_path(app).ok().and_then(|p| read_tail(&p)).unwrap_or_default();
        RemoteStatus {
            running: true,
            port: GATEWAY_PORT,
            // From `serve status`, not the gateway's banner: the banner prints
            // a tailnet URL whenever Tailscale is installed, serving or not.
            url: tailscale_serve_url(GATEWAY_PORT),
            // Only while a window is open: the CLI prints it once, and it is
            // single-use, so a stale one on screen is worse than none.
            pairing_code: scrape(&log, "pairing code:  ").filter(|_| running.pid > 0),
            terminals: running.terminals,
            error: None,
        }
    }

    /// Start the gateway. Idempotent — a second call while it is up is a no-op
    /// rather than a second process fighting for the port.
    pub async fn start(&self, app: &AppHandle, terminals: bool, pair: bool) -> Result<RemoteStatus, String> {
        if self.inner.lock().expect("remote state mutex poisoned").is_some() {
            return Ok(self.status(app));
        }

        let path = Self::log_path(app)?;
        // Truncated per launch: the pairing code is scraped back out of this
        // file, and a previous run's code must never be mistaken for this
        // run's.
        let log = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&path)
            .map_err(|e| format!("cannot open {}: {e}", path.display()))?;

        let binary = crate::paths::remote_bin()
            .ok_or_else(|| "can't find daimon-remote — run agents/scripts/reinstall-cli.sh".to_string())?;

        let mut cmd = std::process::Command::new(binary);
        cmd.arg("--port").arg(GATEWAY_PORT.to_string());
        if terminals {
            cmd.arg("--terminals");
        }
        if pair {
            cmd.arg("--pair");
        }
        let child = cmd
            .env("PATH", crate::paths::augmented_path())
            // Its own group leader, so stop() can take down the tree — the
            // gateway may have started a cloudflared child of its own. Without
            // this the child sits in *our* group and kill(-pid) either misses
            // or, worse, does not.
            .process_group(0)
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log.try_clone().map_err(|e| e.to_string())?)
            .spawn()
            .map_err(|e| format!("could not start daimon-remote: {e}"))?;

        let pid = child.id();
        *self.inner.lock().expect("remote state mutex poisoned") =
            Some(Running { pid, child, _log: log, terminals });

        for _ in 0..HEALTH_POLL_TRIES {
            // Whether our *own* child is alive, checked before the health
            // probe rather than after. A health check on a fixed port cannot
            // tell whose gateway answered it: another one already listening
            // there will reply happily while the process we just spawned is
            // dying of "address already in use", and we would report success
            // and then show a pairing code belonging to a process that no
            // longer exists.
            if let Some(exit) = self.child_exited() {
                let detail = last_error_line(&path).unwrap_or(exit);
                self.stop();
                return Err(detail);
            }
            if health().await {
                tailscale_serve(GATEWAY_PORT);
                return Ok(self.status(app));
            }
            tokio::time::sleep(HEALTH_POLL_INTERVAL).await;
        }
        self.stop();
        Err("daimon-remote did not come up — see daimon-remote.log".to_string())
    }

    /// `Some(reason)` once our child has exited, `None` while it is running.
    fn child_exited(&self) -> Option<String> {
        let mut guard = self.inner.lock().expect("remote state mutex poisoned");
        let running = guard.as_mut()?;
        match running.child.try_wait() {
            Ok(Some(status)) => Some(format!("daimon-remote exited ({status})")),
            Ok(None) => None,
            Err(e) => Some(format!("lost track of daimon-remote: {e}")),
        }
    }

    pub fn stop(&self) {
        let mut guard = self.inner.lock().expect("remote state mutex poisoned");
        if guard.is_some() {
            tailscale_unserve(GATEWAY_PORT);
        }
        if let Some(running) = guard.take() {
            // The group, not the process: the gateway may have started a
            // cloudflared child. SIGTERM rather than SIGKILL so it gets to
            // close its sockets and tear down a tunnel it opened.
            unsafe { libc::kill(-(running.pid as i32), libc::SIGTERM) };
        }
    }
}

/// The Tailscale CLI, wherever it landed.
fn tailscale_bin() -> Option<PathBuf> {
    [
        PathBuf::from("/usr/local/bin/tailscale"),
        PathBuf::from("/opt/homebrew/bin/tailscale"),
        PathBuf::from("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
    ]
    .into_iter()
    .find(|p| p.is_file())
}

/// Put the gateway on the tailnet, and return the URL that actually routes.
///
/// This is the step that used to be a second command typed by hand, and
/// leaving it out was worse than it sounds: the gateway prints a tailnet URL
/// in its banner whenever Tailscale is *installed*, whether or not anything is
/// serving, so the app would show a link that quietly went nowhere. The URL
/// reported here comes from `serve status` — if it is absent, there is nothing
/// to open, and saying so beats a dead link.
fn tailscale_serve(port: u16) -> Option<String> {
    let binary = tailscale_bin()?;
    // Best-effort: a machine that is not signed in, or where the user is not
    // the operator, simply does not get a URL. The gateway still runs and is
    // still reachable on the LAN or over a tunnel.
    let _ = std::process::Command::new(&binary)
        .args(["serve", "--bg", &port.to_string()])
        .output();
    tailscale_serve_url(port)
}

fn tailscale_serve_url(port: u16) -> Option<String> {
    let binary = tailscale_bin()?;
    let out = std::process::Command::new(&binary).args(["serve", "status"]).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    // Only claim a URL if this port is the thing being proxied — a serve
    // config left over for something else is not ours to advertise.
    if !text.contains(&format!("127.0.0.1:{port}")) {
        return None;
    }
    text.lines()
        .find(|l| l.starts_with("https://"))
        .map(|l| l.split_whitespace().next().unwrap_or(l).to_string())
}

fn tailscale_unserve(port: u16) {
    let Some(binary) = tailscale_bin() else { return };
    // Only tear down a config that is ours. `--https=443 off` is the form
    // Tailscale itself prints when the proxy starts.
    if tailscale_serve_url(port).is_none() {
        return;
    }
    let _ = std::process::Command::new(&binary)
        .args(["serve", "--https=443", "off"])
        .output();
}

async fn health() -> bool {
    reqwest::Client::new()
        .get(format!("http://127.0.0.1:{GATEWAY_PORT}/health"))
        .timeout(Duration::from_millis(500))
        .send()
        .await
        .map(|r| r.status().is_success())
        .unwrap_or(false)
}

/// Pull a value the CLI printed for a human out of its log.
fn scrape(log: &str, marker: &str) -> Option<String> {
    log.rsplit_once(marker)
        .map(|(_, rest)| rest.split_whitespace().next().unwrap_or("").to_string())
        .filter(|found| !found.is_empty())
}

/// The most useful line of a Python traceback is its last one. Surfacing it
/// beats "it didn't come up — check the log", which is a instruction to go and
/// do the work by hand.
fn last_error_line(path: &std::path::Path) -> Option<String> {
    let log = read_tail(path)?;
    let last = log.lines().rev().find(|l| !l.trim().is_empty())?;
    if last.contains("address already in use") {
        return Some(format!(
            "port {GATEWAY_PORT} is already in use — something else is serving on it, \
             possibly a daimon-remote started from a terminal"
        ));
    }
    Some(last.trim().to_string())
}

fn read_tail(path: &std::path::Path) -> Option<String> {
    let mut buf = String::new();
    File::open(path).ok()?.read_to_string(&mut buf).ok()?;
    Some(buf)
}

#[tauri::command]
pub async fn remote_status(app: AppHandle) -> Result<RemoteStatus, String> {
    let state = app.state::<RemoteManager>();
    Ok(state.status(&app))
}

#[tauri::command]
pub async fn start_remote(app: AppHandle, terminals: bool, pair: bool) -> Result<RemoteStatus, String> {
    let state = app.state::<RemoteManager>();
    state.start(&app, terminals, pair).await
}

#[tauri::command]
pub async fn stop_remote(app: AppHandle) -> Result<RemoteStatus, String> {
    let state = app.state::<RemoteManager>();
    state.stop();
    Ok(state.status(&app))
}

#[cfg(test)]
mod tests {
    /// The pairing code and URL are read back out of the gateway's own log
    /// rather than passed through a side channel: the code is deliberately
    /// never written anywhere else, and the log is already how the CLI shows
    /// it to a person.
    #[test]
    fn scrape_reads_what_the_cli_printed() {
        let log = "\n  daimon remote — gateway on http://127.0.0.1:4712\n\n  \
                   then open:  https://mac.tailnet.ts.net/\n\n  \
                   pairing code:  QMKS57VM     (valid 10 minutes, one use)\n";
        assert_eq!(
            super::scrape(log, "then open:  ").as_deref(),
            Some("https://mac.tailnet.ts.net/")
        );
        assert_eq!(super::scrape(log, "pairing code:  ").as_deref(), Some("QMKS57VM"));
    }

    #[test]
    fn scrape_finds_nothing_in_a_log_without_it() {
        // A run started without --pair prints no code, and a stale one from a
        // previous launch would be worse than none: the code is single-use.
        let log = "  daimon remote — gateway on http://127.0.0.1:4712\n";
        assert!(super::scrape(log, "pairing code:  ").is_none());
    }

    #[test]
    fn a_port_clash_is_explained_rather_than_dumped() {
        let dir = std::env::temp_dir().join(format!("daimon-remote-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("clash.log");
        std::fs::write(
            &path,
            "Traceback (most recent call last):\n  ...\n\
             OSError: [Errno 48] error while attempting to bind on address \
             ('127.0.0.1', 4712): [errno 48] address already in use\n",
        )
        .unwrap();

        let message = super::last_error_line(&path).unwrap();
        assert!(message.contains("already in use"), "{message}");
        // The Python traceback is not the answer; what to do about it is.
        assert!(!message.contains("Traceback"), "{message}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn scrape_takes_the_most_recent() {
        let log = "pairing code:  AAAAAAAA\npairing code:  BBBBBBBB\n";
        assert_eq!(super::scrape(log, "pairing code:  ").as_deref(), Some("BBBBBBBB"));
    }
}
