//! Phase 11: embedded terminals running the user's own login shell directly
//! on the host machine — outside the per-session background workspace every
//! other execution surface in this app runs inside. See `PROMPT.md`'s "## 12. PHASE 11" for the
//! full rationale; the short version is that this exists purely so the user
//! can drive their *actual* Claude Code CLI against their *actual* project
//! files exactly as if they'd opened Terminal.app, which is fundamentally
//! incompatible with the sandboxed-workspace model the rest of
//! `ARCHITECTURE.md` §5 describes for browsing/form-filling. That document's
//! non-disruption invariants (never move the user's cursor, never steal
//! keyboard focus, never foreground another app) still hold here — this
//! module never does any of that; it only shuttles bytes between a PTY and
//! Daimon's own webview, same as `voice.rs` only ever hands back plain event
//! payloads rather than touching the OS input queue.
//!
//! Deliberately spawns a plain shell, not the `claude` binary itself, as the
//! PTY's direct child — see `PROMPT.md` for why (a shell also lets the user
//! `cd` around and run other commands, and `claude`'s own REPL has no `cd`
//! support of its own since it isn't a shell).
//!
//! **Multiple concurrent terminals**, each identified by a caller-supplied
//! `id` (the frontend mints one per open terminal tab, mirroring how session
//! ids already work for chat) — originally a single global instance, widened
//! once the user asked for multiple terminal tabs. State is a
//! `HashMap<String, ActiveTerminal>` behind one mutex rather than a
//! per-terminal lock: with realistically only a handful of tabs ever open at
//! once, the simplicity of one lock outweighs any contention concern, same
//! tradeoff `workspace.rs`'s `SESSION_PORTS` cache already makes.
//!
//! The PTY's reader is blocking I/O (`portable_pty::MasterPty::try_clone_reader`
//! returns a plain `std::io::Read`, not an async stream), so it's read from a
//! dedicated OS thread rather than a tokio task — the same reasoning already
//! used for `cpal`'s mic-capture thread in `voice.rs`. A second dedicated
//! thread per terminal blocks on `Child::wait()` to detect the shell exiting
//! (e.g. the user typing `exit`) and remove that id's entry so a later
//! `start_terminal(id)` for the same id can spawn a fresh one.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::{Mutex as StdMutex, OnceLock};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use portable_pty::{native_pty_system, ChildKiller, CommandBuilder, MasterPty, PtySize};
use tauri::Emitter;

/// Everything needed to talk to one live terminal: a writer for
/// `write_to_terminal`, the master end for `resize_terminal`, and a
/// `ChildKiller` — a small, independently cloneable handle for terminating
/// the child process (used by `close_terminal` and the app-exit sweep in
/// `lib.rs`) — kept separately from the `Child` itself, which is moved
/// wholesale into that terminal's own wait-thread so that thread can call the
/// simple blocking `Child::wait()` rather than polling `try_wait()` under a
/// shared lock.
struct ActiveTerminal {
    writer: Box<dyn Write + Send>,
    master: Box<dyn MasterPty + Send>,
    killer: Box<dyn ChildKiller + Send + Sync>,
}

// `ActiveTerminal`'s fields are all individually `Send` (`MasterPty` and
// `ChildKiller` both require `Send` in their own trait bounds; a boxed
// `dyn Write + Send` is `Send` by construction), so the compiler derives
// `Send` for the whole struct — no unsafe impl needed, unlike some
// hand-rolled FFI wrappers elsewhere in this crate.
static ACTIVE_TERMINALS: OnceLock<StdMutex<HashMap<String, ActiveTerminal>>> = OnceLock::new();

fn active_terminals() -> &'static StdMutex<HashMap<String, ActiveTerminal>> {
    ACTIVE_TERMINALS.get_or_init(|| StdMutex::new(HashMap::new()))
}

/// `$SHELL` is how every other terminal emulator on the user's machine picks
/// the shell too — falls back to a sane per-platform default only if it's
/// somehow unset (rare, but not impossible in e.g. a minimal launchd
/// environment). This whole feature is macOS-only in practice (Daimon itself
/// only ships there today), but the fallback still branches on target OS
/// rather than hardcoding `/bin/zsh` everywhere, so it doesn't quietly do the
/// wrong thing if this ever runs on Linux.
fn shell_path() -> String {
    std::env::var("SHELL").unwrap_or_else(|_| {
        if cfg!(target_os = "macos") {
            "/bin/zsh".to_string()
        } else {
            "/bin/bash".to_string()
        }
    })
}

/// The directory a freshly opened terminal starts in — same as opening a new
/// Terminal.app window. Falls back to `/` in the pathological case `HOME`
/// isn't set at all, rather than failing `start_terminal` outright over a
/// missing cwd.
fn home_dir() -> String {
    std::env::var("HOME").unwrap_or_else(|_| "/".to_string())
}

/// Idempotent per `id` — if a terminal already exists for this id, this is a
/// no-op (checked under the same lock the new terminal is registered under,
/// so two near-simultaneous calls for the same id can't both win and leak a
/// PTY). Otherwise spawns the user's login shell into a fresh PTY, wires up
/// the output-streaming and exit-watching threads (both tagged with `id` so
/// events route to the right xterm instance on the frontend), and returns as
/// soon as those are launched — it does not wait for anything the shell
/// itself does.
#[tauri::command]
pub async fn start_terminal<R: tauri::Runtime>(app: tauri::AppHandle<R>, id: String) -> Result<(), String> {
    {
        let guard = active_terminals().lock().expect("terminal state mutex poisoned");
        if guard.contains_key(&id) {
            return Ok(());
        }
    }

    let pty_system = native_pty_system();
    // 80x24 is just a safe placeholder — the frontend calls `resize_terminal`
    // immediately after mount via xterm's fit-addon, so this initial size is
    // never actually shown to the user for more than a frame.
    let pair = pty_system
        .openpty(PtySize {
            rows: 24,
            cols: 80,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| format!("failed to open a pty: {e}"))?;

    let shell = shell_path();
    let mut cmd = CommandBuilder::new(&shell);
    cmd.cwd(home_dir());
    // `-l` (login shell): without it, zsh/bash only read `.zshrc`, not
    // `.zprofile`/`.zlogin` — and on macOS it's extremely common for PATH
    // additions (Homebrew's shellenv line, nvm, etc.) to live in the
    // login-only files specifically, since that's what Terminal.app itself
    // always spawns. `portable_pty::CommandBuilder`'s base environment is
    // just whatever Daimon's own process inherited (see `get_base_env` in
    // the `portable-pty` crate) — for a double-click-launched app bundle
    // that's launchd's bare environment, not a real login shell's, so
    // without this flag a tool installed via one of those files (including,
    // plausibly, `claude` itself or a skill/plugin it shells out to) would
    // silently be missing from `PATH` inside this terminal even though it
    // works fine in every other terminal the user has.
    cmd.arg("-l");
    // TUI apps (Claude Code's own interactive UI included) rely on `TERM`
    // correctly advertising terminal capabilities. Daimon's inherited base
    // environment (see above) can easily have no `TERM` at all — GUI apps
    // launched via Launch Services have no controlling terminal and never
    // get one — and an unset/`dumb` `TERM` makes libraries like Node's
    // `readline`/Ink fall back to a degraded, non-interactive rendering
    // mode. Set unconditionally rather than only-if-unset, since
    // `xterm-256color` is genuinely and always correct here — it's what
    // xterm.js (the emulator on the other end of this pty) actually is.
    cmd.env("TERM", "xterm-256color");
    cmd.env("COLORTERM", "truecolor");
    // Same reasoning as `TERM`, but only as a fallback: a missing UTF-8
    // locale makes Unicode box-drawing/icon characters used in TUI menus
    // render as garbled bytes or get stripped outright — but unlike `TERM`,
    // this one *can* legitimately already be set to something the user
    // actually wants (a non-English locale, a different UTF-8 variant), so
    // it's only filled in when completely absent, not overridden.
    if std::env::var_os("LANG").is_none() {
        cmd.env("LANG", "en_US.UTF-8");
    }

    let mut child = pair
        .slave
        .spawn_command(cmd)
        .map_err(|e| format!("failed to spawn shell {shell:?}: {e}"))?;

    // The slave side of a PTY stays "open" (and the master's reader never
    // sees EOF) for as long as *any* handle to it is alive, including this
    // process's own `pair.slave` — not just the child's copy. Without
    // dropping it here, the reader thread below would block forever even
    // after the shell process itself has already exited.
    drop(pair.slave);

    let reader = pair
        .master
        .try_clone_reader()
        .map_err(|e| format!("failed to clone the pty reader: {e}"))?;
    let writer = pair
        .master
        .take_writer()
        .map_err(|e| format!("failed to take the pty writer: {e}"))?;
    // Cloned before `child` is moved into the wait-thread below — this is the
    // one handle `write_to_terminal`/`close_terminal`/app-exit cleanup can
    // use to signal the process independently of whatever thread is blocked
    // in `child.wait()`.
    let killer = child.clone_killer();

    {
        let mut guard = active_terminals().lock().expect("terminal state mutex poisoned");
        if guard.contains_key(&id) {
            // Lost a race against a concurrent `start_terminal(id)` call for
            // the same id, between the fast-path check above and here —
            // extremely unlikely (the frontend only ever calls this once per
            // id, on that terminal tab's mount) but cheap to handle
            // correctly: give up the shell we just spawned rather than
            // silently orphaning it or clobbering the winner's state.
            let _ = child.kill();
            return Ok(());
        }
        guard.insert(
            id.clone(),
            ActiveTerminal {
                writer,
                master: pair.master,
                killer,
            },
        );
    }

    let output_app = app.clone();
    let output_id = id.clone();
    std::thread::spawn(move || {
        let mut reader = reader;
        let mut buf = [0u8; 4096];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break, // EOF — the shell (or its own children) closed the pty.
                Ok(n) => {
                    let encoded = STANDARD.encode(&buf[..n]);
                    let _ = output_app.emit(
                        "terminal-output",
                        serde_json::json!({ "id": output_id, "data": encoded }),
                    );
                }
                Err(e) => {
                    log::warn!("terminal {output_id}: pty read error, ending output stream: {e}");
                    break;
                }
            }
        }
    });

    let exit_app = app;
    let exit_id = id;
    std::thread::spawn(move || {
        let status = child.wait();
        // `ExitStatus::exit_code()` always returns *some* number (portable-pty
        // defaults it to 1 when the process was actually killed by a signal —
        // see its `From<std::process::ExitStatus>` impl), which isn't a real
        // exit code at all, so `signal()` is checked first and takes
        // precedence: a signal-terminated or otherwise unreadable status is
        // reported as `null`, not a misleading `1`.
        let code = match &status {
            Ok(status) if status.signal().is_none() => Some(status.exit_code() as i32),
            _ => None,
        };
        if let Err(e) = &status {
            log::warn!("terminal {exit_id}: error waiting on shell process: {e}");
        }

        // Clear this id's cache entry first so a subsequent `start_terminal`
        // for it (e.g. the frontend reacting to this very `terminal-exited`
        // event by offering to reopen the tab) can spawn fresh rather than
        // seeing a stale "already running" state for a process that's
        // actually gone.
        active_terminals().lock().expect("terminal state mutex poisoned").remove(&exit_id);
        let _ = exit_app.emit(
            "terminal-exited",
            serde_json::json!({ "id": exit_id, "code": code }),
        );
    });

    Ok(())
}

/// Writes `data`'s raw bytes straight to the given terminal's pty master
/// writer — no newline appended, ever. The caller decides what to send,
/// including a dictated result with no trailing `\r`/`\n` so it lands in the
/// shell's input line without auto-executing, matching the same "review
/// before it's acted on" principle the chat input already follows for voice
/// dictation.
#[tauri::command]
pub async fn write_to_terminal(id: String, data: String) -> Result<(), String> {
    let mut guard = active_terminals().lock().expect("terminal state mutex poisoned");
    match guard.get_mut(&id) {
        Some(active) => active
            .writer
            .write_all(data.as_bytes())
            .map_err(|e| format!("failed to write to terminal: {e}")),
        None => Err("no active terminal — start one first".to_string()),
    }
}

/// Resizes the given terminal's live pty grid to match its panel's current
/// fit-addon measurement. Silently a no-op (not an error) if no terminal is
/// running yet for this id — a `ResizeObserver` on the frontend can
/// plausibly fire before that tab's `start_terminal`'s spawn has completed,
/// and that ordinary startup race shouldn't surface as a visible error to
/// the user.
#[tauri::command]
pub async fn resize_terminal(id: String, cols: u16, rows: u16) -> Result<(), String> {
    let guard = active_terminals().lock().expect("terminal state mutex poisoned");
    if let Some(active) = guard.get(&id) {
        active
            .master
            .resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| format!("failed to resize terminal: {e}"))?;
    }
    Ok(())
}

/// Explicitly ends one terminal tab's shell process — needed now that
/// terminals are multiple/independent rather than a single app-lifetime
/// singleton, so closing a tab has somewhere to go besides "wait for the
/// whole app to quit". A no-op if the id is already gone (closed twice, or
/// the shell already exited on its own and the frontend hadn't caught up
/// yet) rather than an error — closing something that's already closed
/// isn't a real failure from the caller's point of view.
#[tauri::command]
pub async fn close_terminal(id: String) -> Result<(), String> {
    let active = active_terminals().lock().expect("terminal state mutex poisoned").remove(&id);
    if let Some(mut active) = active {
        if let Err(e) = active.killer.kill() {
            log::warn!("terminal {id}: failed to kill shell process on close: {e}");
        }
    }
    Ok(())
}

/// A lightweight, side-effect-free check for whether the `claude` CLI
/// resolves on `PATH` — mirrors `get_accessibility_trust_status`'s spirit
/// (a plain status probe, no prompts, safe to call as often as Settings/the
/// terminal tab likes). Spawning `claude --version` is the simplest thing
/// that's actually correct here: it's fast, already installed if it's going
/// to be, and sidesteps re-implementing `PATH` resolution by hand.
///
/// `tokio`'s Command rather than `std`'s: this is awaited from async command
/// handlers, and the blocking version parked a runtime worker thread for the
/// duration of a process spawn on every Settings mount.
pub(crate) async fn claude_cli_available() -> bool {
    tokio::process::Command::new("claude")
        .arg("--version")
        .output()
        .await
        .map(|output| output.status.success())
        .unwrap_or(false)
}

#[tauri::command]
pub async fn get_claude_cli_status() -> bool {
    claude_cli_available().await
}

/// Best-effort cleanup for the `tauri::RunEvent::Exit` handler in `lib.rs` —
/// the same spot that sweeps still-open session containers. Purely local and
/// synchronous (no Docker, no async runtime needed): asks the OS to end
/// every still-running terminal's shell process, so quitting Daimon doesn't
/// leave any of them orphaned in the background. A no-op if no terminal was
/// ever started, or all of them already exited on their own.
pub(crate) fn kill_terminal_on_exit() {
    let mut guard = active_terminals().lock().expect("terminal state mutex poisoned");
    for (id, mut active) in guard.drain() {
        if let Err(e) = active.killer.kill() {
            log::warn!("terminal {id}: failed to kill shell process on app exit: {e}");
        }
    }
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

    /// Same ACL-reachability pattern as `window_focus.rs`'s
    /// `activate_and_focus_window_clears_the_acl` — this deliberately does
    /// *not* assert `Ok`. `start_terminal` genuinely spawns a real PTY and a
    /// real shell process even under `tauri::test`'s mock runtime (nothing
    /// about `portable_pty`/`std::process` is faked by that harness), which
    /// is a real side effect a `#[test]` shouldn't leave running — so this
    /// only confirms the command clears the ACL (a missing
    /// `"allow-start-terminal"` entry surfaces as a distinct "not allowed"
    /// error, checked for explicitly) and doesn't panic, then immediately
    /// kills whatever it spawned via `kill_terminal_on_exit` so the test
    /// process doesn't leak a live shell.
    #[test]
    fn start_terminal_clears_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let response = get_ipc_response(
            &webview,
            invoke_request("start_terminal", serde_json::json!({ "id": "test-tab" })),
        );
        if let Err(error) = &response {
            let message = error.to_string();
            assert!(
                !message.to_lowercase().contains("not allowed"),
                "start_terminal should be allowed by the capability, got: {message}"
            );
        }

        // Best-effort teardown regardless of whether `start_terminal` above
        // actually succeeded in spawning something — see this test's doc
        // comment. Safe to call even if nothing was ever started.
        super::kill_terminal_on_exit();
    }

    /// `write_to_terminal` with no terminal running for this id is the one
    /// path this test can safely exercise without ever spawning a real
    /// shell: it should reach its own well-formed "no active terminal"
    /// error, not panic and not get rejected by the ACL first.
    #[test]
    fn write_to_terminal_clears_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        // Guard against test ordering: if some other test in this same
        // process left a terminal running, tear it down first so this test
        // can rely on the "no active terminal" branch specifically.
        super::kill_terminal_on_exit();

        let response = get_ipc_response(
            &webview,
            invoke_request(
                "write_to_terminal",
                serde_json::json!({ "id": "test-tab", "data": "echo hi" }),
            ),
        );
        let message = response.expect_err("write_to_terminal with no active terminal should error");
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "write_to_terminal should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
        assert!(
            message.contains("no active terminal"),
            "expected the no-active-terminal error, got: {message}"
        );
    }

    /// `resize_terminal` with no terminal running for this id is defined to
    /// be a silent `Ok(())`, not an error — this confirms both that behavior
    /// and ACL reachability in one test.
    #[test]
    fn resize_terminal_clears_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        super::kill_terminal_on_exit();

        let response = get_ipc_response(
            &webview,
            invoke_request(
                "resize_terminal",
                serde_json::json!({ "id": "test-tab", "cols": 100, "rows": 30 }),
            ),
        );
        response.expect("resize_terminal with no active terminal should be a silent Ok(())");
    }

    /// `close_terminal` with no terminal running for this id is defined to
    /// be a silent `Ok(())`, not an error — same reasoning as
    /// `resize_terminal` above, and confirms ACL reachability for the new
    /// command added for the multi-tab terminal feature.
    #[test]
    fn close_terminal_clears_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        super::kill_terminal_on_exit();

        let response = get_ipc_response(
            &webview,
            invoke_request("close_terminal", serde_json::json!({ "id": "test-tab" })),
        );
        response.expect("close_terminal with no active terminal should be a silent Ok(())");
    }

    /// Unlike the other commands here, `claude` is genuinely on `PATH` in
    /// this dev/CI environment, so this asserts the real boolean rather than
    /// just ACL reachability — but doesn't hardcode which value that is, so
    /// this doesn't start failing the moment it runs somewhere without the
    /// CLI installed.
    #[test]
    fn get_claude_cli_status_clears_the_acl_and_returns_a_bool() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let response = get_ipc_response(&webview, invoke_request("get_claude_cli_status", serde_json::json!({})))
            .expect("get_claude_cli_status should be allowed by the capability and never error");
        let found: bool = response.deserialize().expect("expected a plain bool");
        log::info!("get_claude_cli_status_clears_the_acl_and_returns_a_bool: claude on PATH = {found}");
    }
}
