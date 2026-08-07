#!/usr/bin/env bash
# Sets up and supervises a local PinchTab server for the agent harness.
#
#   scripts/setup-pinchtab.sh start   # copy binary, configure a fresh profile, spawn, write .daimon/pinchtab.env
#   scripts/setup-pinchtab.sh stop    # graceful `pinchtab server stop`
#   scripts/setup-pinchtab.sh status
#
# PinchTab knowledge lives only here and in src/daimon_agent/browser.py.
# Env overrides: PINCHTAB_BIN, DAIMON_CHROME_BIN, PINCHTAB_PORT.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Repo root is one level above the agents tree (legacy/ lives there).
ROOT="$(dirname "$REPO_ROOT")"
DAIMON_DIR="$REPO_ROOT/.daimon"
BIN_DIR="$DAIMON_DIR/bin"
PROFILE_DIR="$DAIMON_DIR/pinchtab-profiles/dev"
ENV_FILE="$DAIMON_DIR/pinchtab.env"
PID_FILE="$DAIMON_DIR/pinchtab.pid"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$BIN_DIR" "$PROFILE_DIR" "$LOG_DIR"

# --- Resolve the pinchtab binary (DAIMON_PINCHTAB_BIN > legacy bundle > PATH).
if [[ -n "${PINCHTAB_BIN:-}" ]]; then
    PINCHTAB="$PINCHTAB_BIN"
elif [[ -f "$BIN_DIR/pinchtab" ]]; then
    PINCHTAB="$BIN_DIR/pinchtab"
elif [[ -f "$ROOT/legacy/src-tauri/binaries/pinchtab-aarch64-apple-darwin" ]]; then
    cp "$ROOT/legacy/src-tauri/binaries/pinchtab-aarch64-apple-darwin" "$BIN_DIR/pinchtab"
    chmod +x "$BIN_DIR/pinchtab"
    PINCHTAB="$BIN_DIR/pinchtab"
else
    PINCHTAB="pinchtab"
fi
"$PINCHTAB" --version >/dev/null 2>&1 || { echo "pinchtab binary not usable: $PINCHTAB" >&2; exit 1; }
echo "pinchtab: $($PINCHTAB --version)"

# --- Resolve Chrome (DAIMON_CHROME_BIN > real Chrome app).
if [[ -n "${DAIMON_CHROME_BIN:-}" ]]; then
    CHROME="$DAIMON_CHROME_BIN"
elif [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
    CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
else
    echo "no Chrome found — set DAIMON_CHROME_BIN" >&2
    exit 1
fi
echo "chrome: $CHROME"

# --- Env for every pinchtab CLI call: profile dir as XDG_CONFIG_HOME/HOME.
pinchtab_cli() {
    XDG_CONFIG_HOME="$PROFILE_DIR" HOME="$PROFILE_DIR" "$PINCHTAB" "$@"
}

start() {
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "pinchtab already running (pid $(cat "$PID_FILE"))"
        return 0
    fi
    PORT="${PINCHTAB_PORT:-$((20000 + RANDOM % 20000))}"
    TOKEN="$(uuidgen)"

    pinchtab_cli config init
    pinchtab_cli config set server.bind 127.0.0.1
    pinchtab_cli config set server.token "$TOKEN"
    pinchtab_cli config set server.port "$PORT"
    pinchtab_cli config set security.allowScreencast true
    pinchtab_cli config set security.allowedDomains '*'
    pinchtab_cli config set browser.binary "$CHROME"

    # Spawn as a background process-group leader; logs to logs/pinchtab.log.
    XDG_CONFIG_HOME="$PROFILE_DIR" HOME="$PROFILE_DIR" \
        "$PINCHTAB" server >"$LOG_DIR/pinchtab.log" 2>&1 &
    echo $! > "$PID_FILE"

    for _ in $(seq 1 60); do
        if pinchtab_cli health >/dev/null 2>&1; then
            cat > "$ENV_FILE" <<EOF
PINCHTAB_BASE=http://127.0.0.1:$PORT
PINCHTAB_TOKEN=$TOKEN
EOF
            echo "pinchtab healthy on port $PORT (env written to $ENV_FILE)"
            return 0
        fi
        sleep 0.5
    done
    echo "pinchtab did not become healthy in time — see $LOG_DIR/pinchtab.log" >&2
    exit 1
}

stop() {
    if [[ -f "$PID_FILE" ]]; then
        pinchtab_cli server stop >/dev/null 2>&1 || true
        rm -f "$PID_FILE" "$ENV_FILE"
        echo "pinchtab stopped"
    else
        echo "pinchtab not running"
    fi
}

status() {
    if pinchtab_cli health >/dev/null 2>&1; then
        echo "pinchtab: healthy"
    else
        echo "pinchtab: not healthy"
        exit 1
    fi
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) echo "usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
