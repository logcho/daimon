//! Runtime discovery of the repo root, the `agents/` directory the app
//! spawns its agent server from, and the `uv` binary that runs it.
//!
//! `env!("CARGO_MANIFEST_DIR")` is baked in at *compile* time: it survives
//! in the built binary as the absolute path of whoever built it, so a dev
//! build or a bundled .app keeps pointing at the build-time location
//! forever, even after the whole project folder is moved. This instead
//! walks up from `std::env::current_exe()` at *runtime*, so it tracks
//! wherever the binary (and the repo it's nested inside, for dev/local
//! builds) actually is right now.
//!
//! The `uv` lookup exists for a related reason: a GUI-launched .app does
//! *not* inherit your shell's PATH. Finder, the Dock and Spotlight hand it
//! only launchd's minimal default (`/usr/bin:/bin:/usr/sbin:/sbin`), so the
//! `uv` a terminal happily finds at `~/.local/bin/uv` is invisible to the
//! app, and spawning a bare `"uv"` fails with "No such file or directory".
//! (Launching via `open` from a terminal *does* pass the shell environment
//! through — which is exactly why this failure hides from that kind of
//! testing and only shows up on a real Dock/Finder launch.)

use std::ffi::OsString;
use std::path::{Path, PathBuf};

/// Ancestor levels of the running executable to check before giving up.
/// Generous enough to cover `cargo tauri dev` (target/debug/daimon-app) and
/// a local `cargo tauri build` bundle nested under
/// target/release/bundle/macos/daimon.app/Contents/MacOS/, but bounded so
/// the search can't wander indefinitely.
const MAX_ANCESTOR_SEARCH: usize = 12;

/// Best-effort discovery of the checkout root containing this build — the
/// directory with both an `app/` and an `agents/pyproject.toml`. Returns
/// `None` when the running binary isn't nested inside a checkout at all,
/// which is the normal case for an installed /Applications/daimon.app:
/// there is nothing to discover there, and the caller must fall back to an
/// explicit override rather than guessing.
pub fn discover_repo_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let mut dir = exe.parent()?;
    for _ in 0..MAX_ANCESTOR_SEARCH {
        if looks_like_repo_root(dir) {
            return Some(dir.to_path_buf());
        }
        dir = dir.parent()?;
    }
    None
}

fn looks_like_repo_root(dir: &Path) -> bool {
    dir.join("agents").join("pyproject.toml").is_file() && dir.join("app").is_dir()
}

/// The `agents/` directory to spawn `uv run daimon-agent` in.
/// `DAIMON_AGENT_DIR` always wins; otherwise it's discovered relative to the
/// running binary. `None` means "couldn't find it" — the caller must
/// surface that rather than guessing a path that doesn't exist.
pub fn agents_dir() -> Option<PathBuf> {
    std::env::var_os("DAIMON_AGENT_DIR")
        .map(PathBuf::from)
        .or_else(|| discover_repo_root().map(|root| root.join("agents")))
}

/// User-level bin directories a GUI launch can't see but where `uv` (and the
/// tools the agent itself shells out to) are actually installed. Ordered by
/// how likely they are to hold the copy the user means.
fn user_bin_dirs() -> Vec<PathBuf> {
    let home = dirs_next::home_dir();
    let mut dirs = Vec::new();
    if let Some(home) = &home {
        dirs.push(home.join(".local").join("bin")); // uv's own default installer
        dirs.push(home.join(".cargo").join("bin"));
    }
    dirs.push(PathBuf::from("/opt/homebrew/bin")); // Apple Silicon Homebrew
    dirs.push(PathBuf::from("/usr/local/bin")); // Intel Homebrew / manual installs
    dirs
}

/// First match for `name` in the inherited PATH, if any.
fn find_on_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
}

/// Absolute path to the `uv` binary that runs the agent server, or `None`
/// when it genuinely isn't installed anywhere we know to look.
///
/// Checked in order: `DAIMON_UV_BIN` (explicit override), the inherited
/// PATH (covers `cargo tauri dev` and `open` from a terminal), then the
/// well-known user bin dirs above — which is what rescues a Dock/Finder
/// launch, where PATH holds none of them. Resolving to an absolute path up
/// front means the spawn never depends on the child's PATH at all.
pub fn uv_bin() -> Option<PathBuf> {
    if let Some(explicit) = std::env::var_os("DAIMON_UV_BIN").map(PathBuf::from) {
        if explicit.is_file() {
            return Some(explicit);
        }
    }
    find_on_path("uv").or_else(|| {
        user_bin_dirs()
            .into_iter()
            .map(|dir| dir.join("uv"))
            .find(|candidate| candidate.is_file())
    })
}

/// PATH to hand the spawned agent: the inherited one with the user bin dirs
/// prepended. `uv` itself is invoked by absolute path, but everything
/// *downstream* of it still resolves through PATH — the agent shells out to
/// `bash`, and `scripts/setup-pinchtab.sh` looks up its own binaries — so a
/// Dock-launched app would otherwise hand the agent the same crippled PATH
/// that broke `uv`.
pub fn augmented_path() -> OsString {
    let inherited = std::env::var_os("PATH").unwrap_or_default();
    let mut dirs: Vec<PathBuf> = user_bin_dirs();
    // Keep the inherited entries, minus any we're already prepending, so the
    // result has no duplicates but loses nothing.
    for dir in std::env::split_paths(&inherited) {
        if !dirs.contains(&dir) {
            dirs.push(dir);
        }
    }
    std::env::join_paths(dirs).unwrap_or(inherited)
}

/// The `daimon-remote` executable, installed alongside `daimon` by
/// `uv tool install`. Resolved absolutely for the same reason `uv` is: a
/// Dock launch inherits launchd's minimal PATH, which has no ~/.local/bin.
pub fn remote_bin() -> Option<PathBuf> {
    let candidates = [
        dirs_next::home_dir().map(|h| h.join(".local/bin/daimon-remote")),
        Some(PathBuf::from("/opt/homebrew/bin/daimon-remote")),
        Some(PathBuf::from("/usr/local/bin/daimon-remote")),
    ];
    candidates.into_iter().flatten().find(|p| p.is_file())
}
