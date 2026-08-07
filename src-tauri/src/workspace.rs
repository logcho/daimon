//! Owns the lifecycle of every session's background workspace: PinchTab (a
//! real, stealth-patched Chrome, driven over a local HTTP API — see
//! `agents/src/browser.ts`) and the Node agent server, both spawned and
//! supervised as plain native OS processes directly by this daemon.
//!
//! This replaces the old Docker-based design (`docker build`/`run`/`rm` a
//! `daimon-workspace-<session-id>` container) so Daimon can ship as a single
//! downloadable app with no separate Docker Desktop install. See
//! `sandbox/entrypoint.sh` for the config-setup logic ported into
//! `configure_pinchtab` below, and the plan this implements
//! (`can-you-look-into-eager-shell.md`) for the full design rationale.
//!
//! **A real deviation from that plan worth calling out up front**: the
//! design assumed spawning `pinchtab server` as a single process-group
//! leader would be enough for one `killpg` to take down PinchTab, Chrome,
//! and every one of Chrome's own GPU/renderer/utility children in one shot
//! (ordinary POSIX child-inherits-parent's-pgid semantics). Confirmed
//! empirically against a real spawned instance that this is *not* what
//! happens: `pinchtab server` immediately forks a `pinchtab bridge`
//! subprocess which calls `setsid`/`setpgid` to detach itself into a
//! **brand-new** process group/session the instant it launches Chrome —
//! `kill -9` on the top-level `pinchtab server` PID (or a `killpg` on its
//! original group) leaves `bridge` and the entire Chrome tree running,
//! orphaned, and reparented to init. This is, in miniature, exactly the
//! renderer-accumulation bug this whole plan exists to fix. Two mitigations,
//! both used together:
//! 1. `pinchtab server stop` (shelled out, same profile-dir env) is
//!    PinchTab's own graceful teardown and — confirmed empirically — really
//!    does cleanly kill its whole managed tree, bridge and Chrome included.
//!    This is the primary teardown path (`kill_pinchtab_tree`).
//! 2. A `sysinfo`-based recursive parent→child walk from the recorded leader
//!    PID, `SIGKILL`ing every descendant found, as defense-in-depth if (1)
//!    fails, hangs, or the `pinchtab` binary isn't reachable. This is the
//!    same walk the memory watchdog (below) already needs for RSS summation,
//!    just used to kill instead of sum.
//!
//! Everything under `process_wrap::tokio::ProcessGroup`/`KillOnDrop` is kept
//! anyway (it's still a real, useful safety net for the *directly* spawned
//! leader process on an unclean Rust-side exit/panic), it just isn't
//! sufficient on its own the way the plan originally assumed.
//!
//! **Bundling addendum (follow-up pass, same plan's §4/§5)**: this module's
//! binary/resource resolution (`pinchtab_binary`, `resolve_node_agent_launch`,
//! `configure_pinchtab`'s Chrome-binary resolution) was originally left
//! env-var-driven-only, with a bundled-resource-dir production path
//! explicitly deferred. That path is now wired up — see `resource_dir`,
//! `bundled_sidecar_path`, and `bundled_chromium_binary` — using a bundled
//! resource whenever no dev env-var override is set and the resource
//! actually exists, falling back to the pre-existing dev-tree/bare-`$PATH`
//! behavior otherwise. `scripts/fetch-sidecars.sh` (repo root) is what
//! populates `src-tauri/binaries/`/`src-tauri/resources/` for this to find.
//!
//! **Two more real regressions from that migration, fixed in a later pass
//! (same plan, `can-you-look-into-eager-shell.md`'s follow-up)**:
//!
//! 1. *Recordings silently never finished.* PinchTab's WebM/MP4 recording
//!    encode step (`internal/handlers/record_encode.go`) shells out to a
//!    bare `ffmpeg` resolved via `$PATH` — no config override exists. The
//!    old Docker image had `ffmpeg` via `apt-get`; the native bundling
//!    pipeline never fetched it, so a real built `.app` (which doesn't
//!    inherit an interactive shell's Homebrew-augmented `PATH` the way
//!    `cargo tauri dev` from a terminal does — a well-known macOS gotcha)
//!    silently never produced a finished recording. Fixed by bundling a
//!    static ffmpeg (`scripts/fetch-sidecars.sh`) and prepending its
//!    directory onto the spawned PinchTab process's own `PATH` — see
//!    `ffmpeg_binary`/`pinchtab_path_env`, used by `spawn_pinchtab_server`.
//! 2. *The installer bundled a full pinned Chromium (~491MB of a 755MB
//!    total)*, fighting the whole "single lightweight download" reason
//!    Tauri was chosen over Electron. Fixed by moving Chromium to a
//!    download-on-first-use flow — the exact same pattern `voice.rs`
//!    already uses for the whisper model — into the app-data directory
//!    instead of bundling it in `tauri.conf.json`'s `resources`. See
//!    `get_chromium_status`/`download_chromium`/`downloaded_chromium_binary`,
//!    and `ensure_session_workspace`'s pre-flight download-if-needed step.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, OnceLock};
use std::time::Duration;

use futures_util::StreamExt;
use process_wrap::tokio::{ChildWrapper, CommandWrap, KillOnDrop, ProcessGroup};
use sha2::{Digest, Sha256};
use sysinfo::{Pid, ProcessesToUpdate, Signal, System};
use tokio::process::Command;
use tokio::sync::Mutex;

// ---------------------------------------------------------------------
// Path resolution
// ---------------------------------------------------------------------

/// The git-checkout root (`CARGO_MANIFEST_DIR`-based) — a dev-only concept
/// that doesn't exist in a shipped/bundled binary. Still legitimately used
/// for locating *dev* resources: the `DAIMON_NODE_BIN`/`DAIMON_AGENT_ENTRY`
/// dev/smoke-testing override and the `tsx`-run dev source tree fallback in
/// `resolve_node_agent_launch` both still resolve `agents/` this way,
/// deliberately independent of `resource_dir()` below (which points at the
/// *bundled* `resources/agent`, a different, compiled-and-pruned copy — see
/// that function). Every *data* directory (memory, vault fallback,
/// automations, pinchtab profiles, PID files, `.env`) must go through
/// `data_dir()` below instead, never this.
pub(crate) fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent directory")
        .to_path_buf()
}

/// Resolved once, early in `run()` (see `init_data_dir`), from Tauri's own
/// `app_data_dir()` — a real, writable, per-app OS directory
/// (`~/Library/Application Support/com.daimon.app` on macOS) that exists in
/// a bundled `.app` the way `project_root()` never can. A plain `OnceLock`
/// rather than needing an `AppHandle` threaded through every caller: the
/// plan this implements is explicit that the module's public API (and, by
/// extension, every other module's data-directory helpers built on it)
/// should stay stable — only `ensure_session_workspace` actually needs an
/// `AppHandle` (to hand to the watchdog for error events).
static DATA_DIR: OnceLock<PathBuf> = OnceLock::new();

/// Must be called once, early in `run()` — before `.env` loading or any
/// command that touches a data directory. Tests never call this (matching
/// the existing pattern for the log/global-shortcut plugins, which are also
/// only registered in `run()`, not `build_app`): `data_dir()` transparently
/// falls back to `project_root()` when this hasn't run, so every existing
/// `cargo test` keeps reading/writing the same git-checkout-relative
/// `memory/`/`vault/`/`automations/` directories it always has.
pub(crate) fn init_data_dir<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    use tauri::Manager;
    match app.path().app_data_dir() {
        Ok(dir) => {
            if let Err(e) = std::fs::create_dir_all(&dir) {
                log::error!("failed to create app data directory {}: {e}", dir.display());
                return;
            }
            let _ = DATA_DIR.set(dir);
        }
        Err(e) => log::error!("failed to resolve app data directory: {e} — falling back to the dev checkout path"),
    }
}

/// Where every piece of Daimon's durable local state lives: `memory/`,
/// `vault/` (default fallback), `automations/`+`automations.json`,
/// `recordings/`, `pinchtab-profiles/`, `run/` (session PID files), `.env`,
/// `whisper-models/`. Resolves to the real OS app-data directory in a built
/// app (see `init_data_dir`) and to the git checkout root in `cargo test`.
pub(crate) fn data_dir() -> PathBuf {
    DATA_DIR.get().cloned().unwrap_or_else(project_root)
}

fn pinchtab_profiles_dir() -> PathBuf {
    data_dir().join("pinchtab-profiles")
}

/// Where `write_spreadsheet` (agents/src/tools.ts) saves real document files
/// it generates — same "plain host directory passed as an env var" shape as
/// `vault_dir`/`recordings_dir` below, not a bind mount (native processes
/// have no mount namespace to rely on).
fn documents_dir() -> PathBuf {
    let dir = data_dir().join("documents");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn run_dir() -> PathBuf {
    let dir = data_dir().join("run");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn logs_dir() -> PathBuf {
    let dir = data_dir().join("logs");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn pid_file_path(session_id: &str) -> PathBuf {
    run_dir().join(format!("{session_id}.pid"))
}

/// Resolved once, early in `run()` (see `init_resource_dir`), from Tauri's
/// own `resource_dir()` — the real, read-only directory a bundled `.app`
/// copies `bundle.resources` entries into (`Contents/Resources/` on macOS,
/// via `tauri.conf.json`'s `bundle.resources` map). The mirror of `DATA_DIR`
/// above but for *shipped, read-only* resources rather than mutable app
/// data: the pinned Chromium build (`resources/chromium/<platform>/`) and
/// the compiled+pruned `agents/` production bundle
/// (`resources/agent/dist/server.js` + its own pruned `node_modules/`) both
/// live under here in a real bundle — see `bundled_chromium_binary` and
/// `resolve_node_agent_launch`. Same "plain `OnceLock`, no `AppHandle`
/// threading" reasoning as `DATA_DIR`.
static RESOURCE_DIR: OnceLock<PathBuf> = OnceLock::new();

/// Must be called once, early in `run()`, alongside `init_data_dir` — before
/// anything resolves a bundled binary/resource path. Tests never call this:
/// `resource_dir()` transparently falls back to this crate's own
/// `src-tauri/resources/` (where `scripts/fetch-sidecars.sh` puts its output
/// during local development) when unset, so a `cargo test` run that sets
/// `DAIMON_CHROME_BIN`/`DAIMON_PINCHTAB_BIN` to a real fetched binary still
/// works without ever calling this — see `session_process_snapshot`'s
/// callers for exactly that pattern.
pub(crate) fn init_resource_dir<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    use tauri::Manager;
    match app.path().resource_dir() {
        Ok(dir) => {
            let _ = RESOURCE_DIR.set(dir);
        }
        Err(e) => log::error!("failed to resolve app resource directory: {e} — falling back to the dev checkout path"),
    }
}

fn resource_dir() -> PathBuf {
    RESOURCE_DIR
        .get()
        .cloned()
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("resources"))
}

/// Resolves a bundled sidecar's path in a real `.app` bundle: Tauri's
/// `externalBin` bundling drops the `-<target-triple>` suffix and places the
/// binary directly alongside the main executable (`Contents/MacOS/` on
/// macOS) — the same convention `tauri-plugin-shell`'s own sidecar
/// resolution uses internally (registered in `lib.rs` for that reason, and
/// so `externalBin`'s bundling/ACL story matches Tauri's own conventions),
/// deliberately replicated directly here rather than routing spawning
/// through the plugin's own `Command` type: this module's
/// `process_wrap`-based process-group-leader spawning (see this module's
/// doc comment — needed for PinchTab's own bridge-detach behavior) needs a
/// real `tokio::process::Command` builder, which the shell plugin's own
/// executor doesn't expose. Returns `None` (not an error) whenever this
/// isn't actually running from inside a bundle with that sidecar present —
/// every caller already has an env-var override and/or a dev-tree/bare-PATH
/// fallback for that case.
fn bundled_sidecar_path(name: &str) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let candidate = exe.parent()?.join(name);
    candidate.is_file().then_some(candidate)
}

/// Chrome for Testing's platform segment (`mac-arm64`/`mac-x64`), picked
/// from `std::env::consts::ARCH` — a compile-time constant matching
/// whichever target this binary itself was built for, mirroring
/// `scripts/fetch-sidecars.sh`'s own (now-removed, see `download_chromium`'s
/// doc comment) `PW_PLATFORM` selection — since only per-arch builds are
/// fetched, never a universal2 binary (see the plan's unresolved-unknowns
/// list). `None` for an unrecognized platform.
fn chromium_platform_segment() -> Option<&'static str> {
    match std::env::consts::ARCH {
        "aarch64" => Some("mac-arm64"),
        "x86_64" => Some("mac-x64"),
        _ => None,
    }
}

/// Chrome for Testing's real executable path under a `chromium/<platform>/`
/// root directory — shared by `bundled_chromium_binary` (rooted at
/// `resource_dir()`) and `downloaded_chromium_binary` (rooted at
/// `data_dir()`, see `download_chromium`) so the "where's the executable
/// inside the `.app`" knowledge lives in exactly one place regardless of
/// which root it's being resolved under. Bundling a pinned build at all
/// (rather than relying on PinchTab's own Chrome autodiscovery) is load-
/// bearing correctness, not just a security nicety: a recent/bleeding-edge
/// system Chrome reproducibly breaks PinchTab's headless screenshot capture
/// with a -32000 CDP error, confirmed empirically; the pinned
/// 131.0.6778.33 build does not have that problem, also confirmed
/// empirically against a real spawned session.
fn chromium_binary_under(chromium_root: &Path) -> Option<PathBuf> {
    let platform = chromium_platform_segment()?;
    let candidate = chromium_root
        .join(platform)
        .join("Google Chrome for Testing.app")
        .join("Contents")
        .join("MacOS")
        .join("Google Chrome for Testing");
    candidate.is_file().then_some(candidate)
}

/// The pinned Chromium build's binary path if it's present under this
/// crate's *bundled resources* (`resource_dir()/chromium/<platform>/…`).
/// This used to be the sole production path — `scripts/fetch-sidecars.sh`
/// fetched Chromium at build-prep time and `tauri.conf.json`'s
/// `bundle.resources` copied it straight into the installer. It's no longer
/// bundled in a fresh build (see `download_chromium`'s doc comment for why:
/// ~491MB of a 755MB installer), so in a real shipped `.app` this now
/// resolves to `None` — kept only as a harmless fallback for a dev checkout
/// that still has `src-tauri/resources/chromium/` populated from before this
/// change (or a future re-bundling decision), checked *after*
/// `downloaded_chromium_binary` in `resolve_chrome_binary`'s priority order.
fn bundled_chromium_binary() -> Option<PathBuf> {
    chromium_binary_under(&resource_dir().join("chromium"))
}

/// Where a downloaded Chromium build lives — `data_dir()/chromium/`, the
/// real per-app OS data directory, exactly parallel to `voice.rs`'s
/// `model_dir()` for the whisper model. Public resources/root, not a full
/// binary path — `download_chromium` extracts a whole `<platform>/…/
/// Google Chrome for Testing.app` tree under here.
fn downloaded_chromium_dir() -> PathBuf {
    data_dir().join("chromium")
}

/// The pinned Chromium build's binary path if it's already been downloaded
/// into the app-data directory — the new production default (see
/// `download_chromium`'s doc comment and `resolve_chrome_binary`'s priority
/// order).
fn downloaded_chromium_binary() -> Option<PathBuf> {
    chromium_binary_under(&downloaded_chromium_dir())
}

/// Resolves which Chrome/Chromium binary `configure_pinchtab` should point
/// PinchTab at, in priority order:
/// 1. `DAIMON_CHROME_BIN` — dev/smoke-testing override, also how a locally
///    installed Chrome/Chromium can stand in during manual testing.
/// 2. `downloaded_chromium_binary` — the real production default: a build
///    fetched on demand into the app-data directory (see
///    `download_chromium`).
/// 3. `bundled_chromium_binary` — a dev-checkout-only fallback (see that
///    function's doc comment); essentially never populated in a real
///    shipped `.app` since Chromium is no longer bundled into the installer.
/// 4. `None` — nothing found; PinchTab's own binary autodiscovery is the
///    last-resort fallback `configure_pinchtab` already had before any of
///    this bundling/download work existed.
fn resolve_chrome_binary() -> Option<PathBuf> {
    std::env::var("DAIMON_CHROME_BIN")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .or_else(downloaded_chromium_binary)
        .or_else(bundled_chromium_binary)
}

// ---------------------------------------------------------------------
// Chromium download-on-demand — mirrors `voice.rs`'s whisper-model pattern
// exactly (download to a `.part` sibling, verify, then atomically move into
// place; a Settings-triggered command plus an automatic pre-flight step
// share the same implementation via a progress callback). See this module's
// doc comment for why this replaced bundling Chromium directly in the
// installer.
// ---------------------------------------------------------------------

/// The exact same pinned Chrome for Testing build `scripts/fetch-sidecars.sh`
/// used to fetch at build time — see that script's git history and this
/// constant's sibling `CHROMIUM_SHA256_MAC_ARM64` for the empirical
/// validation this pin represents (confirmed NOT to hit PinchTab's -32000
/// screenshot-capture CDP error, unlike a recent/bleeding-edge system
/// Chrome). Do not bump casually — re-validate against the same
/// reproduction steps (a real spawned session, screenshot tool call) before
/// changing it.
const CHROMIUM_VERSION: &str = "131.0.6778.33";
/// sha256 of `https://cdn.playwright.dev/builds/cft/131.0.6778.33/mac-arm64/chrome-mac-arm64.zip`
/// — Chrome for Testing publishes no official per-file checksum for this CDN
/// mirror path, so this is a trust-on-first-use pin, independently
/// re-verified against the very build this downloads (see
/// `scripts/fetch-sidecars.sh`'s original comment for this same value, now
/// migrated here since this is the only place that still fetches it).
const CHROMIUM_SHA256_MAC_ARM64: &str = "96c90f22098860be358c9bfebdbf934b57adbb5a6f32b3e391314e8ebb227b89";

/// Serializes concurrent Chromium downloads — two sessions/automations
/// racing to start on the same fresh install (neither has a Chromium yet)
/// could otherwise both try to download-and-extract into the same
/// `downloaded_chromium_dir()` simultaneously. Callers re-check
/// `resolve_chrome_binary()` after acquiring this so a download that raced
/// and lost just finds the winner's result already in place and skips its
/// own.
static CHROMIUM_DOWNLOAD_LOCK: Mutex<()> = Mutex::const_new(());

/// Downloads, checksum-verifies, and extracts the pinned Chrome for Testing
/// build into `downloaded_chromium_dir()` — idempotent (a no-op, with no
/// callback invocations at all, if `resolve_chrome_binary()` already
/// resolves to something), exactly mirroring
/// `voice.rs::download_voice_model`'s early-return shape. `on_progress` is
/// invoked with `(downloaded_bytes, total_bytes, done)` for each streamed
/// chunk (`done: false`) and once more after a successful
/// verify-and-extract (`done: true`, with final byte counts) — shared by
/// both real callers (`download_chromium`, a Settings-triggered command
/// emitting on the generic `chromium-download` channel, and
/// `ensure_session_workspace`'s automatic first-session pre-flight, emitting
/// session-scoped progress on `session-status` instead) so the actual
/// download/verify/extract logic exists exactly once.
async fn download_chromium_impl(on_progress: impl Fn(u64, Option<u64>, bool)) -> Result<(), String> {
    if resolve_chrome_binary().is_some() {
        log::info!("workspace: chromium already available, skipping download");
        return Ok(());
    }

    let Some(platform) = chromium_platform_segment() else {
        return Err(format!("unsupported architecture for a chromium download: {}", std::env::consts::ARCH));
    };
    let (asset, expected_sha256) = match platform {
        "mac-arm64" => ("chrome-mac-arm64", CHROMIUM_SHA256_MAC_ARM64),
        // No independently verified checksum is pinned for Intel Macs yet —
        // `scripts/fetch-sidecars.sh` had this exact same limitation before
        // Chromium moved to this download-on-demand flow (its own
        // `CHROMIUM_SHA256_MAC_X64` was a literal `"UNVERIFIED"` placeholder
        // that made the old build-time fetch refuse to proceed for x64) —
        // not a new regression introduced by this change, just carried
        // forward rather than silently trusting an unverified download.
        "mac-x64" => {
            return Err(
                "chromium download for Intel Macs isn't available yet — no independently verified checksum is \
                 pinned for this build; set DAIMON_CHROME_BIN to a local Chrome/Chromium install in the meantime"
                    .to_string(),
            )
        }
        other => return Err(format!("unsupported chromium platform segment: {other}")),
    };

    let url = format!("https://cdn.playwright.dev/builds/cft/{CHROMIUM_VERSION}/{platform}/{asset}.zip");
    log::info!("workspace: downloading chromium from {url}");

    let root_dir = downloaded_chromium_dir();
    std::fs::create_dir_all(&root_dir).map_err(|e| format!("failed to create chromium directory: {e}"))?;

    let response = reqwest::get(&url)
        .await
        .map_err(|e| format!("failed to reach the chromium download host: {e}"))?;
    if !response.status().is_success() {
        return Err(format!("chromium download failed with HTTP status {}", response.status()));
    }
    let total_bytes = response.content_length();

    // Same "download to a `.part` sibling, verify, then move into place"
    // shape as `voice.rs`'s model download — a crash/quit mid-download must
    // never leave a partial file `resolve_chrome_binary` could mistake for a
    // real, usable install.
    let tmp_zip = root_dir.join(format!("{asset}.zip.part"));
    let mut file = tokio::fs::File::create(&tmp_zip)
        .await
        .map_err(|e| format!("failed to create chromium download file: {e}"))?;

    let mut stream = response.bytes_stream();
    let mut downloaded_bytes: u64 = 0;
    let mut hasher = Sha256::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("chromium download was interrupted: {e}"))?;
        hasher.update(&chunk);
        tokio::io::AsyncWriteExt::write_all(&mut file, &chunk)
            .await
            .map_err(|e| format!("failed to write chromium download file: {e}"))?;
        downloaded_bytes += chunk.len() as u64;
        on_progress(downloaded_bytes, total_bytes, false);
    }
    drop(file);

    let actual_sha256 = format!("{:x}", hasher.finalize());
    if actual_sha256 != expected_sha256 {
        let _ = tokio::fs::remove_file(&tmp_zip).await;
        return Err(format!(
            "chromium download checksum mismatch (expected {expected_sha256}, got {actual_sha256}) — refusing to use it"
        ));
    }

    let final_zip = root_dir.join(format!("{asset}.zip"));
    tokio::fs::rename(&tmp_zip, &final_zip)
        .await
        .map_err(|e| format!("failed to finalize chromium download: {e}"))?;

    // Extracted via macOS's builtin `unzip` (this app is macOS-only for now
    // — see `scripts/fetch-sidecars.sh`'s own doc comment) rather than
    // pulling in a zip-handling crate for this one, platform-specific
    // extraction, run off the async runtime since it's blocking I/O.
    let extract_root = root_dir.clone();
    let zip_for_extract = final_zip.clone();
    tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
        let status = std::process::Command::new("unzip")
            .arg("-q")
            .arg("-o")
            .arg(&zip_for_extract)
            .arg("-d")
            .arg(&extract_root)
            .status()
            .map_err(|e| format!("failed to run unzip: {e}"))?;
        if !status.success() {
            return Err(format!("unzip exited with status {status}"));
        }
        Ok(())
    })
    .await
    .map_err(|e| format!("chromium extraction task panicked: {e}"))??;

    // `unzip` extracts to `<root_dir>/<asset>/…` (matching the zip's own
    // internal layout) — rename that into the `<platform>/…` directory
    // `chromium_binary_under` expects, mirroring what
    // `scripts/fetch-sidecars.sh`'s old `fetch_chromium` did with a plain
    // `mv`.
    let unzipped_dir = root_dir.join(asset);
    let platform_dir = root_dir.join(platform);
    let _ = tokio::fs::remove_dir_all(&platform_dir).await; // clear any partial previous extraction
    tokio::fs::rename(&unzipped_dir, &platform_dir)
        .await
        .map_err(|e| format!("failed to finalize chromium install directory: {e}"))?;
    let _ = tokio::fs::remove_file(&final_zip).await;

    if downloaded_chromium_binary().is_none() {
        return Err(format!(
            "chromium was downloaded and extracted but the expected binary was not found under {}",
            platform_dir.display()
        ));
    }

    log::info!("workspace: chromium download complete ({downloaded_bytes} bytes) at {}", platform_dir.display());
    on_progress(downloaded_bytes, total_bytes, true);
    Ok(())
}

// ---------------------------------------------------------------------
// Per-session process state
// ---------------------------------------------------------------------

struct SessionProcesses {
    node_port: u16,
    pinchtab_port: u16,
    // Deliberately not stored here: once `configure_pinchtab` writes it into
    // `profile_dir`'s config.json (`server.token`), it's durable — a
    // watchdog recycle respawns PinchTab against the same on-disk config
    // without needing to re-supply it, and Node only ever needs it once, at
    // its own spawn time.
    profile_dir: PathBuf,
    pinchtab_child: Box<dyn ChildWrapper>,
    node_child: Box<dyn ChildWrapper>,
    watchdog: tauri::async_runtime::JoinHandle<()>,
}

/// A session's processes stay up for the session's whole lifetime (Phase 8:
/// follow-up messages continue the same browser/page state rather than
/// starting fresh) — this is the cache that makes a session's second and
/// later messages skip process creation entirely and reuse the
/// already-running processes' known port. Entries are removed by
/// `end_session_workspace`.
static SESSIONS: LazyLock<Mutex<HashMap<String, SessionProcesses>>> = LazyLock::new(|| Mutex::new(HashMap::new()));

// ---------------------------------------------------------------------
// Port allocation
// ---------------------------------------------------------------------

/// Bind-then-release: the native equivalent of Docker's `-p 127.0.0.1::4711`
/// random host-port assignment. A small bind-to-release-to-rebind TOCTOU
/// race is theoretically possible but not practically a concern here — nothing
/// else in this process allocates ports in the same window.
fn allocate_port() -> Result<u16, String> {
    let listener =
        std::net::TcpListener::bind("127.0.0.1:0").map_err(|e| format!("failed to allocate a port: {e}"))?;
    listener
        .local_addr()
        .map(|addr| addr.port())
        .map_err(|e| format!("failed to read allocated port: {e}"))
}

// ---------------------------------------------------------------------
// PinchTab: binary resolution, config setup, spawn, health, teardown
// ---------------------------------------------------------------------

/// Resolves the `pinchtab` binary to run, in priority order:
/// 1. `DAIMON_PINCHTAB_BIN` — dev/smoke-testing override, lets a specific
///    downloaded release binary be used without it being on `$PATH`.
/// 2. The bundled sidecar (`Contents/MacOS/pinchtab` in a real `.app` — see
///    `bundled_sidecar_path`) — the real production path once
///    `scripts/fetch-sidecars.sh` has populated `src-tauri/binaries/` and
///    `npm run tauri build` has bundled it in.
/// 3. A bare `pinchtab` on `$PATH` — last-resort dev convenience for a
///    `cargo tauri dev`/`cargo test` run where neither of the above applies.
fn pinchtab_binary() -> PathBuf {
    if let Ok(bin) = std::env::var("DAIMON_PINCHTAB_BIN") {
        return PathBuf::from(bin);
    }
    bundled_sidecar_path("pinchtab").unwrap_or_else(|| PathBuf::from("pinchtab"))
}

/// Resolves the bundled `ffmpeg` binary PinchTab's recording encode step
/// needs, in the same priority order as `pinchtab_binary`/`ffmpeg_binary`'s
/// siblings:
/// 1. `DAIMON_FFMPEG_BIN` — dev/smoke-testing override, an absolute path to
///    a specific `ffmpeg` binary (expected to actually be named `ffmpeg` —
///    see `pinchtab_path_env`'s doc comment for why the filename matters
///    here, unlike `DAIMON_CHROME_BIN`).
/// 2. The bundled sidecar (`Contents/MacOS/ffmpeg` in a real `.app` — see
///    `bundled_sidecar_path`), populated by `scripts/fetch-sidecars.sh`.
/// 3. `None` — no override, no bundled resource found (a dev checkout that
///    hasn't run the fetch script). PinchTab's spawned process then simply
///    inherits this daemon's own `$PATH` unmodified, so a `cargo tauri dev`
///    run on a machine with Homebrew ffmpeg already on `PATH` still works —
///    exactly the situation that let this bug go unnoticed during earlier
///    development (see this module's `spawn_pinchtab_server` doc comment).
pub(crate) fn ffmpeg_binary() -> Option<PathBuf> {
    if let Ok(bin) = std::env::var("DAIMON_FFMPEG_BIN") {
        return Some(PathBuf::from(bin));
    }
    bundled_sidecar_path("ffmpeg")
}

/// Builds the `PATH` env var for the spawned `pinchtab server` process.
/// PinchTab's own WebM/MP4 recording encode step shells out to a bare
/// `ffmpeg`, resolved via Go's `exec.LookPath` against whatever `$PATH` it
/// inherits — confirmed directly against PinchTab's real source
/// (`internal/handlers/record_encode.go`): no config key exists to point it
/// at a specific binary path directly, `PATH` is the only mechanism
/// available. So the fix is exactly what a real GUI-launched `.app` needs
/// and a `cargo tauri dev` run from an interactive shell doesn't: the
/// bundled ffmpeg's *directory* prepended ahead of this daemon process's own
/// inherited `PATH` (never replacing it — a dev machine's Homebrew ffmpeg on
/// `PATH` still works as a fallback if the bundled one isn't found for some
/// reason), so `exec.LookPath("ffmpeg")` finds the pinned bundled binary
/// first. This is why `ffmpeg_binary()` must actually be named `ffmpeg` — a
/// `DAIMON_FFMPEG_BIN` override pointing at a differently-named file would
/// resolve here (as a `PathBuf`) but silently fail to satisfy PinchTab's own
/// `LookPath("ffmpeg")` call, since only the *directory* crosses into
/// PinchTab's environment, not the exact path.
fn pinchtab_path_env() -> String {
    let inherited = std::env::var("PATH").unwrap_or_default();
    match ffmpeg_binary().and_then(|bin| bin.parent().map(Path::to_path_buf)) {
        Some(dir) => format!("{}:{inherited}", dir.display()),
        None => inherited,
    }
}

/// Runs a one-shot `pinchtab <args>` CLI subcommand (config set/get/init,
/// health, server stop) against `profile_dir`'s config — not the long-running
/// `pinchtab server` itself, which is spawned separately as a supervised
/// group leader (see `spawn_pinchtab_server`). `XDG_CONFIG_HOME`/`HOME` both
/// point at `profile_dir` — confirmed empirically that PinchTab derives its
/// real config path from `$XDG_CONFIG_HOME/.pinchtab/config.json` (not
/// `$XDG_CONFIG_HOME/pinchtab/...`, despite `sandbox/entrypoint.sh`'s old
/// comment assuming that shape — harmless there since it only used it for an
/// existence check, but worth correcting here) and Chrome's own profile
/// directory from the same `HOME`, which is also what keeps each session's
/// Chrome profile/cookies genuinely isolated from the real user's `$HOME`
/// and from every other session — confirmed empirically via a real spawned
/// instance's `--user-data-dir` argv.
async fn run_pinchtab_cli(profile_dir: &Path, args: &[&str]) -> Result<std::process::Output, String> {
    Command::new(pinchtab_binary())
        .args(args)
        .env("XDG_CONFIG_HOME", profile_dir)
        .env("HOME", profile_dir)
        .current_dir(profile_dir)
        .output()
        .await
        .map_err(|e| format!("failed to run `pinchtab {}`: {e}", args.join(" ")))
}

async fn pinchtab_config_set(profile_dir: &Path, key: &str, value: &str) -> Result<(), String> {
    let output = run_pinchtab_cli(profile_dir, &["config", "set", key, value]).await?;
    if !output.status.success() {
        return Err(format!(
            "pinchtab config set {key} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(())
}

/// Ported 1:1 from `sandbox/entrypoint.sh`'s config-setup block (the source
/// of truth for this logic — read it before touching this function), plus
/// one new call: `server.port`, needed now that concurrent sessions each
/// need their own PinchTab instance rather than sharing one container's
/// fixed 9867 behind Docker's per-container network namespace. Confirmed
/// empirically (`pinchtab config get server.port` / `config set
/// server.port <n>` against a real release binary) that PinchTab's config
/// genuinely supports a per-instance port — the plan's flagged unknown this
/// resolves. Only called on a session's *first* spawn — the watchdog's
/// soft-threshold PinchTab recycle (`recycle_pinchtab`) reuses the same
/// `profile_dir` with config already on disk and skips this entirely.
async fn configure_pinchtab(profile_dir: &Path, port: u16, token: &str) -> Result<(), String> {
    std::fs::create_dir_all(profile_dir).map_err(|e| format!("failed to create pinchtab profile directory: {e}"))?;

    let init = run_pinchtab_cli(profile_dir, &["config", "init"]).await?;
    if !init.status.success() {
        return Err(format!(
            "pinchtab config init failed: {}",
            String::from_utf8_lossy(&init.stderr)
        ));
    }

    // 127.0.0.1, not PinchTab's own default of 0.0.0.0 — deliberately more
    // restrictive, matching entrypoint.sh's original reasoning: Node reaches
    // PinchTab over loopback only, and nothing about PinchTab's own port is
    // ever meant to be reachable from anywhere but this same machine.
    pinchtab_config_set(profile_dir, "server.bind", "127.0.0.1").await?;
    if !token.is_empty() {
        pinchtab_config_set(profile_dir, "server.token", token).await?;
    }
    pinchtab_config_set(profile_dir, "server.port", &port.to_string()).await?;
    // Off by default; gates PinchTab's native video recording, which
    // agents/src/browser.ts's finish_recording tool depends on.
    pinchtab_config_set(profile_dir, "security.allowScreencast", "true").await?;
    // Deliberately NOT setting browser.extraFlags to "--disable-gpu" here,
    // despite that having briefly been this line during the Docker-removal
    // migration: it was meant to fix Chrome's GPU process spinning at
    // ~1080% CPU indefinitely, a real bug seen under the old pre-PinchTab
    // Playwright-driven Chromium. But PinchTab's own headless launch code
    // (confirmed directly in the pinned v0.15.0 source,
    // internal/browsers/chrome/chrome.go and internal/bridge/runtime/init.go)
    // already solves that exact problem correctly on its own — it launches
    // with --headless=new, --use-angle=swiftshader, --enable-unsafe-swiftshader
    // (a software-rendered compositor needing no real GPU), specifically
    // *because* Page.captureScreenshot/printToPDF route through that
    // compositor and need a backend. Forcing --disable-gpu removes the GPU
    // process — and therefore that backend — entirely, which is exactly
    // what caused this workspace's screenshot/live-view/recording capture to
    // fail with CDP "-32000 Unable to capture screenshot" errors and
    // grey/frozen frames on every session, deterministically, regardless of
    // machine state. PinchTab's own test suite (chrome_test.go,
    // bridge/init_test.go, bridge/runtime/init_test.go) treats
    // --disable-gpu appearing in headless launch args as a hard regression
    // for this reason. Do not re-add it chasing the old CPU-spin memory —
    // PinchTab's swiftshader-based defaults already avoid that without it.
    // PinchTab's IDPI layer blocks navigation to any domain not on this
    // allowlist (default: loopback only). Every `open_url`/`web_search`
    // target is untrusted-by-design here (job boards, arbitrary company
    // sites the user names), so the allowlist model doesn't fit this
    // product — widen it to "*". IDPI's *content* defenses
    // (scanContent/wrapContent) stay on: orthogonal to which domains are
    // reachable, and a free prompt-injection mitigation on scraped page text.
    pinchtab_config_set(profile_dir, "security.allowedDomains", "*").await?;

    // See `resolve_chrome_binary`'s doc comment for the full priority order
    // (env override, then downloaded-on-demand, then a dev-checkout-only
    // bundled fallback). Left unset only when none of those resolve —
    // PinchTab's own binary autodiscovery is the last-resort fallback in
    // that case, same as before this pass. By the time `configure_pinchtab`
    // runs, `ensure_session_workspace` has already ensured a downloaded
    // Chromium exists whenever neither an env override nor a bundled
    // fallback was available — see that function's pre-flight step — so in
    // practice this only stays `None` on an unsupported architecture.
    let chrome_bin = resolve_chrome_binary();
    if let Some(chrome_bin) = chrome_bin {
        pinchtab_config_set(profile_dir, "browser.binary", &chrome_bin.to_string_lossy()).await?;
    }

    Ok(())
}

/// Opens (or creates and truncates) a log file for one session/process pair
/// — the native-process equivalent of `docker logs <container>`, since a
/// spawned child's stdout/stderr otherwise has nowhere durable to go. Best
/// effort: falls back to two independent `Stdio::null()`s (never fails the
/// spawn over a log file we couldn't open) so a permissions problem here
/// can't block a session from starting.
///
/// Returns *one* `(stdout, stderr)` pair sharing a single underlying open
/// file description (via `try_clone`), not two separate `File::create`
/// calls to the same path. This matters: two independent file descriptors
/// opened separately each track their own write cursor starting at offset
/// 0, so concurrent writes from stdout and stderr onto the "same" file
/// would actually race and overwrite each other's bytes at the same
/// offsets — silently corrupting/losing whichever stream loses the race.
/// This is exactly why `console.error`/`log.Println` output describing a
/// real failure could go missing from these logs even though the failure
/// genuinely happened: the very message that would have explained it got
/// clobbered by an interleaved stdout write. `try_clone`'s dup'd descriptor
/// shares the same underlying file offset as the original, so appends from
/// either stream always land at the true end of file, exactly like normal
/// terminal output interleaving stdout/stderr correctly.
fn log_file_stdio(session_id: &str, which: &str) -> (std::process::Stdio, std::process::Stdio) {
    let path = logs_dir().join(format!("{session_id}-{which}.log"));
    match std::fs::File::create(&path) {
        Ok(file) => match file.try_clone() {
            Ok(cloned) => (file.into(), cloned.into()),
            Err(e) => {
                log::warn!("failed to clone log file handle for {}: {e}", path.display());
                (file.into(), std::process::Stdio::null())
            }
        },
        Err(e) => {
            log::warn!("failed to open log file {}: {e}", path.display());
            (std::process::Stdio::null(), std::process::Stdio::null())
        }
    }
}

/// Spawns `pinchtab server` as its own process-group leader. Note this
/// alone does *not* guarantee the whole Chrome tree dies with it — see this
/// module's doc comment; `kill_pinchtab_tree` (not a plain kill on this
/// child) is the real teardown path.
fn spawn_pinchtab_server(session_id: &str, profile_dir: &Path) -> Result<Box<dyn ChildWrapper>, String> {
    let (stdout, stderr) = log_file_stdio(session_id, "pinchtab");
    let path_env = pinchtab_path_env();
    let mut command = CommandWrap::with_new(pinchtab_binary(), |cmd| {
        cmd.arg("server")
            .env("XDG_CONFIG_HOME", profile_dir)
            .env("HOME", profile_dir)
            // The bundled ffmpeg's directory prepended ahead of this
            // daemon's own inherited PATH — see `pinchtab_path_env`'s doc
            // comment for why this is the only way PinchTab's recording
            // encode step can find a bundled (rather than
            // Homebrew-on-PATH-only) ffmpeg.
            .env("PATH", &path_env)
            .current_dir(profile_dir)
            .stdout(stdout)
            .stderr(stderr);
    });
    command.wrap(ProcessGroup::leader());
    command.wrap(KillOnDrop);
    command.spawn().map_err(|e| format!("failed to spawn pinchtab server: {e}"))
}

async fn wait_for_pinchtab_health(profile_dir: &Path) -> Result<(), String> {
    for _ in 0..60 {
        if let Ok(output) = run_pinchtab_cli(profile_dir, &["health"]).await {
            if output.status.success() {
                return Ok(());
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    Err("pinchtab did not become healthy in time".into())
}

/// The real teardown path for PinchTab's whole managed process tree — see
/// this module's doc comment for why a plain kill on the leader PID alone
/// (what the original plan assumed would suffice) isn't enough.
///
/// The graceful-stop timeout below was 5s originally — confirmed
/// empirically (a real timed `pinchtab server stop` against a Chrome tree
/// of a size representative of a real, already-recycled-a-few-times
/// session) that a graceful stop can legitimately take just over 5s once
/// there are enough tabs/renderer processes for it to tear down cleanly.
/// Racing a real, still-succeeding graceful shutdown against too tight a
/// timeout is exactly the kind of thing that can leave a teardown
/// half-finished, so this is widened with real headroom rather than tuned
/// to the one data point measured.
async fn kill_pinchtab_tree(profile_dir: &Path, leader_pid: u32) {
    let stop = tokio::time::timeout(Duration::from_secs(20), run_pinchtab_cli(profile_dir, &["server", "stop"])).await;
    match stop {
        Ok(Ok(output)) if output.status.success() => {
            log::info!("pinchtab server stop succeeded for {}", profile_dir.display());
        }
        other => {
            log::warn!(
                "pinchtab server stop did not complete cleanly for {} ({other:?}) — falling back to a manual process-tree sweep",
                profile_dir.display()
            );
        }
    }
    // Defense-in-depth regardless of the above outcome. Two sweeps, not one:
    // the PID-ancestry walk catches `server`/`bridge` themselves if `stop`
    // left them behind, but — confirmed empirically, see `kill_process_tree`'s
    // doc comment — is NOT reliable for Chrome's own tree, since `stop`'s own
    // graceful shutdown tends to reparent surviving Chrome processes to PID 1
    // before this runs. The profile-dir sweep is what actually reaches them.
    kill_process_tree(leader_pid).await;
    kill_processes_by_profile_dir(profile_dir).await;
}

// ---------------------------------------------------------------------
// One-time browser login — a persistent, authenticated profile every
// session's own fresh, per-session profile can borrow cookies/storage
// from (see `copy_login_profile_into` below), instead of every session
// starting from a completely blank, logged-out browser. This is the one
// deliberate exception to "Daimon never shows a window" anywhere in this
// codebase — it only ever runs because the user explicitly clicked "log
// in" in Settings, is a real, visible Chrome window they interact with
// directly (typing a URL, entering credentials, solving a CAPTCHA — same
// as using any other browser), and tears back down the moment they say
// they're done.
// ---------------------------------------------------------------------

fn canonical_browser_profile_dir() -> PathBuf {
    data_dir().join("canonical-browser-profile")
}

struct LoginProcessHandle {
    child: Box<dyn ChildWrapper>,
    profile_dir: PathBuf,
}

static LOGIN_PROCESS: LazyLock<Mutex<Option<LoginProcessHandle>>> = LazyLock::new(|| Mutex::new(None));

/// Starts a real, visible Chrome window (PinchTab in `headed` mode) pointed
/// at the canonical login profile. Idempotent: a no-op (not an error) if a
/// login window is already open, so double-clicking the Settings button
/// doesn't spawn a second one.
#[tauri::command]
pub async fn login_browser_profile() -> Result<(), String> {
    {
        let existing = LOGIN_PROCESS.lock().await;
        if existing.is_some() {
            return Ok(());
        }
    }

    if resolve_chrome_binary().is_none() {
        return Err("Chromium isn't downloaded yet — start a regular session first so it can download.".to_string());
    }

    let profile_dir = canonical_browser_profile_dir();
    let port = allocate_port()?;
    let token = uuid::Uuid::new_v4().to_string();

    configure_pinchtab(&profile_dir, port, &token).await?;
    // The one config key `configure_pinchtab` itself never sets (every
    // other caller wants its default, `headless`) — see this section's own
    // doc comment for why a real visible window is correct here
    // specifically.
    pinchtab_config_set(&profile_dir, "instanceDefaults.mode", "headed").await?;

    let child = spawn_pinchtab_server("browser-login", &profile_dir)?;
    if let Err(e) = wait_for_pinchtab_health(&profile_dir).await {
        let pid = child.id();
        drop(child);
        if let Some(pid) = pid {
            kill_pinchtab_tree(&profile_dir, pid).await;
        }
        return Err(e);
    }

    *LOGIN_PROCESS.lock().await = Some(LoginProcessHandle { child, profile_dir });
    Ok(())
}

/// Ends the login window — critically, this waits for PinchTab's own
/// graceful `server stop` (inside `kill_pinchtab_tree`) to actually flush
/// cookies/storage to disk before returning, since the whole point of this
/// flow is that `copy_login_profile_into` below can trust the canonical
/// profile's files are complete and quiescent by the time a future session
/// reads them. A no-op if no login window is currently open.
#[tauri::command]
pub async fn finish_browser_login() -> Result<(), String> {
    let handle = LOGIN_PROCESS.lock().await.take();
    let Some(handle) = handle else {
        return Ok(());
    };
    let pid = handle.child.id();
    drop(handle.child);
    if let Some(pid) = pid {
        kill_pinchtab_tree(&handle.profile_dir, pid).await;
    }
    // Without this, only a session started *after* this point would ever
    // see the new login — any chat already open would silently keep
    // browsing logged out, indefinitely, until it happened to get recycled
    // for an unrelated reason (the memory watchdog) or the user thought to
    // start a fresh session. Recycling every live session now (reusing the
    // exact same respawn `recycle_pinchtab` already does for the watchdog)
    // makes an already-open chat pick up the login within moments instead.
    recycle_all_sessions_for_login().await;
    Ok(())
}

/// Snapshots every currently-active session's (id, profile_dir, port), then
/// recycles each one's PinchTab tree — see `finish_browser_login`'s call
/// site for why. The snapshot exists so this never holds `SESSIONS` locked
/// across the slow per-session teardown/respawn work `recycle_pinchtab`
/// itself does internally (it takes the same lock again per session); doing
/// that here would otherwise serialize against any concurrent
/// `send_message` trying to use one of these sessions in the meantime.
async fn recycle_all_sessions_for_login() {
    let targets: Vec<(String, PathBuf, u16)> = {
        let sessions = SESSIONS.lock().await;
        sessions
            .iter()
            .map(|(id, s)| (id.clone(), s.profile_dir.clone(), s.pinchtab_port))
            .collect()
    };

    for (session_id, profile_dir, pinchtab_port) in targets {
        log::info!("session {session_id}: recycling to pick up a newly-logged-in browser profile");
        if let Err(e) = recycle_pinchtab(&session_id, &profile_dir, pinchtab_port).await {
            log::warn!("session {session_id}: failed to recycle after browser login: {e}");
        }
    }
}

/// Whether the canonical profile has ever actually captured a real login —
/// checked by looking for a non-empty cookie jar, not just whether the
/// directory exists (which `configure_pinchtab`/`pinchtab config init`
/// alone would already create even before the user logs into anything).
/// Drives Settings' own "logged in" vs. "not logged in yet" display.
#[tauri::command]
pub async fn get_browser_login_status() -> Result<bool, String> {
    let cookies = canonical_browser_profile_dir()
        .join(".pinchtab")
        .join("profiles")
        .join("default")
        .join("Default")
        .join("Cookies");
    Ok(cookies.metadata().map(|m| m.len() > 0).unwrap_or(false))
}

/// The safe, durable-state subset of a Chrome profile to carry from the
/// canonical login profile into a fresh session's own profile. Paths below
/// are relative to a profile's `Default/` subdirectory.
///
/// Copy: `Cookies`/`Cookies-journal` (the cookie jar), `Local Storage` and
/// `IndexedDB` (where most modern SPA auth actually lives — confirmed
/// against PinchTab's own `profiles_crud.go` reset logic, which spares
/// exactly these two while nuking everything else as pure cache/history),
/// `Session Storage` (low value but harmless), `Preferences` (site
/// permissions).
///
/// Never copy: `SingletonLock`/`SingletonCookie`/`SingletonSocket` (PID/
/// socket-bound — copying these into a fresh profile would make Chrome
/// think another live process already owns it), `pinchtab.pid`/
/// `.pinchtab-state/` (this orchestrator's own per-instance state),
/// `Cache`/`Code Cache`/`GPUCache`/`ShaderCache` (pure bloat, regenerates
/// on its own), `Sessions` (tab-restore — would try to reopen the login
/// window's own old tabs), `History`/`Visited Links`/`Favicons` (irrelevant
/// to login, a privacy leak into every session if copied), `Login Data`
/// (saved passwords/autofill — carrying cookies, not autofill).
const LOGIN_PROFILE_COPY_FILES: &[&str] = &["Cookies", "Cookies-journal", "Preferences"];
const LOGIN_PROFILE_COPY_DIRS: &[&str] = &["Local Storage", "IndexedDB", "Session Storage"];

/// Copies the canonical login profile's cookies/storage into a fresh
/// session's own profile directory — called from `ensure_session_workspace`
/// after `configure_pinchtab` (directory + PinchTab config exist) but
/// before `spawn_pinchtab_server` (Chrome hasn't launched against it yet).
/// A no-op, not an error, if the user has never logged in (no canonical
/// profile yet) — sessions behave exactly as they did before this feature
/// existed. Each session gets its own independent copy rather than sharing
/// one profile directory: Chrome locks a `--user-data-dir` to one running
/// process, and Daimon supports multiple concurrent sessions each with
/// their own Chrome instance, so literally pointing them at the same
/// directory would break the moment a second session started.
async fn copy_login_profile_into(session_profile_dir: &Path) -> Result<(), String> {
    let source_default = canonical_browser_profile_dir()
        .join(".pinchtab")
        .join("profiles")
        .join("default")
        .join("Default");
    if !source_default.is_dir() {
        return Ok(());
    }

    let dest_default = session_profile_dir.join(".pinchtab").join("profiles").join("default").join("Default");
    std::fs::create_dir_all(&dest_default).map_err(|e| format!("failed to create profile directory: {e}"))?;

    for name in LOGIN_PROFILE_COPY_FILES {
        let src = source_default.join(name);
        if src.is_file() {
            if let Err(e) = std::fs::copy(&src, dest_default.join(name)) {
                log::warn!("failed to copy login profile file {name}: {e}");
            }
        }
    }
    for name in LOGIN_PROFILE_COPY_DIRS {
        let src = source_default.join(name);
        if src.is_dir() {
            if let Err(e) = copy_dir_recursive(&src, &dest_default.join(name)) {
                log::warn!("failed to copy login profile directory {name}: {e}");
            }
        }
    }
    Ok(())
}

fn copy_dir_recursive(src: &Path, dest: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dest)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let dest_path = dest.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir_recursive(&entry.path(), &dest_path)?;
        } else {
            std::fs::copy(entry.path(), dest_path)?;
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------
// Node agent process
// ---------------------------------------------------------------------

/// Resolves how to launch the Node agent server, in priority order:
/// 1. `DAIMON_NODE_BIN` + `DAIMON_AGENT_ENTRY` (both must be set together) —
///    dev/smoke-testing override, e.g. pointing at a real fetched sidecar
///    without a full bundle around it.
/// 2. **Release builds only** — the bundled sidecar Node binary
///    (`Contents/MacOS/node` in a real `.app`) running the bundled,
///    compiled+pruned agent at `resource_dir()/agent/dist/server.js` — the
///    real production path this pass builds (see
///    `scripts/fetch-sidecars.sh`), reachable once that script has
///    populated both `src-tauri/binaries/` and `src-tauri/resources/agent/`.
/// 3. The dev source tree, run via the `tsx` binary already installed under
///    `agents/node_modules/.bin` (`project_root()` is a legitimate use here
///    — see that function's doc comment — locating dev-only agent sources,
///    not a data directory).
///
/// Step 2 is gated on `!cfg!(debug_assertions)` — deliberately, the hard
/// way: a `cargo build`/`scripts/fetch-sidecars.sh` pass run at any earlier
/// point (even once, e.g. while testing the release-bundling story) leaves a
/// real `resource_dir()/agent/dist/server.js` sitting on disk indefinitely,
/// and in a `cargo run`/`tauri dev` debug build `resource_dir()` resolves
/// under `target/debug/resources` — the same tree that build/bundle step
/// populates. Without this gate, step 2's file-existence check alone can't
/// tell "a real release artifact" apart from "a stale leftover from a
/// previous build," so a `cargo run` dev session would silently keep
/// launching that stale compiled snapshot forever, never picking up new
/// edits to `agents/src/**` again — confirmed as exactly what happened here:
/// an entire session's worth of `browser.ts` fixes ran via `npx tsx`
/// directly (and passed) but were never actually exercised by the real app,
/// because `resolve_node_agent_launch` kept resolving to a `dist/server.js`
/// dated a full day before those fixes existed. A debug build has the
/// source tree right there and should never prefer a stale bundle over it;
/// only a real shipped `.app` (no source tree to run live) has a reason to
/// use step 2 at all.
fn resolve_node_agent_launch() -> (PathBuf, Vec<String>, PathBuf) {
    if let (Ok(node_bin), Ok(entry)) = (std::env::var("DAIMON_NODE_BIN"), std::env::var("DAIMON_AGENT_ENTRY")) {
        let agents_dir = project_root().join("agents");
        return (PathBuf::from(node_bin), vec![entry], agents_dir);
    }

    if !cfg!(debug_assertions) {
        let bundled_agent_dir = resource_dir().join("agent");
        let bundled_entry = bundled_agent_dir.join("dist").join("server.js");
        if let (Some(node_bin), true) = (bundled_sidecar_path("node"), bundled_entry.is_file()) {
            return (node_bin, vec![bundled_entry.to_string_lossy().to_string()], bundled_agent_dir);
        }
    }

    let agents_dir = project_root().join("agents");
    let tsx_bin = agents_dir.join("node_modules").join(".bin").join("tsx");
    (tsx_bin, vec!["src/server.ts".to_string()], agents_dir)
}

fn spawn_node_agent(
    session_id: &str,
    node_port: u16,
    pinchtab_port: u16,
    pinchtab_token: &str,
    spotify_access_token: Option<&str>,
) -> Result<Box<dyn ChildWrapper>, String> {
    let (program, args, cwd) = resolve_node_agent_launch();
    let (stdout, stderr) = log_file_stdio(session_id, "node");

    let memory_db = data_dir().join("memory").join("daimon.db");
    if let Some(parent) = memory_db.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let vault_dir = crate::vault::vault_path();
    let automations_dir = crate::automation::automations_dir();
    let recordings_dir = crate::recordings::recordings_dir();
    let documents_dir = documents_dir();

    let auth_mode = crate::settings::agent_auth_mode();
    let api_key = std::env::var("ANTHROPIC_API_KEY").ok();

    let mut command = CommandWrap::with_new(program, |cmd| {
        cmd.args(&args)
            .current_dir(&cwd)
            .env("PORT", node_port.to_string())
            .env("PINCHTAB_BASE", format!("http://127.0.0.1:{pinchtab_port}"))
            .env("PINCHTAB_TOKEN", pinchtab_token)
            .env("DAIMON_VAULT_DIR", &vault_dir)
            .env("DAIMON_AUTOMATIONS_DIR", &automations_dir)
            .env("DAIMON_MEMORY_DB", &memory_db)
            .env("DAIMON_RECORDINGS_DIR", &recordings_dir)
            .env("DAIMON_DOCUMENTS_DIR", &documents_dir)
            .env("DAIMON_AUTH_MODE", auth_mode.as_str())
            .stdout(stdout)
            .stderr(stderr);
        match auth_mode {
            // `env_remove`, not merely "don't call .env()". The child inherits
            // this process's environment, and ANTHROPIC_API_KEY is *in* it —
            // `set_env_var` mirrors every stored key into the live process and
            // dotenvy loads `.env` at startup. An inherited key would shadow
            // the user's Claude Code OAuth login inside the harness, silently
            // billing their API account for every turn while the UI reports
            // that their subscription is in use. The agent process strips it a
            // second time on its own (see `buildOptions` in agents/src/agent.ts)
            // because the cost of getting this wrong is money, quietly.
            crate::settings::AuthMode::Subscription => {
                cmd.env_remove("ANTHROPIC_API_KEY");
            }
            crate::settings::AuthMode::ApiKey => {
                if let Some(key) = &api_key {
                    cmd.env("ANTHROPIC_API_KEY", key);
                }
            }
        }
        // Short-lived by design — fetched fresh (via the stored refresh
        // token) right before every spawn rather than ever handing the
        // long-lived refresh token itself to the agent process. Absent
        // entirely (not an empty string) if no Spotify account is
        // connected, so `agents/src/spotify.ts` can treat "not set" as "not
        // connected" without a separate status check.
        if let Some(token) = spotify_access_token {
            cmd.env("SPOTIFY_ACCESS_TOKEN", token);
        }
    });
    command.wrap(ProcessGroup::leader());
    command.wrap(KillOnDrop);
    command.spawn().map_err(|e| format!("failed to spawn node agent: {e}"))
}

async fn wait_for_health(port: u16) -> Result<(), String> {
    let client = reqwest::Client::new();
    let url = format!("http://127.0.0.1:{port}/health");

    for _ in 0..60 {
        if let Ok(resp) = client.get(&url).send().await {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    Err("background workspace did not become healthy in time".into())
}

// ---------------------------------------------------------------------
// Process-tree walking (shared by the memory watchdog and every teardown
// path — both need "every descendant of this PID", just for different ends)
// ---------------------------------------------------------------------

fn collect_process_tree(system: &System, root: Pid) -> Vec<Pid> {
    let mut result = vec![root];
    let mut frontier = vec![root];
    while let Some(pid) = frontier.pop() {
        for (candidate_pid, process) in system.processes() {
            if process.parent() == Some(pid) && !result.contains(candidate_pid) {
                result.push(*candidate_pid);
                frontier.push(*candidate_pid);
            }
        }
    }
    result
}

/// Sums RSS (`Process::memory()`, bytes) across `root_pid` and every one of
/// its descendants — necessary because Chrome's renderer/GPU/utility
/// processes are children of Chrome's own main process, not direct children
/// of PinchTab, so a single-level scan undercounts exactly the processes
/// this exists to catch.
async fn sum_process_tree_rss(root_pid: u32) -> u64 {
    tokio::task::spawn_blocking(move || {
        let mut system = System::new_all();
        system.refresh_processes(ProcessesToUpdate::All, true);
        let root = Pid::from_u32(root_pid);
        if system.process(root).is_none() {
            return 0;
        }
        collect_process_tree(&system, root)
            .into_iter()
            .filter_map(|pid| system.process(pid))
            .map(|process| process.memory())
            .sum()
    })
    .await
    .unwrap_or(0)
}

/// `SIGKILL`s `root_pid` and every descendant found via a live parent→child
/// walk — the general-purpose teardown primitive used both as
/// `kill_pinchtab_tree`'s defense-in-depth sweep and directly for the Node
/// process (which, unlike PinchTab, doesn't have its own graceful
/// `stop` subcommand to prefer).
///
/// **Confirmed empirically to be unreliable for PinchTab's own Chrome
/// tree specifically** (see `kill_processes_by_profile_dir`, which exists
/// because of this): PID-ancestry walks assume `Process::parent()` still
/// traces back to `root_pid` at kill time, but `pinchtab server stop`'s own
/// graceful shutdown appears to let `server`/`bridge` exit — and get
/// reaped — before Chrome's tree has actually terminated, which reparents
/// every surviving Chrome process to PID 1. A walk that runs *after* that
/// reparenting (exactly when this defense-in-depth sweep runs, right after
/// awaiting `server stop`) finds nothing, even though Chrome is still very
/// much alive. Confirmed directly against the running app's own logs: the
/// memory watchdog's hard-ceiling teardown fired, `kill_pinchtab_tree` ran,
/// yet `ps aux` immediately afterward still showed a full live Chrome
/// tree (renderer/GPU/utility helpers) for that exact session's
/// `profile_dir`. Kept as a first-pass sweep anyway — cheap, and it does
/// still catch `server`/`bridge` themselves if `stop` didn't — but
/// `kill_pinchtab_tree` no longer relies on this alone.
async fn kill_process_tree(root_pid: u32) {
    tokio::task::spawn_blocking(move || {
        let mut system = System::new_all();
        system.refresh_processes(ProcessesToUpdate::All, true);
        let root = Pid::from_u32(root_pid);
        for pid in collect_process_tree(&system, root) {
            if let Some(process) = system.process(pid) {
                let _ = process.kill_with(Signal::Kill);
            }
        }
    })
    .await
    .ok();
}

/// SIGKILLs every currently-running process whose command line references
/// `profile_dir` — Chrome's own `--user-data-dir=<profile_dir>/...`
/// argument, set identically on every process in a session's Chrome tree
/// (main process, every renderer, the GPU process, utility/zygote
/// processes, ...) regardless of what they've been reparented to. This is
/// the actually-reliable half of `kill_pinchtab_tree`'s teardown — see
/// `kill_process_tree`'s doc comment for why the PID-ancestry sweep alone
/// isn't enough: `profile_dir` is a stable, session-scoped fingerprint
/// baked into each process's own argv, so matching on it directly
/// sidesteps the reparenting problem entirely rather than depending on a
/// parent-child chain that may no longer exist by the time this runs.
async fn kill_processes_by_profile_dir(profile_dir: &Path) {
    let needle = profile_dir.to_string_lossy().to_string();
    tokio::task::spawn_blocking(move || {
        let mut system = System::new_all();
        system.refresh_processes(ProcessesToUpdate::All, true);
        for process in system.processes().values() {
            let matches = process.cmd().iter().any(|arg| arg.to_string_lossy().contains(&needle));
            if matches {
                let _ = process.kill_with(Signal::Kill);
            }
        }
    })
    .await
    .ok();
}

// ---------------------------------------------------------------------
// Memory/renderer watchdog — the concrete fix for the renderer-accumulation
// bug (6 renderers, 1.48GB RSS after 6 minutes of multi-topic browsing;
// nothing previously enforced a memory ceiling the way Docker's `--memory`
// flag theoretically could have, but never actually was set).
// ---------------------------------------------------------------------

const WATCHDOG_INTERVAL: Duration = Duration::from_secs(30);

/// Originally set from the reference renderer-leak incident (1.48GB after 6
/// minutes of many *dead* accumulated renderers) — confirmed too low once
/// real usage included legitimate video playback: a single actively-playing
/// YouTube tab under PinchTab's swiftshader-based headless launch (software
/// video decode, no hardware accel — see `configure_pinchtab`'s doc comment)
/// genuinely uses close to 2GB in well under a minute, confirmed directly
/// via `ps` RSS summation on a real session's Chrome tree — that's not a leak, it's one
/// healthy tab doing real work, and recycling it doesn't help (it just
/// interrupts playback and likely reaccumulates the same memory once video
/// resumes). Raised well above realistic single-tab-with-video usage, with
/// real headroom rather than tuned to the one incident that motivated the
/// original number. Still meaningfully below the hard ceiling below, so a
/// genuinely unbounded leak (many accumulated *dead* renderers, the
/// original bug class) still gets caught and recycled preventively.
const SOFT_MEMORY_THRESHOLD_BYTES: u64 = 3 * 1024 * 1024 * 1024;

/// Same recalibration as `SOFT_MEMORY_THRESHOLD_BYTES` above, same reason —
/// raised well past realistic legitimate single-tab-with-video usage.
/// Deliberately generous relative to typical modern machine RAM (tens of
/// GB) rather than tight: this is a backstop against genuinely unbounded
/// growth, not a budget for normal browsing, and a session that's
/// legitimately doing memory-heavy work (video, a media-rich page) should
/// never get torn down mid-task for behaving normally.
const HARD_MEMORY_CEILING_BYTES: u64 = 6 * 1024 * 1024 * 1024;

/// Kills the old PinchTab+Chrome tree and spawns a fresh one on the *same*
/// profile directory/port/token — config is already on disk from the
/// session's first spawn, so `configure_pinchtab` is skipped entirely. This
/// is what actually fixes the renderer-accumulation bug: cookies/login state
/// (persisted to `profile_dir`) survive, and Node/the agent loop are never
/// touched, but every accumulated renderer/GPU/utility process is reaped.
async fn recycle_pinchtab(session_id: &str, profile_dir: &Path, pinchtab_port: u16) -> Result<(), String> {
    let old_pid = {
        let sessions = SESSIONS.lock().await;
        sessions.get(session_id).and_then(|s| s.pinchtab_child.id())
    };
    if let Some(pid) = old_pid {
        kill_pinchtab_tree(profile_dir, pid).await;
    }

    // Re-check the canonical login profile on every recycle, not just a
    // session's first spawn — this is what actually makes "log in via
    // Settings while a chat is already open" take effect for that chat,
    // rather than only ever benefiting a session started *after* the login.
    // See `copy_login_profile_into`'s own doc comment; a no-op if nothing's
    // changed there since the last spawn/recycle.
    copy_login_profile_into(profile_dir).await?;

    let new_child = spawn_pinchtab_server(session_id, profile_dir)?;
    if let Err(e) = wait_for_pinchtab_health(profile_dir).await {
        if let Some(pid) = new_child.id() {
            kill_pinchtab_tree(profile_dir, pid).await;
        }
        return Err(e);
    }

    let mut sessions = SESSIONS.lock().await;
    match sessions.get_mut(session_id) {
        Some(session) => {
            session.pinchtab_child = new_child;
            log::info!("session {session_id}: recycled PinchTab (still on port {pinchtab_port})");
            Ok(())
        }
        None => {
            // The session was torn down (explicit end_session, or the app
            // exit sweep) while this recycle was in flight — don't leak the
            // tree we just spawned.
            drop(sessions);
            if let Some(pid) = new_child.id() {
                kill_pinchtab_tree(profile_dir, pid).await;
            }
            Ok(())
        }
    }
}

/// Spawned once per session at the end of `ensure_session_workspace`'s
/// startup sequence; the returned handle is stored on `SessionProcesses` so
/// `end_session_workspace` can stop it immediately rather than let one more
/// tick run against a session that's already gone.
fn spawn_watchdog<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    session_id: String,
) -> tauri::async_runtime::JoinHandle<()> {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(WATCHDOG_INTERVAL).await;

            // Snapshot just what's needed under the lock, then drop it
            // before any slow work (process enumeration, a possible
            // respawn) — never hold SESSIONS locked across a multi-hundred-
            // millisecond await, or a concurrent send_message on this same
            // session would stall behind it.
            let snapshot = {
                let sessions = SESSIONS.lock().await;
                sessions
                    .get(&session_id)
                    .map(|s| (s.pinchtab_child.id(), s.profile_dir.clone(), s.pinchtab_port))
            };
            let Some((Some(pinchtab_pid), profile_dir, pinchtab_port)) = snapshot else {
                // Session already torn down by something else (explicit
                // end_session, or the app exit sweep, raced this tick) —
                // nothing left to watch. Exit rather than loop forever on a
                // dead session.
                return;
            };

            let total_rss = sum_process_tree_rss(pinchtab_pid).await;

            if total_rss >= HARD_MEMORY_CEILING_BYTES {
                log::error!(
                    "session {session_id}: workspace memory hit the hard ceiling ({total_rss} bytes >= {HARD_MEMORY_CEILING_BYTES}) — tearing down"
                );
                end_session_workspace(&session_id).await;
                let message = format!(
                    "Background workspace exceeded its memory ceiling ({}MB) and was shut down.",
                    HARD_MEMORY_CEILING_BYTES / (1024 * 1024)
                );
                crate::session::emit(&app, &session_id, serde_json::json!({ "type": "error", "message": message }));
                return;
            }

            if total_rss >= SOFT_MEMORY_THRESHOLD_BYTES {
                log::warn!(
                    "session {session_id}: workspace memory hit the soft threshold ({total_rss} bytes >= {SOFT_MEMORY_THRESHOLD_BYTES}) — recycling PinchTab"
                );
                if let Err(e) = recycle_pinchtab(&session_id, &profile_dir, pinchtab_port).await {
                    log::error!("session {session_id}: failed to recycle pinchtab: {e}");
                }
            }
        }
    })
}

// ---------------------------------------------------------------------
// PID-file-based orphan reconciliation (replaces the Docker-label-based
// `docker ps --filter name=...` version — no equivalent "ask the daemon
// what's running" exists without Docker)
// ---------------------------------------------------------------------

fn write_pid_file(session_id: &str, pinchtab_pid: Option<u32>, node_pid: Option<u32>, profile_dir: &Path) {
    let content = format!(
        "{}\n{}\n{}\n",
        pinchtab_pid.unwrap_or(0),
        node_pid.unwrap_or(0),
        profile_dir.display()
    );
    if let Err(e) = std::fs::write(pid_file_path(session_id), content) {
        log::warn!("failed to write PID file for session {session_id}: {e}");
    }
}

/// Best-effort startup sweep for orphaned session processes. `SESSIONS` is
/// in-memory only and always empty in a freshly started process — so unlike
/// the exit-time sweep (`all_session_ids`, which tracks specific sessions
/// this same process opened), there's no "is this one still mine" question
/// to answer here: any PID file found at this point necessarily survived
/// from a *previous* process that didn't reach `RunEvent::Exit` (a crash or
/// force-quit skips that handler entirely) — see `lib.rs`'s exit sweep for
/// the counterpart this covers. Cross-checks each recorded PID's live
/// command name (via `sysinfo`, not a raw `kill -0`) before killing it,
/// since PIDs can be reused by an unrelated process in the time since this
/// file was written.
pub async fn reconcile_orphaned_sessions() {
    let dir = run_dir();
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return;
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("pid") {
            continue;
        }

        let Ok(content) = std::fs::read_to_string(&path) else {
            continue;
        };
        let mut lines = content.lines();
        let pinchtab_pid = lines.next().and_then(|l| l.trim().parse::<u32>().ok()).filter(|p| *p != 0);
        let node_pid = lines.next().and_then(|l| l.trim().parse::<u32>().ok()).filter(|p| *p != 0);
        let profile_dir = lines.next().map(PathBuf::from);

        log::info!("startup reconciliation: found orphaned session pid file {}", path.display());

        if let (Some(pid), Some(profile_dir)) = (pinchtab_pid, &profile_dir) {
            kill_pinchtab_tree(profile_dir, pid).await;
        }
        if let Some(pid) = node_pid {
            // Cross-check the command name before killing, unlike the
            // PinchTab path above (where `kill_pinchtab_tree`'s sweep
            // already does this implicitly by only walking real live
            // descendants) — a bare recorded PID could have been reused by
            // an unrelated process since this file was written.
            let alive_and_matches = tokio::task::spawn_blocking(move || {
                let mut system = System::new_all();
                system.refresh_processes(ProcessesToUpdate::All, true);
                system
                    .process(Pid::from_u32(pid))
                    .map(|p| p.name().to_string_lossy().to_lowercase().contains("node"))
                    .unwrap_or(false)
            })
            .await
            .unwrap_or(false);
            if alive_and_matches {
                kill_process_tree(pid).await;
            }
        }

        let _ = std::fs::remove_file(&path);
    }
}

// ---------------------------------------------------------------------
// Public API — kept stable per the plan this implements: callers in
// session.rs/lib.rs/voice.rs/oauth.rs need no structural changes beyond
// `ensure_session_workspace` gaining an `AppHandle` (for the watchdog).
// ---------------------------------------------------------------------

/// Returns the Node agent's host port for `session_id`'s workspace,
/// creating (PinchTab + Node, both native processes) it if this is the
/// session's first message. If the workspace already exists (a follow-up
/// message in an ongoing session), this is a plain cache lookup with no
/// process spawning at all — already up, browser state left exactly as the
/// previous turn left it.
pub async fn ensure_session_workspace<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    session_id: &str,
) -> Result<u16, String> {
    {
        let sessions = SESSIONS.lock().await;
        if let Some(session) = sessions.get(session_id) {
            return Ok(session.node_port);
        }
    }

    // First real session on a fresh install: Chromium isn't bundled in the
    // installer anymore (see this module's doc comment and
    // `download_chromium`'s), so it has to be fetched before this session's
    // workspace can actually browse anything. Blocking session startup on
    // this (rather than silently falling through to `configure_pinchtab`
    // leaving `browser.binary` unset, and PinchTab's own autodiscovery
    // possibly finding nothing at all) is a deliberate UX call — unlike the
    // whisper model (a secondary, opt-in feature gated behind a Settings
    // button), browsing is the core product function, so a first-run
    // "silently doesn't work until you find Settings" failure mode would be
    // worse than a one-time wait with real progress reported. Progress
    // streams over the same `session-status` channel every other event for
    // this session uses, tagged with a `"downloading_chromium"` type, so the
    // pill can show real download progress instead of looking hung.
    if resolve_chrome_binary().is_none() {
        let _download_guard = CHROMIUM_DOWNLOAD_LOCK.lock().await;
        // Re-check after acquiring the lock: another session that started
        // concurrently and raced this one to the lock may have already
        // finished the download while this task was waiting.
        if resolve_chrome_binary().is_none() {
            log::info!("session {session_id}: no chromium available yet — downloading before this session can start");
            let app_for_progress = app.clone();
            let session_id_for_progress = session_id.to_string();
            download_chromium_impl(move |downloaded_bytes, total_bytes, done| {
                crate::session::emit(
                    &app_for_progress,
                    &session_id_for_progress,
                    serde_json::json!({
                        "type": "downloading_chromium",
                        "downloadedBytes": downloaded_bytes,
                        "totalBytes": total_bytes,
                        "done": done
                    }),
                );
            })
            .await?;
        }
    }

    let pinchtab_port = allocate_port()?;
    let node_port = allocate_port()?;
    let profile_dir = pinchtab_profiles_dir().join(session_id);
    let pinchtab_token = uuid::Uuid::new_v4().to_string();

    configure_pinchtab(&profile_dir, pinchtab_port, &pinchtab_token).await?;
    // See `copy_login_profile_into`'s doc comment — no-ops if the user has
    // never done the one-time browser login in Settings.
    copy_login_profile_into(&profile_dir).await?;

    let pinchtab_child = spawn_pinchtab_server(session_id, &profile_dir)?;
    let pinchtab_pid = pinchtab_child.id();
    if let Err(e) = wait_for_pinchtab_health(&profile_dir).await {
        if let Some(pid) = pinchtab_pid {
            kill_pinchtab_tree(&profile_dir, pid).await;
        }
        return Err(e);
    }

    // Best-effort: a failure to refresh (no account connected, or a real
    // network/API error) should never block a session from starting over
    // something as secondary as music playback — just start without it, so
    // `SPOTIFY_ACCESS_TOKEN` ends up absent and `play_music_by_name` fails
    // with a clear "not connected" message if the model tries to use it.
    let spotify_access_token = crate::spotify_oauth::fresh_access_token().await.unwrap_or_else(|e| {
        log::warn!("session {session_id}: failed to refresh Spotify access token: {e}");
        None
    });

    let node_child = match spawn_node_agent(
        session_id,
        node_port,
        pinchtab_port,
        &pinchtab_token,
        spotify_access_token.as_deref(),
    ) {
        Ok(child) => child,
        Err(e) => {
            if let Some(pid) = pinchtab_pid {
                kill_pinchtab_tree(&profile_dir, pid).await;
            }
            return Err(e);
        }
    };
    let node_pid = node_child.id();

    if let Err(e) = wait_for_health(node_port).await {
        if let Some(pid) = pinchtab_pid {
            kill_pinchtab_tree(&profile_dir, pid).await;
        }
        if let Some(pid) = node_pid {
            kill_process_tree(pid).await;
        }
        return Err(e);
    }

    write_pid_file(session_id, pinchtab_pid, node_pid, &profile_dir);
    let watchdog = spawn_watchdog(app.clone(), session_id.to_string());

    let mut sessions = SESSIONS.lock().await;
    sessions.insert(
        session_id.to_string(),
        SessionProcesses {
            node_port,
            pinchtab_port,
            profile_dir,
            pinchtab_child,
            node_child,
            watchdog,
        },
    );
    Ok(node_port)
}

/// Explicit, session-triggered teardown — the *only* thing that removes a
/// session's processes now (Phase 8 reversed the old "tear down after every
/// task" policy): either the user ends the session, or the app quits and
/// sweeps every still-cached session (see `all_session_ids` and `lib.rs`'s
/// exit handler). Idempotent: removing an id that was never cached, or
/// tearing down a workspace that's already gone, is a harmless no-op.
pub async fn end_session_workspace(session_id: &str) {
    let removed = SESSIONS.lock().await.remove(session_id);
    let Some(mut session) = removed else {
        return;
    };
    session.watchdog.abort();

    let pinchtab_pid = session.pinchtab_child.id();
    let node_pid = session.node_child.id();
    if let Some(pid) = pinchtab_pid {
        kill_pinchtab_tree(&session.profile_dir, pid).await;
    }
    if let Some(pid) = node_pid {
        kill_process_tree(pid).await;
    }
    // Reap process-wrap's own handles (already dead by this point via the
    // kills above) so its internal bookkeeping doesn't leave zombies.
    let _ = session.pinchtab_child.wait().await;
    let _ = session.node_child.wait().await;

    let _ = std::fs::remove_file(pid_file_path(session_id));
}

/// Every session id whose workspace is currently believed to be up — used
/// only for the best-effort app-exit sweep, so a quit doesn't leave orphaned
/// processes running indefinitely now that nothing tears them down
/// automatically per message.
pub async fn all_session_ids() -> Vec<String> {
    SESSIONS.lock().await.keys().cloned().collect()
}

pub fn has_api_key() -> bool {
    std::env::var("ANTHROPIC_API_KEY")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
}

/// Persists the key to `.env` and updates it in this process's live
/// environment so the next *new* session picks it up without an app
/// restart. Note this doesn't reach any session whose workspace is already
/// running — since Phase 8, a session's workspace lives for the session's
/// whole lifetime, so an already-open session keeps whatever key it was
/// started with until it's ended and a fresh one is started.
pub async fn set_api_key(key: &str) -> Result<(), String> {
    let key = key.trim();
    if key.is_empty() {
        return Err("API key cannot be empty".into());
    }
    set_env_var("ANTHROPIC_API_KEY", key)
}

/// Upserts `KEY=value` into the app data directory's `.env` file and
/// mirrors it into this process's live environment, so whichever caller
/// needs it next (a freshly started session's Node process, an OAuth helper
/// reading a client ID) picks it up without an app restart. Shared by every
/// `.env`-backed setting — `ANTHROPIC_API_KEY`, `GOOGLE_OAUTH_CLIENT_ID`,
/// and any future one — rather than forking the same read-modify-write
/// logic per key.
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

    std::fs::write(&env_path, lines.join("\n") + "\n").map_err(|e| format!("failed to write .env: {e}"))?;

    // SAFETY: this only races with another thread concurrently *reading*
    // this env var (spawn_node_agent does, for ANTHROPIC_API_KEY, when
    // starting a session) — a rare, user-initiated, single-value update, not
    // a pattern that's realistically hit concurrently in this app's usage.
    unsafe {
        std::env::set_var(key, value);
    }

    Ok(())
}

// ---------------------------------------------------------------------
// Test-only accessors — reports whether a session id's processes are still
// alive (and, via the returned PIDs, their identity) without any other
// module reaching into PID files directly. Replaces the old
// `docker inspect`-based `container_id`/`wait_for_container_removed` test
// helpers in session.rs/automation.rs.
// ---------------------------------------------------------------------

#[cfg(test)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SessionProcessSnapshot {
    pub node_pid: Option<u32>,
    pub pinchtab_pid: Option<u32>,
}

#[cfg(test)]
pub(crate) async fn session_process_snapshot(session_id: &str) -> Option<SessionProcessSnapshot> {
    let mut sessions = SESSIONS.lock().await;
    let session = sessions.get_mut(session_id)?;
    let node_alive = matches!(session.node_child.try_wait(), Ok(None));
    let pinchtab_alive = matches!(session.pinchtab_child.try_wait(), Ok(None));
    if !node_alive || !pinchtab_alive {
        return None;
    }
    Some(SessionProcessSnapshot {
        node_pid: session.node_child.id(),
        pinchtab_pid: session.pinchtab_child.id(),
    })
}

// ---------------------------------------------------------------------
// Bundled resource resolution — real filesystem checks against whatever
// `scripts/fetch-sidecars.sh` has actually fetched, not mocks. `resource_dir()`
// falls back to `<CARGO_MANIFEST_DIR>/resources` in every `cargo test` run
// (no test ever calls `init_resource_dir`, matching `data_dir()`'s
// equivalent fallback) — exactly where that script writes its output during
// local development, so these exercise the *real* production code path.
// `bundled_sidecar_path`'s own half (pinchtab/node) depends on
// `current_exe()` living inside a real bundle's `Contents/MacOS/`, which a
// plain `cargo test` binary never does — verified separately against a real
// `tauri build` output (see this pass's implementation notes) rather than
// here.
// ---------------------------------------------------------------------

#[cfg(test)]
mod bundled_resource_resolution_tests {
    use super::*;

    #[test]
    fn bundled_chromium_binary_resolves_a_real_fetched_build() {
        let Some(path) = bundled_chromium_binary() else {
            eprintln!(
                "skipping: no bundled chromium found at {} — run scripts/fetch-sidecars.sh first",
                resource_dir().join("chromium").display()
            );
            return;
        };
        assert!(path.is_file(), "resolved chromium path {} should exist", path.display());
        assert!(
            path.to_string_lossy().contains("Google Chrome for Testing"),
            "expected the Chrome for Testing executable, got {}",
            path.display()
        );
    }

    #[test]
    fn bundled_agent_entry_point_exists_after_fetch_sidecars() {
        let entry = resource_dir().join("agent").join("dist").join("server.js");
        if !entry.is_file() {
            eprintln!(
                "skipping: no bundled agent entry point at {} — run scripts/fetch-sidecars.sh first",
                entry.display()
            );
            return;
        }
        assert!(entry.is_file());
    }
}
