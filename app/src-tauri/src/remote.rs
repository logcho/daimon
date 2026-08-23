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

/// Where the gateway keeps its paired-device file. Mirrors
/// `remote.gateway.default_state_dir()` — the two have to agree, and the app
/// cannot ask the gateway for it without already being paired.
fn state_dir() -> Option<PathBuf> {
    Some(dirs_home()?.join(".local/share/daimon/remote"))
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

/// One device that has been paired with this machine.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PairedDevice {
    pub id: String,
    pub label: String,
    /// Unix seconds. Zero when the file did not say.
    pub created_at: f64,
    pub last_seen_at: Option<f64>,
}

/// The mount point the gateway is served at. Named explicitly so the teardown
/// can name it too — see `tailscale_unserve`.
const SERVE_PATH: &str = "/";

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
    /// Why the gateway stopped, when it stopped without being asked to.
    /// Cleared by the next start. Kept because the panel is the only sign the
    /// gateway is on, so it has to be the thing that says it isn't any more.
    died: Mutex<Option<String>>,
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

    /// The gateway as it actually is, not as it was asked to be.
    ///
    /// `pairing_open` comes from the gateway's own `/health`, because the code
    /// is scraped out of a log that is only truncated at launch — so once
    /// printed it stays readable for the process's whole life, long past its
    /// ten-minute TTL and past being redeemed. Only the gateway knows whether
    /// the window is still open, and it says so on a route the app is already
    /// probing.
    pub fn status(&self, app: &AppHandle, pairing_open: Option<bool>) -> RemoteStatus {
        let mut guard = self.inner.lock().expect("remote state mutex poisoned");

        // Liveness, not merely presence. This used to report `running` from
        // `Option::is_some`, so a gateway that crashed, was OOM-killed or was
        // `pkill`ed left the panel saying "on" forever — with a tailnet URL
        // that went nowhere, and no way to start it again, because `start` is
        // a no-op while we think one is running.
        let exited = match guard.as_mut() {
            Some(running) => match running.child.try_wait() {
                Ok(Some(status)) => Some(format!("daimon-remote exited ({status})")),
                Ok(None) => None,
                Err(e) => Some(format!("lost track of daimon-remote: {e}")),
            },
            None => None,
        };
        if let Some(reason) = exited {
            guard.take();
            drop(guard);
            let detail = Self::log_path(app)
                .ok()
                .and_then(|p| last_error_line(&p))
                .unwrap_or(reason);
            *self.died.lock().expect("remote death mutex poisoned") = Some(detail.clone());
            // Nothing is behind the proxy any more; leaving it configured is
            // what makes `serve status` keep handing out a dead URL.
            tailscale_unserve(GATEWAY_PORT);
            return RemoteStatus {
                port: GATEWAY_PORT,
                error: Some(detail),
                ..Default::default()
            };
        }

        let Some(running) = guard.as_ref() else {
            return RemoteStatus {
                port: GATEWAY_PORT,
                error: self.died.lock().expect("remote death mutex poisoned").clone(),
                ..Default::default()
            };
        };
        let log = Self::log_path(app).ok().and_then(|p| read_tail(&p)).unwrap_or_default();
        RemoteStatus {
            running: true,
            port: GATEWAY_PORT,
            // From `serve status`, not the gateway's banner: the banner prints
            // a tailnet URL whenever Tailscale is installed, serving or not.
            url: tailscale_serve_url(GATEWAY_PORT),
            // Only while the gateway says a window is genuinely open. The old
            // `.filter(|_| running.pid > 0)` was dead — `child.id()` is never
            // zero — so a redeemed, expired code sat on screen indefinitely,
            // and because a code was always present the "pair another device"
            // button could never render.
            pairing_code: match pairing_open {
                Some(true) => scrape(&log, "pairing code:  "),
                Some(false) => None,
                // The probe failed, so we do not know. Showing a code we
                // cannot vouch for is the failure we are fixing.
                None => None,
            },
            // What the child reported it started with, rather than what we
            // asked it for. The banner prints this warning only when the flag
            // is actually set, so it is a readback of the running config.
            terminals: log.contains("terminals are exposed") || running.terminals,
            error: None,
        }
    }

    /// Open a fresh pairing window on the *running* gateway.
    ///
    /// A signal rather than a restart: re-pairing used to stop and respawn the
    /// process, dropping every connected phone's socket and tearing down the
    /// tailnet mapping in order to mint eight characters the gateway makes in
    /// memory. Only the parent can signal it, which is exactly who is asking.
    pub async fn request_pairing(&self, app: &AppHandle) -> Result<(), String> {
        // Scoped so the guard is gone before the first await: a MutexGuard held
        // across one makes the whole future non-Send, and Tauri needs to move
        // it between threads.
        let pid = {
            let guard = self.inner.lock().expect("remote state mutex poisoned");
            let Some(running) = guard.as_ref() else {
                return Err("the gateway is not running".to_string());
            };
            running.pid
        };
        let before = Self::log_path(app).ok().and_then(|p| read_tail(&p));
        let before_code = before.as_deref().and_then(|l| scrape(l, "pairing code:  "));
        // The process, not the group: the cloudflared child has no idea what
        // this means and SIGUSR1 would simply kill it.
        if unsafe { libc::kill(pid as i32, libc::SIGUSR1) } != 0 {
            return Err("could not reach the gateway".to_string());
        }

        // The handler prints on the event loop, so the line lands a moment
        // later. Waiting for it here means the panel can show the code as soon
        // as the call returns rather than on whichever poll happens to catch it.
        for _ in 0..40 {
            // tokio's, not the thread's: this runs on the async runtime, and
            // blocking it for two seconds stalls every other command with it.
            tokio::time::sleep(Duration::from_millis(50)).await;
            let now = Self::log_path(app).ok().and_then(|p| read_tail(&p));
            let code = now.as_deref().and_then(|l| scrape(l, "pairing code:  "));
            if code.is_some() && code != before_code {
                return Ok(());
            }
        }
        Err("the gateway did not open a pairing window".to_string())
    }

    /// Start the gateway. Idempotent — a second call while it is up is a no-op
    /// rather than a second process fighting for the port.
    pub async fn start(&self, app: &AppHandle, terminals: bool, pair: bool) -> Result<RemoteStatus, String> {
        if self.inner.lock().expect("remote state mutex poisoned").is_some() {
            return Ok(self.status(app, health().await.pairing));
        }
        // A previous run's dying words are not this run's news.
        *self.died.lock().expect("remote death mutex poisoned") = None;

        // Nothing may already be on the port. A gateway we did not start
        // answers the health probe below just as happily as our own child
        // would, so spawning into an occupied port produces the worst possible
        // outcome: the child prints a banner and a pairing code, dies of
        // "address already in use", and this returns success — reporting the
        // flags that were *asked* for while the stranger, started with
        // whatever flags it likes, is the one actually serving. Shells being
        // switched on here and refused on the phone is exactly that.
        if health().await.ok {
            return Err(format!(
                "port {GATEWAY_PORT} is already serving a gateway this app did not start. \
                 Quit it first: pkill -f daimon-remote"
            ));
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
            let probe = health().await;
            if probe.ok {
                tailscale_serve(GATEWAY_PORT);
                return Ok(self.status(app, probe.pairing));
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
    //
    // `--set-path=/` is what makes the teardown targetable: without naming a
    // mount point going up, there is no mount point to name coming down, and
    // the only available "off" is the one that removes everything on :443.
    let _ = std::process::Command::new(&binary)
        .args(["serve", "--bg", "--set-path", SERVE_PATH, &port.to_string()])
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
    // Only tear down a config that is ours, and only the part of it that is.
    //
    // This used to run `serve --https=443 off`, which removes the *entire*
    // 443 proxy configuration — guarded only by "does 127.0.0.1:<port> appear
    // anywhere in `serve status`". Anyone serving something else on 443 lost
    // it the first time they switched remote access off. Naming the mount
    // point takes down exactly what `tailscale_serve` put up.
    if tailscale_serve_url(port).is_none() {
        return;
    }
    let _ = std::process::Command::new(&binary)
        .args(["serve", "--https=443", "--set-path", SERVE_PATH, "off"])
        .output();
}

/// What the gateway says about itself on its one public route.
#[derive(Default)]
struct Health {
    /// Something answered, and it is a daimon gateway.
    ok: bool,
    /// Whether a pairing window is open right now. `None` when we could not
    /// ask — which is not the same as "no", and must not be shown as a code.
    pairing: Option<bool>,
}

async fn health() -> Health {
    let Ok(response) = reqwest::Client::new()
        .get(format!("http://127.0.0.1:{GATEWAY_PORT}/health"))
        .timeout(Duration::from_millis(500))
        .send()
        .await
    else {
        return Health::default();
    };
    if !response.status().is_success() {
        return Health::default();
    }
    let body: serde_json::Value = response.json().await.unwrap_or(serde_json::Value::Null);
    Health {
        ok: true,
        pairing: body.get("pairing").and_then(|v| v.as_bool()),
    }
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
    // Ask the gateway before reading our own bookkeeping: whether a pairing
    // window is still open is the gateway's to know, and the log cannot say.
    let pairing = health().await.pairing;
    let state = app.state::<RemoteManager>();
    Ok(state.status(&app, pairing))
}

/// Open a fresh pairing window without restarting anything.
/// The devices paired with this machine.
///
/// Read straight off the token store rather than through the gateway's
/// `/devices` route, because reaching that route means holding a credential
/// and this app has never had one. The file holds only SHA-256 hashes, so
/// there is nothing here a reader could authenticate with — and listing it
/// makes no one a second writer of it.
#[tauri::command]
pub async fn remote_devices() -> Result<Vec<PairedDevice>, String> {
    let Some(path) = state_dir().map(|d| d.join("tokens.json")) else {
        return Ok(Vec::new());
    };
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return Ok(Vec::new()); // nothing paired yet is not an error
    };
    let parsed: serde_json::Value =
        serde_json::from_str(&raw).map_err(|e| format!("cannot read paired devices: {e}"))?;
    let rows = parsed.get("devices").and_then(|d| d.as_array()).cloned().unwrap_or_default();
    Ok(rows
        .iter()
        .filter_map(|row| {
            Some(PairedDevice {
                id: row.get("id")?.as_str()?.to_string(),
                label: row.get("label").and_then(|v| v.as_str()).unwrap_or("device").to_string(),
                created_at: row.get("created_at").and_then(|v| v.as_f64()).unwrap_or(0.0),
                last_seen_at: row.get("last_seen_at").and_then(|v| v.as_f64()),
            })
        })
        .collect())
}

/// Un-pair a device.
///
/// The gateway does the work: it is the only writer of the token store, which
/// is a property worth keeping. This names the device in a drop-file and asks.
/// Doing it any other way would mean either shipping a gateway credential to
/// the machine that already supervises the process, or having two writers of
/// a file whose whole design assumes one.
#[tauri::command]
pub async fn revoke_remote_device(app: AppHandle, id: String) -> Result<Vec<PairedDevice>, String> {
    if id.trim().is_empty() {
        return Err("no device given".to_string());
    }
    let dir = state_dir().ok_or_else(|| "no home directory".to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot reach the gateway's state: {e}"))?;
    std::fs::write(dir.join("revoke"), format!("{id}
"))
        .map_err(|e| format!("cannot ask the gateway to revoke: {e}"))?;

    // Same scoping as `request_pairing`: the guard must not survive into the
    // await below.
    let pid = {
        let state = app.state::<RemoteManager>();
        let guard = state.inner.lock().expect("remote state mutex poisoned");
        let Some(running) = guard.as_ref() else {
            return Err("the gateway is not running — start it to un-pair a device".to_string());
        };
        running.pid
    };
    if unsafe { libc::kill(pid as i32, libc::SIGHUP) } != 0 {
        return Err("could not reach the gateway".to_string());
    }

    // The handler runs on the gateway's event loop, so the file is consumed a
    // moment later. Waiting means the list this returns is the list after.
    for _ in 0..40 {
        tokio::time::sleep(Duration::from_millis(50)).await;
        if !dir.join("revoke").exists() {
            break;
        }
    }
    remote_devices().await
}

#[tauri::command]
pub async fn pair_remote(app: AppHandle) -> Result<RemoteStatus, String> {
    {
        let state = app.state::<RemoteManager>();
        state.request_pairing(&app).await?;
    }
    let pairing = health().await.pairing;
    let state = app.state::<RemoteManager>();
    Ok(state.status(&app, pairing))
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
    // Stopping on purpose is not dying, so it leaves no death note behind to
    // be reported as an error on the next poll.
    *state.died.lock().expect("remote death mutex poisoned") = None;
    Ok(state.status(&app, None))
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
