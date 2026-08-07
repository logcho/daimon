#!/usr/bin/env bash
# Build-prep step for `tauri build` (or `tauri dev` against a real bundled
# resource dir): fetches and checksum-verifies every native binary/resource
# the production app needs so it can run as a single download with no Docker
# and no system Node.
#
# Deliberately a separate script rather than folded into `beforeBuildCommand`
# in tauri.conf.json (which already just handles the frontend build) — see
# the plan's §4. Run this once before `tauri build`/`tauri dev` whenever
# `src-tauri/binaries/`, `src-tauri/resources/`, or `agents/src` change;
# everything it produces is gitignored, not source.
#
# What it does, in order:
#   1. Fetches a pinned `pinchtab` release binary from GitHub Releases,
#      checksum-verified against that release's own published checksums.txt.
#   2. Fetches a pinned Node.js binary from nodejs.org/dist, checksum-verified
#      against the matching SHASUMS256.txt.
#   3. Fetches a static `ffmpeg` build (checksum-verified against a hash
#      pinned in this script — see the FFMPEG_SHA256_* comment below for
#      provenance), needed for PinchTab's WebM/MP4 recording encode step
#      (`internal/handlers/record_encode.go` shells out to a bare `ffmpeg`
#      resolved via `$PATH` — see `workspace.rs`'s `spawn_pinchtab_server`
#      doc comment for how the bundled binary's directory gets prepended to
#      the spawned PinchTab process's `PATH`).
#   4. Builds agents/ to plain compiled JS (`tsc`) and produces a pruned,
#      production-only copy of agents/node_modules (`npm ci --omit=dev`,
#      including the platform-correct `better-sqlite3` prebuild for whatever
#      machine this script runs on).
#
# Deliberately NOT fetched here: the pinned Chromium ("Chrome for Testing")
# build PinchTab drives — that used to be step 3 (fetched from Playwright's
# CDN and bundled straight into the installer via `tauri.conf.json`'s
# `bundle.resources`), but bundling it added ~491MB to every install (755MB
# total) and fought the whole "single lightweight download" reason Tauri was
# chosen over Electron in the first place. It's now downloaded on first real
# use instead, into the app-data directory, the same way `voice.rs` already
# downloads the whisper model on first use — see `workspace.rs`'s
# `get_chromium_status`/`download_chromium`/`CHROMIUM_VERSION`/
# `CHROMIUM_SHA256_MAC_ARM64` for the exact same pinned version/checksum this
# script used to fetch, now living there instead. (If a dev checkout still
# has `src-tauri/resources/chromium/` populated from before this change, it's
# left alone as a harmless fallback — see `bundled_chromium_binary` — rather
# than actively cleaned up here.)
#
# macOS-only for now, matching the plan's decision #1 (macOS-first; Windows/
# Linux deferred). Requires: curl, unzip, shasum (all present on macOS by
# default), npm/node on PATH (for the agents/ build step only — the fetched
# Node binary is for the *bundled app* to run agents/dist with, not for
# running this script).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BINARIES_DIR="$REPO_ROOT/src-tauri/binaries"
RESOURCES_DIR="$REPO_ROOT/src-tauri/resources"
AGENTS_DIR="$REPO_ROOT/agents"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

log() { echo "[fetch-sidecars] $*" >&2; }
fail() { echo "[fetch-sidecars] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------
# Pinned versions — bump deliberately, not silently. Each was validated
# empirically (see this repo's implementation notes for
# can-you-look-into-eager-shell.md) before being pinned here.
#
# (The pinned Chromium/"Chrome for Testing" version+checksum used to live
# here too — it's now in `workspace.rs`'s `CHROMIUM_VERSION`/
# `CHROMIUM_SHA256_MAC_ARM64`, since that's the only place that fetches it
# now. See this file's header comment.)
# ---------------------------------------------------------------------
PINCHTAB_VERSION="${PINCHTAB_VERSION:-v0.15.0}"
NODE_VERSION="${NODE_VERSION:-v24.18.0}"

# ---------------------------------------------------------------------
# Target architecture — defaults to the host running this script (macOS-first
# per the plan's decision #1; only the host arch has actually been validated
# end-to-end). TARGET_ARCH=x64 is wired up for pinchtab/node/ffmpeg but NOT
# independently verified end-to-end on this arm64 dev machine.
# ---------------------------------------------------------------------
host_arch="$(uname -m)"
case "${TARGET_ARCH:-$host_arch}" in
  arm64|aarch64)
    TARGET_ARCH="arm64"
    RUST_TRIPLE="aarch64-apple-darwin"
    PINCHTAB_ARCH="arm64"
    NODE_ARCH="arm64"
    ;;
  x64|x86_64)
    TARGET_ARCH="x64"
    RUST_TRIPLE="x86_64-apple-darwin"
    PINCHTAB_ARCH="amd64"
    NODE_ARCH="x64"
    ;;
  *)
    fail "unsupported TARGET_ARCH/uname -m: ${TARGET_ARCH:-$host_arch} (this script only knows arm64/x64 macOS)"
    ;;
esac
log "target arch: $TARGET_ARCH (rustc triple $RUST_TRIPLE)"

# sha256 of the *extracted* `ffmpeg` binary (not the `.zip` it ships in) from
# osxexperts.net's static macOS builds — published directly on their
# download page per-build, independently reproduced here from a real
# download (`shasum -a 256` against the extracted binary matched exactly)
# before being pinned. Not evermeet.cx (ffmpeg.org's own only officially
# linked macOS source): confirmed by downloading a real evermeet.cx build and
# running `lipo -info` against it that it ships x86_64-only, no arm64 build
# at all ("static FFmpeg binaries for macOS 64-bit Intel" per evermeet.cx's
# own page) — no use bundling a binary that can't run natively on the same
# Apple Silicon machines the pinned Chromium build already targets.
# osxexperts.net is a second, deliberately chosen source, not a blindly
# trusted download — same trust-on-first-use posture as
# `workspace.rs`'s `CHROMIUM_SHA256_MAC_ARM64`. Re-verify (re-download,
# `shasum -a 256`, confirm a real spawned session's recording actually
# encodes to a playable file) before bumping either version below.
FFMPEG_VERSION_ARM="${FFMPEG_VERSION_ARM:-8.1}"
FFMPEG_VERSION_X64="${FFMPEG_VERSION_X64:-8.0}"
FFMPEG_SHA256_MAC_ARM64="9a08d61f9328e8164ba560ee7a79958e357307fcfeea6fe626b7d66cdc287028"
FFMPEG_SHA256_MAC_X64="df3f1e3facdc1ae0ad0bd898cdfb072fbc9641bf47b11f172844525a05db8d11"

mkdir -p "$BINARIES_DIR" "$RESOURCES_DIR"

# Persistent (gitignored) cache of the *full* extracted Node distribution,
# not just the `bin/node` sidecar copy — build_agent() below needs the
# matching `npm` from inside this same distribution (its
# lib/node_modules/npm/bin/npm-cli.js), not whatever `npm` happens to be on
# this machine's PATH. Running `npm ci --omit=dev` under a *different* Node
# than the one the app will actually ship broke this in practice: npm's
# native-module install step (prebuild-install, for better-sqlite3) detects
# the target Node ABI from the Node interpreter actually running it, so a
# mismatched host npm/Node produces a prebuild for the *host's* Node version
# (confirmed empirically — a fully-built bundle failed at startup with
# `NODE_MODULE_VERSION 131` vs `137` ERR_DLOPEN_FAILED, host Node v23 vs
# bundled Node v24). Kept outside src-tauri/ entirely so nothing here is
# mistaken for a shippable resource.
NODE_CACHE_DIR="$REPO_ROOT/.cache/node-toolchain/$RUST_TRIPLE"

# ---------------------------------------------------------------------
# 1. PinchTab
# ---------------------------------------------------------------------
fetch_pinchtab() {
  local dest="$BINARIES_DIR/pinchtab-$RUST_TRIPLE"
  if [ -f "$dest" ]; then
    log "pinchtab: $dest already exists, skipping (delete it to re-fetch)"
    return
  fi
  log "pinchtab: fetching $PINCHTAB_VERSION for darwin-$PINCHTAB_ARCH"
  local base="https://github.com/pinchtab/pinchtab/releases/download/$PINCHTAB_VERSION"
  curl -fsSL "$base/checksums.txt" -o "$WORK_DIR/pinchtab-checksums.txt"
  curl -fsSL "$base/pinchtab-darwin-$PINCHTAB_ARCH" -o "$WORK_DIR/pinchtab-bin"

  local expected
  expected="$(grep " pinchtab-darwin-$PINCHTAB_ARCH\$" "$WORK_DIR/pinchtab-checksums.txt" | awk '{print $1}')"
  [ -n "$expected" ] || fail "pinchtab: no checksum entry found for pinchtab-darwin-$PINCHTAB_ARCH in checksums.txt"
  local actual
  actual="$(shasum -a 256 "$WORK_DIR/pinchtab-bin" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || fail "pinchtab: checksum mismatch (expected $expected, got $actual)"

  cp "$WORK_DIR/pinchtab-bin" "$dest"
  chmod +x "$dest"
  log "pinchtab: verified and installed at $dest"
}

# ---------------------------------------------------------------------
# 2. Node.js
# ---------------------------------------------------------------------
fetch_node() {
  local dest="$BINARIES_DIR/node-$RUST_TRIPLE"
  if [ -f "$dest" ] && [ -d "$NODE_CACHE_DIR" ]; then
    log "node: $dest already exists, skipping (delete it and $NODE_CACHE_DIR to re-fetch)"
    return
  fi
  log "node: fetching $NODE_VERSION for darwin-$NODE_ARCH"
  local base="https://nodejs.org/dist/$NODE_VERSION"
  local tarball="node-$NODE_VERSION-darwin-$NODE_ARCH.tar.gz"
  curl -fsSL "$base/SHASUMS256.txt" -o "$WORK_DIR/node-SHASUMS256.txt"
  curl -fsSL "$base/$tarball" -o "$WORK_DIR/$tarball"

  local expected
  expected="$(grep " $tarball\$" "$WORK_DIR/node-SHASUMS256.txt" | awk '{print $1}')"
  [ -n "$expected" ] || fail "node: no checksum entry found for $tarball in SHASUMS256.txt"
  local actual
  actual="$(shasum -a 256 "$WORK_DIR/$tarball" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || fail "node: checksum mismatch (expected $expected, got $actual)"

  tar xzf "$WORK_DIR/$tarball" -C "$WORK_DIR"
  rm -rf "$NODE_CACHE_DIR"
  mkdir -p "$(dirname "$NODE_CACHE_DIR")"
  mv "$WORK_DIR/node-$NODE_VERSION-darwin-$NODE_ARCH" "$NODE_CACHE_DIR"

  cp "$NODE_CACHE_DIR/bin/node" "$dest"
  chmod +x "$dest"
  log "node: verified and installed at $dest (full toolchain cached at $NODE_CACHE_DIR for build_agent's npm)"
}

# ---------------------------------------------------------------------
# 3. ffmpeg (static build, needed for PinchTab's recording encode step — see
# this file's header comment and workspace.rs's `spawn_pinchtab_server`)
# ---------------------------------------------------------------------
fetch_ffmpeg() {
  local dest="$BINARIES_DIR/ffmpeg-$RUST_TRIPLE"
  if [ -f "$dest" ]; then
    log "ffmpeg: $dest already exists, skipping (delete it to re-fetch)"
    return
  fi

  local url version expected
  case "$TARGET_ARCH" in
    arm64)
      version="$FFMPEG_VERSION_ARM"
      url="https://www.osxexperts.net/ffmpeg${FFMPEG_VERSION_ARM/./}arm.zip"
      expected="$FFMPEG_SHA256_MAC_ARM64"
      ;;
    x64)
      version="$FFMPEG_VERSION_X64"
      url="https://www.osxexperts.net/ffmpeg${FFMPEG_VERSION_X64/./}intel.zip"
      expected="$FFMPEG_SHA256_MAC_X64"
      ;;
  esac

  log "ffmpeg: fetching static build $version for $TARGET_ARCH from osxexperts.net"
  local zip="$WORK_DIR/ffmpeg-$TARGET_ARCH.zip"
  curl -fsSL "$url" -o "$zip"

  local extract_dir="$WORK_DIR/ffmpeg-extract-$TARGET_ARCH"
  mkdir -p "$extract_dir"
  unzip -q "$zip" -d "$extract_dir"
  [ -f "$extract_dir/ffmpeg" ] || fail "ffmpeg: expected an extracted 'ffmpeg' binary at $extract_dir/ffmpeg, found none"

  local actual
  actual="$(shasum -a 256 "$extract_dir/ffmpeg" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || fail "ffmpeg: checksum mismatch for $TARGET_ARCH build (expected $expected, got $actual) — osxexperts.net may have updated the build at this URL; re-verify by hand and re-pin FFMPEG_SHA256_* before proceeding"

  cp "$extract_dir/ffmpeg" "$dest"
  chmod +x "$dest"
  # Ad-hoc-sign so a freshly fetched binary runs under Gatekeeper without a
  # manual `xattr -cr`/`codesign -s -` step — osxexperts.net's own download
  # page instructs users to do this by hand for an interactive download;
  # automated here since this is unattended build-time fetching.
  xattr -cr "$dest" 2>/dev/null || true
  codesign -s - "$dest" 2>/dev/null || true
  log "ffmpeg: verified and installed at $dest"
}

# ---------------------------------------------------------------------
# 4. agents/ production build (tsc — see the tsc-vs-tsup verification notes
# for why plain tsc output was confirmed sufficient, no bundler needed)
# ---------------------------------------------------------------------
build_agent() {
  local dest="$RESOURCES_DIR/agent"
  [ -d "$NODE_CACHE_DIR" ] || fail "agent: $NODE_CACHE_DIR not found — fetch_node must run before build_agent"
  local bundled_node="$NODE_CACHE_DIR/bin/node"
  local bundled_npm_cli="$NODE_CACHE_DIR/lib/node_modules/npm/bin/npm-cli.js"

  log "agent: installing full deps in $AGENTS_DIR (npm ci, host toolchain — dev-only, not shipped)"
  (cd "$AGENTS_DIR" && npm ci)

  log "agent: compiling TypeScript (npm run build)"
  rm -rf "$AGENTS_DIR/dist"
  (cd "$AGENTS_DIR" && npm run build)

  log "agent: assembling pruned production bundle at $dest"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -R "$AGENTS_DIR/dist" "$dest/dist"
  cp "$AGENTS_DIR/package.json" "$AGENTS_DIR/package-lock.json" "$dest/"

  # Run inside $dest (not $AGENTS_DIR) so this doesn't disturb the full dev
  # node_modules developers actually use for `npm run typecheck`/tests.
  #
  # Two things had to be true together for this to actually produce a
  # correctly-ABI'd better-sqlite3 native build, confirmed the hard way (the
  # built app crashed at startup with `ERR_DLOPEN_FAILED` /
  # `NODE_MODULE_VERSION 131 vs 137` the first two times this was tried):
  #
  # 1. Run via the *bundled* Node binary executing its own bundled
  #    npm-cli.js (both from $NODE_CACHE_DIR), not this machine's global
  #    `npm` on PATH — otherwise npm itself resolves against the host Node.
  # 2. That alone is still NOT sufficient: node-gyp/prebuild-install (the
  #    lifecycle install script better-sqlite3 runs) does not infer the
  #    target ABI from "whichever node executed npm" — confirmed empirically
  #    it instead fell back to node-gyp's own locally cached
  #    `~/Library/Caches/node-gyp/<host-nvm-version>` headers, silently
  #    rebuilding against the *host's* Node ABI regardless of which npm ran
  #    it. The standard cross-target override (the same mechanism
  #    Electron-targeting native modules use to build against a different
  #    Node/Electron ABI than the one running npm) is these npm_config_*
  #    env vars — `target`/`target_arch`/`target_platform` tell
  #    prebuild-install/node-gyp which ABI to build for regardless of the
  #    running interpreter, and `build_from_source=true` skips
  #    prebuild-install's own prebuilt-binary download attempt entirely
  #    (there may not even be a published prebuild yet for a Node version
  #    this new) and goes straight to a real node-gyp compile against the
  #    correct target headers.
  log "agent: pruning to production-only deps (npm ci --omit=dev, via the bundled Node's own npm, targeted at the bundled Node's ABI)"
  (cd "$dest" && env \
    npm_config_target="${NODE_VERSION#v}" \
    npm_config_target_arch="$NODE_ARCH" \
    npm_config_target_platform="darwin" \
    npm_config_arch="$NODE_ARCH" \
    npm_config_disturl="https://nodejs.org/dist" \
    npm_config_runtime="node" \
    npm_config_build_from_source="true" \
    "$bundled_node" "$bundled_npm_cli" ci --omit=dev)

  log "agent: production bundle ready at $dest"
}

fetch_pinchtab
fetch_node
fetch_ffmpeg
build_agent

log "done. src-tauri/binaries/ and src-tauri/resources/ are ready for 'npm run tauri build'."
